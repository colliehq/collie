"""What can be checked without a Slack workspace.

The live half — the WebSocket, the ack, the round trip — needs tokens and is not
mocked here: a fake Slack would only prove the fake agrees with itself. What is
worth pinning is everything that decides *behaviour* once it does run: the
identity a channel sees, the queue surviving a restart, and the ask surviving the
mention, because each of those fails silently. Slack redelivers any envelope not
acked within three seconds, and a duplicated run is invisible until it has
already done the work twice.

    python3 tests/test_slackbot.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import slackbot as sb   # noqa: E402

fails = []


def check(ok, label):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label)


def main():
    # ── identity: what the channel sees ────────────────────────────────────
    label = sb.machine_label()
    check(bool(label) and len(label) <= 24, "a machine always has a sayable label (%r)" % label)

    a, b = sb.fingerprint(), sb.fingerprint()
    check(a == b and len(a) == 4,
          "the fingerprint is stable and short (%s) — one that changes between calls "
          "disambiguates nothing" % a)

    tmp = tempfile.mkdtemp()
    sb.IDENTITY = os.path.join(tmp, "identity.json")
    first = sb.load_identity()
    check(first["name"] in sb.KENNEL, "it names itself from the kennel (%s)" % first["name"])
    check(first.get("_fresh") is True, "and flags the first run, so the rename is offered once")
    again = sb.load_identity()
    check(again["name"] == first["name"], "the name is the part that stays put across restarts")
    check(not again.get("_fresh"), "and the offer is not repeated on every start")
    sb.load_identity(name="Bramble")
    check(sb.load_identity()["name"] == "Bramble", "a rename sticks")
    check(sb.load_identity()["machine"] == sb._hostname(),
          "the machine is recomputed, never stored beside the name — carrying a name to "
          "another laptop has to change what the channel sees")

    check(all(sb.AUTONOMY.get(lvl) for lvl in ("propose", "branch", "main")),
          "every autonomy level has a sentence, because the greeting prints it")

    # ── the queue ──────────────────────────────────────────────────────────
    sb.QUEUE_DIR = tmp
    q = sb.TaskQueue("jess")
    q.add("first thing", "C1", "111.1", "U1")
    second = q.add("second thing", "C1", "222.2", "U1")
    check(q.waiting() == 2, "two asks queue")

    reopened = sb.TaskQueue("jess")
    check(reopened.waiting() == 2 and "first thing" in reopened.listing(),
          "and survive a restart — an ask made an hour ago must not read as never heard")

    got = reopened.take()
    check(bool(got) and got["state"] == "running", "take marks one running")
    check("already running" in reopened.drop(got["id"]),
          "drop refuses to yank a running task — `stop` is the word for that")
    check(reopened.drop(second["id"]) == "dropped #%d" % second["id"], "and removes a waiting one")
    check(reopened.drop(999) == "no #999 in the queue", "an unknown id says so rather than passing")
    reopened.finish(got["id"])
    check(sb.TaskQueue("jess").waiting() == 0, "finishing clears it from disk too")

    # ── the ask itself ─────────────────────────────────────────────────────
    t = sb.MENTION_RE.sub("", "<@U08ABCD1> release 0.20.29 and say so").strip()
    check(t == "release 0.20.29 and say so",
          "the mention is stripped and the ask is not (%r)" % t)
    check(sb.MENTION_RE.sub("", "<@U1> <@U2> both of you").strip() == "both of you",
          "including when several people are named")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "slackbot: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
