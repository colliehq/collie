"""Focused, network-free checks for Collie's lease-based presence client."""
from __future__ import annotations

import json
import queue
import threading
import time
import urllib.parse

import pytest

from harness.presence import PresenceClient, normalize_health
from harness.wsclient import WebSocketClosed


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.incoming = queue.Queue()
        self.closed = False

    def send_text(self, text):
        if self.closed:
            raise WebSocketClosed()
        self.sent.append(json.loads(text))

    def recv_message(self):
        item = self.incoming.get(timeout=2)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self, *_args, **_kwargs):
        if not self.closed:
            self.closed = True
            self.incoming.put(WebSocketClosed())


def eventually(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def test_wire_identity_bearer_and_graceful_bye_without_secret_leak():
    secret = "super-secret-presence-token"
    ws = FakeWebSocket()
    calls = []
    logs = []

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return ws

    client = PresenceClient(
        "wss://relay.example/base/", "T_WORKSPACE", "U_CORNELLO", secret,
        session="session-1", connect=connect, heartbeat_s=99, logf=logs.append,
    )
    thread = client.start()
    assert client.connected.wait(1)

    url, kwargs = calls[0]
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    assert parsed.path == "/base/presence/ws"
    assert query == {"pack": "T_WORKSPACE", "dog": "U_CORNELLO", "session": "session-1"}
    assert secret not in url
    assert kwargs["headers"] == {"Authorization": "Bearer " + secret}
    assert kwargs["timeout"] == PresenceClient.CONNECT_TIMEOUT_S

    assert [message["t"] for message in ws.sent[:2]] == ["hello", "heartbeat"]
    for message in ws.sent[:2]:
        assert message["pack"] == "T_WORKSPACE"
        assert message["dog"] == "U_CORNELLO"
        assert message["session"] == "session-1"
        assert message["health"] == "ok"
        assert secret not in json.dumps(message)

    client.stop()
    thread.join(1)
    assert not thread.is_alive()
    assert ws.sent[-1]["t"] == "bye"
    assert not client.connected.is_set()
    assert all(secret not in line for line in logs)


def test_health_is_resampled_each_heartbeat_and_sequence_is_monotonic():
    state = {"socket": True, "worker": True}
    ws = FakeWebSocket()
    client = PresenceClient(
        "wss://relay.example", "p", "d", "token",
        connect=lambda *_args, **_kwargs: ws,
        session="session-s", health_fn=lambda: state.copy(), heartbeat_s=99,
    )
    client.start()
    assert client.connected.wait(1)
    assert ws.sent[1]["health"] == "ok"
    state["worker"] = False
    assert client.heartbeat_now()
    state["worker"] = True
    assert client.heartbeat_now()

    beats = [message for message in ws.sent if message["t"] == "heartbeat"]
    assert [message["seq"] for message in beats] == [1, 2, 3]
    assert [message["health"] for message in beats] == ["ok", "degraded", "ok"]
    client.stop()


def test_reconnect_uses_capped_backoff_and_stop_interrupts_wait():
    attempts = []
    delays = []
    stopped = threading.Event()
    holder = {}

    def connect(*_args, **_kwargs):
        attempts.append(object())
        raise OSError("relay unavailable")

    def waiter(delay):
        delays.append(delay)
        if len(delays) == 3:
            holder["client"].stop(send_bye=False, join_timeout=0)
            stopped.set()
            return True
        return False

    client = PresenceClient(
        "wss://relay.example", "p", "d", "token", connect=connect,
        waiter=waiter, random_fn=lambda: 1.0,
        backoff_initial_s=0.25, backoff_max_s=0.5, heartbeat_s=99,
    )
    holder["client"] = client
    client.run_forever()
    assert stopped.is_set()
    assert len(attempts) == 3
    assert delays == [0.25, 0.5, 0.5]
    assert not client.connected.is_set()


def test_a_successful_connection_resets_reconnect_backoff():
    attempts = 0
    delays = []
    holder = {}

    def connect(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts != 2:
            raise OSError("relay unavailable")
        ws = FakeWebSocket()
        ws.incoming.put(WebSocketClosed())
        return ws

    def waiter(delay):
        delays.append(delay)
        if len(delays) == 3:
            holder["client"].stop(send_bye=False, join_timeout=0)
            return True
        return False

    client = PresenceClient(
        "wss://relay.example", "p", "d", "token", connect=connect,
        waiter=waiter, random_fn=lambda: 1.0,
        backoff_initial_s=0.25, backoff_max_s=1.0, heartbeat_s=99,
    )
    holder["client"] = client
    client.run_forever()
    assert attempts == 3
    assert delays == [0.25, 0.25, 0.25]


def test_stop_during_reconnect_wait_returns_immediately():
    attempted = threading.Event()

    def fail(*_args, **_kwargs):
        attempted.set()
        raise OSError("offline")

    client = PresenceClient(
        "wss://relay.example", "p", "d", "token", connect=fail,
        backoff_initial_s=30, backoff_max_s=30,
    )
    thread = client.start()
    assert attempted.wait(1)
    started = time.monotonic()
    client.stop()
    thread.join(1)
    assert time.monotonic() - started < 0.5
    assert not thread.is_alive()


def test_errors_and_logs_redact_bearer_token():
    token = "do-not-print-me"
    logs = []
    waits = []
    holder = {}

    def fail(*_args, **_kwargs):
        raise RuntimeError("bad Authorization: Bearer " + token)

    def waiter(delay):
        waits.append(delay)
        holder["client"].stop(send_bye=False, join_timeout=0)
        return True

    client = PresenceClient(
        "wss://relay.example", "p", "d", token, connect=fail,
        waiter=waiter, logf=logs.append,
    )
    holder["client"] = client
    client.run_forever()
    assert token not in (client.last_error or "")
    assert "[redacted]" in (client.last_error or "")
    assert logs and all(token not in line for line in logs)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ok", "ok"),
        ("ONLINE", "ok"),
        ("offline", "degraded"),
        (True, "ok"),
        (False, "degraded"),
        ({"socket": True, "worker": True}, "ok"),
        ({"socket": True, "worker": False}, "degraded"),
        ({"status": "ok", "worker": False}, "degraded"),
        ({}, "degraded"),
    ],
)
def test_health_normalization(value, expected):
    assert normalize_health(value) == expected


