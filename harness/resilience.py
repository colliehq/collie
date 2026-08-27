"""Deterministic recovery matrix and restartable soak runner.

This module injects failures only into private temporary state.  It never sends
a model request, opens the network, changes the user's configuration, or kills a
process Collie did not create.  The matrix is therefore suitable for CI and for
``collie resilience matrix`` on a production host.

The soak command repeats the same invariants and checkpoints after every cycle.
Re-running it with the same report path resumes an unfinished campaign; a long
gap is recorded as a sleep/restart observation instead of being hidden.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import os
import re
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping

from . import __version__


SCHEMA = "collie-resilience/1"


def _finite(value: Any, name: str, *, minimum: float = 0.0) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(float(value)) or float(value) < minimum):
        raise ValueError("%s must be a finite number >= %s" % (name, minimum))
    return float(value)


def _atomic_json(path: str, value: Mapping[str, Any]) -> None:
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dict(value), handle, ensure_ascii=False, indent=2,
                      allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle, parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("non-finite JSON number: %s" % value)))
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


@dataclass(frozen=True)
class FaultResult:
    name: str
    status: str
    duration_ms: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _scenario(name: str, run: Callable[[str], str], root: str) -> FaultResult:
    began = time.monotonic()
    try:
        detail = str(run(root) or "invariant held")[:500]
        status = "PASS"
    except Exception as exc:
        status = "FAIL"
        detail = "%s: %s" % (type(exc).__name__, str(exc)[:400])
    return FaultResult(name, status, int((time.monotonic() - began) * 1000), detail)


def _notification_disconnect(root: str) -> str:
    from .ops import OpsStore

    path = os.path.join(root, "notifications.db")
    with OpsStore(path) as store:
        notification_id = store.enqueue(
            "completion", "private title", "private body", now=10)
        first = store.deliver_once(lambda _item: False, max_attempts=2, now=10)
        second = store.deliver_once(lambda _item: False, max_attempts=2, now=16)
        if first != {"sent": 0, "retried": 1, "dead": 0} or second["dead"] != 1:
            raise AssertionError("disconnect did not reach bounded dead-letter state")
        if store.retry_dead(notification_id, now=17) != 1:
            raise AssertionError("explicit dead-letter retry was not accepted")
        recovered = store.deliver_once(lambda _item: True, max_attempts=2, now=17)
        if recovered["sent"] != 1:
            raise AssertionError("notification did not drain after reconnect")
        state = store.db.execute(
            "SELECT state FROM notifications WHERE notification_id=?",
            (notification_id,)).fetchone()["state"]
        if state != "delivered":
            raise AssertionError("delivered notification is not durable")
    return "offline retry -> dead letter -> confirmed retry -> delivered"


def _automation_restart(root: str) -> str:
    from .automations import (AutomationSpec, AutomationStore, NEEDS_YOU,
                              PENDING, SUCCEEDED)

    path = os.path.join(root, "automations.db")

    def spec(aid: str, *, external: bool) -> AutomationSpec:
        return AutomationSpec.from_dict({
            "id": aid, "task": "resilience fixture",
            "trigger": {"provider": "timer", "every_s": 60},
            "execution": {"mode": "project" if external else "plan"},
            "workspace": {"mode": "isolated"},
            "permissions": {"read_roots": [root],
                            "external_writes": external},
            "budget": {"max_retries": 1},
        })

    with AutomationStore(path) as store:
        safe = store.upsert(spec("safe-restart", external=False), now=1)
        safe_id = store.enqueue(safe, "event-safe", {"kind": "timer"}, now=2)
        first = store.claim(lease_s=1, now=3)
        if not first or first["execution_id"] != safe_id:
            raise AssertionError("safe execution was not claimed")
        stale_token = first["lease_token"]

    # New store instance = daemon/process restart.  A read-only plan may retry,
    # but its old lease token must never be able to publish a late result.
    with AutomationStore(path) as store:
        if store.recover_expired(now=5) != 1:
            raise AssertionError("expired safe lease was not recovered")
        row = store.executions("safe-restart")[0]
        if row["state"] != PENDING:
            raise AssertionError("replay-safe work did not return to pending")
        if store.finish(safe_id, SUCCEEDED, {}, lease_token=stale_token, now=5):
            raise AssertionError("stale owner crossed the lease fence")
        second = store.claim(lease_s=10, now=6)
        if not second or second["lease_token"] == stale_token:
            raise AssertionError("reclaimed execution did not get a fresh token")
        if not store.finish(safe_id, SUCCEEDED, {"ok": True},
                            lease_token=second["lease_token"], now=7):
            raise AssertionError("fresh lease owner could not finish")

        unsafe = store.upsert(spec("unsafe-restart", external=True), now=8)
        unsafe_id = store.enqueue(unsafe, "event-unsafe", {"kind": "timer"}, now=9)
        claimed = store.claim(lease_s=1, now=10)
        if not claimed or claimed["execution_id"] != unsafe_id:
            raise AssertionError("unsafe execution was not claimed")
        unsafe_token = claimed["lease_token"]

    with AutomationStore(path) as store:
        if store.recover_expired(now=12) != 1:
            raise AssertionError("expired external-write lease was not recovered")
        row = store.executions("unsafe-restart")[0]
        if row["state"] != NEEDS_YOU:
            raise AssertionError("uncertain external writes were replayed automatically")
        if store.finish(unsafe_id, SUCCEEDED, {}, lease_token=unsafe_token, now=12):
            raise AssertionError("stale external-write owner crossed the lease fence")
    return "restart requeued read-only work; external writes parked; stale tokens fenced"


def _corrupt_durable_request(root: str) -> str:
    from .automations import AutomationSpec, AutomationStore, NEEDS_YOU

    path = os.path.join(root, "corrupt-automation.db")
    value = AutomationSpec.from_dict({
        "id": "corrupt-request", "task": "fixture",
        "trigger": {"provider": "timer", "every_s": 60},
        "permissions": {"read_roots": [root]},
    })
    with AutomationStore(path) as store:
        store.upsert(value, now=1)
        execution_id = store.enqueue(value, "event-corrupt", {}, now=2)
        store.db.execute("UPDATE executions SET request_json=? WHERE execution_id=?",
                         ('{"budget":NaN}', execution_id))
        store.db.commit()
        if store.claim(now=3) is not None:
            raise AssertionError("corrupt durable request was handed to a worker")
        row = store.executions("corrupt-request")[0]
        if row["state"] != NEEDS_YOU or "invalid durable request" not in row["last_error"]:
            raise AssertionError("corrupt request was not parked for inspection")
    return "non-standard JSON parked in needs_you before claim"


def _update_disk_and_rollback(root: str) -> str:
    from . import update

    path = os.path.join(root, "update-journal.json")
    update._write_update_journal({"schema": 1, "state": "healthy", "sentinel": "old"}, path)

    def disk_full(_source: str, _target: str) -> None:
        raise OSError("simulated disk full")

    try:
        update._write_update_journal(
            {"schema": 1, "state": "installing", "sentinel": "new"}, path,
            _replace=disk_full)
    except OSError:
        pass
    else:
        raise AssertionError("simulated disk-full replace unexpectedly succeeded")
    if update._read_update_journal(path).get("sentinel") != "old":
        raise AssertionError("failed atomic replace damaged the last good journal")

    update.begin_update_journal(
        artifact="Collie-Setup.exe", mode="windows", target_version="99.0.0",
        rollback={"kind": "verified_installer", "path": "previous.exe",
                  "sha256": "fixture"}, path=path, now=10)
    update.record_update_handoff(ok=True, path=path, now=11)
    for now in (12, 13, 14):
        update.record_startup_health(False, "fixture startup failure", path=path,
                                     now=now, failure_threshold=3)
    status = update.rollback_status(path)
    if not status["required"] or not status["available"]:
        raise AssertionError("repeated startup failures did not request declared rollback")
    return "disk-full preserved journal; third failed startup requested verified rollback"


def _extension_tamper(root: str) -> str:
    from .extensions import ExtensionError, ExtensionStore, scaffold_package, validate_package

    source = os.path.join(root, "extension")
    scaffold_package(source, "resilience.fixture", "Resilience fixture", "Collie tests")
    original = validate_package(source)
    skill = next(rel for rel in original["files"] if rel.endswith("SKILL.md"))
    with open(os.path.join(source, *skill.split("/")), "a", encoding="utf-8") as handle:
        handle.write("\nchanged after review\n")
    store = ExtensionStore(os.path.join(root, "extension-state"))
    try:
        store.install(source, expected_digest=original["digest"])
    except ExtensionError as exc:
        if "provenance pin" not in str(exc):
            raise
    else:
        raise AssertionError("tampered extension crossed its digest pin")
    if store.list():
        raise AssertionError("tampered extension left an installed record")
    return "post-review content mutation refused by exact digest pin"


def _owned_process_kill(root: str) -> str:
    from . import runner_compat

    report = runner_compat.run_matrix(
        ["codex-app-server"], checks=["cancel"], workspace=root)
    cell = report["runners"]["codex-app-server"]["checks"]["cancel"]
    if cell["status"] != runner_compat.PASS:
        raise AssertionError(cell.get("detail") or "cancel conformance failed")
    return "native interrupt ignored; owned process tree escalated and confirmed extinct"


_SCENARIOS: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("notification_disconnect", _notification_disconnect),
    ("automation_restart_lease", _automation_restart),
    ("corrupt_durable_request", _corrupt_durable_request),
    ("update_disk_full_rollback", _update_disk_and_rollback),
    ("extension_tamper", _extension_tamper),
    ("owned_process_kill", _owned_process_kill),
)


def scenario_names() -> tuple[str, ...]:
    return tuple(name for name, _run in _SCENARIOS)


def parse_duration(value: Any) -> float:
    """Parse ``90``, ``30s``, ``15m`` or ``12h`` into finite seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _finite(value, "duration", minimum=.01)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*",
                         str(value or ""), re.IGNORECASE)
    if not match:
        raise ValueError("duration must look like 30s, 15m, 12h, or 1d")
    scale = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    return _finite(float(match.group(1)) * scale[match.group(2).lower()],
                   "duration", minimum=.01)


