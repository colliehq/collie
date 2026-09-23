"""An already accepted follow-up, admitted once the provider says it will answer.

The behaviour under test is narrow on purpose.  A run dies on a quota refusal
that carries the provider's own reset time; a *different*, separately accepted
request is already waiting in the durable inbox under its own frozen budget.
What this locks down is that the waiting request starts — exactly once, through
the ordinary managed scheduling path, after the reset, under the settings it was
accepted with — and that nothing else does: the interrupted request is never
re-run, no "continue" text is invented, and every refusal a person owns (a
recovery fence, a cancellation, a newer run already going) still wins.

The lifecycle failures are what most of this file is about, because they are
what a passing happy path says nothing about: a claimant killed mid-admission,
an old claimant racing a newer wait, a manual run that supersedes the schedule,
a store whose state cannot be read, and a ticker asked to shut down.  Those are
exercised against real processes, a real OS session lease, the real store and
the real ``serve_managed_stream``, not against stand-ins for them.

Offline throughout.  The fake is the harness, which implements the real
durable-input handshake (see ``tests/test_web_execution_manager``, whose lab
this reuses).

    python -m pytest -q tests/test_quota_followup_resume.py
"""
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import quota_resume, session_owner, sessions, task_inbox, web_tasks  # noqa: E402
from test_web_execution_manager import (CONFIG, _Result, _accept, _get,  # noqa: E402,F401
                                        _hold_lease_in_another_process, _settle,
                                        _states, _stream, lab)

SOON = 60           # the provider's reset, seconds from now
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _quota(retry_at):
    """What the loop returns when the provider refuses on quota and names a time."""
    return _Result(answer="", error="rate limit reached", success=False,
                   retry_at=int(retry_at))


def _run_until_quota(lab, session, *, queued=("follow-1",), retry_at=None):
    """One run that fails on quota, with ``queued`` already accepted behind it."""
    retry_at = int(retry_at or time.time() + SOON)
    for index, entry_id in enumerate(queued):
        _accept(lab, session, entry_id, "then update the README #%d" % index)
    lab.results = [_quota(retry_at)]
    events = _stream(lab, q="start the long job", session=session)
    assert events[-1][0] == "done"
    _settle()
    return retry_at


def _child(code, state, *, wait_for=None):
    """Run ``code`` in a genuinely separate interpreter against this store."""
    script = ("import os, sys, time, json;sys.path.insert(0, %r);"
              "os.environ['COLLIE_SESSIONS_DIR'] = %r;" % (HERE, str(state / "sessions"))) + code
    child = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                             text=True)
    if wait_for is not None:
        assert child.stdout.readline().strip() == wait_for
    return child


# --------------------------------------------------------------- the record

def test_the_wait_is_written_before_the_stream_returns_and_survives_a_new_process(lab):
    """A browser tab owns none of this: the record is on disk, and readable by
    a process that did not exist when the run failed."""
    session = "quota-durable"
    retry_at = _run_until_quota(lab, session)

    row = quota_resume.read(session)
    assert row is not None and row["state"] == "waiting"
    assert row["retry_at"] == retry_at and row["entry"] == "follow-1"
    assert _states(session) == {"follow-1": "pending"}, "still accepted, not started"
    assert not lab.calls[1:], "the failed run started nothing"

    # A genuinely separate interpreter, with only the store on disk to go on.
    out = subprocess.run(
        [sys.executable, "-c",
         "import json,sys;sys.path.insert(0,%r);from harness import quota_resume;"
         "print(json.dumps(quota_resume.waiting()))" % HERE],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ, COLLIE_STATE_DIR=str(lab.state),
                 COLLIE_SESSIONS_DIR=str(lab.state / "sessions")))
    assert out.returncode == 0, out.stderr
    assert [(r["session"], r["retry_at"], r["entry"]) for r in json.loads(out.stdout)] == [
        (session, retry_at, "follow-1")]


def test_no_accepted_follow_up_records_no_wait(lab):
    """Nothing to admit means the ordinary wait-for-Start, not a timer looking
    for something to invent."""
    session = "quota-empty"
    lab.results = [_quota(time.time() + SOON)]
    _stream(lab, q="only this one", session=session)
    _settle()
    assert quota_resume.read(session) is None
    assert quota_resume.waiting() == []


