"""Routing evidence: what may escalate a run, and what may never impose Required.

Two regressions are pinned here.  (1) Required verification is a hard gate that
fails an otherwise successful edit without an executed post-edit assertion, so it
may only ever come from a choice — never from words like "auth" or "migration"
in the task.  (2) Failure escalation is typed and transient: it comes from
structured run receipts, escalates on the latest real failure, and is cleared by
the next clean receipt.  Assistant prose is not evidence in either direction.
"""
from types import SimpleNamespace

import pytest


def _decide(text, **kw):
    from harness.router import resolve_run_decision

    kw.setdefault("route_kind", "code")
    return resolve_run_decision(text, "codex-oauth", **kw)


def _evidence(**over):
    """A verification-evidence blob shaped like harness.verification writes one."""
    row = {"command": "python -m pytest -q", "exit_code": 0, "command_passed": True,
           "passed": True, "executed": True, "freshness": "fresh",
           "ran_after_last_edit": True, "source": "test"}
    row.update(over)
    return row


def _settings_auto(monkeypatch):
    from harness import settings

    monkeypatch.setattr(
        settings, "get",
        lambda key, default=None: {
            "PROVIDER": "codex-oauth", "MODEL": "", "REASONING_EFFORT": "auto",
        }.get(key, default),
    )


# --------------------------------------------------------------------------- #
# Required verification is a choice, never a keyword

@pytest.mark.parametrize("task", [
    "Fix the auth token refresh bug in login.py",
    "Fix the security regression in the permission check",
    "修复这个权限校验的认证缺陷",
])
def test_ordinary_hard_sounding_fix_stays_auto_verification(task):
    decision = _decide(task)

    assert decision.verification == "auto"
    assert decision.sources["verification"] == "task-policy"
    assert any("Required is only ever an explicit choice" in r for r in decision.reasons)
    # Risk keywords still buy capability — they just cannot install a gate.
    assert decision.model == "gpt-5.6-sol" and decision.complexity == "hard"


def test_explicit_required_is_preserved_and_explicit_auto_is_not_overridden():
    chosen = _decide("Fix the security race condition", verification="required",
                     explicit_axes=["verification"])
    declined = _decide("Fix the security race condition", verification="auto",
                       explicit_axes=["verification"])

    assert chosen.verification == "required" and chosen.sources["verification"] == "user"
    assert declined.verification == "auto" and declined.sources["verification"] == "user"


def test_caller_requested_required_survives_without_naming_the_axis():
    """No default anywhere is "required", so asking for it is always deliberate.

    The benchmark protocol and legacy web modes request it positionally; a
    silent downgrade there would quietly drop executed-verification runs.
    """
    decision = _decide("Add a --json flag", verification="required")

    assert decision.verification == "required"
    assert decision.sources["verification"] == "caller-request"


def test_explicit_quality_choice_survives_a_failure_escalation():
    decision = _decide("Fix the bug", quality="quick", explicit_axes=["quality"],
                       receipts=[{"error": "provider timeout"}])

    assert decision.quality == "quick" and decision.sources["quality"] == "user"
    assert decision.effort == "low"
    assert decision.complexity == "hard"        # capability still escalates
    assert decision.verification == "auto"      # a failure never installs the gate


# --------------------------------------------------------------------------- #
# Failure escalation comes from receipts, and only from the latest one

def test_assistant_prose_about_a_resolved_error_does_not_escalate():
    history = [
        {"role": "user", "content": "why is this failing?"},
        {"role": "assistant", "content":
            "The error was a null deref; the test failed before, and the fix passes now."},
        {"role": "assistant", "content": "No errors remain — verification is green."},
    ]

    decision = _decide("Fix the bug", history=history)

    assert decision.complexity == "standard"
    assert decision.model == "gpt-5.6-terra"
    assert not any("escalated" in reason for reason in decision.reasons)


