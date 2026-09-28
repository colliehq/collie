"""A to-do list the person edits, read by the Daily Brief like any other source.

Three halves, each against the real code:

* the store -- revisions refuse an edit or delete from a stale window, a create whose
  answer was lost is safe to send again, and every refusal leaves the list unchanged;
* the builder -- late and due-today work needs the person, work ticked off today is
  done today, and an undated or later to-do stays out of the day, exactly as the
  brief treats a personal reminder that is only "upcoming";
* the surface -- the brief page reads the whole list, the route refuses without the
  session token, reports a stale window as a conflict, and a hidden to-do is still a
  to-do.

Every store lives under ``tmp_path``.  No network, no model, no real user state.
"""
import datetime as dt
import json
import os
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from harness import daily_brief as db
from harness import daily_brief_todos as todos
from harness import daily_brief_web as web
from test_daily_brief import NOON, payloads

TODAY = "2026-09-23"                      # NOON's date in UTC
UTC = dt.timezone.utc


@pytest.fixture
def root(tmp_path):
    return str(tmp_path / "state")


def todo_rows(*rows):
    return {"todos": [dict({"id": "t%d" % index, "title": "To-do %d" % index, "due": "",
                            "done": False, "revision": 1, "created_at": NOON - 86400,
                            "updated_at": NOON - 86400, "done_at": None}, **row)
                      for index, row in enumerate(rows)]}


def by_title(rows):
    return {row["title"]: row for row in rows}


# --------------------------------------------------------------- the store


def test_a_lost_create_is_safe_to_resend_and_a_stale_window_is_refused(root):
    store = todos.TodoStore(root)
    draft = {"id": "stable-create", "revision": 0, "title": "Renew passport", "due": "2026-10-01"}
    first = store.save(draft)
    assert first["revision"] == 1 and first["done"] is False
    # The answer was lost and the page sent the same create again: same row, no duplicate.
    assert store.save(draft) == first
    assert len(store.todos()) == 1

    done = store.save(dict(first, done=True))
    assert done["revision"] == 2 and done["done_at"]
    # The first window still holds revision 1. Neither its edit nor its delete may land.
    with pytest.raises(todos.TodoConflict):
        store.save(dict(first, title="Stale title"))
    with pytest.raises(todos.TodoConflict):
        store.delete(first)
    reopened = todos.TodoStore(root).todos()
    assert [(row["title"], row["done"]) for row in reopened] == [("Renew passport", True)]

    assert store.delete(done) == {"id": "stable-create", "deleted": True}
    # A window that still shows the deleted row cannot bring it back by editing it.
    with pytest.raises(todos.TodoConflict):
        store.save(dict(done, done=False))
    assert store.todos() == []
    # Deleting something already gone is a no-op, so a retried delete is safe too.
    assert store.delete(done) == {"id": "stable-create", "deleted": False}


def test_a_resent_create_with_different_words_is_a_conflict_not_an_overwrite(root):
    store = todos.TodoStore(root)
    store.save({"id": "same-id", "revision": 0, "title": "First words"})
    with pytest.raises(todos.TodoConflict):
        store.save({"id": "same-id", "revision": 0, "title": "Other words"})
    assert [row["title"] for row in store.todos()] == ["First words"]


def test_completion_time_is_kept_across_edits_and_cleared_when_reopened(root):
    store = todos.TodoStore(root)
    row = store.save({"title": "Pay rent", "due": "2026-09-30"})
    assert row["done_at"] is None and len(row["id"]) == 32
    done = store.save(dict(row, done=True))
    renamed = store.save(dict(done, title="Pay the rent"))
    assert renamed["done_at"] == done["done_at"]       # editing the words is not finishing again
    reopened = store.save(dict(renamed, done=False))
    assert reopened["done_at"] is None and reopened["revision"] == 4


@pytest.mark.parametrize("value", [
    {"title": ""}, {"title": "   \n\t "}, {"title": "x" * 301}, {"title": 5},
    {"title": "x", "due": "2026-02-30"}, {"title": "x", "due": "30/09/2026"},
    {"title": "x", "due": 20260930}, {"title": "x", "done": "false"},
    {"title": "x", "id": "../../etc"}, {"title": "x", "id": "a" * 81},
    {"title": "x", "revision": True}, {"title": "x", "revision": -1}, ["not", "a", "dict"]])
def test_an_invalid_todo_is_refused_and_changes_nothing(root, value):
    store = todos.TodoStore(root)
    kept = store.save({"title": "Keep me"})
    with pytest.raises(todos.TodoError):
        store.save(value)
    assert store.todos() == [kept]


def test_titles_are_one_line_of_the_persons_own_words(root):
    row = todos.TodoStore(root).save({"title": "  Call\nthe\x07 bank  "})
    assert row["title"] == "Call the bank"


