"""Read-only product diagnostics and tightly-scoped operator repairs.

The ordinary health endpoint answers whether work can run.  Doctor answers the next question an
operator actually has: *which installed component is this, what is out of alignment, and what can I
do about it?*  Reports contain paths, versions, counts and commands, never credential values,
notification content, prompts or model output.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

from . import __version__, plat


def _version(text: str) -> str:
    match = re.search(r"(?<!\d)(\d+\.\d+(?:\.\d+)?)(?!\d)", str(text or ""))
    return match.group(1) if match else ""


def _command_version(path: str, timeout: float = 5.0) -> dict:
    if not path:
        return {"path": "", "version": "", "ok": False, "error": "not found on PATH"}
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, **plat.no_window_kwargs())
        output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        return {"path": os.path.abspath(path), "version": _version(output),
                "ok": result.returncode == 0, "error": "" if result.returncode == 0 else
                (output[:300] or "version command failed")}
    except Exception as exc:
        return {"path": os.path.abspath(path), "version": "", "ok": False,
                "error": "%s: %s" % (type(exc).__name__, str(exc)[:240])}


def _get_json(url: str, timeout: float = 1.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            value = json.loads(response.read(65536) or b"{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _shipped_extension_version() -> str:
    path = os.path.join(os.path.dirname(__file__), "browser_ext", "manifest.json")
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return str(value.get("version") or "") if isinstance(value, dict) else ""
    except (OSError, ValueError, TypeError):
        return ""


def _check(check_id: str, status: str, title: str, detail: str,
           *, action: str = "", command: str = "") -> dict:
    return {"id": check_id, "status": status, "title": title, "detail": detail,
            "action": action, "command": command}


def report(state_root: str | None = None, *, probe_services: bool = True,
           now: float | None = None) -> dict:
    """Return one content-free version/health matrix and actionable check list."""
    from .controlplane import health, state_dir
    from .supervisor import remote_notifications_enabled
    from .update import install_kind, rollback_status

    now = float(time.time() if now is None else now)
    root = state_dir(state_root)
    active = _command_version(shutil.which("collie") or "")
    bridge = _get_json("http://127.0.0.1:8677/health") if probe_services else {}
    shipped_extension = _shipped_extension_version()
    loaded_extension = str(bridge.get("extension_version") or "")
    runtime = {
        "source": {"version": __version__, "package": os.path.abspath(os.path.dirname(__file__)),
                   "python": os.path.abspath(sys.executable)},
        "command": active,
        "install_kind": install_kind(),
        "browser_bridge": {
            "version": str(bridge.get("backend_version") or ""),
            "ok": bool(bridge.get("ok")),
        },
        "browser_extension": {
            "shipped": shipped_extension, "loaded": loaded_extension,
            "connected": bool(bridge.get("extension_connected")),
        },
    }
    checks = []
    if active.get("version") and active["version"] != __version__:
        checks.append(_check(
            "version-drift", "needs_you", "Installed Collie is not this source build",
            "The command on PATH is %s while this control plane is %s." %
            (active["version"], __version__), action="update",
            command="collie update --yes"))
    elif not active.get("ok"):
        checks.append(_check("command-missing", "warning", "Collie command is unavailable",
                             active.get("error") or "No collie command was found on PATH."))
    else:
        checks.append(_check("version-aligned", "ok", "Collie versions are aligned",
                             "The active command and this source report %s." % __version__))
    if loaded_extension and shipped_extension and loaded_extension != shipped_extension:
        checks.append(_check(
            "extension-drift", "needs_you", "Browser extension needs reload",
            "Chrome is running %s; the shipped extension is %s." %
            (loaded_extension, shipped_extension), action="reload_browser_extension"))
    elif bridge and not bridge.get("extension_connected"):
        checks.append(_check("extension-disconnected", "warning", "Browser extension is disconnected",
                             "The bridge is running but no recent extension poll was observed."))

    health_report = health(root, probe_services=probe_services)
    notifications_enabled = remote_notifications_enabled()
    queue = ((health_report.get("queues") or {}).get("notifications") or {})
    if notifications_enabled and queue.get("stale"):
        checks.append(_check(
            "notification-backlog", "needs_you", "Notifications are not draining",
            "%d notifications have waited for about %d minutes." % (
                int(queue.get("live") or 0),
                int(float(queue.get("oldest_pending_age_s") or 0) / 60)),
            action="test_notifications"))
    for name, row in (health_report.get("heartbeats") or {}).items():
        if (notifications_enabled and name in ("notification-pump", "remote") and
                not row.get("fresh")):
            checks.append(_check(
                "stale-heartbeat:" + name, "warning", "%s heartbeat is stale" % name,
                "The last heartbeat was about %d seconds ago." % int(row.get("age_s") or 0),
                command="collie supervisor status"))
    rollback = rollback_status()
    if rollback.get("required"):
        checks.append(_check("rollback-required", "needs_you", "Update rollback needs review",
                             str(rollback.get("last_error") or "startup checks failed")[:300]))

    databases = []
    for name in ("ops.db", "jobs.db", "tasktree.db", "automations.db", "memory.db", "runs.db"):
        path = os.path.join(root, name)
        databases.append({"name": name, "present": os.path.isfile(path),
                          "bytes": os.path.getsize(path) if os.path.isfile(path) else 0})
    severity = {"ok": 0, "warning": 1, "needs_you": 2, "error": 3}
    worst = max((severity.get(row["status"], 0) for row in checks), default=0)
    return {
        "at": now, "ok": worst == 0 and bool(health_report.get("ok")),
        "status": ("needs_you" if worst >= 2 else "warning" if worst == 1 else
                   str(health_report.get("status") or "ok")),
        "runtime": runtime, "checks": checks, "databases": databases,
        "health": health_report, "rollback": rollback,
    }


def repair(action: str, state_root: str | None = None, *, confirmed: bool = False) -> dict:
    """Execute only bounded, reversible control-plane repairs.

    Installing code, restarting arbitrary processes and reconciling uncertain side effects are
    intentionally absent.  Those remain explicit updater/recovery workflows.
    """
    from .controlplane import state_dir
    from .ops import OpsStore

    root = state_dir(state_root)
    action = str(action or "").strip()
    if action == "test_notifications":
        with OpsStore(os.path.join(root, "ops.db")) as store:
            notification_id = store.enqueue(
                "doctor_test", "Collie notification test", "Doctor queued a delivery test.",
                severity="info", dedupe_key="doctor:test", cooldown_s=0)
        return {"ok": True, "action": action, "notification_id": notification_id}
    if action == "retry_dead_notifications":
        if not confirmed:
            raise ValueError("confirmed=true is required to retry dead notifications")
        with OpsStore(os.path.join(root, "ops.db")) as store:
            changed = store.retry_dead()
        return {"ok": True, "action": action, "retried": changed}
    if action == "reprobe_workers":
        from . import runner_registry
        runner_registry.reset_cache()
        probes = runner_registry.probe_all(keys=runner_registry.option_keys())
        return {"ok": True, "action": action,
                "workers": {key: value.to_dict() for key, value in probes.items()}}
    raise ValueError("unsupported doctor repair action")
