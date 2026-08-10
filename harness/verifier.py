"""Pluggable done-checks: collie's verification gate, lifted off the code path.

Today the gate is three booleans wired into loop.Harness (`verify_gate`,
`require_assert`, `critic`, loop.py:199-216) whose ground truth is the process
exit code of a post-edit reproduction (loop.py:97-106). That is the code special
case of a general shape. A delegate that acts in the world needs the SAME shape
with a different substrate: instead of "re-run repro.py, read the exit code",
it "re-observe the world through an independent channel, assert the outcome".

This module is that shape as a small protocol so a code fix and a published
marketplace listing clear the same gate. The signature stays: **no evidence ->
not verified**. It is intentionally loop-free and stdlib-only; loop.Harness (and
later the daemon-side executor) feed it the signals they already track.

The six load-bearing pieces, each mapped to the line in loop.py it generalizes:

  1. ARMS ONLY AFTER A CHANGE      did_edit  (loop.py:604,693) -> Mutation list
  2. GROUND TRUTH, NOT SELF-REPORT exit code (loop.py:97-106) -> Observation.ok
  3. FRESHNESS                     repro_turn >= edit_turn (loop.py:695) -> .at
  4. ASSERTION STRENGTH DIAL       require_assert (loop.py:610,699) -> .asserted
  5. BOUNDED REPAIR                verify_max=2 (loop.py:200,700) -> repairable()
  6. REVERSIBILITY SPLIT           (new for the world) -> Mutation.reversible

Piece 6 has no code analogue because edits are reversible: the code gate is
purely post-hoc (edit -> re-run -> repair). A sent email or a published listing
is not, so a failed post-check on an IRREVERSIBLE action is never a silent retry
round — it is `failed` plus optional compensation. `repairable()` encodes that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence


# ── verdict vocabulary ──────────────────────────────────────────────────────
# Deliberately four states, not a bool. "inconclusive" is the anti-Manus state:
# the action may have happened but we could not OBSERVE that it did — it must be
# distinguishable from "verified", and it can never be laundered into success.
VERIFIED = "verified"        # fresh independent evidence satisfies the check
FAILED = "failed"            # fresh evidence REFUTES the outcome
INCONCLUSIVE = "inconclusive"  # armed, but no fresh/asserted evidence — NOT done
NOT_ARMED = "not-armed"      # no state change occurred; nothing to verify


@dataclass(frozen=True)
class Observation:
    """One freshly-executed READ of the target state, produced AFTER an action.

    The code gate's observation is a `python repro.py` run; `ok` is its exit
    code (loop.py:97-106 — exit 0, not a 'Traceback' substring, is ground truth).
    A world observation is a logged-out page fetch, an IMAP query of the Sent
    folder, a re-read confirmation page. `channel` names HOW it was observed so a
    verifier can refuse evidence that came back through the same path that acted
    (the app's own "Success!" toast must not count as verification).
    """
    channel: str            # "exit-code" | "logged-out-fetch" | "imap-sent" | ...
    at: float               # monotone order key: turn index or unix ts
    ok: bool                # did the observation's ground-truth signal pass?
    asserted: bool = False  # did it execute a HARD assertion (assert-mode)?
    detail: str = ""        # human-readable, goes into the receipt


@dataclass(frozen=True)
class Mutation:
    """A state-changing action that ARMS the gate (the `did_edit` generalization).

    reversible=True is a code edit (undo is free). reversible=False is a send /
    publish / submit / pay: a failed post-check cannot be repaired by another
    round — see repairable().
    """
    at: float
    kind: str = "edit"
    reversible: bool = True
    detail: str = ""


@dataclass(frozen=True)
class Verdict:
    status: str
    reason: str = ""
    evidence: tuple = ()      # the Observation(s) that decided it

    @property
    def verified(self) -> bool:
        return self.status == VERIFIED

    def __bool__(self) -> bool:  # `if verdict:` means "safe to finish as done(verified)"
        return self.verified


# ── the protocol ────────────────────────────────────────────────────────────
class Verifier:
    """A per-errand-type done-check. Subclasses pick which channels count and
    whether a hard assertion is required; the decision logic is shared and is a
    faithful generalization of loop.py:693-699.

    Authored ONCE per errand type (the SWE gate is configured once, not per run),
    then fed the mutations/observations the runtime already tracks.
    """

    require_assert: bool = False

    # Which observation channels this verifier will TRUST as evidence. The base
    # accepts everything; world verifiers narrow it to an INDEPENDENT channel so
    # the acting path cannot vouch for itself.
    channels: tuple = ()

    def accepts(self, obs: Observation) -> bool:
        return not self.channels or obs.channel in self.channels

    def arms(self, muts: Sequence[Mutation]) -> bool:
        """Gate is armed iff a state change happened (did_edit at loop.py:693)."""
        return bool(muts)

    def verdict(self, muts: Sequence[Mutation], obs: Iterable[Observation]) -> Verdict:
        """Post-condition decision — the direct generalization of the finish
        interception at loop.py:693-699.

        verified  iff  armed AND fresh evidence exists AND newest passes AND
                       (asserted OR not require_assert)
        """
        muts = list(muts)
        if not self.arms(muts):
            return Verdict(NOT_ARMED, "no state change occurred; nothing to verify")

        last_change = max(m.at for m in muts)
        # freshness: evidence must post-date the last change (loop.py:695). Stale
        # evidence from before the change is the "phantom failure" hole the
        # comment at loop.py:52-54 describes, inverted for success.
        fresh = [o for o in obs if self.accepts(o) and o.at >= last_change]
        if not fresh:
            return Verdict(INCONCLUSIVE,
                           "no fresh evidence on an accepted channel after the last change")

        newest = max(fresh, key=lambda o: o.at)
        if not newest.ok:
            return Verdict(FAILED,
                           "post-change observation refutes the outcome on channel "
                           f"{newest.channel!r}", (newest,))
        if self.require_assert and not newest.asserted:
            # a print-only repro (no `assert`) is NOT verification — the
            # wrong-output-doesn't-raise hole closed at loop.py:202-206.
            return Verdict(INCONCLUSIVE,
                           "observation ran but executed no hard assertion", (newest,))
        return Verdict(VERIFIED, f"confirmed via {newest.channel!r}", (newest,))

    # ── the reversibility split (piece 6) ──────────────────────────────────
    def repairable(self, verdict: Verdict, muts: Sequence[Mutation]) -> bool:
        """May the runtime spend another bounded repair round on this?

        Code case: any non-verified verdict is repairable (edit again, re-run) —
        this is loop.py's verify_rounds < verify_max loop. World case: once an
        IRREVERSIBLE mutation has happened, a FAILED post-check must NOT trigger a
        blind retry (that is how you double-send); it becomes failed + optional
        compensation. INCONCLUSIVE (we could not observe) is still repairable —
        re-observe, don't re-act.
        """
        if verdict.status in (VERIFIED, NOT_ARMED):
            return False
        if verdict.status == FAILED and any(not m.reversible for m in muts):
            return False
        return True

    # ── pre-condition gate (the "repro must FAIL on broken code first" half) ─
    def precheck(self, prepared, mandate) -> Verdict:
        """Assert the PREPARED state is correct BEFORE an irreversible action —
        the world's cheap, reversible safety half (form values == mandate, cart
        total <= budget, target does not already exist). Default: nothing to
        check. World verifiers override. Failing here costs nothing because the
        irreversible action has not fired yet.
        """
        return Verdict(VERIFIED, "no precondition declared")


class GoalVerifier:
    """Independent end-to-end Mission outcome verifier.

    Action verifiers answer "did this one primitive do what it claimed?".  A
    goal verifier answers the wider question "is the user's whole goal now
    satisfied?".  The base implementation deliberately refuses to infer that
    from a model's ``done`` token; deployments inject a domain verifier that
    re-observes the world and returns a :class:`Verdict`.
    """

    def verify(self, goal: str, case: dict, events=(), steps=()) -> Verdict:
        return Verdict(INCONCLUSIVE,
                       "no independent mission-level goal verifier configured")


class CallableGoalVerifier(GoalVerifier):
    """Small adapter for services/tests that already expose a verification callable."""

    def __init__(self, fn):
        self.fn = fn

    def verify(self, goal: str, case: dict, events=(), steps=()) -> Verdict:
        result = self.fn(goal, case, events, steps)
        return result if isinstance(result, Verdict) else Verdict(
            INCONCLUSIVE, "goal verifier returned no typed evidence verdict")


# ── the code gate, re-expressed (proves the abstraction is faithful) ────────
class CodeReproVerifier(Verifier):
    """collie's existing assert-verify gate as a Verifier. Channel = the process
    exit code of a post-edit reproduction. Behavior is identical to the inline
    loop.py:693-699 decision; test_verifier.py pins the equivalence."""

    channels = ("exit-code",)

    def __init__(self, require_assert: bool = False):
        self.require_assert = require_assert


# ── a world gate, to show the generalization (the insurance, not the fix) ───
class ListingVerifier(Verifier):
    """Publish-a-marketplace-listing done-check.

    Differential + independent channel: the ONLY evidence it trusts is a
    LOGGED-OUT re-fetch of the listing URL (channel 'logged-out-fetch') — never
    the publish page's own success toast, which came back through the acting
    path. require_assert forces that re-fetch to assert title+price are present,
    so an optimistic 200 with an empty body does not pass. The mutation is
    irreversible, so a FAILED post-check compensates (unpublish) instead of
    re-publishing (which would duplicate the listing).
    """

    channels = ("logged-out-fetch",)
    require_assert = True

    def precheck(self, prepared, mandate) -> Verdict:
        price = prepared.get("price")
        cap = mandate.get("price_floor")
        if cap is not None and price is not None and price < cap:
            return Verdict(FAILED,
                           f"prepared price {price} is below the mandate floor {cap}")
        if prepared.get("already_live"):
            # differential front half: it already exists -> idempotent no-op,
            # do not publish again (this is where dedup lives).
            return Verdict(FAILED, "listing already live — publishing would duplicate")
        return Verdict(VERIFIED, "prepared listing satisfies the mandate")