def test_insecure_or_credentialed_presence_url_is_rejected():
    with pytest.raises(ValueError, match="wss"):
        PresenceClient("ws://relay.example", "p", "d", "token")
    with pytest.raises(ValueError, match="credentials"):
        PresenceClient("wss://user:password@relay.example", "p", "d", "token")


def test_presence_token_cannot_inject_a_websocket_header():
    with pytest.raises(ValueError, match="visible-ASCII"):
        PresenceClient("wss://relay.example", "p", "d", "good\r\nX-Evil: yes")


def test_wire_identity_is_validated_before_a_reconnect_loop_starts():
    with pytest.raises(ValueError, match="dog"):
        PresenceClient("wss://relay.example", "T1", "friendly dog name", "token")
    with pytest.raises(ValueError, match="session"):
        PresenceClient("wss://relay.example", "T1", "U1", "token", session="short")


def test_concurrent_heartbeats_cannot_reorder_their_sequences():
    first_entered = threading.Event()
    release_first = threading.Event()
    sent = []

    class SlowFirstSocket:
        def send_text(self, raw):
            message = json.loads(raw)
            if message["seq"] == 1:
                first_entered.set()
                assert release_first.wait(1)
            sent.append(message["seq"])

    client = PresenceClient("wss://relay.example", "T1", "U1", "token")
    ws = SlowFirstSocket()
    one = threading.Thread(target=client._send_heartbeat, args=(ws,))
    two = threading.Thread(target=client._send_heartbeat, args=(ws,))
    one.start()
    assert first_entered.wait(1)
    two.start()
    time.sleep(0.02)
    release_first.set()
    one.join(1)
    two.join(1)
    assert sent == [1, 2]
