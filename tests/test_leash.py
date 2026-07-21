"""Pin the leash authority model (harness.leash) + its enforcement in the executor.

Run: python tests/test_leash.py   (exit 0 = all green)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.leash import evaluate, ALLOW, ASK, DENY  # noqa: E402
from harness.actions import ActionStore, RefusedError  # noqa: E402
from harness.jobs import (Capability, Executor, JobStore, register,  # noqa: E402
                          clear_registry, NEEDS_YOU)
from harness.verifier import Verdict, VERIFIED  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


def test_unenforced_when_no_may():
    print("test_unenforced_when_no_may")
    check(evaluate({}, "anything").decision == ALLOW, "empty leash is unenforced")
    check(evaluate({"price_floor": 400}, "listing.publish").decision == ALLOW,
          "a leash without `may` is unenforced (backward compat)")


def test_allowlist_glob():
    print("test_allowlist_glob")
    L = {"may": ["listing.*", "note.append"]}
    check(evaluate(L, "listing.publish").decision == ASK, "matched irreversible -> ASK")
    check(evaluate(L, "note.append", cap_risk="reversible").decision == ALLOW,
          "matched reversible -> ALLOW")
    check(evaluate(L, "pay.charge").decision == DENY, "unmatched capability -> DENY")


def test_empty_may_denies_all():
    print("test_empty_may_denies_all")
    check(evaluate({"may": []}, "note.append").decision == DENY,
          "declared-but-empty may is an explicit lockdown")


def test_irreversible_modes():
    print("test_irreversible_modes")
    check(evaluate({"may": ["pay.*"], "irreversible": "deny"}, "pay.charge").decision == DENY,
          "irreversible=deny blocks")
    check(evaluate({"may": ["pay.*"], "irreversible": "allow"}, "pay.charge").decision == ALLOW,
          "irreversible=allow pre-authorizes")
    check(evaluate({"may": ["pay.*"]}, "pay.charge").decision == ASK,
          "default irreversible -> ASK (needs confirm)")


def test_spend_cap_and_expiry():
    print("test_spend_cap_and_expiry")
    check(evaluate({"may": ["pay.*"], "spend_max_usd": 100}, "pay.charge",
                   spend_usd=250).decision == DENY, "over spend cap -> DENY")
    check(evaluate({"may": ["x.*"], "expires": "2026-01-01"}, "x.y",
                   now_iso="2026-07-21").decision == DENY, "expired leash -> DENY")


def test_executor_enforces_deny_even_after_confirm():
    print("test_executor_enforces_deny_even_after_confirm")
    clear_registry()
    fired = {"v": False}
    register(Capability("pay.charge", execute=lambda r: fired.__setitem__("v", True),
                        verify=lambda r, res: Verdict(VERIFIED, "ok"), reversible=False))
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    acts = ActionStore(p + ".a")
    jobs = JobStore(p + ".j")
    # leash permits only listing.*; pay.charge is out of scope
    jobs.create("j1", "pay something", leash={"may": ["listing.*"]})
    n = acts.propose("pay.charge", {"amt": 50}, job_id="j1")
    acts.confirm(n)                        # human confirms — but leash still denies
    try:
        Executor(acts, jobs).run_confirmed(n, job_id="j1")
        check(False, "leash DENY must block execution even after confirm")
    except RefusedError as e:
        check("leash denied" in str(e), f"refusal must cite the leash: {e}")
    check(not fired["v"], "a leash-denied side effect must NOT fire")
    acts.close(); jobs.close()


def test_executor_allows_within_leash():
    print("test_executor_allows_within_leash")
    clear_registry()
    register(Capability("listing.publish", execute=lambda r: {"ok": True},
                        verify=lambda r, res: Verdict(VERIFIED, "ok"), reversible=False))
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    acts = ActionStore(p + ".a")
    jobs = JobStore(p + ".j")
    jobs.create("j2", "publish", leash={"may": ["listing.*"], "irreversible": "confirm"})
    n = acts.propose("listing.publish", {"x": 1}, job_id="j2")
    acts.confirm(n)
    v = Executor(acts, jobs).run_confirmed(n, job_id="j2")
    check(v.status == VERIFIED, f"in-scope confirmed action must execute, got {v.status}")
    acts.close(); jobs.close()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    clear_registry()
    if _fails:
        print(f"\n== LEASH: {len(_fails)} FAILED ==")
        sys.exit(1)
    print(f"\n== LEASH: {len(tests)} test groups passed ==")


if __name__ == "__main__":
    main()
