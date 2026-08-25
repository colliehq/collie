"""Local personal state for notes and calendar events.

This is deliberately independent from any account, sync, or cloud service.  The SQLite database is
the canonical local record and uses the same note/event shapes as the broader Personal AI work so
future sync can be added without migrating the product surface again.
"""
from __future__ import annotations

import datetime as _dt
import os
import secrets
import sqlite3
import threading
import time

__all__ = [
    "PersonalState", "default_path", "state_dir", "new_id", "parse_time", "format_time",
    "EVENT_KINDS",
]

EVENT_KINDS = ("meeting", "interview", "deadline", "call", "block", "reminder", "other")


def state_dir() -> str:
    return os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")


def default_path() -> str:
    # Do not share another experimental personal.db implicitly. HQ owns an independent local
    # database; a future importer or sync layer must be an explicit user action.
    return os.environ.get("COLLIE_PERSONAL_DB") or os.path.join(state_dir(), "collie-personal.db")


def new_id(kind: str) -> str:
    prefix = {"note": "nte", "event": "evt"}.get(kind, kind[:3])
    return "%s_%s" % (prefix, secrets.token_hex(5))


def _now() -> int:
    return int(time.time())


def _clip(text: str, size: int = 80) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= size else value[: size - 1] + "…"


def parse_time(value, *, end_of_day: bool = False) -> int:
    """Parse an epoch value or a local/offset ISO-8601 date-time into epoch seconds.

    Naive date-times are interpreted in this computer's local timezone.  Date-only values become
    local midnight (or the last second of that day when ``end_of_day`` is requested).
    """
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError("date/time required")
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value).strip()
    if not raw:
        raise ValueError("date/time required")
    try:
        return int(raw)
    except ValueError:
        pass
    normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    if len(normalized) == 10:
        try:
            parsed = _dt.datetime.strptime(normalized, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("use YYYY-MM-DD or ISO-8601 date/time") from exc
        if end_of_day:
            parsed = parsed.replace(hour=23, minute=59, second=59)
    else:
        try:
            parsed = _dt.datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("use YYYY-MM-DD or ISO-8601 date/time") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return int(parsed.timestamp())


def format_time(value, *, all_day: bool = False) -> str:
    if value in (None, ""):
        return ""
    dt = _dt.datetime.fromtimestamp(int(value)).astimezone()
    return dt.strftime("%Y-%m-%d" if all_day else "%Y-%m-%d %H:%M %Z")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS events(
    id TEXT PRIMARY KEY, title TEXT NOT NULL, start_at INTEGER NOT NULL, end_at INTEGER,
    all_day INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'meeting',
    location TEXT NOT NULL DEFAULT '', project_id TEXT NOT NULL DEFAULT '',
    goal_id TEXT NOT NULL DEFAULT '', external_ref TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS events_start_at ON events(start_at);
CREATE TABLE IF NOT EXISTS notes(
    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT '', goal_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'user', pinned INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS notes_updated_at ON notes(pinned DESC, updated_at DESC);
"""


class PersonalState:
    """Thread-safe local store for the owner's notes and calendar."""

    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _row(self, sql: str, args: tuple = ()) -> dict | None:
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # ------------------------------------------------------------------ notes
    def add_note(self, body: str, *, title: str = "", project_id: str = "", goal_id: str = "",
                 source: str = "user", pinned: bool = False, note_id: str = "") -> dict:
        body = str(body or "").strip()
        if not body:
            raise ValueError("note text required")
        now = _now()
        nid = str(note_id or "").strip() or new_id("note")
        title = str(title or "").strip() or _clip(body.splitlines()[0]) or "Note"
        self._exec(
            "INSERT INTO notes(id,title,body,project_id,goal_id,source,pinned,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (nid, title, body, str(project_id or ""), str(goal_id or ""), str(source or "user"),
             1 if pinned else 0, now, now))
        return self.note(nid)

    def note(self, note_id: str) -> dict | None:
        return self._row("SELECT * FROM notes WHERE id=?", (str(note_id or ""),))

    def notes(self, *, project_id: str = "", goal_id: str = "", query: str = "",
              limit: int = 100) -> list[dict]:
        sql, args = "SELECT * FROM notes WHERE 1=1", []
        if project_id:
            sql += " AND project_id=?"; args.append(str(project_id))
        if goal_id:
            sql += " AND goal_id=?"; args.append(str(goal_id))
        if query:
            q = "%" + str(query).casefold() + "%"
            sql += " AND (lower(title) LIKE ? OR lower(body) LIKE ?)"; args.extend((q, q))
        sql += " ORDER BY pinned DESC, updated_at DESC LIMIT ?"
        args.append(max(1, min(500, int(limit))))
        return self._rows(sql, tuple(args))

    def find_note(self, text: str) -> dict | None:
        needle = str(text or "").casefold().strip()
        if not needle:
            return None
        exact = next((row for row in self.notes(limit=500)
                      if str(row.get("title") or "").casefold() == needle), None)
        if exact:
            return exact
        candidates = [row for row in self.notes(limit=500)
                      if needle in str(row.get("title") or "").casefold()
                      or str(row.get("title") or "").casefold() in needle]
        return min(candidates, key=lambda row: len(str(row.get("title") or ""))) if candidates else None

    def append_note(self, note_id: str, text: str, *, source: str = "user") -> dict | None:
        note = self.note(note_id)
        text = str(text or "").strip()
        if not note:
            return None
        if not text:
            raise ValueError("note text required")
        body = (str(note["body"]).rstrip() + "\n\n" + text).strip()
        self._exec("UPDATE notes SET body=?, source=?, updated_at=? WHERE id=?",
                   (body, str(source or "user"), _now(), note_id))
        return self.note(note_id)

    def update_note(self, note_id: str, **fields) -> dict | None:
        current = self.note(note_id)
        if not current:
            return None
        allowed = {"title", "body", "project_id", "goal_id", "source", "pinned"}
        sets, args = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError("cannot update note.%s" % key)
            if key == "body":
                value = str(value or "").strip()
                if not value:
                    raise ValueError("note text required")
            elif key == "title":
                value = str(value or "").strip()
                if not value:
                    body = str(fields.get("body") or current["body"])
                    value = _clip(body.splitlines()[0]) or "Note"
            elif key == "pinned":
                value = 1 if bool(value) else 0
            else:
                value = str(value or "")
            sets.append("%s=?" % key); args.append(value)
        if not sets:
            return current
        sets.append("updated_at=?"); args.extend((_now(), note_id))
        self._exec("UPDATE notes SET %s WHERE id=?" % ", ".join(sets), tuple(args))
        return self.note(note_id)

    def delete_note(self, note_id: str) -> dict | None:
        note = self.note(note_id)
        if note:
            self._exec("DELETE FROM notes WHERE id=?", (note_id,))
        return note

    # --------------------------------------------------------------- calendar
    def add_event(self, title: str, start_at, *, end_at=None, all_day: bool = False,
                  kind: str = "meeting", location: str = "", project_id: str = "",
                  goal_id: str = "", external_ref: str = "", notes: str = "",
                  event_id: str = "") -> dict:
        title = str(title or "").strip()
        if not title:
            raise ValueError("event title required")
        kind = str(kind or "meeting").strip().lower()
        if kind not in EVENT_KINDS:
            raise ValueError("unknown event kind: %s" % kind)
        start = parse_time(start_at)
        end = parse_time(end_at, end_of_day=bool(all_day)) if end_at not in (None, "") else None
        if end is not None and end < start:
            raise ValueError("event end_at must not be before start_at")
        now, eid = _now(), str(event_id or "").strip() or new_id("event")
        self._exec(
            "INSERT INTO events(id,title,start_at,end_at,all_day,kind,location,project_id,goal_id,"
            "external_ref,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, title, start, end, 1 if all_day else 0, kind, str(location or ""),
             str(project_id or ""), str(goal_id or ""), str(external_ref or ""),
             str(notes or ""), now, now))
        return self.event(eid)

    def event(self, event_id: str) -> dict | None:
        return self._row("SELECT * FROM events WHERE id=?", (str(event_id or ""),))

    def events(self, *, since=None, until=None, query: str = "", limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if since not in (None, ""):
            sql += " AND COALESCE(end_at,start_at)>=?"; args.append(parse_time(since))
        if until not in (None, ""):
            sql += " AND start_at<=?"; args.append(parse_time(until, end_of_day=True))
        if query:
            q = "%" + str(query).casefold() + "%"
            sql += " AND (lower(title) LIKE ? OR lower(location) LIKE ? OR lower(notes) LIKE ?)"
            args.extend((q, q, q))
        sql += " ORDER BY start_at LIMIT ?"; args.append(max(1, min(500, int(limit))))
        return self._rows(sql, tuple(args))

    def upcoming(self, *, now: int | None = None, days: int = 14, limit: int = 50) -> list[dict]:
        start = int(now if now is not None else _now())
        return self.events(since=start - 3600, until=start + max(1, int(days)) * 86400,
                           limit=limit)

    def update_event(self, event_id: str, **fields) -> dict | None:
        current = self.event(event_id)
        if not current:
            return None
        allowed = {"title", "start_at", "end_at", "all_day", "kind", "location", "project_id",
                   "goal_id", "external_ref", "notes"}
        normalized = {}
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError("cannot update event.%s" % key)
            if key == "title":
                value = str(value or "").strip()
                if not value:
                    raise ValueError("event title required")
            elif key == "start_at":
                value = parse_time(value)
            elif key == "end_at":
                value = parse_time(value, end_of_day=bool(fields.get("all_day", current["all_day"]))) \
                    if value not in (None, "") else None
            elif key == "all_day":
                value = 1 if bool(value) else 0
            elif key == "kind":
                value = str(value or "meeting").strip().lower()
                if value not in EVENT_KINDS:
                    raise ValueError("unknown event kind: %s" % value)
            else:
                value = str(value or "")
            normalized[key] = value
        proposed = dict(current); proposed.update(normalized)
        if proposed.get("end_at") is not None and int(proposed["end_at"]) < int(proposed["start_at"]):
            raise ValueError("event end_at must not be before start_at")
        if not normalized:
            return current
        sets = ["%s=?" % key for key in normalized] + ["updated_at=?"]
        args = list(normalized.values()) + [_now(), event_id]
        self._exec("UPDATE events SET %s WHERE id=?" % ", ".join(sets), tuple(args))
        return self.event(event_id)

    def delete_event(self, event_id: str) -> dict | None:
        event = self.event(event_id)
        if event:
            self._exec("DELETE FROM events WHERE id=?", (event_id,))
        return event
