"""Pin the built-in capabilities + the autonomous drive() path (harness.capabilities).

Run: python tests/test_capabilities.py   (exit 0 = all green)

Runs the real note.append capability end to end (a real file write, verified by
a real independent re-read), proves a reversible in-scope action auto-executes
under drive() while an irreversible one parks for confirm, and checks path-
traversal safety.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_notes = tempfile.mkdtemp(prefix="collie-notes-")
os.environ["COLLIE_NOTES_DIR"] = _tmp_notes

from harness.actions import ActionStore, RefusedError  # noqa: E402
from harness.jobs import (Capability, Executor, JobStore, register,  # noqa: E402
                          clear_registry, NEEDS_YOU, DONE_VERIFIED)
from harness import capabilities as caps  # noqa: E402
from harness.verifier import Verdict, VERIFIED, INCONCLUSIVE  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


def _stores():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return ActionStore(p + ".a"), JobStore(p + ".j")


def test_note_append_live_end_to_end():
    print("test_note_append_live_end_to_end")
    clear_registry()
    caps.register_builtins()
    acts, jobs = _stores()
    jobs.create("j", "remember", leash={"may": ["note.*"]})
    n = acts.propose("note.append", {"file": "t.txt", "text": "hello-world-42"}, job_id="j")
    v = Executor(acts, jobs).drive(n)            # reversible + in scope -> auto-run
    check(v.status == VERIFIED, f"note.append must verify via re-read, got {v.status}")
    check(jobs.get("j").state == DONE_VERIFIED, "job -> done_verified")
    with open(os.path.join(_tmp_notes, "t.txt"), encoding="utf-8") as f:
        check("hello-world-42" in f.read(), "the text must actually be on disk")
    acts.close(); jobs.close()


def test_failed_when_verify_cannot_find_text():
    print("test_failed_when_verify_cannot_find_text")
    # a capability whose execute writes NOTHING but claims note.append semantics:
    # the independent re-read finds the text absent -> FAILED (not a false pass).
    clear_registry()
    register(Capability("note.append", execute=lambda r: {"ok": True},  # no write
                        verify=caps._note_verify, reversible=True, risk="reversible"))
    acts, jobs = _stores()
    jobs.create("j2", "x", leash={"may": ["note.*"]})
    n = acts.propose("note.append", {"file": "never.txt", "text": "ghost"}, job_id="j2")
    v = Executor(acts, jobs).drive(n)
    check(v.status != VERIFIED, f"a write that didn't land must NOT verify, got {v.status}")
    acts.close(); jobs.close()


def test_drive_parks_irreversible_for_confirm():
    print("test_drive_parks_irreversible_for_confirm")
    clear_registry()
    fired = {"v": False}
    register(Capability("pay.charge", execute=lambda r: fired.__setitem__("v", True),
                        verify=lambda r, res: Verdict(VERIFIED, "ok"),
                        reversible=False, risk="irreversible"))
    acts, jobs = _stores()
    jobs.create("j3", "pay", leash={"may": ["pay.*"]})   # default irreversible -> ASK
    n = acts.propose("pay.charge", {"amt": 5}, job_id="j3")
    ex = Executor(acts, jobs)
    v = ex.drive(n)
    check(v.status == INCONCLUSIVE and not fired["v"],
          "an unconfirmed irreversible action must NOT fire under drive()")
    check(jobs.get("j3").state == NEEDS_YOU, "it must park the job in needs_you")
    # now a human confirms -> it executes
    acts.confirm(n)
    v2 = ex.run_confirmed(n, job_id="j3")
    check(v2.status == VERIFIED and fired["v"], "after confirm it fires and verifies")
    acts.close(); jobs.close()


def test_path_traversal_is_contained():
    print("test_path_traversal_is_contained")
    p = caps._safe_path("../../etc/passwd")
    check(os.path.dirname(p) == _tmp_notes, f"path must stay in the sandbox dir, got {p}")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    clear_registry()
    if _fails:
        print(f"\n== CAPABILITIES: {len(_fails)} FAILED ==")
        sys.exit(1)
    print(f"\n== CAPABILITIES: {len(tests)} test groups passed ==")


if __name__ == "__main__":
    main()