def write_report(path: str, report: Mapping[str, Any]) -> str:
    path = os.path.abspath(path)
    _atomic_json(path, report)
    return path


def run_fault_matrix(*, scenarios: Iterable[str] | None = None,
                     scratch_root: str | None = None) -> dict[str, Any]:
    """Run selected isolated failure injections; return a publishable report."""
    wanted = set(scenario_names() if scenarios is None else (str(x) for x in scenarios))
    unknown = sorted(wanted - set(scenario_names()))
    if unknown:
        raise ValueError("unknown resilience scenario(s): %s" % ", ".join(unknown))
    parent = os.path.abspath(scratch_root) if scratch_root else None
    if parent and not os.path.isdir(parent):
        raise ValueError("scratch_root must be an existing directory")
    began = time.time()
    rows: list[FaultResult] = []
    with tempfile.TemporaryDirectory(prefix="collie-resilience-", dir=parent) as root:
        for name, run in _SCENARIOS:
            if name in wanted:
                scenario_root = os.path.join(root, name)
                os.makedirs(scenario_root, exist_ok=True)
                rows.append(_scenario(name, run, scenario_root))
    failed = [row.name for row in rows if row.status != "PASS"]
    return {
        "schema": SCHEMA, "collie_version": __version__,
        "generated_at": began,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(began)),
        "status": "PASS" if not failed else "FAIL",
        "passed": len(rows) - len(failed), "failed": len(failed),
        "failures": failed, "duration_ms": int((time.time() - began) * 1000),
        "scenarios": [row.to_dict() for row in rows],
    }


