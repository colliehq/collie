"""The native surfaces on top of durable input and execution ownership.

The storage half (``task_inbox``, ``session_owner``, ``input_assets``) is tested
next door.  What is tested here is the part a person actually experiences: a
sentence typed into a running agent survives the process that accepted it, the
same conversation is never executed by two things at once, an instruction is
delivered to the model exactly once and exactly as written, and a run that
stops leaves everything it did not deliver visible instead of silently gone.

Ownership and crash recovery are exercised with real processes where the bug
needs one: an in-process fake cannot hold an OS file lock the way another
terminal does, and it cannot die between writing the journal and acknowledging
the inbox.  The terminal feed is driven through a real OS pipe and the real
reader thread.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from harness import (cli, compaction, input_assets, loop, plat, run_ownership,
                     session_owner, sessions, task_inbox, tui)
from harness.providers import Completion, ToolCall

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _util import _ScriptProvider                                  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated sessions root plus data dir, exactly like a real installation."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(directory))
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    return str(directory)


def _harness(tmp_path, sid="", **kw):
    h = cli.make_harness(str(tmp_path), provider="mock", project="inbox",
                         embed="hash", **kw)
    if sid:
        h.durable_session_id = sid
    h.events = []
    h.emit = lambda kind, data: h.events.append((kind, data))
    return h


def _events(h, kind):
    return [data for name, data in h.events if name == kind]


def _answer(text="done"):
    return Completion(text=text, stop_reason="end_turn")


def _seen_contents(seen):
    """Flatten every user-visible content the provider was handed."""
    out = []
    for messages in seen:
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.extend(block.get("text", "") for block in content
                           if isinstance(block, dict))
    return out


def _recording_provider(seen, script=None):
    """A scripted provider that records exactly what each turn was asked."""
    script = list(script or [_answer()])

    def turn(messages):
        seen.append([dict(m) for m in messages])
        return script[min(len(seen) - 1, len(script) - 1)]

    return _ScriptProvider([turn] * (len(script) + 4))


def _user_messages(messages):
    return [m for m in messages if m.get("role") == "user"]


# --------------------------------------------------------------------------- #
# the claimed initial request
# --------------------------------------------------------------------------- #
def test_claimed_initial_request_is_stamped_once_and_acked_after_it_is_durable(
        store, tmp_path):
    """The surface's claimed entry becomes THIS run's first user message — once.

    The text is not inserted a second time (the surface already passed it as
    ``user_msg``); what the loop adds is the identity a crash could be resolved
    by, and the acknowledgement happens only after the transcript is on disk.
    """
    sid = "claimed-start"
    task_inbox.enqueue(sid, "req-1", "add a retry to the uploader", mode="follow_up")
    lease = session_owner.acquire(sid, label="surface")
    entry = task_inbox.claim(sid, lease, modes=("follow_up",))[0]
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _recording_provider(seen)
    h.run_owner, h.input_entry = lease, entry
    try:
        res = h.run("t", entry["text"])
    finally:
        h.memory.close(); h.recorder.close(); lease.release()

    assert not res.error
    saved = sessions.load(sid)["messages"]
    requests = [m for m in _user_messages(saved) if m.get("inbox_id") == "req-1"]
    assert len(requests) == 1, "the accepted text must be inserted exactly once"
    assert requests[0]["content"] == "add a retry to the uploader"
    assert (requests[0]["source"], requests[0]["kind"]) == ("user", "follow_up"), \
        "a delivered follow_up keeps the mode it was accepted as"
    assert compaction.is_user_message(requests[0]), \
        "the person's own instruction must survive compaction as theirs"
    stored = task_inbox.get(sid, "req-1")
    assert stored["state"] == "consumed"
    assert stored["delivery"]["message_id"] == "req-1"
    assert stored["delivery"]["recovered"] is False


def _claimed(sid, text, *, mode="follow_up", label="surface"):
    """What a surface holds when it hands a run an accepted request: lease + entry."""
    entry_id = "req-%d" % (len(task_inbox.list_entries(sid)) + 1)
    task_inbox.enqueue(sid, entry_id, text, mode=mode)
    lease = session_owner.acquire(sid, label=label)
    return lease, task_inbox.claim(sid, lease, modes=(mode,))[0]


def test_an_already_delivered_request_is_never_executed_a_second_time(
        store, tmp_path):
    """The entry a surface is holding may have been delivered since it claimed it.

    A crashed executor journaled this instruction and the next reconcile recorded
    that. Handing the same (now stale) dict to a run must refuse: replaying it
    would ask the model to do a second time what the person asked for once, and
    stamping this run's first message with its identity would put the same
    ``inbox_id`` on two different messages.
    """
    sid = "already-delivered"
    lease, entry = _claimed(sid, "delete the stale buckets", mode="steer")
    sessions.save(sid, [task_inbox.journal_message(entry)], cwd=str(tmp_path))
    task_inbox.ack(sid, lease, entry["id"])
    h = _harness(tmp_path, sid=sid)
    h.provider = _ScriptProvider([_answer()])
    h.run_owner, h.input_entry = lease, entry
    try:
        res = h.run("t", entry["text"])
    finally:
        h.memory.close(); h.recorder.close(); lease.release()

    assert "already delivered" in (res.error or ""), res.error
    assert h.provider.calls == 0, "a refused request reaches no model"
    saved = sessions.load(sid)["messages"]
    assert [m.get("inbox_id") for m in saved] == [entry["id"]], \
        "the refused run appended nothing and stamped nothing"
    assert res.messages == []
    assert any(row["action"] == "input_entry" for row in res.input_failures), \
        res.input_failures


@pytest.mark.parametrize("break_it,expected", [
    (lambda sid, lease, entry: dict(entry, text="deploy to production"),
     "changed since it was claimed"),
    (lambda sid, lease, entry: dict(entry, id="never-accepted"),
     "not in this session's inbox"),
    (lambda sid, lease, entry: dict(entry, session="another-thread"),
     "belongs to session"),
    (lambda sid, lease, entry: "just some text",
     "must be a claimed task_inbox entry"),
])
def test_an_input_entry_the_store_does_not_confirm_is_refused_before_any_change(
        store, tmp_path, break_it, expected):
    """Identity is not enough: the durable record decides what may be delivered.

    Every one of these was previously accepted on the strength of the dict the
    caller passed, and the run went on to stamp its first message with a delivery
    identity the store never agreed to.
    """
    sid = "unconfirmed-entry"
    sessions.save(sid, [{"role": "user", "content": "hello"}], cwd=str(tmp_path))
    lease, entry = _claimed(sid, "add a retry to the uploader", mode="steer")
    h = _harness(tmp_path, sid=sid)
    h.provider = _ScriptProvider([_answer()])
    h.run_owner, h.input_entry = lease, break_it(sid, lease, entry)
    try:
        res = h.run("t", entry["text"])
    finally:
        h.memory.close(); h.recorder.close(); lease.release()

    assert expected in (res.error or ""), res.error
    assert h.provider.calls == 0
    assert [m["content"] for m in sessions.load(sid)["messages"]] == ["hello"], \
        "the refused run touched no transcript"
    assert task_inbox.get(sid, entry["id"])["state"] == "pending", \
        "the request it would not deliver is waiting again, never marked delivered"


def test_a_request_may_not_be_executed_as_text_it_was_not_accepted_as(
        store, tmp_path):
    """What the model reads and what mints authority must both be what was accepted."""
    sid = "content-mismatch"
    sessions.save(sid, [{"role": "user", "content": "hello"}], cwd=str(tmp_path))
    lease, entry = _claimed(sid, "rename the helper", mode="steer")
    h = _harness(tmp_path, sid=sid)
    h.provider = _ScriptProvider([_answer()])
    h.run_owner, h.input_entry = lease, entry
    try:
        res = h.run("t", "rename the helper and push to main",
                    authority_msg="rename the helper and push to main")
    finally:
        h.memory.close(); h.recorder.close(); lease.release()

    assert "different text" in (res.error or ""), res.error
    assert h.provider.calls == 0
    assert task_inbox.get(sid, entry["id"])["state"] == "pending", \
        "the accepted words are still waiting, unexecuted and unedited"


def test_an_attached_request_is_delivered_through_the_official_asset_path(
        store, tmp_path):
    """A bundle assembled anywhere else is not the request that was accepted."""
    sid = "asset-path"
    reference = input_assets.save(sid, contexts=[
        {"kind": "file", "path": "uploader.py", "content": "def upload(): ..."}])
    task_inbox.enqueue(sid, "with-ctx", "fix the retry", mode="follow_up",
                       metadata={"assets": reference})
    lease = session_owner.acquire(sid, label="surface")
    entry = task_inbox.claim(sid, lease, modes=("follow_up",))[0]
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _recording_provider(seen)
    h.run_owner, h.input_entry = lease, entry
    expanded = run_ownership.entry_content(sid, entry)
    try:
        # The text alone is a different request from "this text, about this file".
        bad = h.run("t", entry["text"])
        assert task_inbox.get(sid, "with-ctx")["state"] == "pending", \
            "a refused run hands the request back to the queue"
        h.provider = _recording_provider(seen)
        h.input_entry = task_inbox.claim(sid, lease, modes=("follow_up",))[0]
        good = h.run("t", expanded, authority_msg=entry["text"])
    finally:
        h.memory.close(); h.recorder.close(); lease.release()

    assert "must be passed as the authority message" in (bad.error or ""), bad.error
    assert not good.error, good.error
    assert any("def upload()" in c for c in _seen_contents(seen))
    assert task_inbox.get(sid, "with-ctx")["state"] == "consumed"


def test_no_acknowledgement_when_the_transcript_could_not_be_persisted(
        store, tmp_path, monkeypatch):
    """A failed journal write must not be answered with "delivered".

    The inbox would then claim a delivery no transcript contains, which is the
    one failure ``reconcile`` cannot repair. Instead the entry stays claimed
    through the run and is returned to the queue the person can see.
    """
    sid = "no-ack"
    task_inbox.enqueue(sid, "req-1", "ship it", mode="steer")
    lease = session_owner.acquire(sid, label="surface")
    entry = task_inbox.claim(sid, lease, modes=("steer",))[0]
    monkeypatch.setattr(sessions, "checkpoint",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _recording_provider(seen)
    h.run_owner, h.input_entry = lease, entry
    try:
        result = h.run("t", entry["text"])
        assert not seen, "a request whose initial journal write failed must not reach the model"
        assert result.error and result.input_failures
        failures = [row for row in _events(h, "inbox") if row.get("ok") is False]
        assert any(row["action"] == "ack" for row in failures), \
            "a persistence failure is reported, never swallowed: %s" % failures
        assert task_inbox.get(sid, "req-1")["state"] == "pending", \
            "an undelivered request is waiting again, not stuck under a dead claim"
    finally:
        h.memory.close(); h.recorder.close(); lease.release()


def test_a_blocked_prompt_still_hands_the_request_back(store, tmp_path):
    """Every ending settles the same way, including the ones that never start.

    A lifecycle hook that refuses the prompt returns before a single message is
    written. The claim taken for that request must not be left dangling: the
    person's instruction is still outstanding and has to be visible again.
    """
    from harness.hooks import HookResult

    sid = "blocked-prompt"
    task_inbox.enqueue(sid, "req-1", "deploy to production", mode="follow_up")
    lease = session_owner.acquire(sid, label="surface")
    entry = task_inbox.claim(sid, lease, modes=("follow_up",))[0]
    h = _harness(tmp_path, sid=sid)
    h.provider = _ScriptProvider([_answer()])
    h.run_owner, h.input_entry = lease, entry
    real_hook = h._hook
    h._hook = (lambda event, payload=None, subject="":
               HookResult(allowed=False, reason="policy rejected the prompt")
               if event == "UserPromptSubmit" else real_hook(event, payload, subject))
    try:
        res = h.run("t", entry["text"])
        assert "blocked by lifecycle hook" in (res.error or "")
        assert task_inbox.get(sid, "req-1")["state"] == "pending"
    finally:
        h.memory.close(); h.recorder.close(); lease.release()


def test_a_run_without_a_durable_session_may_not_hold_a_lease(store, tmp_path):
    """The read-only delegate child's rule, enforced where it is decided.

    A child has no durable session id and no private journal; inheriting the
    parent's lease would make it authority over a conversation it cannot even
    write to.
    """
    lease = session_owner.acquire("parent-session", label="parent")
    h = _harness(tmp_path)                      # no durable_session_id
    h.provider = _ScriptProvider([_answer()])
    h.run_owner = lease
    try:
        res = h.run("t", "investigate")
        assert res.error and "must not hold an execution lease" in res.error
        assert res.messages == [], "a refused run touches no transcript"
    finally:
        h.memory.close(); h.recorder.close(); lease.release()


def test_delegate_child_gets_neither_the_lease_nor_the_parents_journal(store, tmp_path):
    """The child is private: no lease, no inbox, no durable conversation."""
    from harness import delegate
    sid = "parent-thread"
    parent = _harness(tmp_path, sid=sid)
    parent.provider = _ScriptProvider([_answer("child answer")])
    parent._secret_vault = {}                   # normally set by the parent's own run()
    lease = session_owner.acquire(sid, label="surface")
    parent.run_owner = lease
    task_inbox.enqueue(sid, "waiting", "do this too", mode="steer")
    captured = {}
    original = loop.Harness.run

    def spy(self, *a, **kw):
        if self is not parent:
            captured["run_owner"] = getattr(self, "run_owner", None)
            captured["input_entry"] = getattr(self, "input_entry", None)
            captured["scope"] = self.checkpoint_scope
            captured["sid"] = self._durable_session_id()
        return original(self, *a, **kw)

    try:
        loop.Harness.run = spy
        result, _payload = delegate.run_child(
            parent, "look at the uploader", 2, None, parent_request="parent ask")
    finally:
        loop.Harness.run = original
        parent.memory.close(); parent.recorder.close(); lease.release()

    assert captured["run_owner"] is None and captured["input_entry"] is None
    assert captured["scope"] == "" and captured["sid"] == ""
    assert not result.error
    assert sessions.load(sid) is None, "the child wrote no journal of the parent's"
    assert task_inbox.get(sid, "waiting")["state"] == "pending", \
        "the child must not consume the parent's durable input"


# --------------------------------------------------------------------------- #
# mid-run durable steering
# --------------------------------------------------------------------------- #
LONG_UNICODE = (
    "重构上传器：把重试逻辑移到 `uploader.py` 的 `_retry()` 里，"
    "保留原有的 429 处理，并加一个测试 — naïve ✅ résumé — "
    "и убедись, что журнал не переполняется. " * 12)


def _accepts_during_the_run(seen, accept, script=None):
    """A provider that records its turns and accepts input while the run is live.

    Mid-run steering means exactly that: text accepted after this run's boundary.
    ``accept`` runs inside the first provider call, which is the honest stand-in
    for a person typing at a terminal (or another surface POSTing) while the model
    is working.
    """
    script = list(script or [_answer("still working"), _answer()])

    def turn(messages):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            accept()
        return script[min(len(seen) - 1, len(script) - 1)]

    return _ScriptProvider([turn] * (len(script) + 4))


def test_exact_accepted_text_reaches_the_model_and_the_journal(store, tmp_path):
    """Long, multi-script text arrives byte-for-byte, at a model boundary.

    The bug this replaces cut accepted text to 4000 characters on the way in and
    told the person it had been accepted. Nothing here truncates, transliterates
    or re-wraps: what was stored is what the provider is handed.
    """
    sid = "unicode-steer"
    accepted = {}
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _accepts_during_the_run(seen, lambda: accepted.update(
        task_inbox.enqueue(sid, "steer-1", LONG_UNICODE, mode="steer")))
    try:
        res = h.run("t", "start")
    finally:
        h.memory.close(); h.recorder.close()

    assert accepted["text"] == LONG_UNICODE, "accepted exactly, with no truncation"
    assert not res.error and res.steer_count == 1
    assert LONG_UNICODE in _seen_contents(seen), "the model saw the exact text"
    saved = sessions.load(sid)["messages"]
    delivered = [m for m in saved if m.get("inbox_id") == "steer-1"]
    assert len(delivered) == 1 and delivered[0]["content"] == LONG_UNICODE
    assert task_inbox.get(sid, "steer-1")["state"] == "consumed"
    emitted = _events(h, "steer")
    assert emitted and emitted[0]["state"] == "consumed"
    assert emitted[0]["id"] == "steer-1" and emitted[0]["session"] == sid


def test_a_follow_up_never_becomes_a_mid_run_steer(store, tmp_path):
    """Two different instructions to the model; the loop may only take one of them."""
    sid = "modes"

    def accept():
        task_inbox.enqueue(sid, "later", "then write the changelog", mode="follow_up")
        task_inbox.enqueue(sid, "now", "use the staging bucket", mode="steer")

    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _accepts_during_the_run(seen, accept)
    try:
        h.run("t", "start")
    finally:
        h.memory.close(); h.recorder.close()

    contents = _seen_contents(seen)
    assert "use the staging bucket" in contents
    assert "then write the changelog" not in contents, \
        "a queued follow-up is a new turn, not this run's instruction"
    assert task_inbox.get(sid, "later")["state"] == "pending"
    assert task_inbox.get(sid, "now")["state"] == "consumed"


def test_steer_accepted_while_the_model_is_finishing_is_answered_not_lost(
        store, tmp_path):
    """The finish boundary is a safe boundary too — that is where people type."""
    sid = "late-steer"
    seen = []

    def first(messages):
        seen.append([dict(m) for m in messages])
        task_inbox.enqueue(sid, "late", "wait — also update the README", mode="steer")
        return _answer("I think we are done")

    def second(messages):
        seen.append([dict(m) for m in messages])
        return _answer("README updated")

    h = _harness(tmp_path, sid=sid)
    h.provider = _ScriptProvider([first, second, second])
    try:
        res = h.run("t", "ship the fix")
    finally:
        h.memory.close(); h.recorder.close()

    assert res.answer == "README updated"
    saved = sessions.load(sid)["messages"]
    roles = [(m["role"], str(m.get("content"))[:40]) for m in saved]
    assert ("assistant", "I think we are done") in roles, \
        "the finishing text stays in the thread the steer answered"
    assert [m for m in saved if m.get("inbox_id") == "late"], "the steer was delivered"
    assert task_inbox.get(sid, "late")["state"] == "consumed"


def test_the_old_volatile_callback_and_the_inbox_do_not_both_insert(store, tmp_path):
    """``h.steering`` still works, and it is a different source from the inbox."""
    sid = "both-sources"
    volatile = ["volatile instruction"]
    h = _harness(tmp_path, sid=sid)
    h.steering = lambda: [volatile.pop()] if volatile else []
    seen = []
    h.provider = _accepts_during_the_run(seen, lambda: task_inbox.enqueue(
        sid, "durable", "durable instruction", mode="steer"))
    try:
        h.run("t", "start")
    finally:
        h.memory.close(); h.recorder.close()

    saved = sessions.load(sid)["messages"]
    contents = [str(m.get("content")) for m in saved]
    assert contents.count("durable instruction") == 1
    assert contents.count("volatile instruction") == 1


def test_unreadable_attachments_stop_before_the_next_model_or_tool_work(store, tmp_path):
    """An accepted request with a missing bundle is not delivered as a different one.

    Loading fails, the run stops at that boundary — the model is never asked to
    act on the text without the screenshot it was written about — and the request
    goes back to pending so it stays visible for correction or cancel.
    """
    sid = "lost-assets"

    def accept():
        reference = input_assets.save(
            sid, images=[{"media_type": "image/png", "data": "aGVsbG8="}])
        task_inbox.enqueue(sid, "with-image", "what is wrong in this screenshot?",
                           mode="steer", metadata={"assets": reference})
        # The bundle disappears between acceptance and delivery (a wiped cache, a
        # half-restored backup): the text alone is a different request.
        from harness.input_assets import _path as asset_path
        os.remove(asset_path(sid, reference["digest"]))

    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _accepts_during_the_run(seen, accept)
    try:
        res = h.run("t", "start")
    finally:
        h.memory.close(); h.recorder.close()

    assert len(seen) == 1, "the run stopped instead of asking the model anything else"
    assert "with-image" in (res.error or "") and "could not be delivered" in res.error
    assert task_inbox.get(sid, "with-image")["state"] == "pending", \
        "the request is waiting where the person can fix it"
    failures = [row for row in _events(h, "inbox") if row.get("ok") is False]
    assert any(row["action"] == "attachments" for row in failures)
    assert not _events(h, "steer"), "nothing was reported as delivered"


def test_attachments_are_expanded_for_the_model_but_never_for_authority(
        store, tmp_path):
    """Project files travel as context; only the person's words grant anything."""
    sid = "assets-authority"

    def accept():
        reference = input_assets.save(sid, contexts=[
            {"kind": "file", "path": "uploader.py", "content": "def upload(): ..."}])
        task_inbox.enqueue(sid, "ctx", "fix the retry", mode="steer",
                           metadata={"assets": reference})

    granted = []

    class Gate:
        def evaluate(self, *a, **kw):
            raise AssertionError("no tool call in this test")

        def begin_request(self, text, **kw):
            granted.append(text)

        def extend_request(self, text):
            granted.append(text)

    h = _harness(tmp_path, sid=sid)
    h.gate = Gate()
    seen = []
    h.provider = _accepts_during_the_run(seen, accept)
    try:
        h.run("t", "start")
    finally:
        h.memory.close(); h.recorder.close()

    contents = _seen_contents(seen)
    assert any("uploader.py" in c and "def upload()" in c for c in contents), \
        "the attached context reached the model"
    assert granted[1:] == ["fix the retry"], \
        "authority was extended with the typed words only: %s" % granted[1:]


