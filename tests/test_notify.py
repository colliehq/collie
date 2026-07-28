"""webapp._notify_done — when a finished run is worth interrupting someone for.

A notification that fires for every run is one people turn off, and once it is off the phone is back
to being a screen you have to remember to check. So the rule has to be defensible: a run you waited
for, or a run that failed. This pins that rule, and pins the things a notification must never do —
throw, or fire when nobody is paired.

    python3 tests/test_notify.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import webapp                                             # noqa: E402

_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


class FakeResult(object):
    def __init__(self, answer="", error=None, wall_ms=0):
        self.answer = answer
        self.error = error
        self.wall_ms = wall_ms


class FakeRemote(object):
    """Stands in for the relay client. Records instead of sending."""

    def __init__(self, explode=False):
        self.sent = []
        self.explode = explode

    def notify(self, title, body, session="", thread="collie"):
        if self.explode:
            raise RuntimeError("the relay went away mid-notification")
        self.sent.append({"title": title, "body": body, "session": session, "thread": thread})
        return True


def with_remote(remote, fn):
    original = webapp.REMOTE
    webapp.REMOTE = remote
    try:
        return fn()
    finally:
        webapp.REMOTE = original


def main():
    threshold = webapp.Handler.NOTIFY_AFTER_MS

    # A run short enough that you are still looking at the screen is not news.
    r = FakeRemote()
    with_remote(r, lambda: webapp.Handler._notify_done(
        "s1", FakeResult(answer="done", wall_ms=1000), wall_ms=1000))
    check(r.sent == [], "a quick run does not buzz the phone")

    # One you walked away from is.
    r = FakeRemote()
    with_remote(r, lambda: webapp.Handler._notify_done(
        "s2", FakeResult(answer="the tests pass now", wall_ms=threshold + 1),
        wall_ms=threshold + 1))
    check(len(r.sent) == 1, "a long run does")
    check(r.sent and r.sent[0]["title"] == "Run finished", "and says so")
    check(r.sent and r.sent[0]["session"] == "s2",
          "carrying the session, so a tap can open that run")
    check(r.sent and "the tests pass now" in r.sent[0]["body"],
          "with the answer, not just 'a run finished'")

    # A failure is worth knowing however fast it happened.
    r = FakeRemote()
    with_remote(r, lambda: webapp.Handler._notify_done(
        "s3", FakeResult(error="ValueError: nope", wall_ms=200), wall_ms=200))
    check(len(r.sent) == 1 and r.sent[0]["title"] == "Run failed",
          "a failure notifies even when it failed immediately")
    check(r.sent and "ValueError" in r.sent[0]["body"], "and says what went wrong")

    # An answer of many thousands of characters must not become the notification.
    r = FakeRemote()
    with_remote(r, lambda: webapp.Handler._notify_done(
        "s4", FakeResult(answer="x" * 5000, wall_ms=threshold + 1), wall_ms=threshold + 1))
    check(r.sent and len(r.sent[0]["body"]) <= 200, "a huge answer is trimmed, not sent whole")

    # Newlines in a notification body render as spaces at best; flatten them here rather than
    # leaving the alert to decide.
    r = FakeRemote()
    with_remote(r, lambda: webapp.Handler._notify_done(
        "s5", FakeResult(answer="line one\nline two", wall_ms=threshold + 1), wall_ms=threshold + 1))
    check(r.sent and "\n" not in r.sent[0]["body"], "the body is a single line")

    # Nothing paired: the run must not notice.
    ok = True
    try:
        with_remote(None, lambda: webapp.Handler._notify_done(
            "s6", FakeResult(answer="hi", wall_ms=threshold + 1), wall_ms=threshold + 1))
    except Exception as e:                                             # noqa: BLE001
        ok = False
        print("    raised:", e)
    check(ok, "with no relay at all, notifying is a silent no-op")

    # And a relay that fails mid-send must not take a finished run down with it.
    r = FakeRemote(explode=True)
    ok = True
    try:
        with_remote(r, lambda: webapp.Handler._notify_done(
            "s7", FakeResult(answer="hi", wall_ms=threshold + 1), wall_ms=threshold + 1))
    except Exception as e:                                             # noqa: BLE001
        ok = False
        print("    raised:", e)
    check(ok, "a relay that throws does not fail the run that just succeeded")

    # ---- the message that actually goes down the socket ------------------------------------------
    # relay_push_test.js feeds this same shape into the worker's handler, so the two halves meet on a
    # message format both sides have been checked against rather than on one side's assumption.
    import json as _json

    from harness import remote as remote_mod

    class FakeWS(object):
        def __init__(self, explode=False):
            self.sent = []
            self.explode = explode

        def send_text(self, s):
            if self.explode:
                raise OSError("socket closed under us")
            self.sent.append(_json.loads(s))

    client = remote_mod.RelayClient.__new__(remote_mod.RelayClient)
    ws = FakeWS()
    client._ws = ws
    check(client.notify("Run finished", "all green", session="s9") is True,
          "the client reports it sent")
    msg = ws.sent[0] if ws.sent else {}
    check(msg.get("t") == "notify", "the socket message is a `notify`")
    check(msg.get("title") == "Run finished" and msg.get("body") == "all green",
          "carrying the title and body the desktop chose")
    check(msg.get("session") == "s9", "and the session id")

    # Long text must be cut here, not at the phone: an alert silently truncates and the useful part
    # is at the start.
    ws = FakeWS()
    client._ws = ws
    client.notify("t" * 500, "b" * 900)
    check(ws.sent and len(ws.sent[0]["title"]) <= 120 and len(ws.sent[0]["body"]) <= 300,
          "title and body are bounded before they leave the machine")

    # Not connected, and a socket that dies mid-send: both are a quiet false, never an exception.
    client._ws = None
    check(client.notify("x", "y") is False, "with no socket, notifying is a quiet false")
    client._ws = FakeWS(explode=True)
    check(client.notify("x", "y") is False, "a socket that throws mid-send is caught")

    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "notify: all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