def test_the_list_is_bounded_by_what_the_brief_reads(root, monkeypatch):
    assert todos.MAX_TODOS == db.TODO_LIMIT
    monkeypatch.setattr(todos, "MAX_TODOS", 2)
    store = todos.TodoStore(root)
    store.save({"title": "one"})
    store.save({"title": "two"})
    with pytest.raises(todos.TodoError, match="at most 2"):
        store.save({"title": "three"})
    assert len(store.todos()) == 2


def test_reading_a_root_without_a_list_creates_nothing(root):
    assert todos.read(root) == {"todos": []}
    assert not os.path.exists(root)


# --------------------------------------------------------------- the builder


def test_late_and_due_today_need_you_and_later_or_undated_work_stays_in_the_list():
    brief = db.build(payloads(todos=todo_rows(
        {"title": "Late", "due": "2026-09-20"},
        {"title": "Today", "due": TODAY},
        {"title": "Later", "due": "2026-09-30"},
        {"title": "Someday"},
        {"title": "Finished today", "due": "2026-09-20", "done": True, "done_at": NOON - 600},
        {"title": "Finished yesterday", "done": True, "done_at": NOON - 86400})), now=NOON)

    attention = by_title(brief["attention"])
    assert set(attention) == {"Late", "Today"}
    assert attention["Late"]["status"] == "overdue"
    assert attention["Late"]["detail"] == "Past its due date."
    assert attention["Today"]["status"] == "due_today"
    assert attention["Today"]["detail"] == "Due today."
    assert attention["Late"]["evidence"]["due"] == "2026-09-20"
    assert [row["title"] for row in brief["completed"]] == ["Finished today"]
    # An undated or later to-do is not a claim on today, and never a placeholder row.
    everything = json.dumps(brief)
    assert "Later" not in everything and "Someday" not in everything
    assert "Finished yesterday" not in everything
    assert brief["all_clear"] is False
    assert brief["counts"]["attention"] == 2
    assert "todos" in brief["coverage"]["ok"]


def test_the_readers_local_day_decides_what_is_late():
    rows = todo_rows({"title": "Due on the 23rd", "due": TODAY})
    east = db.build(payloads(todos=rows), now=NOON, timezone=dt.timezone(dt.timedelta(hours=14)))
    west = db.build(payloads(todos=rows), now=NOON, timezone=dt.timezone(dt.timedelta(hours=-11)))
    assert east["date"] == "2026-09-24" and east["attention"][0]["status"] == "overdue"
    assert west["date"] == TODAY and west["attention"][0]["status"] == "due_today"


def test_a_todo_is_the_persons_own_sentence_and_only_our_words_are_translated():
    brief = db.build(payloads(todos=todo_rows({"title": "Call the bank", "due": "2026-09-01"})),
                     now=NOON, language="zh")
    row = brief["attention"][0]
    assert row["title"] == "Call the bank"
    assert row["detail"] == "已过截止日期。"
    assert row["generated"] == ["detail"]
    assert "Call the bank · (已过截止日期。)" in db.render_text(brief)


def test_a_todo_row_links_to_the_brief_page_and_nothing_else():
    brief = db.build(payloads(todos=todo_rows({"id": "abc-123", "title": "Late", "due": "2026-09-01"})),
                     now=NOON)
    href = brief["attention"][0]["source_ref"]["href"]
    assert href == "/brief?todo=abc-123"
    assert db.safe_href(href) == href
    # An id the store would never write still cannot turn into a different target.
    odd = db.build(payloads(todos=todo_rows({"id": "x y/../z", "title": "Late", "due": "2026-09-01"})),
                   now=NOON)["attention"][0]["source_ref"]["href"]
    assert odd.startswith("/brief?todo=") and db.safe_href(odd) == odd


def test_a_todo_can_never_outrank_a_waiting_decision():
    brief = db.build(payloads(
        approvals={"approvals": [{"id": "a1", "title": "Approve the payment"}]},
        todos=todo_rows({"title": "Late", "due": "2026-09-01"})), now=NOON)
    assert [row["kind"] for row in brief["top"]] == ["approval", "todo"]


def test_an_unchanged_late_todo_is_demoted_after_three_days_not_dropped(root):
    rows = todo_rows({"title": "Late", "due": "2026-09-01"})
    for day in range(4):
        brief = db.build(payloads(todos=rows), now=NOON + day * 86400, state_dir=root)
    row = brief["attention"][0]
    assert row["stale"] is True and row["first_seen_date"] == TODAY


# --------------------------------------------------------------- the surface


def test_the_page_reads_the_whole_list_and_the_brief_reads_the_same_rows(root):
    store = todos.TodoStore(root)
    store.save({"title": "Late", "due": "2026-09-01"})
    store.save({"title": "Someday"})
    answer = web.read(root, now=NOON)
    assert [row["title"] for row in answer["todos"]["items"]] == ["Late", "Someday"]
    assert answer["todos"]["state"] == "ok"
    assert [row["title"] for row in answer["brief"]["attention"]] == ["Late"]
    report = {row["name"]: row for row in answer["sources"]}
    assert report["todos"]["state"] == "ok"


