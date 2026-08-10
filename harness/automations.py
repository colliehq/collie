"""Durable, permissioned scheduled/event-triggered automations.

This module intentionally separates *triggering* from *executing*:

* :class:`TriggerEngine` polls registered providers and writes immutable execution requests.
* :class:`AutomationExecutor` leases those requests and invokes a caller-supplied runner.

That boundary keeps a page/file/webhook predicate from acquiring model or tool authority merely
because it fired.  Every request carries a snapshot of its context, workspace, budget,
notification and permission policy; decisions and state changes are written to an audit ledger.

The module CLI can run the polling/execution daemon.  Webhook ingestion remains an API rather than
an unauthenticated listener: a Web surface must authenticate first and then call
``TriggerEngine.ingest_webhook``.
"""

from __future__ import annotations

import hashlib
import argparse
import json
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol


_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
_MAX_PAGE_BYTES = 1024 * 1024
_MAX_WEBHOOK_BYTES = 64 * 1024
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
NEEDS_YOU = "needs_you"
DEAD = "dead"


class AutomationError(RuntimeError):
    pass


class PermissionDenied(AutomationError):
    pass


class BudgetExceeded(AutomationError):
    pass


class AutomationQueueFull(AutomationError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _canonical(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _inside(path: str, roots: tuple[str, ...]) -> bool:
    path = _canonical(path)
    for root in roots:
        try:
            if os.path.commonpath((path, root)) == root:
                return True
        except ValueError:
            continue
    return False


@dataclass(frozen=True)
class PermissionPolicy:
    read_roots: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()
    network_hosts: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    external_writes: bool = False
    current_workspace: bool = False
    webhook_ingest: bool = False

    @classmethod
    def from_dict(cls, value: dict | None):
        value = value or {}
        return cls(
            tuple(_canonical(p) for p in value.get("read_roots", ()) if str(p).strip()),
            tuple(_canonical(p) for p in value.get("write_roots", ()) if str(p).strip()),
            tuple(sorted({str(h).strip().lower() for h in value.get("network_hosts", ())
                          if str(h).strip()})),
            tuple(sorted({str(t).strip() for t in value.get("tools", ()) if str(t).strip()})),
            bool(value.get("external_writes", False)),
            bool(value.get("current_workspace", False)),
            bool(value.get("webhook_ingest", False)),
        )

    def as_dict(self) -> dict:
        return asdict(self)

    def require_read(self, path: str) -> str:
        path = _canonical(path)
        if not _inside(path, self.read_roots):
            raise PermissionDenied("file trigger path is outside permitted read roots")
        return path

    def require_write(self, path: str) -> str:
        path = _canonical(path)
        if not _inside(path, self.write_roots):
            raise PermissionDenied("path is outside permitted write roots")
        return path

    def require_tool(self, name: str) -> str:
        name = str(name)
        if name not in self.tools:
            raise PermissionDenied("tool %s is not permitted" % name)
        return name

    def require_external_write(self):
        if not self.external_writes:
            raise PermissionDenied("external writes are not permitted")
        return True

    def require_url(self, url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not host:
            raise PermissionDenied("page trigger requires an http(s) URL")
        if host not in self.network_hosts:
            raise PermissionDenied("page trigger host %s is not permitted" % host)
        # Plain HTTP is only reasonable for an explicitly allowed loopback development endpoint.
        if parsed.scheme != "https" and host not in ("localhost", "127.0.0.1", "::1"):
            raise PermissionDenied("non-loopback page triggers require HTTPS")
        return url


@dataclass(frozen=True)
class AutomationSpec:
    automation_id: str
    task: str
    trigger: dict
    context: dict = field(default_factory=lambda: {"policy": "fresh"})
    workspace: dict = field(default_factory=lambda: {"mode": "isolated"})
    budget: dict = field(default_factory=dict)
    execution: dict = field(default_factory=dict)
    notifications: tuple[str, ...] = ("failure", "needs_you")
    permissions: PermissionPolicy = field(default_factory=PermissionPolicy)
    enabled: bool = True

    @classmethod
    def from_dict(cls, value: dict):
        aid = str(value.get("automation_id") or value.get("id") or "")
        if not _ID_RE.fullmatch(aid):
            raise ValueError("automation id must be 1-80 safe characters")
        task = str(value.get("task") or "").strip()
        if not task:
            raise ValueError("automation task is required")
        trigger = dict(value.get("trigger") or {})
        if not str(trigger.get("provider") or "").strip():
            raise ValueError("trigger.provider is required")
        context = dict(value.get("context") or {"policy": "fresh"})
        policy = context.get("policy", "fresh")
        if policy not in ("fresh", "continued"):
            raise ValueError("context.policy must be fresh or continued")
        if policy == "continued" and not str(context.get("session_id") or "").strip():
            raise ValueError("continued context requires an explicit session_id")
        workspace = dict(value.get("workspace") or {"mode": "isolated"})
        if workspace.get("mode", "isolated") not in ("isolated", "current"):
            raise ValueError("workspace.mode must be isolated or current")
        permissions = PermissionPolicy.from_dict(value.get("permissions"))
        if workspace.get("mode", "isolated") == "current" and not permissions.current_workspace:
            raise ValueError("current workspace requires permissions.current_workspace=true")
        budget = dict(value.get("budget") or {})
        defaults = {"max_wall_s": 1800.0, "max_model_tokens": 200000,
                    "max_cost_usd": 25.0, "max_actions": 100,
                    "max_runs_per_day": 24, "max_retries": 1, "max_turns": 50}
        for key, default in defaults.items():
            raw = budget.get(key, default)
            try:
                number = float(raw) if isinstance(default, float) else int(raw)
            except (TypeError, ValueError):
                raise ValueError("budget.%s must be numeric" % key)
            if number < 0 or (key != "max_retries" and number == 0):
                raise ValueError("budget.%s must be positive" % key)
            budget[key] = number
        execution = dict(value.get("execution") or {})
        mode = str(execution.get("mode") or "project")
        if mode not in ("plan", "project"):
            raise ValueError("unattended execution.mode must be plan or project")
        execution["mode"] = mode
        for key in ("provider", "model", "project"):
            if key in execution:
                execution[key] = str(execution[key])
        notifications = tuple(sorted({str(x) for x in value.get(
            "notifications", ("failure", "needs_you"))
                                      if str(x) in ("start", "success", "failure", "needs_you")}))
        return cls(automation_id=aid, task=task, trigger=trigger, context=context,
                   workspace=workspace, budget=budget, execution=execution,
                   notifications=notifications, permissions=permissions,
                   enabled=bool(value.get("enabled", True)))

    def as_dict(self) -> dict:
        value = asdict(self)
        value["permissions"] = self.permissions.as_dict()
        value["notifications"] = list(self.notifications)
        return value


@dataclass
class TriggerEvaluation:
    fired: bool
    event_id: str
    event: dict
    cursor: dict


class TriggerProvider(Protocol):
    name: str

    def evaluate(self, spec: AutomationSpec, cursor: dict, now: float) -> TriggerEvaluation:
        ...


class TimerTrigger:
    name = "timer"

    def evaluate(self, spec: AutomationSpec, cursor: dict, now: float) -> TriggerEvaluation:
        cfg = spec.trigger
        every = float(cfg.get("every_s") or 0)
        at = float(cfg.get("at") or 0)
        if every <= 0 and at <= 0:
            raise ValueError("timer trigger requires positive every_s or at")
        due = float(cursor.get("next_at") or at or (
            now if cfg.get("fire_immediately") else now + every))
        if cursor.get("done"):
            return TriggerEvaluation(False, "", {}, cursor)
        if now < due:
            return TriggerEvaluation(False, "", {}, {"next_at": due})
        if every > 0:
            next_at = due + every
            if not cfg.get("catch_up", True):
                next_at = now + every
            else:
                while next_at <= now:
                    next_at += every
            nxt = {"next_at": next_at}
        else:
            nxt = {"next_at": due, "done": True}
        stamp = int(due * 1000)
        return TriggerEvaluation(True, "timer:%s:%d" % (spec.automation_id, stamp),
                                 {"kind": "timer", "scheduled_at": due,
                                  "observed_at": now}, nxt)


def _predicate(value: bytes, predicate: dict) -> bool:
    kind = str(predicate.get("type") or "exists")
    if kind == "exists":
        return True
    text = value.decode(str(predicate.get("encoding") or "utf-8"), "replace")
    needle = str(predicate.get("value") or "")
    if kind == "contains":
        return needle in text
    if kind == "not_contains":
        return needle not in text
    if kind == "regex":
        return re.search(needle, text) is not None
    if kind == "changed":
        return True
    raise ValueError("unsupported predicate type %s" % kind)


def _edge_result(spec: AutomationSpec, cursor: dict, now: float, *, kind: str,
                 fingerprint: str, matched: bool, metadata: dict) -> TriggerEvaluation:
    old = str(cursor.get("fingerprint") or "")
    old_match = bool(cursor.get("matched", False))
    first = not bool(cursor.get("seen"))
    pred_kind = str((spec.trigger.get("predicate") or {}).get("type") or "exists")
    changed = fingerprint != old
    fired = matched and ((pred_kind == "changed" and changed and not first)
                         or (pred_kind != "changed" and not old_match))
    if first and spec.trigger.get("fire_on_initial", False):
        fired = matched
    new_cursor = {"seen": True, "fingerprint": fingerprint, "matched": matched,
                  "checked_at": now}
    event_id = "%s:%s:%s" % (kind, spec.automation_id, fingerprint) if fired else ""
    event = {"kind": kind, "observed_at": now, **metadata} if fired else {}
    return TriggerEvaluation(fired, event_id, event, new_cursor)


class FilePredicateTrigger:
    name = "file"

    def evaluate(self, spec: AutomationSpec, cursor: dict, now: float) -> TriggerEvaluation:
        path = spec.permissions.require_read(str(spec.trigger.get("path") or ""))
        exists = os.path.isfile(path)
        data = b""
        if exists:
            max_bytes = max(1, min(_MAX_PAGE_BYTES, int(spec.trigger.get(
                "max_bytes", _MAX_PAGE_BYTES))))
            with open(path, "rb") as fh:
                data = fh.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("file trigger input exceeds max_bytes")
        pred = dict(spec.trigger.get("predicate") or {"type": "exists"})
        matched = exists and _predicate(data, pred)
        fingerprint = hashlib.sha256((b"1" if exists else b"0") + data).hexdigest()
        return _edge_result(spec, cursor, now, kind="file", fingerprint=fingerprint,
                            matched=matched,
                            metadata={"path": path, "exists": exists,
                                      "content_sha256": fingerprint})


class PagePredicateTrigger:
    name = "page"

    def __init__(self, opener=urllib.request.urlopen):
        self.opener = opener

    def evaluate(self, spec: AutomationSpec, cursor: dict, now: float) -> TriggerEvaluation:
        url = spec.permissions.require_url(str(spec.trigger.get("url") or ""))
        request = urllib.request.Request(url, headers={"User-Agent": "collie-trigger/1"})
        timeout = max(0.2, min(30.0, float(spec.trigger.get("timeout_s", 10))))
        with self.opener(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            data = response.read(_MAX_PAGE_BYTES + 1)
        if len(data) > _MAX_PAGE_BYTES:
            raise ValueError("page trigger response exceeds 1 MiB")
        pred = dict(spec.trigger.get("predicate") or {"type": "changed"})
        matched = _predicate(data, pred)
        fingerprint = hashlib.sha256(data).hexdigest()
        return _edge_result(spec, cursor, now, kind="page", fingerprint=fingerprint,
                            matched=matched, metadata={"url": url, "status": status,
                                                       "content_sha256": fingerprint})


class WebhookTrigger:
    """Marker provider; authenticated events enter through ``ingest_webhook``."""
    name = "webhook"

    def evaluate(self, spec: AutomationSpec, cursor: dict, now: float) -> TriggerEvaluation:
        return TriggerEvaluation(False, "", {}, cursor)


class TriggerRegistry:
    def __init__(self):
        self._providers: dict[str, TriggerProvider] = {}
        self.register(TimerTrigger())
        self.register(FilePredicateTrigger())
        self.register(PagePredicateTrigger())
        self.register(WebhookTrigger())

    def register(self, provider: TriggerProvider):
        name = str(getattr(provider, "name", "") or "")
        if not _ID_RE.fullmatch(name):
            raise ValueError("trigger provider needs a safe name")
        self._providers[name] = provider
        return provider

    def get(self, name: str) -> TriggerProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise ValueError("unknown trigger provider %s" % name)

    def names(self) -> list[str]:
        return sorted(self._providers)


class AutomationStore:
    def __init__(self, path: str | None = None, *, queue_cap: int = 1000):
        path = path or os.path.expanduser("~/.collie/automations.db")
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.queue_cap = max(1, int(queue_cap))
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS automations(
            automation_id TEXT PRIMARY KEY, spec_json TEXT NOT NULL, enabled INTEGER NOT NULL,
            updated_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS trigger_state(
            automation_id TEXT PRIMARY KEY, cursor_json TEXT NOT NULL DEFAULT '{}',
            last_error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS executions(
            execution_id TEXT PRIMARY KEY, automation_id TEXT NOT NULL, event_id TEXT NOT NULL,
            request_json TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            started_at REAL NOT NULL DEFAULT 0, finished_at REAL NOT NULL DEFAULT 0,
            result_json TEXT NOT NULL DEFAULT '{}', last_error TEXT NOT NULL DEFAULT '',
            UNIQUE(automation_id,event_id));
          CREATE INDEX IF NOT EXISTS execution_due ON executions(state,created_at);
          CREATE TABLE IF NOT EXISTS usage(
            execution_id TEXT PRIMARY KEY, model_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0, actions INTEGER NOT NULL DEFAULT 0,
            wall_s REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS audit(
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
            automation_id TEXT NOT NULL, execution_id TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL, decision TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}');
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def audit(self, automation_id: str, event: str, decision: str,
              detail: dict | None = None, *, execution_id: str = "", now: float | None = None):
        now = float(time.time() if now is None else now)
        self.db.execute(
            "INSERT INTO audit(at,automation_id,execution_id,event,decision,detail_json) "
            "VALUES(?,?,?,?,?,?)", (now, automation_id, execution_id, event, decision,
                                    _json(detail or {})))

    def upsert(self, spec: AutomationSpec | dict, *, now: float | None = None):
        spec = spec if isinstance(spec, AutomationSpec) else AutomationSpec.from_dict(spec)
        now = float(time.time() if now is None else now)
        with self._lock:
            self.db.execute(
                "INSERT INTO automations(automation_id,spec_json,enabled,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(automation_id) DO UPDATE SET spec_json=excluded.spec_json,"
                "enabled=excluded.enabled,updated_at=excluded.updated_at",
                (spec.automation_id, _json(spec.as_dict()), int(spec.enabled), now))
            self.audit(spec.automation_id, "configuration", "accepted", {
                "trigger_provider": spec.trigger["provider"],
                "context_policy": spec.context.get("policy"),
                "workspace_mode": spec.workspace.get("mode"),
                "permission_sha256": hashlib.sha256(
                    _json(spec.permissions.as_dict()).encode()).hexdigest(),
            }, now=now)
            self.db.commit()
        return spec

    def specs(self, *, enabled_only: bool = True) -> list[AutomationSpec]:
        sql = "SELECT spec_json FROM automations" + (" WHERE enabled=1" if enabled_only else "")
        return [AutomationSpec.from_dict(json.loads(row[0]))
                for row in self.db.execute(sql + " ORDER BY automation_id")]

    def spec(self, automation_id: str) -> AutomationSpec | None:
        row = self.db.execute("SELECT spec_json FROM automations WHERE automation_id=?",
                              (automation_id,)).fetchone()
        return AutomationSpec.from_dict(json.loads(row[0])) if row else None

    def cursor(self, automation_id: str) -> dict:
        row = self.db.execute("SELECT cursor_json FROM trigger_state WHERE automation_id=?",
                              (automation_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def set_cursor(self, automation_id: str, cursor: dict, *, error: str = "",
                   now: float | None = None):
        now = float(time.time() if now is None else now)
        self.db.execute(
            "INSERT INTO trigger_state(automation_id,cursor_json,last_error,updated_at) "
            "VALUES(?,?,?,?) ON CONFLICT(automation_id) DO UPDATE SET "
            "cursor_json=excluded.cursor_json,last_error=excluded.last_error,"
            "updated_at=excluded.updated_at",
            (automation_id, _json(cursor), str(error)[:500], now))

    def daily_runs(self, automation_id: str, now: float) -> int:
        return int(self.db.execute(
            "SELECT count(*) FROM executions WHERE automation_id=? AND created_at>=?",
            (automation_id, float(now) - 86400)).fetchone()[0])

    def enqueue(self, spec: AutomationSpec, event_id: str, event: dict, *,
                now: float | None = None) -> str | None:
        now = float(time.time() if now is None else now)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = self.db.execute(
                    "SELECT execution_id FROM executions WHERE automation_id=? AND event_id=?",
                    (spec.automation_id, event_id)).fetchone()
                if existing:
                    self.db.commit()
                    return None
                live = int(self.db.execute(
                    "SELECT count(*) FROM executions WHERE state IN (?,?)",
                    (PENDING, RUNNING)).fetchone()[0])
                if live >= self.queue_cap:
                    self.audit(spec.automation_id, "enqueue", "denied_queue_full",
                               {"queue_cap": self.queue_cap}, now=now)
                    self.db.commit()
                    raise AutomationQueueFull("automation execution queue is full")
                max_daily = int(spec.budget.get("max_runs_per_day", 24))
                if max_daily and self.daily_runs(spec.automation_id, now) >= max_daily:
                    self.audit(spec.automation_id, "enqueue", "denied_daily_budget",
                               {"max_runs_per_day": max_daily}, now=now)
                    self.db.commit()
                    return None
                eid = "aut_" + uuid.uuid4().hex
                context_policy = spec.context.get("policy", "fresh")
                session_id = (str(spec.context.get("session_id")) if context_policy == "continued"
                              else "%s-%s" % (spec.automation_id, eid[4:16]))
                request = {
                    "execution_id": eid, "automation_id": spec.automation_id,
                    "task": spec.task, "trigger_event": event,
                    "context": {**spec.context, "session_id": session_id},
                    "workspace": spec.workspace, "budget": spec.budget,
                    "execution": spec.execution,
                    "notifications": list(spec.notifications),
                    "permissions": spec.permissions.as_dict(),
                }
                self.db.execute(
                    "INSERT INTO executions(execution_id,automation_id,event_id,request_json,state,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (eid, spec.automation_id, event_id, _json(request), PENDING, now, now))
                self.db.execute(
                    "INSERT INTO usage(execution_id,updated_at) VALUES(?,?)", (eid, now))
                self.audit(spec.automation_id, "enqueue", "accepted",
                           {"event_id": event_id}, execution_id=eid, now=now)
                self.db.commit()
                return eid
            except Exception:
                if self.db.in_transaction:
                    self.db.rollback()
                raise

    def claim(self, *, lease_s: float = 300, now: float | None = None) -> dict | None:
        now = float(time.time() if now is None else now)
        self.recover_expired(now=now)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                rows = list(self.db.execute(
                    "SELECT * FROM executions e WHERE state=? AND NOT EXISTS ("
                    "SELECT 1 FROM executions r WHERE r.automation_id=e.automation_id AND "
                    "r.state=?) ORDER BY created_at LIMIT 1", (PENDING, RUNNING)))
                if not rows:
                    self.db.commit()
                    return None
                row = rows[0]
                self.db.execute(
                    "UPDATE executions SET state=?,attempts=attempts+1,lease_until=?,started_at=?,"
                    "updated_at=? WHERE execution_id=? AND state=?",
                    (RUNNING, now + max(1.0, lease_s), now, now,
                     row["execution_id"], PENDING))
                self.audit(row["automation_id"], "execution", "claimed", {},
                           execution_id=row["execution_id"], now=now)
                self.db.commit()
                out = dict(row)
                out["attempts"] = int(out["attempts"]) + 1
                out["request"] = json.loads(out.pop("request_json"))
                return out
            except Exception:
                self.db.rollback()
                raise

    def recover_expired(self, *, now: float | None = None) -> int:
        now = float(time.time() if now is None else now)
        changed = 0
        with self._lock:
            rows = list(self.db.execute(
                "SELECT execution_id,automation_id,request_json,attempts FROM executions "
                "WHERE state=? AND lease_until<=?", (RUNNING, now)))
            for row in rows:
                request = json.loads(row["request_json"])
                perms = request.get("permissions") or {}
                retries = int((request.get("budget") or {}).get("max_retries", 1))
                # Unknown side effects are never guessed safe. Read-only runs may be retried within
                # their explicit retry budget; externally mutating ones wait for a human verdict.
                state = (PENDING if not perms.get("external_writes")
                         and int(row["attempts"]) <= retries else NEEDS_YOU)
                self.db.execute(
                    "UPDATE executions SET state=?,lease_until=0,updated_at=?,last_error=? "
                    "WHERE execution_id=? AND state=?",
                    (state, now, "execution lease expired; prior outcome unknown",
                     row["execution_id"], RUNNING))
                self.audit(row["automation_id"], "recovery", state, {
                    "reason": "lease_expired"}, execution_id=row["execution_id"], now=now)
                changed += 1
            self.db.commit()
        return changed

    def finish(self, execution_id: str, state: str, result: dict | None = None,
               error: str = "", *, now: float | None = None) -> bool:
        if state not in (SUCCEEDED, FAILED, NEEDS_YOU, DEAD, PENDING):
            raise ValueError("invalid execution terminal state")
        now = float(time.time() if now is None else now)
        row = self.db.execute(
            "SELECT automation_id FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
        if not row:
            return False
        finished = now if state in (SUCCEEDED, FAILED, NEEDS_YOU, DEAD) else 0
        changed = self.db.execute(
            "UPDATE executions SET state=?,result_json=?,last_error=?,lease_until=0,"
            "finished_at=?,updated_at=? WHERE execution_id=? AND state=?",
            (state, _json(result or {}), str(error)[:2000], finished, now,
             execution_id, RUNNING))
        if changed.rowcount:
            self.audit(row["automation_id"], "execution", state,
                       {"error": str(error)[:500]}, execution_id=execution_id, now=now)
        self.db.commit()
        return changed.rowcount == 1

    def add_usage(self, execution_id: str, *, model_tokens: int = 0,
                  cost_usd: float = 0, actions: int = 0, wall_s: float = 0,
                  now: float | None = None) -> dict:
        now = float(time.time() if now is None else now)
        self.db.execute(
            "UPDATE usage SET model_tokens=model_tokens+?,cost_usd=cost_usd+?,"
            "actions=actions+?,wall_s=wall_s+?,updated_at=? WHERE execution_id=?",
            (max(0, int(model_tokens)), max(0.0, float(cost_usd)), max(0, int(actions)),
             max(0.0, float(wall_s)), now, execution_id))
        self.db.commit()
        return self.usage(execution_id)

    def usage(self, execution_id: str) -> dict:
        row = self.db.execute("SELECT * FROM usage WHERE execution_id=?",
                              (execution_id,)).fetchone()
        return dict(row) if row else {}

    def executions(self, automation_id: str = "") -> list[dict]:
        query, args = "SELECT * FROM executions", ()
        if automation_id:
            query += " WHERE automation_id=?"; args = (automation_id,)
        return [dict(row) for row in self.db.execute(query + " ORDER BY created_at", args)]

    def audit_log(self, automation_id: str = "", limit: int = 200) -> list[dict]:
        query, args = "SELECT * FROM audit", []
        if automation_id:
            query += " WHERE automation_id=?"; args.append(automation_id)
        query += " ORDER BY audit_id DESC LIMIT ?"; args.append(max(1, int(limit)))
        out = []
        for row in self.db.execute(query, args):
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
            out.append(item)
        return out


class TriggerEngine:
    def __init__(self, store: AutomationStore, registry: TriggerRegistry | None = None):
        self.store = store
        self.registry = registry or TriggerRegistry()

    def tick(self, now: float | None = None) -> list[str]:
        now = float(time.time() if now is None else now)
        created = []
        for spec in self.store.specs():
            provider_name = str(spec.trigger.get("provider"))
            if provider_name == "webhook":
                continue
            cursor = self.store.cursor(spec.automation_id)
            try:
                evaluation = self.registry.get(provider_name).evaluate(spec, cursor, now)
                # Enqueue before advancing the cursor. If the durable queue is unavailable/full,
                # the old cursor remains and a later tick observes the event again. Event ids make
                # the opposite crash boundary (enqueued, cursor not advanced) idempotent.
                if evaluation.fired:
                    eid = self.store.enqueue(spec, evaluation.event_id,
                                             evaluation.event, now=now)
                    if eid:
                        created.append(eid)
                with self.store._lock:
                    self.store.set_cursor(spec.automation_id, evaluation.cursor, now=now)
                    self.store.db.commit()
            except Exception as exc:
                with self.store._lock:
                    self.store.set_cursor(spec.automation_id, cursor,
                                          error="%s: %s" % (type(exc).__name__, exc), now=now)
                    self.store.audit(spec.automation_id, "trigger", "failed", {
                        "provider": provider_name,
                        "error": "%s: %s" % (type(exc).__name__, exc)}, now=now)
                    self.store.db.commit()
        return created

    def ingest_webhook(self, automation_id: str, payload: dict, *, authenticated: bool,
                       delivery_id: str = "", now: float | None = None) -> str | None:
        """Evaluate an already-authenticated webhook; this module does not expose a listener.

        Only explicitly selected payload fields enter durable state. Authentication headers and
        bearer values must be consumed by the calling HTTP boundary and are never accepted here.
        """
        now = float(time.time() if now is None else now)
        spec = self.store.spec(automation_id)
        if not spec or not spec.enabled:
            raise KeyError("automation not found or disabled")
        if spec.trigger.get("provider") != "webhook":
            raise ValueError("automation is not webhook-triggered")
        if not authenticated or not spec.permissions.webhook_ingest:
            self.store.audit(automation_id, "webhook", "denied", {
                "authenticated": bool(authenticated)}, now=now)
            self.store.db.commit()
            raise PermissionDenied("authenticated webhook ingestion is not permitted")
        raw = _json(payload).encode("utf-8")
        if len(raw) > _MAX_WEBHOOK_BYTES:
            raise ValueError("webhook payload exceeds 64 KiB")
        predicate = dict(spec.trigger.get("predicate") or {})
        field_name = str(predicate.get("field") or "")
        matched = not field_name or payload.get(field_name) == predicate.get("equals")
        if not matched:
            self.store.audit(automation_id, "webhook", "ignored_predicate", {}, now=now)
            self.store.db.commit()
            return None
        allowed = [str(x) for x in spec.trigger.get("persist_fields", ())]
        selected = {key: payload.get(key) for key in allowed if key in payload}
        digest = hashlib.sha256(raw).hexdigest()
        event_id = "webhook:%s:%s" % (automation_id, delivery_id or digest)
        event = {"kind": "webhook", "observed_at": now, "delivery_id": delivery_id,
                 "payload_sha256": digest, "fields": selected}
        return self.store.enqueue(spec, event_id, event, now=now)


class BudgetGuard:
    def __init__(self, store: AutomationStore, execution_id: str, budget: dict,
                 *, request: dict | None = None, clock=time.monotonic):
        self.store, self.execution_id, self.budget = store, execution_id, budget
        self.clock, self.started = clock, clock()
        self.authority = ExecutionAuthority(store, request or {})

    def consume(self, *, model_tokens: int = 0, cost_usd: float = 0,
                actions: int = 0) -> dict:
        wall = max(0.0, self.clock() - self.started)
        usage = self.store.add_usage(self.execution_id, model_tokens=model_tokens,
                                     cost_usd=cost_usd, actions=actions, wall_s=wall)
        self.started = self.clock()
        checks = (("model_tokens", "max_model_tokens"), ("cost_usd", "max_cost_usd"),
                  ("actions", "max_actions"), ("wall_s", "max_wall_s"))
        for used, limit in checks:
            cap = float(self.budget.get(limit, 0) or 0)
            if cap and float(usage.get(used, 0) or 0) > cap:
                raise BudgetExceeded("automation %s exceeded %s" % (self.execution_id, limit))
        return usage

    def check(self):
        return self.consume()


class ExecutionAuthority:
    """Permission checks that also append an allow/deny receipt to the audit ledger."""

    def __init__(self, store: AutomationStore, request: dict):
        self.store, self.request = store, request
        self.policy = PermissionPolicy.from_dict(request.get("permissions"))

    def _check(self, operation: str, target: str, fn: Callable[[], Any]):
        aid = str(self.request.get("automation_id") or "")
        eid = str(self.request.get("execution_id") or "")
        try:
            result = fn()
            decision, detail = "allowed", {"operation": operation, "target": target}
        except PermissionDenied:
            decision, detail = "denied", {"operation": operation, "target": target}
            self.store.audit(aid, "permission", decision, detail, execution_id=eid)
            self.store.db.commit()
            raise
        self.store.audit(aid, "permission", decision, detail, execution_id=eid)
        self.store.db.commit()
        return result

    def read(self, path: str) -> str:
        return self._check("filesystem_read", _canonical(path),
                           lambda: self.policy.require_read(path))

    def write(self, path: str) -> str:
        return self._check("filesystem_write", _canonical(path),
                           lambda: self.policy.require_write(path))

    def network(self, url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        return self._check("network", parsed.hostname or "", lambda: self.policy.require_url(url))

    def tool(self, name: str) -> str:
        return self._check("tool", str(name), lambda: self.policy.require_tool(name))

    def external_write(self, target: str = "external"):
        return self._check("external_write", target, self.policy.require_external_write)


class WorkspaceAllocator:
    """Provision an empty per-execution directory or validate explicit current-workspace use."""

    def __init__(self, root: str | None = None):
        self.root = _canonical(root or os.path.expanduser("~/.collie/automation-workspaces"))
        os.makedirs(self.root, exist_ok=True)

    def prepare(self, request: dict) -> str:
        policy = PermissionPolicy.from_dict(request.get("permissions"))
        workspace = request.get("workspace") or {}
        if workspace.get("mode", "isolated") == "current":
            path = _canonical(str(workspace.get("path") or ""))
            if not policy.current_workspace or not _inside(path, policy.write_roots):
                raise PermissionDenied("current workspace is not inside a permitted write root")
            return path
        source = str(workspace.get("source") or "").strip()
        if source:
            source = policy.require_read(source)
            # git worktree creation writes refs/worktrees metadata in the source repository. Make
            # that authority explicit rather than smuggling a write through a nominal read root.
            policy.require_write(source)
            from . import worktree
            prepared = worktree.prepare(
                source, request["execution_id"], label=request["automation_id"])
            if not prepared.get("ok") or prepared.get("kind") != "worktree":
                raise AutomationError(prepared.get("error") or
                                      "could not provision an isolated git worktree")
            request["workspace_branch"] = prepared.get("branch", "")
            request["workspace_source"] = source
            return prepared["dir"]
        path = os.path.join(self.root, request["execution_id"])
        os.makedirs(path, exist_ok=True)
        marker = os.path.join(path, ".collie-automation.json")
        if not os.path.exists(marker):
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"execution_id": request["execution_id"],
                           "automation_id": request["automation_id"]}, fh)
        return path


class AutomationExecutor:
    """Lease and run one execution request through a supplied, budget-aware runner."""

    def __init__(self, store: AutomationStore,
                 runner: Callable[[dict, BudgetGuard], dict], *,
                 workspace_allocator: WorkspaceAllocator | None = None,
                 notification_store=None):
        self.store, self.runner = store, runner
        self.workspaces = workspace_allocator or WorkspaceAllocator()
        self.notifications = notification_store

    def _notify(self, request: dict, event: str, body: str):
        if not self.notifications or event not in request.get("notifications", ()):
            return
        self.notifications.enqueue(
            "automation_" + event, "Collie automation %s" % event, body,
            severity="error" if event in ("failure", "needs_you") else "info",
            payload={"automation_id": request["automation_id"],
                     "execution_id": request["execution_id"]},
            dedupe_key="automation:%s:%s" % (request["execution_id"], event), cooldown_s=0)

    def cancel(self):
        cancel = getattr(self.runner, "cancel", None)
        if cancel:
            cancel()

    def step(self, *, now: float | None = None) -> str:
        now = float(time.time() if now is None else now)
        row = self.store.claim(now=now)
        if not row:
            return "idle"
        request = row["request"]
        guard = BudgetGuard(self.store, request["execution_id"], request.get("budget") or {},
                            request=request)
        try:
            request["resolved_workspace"] = self.workspaces.prepare(request)
            self._notify(request, "start", "Automation %s started" % request["automation_id"])
            result = self.runner(request, guard) or {}
            guard.check()
            status = str(result.get("status") or SUCCEEDED)
            if status not in (SUCCEEDED, FAILED, NEEDS_YOU):
                raise ValueError("runner returned invalid status %s" % status)
            error = str(result.get("error") or "")
        except (BudgetExceeded, PermissionDenied) as exc:
            status, result, error = NEEDS_YOU, {}, "%s: %s" % (type(exc).__name__, exc)
        except Exception as exc:
            error = "%s: %s" % (type(exc).__name__, exc)
            perms = PermissionPolicy.from_dict(request.get("permissions"))
            max_retries = int((request.get("budget") or {}).get("max_retries", 1))
            status = (PENDING if not perms.external_writes and row["attempts"] <= max_retries
                      else FAILED)
            result = {}
        self.store.finish(request["execution_id"], status, result, error, now=now)
        if status != PENDING:
            notice = "success" if status == SUCCEEDED else (
                "needs_you" if status == NEEDS_YOU else "failure")
            self._notify(request, notice, error or "Automation %s %s" % (
                request["automation_id"], status))
        return status


class _LimitedTool:
    """Enforce snapshotted filesystem authority plus action/wall limits per tool call."""

    def __init__(self, inner, deadline: float, counter: dict, max_actions: int,
                 policy: PermissionPolicy, cwd: str, audit_store=None,
                 automation_id: str = "", execution_id: str = ""):
        self.inner, self.deadline, self.counter, self.max_actions = (
            inner, deadline, counter, max_actions)
        self.policy, self.cwd, self.audit_store = policy, _canonical(cwd), audit_store
        self.automation_id, self.execution_id = automation_id, execution_id
        for name in ("name", "description", "tier", "schema"):
            setattr(self, name, getattr(inner, name))

    def provider_schema(self):
        return self.inner.provider_schema()

    def _audit(self, decision: str, target: str, reason: str = ""):
        if self.audit_store is None:
            return
        self.audit_store.audit(self.automation_id, "permission", decision, {
            "operation": "tool", "tool": self.name, "target": target,
            "reason": reason[:300]}, execution_id=self.execution_id)
        self.audit_store.db.commit()

    def _path(self, raw: str, *, write: bool) -> str:
        path = _canonical(raw if os.path.isabs(raw) else os.path.join(self.cwd, raw))
        roots = ((self.policy.write_roots if write else self.policy.read_roots) + (self.cwd,))
        if not _inside(path, roots):
            raise PermissionDenied("%s path is outside snapshotted roots" % self.name)
        return path

    def _authorize_args(self, args: dict):
        if self.name in ("read_file", "grep"):
            self._path(str(args.get("path") or "."), write=False)
        elif self.name in ("write_file", "edit_file"):
            self._path(str(args.get("path") or ""), write=True)
        elif self.name == "glob":
            pattern = str(args.get("pattern") or "")
            if os.path.isabs(pattern) or ".." in pattern.replace("\\", "/").split("/"):
                raise PermissionDenied("glob pattern may not escape the resolved workspace")

    def run(self, args, ctx):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.counter["actions"] >= self.max_actions:
            self.counter["cancelled"] = True
            return "ERROR: automation wall/action budget exhausted"
        args = dict(args or {})
        try:
            self._authorize_args(args)
        except PermissionDenied as exc:
            self._audit("denied", str(args.get("path") or args.get("pattern") or ""), str(exc))
            return "ERROR: permission denied: %s" % exc
        self._audit("allowed", str(args.get("path") or args.get("pattern") or self.name))
        props = (self.schema or {}).get("properties") or {}
        for key in ("timeout_s", "timeout"):
            if key in props:
                try:
                    requested = int(args.get(key, max(1, int(remaining))))
                except (TypeError, ValueError):
                    requested = max(1, int(remaining))
                args[key] = max(1, min(requested, int(max(1, remaining))))
                break
        self.counter["actions"] += 1
        result = self.inner.run(args, ctx)
        if time.monotonic() >= self.deadline or self.counter["actions"] >= self.max_actions:
            self.counter["cancelled"] = True
        return result


def _unscopable_unattended_tool(name: str) -> bool:
    """Return true when ambient authority cannot be reduced to this execution's snapshot."""
    return (name in ("bash", "execute_code", "load_tools", "enable_capability", "screenshot")
            or name.startswith(("browser_", "desktop_", "mcp__", "mcpctl_")))


def _run_collie_request(request: dict) -> dict:
    """Child-process body for :class:`DefaultCollieRunner`."""
    from . import sessions, settings
    from .cli import default_gate, make_harness

    settings.apply()
    execution = request.get("execution") or {}
    budget = request.get("budget") or {}
    provider = str(execution.get("provider") or os.environ.get("COLLIE_PROVIDER") or "")
    if not provider:
        raise PermissionDenied(
            "automation has no configured model provider; set execution.provider or Settings")
    if provider == "mock" and not execution.get("allow_mock", False):
        raise PermissionDenied("mock provider is disabled for unattended automations")
    cwd = str(request.get("resolved_workspace") or "")
    mode = str(execution.get("mode") or "project")
    if mode not in ("plan", "project"):
        raise PermissionDenied("unattended Collie runner only permits plan/project modes")
    harness = make_harness(
        cwd, provider=provider, model=execution.get("model") or None,
        project=str(execution.get("project") or request["automation_id"]),
        web_search=False, exec_code=True, delegate=False, gate=default_gate(cwd, mode))
    wall_cap = float(budget.get("max_wall_s") or 1800)
    deadline = time.monotonic() + wall_cap
    counter = {"actions": 0, "cancelled": False}
    authority_store = (AutomationStore(str(request.get("_authority_db")))
                       if request.get("_authority_db") else None)
    try:
        harness.max_turns = min(harness.max_turns, int(budget.get("max_turns") or 50))
        harness._max_turns_hard_cap = harness.max_turns
        if hasattr(harness.provider, "max_tokens"):
            harness.provider.max_tokens = max(1, min(
                int(harness.provider.max_tokens), int(budget.get("max_model_tokens") or 1)))
        if hasattr(harness.provider, "timeout"):
            harness.provider.timeout = max(.2, min(float(harness.provider.timeout), wall_cap))
        allowed = set((request.get("permissions") or {}).get("tools") or ())
        # These tools cannot be scoped precisely by path/host after handoff (shell can `cd ..` or
        # open arbitrary sockets; browser/MCP/desktop calls operate on ambient authenticated state).
        # The default unattended executor therefore never registers them. A future OS sandbox or
        # authenticated external-action executor can implement a separate runner explicitly.
        unsafe = {name for name in harness.registry._tools  # noqa: SLF001
                  if _unscopable_unattended_tool(name)}
        if "*" not in allowed:
            harness.registry._tools = {name: tool for name, tool in harness.registry._tools.items()
                                       if name in allowed and name not in unsafe}  # noqa: SLF001
        else:
            harness.registry._tools = {name: tool for name, tool in harness.registry._tools.items()
                                       if name not in unsafe}  # noqa: SLF001
        if authority_store is not None:
            authority_store.audit(request["automation_id"], "tool_allowlist", "enforced", {
                "registered": sorted(harness.registry._tools),  # noqa: SLF001
                "denied_unscopable": sorted(allowed & unsafe),
            }, execution_id=request["execution_id"])
            authority_store.db.commit()
        policy = PermissionPolicy.from_dict(request.get("permissions"))
        harness.registry._tools = {  # noqa: SLF001
            name: _LimitedTool(tool, deadline, counter,
                               int(budget.get("max_actions") or 1), policy, cwd,
                               authority_store, request["automation_id"],
                               request["execution_id"])
            for name, tool in harness.registry._tools.items()}  # noqa: SLF001
        harness.cancelled = lambda: counter["cancelled"] or time.monotonic() >= deadline

        context = request.get("context") or {}
        history = None
        if context.get("policy") == "continued":
            saved = sessions.load(str(context.get("session_id") or ""))
            if not saved:
                raise PermissionDenied("continued automation session does not exist")
            history = saved.get("messages") or []
            session_id = str(context["session_id"])
        else:
            session_id = sessions.new_id()
        harness.checkpoint_scope = "session:" + session_id
        result = harness.run("automation:" + request["execution_id"], request["task"],
                             history=history)
        sessions.save(session_id, result.messages,
                      project=str(execution.get("project") or request["automation_id"]),
                      cwd=cwd, answer=result.answer or "")
        return {
            "status": NEEDS_YOU if counter["cancelled"] else (
                FAILED if result.error else SUCCEEDED),
            "error": ("automation wall/action budget exhausted" if counter["cancelled"] else
                      str(result.error or "")[:2000]),
            "session_id": session_id, "summary": str(result.answer or "")[:4000],
            "model": str(getattr(result, "model", "") or ""),
            "total_tokens": int(getattr(result, "total_tokens", 0) or 0),
            "cost_usd": float(getattr(result, "cost_usd", 0) or 0),
            "tool_calls": int(getattr(result, "tool_calls", 0) or 0),
        }
    finally:
        if authority_store is not None:
            authority_store.close()
        try:
            harness.memory.close()
        except Exception:
            pass
        try:
            harness.recorder.close()
        except Exception:
            pass


class DefaultCollieRunner:
    """Run native Collie in a killable child with hard wall/token/cost/turn/action caps."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None

    def cancel(self):
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            from . import plat
            plat.kill_tree(proc)

    def __call__(self, request: dict, guard: BudgetGuard) -> dict:
        budget = request.get("budget") or {}
        allowed = sorted(set((request.get("permissions") or {}).get("tools") or ()))
        guard.store.audit(request["automation_id"], "tool_allowlist", "enforced", {
            "tools": allowed}, execution_id=request["execution_id"])
        guard.store.db.commit()
        temp_root = tempfile.mkdtemp(prefix="collie-automation-exec-")
        request_path, result_path = (os.path.join(temp_root, name)
                                     for name in ("request.json", "result.json"))
        try:
            fd = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            child_request = dict(request)
            child_request["_authority_db"] = guard.store.path
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(child_request, fh, ensure_ascii=False)
            env = os.environ.copy()
            env["COLLIE_MAX_TOTAL_TOKENS"] = str(int(budget["max_model_tokens"]))
            env["COLLIE_MAX_COST"] = str(float(budget["max_cost_usd"]))
            env["COLLIE_MAX_TURNS"] = str(int(budget["max_turns"]))
            env["COLLIE_HTTP_TIMEOUT"] = str(max(.2, float(budget["max_wall_s"])))
            proc = subprocess.Popen(
                [sys.executable, "-m", "harness.automations", "_execute",
                 "--request", request_path, "--result", result_path],
                cwd=request["resolved_workspace"], env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with self._lock:
                self._proc = proc
            try:
                proc.wait(timeout=max(.2, float(budget["max_wall_s"])))
            except subprocess.TimeoutExpired:
                from . import plat
                plat.kill_tree(proc)
                raise BudgetExceeded("automation hard wall-time budget exhausted")
            try:
                with open(result_path, encoding="utf-8") as fh:
                    result = json.load(fh)
            except Exception:
                raise AutomationError("Collie automation child exited %s without a result" %
                                      proc.returncode)
            if result.get("exception"):
                kind = result.get("exception_type")
                if kind == "PermissionDenied":
                    raise PermissionDenied(str(result["exception"]))
                if kind == "BudgetExceeded":
                    raise BudgetExceeded(str(result["exception"]))
                raise AutomationError(str(result["exception"]))
            guard.consume(model_tokens=int(result.get("total_tokens") or 0),
                          cost_usd=float(result.get("cost_usd") or 0),
                          actions=int(result.get("tool_calls") or 0))
            return result
        finally:
            with self._lock:
                self._proc = None
            shutil.rmtree(temp_root, ignore_errors=True)


class AutomationDaemon:
    """Continuously poll triggers while at most one execution worker runs in parallel."""

    def __init__(self, engine: TriggerEngine, executor: AutomationExecutor, *,
                 interval_s: float = 5.0, ops_store=None):
        self.engine, self.executor = engine, executor
        self.interval_s = max(0.2, float(interval_s))
        self.ops_store = ops_store
        self.stop_event = threading.Event()
        self._executor_thread = None
        self._last_execution_state = "idle"

    def request_stop(self, *_):
        self.stop_event.set()

    def _execute_one(self):
        try:
            self._last_execution_state = self.executor.step()
        except Exception as exc:
            self._last_execution_state = "failed: %s: %s" % (type(exc).__name__, exc)

    def step(self, now: float | None = None) -> dict:
        now = float(time.time() if now is None else now)
        created = self.engine.tick(now)
        pending = int(self.engine.store.db.execute(
            "SELECT count(*) FROM executions WHERE state=?", (PENDING,)).fetchone()[0])
        thread = self._executor_thread
        if pending and (thread is None or not thread.is_alive()):
            thread = threading.Thread(target=self._execute_one, name="automation-executor",
                                      daemon=True)
            self._executor_thread = thread
            thread.start()
        detail = {"created": len(created), "pending": pending,
                  "executor": self._last_execution_state,
                  "executor_alive": bool(self._executor_thread and
                                         self._executor_thread.is_alive())}
        if self.ops_store is not None:
            self.ops_store.beat("automation-daemon", "running", detail,
                                ttl=max(10.0, self.interval_s * 3), now=now)
        return detail

    def run(self) -> int:
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    signal.signal(sig, self.request_stop)
                except (ValueError, OSError):
                    pass
        try:
            while not self.stop_event.is_set():
                try:
                    self.step()
                except Exception as exc:
                    if self.ops_store is not None:
                        self.ops_store.beat("automation-daemon", "failed", {
                            "error": "%s: %s" % (type(exc).__name__, exc)}, ttl=60)
                self.stop_event.wait(self.interval_s)
            return 0
        finally:
            if self._executor_thread and self._executor_thread.is_alive():
                self.executor.cancel()
                self._executor_thread.join(timeout=10)
                if self._executor_thread.is_alive() and self.ops_store is not None:
                    self.ops_store.beat("automation-daemon", "shutdown_timeout", {
                        "reason": "executor did not stop after child termination"}, ttl=120)


def _cli_paths(args):
    state = os.path.abspath(os.path.expanduser(args.state_dir or
        os.environ.get("COLLIE_STATE_DIR") or "~/.collie"))
    db = os.path.abspath(os.path.expanduser(args.db or os.path.join(state, "automations.db")))
    ops_db = os.path.abspath(os.path.expanduser(args.ops_db or os.path.join(state, "ops.db")))
    workspaces = os.path.abspath(os.path.expanduser(
        args.workspace_root or os.path.join(state, "automation-workspaces")))
    return state, db, ops_db, workspaces


def _add_paths(parser):
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--db", default="")
    parser.add_argument("--ops-db", default="")
    parser.add_argument("--workspace-root", default="")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.automations")
    sub = parser.add_subparsers(dest="action", required=True)
    daemon_p = sub.add_parser("daemon", help="poll triggers and execute durable requests")
    _add_paths(daemon_p); daemon_p.add_argument("--interval", type=float, default=5)
    tick_p = sub.add_parser("tick", help="poll every trigger once (catch-up after sleep/reboot)")
    _add_paths(tick_p); tick_p.add_argument("--execute", action="store_true")
    list_p = sub.add_parser("list", help="list configured automations")
    _add_paths(list_p)
    status_p = sub.add_parser("status", help="show execution and audit state")
    _add_paths(status_p); status_p.add_argument("automation_id", nargs="?", default="")
    upsert_p = sub.add_parser("upsert", help="validate and store a JSON automation spec")
    _add_paths(upsert_p); upsert_p.add_argument("config", help="JSON file, or - for stdin")
    child_p = sub.add_parser("_execute", help=argparse.SUPPRESS)
    child_p.add_argument("--request", required=True); child_p.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    if args.action == "_execute":
        try:
            with open(args.request, encoding="utf-8") as fh:
                value = _run_collie_request(json.load(fh))
        except Exception as exc:
            value = {"exception_type": type(exc).__name__,
                     "exception": "%s: %s" % (type(exc).__name__, exc)}
        fd = os.open(args.result, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False)
            fh.flush(); os.fsync(fh.fileno())
        return 0
    state, db_path, ops_path, workspace_root = _cli_paths(args)

    if args.action == "upsert":
        raw = sys.stdin.read() if args.config == "-" else open(
            os.path.abspath(args.config), encoding="utf-8").read()
        values = json.loads(raw)
        values = values if isinstance(values, list) else [values]
        with AutomationStore(db_path) as store:
            specs = [store.upsert(value).as_dict() for value in values]
        print(json.dumps(specs, ensure_ascii=False, indent=2))
        return 0
    if args.action == "list":
        with AutomationStore(db_path) as store:
            values = [spec.as_dict() for spec in store.specs(enabled_only=False)]
        print(json.dumps(values, ensure_ascii=False, indent=2))
        return 0
    if args.action == "status":
        with AutomationStore(db_path) as store:
            value = {"executions": store.executions(args.automation_id),
                     "audit": store.audit_log(args.automation_id),
                     "configured": [spec.automation_id for spec in store.specs(False)]}
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    if args.action == "tick":
        from .ops import OpsStore
        with AutomationStore(db_path) as store:
            created = TriggerEngine(store).tick()
        result = {"created": created}
        if args.execute:
            with AutomationStore(db_path) as exec_store, OpsStore(ops_path) as ops_store:
                executor = AutomationExecutor(
                    exec_store, DefaultCollieRunner(),
                    workspace_allocator=WorkspaceAllocator(workspace_root),
                    notification_store=ops_store)
                result["execution"] = executor.step()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    from .ops import OpsStore
    from .supervisor import InstanceLock
    os.makedirs(state, exist_ok=True)
    lock = InstanceLock(os.path.join(state, "automations.lock"))
    trigger_store = AutomationStore(db_path)
    executor_store = AutomationStore(db_path)
    ops_store = OpsStore(ops_path)
    try:
        daemon = AutomationDaemon(
            TriggerEngine(trigger_store),
            AutomationExecutor(executor_store, DefaultCollieRunner(),
                               workspace_allocator=WorkspaceAllocator(workspace_root),
                               notification_store=ops_store),
            interval_s=args.interval, ops_store=ops_store)
        return daemon.run()
    finally:
        executor_store.close(); trigger_store.close(); ops_store.close(); lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