def test_undelivered_claims_are_pending_again_when_the_run_ends(
        store, tmp_path, monkeypatch):
    """Nothing accepted may be stranded under the claim of a finished run.

    Two instructions are claimed together and the first cannot be written down.
    The run stops there, so the second was never inserted — and an entry nothing
    delivered has to be waiting in the queue the person can see, not held by a
    claim whose owner is about to disappear.
    """
    sid = "release-at-end"
    real_checkpoint = sessions.checkpoint

    def refuse_the_steer(sid_, messages, **kw):
        if (kw.get("detail") or {}).get("inbox_id"):
            raise OSError("journal is read-only")
        return real_checkpoint(sid_, messages, **kw)

    def accept():
        task_inbox.enqueue(sid, "s1", "keep this visible", mode="steer")
        task_inbox.enqueue(sid, "s2", "and this one too", mode="steer")

    monkeypatch.setattr(sessions, "checkpoint", refuse_the_steer)
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _accepts_during_the_run(seen, accept)
    try:
        h.run("t", "start")
    finally:
        h.memory.close(); h.recorder.close()

    assert task_inbox.get(sid, "s2")["state"] == "pending", \
        "an instruction this run never inserted is waiting again"
    released = _events(h, "inbox")
    assert any(row.get("action") == "release" and "s2" in (row.get("released") or [])
               for row in released), released


