"""Durable operational state for a Collie that is expected to stay up.

The agent loop owns *work*.  This module owns the deliberately boring things around it:

* cross-process heartbeats;
* a leased, retryable notification outbox with a dead-letter state;
* safe credential-expiry summaries (never token values);
* aggregate health data suitable for ``GET /api/healthz``;
* bounded, rotating text logs.

Everything is stdlib-only and SQLite-backed.  A process may disappear after any statement; the
next process can reclaim an expired delivery lease without pretending an uncertain delivery
succeeded.  Callers should expose :func:`aggregate_health` through their existing authenticated
HTTP surface rather than starting another network listener here.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable


DEFAULT_STATE_DIR = os.path.expanduser("~/.collie")
DEFAULT_DB = os.path.join(DEFAULT_STATE_DIR, "ops.db")


class OutboxFull(RuntimeError):
    """Neither the live outbox nor its bounded dead-letter queue can accept another item."""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


class OpsStore:
    """One small WAL database shared by the supervisor and its workers."""

    def __init__(self, path: str | None = None, *, outbox_cap: int = 1000,
                 dead_letter_cap: int = 2000):
        self.path = os.path.abspath(path or os.environ.get("COLLIE_OPS_DB") or DEFAULT_DB)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.outbox_cap = max(1, int(outbox_cap))
        self.dead_letter_cap = max(1, int(dead_letter_cap))
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS heartbeats(
                name TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                pid INTEGER NOT NULL DEFAULT 0,
                at REAL NOT NULL,
                expires_at REAL NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS notifications(
                notification_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                delivered_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS notifications_due
              ON notifications(state, next_attempt_at, created_at);
            CREATE INDEX IF NOT EXISTS notifications_dedupe
              ON notifications(dedupe_key, updated_at);
        """)
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ---------------------------------------------------------------- heartbeats
    def beat(self, name: str, state: str = "ok", detail: dict | None = None,
             *, pid: int | None = None, ttl: float = 45.0, now: float | None = None):
        now = float(time.time() if now is None else now)
        safe_detail = dict(detail or {})
        # Error strings are useful; unbounded provider responses and tracebacks are not.
        for key, value in list(safe_detail.items()):
            if isinstance(value, str):
                safe_detail[key] = value[:500]
        with self._lock:
            self.db.execute(
                "INSERT INTO heartbeats(name,state,pid,at,expires_at,detail_json) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET state=excluded.state,pid=excluded.pid,"
                "at=excluded.at,expires_at=excluded.expires_at,detail_json=excluded.detail_json",
                (str(name)[:120], str(state)[:40],
                 int(os.getpid() if pid is None else pid), now,
                 now + max(1.0, float(ttl)), _json(safe_detail)))
            self.db.commit()

    def heartbeats(self, *, now: float | None = None) -> dict[str, dict]:
        now = float(time.time() if now is None else now)
        with self._lock:
            rows = list(self.db.execute("SELECT * FROM heartbeats ORDER BY name"))
        out = {}
        for row in rows:
            try:
                detail = json.loads(row["detail_json"] or "{}")
            except ValueError:
                detail = {}
            out[row["name"]] = {
                "state": row["state"], "pid": row["pid"], "at": row["at"],
                "age_s": max(0.0, now - row["at"]),
                "fresh": row["expires_at"] >= now, "detail": detail,
            }
        return out

    # -------------------------------------------------------- notification outbox
    def enqueue(self, kind: str, title: str, body: str, *, severity: str = "warning",
                payload: dict | None = None, dedupe_key: str = "", cooldown_s: float = 300,
                now: float | None = None) -> str:
        """Durably enqueue an alert, or return the recent duplicate's id.

        Once the live queue is full the item is recorded directly as a dead letter.  Once *that*
        bounded ledger is full, :class:`OutboxFull` is raised; overflow is never reported as sent.
        """
        now = float(time.time() if now is None else now)
        dedupe_key = str(dedupe_key or "")[:240]
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if dedupe_key:
                    row = self.db.execute(
                        "SELECT notification_id,updated_at FROM notifications WHERE dedupe_key=? "
                        "ORDER BY updated_at DESC LIMIT 1", (dedupe_key,)).fetchone()
                    if row and now - float(row["updated_at"]) < max(0.0, float(cooldown_s)):
                        self.db.commit()
                        return str(row["notification_id"])
                live = self.db.execute(
                    "SELECT count(*) FROM notifications WHERE state IN ('pending','delivering')"
                ).fetchone()[0]
                dead = self.db.execute(
                    "SELECT count(*) FROM notifications WHERE state='dead'"
                ).fetchone()[0]
                state, last_error = "pending", ""
                if live >= self.outbox_cap:
                    if dead >= self.dead_letter_cap:
                        raise OutboxFull("notification outbox and dead-letter queue are full")
                    state, last_error = "dead", "outbox capacity exceeded"
                nid = "ntf_" + uuid.uuid4().hex
                self.db.execute(
                    "INSERT INTO notifications(notification_id,dedupe_key,kind,severity,title,body,"
                    "payload_json,state,attempts,next_attempt_at,lease_until,last_error,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,0,?,0,?,?,?)",
                    (nid, dedupe_key, str(kind)[:80], str(severity)[:20], str(title)[:160],
                     str(body)[:1000], _json(payload or {}), state, now, last_error, now, now))
                self.db.commit()
                return nid
            except Exception:
                self.db.rollback()
                raise

    def claim(self, *, limit: int = 10, lease_s: float = 60,
              now: float | None = None) -> list[dict]:
        now = float(time.time() if now is None else now)
        claimed = []
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # A crash after delivery started is uncertain.  Retry is intentional for
                # notifications (unlike irreversible work), and every sink should use the id as its
                # idempotency key when it has such a facility.
                self.db.execute(
                    "UPDATE notifications SET state='pending',lease_until=0,next_attempt_at=?,"
                    "last_error=CASE WHEN last_error='' THEN 'delivery lease expired' ELSE last_error END,"
                    "updated_at=? WHERE state='delivering' AND lease_until<=?", (now, now, now))
                rank = "CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1 " \
                       "WHEN 'warning' THEN 2 ELSE 3 END"
                rows = list(self.db.execute(
                    "SELECT * FROM notifications WHERE state='pending' AND next_attempt_at<=? "
                    "ORDER BY %s,created_at LIMIT ?" % rank, (now, max(1, int(limit)))))
                for row in rows:
                    changed = self.db.execute(
                        "UPDATE notifications SET state='delivering',attempts=attempts+1,"
                        "lease_until=?,updated_at=? WHERE notification_id=? AND state='pending'",
                        (now + max(1.0, float(lease_s)), now, row["notification_id"]))
                    if changed.rowcount == 1:
                        item = dict(row)
                        item["attempts"] = int(item["attempts"]) + 1
                        try:
                            item["payload"] = json.loads(item.pop("payload_json") or "{}")
                        except ValueError:
                            item["payload"] = {}
                        claimed.append(item)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return claimed

    def delivered(self, notification_id: str, *, now: float | None = None) -> bool:
        now = float(time.time() if now is None else now)
        with self._lock:
            changed = self.db.execute(
                "UPDATE notifications SET state='delivered',delivered_at=?,lease_until=0,"
                "last_error='',updated_at=? WHERE notification_id=? AND state='delivering'",
                (now, now, notification_id))
            self.db.commit()
            return changed.rowcount == 1

    def failed(self, notification_id: str, error: str, *, max_attempts: int = 8,
               base_delay_s: float = 5, max_delay_s: float = 1800,
               now: float | None = None) -> str:
        now = float(time.time() if now is None else now)
        with self._lock:
            row = self.db.execute(
                "SELECT attempts,state FROM notifications WHERE notification_id=?",
                (notification_id,)).fetchone()
            if not row or row["state"] != "delivering":
                return "missing"
            attempts = int(row["attempts"])
            state = "dead" if attempts >= max(1, int(max_attempts)) else "pending"
            delay = 0.0 if state == "dead" else min(
                float(max_delay_s), float(base_delay_s) * (2 ** max(0, attempts - 1)))
            self.db.execute(
                "UPDATE notifications SET state=?,next_attempt_at=?,lease_until=0,last_error=?,"
                "updated_at=? WHERE notification_id=? AND state='delivering'",
                (state, now + delay, (str(error) or "delivery returned false")[:500],
                 now, notification_id))
            self.db.commit()
            return state

    def deliver_once(self, sender: Callable[[dict], bool], *, limit: int = 10,
                     max_attempts: int = 8, now: float | None = None) -> dict:
        sent = retried = dead = 0
        for item in self.claim(limit=limit, now=now):
            try:
                ok = bool(sender(item))
                error = "" if ok else "delivery returned false"
            except Exception as exc:  # delivery must never kill the supervisor
                ok, error = False, "%s: %s" % (type(exc).__name__, exc)
            if ok and self.delivered(item["notification_id"], now=now):
                sent += 1
            elif not ok:
                state = self.failed(item["notification_id"], error,
                                    max_attempts=max_attempts, now=now)
                dead += state == "dead"
                retried += state == "pending"
        return {"sent": sent, "retried": retried, "dead": dead}

    def notification_stats(self) -> dict[str, int]:
        with self._lock:
            rows = self.db.execute(
                "SELECT state,count(*) AS n FROM notifications GROUP BY state").fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    def dead_letters(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT notification_id,kind,severity,title,attempts,last_error,created_at,updated_at "
                "FROM notifications WHERE state='dead' ORDER BY updated_at DESC LIMIT ?",
                (max(1, int(limit)),)).fetchall()
        return [dict(row) for row in rows]

    def prune(self, *, delivered_before: float, dead_before: float | None = None) -> int:
        with self._lock:
            n = self.db.execute(
                "DELETE FROM notifications WHERE state='delivered' AND delivered_at<?",
                (float(delivered_before),)).rowcount
            if dead_before is not None:
                n += self.db.execute(
                    "DELETE FROM notifications WHERE state='dead' AND updated_at<?",
                    (float(dead_before),)).rowcount
            self.db.commit()
            return n


