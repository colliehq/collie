"""Anchored artifact comments and bounded rework handoffs.

Comments refer to exact file lines, image rectangles, meeting timestamps, diffs, or session
messages.  They are durable local review data; creating a comment does not grant write authority.
Selected unresolved comments can be converted into a narrowly-scoped Build prompt.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid


_LOCK = threading.RLock()
_ID = re.compile(r"^ann_[0-9a-f]{24}$")
_KINDS = {"file", "diff", "image", "meeting", "session"}


def _db_path():
    root = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
    return os.path.abspath(os.path.join(root, "annotations.db"))


def _text(value, limit):
    return str(value or "").replace("\x00", "")[:limit]


def _number(value, minimum=0.0, maximum=1e12):
    if isinstance(value, bool):
        raise ValueError("expected a number")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("expected a number")
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ValueError("number is out of range")
    return value


def _anchor(kind, value):
    if not isinstance(value, dict):
        raise ValueError("annotation anchor must be an object")
    out = {}
    if kind in {"file", "diff"}:
        path = _text(value.get("path"), 1000).strip()
        if not path:
            raise ValueError("file annotation requires path")
        line = int(_number(value.get("line", 1), 1, 10_000_000))
        end = int(_number(value.get("end_line", line), line, 10_000_000))
        out = {"path": path, "line": line, "end_line": end,
               "side": _text(value.get("side") or "current", 20),
               "context": _text(value.get("context"), 1000),
               "sha256": _text(value.get("sha256"), 64)}
    elif kind == "image":
        artifact = _text(value.get("artifact"), 1000).strip()
        if not artifact:
            raise ValueError("image annotation requires artifact")
        out = {"artifact": artifact}
        for field in ("x", "y", "width", "height"):
            out[field] = _number(value.get(field, 0), 0, 1)
    elif kind == "meeting":
        meeting_id = _text(value.get("meeting_id"), 128).strip()
        if not meeting_id:
            raise ValueError("meeting annotation requires meeting_id")
        out = {"meeting_id": meeting_id, "seconds": _number(value.get("seconds", 0), 0, 604800)}
    else:
        session_id = _text(value.get("session_id"), 128).strip()
        if not session_id:
            raise ValueError("session annotation requires session_id")
        out = {"session_id": session_id,
               "message_index": int(_number(value.get("message_index", 0), 0, 1_000_000))}
    return out


class AnnotationStore:
    def __init__(self, path=None):
        self.path = os.path.abspath(path or _db_path())
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS annotations(
                annotation_id TEXT PRIMARY KEY, artifact_kind TEXT NOT NULL,
                artifact_id TEXT NOT NULL, anchor_json TEXT NOT NULL, body TEXT NOT NULL,
                author TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, resolution TEXT NOT NULL DEFAULT '')""")
            self.db.execute("CREATE INDEX IF NOT EXISTS annotations_artifact_idx ON annotations(artifact_kind,artifact_id,status,created_at)")

    @staticmethod
    def _row(row):
        if not row:
            return None
        out = dict(row)
        out["anchor"] = json.loads(out.pop("anchor_json"))
        return out

    def create(self, kind, artifact_id, anchor, body, *, author="user"):
        kind = str(kind or "").lower()
        if kind not in _KINDS:
            raise ValueError("unsupported artifact kind")
        artifact_id = _text(artifact_id, 1000).strip()
        body = _text(body, 10_000).strip()
        if not artifact_id or not body:
            raise ValueError("artifact_id and comment are required")
        clean_anchor = _anchor(kind, anchor)
        now = time.time(); annotation_id = "ann_" + uuid.uuid4().hex[:24]
        with _LOCK, self.db:
            self.db.execute("INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?,?,?)",
                (annotation_id, kind, artifact_id, json.dumps(clean_anchor, ensure_ascii=False),
                 body, _text(author, 80) or "user", "open", now, now, ""))
        return self.get(annotation_id)

    def get(self, annotation_id):
        if not _ID.fullmatch(str(annotation_id or "")):
            raise ValueError("invalid annotation id")
        with _LOCK:
            row = self.db.execute("SELECT * FROM annotations WHERE annotation_id=?",
                                  (annotation_id,)).fetchone()
        if not row:
            raise ValueError("annotation not found")
        return self._row(row)

    def list(self, *, kind="", artifact_id="", status="open", limit=200):
        sql, args = "SELECT * FROM annotations WHERE 1=1", []
        if kind:
            if kind not in _KINDS:
                raise ValueError("unsupported artifact kind")
            sql += " AND artifact_kind=?"; args.append(kind)
        if artifact_id:
            sql += " AND artifact_id=?"; args.append(_text(artifact_id, 1000))
        if status:
            if status not in {"open", "resolved", "dismissed"}:
                raise ValueError("invalid annotation status")
            sql += " AND status=?"; args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"; args.append(max(1, min(500, int(limit))))
        with _LOCK:
            return [self._row(row) for row in self.db.execute(sql, args)]

    def resolve(self, annotation_id, *, action="resolved", resolution=""):
        if action not in {"resolved", "dismissed", "open"}:
            raise ValueError("invalid annotation action")
        if not _ID.fullmatch(str(annotation_id or "")):
            raise ValueError("invalid annotation id")
        with _LOCK, self.db:
            cur = self.db.execute("UPDATE annotations SET status=?,resolution=?,updated_at=? WHERE annotation_id=?",
                                  (action, _text(resolution, 4000), time.time(), annotation_id))
        if not cur.rowcount:
            raise ValueError("annotation not found")
        return self.get(annotation_id)

    def rework(self, annotation_ids):
        ids = list(dict.fromkeys(str(x) for x in (annotation_ids or [])))[:100]
        if not ids or any(not _ID.fullmatch(x) for x in ids):
            raise ValueError("select valid annotation ids")
        marks = ",".join("?" for _ in ids)
        with _LOCK:
            rows = [self._row(r) for r in self.db.execute(
                "SELECT * FROM annotations WHERE status='open' AND annotation_id IN (%s) ORDER BY created_at" % marks,
                ids)]
        if len(rows) != len(ids):
            raise ValueError("one or more annotations are missing or no longer open")
        lines = ["Apply only the selected anchored review comments below.",
                 "Re-read each current artifact before editing; anchors may be stale.",
                 "Preserve unrelated work and report verification for every changed artifact.", ""]
        for row in rows:
            a = row["anchor"]
            if row["artifact_kind"] in {"file", "diff"}:
                where = "%s:%s-%s" % (a["path"], a["line"], a["end_line"])
            elif row["artifact_kind"] == "meeting":
                where = "meeting %s at %.1fs" % (a["meeting_id"], a["seconds"])
            elif row["artifact_kind"] == "image":
                where = "image %s rectangle %.3f,%.3f %.3fx%.3f" % (
                    a["artifact"], a["x"], a["y"], a["width"], a["height"])
            else:
                where = "session %s message %s" % (a["session_id"], a["message_index"])
            lines.append("- [%s] %s — %s" % (row["annotation_id"], where, row["body"]))
        return {"intent": "build", "source": "annotations",
                "annotation_ids": ids, "prompt": "\n".join(lines), "comments": rows}

    def close(self):
        self.db.close()