# --------------------------------------------------------------------------- #
# what may settle an accepted request: durable evidence only
# --------------------------------------------------------------------------- #
def test_messages_that_never_reached_disk_cannot_settle_an_accepted_request(
        store, tmp_path, monkeypatch):
    """In-memory messages are not evidence, and "consumed" is not a guess.

    The journal write silently does nothing here and the acknowledgement fails —
    the exact crash-shaped pair this stack exists for. The recovery that follows
    used to consult the run's own message list, which meant an accepted request
    could be marked delivered with no transcript row anywhere: the person is told
    it ran, and nothing ever ran it.
    """
    sid = "memory-is-not-evidence"
    lease, entry = _claimed(sid, "use the staging bucket", mode="steer")
    monkeypatch.setattr(sessions, "checkpoint", lambda *a, **kw: sid)  # writes nothing
    monkeypatch.setattr(task_inbox, "ack", lambda *a, **kw: (_ for _ in ()).throw(
        OSError("inbox is read-only")))
    h = _harness(tmp_path, sid=sid)
    h.provider = _ScriptProvider([_answer()])
    h.run_owner, h.input_entry = lease, entry
    try:
        h.run("t", entry["text"])
    finally:
        h.memory.close(); h.recorder.close(); lease.release()

    assert sessions.load(sid) is None, "nothing was ever written down"
    settled = task_inbox.get(sid, entry["id"])
    assert settled["state"] == "pending", \
        "an unproven delivery leaves the request waiting, never consumed"
    assert settled["delivery"] is None