def test_the_shared_wait_reading_is_the_only_one(lab):
    """Host error, budget cap, cancel and fence all outrank a reset time, and
    this file computes no second opinion about any of them."""
    from harness.recorder import note_host_error
    upcoming = int(time.time() + SOON)

    waited = web_tasks.terminal_outcome(_quota(upcoming))
    assert waited["provider_wait"] is True and waited["retry_at"] == upcoming
    assert waited["auto_next"] is False, "a wait is still not a finish"

    # Every withdrawal leaves the row with no wait keys at all — the shape the
    # parallel provider-wait work defined, not a zeroed retry_at beside it.
    for row in (web_tasks.terminal_outcome(_quota(upcoming), canceled=True),
                web_tasks.terminal_outcome(_quota(upcoming), recovery_required=True),
                web_tasks.terminal_outcome(_Result(error="boom", success=False)),
                web_tasks.terminal_outcome(_Result(answer="", success=False,
                                                   budget_exhausted=True,
                                                   retry_at=upcoming))):
        assert "retry_at" not in row and "provider_wait" not in row, row

    # A reset that has already arrived stays a wait — that is the shared
    # validator's documented second reading, and it means "askable now", which
    # is exactly what a record written just before a long sleep should mean.
    passed = web_tasks.terminal_outcome(_quota(int(time.time()) - 10))
    assert passed["provider_wait"] is True and passed["retry_at"] < time.time()

    host = _quota(upcoming)
    note_host_error(host, "the transcript could not be saved")
    assert "retry_at" not in web_tasks.terminal_outcome(host)

    # ...and each of those records nothing, whatever is queued behind it.
    session = "quota-precedence"
    _accept(lab, session, "follow-1", "the queued one")
    for outcome in (web_tasks.terminal_outcome(host),
                    web_tasks.terminal_outcome(_quota(upcoming), canceled=True),
                    web_tasks.terminal_outcome(_quota(upcoming), recovery_required=True)):
        assert web_tasks.note_quota_wait(session, outcome) is None
    assert quota_resume.read(session) is None
    assert web_tasks.note_quota_wait(
        session, web_tasks.terminal_outcome(_quota(upcoming))) is not None


def test_a_budget_cap_with_a_reset_time_schedules_nothing(lab):
    """A run stopped by its own frozen budget is not waiting for the provider."""
    session = "quota-budget"
    _accept(lab, session, "follow-1", "the queued one")
    lab.results = [_Result(answer="", success=False, budget_exhausted=True,
                           retry_at=int(time.time() + SOON))]
    _stream(lab, q="the expensive one", session=session)
    _settle()
    assert quota_resume.read(session) is None
    assert _states(session) == {"follow-1": "pending"}, "waiting for an explicit Start"


# ------------------------------------------------------------- the admission

def test_before_the_reset_nothing_launches(lab):
    session = "quota-early"
    retry_at = _run_until_quota(lab, session)
    started = len(lab.calls)

    assert quota_resume.tick(now=retry_at - 1) == []
    _settle()
    assert len(lab.calls) == started, "no run before the provider said it would answer"
    assert quota_resume.read(session)["state"] == "waiting"
    assert _states(session) == {"follow-1": "pending"}


def test_the_queued_request_starts_once_through_managed_scheduling(lab):
    """The distinct queued request runs, under the budget frozen when it was
    accepted — and a duplicate pass does not run it a second time."""
    session = "quota-admit"
    retry_at = _run_until_quota(lab, session)
    frozen = task_inbox.get(session, "follow-1")["config"]["frozen"]

    first = quota_resume.tick(now=retry_at + 1)
    second = quota_resume.tick(now=retry_at + 1)          # a duplicate tick
    _settle()

    assert [row["started"] for row in first] == [True]
    assert all(not row.get("started") for row in second), second
    delivered = [call for call in lab.calls[1:]]
    assert [call["message"] for call in delivered] == ["then update the README #0"]
    assert [call["entry"] for call in delivered] == ["follow-1"]
    assert _states(session) == {"follow-1": "consumed"}
    # The interrupted request is not replayed and no 'continue' text is invented.
    assert all("continue" not in call["message"].lower() for call in delivered)
    assert lab.calls[0]["message"] == "start the long job"

    # Frozen ceiling and capabilities are the ones accepted, untouched by the wait.
    assert task_inbox.get(session, "follow-1")["config"]["frozen"] == frozen
    assert quota_resume.read(session) is None, "an admitted wait is spent"


