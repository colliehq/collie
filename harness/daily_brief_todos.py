"""The person's own to-do list: one more source the Daily Brief reads.

Every other brief source is something Collie or a connection wrote.  This one is
written by the person, on the brief page, and it is the only brief input a person
edits directly -- so it is the one that has to survive two open windows and a
lost network answer:

* **Every row carries a revision.**  An edit or a delete names the revision it was
  made against, and a row that moved since is refused with :class:`TodoConflict`
  instead of being overwritten.  A tab left open since yesterday cannot quietly undo
  what another window did this morning, and it cannot resurrect a deleted row.
* **Creating is safe to retry.**  The page chooses the new row's id before it
  sends, so when an answer is lost and the same create is sent again, the second
  request finds its own row (still at revision 1, still the same words) and returns
  it rather than adding a duplicate.
* **Refusals change nothing.**  Each write runs in one ``BEGIN IMMEDIATE``
  transaction, and validation happens before it opens.

What the brief does with a row is decided in :mod:`harness.daily_brief`, under the
same rules as every other personal item: late and due-today work needs the person,
what they ticked off today is done today, and an undated or later to-do stays in the
list and out of the day.  Nothing here runs a model, and a title is the person's own
sentence -- it is never translated, rewritten or treated as an instruction.

The list lives in ``<state>/daily-brief/todos.db``.  Reading a state directory that
has never had a list returns an empty one and creates nothing.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

from . import daily_brief

SCHEMA = "collie.daily_brief.todos/1"
#: The builder reads every row, so the store may never hold more than it reads.
MAX_TODOS = daily_brief.TODO_LIMIT
TITLE_LIMIT = 300
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_FIELDS = ("id", "title", "due", "done", "revision", "created_at", "updated_at", "done_at")


class TodoError(ValueError):
    """A request this store refuses by name.  Nothing was changed."""


class TodoConflict(TodoError):
    """Another window changed or removed this to-do first.  Nothing was changed."""


def path_for(root):
    return os.path.join(os.path.abspath(str(root)), "daily-brief", "todos.db")


def _title(value):
    if not isinstance(value, str):
        raise TodoError("Enter what the to-do is")
    title = " ".join("".join(ch if ch >= " " else " " for ch in value).split())
    if not title:
        raise TodoError("Enter what the to-do is")
    if len(title) > TITLE_LIMIT:
        raise TodoError("A to-do can be at most %d characters" % TITLE_LIMIT)
    return title


def _due(value):
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise TodoError("Use a due date like 2026-09-30, or leave it empty")
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        raise TodoError("That due date does not exist") from None
    return value


def _ident(value, *, required):
    if value in (None, "") and not required:
        return uuid.uuid4().hex
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise TodoError("This to-do has an invalid id")
    return value


def _revision(value):
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TodoError("This to-do has an invalid revision")
    return value


def _row(row):
    value = {key: row[key] for key in _FIELDS}
    value["done"] = bool(value["done"])
    return value


class TodoStore:
    """The to-do list under one state directory.  Short-lived connections, any thread."""

    def __init__(self, root):
        self.path = path_for(root)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS todos(
                id TEXT PRIMARY KEY, title TEXT NOT NULL, due TEXT NOT NULL,
                done INTEGER NOT NULL, revision INTEGER NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, done_at REAL)""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def todos(self):
        """Open work first, dated before undated, earliest due first."""
        with self._connect() as db:
            return [_row(row) for row in db.execute(
                "SELECT * FROM todos ORDER BY done, CASE WHEN due='' THEN 1 ELSE 0 END, "
                "due, created_at, id")]

    def save(self, value):
        """Create or edit one to-do.  ``revision`` 0 creates; anything else must match."""
        if not isinstance(value, dict):
            raise TodoError("Expected a to-do")
        title, due = _title(value.get("title")), _due(value.get("due"))
        done = value.get("done", False)
        if type(done) is not bool:
            raise TodoError("Completion must be true or false")
        ident = _ident(value.get("id"), required=False)
        revision = _revision(value.get("revision"))
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM todos WHERE id=?", (ident,)).fetchone()
            if old is None:
                if revision != 0:
                    raise TodoConflict("This to-do was deleted in another window.")
                if db.execute("SELECT count(*) FROM todos").fetchone()[0] >= MAX_TODOS:
                    raise TodoError("The list holds at most %d to-dos. Delete finished ones "
                                    "first." % MAX_TODOS)
                db.execute("INSERT INTO todos VALUES(?,?,?,?,?,?,?,?)",
                           (ident, title, due, int(done), 1, now, now, now if done else None))
            else:
                # The same create sent twice (its first answer was lost) finds its own
                # untouched row and returns it.  Anything else must name the revision
                # it was made against.
                if revision == 0 and old["revision"] == 1 and (
                        old["title"], old["due"], bool(old["done"])) == (title, due, done):
                    return _row(old)
                if revision != old["revision"]:
                    raise TodoConflict("This to-do changed in another window. Reload before "
                                       "saving.")
                done_at = (old["done_at"] or now) if done else None
                db.execute("UPDATE todos SET title=?, due=?, done=?, revision=?, updated_at=?, "
                           "done_at=? WHERE id=?", (title, due, int(done), revision + 1, now,
                                                   done_at, ident))
            return _row(db.execute("SELECT * FROM todos WHERE id=?", (ident,)).fetchone())

    def delete(self, value):
        """Delete one to-do at the revision the caller saw.  A row already gone is fine."""
        if not isinstance(value, dict):
            raise TodoError("Expected a to-do")
        ident = _ident(value.get("id"), required=True)
        revision = _revision(value.get("revision"))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT revision FROM todos WHERE id=?", (ident,)).fetchone()
            if old is None:
                return {"id": ident, "deleted": False}
            if old[0] != revision:
                raise TodoConflict("This to-do changed in another window. Reload before "
                                   "deleting.")
            db.execute("DELETE FROM todos WHERE id=?", (ident,))
        return {"id": ident, "deleted": True}


def read(root):
    """The list as a brief payload.  A root that never had one reads as empty."""
    if not os.path.isfile(path_for(root)):
        return {"todos": []}
    return {"todos": TodoStore(root).todos()}
