"""Acknowledging a request and deleting its conversation, against each other.

Two writers, two files: acceptance appends to ``<sessions>/inbox/<id>.json`` and
deletion removes ``<sessions>/<id>.json``.  Nothing in the old code ordered them,
so "is anything waiting?" was a read another *process* could invalidate one
instruction later — and the answer to the person was ``{"accepted": true}`` for a
request whose conversation no longer existed.  Rechecking the queue before the
removal only narrows that window; an in-memory lock does not close it at all,
because the second surface is usually a different process.

Everything here goes over real HTTP against a real server, and every producer
that has to race the delete is a *separate OS process* doing its own POST.  The
only scheduling control is a barrier at the boundary under test; nothing is
mocked, no model is called, and the assertions are about what is on disk
afterwards.  Both orders are covered, and either outcome is allowed as long as it
is honest: refuse the late request, or keep the conversation.  Never both
accepted and deleted.
"""
import json
import os
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import (                                                    # noqa: E402
    input_assets, session_index, session_owner, sessions, task_inbox)
from test_web_task_inbox import (                                  # noqa: E402,F401
    CONFIG, PNG, _get, _hold_lease_in_another_process, _post, _upload, web)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A real second client: its own interpreter, its own socket, its own view of the
# store.  A thread would share this process's locks and prove much less.
_POST_SCRIPT = """
import json, sys, urllib.error, urllib.request
url, body = sys.argv[1], json.loads(sys.argv[2])
request = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'),
                                 method='POST',
                                 headers={'Content-Type': 'application/json'})
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        out = [response.status, json.loads(response.read())]
except urllib.error.HTTPError as exc:
    out = [exc.code, json.loads(exc.read())]
print('ANSWER ' + json.dumps(out), flush=True)
"""


def _post_from_another_process(base, token, path, body):
    """Start a real HTTP POST from a second process; return the live child."""
    return subprocess.Popen(
        [sys.executable, "-c", _POST_SCRIPT, base + path + "?token=" + token,
         json.dumps(body)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _answer(child, timeout=60):
    out, err = child.communicate(timeout=timeout)
    assert child.returncode == 0, err
    line = [row for row in out.splitlines() if row.startswith("ANSWER ")][0]
    return json.loads(line[len("ANSWER "):])


# A probe run in a process that never saw this conversation: exactly what a
# restarted server, a CLI or a second worker reads off the disk.
_PROBE_SCRIPT = """
import json, os, sys
sys.path.insert(0, {root!r})
os.environ['COLLIE_SESSIONS_DIR'] = {store!r}
from harness import sessions, task_inbox
out = {{}}
for name in {sessions!r}:
    row = {{'lifecycle': task_inbox.lifecycle(name),
            'journal': sessions.load(name) is not None,
            'entries': [{{'id': e['id'], 'state': e['state'], 'text': e['text']}}
                        for e in task_inbox.list_entries(name)]}}
    try:
        entry = task_inbox.enqueue(name, {entry_id!r}, {text!r}, mode='follow_up')
        row['enqueue'] = {{'accepted': True, 'state': entry['state']}}
    except task_inbox.SessionClosed as exc:
        row['enqueue'] = {{'accepted': False, 'refusal': 'SessionClosed',
                           'state': exc.state, 'error': str(exc)}}
    except task_inbox.InboxError as exc:
        row['enqueue'] = {{'accepted': False, 'refusal': type(exc).__name__,
                           'error': str(exc)}}
    out[name] = row
print('PROBE ' + json.dumps(out), flush=True)
"""


def _probe_in_a_fresh_process(state, session_ids, *, entry_id="late-request",
                              text="a request sent after the fact"):
    code = _PROBE_SCRIPT.format(root=ROOT, store=str(state / "sessions"),
                                sessions=list(session_ids), entry_id=entry_id, text=text)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=180)
    assert out.returncode == 0, out.stderr
    line = [row for row in out.stdout.splitlines() if row.startswith("PROBE ")][0]
    return json.loads(line[len("PROBE "):])


# A closer that dies where a crash actually hurts: after the conversation has
# been marked, with or without the journal still there.
_CRASH_SCRIPT = """
import os, sys
sys.path.insert(0, {root!r})
os.environ['COLLIE_SESSIONS_DIR'] = {store!r}
from harness import session_owner, sessions, task_inbox
session = {session!r}
lease = session_owner.acquire(session, label='crashing-delete')
task_inbox.begin_close(session, lease, discard={discard!r}, reason='session deleted')
if {remove!r}:
    assert sessions.delete(session)
print('CLOSING', flush=True)
os._exit(9)
"""


def _close_then_die(state, session, *, remove=False, discard=False):
    """Mark the conversation closing in another process, then kill it outright.

    With ``remove=False`` the journal is still there, so the delete never
    happened; with ``remove=True`` it did happen and only the final bookkeeping
    is missing.  Both leave a ``closing`` mark whose owner will never come back.
    """
    code = _CRASH_SCRIPT.format(root=ROOT, store=str(state / "sessions"),
                                session=session, remove=bool(remove),
                                discard=bool(discard))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=180)
    assert out.returncode == 9, out.stderr
    assert "CLOSING" in out.stdout, out.stdout