def run_soak(*, duration_s: float, interval_s: float = 60.0,
             report_path: str, scenarios: Iterable[str] | None = None,
             scratch_root: str | None = None,
             clock: Callable[[], float] = time.time,
             sleeper: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Repeat the matrix, checkpointing every cycle and resuming by report path."""
    duration_s = _finite(duration_s, "duration_s", minimum=.01)
    interval_s = _finite(interval_s, "interval_s", minimum=.01)
    raw_report_path = str(report_path or "").strip()
    if not raw_report_path:
        raise ValueError("report_path is required")
    report_path = os.path.abspath(raw_report_path)
    now = clock()
    report = _read_json(report_path)
    selected = list(scenario_names() if scenarios is None else (str(x) for x in scenarios))
    if (report.get("schema") != SCHEMA or report.get("kind") != "soak" or
            report.get("scenarios_selected") != selected or
            report.get("status") in ("PASS", "FAIL")):
        report = {
            "schema": SCHEMA, "kind": "soak", "collie_version": __version__,
            "status": "RUNNING", "started_at": now, "updated_at": now,
            "target_duration_s": duration_s, "interval_s": interval_s,
            "scenarios_selected": selected, "cycles": 0,
            "passed_cycles": 0, "failed_cycles": 0, "sleep_or_restart_gaps": 0,
            "last_cycle_at": 0.0, "last_matrix": {}, "failures": [],
        }
    else:
        # A resumed invocation may extend, but never shorten, an existing campaign.
        report["target_duration_s"] = max(
            float(report.get("target_duration_s") or 0), duration_s)
        report["interval_s"] = interval_s
        last = float(report.get("last_cycle_at") or 0)
        if last and now - last > max(interval_s * 4, 120.0):
            report["sleep_or_restart_gaps"] = int(
                report.get("sleep_or_restart_gaps") or 0) + 1

    deadline = float(report["started_at"]) + float(report["target_duration_s"])
    while True:
        matrix = run_fault_matrix(scenarios=selected, scratch_root=scratch_root)
        cycle_at = clock()
        report["cycles"] = int(report.get("cycles") or 0) + 1
        report["last_cycle_at"] = cycle_at
        report["updated_at"] = cycle_at
        report["last_matrix"] = matrix
        if matrix["status"] == "PASS":
            report["passed_cycles"] = int(report.get("passed_cycles") or 0) + 1
        else:
            report["failed_cycles"] = int(report.get("failed_cycles") or 0) + 1
            report["failures"] = (list(report.get("failures") or []) + [{
                "cycle": report["cycles"], "at": cycle_at,
                "scenarios": list(matrix.get("failures") or []),
            }])[-100:]
        elapsed = max(0.0, cycle_at - float(report["started_at"]))
        report["elapsed_s"] = elapsed
        if cycle_at >= deadline:
            report["status"] = "PASS" if not report["failed_cycles"] else "FAIL"
            report["finished_at"] = cycle_at
        _atomic_json(report_path, report)
        if report["status"] != "RUNNING":
            return report
        sleeper(min(interval_s, max(0.0, deadline - cycle_at)))


__all__ = [
    "FaultResult", "SCHEMA", "parse_duration", "run_fault_matrix", "run_soak",
    "scenario_names", "write_report",
]
