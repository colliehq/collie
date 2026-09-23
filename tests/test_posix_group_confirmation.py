"""A transient probe denial after SIGKILL is neither extinction nor a permanent failure.

Both copies of the owned-group contract are exercised through the same cases, because
``tool_process._kill_owned_group`` and ``verification._terminate_owned_posix_group``
make the same promise to two different callers and may not drift apart. They reach
their reaper differently — one is handed a callable, the other calls ``proc.poll()`` —
so the seam is parametrised, not the behaviour.
"""
import os

import pytest

from harness import tool_process, verification


@pytest.fixture(params=["tool", "verification"])
def terminate(request):
    if request.param == "tool":
        return lambda timeout: tool_process._kill_owned_group(4242, timeout)
    return lambda timeout: verification._terminate_owned_posix_group(4242, timeout_s=timeout)


@pytest.fixture(params=["tool", "verification"])
def terminate_with_reaper(request):
    """The same two helpers, each given a way to wait on the caller's own direct child."""
    def build(reap):
        if request.param == "tool":
            return lambda timeout: tool_process._kill_owned_group(4242, timeout, reap=reap)
        proc = type("_Proc", (), {"poll": staticmethod(reap)})()
        return lambda timeout: verification._terminate_owned_posix_group(
            4242, proc=proc, timeout_s=timeout)
    return build


def test_successful_kill_waits_through_probe_denial_for_esrch(monkeypatch, terminate):
    signals = []
    def killpg(pgid, signal):
        assert pgid == 4242
        signals.append(signal)
        if signal:
            return
        if len(signals) == 2:
            raise PermissionError(1, "group is disappearing")
        raise ProcessLookupError(3, "group is gone")
    monkeypatch.setattr(os, "killpg", killpg, raising=False)
    assert terminate(1) == (True, "")
    assert signals == [9, 0, 0]


def test_persistent_probe_denial_is_never_reported_as_extinction(monkeypatch, terminate):
    def killpg(pgid, signal):
        if not signal:
            raise PermissionError(1, "inspection denied")
    monkeypatch.setattr(os, "killpg", killpg, raising=False)
    confirmed, detail = terminate(.05)
    assert not confirmed and "PermissionError" in detail


def test_denied_kill_still_fails_without_claiming_to_have_stopped_anything(monkeypatch, terminate):
    """With no reaper there is no child of ours to blame, so a denial ends it at once.

    Unchanged, and deliberately: the reap-and-ask-again path below exists only because
    the caller has a direct child that could be the sole reason for the denial.
    """
    calls = []
    def killpg(pgid, signal):
        calls.append(signal)
        raise PermissionError(1, "not owned")
    monkeypatch.setattr(os, "killpg", killpg, raising=False)
    confirmed, detail = terminate(1)
    assert not confirmed and "PermissionError" in detail
    assert calls == [9]


# ── a denied FIRST signal, which Darwin makes ordinary ───────────────────────
#
# Measured on macos-26-arm64 (public CI run 35811448807), 12 direct children out of 12:
# while the exited child is observed as Z by ``ps`` and not yet reaped, killpg answers
# EPERM for SIGKILL and for signal 0 alike; after the reap, both answer ESRCH. The
# injected answers below are that measurement, replayed so the ORDER can be asserted
# from any host. They are not themselves evidence of anything.

def _record(monkeypatch, answer):
    """Log every signal sent to the group, interleaved with the caller's own reap."""
    events = []
    def killpg(pgid, signal):
        assert pgid == 4242, pgid
        events.append(signal)
        return answer(signal)
    monkeypatch.setattr(os, "killpg", killpg, raising=False)
    return events


def _destructive_after_the_reap(events, reaped_at):
    return [s for s in events[reaped_at:] if s]


def test_a_denied_first_signal_settles_our_child_and_then_asks_the_group(
        monkeypatch, terminate_with_reaper):
    """The group was denying us because of our own zombie; it stops once that is gone."""
    state = {"zombie": True, "reaped_at": None}

    def answer(signal):
        if state["zombie"]:
            raise PermissionError(1, "Operation not permitted")
        raise ProcessLookupError(3, "no such process group")

    events = _record(monkeypatch, answer)

    def reap():
        state["zombie"] = False
        if state["reaped_at"] is None:
            state["reaped_at"] = len(events)

    confirmed, detail = terminate_with_reaper(reap)(1)

    assert confirmed and detail == "", detail
    assert events[0] == 9, "the destructive signal must go first, before any reap: %r" % events
    assert state["reaped_at"] == 1, "the reap belongs between the kill and the probe: %r" % events
    assert events[1:] == [0], "the group may only be ASKED about after the reap: %r" % events


def test_a_recycled_group_id_is_never_signalled_destructively_after_the_reap(
        monkeypatch, terminate_with_reaper):
    """Reaping frees the pgid. Whatever answers next may be a stranger, so only ask.

    The group here keeps answering after our child is gone — the same shape as a pgid
    handed to an unrelated group. It must end unconfirmed, and it must end without our
    having sent a second SIGKILL to somebody else's tree.
    """
    state = {"reaped_at": None}

    def answer(signal):
        if state["reaped_at"] is None:
            raise PermissionError(1, "Operation not permitted")
        return None                       # something is alive under this pgid now

    events = _record(monkeypatch, answer)

    def reap():
        if state["reaped_at"] is None:
            state["reaped_at"] = len(events)

    confirmed, detail = terminate_with_reaper(reap)(.05)

    assert not confirmed, "a group that still answers was reported extinct"
    assert "PermissionError" in detail, detail
    assert events[0] == 9 and state["reaped_at"] == 1, events
    assert _destructive_after_the_reap(events, state["reaped_at"]) == [], events


def test_a_denial_that_outlives_our_child_is_still_never_extinction(
        monkeypatch, terminate_with_reaper):
    """EPERM stays uninterpretable: we reaped, we asked again, and we were still denied."""
    reaps = []

    def answer(signal):
        raise PermissionError(1, "Operation not permitted")

    events = _record(monkeypatch, answer)
    confirmed, detail = terminate_with_reaper(lambda: reaps.append(len(events)))(.05)

    assert not confirmed and "PermissionError" in detail, detail
    assert reaps and reaps[0] == 1, "our own child is settled before the group is judged: %r" % events
    assert _destructive_after_the_reap(events, reaps[0]) == [], events
