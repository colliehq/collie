"""Operator-facing control-center projections over Collie's durable stores.

Every function here is usable by both CLI tests and the authenticated Web UI.  Mutations are narrow
verbs with exact validation; read projections strip transcripts, model answers, automation request
payloads and secrets while retaining the facts an operator needs to make a decision.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid


def _root(path=None) -> str:
    from .controlplane import state_dir
    return state_dir(path)


def _bounded(value, limit=1000) -> str:
    from .runner_specs import redact_text
    return redact_text(str(value or ""), int(limit))


def recovery_snapshot(path=None) -> dict:
    """Unify every decision/recovery lane with an explicit next action."""
    from .controlplane import health
    from .doctor import report as doctor_report

    root = _root(path)
    doctor = doctor_report(root)
    state = doctor.get("health") or health(root)
    items = []
    labels = {
        "interactive": "Interrupted interactive action",
        "mission": "Mission recovery required",
        "specialist": "Specialist needs attention",
        "automation": "Automation outcome needs review",
    }
    for row in ((state.get("work") or {}).get("recovery_required") or []):
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "work")
        identity = str(row.get("session_id") or row.get("run_id") or
                       row.get("execution_id") or "")
        actions = []
        if kind == "interactive":
            actions = ["completed", "not_fired", "cancel"]
        elif kind == "mission":
            actions = ["inspect_mission"]
        elif kind == "specialist":
            actions = ["inspect_specialist", "cancel_specialist"]
        elif kind == "automation":
            actions = ["inspect_automation"]
        items.append({
            "id": "%s:%s" % (kind, identity), "kind": kind, "identity": identity,
            "severity": "needs_you", "title": labels.get(kind, "Recovery required"),
            "detail": _bounded(row.get("reason") or row.get("state") or row.get("status"), 500),
            "actions": actions,
        })
    queues = state.get("queues") or {}
    notification_queue = queues.get("notifications") or {}
    if notification_queue.get("stale") or notification_queue.get("dead"):
        items.append({
            "id": "service:notifications", "kind": "notification", "identity": "notifications",
            "severity": "needs_you" if notification_queue.get("dead") else "warning",
            "title": "Notification delivery needs attention",
            "detail": "%d live, %d dead; oldest live item is about %d minutes old" % (
                int(notification_queue.get("live") or 0), int(notification_queue.get("dead") or 0),
                int(float(notification_queue.get("oldest_pending_age_s") or 0) / 60)),
            "actions": (["test_notifications"] +
                        (["retry_dead_notifications"] if notification_queue.get("dead") else [])),
        })
    for name in ("notification-pump", "remote"):
        heartbeat = (state.get("heartbeats") or {}).get(name) or {}
        if heartbeat and not heartbeat.get("fresh"):
            items.append({
                "id": "service:" + name, "kind": "service", "identity": name,
                "severity": "warning", "title": "%s is stale" % name,
                "detail": "No fresh heartbeat for about %d seconds" %
                          int(heartbeat.get("age_s") or 0),
                "actions": ["inspect_doctor"],
            })
    for check in doctor.get("checks") or []:
        if check.get("status") not in ("warning", "needs_you", "error"):
            continue
        if check.get("id") in {item["id"] for item in items}:
            continue
        items.append({
            "id": "doctor:" + str(check.get("id") or "check"), "kind": "doctor",
            "identity": str(check.get("id") or ""), "severity": check.get("status"),
            "title": check.get("title"), "detail": check.get("detail"),
            "actions": [check["action"]] if check.get("action") else ["inspect_doctor"],
            "command": check.get("command") or "",
        })
    return {
        "at": time.time(), "status": "needs_you" if any(
            row["severity"] in ("needs_you", "error") for row in items) else
            "warning" if items else "ok",
        "summary": {
            "total": len(items),
            "needs_you": sum(row["severity"] in ("needs_you", "error") for row in items),
            "warning": sum(row["severity"] == "warning" for row in items),
        },
        "items": items,
    }


AUTOMATION_RECIPES = (
    {"id": "daily-repo-review", "label": "Daily repository review",
     "description": "Review the current project each day and report evidence.",
     "spec": {"automation_id": "daily-repo-review", "task": "Review this repository for high-impact problems and report scoped evidence.",
              "trigger": {"provider": "timer", "every_s": 86400},
              "workspace": {"mode": "isolated"}, "permissions": {}, "enabled": False}},
    {"id": "release-readiness", "label": "Release readiness",
     "description": "Run a release-readiness review when the changelog changes.",
     "spec": {"automation_id": "release-readiness", "task": "Check release readiness and return the exact checks and remaining risks.",
              "trigger": {"provider": "file", "path": "CHANGELOG.md",
                          "predicate": {"type": "changed"}},
              "workspace": {"mode": "isolated"}, "permissions": {"read_roots": ["."]},
              "enabled": False}},
    {"id": "page-monitor", "label": "Page monitor",
     "description": "Watch an HTTPS page for a content change.",
     "spec": {"automation_id": "page-monitor", "task": "Summarize the monitored page change and explain whether action is needed.",
              "trigger": {"provider": "page", "url": "https://example.com",
                          "predicate": {"type": "changed"}},
              "workspace": {"mode": "isolated"},
              "permissions": {"network_hosts": ["example.com"]}, "enabled": False}},
    {"id": "webhook-triage", "label": "Webhook triage",
     "description": "Turn an authenticated webhook into a bounded triage run.",
     "spec": {"automation_id": "webhook-triage", "task": "Triage this authenticated event and report the recommended next action.",
              "trigger": {"provider": "webhook"}, "workspace": {"mode": "isolated"},
              "permissions": {"webhook_ingest": True}, "enabled": False}},
)


def _public_execution(row: dict, usage: dict | None = None) -> dict:
    keys = ("execution_id", "automation_id", "event_id", "state", "attempts",
            "created_at", "updated_at", "started_at", "finished_at")
    out = {key: row.get(key) for key in keys if row.get(key) is not None}
    if row.get("last_error"):
        out["last_error"] = _bounded(row.get("last_error"), 500)
    out["usage"] = {key: (usage or {}).get(key, 0) for key in
                    ("model_tokens", "cost_usd", "actions", "wall_s")}
    return out


def automation_snapshot(path=None, automation_id: str = "") -> dict:
    from .automations import AutomationStore, TriggerRegistry

    db = os.path.join(_root(path), "automations.db")
    with AutomationStore(db) as store:
        specs = [spec.as_dict() for spec in store.specs(enabled_only=False)]
        if automation_id:
            specs = [row for row in specs if row["automation_id"] == automation_id]
        executions = store.executions(automation_id)[-200:]
        public = [_public_execution(row, store.usage(row["execution_id"]))
                  for row in reversed(executions)]
    return {"at": time.time(), "providers": TriggerRegistry().names(),
            "specs": specs, "executions": public, "recipes": list(AUTOMATION_RECIPES)}


def automation_upsert(spec: dict, path=None) -> dict:
    from .automations import AutomationSpec, AutomationStore
    parsed = AutomationSpec.from_dict(spec)
    with AutomationStore(os.path.join(_root(path), "automations.db")) as store:
        saved = store.upsert(parsed)
    return {"ok": True, "spec": saved.as_dict()}


def automation_preview(spec: dict | None = None, path=None, *, automation_id: str = "",
                       now: float | None = None) -> dict:
    from .automations import AutomationSpec, AutomationStore, TriggerRegistry
    now = float(time.time() if now is None else now)
    with AutomationStore(os.path.join(_root(path), "automations.db")) as store:
        parsed = AutomationSpec.from_dict(spec) if spec is not None else store.spec(automation_id)
        if parsed is None:
            raise KeyError("automation is not configured")
        cursor = store.cursor(parsed.automation_id)
        result = TriggerRegistry().get(str(parsed.trigger.get("provider"))).evaluate(
            parsed, cursor, now)
    event = result.event if isinstance(result.event, dict) else {}
    # Trigger metadata is already hashes/paths/URLs and contains no page/file body.
    return {"ok": True, "automation_id": parsed.automation_id,
            "fired": bool(result.fired), "event_id": result.event_id,
            "event": event, "next_cursor": result.cursor, "persisted": False}


def automation_set_enabled(automation_id: str, enabled: bool, path=None) -> dict:
    from .automations import AutomationStore
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be boolean")
    with AutomationStore(os.path.join(_root(path), "automations.db")) as store:
        spec = store.spec(str(automation_id or ""))
        if spec is None:
            raise KeyError("automation is not configured")
        value = spec.as_dict(); value["enabled"] = enabled
        saved = store.upsert(value)
    return {"ok": True, "spec": saved.as_dict()}


def automation_run_now(automation_id: str, path=None, *, confirmed: bool = False,
                       now: float | None = None) -> dict:
    from .automations import AutomationStore
    if not confirmed:
        raise ValueError("confirmed=true is required to queue an automation run")
    now = float(time.time() if now is None else now)
    with AutomationStore(os.path.join(_root(path), "automations.db")) as store:
        spec = store.spec(str(automation_id or ""))
        if spec is None:
            raise KeyError("automation is not configured")
        event_id = "manual:%s:%s" % (spec.automation_id, uuid.uuid4().hex)
        execution_id = store.enqueue(spec, event_id, {"kind": "manual", "observed_at": now},
                                     now=now)
    return {"ok": True, "automation_id": spec.automation_id,
            "execution_id": execution_id, "queued": True}


def _memory_path(path=None) -> str:
    return os.path.join(_root(path), "data", "memory.db")


def _public_claim(row: dict) -> dict:
    keys = ("id", "project", "text", "keys", "importance", "created_at", "superseded_by",
            "status", "source", "evidence", "provenance", "scope", "review_source",
            "review_evidence", "review_provenance", "reviewed_at")
    out = {key: row.get(key) for key in keys if row.get(key) is not None}
    for key in ("text", "keys", "evidence", "provenance", "review_evidence",
                "review_provenance"):
        if key in out:
            out[key] = _bounded(out[key], 4000 if key == "text" else 1000)
    return out


def memory_snapshot(path=None, *, status: str | None = None, project: str | None = None,
                    limit: int = 200) -> dict:
    from .memory import SqliteMemory
    db = _memory_path(path)
    if not os.path.isfile(db):
        return {"at": time.time(), "claims": [], "counts": {}}
    memory = SqliteMemory(db, embedder=None)
    try:
        claims = [_public_claim(row) for row in memory.list_claims(
            status=status, project=project, limit=limit)]
        counts = {str(row[0]): int(row[1]) for row in memory.db.execute(
            "SELECT status,count(*) FROM facts GROUP BY status")}
    finally:
        memory.close()
    return {"at": time.time(), "claims": claims, "counts": counts}


def memory_review(memory_id: int, action: str, path=None, *, note: str = "",
                  confirmed: bool = False) -> dict:
    from .memory import SqliteMemory
    if not confirmed:
        raise ValueError("confirmed=true is required to review memory")
    if isinstance(memory_id, bool) or int(memory_id) <= 0:
        raise ValueError("memory_id must be a positive integer")
    action = str(action or "")
    if action not in ("approve", "attest", "reject", "invalidate"):
        raise ValueError("unsupported memory review action")
    memory = SqliteMemory(_memory_path(path), embedder=None)
    try:
        claim = memory.get_claim(int(memory_id))
        if claim is None:
            raise KeyError("memory claim does not exist")
        evidence = str(note or "Web control-center review")[:1000]
        if action in ("approve", "attest"):
            changed = memory.promote(int(memory_id), status="attested", evidence=evidence,
                                     review_source="local_user",
                                     review_provenance="Collie Memory Review")
        elif action == "reject":
            changed = memory.reject(int(memory_id), evidence=evidence,
                                    review_source="local_user",
                                    review_provenance="Collie Memory Review")
        else:
            changed = memory.invalidate(int(memory_id), evidence=evidence,
                                        review_source="local_user",
                                        review_provenance="Collie Memory Review")
        if not changed:
            raise ValueError("claim state does not allow this review action")
        reviewed = memory.get_claim(int(memory_id))
    finally:
        memory.close()
    return {"ok": True, "claim": _public_claim(reviewed)}


def budget_snapshot(path=None, *, live_quota: bool = False) -> dict:
    """Aggregate content-free Mission, automation, run-history and quota usage."""
    root = _root(path)
    missions = []
    mission_db = os.path.join(root, "jobs.db")
    if os.path.isfile(mission_db):
        from .mission import MissionStore
        store = MissionStore(mission_db)
        try:
            for mission in store.list():
                usage = store.aggregate_runtime(mission.mission_id)
                leash = mission.leash or {}
                total_tokens = sum(int(usage.get(key) or 0) for key in
                                   ("input_tokens", "output_tokens", "cache_tokens"))
                missions.append({
                    "mission_id": mission.mission_id, "state": mission.state,
                    "limits": {"tokens": int(leash.get("max_model_tokens", 2_000_000)),
                               "cost_usd": float(leash.get("max_model_cost_usd", 25.0)),
                               "calls": int(leash.get("max_model_calls",
                                                       leash.get("max_total_steps", 1000)))},
                    "usage": {"tokens": total_tokens,
                              "cost_usd": float(usage.get("model_cost_usd") or 0),
                              "calls": int(usage.get("model_calls") or 0)},
                })
        finally:
            store.close()
    automations = []
    automation_db = os.path.join(root, "automations.db")
    if os.path.isfile(automation_db):
        from .automations import AutomationStore
        with AutomationStore(automation_db) as store:
            for spec in store.specs(enabled_only=False):
                executions = store.executions(spec.automation_id)
                usages = [store.usage(row["execution_id"]) for row in executions]
                automations.append({
                    "automation_id": spec.automation_id, "enabled": spec.enabled,
                    "limits": spec.budget,
                    "usage": {"runs": len(executions),
                              "tokens": sum(int(row.get("model_tokens") or 0) for row in usages),
                              "cost_usd": round(sum(float(row.get("cost_usd") or 0)
                                                    for row in usages), 6),
                              "actions": sum(int(row.get("actions") or 0) for row in usages)},
                })
    history = {"runs": 0, "tokens": 0, "cost_usd": 0.0, "wall_ms": 0}
    runs_db = os.path.join(root, "data", "runs.db")
    if os.path.isfile(runs_db):
        uri = "file:%s?mode=ro" % os.path.abspath(runs_db).replace("\\", "/")
        try:
            db = sqlite3.connect(uri, uri=True, timeout=2)
            row = db.execute(
                "SELECT count(*),COALESCE(sum(total_tokens),0),COALESCE(sum(cost_usd),0),"
                "COALESCE(sum(wall_ms),0) FROM runs WHERE ts>=?", (int(time.time()) - 30*86400,)
            ).fetchone()
            history = {"runs": int(row[0]), "tokens": int(row[1]),
                       "cost_usd": round(float(row[2]), 6), "wall_ms": int(row[3])}
            db.close()
        except sqlite3.Error:
            pass
    from . import runner_registry, runner_signals
    keys = runner_registry.option_keys()
    probes = runner_registry.probe_all(keys=keys)
    signals = runner_signals.collect(keys, probes, runs_db=runs_db,
                                     live_quota=bool(live_quota)).to_dict()
    return {"at": time.time(), "missions": missions, "automations": automations,
            "last_30_days": history, "routes": signals,
            "quota_live": bool(live_quota)}


def security_snapshot(path=None) -> dict:
    """One inventory of standing authority and connected identities."""
    root = _root(path)
    from . import mcpclient, settings
    from .extensions import ExtensionStore
    from .overrides import RiskOverrideStore
    from .workidentity import public_connections

    values = settings.all_values()
    try:
        servers = mcpclient.status()
    except Exception as exc:
        servers = [{"name": "mcp", "status": "unavailable", "error": _bounded(exc, 300)}]
    try:
        extensions = ExtensionStore(root).list()
        extension_connections = ExtensionStore(root).connections()
    except Exception:
        extensions, extension_connections = [], []
    risk = RiskOverrideStore(os.path.join(root, "risk_overrides.json"))
    return {
        "at": time.time(),
        "browser": {"site_access": values.get("BROWSER_SITE_ACCESS", "all_except_sensitive"),
                    "sensitive_hosts": [item.strip() for item in str(
                        values.get("BROWSER_SENSITIVE_HOSTS", "")).split(",") if item.strip()]},
        "risk_overrides": [{"pattern": row.pattern, "risk": row.risk.value}
                           for row in risk.list()],
        "mcp": servers, "work_identities": public_connections(root),
        "extension_connections": extension_connections,
        "extensions": [{"id": row.get("id"), "name": row.get("name"),
                        "publisher": row.get("publisher"), "enabled": row.get("enabled"),
                        "active_version": row.get("active_version"),
                        "permissions": row.get("permissions") or {}}
                       for row in extensions],
    }


def security_revoke_risk(pattern: str, path=None, *, confirmed: bool = False) -> dict:
    """Remove one exact standing risk override; never accepts a glob as a selector."""
    from .overrides import RiskOverrideStore
    if not confirmed:
        raise ValueError("confirmed=true is required to revoke standing authority")
    pattern = str(pattern or "")
    if not pattern or len(pattern) > 300:
        raise ValueError("an exact risk-override pattern is required")
    store = RiskOverrideStore(os.path.join(_root(path), "risk_overrides.json"))
    return {"ok": True, "pattern": pattern, "removed": bool(store.unset(pattern))}


def snapshot(path=None) -> dict:
    """The lightweight Control Center landing payload (heavy quota is a separate request)."""
    root = _root(path)
    return {"at": time.time(), "recovery": recovery_snapshot(root),
            "automations": automation_snapshot(root),
            "memory": memory_snapshot(root, limit=100),
            "budgets": budget_snapshot(root, live_quota=False),
            "security": security_snapshot(root)}
