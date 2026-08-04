"""Joining a run in progress shows what it already did.

Following a run started in another window used to mean watching a blank screen under a note that
said work was happening. The mirror bus is session-scoped and takes late subscribers, but it kept
nothing, so "live" meant "from this instant" — and the instant you opened a thread is the least
interesting moment in it.

It keeps a short tail now. Two rules the tail exists to respect: tokens are not in it (one paragraph
would evict everything Collie actually DID, which is the part a late joiner needs), and it is dropped
the moment the run ends, because from then on the saved thread is the better record and a stale
backlog would replay a finished run over a fresh one with the same id.

    python3 tests/test_mirror_backlog.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def main():
    from harness.webapp import Handler

    with Handler._mirror_lock:
        Handler._mirror_backlog.clear()

    Handler._mirror_pub("m1", "start", {"session": "m1"})
    Handler._mirror_pub("m1", "tool", {"name": "bash", "args": {"cmd": "ls"}})
    Handler._mirror_pub("m1", "token", {"t": "some prose "})
    Handler._mirror_pub("m1", "edit", {"name": "write", "args": {"path": "a.py"}})

    with Handler._mirror_lock:
        buf = list(Handler._mirror_backlog.get("m1", ()))
    kinds = [k for k, _ in buf]
    check(kinds == ["start", "tool", "edit"],
          "structural events are kept in order, tokens are not (%s)" % kinds)

    for i in range(Handler._MIRROR_BACKLOG + 20):
        Handler._mirror_pub("m1", "tool", {"name": "bash", "args": {"cmd": "step%d" % i}})
    with Handler._mirror_lock:
        buf = list(Handler._mirror_backlog.get("m1", ()))
    check(len(buf) == Handler._MIRROR_BACKLOG,
          "the tail is bounded (%d == %d)" % (len(buf), Handler._MIRROR_BACKLOG))
    check(buf[-1][1]["args"]["cmd"] == "step%d" % (Handler._MIRROR_BACKLOG + 19),
          "and keeps the NEWEST, not the oldest — what a joiner missed most recently")

    Handler._mirror_pub("m1", "done", {"session": "m1", "answer": "x"})
    with Handler._mirror_lock:
        gone = "m1" not in Handler._mirror_backlog
    check(gone, "the tail is dropped when the run ends, so a new run cannot replay the old one")

    # A session that never ran must not accumulate anything from a stray publish either.
    Handler._mirror_pub("m2", "ping", {})
    with Handler._mirror_lock:
        m2 = list(Handler._mirror_backlog.get("m2", ()))
    check(len(m2) == 1, "unrelated events still land in their own session's tail, not m1's")
    with Handler._mirror_lock:
        Handler._mirror_backlog.clear()

    print("\n  " + ("%d FAILED" % len(fails) if fails else "mirror backlog: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