def test_two_consumers_racing_the_same_wait_admit_it_once(lab):
    """The claim is a compare-and-set on the durable record, so a second pass —
    in this process or another — finds nothing to do."""
    session = "quota-race"
    retry_at = _run_until_quota(lab, session)

    held = quota_resume.claim(session, now=retry_at + 1)
    assert held is not None and held["state"] == "admitting"
    assert quota_resume.claim(session, now=retry_at + 1) is None
    assert quota_resume.tick(now=retry_at + 1) == []
    _settle()
    assert len(lab.calls) == 1, "the claimed wait was not admitted twice"


def test_a_slept_through_reset_still_starts(lab):
    """A laptop asleep for a day did not lose its place: there is no expiry and
    no busy counter that turns being away into 'press Start again'."""
    session = "quota-slept"
    retry_at = _run_until_quota(lab, session)

    out = quota_resume.tick(now=retry_at + 3 * 86400)
    _settle()
    assert [row["started"] for row in out] == [True]
    assert [call["entry"] for call in lab.calls[1:]] == ["follow-1"]


def test_a_busy_conversation_is_asked_again_and_never_gives_up(lab):
    """Busy means someone newer is already working; the wait defers, and never
    interrupts, double-starts, or counts itself out."""
    session = "quota-busy"
    retry_at = _run_until_quota(lab, session)
    lease = session_owner.try_acquire(session, label="manual")
    assert lease is not None
    try:
        when = retry_at + 1
        for attempt in range(1, 4):
            out = quota_resume.tick(now=when)
            assert [row["reason"] for row in out] == ["busy"], attempt
            row = quota_resume.read(session)
            assert row["state"] == "waiting" and row["attempts"] == 0
            assert row["deferrals"] == attempt, "deferring is counted, never fatal"
            assert row["next_attempt_at"] > when, "backs off instead of spinning"
            # Due again once the backoff has passed; nothing expired it.
            when = row["next_attempt_at"]
            assert quota_resume.waiting(now=when, due_only=True)
    finally:
        lease.release()
    _settle()
    assert len(lab.calls) == 1
    assert _states(session) == {"follow-1": "pending"}


# ----------------------------------------------------- the claimant can die

def test_a_killed_claimant_is_recovered_and_the_request_still_starts(lab):
    """The blocker: a process that dies after claiming used to strand the wait.

    The claim is taken by a real other process, which is then killed outright.
    Evidence, not a timeout: the OS drops that process's session lease, and the
    inbox still shows the bound request pending.
    """
    session = "quota-killed"
    retry_at = _run_until_quota(lab, session)
    child = _child("from harness import quota_resume;"
                   "held = quota_resume.claim(%r, now=%r);"
                   "print('claimed' if held else 'no', flush=True);"
                   "time.sleep(300)" % (session, retry_at + 1),
                   lab.state, wait_for="claimed")
    child.kill(), child.wait()

    stranded = quota_resume.read(session)
    assert stranded["state"] == "admitting" and stranded["claimed_by"] != os.getpid()
    assert quota_resume.waiting(now=retry_at + 2, due_only=True) == [], "claimed, not due"

    # A pass later than the claim grace: no lease is held, the entry is pending.
    out = quota_resume.tick(now=retry_at + 1 + quota_resume.CLAIM_GRACE + 1)
    _settle()
    assert [row.get("reason") for row in out] == ["recovered", None], out
    assert [row["started"] for row in out][-1] is True
    assert [call["entry"] for call in lab.calls[1:]] == ["follow-1"]
    assert _states(session) == {"follow-1": "consumed"}
    assert quota_resume.read(session) is None


def test_a_live_claimant_is_left_alone_and_a_dead_one_is_not(lab):
    """Alive or dead is asked of the OS lease, never assumed from the clock."""
    session = "quota-alive"
    retry_at = _run_until_quota(lab, session)
    held = quota_resume.claim(session, now=retry_at + 1)
    late = retry_at + 1 + quota_resume.CLAIM_GRACE + 1

    holder = _hold_lease_in_another_process(lab.state, session)
    try:
        assert quota_resume.claim_evidence(held, now=late) == "in_flight"
        assert quota_resume.recover(held, now=late) is None
        assert quota_resume.tick(now=late) == [], "nothing while an executor lives"
        assert quota_resume.read(session)["state"] == "admitting"
        # Still in flight for as long as it runs, however long that is.
        assert quota_resume.claim_evidence(held, now=late + 10 * 86400) == "in_flight"
    finally:
        holder.kill(), holder.wait()

    assert quota_resume.claim_evidence(held, now=late) == "abandoned"
    recovered = quota_resume.recover(held, now=late)
    assert recovered["state"] == "waiting" and recovered["nonce"] != held["nonce"]
    # The zombie's authority is gone with the nonce it held.
    assert quota_resume.retire(session, "too late", nonce=held["nonce"]) is None
    assert quota_resume.read(session)["state"] == "waiting"