def heartbeat(name: str, state: str = "ok", detail: dict | None = None, *,
              ttl: float = 45.0, db_path: str | None = None):
    """Cheap convenience for workers that do not otherwise own an :class:`OpsStore`."""
    try:
        with OpsStore(db_path) as store:
            store.beat(name, state, detail, ttl=ttl)
    except Exception:
        pass  # observability must not take the observed worker down


@dataclass
class CredentialStatus:
    name: str
    state: str
    expires_at: float = 0.0
    seconds_remaining: float | None = None
    refresh_available: bool = False
    refresh_owner: str = ""
    action: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _jwt_exp(token: str) -> float:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return float(json.loads(base64.urlsafe_b64decode(part)).get("exp") or 0)
    except Exception:
        return 0.0


def credential_health(*, now: float | None = None, claude_path: str | None = None,
                      codex_path: str | None = None) -> list[dict]:
    """Return credential *metadata* only.  Access/refresh token values never leave this function."""
    now = float(time.time() if now is None else now)
    claude_path = claude_path or os.path.expanduser("~/.claude/.credentials.json")
    codex_path = codex_path or os.path.expanduser("~/.codex/auth.json")
    out = []

    claude = (_load_json(claude_path).get("claudeAiOauth") or {})
    if claude:
        try:
            expires = float(claude.get("expiresAt") or 0) / 1000.0
        except (TypeError, ValueError):
            expires = 0.0
        remaining = expires - now if expires else None
        state = "expired" if remaining is not None and remaining <= 0 else \
            ("expiring" if remaining is not None and remaining <= 3600 else "ok")
        out.append(CredentialStatus(
            "claude-oauth", state, expires, remaining, bool(claude.get("refreshToken")),
            # Claude Code owns its credential store. Collie alerts instead of racing its writer.
            "claude-code", "run `claude` to refresh the subscription login").as_dict())
    else:
        out.append(CredentialStatus(
            "claude-oauth", "missing", action="run `claude` to sign in").as_dict())

    codex = _load_json(codex_path)
    tokens = codex.get("tokens") or {}
    if tokens:
        expires = _jwt_exp(str(tokens.get("access_token") or ""))
        remaining = expires - now if expires else None
        refresh = bool(tokens.get("refresh_token"))
        state = "expired" if remaining is not None and remaining <= 0 and not refresh else \
            ("expiring" if remaining is not None and remaining <= 3600 else "ok")
        out.append(CredentialStatus(
            "codex-oauth", state, expires, remaining, refresh, "collie-codex-owner",
            "run `codex login`" if not refresh else "").as_dict())
    else:
        out.append(CredentialStatus(
            "codex-oauth", "missing", action="run `codex login`").as_dict())
    return out


