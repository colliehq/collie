"""Measure what a session checkpoint actually costs on a long conversation.

`sessions.checkpoint()` runs on every replay-safe boundary of a run, and each call
rewrites the whole `data/sessions/<id>.json`. This script answers, with numbers
rather than intuition:

* how long one checkpoint takes at 100 / 1,000 / 5,000 messages,
* how many bytes it actually writes,
* whether the cost tracks message COUNT or journal BYTES,
* where the time goes (parse / validate / merge / serialize / write+fsync),
* what a whole run costs when every appended message is checkpointed.

Writes only into a temporary directory it creates and deletes. No network, no
provider calls, no user state. Run:

    python bench/session_storage_profile.py            # full grid
    python bench/session_storage_profile.py --quick    # short version
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_messages(count, profile):
    """A transcript shaped like the traffic a coding agent actually persists.

    `lean` is chat-only; `rich` carries tool calls and multi-KB tool results,
    which is what a long unattended run really accumulates.
    """
    out = []
    for i in range(count):
        slot = i % 4
        if profile == "lean":
            if slot == 0:
                out.append({"role": "user", "content": "step %d: keep going" % i})
            else:
                out.append({"role": "assistant",
                            "content": "acknowledged %d; %s" % (i, "ok " * 12)})
            continue
        if slot == 0:
            out.append({"role": "user",
                        "content": "step %d: find and fix the failing case\n%s"
                                   % (i, "context line\n" * 6)})
        elif slot == 1:
            out.append({"role": "assistant",
                        "content": "I will read the module first.\n%s" % ("reasoning. " * 40),
                        "tool_calls": [{"id": "call-%d" % i, "name": "read_file",
                                        "args": {"path": "harness/module_%d.py" % i,
                                                 "offset": i, "limit": 200}}]})
        elif slot == 2:
            out.append({"role": "tool", "tool_call_id": "call-%d" % (i - 1),
                        "name": "read_file",
                        "content": "".join("%4d| source line %d of module\n" % (n, n)
                                           for n in range(60))})
        else:
            out.append({"role": "assistant",
                        "content": "Here is what that shows.\n%s" % ("analysis sentence. " * 30)})
    return out


def measure_bytes(sessions):
    """Wrap _atomic_dump so every byte the store writes is counted exactly."""
    original = sessions._atomic_dump
    counter = {"writes": 0, "bytes": 0}

    def counting_dump(obj, path):
        original(obj, path)
        counter["writes"] += 1
        try:
            counter["bytes"] += os.path.getsize(path)
        except OSError:
            pass
        return None

    sessions._atomic_dump = counting_dump
    return counter, (lambda: setattr(sessions, "_atomic_dump", original))


def bounded_updates(sessions, sid, messages, updates):
    """Cost of `updates` successive checkpoints on an already long journal."""
    live = list(messages)
    sessions.checkpoint(sid, live, run_id="seed", turn=0, state="turn_boundary")
    counter, restore = measure_bytes(sessions)
    samples = []
    try:
        for n in range(updates):
            live.append({"role": "assistant", "content": "checkpoint update %d" % n})
            start = time.perf_counter()
            sessions.checkpoint(sid, live, run_id="run-1", turn=n,
                                state="executing_tool",
                                detail={"tool_name": "edit_file", "tool_call_id": "u%d" % n})
            samples.append((time.perf_counter() - start) * 1000.0)
    finally:
        restore()
    return {
        "updates": updates,
        "ms_median": statistics.median(samples),
        "ms_total": sum(samples),
        "bytes_written": counter["bytes"],
        "file_bytes": sessions.storage_bytes(sid),
    }


def phase_breakdown(sessions, sid, messages, reps=5):
    """Where one checkpoint's time goes, measured on the same journal."""
    path = sessions._path(sid)
    parse = validate = merge = serialize = write = 0.0
    outgoing = sessions._msgs_out(messages)
    for _ in range(reps):
        start = time.perf_counter()
        raw = sessions._load_raw(path)
        parse += time.perf_counter() - start

        start = time.perf_counter()
        sessions._validate_raw(raw, sid)
        validate += time.perf_counter() - start

        start = time.perf_counter()
        merged = sessions._merge_messages(raw.get("messages"), outgoing)
        merge += time.perf_counter() - start

        obj = dict(raw)
        obj["messages"] = merged
        start = time.perf_counter()
        json.dumps(obj, ensure_ascii=False, default=str, allow_nan=False)
        serialize += time.perf_counter() - start

        start = time.perf_counter()
        sessions._atomic_dump(obj, path)
        write += time.perf_counter() - start
    scale = 1000.0 / reps
    total = (parse + validate + merge + serialize + write) * scale
    return {"parse_ms": parse * scale, "validate_ms": validate * scale,
            "merge_ms": merge * scale, "serialize_ms": serialize * scale,
            "atomic_write_ms": write * scale, "total_ms": total}


