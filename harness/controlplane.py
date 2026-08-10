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
            limit=limit, directory=os.path.join(root, "sessions"))
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


def health(path: str | None = None, *, probe_services: bool = True) -> dict:
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
    with OpsStore(os.path.join(root, "ops.db")) as store:
        report = aggregate_health(store, desired_workers=desired, state_dir=root,
                                  probe_services=probe_services)
    report["supervisor"] = query_windows(root=root)
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
    automation_recovery = [{
        "kind": "automation", "execution_id": row.get("execution_id"),
        "automation_id": row.get("automation_id"), "state": row.get("state"),
        "last_error": row.get("last_error"),
    } for row in work["automations"] if row.get("state") == "needs_you"]
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
    if config_error:
        report["activity_errors"]["supervisor_config"] = config_error
        report["ok"] = False
        if report.get("status") == "ok":
            report["status"] = "degraded"
    if report["work"]["recovery_required"]:
        report["ok"], report["status"] = False, "needs_you"
    return report