def _probe_json(url: str, timeout: float = 1.5) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read(65536)
        value = json.loads(raw or b"{}")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def slack_queue_health(state_dir: str | None = None) -> dict:
    """Count queue state without exposing task text, channel ids, or users."""
    state_dir = state_dir or os.environ.get("COLLIE_STATE_DIR") or DEFAULT_STATE_DIR
    total = waiting = unresolved = dead = 0
    queues = 0
    try:
        names = [n for n in os.listdir(state_dir) if n.startswith("queue-") and n.endswith(".json")]
    except OSError:
        names = []
    for name in names:
        doc = _load_json(os.path.join(state_dir, name))
        items = doc.get("items") if isinstance(doc.get("items"), list) else []
        dlq = doc.get("dead_letters") if isinstance(doc.get("dead_letters"), list) else []
        queues += 1
        total += len(items)
        waiting += sum(1 for item in items if isinstance(item, dict)
                       and item.get("state") in ("waiting", "delivery_ready"))
        unresolved += sum(1 for item in items if isinstance(item, dict)
                          and item.get("state") in ("interrupted", "orphaned",
                                                    "delivery_failed", "delivery_interrupted"))
        dead += len(dlq)
    return {"queues": queues, "total": total, "waiting": waiting,
            "unresolved": unresolved, "dead_letters": dead}


