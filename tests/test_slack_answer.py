"""The answer path, executed rather than read.

`_run_one` referred to a `head` that had been deleted with the status messages it belonged to.
Every ask a dog accepted died on `NameError: name 'head' is not defined` — after the run had
finished and been paid for, in a worker thread, where the traceback goes to a log nobody has open.
In the channel it looked like a dog that took the work and never came back.

The suites around it are source checks: they grep slackbot.py for the shape of a call. A name that
does not exist is invisible to that and obvious to one execution, so this runs the real method with
the process, the network and the clock stubbed, and reads what it tried to post.

    python3 tests/test_slack_answer.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


class _Proc:
    """A finished `collie run`: what it wrote, and how it exited."""

    def __init__(self, out, err="", rc=0):
        self._out, self._err, self.returncode = out, err, rc

    def communicate(self):
        return self._out, self._err


def _worker(sb, posted, reactions, rc=0, payload=None, err=""):
    """A Worker whose run is a canned process and whose Slack is a list."""
    sb.say = lambda token, channel, text, thread="", tag="", broadcast=False: (
        posted.append({"channel": channel, "text": text, "thread": thread}) or "1.0")
    sb.react = lambda token, channel, ts, emoji, on=True: reactions.append((emoji, on))
    sb.roster = lambda token, channel, now=0.0: [
        {"id": "U_ROWAN", "name": "Rowan", "is_bot": True},
        {"id": "U_HUMAN", "name": "Daming", "is_bot": False}]
    sb.api = lambda method, token, **p: {"ok": True, "user_id": "U_ME"}
    body = json.dumps(payload) if payload is not None else "{}"
    sb.subprocess.Popen = lambda *a, **k: _Proc(body, err, rc)

    q = sb.TaskQueue("TestDog")
    q.path = os.path.join(sb.QUEUE_DIR, "queue-testdog-unit.json")
    return sb.Worker(q, {"name": "TestDog", "autonomy": "branch", "machine": "m", "os": "macOS"},
                     "xoxb-t", ROOT, "mock"), q


def main():
    from harness import slackbot as sb

    posted, reactions = [], []
    w, q = _worker(sb, posted, reactions, payload={"answer": "the branch is main", "session": "s1"})
    item = q.add("what branch", "C1", "T1", "U_HUMAN")

    # The bug: this raised NameError, in a worker thread, after the run was paid for.
    w._run_one(item)

    check(len(posted) == 1, "one ask produces exactly one message — not queued, on it, and done")
    if posted:
        text = posted[0]["text"]
        check("the branch is main" in text, "and that message carries the answer")
        check(text.startswith("<@U_HUMAN>"), "addressed to whoever asked")
        check("```" not in text, "outside a code fence — Slack renders no mention inside one")
        check("#" not in text.split("\n")[0].replace("<@U_HUMAN>", ""),
              "with no task number: it indexes this dog's queue and means nothing to a reader")
    check([e for e, on in reactions if on], "the ask is marked with a reaction instead")

    # A failure must still say why, and say it as a failure.
    posted.clear()
    w, q = _worker(sb, posted, reactions, rc=1, payload={"error": "gate refused the write"},
                   err="Traceback: something")
    w._run_one(q.add("break it", "C1", "T2", "U_ROWAN"))
    check(len(posted) == 1 and "gate refused the write" in posted[0]["text"],
          "a failed run reports the reason rather than an empty answer")
    check("⚠️" in posted[0]["text"], "marked as a failure, which is the one thing a peer reads")

    # A run that produced nothing at all is still answered — silence is the failure mode this
    # whole file exists to catch.
    posted.clear()
    w, q = _worker(sb, posted, reactions, payload={"answer": ""})
    w._run_one(q.add("say nothing", "C1", "T3", "U_HUMAN"))
    check(len(posted) == 1 and posted[0]["text"].strip() != "<@U_HUMAN>",
          "an empty run still comes back with something rather than nothing")

    # ---- a thread's memory belongs to the dog that made it ---------------------------------------
    # One machine can run several dogs — that is what the kennel is for, and they work in different
    # repositories — and two of them in one Slack thread share threads.json. Keyed by thread alone,
    # the second one to be @-ed resumes the first one's session: another dog's conversation, in
    # another repository, offered as its own memory of what was just said.
    import tempfile
    sb.THREADS = os.path.join(tempfile.mkdtemp(prefix="collie_threads_"), "threads.json")
    sb.thread_session("C1", "T9", "session-of-bigmac", dog="BigMac")
    check(sb.thread_session("C1", "T9", dog="BigMac") == "session-of-bigmac",
          "a dog resumes the session it made in this thread")
    check(sb.thread_session("C1", "T9", dog="Cornetto") == "",
          "and a packmate in the SAME thread gets none of it, rather than someone else's run")
    sb.thread_session("C1", "T9", "session-of-cornetto", dog="Cornetto")
    check(sb.thread_session("C1", "T9", dog="BigMac") == "session-of-bigmac",
          "the two coexist — neither overwrites the other")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "slack answer: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