def _hold(target, attribute, sid, monkeypatch):
    """Stop the first caller at one boundary until the test releases it."""
    arrived, release = threading.Event(), threading.Event()
    original = getattr(target, attribute)

    def held(name, *args, **kwargs):
        if name == sid:
            arrived.set()
            assert release.wait(30), "barrier was never released"
        return original(name, *args, **kwargs)

    monkeypatch.setattr(target, attribute, held)
    return arrived, release


def _delete_in_a_thread(base, token, sid, query=""):
    answer = {}
    thread = threading.Thread(
        target=lambda: answer.update(
            zip(("code", "body"), _get(base, token, "/api/delete/" + sid + query))))
    thread.start()
    return thread, answer


def _body(sid, entry_id, text, route="/api/task-inbox"):
    out = {"session": sid, "id": entry_id, "text": text, "mode": "follow_up",
           "config": dict(CONFIG)}
    if route == "/api/steer":
        out["q"] = text
    return out


def _inbox_bytes(state, sid):
    path = state / "sessions" / "inbox" / (sid + ".json")
    return path.read_bytes() if path.exists() else b""


# ------------------------------------------------ the delete gets there first

@pytest.mark.parametrize("route", ["/api/task-inbox", "/api/steer"])
def test_another_process_is_refused_while_the_delete_is_in_flight(web, monkeypatch, route):
    """The window the fix is about: the journal is being removed *right now*.

    The second process gets an answer while the delete is still held, so the
    refusal is a decision and not a lock it is waiting behind — and the words it
    sent are nowhere on disk, because nothing was accepted.
    """
    base, token, state = web
    sid = "race-delete-first"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    text = "Keep this newly accepted instruction and its conversation."
    arrived, release = _hold(sessions, "delete", sid, monkeypatch)

    thread, deleted = _delete_in_a_thread(base, token, sid)
    try:
        assert arrived.wait(30), "the route never reached the disk deletion"
        child = _post_from_another_process(base, token, route,
                                           _body(sid, "late-one", text, route))
        code, answer = _answer(child, timeout=60)
    finally:
        release.set()
    thread.join(timeout=60)

    assert code == 409, answer
    assert "was not stored" in answer["error"]
    assert answer.get("accepted") is None and answer.get("queued") is not True
    assert deleted["code"] == 200 and deleted["body"] == {"ok": True, "canceled": []}
    assert sessions.load(sid) is None
    # Refused means refused: no entry, and not one byte of that text kept.
    assert task_inbox.list_entries(sid) == []
    assert text.encode("utf-8") not in _inbox_bytes(state, sid)
    assert task_inbox.lifecycle(sid)["state"] == "closed"

    seen = _probe_in_a_fresh_process(state, [sid])[sid]
    assert seen["lifecycle"]["state"] == "closed" and seen["journal"] is False
    assert seen["enqueue"] == {"accepted": False, "refusal": "SessionClosed",
                               "state": "closed", "error": seen["enqueue"]["error"]}


# ---------------------------------------------- the acceptance gets there first

