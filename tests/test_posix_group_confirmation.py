"""A transient probe denial after SIGKILL is neither extinction nor a permanent failure."""
import os

import pytest

from harness import tool_process, verification


@pytest.fixture(params=["tool", "verification"])
def terminate(request):
    if request.param == "tool":
        return lambda timeout: tool_process._kill_owned_group(4242, timeout)
    return lambda timeout: verification._terminate_owned_posix_group(4242, timeout_s=timeout)


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
    calls = []
    def killpg(pgid, signal):
        calls.append(signal)
        raise PermissionError(1, "not owned")
    monkeypatch.setattr(os, "killpg", killpg, raising=False)
    confirmed, detail = terminate(1)
    assert not confirmed and "PermissionError" in detail
    assert calls == [9]
