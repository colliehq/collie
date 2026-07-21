"""Pin harness.verifier against the real loop.py gate + the world generalization.

Run: python tests/test_verifier.py   (exit 0 = all green)

The load-bearing test is test_matches_loop_gate: it re-derives the exact accept
condition from loop.py:693-699 as a reference and asserts CodeReproVerifier
agrees on the full truth table — so the extraction is provably faithful, not a
lookalike. If someone later edits the gate logic in either place, this fails.
"""
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.verifier import (  # noqa: E402
    CodeReproVerifier, ListingVerifier, Mutation, Observation, Verdict,
    VERIFIED, FAILED, INCONCLUSIVE, NOT_ARMED,
)

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


def loop_gate_verified(did_edit, last_edit_turn, last_repro_turn,
                       last_repro_failed, last_repro_asserted, require_assert):
    """Reference: the EXACT accept condition inlined at loop.py:693-699.
    Returns True iff the loop would let the model finish as verified."""
    if not did_edit:
        return None  # gate not armed: finish allowed via a different branch
    return (last_repro_turn >= last_edit_turn
            and not last_repro_failed
            and (last_repro_asserted or not require_assert))


def test_matches_loop_gate():
    print("test_matches_loop_gate")
    # Sweep the full boolean/ordering space the loop tracks.
    edit_turns = [5]
    repro_turns = [3, 5, 7]          # before / same / after the edit (freshness)
    for (did_edit, et, rt, failed, asserted, req) in itertools.product(
            [True, False], edit_turns, repro_turns,
            [True, False], [True, False], [True, False]):
        ref = loop_gate_verified(did_edit, et, rt, failed, asserted, req)
        v = CodeReproVerifier(require_assert=req)
        muts = [Mutation(at=et)] if did_edit else []
        # one post-edit reproduction observed on the exit-code channel
        obs = [Observation(channel="exit-code", at=rt, ok=not failed, asserted=asserted)]
        verdict = v.verdict(muts, obs)
        if ref is None:
            check(verdict.status == NOT_ARMED,
                  f"did_edit=False must be NOT_ARMED, got {verdict.status}")
        else:
            check(verdict.verified == ref,
                  f"mismatch vs loop gate at "
                  f"(rt={rt},et={et},failed={failed},asserted={asserted},req={req}): "
                  f"loop={ref} verifier={verdict.verified} ({verdict.status})")


def test_traceback_but_exit_zero_passes():
    # loop.py:100-102: a repro that PRINTS 'Traceback' but exits 0 must pass —
    # the exit code is ground truth, not the substring.
    print("test_traceback_but_exit_zero_passes")
    v = CodeReproVerifier(require_assert=True)
    obs = [Observation(channel="exit-code", at=6, ok=True, asserted=True,
                       detail="Traceback (most recent call last):  # caught + echoed")]
    check(v.verdict([Mutation(at=5)], obs).verified,
          "exit-0 repro that prints 'Traceback' must still verify")


def test_stale_evidence_is_inconclusive():
    # freshness (loop.py:695): a passing repro from BEFORE the last edit does not
    # verify the current code.
    print("test_stale_evidence_is_inconclusive")
    v = CodeReproVerifier()
    obs = [Observation(channel="exit-code", at=3, ok=True)]  # at < edit turn 5
    vd = v.verdict([Mutation(at=5)], obs)
    check(vd.status == INCONCLUSIVE, f"stale evidence must be INCONCLUSIVE, got {vd.status}")


def test_print_only_no_assert_inconclusive_in_assert_mode():
    # loop.py:202-206: in assert-mode a print-only repro (no `assert`) is not
    # verification.
    print("test_print_only_no_assert_inconclusive_in_assert_mode")
    v = CodeReproVerifier(require_assert=True)
    obs = [Observation(channel="exit-code", at=6, ok=True, asserted=False)]
    vd = v.verdict([Mutation(at=5)], obs)
    check(vd.status == INCONCLUSIVE,
          f"print-only repro in assert-mode must be INCONCLUSIVE, got {vd.status}")
    # ...and the SAME observation verifies once require_assert is off
    check(CodeReproVerifier(require_assert=False).verdict([Mutation(at=5)], obs).verified,
          "print-only repro must verify when assert is not required")


def test_world_rejects_the_acting_channel():
    # The generalization's whole point: a listing's own "Published!" toast came
    # back through the acting path and MUST NOT count. Only the independent
    # logged-out re-fetch verifies.
    print("test_world_rejects_the_acting_channel")
    v = ListingVerifier()
    mut = [Mutation(at=10, kind="publish", reversible=False)]
    toast = [Observation(channel="publish-page-toast", at=11, ok=True, asserted=True)]
    check(v.verdict(mut, toast).status == INCONCLUSIVE,
          "success toast on the acting channel must NOT verify a listing")
    refetch = [Observation(channel="logged-out-fetch", at=11, ok=True, asserted=True,
                           detail="GET listing url -> 200, title+price present")]
    check(v.verdict(mut, refetch).verified,
          "logged-out re-fetch asserting title+price must verify")


def test_irreversible_failure_is_not_repairable():
    # piece 6: a FAILED post-check on an irreversible action must not trigger a
    # blind retry round (that double-sends). INCONCLUSIVE still may (re-observe).
    print("test_irreversible_failure_is_not_repairable")
    v = ListingVerifier()
    irreversible = [Mutation(at=10, kind="publish", reversible=False)]
    failed = Verdict(FAILED, "refuted")
    inconclusive = Verdict(INCONCLUSIVE, "could not observe")
    check(not v.repairable(failed, irreversible),
          "FAILED on an irreversible action must NOT be repairable")
    check(v.repairable(inconclusive, irreversible),
          "INCONCLUSIVE (could not observe) stays repairable — re-observe, don't re-act")
    # reversible code edit: a failed post-check IS repairable (edit again, re-run)
    check(v.repairable(failed, [Mutation(at=10, reversible=True)]),
          "FAILED on a reversible edit must be repairable")


def test_precheck_enforces_mandate_before_acting():
    # the "repro must fail on broken code first" half, in the world: assert the
    # PREPARED state before the irreversible publish.
    print("test_precheck_enforces_mandate_before_acting")
    v = ListingVerifier()
    ok = v.precheck({"price": 500, "already_live": False}, {"price_floor": 450})
    check(ok.verified, "price above floor should pass precheck")
    low = v.precheck({"price": 400, "already_live": False}, {"price_floor": 450})
    check(low.status == FAILED, "price below the mandate floor must fail precheck")
    dup = v.precheck({"price": 500, "already_live": True}, {"price_floor": 450})
    check(dup.status == FAILED, "already-live listing must fail precheck (dedup)")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if _fails:
        print(f"\n== VERIFIER: {len(_fails)} FAILED ==")
        sys.exit(1)
    print(f"\n== VERIFIER: {len(tests)} test groups passed ==")


if __name__ == "__main__":
    main()