def growth_run(sessions, sid, messages):
    """A whole run: checkpoint after every appended message, from empty."""
    counter, restore = measure_bytes(sessions)
    live = []
    start = time.perf_counter()
    try:
        for message in messages:
            live.append(message)
            sessions.checkpoint(sid, live, run_id="grow", turn=len(live),
                                state="turn_boundary")
    finally:
        restore()
    return {"messages": len(messages), "wall_s": time.perf_counter() - start,
            "checkpoints": counter["writes"], "bytes_written": counter["bytes"],
            "file_bytes": sessions.storage_bytes(sid)}


def legacy_atomic_dump(obj, p):
    """`sessions._atomic_dump` exactly as it was before this review's change.

    Kept here so the before/after claim stays reproducible after the fix has
    landed: the only difference is `json.dump(obj, fp)` (CPython's pure-Python
    streaming encoder, one fp.write per token) versus `json.dumps` + one write.
    """
    tmp = "%s.%d.%s.tmp" % (p, os.getpid(), os.urandom(6).hex())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, default=str, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        for attempt in range(7):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if attempt >= 6:
                    raise
                time.sleep(.01 * (2 ** attempt))
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def ab_compare(sessions, counts, profiles, reps=25):
    """Interleave the old and new durable write on identical input.

    Both variants run back to back on the same object in the same process, so
    antivirus/scheduler noise hits them equally; the byte-for-byte assertion is
    what makes the timing comparison a fair one.
    """
    root = os.path.dirname(sessions._path("ab"))
    rows = []
    for profile in profiles:
        for count in counts:
            messages = sessions._msgs_out(build_messages(count, profile))
            obj = {"id": "ab", "project": "p", "cwd": "c", "updated": 1.0,
                   "messages": messages, "last_answer": ""}
            old_path = os.path.join(root, "ab-old.json")
            new_path = os.path.join(root, "ab-new.json")
            old, new = [], []
            for _ in range(reps):
                start = time.perf_counter()
                legacy_atomic_dump(obj, old_path)
                old.append((time.perf_counter() - start) * 1000.0)
                start = time.perf_counter()
                sessions._atomic_dump(obj, new_path)
                new.append((time.perf_counter() - start) * 1000.0)
            with open(old_path, "rb") as fh:
                old_bytes = fh.read()
            with open(new_path, "rb") as fh:
                new_bytes = fh.read()
            identical = old_bytes == new_bytes
            row = {"profile": profile, "messages": count, "file_bytes": len(new_bytes),
                   "old_ms": statistics.median(old), "new_ms": statistics.median(new),
                   "identical_bytes": identical}
            rows.append(row)
            print("[ab     ] %-5s %5d msgs (%7.1f KiB)  legacy=%6.2f ms  current=%6.2f ms"
                  "  %+.0f%%  bytes_identical=%s"
                  % (profile, count, len(new_bytes) / 1024, row["old_ms"], row["new_ms"],
                     -100 * (1 - row["new_ms"] / row["old_ms"]), identical))
            for path in (old_path, new_path):
                try:
                    os.remove(path)
                except OSError:
                    pass
    return rows