def test_a_claim_younger_than_the_grace_is_never_treated_as_dead(lab):
    session = "quota-inflight"
    retry_at = _run_until_quota(lab, session)
    held = quota_resume.claim(session, now=retry_at + 1)
    assert quota_resume.claim_evidence(held, now=retry_at + 2) == "in_flight"
    assert quota_resume.recover(held, now=retry_at + 2) is None
    assert quota_resume.read(session)["state"] == "admitting"


def test_a_recovered_claim_whose_request_already_ran_retires(lab):
    """Journal evidence, not the wait's own optimism: a delivered request is
    finished work, and nothing restarts it."""
    session = "quota-delivered"
    retry_at = _run_until_quota(lab, session)
    held = quota_resume.claim(session, now=retry_at + 1)
    quota_resume.tick(now=retry_at + 1)              # claimed already: no-op
    task_inbox.cancel(session, "follow-1", reason="withdrawn while stranded")

    late = retry_at + 1 + quota_resume.CLAIM_GRACE + 1
    assert quota_resume.claim_evidence(held, now=late) == "gone"
    assert quota_resume.tick(now=late) == []
    _settle()
    assert len(lab.calls) == 1
    row = quota_resume.read(session)
    assert row["state"] == "retired" and "no longer queued" in row["reason"]


# ------------------------------------------------- an old pass, a new wait

def test_an_old_claimants_retirement_cannot_delete_a_newer_wait(lab):
    """The start/retire race: while one pass was stalled, a newer failing run
    recorded a newer wait.  The stalled pass may not cancel it."""
    session = "quota-cas"
    retry_at = _run_until_quota(lab, session)
    stale = quota_resume.claim(session, now=retry_at + 1)

    later = retry_at + 900
    fresh = quota_resume.record(session, retry_at=later, entry_id="follow-1")
    assert fresh["nonce"] != stale["nonce"] and fresh["retry_at"] == later

    # Everything the stalled pass could still do, refused on the nonce.
    assert quota_resume.retire(session, "gave up", nonce=stale["nonce"]) is None
    assert quota_resume.defer(session, "busy", nonce=stale["nonce"]) is None
    assert quota_resume.failed(session, "broke", nonce=stale["nonce"]) is None
    assert quota_resume.clear(session, nonce=stale["nonce"]) is None
    assert quota_resume.retire(session, "done", nonce=stale["nonce"],
                               state="admitted") is None

    row = quota_resume.read(session)
    assert row["state"] == "waiting" and row["retry_at"] == later
    assert quota_resume.tick(now=retry_at + 2) == [], "the newer reset is not due yet"
    _settle()
    assert len(lab.calls) == 1


def test_a_newer_reset_for_the_same_request_wins_and_an_earlier_one_does_not(lab):
    session = "quota-newest"
    retry_at = _run_until_quota(lab, session)
    first = quota_resume.read(session)
    assert quota_resume.record(session, retry_at=retry_at - 30,
                               entry_id="follow-1")["nonce"] == first["nonce"]
    assert quota_resume.read(session)["retry_at"] == retry_at
    assert quota_resume.record(session, retry_at=retry_at + 30,
                               entry_id="follow-1")["retry_at"] == retry_at + 30


def test_a_newer_manual_run_supersedes_the_schedule(lab):
    """A person who came back and ran the conversation themselves is newer than
    any timer set before they did."""
    session = "quota-manual"
    retry_at = _run_until_quota(lab, session)
    lab.results = [_Result(answer="handled by hand")]
    _stream(lab, q="never mind, do this instead", session=session)
    _settle()

    assert quota_resume.read(session) is None, "the wait did not survive a newer run"
    before = len(lab.calls)
    assert quota_resume.tick(now=retry_at + 1) == []
    _settle()
    assert len(lab.calls) == before


def test_a_newer_run_that_was_canceled_still_supersedes(lab):
    """Cancelling the newer run is a decision too: the old schedule does not
    quietly come back and start queued work behind it."""
    session = "quota-manual-canceled"
    retry_at = _run_until_quota(lab, session)
    lab.results = [_Result(answer="", canceled=True, success=False)]
    _stream(lab, q="actually, stop", session=session)
    _settle()

    assert quota_resume.read(session) is None
    before = len(lab.calls)
    assert quota_resume.tick(now=retry_at + 1) == []
    _settle()
    assert len(lab.calls) == before
    assert _states(session)["follow-1"] == "pending", "still accepted, still visible"


