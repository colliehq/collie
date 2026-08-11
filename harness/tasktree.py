"""Durable background run tree and scoped-specialist mailbox.

This is deliberately a backend primitive, not a UI or an agent implementation.
Web/CLI/Harness code can create runs, provision worktrees, claim leases, stream
progress, steer/cancel, and dispatch notifications without keeping correctness in
one process.  Specialist authority is always a deterministic subset of its parent.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time


QUEUED = "queued"
RUNNING = "running"
BLOCKED = "blocked"
WAITING = "waiting"
NEEDS_YOU = "needs_you"
PAUSED = "paused"
COMPLETED = "completed"
FAILED = "failed"
CANCEL_REQUESTED = "cancel_requested"
CANCELLED = "cancelled"
RECOVERY_REQUIRED = "recovery_required"
WORKSPACE_REQUIRED = "workspace_required"
_TERMINAL = {COMPLETED, FAILED, CANCELLED}


_ARTIFACT_REF_KEYS = (
    "kind", "name", "uri", "path", "digest", "media_type", "receipt_id",
    "revision", "size",
)


def _js(value):
    return json.dumps(value if value is not None else {}, ensure_ascii=False,
                      sort_keys=True, default=str)


def _jl(value, default=None):
    try:
        return json.loads(value) if value else ({} if default is None else default)
    except (TypeError, ValueError):
        return {} if default is None else default


def normalize_artifact_refs(values):
    """Keep only bounded references; child output content never rides the mailbox.

    Artifact references are observations, not grants.  In particular, returning a
    path does not add it to the parent's resource ownership.  The parent must still
    pass ``can_access``/its Mission leash before using a tool against that path.
    """
    if values in (None, ""):
        return []
    if isinstance(values, (str, dict)):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    out = []
    for value in values[:12]:
        value = {"uri": value} if isinstance(value, str) else value
        if not isinstance(value, dict):
            continue
        ref = {}
        for key in _ARTIFACT_REF_KEYS:
            item = value.get(key)
            if item in (None, ""):
                continue
            if key in ("revision", "size") and isinstance(item, (int, float)) \
                    and not isinstance(item, bool):
                ref[key] = item
            else:
                ref[key] = str(item)[:2000 if key in ("uri", "path") else 300]
        if ref and any(ref.get(key) for key in ("uri", "path", "receipt_id", "digest")):
            out.append(ref)
    return out


def _covered_capability(name, parent_patterns):
    return any(fnmatch.fnmatchcase(str(name), str(pattern))
               for pattern in (parent_patterns or ()))


def narrow_leash(parent, requested=None):
    """Return a child leash or raise when any requested authority expands parent."""
    parent = dict(parent or {})
    if requested is None:
        child = dict(parent)
        # A specialist gets an isolated filesystem even when the interactive
        # parent was allowed to use cwd. Equal tool/budget limits are ceilings;
        # cumulative usage is charged through every ancestor below.
        if child.get("workspace_mode") == "current":
            child["workspace_mode"] = "isolated"
        return child
    requested = dict(requested or {})
    child = dict(parent)
    unknown = set(requested) - set(parent)
    if unknown:
        raise ValueError("specialist leash introduces parent-unknown authority: %s" %
                         ", ".join(sorted(unknown)))

    if "may" in requested:
        may = requested.get("may")
        if not isinstance(may, (list, tuple)) or not all(
                isinstance(item, str) and _covered_capability(item, parent.get("may"))
                for item in may):
            raise ValueError("specialist capabilities must be covered by parent leash.may")
        child["may"] = sorted(set(may))

    numeric_caps = {
        "spend_max_usd", "max_total_steps", "max_irreversible_actions",
        "actions_per_hour", "max_model_tokens", "max_model_cost_usd",
        "max_active_wall_seconds", "max_elapsed_seconds", "max_step_seconds",
        "max_retries", "max_storage_bytes", "checkpoint_keep",
        "human_escalate_seconds", "human_timeout_seconds", "max_specialists",
        "max_specialist_depth",
    }
    for key, value in requested.items():
        if key == "may":
            continue
        if key in numeric_caps:
            try:
                if float(value) > float(parent[key]):
                    raise ValueError("specialist %s cannot exceed parent (%s > %s)" %
                                     (key, value, parent[key]))
            except (TypeError, ValueError) as exc:
                if isinstance(exc, ValueError) and str(exc).startswith("specialist"):
                    raise
                raise ValueError("specialist %s must be numeric" % key)
            child[key] = value
            continue
        if key == "allowed_domains":
            domains = value or []
            parent_domains = parent.get(key) or []
            if not all(any(fnmatch.fnmatchcase(str(domain), str(pattern))
                           for pattern in parent_domains) for domain in domains):
                raise ValueError("specialist domains must be covered by parent domains")
            child[key] = list(domains)
            continue
        if key == "irreversible":
            order = {"deny": 0, "confirm": 1, "allow": 2}
            if value not in order or order[value] > order.get(parent.get(key, "confirm"), 1):
                raise ValueError("specialist irreversible authority expands parent")
            child[key] = value
            continue
        if key == "workspace_mode":
            # isolated is a restriction of current; the inverse expands scope.
            if value not in ("current", "isolated") or (
                    parent.get(key) == "isolated" and value != "isolated"):
                raise ValueError("specialist workspace mode expands parent")
            child[key] = value
            continue
        if key == "expires":
            if parent.get(key) and str(value) > str(parent[key]):
                raise ValueError("specialist expiry cannot outlive parent")
            child[key] = value
            continue
        if value != parent.get(key):
            raise ValueError("specialist field %s must equal parent" % key)
    return child


def _normalize_resource(item):
    if isinstance(item, str):
        if ":" not in item:
            raise ValueError("resource strings use kind:id")
        kind, ident = item.split(":", 1)
        item = {"kind": kind, "id": ident, "mode": "write"}
    if not isinstance(item, dict):
        raise ValueError("resource must be a mapping")
    kind = str(item.get("kind") or "").strip().lower()
    ident = str(item.get("id") or item.get("path") or "").strip()
    mode = str(item.get("mode") or "write").strip().lower()
    if not kind or not ident or mode not in ("read", "write"):
        raise ValueError("resource needs kind/id and read|write mode")
    if kind == "file":
        ident = os.path.normcase(os.path.realpath(os.path.abspath(ident)))
    return {"kind": kind, "id": ident, "mode": mode}


def normalize_resources(resources):
    out = []
    seen = set()
    for item in resources or ():
        resource = _normalize_resource(item)
        key = (resource["kind"], resource["id"], resource["mode"])
        if key not in seen:
            out.append(resource)
            seen.add(key)
    return out


def _resource_contains(parent, child):
    if parent["kind"] != child["kind"]:
        return False
    if parent["mode"] == "read" and child["mode"] == "write":
        return False
    if parent["kind"] != "file":
        return parent["id"] == child["id"]
    try:
        return os.path.commonpath([parent["id"], child["id"]]) == parent["id"]
    except ValueError:
        return False


class TaskTreeStore:
    """SQLite run tree with durable progress, mailbox and notification outbox."""

    def __init__(self, path=None, hooks=None):
        path = path or os.path.join(
            os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie"),
            "tasktree.db")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.hooks = hooks
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS agent_runs(
            run_id TEXT PRIMARY KEY, parent_run_id TEXT NOT NULL DEFAULT '',
            root_run_id TEXT NOT NULL, mission_id TEXT NOT NULL DEFAULT '',
            depth INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL DEFAULT 'general',
            task TEXT NOT NULL, status TEXT NOT NULL, background INTEGER NOT NULL DEFAULT 0,
            leash_json TEXT NOT NULL, resources_json TEXT NOT NULL DEFAULT '[]',
            workspace_mode TEXT NOT NULL DEFAULT 'worktree', workspace TEXT NOT NULL DEFAULT '',
            owns_workspace INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '',
            owner_token TEXT NOT NULL DEFAULT '', lease_until INTEGER NOT NULL DEFAULT 0,
            progress_seq INTEGER NOT NULL DEFAULT 0, progress_at INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            model_cost_microusd INTEGER NOT NULL DEFAULT 0,
            active_wall_ms INTEGER NOT NULL DEFAULT 0, retry_count INTEGER NOT NULL DEFAULT 0,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            cancel_ack_at INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS agent_runs_parent ON agent_runs(parent_run_id,created_at);
        CREATE TABLE IF NOT EXISTS agent_events(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS agent_events_run ON agent_events(run_id,event_id);
        CREATE TABLE IF NOT EXISTS agent_mailbox(
            message_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            sender_run_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'queued',
            created_at INTEGER NOT NULL, delivered_at INTEGER NOT NULL DEFAULT 0,
            acked_at INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS agent_mailbox_run ON agent_mailbox(run_id,state,message_id);
        CREATE TABLE IF NOT EXISTS agent_notifications(
            notification_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            kind TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL DEFAULT 'queued', created_at INTEGER NOT NULL,
            acked_at INTEGER NOT NULL DEFAULT 0);
        """)
        self.db.commit()

    def _hook(self, event, payload, subject=""):
        if self.hooks is None:
            return None
        try:
            return self.hooks.dispatch(event, payload, subject=subject)
        except Exception as exc:
            run_id = str((payload or {}).get("run_id") or "")
            if run_id:
                with self.lock:
                    self._event_locked(run_id, "hook_error",
                                       {"event": event, "error": "%s: %s" %
                                        (type(exc).__name__, exc)})
                    self.db.commit()
            return None

    def _event_locked(self, run_id, kind, payload=None, now=None):
        self.db.execute(
            "INSERT INTO agent_events(run_id,kind,payload_json,at) VALUES(?,?,?,?)",
            (run_id, kind, _js(payload or {}), int(now if now is not None else time.time())))

    def _notify_locked(self, run_id, kind, payload=None, now=None):
        self.db.execute(
            "INSERT INTO agent_notifications(run_id,kind,payload_json,state,created_at) "
            "VALUES(?,?,?,'queued',?)",
            (run_id, kind, _js(payload or {}), int(now if now is not None else time.time())))

    def _queue_child_result_locked(self, run_id, state, result, now, *,
                                   artifacts=None, observation=None):
        """Publish one terminal child outcome to its parent exactly once."""
        row = self.db.execute(
            "SELECT parent_run_id,role,mission_id,workspace FROM agent_runs WHERE run_id=?",
            (run_id,)).fetchone()
        if not row or not row["parent_run_id"]:
            return None
        parent = self.db.execute(
            "SELECT status,cancel_requested FROM agent_runs WHERE run_id=?",
            (row["parent_run_id"],)).fetchone()
        if (not parent or parent["status"] in _TERMINAL or
                parent["status"] == CANCEL_REQUESTED or parent["cancel_requested"]):
            return None
        existing = self.db.execute(
            "SELECT message_id FROM agent_mailbox WHERE run_id=? AND sender_run_id=? "
            "AND kind='child_result' LIMIT 1",
            (row["parent_run_id"], run_id)).fetchone()
        if existing:
            return existing["message_id"]
        observed = observation if isinstance(observation, dict) else {}
        observed_raw = _js(observed)
        if len(observed_raw) > 4000:
            observed = {"summary": observed_raw[:3900], "truncated": True}
        payload = {
            "run_id": run_id,
            "mission_id": row["mission_id"],
            "role": row["role"],
            "state": state,
            "result": str(result or "")[:4000],
            "artifacts": normalize_artifact_refs(artifacts),
            "observation": observed,
        }
        cur = self.db.execute(
            "INSERT INTO agent_mailbox(run_id,sender_run_id,kind,payload_json,state,created_at) "
            "VALUES(?,?, 'child_result',?,'queued',?)",
            (row["parent_run_id"], run_id, _js(payload), now))
        self._event_locked(
            row["parent_run_id"], "child_result_queued",
            {"message_id": cur.lastrowid, "run_id": run_id,
             "state": state, "role": row["role"]}, now)
        return cur.lastrowid

    def _row(self, run_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row):
        out = dict(row)
        out["leash"] = _jl(out.pop("leash_json"))
        out["resources"] = _jl(out.pop("resources_json"), [])
        out["background"] = bool(out["background"])
        out["owns_workspace"] = bool(out["owns_workspace"])
        out["cancel_requested"] = bool(out["cancel_requested"])
        out["model_cost_usd"] = out["model_cost_microusd"] / 1_000_000.0
        return out

    def create_root(self, task, leash, resources, *, run_id=None, mission_id="",
                    workspace="", workspace_mode="worktree"):
        run_id = run_id or "run_" + secrets.token_hex(8)
        now = int(time.time())
        task = str(task)[:4000]
        leash = dict(leash or {})
        resources = normalize_resources(resources)
        workspace = os.path.realpath(os.path.abspath(workspace)) if workspace else ""
        status = QUEUED if workspace or workspace_mode != "worktree" else WORKSPACE_REQUIRED
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = self.db.execute(
                    "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
                if existing:
                    decoded = self._decode(existing)
                    matches = (
                        not decoded["parent_run_id"] and
                        decoded["mission_id"] == mission_id and
                        decoded["task"] == task and
                        decoded["leash"] == leash and
                        decoded["resources"] == resources and
                        decoded["workspace_mode"] == workspace_mode and
                        decoded["workspace"] == workspace
                    )
                    if not matches:
                        self.db.rollback()
                        raise ValueError(
                            "root run id is already bound to a different operation")
                    self.db.commit()
                    return decoded
                self.db.execute(
                    "INSERT INTO agent_runs(run_id,parent_run_id,root_run_id,mission_id,depth,role,"
                    "task,status,leash_json,resources_json,workspace_mode,workspace,created_at,updated_at,"
                    "progress_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, "", run_id, mission_id, 0, "orchestrator", task, status,
                     _js(leash), _js(resources), workspace_mode, workspace, now, now, now))
                self._event_locked(run_id, "created", {"status": status}, now)
                self.db.commit()
            except Exception:
                if self.db.in_transaction:
                    self.db.rollback()
                raise
        run = self.get(run_id)
        self._hook("TaskCreated", {"run_id": run_id, "parent_run_id": "",
                                    "task": run["task"], "role": run["role"],
                                    "resources": run["resources"]}, subject=run["role"])
        return run

    def get(self, run_id):
        return self._row(run_id)

    def spawn_specialist(self, parent_run_id, role, task, *, leash=None, resources=None,
                         run_id=None, workspace="", workspace_mode="worktree"):
        parent = self.get(parent_run_id)
        if not parent or parent["status"] in _TERMINAL:
            raise ValueError("specialist parent is missing or terminal")
        max_depth = int(parent["leash"].get("max_specialist_depth", 2))
        if parent["depth"] + 1 > max_depth:
            raise ValueError("specialist depth exceeds parent leash")
        child_leash = narrow_leash(parent["leash"], leash)
        child_resources = normalize_resources(parent["resources"] if resources is None else resources)
        for resource in child_resources:
            if not any(_resource_contains(owned, resource) for owned in parent["resources"]):
                raise ValueError("specialist resource expands parent ownership: %s:%s" %
                                 (resource["kind"], resource["id"]))
        run_id = run_id or "run_" + secrets.token_hex(8)
        now = int(time.time())
        workspace = os.path.realpath(os.path.abspath(workspace)) if workspace else ""
        status = QUEUED if workspace or workspace_mode != "worktree" else WORKSPACE_REQUIRED
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.db.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                decoded = self._decode(existing)
                matches = (
                    decoded["parent_run_id"] == parent_run_id and
                    decoded["role"] == str(role or "specialist")[:80] and
                    decoded["task"] == str(task)[:4000] and
                    decoded["leash"] == child_leash and
                    decoded["resources"] == child_resources and
                    decoded["workspace_mode"] == workspace_mode
                )
                if not matches:
                    self.db.rollback()
                    raise ValueError("specialist run id is already bound to different authority")
                self.db.commit()
                return decoded
            current_parent = self.db.execute(
                "SELECT status,cancel_requested FROM agent_runs WHERE run_id=?",
                (parent_run_id,)).fetchone()
            if (not current_parent or current_parent["status"] in _TERMINAL or
                    current_parent["status"] == CANCEL_REQUESTED or
                    current_parent["cancel_requested"]):
                self.db.rollback()
                raise ValueError("specialist parent is stopping or terminal")
            count = self.db.execute(
                "SELECT COUNT(*) n FROM agent_runs WHERE parent_run_id=? AND status NOT IN (?,?,?)",
                (parent_run_id, COMPLETED, FAILED, CANCELLED)).fetchone()["n"]
            if count >= int(parent["leash"].get("max_specialists", 4)):
                self.db.rollback()
                raise ValueError("parent specialist concurrency budget exhausted")
            # Siblings may read the same scope; write ownership is exclusive. The
            # parent can query can_access() and must stop touching delegated files.
            siblings = self.db.execute(
                "SELECT resources_json FROM agent_runs WHERE parent_run_id=? "
                "AND status NOT IN (?,?,?)", (parent_run_id, COMPLETED, FAILED, CANCELLED)).fetchall()
            for sibling in siblings:
                for old in _jl(sibling["resources_json"], []):
                    for new in child_resources:
                        overlap = _resource_contains(old, new) or _resource_contains(new, old)
                        if overlap and "write" in (old["mode"], new["mode"]):
                            self.db.rollback()
                            raise ValueError("specialist write resource already owned: %s:%s" %
                                             (new["kind"], new["id"]))
            self.db.execute(
                "INSERT INTO agent_runs(run_id,parent_run_id,root_run_id,mission_id,depth,role,"
                "task,status,leash_json,resources_json,workspace_mode,workspace,created_at,updated_at,"
                "progress_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, parent_run_id, parent["root_run_id"], "",
                 parent["depth"] + 1, str(role or "specialist")[:80], str(task)[:4000], status,
                 _js(child_leash), _js(child_resources), workspace_mode, workspace, now, now, now))
            self._event_locked(run_id, "created", {"parent_run_id": parent_run_id,
                                                     "role": role, "status": status}, now)
            self._event_locked(parent_run_id, "child_created", {"run_id": run_id,
                                                                  "role": role}, now)
            self.db.commit()
        child = self.get(run_id)
        self._hook("TaskCreated", {"run_id": run_id, "parent_run_id": parent_run_id,
                                    "task": child["task"], "role": child["role"],
                                    "resources": child["resources"]}, subject=child["role"])
        return child

    def bind_workspace(self, run_id, path, *, owns_workspace=False):
        canonical = os.path.realpath(os.path.abspath(str(path or "")))
        if not path or not os.path.isdir(canonical):
            raise ValueError("provisioned worktree does not exist")
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET workspace=?,owns_workspace=?,status=CASE WHEN status=? "
                "THEN ? ELSE status END,updated_at=? WHERE run_id=? AND status NOT IN (?,?,?) "
                "AND owner_token='' AND (workspace='' OR workspace=?)",
                (canonical, int(bool(owns_workspace)), WORKSPACE_REQUIRED, QUEUED, now, run_id,
                 COMPLETED, FAILED, CANCELLED, canonical))
            if cur.rowcount:
                self._event_locked(run_id, "workspace_bound",
                                   {"workspace": canonical, "owned": bool(owns_workspace)}, now)
            self.db.commit()
        return self.get(run_id) if cur.rowcount else None

    def initialize_root_workspace_authority(self, run_id, path, mode="read"):
        """One-time host binding for an authority-empty lazy Mission root.

        This is intentionally narrower than a general resource mutation API: it
        accepts only a root whose workspace/resources are both empty and whose
        tree has no active descendants.  An identical replay is idempotent; all
        other attempts to move or expand an established grant fail closed.
        """
        canonical = os.path.realpath(os.path.abspath(str(path or "")))
        mode = str(mode or "read").lower()
        if not path or not os.path.isdir(canonical):
            raise ValueError("root workspace does not exist")
        if mode not in ("read", "write"):
            raise ValueError("root workspace authority must be read or write")
        resource = {"kind": "file", "id": os.path.normcase(canonical), "mode": mode}
        now = int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                self.db.rollback()
                raise ValueError("root run is missing")
            run = self._decode(row)
            if run["parent_run_id"]:
                self.db.rollback()
                raise ValueError("workspace authority can only initialize a root run")
            if (os.path.normcase(run["workspace"]) == os.path.normcase(canonical) and
                    run["resources"] == [resource] and
                    run["status"] not in _TERMINAL and not run["cancel_requested"]):
                self.db.commit()
                return run
            if (run["workspace"] or run["resources"] or run["status"] != WORKSPACE_REQUIRED or
                    run["owner_token"] or run["cancel_requested"]):
                self.db.rollback()
                raise ValueError("root workspace authority is already initialized or unavailable")
            active = self.db.execute(
                "SELECT 1 FROM agent_runs WHERE root_run_id=? AND run_id<>? "
                "AND status NOT IN (?,?,?) LIMIT 1",
                (run_id, run_id, COMPLETED, FAILED, CANCELLED)).fetchone()
            if active:
                self.db.rollback()
                raise ValueError(
                    "root workspace authority cannot initialize while specialists are active")
            self.db.execute(
                "UPDATE agent_runs SET workspace=?,resources_json=?,status=?,updated_at=? "
                "WHERE run_id=? AND workspace='' AND resources_json='[]' "
                "AND status=? AND owner_token='' AND cancel_requested=0",
                (canonical, _js([resource]), QUEUED, now, run_id, WORKSPACE_REQUIRED))
            self._event_locked(
                run_id, "root_workspace_authority_initialized",
                {"workspace": canonical, "mode": mode}, now)
            self.db.commit()
        return self.get(run_id)

    def bind_mission(self, run_id, mission_id):
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET mission_id=?,updated_at=? WHERE run_id=? "
                "AND status NOT IN (?,?,?) AND owner_token='' "
                "AND (mission_id='' OR mission_id=?)",
                (str(mission_id)[:100], int(time.time()), run_id,
                 COMPLETED, FAILED, CANCELLED, str(mission_id)[:100]))
            self.db.commit()
        return cur.rowcount == 1

    def provision_worktree(self, run_id, parent_cwd, *, prepare_fn=None):
        """Provision the default isolated checkout and bind it to the durable run.

        Cleanup is intentionally not automatic: a worktree may hold the user's
        completed changes and ``worktree.release`` already refuses to remove such
        work.  Callers own review/merge/release as an explicit later workflow.
        """
        run = self.get(run_id)
        if run and run["workspace_mode"] == "worktree" and run["workspace"]:
            return {"ok": True, "kind": "worktree", "dir": run["workspace"],
                    "run": run, "replayed": True}
        if not run or run["workspace_mode"] != "worktree":
            return {"ok": False, "error": "run does not need a worktree", "run": run}
        token = secrets.token_hex(16)
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET owner_token=?,lease_until=?,updated_at=? "
                "WHERE run_id=? AND status=? AND workspace='' AND cancel_requested=0 "
                "AND (owner_token='' OR lease_until<?)",
                (token, now + 300, now, run_id, WORKSPACE_REQUIRED, now))
            if cur.rowcount:
                self._event_locked(run_id, "workspace_provision_claimed",
                                   {"lease_until": now + 300}, now)
            self.db.commit()
        if not cur.rowcount:
            current = self.get(run_id)
            if current and current.get("workspace"):
                return {"ok": True, "kind": "worktree", "dir": current["workspace"],
                        "run": current, "replayed": True}
            return {"ok": False, "busy": True,
                    "error": "specialist workspace provisioning is already claimed",
                    "run": current}
        # Recovery keys must identify the whole durable run.  A six-hex suffix
        # reaches birthday-collision territory in ordinary long-lived installs
        # and could rebind a new run to an unrelated abandoned worktree.
        run_hash = hashlib.sha256(run_id.encode("utf-8", "replace")).hexdigest()[:16]
        label = "%s-%s" % (str(run["role"] or "specialist")[:24], run_hash)
        try:
            if prepare_fn is None:
                from .worktree import find_prepared, prepare
                # Crash recovery: git may have completed worktree creation before SQLite recorded
                # its path. The deterministic branch label re-binds that exact checkout.
                prepared = find_prepared(parent_cwd, run_id, label) or \
                    prepare(parent_cwd, run_id, label)
            else:
                prepared = prepare_fn(parent_cwd, run_id, label)
            if not prepared.get("ok") or prepared.get("kind") != "worktree":
                with self.lock:
                    self.db.execute(
                        "UPDATE agent_runs SET owner_token='',lease_until=0,updated_at=? "
                        "WHERE run_id=? AND status=? AND owner_token=? AND workspace=''",
                        (int(time.time()), run_id, WORKSPACE_REQUIRED, token))
                    self.db.commit()
                return {**prepared, "run": self.get(run_id)}
            canonical = os.path.realpath(os.path.abspath(str(prepared.get("dir") or "")))
            if not canonical or not os.path.isdir(canonical):
                raise ValueError("provisioned worktree does not exist")
            with self.lock:
                bound = self.db.execute(
                    "UPDATE agent_runs SET workspace=?,owns_workspace=1,status=?,"
                    "owner_token='',lease_until=0,updated_at=? WHERE run_id=? AND status=? "
                    "AND workspace='' AND owner_token=? AND cancel_requested=0",
                    (canonical, QUEUED, int(time.time()), run_id,
                     WORKSPACE_REQUIRED, token))
                if bound.rowcount:
                    self._event_locked(run_id, "workspace_bound",
                                       {"workspace": canonical, "owned": True})
                self.db.commit()
            current = self.get(run_id)
            if not bound.rowcount:
                if current and current.get("workspace"):
                    return {**prepared, "run": current, "replayed": True}
                return {**prepared, "ok": False, "busy": True,
                        "error": "workspace provisioning ownership changed", "run": current}
            return {**prepared, "run": current}
        except Exception:
            with self.lock:
                self.db.execute(
                    "UPDATE agent_runs SET owner_token='',lease_until=0,updated_at=? "
                    "WHERE run_id=? AND status=? AND owner_token=? AND workspace=''",
                    (int(time.time()), run_id, WORKSPACE_REQUIRED, token))
                self.db.commit()
            raise

    def mark_orphan_needs_you(self, run_id, reason, phase=""):
        """Surface an unclaimed specialist repair failure instead of stranding it."""
        now = int(time.time())
        result = str(reason or "specialist reconciliation failed")[:4000]
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT status,workspace,mission_id,owner_token,cancel_requested "
                "FROM agent_runs WHERE run_id=? AND parent_run_id<>''", (run_id,)).fetchone()
            workspace_failure = bool(
                row and row["status"] == WORKSPACE_REQUIRED and not row["workspace"])
            mission_failure = bool(
                row and row["status"] in (QUEUED, RECOVERY_REQUIRED) and
                row["workspace"])
            eligible = ((phase == "workspace" and workspace_failure) or
                        (phase == "mission" and mission_failure) or
                        (not phase and (workspace_failure or mission_failure)))
            if (not eligible or row["owner_token"] or row["cancel_requested"]):
                self.db.rollback()
                return False
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result=?,updated_at=? WHERE run_id=? "
                "AND parent_run_id<>'' AND status=? AND workspace=? AND mission_id=? "
                "AND owner_token='' AND cancel_requested=0",
                (NEEDS_YOU, result, now, run_id, row["status"], row["workspace"],
                 row["mission_id"]))
            if cur.rowcount:
                self._event_locked(
                    run_id, "orphan_reconciliation_failed", {"reason": result[:1000]}, now)
                self._notify_locked(run_id, "needs_you", {"reason": result[:1000]}, now)
            self.db.commit()
        if cur.rowcount:
            self._hook("Notification", {"run_id": run_id, "kind": "needs_you",
                                         "state": NEEDS_YOU}, subject="needs_you")
        return cur.rowcount == 1

    def claim(self, run_id, lease_s=300):
        token, now = secrets.token_hex(16), int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            candidate = self.db.execute(
                "SELECT parent_run_id,status,cancel_requested FROM agent_runs WHERE run_id=?",
                (run_id,)).fetchone()
            if not candidate or candidate["status"] != QUEUED:
                self.db.commit()
                return None
            ancestry = [run_id]
            stopping = (run_id, "cancellation already requested") \
                if candidate["cancel_requested"] else None
            parent_id = candidate["parent_run_id"]
            while parent_id:
                ancestry.append(parent_id)
                parent = self.db.execute(
                    "SELECT parent_run_id,status,cancel_requested FROM agent_runs WHERE run_id=?",
                    (parent_id,)).fetchone()
                if not parent:
                    stopping = (parent_id, "ancestor is missing")
                    break
                if parent["status"] in _TERMINAL or parent["status"] == CANCEL_REQUESTED or \
                        parent["cancel_requested"]:
                    stopping = (parent_id, "ancestor is %s" % parent["status"])
                    break
                parent_id = parent["parent_run_id"] if parent else ""
            if stopping:
                missing = stopping[1] == "ancestor is missing"
                state = NEEDS_YOU if missing else CANCELLED
                cur = self.db.execute(
                    "UPDATE agent_runs SET status=?,result=?,cancel_requested=?,cancel_ack_at=?,"
                    "updated_at=? WHERE run_id=? AND status=? AND owner_token=''",
                    (state, "%s: %s" % stopping, 0 if missing else 1,
                     0 if missing else now, now, run_id, QUEUED))
                if cur.rowcount:
                    kind = "needs_you" if missing else "cancelled"
                    self._event_locked(run_id, "ancestor_stopped_claim", {
                        "ancestor_run_id": stopping[0], "reason": stopping[1]}, now)
                    self._notify_locked(run_id, kind, {"reason": stopping[1]}, now)
                self.db.commit()
                return None
            exhausted = None
            for rid in ancestry:
                reason = self.budget_reason(rid)
                if reason:
                    exhausted = (rid, reason)
                    break
            if exhausted:
                reason = "%s: %s" % exhausted
                cur = self.db.execute(
                    "UPDATE agent_runs SET status=?,result=?,updated_at=? WHERE run_id=? "
                    "AND status=? AND owner_token=''",
                    (NEEDS_YOU, reason[:4000], now, run_id, QUEUED))
                if cur.rowcount:
                    self._event_locked(run_id, "budget_blocked", {
                        "budget_run_id": exhausted[0], "reason": exhausted[1]}, now)
                    self._notify_locked(run_id, "needs_you", {"reason": reason[:1000]}, now)
                self.db.commit()
                return None
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,owner_token=?,lease_until=?,updated_at=? "
                "WHERE run_id=? AND status=? AND owner_token='' AND cancel_requested=0",
                (RUNNING, token, now + int(lease_s), now, run_id, QUEUED))
            if cur.rowcount:
                self._event_locked(run_id, "claimed", {"lease_until": now + int(lease_s)}, now)
            self.db.commit()
        return token if cur.rowcount else None

    def renew(self, run_id, token, lease_s=300):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET lease_until=?,updated_at=? WHERE run_id=? "
                "AND status IN (?,?) AND owner_token=?",
                (now + int(lease_s), now, run_id, RUNNING, CANCEL_REQUESTED, token))
            self.db.commit()
        return cur.rowcount == 1

    def progress(self, run_id, token, summary, *, percent=None, detail=None):
        now = int(time.time())
        payload = {"summary": str(summary)[:1000]}
        if percent is not None:
            payload["percent"] = max(0, min(100, float(percent)))
        if detail is not None:
            payload["detail"] = detail
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET progress_seq=progress_seq+1,progress_at=?,updated_at=? "
                "WHERE run_id=? AND status IN (?,?) AND owner_token=?",
                (now, now, run_id, RUNNING, CANCEL_REQUESTED, token))
            if cur.rowcount:
                self._event_locked(run_id, "progress", payload, now)
            self.db.commit()
        return cur.rowcount == 1

    def set_background(self, run_id, background=True, token=""):
        with self.lock:
            suffix, args = "", [int(bool(background)), int(time.time()), run_id]
            if token:
                suffix = " AND owner_token=?"
                args.append(token)
            cur = self.db.execute(
                "UPDATE agent_runs SET background=?,updated_at=? WHERE run_id=? "
                "AND status NOT IN (?,?,?)" + suffix,
                (*args[:3], COMPLETED, FAILED, CANCELLED, *args[3:]))
            self.db.commit()
        return cur.rowcount == 1

    def _cancel_descendants_locked(self, run_id, now, reason):
        """Fence every active descendant after an ancestor terminal failure."""
        rows = self.db.execute(
            "WITH RECURSIVE descendants(run_id) AS ("
            "SELECT run_id FROM agent_runs WHERE parent_run_id=? UNION ALL "
            "SELECT child.run_id FROM agent_runs child JOIN descendants parent "
            "ON child.parent_run_id=parent.run_id) "
            "SELECT run_id,status,owner_token FROM agent_runs "
            "WHERE run_id IN (SELECT run_id FROM descendants) "
            "ORDER BY depth DESC,run_id",
            (run_id,)).fetchall()
        cancelled = 0
        for row in rows:
            child_id = row["run_id"]
            if row["status"] in _TERMINAL:
                continue
            if row["status"] in (RUNNING, CANCEL_REQUESTED) and row["owner_token"]:
                transitioned = row["status"] != CANCEL_REQUESTED
                self.db.execute(
                    "UPDATE agent_runs SET status=?,cancel_requested=1,updated_at=? "
                    "WHERE run_id=? AND status IN (?,?) AND owner_token=?",
                    (CANCEL_REQUESTED, now, child_id, RUNNING, CANCEL_REQUESTED,
                     row["owner_token"]))
                pending = self.db.execute(
                    "SELECT 1 FROM agent_mailbox WHERE run_id=? AND kind='cancel' "
                    "AND state IN ('queued','delivered') AND acked_at=0 LIMIT 1",
                    (child_id,)).fetchone()
                if not pending:
                    self.db.execute(
                        "INSERT INTO agent_mailbox(run_id,sender_run_id,kind,payload_json,"
                        "state,created_at) VALUES(?,?,'cancel','{}','queued',?)",
                        (child_id, run_id, now))
                if transitioned:
                    self._event_locked(
                        child_id, "cancel_requested",
                        {"cascade_from": run_id, "reason": str(reason)[:500]}, now)
                cancelled += 1
                continue
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result=?,owner_token='',lease_until=0,"
                "cancel_requested=1,cancel_ack_at=?,updated_at=? WHERE run_id=? "
                "AND status NOT IN (?,?,?) AND owner_token=''",
                (CANCELLED, str(reason or "ancestor failed")[:4000], now, now, child_id,
                 COMPLETED, FAILED, CANCELLED))
            if cur.rowcount:
                self._event_locked(
                    child_id, "cancel_acknowledged",
                    {"without_worker": True, "cascade_from": run_id,
                     "reason": str(reason)[:500]}, now)
                self._notify_locked(
                    child_id, "cancelled",
                    {"acknowledged": True, "cascade_from": run_id}, now)
                cancelled += 1
        return cancelled

    def _stop_owned(self, run_id, token, state, result, notify, *,
                    artifacts=None, observation=None):
        if state in (COMPLETED, FAILED):
            hook = self._hook(
                "TaskCompleted", {"run_id": run_id, "state": state,
                                  "result": str(result or "")[:4000]}, subject=state)
            if (state == COMPLETED and hook is not None and
                    not getattr(hook, "allowed", True)):
                with self.lock:
                    self._event_locked(
                        run_id, "completion_hook_blocked",
                        {"reason": getattr(hook, "reason", "") or "policy check did not pass"})
                    self.db.commit()
                return False
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result=?,owner_token='',lease_until=0,updated_at=? "
                "WHERE run_id=? AND status=? AND owner_token=?",
                (state, str(result or "")[:4000], now, run_id, RUNNING, token))
            if cur.rowcount:
                self._event_locked(run_id, state, {"result": str(result or "")[:1000]}, now)
                if notify:
                    self._notify_locked(run_id, notify,
                                        {"state": state, "result": str(result or "")[:1000]}, now)
                if state == FAILED:
                    self._cancel_descendants_locked(
                        run_id, now, "cancelled because ancestor %s failed" % run_id)
                if state in _TERMINAL:
                    self._queue_child_result_locked(
                        run_id, state, result, now, artifacts=artifacts,
                        observation=observation)
            self.db.commit()
        if cur.rowcount and notify:
            self._hook("Notification", {"run_id": run_id, "kind": notify,
                                         "state": state}, subject=notify)
        return cur.rowcount == 1

    def block(self, run_id, token, reason, *, needs_you=False):
        return self._stop_owned(run_id, token, NEEDS_YOU if needs_you else BLOCKED,
                                reason, "needs_you" if needs_you else "blocked")

    def complete(self, run_id, token, result="", *, artifacts=None, observation=None):
        return self._stop_owned(
            run_id, token, COMPLETED, result, "completed",
            artifacts=artifacts, observation=observation)

    def fail(self, run_id, token, result=""):
        return self._stop_owned(run_id, token, FAILED, result, "failed")

    def fail_mission_root(self, run_id, mission_id, result=""):
        """Mirror a terminal root Mission failure and fence its whole subtree."""
        reason = str(result or "root Mission failed")[:4000]
        now = int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT parent_run_id,mission_id,status,owner_token FROM agent_runs "
                "WHERE run_id=?", (run_id,)).fetchone()
            if (not row or row["parent_run_id"] or row["mission_id"] != str(mission_id) or
                    row["owner_token"]):
                self.db.rollback()
                return False
            if row["status"] == FAILED:
                # Idempotent reconciliation also repairs an impossible-looking
                # but durable partial/corrupt projection where the root marker is
                # present yet a descendant was not fenced.
                self._cancel_descendants_locked(
                    run_id, now, "cancelled because ancestor %s failed" % run_id)
                self.db.commit()
                return True
            if row["status"] in _TERMINAL:
                self.db.commit()
                return False
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result=?,owner_token='',lease_until=0,"
                "updated_at=? WHERE run_id=? AND parent_run_id='' AND mission_id=? "
                "AND status NOT IN (?,?,?) AND owner_token=''",
                (FAILED, reason, now, run_id, str(mission_id),
                 COMPLETED, FAILED, CANCELLED))
            if cur.rowcount:
                self._event_locked(run_id, FAILED, {"result": reason[:1000]}, now)
                self._notify_locked(
                    run_id, "failed", {"state": FAILED, "result": reason[:1000]}, now)
                self._cancel_descendants_locked(
                    run_id, now, "cancelled because ancestor %s failed" % run_id)
            self.db.commit()
        if cur.rowcount:
            self._hook(
                "TaskCompleted", {"run_id": run_id, "state": FAILED, "result": reason},
                subject=FAILED)
            self._hook("Notification", {"run_id": run_id, "kind": "failed",
                                         "state": FAILED}, subject="failed")
        return cur.rowcount == 1

    def complete_mission_root(self, run_id, mission_id, result=""):
        """Mirror a successful root Mission once its delegated subtree is terminal.

        Root Mission execution is leased by MissionStore, not by TaskTree, so the
        root run normally remains ownerless.  This narrow projection CAS keeps the
        two durable views coherent without making an ownerless root claimable as a
        specialist or bypassing the descendant completion fence.
        """
        outcome = str(result or "")[:4000]
        now = int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT parent_run_id,root_run_id,mission_id,status,owner_token,"
                "cancel_requested FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if (not row or row["parent_run_id"] or row["root_run_id"] != run_id or
                    row["mission_id"] != str(mission_id) or row["owner_token"] or
                    row["cancel_requested"]):
                self.db.rollback()
                return False
            if row["status"] == COMPLETED:
                self.db.commit()
                return True
            if row["status"] in _TERMINAL or row["status"] == CANCEL_REQUESTED:
                self.db.commit()
                return False
            active = self.db.execute(
                "SELECT 1 FROM agent_runs WHERE root_run_id=? AND run_id<>? "
                "AND status NOT IN (?,?,?) LIMIT 1",
                (run_id, run_id, COMPLETED, FAILED, CANCELLED)).fetchone()
            if active:
                self.db.rollback()
                return False
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result=?,owner_token='',lease_until=0,"
                "updated_at=? WHERE run_id=? AND parent_run_id='' AND root_run_id=? "
                "AND mission_id=? AND status NOT IN (?,?,?) AND status<>? "
                "AND owner_token='' AND cancel_requested=0",
                (COMPLETED, outcome, now, run_id, run_id, str(mission_id),
                 COMPLETED, FAILED, CANCELLED, CANCEL_REQUESTED))
            if cur.rowcount:
                self._event_locked(run_id, COMPLETED, {"result": outcome[:1000]}, now)
                self._notify_locked(
                    run_id, "completed", {"state": COMPLETED, "result": outcome[:1000]}, now)
            self.db.commit()
        if cur.rowcount:
            # Mission completion is already authoritative at this point.  The
            # TaskTree hook is an audit/lifecycle projection, not a second veto.
            self._hook(
                "TaskCompleted", {"run_id": run_id, "state": COMPLETED,
                                  "result": outcome}, subject=COMPLETED)
            self._hook("Notification", {"run_id": run_id, "kind": "completed",
                                         "state": COMPLETED}, subject="completed")
        return cur.rowcount == 1

    def cancel_owned(self, run_id, token, result="cancelled"):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result=?,owner_token='',lease_until=0,"
                "cancel_requested=1,cancel_ack_at=?,updated_at=? WHERE run_id=? "
                "AND status IN (?,?) AND owner_token=?",
                (CANCELLED, str(result)[:4000], now, now, run_id,
                 RUNNING, CANCEL_REQUESTED, token))
            if cur.rowcount:
                self.db.execute(
                    "UPDATE agent_mailbox SET state='acked',acked_at=? WHERE run_id=? "
                    "AND kind='cancel' AND state IN ('queued','delivered')", (now, run_id))
                self._event_locked(run_id, "cancel_acknowledged", {}, now)
                self._notify_locked(run_id, "cancelled", {"acknowledged": True}, now)
                self._queue_child_result_locked(run_id, CANCELLED, result, now)
            self.db.commit()
        return cur.rowcount == 1

    def resume(self, run_id):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result='',updated_at=? WHERE run_id=? "
                "AND status IN (?,?,?) AND owner_token='' AND cancel_requested=0 "
                "AND (workspace_mode<>'worktree' OR workspace<>'')",
                (QUEUED, now, run_id, BLOCKED, NEEDS_YOU, PAUSED))
            if cur.rowcount:
                self._event_locked(run_id, "resumed", {}, now)
            self.db.commit()
        return cur.rowcount == 1

    def park_waiting(self, run_id, token, reason="waiting"):
        return self._stop_owned(run_id, token, WAITING, reason, "")

    def requeue_waiting(self, run_id):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result='',updated_at=? WHERE run_id=? "
                "AND status=? AND owner_token='' AND cancel_requested=0",
                (QUEUED, now, run_id, WAITING))
            if cur.rowcount:
                self._event_locked(run_id, "wake_due", {}, now)
            self.db.commit()
        return cur.rowcount == 1

    def mark_recovery(self, run_id, token, reason):
        return self._stop_owned(run_id, token, RECOVERY_REQUIRED, reason,
                                "recovery_required")

    def reconcile(self, run_id, note=""):
        now = int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            before = self.db.execute(
                "SELECT cancel_requested FROM agent_runs WHERE run_id=? AND status=? "
                "AND owner_token=''", (run_id, RECOVERY_REQUIRED)).fetchone()
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result=?,cancel_requested=0,updated_at=? "
                "WHERE run_id=? AND status=? AND owner_token=''",
                (QUEUED, str(note or "explicitly reconciled")[:4000], now,
                 run_id, RECOVERY_REQUIRED))
            if cur.rowcount:
                self._event_locked(run_id, "reconciled", {"note": note}, now)
                if before and before["cancel_requested"]:
                    # Reconcile is the explicit decision to resume despite an interrupted cancel.
                    # Retire its old mailbox item so the fresh worker is not immediately cancelled.
                    self.db.execute(
                        "UPDATE agent_mailbox SET state='acked',acked_at=? WHERE run_id=? "
                        "AND kind='cancel' AND state IN ('queued','delivered')", (now, run_id))
                    self._event_locked(run_id, "cancel_superseded_by_reconcile", {}, now)
            self.db.commit()
        return cur.rowcount == 1

    def steer(self, run_id, text, sender_run_id=""):
        if not str(text or "").strip():
            raise ValueError("steer text is empty")
        now = int(time.time())
        with self.lock:
            row = self.db.execute(
                "SELECT status,cancel_requested FROM agent_runs WHERE run_id=?",
                (run_id,)).fetchone()
            if (not row or row["status"] in _TERMINAL or
                    row["status"] == CANCEL_REQUESTED or row["cancel_requested"]):
                return None
            cur = self.db.execute(
                "INSERT INTO agent_mailbox(run_id,sender_run_id,kind,payload_json,state,created_at) "
                "VALUES(?,?,'steer',?,'queued',?)",
                (run_id, sender_run_id, _js({"text": str(text)[:4000]}), now))
            self.db.execute(
                "UPDATE agent_runs SET status=?,result='',updated_at=? WHERE run_id=? "
                "AND status=? AND owner_token='' AND cancel_requested=0",
                (QUEUED, now, run_id, WAITING))
            self._event_locked(run_id, "steer_queued", {"message_id": cur.lastrowid}, now)
            self.db.commit()
        return cur.lastrowid

    def request_cancel(self, run_id, sender_run_id=""):
        """Cancel a run and its whole delegated subtree atomically.

        Work which has not started is terminally cancelled in the same transaction.  A live owner
        is fenced as ``cancel_requested`` and receives one durable mailbox message, so it stops at
        its next safe boundary.  Descendants are included because cancelling an orchestrator while
        leaving delegated authority runnable would violate the parent's stop decision.
        """
        now = int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "WITH RECURSIVE subtree(run_id) AS ("
                "SELECT run_id FROM agent_runs WHERE run_id=? UNION "
                "SELECT child.run_id FROM agent_runs child JOIN subtree parent "
                "ON child.parent_run_id=parent.run_id) "
                "SELECT run_id,status,owner_token,cancel_requested FROM agent_runs "
                "WHERE run_id IN (SELECT run_id FROM subtree) ORDER BY depth DESC,run_id",
                (run_id,)).fetchall()
            if not rows:
                self.db.commit()
                return False
            active = [row for row in rows if row["status"] not in _TERMINAL]
            if not active:
                self.db.commit()
                return False
            for row in active:
                child_id = row["run_id"]
                if row["status"] in (RUNNING, CANCEL_REQUESTED) and row["owner_token"]:
                    transitioned = row["status"] != CANCEL_REQUESTED
                    self.db.execute(
                        "UPDATE agent_runs SET status=?,cancel_requested=1,updated_at=? "
                        "WHERE run_id=? AND status IN (?,?)",
                        (CANCEL_REQUESTED, now, child_id, RUNNING, CANCEL_REQUESTED))
                    pending = self.db.execute(
                        "SELECT 1 FROM agent_mailbox WHERE run_id=? AND kind='cancel' "
                        "AND state IN ('queued','delivered') AND acked_at=0 LIMIT 1",
                        (child_id,)).fetchone()
                    if not pending:
                        self.db.execute(
                            "INSERT INTO agent_mailbox(run_id,sender_run_id,kind,payload_json,"
                            "state,created_at) VALUES(?,?,'cancel','{}','queued',?)",
                            (child_id, sender_run_id, now))
                    if transitioned:
                        self._event_locked(child_id, "cancel_requested", {
                            "cascade_from": run_id if child_id != run_id else ""}, now)
                    continue
                cur = self.db.execute(
                    "UPDATE agent_runs SET status=?,result=?,owner_token='',lease_until=0,"
                    "cancel_requested=1,cancel_ack_at=?,updated_at=? WHERE run_id=? "
                    "AND status NOT IN (?,?,?)",
                    (CANCELLED, "cancelled before execution", now, now, child_id,
                     COMPLETED, FAILED, CANCELLED))
                if cur.rowcount:
                    self._event_locked(child_id, "cancel_acknowledged", {
                        "without_worker": True,
                        "cascade_from": run_id if child_id != run_id else ""}, now)
                    self._notify_locked(child_id, "cancelled", {
                        "acknowledged": True,
                        "cascade_from": run_id if child_id != run_id else ""}, now)
                    # Only the subtree root reports cancellation to authority
                    # outside the subtree. Descendant parents are themselves
                    # stopping and cannot act on intermediate outcomes.
                    if child_id == run_id:
                        self._queue_child_result_locked(
                            child_id, CANCELLED, "cancelled before execution", now)
            self.db.commit()
        return True

    def claim_messages(self, run_id, token, limit=20):
        now = int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            owner = self.db.execute(
                "SELECT 1 FROM agent_runs WHERE run_id=? AND owner_token=? "
                "AND status IN (?,?)", (run_id, token, RUNNING, CANCEL_REQUESTED)).fetchone()
            if not owner:
                self.db.rollback()
                return []
            rows = self.db.execute(
                "SELECT * FROM agent_mailbox WHERE run_id=? AND state='queued' "
                "ORDER BY message_id LIMIT ?", (run_id, max(1, int(limit)))).fetchall()
            ids = [row["message_id"] for row in rows]
            if ids:
                marks = ",".join("?" for _ in ids)
                self.db.execute(
                    "UPDATE agent_mailbox SET state='delivered',delivered_at=? "
                    "WHERE message_id IN (%s) AND state='queued'" % marks, (now, *ids))
            self.db.commit()
        return [{**dict(row), "payload": _jl(row["payload_json"])} for row in rows]

    def claim_child_results(self, run_id, consumer_mission_id, limit=20):
        """Deliver terminal child outcomes to the Mission bound to ``run_id``.

        Unlike a worker mailbox claim, the parent Mission and TaskTree run use
        different lease tokens.  The durable Mission binding is therefore the
        authority check.  Delivered-but-unacked rows are replayed so a crash after
        folding into Mission case cannot lose the outcome.
        """
        now = int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            owner = self.db.execute(
                "SELECT status,cancel_requested FROM agent_runs "
                "WHERE run_id=? AND mission_id=?",
                (run_id, str(consumer_mission_id or ""))).fetchone()
            if (not owner or owner["status"] in _TERMINAL or
                    owner["status"] == CANCEL_REQUESTED or owner["cancel_requested"]):
                self.db.rollback()
                return []
            rows = self.db.execute(
                "SELECT * FROM agent_mailbox WHERE run_id=? AND kind='child_result' "
                "AND state IN ('queued','delivered') ORDER BY message_id LIMIT ?",
                (run_id, max(1, int(limit)))).fetchall()
            queued = [row["message_id"] for row in rows if row["state"] == "queued"]
            if queued:
                marks = ",".join("?" for _ in queued)
                self.db.execute(
                    "UPDATE agent_mailbox SET state='delivered',delivered_at=? "
                    "WHERE message_id IN (%s) AND state='queued'" % marks,
                    (now, *queued))
            self.db.commit()
        return [{**dict(row), "state": "delivered",
                 "payload": _jl(row["payload_json"])} for row in rows]

    def completion_blocker(self, run_id, consumer_mission_id):
        """Atomically find delegated work/results that must precede Mission success.

        Child terminalization and its child_result enqueue share one TaskTree
        transaction.  Taking an immediate transaction here therefore closes the
        race where a child finishes after Mission control polled but before its
        model reports done: this sees either the active child or its queued result.
        """
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            owner = self.db.execute(
                "SELECT status,cancel_requested FROM agent_runs "
                "WHERE run_id=? AND mission_id=?",
                (run_id, str(consumer_mission_id or ""))).fetchone()
            if (not owner or owner["status"] in _TERMINAL or
                    owner["status"] == CANCEL_REQUESTED or owner["cancel_requested"]):
                self.db.commit()
                return {"reason": "calling run is stopping, terminal, or unbound",
                        "seconds": 60}
            pending = self.db.execute(
                "SELECT COUNT(*) n FROM agent_mailbox WHERE run_id=? "
                "AND kind='child_result' AND state IN ('queued','delivered')",
                (run_id,)).fetchone()["n"]
            active = self.db.execute(
                "WITH RECURSIVE descendants(run_id,role,status) AS ("
                "SELECT run_id,role,status FROM agent_runs WHERE parent_run_id=? UNION ALL "
                "SELECT child.run_id,child.role,child.status FROM agent_runs child "
                "JOIN descendants parent ON child.parent_run_id=parent.run_id) "
                "SELECT role,status FROM descendants WHERE status NOT IN (?,?,?) "
                "ORDER BY run_id LIMIT 6",
                (run_id, COMPLETED, FAILED, CANCELLED)).fetchall()
            self.db.commit()
        if pending:
            return {
                "reason": "%d delegated specialist result(s) await durable folding" % pending,
                "seconds": 1,
            }
        if active:
            roles = ", ".join("%s:%s" % (row["role"] or "specialist", row["status"])
                              for row in active)
            return {
                "reason": "%d+ delegated specialist(s) still active (%s)" %
                          (len(active), roles),
                "seconds": 60,
            }
        return {}

    def has_child_results(self, run_id, consumer_mission_id):
        """Check the bound parent inbox without changing delivery state."""
        with self.lock:
            row = self.db.execute(
                "SELECT 1 FROM agent_mailbox m JOIN agent_runs r ON r.run_id=m.run_id "
                "WHERE m.run_id=? AND r.mission_id=? AND m.kind='child_result' "
                "AND m.state IN ('queued','delivered') LIMIT 1",
                (run_id, str(consumer_mission_id or ""))).fetchone()
        return bool(row)

    def has_messages(self, run_id, kinds):
        """Read-only signal used to event-wake a specialist Mission wait."""
        kinds = (kinds,) if isinstance(kinds, str) else tuple(kinds or ())
        if not kinds:
            return False
        marks = ",".join("?" for _ in kinds)
        with self.lock:
            row = self.db.execute(
                "SELECT 1 FROM agent_mailbox WHERE run_id=? AND kind IN (%s) "
                "AND state IN ('queued','delivered') LIMIT 1" % marks,
                (run_id, *kinds)).fetchone()
        return bool(row)

    def ack_child_result(self, run_id, consumer_mission_id, message_id):
        """Idempotently acknowledge one outcome after it is folded into Mission case."""
        now = int(time.time())
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT m.state FROM agent_mailbox m JOIN agent_runs r ON r.run_id=m.run_id "
                "WHERE m.message_id=? AND m.run_id=? AND m.kind='child_result' "
                "AND r.mission_id=?",
                (int(message_id), run_id, str(consumer_mission_id or ""))).fetchone()
            if not row:
                self.db.rollback()
                return False
            if row["state"] == "acked":
                self.db.commit()
                return True
            cur = self.db.execute(
                "UPDATE agent_mailbox SET state='acked',acked_at=? WHERE message_id=? "
                "AND run_id=? AND kind='child_result' AND state='delivered'",
                (now, int(message_id), run_id))
            if cur.rowcount:
                self._event_locked(
                    run_id, "child_result_acknowledged", {"message_id": message_id}, now)
            self.db.commit()
        return cur.rowcount == 1

    def ack_message(self, run_id, token, message_id):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_mailbox SET state='acked',acked_at=? WHERE message_id=? "
                "AND run_id=? AND state='delivered' AND EXISTS (SELECT 1 FROM agent_runs "
                "WHERE run_id=? AND owner_token=? AND status IN (?,?))",
                (now, int(message_id), run_id, run_id, token, RUNNING, CANCEL_REQUESTED))
            if cur.rowcount:
                self._event_locked(run_id, "message_acknowledged", {"message_id": message_id}, now)
            self.db.commit()
        return cur.rowcount == 1

    def ack_cancel(self, run_id, token, result="cancelled by worker"):
        now = int(time.time())
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_runs SET status=?,result=?,owner_token='',lease_until=0,"
                "cancel_ack_at=?,updated_at=? WHERE run_id=? AND status=? AND owner_token=?",
                (CANCELLED, str(result)[:4000], now, now, run_id, CANCEL_REQUESTED, token))
            if cur.rowcount:
                self.db.execute(
                    "UPDATE agent_mailbox SET state='acked',acked_at=? WHERE run_id=? "
                    "AND kind='cancel' AND state IN ('queued','delivered')", (now, run_id))
                self._event_locked(run_id, "cancel_acknowledged", {}, now)
                self._notify_locked(run_id, "cancelled", {"acknowledged": True}, now)
                self._queue_child_result_locked(run_id, CANCELLED, result, now)
            self.db.commit()
        return cur.rowcount == 1

    def is_descendant(self, parent_run_id, target_run_id):
        """Return true only when target is below parent in the immutable run tree."""
        if not parent_run_id or not target_run_id or parent_run_id == target_run_id:
            return False
        with self.lock:
            row = self.db.execute(
                "WITH RECURSIVE descendants(run_id) AS ("
                "SELECT run_id FROM agent_runs WHERE parent_run_id=? UNION ALL "
                "SELECT child.run_id FROM agent_runs child JOIN descendants parent "
                "ON child.parent_run_id=parent.run_id) "
                "SELECT 1 FROM descendants WHERE run_id=? LIMIT 1",
                (parent_run_id, target_run_id)).fetchone()
        return bool(row)

    def send_to_descendant(self, sender_run_id, target_run_id, text):
        """Queue a steer without allowing a model to address peers or ancestors."""
        if not self.is_descendant(sender_run_id, target_run_id):
            raise ValueError("specialist target is outside caller descendant scope")
        return self.steer(target_run_id, text, sender_run_id)

    def cancel_descendant(self, sender_run_id, target_run_id):
        """Cancel only authority delegated below the calling run."""
        if not self.is_descendant(sender_run_id, target_run_id):
            raise ValueError("specialist target is outside caller descendant scope")
        return self.request_cancel(target_run_id, sender_run_id)

    def account_usage(self, run_id, token, *, input_tokens=0, output_tokens=0,
                      cost_usd=0.0, wall_ms=0, retries=0):
        """Charge a specialist and every ancestor, preventing fan-out budget escape."""
        run = self.get(run_id)
        if not run:
            return []
        ancestry = []
        cursor = run
        while cursor:
            ancestry.append(cursor["run_id"])
            cursor = self.get(cursor["parent_run_id"]) if cursor["parent_run_id"] else None
        with self.lock:
            owner = self.db.execute(
                "SELECT 1 FROM agent_runs WHERE run_id=? AND owner_token=? "
                "AND status IN (?,?)", (run_id, token, RUNNING, CANCEL_REQUESTED)).fetchone()
            if not owner:
                return ["run ownership lost"]
            marks = ",".join("?" for _ in ancestry)
            self.db.execute(
                "UPDATE agent_runs SET input_tokens=input_tokens+?,output_tokens=output_tokens+?,"
                "model_cost_microusd=model_cost_microusd+?,active_wall_ms=active_wall_ms+?,"
                "retry_count=retry_count+? WHERE run_id IN (%s)" % marks,
                (max(0, int(input_tokens)), max(0, int(output_tokens)),
                 max(0, int(round(float(cost_usd) * 1_000_000))), max(0, int(wall_ms)),
                 max(0, int(retries)), *ancestry))
            self.db.commit()
        return [(rid, self.budget_reason(rid)) for rid in ancestry if self.budget_reason(rid)]

    def budget_reason(self, run_id):
        run = self.get(run_id)
        if not run:
            return "run missing"
        leash = run["leash"]
        checks = (
            (run["input_tokens"] + run["output_tokens"] >=
             int(leash.get("max_model_tokens", 2_000_000)), "model-token budget exhausted"),
            (run["model_cost_usd"] >= float(leash.get("max_model_cost_usd", 25)),
             "model-cost budget exhausted"),
            (run["active_wall_ms"] >= int(leash.get("max_active_wall_seconds", 21600)) * 1000,
             "active wall-time budget exhausted"),
            (run["retry_count"] >= int(leash.get("max_retries", 32)),
             "retry budget exhausted"),
            (int(time.time()) - int(run["created_at"]) >=
             int(leash.get("max_elapsed_seconds", 2_592_000)),
             "elapsed-time budget exhausted"),
            (self.storage_bytes(run_id) >= int(leash.get("max_storage_bytes", 5_000_000)),
             "durable-storage budget exhausted"),
        )
        return next((reason for hit, reason in checks if hit), "")

    def storage_bytes(self, run_id):
        with self.lock:
            row = self.db.execute(
                "SELECT LENGTH(CAST(task AS BLOB))+LENGTH(CAST(leash_json AS BLOB))+"
                "LENGTH(CAST(resources_json AS BLOB))+LENGTH(CAST(result AS BLOB)) n "
                "FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            events = self.db.execute(
                "SELECT COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0) n "
                "FROM agent_events WHERE run_id=?", (run_id,)).fetchone()
            mailbox = self.db.execute(
                "SELECT COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0) n "
                "FROM agent_mailbox WHERE run_id=? OR sender_run_id=?",
                (run_id, run_id)).fetchone()
        return int(row["n"] or 0) + int(events["n"] or 0) + int(mailbox["n"] or 0) \
            if row else 0

    def recover_stale(self, now=None):
        now = int(now if now is not None else time.time())
        changed = 0
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT run_id,owner_token,status FROM agent_runs "
                "WHERE status IN (?,?) AND owner_token<>'' "
                "AND lease_until>0 AND lease_until<=?", (RUNNING, CANCEL_REQUESTED, now)).fetchall()
            for row in rows:
                cur = self.db.execute(
                    "UPDATE agent_runs SET status=?,owner_token='',lease_until=0,result=?,updated_at=? "
                    "WHERE run_id=? AND owner_token=? AND status IN (?,?) AND lease_until<=?",
                    (RECOVERY_REQUIRED,
                     "worker lease expired; inspect its worktree/resources before resume",
                     now, row["run_id"], row["owner_token"], RUNNING, CANCEL_REQUESTED, now))
                if not cur.rowcount:
                    continue
                replayed = self.db.execute(
                    "UPDATE agent_mailbox SET state='queued',delivered_at=0 "
                    "WHERE run_id=? AND state='delivered' AND acked_at=0",
                    (row["run_id"],)).rowcount
                self._event_locked(row["run_id"], "recovery_required", {
                    "previous_status": row["status"], "messages_requeued": replayed}, now)
                self._notify_locked(row["run_id"], "recovery_required", {}, now)
                changed += 1
            self.db.commit()
        return changed

    def can_access(self, run_id, resource, mode="write"):
        """Check declared scope and active descendant ownership before a tool call."""
        run = self.get(run_id)
        if not run:
            return False, "run missing"
        wanted = _normalize_resource({**(resource if isinstance(resource, dict) else
                                         {"kind": "file", "id": resource}), "mode": mode})
        if not any(_resource_contains(owned, wanted) for owned in run["resources"]):
            return False, "resource is outside run ownership"
        if mode == "write":
            with self.lock:
                rows = self.db.execute(
                    "SELECT run_id,resources_json FROM agent_runs WHERE root_run_id=? "
                    "AND run_id<>? AND status NOT IN (?,?,?)",
                    (run["root_run_id"], run_id, COMPLETED, FAILED, CANCELLED)).fetchall()
            descendants = {child["run_id"] for child in self.tree(run_id)["flat"]
                           if child["run_id"] != run_id}
            for row in rows:
                if row["run_id"] not in descendants:
                    continue
                for delegated in _jl(row["resources_json"], []):
                    if (_resource_contains(delegated, wanted) or
                            _resource_contains(wanted, delegated)):
                        return False, "write ownership delegated to %s" % row["run_id"]
        return True, "owned"

    def events(self, run_id, limit=100):
        with self.lock:
            rows = self.db.execute(
                "SELECT event_id,kind,payload_json,at FROM agent_events WHERE run_id=? "
                "ORDER BY event_id DESC LIMIT ?", (run_id, max(1, int(limit)))).fetchall()
        return [{"event_id": row["event_id"], "kind": row["kind"],
                 "payload": _jl(row["payload_json"]), "at": row["at"]}
                for row in reversed(rows)]

    def list_runs(self, status=None, *, specialists_only=False):
        # ``specialists_only`` is the scheduler's polling API (rather than the
        # read-only Activity/control-plane listing).  Make that poll the
        # durable recovery clock as well, otherwise a process crash can leave
        # a specialist in RUNNING forever: no queued row exists for the
        # dispatcher to claim and nothing else invokes recover_stale().
        if specialists_only:
            self.recover_stale()
        where, args = [], []
        if status:
            states = (status,) if isinstance(status, str) else tuple(status)
            where.append("status IN (%s)" % ",".join("?" for _ in states))
            args.extend(states)
        if specialists_only:
            where.append("parent_run_id<>''")
        query = "SELECT * FROM agent_runs"
        if where:
            query += " WHERE " + " AND ".join(where)
        with self.lock:
            rows = self.db.execute(query + " ORDER BY created_at,run_id", args).fetchall()
        return [self._decode(row) for row in rows]

    def notifications(self, state="queued", limit=100):
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM agent_notifications WHERE state=? "
                "ORDER BY notification_id LIMIT ?", (state, max(1, int(limit)))).fetchall()
        return [{**dict(row), "payload": _jl(row["payload_json"])} for row in rows]

    def ack_notification(self, notification_id):
        with self.lock:
            cur = self.db.execute(
                "UPDATE agent_notifications SET state='acked',acked_at=? "
                "WHERE notification_id=? AND state='queued'",
                (int(time.time()), int(notification_id)))
            self.db.commit()
        return cur.rowcount == 1

    def tree(self, run_id):
        root = self.get(run_id)
        if not root:
            return {"root": None, "flat": []}
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM agent_runs WHERE root_run_id=? ORDER BY depth,created_at,run_id",
                (root["root_run_id"],)).fetchall()
        decoded = [self._decode(row) for row in rows]
        wanted = {run_id}
        changed = True
        while changed:
            changed = False
            for row in decoded:
                if row["parent_run_id"] in wanted and row["run_id"] not in wanted:
                    wanted.add(row["run_id"])
                    changed = True
        flat = [row for row in decoded if row["run_id"] in wanted]
        return {"root": self.get(run_id), "flat": flat}

    def close(self):
        with self.lock:
            self.db.close()