@pytest.mark.parametrize("route", ["/api/task-inbox", "/api/steer"])
def test_a_request_accepted_first_keeps_the_conversation_it_was_sent_to(web, monkeypatch,
                                                                        route):
    """The other order, where the honest answer is to keep the conversation.

    The delete is stopped before it decides anything, a second process has its
    request accepted, and the delete must then see it.  This is the case a
    pre-check cannot get right on its own: the read happened before the write.
    """
    base, token, state = web
    sid = "race-accept-first"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    text = "Please also update the changelog before you stop."
    arrived, release = _hold(task_inbox, "begin_close", sid, monkeypatch)

    thread, deleted = _delete_in_a_thread(base, token, sid)
    try:
        assert arrived.wait(30), "the route never reached the close"
        child = _post_from_another_process(base, token, route,
                                           _body(sid, "in-time", text, route))
        code, answer = _answer(child, timeout=60)
    finally:
        release.set()
    thread.join(timeout=60)

    assert code == 200, answer
    assert answer.get("accepted") is True or answer.get("queued") is True
    assert answer["entry"]["text"] == text and answer["entry"]["state"] == "pending"
    # The conversation is still here, and so is the request that was accepted.
    assert deleted["code"] == 409, deleted
    assert deleted["body"]["ok"] is False and deleted["body"]["pending"] == 1
    assert [row["id"] for row in deleted["body"]["entries"]] == ["in-time"]
    assert sessions.load(sid) is not None
    entry = task_inbox.get(sid, "in-time")
    assert entry["state"] == "pending" and entry["text"] == text
    assert entry["config"]["frozen"]["provider"] == "mock"
    # A refused delete leaves no mark behind: the conversation is ordinary again.
    assert task_inbox.lifecycle(sid)["state"] == "open"
    code, again = _post(base, token, "/api/task-inbox", _body(sid, "another", "and this"))
    assert code == 200 and again["entry"]["state"] == "pending"


# ------------------------------------------------------ across a restart

def test_a_deleted_conversation_refuses_late_input_but_a_new_id_is_not_a_deletion(web):
    """The distinction that must survive the process: deleted vs. never created.

    A brand-new session id has no transcript either, and accepting its first
    request is exactly how a new conversation starts.  Only an id a delete
    actually finished on is closed.
    """
    base, token, state = web
    deleted, fresh = "restart-deleted", "restart-never-created"
    sessions.append_exchange(deleted, "hello", "hi", project="web", cwd=str(state))
    code, _ = _post(base, token, "/api/task-inbox", _body(deleted, "r1", "queued work"))
    assert code == 200

    code, gone = _get(base, token, "/api/delete/" + deleted + "?discard_pending=1")
    assert code == 200 and gone["ok"] is True and gone["canceled"] == ["r1"]

    seen = _probe_in_a_fresh_process(state, [deleted, fresh])
    assert seen[deleted]["lifecycle"]["state"] == "closed"
    assert seen[deleted]["journal"] is False
    assert seen[deleted]["enqueue"]["refusal"] == "SessionClosed"
    assert [row["state"] for row in seen[deleted]["entries"]] == ["canceled"]
    # ...and the id nothing ever created accepts its first request, from a
    # process that has never seen it, with no journal anywhere.
    assert seen[fresh]["lifecycle"] == {"session": fresh, "state": "open",
                                        "record": None, "journal": "missing"}
    assert seen[fresh]["enqueue"] == {"accepted": True, "state": "pending"}

    # The same two answers over HTTP, from the server that did the delete.
    code, refused = _post(base, token, "/api/task-inbox",
                          _body(deleted, "after-the-fact", "one more thing"))
    assert code == 409 and "was deleted" in refused["error"]
    assert task_inbox.get(deleted, "after-the-fact") is None
    code, accepted = _post(base, token, "/api/task-inbox",
                           _body("restart-brand-new", "first", "start something new"))
    assert code == 200 and accepted["entry"]["state"] == "pending"

    code, listing = _get(base, token, "/api/task-inbox?session=" + deleted)
    assert code == 200 and listing["lifecycle"] == "closed" and listing["journal"] == "missing"
    code, listing = _get(base, token, "/api/task-inbox?session=restart-brand-new")
    assert code == 200 and listing["lifecycle"] == "open" and listing["journal"] == "missing"


# --------------------------------------------------------- the delete fails