def test_latest_real_failure_escalates_once_until_a_clean_receipt():
    failed = {"decision": {"model": "gpt-5.6-terra"}, "verified": False,
              "error": "provider timeout after 3 retries"}
    clean = {"decision": {"model": "gpt-5.6-sol"}, "verified": True, "error": ""}

    escalated = _decide("Fix the bug", receipts=[clean, failed])
    cleared = _decide("Fix the bug", receipts=[clean, failed, clean])

    assert escalated.complexity == "hard" and escalated.model == "gpt-5.6-sol"
    assert any("escalated to hard after the last run receipt failed" in reason
               for reason in escalated.reasons)
    assert cleared.complexity == "standard" and cleared.model == "gpt-5.6-terra"
    assert not any("escalated" in reason for reason in cleared.reasons)


def test_a_stale_failure_behind_a_clean_run_does_not_keep_escalating():
    from harness.router import failure_signal

    receipts = [{"error": "boom"}] + [{"verified": True, "error": ""}] * 3

    assert failure_signal(receipts).failed is False


def test_an_executed_failing_check_escalates_but_a_missing_check_does_not():
    from harness.router import failure_signal

    ran_and_failed = failure_signal([{"error": "", "verification_evidence": _evidence(
        exit_code=1, command_passed=False, passed=False)}])
    never_ran = failure_signal([{"error": "", "verified": False,
                                 "verification_evidence": _evidence(
                                     command="", exit_code=None, command_passed=False,
                                     passed=False, executed=False, freshness="not_run",
                                     ran_after_last_edit=False)}])

    assert ran_and_failed.failed and ran_and_failed.source == "receipt"
    assert "did not pass" in ran_and_failed.reason
    assert never_ran.failed is False, "an absent check is not a failed check"


def test_stale_evidence_from_an_executed_check_still_counts_as_a_failure():
    from harness.router import failure_signal

    signal = failure_signal([{"error": "", "verification_evidence": _evidence(
        passed=False, ran_after_last_edit=False, freshness="changed_during_check")}])

    assert signal.failed is True


def test_a_canceled_run_is_not_a_failed_run():
    from harness.router import failure_signal

    assert failure_signal([{"error": "canceled by user", "canceled": True}]).failed is False
    assert failure_signal([{"error": "", "verification_evidence": _evidence(
        passed=False, cancelled=True)}]).failed is False


def test_receipts_without_an_outcome_never_invent_a_failure():
    from harness.router import failure_signal

    # A decision-only row (routing note, run still in flight) is not a verdict.
    assert failure_signal([{"decision": {"model": "gpt-5.6-terra"}}]).failed is False
    assert failure_signal([]).failed is False
    assert failure_signal(None).failed is False


@pytest.mark.parametrize("receipts", [
    [None], ["oops"], [42], [{"error": {"message": "boom"}}],
    [{"verification_evidence": "boom"}], [{"verification_evidence": {"passed": "no"}}],
    [{"error": "", "verification_evidence": {}}],
    {"error": "boom"}, 42, object(),
])
def test_malformed_receipts_neither_crash_nor_escalate(receipts):
    from harness.router import failure_signal

    assert failure_signal(receipts).failed is False
    assert _decide("Fix the bug", receipts=receipts).complexity == "standard"


def test_a_malformed_row_does_not_hide_the_real_failure_behind_it():
    from harness.router import failure_signal

    assert failure_signal([{"error": "provider timeout"}, None]).failed is True


def test_failure_signal_is_typed_and_passthrough():
    from harness.router import FailureSignal, failure_signal

    signal = failure_signal([{"error": "provider timeout"}])

    assert isinstance(signal, FailureSignal) and bool(signal) is True
    assert failure_signal(signal) is signal
    assert _decide("Fix the bug", receipts=signal).complexity == "hard"


# --------------------------------------------------------------------------- #
# Every surface reads the same evidence

