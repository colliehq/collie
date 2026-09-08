"""App Server steering requires a receipt for the expected active turn."""
import queue
import threading

import pytest

from harness import runner_specs, runner_slice
from harness.codex_app_server_runner import CodexAppServerRunner


class Peer:
    def __init__(self, response="accept"):
        self.response = response
        self.incoming = queue.Queue()
        self.sent = []
        self.active = threading.Event()

    returncode = 0
    stderr = ""

    def send(self, message):
        self.sent.append(message)
        method = message.get("method")
        result = None
        if method == "initialize":
            result = {}
        elif method == "thread/start":
            result = {"thread": {"id": "thr_test"}}
        elif method == "turn/start":
            result = {"turn": {"id": "turn_test", "status": "inProgress"}}
        elif method == "turn/steer":
            if message["params"].get("expectedTurnId") != "turn_test":
                self.incoming.put({"id": message["id"], "error": {
                    "code": -32602, "message": "expectedTurnId is required"}})
                return
            if self.response == "drop":
                return
            if self.response == "write_error":
                raise OSError("ambiguous partial pipe write")
            if self.response == "reject":
                self.incoming.put({"id": message["id"], "error": {
                    "code": -32602, "message": "no active turn"}})
                return
            result = {"turnId": "other_turn" if self.response == "wrong" else "turn_test"}
        if result is not None:
            self.incoming.put({"id": message["id"], "result": result})
        if method == "turn/start":
            self.incoming.put({"method": "turn/started", "params": {
                "threadId": "thr_test", "turn": {"id": "turn_test"}}})

    def receive(self, timeout_s):
        try:
            return self.incoming.get(timeout=timeout_s)
        except queue.Empty as exc:
            raise TimeoutError from exc

    def finish(self):
        self.incoming.put({"method": "turn/completed", "params": {
            "threadId": "thr_test", "turn": {"id": "turn_test", "status": "completed"}}})

    def close(self):
        return True


def start_peer(tmp_path, response="accept"):
    peer = Peer(response)
    runner = CodexAppServerRunner(
        transport_factory=lambda *args: peer,
        snapshotter=lambda *args: {"tree_digest": "same", "snapshot_complete": True},
        event_callback=lambda event: peer.active.set() if event.type == "turn/started" else None)
    runner._steer_ack_timeout_s = .2
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.start("first", str(tmp_path))))
    thread.start()
    assert peer.active.wait(2)
    return runner, peer, thread, results


def stop_peer(peer, thread, results):
    peer.finish()
    thread.join(2)
    assert not thread.is_alive()
    assert results[0].settled


def test_steering_waits_for_acceptance_of_the_expected_turn(tmp_path):
    runner, peer, thread, results = start_peer(tmp_path)
    try:
        assert runner.steer_current("preserve existing files") is True
        request = next(m for m in peer.sent if m.get("method") == "turn/steer")
        assert request["params"] == {
            "threadId": "thr_test", "expectedTurnId": "turn_test",
            "input": [{"type": "text", "text": "preserve existing files"}]}
        # Consumed replies must not accumulate in the unrelated-RPC cache.
        for i in range(70):
            assert runner.steer_current("correction %d" % i) is True
    finally:
        stop_peer(peer, thread, results)


@pytest.mark.parametrize("response,delivery", [
    ("reject", "rejected"), ("wrong", "unknown"),
    ("drop", "unknown"), ("write_error", "unknown")])
def test_failed_or_missing_receipts_are_not_reported_as_delivered(tmp_path, response, delivery):
    runner, peer, thread, results = start_peer(tmp_path, response)
    try:
        with pytest.raises(runner_specs.RunnerMessageDeliveryError) as caught:
            runner.steer_current("a correction")
        assert caught.value.delivery == delivery
        assert len([m for m in peer.sent if m.get("method") == "turn/steer"]) == 1
        # A late receipt cannot fill the unmatched-response cache or complete
        # a later steering request. Keep the main receive loop alive afterwards.
        for i in range(70):
            peer.incoming.put({"id": "collie-steer-expired-%d" % i,
                               "result": {"turnId": "turn_test"}})
    finally:
        stop_peer(peer, thread, results)


def test_launching_without_a_turn_id_does_not_attempt_a_write():
    peer = Peer()
    runner = CodexAppServerRunner()
    runner._active = peer
    runner._active_thread_id = "thr_test"
    assert runner.steer_current("wait for the active turn") is False
    assert peer.sent == []


def test_watcher_does_not_replay_an_unacknowledged_message():
    calls = []
    events = []
    delivered = threading.Event()

    class Runner:
        def steer_current(self, text):
            calls.append(text)
            if text == "first":
                raise runner_specs.RunnerMessageDeliveryError("receipt lost")
            return True

    rows = ["first", "second"]

    def drain():
        result = list(rows)
        rows.clear()
        return result

    def emit(name, data):
        events.append(data)
        if data["accepted"]:
            delivered.set()

    with runner_slice._SteerWatcher(Runner(), drain, poll_s=.01, emit=emit):
        assert delivered.wait(2)
    assert calls == ["first", "second"]
    assert [(row["accepted"], row["delivery"]) for row in events] == [
        (False, "unknown"), (True, "accepted")]
