"""Privacy-preserving, local-first procedural memory.

Raw observations contain structured action metadata only and are always device-only.
Repeated sequences may become sealed suggestions, but no suggestion executes or gains
authority until a person reviews it. Acceptance still grants zero authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import uuid

from .embeddings import HashEmbedding, cosine

RAW_DATA_CLASS = "device_only"
DERIVED_DATA_CLASS = "sealed"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_EXCLUDED_APPS = (
    "1password", "bitwarden", "dashlane", "keepass", "keychain",
    "password manager", "private browsing", "incognito",
)
_PRIVATE_MARKERS = ("incognito", "private browsing", "password", "secret", "credential")
_SECRET_KEYS = ("token", "secret", "password", "passwd", "api_key", "apikey",
                "access_token", "refresh_token", "authorization", "cookie")
_CONTENT_KEYS = ("body", "content", "html", "message", "text", "prompt", "response",
                 "screenshot", "pixels", "clipboard", "typed")
_SPACE = re.compile(r"\s+")


def _path(path=None):
    if path:
        return os.path.abspath(path)
    root = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
    return os.path.join(root, "procedural-memory.db")


def _bounded(value, limit=160):
    return _SPACE.sub(" ", str(value or "")).strip()[:limit]


def _digest(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _tool_app(tool):
    name = str(tool or "").lower()
    for prefix, app in (("browser_", "browser"), ("desktop_", "desktop"),
                        ("mcp", "mcp"), ("bash", "terminal"), ("shell", "terminal"),
                        ("powershell", "terminal"), ("terminal", "terminal")):
        if name.startswith(prefix):
            return app
    if name.startswith(("read", "write", "edit", "apply_patch", "glob", "grep")):
        return "filesystem"
    return _bounded(name or "collie", 80)


def _object_kind(tool, args):
    name = str(tool or "").lower()
    if name.startswith("browser_"):
        return "web"
    if name.startswith("desktop_"):
        return "window"
    if any(k in args for k in ("path", "file", "filename", "cwd")):
        return "file"
    if any(k in args for k in ("url", "uri", "host")):
        return "web"
    if any(k in args for k in ("cmd", "command")):
        return "command"
    return "tool"


def safe_object_ref(value, kind="", *, project=""):
    """Keep a useful identity without retaining content, query strings, or commands."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme in ("http", "https") and parsed.hostname:
            port = ":" + str(parsed.port) if parsed.port else ""
            return "%s://%s%s" % (parsed.scheme, parsed.hostname.lower(), port)
    except (TypeError, ValueError):
        pass
    if kind == "command":
        first = re.split(r"\s+", raw, maxsplit=1)[0].strip("'\"")
        return os.path.basename(first)[:80]
    if kind == "file" or "/" in raw or "\\" in raw:
        try:
            full = os.path.abspath(os.path.expanduser(raw))
            if project:
                relative = os.path.relpath(full, os.path.abspath(project))
                if relative != ".." and not relative.startswith(".." + os.sep):
                    return _bounded(relative.replace("\\", "/"), 160)
            return _bounded(os.path.basename(full), 120)
        except (OSError, ValueError):
            return _bounded(os.path.basename(raw), 120)
    return _bounded(raw, 100)


