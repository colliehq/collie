"""What the next model turn is told about the last host check.

``run_ownership`` projects the newest ``run_receipts`` entry into one
host-authored message so a resumed conversation knows whether the project's
tests passed.  It derived that one word from ``passed`` alone, and ``passed`` is
the completion-grade verdict: it is false for a check that was killed at its
deadline and for one that exited 0 but could not be bound to the bytes that
exist now.  Both landed in the transcript as::

    The last required check on this conversation FAILED.

which is a sentence no receipt supports.  The next turn then went looking for a
broken test — and the most common way to reach the second case is a *green* run
of a check that writes its own build output into the workspace it grades.

``verification.check_result_state`` already classifies all six outcomes, and the
CLI and Web endings already report from it.  These tests pin the projection to
that same classifier, end to end: a real receipt saved in a real session store,
carried through ``Harness.run``, read back out of what the provider was actually
handed for the next turn.  Two of the six receipts are produced by running real
checks in a real subprocess, so the ambiguous cases come from the product's own
freshness and deadline rules rather than from a hand-written dict.
"""
import pytest

from harness import compaction, run_ownership, sessions, verification

from test_native_inbox import (  # the real native wiring, already fixtured
    store, _events, _harness, _recording_provider, _seen_contents,   # noqa: F401
)
from test_verification_cancel import PYTHON, _spaces
from test_verification_inconclusive_receipt import (_build_output_check,
                                                    _failing_check)
from test_verification_timeout_receipt import CHECK_TIMEOUT_S, _overrunning_check

COMMAND = "python -m pytest -q tests/test_uploader.py"


def _evidence(**fields):
    """A receipt shaped exactly like ``run_verification_command`` writes one."""
    row = {"command": COMMAND, "executed": True, "cancelled": False,
           "timed_out": False, "timeout_s": 900, "command_passed": True,
           "passed": True, "exit_code": 0, "freshness": "fresh",
           "source": "user", "timestamp": "2026-09-12T10:00:00+00:00"}
    row.update(fields)
    return row


def _saved(sid, tmp_path, evidence):
    """A real session with a real receipt behind it, ready to be resumed."""
    sessions.save(sid, [{"role": "user", "content": "fix the uploader"},
                        {"role": "assistant", "content": "fixed it"}],
                  cwd=str(tmp_path))
    assert sessions.append_run_receipt(
        sid, {"stop_reason": "completed", "verified": bool(evidence.get("passed")),
              "verification_evidence": evidence})
    return sid


def _next_turn_sees(tmp_path, sid):
    """Run one turn and return the host projection the provider was handed."""
    h = _harness(tmp_path, sid=sid)
    seen = []
    h.provider = _recording_provider(seen)
    try:
        h.run("t", "now add the docs", history=sessions.load(sid)["messages"])
    finally:
        h.memory.close(); h.recorder.close()
    projected = [c for c in _seen_contents(seen)
                 if "Host verification evidence" in c]
    assert len(projected) == 1, "exactly one host-authored context message"
    return projected[0], _events(h, "verification_context")


# --------------------------------------------------------------------------- #
# all six outcomes, as the next turn reads them
# --------------------------------------------------------------------------- #
_CASES = {
    "passed": (_evidence(), "PASSED"),
    "failed": (_evidence(command_passed=False, passed=False, exit_code=1),
               "FAILED"),
    "timed_out": (_evidence(command_passed=False, passed=False, exit_code=None,
                            timed_out=True, freshness="timed_out"), "TIMED OUT"),
    # Exit zero, and still nothing certified: the tree moved underneath it.
    "inconclusive": (_evidence(passed=False, freshness="changed_during_check"),
                     "INCONCLUSIVE"),
    "canceled": (_evidence(command_passed=False, passed=False, exit_code=None,
                           cancelled=True, freshness="cancelled"), "STOPPED"),
    "not_run": (_evidence(command_passed=False, passed=False, exit_code=None,
                          executed=False, freshness="not_run",
                          skipped_reason="the run was stopped before it started"),
                "NOT RUN"),
}