def test_a_delete_that_could_not_remove_the_journal_leaves_the_inbox_open(web, monkeypatch):
    """Rollback: the mark exists to protect a removal that happened.

    If the journal is still there, the conversation is still there, and it must
    go on accepting work.  What does *not* roll back is the discard: a request
    the person was told had been withdrawn stays withdrawn, with its reason.
    """
    base, token, state = web
    sid = "delete-fails"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    code, _ = _post(base, token, "/api/task-inbox", _body(sid, "r1", "queued work"))
    assert code == 200
    monkeypatch.setattr(sessions, "delete", lambda name: False)

    code, out = _get(base, token, "/api/delete/" + sid + "?discard_pending=1")
    assert code == 200 and out["ok"] is False and out["canceled"] == ["r1"]
    assert sessions.load(sid) is not None
    assert task_inbox.lifecycle(sid)["state"] == "open"
    assert task_inbox.get(sid, "r1")["state"] == "canceled"
    assert task_inbox.get(sid, "r1")["canceled"]["reason"] == "session deleted"

    code, accepted = _post(base, token, "/api/task-inbox", _body(sid, "r2", "carry on"))
    assert code == 200 and accepted["entry"]["state"] == "pending"
    assert _probe_in_a_fresh_process(state, [sid])[sid]["lifecycle"]["state"] == "open"


# ------------------------------------------------- the closer does not survive

def test_a_closer_that_died_with_the_journal_intact_does_not_silence_the_conversation(web):
    """A crash mid-delete must not leave a conversation nobody can write to.

    The mark is authoritative only while the lease that wrote it is held, and the
    kernel releases that lease when its holder dies.  The journal then decides:
    it is still here, so the delete never happened and the conversation is open.
    """
    base, token, state = web
    sid = "closer-crashed"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    _close_then_die(state, sid, remove=False)
    assert sessions.load(sid) is not None

    code, accepted = _post(base, token, "/api/task-inbox",
                           _body(sid, "after-crash", "the conversation is still here"))
    assert code == 200 and accepted["entry"]["state"] == "pending"
    assert task_inbox.lifecycle(sid)["state"] == "open"
    assert task_inbox.get(sid, "after-crash")["text"] == "the conversation is still here"
    # And the delete this conversation still deserves works normally afterwards.
    code, gone = _get(base, token, "/api/delete/" + sid + "?discard_pending=1")
    assert code == 200 and gone["ok"] is True and gone["canceled"] == ["after-crash"]


def test_a_closer_that_died_after_removing_the_journal_still_ends_as_deleted(web):
    """The same crash on the other side of the removal, settled the same way.

    Nothing is left to run the request against, so a late acknowledgement would
    be the orphan this whole protocol exists to prevent — and the answer has to
    hold in a process that was not there when it happened.
    """
    base, token, state = web
    sid = "closer-crashed-after"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    _close_then_die(state, sid, remove=True)
    assert sessions.load(sid) is None

    code, refused = _post(base, token, "/api/task-inbox",
                          _body(sid, "after-crash", "too late for this one"))
    assert code == 409 and "was deleted" in refused["error"]
    assert task_inbox.get(sid, "after-crash") is None
    assert task_inbox.lifecycle(sid)["state"] == "closed"
    seen = _probe_in_a_fresh_process(state, [sid])[sid]
    assert seen["lifecycle"]["state"] == "closed"
    assert seen["enqueue"]["refusal"] == "SessionClosed"


def test_an_abandoned_mark_never_outranks_the_live_agent_that_holds_the_lease(web):
    """The refusal must not degenerate into "somebody is busy, so no input".

    A live run holds the run lease for as long as it runs.  If a stale mark from
    a crashed delete were read as "a delete is in progress" merely because the
    lease is taken, steering a running agent would be impossible — which is the
    shortcut this fix is not allowed to take.  Identity, not busyness: the lease
    is held by a *different* owner than the one that wrote the mark.
    """
    base, token, state = web
    sid = "stale-mark-live-run"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    _close_then_die(state, sid, remove=False)
    stored = json.loads(_inbox_bytes(state, sid).decode("utf-8"))
    assert stored["lifecycle"]["state"] == "closing", "the mark really is on disk"

    child = _hold_lease_in_another_process(state, sid)
    try:
        assert session_owner.probe_busy(sid) is True
        code, steered = _post(base, token, "/api/steer",
                              _body(sid, "steer-1", "change of plan: run the tests first",
                                    "/api/steer"))
        assert code == 200 and steered["queued"] is True, steered
        assert steered["entry"]["state"] == "pending"
        # Deleting it *is* refused while that run owns the session.
        code, busy = _get(base, token, "/api/delete/" + sid + "?discard_pending=1")
        assert code == 409 and "running" in busy["error"]
    finally:
        child.terminate(); child.wait(timeout=60)
    assert task_inbox.get(sid, "steer-1")["text"].startswith("change of plan")


