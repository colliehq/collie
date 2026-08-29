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
MAX_DISCOVERY_EVENTS = 5000
MAX_SESSION_EVENTS = 200
MAX_CANDIDATES_PER_PROJECT = 64
AMBIENT_PROJECT = "@ambient"
OBSERVATION_MODES = frozenset(("off", "activity", "personal"))
OBSERVATION_CONSENT_VERSION = "outside-ai-observation-v1"
OBSERVATION_DISCLOSURE = (
    "continuous local app/system activity; separately connected personal sources; "
    "browser history reduced before storage to origin/time/count; raw history not uploaded; "
    "derived sync off by default; withdrawal available in the same control surface")
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
    if kind == "web":
        # A selector, page title, route, or pasted search must not masquerade as
        # a web identity. Bare public-looking hostnames are the only fallback.
        host = raw.lower().rstrip(".")
        if re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::\d{1,5})?", host):
            return host[:255]
        return ""
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
    # Window titles and generic tool targets are arbitrary user/application text.
    # The action already provides the useful procedural signal, so retain no value.
    return ""


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


def _clean_sequence(value):
    if not isinstance(value, list) or not 2 <= len(value) <= 8:
        raise ValueError("workflow sequence must contain between 2 and 8 steps")
    out = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("workflow steps must be objects")
        app = _bounded(raw.get("app"), 80).lower()
        action = _bounded(raw.get("action"), 100).lower()
        kind = _bounded(raw.get("object_kind") or "tool", 40).lower()
        if not app or not action or kind not in ("tool", "web", "window", "file", "command"):
            raise ValueError("workflow step has an invalid app, action, or object kind")
        ref = str(raw.get("object_ref") or "")
        if kind == "file":
            if os.path.isabs(ref):
                ref = os.path.basename(ref)
            else:
                parts = [part for part in ref.replace("\\", "/").split("/")
                         if part not in ("", ".")]
                ref = "/".join(parts) if ".." not in parts else os.path.basename(ref)
            ref = _bounded(ref, 160)
        else:
            ref = safe_object_ref(ref, kind)
        out.append({"app": app, "action": action, "object_kind": kind,
                    "object_ref": ref})
    return out


def _review_rank(status):
    # A safety dismissal wins an exact-timestamp conflict.
    return {"proposed": 0, "accepted": 1, "dismissed": 2}.get(str(status), -1)


