"""What the gate decided, and why — a durable local record.

The question this has to be able to answer is not "what did collie do" (the session
transcript has that) but **"why was I not asked about that?"**. So the invariant is:

    every call that ran WITHOUT a prompt records the rule that let it through.

`project` mode, a repo allowance, a standing rule, an override, an explicit approval —
each writes the reason it applied. A row that says "allowed" and cannot say why would be
the one row you actually needed, so `reason` is not optional anywhere.

Arguments are sanitised on the way in. The loop already hands the gate PRE-redaction
arguments (placeholders, not credentials), and this adds a second pass for the shapes
that are sensitive by position rather than by value: what someone typed into a page, the
body of a message. Belt and braces, because an audit log is exactly the file people
forget is readable and mail to each other when something goes wrong.

Local only. Nothing here is sent anywhere; there is no collie server to send it to.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

# Key names whose VALUE is a credential wherever it appears.
_SECRET_KEYS = ("token", "secret", "password", "passwd", "api_key", "apikey",
                "access_token", "refresh_token", "auth", "credential", "cookie")
# Key names whose value is free text a person wrote — not a secret by type, but not
# something to keep a copy of either.
_BODY_KEYS = ("body", "content", "html", "message", "text")
# Tools where an argument is, by construction, whatever the user was typing.
_TYPED_INPUT = {"browser_type", "desktop_type"}

STAGES = ("asked", "approved", "denied", "auto")


class AuditLog:
    def __init__(self, path: str = None):
        path = path or os.path.join(
            os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie"),
            "audit.db")
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self.db.execute("""CREATE TABLE IF NOT EXISTS gate_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER, session TEXT, cwd TEXT,
            tool TEXT, risk TEXT, target TEXT, stage TEXT, outcome TEXT,
            reason TEXT, rule TEXT, args TEXT)""")
        self.db.commit()

    def record(self, *, session="", cwd="", tool="", risk="", target="", stage="",
               outcome="", reason="", rule="", args=None) -> None:
        """One decision. Never raises into the caller — an audit failure must not take
        down the run it was recording, and a run that dies because logging failed is a
        worse outcome than a gap in the log."""
        try:
            with self._lock:
                self.db.execute(
                    "INSERT INTO gate_events(at,session,cwd,tool,risk,target,stage,"
                    "outcome,reason,rule,args) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (int(time.time()), session, cwd, tool, risk, target or "", stage,
                     outcome, reason[:500], rule or "",
                     json.dumps(sanitize(tool, args), default=str)[:2000]))
                self.db.commit()
        except Exception:
            pass

    def list(self, limit=100, tool=None, stage=None, session=None) -> list:
        sql, params, where = "SELECT * FROM gate_events", [], []
        for col, val in (("tool", tool), ("stage", stage), ("session", session)):
            if val:
                where.append("%s=?" % col)
                params.append(val)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit or 100), 1000)))
        out = []
        for r in self.db.execute(sql, params).fetchall():
            row = dict(r)
            try:
                row["args"] = json.loads(row.get("args") or "{}")
            except ValueError:
                row["args"] = {}
            out.append(row)
        return out

    def unexplained(self, limit=1000) -> list:
        """Rows that ran without a prompt and cannot say why.

        This should always be empty. It is a query rather than an assertion because the
        useful version of the invariant is one anybody can run against their own machine
        — `collie audit --unexplained` — not one that only holds in the test suite.
        """
        return [r for r in self.list(limit=limit)
                if r["stage"] == "auto" and not (r["reason"] or r["rule"])]

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass


def sanitize(tool: str, args) -> dict:
    if not isinstance(args, dict):
        return {}
    out = {}
    for k, v in args.items():
        lk = str(k).lower()
        if any(s in lk for s in _SECRET_KEYS):
            out[k] = "[redacted]"
        elif tool in _TYPED_INPUT and lk == "text":
            # Keep the LENGTH, drop the value: the point of an audit row for a keystroke
            # tool is that something was typed and roughly how much, never what. Same rule
            # browserbridge already applies to its own logging.
            out[k] = "[%d chars]" % len(str(v))
        elif any(lk == b or lk.endswith("_" + b) for b in _BODY_KEYS):
            out[k] = "[%d chars]" % len(str(v))
        else:
            out[k] = _shorten(v)
    return out


def _shorten(v):
    if isinstance(v, str):
        return v if len(v) <= 200 else v[:197] + "..."
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    if isinstance(v, list):
        return [_shorten(x) for x in v[:10]]
    if isinstance(v, dict):
        return {str(k): _shorten(x) for k, x in list(v.items())[:20]}
    return _shorten(str(v))