def test_steering_a_live_run_is_still_accepted_with_no_mark_anywhere(web):
    """The plain case, kept as a guard: holding the lease is not a reason to refuse."""
    base, token, state = web
    sid = "live-run-steering"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    child = _hold_lease_in_another_process(state, sid)
    try:
        code, steered = _post(base, token, "/api/steer",
                              _body(sid, "steer-live", "use the staging database",
                                    "/api/steer"))
        assert code == 200 and steered["queued"] is True
    finally:
        child.terminate(); child.wait(timeout=60)
    assert task_inbox.get(sid, "steer-live")["state"] == "pending"


# ------------------------------------------------------------ receipts and assets

def test_a_retried_id_is_answered_with_the_record_that_exists_not_a_blanket_refusal(web):
    """A closed conversation refuses *new* input.  It does not rewrite history.

    The browser that retries a POST it never saw the answer to is asking "what
    happened to this request?", and the truthful answer after a discard is "it
    was accepted and then cancelled" — not "you never sent it", and not a second
    acceptance.
    """
    base, token, state = web
    sid = "receipts-after-delete"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    body = _body(sid, "r1", "the request that was queued")
    code, first = _post(base, token, "/api/task-inbox", body)
    assert code == 200 and first["entry"]["state"] == "pending"

    code, gone = _get(base, token, "/api/delete/" + sid + "?discard_pending=1")
    assert code == 200 and gone["canceled"] == ["r1"]

    code, receipt = _post(base, token, "/api/task-inbox", body)
    assert code == 200 and receipt["entry"]["duplicate"] is True
    assert receipt["entry"]["state"] == "canceled"
    assert receipt["entry"]["text"] == body["text"]
    assert receipt["entry"]["digest"] == first["entry"]["digest"]

    # The same id carrying different words is still a conflict, never an overwrite.
    code, conflict = _post(base, token, "/api/task-inbox",
                           dict(body, text="something else entirely"))
    assert code == 409 and "different message" in conflict["error"]
    assert task_inbox.get(sid, "r1")["text"] == body["text"]

    # A genuinely new request is the one thing the closed conversation refuses.
    code, refused = _post(base, token, "/api/task-inbox", _body(sid, "r2", "and one more"))
    assert code == 409 and "was deleted" in refused["error"]


def test_a_refused_delete_leaves_the_accepted_configuration_and_attachments_intact(web):
    """Refusing is not touching: the request keeps everything it was accepted with."""
    base, token, state = web
    sid = "refused-delete-assets"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    body = dict(_body(sid, "with-assets", "what is wrong in this screenshot?"),
                images=[_upload(base, token)],
                contexts=[{"kind": "file", "path": "README.md", "content": "# hi"}])
    code, accepted = _post(base, token, "/api/task-inbox", body)
    assert code == 200 and accepted["entry"]["assets"]["images"] == 1

    code, refused = _get(base, token, "/api/delete/" + sid)
    assert code == 409 and refused["pending"] == 1
    assert [row["id"] for row in refused["entries"]] == ["with-assets"]

    entry = task_inbox.get(sid, "with-assets")
    assert entry["state"] == "pending" and entry["text"] == body["text"]
    assert entry["config"] == accepted["entry"]["config"]
    bundle = input_assets.load(sid, entry["metadata"]["assets"])
    assert bundle["images"] == [{"media_type": "image/png", "data": PNG}]
    assert [item.get("path") for item in bundle["contexts"]] == ["README.md"]
    assert sessions.load(sid) is not None
    assert task_inbox.lifecycle(sid)["state"] == "open"


