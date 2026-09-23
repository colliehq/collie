"""Who owns which tree when a cancel arrives in the middle of a nested tool call.

``execute_code`` running ``bash(...)`` is the one place where Collie holds TWO owned
process trees at once, and terminates them from two threads at the same instant:

    Harness (main thread) ── execute_code script ............... group A
           │                     └─ urlopen → RPC
           └─ RPC handler thread ── /bin/sh → command → child ... group B

Both owners poll the SAME host Stop callback, so a cancel releases both within one
poll interval of each other, and neither waits for the other. If the inner call
happens to finish first, the script receives its result, prints it and **exits** — and
the outer owner then kills a process group whose only remaining member is Collie's own
not-yet-waited-on child.

That half of the race is what fences runs on macOS. Baseline evidence, macos-26-arm64,
public CI run 35811448807: 4 of 30 repetitions of
``test_cancel_nested_python_command_stops_descendants_and_resumes`` failed, each with
``execute_code process-tree termination could not be confirmed ... (PermissionError:
[Errno 1] Operation not permitted)`` — group A — while the inner bash result in the same
payload reported its tree stopped. The same job measured the kernel directly, 12 direct
children out of 12: with the exited child observed as ``Z`` via ``ps`` and NOT reaped,
``killpg(SIGKILL)`` and ``killpg(0)`` both returned EPERM; after the reap, both returned
ESRCH. Linux ran 30/30 green. So the group answers for our own zombie, and only we can
make it stop answering.

Two things are under test here, and their ORDER is the point:

First, the outcome of the race must not decide whether the run is fenced: a denied
signal is not the end of the attempt, because our own child may be the only reason it
was denied.

Second, the destructive signal still goes first and never again. The unreaped child is
the last thing pinning the process group id; reaping before the kill would let the pgid
be recycled under a SIGKILL aimed at it. After the reap the group is only ever ASKED
about, with signal 0 — and a group that still answers stays unconfirmed, however long
we wait.

The group calls are injected: this ordering is invisible on Windows (kernel Job
accounting) and only reachable on POSIX. The injected EPERM mirrors the measured Darwin
behaviour above; the mock is the ordering contract, the CI run is the evidence.
"""
import os
import signal
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import plat, tool_process

SIGKILL = getattr(signal, "SIGKILL", 9)
KILL = "killpg%d" % SIGKILL
PROBE = "killpg0"


class _ExitedChild:
    """A direct child that has exited and has NOT been waited on yet."""

    def __init__(self, pid=4242):
        self.pid = pid
        self.returncode = None
        self.events = []

    def poll(self):
        self.events.append("poll")
        return self.returncode

    def wait(self, timeout=None):
        self.events.append("reap")
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    @property
    def reaped(self):
        return self.returncode is not None


class _LiveChild(_ExitedChild):
    """A direct child that is still running until something waits on it."""

    def wait(self, timeout=None):
        self.events.append("reap")
        self.returncode = -SIGKILL
        return self.returncode


@pytest.fixture
def posix(monkeypatch):
    monkeypatch.setattr(plat, "is_windows", lambda: False)
    monkeypatch.setattr(signal, "SIGKILL", SIGKILL, raising=False)
    return monkeypatch


def _owner(child):
    return tool_process._Owner(child, {"start_new_session": True})


def _inject_group(monkeypatch, child, answer):
    """Record every signal the owner sends to the group, in one order with the reap."""
    def killpg(pgid, sig):
        assert pgid == child.pid, pgid
        child.events.append("killpg%d" % sig)
        return answer(sig)
    monkeypatch.setattr(os, "killpg", killpg, raising=False)


def _after_the_reap(events):
    return events[events.index("reap") + 1:]


def test_a_denied_kill_settles_our_own_child_before_it_judges_the_group(posix, monkeypatch):
    """The lost half of the nested-cancel race: the script already returned.

    Nothing of the command survives — the only member of the group is the child Collie
    has not waited on, and on Darwin that child makes the group deny the SIGKILL itself.
    Giving up there fences a run over Collie's own bookkeeping, and no recovery
    inspection can ever clear it, because the thing to be inspected has already exited.
    """
    child = _ExitedChild()

    def answer(sig):
        if child.reaped:
            raise ProcessLookupError(3, "no such process group")
        raise PermissionError(1, "Operation not permitted")   # measured Darwin answer

    _inject_group(monkeypatch, child, answer)

    confirmed, detail = _owner(child).terminate(timeout_s=.2)

    assert child.reaped, "the group was only still answering because of our own child"
    assert confirmed, "an exited, unreaped child was reported as a surviving tree: %r" % detail
    assert detail == "", detail
    # The order, in full: kill, then settle our child, then ask — and only ask.
    assert child.events[0] == KILL, child.events
    assert child.events[1] == "reap", child.events
    assert _after_the_reap(child.events) == [PROBE], child.events


def test_the_group_is_signalled_before_our_child_is_reaped_even_when_it_has_exited(
        posix, monkeypatch):
    """The pgid-reuse guard, stated on its own.

    Our unreaped child is the last member pinning the group id. Were it reaped first,
    an otherwise empty pgid could be handed to an unrelated group before the SIGKILL
    that follows — so the destructive signal must leave first even though the child is
    already dead and the signal is about to be denied.
    """
    child = _ExitedChild()
    _inject_group(monkeypatch, child, lambda sig: None)       # the group keeps answering

    _owner(child).terminate(timeout_s=.05)

    assert child.events[0] == KILL, child.events
    assert child.events.index(KILL) < child.events.index("reap"), child.events


def test_a_live_child_is_signalled_before_anything_waits_on_it(posix, monkeypatch):
    """A running command is killed, never waited out.

    A ``wait()`` before the signal would hand a cancelled command the whole termination
    budget to keep running in.
    """
    child = _LiveChild()

    def answer(sig):
        if child.reaped:
            raise ProcessLookupError(3, "no such process group")
        return None                      # a live member accepts both the kill and the probe

    _inject_group(monkeypatch, child, answer)

    confirmed, detail = _owner(child).terminate(timeout_s=.5)

    assert confirmed and detail == "", detail
    assert child.events.index(KILL) < child.events.index("reap"), child.events


def test_a_group_that_still_answers_after_the_kill_is_never_confirmed(posix, monkeypatch):
    """The fence, unchanged: only ESRCH is evidence, and EPERM is not extinction.

    A denial that outlives our own child is a group we could neither kill nor account
    for, so the turn stays fenced — and the probe that established it was never a kill.
    """
    child = _ExitedChild()

    def answer(sig):
        raise PermissionError(1, "Operation not permitted")

    _inject_group(monkeypatch, child, answer)

    confirmed, detail = _owner(child).terminate(timeout_s=.05)

    assert not confirmed, "EPERM is not proof that anything stopped"
    assert "PermissionError" in detail, detail
    assert child.reaped, "our own child must be settled before the group is judged"
    assert set(_after_the_reap(child.events)) == {PROBE}, child.events


def test_a_surviving_member_keeps_the_tree_unconfirmed(posix, monkeypatch):
    """Our own child is gone and something else in the group is not: still fenced."""
    child = _ExitedChild()
    _inject_group(monkeypatch, child, lambda sig: None)

    confirmed, detail = _owner(child).terminate(timeout_s=.05)

    assert not confirmed
    assert "still had members" in detail, detail