def _safe_metadata(value):
    """Only operational labels survive; content-bearing values become lengths."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, item in list(value.items())[:24]:
        name = _bounded(key, 60).lower()
        if not name:
            continue
        if any(secret in name for secret in _SECRET_KEYS):
            out[name] = "[redacted]"
        elif any(name == body or name.endswith("_" + body) for body in _CONTENT_KEYS):
            out[name] = "[%d chars]" % len(str(item or ""))
        elif isinstance(item, (bool, int, float)) or item is None:
            out[name] = item
        elif name in ("risk", "effect", "stage", "status", "rule", "provider", "kind"):
            out[name] = _bounded(item, 100)
    return out


class ProcedureMemory:
    """Local observation journal plus explicitly reviewed, syncable derivatives."""

    def __init__(self, path=None, *, embedder=None):
        self.path = _path(path)
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.embedder = embedder or HashEmbedding()
        self._lock = threading.RLock()
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
        CREATE TABLE IF NOT EXISTS procedure_settings(
          key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS procedure_events(
          event_id TEXT PRIMARY KEY, observed_at REAL NOT NULL, session TEXT NOT NULL,
          project TEXT NOT NULL, app TEXT NOT NULL, action TEXT NOT NULL,
          object_kind TEXT NOT NULL, object_ref TEXT NOT NULL, result TEXT NOT NULL,
          data_class TEXT NOT NULL CHECK(data_class='device_only'), metadata_json TEXT NOT NULL,
          fingerprint TEXT NOT NULL, source TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS procedure_events_session_idx
          ON procedure_events(project,session,observed_at);
        CREATE INDEX IF NOT EXISTS procedure_events_time_idx ON procedure_events(observed_at);
        CREATE TABLE IF NOT EXISTS procedure_candidates(
          candidate_id TEXT PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL,
          summary TEXT NOT NULL, sequence_json TEXT NOT NULL, support INTEGER NOT NULL,
          confidence REAL NOT NULL, evidence_json TEXT NOT NULL, status TEXT NOT NULL,
          data_class TEXT NOT NULL CHECK(data_class='sealed'), vector_json TEXT NOT NULL,
          origin_device_id TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL, reviewed_at INTEGER, review_note TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS procedure_candidates_status_idx
          ON procedure_candidates(project,status,updated_at);
        CREATE TABLE IF NOT EXISTS learned_workflows(
          workflow_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, project TEXT NOT NULL,
          title TEXT NOT NULL, summary TEXT NOT NULL, sequence_json TEXT NOT NULL,
          digest TEXT NOT NULL, status TEXT NOT NULL, authority_scope TEXT NOT NULL,
          data_class TEXT NOT NULL CHECK(data_class='sealed'), origin_device_id TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        """)
        defaults = {"enabled": True, "paused": False,
                    "retention_days": DEFAULT_RETENTION_DAYS,
                    "excluded_apps": list(DEFAULT_EXCLUDED_APPS)}
        now = int(time.time())
        for key, value in defaults.items():
            self.db.execute(
                "INSERT OR IGNORE INTO procedure_settings(key,value,updated_at) VALUES(?,?,?)",
                (key, json.dumps(value), now))
        self.db.commit()

    def settings(self):
        out = {}
        for row in self.db.execute("SELECT key,value FROM procedure_settings"):
            try:
                out[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                out[row["key"]] = row["value"]
        return out

    def update_privacy(self, *, enabled=None, paused=None, retention_days=None,
                       exclude_app="", include_app=""):
        values = self.settings()
        for key, value in (("enabled", enabled), ("paused", paused)):
            if value is not None:
                if not isinstance(value, bool):
                    raise ValueError("%s must be boolean" % key)
                values[key] = value
        if retention_days is not None:
            days = int(retention_days)
            if days < 1 or days > 3650:
                raise ValueError("retention_days must be between 1 and 3650")
            values["retention_days"] = days
        excluded = {_bounded(x, 100).lower() for x in values.get("excluded_apps", [])
                    if str(x).strip()}
        if exclude_app:
            excluded.add(_bounded(exclude_app, 100).lower())
        if include_app:
            excluded.discard(_bounded(include_app, 100).lower())
        values["excluded_apps"] = sorted(excluded)
        now = int(time.time())
        with self._lock:
            for key in ("enabled", "paused", "retention_days", "excluded_apps"):
                self.db.execute("""INSERT INTO procedure_settings(key,value,updated_at)
                    VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,updated_at=excluded.updated_at""",
                    (key, json.dumps(values[key]), now))
            self.db.commit()
        return self.settings()

    def _excluded(self, app, ref, metadata):
        settings = self.settings()
        if not settings.get("enabled", True) or settings.get("paused", False):
            return True
        haystack = (str(app or "") + " " + str(ref or "")).lower()
        if any(marker in haystack for marker in _PRIVATE_MARKERS):
            return True
        if isinstance(metadata, dict) and any(metadata.get(key) is True for key in
                ("private", "incognito", "sensitive", "password_field")):
            return True
        return any(name and name in haystack for name in settings.get("excluded_apps", []))

    def observe(self, *, session="", project="", app="", action="", object_kind="tool",
                object_ref="", result="", metadata=None, source="collie", observed_at=None):
        app = _bounded(app, 80).lower()
        action = _bounded(action, 100).lower()
        kind = _bounded(object_kind or "tool", 40).lower()
        project = os.path.abspath(project) if project else ""
        ref = safe_object_ref(object_ref, kind, project=project)
        if not app or not action or self._excluded(app, ref, metadata):
            return None
        session = _bounded(session or "unscoped", 160)
        result = _bounded(result or "observed", 40).lower()
        at = float(time.time() if observed_at is None else observed_at)
        fingerprint = _digest("|".join((app, action, kind, ref, result)))
        with self._lock:
            prior = self.db.execute("""SELECT observed_at,fingerprint FROM procedure_events
                WHERE session=? ORDER BY observed_at DESC LIMIT 1""", (session,)).fetchone()
            if prior and prior["fingerprint"] == fingerprint and at - prior["observed_at"] <= 2:
                return None
            event_id = "evt_" + uuid.uuid4().hex
            self.db.execute("""INSERT INTO procedure_events(event_id,observed_at,session,project,
                app,action,object_kind,object_ref,result,data_class,metadata_json,fingerprint,source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, at, session, project, app, action, kind, ref, result,
                 RAW_DATA_CLASS, json.dumps(_safe_metadata(metadata), sort_keys=True),
                 fingerprint, _bounded(source, 80)))
            self.db.commit()
        self.prune()
        # Run bounded discovery twice per session, not on every action. The second
        # action catches short routines promptly; the fifth lets longer routines from
        # two completed sessions emerge without an operator pressing Discover.
        try:
            session_events = int(self.db.execute(
                "SELECT count(*) FROM procedure_events WHERE project=? AND session=?",
                (project, session)).fetchone()[0])
            distinct_sessions = int(self.db.execute(
                "SELECT count(DISTINCT session) FROM procedure_events WHERE project=?",
                (project,)).fetchone()[0])
            if distinct_sessions >= 2 and session_events in (2, 5):
                self.discover(project=project)
        except Exception:
            pass
        return event_id

    def observe_tool(self, tool, args=None, *, session="", project="", target="",
                     result="", metadata=None, source="gate"):
        args = args if isinstance(args, dict) else {}
        kind = _object_kind(tool, args)
        value = target
        if not value:
            for key in ("url", "uri", "path", "file", "filename", "cmd", "command", "host"):
                if args.get(key):
                    value = args[key]
                    break
        return self.observe(session=session, project=project, app=_tool_app(tool), action=tool,
                            object_kind=kind, object_ref=value, result=result,
                            metadata=metadata, source=source)

    def prune(self, *, now=None):
        days = int(self.settings().get("retention_days") or DEFAULT_RETENTION_DAYS)
        cutoff = float(time.time() if now is None else now) - days * 86400
        with self._lock:
            cur = self.db.execute("DELETE FROM procedure_events WHERE observed_at < ?", (cutoff,))
            self.db.commit()
            return max(0, int(cur.rowcount or 0))

    def purge_events(self, *, confirmed=False):
        if not confirmed:
            raise ValueError("confirmed=true is required to delete raw observations")
        with self._lock:
            cur = self.db.execute("DELETE FROM procedure_events")
            self.db.commit()
            return max(0, int(cur.rowcount or 0))

    def list_events(self, *, project=None, session=None, limit=200):
        sql, params, where = "SELECT * FROM procedure_events", [], []
        for key, value in (("project", project), ("session", session)):
            if value:
                where.append(key + "=?")
                params.append(value)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 2000)))
        out = []
        for row in self.db.execute(sql, params):
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json"))
            except (TypeError, ValueError):
                item["metadata"] = {}
                item.pop("metadata_json", None)
            out.append(item)
        return out

    @staticmethod
    def _step(row):
        return {"app": row["app"], "action": row["action"],
                "object_kind": row["object_kind"], "object_ref": row["object_ref"]}

    @staticmethod
    def _step_key(row):
        return "|".join((row["app"], row["action"], row["object_kind"], row["object_ref"]))

    def discover(self, *, project=None, min_support=2, min_steps=2, max_steps=5):
        """Mine sequences seen in distinct sessions; frequency never implies permission."""
        min_support, min_steps = max(2, int(min_support)), max(2, int(min_steps))
        max_steps = max(min_steps, min(8, int(max_steps)))
        sql, params = "SELECT * FROM procedure_events", []
        if project:
            sql += " WHERE project=?"
            params.append(project)
        sql += " ORDER BY project,session,observed_at"
        sessions = {}
        for row in self.db.execute(sql, params):
            bucket = sessions.setdefault((row["project"], row["session"]), [])
            if not bucket or bucket[-1]["fingerprint"] != row["fingerprint"]:
                bucket.append(row)
        patterns = {}
        for (project_name, session), rows in sessions.items():
            for length in range(min_steps, min(max_steps, len(rows)) + 1):
                for start in range(len(rows) - length + 1):
                    window = rows[start:start + length]
                    keys = tuple(self._step_key(row) for row in window)
                    item = patterns.setdefault((project_name, keys), {
                        "project": project_name, "steps": [self._step(row) for row in window],
                        "sessions": set(), "evidence": []})
                    item["sessions"].add(session)
                    if len(item["evidence"]) < 12:
                        item["evidence"].append({"session": session,
                            "first_event": window[0]["event_id"], "last_event": window[-1]["event_id"]})
        supported = [p for p in patterns.values() if len(p["sessions"]) >= min_support]
        supported.sort(key=lambda p: (-len(p["steps"]), -len(p["sessions"])))
        chosen = []
        for item in supported:
            sequence = [self._step_key(step) for step in item["steps"]]
            if any(item["sessions"] == prior["sessions"] and len(sequence) < len(prior["sequence"])
                   and any(prior["sequence"][i:i + len(sequence)] == sequence
                           for i in range(len(prior["sequence"]) - len(sequence) + 1))
                   for prior in chosen):
                continue
            item["sequence"] = sequence
            chosen.append(item)
        now, changed = int(time.time()), []
        with self._lock:
            for item in chosen:
                canonical = json.dumps(item["steps"], sort_keys=True, separators=(",", ":"))
                candidate_id = "routine_" + _digest(item["project"] + "\n" + canonical)[:24]
                support = len(item["sessions"])
                total = max(1, sum(1 for key in sessions if key[0] == item["project"]))
                confidence = min(0.99, support / total)
                verbs = [step["action"].replace("_", " ") for step in item["steps"]]
                title = " -> ".join(verbs[:3]) + ("..." if len(verbs) > 3 else "")
                summary = "%s (%d steps, repeated in %d sessions)" % (title, len(verbs), support)
                vector = self.embedder.embed(summary, kind="passage")
                prior = self.db.execute(
                    "SELECT status,created_at FROM procedure_candidates WHERE candidate_id=?",
                    (candidate_id,)).fetchone()
                status = prior["status"] if prior else "proposed"
                created = int(prior["created_at"]) if prior else now
                self.db.execute("""INSERT INTO procedure_candidates(candidate_id,project,title,
                    summary,sequence_json,support,confidence,evidence_json,status,data_class,
                    vector_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(candidate_id) DO UPDATE SET title=excluded.title,
                    summary=excluded.summary,support=excluded.support,
                    confidence=excluded.confidence,evidence_json=excluded.evidence_json,
                    vector_json=excluded.vector_json,updated_at=excluded.updated_at""",
                    (candidate_id, item["project"], title, summary, canonical, support, confidence,
                     json.dumps(item["evidence"], sort_keys=True), status, DERIVED_DATA_CLASS,
                     json.dumps(vector), created, now))
                changed.append(candidate_id)
            self.db.commit()
        return [self.get_candidate(candidate_id) for candidate_id in changed]

    def _candidate(self, row):
        value = dict(row)
        for source, target in (("sequence_json", "sequence"), ("evidence_json", "evidence")):
            try:
                value[target] = json.loads(value.pop(source))
            except (TypeError, ValueError):
                value[target] = []
                value.pop(source, None)
        value.pop("vector_json", None)
        return value

    def get_candidate(self, candidate_id):
        row = self.db.execute("SELECT * FROM procedure_candidates WHERE candidate_id=?",
                              (str(candidate_id),)).fetchone()
        if row is None:
            raise KeyError("workflow candidate not found")
        return self._candidate(row)

    def list_candidates(self, *, status=None, project=None, limit=200):
        sql, params, where = "SELECT * FROM procedure_candidates", [], []
        for key, value in (("status", status), ("project", project)):
            if value:
                where.append(key + "=?")
                params.append(value)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY support DESC,updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        return [self._candidate(row) for row in self.db.execute(sql, params)]

    def search_candidates(self, query, *, status=None, limit=20):
        needle = self.embedder.embed(str(query or ""), kind="query")
        scored = []
        for row in self.db.execute("SELECT * FROM procedure_candidates"):
            if status and row["status"] != status:
                continue
            try:
                vector = json.loads(row["vector_json"])
            except (TypeError, ValueError):
                vector = []
            scored.append((cosine(needle, vector), row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        out = []
        for score, row in scored[:max(1, min(int(limit), 100))]:
            value = self._candidate(row)
            value["similarity"] = round(float(score), 6)
            out.append(value)
        return out

    def review(self, candidate_id, action, *, confirmed=False, note=""):
        if not confirmed:
            raise ValueError("confirmed=true is required to review a learned workflow")
        action = str(action or "").strip().lower()
        if action not in ("accept", "dismiss"):
            raise ValueError("action must be accept or dismiss")
        candidate, now = self.get_candidate(candidate_id), int(time.time())
        status = "accepted" if action == "accept" else "dismissed"
        workflow_id = ""
        with self._lock:
            self.db.execute("""UPDATE procedure_candidates SET status=?,reviewed_at=?,
                review_note=?,updated_at=? WHERE candidate_id=?""",
                (status, now, _bounded(note, 500), now, candidate_id))
            if action == "accept":
                canonical = json.dumps(candidate["sequence"], sort_keys=True, separators=(",", ":"))
                workflow_id = "learned_" + _digest(candidate_id)[:24]
                digest = _digest(candidate["project"] + "\n" + canonical)
                self.db.execute("""INSERT INTO learned_workflows(workflow_id,candidate_id,project,
                    title,summary,sequence_json,digest,status,authority_scope,data_class,
                    created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(workflow_id) DO UPDATE SET title=excluded.title,
                    summary=excluded.summary,sequence_json=excluded.sequence_json,
                    digest=excluded.digest,status=excluded.status,authority_scope='none',
                    updated_at=excluded.updated_at""",
                    (workflow_id, candidate_id, candidate["project"], candidate["title"],
                     candidate["summary"], canonical, digest, "accepted", "none",
                     DERIVED_DATA_CLASS, now, now))
            self.db.commit()
        out = self.get_candidate(candidate_id)
        if workflow_id:
            out["workflow_id"] = workflow_id
        return out

    def list_workflows(self, *, project=None, status=None, limit=200):
        sql, params, where = "SELECT * FROM learned_workflows", [], []
        for key, value in (("project", project), ("status", status)):
            if value:
                where.append(key + "=?")
                params.append(value)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        out = []
        for row in self.db.execute(sql, params):
            value = dict(row)
            try:
                value["sequence"] = json.loads(value.pop("sequence_json"))
            except (TypeError, ValueError):
                value["sequence"] = []
                value.pop("sequence_json", None)
            out.append(value)
        return out

    def upsert_synced_candidate(self, content):
        """Import a decrypted same-user derivative; raw events have no import path."""
        candidate_id = _bounded(content.get("candidate_id"), 100)
        if not candidate_id:
            raise ValueError("candidate_id required")
        sequence = content.get("sequence") if isinstance(content.get("sequence"), list) else []
        summary, now = _bounded(content.get("summary"), 500), int(time.time())
        vector = self.embedder.embed(summary, kind="passage")
        with self._lock:
            self.db.execute("""INSERT INTO procedure_candidates(candidate_id,project,title,summary,
              sequence_json,support,confidence,evidence_json,status,data_class,vector_json,
              origin_device_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(candidate_id) DO UPDATE SET title=excluded.title,
              summary=excluded.summary,sequence_json=excluded.sequence_json,
              support=MAX(procedure_candidates.support,excluded.support),
              confidence=MAX(procedure_candidates.confidence,excluded.confidence),
              origin_device_id=excluded.origin_device_id,updated_at=excluded.updated_at""",
              (candidate_id, _bounded(content.get("project"), 500),
               _bounded(content.get("title"), 300), summary,
               json.dumps(sequence, sort_keys=True), int(content.get("support") or 2),
               float(content.get("confidence") or 0), "[]",
               str(content.get("status") or "proposed"), DERIVED_DATA_CLASS,
               json.dumps(vector), _bounded(content.get("origin_device_id"), 100),
               int(content.get("created_at") or now), now))
            self.db.commit()
        return candidate_id

    def upsert_synced_workflow(self, content):
        workflow_id = _bounded(content.get("workflow_id"), 100)
        if not workflow_id:
            raise ValueError("workflow_id required")
        sequence = content.get("sequence") if isinstance(content.get("sequence"), list) else []
        canonical = json.dumps(sequence, sort_keys=True, separators=(",", ":"))
        now = int(time.time())
        with self._lock:
            self.db.execute("""INSERT INTO learned_workflows(workflow_id,candidate_id,project,title,
              summary,sequence_json,digest,status,authority_scope,data_class,origin_device_id,
              created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(workflow_id) DO UPDATE SET title=excluded.title,
              summary=excluded.summary,sequence_json=excluded.sequence_json,
              digest=excluded.digest,status=excluded.status,authority_scope='none',
              origin_device_id=excluded.origin_device_id,updated_at=excluded.updated_at""",
              (workflow_id, _bounded(content.get("candidate_id"), 100),
               _bounded(content.get("project"), 500), _bounded(content.get("title"), 300),
               _bounded(content.get("summary"), 500), canonical,
               _bounded(content.get("digest"), 100) or _digest(canonical),
               str(content.get("status") or "accepted"), "none", DERIVED_DATA_CLASS,
               _bounded(content.get("origin_device_id"), 100),
               int(content.get("created_at") or now), now))
            self.db.commit()
        return workflow_id

    def snapshot(self, *, project=None, event_limit=50):
        return {"privacy": self.settings(), "raw_data_class": RAW_DATA_CLASS,
                "derived_data_class": DERIVED_DATA_CLASS,
                "events": self.list_events(project=project, limit=event_limit),
                "candidates": self.list_candidates(project=project),
                "workflows": self.list_workflows(project=project),
                "guarantees": {"raw_sync": False, "captures_content": False,
                               "acceptance_required": True, "accepted_authority": "none"}}

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
