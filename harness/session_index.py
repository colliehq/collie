"""Disposable session-list metadata. Journals remain the only recovery authority.

Entries are used only while the source file's identity, size and nanosecond
timestamps match. A missing, stale or unavailable index falls back to the JSON
journal. No conversation, authorization or execution state is loaded from here.
"""
import contextlib
import json
import os
import sqlite3


_FILENAME = ".session-summary-v1.sqlite3"


def fingerprint(stat):
    return ":".join(str(value) for value in (
        stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))


@contextlib.contextmanager
def _connect(directory):
    path = os.path.join(directory, _FILENAME)
    db = sqlite3.connect(path, timeout=0.05)
    try:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        db.execute("CREATE TABLE IF NOT EXISTS summaries ("
                   "sid TEXT PRIMARY KEY, stamp TEXT NOT NULL, payload TEXT NOT NULL)")
        yield db
    finally:
        db.close()


def read(directory, stamps):
    rows = {}
    try:
        with _connect(directory) as db:
            for sid, stamp in stamps.items():
                row = db.execute("SELECT payload FROM summaries WHERE sid=? AND stamp=?",
                                 (sid, stamp)).fetchone()
                if row:
                    try:
                        value = json.loads(row[0])
                        if (isinstance(value, dict) and value.get("id") == sid and
                                all(isinstance(value.get(key), str) for key in
                                    ("title", "cwd", "last", "forked_from")) and
                                all(type(value.get(key)) is int for key in
                                    ("turns", "edits", "touches")) and
                                isinstance(value.get("workspace"), dict) and
                                type(value.get("_has_active")) is bool and
                                type(value.get("updated")) in (int, float)):
                            rows[sid] = value
                    except (ValueError, TypeError):
                        pass
    except (OSError, sqlite3.Error):
        pass
    return rows


def write(directory, rows):
    if not rows:
        return
    try:
        with _connect(directory) as db:
            with db:
                db.executemany("INSERT OR REPLACE INTO summaries VALUES (?,?,?)", [
                    (sid, stamp, json.dumps(value, ensure_ascii=False, allow_nan=False))
                    for sid, stamp, value in rows])
    except (OSError, sqlite3.Error, ValueError, TypeError):
        pass


def forget(directory, sid):
    if not os.path.isfile(os.path.join(directory, _FILENAME)):
        return
    try:
        with _connect(directory) as db:
            with db:
                db.execute("DELETE FROM summaries WHERE sid=?", (sid,))
    except (OSError, sqlite3.Error):
        pass
