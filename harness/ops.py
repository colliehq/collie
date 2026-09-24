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
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Callable


DEFAULT_STATE_DIR = os.path.expanduser("~/.collie")
DEFAULT_DB = os.path.join(DEFAULT_STATE_DIR, "ops.db")
HEALTH_NOTIFICATION_KINDS = frozenset({
    "browser_disconnected",
    "credential_expiry",
    "dead_letters",
    "notification_backlog",  # legacy self-referential alert, removed by maintenance
    "notification_dead_letters",
    "queue_backlog",
    "service_down",
    "worker_dead",
})
HEALTH_ALERT_COOLDOWN_S = 24 * 60 * 60


class OutboxFull(RuntimeError):
    """Neither the live outbox nor its bounded dead-letter queue can accept another item."""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)


def _reject_json_constant(value):
    raise ValueError("non-finite JSON number is forbidden: %s" % value)


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key: %s" % key)
        value[key] = item
    return value


def _json_object(value, label="JSON") -> dict:
    parsed = json.loads(
        value or "{}", parse_constant=_reject_json_constant,
        object_pairs_hook=_unique_json_object)
    if not isinstance(parsed, dict):
        raise ValueError("%s must be an object" % label)
    return parsed


def _finite(value, name: str, *, minimum=None) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or (minimum is not None and value < minimum)):
        raise ValueError("%s must be a finite number" % name)
    return float(value)


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(
                f, parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_json_object)
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
        now = _finite(time.time() if now is None else now, "heartbeat now")
        ttl = _finite(ttl, "heartbeat ttl", minimum=1)
        if detail is not None and not isinstance(detail, dict):
            raise ValueError("heartbeat detail must be an object")
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
                 now + ttl, _json(safe_detail)))
            self.db.commit()

    def heartbeats(self, *, now: float | None = None) -> dict[str, dict]:
        now = _finite(time.time() if now is None else now, "heartbeat read time")
        with self._lock:
            rows = list(self.db.execute("SELECT * FROM heartbeats ORDER BY name"))
        out = {}
        for row in rows:
            try:
                detail = _json_object(row["detail_json"], "heartbeat detail")
            except (TypeError, ValueError, json.JSONDecodeError):
                detail = {"_error": "invalid durable heartbeat detail"}
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
        """Durably enqueue an alert, or coalesce it with the unresolved duplicate.

        Once the live queue is full the item is recorded directly as a dead letter.  Once *that*
        bounded ledger is full, :class:`OutboxFull` is raised; overflow is never reported as sent.
        """
        now = _finite(time.time() if now is None else now, "notification enqueue time")
        cooldown_s = _finite(cooldown_s, "notification cooldown", minimum=0)
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("notification payload must be an object")
        payload_json = _json(payload or {})
        dedupe_key = str(dedupe_key or "")[:240]
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if dedupe_key:
                    row = self.db.execute(
                        "SELECT notification_id,state,updated_at FROM notifications "
                        "WHERE dedupe_key=? "
                        "ORDER BY CASE state WHEN 'pending' THEN 0 WHEN 'delivering' THEN 1 "
                        "WHEN 'dead' THEN 2 ELSE 3 END,updated_at DESC LIMIT 1",
                        (dedupe_key,)).fetchone()
                    # Cooldowns suppress repeat reminders after delivery.  An unresolved incident
                    # is different: it must remain one item for its entire lifetime, even after the
                    # cooldown expires.  Refresh pending display data in place so a local UI sees
                    # the current facts without turning every supervisor poll into a new alert.
                    if row and row["state"] == "pending":
                        self.db.execute(
                            "UPDATE notifications SET kind=?,severity=?,title=?,body=?,"
                            "payload_json=?,updated_at=? WHERE notification_id=? AND state='pending'",
                            (str(kind)[:80], str(severity)[:20], str(title)[:160],
                             str(body)[:1000], payload_json, now, row["notification_id"]))
                        self.db.commit()
                        return str(row["notification_id"])
                    if row and row["state"] in ("delivering", "dead"):
                        self.db.commit()
                        return str(row["notification_id"])
                    if row and now - float(row["updated_at"]) < cooldown_s:
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
                     str(body)[:1000], payload_json, state, now, last_error, now, now))
                self.db.commit()
                return nid
            except Exception:
                self.db.rollback()
                raise

    def claim(self, *, limit: int = 10, lease_s: float = 60,
              now: float | None = None) -> list[dict]:
        now = _finite(time.time() if now is None else now, "notification claim time")
        lease_s = _finite(lease_s, "notification lease", minimum=1)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("notification claim limit must be a positive integer")
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
                    "ORDER BY %s,created_at LIMIT ?" % rank, (now, limit)))
                for row in rows:
                    try:
                        payload = _json_object(
                            row["payload_json"], "notification payload")
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        self.db.execute(
                            "UPDATE notifications SET state='dead',lease_until=0,last_error=?,"
                            "updated_at=? WHERE notification_id=? AND state='pending'",
                            (("invalid durable notification payload: %s" % exc)[:500],
                             now, row["notification_id"]))
                        continue
                    changed = self.db.execute(
                        "UPDATE notifications SET state='delivering',attempts=attempts+1,"
                        "lease_until=?,updated_at=? WHERE notification_id=? AND state='pending'",
                        (now + lease_s, now, row["notification_id"]))
                    if changed.rowcount == 1:
                        item = dict(row)
                        item["attempts"] = int(item["attempts"]) + 1
                        item.pop("payload_json", None)
                        item["payload"] = payload
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

    def notification_health(self, *, now: float | None = None,
                            stale_after_s: float = 300.0) -> dict[str, int | float | bool]:
        """Return content-free delivery health, including backlog *age*.

        Counts alone made a queue of two fresh notifications indistinguishable from two items
        stranded since yesterday.  This deliberately exposes only numeric aggregates; titles,
        bodies, payloads and delivery errors stay in the durable store.
        """
        now = _finite(time.time() if now is None else now, "notification health time")
        stale_after_s = _finite(stale_after_s, "notification stale threshold", minimum=1)
        with self._lock:
            rows = self.db.execute(
                "SELECT state,count(*) AS n FROM notifications GROUP BY state").fetchall()
            pending = self.db.execute(
                "SELECT min(created_at) AS oldest,min(next_attempt_at) AS next_due,"
                "sum(attempts) AS attempts,sum(CASE WHEN next_attempt_at<=? THEN 1 ELSE 0 END) AS due "
                "FROM notifications WHERE state IN ('pending','delivering')", (now,)).fetchone()
        out: dict[str, int | float | bool] = {
            str(row["state"]): int(row["n"]) for row in rows
        }
        oldest = float(pending["oldest"] or 0) if pending else 0.0
        next_due = float(pending["next_due"] or 0) if pending else 0.0
        age = max(0.0, now - oldest) if oldest else 0.0
        live = int(out.get("pending", 0)) + int(out.get("delivering", 0))
        out.update(
            live=live,
            due=int((pending["due"] if pending else 0) or 0),
            attempts=int((pending["attempts"] if pending else 0) or 0),
            oldest_pending_at=oldest,
            oldest_pending_age_s=age,
            next_attempt_at=next_due,
            stale=bool(live and age >= stale_after_s),
        )
        return out

    def retry_dead(self, notification_id: str = "", *, now: float | None = None) -> int:
        """Move explicitly selected dead letters back to pending delivery.

        The caller must make the operator confirmation decision.  Payload bytes are retained and
        revalidated by :meth:`claim`; this method never reports a notification as delivered.
        """
        now = _finite(time.time() if now is None else now, "notification retry time")
        notification_id = str(notification_id or "").strip()
        with self._lock:
            if notification_id:
                changed = self.db.execute(
                    "UPDATE notifications SET state='pending',attempts=0,next_attempt_at=?,"
                    "lease_until=0,last_error='',updated_at=? "
                    "WHERE notification_id=? AND state='dead'",
                    (now, now, notification_id))
            else:
                changed = self.db.execute(
                    "UPDATE notifications SET state='pending',attempts=0,next_attempt_at=?,"
                    "lease_until=0,last_error='',updated_at=? WHERE state='dead'",
                    (now, now))
            self.db.commit()
            return int(changed.rowcount)

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

    def maintain_notifications(self, *, delivery_enabled: bool,
                               now: float | None = None,
                               delivered_retention_s: float = 30 * 24 * 60 * 60,
                               dead_retention_s: float = 90 * 24 * 60 * 60,
                               keep_delivered: int = 50) -> dict[str, int]:
        """Keep the outbox quiet and bounded without discarding user-requested notices.

        Health alerts are derived from current local state.  When remote delivery is disabled they
        have no recipient and are safe to dismiss; doctor/control-center projections continue to
        expose the underlying health facts.  Automation and explicit test notifications are never
        removed here.  Legacy duplicate pending rows are coalesced for every non-empty dedupe key.
        """
        if not isinstance(delivery_enabled, bool):
            raise ValueError("delivery_enabled must be boolean")
        now = _finite(time.time() if now is None else now, "notification maintenance time")
        delivered_retention_s = _finite(
            delivered_retention_s, "delivered retention", minimum=0)
        dead_retention_s = _finite(dead_retention_s, "dead retention", minimum=0)
        if (isinstance(keep_delivered, bool) or not isinstance(keep_delivered, int) or
                keep_delivered < 0):
            raise ValueError("keep_delivered must be a non-negative integer")
        result = {"health_dismissed": 0, "duplicates_removed": 0,
                  "delivered_pruned": 0, "dead_pruned": 0}
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # This alert was historically inserted into the queue whose inability to drain it
                # described, which amplified every outage.  It is local-health information only.
                result["health_dismissed"] += int(self.db.execute(
                    "DELETE FROM notifications WHERE state='pending' "
                    "AND kind='notification_backlog'").rowcount)
                if not delivery_enabled:
                    placeholders = ",".join("?" for _ in HEALTH_NOTIFICATION_KINDS)
                    result["health_dismissed"] += int(self.db.execute(
                        "DELETE FROM notifications WHERE state='pending' AND kind IN (%s)" %
                        placeholders, tuple(sorted(HEALTH_NOTIFICATION_KINDS))).rowcount)

                keys = self.db.execute(
                    "SELECT dedupe_key FROM notifications "
                    "WHERE state IN ('pending','delivering') AND dedupe_key<>'' "
                    "GROUP BY dedupe_key HAVING count(*)>1").fetchall()
                for key_row in keys:
                    key = key_row["dedupe_key"]
                    keep = self.db.execute(
                        "SELECT notification_id FROM notifications WHERE dedupe_key=? "
                        "AND state IN ('delivering','pending') "
                        "ORDER BY CASE state WHEN 'delivering' THEN 0 ELSE 1 END,"
                        "updated_at DESC LIMIT 1", (key,)).fetchone()
                    if keep:
                        result["duplicates_removed"] += int(self.db.execute(
                            "DELETE FROM notifications WHERE dedupe_key=? AND state='pending' "
                            "AND notification_id<>?", (key, keep["notification_id"])).rowcount)

                # History is a concise record of distinct incidents, not a poll-by-poll audit log.
                # Keep the latest delivery for each stable incident identity.
                delivered_keys = self.db.execute(
                    "SELECT dedupe_key FROM notifications WHERE state='delivered' "
                    "AND dedupe_key<>'' GROUP BY dedupe_key HAVING count(*)>1").fetchall()
                for key_row in delivered_keys:
                    key = key_row["dedupe_key"]
                    keep = self.db.execute(
                        "SELECT notification_id FROM notifications WHERE dedupe_key=? "
                        "AND state='delivered' ORDER BY delivered_at DESC,updated_at DESC LIMIT 1",
                        (key,)).fetchone()
                    if keep:
                        result["delivered_pruned"] += int(self.db.execute(
                            "DELETE FROM notifications WHERE dedupe_key=? AND state='delivered' "
                            "AND notification_id<>?", (key, keep["notification_id"])).rowcount)

                result["delivered_pruned"] += int(self.db.execute(
                    "DELETE FROM notifications WHERE state='delivered' AND delivered_at<?",
                    (now - delivered_retention_s,)).rowcount)
                if keep_delivered == 0:
                    result["delivered_pruned"] += int(self.db.execute(
                        "DELETE FROM notifications WHERE state='delivered'").rowcount)
                else:
                    result["delivered_pruned"] += int(self.db.execute(
                        "DELETE FROM notifications WHERE state='delivered' AND notification_id "
                        "NOT IN (SELECT notification_id FROM notifications WHERE state='delivered' "
                        "ORDER BY delivered_at DESC,updated_at DESC LIMIT ?)",
                        (keep_delivered,)).rowcount)
                result["dead_pruned"] += int(self.db.execute(
                    "DELETE FROM notifications WHERE state='dead' AND updated_at<?",
                    (now - dead_retention_s,)).rowcount)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return result


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