def test_an_unreadable_journal_settles_nothing_and_stops_the_run(store, tmp_path):
    """"We cannot tell" must never be spent as "there was nothing waiting".

    The transcript is the only thing that can decide what an abandoned claim
    means. When it cannot be read, the claims stay exactly as they are and the
    run refuses — before a hook, a checkpoint or the model — with a failure the
    surface can show.
    """
    sid = "torn-journal"
    task_inbox.enqueue(sid, "s1", "roll back the migration", mode="steer")
    dead = session_owner.acquire(sid, label="an executor that vanished")
    task_inbox.claim(sid, dead, modes=("steer",))
    dead.release()
    with open(os.path.join(store, sid + ".json"), "w", encoding="utf-8") as fh:
        fh.write("{this is not a journal")

    h = _harness(tmp_path, sid=sid)
    h.provider = _ScriptProvider([_answer()])
    try:
        res = h.run("t", "carry on")
    finally:
        h.memory.close(); h.recorder.close()

    assert "could not be reconciled" in (res.error or ""), res.error
    assert h.provider.calls == 0, "nothing ran on a session whose state is unknown"
    assert task_inbox.get(sid, "s1")["state"] == "claimed", \
        "an unverifiable claim is preserved, not reopened and not consumed"
    assert [row["action"] for row in res.input_failures] == ["reconcile", "settle"], \
        "both the opening reconcile and the closing settle refuse, and say so: %s" \
        % res.input_failures


def test_a_delivered_instruction_is_not_handed_back_for_editing_at_the_end(
        store, tmp_path, monkeypatch):
    """End-of-run release is reconcile-then-release, never release-and-hope.

    The acknowledgement fails here, so the inbox still calls this entry claimed —
    but its message IS in the transcript. Returning it to ``pending`` would show
    the person a request they could edit or cancel after the model has already
    been given it.
    """
    sid = "delivered-not-reopened"
    monkeypatch.setattr(task_inbox, "ack", lambda *a, **kw: (_ for _ in ()).throw(
        OSError("inbox is read-only")))
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _accepts_during_the_run(seen, lambda: task_inbox.enqueue(
        sid, "s1", "use the staging bucket", mode="steer"))
    try:
        res = h.run("t", "start")
    finally:
        h.memory.close(); h.recorder.close()

    assert "could not be recorded as delivered" in (res.error or ""), res.error
    delivered = [m for m in sessions.load(sid)["messages"] if m.get("inbox_id") == "s1"]
    assert len(delivered) == 1, "the instruction reached the transcript once"
    settled = task_inbox.get(sid, "s1")
    assert settled["state"] == "consumed" and settled["delivery"]["recovered"] is True
    assert not [row for row in _events(h, "inbox")
                if row.get("action") == "release" and "s1" in (row.get("released") or [])]
    assert any(row["action"] == "ack" for row in res.input_failures), res.input_failures


def test_a_failed_boundary_write_is_never_shown_as_a_consumed_steer(
        store, tmp_path, monkeypatch):
    """No consumed event, no steer count, and no next turn on a false premise."""
    sid = "false-ack"
    real_checkpoint = sessions.checkpoint

    def refuse_the_steer(sid_, messages, **kw):
        if (kw.get("detail") or {}).get("inbox_id"):
            raise OSError("journal is read-only")
        return real_checkpoint(sid_, messages, **kw)

    monkeypatch.setattr(sessions, "checkpoint", refuse_the_steer)
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _accepts_during_the_run(seen, lambda: task_inbox.enqueue(
        sid, "s1", "stop and use the staging bucket", mode="steer"))
    try:
        res = h.run("t", "start")
    finally:
        h.memory.close(); h.recorder.close()

    assert not [row for row in _events(h, "steer") if row.get("state") == "consumed"], \
        "a write that failed is not an acknowledgement"
    assert res.steer_count == 0
    assert len(seen) == 1, "the run stopped at the boundary, before the next turn"
    assert [row["action"] for row in res.input_failures] == ["checkpoint"], \
        res.input_failures


def test_an_unreadable_inbox_stops_the_run_instead_of_ignoring_a_correction(
        store, tmp_path):
    """A store we cannot read may be holding the correction the person just sent.

    Continuing would answer the older request as though they had never sent it,
    and the failure would be one line in an event stream nobody reads.
    """
    sid = "torn-inbox"

    def corrupt():
        task_inbox.enqueue(sid, "s1", "wait — not that bucket", mode="steer")
        with open(task_inbox.store_path(sid), "w", encoding="utf-8") as fh:
            fh.write("{not an inbox")

    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _accepts_during_the_run(seen, corrupt)
    try:
        res = h.run("t", "start")
    finally:
        h.memory.close(); h.recorder.close()

    assert "cannot tell whether a correction is waiting" in (res.error or ""), res.error
    assert len(seen) == 1, "no further model work on a possibly-outdated request"
    assert any(row["action"] == "claim" for row in res.input_failures), \
        res.input_failures


# --------------------------------------------------------------------------- #
# chronology: which instructions belong to THIS run
# --------------------------------------------------------------------------- #
def test_an_old_pending_instruction_never_overrides_the_newer_one(store, tmp_path):
    """A steer accepted for an earlier run is not an amendment to this one.

    A canceled run left ten instructions queued, one of them the opposite of what
    the person has now decided. Appending them after the new request would let
    yesterday's sentence overrule today's — and with a backlog larger than one
    claim, the steer typed DURING this run would never be reached at all.
    """
    sid = "chronology"
    for index in range(9):
        task_inbox.enqueue(sid, "old-%d" % index, "old instruction %d" % index,
                           mode="steer")
    task_inbox.enqueue(sid, "old-contradiction", "deploy to production now",
                       mode="steer")
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _accepts_during_the_run(seen, lambda: task_inbox.enqueue(
        sid, "fresh", "and skip the changelog entry", mode="steer"))
    try:
        res = h.run("t", "do NOT deploy — just fix the retry")
    finally:
        h.memory.close(); h.recorder.close()

    contents = _seen_contents(seen)
    assert not res.error and res.steer_count == 1
    assert any("and skip the changelog entry" in c for c in contents), \
        "the instruction typed during this run was not starved by the backlog"
    assert not any("deploy to production now" in c for c in contents), \
        "an instruction from a canceled run never reaches this one's model turn"
    stale = ["old-contradiction"] + ["old-%d" % index for index in range(9)]
    for entry_id in stale:
        row = task_inbox.get(sid, entry_id)
        assert row["state"] == "pending", "%s: %s" % (entry_id, row["state"])
        assert row["attempts"] == 0, \
            "older rows are left alone, not claimed and released in a loop"
    assert task_inbox.get(sid, "fresh")["state"] == "consumed"
    order = [m.get("content") for m in sessions.load(sid)["messages"]
             if m.get("role") == "user"]
    assert order[0] == "do NOT deploy — just fix the retry"
    assert order[-1] == "and skip the changelog entry"


def test_a_queued_request_is_followed_by_steers_accepted_after_it(store, tmp_path):
    """The boundary for a queued run is the entry itself, not the clock.

    Someone starts an accepted request explicitly. Anything accepted after that
    request — including while the provider was still being set up — belongs to
    this run and must arrive after it. Anything accepted before it does not.
    """
    sid = "queued-then-steer"
    task_inbox.enqueue(sid, "older", "an idea from yesterday", mode="steer")
    task_inbox.enqueue(sid, "req", "add retries to the uploader", mode="follow_up")
    lease = session_owner.acquire(sid, label="surface")
    entry = task_inbox.claim(sid, lease, modes=("follow_up",))[0]
    task_inbox.enqueue(sid, "newer", "and cap them at three", mode="steer")
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _accepts_during_the_run(seen, lambda: None)
    h.run_owner, h.input_entry = lease, entry
    try:
        res = h.run("t", entry["text"])
    finally:
        h.memory.close(); h.recorder.close(); lease.release()

    assert not res.error
    saved = sessions.load(sid)["messages"]
    assert [m["inbox_id"] for m in saved if m.get("inbox_id")] == ["req", "newer"], \
        "the request first, then the instruction accepted after it"
    assert task_inbox.get(sid, "older")["state"] == "pending", \
        "an older instruction stays visible for an explicit start of its own"


def test_a_surface_may_pin_the_boundary_it_took_ownership_at(store, tmp_path):
    """Provider setup takes time, and people type during it.

    A surface that takes execution ownership before building a provider records
    the sequence it saw there and hands it over, so an instruction accepted in
    that gap is still this run's — without reaching back to instructions that
    were already waiting.
    """
    sid = "pinned-floor"
    task_inbox.enqueue(sid, "before", "an instruction from an earlier run",
                       mode="steer")
    lease = session_owner.acquire(sid, label="web execution manager")
    floor = run_ownership.sequence_floor(sid, lease)
    task_inbox.enqueue(sid, "during", "while the provider was warming up",
                       mode="steer")
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _recording_provider(seen)
    h.run_owner, h.steering_after_seq = lease, floor
    try:
        res = h.run("t", "start")
    finally:
        h.memory.close(); h.recorder.close(); lease.release()

    assert not res.error and res.steer_count == 1
    assert any("while the provider was warming up" in c for c in _seen_contents(seen))
    assert task_inbox.get(sid, "during")["state"] == "consumed"
    assert task_inbox.get(sid, "before")["state"] == "pending"


