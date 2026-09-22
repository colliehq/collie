"""A command kill must reach that command's tree — and nothing else.

``plat.kill_tree`` is the one process-cleanup primitive the timeout, cancel and
supervisor paths all share, and the two platforms scope it differently:
``taskkill /T`` walks the PID tree, while ``killpg`` addresses a process GROUP.
A child started without ``plat.new_group_kwargs()`` inherits Collie's own group
on POSIX, so ``killpg(getpgid(child))`` there is not a tree kill at all — it
SIGKILLs the Collie process issuing it, plus every sibling in that group.

These tests simulate POSIX on any host (the group calls are injected), because
the defect is invisible on Windows and destructive on Linux/macOS — exactly the
shape that only ever gets found in production.
"""
import json
import os
import signal
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import plat

OUR_GROUP = 4242          # the group Collie itself runs in
CHILD_PID = 9001


class FakeProc:
    """Just enough of Popen for the cleanup primitive."""

    def __init__(self, pid=CHILD_PID):
        self.pid = pid
        self.kills = 0

    def kill(self):
        self.kills += 1

    def poll(self):
        return None


@pytest.fixture
def posix(monkeypatch):
    """Run kill_tree's POSIX branch here, recording every group signal it sends."""
    signalled = []
    monkeypatch.setattr(plat, "is_windows", lambda: False)
    # A Windows host has no SIGKILL; without it the POSIX branch would raise and
    # silently fall through to the direct kill, hiding what is under test.
    monkeypatch.setattr(signal, "SIGKILL", getattr(signal, "SIGKILL", 9),
                        raising=False)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)),
                        raising=False)
    return signalled


def test_kill_tree_never_signals_the_group_collie_is_in(posix, monkeypatch):
    """The child shares Collie's group: signalling it would kill Collie itself."""
    monkeypatch.setattr(os, "getpgid", lambda pid: OUR_GROUP, raising=False)
    proc = FakeProc()

    plat.kill_tree(proc)

    assert posix == [], "kill_tree SIGKILLed the process group Collie is running in"
    assert proc.kills == 1, "the direct child must still be killed"


def test_kill_tree_ends_a_group_the_child_leads(posix, monkeypatch):
    """The ordinary owned tree — start_new_session made the child its leader."""
    monkeypatch.setattr(os, "getpgid", lambda pid: pid, raising=False)
    proc = FakeProc()

    plat.kill_tree(proc)

    assert posix == [(CHILD_PID, signal.SIGKILL)]
    assert proc.kills == 0, "the group kill already covers the direct child"


def test_kill_tree_falls_back_to_the_direct_child_when_the_group_kill_fails(
        posix, monkeypatch):
    """A group that vanished between the two calls still owes the caller a kill."""
    monkeypatch.setattr(os, "getpgid", lambda pid: pid, raising=False)

    def boom(pgid, sig):
        raise ProcessLookupError(pgid)

    monkeypatch.setattr(os, "killpg", boom, raising=False)
    proc = FakeProc()

    plat.kill_tree(proc)

    assert proc.kills == 1


def test_kill_tree_on_windows_is_scoped_to_this_pids_tree(monkeypatch):
    """The counterpart guarantee: taskkill /T can only reach this child's tree."""
    calls = []
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat.subprocess, "run",
                        lambda argv, **kw: calls.append(list(argv)))
    monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("killpg on Windows"),
                        raising=False)
    proc = FakeProc()

    plat.kill_tree(proc)

    assert calls == [["taskkill", "/F", "/T", "/PID", str(CHILD_PID)]]
    assert proc.kills == 0


def test_new_group_kwargs_and_kill_tree_agree_about_ownership(monkeypatch):
    """The two helpers are one contract: a group is only killed if one was made.

    ``new_group_kwargs`` deliberately returns nothing for a worker that must stay
    inside an externally guarded group. That is precisely the case where the
    child is not a group leader, and therefore the case kill_tree must not
    widen into a group signal.
    """
    monkeypatch.setattr(plat, "is_windows", lambda: False)
    monkeypatch.delenv("COLLIE_PROCESS_OWNER", raising=False)
    assert plat.new_group_kwargs() == {"start_new_session": True}
    monkeypatch.setenv("COLLIE_PROCESS_OWNER", "slackexec")
    assert plat.new_group_kwargs() == {}


# ------------------------------------------------------- the reachable caller

def test_automation_wall_budget_kill_owns_a_tree_of_its_own(tmp_path, monkeypatch):
    """The hard wall-time kill must be able to end the automation's whole tree.

    ``DefaultCollieRunner`` advertises a "killable child"; on POSIX that is only
    true if the child was started in its own process group. Started without one,
    the budget kill either reached no further than the direct child or (before
    the kill_tree repair) took the scheduler down with it.
    """
    from harness.automations import (AutomationStore, BudgetExceeded, BudgetGuard,
                                     DefaultCollieRunner, TriggerEngine)

    spec = {
        "id": "wall-group", "task": "inspect and report",
        "trigger": {"provider": "timer", "every_s": 1, "fire_immediately": True},
        "workspace": {"mode": "isolated"},
        "permissions": {"read_roots": [str(tmp_path)]},
        "execution": {"provider": "mock", "allow_mock": True},
        "budget": {"max_wall_s": .2},
    }

    class HungProcess:
        returncode = None
        def poll(self):
            return None
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("automation", timeout)

    started = {}
    killed = []
    monkeypatch.setattr(plat, "is_windows", lambda: False)       # simulate POSIX
    monkeypatch.delenv("COLLIE_PROCESS_OWNER", raising=False)
    monkeypatch.setattr("harness.automations.subprocess.Popen",
                        lambda *a, **kw: (started.update(kw), HungProcess())[1])
    monkeypatch.setattr(plat, "kill_tree", lambda proc: killed.append(proc))
    with AutomationStore(str(tmp_path / "automations.db")) as store:
        store.upsert(spec, now=1)
        TriggerEngine(store).tick(1)
        request = json.loads(store.executions()[0]["request_json"])
        request["resolved_workspace"] = str(tmp_path)
        guard = BudgetGuard(store, request["execution_id"], request["budget"],
                            request=request)
        with pytest.raises(BudgetExceeded, match="hard wall-time"):
            DefaultCollieRunner()(request, guard)
    assert killed, "the wall budget must still kill the child"
    assert started.get("start_new_session") is True, (
        "the automation child shares Collie's process group, so the budget kill "
        "cannot reach the tree it was meant to bound")
    assert "creationflags" not in started, "creationflags is Windows-only"