def test_a_claim_that_was_superseded_before_the_lease_executes_nothing(lab):
    """The blocker: ``claim`` returns a nonce, but the world keeps moving until
    the session lease is actually held.  A claim that is no longer the record's
    current one has no authority — so it starts nothing, claims nothing from the
    inbox, and does not delete or retire whatever replaced it."""
    session = "quota-superseded"
    retry_at = _run_until_quota(lab, session)
    calls = len(lab.calls)

    # 1. a newer manual run cleared the wait between the claim and the lease
    held = quota_resume.claim(session, now=retry_at + 1)
    assert quota_resume.clear(session, reason="a newer run took this conversation")
    assert quota_resume.read(session) is None
    out = web_tasks.admit_after_quota_reset(held)
    _settle()
    assert out == {"session": session, "started": False, "reason": "superseded"}
    assert quota_resume.read(session) is None, "nothing was resurrected or written"

    # 2. a newer failing run replaced it with its own, later reset
    fresh = quota_resume.record(session, retry_at=retry_at + 900, entry_id="follow-1")
    held = quota_resume.claim(session, now=retry_at + 901)
    newer = quota_resume.record(session, retry_at=retry_at + 1800, entry_id="follow-1")
    assert newer["nonce"] != held["nonce"] != fresh["nonce"]
    assert web_tasks.admit_after_quota_reset(held)["reason"] == "superseded"
    _settle()
    row = quota_resume.read(session)
    assert row["nonce"] == newer["nonce"] and row["state"] == "waiting", "untouched"

    # 3. a recovery pass decided the claimant was gone and rotated the nonce
    held = quota_resume.claim(session, now=retry_at + 1801)
    recovered = quota_resume.recover(held, now=retry_at + 1801 + quota_resume.CLAIM_GRACE + 1)
    assert recovered["state"] == "waiting" and recovered["nonce"] != held["nonce"]
    assert web_tasks.admit_after_quota_reset(held)["reason"] == "superseded"
    _settle()
    row = quota_resume.read(session)
    assert row["nonce"] == recovered["nonce"] and row["state"] == "waiting"

    # Through all of it: no run, and the accepted request never left the queue.
    assert len(lab.calls) == calls, "a superseded claim started nothing"
    assert _states(session) == {"follow-1": "pending"}, "never claimed, never consumed"
    free = session_owner.try_acquire(session, label="after")
    assert free is not None, "each superseded attempt released the session lease"
    free.release()


# ------------------------------------------------- the bound request, exactly

def test_cancelling_the_scheduled_request_starts_nothing_else(lab):
    """The wait is bound to one accepted entry.  Withdrawing it ends the wait;
    it never silently retargets whatever else happens to be queued."""
    session = "quota-bound"
    retry_at = _run_until_quota(lab, session)
    _accept(lab, session, "follow-2", "and bump the version")
    task_inbox.cancel(session, "follow-1", reason="not that one after all")

    out = quota_resume.tick(now=retry_at + 1)
    _settle()
    assert [row["reason"] for row in out] == ["nothing_pending"]
    assert len(lab.calls) == 1, "nothing ran"
    assert _states(session)["follow-2"] == "pending", "not retargeted"
    row = quota_resume.read(session)
    assert row["state"] == "retired" and "no longer queued" in row["reason"]


def test_a_recovery_fence_leaves_the_request_for_a_person(lab, monkeypatch):
    session = "quota-fenced"
    retry_at = _run_until_quota(lab, session)
    monkeypatch.setattr(sessions, "recovery_state",
                        lambda sid, directory=None: {
                            "recovery_required": True,
                            "reason": "an edit boundary is unresolved"})

    out = quota_resume.tick(now=retry_at + 1)
    _settle()
    assert [row["reason"] for row in out] == ["recovery_required"]
    assert len(lab.calls) == 1
    row = quota_resume.read(session)
    assert row["state"] == "retired" and row["reason"] == "an edit boundary is unresolved"
    assert _states(session) == {"follow-1": "pending"}, "still accepted, still visible"


