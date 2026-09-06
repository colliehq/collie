"""Read-only health and quota evidence for external runner selection.

The selector is pure; this module gathers the mutable facts it may consider.
Two sources are intentionally kept separate:

* ``runs.db`` supplies recent failures and verified-history statistics.  It is
  local evidence and opening it here is read-only.
* Codex app-server supplies the account's current rate-limit windows through
  ``account/rateLimits/read``.  The short-lived process receives only
  ``initialize``, ``initialized`` and that read request.  It never opens a
  thread, sends a prompt, consumes a reset credit, or contacts a model.

Unknown stays unknown.  A broken database or an unavailable app-server yields a
snapshot with an explanatory error, never an invented zero-percent quota.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import shutil
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from . import __version__, runner_env, runner_specs
from .agent_runners import (
    ProcessOutcome, SubprocessRunner, _finite_number, _lf_records,
    _reject_json_constant,
)


HISTORY_WINDOW_S = 30 * 24 * 60 * 60
RATE_LIMIT_WINDOW_S = 15 * 60
COOLDOWN_S = 5 * 60
QUOTA_CACHE_S = 60.0
QUOTA_GUARD = 90


# app-server handles account reads asynchronously and exits as soon as stdin is
# closed, so writing all requests through ``communicate`` can race the second
# response.  This tiny target performs the initialize handshake in two phases.
# It runs *inside* SubprocessRunner's trusted gate, process group / Job Object,
# and therefore cannot outlive the same extinction proof as a normal worker.
_QUOTA_HELPER_SCRIPT = r"""
import json
import os
import subprocess
import sys

requests = [line[:-1] if line.endswith("\r") else line
            for line in sys.stdin.read().split("\n") if line.strip()]
if len(requests) != 3:
    raise SystemExit(124)