def test_claim_filters_by_sequence_under_the_same_lock_that_claims(store):
    """The storage half of the boundary, where it cannot race the caller."""
    sid = "after-seq"
    for entry_id in ("a", "b", "c"):
        task_inbox.enqueue(sid, entry_id, "instruction " + entry_id, mode="steer")
    lease = session_owner.acquire(sid, label="run")
    try:
        taken = task_inbox.claim(sid, lease, after_seq=task_inbox.get(sid, "b")["seq"])
        assert [row["id"] for row in taken] == ["c"]
        for entry_id in ("a", "b"):
            row = task_inbox.get(sid, entry_id)
            assert row["state"] == "pending" and row["attempts"] == 0, \
                "an entry below the boundary is not touched at all"
        assert task_inbox.claim(sid, lease, after_seq=99) == []
        with pytest.raises(task_inbox.InvalidRequest):
            task_inbox.claim(sid, lease, after_seq=-1)
    finally:
        lease.release()


# --------------------------------------------------------------------------- #
# one executor per session
# --------------------------------------------------------------------------- #
def test_a_second_executor_is_refused_instead_of_appending(store, tmp_path):
    """Two runs on one session interleave two conversations into one transcript."""
    sid = "single-executor"
    sessions.save(sid, [{"role": "user", "content": "first"}], cwd=str(tmp_path))
    holder = session_owner.acquire(sid, label="the run in flight")
    h = _harness(tmp_path, sid=sid)
    h.provider = _ScriptProvider([_answer()])
    try:
        res = h.run("t", "second executor")
        assert res.error and "already being executed" in res.error
        assert h.provider.calls == 0
        assert [m["content"] for m in sessions.load(sid)["messages"]] == ["first"], \
            "the refused run appended nothing"
        refusals = _events(h, "ownership")
        assert refusals and refusals[0]["busy"] is True
    finally:
        h.memory.close(); h.recorder.close(); holder.release()


def test_a_direct_embedder_gets_a_lease_and_gives_it_back(store, tmp_path):
    """The fallback lease exists for the length of run(), and not one moment longer."""
    sid = "fallback-lease"
    h = _harness(tmp_path, sid=sid)
    held = {}

    def check(messages):
        held["during"] = session_owner.try_acquire(sid, label="probe")
        return _answer()

    h.provider = _ScriptProvider([check])
    try:
        h.run("t", "go")
    finally:
        h.memory.close(); h.recorder.close()

    assert held["during"] is None, "the run held the lease while it was running"
    after = session_owner.try_acquire(sid, label="probe")
    assert after is not None, "and released it when it finished"
    after.release()


def test_a_lease_for_another_sessions_root_is_not_authority_here(store, tmp_path,
                                                                 monkeypatch):
    """Session ids are unique per directory, not globally.

    A lease over ``A/run-7`` must never be accepted as authority over ``B/run-7``
    — otherwise a second store (a test fixture, a relocated data directory)
    silently authorises executing somebody else's conversation of the same name.
    """
    sid = "run-7"
    other_root = tmp_path / "other-sessions"
    other_root.mkdir()
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(other_root))
    foreign = session_owner.acquire(sid, label="the other store")
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", store)
    h = _harness(tmp_path, sid=sid)
    h.provider = _ScriptProvider([_answer()])
    h.run_owner = foreign
    try:
        res = h.run("t", "go")
        assert res.error and "bound to" in res.error
        assert h.provider.calls == 0
    finally:
        h.memory.close(); h.recorder.close(); foreign.release()


def test_a_supplied_lease_survives_the_run_that_used_it(store, tmp_path):
    """A surface still owns its session after run() returns — its save is next."""
    sid = "supplied-lease"
    lease = session_owner.acquire(sid, label="surface")
    h = _harness(tmp_path, sid=sid)
    h.provider = _ScriptProvider([_answer()])
    h.run_owner = lease
    try:
        h.run("t", "go")
        assert lease.held, "run() must not release a lease it was handed"
        assert session_owner.try_acquire(sid, label="probe") is None
    finally:
        h.memory.close(); h.recorder.close(); lease.release()


# --------------------------------------------------------------------------- #
# host verification evidence, projected into the next turn
# --------------------------------------------------------------------------- #
def _receipt(**evidence):
    row = {"command": "python -m pytest -q tests/test_uploader.py", "executed": True,
           "passed": True, "cancelled": False, "exit_code": 0, "freshness": "fresh",
           "source": "user", "timestamp": "2026-09-06T10:00:00+00:00"}
    row.update(evidence)
    return {"stop_reason": "completed", "verified": bool(row["passed"]),
            "verification_evidence": row}


@pytest.mark.parametrize("evidence,expected", [
    ({}, "PASSED"),
    ({"passed": False, "exit_code": 1}, "FAILED"),
    ({"passed": False, "cancelled": True, "exit_code": None}, "STOPPED"),
    ({"passed": False, "executed": False, "exit_code": None,
      "skipped_reason": "the run was stopped before it finished"}, "NOT RUN"),
])
def test_a_resumed_model_turn_sees_the_real_check_verdict(
        store, tmp_path, evidence, expected):
    """The verdict lived only in ``run_receipts``, where no model turn can read it.

    A conversation resumed after a host check had no idea whether the project's
    tests passed, failed or were stopped — so it re-asserted whatever the earlier
    transcript claimed. This carries the host's own record forward instead.
    """
    sid = "verdict"
    sessions.save(sid, [{"role": "user", "content": "fix the uploader"},
                        {"role": "assistant", "content": "fixed it"}],
                  cwd=str(tmp_path))
    assert sessions.append_run_receipt(sid, _receipt(**evidence))
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _recording_provider(seen)
    try:
        h.run("t", "now add the docs", history=sessions.load(sid)["messages"])
    finally:
        h.memory.close(); h.recorder.close()

    projected = [c for c in _seen_contents(seen) if "Host verification evidence" in c]
    assert len(projected) == 1, "exactly one host-authored context message"
    assert expected in projected[0], projected[0]
    assert "python -m pytest -q tests/test_uploader.py" in projected[0], \
        "the exact command it ran is the actionable half"
    emitted = _events(h, "verification_context")
    assert emitted and emitted[0]["command"] == \
        "python -m pytest -q tests/test_uploader.py"


def test_the_projection_is_host_text_not_a_user_turn(store, tmp_path):
    """It must not become the thread's title, its turn count, or granted authority."""
    sid = "projection-identity"
    sessions.save(sid, [{"role": "user", "content": "fix the uploader"}],
                  cwd=str(tmp_path))
    sessions.append_run_receipt(sid, _receipt(passed=False, exit_code=2))
    granted = []

    class Gate:
        def begin_request(self, text, **kw):
            granted.append(text)

        def extend_request(self, text):
            granted.append(text)

    h = _harness(tmp_path, sid=sid)
    h.gate = Gate()
    h.provider = _ScriptProvider([_answer()])
    try:
        h.run("t", "now add the docs", history=sessions.load(sid)["messages"])
    finally:
        h.memory.close(); h.recorder.close()

    saved = sessions.load(sid)["messages"]
    context = [m for m in saved if m.get("kind") == run_ownership.CONTEXT_KIND]
    assert len(context) == 1
    assert context[0]["source"] == "harness"
    assert not compaction.is_user_message(context[0]), \
        "host evidence is not the person's instruction"
    assert granted == ["now add the docs"], \
        "a receipt never extends what the user authorized: %s" % granted
    listed = [row for row in sessions.recent(10) if row["id"] == sid][0]
    assert listed["title"] == "fix the uploader"
    assert listed["turns"] == 2, "the projection is not one of the person's turns"