@pytest.mark.parametrize("outcome", sorted(_CASES))
def test_every_recorded_outcome_reaches_the_next_turn_as_itself(
        store, tmp_path, outcome):
    """Six receipt states, six sentences — not four, with two of them a lie."""
    evidence, expected = _CASES[outcome]
    content, emitted = _next_turn_sees(
        tmp_path, _saved("outcome-" + outcome, tmp_path, evidence))

    assert expected in content, content
    assert COMMAND in content, "the command it ran is the actionable half"
    assert emitted and emitted[0]["outcome"] == outcome, \
        "the surface event carries the same word the model was given"
    # The classifier the CLI and Web endings report from agrees, modulo the
    # durable spelling this projection has always emitted.
    shared = verification.check_result_state(evidence)
    assert outcome == ("canceled" if shared == "cancelled" else shared)


@pytest.mark.parametrize("outcome", ["timed_out", "inconclusive", "canceled",
                                     "not_run"])
def test_an_unfinished_check_is_never_announced_as_a_failure(store, tmp_path,
                                                             outcome):
    """The repair round this fixes started at the word FAILED."""
    evidence, _ = _CASES[outcome]
    content, _emitted = _next_turn_sees(
        tmp_path, _saved("not-failed-" + outcome, tmp_path, evidence))

    assert "FAILED" not in content, content
    assert "conversation FAILED" not in content
    assert ("this is NOT a failing test" in content
            or "proved nothing" in content or "NOT RUN" in content), content
    assert "re-run the command" in content, \
        "the only honest next step for evidence that settled nothing"


def test_a_real_failing_check_is_still_reported_as_a_failure(store, tmp_path):
    """The control: softening a red check would be the opposite defect."""
    workspace, _ = _spaces(tmp_path, "red")
    evidence = verification.run_verification_command(
        _failing_check(workspace), str(workspace), timeout=120)
    assert evidence["command_passed"] is False and evidence["exit_code"] == 1

    content, emitted = _next_turn_sees(
        tmp_path, _saved("real-failure", tmp_path, evidence))
    assert "FAILED" in content and emitted[0]["outcome"] == "failed"
    assert "NOT a failing test" not in content
    assert "exit code: 1" in content


def test_a_real_green_check_that_moved_the_tree_is_not_a_failure(store, tmp_path):
    """The common trigger, run for real: exit 0, build output, nothing certified."""
    workspace, _ = _spaces(tmp_path, "build")
    command = _build_output_check(workspace)
    evidence = verification.run_verification_command(
        command, str(workspace), timeout=120)
    assert evidence["command_passed"] is True and evidence["passed"] is False
    assert evidence["freshness"] == "changed_during_check"

    content, emitted = _next_turn_sees(
        tmp_path, _saved("real-inconclusive", tmp_path, evidence))
    assert emitted[0]["outcome"] == "inconclusive"
    assert "INCONCLUSIVE" in content and "FAILED" not in content
    assert "exited 0" in content, "the exit code is not hidden either"
    # The receipt's own explanation, in the wording the other surfaces use.
    assert "the workspace changed while it was running" in content, content
    assert "PASSED" not in content, "and an uncertified check is still not a pass"


def test_a_real_check_killed_at_its_deadline_reports_the_deadline(store, tmp_path):
    """No exit code at all: "failed (exit None)" was the sentence to kill."""
    workspace, _ = _spaces(tmp_path, "slow")
    evidence = verification.run_verification_command(
        _overrunning_check(workspace), str(workspace), timeout=CHECK_TIMEOUT_S)
    assert evidence["timed_out"] is True and evidence["exit_code"] != 0

    content, emitted = _next_turn_sees(
        tmp_path, _saved("real-timeout", tmp_path, evidence))
    assert emitted[0]["outcome"] == "timed_out"
    assert "TIMED OUT" in content and "FAILED" not in content
    assert "ran out of time" in content and "judged nothing" in content
    assert "%ss" % CHECK_TIMEOUT_S in content, "a deadline is only useful with its size"