flags = 0x08000000 if os.name == "nt" else 0
child = subprocess.Popen(
    [sys.argv[1], "app-server", "--stdio", "--strict-config"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
    text=True, encoding="utf-8", errors="replace", creationflags=flags)

def reject_constant(value):
    raise ValueError("non-finite JSON number is forbidden: %s" % value)

def wait_for(request_id):
    while True:
        # Keep the helper itself bounded too. The outer transport caps retained
        # output, but an unbounded readline here would allocate a hostile
        # app-server record before the outer process ever saw it.
        line = child.stdout.readline(131072)
        if not line:
            raise RuntimeError("app-server closed before response %s" % request_id)
        if not line.endswith("\n"):
            raise RuntimeError("app-server emitted an oversized or partial JSONL record")
        try:
            value = json.loads(line, parse_constant=reject_constant)
        except Exception:
            continue
        if isinstance(value, dict) and value.get("id") == request_id:
            sys.stdout.write(line)
            sys.stdout.flush()
            return

try:
    child.stdin.write(requests[0] + "\n")
    child.stdin.flush()
    wait_for(1)
    child.stdin.write(requests[1] + "\n" + requests[2] + "\n")
    child.stdin.flush()
    wait_for(2)
finally:
    try:
        child.stdin.close()
    except Exception:
        pass
    try:
        child.terminate()
        child.wait(timeout=2)
    except Exception:
        try:
            child.kill()
        except Exception:
            pass
"""


def _integer(value: Any) -> int | None:
    if (not _finite_number(value) or float(value) < 0
            or not float(value).is_integer()):
        return None
    return int(value)


@dataclass(frozen=True)
class RateLimitWindow:
    used_percent: int
    resets_at: int | None = None
    window_duration_mins: int | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "RateLimitWindow | None":
        if not isinstance(value, Mapping):
            return None
        used = _integer(value.get("usedPercent", value.get("used_percent")))
        if used is None:
            return None
        return cls(
            used_percent=max(0, min(100, used)),
            resets_at=_integer(value.get("resetsAt", value.get("resets_at"))),
            window_duration_mins=_integer(
                value.get("windowDurationMins", value.get("window_duration_mins"))))

    def to_dict(self) -> dict[str, Any]:
        return {"used_percent": self.used_percent, "resets_at": self.resets_at,
                "window_duration_mins": self.window_duration_mins}


@dataclass(frozen=True)
class QuotaSnapshot:
    primary: RateLimitWindow | None = None
    secondary: RateLimitWindow | None = None
    rate_limit_reached_type: str = ""
    plan_type: str = ""
    observed_at: float = 0.0

    @classmethod
    def from_dict(cls, value: Any, *, observed_at: float
                  ) -> "QuotaSnapshot | None":
        if (not isinstance(value, Mapping) or not _finite_number(observed_at) or
                float(observed_at) < 0):
            return None
        primary = RateLimitWindow.from_dict(value.get("primary"))
        secondary = RateLimitWindow.from_dict(value.get("secondary"))
        if ((value.get("primary") is not None and primary is None) or
                (value.get("secondary") is not None and secondary is None)):
            # A malformed documented window is not equivalent to an absent
            # window. Publishing only the plan label would hide broken quota
            # evidence and let selection treat it as merely unknown detail.
            return None
        reached = runner_specs.redact_text(
            value.get("rateLimitReachedType") or
            value.get("rate_limit_reached_type") or "", 160)
        plan = runner_specs.redact_text(
            value.get("planType") or value.get("plan_type") or "", 160)
        # An object with none of the documented facts is not a zero-usage quota.
        if primary is None and secondary is None and not reached and not plan:
            return None
        return cls(primary=primary, secondary=secondary,
                   rate_limit_reached_type=reached, plan_type=plan,
                   observed_at=float(observed_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary.to_dict() if self.primary else None,
            "secondary": self.secondary.to_dict() if self.secondary else None,
            "rate_limit_reached_type": self.rate_limit_reached_type or None,
            "plan_type": self.plan_type or None,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class RateLimitEvidence:
    observed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"observed_at": self.observed_at}


@dataclass(frozen=True)
class HistorySnapshot:
    runs: int = 0
    success: int = 0
    verified: int = 0
    errors: int = 0
    retry_429: int = 0
    avg_cost_usd: float | None = None
    avg_wall_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": self.runs, "success": self.success,
            "verified": self.verified, "errors": self.errors,
            "retry_429": self.retry_429,
            "avg_cost_usd": self.avg_cost_usd,
            "avg_wall_ms": self.avg_wall_ms,
        }


@dataclass(frozen=True)
class RouteSignals:
    runner: str
    auth_status: str = "unknown"
    recent_429: int = 0
    cooldown_until: float | None = None
    quota: QuotaSnapshot | None = None
    history: HistorySnapshot | None = None
    mission_budget: Any = None
    rate_limit: RateLimitEvidence | None = None
    quota_guard: int = QUOTA_GUARD
    observed_at: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "runner": self.runner, "auth_status": self.auth_status,
            "recent_429": self.recent_429,
            "cooldown_until": self.cooldown_until,
            "quota": self.quota.to_dict() if self.quota else None,
            "history": self.history.to_dict() if self.history else None,
            "mission_budget": self.mission_budget,
            "rate_limit": self.rate_limit.to_dict() if self.rate_limit else None,
            "quota_guard": self.quota_guard, "observed_at": self.observed_at,
            "error": runner_specs.redact_text(self.error, 500),
        }


@dataclass(frozen=True)
class SignalSet:
    by_runner: Mapping[str, RouteSignals] = field(default_factory=dict)

    def for_runner(self, key: str) -> RouteSignals | None:
        return self.by_runner.get(str(key or ""))

    def to_dict(self) -> dict[str, Any]:
        return {key: value.to_dict()
                for key, value in sorted(self.by_runner.items())}

    def digest(self) -> str:
        return runner_specs.stable_digest(self.to_dict())


def _auth_status(probe: Any) -> str:
    value = str(getattr(probe, "login", "") or "unknown")
    if value in ("ok", "expired", "not-logged-in"):
        return value
    if value == "not-configured":
        return "missing-key"
    return "unknown"


def _is_rate_limit(error: Any) -> bool:
    lowered = str(error or "").lower()
    return any(marker in lowered for marker in (
        "429", "rate limit", "rate_limit", "quota exceeded", "capacity"))


def _history(runs_db: str, key: str, now: float) -> tuple[HistorySnapshot | None,
                                                           int, float | None, str]:
    if not runs_db or not os.path.isfile(runs_db):
        return None, 0, None, ""
    uri = "file:%s?mode=ro" % os.path.abspath(runs_db).replace("\\", "/")
    db = None
    try:
        db = sqlite3.connect(uri, uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        from .recorder import root_run_filter
        rows = db.execute(
            "SELECT ts,success,verified,cost_usd,wall_ms,error FROM runs "
            "WHERE " + root_run_filter(db) + " AND harness=? AND ts>=? ORDER BY ts DESC LIMIT 200",
            (key, int(now - HISTORY_WINDOW_S))).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return None, 0, None, runner_specs.redact_text(
            "runs.db history unavailable: %s: %s" % (type(exc).__name__, exc), 300)
    finally:
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass
    if not rows:
        return HistorySnapshot(), 0, None, ""
    try:
        timestamps = [float(row["ts"] or 0) for row in rows]
        costs = [float(row["cost_usd"]) for row in rows
                 if row["cost_usd"] is not None]
        walls = [int(row["wall_ms"] or 0) for row in rows]
        if (any(not _finite_number(value) or value < 0 for value in timestamps) or
                any(not _finite_number(value) or value < 0 for value in costs) or
                any(value < 0 or value > (1 << 63) - 1 for value in walls)):
            raise ValueError("history contains a non-finite or negative number")
        recent = [row for row, timestamp in zip(rows, timestamps)
                  if timestamp >= now - RATE_LIMIT_WINDOW_S
                  and _is_rate_limit(row["error"])]
        history = HistorySnapshot(
            runs=len(rows), success=sum(bool(row["success"]) for row in rows),
            verified=sum(bool(row["verified"]) for row in rows),
            errors=sum(not bool(row["success"]) for row in rows),
            retry_429=sum(_is_rate_limit(row["error"]) for row in rows),
            avg_cost_usd=(sum(costs) / len(costs)) if costs else None,
            avg_wall_ms=int(sum(walls) / len(walls)) if walls else 0)
        last = max((float(row["ts"] or 0) for row in recent), default=0.0)
        return history, len(recent), ((last + COOLDOWN_S) if last else None), ""
    except (TypeError, ValueError, OverflowError) as exc:
        return None, 0, None, runner_specs.redact_text(
            "runs.db history values unavailable: %s: %s" %
            (type(exc).__name__, exc), 300)


_QUOTA_LOCK = threading.Lock()
_QUOTA_PROBE_LOCK = threading.Lock()
_QUOTA_CACHE: tuple[
    float, tuple[str, str, str], QuotaSnapshot | None, str,
    SubprocessRunner | None,
] | None = None


def _quota_cache_key(executable: str) -> tuple[str, str, str]:
    """Account/runtime identity for a quota snapshot, containing no credential."""
    resolved = shutil.which(executable) or ""
    codex_home = os.path.realpath(os.path.abspath(
        os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")))
    return str(executable or "codex"), os.path.realpath(resolved) if resolved else "", codex_home


def _read_codex_quota_uncached(*, now: float, timeout_s: float,
                               executable: str,
                               transport: SubprocessRunner | None
                               ) -> tuple[QuotaSnapshot | None, str]:
    resolved = shutil.which(executable) or ""
    if not resolved:
        return None, "Codex CLI is not installed"
    try:
        runner_env.assert_no_billing_override(os.environ, "codex")
        env, _receipt = runner_env.child_env("codex")
        requests = [
            {"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "collie", "version": __version__},
                "capabilities": {"experimentalApi": True},
            }},
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read", "params": None},
        ]
        stdin_text = "".join(json.dumps(item, separators=(",", ":")) + "\n"
                             for item in requests)
        outcome = (transport or SubprocessRunner()).run(
            (os.path.realpath(os.sys.executable), "-I", "-c",
             _QUOTA_HELPER_SCRIPT, resolved),
            cwd=os.getcwd(), stdin_text=stdin_text, timeout_s=float(timeout_s),
            on_process=lambda _proc: True, env=env)
        if not isinstance(outcome, ProcessOutcome):
            raise TypeError("quota transport returned no ProcessOutcome")
        if outcome.timed_out:
            raise TimeoutError("Codex quota probe timed out")
        if outcome.output_truncated:
            raise ValueError("Codex quota probe output exceeded its capture limit")
        response = None
        # app-server is LF-JSONL.  Python's splitlines() also treats U+2028 and
        # U+2029 as line breaks, corrupting otherwise valid JSON string values.
        if outcome.exit_code != 0:
            raise RuntimeError("Codex quota helper exited with status %s" %
                               outcome.exit_code)
        for raw in _lf_records(outcome.stdout or ""):
            try:
                value = json.loads(raw, parse_constant=_reject_json_constant)
            except (ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and value.get("id") == 2:
                response = value
                break
        if response is None:
            raise ValueError("app-server returned no account/rateLimits/read response")
        if response.get("error"):
            error = response.get("error")
            detail = error.get("message") if isinstance(error, Mapping) else error
            raise RuntimeError(str(detail or "rate-limit read failed"))
        result = response.get("result")
        result = result if isinstance(result, Mapping) else {}
        quota = QuotaSnapshot.from_dict(result.get("rateLimits"), observed_at=now)
        if quota is None:
            raise ValueError("app-server returned no documented rate-limit window")
        error_text = ""
    except Exception as exc:
        quota = None
        error_text = runner_specs.redact_text(
            "%s: %s" % (type(exc).__name__, exc), 500)
    return quota, error_text


def read_codex_quota(*, now: float | None = None, timeout_s: float = 10.0,
                     executable: str = "codex",
                     transport: SubprocessRunner | None = None
                     ) -> tuple[QuotaSnapshot | None, str]:
    """Read Codex rate-limit windows without starting a model turn.

    Concurrent callers share one in-flight app-server read.  The cache is
    checked again after acquiring the probe lock so several Web clients cannot
    create duplicate helper processes at startup.
    """
    global _QUOTA_CACHE
    observed = time.time() if now is None else float(now)
    if not _finite_number(observed):
        raise ValueError("quota observation time must be finite")
    if not _finite_number(timeout_s) or float(timeout_s) <= 0:
        raise ValueError("quota timeout must be positive and finite")
    cache_key = _quota_cache_key(executable)
    with _QUOTA_LOCK:
        if (_QUOTA_CACHE is not None and
                _QUOTA_CACHE[1] == cache_key and
                _QUOTA_CACHE[4] is transport and
                0 <= observed - _QUOTA_CACHE[0] < QUOTA_CACHE_S):
            return _QUOTA_CACHE[2], _QUOTA_CACHE[3]
    with _QUOTA_PROBE_LOCK:
        with _QUOTA_LOCK:
            if (_QUOTA_CACHE is not None and
                    _QUOTA_CACHE[1] == cache_key and
                    _QUOTA_CACHE[4] is transport and
                    0 <= observed - _QUOTA_CACHE[0] < QUOTA_CACHE_S):
                return _QUOTA_CACHE[2], _QUOTA_CACHE[3]
        quota, error_text = _read_codex_quota_uncached(
            now=observed, timeout_s=timeout_s, executable=executable,
            transport=transport)
        with _QUOTA_LOCK:
            _QUOTA_CACHE = (observed, cache_key, quota, error_text, transport)
        return quota, error_text


def reset_cache() -> None:
    global _QUOTA_CACHE
    with _QUOTA_LOCK:
        _QUOTA_CACHE = None


def collect(keys: Sequence[str], probes: Mapping[str, Any], *, runs_db: str = "",
            live_quota: bool = False, now: float | None = None,
            quota_reader: Callable[..., tuple[QuotaSnapshot | None, str]] | None = None
            ) -> SignalSet:
    """Gather route-specific signals for exactly the candidate keys requested."""
    now = time.time() if now is None else float(now)
    wanted = tuple(dict.fromkeys(str(key) for key in keys if key))
    codex_quota: QuotaSnapshot | None = None
    quota_error = ""
    if live_quota and "codex-exec" in wanted:
        reader = quota_reader or read_codex_quota
        try:
            codex_quota, quota_error = reader(now=now)
        except Exception as exc:
            codex_quota = None
            quota_error = runner_specs.redact_text(
                "%s: %s" % (type(exc).__name__, exc), 500)

    rows: dict[str, RouteSignals] = {}
    for key in wanted:
        history, recent, cooldown, history_error = _history(runs_db, key, now)
        quota = codex_quota if key == "codex-exec" else None
        observed = quota.observed_at if quota is not None else now
        errors = "; ".join(item for item in (history_error,
                                              quota_error if key == "codex-exec" else "")
                           if item)
        rows[key] = RouteSignals(
            runner=key, auth_status=_auth_status(probes.get(key)),
            recent_429=recent, cooldown_until=cooldown, quota=quota,
            history=history, rate_limit=(RateLimitEvidence(observed_at=observed)
                                         if recent or quota else None),
            observed_at=observed, error=errors)
    return SignalSet(rows)


def for_selection(request: Any, probes: Mapping[str, Any], *, runs_db: str = "",
                  now: float | None = None) -> SignalSet | None:
    """Collect only when selection has a real external route to compare.

    Quota is a ranking/Auto guard.  A named worker is the operator's explicit
    route and H7 intentionally does not reject it for headroom; avoiding a live
    account read there also keeps the ordinary pinned path fast.
    """
    candidates = tuple(request.candidates())
    if candidates == ("collie",) or not any(key != "collie" for key in candidates):
        return None
    auto = not str(getattr(request, "pin", "") or "") and \
        str(getattr(request, "configured", "") or "collie") == "auto"
    return collect(candidates, probes, runs_db=runs_db,
                   live_quota=auto and "codex-exec" in candidates, now=now)


__all__ = [
    "COOLDOWN_S", "HISTORY_WINDOW_S", "QUOTA_GUARD", "RATE_LIMIT_WINDOW_S",
    "HistorySnapshot", "QuotaSnapshot", "RateLimitEvidence", "RateLimitWindow",
    "RouteSignals", "SignalSet", "collect", "for_selection",
    "read_codex_quota", "reset_cache",
]
