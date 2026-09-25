"""A killed agent process group is confirmed gone even when Darwin answers EPERM for a while.

On macOS, killpg() on a group whose only members are killed, not-yet-reaped processes answers
EPERM, and ESRCH only once they are reaped (tool_process already waits this out). The agent
transport gave up on the first EPERM, so a timed-out Codex run on a loaded macOS runner raised
"process-tree extinction could not be confirmed" for a tree that was, a moment later, gone.
"""
import errno
import signal

import pytest

from harness import agent_runners


class _Proc:
    _collie_process_group = 4242
    returncode = -9

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def _darwin_killpg(eperm_probes, *, eperm_on_kill=False):
    calls = []

    def killpg(pgid, sig):
        calls.append(sig)
        if sig != 0:
            if eperm_on_kill:
                raise PermissionError(errno.EPERM, "Operation not permitted")
            return
        if sum(1 for s in calls if s == 0) <= eperm_probes:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        raise ProcessLookupError(errno.ESRCH, "No such process")
    return killpg, calls


@pytest.mark.parametrize("eperm_on_kill", [False, True], ids=["probe", "kill-and-probe"])
def test_eperm_while_the_group_disappears_is_waited_out(monkeypatch, eperm_on_kill):
    killpg, calls = _darwin_killpg(5, eperm_on_kill=eperm_on_kill)
    monkeypatch.setattr(agent_runners.os, "killpg", killpg, raising=False)
    assert agent_runners._terminate_posix_group(_Proc(), timeout_s=2.0) is True
    assert calls[0] == getattr(signal, "SIGKILL", 9)
    assert calls.count(0) == 6                     # five EPERM answers, then ESRCH


def test_a_group_that_never_goes_is_still_reported(monkeypatch):
    killpg, _calls = _darwin_killpg(10 ** 9)
    monkeypatch.setattr(agent_runners.os, "killpg", killpg, raising=False)
    assert agent_runners._terminate_posix_group(_Proc(), timeout_s=0.2) is False