class ProcedureMemory:
    """Local observation journal plus explicitly reviewed, syncable derivatives."""

    def __init__(self, path=None, *, embedder=None):
        self.path = _path(path)
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.embedder = embedder or HashEmbedding()
        self._lock = threading.RLock()
        self._settings_cache = None
        self._settings_cached_at = 0.0
        self._last_prune = 0.0
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
        CREATE TABLE IF NOT EXISTS procedure_consent_receipts(
          receipt_id TEXT PRIMARY KEY,version TEXT NOT NULL,mode TEXT NOT NULL,
          action TEXT NOT NULL,disclosure_digest TEXT NOT NULL,created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS procedure_events(
          event_id TEXT PRIMARY KEY, observed_at REAL NOT NULL, session TEXT NOT NULL,
          project TEXT NOT NULL, app TEXT NOT NULL, action TEXT NOT NULL,
          object_kind TEXT NOT NULL, object_ref TEXT NOT NULL, result TEXT NOT NULL,
          data_class TEXT NOT NULL CHECK(data_class='device_only'), metadata_json TEXT NOT NULL,
          fingerprint TEXT NOT NULL, source TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS procedure_events_session_idx
          ON procedure_events(project,session,observed_at);
        CREATE INDEX IF NOT EXISTS procedure_events_time_idx ON procedure_events(observed_at);
        CREATE TABLE IF NOT EXISTS procedure_sessions(
          project TEXT NOT NULL, session TEXT NOT NULL, event_count INTEGER NOT NULL,
          first_at REAL NOT NULL, last_at REAL NOT NULL,
          PRIMARY KEY(project,session));
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
        columns = {row[1] for row in self.db.execute(
            "PRAGMA table_info(procedure_candidates)").fetchall()}
        if "pattern_digest" not in columns:
            self.db.execute(
                "ALTER TABLE procedure_candidates ADD COLUMN pattern_digest TEXT NOT NULL DEFAULT ''")
        for row in self.db.execute(
                "SELECT candidate_id,sequence_json FROM procedure_candidates WHERE pattern_digest=''"):
            self.db.execute("UPDATE procedure_candidates SET pattern_digest=? WHERE candidate_id=?",
                            (_digest(row["sequence_json"]), row["candidate_id"]))
        # Digests are portable identities. Older builds included the absolute project
        # path, which made one routine look different on every computer.
        for row in self.db.execute("SELECT workflow_id,sequence_json FROM learned_workflows"):
            digest = _digest(row["sequence_json"])
            self.db.execute("UPDATE learned_workflows SET digest=? WHERE workflow_id=? AND digest<>?",
                            (digest, row["workflow_id"], digest))
        self.db.execute("""CREATE INDEX IF NOT EXISTS procedure_candidates_pattern_idx
          ON procedure_candidates(project,pattern_digest)""")
        self.db.execute("""CREATE INDEX IF NOT EXISTS learned_workflows_digest_idx
          ON learned_workflows(project,digest)""")
        existing_settings = {row[0] for row in self.db.execute(
            "SELECT key FROM procedure_settings").fetchall()}
        defaults = {"enabled": True, "paused": False,
                    "retention_days": DEFAULT_RETENTION_DAYS,
                    "observation_mode": "off",
                    "observation_consent_version": "",
                    "observation_consent_at": 0,
                    "observation_consent_revoked_at": 0,
                    "derived_sync_enabled": False,
                    "ambient_enabled": False, "ambient_sample_seconds": 5,
                    "ambient_retention_days": 7,
                    "excluded_apps": list(DEFAULT_EXCLUDED_APPS)}
        now = int(time.time())
        for key, value in defaults.items():
            self.db.execute(
                "INSERT OR IGNORE INTO procedure_settings(key,value,updated_at) VALUES(?,?,?)",
                (key, json.dumps(value), now))
        # Preserve an existing opt-in from the short-lived boolean setting while
        # making every fresh installation start with outside-AI observation off.
        if "observation_mode" not in existing_settings:
            row = self.db.execute(
                "SELECT value FROM procedure_settings WHERE key='ambient_enabled'").fetchone()
            try:
                legacy_enabled = bool(json.loads(row[0])) if row else False
            except (TypeError, ValueError):
                legacy_enabled = False
            if legacy_enabled:
                self.db.execute(
                    "UPDATE procedure_settings SET value=?,updated_at=? WHERE key='observation_mode'",
                    (json.dumps("activity"), now))
                self.db.execute(
                    "UPDATE procedure_settings SET value=?,updated_at=? WHERE key='observation_consent_version'",
                    (json.dumps(OBSERVATION_CONSENT_VERSION), now))
                self.db.execute(
                    "UPDATE procedure_settings SET value=?,updated_at=? WHERE key='observation_consent_at'",
                    (json.dumps(now), now))
                self._append_consent_receipt("activity", "migrated_grant", now=now)
        self.db.commit()

    def _append_consent_receipt(self, mode, action, *, now=None):
        at = int(time.time() if now is None else now)
        self.db.execute("""INSERT INTO procedure_consent_receipts(
            receipt_id,version,mode,action,disclosure_digest,created_at)
            VALUES(?,?,?,?,?,?)""",
            ("consent_" + uuid.uuid4().hex, OBSERVATION_CONSENT_VERSION,
             str(mode), str(action), _digest(OBSERVATION_DISCLOSURE), at))

    def consent_receipts(self, limit=50):
        return [dict(row) for row in self.db.execute(
            """SELECT * FROM procedure_consent_receipts
               ORDER BY rowid DESC LIMIT ?""",
            (max(1, min(int(limit), 500)),))]

    def settings(self, *, refresh=False):
        now = time.monotonic()
        if (not refresh and self._settings_cache is not None and
                now - self._settings_cached_at < 1.0):
            return dict(self._settings_cache)
        out = {}
        for row in self.db.execute("SELECT key,value FROM procedure_settings"):
            try:
                out[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                out[row["key"]] = row["value"]
        self._settings_cache = out
        self._settings_cached_at = now
        return dict(out)

    def update_privacy(self, *, enabled=None, paused=None, retention_days=None,
                       observation_mode=None, consent=False,
                       derived_sync_enabled=None,
                       ambient_enabled=None, ambient_sample_seconds=None,
                       ambient_retention_days=None, exclude_app="", include_app=""):
        values = self.settings()
        if observation_mode is None and ambient_enabled is not None:
            observation_mode = "activity" if ambient_enabled else "off"
        prior_mode = str(values.get("observation_mode") or "off")
        consent_action = ""
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
        if observation_mode is not None:
            mode = str(observation_mode or "").strip().lower()
            if mode not in OBSERVATION_MODES:
                raise ValueError("observation_mode must be off, activity, or personal")
            consent_stale = (
                values.get("observation_consent_version") != OBSERVATION_CONSENT_VERSION or
                int(values.get("observation_consent_revoked_at") or 0) >=
                int(values.get("observation_consent_at") or 0))
            if mode != "off" and consent_stale:
                if consent is not True:
                    raise ValueError("confirmed consent is required before outside-AI observation")
                values["observation_consent_version"] = OBSERVATION_CONSENT_VERSION
                values["observation_consent_at"] = int(time.time())
                values["observation_consent_revoked_at"] = 0
                consent_action = "granted"
            if mode == "off" and values.get("observation_mode") != "off":
                values["observation_consent_revoked_at"] = int(time.time())
                consent_action = "revoked"
            elif mode != prior_mode and not consent_action:
                consent_action = "mode_changed"
            values["observation_mode"] = mode
            values["ambient_enabled"] = mode != "off"
        if derived_sync_enabled is not None:
            if not isinstance(derived_sync_enabled, bool):
                raise ValueError("derived_sync_enabled must be boolean")
            # Upload is deliberately independent from collection. A user may use
            # personal intelligence forever without sending even a derivative.
            values["derived_sync_enabled"] = derived_sync_enabled
        if ambient_enabled is not None:
            if not isinstance(ambient_enabled, bool):
                raise ValueError("ambient_enabled must be boolean")
            values["ambient_enabled"] = ambient_enabled
        if ambient_sample_seconds is not None:
            seconds = int(ambient_sample_seconds)
            if seconds < 2 or seconds > 300:
                raise ValueError("ambient_sample_seconds must be between 2 and 300")
            values["ambient_sample_seconds"] = seconds
        if ambient_retention_days is not None:
            days = int(ambient_retention_days)
            if days < 1 or days > 90:
                raise ValueError("ambient_retention_days must be between 1 and 90")
            values["ambient_retention_days"] = days
        excluded = {_bounded(x, 100).lower() for x in values.get("excluded_apps", [])
                    if str(x).strip()}
        if exclude_app:
            excluded.add(_bounded(exclude_app, 100).lower())
        if include_app:
            excluded.discard(_bounded(include_app, 100).lower())
        values["excluded_apps"] = sorted(excluded)
        now = int(time.time())
        with self._lock:
            if consent_action:
                self._append_consent_receipt(
                    str(values.get("observation_mode") or "off"), consent_action, now=now)
            for key in ("enabled", "paused", "retention_days", "observation_mode",
                        "observation_consent_version", "observation_consent_at",
                        "observation_consent_revoked_at",
                        "derived_sync_enabled", "ambient_enabled",
                        "ambient_sample_seconds", "ambient_retention_days", "excluded_apps"):
                self.db.execute("""INSERT INTO procedure_settings(key,value,updated_at)
                    VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,updated_at=excluded.updated_at""",
                    (key, json.dumps(values[key]), now))
            self.db.commit()
        self._settings_cache = None
        return self.settings(refresh=True)

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
        project = (str(project) if str(project).startswith("@") else
                   (os.path.abspath(project) if project else ""))
        ref = safe_object_ref(object_ref, kind, project=project)
        if not app or not action or self._excluded(app, ref, metadata):
            return None
        session = _bounded(session or "unscoped", 160)
        result = _bounded(result or "observed", 40).lower()
        at = float(time.time() if observed_at is None else observed_at)
        fingerprint = _digest("|".join((app, action, kind, ref, result)))
        should_discover = False
        with self._lock:
            prior = self.db.execute("""SELECT observed_at,fingerprint FROM procedure_events
                WHERE project=? AND session=? ORDER BY observed_at DESC LIMIT 1""",
                (project, session)).fetchone()
            if prior and prior["fingerprint"] == fingerprint and at - prior["observed_at"] <= 2:
                return None
            event_id = "evt_" + uuid.uuid4().hex
            self.db.execute("""INSERT INTO procedure_events(event_id,observed_at,session,project,
                app,action,object_kind,object_ref,result,data_class,metadata_json,fingerprint,source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, at, session, project, app, action, kind, ref, result,
                 RAW_DATA_CLASS, json.dumps(_safe_metadata(metadata), sort_keys=True),
                 fingerprint, _bounded(source, 80)))
            self.db.execute("""INSERT INTO procedure_sessions(
                project,session,event_count,first_at,last_at) VALUES(?,?,1,?,?)
                ON CONFLICT(project,session) DO UPDATE SET
                event_count=procedure_sessions.event_count+1,last_at=excluded.last_at""",
                (project, session, at, at))
            session_events = int(self.db.execute(
                "SELECT event_count FROM procedure_sessions WHERE project=? AND session=?",
                (project, session)).fetchone()[0])
            if session_events in (2, 5):
                should_discover = self.db.execute(
                    """SELECT 1 FROM procedure_sessions
                       WHERE project=? AND session<>? AND event_count>=? LIMIT 1""",
                    (project, session, session_events)).fetchone() is not None
            self.db.commit()
        self._maybe_prune(time.time())
        # Run bounded discovery twice per session, not on every action. The second
        # action catches short routines promptly; the fifth lets longer routines from
        # two completed sessions emerge without an operator pressing Discover.
        try:
            if should_discover:
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

    def _maybe_prune(self, now):
        # Retention is maintenance, not part of the per-action hot path.
        if float(now) - self._last_prune >= 3600:
            self.prune(now=now)
            self._last_prune = float(now)

    def prune(self, *, now=None):
        settings = self.settings()
        wall = float(time.time() if now is None else now)
        cutoff = wall - int(settings.get("retention_days") or DEFAULT_RETENTION_DAYS) * 86400
        ambient_cutoff = wall - int(settings.get("ambient_retention_days") or 7) * 86400
        with self._lock:
            cur = self.db.execute("""DELETE FROM procedure_events WHERE observed_at < ?
                OR (source LIKE 'ambient%' AND observed_at < ?)""",
                (cutoff, ambient_cutoff))
            self.db.execute("""DELETE FROM procedure_sessions WHERE NOT EXISTS(
                SELECT 1 FROM procedure_events e WHERE e.project=procedure_sessions.project
                  AND e.session=procedure_sessions.session)""")
            self.db.commit()
            return max(0, int(cur.rowcount or 0))

    def purge_events(self, *, confirmed=False):
        if not confirmed:
            raise ValueError("confirmed=true is required to delete raw observations")
        with self._lock:
            cur = self.db.execute("DELETE FROM procedure_events")
            self.db.execute("DELETE FROM procedure_sessions")
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
        if not project:
            out = []
            projects = [row[0] for row in self.db.execute(
                "SELECT DISTINCT project FROM procedure_events ORDER BY project")]
            for project_name in projects:
                out.extend(self.discover(
                    project=project_name, min_support=min_support,
                    min_steps=min_steps, max_steps=max_steps))
            return out
        rows = self.db.execute(
            """SELECT * FROM procedure_events WHERE project=?
               ORDER BY observed_at DESC LIMIT ?""",
            (project, MAX_DISCOVERY_EVENTS)).fetchall()
        sessions = {}
        for row in reversed(rows):
            bucket = sessions.setdefault((row["project"], row["session"]), [])
            if not bucket or bucket[-1]["fingerprint"] != row["fingerprint"]:
                bucket.append(row)
                if len(bucket) > MAX_SESSION_EVENTS:
                    del bucket[0]
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
        supported.sort(key=lambda p: (-len(p["sessions"]) * len(p["steps"]),
                                      -len(p["steps"])))
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
        chosen.sort(key=lambda p: (
            -(len(p["sessions"]) / max(1, len(sessions))) *
            len(p["sessions"]) * len(p["steps"]),
            -len(p["steps"])))
        chosen = chosen[:MAX_CANDIDATES_PER_PROJECT]
        now, changed = int(time.time()), []
        with self._lock:
            for item in chosen:
                canonical = json.dumps(item["steps"], sort_keys=True, separators=(",", ":"))
                pattern_digest = _digest(canonical)
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
                self.db.execute("""INSERT INTO procedure_candidates(candidate_id,project,
                    pattern_digest,title,summary,sequence_json,support,confidence,evidence_json,
                    status,data_class,vector_json,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(candidate_id) DO UPDATE SET title=excluded.title,
                    summary=excluded.summary,support=excluded.support,
                    confidence=excluded.confidence,evidence_json=excluded.evidence_json,
                    pattern_digest=excluded.pattern_digest,vector_json=excluded.vector_json,
                    updated_at=excluded.updated_at""",
                    (candidate_id, item["project"], pattern_digest, title, summary, canonical,
                     support, confidence,
                     json.dumps(item["evidence"], sort_keys=True), status, DERIVED_DATA_CLASS,
                     json.dumps(vector), created, now))
                changed.append(candidate_id)
            if changed:
                marks = ",".join("?" * len(changed))
                self.db.execute(
                    """DELETE FROM procedure_candidates WHERE project=? AND status='proposed'
                       AND candidate_id NOT IN (%s)""" % marks,
                    (project, *changed))
            else:
                self.db.execute(
                    "DELETE FROM procedure_candidates WHERE project=? AND status='proposed'",
                    (project,))
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
                review_note=?,origin_device_id='',updated_at=? WHERE candidate_id=?""",
                (status, now, _bounded(note, 500), now, candidate_id))
            if action == "accept":
                canonical = json.dumps(candidate["sequence"], sort_keys=True, separators=(",", ":"))
                workflow_id = "learned_" + _digest(candidate_id)[:24]
                digest = _digest(canonical)
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
        remote_id = _bounded(content.get("candidate_id"), 100)
        if not remote_id:
            raise ValueError("candidate_id required")
        project = _bounded(content.get("project"), 500)
        sequence = _clean_sequence(content.get("sequence"))
        canonical = json.dumps(sequence, sort_keys=True, separators=(",", ":"))
        pattern_digest = _digest(canonical)
        status = str(content.get("status") or "proposed")
        if status not in ("proposed", "accepted", "dismissed"):
            raise ValueError("invalid synced candidate status")
        support = max(2, min(int(content.get("support") or 2), 100000))
        confidence = max(0.0, min(float(content.get("confidence") or 0), 1.0))
        now = int(time.time())
        reviewed_at = int(content.get("reviewed_at") or 0) or None
        if status != "proposed" and reviewed_at is None:
            raise ValueError("reviewed synced candidate requires reviewed_at")
        summary = _bounded(content.get("summary"), 500)
        title = _bounded(content.get("title"), 300)
        vector = self.embedder.embed(summary, kind="passage")
        with self._lock:
            existing = self.db.execute("""SELECT * FROM procedure_candidates
                WHERE project=? AND pattern_digest=? ORDER BY
                CASE WHEN origin_device_id='' THEN 0 ELSE 1 END,updated_at DESC LIMIT 1""",
                (project, pattern_digest)).fetchone()
            if existing is None:
                collision = self.db.execute(
                    "SELECT pattern_digest FROM procedure_candidates WHERE candidate_id=?",
                    (remote_id,)).fetchone()
                candidate_id = (remote_id if collision is None else
                                "routine_" + _digest(project + "\n" + canonical)[:24])
                self.db.execute("""INSERT INTO procedure_candidates(
                    candidate_id,project,pattern_digest,title,summary,sequence_json,support,
                    confidence,evidence_json,status,data_class,vector_json,origin_device_id,
                    created_at,updated_at,reviewed_at,review_note)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (candidate_id, project, pattern_digest, title, summary, canonical,
                     support, confidence, "[]", status, DERIVED_DATA_CLASS,
                     json.dumps(vector), _bounded(content.get("origin_device_id"), 100),
                     int(content.get("created_at") or now), now, reviewed_at,
                     _bounded(content.get("review_note"), 500)))
            else:
                candidate_id = existing["candidate_id"]
                current_reviewed = int(existing["reviewed_at"] or 0)
                incoming_reviewed = int(reviewed_at or 0)
                take_review = (incoming_reviewed > current_reviewed or
                               (incoming_reviewed == current_reviewed and
                                _review_rank(status) > _review_rank(existing["status"])))
                merged_status = status if take_review else existing["status"]
                merged_reviewed = reviewed_at if take_review else existing["reviewed_at"]
                merged_note = (_bounded(content.get("review_note"), 500) if take_review
                               else existing["review_note"])
                self.db.execute("""UPDATE procedure_candidates SET title=?,summary=?,
                    sequence_json=?,pattern_digest=?,support=MAX(support,?),
                    confidence=MAX(confidence,?),status=?,vector_json=?,reviewed_at=?,
                    review_note=?,updated_at=? WHERE candidate_id=?""",
                    (title or existing["title"], summary or existing["summary"], canonical,
                     pattern_digest, support, confidence, merged_status, json.dumps(vector),
                     merged_reviewed, merged_note, now, candidate_id))
            self.db.commit()
        return candidate_id

    def upsert_synced_workflow(self, content):
        remote_id = _bounded(content.get("workflow_id"), 100)
        if not remote_id:
            raise ValueError("workflow_id required")
        project = _bounded(content.get("project"), 500)
        sequence = _clean_sequence(content.get("sequence"))
        canonical = json.dumps(sequence, sort_keys=True, separators=(",", ":"))
        digest = _digest(canonical)
        status = str(content.get("status") or "accepted")
        if status not in ("accepted", "disabled"):
            raise ValueError("invalid synced workflow status")
        now = int(time.time())
        with self._lock:
            candidate = self.db.execute("""SELECT candidate_id FROM procedure_candidates
                WHERE project=? AND pattern_digest=? ORDER BY
                CASE WHEN origin_device_id='' THEN 0 ELSE 1 END LIMIT 1""",
                (project, digest)).fetchone()
            candidate_id = (candidate["candidate_id"] if candidate else
                            _bounded(content.get("candidate_id"), 100))
            existing = self.db.execute("""SELECT * FROM learned_workflows
                WHERE project=? AND digest=? ORDER BY
                CASE WHEN origin_device_id='' THEN 0 ELSE 1 END,updated_at DESC LIMIT 1""",
                (project, digest)).fetchone()
            if existing is None:
                collision = self.db.execute(
                    "SELECT digest FROM learned_workflows WHERE workflow_id=?",
                    (remote_id,)).fetchone()
                workflow_id = (remote_id if collision is None else
                               "learned_" + digest[:24])
                self.db.execute("""INSERT INTO learned_workflows(
                    workflow_id,candidate_id,project,title,summary,sequence_json,digest,status,
                    authority_scope,data_class,origin_device_id,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (workflow_id, candidate_id, project, _bounded(content.get("title"), 300),
                     _bounded(content.get("summary"), 500), canonical, digest, status,
                     "none", DERIVED_DATA_CLASS,
                     _bounded(content.get("origin_device_id"), 100),
                     int(content.get("created_at") or now), now))
            else:
                workflow_id = existing["workflow_id"]
                # Preserve a locally authored state; imported copies are replicas,
                # never a way to silently overwrite a decision made on this device.
                merged_status = existing["status"] if not existing["origin_device_id"] else status
                self.db.execute("""UPDATE learned_workflows SET candidate_id=?,title=?,
                    summary=?,sequence_json=?,digest=?,status=?,authority_scope='none',
                    updated_at=? WHERE workflow_id=?""",
                    (candidate_id, _bounded(content.get("title"), 300) or existing["title"],
                     _bounded(content.get("summary"), 500) or existing["summary"], canonical,
                     digest, merged_status, now, workflow_id))
            self.db.commit()
        return workflow_id

    def snapshot(self, *, project=None, event_limit=50):
        return {"privacy": self.settings(), "raw_data_class": RAW_DATA_CLASS,
                "consent_receipts": self.consent_receipts(),
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