def ab_checkpoint(sessions, counts, profiles, reps=15):
    """The same interleaved comparison, but end to end through `checkpoint()`.

    This is the number a run actually experiences: lock, parse, validate, merge,
    encode, fsync, replace. Only the encode+write half is affected by the change,
    so the whole-checkpoint improvement is necessarily smaller than the write-only
    improvement measured by `ab_compare`.
    """
    current = sessions._atomic_dump
    rows = []
    for profile in profiles:
        for count in counts:
            messages = build_messages(count, profile)
            old, new = [], []
            for variant, samples, dump in (("old", old, legacy_atomic_dump),
                                           ("new", new, current)):
                sid = "abck-%s-%d-%s" % (profile, count, variant)
                sessions._atomic_dump = current
                sessions.checkpoint(sid, messages, run_id="seed", state="turn_boundary")
                sessions._atomic_dump = dump
                live = list(messages)
                for n in range(reps):
                    live.append({"role": "assistant", "content": "u%d" % n})
                    start = time.perf_counter()
                    sessions.checkpoint(sid, live, run_id="r", turn=n,
                                        state="executing_tool",
                                        detail={"tool_name": "edit_file",
                                                "tool_call_id": "u%d" % n})
                    samples.append((time.perf_counter() - start) * 1000.0)
            sessions._atomic_dump = current
            row = {"profile": profile, "messages": count,
                   "old_ms": statistics.median(old), "new_ms": statistics.median(new)}
            rows.append(row)
            print("[ab-ckpt] %-5s %5d msgs  legacy=%6.2f ms  current=%6.2f ms  %+.0f%%"
                  " per checkpoint"
                  % (profile, count, row["old_ms"], row["new_ms"],
                     -100 * (1 - row["new_ms"] / row["old_ms"])))
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--updates", type=int, default=20)
    parser.add_argument("--ab", action="store_true",
                        help="interleaved before/after comparison of the durable write")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    counts = [100, 1000] if args.quick else [100, 1000, 5000]
    profiles = ["rich"] if args.quick else ["lean", "rich"]
    growth_counts = [200] if args.quick else [200, 1000]

    root = tempfile.mkdtemp(prefix="collie-session-profile-")
    os.environ["COLLIE_SESSIONS_DIR"] = root
    from harness import sessions

    began = time.time()
    report = {"python": sys.version.split()[0], "platform": sys.platform,
              "bounded": [], "phases": [], "growth": [], "ab": []}
    try:
        if args.ab:
            report["ab"] = ab_compare(sessions, counts, profiles)
            report["ab_checkpoint"] = ab_checkpoint(sessions, counts, profiles)
        for profile in profiles:
            for count in counts:
                messages = build_messages(count, profile)
                sid = "prof-%s-%d" % (profile, count)
                row = bounded_updates(sessions, sid, messages, args.updates)
                row.update(profile=profile, messages=count)
                row["bytes_per_message"] = row["file_bytes"] / max(1, count)
                report["bounded"].append(row)
                print("[bounded] %-5s %5d msgs  file=%7.1f KiB  "
                      "median=%7.2f ms/checkpoint  wrote=%8.1f KiB / %d updates"
                      % (profile, count, row["file_bytes"] / 1024, row["ms_median"],
                         row["bytes_written"] / 1024, row["updates"]))

                phases = phase_breakdown(sessions, sid, messages)
                phases.update(profile=profile, messages=count)
                report["phases"].append(phases)
                print("           parse=%.2f validate=%.2f merge=%.2f "
                      "serialize=%.2f write+fsync=%.2f  (ms)"
                      % (phases["parse_ms"], phases["validate_ms"], phases["merge_ms"],
                         phases["serialize_ms"], phases["atomic_write_ms"]))

        for count in growth_counts:
            messages = build_messages(count, "rich")
            row = growth_run(sessions, "grow-%d" % count, messages)
            report["growth"].append(row)
            print("[growth ] %5d msgs  %6.2f s for %d checkpoints, "
                  "wrote %.1f MiB to reach a %.1f KiB journal"
                  % (row["messages"], row["wall_s"], row["checkpoints"],
                     row["bytes_written"] / 1048576, row["file_bytes"] / 1024))

        report["elapsed_s"] = time.time() - began
        print("\ntotal profiling wall time: %.1f s" % report["elapsed_s"])
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)
    finally:
        # Only remove the temporary profile directory created by this invocation.
        assert os.path.dirname(os.path.realpath(root)) == os.path.realpath(tempfile.gettempdir())
        assert os.path.basename(root).startswith("collie-session-profile-")
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
