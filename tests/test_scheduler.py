"""Pin durable waiting + catch-up-on-wake (harness.scheduler).

Run: python tests/test_scheduler.py   (exit 0 = all green)

Proves a parked action fires when due, survives a "restart", is not fired early,
and that an irreversible parked action parks (needs_you) instead of auto-firing.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_notes = tempfile.mkdtemp(prefix="collie-sched-notes-")
os.environ["COLLIE_NOTES_DIR"] = _notes

from harness.actions import ActionStore  # noqa: E402
from harness.jobs import (Capability, JobStore, register, clear_registry,  # noqa: E402
                          WAITING, NEEDS_YOU, DONE_VERIFIED)
from harness.scheduler import Scheduler  # noqa: E402
from harness import capabilities as caps  # noqa: E402
from harness.verifier import Verdict, VERIFIED  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


def _paths():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return p + ".a", p + ".j"


def test_due_wait_fires_and_drives():
    print("test_due_wait_fires_and_drives")
    clear_registry(); caps.register_builtins()
    ap, jp = _paths()
    acts, jobs = ActionStore(ap), JobStore(jp)
    jobs.create("j", "later", leash={"may": ["note.*"]})
    n = acts.propose("note.append", {"file": "s.txt", "text": "wake-fired"}, job_id="j")
    sched = Scheduler(acts, jobs, db_path=jp)
    sched.schedule("j", n, fire_at=100, now=90)
    check(jobs.get("j").state == WAITING, "scheduling a wait -> job WAITING")
    check(sched.tick(now=95) == 0, "a not-yet-due wait must NOT fire")
    check(sched.tick(now=100) == 1, "a due wait must fire")
    check(jobs.get("j").state == DONE_VERIFIED, "firing drove the action to done_verified")
    with open(os.path.join(_notes, "s.txt"), encoding="utf-8") as f:
        check("wake-fired" in f.read(), "the parked action really ran")
    sched.close(); acts.close(); jobs.close()


def test_catch_up_across_restart():
    print("test_catch_up_across_restart")
    clear_registry(); caps.register_builtins()
    ap, jp = _paths()
    acts, jobs = ActionStore(ap), JobStore(jp)
    jobs.create("j", "later", leash={"may": ["note.*"]})
    n = acts.propose("note.append", {"file": "r.txt", "text": "survived"}, job_id="j")
    s1 = Scheduler(acts, jobs, db_path=jp)
    s1.schedule("j", n, fire_at=100, now=50)
    s1.close(); acts.close(); jobs.close()          # simulate the box sleeping/rebooting
    # fresh process reopens the durable stores; catch-up fires the overdue wait
    acts2, jobs2 = ActionStore(ap), JobStore(jp)
    s2 = Scheduler(acts2, jobs2, db_path=jp)
    fired = s2.tick(now=200)                          # now way past fire_at
    check(fired == 1, "catch-up-on-wake must fire the overdue wait after restart")
    check(jobs2.get("j").state == DONE_VERIFIED, "the job converges after restart")
    s2.close(); acts2.close(); jobs2.close()


def test_irreversible_parks_not_autofires():
    print("test_irreversible_parks_not_autofires")
    clear_registry()
    fired = {"v": False}
    register(Capability("pay.charge", execute=lambda r: fired.__setitem__("v", True),
                        verify=lambda r, res: Verdict(VERIFIED, "ok"),
                        reversible=False, risk="irreversible"))
    ap, jp = _paths()
    acts, jobs = ActionStore(ap), JobStore(jp)
    jobs.create("j", "pay later", leash={"may": ["pay.*"]})
    n = acts.propose("pay.charge", {"amt": 9}, job_id="j")
    sched = Scheduler(acts, jobs, db_path=jp)
    sched.schedule("j", n, fire_at=10, now=1)
    sched.tick(now=20)                                # due, but irreversible
    check(not fired["v"], "a timer must NOT auto-fire an irreversible action")
    check(jobs.get("j").state == NEEDS_YOU, "it parks in needs_you for a human confirm")
    check(len(sched.pending_waits()) == 0, "the wait itself is spent (won't re-fire in a loop)")
    sched.close(); acts.close(); jobs.close()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    clear_registry()
    if _fails:
        print(f"\n== SCHEDULER: {len(_fails)} FAILED ==")
        sys.exit(1)
    print(f"\n== SCHEDULER: {len(tests)} test groups passed ==")


if __name__ == "__main__":
    main()
