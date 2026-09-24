"""A bridge with no extension behind it says so at once, and only when that is really the case.

On the developer's machine a closed browser turned every Mission tick into 88 seconds of timeouts
(a 60s form read, then seven 4s origin checks), 140 in a row over a day, each reporting only that
the browser "did not respond".
"""
import json
import os
import threading
import time

import pytest

from harness import browserbridge as bb


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(bb, "_home", lambda: str(tmp_path))
    return tmp_path


def _audit(home):
    path = os.path.join(str(home), "bridge-audit.log")
    if not os.path.exists(path):
        return []
    return [json.loads(x) for x in open(path, encoding="utf-8").read().splitlines() if x.strip()]


def test_an_extension_gone_for_minutes_is_reported_without_waiting(home):
    bridge = bb._Bridge()
    bridge.last_poll = time.time() - 600
    t0 = time.monotonic()
    res = bridge.enqueue({"action": "form_snapshot", "space": "mission"}, timeout=30)
    assert time.monotonic() - t0 < 1.0
    assert res["ok"] is False and res["extension_connected"] is False
    assert "not connected" in res["error"] and "10 min ago" in res["error"]
    assert "nothing was sent" in res["error"]
    assert bridge.pending.empty(), "a browser opened later must not run a command nobody awaits"
    assert [r["outcome"] for r in _audit(home)] == ["not-connected"]


def test_a_bridge_nobody_ever_polled_says_since_when(home):
    bridge = bb._Bridge()
    bridge.started = time.time() - 3 * 3600
    res = bridge.enqueue({"action": "spaces"}, timeout=4)
    assert res["extension_connected"] is False
    assert "since the bridge started 3 h ago" in res["error"]


def test_a_newly_started_bridge_still_waits_for_the_extension_to_arrive(home):
    bridge = bb._Bridge()                      # started now, never polled: it may be on its way

    def arrive():
        cmd = bridge.next_cmd(wait=5)
        if cmd:
            bridge.deliver(cmd["id"], {"url": "https://example.test/"})

    threading.Thread(target=arrive, daemon=True).start()
    res = bridge.enqueue({"action": "read"}, timeout=5)
    assert res == {"ok": True, "data": {"url": "https://example.test/"}}


def test_an_extension_busy_with_a_long_command_is_not_called_absent(home):
    bridge = bb._Bridge()
    took = {}

    def extension():
        took["cmd"] = bridge.next_cmd(wait=5)     # takes the long command and keeps working on it

    worker = threading.Thread(target=extension, daemon=True)
    worker.start()
    long_call = threading.Thread(
        target=lambda: took.setdefault("long", bridge.enqueue({"action": "script"}, timeout=5)),
        daemon=True)
    long_call.start()
    worker.join(3)
    assert took.get("cmd") and took["cmd"]["action"] == "script"
    bridge.last_poll = time.time() - 600          # not polling: busy for ten minutes
    res = bridge.enqueue({"action": "spaces"}, timeout=1)
    assert "extension_connected" not in res, "a busy extension is waited for, not written off"
    assert "still working on an earlier command" in res["error"]
    bridge.deliver(took["cmd"]["id"], {"ok": 1})
    long_call.join(3)
    assert took["long"]["ok"] is True


def test_a_command_that_timed_out_in_the_extensions_hands_keeps_it_counted_as_busy(home):
    """The extension is still stuck on it after the caller gave up (a page dialog held the tab):
    the next command must be told that, not that the extension is gone."""
    bridge = bb._Bridge()
    took = {}
    worker = threading.Thread(target=lambda: took.setdefault("cmd", bridge.next_cmd(wait=5)),
                              daemon=True)
    worker.start()
    res = bridge.enqueue({"action": "click", "selector": "#save"}, timeout=1)
    worker.join(3)
    assert "took this command but had not finished it after 1s" in res["error"]
    bridge.last_poll = time.time() - 600
    later = bridge.enqueue({"action": "spaces"}, timeout=1)
    assert "extension_connected" not in later
    assert "still working on an earlier command" in later["error"]
    assert bridge.next_cmd(wait=0.1) is None      # back for more: whatever it held is finished
    assert not bridge.taken


def test_a_poll_held_open_counts_as_present_until_it_ends(home):
    bridge = bb._Bridge()
    bridge.last_poll = time.time() - 600
    bridge.polling = 1                            # a poll is open right now
    assert bridge.absent_for() is None
    bridge.polling = 0
    assert bridge.absent_for() > 500


def test_a_finished_poll_refreshes_when_the_extension_was_last_seen(home):
    bridge = bb._Bridge()
    bridge.next_cmd(wait=0.2)
    assert time.time() - bridge.last_poll < 0.5 and bridge.polling == 0
    assert bridge.absent_for() is None


def test_the_tool_path_hands_the_not_connected_answer_through(monkeypatch):
    """Over HTTP, on a machine with no other route to the browser, the tool sees the same answer."""
    from harness.httpserver import ThreadingHTTPServer
    monkeypatch.setenv("COLLIE_BRIDGE_DANGEROUSLY_OMIT_AUTH", "1")
    monkeypatch.setenv("COLLIE_NO_APPLE_EVENTS", "1")
    bridge = bb._Bridge()
    bridge.last_poll = time.time() - 7200
    server = ThreadingHTTPServer(("127.0.0.1", 0), bb._handler(bridge))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(bb, "_port", lambda: server.server_address[1])
        t0 = time.monotonic()
        res = bb._call({"action": "read"}, timeout=20)
        assert time.monotonic() - t0 < 3.0
        assert res["ok"] is False and res["extension_connected"] is False
        assert bb._fmt(res).startswith("ERROR(browser): the Collie browser extension is not connected")
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_ago_reads_naturally():
    assert bb._ago(42) == "42s"
    assert bb._ago(600) == "10 min"
    assert bb._ago(3 * 3600 + 5) == "3 h"
    assert bb._ago(5 * 86400) == "5 days"
