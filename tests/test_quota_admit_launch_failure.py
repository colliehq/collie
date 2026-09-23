"""A quota admission that claims a request and then cannot launch it.

``admit_after_quota_reset`` claims the exact accepted request the wait was bound
to, and only then hands it to ``schedule``.  ``schedule`` starts an OS thread, so
it can fail — and when it does, the claim has already been taken.  A claim is the
one inbox state with no way out from a surface: cancel and edit are refused
*because* it is claimed, and Start is refused *while* it is claimed.  So a launch
failure that leaves the claim behind takes an accepted request away from the
person who accepted it, and the wait's own bounded retry is what would have
reconciled it — until the wait retires after ``MAX_ATTEMPTS``.

What is locked down here is the sibling rule ``_settle_and_schedule`` already
follows: the request this owner claimed and did not deliver goes back to waiting,
with a reason, before the lease is released.  Exactly that request — nothing
already journalled is disturbed, no accepted request is discarded, and nothing
else is started in its place.

That return can itself fail — the lease is gone, the store will not write — and
then it is the *launch* failure a person still has to be told about, once, with
no pretence that the request is waiting again.  Two tests hold that: a lease lost
with the launch, and an inbox that stops accepting writes.

The second half covers the nearby integration this repair sits inside: a natural
reset that admits A while B is queued behind it, so A runs under the settings it
was accepted with and B chains after A, each delivered once.

    python -m pytest -q tests/test_quota_admit_launch_failure.py
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import quota_resume, session_owner, sessions, task_inbox, web_tasks  # noqa: E402
from test_quota_followup_resume import SOON, _quota, _run_until_quota  # noqa: E402,F401
from test_web_execution_manager import (CONFIG, _accept, _post, _settle,  # noqa: E402,F401
                                        _states, _stream, lab)


class _LaunchFailure(RuntimeError):
    """What ``threading.Thread.start`` raises when the OS will not give one."""


def _refuse_to_launch(monkeypatch, calls=None, breaking=None):
    """``schedule`` raising after the claim has already been taken.

    Returns a switch that restores the real launcher.  ``monkeypatch.undo()``
    would do it too — and would also undo the ``lab`` fixture's patches, which
    share this function-scoped ``monkeypatch``; the store and the harness would
    move out from under the test halfway through.

    ``breaking`` runs with ``(session, owner, entry)`` immediately before the
    refusal: the cases where whatever stopped the launch also broke the cleanup
    that has to follow it.
    """
    real, refusing = web_tasks.schedule, {"on": True}

    def _boom(session, owner, entry):
        if not refusing["on"]:
            return real(session, owner, entry)
        if calls is not None:
            calls.append(entry["id"])
        if breaking is not None:
            breaking(session, owner, entry)
        raise _LaunchFailure("can't start new thread")

    monkeypatch.setattr(web_tasks, "schedule", _boom)
    return refusing


def _break_inbox_writes(monkeypatch, session):
    """Arm "this session's inbox file will not be written".

    The failure is a raw ``OSError`` out of the real store code — a full or
    unwritable disk — not an exception handed to the admission at its own call
    boundary, and only for this inbox, so the wait record and the published
    error are written by their ordinary code.
    """
    real, broken = sessions._atomic_dump, {"on": False}
    target = os.path.abspath(task_inbox.store_path(session))

    def _dump(obj, path):
        if broken["on"] and os.path.abspath(str(path)) == target:
            raise OSError(28, "No space left on device")
        return real(obj, path)

    monkeypatch.setattr(sessions, "_atomic_dump", _dump)
    return broken


def _journalled(session):
    stored = sessions.load(session)["messages"]
    return [m.get("inbox_id") for m in stored if m.get("inbox_id")]


# ------------------------------------------------- one failed launch, exactly

def test_a_launch_failure_returns_the_claimed_request_to_waiting(lab, monkeypatch):
    """The admission claimed it and could not start it, so it goes back — still
    accepted, still cancellable, and still carrying exactly what was typed."""
    session = "quota-launch-failed"
    retry_at = _run_until_quota(lab, session)
    accepted = task_inbox.get(session, "follow-1")

    attempted = []
    _refuse_to_launch(monkeypatch, attempted)
    out = quota_resume.tick(now=retry_at + 1)
    _settle()

    assert [row["reason"] for row in out] == ["error"], out
    assert attempted == ["follow-1"], "the bound request is the one it tried"
    assert len(lab.calls) == 1, "nothing ran"

    # The claim this owner took and did not deliver is back where a person can
    # reach it, and the bytes and the frozen budget are the accepted ones.
    restored = task_inbox.get(session, "follow-1")
    assert restored["state"] == "pending", "a stranded claim cannot be canceled or started"
    assert restored["text"] == accepted["text"]
    assert restored["bytes"] == accepted["bytes"]
    assert restored["config"] == accepted["config"]
    assert _journalled(session) == [], "nothing was delivered, so nothing was written down"

    # The failure is visible rather than silent, and the wait kept its place.
    error = web_tasks.queue_error(session)
    assert error is not None and "could not be started" in error["error"]
    assert "_LaunchFailure" in error["error"], "the real failure, not a summary"
    row = quota_resume.read(session)
    assert row["state"] == "waiting" and row["attempts"] == 1

    # The lease is free: a surface, a manual Start or the next pass can take it.
    lease = session_owner.try_acquire(session, label="after-launch-failure")
    assert lease is not None, "the owner lease was released"
    lease.release()


def test_a_later_retry_starts_the_same_request_once(lab, monkeypatch):
    """A returned request is not a spent one: the very next pass that can launch
    delivers it, exactly once, under its own frozen budget."""
    session = "quota-launch-retry"
    retry_at = _run_until_quota(lab, session)
    frozen = task_inbox.get(session, "follow-1")["config"]["frozen"]

    refusing = _refuse_to_launch(monkeypatch)
    assert [row["reason"] for row in quota_resume.tick(now=retry_at + 1)] == ["error"]
    _settle()
    assert task_inbox.get(session, "follow-1")["state"] == "pending"

    # The backoff this module wrote is honoured, and then it starts.
    refusing["on"] = False
    assert quota_resume.tick(now=retry_at + 2) == [], "not before the backoff"
    started = quota_resume.tick(now=retry_at + 1 + quota_resume.RETRY_BACKOFF[0] + 1)
    _settle()

    assert [row["started"] for row in started] == [True], started
    assert [call["entry"] for call in lab.calls[1:]] == ["follow-1"], "once, not twice"
    assert [call["message"] for call in lab.calls[1:]] == ["then update the README #0"]
    assert task_inbox.get(session, "follow-1")["config"]["frozen"] == frozen
    assert _states(session) == {"follow-1": "consumed"}
    assert _journalled(session) == ["follow-1"], "written down exactly once"


def test_repeated_launch_failures_retire_the_wait_and_leave_the_request_startable(
        lab, monkeypatch):
    """After ``MAX_ATTEMPTS`` nothing will come back to reconcile this claim, so
    the request must already be waiting — and an explicit Start must still run
    it, once."""
    session = "quota-launch-exhausted"
    retry_at = _run_until_quota(lab, session)
    accepted = task_inbox.get(session, "follow-1")

    refusing = _refuse_to_launch(monkeypatch)
    now = retry_at + 1
    for attempt in range(1, quota_resume.MAX_ATTEMPTS + 1):
        out = quota_resume.tick(now=now)
        _settle()
        assert [row["reason"] for row in out] == ["error"], (attempt, out)
        row = quota_resume.read(session)
        assert row["attempts"] == attempt, row
        assert task_inbox.get(session, "follow-1")["state"] == "pending", (
            "attempt %d stranded the claim" % attempt)
        now += quota_resume.RETRY_BACKOFF[-1] + 1

    row = quota_resume.read(session)
    assert row["state"] == "retired" and row["attempts"] == quota_resume.MAX_ATTEMPTS
    assert quota_resume.tick(now=now + 86400) == [], "the wait gave up, as documented"
    assert len(lab.calls) == 1, "no launch ever succeeded"

    # The schedule stopped being a schedule, and says so where the queue row is.
    shown = web_tasks.scheduled_wait(session)
    assert shown["state"] == "retired" and shown["entry"] == "follow-1"

    # And the request is exactly as accepted, reachable by hand.
    restored = task_inbox.get(session, "follow-1")
    assert restored["state"] == "pending"
    assert restored["text"] == accepted["text"] and restored["bytes"] == accepted["bytes"]
    assert restored["config"] == accepted["config"]

    refusing["on"] = False
    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True, started
    _settle()
    assert [call["entry"] for call in lab.calls[1:]] == ["follow-1"]
    assert _states(session) == {"follow-1": "consumed"}
    assert _journalled(session) == ["follow-1"], "delivered once, not replayed"


def test_a_launch_failure_starts_nothing_else_and_discards_nothing(lab, monkeypatch):
    """The repair returns one request; it does not promote the next one into the
    slot, and it does not throw the failed one away."""
    session = "quota-launch-neighbour"
    retry_at = _run_until_quota(lab, session)
    _accept(lab, session, "follow-2", "and bump the version")

    _refuse_to_launch(monkeypatch)
    assert [row["reason"] for row in quota_resume.tick(now=retry_at + 1)] == ["error"]
    _settle()

    assert _states(session) == {"follow-1": "pending", "follow-2": "pending"}
    assert len(lab.calls) == 1, "no substitute was started"
    assert quota_resume.read(session)["entry"] == "follow-1", "still bound to the same one"


def test_a_lease_lost_with_the_launch_still_reports_why_it_did_not_start(lab, monkeypatch):
    """The cleanup needs the lease the launch failure took with it.  What the
    person is told is still *why the request did not start*, the wait still backs
    off once, and nothing claims the request is waiting again when it is not."""
    session = "quota-launch-lease-lost"
    retry_at = _run_until_quota(lab, session)
    accepted = task_inbox.get(session, "follow-1")
    web_tasks.clear_queue_error(session)

    refusing = _refuse_to_launch(
        monkeypatch, breaking=lambda session_id, owner, entry: owner.release())
    out = quota_resume.tick(now=retry_at + 1)
    _settle()

    assert [row["reason"] for row in out] == ["error"], out
    assert len(lab.calls) == 1, "nothing ran"

    # The cause a person needs is the launch, not the cleanup that failed after it.
    error = web_tasks.queue_error(session)
    assert error is not None, "the admission published nothing a surface can show"
    assert "could not be started" in error["error"]
    assert "_LaunchFailure" in error["error"], "the launch cause, not the cleanup's"

    row = quota_resume.read(session)
    assert row["state"] == "waiting" and row["attempts"] == 1, "counted once, backed off once"
    assert "_LaunchFailure" in row["reason"], "the wait remembers why it could not start"

    # And the request really is still claimed, so nothing may say otherwise —
    # in the durable record the queue row reads from, as well as on the surface.
    assert task_inbox.get(session, "follow-1")["state"] == "claimed"
    assert "could not be returned to waiting" in error["error"], (
        "a stranded claim cannot be canceled or started; saying so is the point")
    assert "OwnershipRequired" in error["error"], "and what stopped the cleanup"
    assert "could not be returned to waiting" in row["reason"]
    stuck = task_inbox.get(session, "follow-1")
    assert stuck["text"] == accepted["text"] and stuck["bytes"] == accepted["bytes"]
    assert stuck["config"] == accepted["config"]
    assert _journalled(session) == [], "nothing was delivered"

    lease = session_owner.try_acquire(session, label="after-lost-lease")
    assert lease is not None, "the owner lease was released"
    lease.release()

    # The next pass reconciles the stranded claim and delivers it once.
    refusing["on"] = False
    started = quota_resume.tick(now=retry_at + 1 + quota_resume.RETRY_BACKOFF[0] + 1)
    _settle()
    assert [row["started"] for row in started] == [True], started
    assert [call["entry"] for call in lab.calls[1:]] == ["follow-1"], "once, not twice"
    assert [call["message"] for call in lab.calls[1:]] == ["then update the README #0"]
    assert _states(session) == {"follow-1": "consumed"}
    assert _journalled(session) == ["follow-1"], "written down exactly once"


def test_an_unwritable_inbox_during_cleanup_neither_raises_nor_hides_the_launch(
        lab, monkeypatch):
    """The store stops accepting writes as the launch fails.  Admission answers
    its caller rather than raising a second failure at it, records the attempt
    once, and publishes the launch as the cause."""
    session = "quota-launch-unwritable"
    retry_at = _run_until_quota(lab, session)
    accepted = task_inbox.get(session, "follow-1")
    web_tasks.clear_queue_error(session)

    broken = _break_inbox_writes(monkeypatch, session)
    refusing = _refuse_to_launch(
        monkeypatch, breaking=lambda session_id, owner, entry: broken.update(on=True))

    held = quota_resume.claim(session, now=retry_at + 1)
    assert held is not None, "the wait was due"
    out = web_tasks.admit_after_quota_reset(held)   # must return, not raise
    _settle()

    assert out["started"] is False and out["reason"] == "error", out
    assert len(lab.calls) == 1, "nothing ran"

    error = web_tasks.queue_error(session)
    assert error is not None, "the admission published nothing a surface can show"
    assert "_LaunchFailure" in error["error"], "the launch cause, not the cleanup's"
    assert "No space left on device" in error["error"], "and the cleanup named too"

    row = quota_resume.read(session)
    assert row["state"] == "waiting" and row["attempts"] == 1, "counted once, backed off once"
    assert "_LaunchFailure" in row["reason"], "the wait remembers why it could not start"

    broken["on"] = False
    assert task_inbox.get(session, "follow-1")["state"] == "claimed", (
        "the write failed, so the claim stayed; the message above says so")
    assert _journalled(session) == [], "nothing was delivered"
    lease = session_owner.try_acquire(session, label="after-unwritable-inbox")
    assert lease is not None, "the owner lease was released"
    lease.release()

    # A store that works again: the claim is reconciled and delivered, once.
    refusing["on"] = False
    started = quota_resume.tick(now=retry_at + 1 + quota_resume.RETRY_BACKOFF[0] + 1)
    _settle()
    assert [row["started"] for row in started] == [True], started
    assert [call["entry"] for call in lab.calls[1:]] == ["follow-1"], "once, not twice"
    assert task_inbox.get(session, "follow-1")["config"] == accepted["config"]
    assert _states(session) == {"follow-1": "consumed"}
    assert _journalled(session) == ["follow-1"], "written down exactly once"


def test_an_inbox_error_on_release_is_swallowed_and_the_claim_waits_for_the_next_pass(
        lab, monkeypatch):
    """``release_undelivered`` swallows ``InboxError`` by design, so this proves
    only what that leaves behind: the launch failure is still published and the
    wait still backs off, while the claim really is still stuck — and the next
    pass is what clears it.  It does *not* show a release failure reported with a
    cause of its own; that is the two tests above."""
    session = "quota-launch-unreleasable"
    retry_at = _run_until_quota(lab, session)

    refusing = _refuse_to_launch(monkeypatch)
    real_release, refusing_release = task_inbox.release_claims, {"on": True}

    def _refuse_release(session_id, owner, entry_ids=None, reason=""):
        if not refusing_release["on"]:
            return real_release(session_id, owner, entry_ids=entry_ids, reason=reason)
        raise task_inbox.InboxError("the inbox could not be written")

    monkeypatch.setattr(task_inbox, "release_claims", _refuse_release)
    out = quota_resume.tick(now=retry_at + 1)
    _settle()

    assert [row["reason"] for row in out] == ["error"], out
    error = web_tasks.queue_error(session)
    assert error is not None and "could not be started" in error["error"]
    row = quota_resume.read(session)
    assert row["state"] == "waiting" and row["attempts"] == 1, "asked again, not lost"
    # The claim really is still there — this test asserts the truth, not a
    # comfortable story about it.  The next pass reconciles it before claiming.
    assert task_inbox.get(session, "follow-1")["state"] == "claimed"

    refusing["on"] = refusing_release["on"] = False
    started = quota_resume.tick(now=retry_at + 1 + quota_resume.RETRY_BACKOFF[0] + 1)
    _settle()
    assert [row["started"] for row in started] == [True], started
    assert [call["entry"] for call in lab.calls[1:]] == ["follow-1"]
    assert _journalled(session) == ["follow-1"]


# ------------------------------------------- the reset, with a queue behind it

def test_a_natural_reset_admits_a_then_chains_b_each_delivered_once(lab, monkeypatch):
    """The whole shape, end to end: A is bound to the wait, B is accepted while
    it waits, the reset admits A under A's frozen settings, and B follows A
    through the ordinary follow-up chain — neither of them twice."""
    from harness import router

    session = "quota-chain"
    decisions = []
    real_decision = router.resolve_run_decision

    def _spy(*args, **kwargs):
        decisions.append(dict(kwargs))
        return real_decision(*args, **kwargs)

    monkeypatch.setattr(router, "resolve_run_decision", _spy)
    _accept(lab, session, "follow-a", "first, update the README",
            config=dict(CONFIG, effort="high", explicit_axes="effort"))
    lab.results = [_quota(int(time.time() + SOON))]
    events = _stream(lab, q="start the long job", session=session)
    assert events[-1][0] == "done"
    _settle()

    retry_at = quota_resume.read(session)["retry_at"]
    assert quota_resume.read(session)["entry"] == "follow-a"

    # B is accepted while the wait is armed, and the panel moves on afterwards.
    _accept(lab, session, "follow-b", "then bump the version")
    lab.settings["MODEL"] = "model-changed-afterwards"
    lab.settings["REASONING_EFFORT"] = "low"

    out = quota_resume.tick(now=retry_at + 1)
    _settle()

    assert [row["started"] for row in out] == [True], out
    chained = lab.calls[1:]
    assert [call["entry"] for call in chained] == ["follow-a", "follow-b"], chained
    assert [call["message"] for call in chained] == ["first, update the README",
                                                     "then bump the version"]
    # A ran under what it was accepted with, not under what the panel says now.
    assert chained[0]["model"] == "model-a"
    admitted = decisions[-2]
    assert admitted["model"] == "model-a", "the frozen model, not the one set later"
    assert admitted["effort"] == "high", "the frozen effort, not the one set later"
    assert lab.settings["MODEL"] == "model-changed-afterwards", "freezing stayed local"
    assert _states(session) == {"follow-a": "consumed", "follow-b": "consumed"}
    assert _journalled(session) == ["follow-a", "follow-b"], "each written down once"
    # The interrupted turn is not replayed and no 'continue' text is invented.
    assert all("continue" not in call["message"].lower() for call in chained)
    assert quota_resume.read(session) is None, "the wait got its answer"


def test_cancelling_a_while_it_waits_neither_replays_a_nor_substitutes_b(lab):
    """Withdrawing the bound request ends the wait.  B is not promoted into A's
    place, and nothing is written down under A's identity."""
    session = "quota-chain-canceled"
    retry_at = _run_until_quota(lab, session, queued=("follow-a",))
    _accept(lab, session, "follow-b", "then bump the version")
    assert quota_resume.read(session)["entry"] == "follow-a"

    assert task_inbox.cancel(session, "follow-a", reason="not that one after all")
    out = quota_resume.tick(now=retry_at + 1)
    _settle()

    assert [row["reason"] for row in out] == ["nothing_pending"], out
    assert len(lab.calls) == 1, "nothing ran"
    assert _states(session)["follow-b"] == "pending", "not retargeted"
    assert _journalled(session) == [], "A was never delivered, under any identity"
    row = quota_resume.read(session)
    assert row["state"] == "retired" and "no longer queued" in row["reason"]

    # B is still a request a person can start, and it runs as itself.
    code, started = _post(lab, "/api/task-inbox/start", {"session": session})
    assert code == 200 and started["started"] is True
    _settle()
    assert [call["entry"] for call in lab.calls[1:]] == ["follow-b"]
    assert [call["message"] for call in lab.calls[1:]] == ["then bump the version"]
    assert _journalled(session) == ["follow-b"]
