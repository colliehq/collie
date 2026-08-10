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
import tempfile
import time
import subprocess as _real_subprocess

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
        self.pid = os.getpid()
        self.stdin = _Pipe()

    def communicate(self):
        return self._out, self._err

    def poll(self):
        return None

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


class _Pipe:
    def __init__(self):
        self.data = ""
        self.closed = False

    def write(self, data):
        self.data += data

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _SubprocessProxy:
    """Module-local Popen seam without mutating Python's process-wide subprocess module."""

    def __init__(self, popen):
        self.Popen = popen

    def __getattr__(self, name):
        return getattr(_real_subprocess, name)


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
    # Do not assign subprocess.Popen on Python's shared module object: presence/guard helpers use
    # subprocess.run, whose implementation would then receive this tiny fake and fail far away.
    sb.subprocess = _SubprocessProxy(lambda *a, **k: _Proc(body, err, rc))

    q = sb.TaskQueue("TestDog")
    q.path = os.path.join(sb.QUEUE_DIR, "queue-testdog-unit.json")
    return sb.Worker(q, {"name": "TestDog", "autonomy": "branch", "machine": "m", "os": "macOS"},
                     "xoxb-t", ROOT, "mock"), q


def main():
    from harness import slackbot as sb

    # Keep every queue in this execution away from a real dog's persistent work.
    sb.QUEUE_DIR = tempfile.mkdtemp(prefix="collie_slack_answer_")

    posted, reactions = [], []
    w, q = _worker(sb, posted, reactions, payload={"answer": "the branch is main", "session": "s1"})
    item = q.add("what branch", "C1", "T1", "U_HUMAN")

    # The bug: this raised NameError, in a worker thread, after the run was paid for.
    w._run_safely(q.take())

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
    q.add("break it", "C1", "T2", "U_ROWAN")
    w._run_safely(q.take())
    check(len(posted) == 1 and "gate refused the write" in posted[0]["text"],
          "a failed run reports the reason rather than an empty answer")
    check("⚠️" in posted[0]["text"], "marked as a failure, which is the one thing a peer reads")

    # A POSIX shell timeout can end the whole owned executor group after making
    # partial changes. The guard's dedicated code is outcome-unknown, not a
    # completed failure that may be deleted after posting an error.
    posted.clear()
    w, q = _worker(sb, posted, reactions, rc=sb.GUARD_INTERRUPTED_EXIT)
    interrupted = q.add("partial shell timeout", "C1", "T2b", "U_HUMAN")
    w._run_safely(q.take())
    check(q.items and q.items[0]["id"] == interrupted["id"]
          and q.items[0]["state"] == "interrupted",
          "an abruptly ended guard preserves the task for explicit recovery")

    posted.clear()
    w, q = _worker(sb, posted, reactions, payload={"answer": "unreachable"})
    killed = q.add("guard itself dies", "C1", "T2c", "U_HUMAN")
    sb.subprocess.Popen = lambda *a, **k: _Proc("", "", -9)
    w._run_safely(q.take())
    check(q.items and q.items[0]["id"] == killed["id"]
          and q.items[0]["state"] == "interrupted",
          "a killed guard with no JSON completion proof also remains recoverable")

    posted.clear()
    w, q = _worker(sb, posted, reactions, payload={"answer": "must not deliver"})
    stopped_item = q.add("stop while guard exits", "C1", "T2d", "U_HUMAN")

    class StopAtExit(_Proc):
        def communicate(self):
            q.record_stop("event:STOP-EXIT", "C1", "2.4")
            w._stop_requested.set()
            return "", ""

    sb.subprocess.Popen = lambda *a, **k: StopAtExit("", "", sb.GUARD_INTERRUPTED_EXIT)
    w._run_safely(q.take())
    texts = [message["text"] for message in posted]
    check(q.items and q.items[0]["id"] == stopped_item["id"]
          and q.items[0]["state"] == "interrupted"
          and any("stopped" in text for text in texts)
          and not any("internal worker error" in text for text in texts),
          "an explicit stop that produces guard code 76 reports stopped, not an internal crash")

    # A run that produced nothing at all is still answered — silence is the failure mode this
    # whole file exists to catch.
    posted.clear()
    w, q = _worker(sb, posted, reactions, payload={"answer": ""})
    q.add("say nothing", "C1", "T3", "U_HUMAN")
    w._run_safely(q.take())
    check(len(posted) == 1 and posted[0]["text"].strip() != "<@U_HUMAN>",
          "an empty run still comes back with something rather than nothing")

    # A failed Slack post keeps the completed result as an outbox entry. Retrying
    # that entry delivers the answer only; it never invokes the run again.
    posted.clear()
    reactions.clear()
    w, q = _worker(sb, posted, reactions, payload={"answer": "do this once"})
    q.add("one external action", "C1", "T4", "U_HUMAN")
    sb.say = lambda token, channel, text, thread="", tag="", broadcast=False: ""
    w._run_safely(q.take())
    kept = q.items[0]
    check(kept["state"] == "delivery_interrupted"
          and kept["delivery_text"].endswith("do this once"),
          "a failed Slack post keeps the completed answer instead of deleting the task")
    check("Inspect the thread" in q.retry(kept["id"]),
          "an uncertain post asks for inspection before it can duplicate a Slack reply")
    check("work will not run again" in q.retry(kept["id"], confirm_delivery=True),
          "confirmed delivery retry distinguishes delivery from execution")
    delivery = q.take()
    w._run_one = lambda item: (_ for _ in ()).throw(AssertionError("must not rerun"))
    sb.say = lambda token, channel, text, thread="", tag="", broadcast=False: "2.0"
    w._deliver_safely(delivery)
    check(not q.items, "the retried outbox answer is delivered and cleared without rerunning work")

    # Stop during preflight: the task is already claimed but Popen does not yet
    # exist. It must still be stopped and must never spawn after roster returns.
    posted.clear()
    w, q = _worker(sb, posted, reactions, payload={"answer": "too late"})
    stopped = q.add("partially edit", "C1", "T5", "U_HUMAN", ask_ts="ASK5")
    entered, release, spawned = sb.threading.Event(), sb.threading.Event(), []

    def blocked_roster(*args, **kwargs):
        entered.set()
        release.wait(timeout=3)
        return []

    sb.roster = blocked_roster
    sb.subprocess.Popen = lambda *a, **k: (spawned.append(True) or _Proc("{}"))
    w.start()
    w.nudge()
    entered.wait(timeout=3)
    check(w.current is None and w.stop_current() == "asked task #%d to stop" % stopped["id"],
          "stop covers the durable claim→spawn preflight window")
    release.set()
    deadline = time.time() + 3
    while time.time() < deadline and (not q.items or q.items[0]["state"] != "interrupted"):
        time.sleep(.02)
    check(q.items and q.items[0]["id"] == stopped["id"]
          and q.items[0]["state"] == "interrupted" and not spawned,
          "preflight stop leaves an explicit retry choice and never starts task code")
    w.shutdown()
    w.join(timeout=1)

    # Narrower still: q.take has committed `running` but run() has not returned
    # from the claim call. stop must latch, not observe a false empty window.
    posted.clear()
    w, q = _worker(sb, posted, reactions, payload={"answer": "must not run"})
    claimed_task = q.add("claim race", "C1", "T5b", "U_HUMAN", ask_ts="ASK5b")
    claimed, release_claim, spawned = sb.threading.Event(), sb.threading.Event(), []
    real_take = q.take

    def paused_take():
        item = real_take()
        if item is not None:
            claimed.set()
            release_claim.wait(timeout=3)
        return item

    q.take = paused_take
    sb.subprocess.Popen = lambda *a, **k: (spawned.append(True) or _Proc("{}"))
    w.start()
    w.nudge()
    claimed.wait(timeout=3)
    stop_result = []
    stopper = sb.threading.Thread(target=lambda: stop_result.append(w.stop_current()))
    stopper.start()
    time.sleep(.05)
    check(not stop_result, "stop waits for an in-flight durable claim instead of saying nothing")
    release_claim.set()
    stopper.join(timeout=2)
    deadline = time.time() + 3
    while time.time() < deadline and (not q.items or q.items[0]["state"] != "interrupted"):
        time.sleep(.02)
    check(stop_result == ["asked task #%d to stop" % claimed_task["id"]]
          and q.items[0]["id"] == claimed_task["id"]
          and q.items[0]["state"] == "interrupted" and not spawned,
          "claim publication and stop are atomic, so task code cannot escape that stop")
    w.shutdown()
    w.join(timeout=1)

    # Run the actual thread: #1 crashes, stays as a fence, and #2 cannot pass it.
    # Once a person resolves #1, the same worker consumes #2 — no silent death.
    posted.clear()
    w, q = _worker(sb, posted, reactions, payload={"answer": "second completed"})
    first = q.add("crash once", "C1", "T6", "U_HUMAN", ask_ts="ASK6")
    second = q.add("then continue", "C1", "T7", "U_HUMAN", ask_ts="ASK7")
    real_run_one = w._run_one

    def flaky(item):
        if item["id"] == first["id"]:
            raise NameError("head")
        return real_run_one(item)

    w._run_one = flaky
    w.start()
    w.nudge()
    deadline = time.time() + 3
    while time.time() < deadline and not any(
            i["id"] == first["id"] and i["state"] == "interrupted" for i in q.items):
        time.sleep(.02)
    check(w.is_alive(), "an unexpected task bug does not kill the worker thread")
    check(any(i["id"] == second["id"] and i["state"] == "waiting" for i in q.items),
          "and the interrupted task fences the next ask until someone resolves it")
    q.drop(first["id"])
    w.nudge()
    deadline = time.time() + 3
    while time.time() < deadline and q.items:
        time.sleep(.02)
    check(not q.items and w.is_alive(), "after resolution, that same worker continues with the next task")
    w.shutdown()
    w.join(timeout=1)

    # A transient disk error can arrive after the process tree is already gone.
    # The listener must keep retrying that terminal write instead of leaving a
    # live-but-permanently-fenced `running` row that even `stop` cannot reach.
    posted.clear()
    w, q = _worker(sb, posted, reactions, payload={"answer": "never"})
    task = q.add("crash during recovery", "C1", "T6b", "U_HUMAN", ask_ts="ASK6b")
    w._run_one = lambda item: (_ for _ in ()).throw(RuntimeError("worker bug"))
    real_interrupt = q.interrupt
    interrupt_calls = []

    def transient_interrupt(task_id):
        interrupt_calls.append(task_id)
        if len(interrupt_calls) == 1:
            raise sb.QueuePersistenceError("disk busy once")
        return real_interrupt(task_id)

    q.interrupt = transient_interrupt
    w.start()
    w.nudge()
    deadline = time.time() + 3
    while time.time() < deadline and q.items[0]["state"] != "interrupted":
        time.sleep(.02)
    check(q.items[0]["id"] == task["id"] and q.items[0]["state"] == "interrupted"
          and len(interrupt_calls) >= 2 and w.is_alive(),
          "a failed execution-terminal write is reconciled before the worker accepts more work")
    w.shutdown()
    w.join(timeout=1)

    # The same reconciliation applies to the answer outbox. Two failed writes
    # cover both the first delivery transition and its immediate error handler;
    # the live loop must make the third attempt after storage recovers.
    posted.clear()
    w, q = _worker(sb, posted, reactions, payload={"answer": "done once"})
    task = q.add("complete once", "C1", "T6c", "U_HUMAN", ask_ts="ASK6c")
    delivering = q.complete(q.take()["id"], "<@U_HUMAN> done once", True)
    real_delivery_failed = q.delivery_failed
    delivery_calls = []

    def transient_delivery(task_id):
        delivery_calls.append(task_id)
        if len(delivery_calls) <= 2:
            raise sb.QueuePersistenceError("disk busy twice")
        return real_delivery_failed(task_id)

    q.delivery_failed = transient_delivery
    sb.say = lambda token, channel, text, thread="", tag="", broadcast=False: ""
    w._deliver_safely(delivering)
    w.start()
    w.nudge()
    deadline = time.time() + 3
    while time.time() < deadline and q.items[0]["state"] == "delivering":
        time.sleep(.02)
    check(q.items[0]["id"] == task["id"]
          and q.items[0]["state"] == "delivery_interrupted"
          and len(delivery_calls) >= 3 and w.is_alive(),
          "a failed delivery-terminal write is reconciled instead of hiding a stuck outbox")
    w.shutdown()
    w.join(timeout=1)

    # ---- a thread's memory belongs to the dog that made it ---------------------------------------
    # One machine can run several dogs — that is what the kennel is for, and they work in different
    # repositories — and two of them in one Slack thread share threads.json. Keyed by thread alone,
    # the second one to be @-ed resumes the first one's session: another dog's conversation, in
    # another repository, offered as its own memory of what was just said.
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