# --------------------------------------------------------------------------- #
# precedence between facts that can all be true at once
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("fields,outcome,why", [
    ({"executed": False, "cancelled": True, "command_passed": True,
      "passed": False}, "not_run",
     "a check that never started established nothing, however it ended"),
    ({"cancelled": True, "timed_out": True, "command_passed": True,
      "passed": False}, "canceled",
     "a stop the person asked for outranks the deadline that followed it"),
    ({"timed_out": True, "command_passed": True, "passed": False}, "timed_out",
     "an exit code that raced the kill does not make the check complete"),
    ({"cancelled": True, "passed": True}, "canceled",
     "a stop is never laundered into a pass"),
    ({"command_passed": True, "passed": False}, "inconclusive",
     "exit zero without certification is the ambiguous case, not a failure"),
    ({"command_passed": False, "passed": False, "exit_code": 2}, "failed",
     "and a command that really exited non-zero still failed"),
])
def test_the_projection_orders_the_facts_the_way_the_classifier_does(fields,
                                                                     outcome, why):
    """One authority for precedence, so no surface can disagree about a receipt."""
    evidence = _evidence(**fields)
    assert run_ownership._row(evidence)["outcome"] == outcome, why
    shared = verification.check_result_state(evidence)
    assert outcome == ("canceled" if shared == "cancelled" else shared), why


def test_a_legacy_receipt_without_the_executed_flag_still_claims_nothing():
    """Older receipts predate the flag; absence of evidence is not a verdict."""
    legacy = {"command": COMMAND, "passed": True, "exit_code": 0}
    assert run_ownership._row(legacy)["outcome"] == "not_run"
    legacy_failed = {"command": COMMAND, "passed": False, "exit_code": 1}
    assert run_ownership._row(legacy_failed)["outcome"] == "not_run"


# --------------------------------------------------------------------------- #
# everything the projection already promised, across the new outcomes
# --------------------------------------------------------------------------- #
def test_an_ambiguous_receipt_keeps_the_projections_own_guarantees(store, tmp_path):
    """Host identity, one message per distinct receipt, bounded, still true here."""
    sid = _saved("ambiguous-invariants", tmp_path,
                 _CASES["inconclusive"][0])
    for turn in range(2):
        h = _harness(tmp_path, sid=sid)
        h.provider = _recording_provider([])
        try:
            h.run("t", "turn %d" % turn, history=sessions.load(sid)["messages"])
        finally:
            h.memory.close(); h.recorder.close()

    saved = sessions.load(sid)["messages"]
    context = [m for m in saved if m.get("kind") == run_ownership.CONTEXT_KIND]
    assert len(context) == 1, "one short message per distinct check, not per turn"
    assert context[0]["source"] == run_ownership.CONTEXT_SOURCE == "harness"
    assert not compaction.is_user_message(context[0]), \
        "host evidence is not the person's instruction"
    assert context[0]["verification_digest"] == \
        run_ownership._row(_CASES["inconclusive"][0])["digest"]

    # A different ambiguous receipt is different evidence, and does get carried.
    sessions.append_run_receipt(sid, {
        "stop_reason": "completed", "verified": False,
        "verification_evidence": _CASES["timed_out"][0]})
    h = _harness(tmp_path, sid=sid)
    h.provider = _recording_provider([])
    try:
        h.run("t", "again", history=sessions.load(sid)["messages"])
    finally:
        h.memory.close(); h.recorder.close()
    carried = [m for m in sessions.load(sid)["messages"]
               if m.get("kind") == run_ownership.CONTEXT_KIND]
    assert len(carried) == 2 and "TIMED OUT" in carried[-1]["content"]


def test_the_ambiguous_explanation_is_bounded_and_host_recorded(store, tmp_path):
    """A long command does not push the row past its bounds, and prose is not evidence."""
    long_command = "python -m pytest " + " ".join(
        "tests/test_%d.py" % n for n in range(200))
    row = run_ownership._row(_evidence(command=long_command, passed=False,
                                       freshness="changed_during_check"))
    assert len(row["command"]) == run_ownership.MAX_COMMAND_CHARS
    assert row["command_truncated"] is True
    assert len(row["reason"]) <= 160 and row["reason"]
    assert row["source"] == "user", "the host's own source field, not the model's"
    content = run_ownership.context_message(row)["content"]
    assert "…(truncated)" in content and "INCONCLUSIVE" in content

    # A receipt with no command at all still projects nothing, ambiguous or not.
    assert run_ownership._row(_evidence(command="", passed=False,
                                        freshness="unknown")) is None