def test_an_unreadable_state_never_executes(lab, monkeypatch):
    """If the authority that could refuse cannot be read, nothing runs — and the
    wait is asked again rather than spent."""
    session = "quota-unreadable"
    retry_at = _run_until_quota(lab, session)

    def _boom(sid, directory=None):
        raise OSError("the recovery record could not be read")

    real = sessions.recovery_state
    monkeypatch.setattr(sessions, "recovery_state", _boom)
    out = quota_resume.tick(now=retry_at + 1)
    _settle()
    assert [row["reason"] for row in out] == ["unreadable_state"]
    assert len(lab.calls) == 1
    assert quota_resume.read(session)["state"] == "waiting"

    # Readable again: the wait was kept, not spent, and it still starts.
    monkeypatch.setattr(sessions, "recovery_state", real)
    assert [row["started"] for row in quota_resume.tick(now=retry_at + 3600)] == [True]
    _settle()
    assert [call["entry"] for call in lab.calls[1:]] == ["follow-1"]


def test_a_changed_setting_after_acceptance_does_not_reach_the_admitted_run(lab):
    """The wait admits the request as accepted: the freeze is what runs."""
    session = "quota-frozen"
    retry_at = _run_until_quota(lab, session)
    frozen = task_inbox.get(session, "follow-1")["config"]["frozen"]
    lab.settings["MODEL"] = "model-changed-afterwards"

    quota_resume.tick(now=retry_at + 1)
    _settle()
    assert [call["entry"] for call in lab.calls[1:]] == ["follow-1"]
    assert task_inbox.get(session, "follow-1")["config"]["frozen"] == frozen
    assert frozen.get("model") != "model-changed-afterwards"


# ------------------------------------------------------- the store, unhappy

def _write_raw(session, record):
    path = session_owner.sidecar_path(session, quota_resume.SUBDIR, ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, allow_nan=True)


def test_malformed_records_are_never_read_as_waits(lab):
    """A wait decides whether work starts on its own, so everything the host
    cannot read as a record is refused outright — not coerced into one."""
    good = dict(quota_resume.record("quota-good", retry_at=int(time.time() + SOON),
                                    entry_id="follow-1"))
    for bad in (dict(good, retry_at=True), dict(good, retry_at="2000000000"),
                dict(good, retry_at=2000000000.0), dict(good, at=float("nan")),
                dict(good, next_attempt_at=float("inf")), dict(good, at=True),
                dict(good, claimed_at="soon"), dict(good, claimed_by=True),
                dict(good, attempts=1.5), dict(good, state="running"),
                dict(good, state="admitting", claimed_at=0.0), dict(good, nonce=""),
                dict(good, entry=""), dict(good, version=1),
                dict(good, session="somebody-else")):
        _write_raw("quota-bad", dict(bad, session=bad.get("session", "quota-bad")))
        assert quota_resume.read("quota-bad") is None, bad
        assert quota_resume.status("quota-bad") is None
        assert [row["session"] for row in quota_resume.records()] == ["quota-good"]
        assert quota_resume.tick(now=time.time() + SOON + 1000) or True

    _write_raw("quota-bad", {"version": 2, "session": "quota-bad", "state": "waiting"})
    assert quota_resume.read("quota-bad") is None
    _settle()


def test_a_reset_time_that_is_not_an_epoch_records_nothing(lab):
    for value in (True, "2000000000", 2000000000.0, None, float("nan"), -1, 0):
        assert quota_resume.record("quota-typed", retry_at=value,
                                   entry_id="follow-1") is None
    assert quota_resume.record("quota-typed", retry_at=int(time.time() + SOON),
                               entry_id="") is None
    assert quota_resume.read("quota-typed") is None


def test_a_huge_number_in_the_file_is_refused_not_raised(lab):
    """A reader whose contract is "never raises for a bad file" must survive the
    numbers JSON allows but a float cannot hold: an integer of a thousand digits
    used to reach ``math.isfinite`` and come back out of ``read`` as an
    OverflowError, taking the whole pass with it."""
    assert quota_resume._finite(10 ** 400) is False
    assert quota_resume._finite(-10 ** 400) is False
    assert quota_resume._finite(True) is False and quota_resume._finite(float("nan")) is False
    assert quota_resume._finite(float("inf")) is False and quota_resume._finite(1.5) is True

    good = dict(quota_resume.record("quota-huge-ok", retry_at=int(time.time() + SOON),
                                    entry_id="follow-1"))
    for bad in (dict(good, at=10 ** 400), dict(good, next_attempt_at=10 ** 400),
                dict(good, claimed_at=10 ** 400), dict(good, retry_at=10 ** 400),
                dict(good, attempts=10 ** 400), dict(good, claimed_by=10 ** 400),
                dict(good, at=float("nan")), dict(good, at=True)):
        _write_raw("quota-huge", dict(bad, session="quota-huge"))
        assert quota_resume.read("quota-huge") is None, bad
        assert quota_resume.status("quota-huge") is None
        assert [row["session"] for row in quota_resume.records()] == ["quota-huge-ok"]
    assert quota_resume.tick(now=time.time() - 10_000) == [], "the pass still ran"
    _settle()


