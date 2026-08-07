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
import json
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
    lock = sb.SlackInstanceLock("Jess")
    duplicate_refused = False
    try:
        sb.SlackInstanceLock("Jess")
    except RuntimeError:
        duplicate_refused = True
    check(duplicate_refused, "an OS-held lock refuses a second live copy of the same dog")
    lock.close()
    replacement = sb.SlackInstanceLock("Jess")
    replacement.close()
    check(True, "and a crashed/exited owner releases the lock for the replacement")

    q = sb.TaskQueue("jess")
    q.add("first thing", "C1", "111.1", "U1")
    second = q.add("second thing", "C1", "222.2", "U1")
    check(q.waiting() == 2, "two asks queue")

    reopened = sb.TaskQueue("jess")
    check(reopened.waiting() == 2 and "first thing" in reopened.listing(),
          "and survive a restart — an ask made an hour ago must not read as never heard")

    got = reopened.take()
    check(bool(got) and got["state"] == "running", "take marks one running")
    del reopened                         # the old process is gone; its OS lock would be gone too
    recovered = sb.TaskQueue("jess", recover_running=True)
    interrupted = next(i for i in recovered.items if i["id"] == got["id"])
    check(interrupted["state"] == "interrupted" and "⚠" in recovered.listing(),
          "a restart preserves outcome-unknown work as interrupted, never silently reruns it")
    check(recovered.take() is None and recovered.waiting() == 1,
          "an interrupted task fences later work in the same working tree")
    check(recovered.retry(got["id"]) == "retrying #%d" % got["id"],
          "an interrupted task can be retried explicitly")
    retried = recovered.take()
    check(retried["id"] == got["id"] and retried["state"] == "running",
          "explicitly retried work runs before newer asks")
    check("already running" in recovered.drop(got["id"]),
          "drop refuses to yank a running task — `stop` is the word for that")
    check(recovered.drop(second["id"]) == "dropped #%d" % second["id"], "and removes a waiting one")
    check(recovered.drop(999) == "no #999 in the queue", "an unknown id says so rather than passing")
    recovered.finish(got["id"])
    check(sb.TaskQueue("jess").waiting() == 0, "finishing clears it from disk too")

    # A claim is durable-before-act. If its write fails, it must remain waiting
    # both in memory and on disk, and take() must not hand it to a worker.
    durable = sb.TaskQueue("durable")
    durable.add("do not duplicate", "C1", "D1", "U1")
    real_write = durable._write
    durable._write = lambda items, next_id, receipts=None: (_ for _ in ()).throw(
        sb.QueuePersistenceError("disk full"))
    claim_failed = False
    try:
        durable.take()
    except sb.QueuePersistenceError:
        claim_failed = True
    with open(durable.path, encoding="utf-8") as f:
        on_disk = json.load(f)
    check(claim_failed and durable.items[0]["state"] == "waiting"
          and on_disk["items"][0]["state"] == "waiting",
          "a failed running-state write cannot release work to execute or diverge from disk")
    durable._write = real_write

    broken_path = os.path.join(tmp, "queue-broken.json")
    with open(broken_path, "w", encoding="utf-8") as f:
        f.write('{"items": [{"id": 1}')
    broken_refused = False
    try:
        sb.TaskQueue("broken")
    except sb.QueuePersistenceError:
        broken_refused = True
    check(broken_refused and os.path.getsize(broken_path) > 0,
          "truncated queue data stops startup and is left for recovery, never treated as empty")

    outbox = sb.TaskQueue("outbox")
    finished_run = outbox.take() if outbox.waiting() else outbox.add(
        "publish once", "C1", "O1", "U1", ask_ts="ASK")
    if finished_run["state"] == "waiting":
        finished_run = outbox.take()
    outbox.complete(finished_run["id"], "stored answer", True)
    del outbox
    delivery_recovery = sb.TaskQueue("outbox", recover_running=True)
    check(delivery_recovery.items[0]["state"] == "delivery_interrupted",
          "a crash around Slack delivery preserves the completed answer separately")
    later = delivery_recovery.add("later work", "C1", "O2", "U1")
    later_run = delivery_recovery.take()
    check(later_run["id"] == later["id"],
          "an unresolved completed outbox does not freeze later working-tree tasks")
    delivery_recovery.finish(later["id"])
    caution = delivery_recovery.retry(finished_run["id"])
    check("Inspect the thread" in caution
          and delivery_recovery.items[0]["state"] == "delivery_interrupted",
          "an uncertain Slack post requires an explicit inspect-and-redeliver confirmation")
    retry_answer = delivery_recovery.retry(finished_run["id"], confirm_delivery=True)
    delivery = delivery_recovery.take()
    check("work will not run again" in retry_answer and delivery["state"] == "delivering"
          and delivery["delivery_text"] == "stored answer",
          "retrying a delivery sends the outbox result without rerunning its work")

    receipts = sb.TaskQueue("receipts")
    once = receipts.add("only once", "C1", "R1", "U1", source_id="event:E1")
    receipts.finish(once["id"])
    next_before = receipts.next_id
    del receipts
    receipts = sb.TaskQueue("receipts")
    duplicate = receipts.add("only once", "C1", "R1", "U1", source_id="event:E1")
    check(duplicate is None and receipts.next_id == next_before and not receipts.items,
          "a durable Slack event receipt rejects redelivery even after completion and restart")

    controls = sb.TaskQueue("controls")
    first_controlled = controls.add("first", "C1", "R1", "U1")
    controls.take()
    stop_target = controls.record_stop("event:STOP1", "C1", "20.0")
    controls.interrupt(first_controlled["id"])
    controls.drop(first_controlled["id"])
    second_controlled = controls.add("second", "C1", "R2", "U1")
    controls.take()
    check(stop_target == first_controlled["id"]
          and controls.is_duplicate("event:STOP1", "C1", "20.0", "R1", "U1", "stop")
          and controls.record_stop("event:STOP1", "C1", "20.0") == -1
          and not controls.stop_requested(second_controlled["id"]),
          "a delayed redelivery of stop stays bound to its original task, never the next one")
    controls.interrupt(second_controlled["id"])
    check(controls.retry(second_controlled["id"], source_id="event:RETRY1",
                         channel="C1", ask_ts="21.0").startswith("retrying"),
          "retry atomically records the control event with its state transition")
    controls.take()
    controls.interrupt(second_controlled["id"])
    repeated_retry = controls.retry(second_controlled["id"], source_id="event:RETRY1",
                                    channel="C1", ask_ts="21.0")
    check("already handled" in repeated_retry
          and controls.items[0]["state"] == "interrupted",
          "a delayed retry event cannot rerun the same task after a later interruption")

    stop_race = sb.TaskQueue("stop-race")
    stop_race_item = stop_race.add("finish boundary", "C1", "R3", "U1")
    stop_race.take()
    stop_race.record_stop("event:STOP-RACE", "C1", "22.0")
    check(stop_race.complete(stop_race_item["id"], "must not post", True) is None
          and stop_race.items[0]["state"] == "interrupted"
          and "delivery_text" not in stop_race.items[0],
          "a durable stop that wins the final queue lock cannot be overwritten by completion")

    legacy_path = os.path.join(tmp, "queue-legacy.json")
    with open(legacy_path, "w", encoding="utf-8") as f:
        json.dump({"next_id": 2, "items": [{"id": 1, "text": "old ask", "channel": "C1",
                   "thread": "ROOT", "user": "U1", "state": "waiting",
                   "from_dog": False, "queued_at": 100.0}]}, f)
    legacy = sb.TaskQueue("legacy")
    legacy.finish(1)
    del legacy
    legacy = sb.TaskQueue("legacy")
    check(legacy.add("old ask", "C1", "ROOT", "U1", ask_ts="105.0",
                     source_id="event:legacy-redelivery") is None,
          "a legacy marker fences one fast redelivery even after the old item finished")
    repeated = legacy.add("old ask", "C1", "ROOT", "U1", ask_ts="106.0",
                          source_id="event:new-repeat")
    check(repeated is not None,
          "consuming the fuzzy legacy marker preserves a distinct repeated instruction")

    legacy_late_path = os.path.join(tmp, "queue-legacy-late.json")
    with open(legacy_late_path, "w", encoding="utf-8") as f:
        json.dump({"next_id": 2, "items": [{"id": 1, "text": "publish report",
                   "channel": "C1", "thread": "ROOT", "user": "U1",
                   "state": "waiting", "from_dog": False, "queued_at": 100.0}]}, f)
    legacy_late = sb.TaskQueue("legacy-late")
    legacy_late.finish(1)
    late_repeat = legacy_late.add("publish report", "C1", "ROOT", "U1",
                                  ask_ts="3700.0", source_id="event:late-repeat")
    check(late_repeat is not None,
          "a legacy tombstone cannot discard the same legitimate instruction an hour later")

    orphan = sb.TaskQueue("orphan")
    orphan.add("guarded", "C1", "G1", "U1")
    guarded = orphan.take()
    orphan.attach_process(guarded["id"], os.getpid(),
                          os.path.join(tmp, "orphan-guard-state.json"))
    del orphan
    orphan = sb.TaskQueue("orphan", recover_running=True)
    check(orphan.items[0]["state"] == "orphaned" and orphan.take() is None,
          "a replacement listener cannot overlap a still-closing execution guard")
    real_execution_alive = sb._execution_alive
    sb._execution_alive = lambda item: False
    check(orphan.reap_orphans() == 1 and orphan.items[0]["state"] == "interrupted",
          "only after that process tree exits does the task become explicitly retryable")
    sb._execution_alive = real_execution_alive

    real_pid_alive, real_identity = sb._pid_alive, sb._process_identity
    sb._pid_alive = lambda pid: True
    sb._process_identity = lambda pid: ""
    check(sb._same_process(123, "persisted-creation-id"),
          "a transient identity lookup failure keeps a known live execution fenced")
    check(not sb._same_process(123, ""),
          "a legacy bare PID is not trusted when process identity cannot be proved")
    sb._process_identity = lambda pid: "different-creation-id"
    check(not sb._same_process(123, "persisted-creation-id"),
          "a positive creation mismatch releases a reused PID fence")
    sb._pid_alive, sb._process_identity = real_pid_alive, real_identity

    # ── scope: what it will even listen to ─────────────────────────────────
    # The gates live in main()'s loop, so what is checked here is the parsing
    # that feeds them — an empty setting must mean "unset", never "allow ''".
    def parse(v):
        return {x.strip() for x in v.split(",") if x.strip()}

    check(parse("") == set(), "an unset allowlist is empty, not a set containing ''")
    check(parse("C1, C2 ,,C3") == {"C1", "C2", "C3"},
          "ids survive spaces and stray commas — a pasted list should not silently lose one")
    check(parse("U1") == {"U1"}, "a single id works")

    # ── the ask itself ─────────────────────────────────────────────────────
    t = sb.MENTION_RE.sub("", "<@U08ABCD1> release 0.20.29 and say so").strip()
    check(t == "release 0.20.29 and say so",
          "the mention is stripped and the ask is not (%r)" % t)
    check(sb.MENTION_RE.sub("", "<@U1> <@U2> both of you").strip() == "both of you",
          "including when several people are named")
    check(sb.slack_event_key({"event_id": "Ev1"}, {"channel": "C1", "ts": "1.0"})
          == sb.slack_event_key({"event_id": "Ev1"}, {"channel": "C9", "ts": "9.0"}),
          "the durable ingress key survives a replacement Socket Mode envelope")

    pack_state = {}
    for i in range(sb.PACK_LAPS):
        sb.pack_gate(pack_state, "PACK-T", "BOT1", source_id="event:lap%d" % i)
    refused = sb.pack_gate(pack_state, "PACK-T", "BOT1", source_id="event:loop-stop")
    gate_receipts = sb.TaskQueue("gate-receipts")
    if refused:
        gate_receipts.record_event("event:loop-stop", "C1", "30.0")
    del gate_receipts
    gate_receipts = sb.TaskQueue("gate-receipts")
    check(bool(refused) and gate_receipts.is_duplicate(
        "event:loop-stop", "C1", "30.0", "PACK-T", "BOT1", "again"),
        "a loop-gate refusal survives restart, so redelivery cannot become executable work")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "slackbot: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