def aggregate_health(store: OpsStore, *, desired_workers: list[str] | None = None,
                     state_dir: str | None = None, now: float | None = None,
                     probe_services: bool = True) -> dict:
    """Build the safe JSON object the Web layer can return from ``/api/healthz``."""
    now = float(time.time() if now is None else now)
    beats = store.heartbeats(now=now)
    desired_workers = list(desired_workers or [])
    workers = {}
    for name in desired_workers:
        row = beats.get("worker:" + name) or {}
        workers[name] = {
            "state": row.get("state", "missing"), "fresh": bool(row.get("fresh")),
            "age_s": row.get("age_s"), "pid": row.get("pid", 0),
            "detail": row.get("detail", {}),
        }

    services = {}
    if probe_services:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8787/api/ver", timeout=1.0) as r:
                services["web"] = {"ok": r.status == 200}
        except Exception:
            services["web"] = {"ok": False}
        bridge = _probe_json("http://127.0.0.1:8677/health")
        services["browser"] = {
            "ok": bool(bridge.get("ok")),
            "extension_connected": bool(bridge.get("extension_connected")),
            "last_poll_secs_ago": bridge.get("last_poll_secs_ago"),
        }

    credentials = credential_health(now=now)
    queues = {"slack": slack_queue_health(state_dir),
              "notifications": store.notification_stats()}
    failing = [name for name, row in workers.items()
               if not row["fresh"] or row["state"] in ("dead", "failed", "circuit_open")]
    expired = [row["name"] for row in credentials if row["state"] in ("expired", "missing")]
    degraded = bool(failing or expired or queues["slack"]["unresolved"]
                    or queues["slack"]["dead_letters"]
                    or queues["notifications"].get("dead", 0))
    if probe_services:
        degraded = degraded or not services.get("web", {}).get("ok", False)
    return {
        "ok": not degraded, "status": "degraded" if degraded else "ok", "at": now,
        "workers": workers, "services": services, "credentials": credentials,
        "queues": queues, "heartbeats": beats,
    }


def enqueue_health_alerts(store: OpsStore, report: dict, *, backlog_warning: int = 25,
                          credential_warning_s: float = 3600, now: float | None = None) -> list[str]:
    """Turn health facts into deduplicated durable notifications."""
    now = float(time.time() if now is None else now)
    queued = []
    def add(*args, **kwargs):
        try:
            queued.append(store.enqueue(*args, **kwargs))
        except OutboxFull:
            # Health reporting cannot be allowed to crash the supervisor. The full heartbeat is
            # deliberately separate so a local health page can still show why alerts stopped.
            store.beat("notification-outbox", "full", {"reason": "live and DLQ capacity"},
                       ttl=300, now=now)

    for name, row in (report.get("workers") or {}).items():
        if not row.get("fresh") or row.get("state") in ("dead", "failed", "circuit_open"):
            add(
                "worker_dead", "Collie worker needs attention",
                "%s is %s" % (name, row.get("state") or "not reporting"), severity="error",
                payload={"worker": name}, dedupe_key="worker-dead:" + name, now=now)
    services = report.get("services") or {}
    if services.get("web") and not services["web"].get("ok"):
        add("service_down", "Collie web service is unavailable",
            "The local web health probe failed", severity="error",
            payload={"service": "web"}, dedupe_key="service-down:web", now=now)
    browser = services.get("browser") or {}
    if browser and (not browser.get("ok") or not browser.get("extension_connected")):
        add("browser_disconnected", "Collie browser control is disconnected",
            ("The browser bridge process is down" if not browser.get("ok") else
             "The bridge is running but no extension is polling"), severity="warning",
            payload={"service": "browser"}, dedupe_key="service-down:browser", now=now)
    slack = ((report.get("queues") or {}).get("slack") or {})
    if int(slack.get("waiting") or 0) >= int(backlog_warning):
        add(
            "queue_backlog", "Collie queue is backing up",
            "%d Slack tasks are waiting" % int(slack["waiting"]), severity="warning",
            payload={"queue": "slack", "waiting": int(slack["waiting"])},
            dedupe_key="queue-backlog:slack", now=now)
    if int(slack.get("dead_letters") or 0):
        add(
            "dead_letters", "Collie has dead-letter tasks",
            "%d Slack tasks need manual review" % int(slack["dead_letters"]),
            severity="error", payload={"queue": "slack"},
            dedupe_key="dead-letters:slack", now=now)
    notification_dead = int(((report.get("queues") or {}).get("notifications") or {}).get(
        "dead", 0) or 0)
    if notification_dead:
        add("notification_dead_letters", "Collie notifications are not being delivered",
            "%d notifications exhausted their retries" % notification_dead,
            severity="error", payload={"queue": "notifications"},
            dedupe_key="dead-letters:notifications", now=now)
    for cred in report.get("credentials") or []:
        remaining = cred.get("seconds_remaining")
        if cred.get("state") in ("expired", "missing", "expiring") or (
                remaining is not None and remaining <= credential_warning_s):
            mins = max(0, int(float(remaining or 0) / 60))
            body = "%s expires in about %d minutes" % (cred["name"], mins) \
                if remaining is not None and remaining > 0 else "%s is %s" % (
                    cred["name"], cred.get("state"))
            add(
                "credential_expiry", "Collie credential needs attention", body,
                severity="critical" if cred.get("state") == "expired" else "warning",
                payload={"credential": cred["name"], "action": cred.get("action", "")},
                dedupe_key="credential:" + cred["name"] + ":" + cred.get("state", ""),
                cooldown_s=900, now=now)
    return queued