def test_the_same_receipt_is_not_re_appended_on_every_run(store, tmp_path):
    """One short message per distinct check — not one per call, forever."""
    sid = "no-duplicates"
    sessions.save(sid, [{"role": "user", "content": "fix it"}], cwd=str(tmp_path))
    sessions.append_run_receipt(sid, _receipt())
    for turn in range(3):
        h = _harness(tmp_path, sid=sid)
        h.provider = _ScriptProvider([_answer()])
        try:
            h.run("t", "turn %d" % turn, history=sessions.load(sid)["messages"])
        finally:
            h.memory.close(); h.recorder.close()
    saved = sessions.load(sid)["messages"]
    assert len([m for m in saved
                if m.get("kind") == run_ownership.CONTEXT_KIND]) == 1

    # A NEW check is new evidence, and does get carried.
    sessions.append_run_receipt(sid, _receipt(passed=False, exit_code=1,
                                              timestamp="2026-09-06T11:00:00+00:00"))
    h = _harness(tmp_path, sid=sid)
    h.provider = _ScriptProvider([_answer()])
    try:
        h.run("t", "again", history=sessions.load(sid)["messages"])
    finally:
        h.memory.close(); h.recorder.close()
    contexts = [m for m in sessions.load(sid)["messages"]
                if m.get("kind") == run_ownership.CONTEXT_KIND]
    assert len(contexts) == 2 and "FAILED" in contexts[-1]["content"]


def test_a_model_claim_of_a_passing_check_never_becomes_host_evidence(
        store, tmp_path):
    """Only what the host executed is projected. Prose is not a receipt."""
    sid = "claims-are-not-evidence"
    sessions.save(sid, [
        {"role": "user", "content": "fix the uploader"},
        {"role": "assistant", "content": "I ran python -m pytest -q and all 42 tests "
                                         "passed, so this is verified."}],
        cwd=str(tmp_path))
    sessions.append_run_receipt(sid, {"stop_reason": "completed", "verified": False,
                                      "error": "", "verification_evidence": None})
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _recording_provider(seen)
    try:
        h.run("t", "what is the state?", history=sessions.load(sid)["messages"])
    finally:
        h.memory.close(); h.recorder.close()

    assert not [c for c in _seen_contents(seen) if "Host verification evidence" in c]
    assert not [m for m in sessions.load(sid)["messages"]
                if m.get("kind") == run_ownership.CONTEXT_KIND]
    assert not _events(h, "verification_context")


def test_a_credential_inside_a_recorded_command_is_not_quoted_back(store, tmp_path):
    """The projection is model-facing text, so it obeys the model-facing rules."""
    sid = "secret-command"
    token = "sk-ant-AAAABBBBCCCCDDDDEEEEFFFF"
    sessions.save(sid, [{"role": "user", "content": "run the check"}], cwd=str(tmp_path))
    sessions.append_run_receipt(sid, _receipt(command="pytest --token " + token))
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _recording_provider(seen)
    try:
        h.run("t", "carry on", history=sessions.load(sid)["messages"])
    finally:
        h.memory.close(); h.recorder.close()

    projected = [c for c in _seen_contents(seen) if "Host verification evidence" in c]
    assert projected and token not in projected[0]
    assert "pytest --token" in projected[0], "the command is still recognisable"
    assert token not in json.dumps(sessions.load(sid)["messages"], ensure_ascii=False)


def test_reading_receipts_requires_the_lease_for_that_session(store, tmp_path):
    """Evidence about one conversation is never assembled while owning another."""
    sessions.save("a", [{"role": "user", "content": "a"}], cwd=str(tmp_path))
    sessions.append_run_receipt("a", _receipt())
    lease = session_owner.acquire("b", label="wrong session")
    try:
        with pytest.raises(run_ownership.OwnershipRefused):
            run_ownership.verification_row("a", lease)
    finally:
        lease.release()


