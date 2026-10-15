"""Extraction must preserve explicit authorization and durable effect fences."""
from harness.audit import AuditLog
from harness.authority import RequestAuthority


def test_negated_or_conditional_send_does_not_mint_action_grant():
    for request in (
        "Draft only; do not send email to synthetic@example.invalid",
        "只写草稿，不要发送给 synthetic@example.invalid",
        "If approved, send to synthetic@example.invalid",
        'Explain the instruction "send email" without doing it',
        "Explain the example 'send email'",
        "Send it after I approve",
        "Send it when I confirm",
        "等我确认后再发送",
        "Do not send to alice@example.invalid; send to bob@example.invalid",
    ):
        assert "send" not in RequestAuthority.compile(request).actions


def test_affirmative_send_retains_explicit_request():
    request = RequestAuthority.compile("Send this email to synthetic@example.invalid")
    assert "send" in request.actions
    assert request.recipients == ("synthetic@example.invalid",)


def test_audit_unavailable_is_not_durable_ack(tmp_path):
    ledger = AuditLog(str(tmp_path / "audit.db"))
    assert ledger.record(tool="synthetic", stage="asked", reason="fixture") is True
    assert len(ledger.list()) == 1
    ledger.db.close()
    assert ledger.record(tool="synthetic", stage="auto", reason="fixture") is False


def test_latest_steering_revokes_and_explicitly_restores_request(tmp_path):
    from harness.gate import Gate
    gate = Gate(tmp_path)
    gate.begin_request("Send to old@example.invalid")
    gate.extend_request("Do not send")
    assert "send" in gate.authority_context.request.withheld_actions
    assert "send" not in gate.authority_context.request.actions
    gate.extend_request("Send to new@example.invalid")
    assert "send" not in gate.authority_context.request.withheld_actions
    assert gate.authority_context.request.recipients == ("new@example.invalid",)


def test_current_withholding_precedes_stored_grant(tmp_path):
    from harness.authority import (ActionIntent, AuthorityContext, AuthorityDecision,
                                   AuthorityEngine, AuthorityStore, Effect, GrantScope)
    store = AuthorityStore(str(tmp_path / "authority.db"))
    store.add(scope=GrantScope.PROJECT, action="send", project="fixture")
    result = AuthorityEngine(store).decide(
        ActionIntent("send", Effect.COMMIT),
        AuthorityContext(request=RequestAuthority.compile("Do not send"), project="fixture"))
    assert result.decision is AuthorityDecision.ASK
    assert not result.grant_id


def test_current_withholding_precedes_session_rule_and_auto_mode(tmp_path):
    from harness.gate import Gate, Mode
    args = {"text": "Send", "url": "https://example.invalid"}
    gate = Gate(tmp_path, origin_lookup=lambda: "https://example.invalid")
    gate.begin_request("Send the email")
    target = gate.evaluate("browser_click", args).target
    assert target
    gate.session_rules.add(("browser_click", target))
    gate.extend_request("Do not send")
    for mode in (Mode.PROJECT, Mode.AUTO):
        gate.mode = mode
        decision = gate.evaluate("browser_click", args)
        assert decision.allowed is False
        assert decision.needs_user is False
        assert decision.action == "send"


def test_required_audit_rejects_absence_and_non_acknowledgement():
    from types import SimpleNamespace
    from harness.loop import Harness
    harness = Harness.__new__(Harness)
    harness.audit_required = True
    harness.audit = None
    tc = SimpleNamespace(name="synthetic", args={})
    decision = SimpleNamespace(risk="external", target="", reason="fixture", rule="")
    assert harness._audit(tc, decision, stage="auto", outcome="allow") is False
    harness.project = "fixture"
    harness.cwd = "."
    harness.audit = SimpleNamespace(record=lambda **kwargs: None)
    assert harness._audit(tc, decision, stage="auto", outcome="allow") is False
    harness.audit = SimpleNamespace(record=lambda **kwargs: True)
    assert harness._audit(tc, decision, stage="auto", outcome="allow") is True
