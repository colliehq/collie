"""Adapters between Collie's real local memory/policy stores and Online objects."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from .memory import RECALLABLE_STATUSES, SqliteMemory
from .online import DataClass, OnlineClient, OnlineStore
from .procedure_memory import ProcedureMemory


POLICY_KEYS = ("MISSION_APPROVAL_MODE", "BROWSER_SITE_ACCESS", "BROWSER_SENSITIVE_HOSTS")


def _same(store: OnlineStore, object_id: str, content, data_class: DataClass) -> bool:
    current = store.get(object_id)
    return bool(current and not current.tombstone and current.data_class is data_class and
                current.content == content)


def _stage_policy(store: OnlineStore, project_id: str) -> int:
    from . import settings
    values = settings.all_values()
    content = {"version": 1, "values": {key: values.get(key, "") for key in POLICY_KEYS},
               "cloud_llm": "off"}
    object_id = "policy:%s:baseline" % project_id
    if _same(store, object_id, content, DataClass.CLOUD_INDEXED):
        return 0
    store.put(object_id=object_id, object_type="policy", project_id=project_id,
              data_class=DataClass.CLOUD_INDEXED, content=content)
    return 1


def _stage_memory(store: OnlineStore, binding: dict) -> int:
    path, local_project, project_id = (binding["memory_db"], binding["local_project"],
                                       binding["project_id"])
    if not os.path.isfile(path):
        return 0
    profile = store.profile()
    memory = SqliteMemory(path, embedder=None)
    changed = 0
    try:
        claims = []
        for status in RECALLABLE_STATUSES:
            claims.extend(memory.list_claims(status=status, project=local_project, limit=1000,
                                              allowed_scopes=[local_project]))
        for claim in claims:
            # A personal/global scope never becomes team/project memory by accident.
            if str(claim.get("scope") or "") != local_project:
                continue
            object_id = "memory:%s:%s" % (profile.device_id, claim["id"])
            content = {key: claim.get(key) for key in
                       ("text", "keys", "importance", "created_at", "status", "evidence",
                        "provenance", "scope", "review_source", "review_evidence")}
            content.update(version=1, origin_user_id=profile.user_id,
                           origin_device_id=profile.device_id, local_project=local_project)
            data_class = DataClass(binding["memory_data_class"])
            if _same(store, object_id, content, data_class):
                continue
            store.put(object_id=object_id, object_type="memory", project_id=project_id,
                      data_class=data_class, content=content)
            changed += 1
    finally:
        memory.close()
    return changed


def _procedure_path(binding: dict) -> str:
    memory_path = os.path.abspath(binding["memory_db"])
    parent = os.path.dirname(memory_path)
    state = os.path.dirname(parent) if os.path.basename(parent).lower() == "data" else parent
    return os.path.join(state, "procedural-memory.db")


def _stage_procedures(store: OnlineStore, binding: dict) -> dict:
    """Stage sealed derivatives only. There is intentionally no raw-event loop."""
    path = _procedure_path(binding)
    if not os.path.isfile(path):
        return {"procedure_candidates": 0, "learned_workflows": 0}
    profile = store.profile()
    procedures = ProcedureMemory(path)
    changed = {"procedure_candidates": 0, "learned_workflows": 0}
    try:
        local_cwd = os.path.abspath(binding.get("cwd") or "")
        candidates = procedures.list_candidates(project=local_cwd) if local_cwd else []
        workflows = procedures.list_workflows(project=local_cwd) if local_cwd else []
        for candidate in candidates:
            object_id = "procedure-candidate:%s:%s" % (
                profile.device_id, candidate["candidate_id"])
            content = {key: candidate.get(key) for key in (
                "candidate_id", "title", "summary", "sequence", "support", "confidence",
                "status", "created_at", "reviewed_at", "review_note")}
            content.update(version=1, project=binding["local_project"],
                           origin_user_id=profile.user_id,
                           origin_device_id=profile.device_id)
            if _same(store, object_id, content, DataClass.SEALED):
                continue
            store.put(object_id=object_id, object_type="procedure_candidate",
                      project_id=binding["project_id"], data_class=DataClass.SEALED,
                      content=content)
            changed["procedure_candidates"] += 1
        for workflow in workflows:
            object_id = "learned-workflow:%s:%s" % (
                profile.device_id, workflow["workflow_id"])
            content = {key: workflow.get(key) for key in (
                "workflow_id", "candidate_id", "title", "summary", "sequence", "digest",
                "status", "authority_scope", "created_at")}
            content.update(version=1, project=binding["local_project"],
                           origin_user_id=profile.user_id,
                           origin_device_id=profile.device_id)
            if _same(store, object_id, content, DataClass.SEALED):
                continue
            store.put(object_id=object_id, object_type="learned_workflow",
                      project_id=binding["project_id"], data_class=DataClass.SEALED,
                      content=content)
            changed["learned_workflows"] += 1
    finally:
        procedures.close()
    return changed


def stage_local(store: OnlineStore) -> dict:
    if not store.connected():
        return {"policies": 0, "memories": 0, "procedure_candidates": 0,
                "learned_workflows": 0}
    policies = memories = procedure_candidates = learned_workflows = 0
    for binding in store.project_bindings():
        policies += _stage_policy(store, binding["project_id"])
        memories += _stage_memory(store, binding)
        procedures = _stage_procedures(store, binding)
        procedure_candidates += procedures["procedure_candidates"]
        learned_workflows += procedures["learned_workflows"]
    return {"policies": policies, "memories": memories,
            "procedure_candidates": procedure_candidates,
            "learned_workflows": learned_workflows}


def _apply_policy(content: dict) -> bool:
    """Remote policy may only narrow local authority automatically, never broaden it."""
    from . import settings
    remote = content.get("values") if isinstance(content, dict) else {}
    if not isinstance(remote, dict):
        return False
    local = settings.all_values(); updates = {}
    site_rank = {"ask_every_site": 0, "all_except_sensitive": 1, "all_sites": 2}
    rsite, lsite = str(remote.get("BROWSER_SITE_ACCESS") or ""), str(local.get("BROWSER_SITE_ACCESS") or "")
    if rsite in site_rank and lsite in site_rank and site_rank[rsite] < site_rank[lsite]:
        updates["BROWSER_SITE_ACCESS"] = rsite
    if str(remote.get("MISSION_APPROVAL_MODE") or "") == "review" and \
            str(local.get("MISSION_APPROVAL_MODE") or "") != "review":
        updates["MISSION_APPROVAL_MODE"] = "review"
    local_hosts = {x.strip() for x in str(local.get("BROWSER_SENSITIVE_HOSTS") or "").split(",") if x.strip()}
    remote_hosts = {x.strip() for x in str(remote.get("BROWSER_SENSITIVE_HOSTS") or "").split(",") if x.strip()}
    union = sorted(local_hosts | remote_hosts)
    if union != sorted(local_hosts):
        updates["BROWSER_SENSITIVE_HOSTS"] = ",".join(union)
    if updates:
        settings.update(updates); settings.apply()
    return bool(updates)


def _imports_table(store: OnlineStore) -> None:
    store.db.execute("""CREATE TABLE IF NOT EXISTS online_memory_imports(
      object_id TEXT PRIMARY KEY, version INTEGER NOT NULL, local_memory_id INTEGER NOT NULL,
      local_project TEXT NOT NULL, updated_at INTEGER NOT NULL)""")
    store.db.execute("""CREATE TABLE IF NOT EXISTS online_procedure_imports(
      object_id TEXT PRIMARY KEY, version INTEGER NOT NULL, object_type TEXT NOT NULL,
      local_project TEXT NOT NULL, updated_at INTEGER NOT NULL)""")
    store.db.commit()


def apply_remote(store: OnlineStore) -> dict:
    _imports_table(store)
    profile = store.profile(); imported = invalidated = policies = routines = workflows = 0
    for binding in store.project_bindings():
        project_id, local_project = binding["project_id"], binding["local_project"]
        for obj in store.list_objects(project_id=project_id, include_deleted=True):
            if obj.object_type == "policy" and not obj.tombstone:
                policies += int(_apply_policy(obj.content if isinstance(obj.content, dict) else {}))
                continue
            if obj.object_type in ("procedure_candidate", "learned_workflow"):
                prior = store.db.execute(
                    "SELECT version FROM online_procedure_imports WHERE object_id=?",
                    (obj.object_id,)).fetchone()
                if prior and int(prior["version"]) >= obj.version:
                    continue
                if obj.tombstone:
                    store.db.execute("DELETE FROM online_procedure_imports WHERE object_id=?",
                                     (obj.object_id,))
                    store.db.commit()
                    continue
                content = obj.content if isinstance(obj.content, dict) else {}
                # Personal behavior models never become team knowledge.
                if str(content.get("origin_user_id") or "") != profile.user_id:
                    continue
                local = dict(content)
                local["project"] = binding.get("cwd") or local_project
                procedures = ProcedureMemory(_procedure_path(binding))
                try:
                    if obj.object_type == "procedure_candidate":
                        procedures.upsert_synced_candidate(local)
                        routines += 1
                    else:
                        # Authority is forced to none again by the local import seam.
                        procedures.upsert_synced_workflow(local)
                        workflows += 1
                finally:
                    procedures.close()
                store.db.execute("""INSERT INTO online_procedure_imports(
                    object_id,version,object_type,local_project,updated_at) VALUES(
                    ?,?,?,?,strftime('%s','now')) ON CONFLICT(object_id) DO UPDATE SET
                    version=excluded.version,object_type=excluded.object_type,
                    local_project=excluded.local_project,updated_at=excluded.updated_at""",
                    (obj.object_id, obj.version, obj.object_type, local_project))
                store.db.commit()
                continue
            if obj.object_type != "memory":
                continue
            prior = store.db.execute(
                "SELECT * FROM online_memory_imports WHERE object_id=?", (obj.object_id,)).fetchone()
            if prior and int(prior["version"]) >= obj.version:
                continue
            memory = SqliteMemory(binding["memory_db"], embedder=None)
            try:
                if prior:
                    invalidated += int(memory.invalidate(
                        int(prior["local_memory_id"]), evidence="superseded by Online object version %d" % obj.version,
                        review_source="collie-online", review_provenance=obj.object_id))
                if obj.tombstone:
                    store.db.execute("DELETE FROM online_memory_imports WHERE object_id=?", (obj.object_id,))
                    store.db.commit(); continue
                content = obj.content if isinstance(obj.content, dict) else {}
                same_user = str(content.get("origin_user_id") or "") == profile.user_id
                incoming = str(content.get("status") or "active")
                status = incoming if same_user and incoming in RECALLABLE_STATUSES else "proposed"
                local_id = memory.remember(
                    str(content.get("text") or ""), keys=str(content.get("keys") or ""),
                    project=local_project, scope=local_project, status=status,
                    importance=float(content.get("importance") or 0.5), consolidate=True,
                    created_at=int(content.get("created_at") or 0) or None,
                    source="collie-online:%s" % (content.get("origin_user_id") or "team"),
                    evidence=str(content.get("evidence") or "")[:1000],
                    provenance="collie-online:%s" % obj.object_id)
                store.db.execute("""INSERT INTO online_memory_imports(object_id,version,local_memory_id,
                    local_project,updated_at) VALUES(?,?,?,?,strftime('%s','now')) ON CONFLICT(object_id)
                    DO UPDATE SET version=excluded.version,local_memory_id=excluded.local_memory_id,
                    local_project=excluded.local_project,updated_at=excluded.updated_at""",
                    (obj.object_id, obj.version, local_id, local_project))
                store.db.commit(); imported += 1
            finally:
                memory.close()
    return {"memories_imported": imported, "memories_invalidated": invalidated,
            "policies_narrowed": policies, "procedure_candidates_imported": routines,
            "learned_workflows_imported": workflows}


def sync_all(store: OnlineStore, *, limit: int = 100) -> dict:
    staged = stage_local(store)
    synced = OnlineClient(store).sync_once(limit=limit)
    applied = apply_remote(store)
    return {**synced, "staged": staged, "applied": applied,
            "bindings": len(store.project_bindings())}