#: Providers that read one of these credential files themselves. Everything else either uses an
#: API key, or runs a CLI that owns its own login -- on macOS Claude Code keeps it in the Keychain,
#: so the file below is absent on every Mac whether or not the person is signed in.
_READS_CREDENTIAL = {
    "claude-oauth": ("anthropic-oauth", "claude-sub"),
    "codex-oauth": ("codex-oauth", "codex-sub", "codex"),
}


#: Routes that run Claude Code, which keeps its login in this file on Windows and Linux (and in
#: the Keychain on macOS) and refreshes it itself: a missing file there still means no login.
_CLI_OWNS_CLAUDE_LOGIN = ("claude-agent-sdk", "claude-sdk", "claude-cli", "cli")


def _configured_provider(provider=None) -> str:
    if provider is None:
        try:
            from . import settings
            provider = settings.get("PROVIDER", "") or ""
        except Exception:
            provider = ""
    return str(provider or "").strip().lower()


def _needed_credentials(provider=None) -> set:
    """Which listed credentials the configured provider cannot work without."""
    provider = _configured_provider(provider)
    return {name for name, readers in _READS_CREDENTIAL.items() if provider in readers}


def _cli_owned_logins(provider=None) -> set:
    """Credentials that must exist but that their owner refreshes (so expiry is not a problem)."""
    if sys.platform == "darwin":
        return set()
    return {"claude-oauth"} if _configured_provider(provider) in _CLI_OWNS_CLAUDE_LOGIN else set()