def test_a_claim_no_executor_can_answer_for_blocks_the_delete_and_keeps_the_journal(web):
    """Cancellation failure keeps the journal — the accepted-40 rule, unchanged.

    A claimed request cannot be withdrawn, and the transcript is the only thing
    that can ever say what became of it.  Deleting it would trade a recoverable
    conversation for a record nobody can settle.
    """
    from test_web_task_inbox import _claim_then_die, _queue

    base, token, state = web
    sid = "claim-blocks-delete"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    _queue(base, token, sid, "req-1", "finish the migration")
    assert _claim_then_die(state, sid) == ["req-1"]

    # The journal cannot decide it (nothing was journaled) and reconcile hands it
    # back to pending, so the delete is refused for the ordinary reason.
    code, refused = _get(base, token, "/api/delete/" + sid)
    assert code == 409 and refused["pending"] == 1
    assert sessions.load(sid) is not None
    assert task_inbox.lifecycle(sid)["state"] == "open"

    # Now make it genuinely unwithdrawable: claimed, with the journal unreadable,
    # so nothing may settle it and the discard cannot cancel it either.
    assert _claim_then_die(state, sid) == ["req-1"]
    journal = state / "sessions" / (sid + ".json")
    journal.write_text("{ this is not json", encoding="utf-8")
    code, blocked = _get(base, token, "/api/delete/" + sid + "?discard_pending=1")
    assert code == 409 and blocked["ok"] is False and blocked["pending"] == 1
    assert "could not be withdrawn" in blocked["error"]
    assert journal.exists(), "the only evidence about that request is still here"
    assert task_inbox.get(sid, "req-1")["state"] == "claimed"
    assert task_inbox.lifecycle(sid)["state"] == "open"


# ------------------------------------------- the delete fails *after* the unlink

def _fail_the_index_after_the_unlink(monkeypatch, state, sid):
    """Break the bookkeeping ``sessions.delete`` runs once the journal is gone.

    This is the real shape of the bug: ``delete`` catches the ``OSError``, returns
    ``False``, and every step above it is told the deletion did not happen — while
    the transcript is already off the disk.
    """
    reached, original = [], session_index.forget

    def failing_forget(directory, session):
        if session == sid:
            reached.append(not (state / "sessions" / (sid + ".json")).exists())
            raise OSError("injected index write failure after the journal unlink")
        return original(directory, session)

    monkeypatch.setattr(session_index, "forget", failing_forget)
    return reached


@pytest.mark.parametrize("route", ["/api/task-inbox", "/api/steer"])
def test_a_delete_that_fails_after_unlinking_the_journal_still_refuses_new_input(
        web, monkeypatch, route):
    """A false ``False`` must not resurrect a conversation that is already gone.

    The tombstone may not be decided by what the delete *returned*, because that
    answer covers work done after the transcript was removed.  With nothing left
    to run against, accepting the next request would be exactly the orphaned
    acknowledgement this protocol exists to prevent — and the refusal has to
    survive into a process that never saw the delete.
    """
    base, token, state = web
    sid = "post-unlink-" + route.rsplit("/", 1)[-1]
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    reached = _fail_the_index_after_the_unlink(monkeypatch, state, sid)

    code, out = _get(base, token, "/api/delete/" + sid)
    assert reached == [True], "the failure never reached the post-unlink boundary"
    assert sessions.load(sid) is None, "the journal really is gone"
    # The receipt is honest in both directions: the delete did not succeed, and it
    # does not pretend the conversation was kept.
    assert code == 500 and out["ok"] is False and out["deleted"] is True
    assert "removed" in out["error"] and "stays deleted" in out["error"]

    text = "this must not be accepted into a conversation that is gone"
    code, refused = _post(base, token, route, _body(sid, "late-input", text, route))
    assert code == 409 and "was deleted" in refused["error"]
    assert refused.get("accepted") is None and refused.get("queued") is not True
    assert task_inbox.get(sid, "late-input") is None
    assert task_inbox.list_entries(sid) == []
    assert text.encode("utf-8") not in _inbox_bytes(state, sid)
    assert task_inbox.lifecycle(sid)["state"] == "closed"

    seen = _probe_in_a_fresh_process(state, [sid])[sid]
    assert seen["journal"] is False and seen["lifecycle"]["state"] == "closed"
    assert seen["enqueue"] == {"accepted": False, "refusal": "SessionClosed",
                               "state": "closed", "error": seen["enqueue"]["error"]}


