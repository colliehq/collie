"""Read-only operational control plane shared by Collie's CLI and Web UI.

The worker implementations intentionally remain independent: a broken Mission DB must not make
session recovery disappear, and a missing automation DB is normal on a fresh install.  This module
therefore gathers each lane best-effort and reports lane-local errors instead of turning one
optional subsystem into a single point of failure.
"""
from __future__ import annotations

import os
import time


def state_dir(path: str | None = None) -> str:
    return os.path.abspath(path or os.environ.get("COLLIE_STATE_DIR")
                           or os.path.expanduser("~/.collie"))


def _mission_row(m) -> dict:
    return {
        "mission_id": m.mission_id, "goal": m.goal, "state": m.state,
        "result": m.result, "updated_at": m.updated_at,
        "lane": (m.case or {}).get("_lane", "mission"),
    }


def activity(path: str | None = None, *, limit: int = 100) -> dict:
    """Return durable work across interactive, Mission, specialist and automation lanes."""
    root, limit = state_dir(path), max(1, min(1000, int(limit)))
    out = {"at": time.time(), "state_dir": root, "sessions": [], "missions": [],
           "task_runs": [], "automations": [], "notifications": [], "errors": {}}

    try:
        from . import sessions
        out["sessions"] = sessions.active_runs(
            limit=limit, directory=sessions.store_root(root))
    except Exception as exc:
        out["errors"]["sessions"] = "%s: %s" % (type(exc).__name__, exc)

    mission_db = os.path.join(root, "jobs.db")
    if os.path.exists(mission_db):
        try:
            from .mission import MissionStore
            store = MissionStore(mission_db)
            try:
                out["missions"] = [_mission_row(m) for m in store.list()][-limit:]
            finally:
                store.close()
        except Exception as exc:
            out["errors"]["missions"] = "%s: %s" % (type(exc).__name__, exc)

    tree_db = os.path.join(root, "tasktree.db")
    if os.path.exists(tree_db):
        try:
            from .tasktree import TaskTreeStore
            tree = TaskTreeStore(tree_db)
            try:
                rows = tree.list_runs()
                out["task_runs"] = rows[-limit:]
                out["notifications"] = tree.notifications(limit=limit)
            finally:
                tree.close()
        except Exception as exc:
            out["errors"]["task_runs"] = "%s: %s" % (type(exc).__name__, exc)

    automation_db = os.path.join(root, "automations.db")
    if os.path.exists(automation_db):
        try:
            from .automations import AutomationStore
            store = AutomationStore(automation_db)
            try:
                out["automations"] = store.executions()[-limit:]
            finally:
                store.close()
        except Exception as exc:
            out["errors"]["automations"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def health(path: str | None = None, *, probe_services: bool = True,
           web_port: int | None = None) -> dict:
    """Aggregate supervisor facts plus durable work that requires human recovery."""
    root = state_dir(path)
    from .ops import OpsStore, aggregate_health
    from .supervisor import config_path, default_config, load_config, query_windows

    config_error = ""
    try:
        config = load_config(config_path(root))
    except Exception as exc:
        config_error = "%s: %s" % (type(exc).__name__, exc)
        config = default_config(root)
    desired = [row["name"] for row in config.get("workers", []) if row.get("enabled", True)]
    supervisor_info = query_windows(root=root)
    with OpsStore(os.path.join(root, "ops.db")) as store:
        # Background workers are only expected where something keeps them running: the Windows
        # installer's supervisor task, or any supervisor that has run against this state (it
        # leaves a heartbeat). The macOS app and a pip `collie web` have none, and listing the
        # generated default workers there reported all five "missing" -- "Needs attention" for
        # good on every Mac, with nothing to fix.
        supervised = bool(supervisor_info.get("installed") or
                          "supervisor" in store.heartbeats())
        report = aggregate_health(store, desired_workers=desired if supervised else [],
                                  state_dir=root, probe_services=probe_services,
                                  web_port=web_port)
    report["supervisor"] = supervisor_info
    report["supervised"] = supervised
    work = activity(root, limit=250)
    session_recovery = [{
        "kind": "interactive", "session_id": row.get("session_id"),
        "run_id": row.get("run_id"), "state": row.get("state"),
        "reason": row.get("recovery_reason") or row.get("reason"),
    } for row in work["sessions"] if row.get("recovery_required")]
    task_recovery = [{
        "kind": "specialist", "run_id": row.get("run_id"),
        "parent_run_id": row.get("parent_run_id"), "status": row.get("status"),
        "role": row.get("role"),
    } for row in work["task_runs"] if row.get("status") in (
        "recovery_required", "needs_you")]
    mission_recovery = [{
        "kind": "mission", "run_id": row.get("mission_id"),
        "state": row.get("state"), "lane": row.get("lane"),
    } for row in work["missions"] if row.get("state") in (
        "recovery_required", "needs_you")]
    # A finished needs_you automation run is a standing demand until somebody says they looked at
    # it.  Only that explicit, durable acknowledgement of this exact execution drops it out of the
    # lane: a later successful run of the same automation proves nothing about this one, and every
    # other state keeps its existing meaning.
    automation_recovery = [{
        "kind": "automation", "execution_id": row.get("execution_id"),
        "automation_id": row.get("automation_id"), "state": row.get("state"),
        "last_error": row.get("last_error"),
    } for row in work["automations"] if row.get("state") == "needs_you"
        and not float(row.get("attention_reviewed_at") or 0) > 0]
    report["work"] = {
        "interactive_active": len(work["sessions"]),
        "missions_active": sum(row.get("state") not in (
            "done_verified", "done_accepted", "failed", "cancelled")
            for row in work["missions"]),
        "task_runs_active": sum(row.get("status") not in (
            "completed", "failed", "cancelled") for row in work["task_runs"]),
        "automations_active": sum(row.get("state") in ("pending", "running", "needs_you")
                                  for row in work["automations"]),
        # Health is safe to show on an operations surface: identifiers and failure metadata only,
        # never prompts, automation request JSON, model output, or conversation content.
        "recovery_required": (session_recovery + mission_recovery + task_recovery +
                              automation_recovery),
    }
    report["activity_errors"] = work["errors"]
    reasons = report.setdefault("reasons", [])
    beat = (report.get("heartbeats") or {}).get("supervisor") or {}
    if supervised and not beat.get("fresh"):
        # Workers report through the supervisor. When it is not running, every worker is silent
        # for that one reason -- including the web server answering this request -- so say that
        # once instead of listing each worker as "not reporting".
        reasons[:] = [row for row in reasons if row.get("code") != "worker_not_reporting"]
        reasons.insert(0, {"code": "supervisor_not_running", "subject": "supervisor"})
    if config_error:
        report["activity_errors"]["supervisor_config"] = config_error
        report["ok"] = False
        if report.get("status") == "ok":
            report["status"] = "degraded"
        reasons.append({"code": "supervisor_config_unreadable", "subject": "supervisor"})
    if report["work"]["recovery_required"]:
        report["ok"], report["status"] = False, "needs_you"
        reasons.insert(0, {"code": "recovery_required", "subject": "work",
                           "count": len(report["work"]["recovery_required"])})
    return report
