"""The pending-work projection has to carry the schedule, or every surface guesses.

``/api/task-inbox?session=`` has always said "this accepted request starts after
T".  ``/api/task-inbox/pending`` — the one lane the Today page and the Needs You
badge read — did not, so a request the server had already scheduled to start by
itself arrived at the browser indistinguishable from one stranded waiting for a
person.  What is locked here is that the schedule crosses, sanitized the same
way, and that nothing which still needs a person quietly gains one.

The second half of this module locks the harder half of that claim: *who decides*
whether a schedule is healthy.  A published ``retry_at`` alone is not enough —
the record's own backoff moves the next try past it, and a due wait is picked up
by a pass that only runs every ``interval`` seconds.  So the host publishes its
own verdict, computed from its own clock, its own backoff and the same liveness
evidence the recovery pass uses, and a browser never re-derives it.
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_web_task_inbox import CONFIG, _get, _post, web                 # noqa: E402,F401

from harness import quota_resume                                        # noqa: E402

PAST = 1_700_000_000              # a fixed epoch, so nothing here depends on the wall clock


def _queue(base, token, sid, entry_id, text):
    code, accepted = _post(base, token, "/api/task-inbox",
                           {"session": sid, "id": entry_id, "text": text,
                            "mode": "follow_up", "config": CONFIG, "client": "web"})
    assert code == 200 and accepted["accepted"] is True
    return accepted


def _row(payload, sid):
    rows = [row for row in payload["sessions"] if row["session"] == sid]
    assert len(rows) == 1, payload
    return rows[0]


def _session(base, token, state, sid, entry_id="req-1", text="keep going after the reset"):
    from harness import sessions
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    _queue(base, token, sid, entry_id, text)
    return sid


def _ticker(monkeypatch, *, now, running=True, last_at=None, interval=30.0):
    """Pin the pass's own heartbeat.

    Whether a due wait is still on its way is a question about *this process's*
    timer thread, so the answer has to come from the thread's own record of its
    last pass — never from a caller's or a browser's clock.
    """
    info = {"passes": 1, "started": 0, "last_error": "", "interval": interval,
            "last_at": float(now if last_at is None else last_at), "running": running}
    monkeypatch.setattr(quota_resume, "ticker_status", lambda: dict(info))


def _private(wait):
    return [key for key in ("nonce", "claimed_by", "claimed_at", "attempts", "deferrals",
                            "run", "version", "session") if key in wait]


def test_pending_projection_carries_the_scheduled_start(web):
    """The fact that separates "waiting for the clock" from "waiting for you"."""
    base, token, state = web
    from harness import sessions

    scheduled, stranded = "queued-scheduled", "queued-stranded"
    for sid in (scheduled, stranded):
        sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    _queue(base, token, scheduled, "req-1", "keep going after the reset")
    _queue(base, token, stranded, "req-1", "this one nothing is going to start")

    retry_at = 2_000_000_000
    assert quota_resume.record(scheduled, retry_at=retry_at, entry_id="req-1") is not None

    code, payload = _get(base, token, "/api/task-inbox/pending")
    assert code == 200

    # The scheduled one says so, with exactly the sanitized shape the
    # per-conversation endpoint publishes — the next try and the host's verdict
    # about it, and no pid, claim time or attempt counter.
    wait = _row(payload, scheduled)["scheduled_wait"]
    assert wait == {"entry": "req-1", "state": "waiting", "retry_at": retry_at,
                    "next_attempt_at": retry_at, "progress": "scheduled", "reason": ""}
    assert _private(wait) == []

    # And the one nothing is going to start keeps saying nothing is going to
    # start it. `null`, not absent: "we looked and there is no schedule" is a
    # different claim from "this build does not report schedules".
    assert _row(payload, stranded)["scheduled_wait"] is None

    # The schedule is reported beside the truths it must never overwrite.
    for sid in (scheduled, stranded):
        row = _row(payload, sid)
        assert row["owner_busy"] is False and row["pending"] == 1

    # Withdrawing the request ends the schedule, and the lane says so rather
    # than leaving a time behind that no longer starts anything.
    from harness import task_inbox
    task_inbox.cancel(scheduled, "req-1", reason="no longer wanted")
    quota_resume.clear(scheduled, reason="withdrawn")
    code, payload = _get(base, token, "/api/task-inbox/pending")
    assert [row["session"] for row in payload["sessions"]] == [stranded]


def test_a_start_time_still_ahead_needs_no_heartbeat_to_be_believed(web, monkeypatch):
    """While T is in the future the record *is* the whole claim — "the server
    intends to start this at T" — and a timer's last pass says nothing about an
    event that has not come due.  A stopped ticker must not flip a healthy future
    schedule into an alarm every time the process has just restarted."""
    base, token, state = web
    sid = _session(base, token, state, "queued-future")
    assert quota_resume.record(sid, retry_at=PAST + 3600, entry_id="req-1") is not None
    _ticker(monkeypatch, now=PAST, running=False, last_at=0.0)
    assert quota_resume.status(sid, now=PAST)["progress"] == "scheduled"


def test_a_deferred_wait_publishes_its_next_try_and_stays_scheduled(web, monkeypatch):
    """A conversation that was busy at the reset is the case this feature exists
    for.  ``defer`` leaves ``retry_at`` in the past and puts the real next try in
    ``next_attempt_at``; publishing only the former calls a healthy 60s wait
    overdue."""
    base, token, state = web
    sid = _session(base, token, state, "queued-deferred")
    assert quota_resume.record(sid, retry_at=PAST, entry_id="req-1") is not None
    held = quota_resume.claim(sid, now=PAST + 1)
    assert held is not None
    row = quota_resume.defer(sid, "this conversation was running when the reset came",
                             nonce=held["nonce"], now=PAST + 1)
    assert row["next_attempt_at"] == PAST + 1 + quota_resume.BUSY_BACKOFF

    _ticker(monkeypatch, now=PAST + 2)
    wait = quota_resume.status(sid, now=PAST + 2)
    assert wait["next_attempt_at"] == int(PAST + 1 + quota_resume.BUSY_BACKOFF)
    assert wait["progress"] == "scheduled", "a 60s busy backoff is the feature working"
    assert wait["retry_at"] == PAST, "the provider's reset is still reported as itself"
    assert _private(wait) == []


def test_a_bookkeeping_backoff_stays_scheduled_too(web, monkeypatch):
    """Same for the retry ladder: 30s, then 120s, then 600s of waiting are this
    module retrying its own failure, not a start that went missing."""
    base, token, state = web
    sid = _session(base, token, state, "queued-backoff")
    assert quota_resume.record(sid, retry_at=PAST, entry_id="req-1") is not None
    held = quota_resume.claim(sid, now=PAST + 1)
    row = quota_resume.failed(sid, "the wait could not be written",
                              nonce=held["nonce"], now=PAST + 1)
    assert row["state"] == "waiting"
    _ticker(monkeypatch, now=PAST + 2)
    wait = quota_resume.status(sid, now=PAST + 2)
    assert wait["progress"] == "scheduled"
    assert wait["next_attempt_at"] == int(PAST + 1 + quota_resume.RETRY_BACKOFF[0])


@pytest.mark.parametrize("elapsed,progress", [
    (0, "scheduled"),
    (5, "queued"),
    (2 * 30.0, "queued"),
    (2 * 30.0 + 1, "queued"),
    (600, "queued"),
])
def test_a_due_wait_is_queued_while_its_ticker_is_healthy(
        web, monkeypatch, elapsed, progress):
    """A bounded pass may need several ticks; age alone is not a failed start."""
    base, token, state = web
    sid = _session(base, token, state, "queued-due")
    assert quota_resume.record(sid, retry_at=PAST, entry_id="req-1") is not None
    now = PAST + elapsed
    _ticker(monkeypatch, now=now, interval=30.0)
    assert quota_resume.status(sid, now=now)["progress"] == progress


def test_a_pass_that_is_not_running_makes_a_due_wait_a_decision(web, monkeypatch):
    """Nothing is polling, so nothing is going to start this: a person is the
    only thing left that can, and the row has to say so."""
    base, token, state = web
    sid = _session(base, token, state, "queued-no-ticker")
    assert quota_resume.record(sid, retry_at=PAST, entry_id="req-1") is not None
    _ticker(monkeypatch, now=PAST + 5, running=False, last_at=0.0)
    assert quota_resume.status(sid, now=PAST + 5)["progress"] == "stalled"


@pytest.mark.parametrize("last_at", [0.0, PAST - 300])
def test_a_heartbeat_that_cannot_be_dated_is_unknown_not_healthy(web, monkeypatch, last_at):
    """A live thread that has not completed a dateable pass is not evidence of
    progress.  Unknown stays unknown — visible, and never called "starting"."""
    base, token, state = web
    sid = _session(base, token, state, "queued-stale-beat")
    assert quota_resume.record(sid, retry_at=PAST, entry_id="req-1") is not None
    _ticker(monkeypatch, now=PAST + 5, last_at=last_at)
    assert quota_resume.status(sid, now=PAST + 5)["progress"] == "unknown"


def test_a_healthy_bounded_pass_does_not_make_its_backlog_a_user_decision(web, monkeypatch):
    from harness import web_tasks
    base, token, state = web
    started = []
    for sid in ("backlog-a", "backlog-b", "backlog-c"):
        _session(base, token, state, sid)
        quota_resume.record(sid, retry_at=PAST, entry_id="req-1")

    def admit(row):
        started.append(row["session"])
        quota_resume.retire(row["session"], "test admission delivered", nonce=row["nonce"])
        return {"session": row["session"], "started": True}

    monkeypatch.setattr(web_tasks, "admit_after_quota_reset", admit)
    now = PAST + 600
    _ticker(monkeypatch, now=now)
    quota_resume.tick(now=now, limit=1)
    wait = quota_resume.status("backlog-c", now=now)
    quota_resume.tick(now=now + 30, limit=1)
    quota_resume.tick(now=now + 60, limit=1)
    assert started == ["backlog-a", "backlog-b", "backlog-c"]
    assert wait["progress"] == "queued", "healthy bounded admission will handle this without a person"


def test_a_recoverable_claim_waits_for_the_healthy_ticker_without_asking_a_person(web, monkeypatch):
    from harness import web_tasks
    base, token, state = web
    sid = _session(base, token, state, "recoverable-claim")
    quota_resume.record(sid, retry_at=PAST, entry_id="req-1")
    held = quota_resume.claim(sid, now=PAST + 1)
    assert held is not None
    now = PAST + quota_resume.CLAIM_GRACE + 10
    _ticker(monkeypatch, now=now)
    assert quota_resume.claim_evidence(held, now=now) == "abandoned"
    wait = quota_resume.status(sid, now=now)
    started = []

    def admit(row):
        started.append(row["session"])
        quota_resume.retire(sid, "test recovered admission delivered", nonce=row["nonce"])
        return {"session": sid, "started": True}

    monkeypatch.setattr(web_tasks, "admit_after_quota_reset", admit)
    quota_resume.tick(now=now)
    assert started == [sid]
    assert wait["progress"] == "queued", "recover() owns this retry; the user need not restart it"


@pytest.mark.parametrize("outcome", ["consumed", "canceled"])
def test_a_resolved_claim_does_not_lend_its_old_schedule_to_another_request(web, monkeypatch, outcome):
    from harness import task_inbox, session_owner, sessions
    base, token, state = web
    sid = _session(base, token, state, "finished-schedule")
    _queue(base, token, sid, "req-2", "a different request with no automatic start")
    quota_resume.record(sid, retry_at=PAST, entry_id="req-1")
    held = quota_resume.claim(sid, now=PAST + 1)
    if outcome == "canceled":
        task_inbox.cancel(sid, "req-1", reason="withdrawn")
    else:
        with session_owner.own(sid, label="test-delivery") as owner:
            entry = task_inbox.claim(sid, owner, limit=1)[0]
            messages = sessions.load_checked(sid)["session"]["messages"]
            messages.append(task_inbox.journal_message(entry))
            sessions.checkpoint(sid, messages, project="web", cwd=str(state),
                                run_id="test-delivery", terminal=True)
            task_inbox.ack(sid, owner, "req-1")
    now = PAST + quota_resume.CLAIM_GRACE + 10
    _ticker(monkeypatch, now=now)
    assert quota_resume.claim_evidence(held, now=now) in ("delivered", "gone")
    assert quota_resume.status(sid, now=now) is None
    code, payload = _get(base, token, "/api/task-inbox/pending")
    row = _row(payload, sid)
    assert code == 200 and row["pending"] == 1 and row["owner_busy"] is False
    assert row["scheduled_wait"] is None, "the other accepted request remains an unscheduled decision"


def test_the_first_live_ticker_pass_has_a_bounded_startup_grace(web, monkeypatch):
    base, token, state = web
    sid = _session(base, token, state, "startup-wait")
    quota_resume.record(sid, retry_at=PAST, entry_id="req-1")
    entered, release = threading.Event(), threading.Event()
    assert quota_resume.stop_ticker()
    monkeypatch.setattr(quota_resume, "_TICK_STATUS", {
        "passes": 0, "started": 0, "last_error": "", "last_at": 0.0, "interval": 0.0})

    def first_pass():
        entered.set()
        release.wait(10)
        return []

    monkeypatch.setattr(quota_resume, "tick", first_pass)
    before = time.time()
    quota_resume.start_ticker(interval=30)
    try:
        assert entered.wait(5)
        info = quota_resume.ticker_status()
        assert info["running"] and info["last_at"] == 0
        assert info.get("started_at", 0) >= before
        assert quota_resume.status(sid)["progress"] == "queued"
        # Starting a pass is not evidence it stays healthy indefinitely.
        assert quota_resume.status(sid, now=before + 120)["progress"] == "unknown"
    finally:
        release.set()
        assert quota_resume.stop_ticker()


def test_a_fresh_claim_is_progress_for_exactly_the_grace_the_pass_gives_it(web, monkeypatch):
    """``admitting`` is an admission in flight, by the same rule the recovery pass
    uses: a claim younger than CLAIM_GRACE is assumed alive.  With a 30s pass and
    10s polling, seeing one is ordinary, and calling it a decision is wrong."""
    base, token, state = web
    sid = _session(base, token, state, "queued-admitting")
    assert quota_resume.record(sid, retry_at=PAST, entry_id="req-1") is not None
    held = quota_resume.claim(sid, now=PAST + 1)
    assert held is not None and held["state"] == "admitting"
    _ticker(monkeypatch, now=PAST + 2)

    wait = quota_resume.status(sid, now=PAST + 2)
    assert wait["state"] == "admitting" and wait["progress"] == "starting"
    assert _private(wait) == [], "a claim is in flight; whose pid it is, is nobody's business"

    # Past the grace, a healthy ticker owns recovery of the abandoned claim.
    late = PAST + 1 + quota_resume.CLAIM_GRACE + 1
    _ticker(monkeypatch, now=late)
    assert quota_resume.status(sid, now=late)["progress"] == "queued"


def test_a_claim_whose_liveness_cannot_be_read_is_unknown(web, monkeypatch):
    """The OS could not answer, so nothing here knows.  Not "starting" — that
    would promise progress — and not dropped either."""
    base, token, state = web
    sid = _session(base, token, state, "queued-unknowable")
    assert quota_resume.record(sid, retry_at=PAST, entry_id="req-1") is not None
    held = quota_resume.claim(sid, now=PAST + 1)
    assert held is not None
    late = PAST + 1 + quota_resume.CLAIM_GRACE + 1
    _ticker(monkeypatch, now=late)
    monkeypatch.setattr(quota_resume.session_owner, "probe_busy", lambda _session: None)
    assert quota_resume.status(sid, now=late)["progress"] == "unknown"


def test_a_schedule_that_gave_up_stays_in_the_pending_projection(web):
    """A retired schedule is the case a person most needs to see."""
    base, token, state = web
    from harness import sessions

    sid = "queued-retired"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    _queue(base, token, sid, "req-1", "this was scheduled once")
    retry_at = 1_000_000_000
    assert quota_resume.record(sid, retry_at=retry_at, entry_id="req-1") is not None
    when = retry_at + 1
    for attempt in range(quota_resume.MAX_ATTEMPTS):
        held = quota_resume.claim(sid, now=when)
        assert held is not None, attempt
        row = quota_resume.failed(sid, "the wait could not be written",
                                  nonce=held["nonce"], now=when)
        when = row["next_attempt_at"] or when + 1
    assert row["state"] == "retired"

    code, payload = _get(base, token, "/api/task-inbox/pending")
    assert code == 200
    wait = _row(payload, sid)["scheduled_wait"]
    assert wait["state"] == "retired" and wait["reason"] == "the wait could not be written"
    assert wait["progress"] == "stopped", "it stopped being scheduled; say so plainly"
    assert wait["next_attempt_at"] == 0, "nothing is going to try again"
    assert _row(payload, sid)["pending"] == 1, "still queued, still startable by hand"


def test_an_unreadable_schedule_never_costs_the_row_its_visibility(web, monkeypatch):
    """The projection is a listing. A schedule it cannot read is not a reason to
    drop work from it — that would hide the request itself behind a failure in
    the thing that was only ever annotating it."""
    base, token, state = web
    from harness import sessions

    sid = "queued-unreadable"
    sessions.append_exchange(sid, "hello", "hi", project="web", cwd=str(state))
    _queue(base, token, sid, "req-1", "still waiting for someone")

    def explode(_session, **_kwargs):
        raise OSError("the schedule store cannot be read")

    monkeypatch.setattr(quota_resume, "status", explode)
    code, payload = _get(base, token, "/api/task-inbox/pending")
    assert code == 200
    row = _row(payload, sid)
    assert row["scheduled_wait"] is None, "unknown reads as unscheduled, never as scheduled"
    assert row["pending"] == 1