def test_an_unreadable_list_is_unavailable_and_never_an_empty_day(root):
    os.makedirs(os.path.join(root, "daily-brief"))
    with open(todos.path_for(root), "wb") as handle:
        handle.write(b"this is not a database, and it is at /private/somebodys/path")
    answer = web.read(root, now=NOON)
    report = {row["name"]: row for row in answer["sources"]}
    assert report["todos"]["state"] == "unavailable"
    assert report["todos"]["reason"] == "todos could not be read (DatabaseError)"
    assert answer["todos"]["state"] == "unavailable" and answer["todos"]["items"] == []
    assert answer["brief"]["all_clear"] is False
    assert "/private" not in json.dumps(answer)


def test_the_todo_action_saves_deletes_and_reports_a_stale_window(root):
    saved = web.todo(root, {"action": "save", "todo": {"title": "Book the dentist"}})
    assert saved["ok"] is True and saved["todo"]["revision"] == 1
    row = saved["todo"]
    assert web.todo(root, {"action": "save", "todo": dict(row, done=True)})["ok"] is True
    stale = web.todo(root, {"action": "save", "todo": dict(row, title="Stale")})
    assert stale == {"ok": False, "action": "save", "conflict": True,
                     "error": "This to-do changed in another window. Reload before saving."}
    assert web.todo(root, {"action": "delete", "todo": dict(row, revision=2)})["deleted"] is True
    for body in ({"action": "complete"}, {"todo": row}, "save"):
        with pytest.raises(db.BriefError):
            web.todo(root, body)
    with pytest.raises(todos.TodoError):
        web.todo(root, {"action": "save", "todo": {"title": ""}})


def test_hiding_a_todo_from_the_brief_is_not_completing_it(root):
    row = todos.TodoStore(root).save({"title": "Late", "due": "2026-09-01"})
    item = web.read(root, now=NOON)["brief"]["attention"][0]
    web.perform(root, {"action": "dismiss", "item_id": item["id"],
                       "fingerprint": item["fingerprint"]}, now=NOON)
    after = web.read(root, now=NOON)
    assert after["brief"]["attention"] == [] and after["feedback"]["count"] == 1
    assert after["todos"]["items"][0]["done"] is False
    # Moving the due date is a change, and a changed item comes back by itself.
    todos.TodoStore(root).save(dict(row, due="2026-09-02"))
    assert [r["title"] for r in web.read(root, now=NOON)["brief"]["attention"]] == ["Late"]


# --------------------------------------------------------------- the HTTP route


@pytest.fixture
def served(tmp_path, monkeypatch):
    from harness import webapp
    from harness.httpserver import ThreadingHTTPServer
    state = tmp_path / "live"
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], webapp.TOKEN, str(state)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def call(served, path, body=None, token=None):
    base, real, _ = served
    url = base + path + ("?token=" + (real if token is None else token))
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, json.loads(exc.read())


def test_the_todo_route_needs_the_token_and_says_why_it_refused(served):
    assert call(served, "/api/brief/todos", {"action": "save", "todo": {"title": "x"}},
                token="wrong")[0] == 403
    status, saved = call(served, "/api/brief/todos", {"action": "save",
                                                      "todo": {"title": "Water the plants"}})
    assert status == 200 and saved["todo"]["title"] == "Water the plants"
    status, stale = call(served, "/api/brief/todos", {"action": "save",
                                                      "todo": dict(saved["todo"], revision=7)})
    assert status == 409 and "another window" in stale["error"]
    status, bad = call(served, "/api/brief/todos", {"action": "save", "todo": {"title": ""}})
    assert status == 400 and bad["error"] == "Enter what the to-do is"
    assert call(served, "/api/brief/todos", {"action": "archive"})[0] == 400
    status, answer = call(served, "/api/brief")
    assert status == 200
    assert [row["title"] for row in answer["todos"]["items"]] == ["Water the plants"]


def test_the_link_a_todo_row_carries_is_a_page_the_server_serves(served):
    base, _, _ = served
    with urllib.request.urlopen(base + "/brief?todo=abc-123", timeout=20) as res:
        assert res.status == 200 and b"collie-token" in res.read()


def test_a_corrupt_list_is_never_overwritten_by_a_save(root):
    os.makedirs(os.path.join(root, "daily-brief"))
    with open(todos.path_for(root), "wb") as handle:
        handle.write(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        web.todo(root, {"action": "save", "todo": {"title": "x"}})
    with open(todos.path_for(root), "rb") as handle:
        assert handle.read() == b"not a database"
