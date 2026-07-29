"""A relay socket that stopped answering must be noticed.

Pinging proves nothing. A TCP socket stays writable long after the far end has stopped treating it as
this room's agent, so every ping succeeds, nothing raises, and the desktop reports `connected: true`
while the relay answers "desktop offline" to the phone. Observed exactly that today: the desktop
insisted it was connected, both relay hostnames said offline, and a manual disable/enable fixed it
instantly. Nothing on the desktop could have told you.

The far end's PONG is the only evidence anyone is listening.

    python3 tests/test_relay_keepalive.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import remote as remote_mod                              # noqa: E402

_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


class FakeWS(object):
    """A socket that always accepts a ping. Whether it ever answers is the variable under test."""

    def __init__(self, answers=True):
        self.answers = answers
        self.pings = 0
        self.closed = False
        self.last_pong = time.time()

    def send_ping(self, data=b""):
        self.pings += 1
        if self.answers:
            self.last_pong = time.time()          # the far end replied
        # else: the write succeeds and nothing comes back — the failure this exists to catch

    def close(self):
        self.closed = True


def client_with(ws, keepalive, grace):
    c = remote_mod.RelayClient.__new__(remote_mod.RelayClient)
    c._log = lambda *a, **k: None
    c.KEEPALIVE_S = keepalive
    c.PONG_GRACE_S = grace
    return c


def main():
    # A live peer: pings keep flowing, the socket is left alone.
    ws = FakeWS(answers=True)
    c = client_with(ws, keepalive=0.05, grace=0.3)
    stop = c._start_keepalive(ws)
    time.sleep(0.9)
    stop.set()
    check(ws.pings >= 3, "a healthy socket keeps getting pinged (%d)" % ws.pings)
    check(not ws.closed, "and is never torn down")

    # A peer that has gone quiet: writable, silent, and must be dropped.
    ws2 = FakeWS(answers=False)
    c2 = client_with(ws2, keepalive=0.05, grace=0.3)
    stop2 = c2._start_keepalive(ws2)
    for _ in range(60):
        if ws2.closed:
            break
        time.sleep(0.05)
    stop2.set()
    check(ws2.closed, "a socket that answers nothing is closed, so the run loop can redial")
    check(ws2.pings >= 1, "after actually having been pinged first (%d)" % ws2.pings)

    # A slow reply is not a dead peer. One missed round trip must not reconnect.
    ws3 = FakeWS(answers=False)
    c3 = client_with(ws3, keepalive=0.05, grace=5.0)
    stop3 = c3._start_keepalive(ws3)
    time.sleep(0.5)
    still_open = not ws3.closed
    stop3.set()
    check(still_open, "one slow round trip does not trigger a reconnect")

    # The shipping numbers have to leave room for more than a single missed beat.
    check(remote_mod.RelayClient.PONG_GRACE_S > 2 * remote_mod.RelayClient.KEEPALIVE_S,
          "the shipping grace spans more than two keepalives (%.0fs vs %.0fs)"
          % (remote_mod.RelayClient.PONG_GRACE_S, remote_mod.RelayClient.KEEPALIVE_S))

    # And the transport has to record the evidence in the first place.
    from harness import wsclient
    src = open(wsclient.__file__, encoding="utf-8").read()
    check("self.last_pong = time.time()" in src,
          "the websocket client timestamps the PONG rather than discarding it")

    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "relay keepalive: all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