def remote_notification_sender(remote_state) -> Callable[[dict], bool]:
    """Adapt ``RemoteState.notify`` to :meth:`OpsStore.deliver_once`.

    Callers enqueue first, then a pump uses this adapter. A disconnected phone relay returns false
    and therefore schedules a retry instead of silently losing the completion notice.
    """
    def send(item: dict) -> bool:
        payload = item.get("payload") or {}
        return bool(remote_state.notify(
            item.get("title", "Collie"), item.get("body", ""),
            session=str(payload.get("session") or ""),
            thread=str(payload.get("thread") or "collie")))
    return send


class NotificationPump:
    """Background retry pump for a durable notification outbox."""

    def __init__(self, store: OpsStore, sender: Callable[[dict], bool], *,
                 interval_s: float = 5.0, max_attempts: int = 8):
        self.store, self.sender = store, sender
        self.interval_s = max(0.2, float(interval_s))
        self.max_attempts = max(1, int(max_attempts))
        self._stop = threading.Event()
        self._thread = None

    def step(self) -> dict:
        result = self.store.deliver_once(self.sender, max_attempts=self.max_attempts)
        self.store.beat("notification-pump", "running", result,
                        ttl=max(10.0, self.interval_s * 3))
        return result

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        def run():
            while not self._stop.is_set():
                try:
                    self.step()
                except Exception as exc:
                    self.store.beat("notification-pump", "failed", {
                        "error": "%s: %s" % (type(exc).__name__, exc)}, ttl=60)
                self._stop.wait(self.interval_s)
        self._thread = threading.Thread(target=run, name="notification-pump", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(0.0, float(timeout)))


class RotatingLog:
    """A tiny size-based rotating writer safe for a supervisor pipe-reader thread."""

    def __init__(self, path: str, *, max_bytes: int = 5 * 1024 * 1024, backups: int = 5):
        self.path = os.path.abspath(path)
        self.max_bytes = max(1024, int(max_bytes))
        self.backups = max(1, int(backups))
        self._lock = threading.Lock()
        self._file = None
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def _open(self):
        if self._file is None:
            self._file = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)

    def _rotate(self):
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        if self._file is not None:
            self._file.close()
            self._file = None
        try:
            oldest = "%s.%d" % (self.path, self.backups)
            if os.path.exists(oldest):
                os.remove(oldest)
            for n in range(self.backups - 1, 0, -1):
                src, dst = "%s.%d" % (self.path, n), "%s.%d" % (self.path, n + 1)
                if os.path.exists(src):
                    os.replace(src, dst)
            os.replace(self.path, self.path + ".1")
        except OSError:
            # Antivirus/indexers may briefly hold the file on Windows. Keep appending rather than
            # turning log maintenance into a worker outage; the next write tries again.
            pass

    def write(self, text: str):
        with self._lock:
            self._rotate()
            self._open()
            self._file.write(str(text))
            if not str(text).endswith("\n"):
                self._file.write("\n")

    def close(self):
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None
