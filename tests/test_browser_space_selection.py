"""An explicit browser space chosen inside a run must be the one its commands use.

A Web run is bound to its own lane (web-<session>) through a ContextVar. A tool that names a
different space (browser_open / browser_tabs space=...) used to write only the process-wide
fallback, which the bound lane always outranks, so the choice was silently ignored -- and leaked
into every unbound caller in the same process. These tests drive the REAL _call over a stubbed
localhost round trip, because _call is where the space is stamped onto each command.
"""
import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness import browserbridge as bb


class _Wire:
    """Record every command the real _call would send to the bridge server."""

    def __init__(self):
        self.sent = []
        self.lock = threading.Lock()

    def urlopen(self, req, timeout=None):
        with self.lock:
            self.sent.append(json.loads((getattr(req, "data", None) or b"{}").decode()))
        payload = json.dumps({"ok": True, "data": {}}).encode()

        class _Reply:
            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Reply()


@pytest.fixture
def wire(monkeypatch):
    stub = _Wire()
    monkeypatch.setattr(bb.urllib.request, "urlopen", stub.urlopen)
    monkeypatch.setattr(bb, "_ensure_server", lambda port: True)
    return stub


@pytest.fixture(autouse=True)
def _fresh_process_space(monkeypatch):
    monkeypatch.setattr(bb, "_CURRENT_SPACE", [None])
    monkeypatch.delenv("COLLIE_BROWSER_SPACE", raising=False)


def test_explicit_space_wins_inside_a_web_run(wire):
    before = bb._space()
    with bb.browser_space("web-capsule", release=True):
        # A tool may run in a copy of the run's context; its choice must still reach the run.
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(contextvars.copy_context().run, bb.BrowserTabs().run,
                        {"action": "status", "space": "live-work-surface"}, {}).result()
        assert bb._space() == "live-work-surface"
        bb.BrowserOpen().run({"url": "https://excalidraw.com/", "space": "live-work-surface"}, {})
        during = list(wire.sent)
    assert [cmd["action"] for cmd in during] == ["status", "open"]
    assert all(cmd["space"] == "live-work-surface" for cmd in during)
    # The choice belongs to this run, not to every unbound caller in the process.
    assert bb._CURRENT_SPACE[0] is None
    assert bb._space() == before


def test_run_end_finalizes_its_own_lane_not_the_one_it_selected(wire):
    with bb.browser_space("web-session-1", release=True):
        bb.BrowserOpen().run({"url": "https://excalidraw.com/", "space": "live-work-surface"}, {})
    opened, finalized = wire.sent[0], wire.sent[-1]
    assert opened["action"] == "open" and opened["space"] == "live-work-surface"
    assert finalized["action"] == "finalize" and finalized["close_owned"] is False
    # The run's own lane is released by name. The selected lane may be someone else's
    # (here the tab the user attached to Live), so ending this run must not finalize it.
    assert finalized["space"] == "web-session-1"
    assert not [cmd for cmd in wire.sent
                if cmd["action"] == "finalize" and cmd["space"] == "live-work-surface"]


def test_concurrent_runs_never_share_selected_spaces():
    barrier = threading.Barrier(2)

    def run(name):
        with bb.browser_space("web-" + name):
            bb._select_space("board-" + name)
            barrier.wait(timeout=3)
            return bb._space()

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(run, name) for name in ("one", "two")]
        assert [job.result() for job in jobs] == ["board-one", "board-two"]


def test_nested_space_restores_outer_selection():
    with bb.browser_space("web-outer"):
        bb._select_space("live-work-surface")
        with bb.browser_space("temporary-inspection"):
            bb._select_space("temporary-board")
            assert bb._space() == "temporary-board"
        assert bb._space() == "live-work-surface"


def test_unbound_caller_still_selects_the_process_space(wire):
    # A CLI run is one process with no bound lane: the choice stays sticky process-wide.
    bb.BrowserOpen().run({"url": "https://example.com", "space": "research"}, {})
    bb.BrowserRead().run({}, {})
    assert [cmd["space"] for cmd in wire.sent] == ["research", "research"]
    assert bb._CURRENT_SPACE[0] == "research"