def credential_health(*, now: float | None = None, claude_path: str | None = None,
                      codex_path: str | None = None, provider: str | None = None) -> list[dict]:
    """Return credential *metadata* only.  Access/refresh token values never leave this function.

    Both logins are always listed. ``needed`` says whether the configured provider reads that
    file itself; only a needed credential that is missing or expired makes Collie unhealthy. A
    person on one subscription used to see "Needs attention" and a recurring "claude-oauth is
    missing" alert forever for the one they never signed into.
    """
    now = float(time.time() if now is None else now)
    needed = _needed_credentials(provider)
    self_refreshing = _cli_owned_logins(provider)
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
    for row in out:
        row["needed"] = row["name"] in needed or row["name"] in self_refreshing
        if row["name"] in self_refreshing:
            row["self_refreshing"] = True
        if row["name"] == "claude-oauth" and row["state"] == "missing" and (
                sys.platform == "darwin" or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")):
            # Absent file, not absent login: macOS keeps Claude's sign-in in the Keychain, and
            # `claude setup-token` signs in through CLAUDE_CODE_OAUTH_TOKEN with no file at all.
            row["needed"] = False
    return out


def _bridge_health_url() -> str:
    # The port the browser tools use, which is the one worth reporting on.
    return "http://127.0.0.1:%s/health" % (os.environ.get("COLLIE_BROWSER_BRIDGE_PORT") or 8677)


def _probe_json(url: str, timeout: float = 1.5) -> dict:
    from .httpserver import loopback_url_down
    if loopback_url_down(url):
        return {}           # not running; on Windows asking anyway waits out the whole timeout
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
                     probe_services: bool = True, web_port: int | None = None) -> dict:
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
            # The web server moves to the next free port when 8787 is taken; the one answering
            # this very request passes its own, so it cannot report itself unreachable.
            from .httpserver import loopback_url_down
            url = "http://127.0.0.1:%d/api/ver" % int(web_port or 8787)
            if loopback_url_down(url):
                raise OSError("nothing listening")
            with urllib.request.urlopen(url, timeout=1.0) as r:
                services["web"] = {"ok": r.status == 200}
        except Exception:
            services["web"] = {"ok": False}
        bridge = _probe_json(_bridge_health_url())
        services["browser"] = {
            "ok": bool(bridge.get("ok")),
            "extension_connected": bool(bridge.get("extension_connected")),
            "last_poll_secs_ago": bridge.get("last_poll_secs_ago"),
        }

    credentials = credential_health(now=now)
    queues = {"slack": slack_queue_health(state_dir),
              "notifications": store.notification_health(now=now)}
    failing = [name for name, row in workers.items()
               if not row["fresh"] or row["state"] in ("dead", "failed", "circuit_open")]
    expired = [row["name"] for row in credentials
               if row.get("needed", True) and (row["state"] == "missing" or (
                   row["state"] == "expired" and not row.get("self_refreshing")))]
    notification_pump = beats.get("notification-pump") or {}
    notification_stalled = bool(
        queues["notifications"].get("stale") and
        (not notification_pump.get("fresh") or
         notification_pump.get("state") in ("dead", "failed", "circuit_open")))
    issues = []
    if notification_stalled:
        issues.append("notification_delivery_stalled")
    degraded = bool(failing or expired or queues["slack"]["unresolved"]
                    or queues["slack"]["dead_letters"]
                    or queues["notifications"].get("dead", 0)
                    or notification_stalled)
    if probe_services:
        degraded = degraded or not services.get("web", {}).get("ok", False)
    # Why, in the order a person should look. Codes and names only: a surface words them in its
    # own language, and "Needs attention" alone never said what to look at.
    reasons = []
    if probe_services and not services.get("web", {}).get("ok", False):
        reasons.append({"code": "web_unreachable", "subject": "web"})
    for row in credentials:
        if row["name"] in expired:
            reasons.append({"code": "login_" + row["state"], "subject": row["name"],
                            "action": row.get("action", "")})
    for name in failing:
        state = workers[name]["state"]
        reasons.append({"code": "worker_stopped" if state in ("dead", "failed", "circuit_open")
                        else "worker_not_reporting", "subject": name, "state": state})
    if queues["slack"]["dead_letters"] or queues["slack"]["unresolved"]:
        reasons.append({"code": "slack_needs_review", "subject": "slack",
                        "count": int(queues["slack"]["dead_letters"] or 0) +
                                 int(queues["slack"]["unresolved"] or 0)})
    if queues["notifications"].get("dead", 0) or notification_stalled:
        reasons.append({"code": "notifications_failing", "subject": "notifications",
                        "count": int(queues["notifications"].get("dead", 0) or 0)})
    return {
        "ok": not degraded, "status": "degraded" if degraded else "ok", "at": now,
        "workers": workers, "services": services, "credentials": credentials,
        "queues": queues, "heartbeats": beats, "issues": issues, "reasons": reasons,
    }


def enqueue_health_alerts(store: OpsStore, report: dict, *, backlog_warning: int = 25,
                          credential_warning_s: float = 3600, now: float | None = None) -> list[str]:
    """Turn health facts into low-frequency, deduplicated durable notifications."""
    now = float(time.time() if now is None else now)
    queued = []
    def add(*args, **kwargs):
        kwargs.setdefault("cooldown_s", HEALTH_ALERT_COOLDOWN_S)
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
    # Notification delivery failures stay in the local doctor/control-center view.  Sending an
    # alert about a broken notification transport through that same transport is self-referential.
    for cred in report.get("credentials") or []:
        if cred.get("needed") is False:
            continue          # listed for the operations view; nothing configured reads it
        if cred.get("self_refreshing") and cred.get("state") != "missing":
            continue          # its owner (Claude Code) refreshes it on use; only absence matters
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
                dedupe_key="credential:" + cred["name"],
                now=now)
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