def test_a_bounded_scan_is_fair_to_a_late_due_record(lab, monkeypatch):
    """A cap on how many records one pass examines is fine; a cap that always
    examines the *same* first records is not, because a due wait whose id sorts
    last is then never reached.  The window rotates instead."""
    monkeypatch.setattr(quota_resume, "MAX_SCAN", 3)
    monkeypatch.setattr(quota_resume, "_SCAN_CURSOR", "")
    session = "zz-late"                    # sorts after every filler below
    retry_at = _run_until_quota(lab, session)
    for index in range(6):
        assert quota_resume.record("aa-filler-%02d" % index,
                                   retry_at=retry_at + 10_000 + index, entry_id="follow-1")

    scans = []
    real_ids = quota_resume._session_ids
    monkeypatch.setattr(quota_resume, "_session_ids",
                        lambda **kw: scans.append(kw) or real_ids(**kw))

    seen, started = [], []
    for _ in range(3):                     # ceil(7 records / cap of 3) passes
        before = len(scans)
        out = quota_resume.tick(now=retry_at + 1)
        assert len(scans) - before == 1, "one window per pass, not one per scan"
        seen.append(len(quota_resume.records(advance=False)))
        started.extend(row for row in out if row.get("started"))
    _settle()

    assert max(seen) <= quota_resume.MAX_SCAN, "still bounded"
    assert [row["session"] for row in started] == [session], "reached, exactly once"
    assert [call["entry"] for call in lab.calls[1:]] == ["follow-1"]
    assert _states(session) == {"follow-1": "consumed"}


def test_thirty_two_future_waits_cannot_starve_a_due_one(lab):
    """Due time decides what a bounded pass looks at — not the alphabet."""
    now = int(time.time())
    for index in range(quota_resume.MAX_DUE_PER_PASS):
        # Named so that every one of them sorts *before* the due wait.
        assert quota_resume.record("aa-future-%02d" % index, retry_at=now + 10_000 + index,
                                   entry_id="follow-1")
    assert quota_resume.record("zz-due", retry_at=now - 10, entry_id="follow-1")

    due = quota_resume.waiting(now=now, due_only=True)
    assert [row["session"] for row in due] == ["zz-due"]
    # ...and the full ordering is by reset time, still bounded.
    ordered = quota_resume.waiting(now=now, limit=3)
    assert [row["session"] for row in ordered] == ["zz-due", "aa-future-00", "aa-future-01"]
    assert len(quota_resume.records()) == quota_resume.MAX_DUE_PER_PASS + 1


# ---------------------------------------------------------- what a page sees

def test_the_queue_endpoint_shows_the_scheduled_start_and_nothing_private(lab):
    session = "quota-surface"
    retry_at = _run_until_quota(lab, session)

    code, data = _get(lab, "/api/task-inbox?session=" + session)
    assert code == 200
    wait = data["scheduled_wait"]
    assert wait == {"entry": "follow-1", "state": "waiting", "retry_at": retry_at,
                    "next_attempt_at": retry_at, "progress": "scheduled", "reason": ""}, \
        "sanitized: the bound entry, its state, its next try and the host's verdict"
    assert [row["id"] for row in data["entries"] if row["state"] == "pending"] == ["follow-1"]

    # The wait is over, and the endpoint says so rather than leaving a stale time.
    task_inbox.cancel(session, "follow-1", reason="no longer wanted")
    quota_resume.tick(now=retry_at + 1)
    _settle()
    code, data = _get(lab, "/api/task-inbox?session=" + session)
    assert data["scheduled_wait"] is None


