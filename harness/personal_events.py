"""Local personal-intelligence index and evidence-backed reminders.

Raw browser history is never stored here. The browser extension reduces it to an
origin, a day/time bucket, and small counters before sending it over loopback; this
module validates and aggregates that summary again. Typed life events are separate:
visiting a shop is not evidence that an order exists.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import math
import os
import re
import sqlite3
import time
import urllib.parse
import uuid


RAW_DATA_CLASS = "device_only"
DERIVED_DATA_CLASS = "sealed"
HISTORY_RETENTION_DAYS = 14
MAX_HISTORY_ITEMS = 2_000
MAX_ORIGINS_PER_DAY = 40
PERSONAL_SOURCE_KINDS = frozenset((
    "browser_history", "email", "calendar", "orders", "manual"))
PERSONAL_EVENT_TYPES = frozenset((
    "order_shipment", "reservation", "bill_due", "appointment",
    "subscription_renewal", "follow_up"))
TERMINAL_STATUSES = frozenset(("done", "delivered", "cancelled", "paid"))
PERSONAL_EVENT_STATUSES = TERMINAL_STATUSES | frozenset((
    "open", "scheduled", "confirmed", "ordered", "shipped", "out_for_delivery",
    "delayed", "due"))
_SENSITIVE_HOST_MARKERS = frozenset((
    "1password", "bitwarden", "dashlane", "keepass", "lastpass", "password",
    "login", "signin", "identity", "auth", "bank", "broker", "invest",
    "paypal", "health", "medical", "clinic", "patient", "pharmacy", "therapy",
    "tax", "irs"))
_DETAIL_KEYS = frozenset((
    "carrier", "merchant", "location_label", "calendar_label", "currency",
    "amount_bucket", "tracking_state", "timezone", "notes_length"))


def _path(root=None):
    root = os.path.abspath(root or os.environ.get("COLLIE_STATE_DIR")
                           or os.path.expanduser("~/.collie"))
    return os.path.join(root, "personal-intelligence.db")


def _text(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _finite(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _origin(value):
    try:
        parsed = urllib.parse.urlsplit(str(value or ""))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ""
        port = ":%d" % parsed.port if parsed.port else ""
        return "%s://%s%s" % (parsed.scheme, parsed.hostname.lower(), port)
    except (TypeError, ValueError):
        return ""


def _sensitive_origin(origin):
    try:
        host = urllib.parse.urlsplit(origin).hostname or ""
        try:
            address = ipaddress.ip_address(host)
            if address.is_private or address.is_loopback or address.is_link_local:
                return True
        except ValueError:
            pass
        compact = host.lower().replace("-", "").replace("_", "")
        return any(marker in compact for marker in _SENSITIVE_HOST_MARKERS)
    except (TypeError, ValueError):
        return True


def _digest(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


class PersonalEventStore:
    def __init__(self, path=None):
        self.path = os.path.abspath(path or _path())
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self._schema()

    def _schema(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS personal_sources(
          source_id TEXT PRIMARY KEY, kind TEXT NOT NULL, enabled INTEGER NOT NULL,
          permission_state TEXT NOT NULL, scopes_json TEXT NOT NULL,
          consent_version TEXT NOT NULL, consent_at INTEGER NOT NULL,
          last_ingested_at INTEGER NOT NULL, last_error TEXT NOT NULL,
          updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS browser_history_summaries(
          day TEXT NOT NULL, origin TEXT NOT NULL, visit_count INTEGER NOT NULL,
          typed_count INTEGER NOT NULL, active_hours_json TEXT NOT NULL,
          last_visit_at INTEGER NOT NULL,
          data_class TEXT NOT NULL CHECK(data_class='device_only'),
          PRIMARY KEY(day,origin));
        CREATE INDEX IF NOT EXISTS browser_history_recent_idx
          ON browser_history_summaries(last_visit_at);
        CREATE TABLE IF NOT EXISTS personal_events(
          event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, title TEXT NOT NULL,
          status TEXT NOT NULL, starts_at INTEGER, due_at INTEGER, expected_at INTEGER,
          source_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_ref_digest TEXT NOT NULL,
          evidence_digest TEXT NOT NULL, confidence REAL NOT NULL,
          details_json TEXT NOT NULL, data_class TEXT NOT NULL CHECK(data_class='sealed'),
          sync_allowed INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS personal_events_due_idx
          ON personal_events(status,due_at,expected_at,starts_at);
        CREATE TABLE IF NOT EXISTS personal_notification_receipts(
          notification_key TEXT PRIMARY KEY,event_id TEXT NOT NULL,state TEXT NOT NULL,
          target_at INTEGER NOT NULL,attempts INTEGER NOT NULL,last_attempt_at INTEGER NOT NULL,
          ok INTEGER NOT NULL,backend TEXT NOT NULL,error TEXT NOT NULL);
        """)
        event_columns = {row[1] for row in self.db.execute(
            "PRAGMA table_info(personal_events)").fetchall()}
        if "source_id" not in event_columns:
            self.db.execute(
                "ALTER TABLE personal_events ADD COLUMN source_id TEXT NOT NULL DEFAULT ''")
            self.db.execute(
                "UPDATE personal_events SET source_id=source_kind WHERE source_id='' ")
        self.db.execute("""INSERT OR IGNORE INTO personal_sources(
            source_id,kind,enabled,permission_state,scopes_json,consent_version,
            consent_at,last_ingested_at,last_error,updated_at)
            VALUES('browser_history','browser_history',0,'not_granted','[]','',0,0,'',?)""",
                        (int(time.time()),))
        self.db.commit()

    def configure_source(self, source_id, *, kind=None, enabled=None,
                         permission_state=None, scopes=None, consent_version="",
                         confirmed=False, purge=False):
        source_id = _text(source_id, 80).lower()
        current = self.db.execute(
            "SELECT * FROM personal_sources WHERE source_id=?", (source_id,)).fetchone()
        source_kind = _text(kind or (current["kind"] if current else source_id), 80).lower()
        if (not re.fullmatch(r"[a-z0-9_.-]{1,80}", source_id) or
                source_kind not in PERSONAL_SOURCE_KINDS):
            raise ValueError("unsupported personal source")
        turn_on = bool(enabled) if enabled is not None else bool(current and current["enabled"])
        if turn_on and not (current and current["enabled"]) and confirmed is not True:
            raise ValueError("confirmed consent is required before enabling a personal source")
        permission = _text(permission_state or
                           (current["permission_state"] if current else "not_granted"), 40)
        if permission not in ("not_granted", "granted", "revoked", "error"):
            raise ValueError("unsupported source permission state")
        if turn_on and permission != "granted":
            raise ValueError("the source permission has not been granted")
        clean_scopes = sorted({_text(item, 80).lower() for item in (scopes or [])
                               if re.fullmatch(r"[a-z0-9_.:-]{1,80}",
                                               _text(item, 80).lower())})
        now = int(time.time())
        consent_at = (now if turn_on and not (current and current["enabled"])
                      else int(current["consent_at"] if current else 0))
        self.db.execute("""INSERT INTO personal_sources(
            source_id,kind,enabled,permission_state,scopes_json,consent_version,
            consent_at,last_ingested_at,last_error,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
            kind=excluded.kind,enabled=excluded.enabled,
            permission_state=excluded.permission_state,scopes_json=excluded.scopes_json,
            consent_version=excluded.consent_version,consent_at=excluded.consent_at,
            updated_at=excluded.updated_at""",
            (source_id, source_kind, int(turn_on), permission,
             json.dumps(clean_scopes), _text(consent_version, 100), consent_at,
             int(current["last_ingested_at"] if current else 0),
             _text(current["last_error"] if current else "", 300), now))
        if purge and source_id == "browser_history":
            self.db.execute("DELETE FROM browser_history_summaries")
        self.db.commit()
        return self.get_source(source_id)

    def get_source(self, source_id):
        row = self.db.execute(
            "SELECT * FROM personal_sources WHERE source_id=?", (source_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        try:
            value["scopes"] = json.loads(value.pop("scopes_json"))
        except (TypeError, ValueError):
            value["scopes"] = []
            value.pop("scopes_json", None)
        return value

    def list_sources(self):
        return [self.get_source(row[0]) for row in self.db.execute(
            "SELECT source_id FROM personal_sources ORDER BY source_id")]

    def ingest_browser_history(self, items, *, now=None):
        source = self.get_source("browser_history")
        if not source or not source["enabled"] or source["permission_state"] != "granted":
            raise ValueError("browser history is not connected")
        if not isinstance(items, list):
            raise ValueError("history summaries must be a list")
        if len(items) > MAX_HISTORY_ITEMS:
            raise ValueError("history batch is too large")
        wall = int(time.time() if now is None else now)
        cutoff = wall - HISTORY_RETENTION_DAYS * 86400
        grouped = {}
        rejected = 0
        for raw in items:
            if not isinstance(raw, dict):
                rejected += 1
                continue
            origin = _origin(raw.get("origin") or raw.get("url"))
            stamp = int(_finite(raw.get("last_visit_at") or raw.get("lastVisitTime"), 0))
            if stamp > 10_000_000_000:
                stamp //= 1000
            if not origin or _sensitive_origin(origin) or stamp < cutoff or stamp > wall + 300:
                rejected += 1
                continue
            local = dt.datetime.fromtimestamp(stamp)
            day, hour = local.date().isoformat(), int(local.hour)
            key = (day, origin)
            row = grouped.setdefault(key, {"visits": 0, "typed": 0, "hours": set(), "last": 0})
            row["visits"] += max(1, min(10_000, int(_finite(raw.get("visit_count"), 1))))
            row["typed"] += max(0, min(10_000, int(_finite(raw.get("typed_count"), 0))))
            row["hours"].add(hour)
            row["last"] = max(row["last"], stamp)
        # Bound cardinality per day before disk: useful top origins survive; a long
        # tail of one-off pages disappears without ever becoming durable state.
        selected = []
        for day in sorted({key[0] for key in grouped}):
            rows = [(origin, grouped[(day, origin)]) for d, origin in grouped if d == day]
            rows.sort(key=lambda item: (item[1]["visits"], item[1]["last"]), reverse=True)
            selected.extend((day, origin, value) for origin, value in rows[:MAX_ORIGINS_PER_DAY])
        with self.db:
            for day, origin, value in selected:
                self.db.execute("""INSERT INTO browser_history_summaries(
                    day,origin,visit_count,typed_count,active_hours_json,last_visit_at,data_class)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(day,origin) DO UPDATE SET
                    visit_count=MAX(browser_history_summaries.visit_count,excluded.visit_count),
                    typed_count=MAX(browser_history_summaries.typed_count,excluded.typed_count),
                    active_hours_json=excluded.active_hours_json,
                    last_visit_at=MAX(browser_history_summaries.last_visit_at,excluded.last_visit_at)""",
                    (day, origin, min(10_000, value["visits"]), min(10_000, value["typed"]),
                     json.dumps(sorted(value["hours"])), value["last"], RAW_DATA_CLASS))
            self.db.execute(
                "DELETE FROM browser_history_summaries WHERE last_visit_at<?", (cutoff,))
            self.db.execute("""UPDATE personal_sources SET last_ingested_at=?,last_error='',
                updated_at=? WHERE source_id='browser_history'""", (wall, wall))
        return {"accepted_origins": len(selected), "discarded_items": rejected,
                "raw_stored": False, "raw_uploaded": False,
                "retention_days": HISTORY_RETENTION_DAYS}

    def browsing_patterns(self, limit=20):
        rows = self.db.execute("""SELECT origin,SUM(visit_count) AS visits,
            MAX(last_visit_at) AS last_visit_at,COUNT(*) AS active_days
            FROM browser_history_summaries GROUP BY origin
            ORDER BY visits DESC,last_visit_at DESC LIMIT ?""", (max(1, min(int(limit), 100)),))
        return [dict(row) for row in rows]

    def upsert_event(self, value, *, confirmed=False):
        if not isinstance(value, dict):
            raise ValueError("personal event must be an object")
        event_type = _text(value.get("event_type"), 80).lower()
        source_kind = _text(value.get("source_kind"), 80).lower()
        if event_type not in PERSONAL_EVENT_TYPES or source_kind not in PERSONAL_SOURCE_KINDS:
            raise ValueError("unsupported personal event or source type")
        if source_kind == "browser_history":
            raise ValueError("browser history alone is not evidence of a life event")
        source_id = _text(value.get("source_id") or source_kind, 80).lower()
        if source_kind != "manual":
            source = self.get_source(source_id)
            if (not source or not source["enabled"] or source["kind"] != source_kind or
                    source["permission_state"] != "granted"):
                raise ValueError("the evidence source is not connected")
        evidence = _text(value.get("evidence_digest"), 128).lower()
        source_ref = _text(value.get("source_ref"), 500)
        if source_kind != "manual" and (not evidence or not source_ref):
            raise ValueError("non-manual personal events require source evidence")
        if source_kind != "manual" and not re.fullmatch(r"[a-f0-9]{32,128}", evidence):
            raise ValueError("evidence_digest must be a hexadecimal digest")
        confidence = max(0.0, min(1.0, _finite(value.get("confidence"), 0.0)))
        if source_kind != "manual" and confidence < 0.5:
            raise ValueError("evidence confidence is too low for a reminder")
        if source_kind == "manual" and confirmed is not True:
            raise ValueError("manual personal events require confirmation")
        now = int(time.time())
        clean_times = []
        for key in ("starts_at", "due_at", "expected_at"):
            raw = value.get(key)
            clean_times.append(int(_finite(raw)) if raw is not None else None)
        details = {key: value["details"][key] for key in _DETAIL_KEYS
                   if isinstance(value.get("details"), dict) and key in value["details"]}
        details = {key: (_text(item, 120) if not isinstance(item, (bool, int, float)) else item)
                   for key, item in details.items()}
        event_id = _text(value.get("event_id"), 100)
        if event_id and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", event_id):
            raise ValueError("event_id must be an opaque identifier")
        event_id = event_id or "personal_" + uuid.uuid4().hex
        status = _text(value.get("status") or "open", 40).lower()
        if status not in PERSONAL_EVENT_STATUSES:
            raise ValueError("unsupported personal event status")
        title = _text(value.get("title") or event_type.replace("_", " "), 240)
        sync_allowed = bool(value.get("sync_allowed") and confirmed is True)
        source_ref_digest = _digest(source_ref)
        existing = self.db.execute(
            """SELECT event_type,source_id,source_kind,source_ref_digest FROM personal_events
               WHERE event_id=?""", (event_id,)).fetchone()
        if existing and (existing["event_type"] != event_type or
                         existing["source_id"] != source_id or
                         existing["source_kind"] != source_kind or
                         existing["source_ref_digest"] != source_ref_digest):
            raise ValueError("personal event identity cannot be rewritten")
        with self.db:
            self.db.execute("""INSERT INTO personal_events(
                event_id,event_type,title,status,starts_at,due_at,expected_at,source_id,source_kind,
                source_ref_digest,evidence_digest,confidence,details_json,data_class,
                sync_allowed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET title=excluded.title,status=excluded.status,
                starts_at=excluded.starts_at,due_at=excluded.due_at,
                expected_at=excluded.expected_at,evidence_digest=excluded.evidence_digest,
                confidence=excluded.confidence,details_json=excluded.details_json,
                sync_allowed=excluded.sync_allowed,updated_at=excluded.updated_at""",
                (event_id, event_type, title, status, clean_times[0], clean_times[1],
                 clean_times[2], source_id, source_kind, source_ref_digest, evidence, confidence,
                 json.dumps(details, ensure_ascii=False, sort_keys=True), DERIVED_DATA_CLASS,
                 int(sync_allowed), now, now))
        return self.get_event(event_id)

    def get_event(self, event_id):
        row = self.db.execute(
            "SELECT * FROM personal_events WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value["sync_allowed"] = bool(value["sync_allowed"])
        value["details"] = json.loads(value.pop("details_json") or "{}")
        return value

    def list_events(self, limit=100):
        return [self.get_event(row[0]) for row in self.db.execute(
            "SELECT event_id FROM personal_events ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),))]

    def reminders(self, *, now=None, horizon_days=14):
        wall = int(time.time() if now is None else now)
        horizon = wall + max(1, min(int(horizon_days), 90)) * 86400
        out = []
        for event in self.list_events(limit=500):
            if event["status"] in TERMINAL_STATUSES:
                continue
            target = event.get("expected_at") or event.get("due_at") or event.get("starts_at")
            if not target or target > horizon:
                continue
            if event["event_type"] == "order_shipment" and target < wall:
                state = "possibly_delayed"
            elif target < wall:
                state = "overdue"
            elif target - wall <= 48 * 3600:
                state = "soon"
            else:
                state = "upcoming"
            out.append({"event_id": event["event_id"], "event_type": event["event_type"],
                        "title": event["title"], "state": state, "at": target,
                        "confidence": event["confidence"], "status": event["status"],
                        "authority_scope": "notify_only"})
        out.sort(key=lambda item: item["at"])
        return out

    def claim_notifications(self, *, now=None, limit=10):
        """Lease reminder states for native delivery, retrying failures without spam."""
        wall = int(time.time() if now is None else now)
        claimed = []
        with self.db:
            for reminder in self.reminders(now=wall)[:max(1, min(int(limit), 50))]:
                # Upcoming is intentionally a dashboard state. Native delivery starts
                # at 48 hours, or immediately when something appears late.
                if reminder["state"] == "upcoming":
                    continue
                key = _digest("%s|%s|%s|%s" % (
                    reminder["event_id"], reminder["state"], reminder["at"],
                    reminder["status"]))
                receipt = self.db.execute(
                    "SELECT * FROM personal_notification_receipts WHERE notification_key=?",
                    (key,)).fetchone()
                if receipt and (receipt["ok"] or receipt["attempts"] >= 5 or
                                wall - receipt["last_attempt_at"] < 3600):
                    continue
                attempts = int(receipt["attempts"] + 1 if receipt else 1)
                self.db.execute("""INSERT INTO personal_notification_receipts(
                    notification_key,event_id,state,target_at,attempts,last_attempt_at,
                    ok,backend,error) VALUES(?,?,?,?,?,?,0,'','pending')
                    ON CONFLICT(notification_key) DO UPDATE SET attempts=excluded.attempts,
                    last_attempt_at=excluded.last_attempt_at,error='pending'""",
                    (key, reminder["event_id"], reminder["state"], reminder["at"],
                     attempts, wall))
                claimed.append(dict(reminder, notification_key=key))
            self.db.execute("""DELETE FROM personal_notification_receipts WHERE notification_key IN(
                SELECT notification_key FROM personal_notification_receipts
                ORDER BY last_attempt_at DESC LIMIT -1 OFFSET 500)""")
        return claimed

    def complete_notification(self, notification_key, result):
        result = result if isinstance(result, dict) else {}
        with self.db:
            self.db.execute("""UPDATE personal_notification_receipts SET ok=?,backend=?,error=?
                WHERE notification_key=?""",
                (int(bool(result.get("ok"))), _text(result.get("backend"), 80),
                 _text(result.get("error"), 300), str(notification_key)))

    def notification_receipts(self, limit=20):
        return [dict(row) for row in self.db.execute(
            """SELECT * FROM personal_notification_receipts
               ORDER BY last_attempt_at DESC LIMIT ?""", (max(1, min(int(limit), 200)),))]

    def snapshot(self):
        return {"sources": self.list_sources(), "browsing_patterns": self.browsing_patterns(),
                "events": self.list_events(), "reminders": self.reminders(),
                "notification_receipts": self.notification_receipts(),
                "guarantees": {"raw_history_stored": False, "raw_history_uploaded": False,
                               "browser_history_can_create_order": False,
                               "external_action_authority": "none"}}

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