def test_terminal_turn_decision_reads_receipts_and_ignores_prose(monkeypatch):
    from harness.cli import resolve_turn_decision

    _settings_auto(monkeypatch)
    prose = [{"role": "assistant", "content": "error: verification failed earlier, now fixed"}]

    calm = resolve_turn_decision("Fix the bug", "codex-oauth", route_kind="code",
                                 history=prose)
    failed = resolve_turn_decision("Fix the bug", "codex-oauth", route_kind="code",
                                   history=prose, receipts=[{"error": "provider timeout"}])
    recovered = resolve_turn_decision(
        "Fix the bug", "codex-oauth", route_kind="code", history=prose,
        receipts=[{"error": "provider timeout"}, {"verified": True, "error": ""}])

    assert calm.model == "gpt-5.6-terra" and calm.complexity == "standard"
    assert failed.model == "gpt-5.6-sol" and failed.complexity == "hard"
    assert recovered.model == "gpt-5.6-terra"


def test_terminal_turn_decision_does_not_invent_a_failure_from_a_missing_check(monkeypatch):
    """A repo with no detectable check produced not_run evidence every turn.

    That used to be rewritten into "error: verification failed" prose, so an
    ordinary chat thread drifted onto the most expensive model and stayed there.
    """
    from harness.cli import resolve_turn_decision

    _settings_auto(monkeypatch)
    receipts = [{"decision": {"model": "gpt-5.6-luna"}, "verified": False, "error": "",
                 "verification_evidence": _evidence(
                     command="", exit_code=None, command_passed=False, passed=False,
                     executed=False, freshness="not_run", ran_after_last_edit=False)}] * 3

    decision = resolve_turn_decision("Fix the bug", "codex-oauth", route_kind="code",
                                     receipts=receipts)

    assert decision.complexity == "standard" and decision.model == "gpt-5.6-terra"
    assert decision.verification == "auto"


def test_web_stream_routes_with_the_threads_stored_receipts(monkeypatch, tmp_path):
    from harness import router, sessions, settings, webapp

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(webapp, "_provider", lambda: "codex-oauth")
    sessions.save("web-receipts", [{"role": "user", "content": "fix it"}])
    sessions.append_run_receipt("web-receipts", {"verified": False,
                                                "error": "provider timeout"})

    seen = {}

    def _capture(text, **kw):
        seen.update(kw)
        raise ValueError("stop before execution")

    monkeypatch.setattr(router, "resolve_run_decision", _capture)
    events = []
    handler = object.__new__(webapp.Handler)
    handler._sse_open = lambda: None
    handler._sse = lambda kind, data: events.append((kind, data))

    webapp.Handler._serve_stream(
        handler, {"q": ["Fix the bug"], "session": ["web-receipts"]})

    assert events[-1] == ("done", {"session": "web-receipts", "answer": "",
                                   "error": "stop before execution"})
    assert router.failure_signal(seen["receipts"]).failed is True


def test_acp_and_repl_receipts_are_shaped_for_the_router():
    """turn_decision_receipt output must be readable as evidence by the router."""
    from harness.cli import turn_decision_receipt
    from harness.router import RunDecision, failure_signal

    decision = RunDecision(
        provider="codex-oauth", model="gpt-5.6-terra", effort="medium", speed="standard",
        billing_multiplier=1.0, intent="build", quality="balanced", verification="auto",
        workspace="current", strategy="single", route_kind="code", complexity="standard")
    ok = turn_decision_receipt(decision, SimpleNamespace(
        model="gpt-5.6-terra", verified=True, verification_evidence=None, error=""))
    bad = turn_decision_receipt(decision, SimpleNamespace(
        model="gpt-5.6-terra", verified=False, verification_evidence=None,
        error="provider timeout"))

    assert failure_signal([ok]).failed is False
    assert failure_signal([bad]).failed is True
    assert failure_signal([bad, ok]).failed is False