def test_a_schedule_that_gave_up_on_itself_is_still_visible(lab):
    """When this module's own bookkeeping fails its way to the end of its
    attempts, the automatic start is gone.  The page used to show nothing at all
    — a queue row that said "Waiting" forever with no timer behind it.  Say so
    instead, and leave the request exactly as startable as it was."""
    session = "quota-gave-up"
    retry_at = _run_until_quota(lab, session)
    when = retry_at + 1
    for attempt in range(quota_resume.MAX_ATTEMPTS):
        held = quota_resume.claim(session, now=when)
        assert held is not None, attempt
        row = quota_resume.failed(session, "the wait could not be written",
                                  nonce=held["nonce"], now=when)
        when = row["next_attempt_at"] or when + 1
    assert row["state"] == "retired" and row["attempts"] == quota_resume.MAX_ATTEMPTS

    assert quota_resume.status(session) == {"entry": "follow-1", "state": "retired",
                                            "retry_at": retry_at, "progress": "stopped",
                                            "next_attempt_at": 0,
                                            "reason": "the wait could not be written"}
    code, data = _get(lab, "/api/task-inbox?session=" + session)
    assert data["scheduled_wait"]["state"] == "retired"
    assert [row["id"] for row in data["entries"] if row["state"] == "pending"] == ["follow-1"]
    assert quota_resume.tick(now=when + 86400) == [], "retired means retired"

    # ...and a person can still start it by hand, the ordinary way.
    lease = session_owner.try_acquire(session, label="manual")
    assert lease is not None
    try:
        assert web_tasks.claim_next(session, lease, ("follow_up",))["id"] == "follow-1"
    finally:
        lease.release()


def test_the_page_reports_the_scheduled_start_on_the_bound_row():
    """The row says this already submitted task starts after the server's time,
    keeps its cancel control, and owns no timer or probe of its own."""
    path = os.path.join(HERE, "harness", "webui", "index.html")
    text = open(path, encoding="utf-8").read()
    start = text.index("function renderTaskQueue()")
    end = text.index("function loadTaskQueue()")
    row = text[start:end]
    # The sentence itself lives in one shared helper, so this row, the thread list
    # and Today cannot disagree about what the server said.
    assert "scheduledWaitLabel(wait)" in row
    label = text[text.index("function scheduledWaitLabel("):
                 text.index("function queuedSessionLabel(")]
    assert "Already submitted — starts after" in label
    assert "Automatic start stopped" in label, "a schedule that gave up says so"
    assert "wait.next_attempt_at" in label, "the next try, never a reset that has gone by"
    assert "wait.entry === entry.id" in row, "only the bound entry"
    assert "scheduled_wait" in text and "state.scheduledWait" in row
    assert "setInterval" not in row and "setTimeout" not in row, "no browser-owned timer"
    assert 'button("Remove"' in row, "the existing cancel control stays"


# --------------------------------------------------------------- the ticker

def test_the_ticker_starts_and_stops_cleanly():
    """A server-owned timer with a real shutdown, not a thread left running."""
    thread = quota_resume.start_ticker(interval=0.05)
    assert quota_resume.start_ticker(interval=0.05) is thread, "idempotent"
    assert quota_resume.ticker_status()["running"] is True
    try:
        deadline = time.time() + 5
        while quota_resume.ticker_status()["passes"] < 1 and time.time() < deadline:
            time.sleep(0.02)
        assert quota_resume.ticker_status()["passes"] >= 1
    finally:
        assert quota_resume.stop_ticker() is True
    assert quota_resume.ticker_status()["running"] is False
    assert not thread.is_alive()


def test_a_pass_that_will_not_end_is_not_reported_as_stopped(monkeypatch):
    """The shutdown bug: a join that timed out used to claim success and drop
    the thread's identity, so the next start ran a second pass beside it — and
    a shared stop event let the old one come back to life."""
    entered, release = threading.Event(), threading.Event()

    def _stuck(**kwargs):
        entered.set()
        release.wait(30)
        return []

    monkeypatch.setattr(quota_resume, "tick", _stuck)
    thread = quota_resume.start_ticker(interval=0.05)
    assert entered.wait(5)
    try:
        assert quota_resume.stop_ticker(timeout=0.2) is False, "it is still running"
        assert quota_resume.ticker_status()["running"] is True
        assert thread.is_alive()
        # No second pass beside the first, and no revived stop signal.
        assert quota_resume.start_ticker(interval=0.05) is thread
        assert len([t for t in threading.enumerate()
                    if t.name == "collie-quota-resume" and t.is_alive()]) == 1
    finally:
        release.set()
    assert quota_resume.stop_ticker(timeout=10) is True
    assert not thread.is_alive()
    assert quota_resume.ticker_status()["running"] is False