# --------------------------------------------------------------------------- #
# real processes: crash recovery and cross-terminal serialization
# --------------------------------------------------------------------------- #
def _env(store):
    env = dict(os.environ)
    env["COLLIE_SESSIONS_DIR"] = store
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _script(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


CRASH_AFTER_JOURNAL = """
import os, sys
from harness import sessions, session_owner, task_inbox

session = sys.argv[1]
lease = session_owner.acquire(session, label="doomed run")
claimed = task_inbox.claim(session, lease, modes=("steer",))
messages = list((sessions.load_checked(session)["session"] or {}).get("messages") or [])
for entry in claimed:
    messages.append(task_inbox.journal_message(entry))
sessions.save(session, messages)
print("JOURNALED %d" % len(claimed), flush=True)
os._exit(9)          # the acknowledgement never happens
"""

HOLD_THE_LEASE = """
import sys, time
from harness import session_owner

lease = session_owner.acquire(sys.argv[1], label="another terminal")
print("HELD", flush=True)
time.sleep(float(sys.argv[2]))
"""


def test_a_crash_between_journal_and_ack_never_re_delivers_the_instruction(
        store, tmp_path):
    """The one-line crash window, resolved from the journal by the next run.

    A real process claims the entry, writes the user message and dies before
    acknowledging it. Re-delivering would repeat the person's instruction to the
    model; dropping it would lose one that never ran. Only the transcript knows.
    """
    sid = "crash-window"
    task_inbox.enqueue(sid, "s1", "use the staging bucket", mode="steer")
    crash = subprocess.run(
        [sys.executable, _script(tmp_path, "crash.py", CRASH_AFTER_JOURNAL), sid],
        cwd=ROOT, env=_env(store), capture_output=True, text=True, timeout=120,
        **plat.no_window_kwargs())
    assert "JOURNALED 1" in crash.stdout, crash.stderr
    assert task_inbox.get(sid, "s1")["state"] == "claimed", "crashed mid-delivery"

    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _recording_provider(seen)
    try:
        res = h.run("t", "carry on", history=sessions.load(sid)["messages"])
    finally:
        h.memory.close(); h.recorder.close()

    assert not res.error
    saved = sessions.load(sid)["messages"]
    assert [str(m.get("content")) for m in saved].count("use the staging bucket") == 1, \
        "the recovered instruction is in the thread exactly once"
    settled = task_inbox.get(sid, "s1")
    assert settled["state"] == "consumed"
    assert settled["delivery"]["recovered"] is True, \
        "the record says this delivery was resolved by recovery, not by an ack"
    assert res.steer_count == 0, "nothing was claimed a second time"


@pytest.mark.parametrize("surface", ["cli", "loop"])
def test_another_terminal_holding_the_session_refuses_this_one(
        store, tmp_path, surface):
    """Real OS ownership: a second terminal is told, not silently interleaved."""
    sid = "busy-session"
    sessions.save(sid, [{"role": "user", "content": "hello"}], cwd=str(tmp_path))
    holder = subprocess.Popen(
        [sys.executable, _script(tmp_path, "hold.py", HOLD_THE_LEASE), sid, "20"],
        cwd=ROOT, env=_env(store), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, **plat.no_window_kwargs())
    try:
        assert holder.stdout.readline().strip() == "HELD"
        if surface == "loop":
            h = _harness(tmp_path, sid=sid)
            h.provider = _ScriptProvider([_answer()])
            try:
                res = h.run("t", "from the second terminal")
                assert "already being executed" in (res.error or "")
            finally:
                h.memory.close(); h.recorder.close()
        else:
            second = subprocess.run(
                [sys.executable, "-m", "harness.cli", "run", "from the second terminal",
                 "--provider", "mock", "--json", "--resume", sid,
                 "--cwd", str(tmp_path), "--project", "inbox"],
                cwd=ROOT, env=_env(store), capture_output=True, text=True,
                timeout=180, **plat.no_window_kwargs())
            assert second.returncode == 2, second.stdout + second.stderr
            payload = json.loads(second.stdout.strip().splitlines()[-1])
            assert payload["busy"] is True and payload["session"] == sid
            assert "already being executed" in payload["error"]
        assert [m["content"] for m in sessions.load(sid)["messages"]] == ["hello"], \
            "a refused executor appended nothing to the conversation"
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_the_cli_reports_input_it_could_not_deliver(store, tmp_path, monkeypatch,
                                                    capsys):
    """A person who was told "accepted" is told when the run could not deliver it."""
    sid = "cli-visible"
    sessions.save(sid, [{"role": "user", "content": "hello"}], cwd=str(tmp_path))
    real_checkpoint = sessions.checkpoint

    def refuse_the_steer(sid_, messages, **kw):
        if (kw.get("detail") or {}).get("inbox_id"):
            raise OSError("journal is read-only")
        return real_checkpoint(sid_, messages, **kw)

    monkeypatch.setattr(sessions, "checkpoint", refuse_the_steer)
    _steering_provider(monkeypatch, [(sid, "s1", "and use the staging bucket")])
    args = _cli_args(sid, cwd=str(tmp_path), task="carry on")
    assert cli.cmd_run(args) == 1, "a run that could not deliver accepted input failed"
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert [row["action"] for row in payload["inbox_errors"]] == ["checkpoint"]
    assert payload["inbox_errors"][0]["id"] == "s1"
    # The boundary write failed, so nothing acknowledged anything. What the entry
    # ends up as is decided by the transcript alone: the run's terminal write did
    # carry the message, so it is consumed exactly once — settled from evidence,
    # never from the ack that never happened.
    delivered = [m for m in sessions.load(sid)["messages"] if m.get("inbox_id") == "s1"]
    assert len(delivered) == 1 and delivered[0]["content"] == "and use the staging bucket"
    assert task_inbox.get(sid, "s1")["state"] == "consumed"
    assert task_inbox.get(sid, "s1")["delivery"]["recovered"] is True


def _steering_provider(monkeypatch, accepted):
    """Make the CLI's harness accept ``accepted`` while its first turn is running."""
    real_make = cli.make_harness
    seen = []

    def make(*a, **kw):
        h = real_make(*a, **kw)
        h.provider = _accepts_during_the_run(
            seen, lambda: [task_inbox.enqueue(sid, eid, text, mode="steer")
                           for sid, eid, text in accepted])
        return h

    monkeypatch.setattr(cli, "make_harness", make)
    return seen


def _cli_args(sid, **overrides):
    from types import SimpleNamespace
    data = dict(cwd=None, provider="mock", model=None, project="inbox", mode=None,
                persona=None, goal=None, resume=sid, cont=False, task="do it",
                stream_json=False, json=True, print=False, web_search=False,
                intent="build", quality="balanced", verification="auto", effort=None,
                speed=None, verify_command=None, runner=None)
    return SimpleNamespace(**dict(data, **overrides))


def test_the_cli_serializes_the_external_worker_path_too(store, tmp_path,
                                                         monkeypatch, capsys):
    """An external worker's turn is still one turn on one owned session."""
    from harness import runner_registry, runner_select, runner_slice
    from harness.recorder import RunResult
    from types import SimpleNamespace

    sid = "worker-session"
    sessions.save(sid, [{"role": "user", "content": "hello"}], cwd=str(tmp_path))
    observed = {}

    def fake_adhoc(decision, task, workspace, **kwargs):
        observed["held"] = session_owner.try_acquire(sid, label="probe") is None
        return RunResult(task_id="adhoc", harness="codex-exec", model="",
                         provider="codex", answer="worker did it", messages=[],
                         success=True)

    monkeypatch.setattr(runner_slice, "run_adhoc", fake_adhoc)
    monkeypatch.setattr(runner_slice, "receipt_of", lambda res: None)
    monkeypatch.setattr(runner_slice, "transcript_text", lambda res: res.answer)
    monkeypatch.setattr(
        runner_select, "decide",
        lambda *a, **kw: SimpleNamespace(
            runner="codex-exec", error="", to_dict=lambda: {"runner": "codex-exec"},
            speed="standard", credential_family="codex", model=""))
    monkeypatch.setattr(runner_registry, "probe_all", lambda **kw: {})

    args = SimpleNamespace(
        cwd=str(tmp_path), provider="mock", model=None, project="inbox", mode=None,
        persona=None, goal=None, resume=sid, cont=False, task="rename the helper",
        stream_json=False, json=True, print=False, web_search=False, intent="build",
        quality="balanced", verification="auto", effort=None, speed=None,
        verify_command=None, runner="codex-exec")
    assert cli.cmd_run(args) == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["session"] == sid
    assert observed["held"] is True, \
        "the lease is held across the external worker, not only the native loop"
    assert session_owner.try_acquire(sid, label="after") is not None, \
        "and released when the command returns"


# --------------------------------------------------------------------------- #
# the terminal feed
# --------------------------------------------------------------------------- #
class _Pipe:
    """A real OS pipe read by the real feed thread (a pty is not portable)."""

    def __init__(self, tty=True):
        read_fd, self._write_fd = os.pipe()
        self.stream = os.fdopen(read_fd, "r", encoding="utf-8", newline="\n")
        self.stream.isatty = lambda: tty

    def type(self, text):
        os.write(self._write_fd, (text + "\n").encode("utf-8"))

    def close(self):
        os.close(self._write_fd)


def test_typed_lines_are_durable_before_the_terminal_calls_them_accepted(store):
    """The acknowledgement on screen is a report, not a promise.

    Storage first, ack second: a line typed into a run that then crashes is
    still an accepted instruction, because it was written down before the
    person was told anything.
    """
    sid = "tui-feed"
    pipe = _Pipe()
    feed = tui._StdinFeed(pipe.stream)
    said = []

    def note(text, style="dim"):
        # What the inbox holds AT THE MOMENT the terminal speaks.
        said.append((text, [e["id"] for e in task_inbox.list_entries(sid)]))

    try:
        with feed.accepting(tui._steer_acceptor(sid, note)):
            pipe.type("also update the changelog")
            deadline = time.time() + 10
            while not said and time.time() < deadline:
                time.sleep(0.02)
        assert said, "the feed never accepted the line"
        message, stored_at_ack_time = said[0]
        assert "queued for this run" in message
        assert len(stored_at_ack_time) == 1, \
            "the text was on disk before the terminal said it was queued"
        entries = task_inbox.list_entries(sid)
        assert entries[0]["text"] == "also update the changelog"
        assert entries[0]["mode"] == "steer" and entries[0]["client"] == "tui"
    finally:
        pipe.close()


def test_a_slash_command_typed_during_a_run_is_still_a_slash_command(store):
    """Shortcuts stay shortcuts: they are honored after the run, never sent to the model."""
    sid = "tui-slash"
    pipe = _Pipe()
    feed = tui._StdinFeed(pipe.stream)
    try:
        with feed.accepting(tui._steer_acceptor(sid, lambda *a, **kw: None)):
            pipe.type("/sessions")
            assert feed.readline_blocking() == "/sessions"
        assert task_inbox.list_entries(sid) == [], \
            "a REPL command is not an instruction to the model"
    finally:
        pipe.close()


def test_an_approval_answer_goes_to_the_prompt_not_to_the_inbox(store):
    """The permission question and the steer channel share one stdin; y means yes."""
    sid = "tui-approval"
    pipe = _Pipe()
    feed = tui._StdinFeed(pipe.stream)
    answers = []
    try:
        with feed.accepting(tui._steer_acceptor(sid, lambda *a, **kw: None)):
            import threading
            reader = threading.Thread(
                target=lambda: answers.append(feed.readline_blocking("allow? ")),
                daemon=True)
            reader.start()
            time.sleep(0.3)               # the prompt is now waiting
            pipe.type("y")
            reader.join(timeout=10)
        assert answers == ["y"]
        assert task_inbox.list_entries(sid) == [], \
            "the approval answer must not be filed as an instruction"
    finally:
        pipe.close()


def test_a_refused_line_is_reported_with_the_text_still_visible(store, monkeypatch):
    """Failed persistence is shown. It is never dressed up as acceptance."""
    sid = "tui-refused"
    monkeypatch.setattr(task_inbox, "enqueue", lambda *a, **kw: (_ for _ in ()).throw(
        task_inbox.InboxFull("64 requests are already waiting")))
    said = []
    accept = tui._steer_acceptor(sid, lambda text, style="dim": said.append((text, style)))
    assert accept("please also fix the docs") is True
    assert said and said[0][1] == "red"
    assert "not accepted" in said[0][0]
    assert "please also fix the docs" in said[0][0], \
        "the person can see (and retype) what was refused"


class _FakeHarness:
    """A harness stand-in that records exactly what each turn was handed."""

    def __init__(self, cwd, on_run=None):
        self.cwd, self.emit, self.steering = cwd, None, None
        self.checkpoint_scope = ""
        self.provider = None
        self.approve = None
        self.run_owner = None
        self.turns = []                   # (text, history) as the surface handed it
        self._on_run = on_run
        self.memory = self.recorder = type(
            "S", (), {"close": staticmethod(lambda: None)})()

    def run(self, _task, text, consolidate=True, history=None, **kw):
        from harness.recorder import RunResult
        self.turns.append((text, list(history or [])))
        if self._on_run is not None:
            self._on_run(self)
        return RunResult(answer="ok", model="mock",
                         messages=list(history or []) + [
                             {"role": "user", "content": text},
                             {"role": "assistant", "content": "ok"}])


def _stub_routing(monkeypatch):
    """Strip routing and gating: these tests are about the surface, not the router."""
    from types import SimpleNamespace
    monkeypatch.setattr(tui, "_HAVE_RICH", False)
    monkeypatch.setattr(cli, "default_gate", lambda *a, **k: None)
    monkeypatch.setattr(cli, "resolve_turn_decision", lambda *a, **k: SimpleNamespace(
        model="mock-1", effort="low", intent="build", quality="balanced",
        verification="auto"))
    monkeypatch.setattr(cli, "apply_turn_decision", lambda *a, **k: None)
    monkeypatch.setattr(cli, "turn_decision_receipt", lambda *a, **k: {"ok": True})


def _drive_surface(monkeypatch, tmp_path, surface, harness, lines, *,
                   at_prompt=None, resume=None):
    """Run one interactive surface over a scripted set of typed lines."""
    monkeypatch.setattr(cli, "make_harness", lambda cwd, **kw: harness)
    typed = iter(lines)

    def read_line(*_a, **_k):
        if at_prompt is not None:
            at_prompt()
        return next(typed)

    if surface == "tui":
        monkeypatch.setattr(tui, "_read_line", read_line)
        return tui.run_tui(str(tmp_path), "mock", None, project="inbox", resume=resume)
    monkeypatch.setattr("builtins.input", read_line)
    return cli.cmd_repl(_cli_args(resume, cwd=str(tmp_path), goal=None))


@pytest.mark.parametrize("surface", ["tui", "repl"])
def test_an_interactive_surface_holds_the_lease_only_while_a_turn_runs(
        store, tmp_path, monkeypatch, surface):
    """Thinking at a prompt must not lock everyone else out of the conversation."""
    sid = "tui-lease"
    monkeypatch.setattr(sessions, "new_id", lambda: sid)
    _stub_routing(monkeypatch)
    probes = {"at_prompt": []}

    def during_the_run(fake):
        probes["during"] = session_owner.try_acquire(sid, label="probe")
        probes["steering"] = fake.steering
        probes["owner_held"] = bool(fake.run_owner and fake.run_owner.held)

    def at_prompt():
        # BETWEEN turns — including the prompt AFTER the turn ran — nobody may be
        # holding this session: a person thinking is not an executor.
        free = session_owner.try_acquire(sid, label="prompt probe")
        probes["at_prompt"].append(free is not None)
        if free is not None:
            free.release()

    assert _drive_surface(monkeypatch, tmp_path, surface,
                          _FakeHarness(str(tmp_path), during_the_run),
                          ("do the thing", "/exit"), at_prompt=at_prompt) == 0
    assert probes["owner_held"] is True, "the turn ran under a real lease"
    assert probes["during"] is None, "which nothing else could take"
    assert probes["at_prompt"] == [True, True], \
        "the prompt holds nothing, before or after a turn: %s" % probes["at_prompt"]
    assert probes["steering"] is None, \
        "durable input is claimed by the loop; the volatile callback stays off"


@pytest.mark.parametrize("surface", ["tui", "repl"])
def test_a_turn_runs_on_the_thread_as_it_is_now_not_as_the_prompt_remembers_it(
        store, tmp_path, monkeypatch, surface):
    """A person sitting at a prompt is not holding the conversation still.

    While they think, another surface can execute this very session to
    completion. Continuing from the copy this process loaded would ask the model
    about a thread that no longer exists and write an answer over messages
    another run recorded — so the history a turn executes on is re-derived under
    the turn's own lease, after the prompt returns.
    """
    sid = "stale-%s" % surface
    sessions.save(sid, [{"role": "user", "content": "first"},
                        {"role": "assistant", "content": "first answer"}],
                  cwd=str(tmp_path))
    _stub_routing(monkeypatch)
    elsewhere = []

    def another_surface_executes_it():
        if elsewhere:
            return                        # only while the FIRST prompt waits
        elsewhere.append(True)
        lease = session_owner.acquire(sid, label="the web surface")
        try:
            sessions.save(sid, sessions.load(sid)["messages"] + [
                {"role": "user", "content": "from the web"},
                {"role": "assistant", "content": "web answer"}], cwd=str(tmp_path))
        finally:
            lease.release()

    harness = _FakeHarness(str(tmp_path))
    assert _drive_surface(monkeypatch, tmp_path, surface, harness,
                          ("carry on", "/exit"),
                          at_prompt=another_surface_executes_it, resume=sid) == 0
    assert [m["content"] for m in harness.turns[0][1]] == [
        "first", "first answer", "from the web", "web answer"], \
        "the turn was handed the durable thread, not the one loaded minutes ago"
    assert [m["content"] for m in sessions.load(sid)["messages"]] == [
        "first", "first answer", "from the web", "web answer", "carry on", "ok"], \
        "and the other run's messages are still in the journal afterwards"


@pytest.mark.parametrize("surface", ["tui", "repl"])
def test_a_fence_raised_while_the_prompt_waits_stops_the_next_turn(
        store, tmp_path, monkeypatch, surface, capsys):
    """The fence that appeared during the wait is the one that governs this turn.

    The recovery state was clean when the surface opened. If an executor stops
    this session inside a tool while the person is typing, the next turn must not
    reason over an effect nobody has inspected — the check has to happen under the
    lease, not once at startup.
    """
    sid = "fenced-%s" % surface
    sessions.save(sid, [{"role": "user", "content": "first"}], cwd=str(tmp_path))
    _stub_routing(monkeypatch)
    raised = []

    def an_executor_stops_inside_a_tool():
        if raised:
            return
        raised.append(True)
        sessions.checkpoint(sid, sessions.load(sid)["messages"], cwd=str(tmp_path),
                            state="executing_tool", detail={"tool_name": "bash"})

    harness = _FakeHarness(str(tmp_path))
    assert _drive_surface(monkeypatch, tmp_path, surface, harness,
                          ("carry on", "/exit"),
                          at_prompt=an_executor_stops_inside_a_tool, resume=sid) == 0
    assert harness.turns == [], "no turn ran over an uninspected effect"
    out = capsys.readouterr().out
    assert "this thread is paused" in out and "collie recovery show" in out, out


@pytest.mark.parametrize("surface", ["tui", "repl"])
def test_a_busy_conversation_is_not_moved_out_from_under_its_executor(
        store, tmp_path, monkeypatch, surface):
    """Opening a session elsewhere with --cwd is a durable write about where it runs.

    A run already in flight resolved its workspace from the journal; relocating
    it underneath that run would leave the executor working in a directory the
    conversation no longer claims.
    """
    sid = "relocating-%s" % surface
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sessions.save(sid, [{"role": "user", "content": "first"}], cwd=str(tmp_path))
    _stub_routing(monkeypatch)
    harness = _FakeHarness(str(elsewhere))
    monkeypatch.setattr(cli, "make_harness", lambda cwd, **kw: harness)
    holder = session_owner.acquire(sid, label="the run in flight")
    try:
        if surface == "tui":
            monkeypatch.setattr(tui, "_read_line", lambda *a, **k: "/exit")
            code = tui.run_tui(str(elsewhere), "mock", None, project="inbox",
                               resume=sid, cwd_explicit=True)
        else:
            monkeypatch.setattr("builtins.input", lambda *a, **k: "/exit")
            code = cli.cmd_repl(_cli_args(sid, cwd=str(elsewhere), goal=None))
    finally:
        holder.release()

    assert code == 2, "the surface refused to open rather than move a busy session"
    assert os.path.normcase(sessions.load(sid)["cwd"]) == \
        os.path.normcase(str(tmp_path)), "the workspace on record is untouched"
    assert harness.turns == []


@pytest.mark.parametrize("surface", ["tui", "repl"])
def test_a_conversation_that_moved_is_not_executed_in_the_old_workspace(
        store, tmp_path, monkeypatch, surface, capsys):
    """A relocated thread is refused here, explicitly, rather than run anyway.

    The journal says where this conversation lives. Executing it against the
    directory this window happens to hold would edit an unrelated tree under the
    authority of somebody else's thread.
    """
    sid = "moved-%s" % surface
    moved_to = tmp_path / "elsewhere"
    moved_to.mkdir()
    sessions.save(sid, [{"role": "user", "content": "first"}], cwd=str(tmp_path))
    _stub_routing(monkeypatch)
    moved = []

    def it_moves_while_the_prompt_waits():
        if moved:
            return
        moved.append(True)
        sessions.relocate(sid, str(moved_to))

    harness = _FakeHarness(str(tmp_path))
    assert _drive_surface(monkeypatch, tmp_path, surface, harness,
                          ("carry on", "/exit"),
                          at_prompt=it_moves_while_the_prompt_waits, resume=sid) == 0
    assert harness.turns == [], "nothing ran in the workspace it no longer belongs to"
    out = capsys.readouterr().out
    assert "elsewhere" in out and "reopen it there" in out, out