def test_a_delete_that_raises_after_removing_the_journal_ends_as_deleted(web, monkeypatch):
    """The same boundary reached by an exception instead of a ``False``.

    Unwinding is not evidence that nothing happened either, so the error path
    settles the mark from the transcript exactly like the ordinary one.
    """
    base, token, state = web
    sid = "post-unlink-raises"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    journal = state / "sessions" / (sid + ".json")

    def unlink_then_raise(name):
        if name == sid:
            os.remove(journal)
            raise OSError("injected failure after the journal unlink")
        return sessions.delete(name)

    monkeypatch.setattr(sessions, "delete", unlink_then_raise)
    code, out = _get(base, token, "/api/delete/" + sid)
    assert not journal.exists()
    assert code == 500 and out["ok"] is False and out["deleted"] is True
    assert "stays deleted" in out["error"]

    code, refused = _post(base, token, "/api/task-inbox",
                          _body(sid, "after-the-raise", "too late for this one"))
    assert code == 409 and "was deleted" in refused["error"]
    assert task_inbox.get(sid, "after-the-raise") is None
    assert task_inbox.lifecycle(sid)["state"] == "closed"
    assert _probe_in_a_fresh_process(state, [sid])[sid]["enqueue"]["refusal"] == "SessionClosed"


def test_a_close_whose_final_mark_cannot_be_written_is_settled_by_the_journal(web,
                                                                             monkeypatch):
    """The third failure order: the journal goes, and neither mark can be stored.

    What is left on disk is the ``closing`` mark written before the removal.  It
    outlives the process, and the next reader settles it from the same two facts
    — a transcript existed when the close began, and none exists now — so a
    restarted server still refuses input for a conversation that is gone.
    """
    base, token, state = web
    sid = "unwritable-final-mark"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))

    def unwritable(*args, **kwargs):
        raise OSError("injected failure writing the final close mark")

    monkeypatch.setattr(task_inbox, "finish_close", unwritable)
    monkeypatch.setattr(task_inbox, "abort_close", unwritable)
    code, out = _get(base, token, "/api/delete/" + sid)
    assert code == 200 and out["ok"] is True
    assert sessions.load(sid) is None

    stored = json.loads(_inbox_bytes(state, sid).decode("utf-8"))
    assert stored["lifecycle"]["state"] == "closing", "the only mark on disk"
    assert stored["lifecycle"]["journal"] is True, "a transcript existed when it began"
    seen = _probe_in_a_fresh_process(state, [sid])[sid]
    assert seen["journal"] is False and seen["lifecycle"]["state"] == "closed"
    assert seen["enqueue"]["refusal"] == "SessionClosed"
    code, refused = _post(base, token, "/api/task-inbox",
                          _body(sid, "after-restart", "one more thing"))
    assert code == 409 and "was deleted" in refused["error"]


def test_deleting_an_id_that_never_had_a_journal_leaves_it_open_for_its_first_request(web):
    """The distinction the journal fact exists for, from the other side.

    "No transcript now" is the state of a deleted conversation *and* of one that
    was never started, so a close that finds nothing to remove must not leave a
    tombstone — the next request is somebody beginning a new conversation.
    """
    base, token, state = web
    sid = "never-created-then-deleted"
    assert sessions.load(sid) is None

    code, out = _get(base, token, "/api/delete/" + sid)
    assert code == 200 and out == {"ok": False, "canceled": []}
    assert task_inbox.lifecycle(sid) == {"session": sid, "state": "open",
                                         "record": None, "journal": "missing"}

    code, accepted = _post(base, token, "/api/task-inbox",
                           _body(sid, "first", "start something new"))
    assert code == 200 and accepted["entry"]["state"] == "pending"
    seen = _probe_in_a_fresh_process(state, [sid], entry_id="second")[sid]
    assert seen["lifecycle"]["state"] == "open"
    assert seen["enqueue"] == {"accepted": True, "state": "pending"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
