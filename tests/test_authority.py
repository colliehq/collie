import time

from harness.authority import (
    ActionIntent, AuthorityContext, AuthorityDecision, AuthorityEngine, AuthorityStore,
    Effect, GrantScope, RequestAuthority, grant_matches, intent_for,
)


def test_request_is_authority_for_exact_result_not_adjacent_result():
    send = RequestAuthority.compile("Send this email to alice@example.com")
    draft = RequestAuthority.compile("Draft an email to alice@example.com")
    intent = ActionIntent("send", Effect.COMMIT, recipients=("alice@example.com",))
    assert send.allows(intent)
    assert not draft.allows(intent)
    assert not send.allows(ActionIntent("send", Effect.COMMIT,
                                        recipients=("mallory@example.com",)))


def test_chinese_explicit_actions_are_compiled():
    auth = RequestAuthority.compile("把日报发送给 alice@example.com，然后发布项目状态")
    assert {"send", "publish"}.issubset(auth.actions)


def test_effect_policy_is_quiet_for_observe_prepare_and_scoped_act():
    engine = AuthorityEngine()
    ctx = AuthorityContext(request=RequestAuthority.compile("inspect the page"))
    assert engine.decide(ActionIntent("observe", Effect.OBSERVE), ctx).decision is AuthorityDecision.ALLOW_SILENT
    assert engine.decide(ActionIntent("enter_data", Effect.PREPARE), ctx).decision is AuthorityDecision.ALLOW_SILENT
    assert engine.decide(ActionIntent("update_issue", Effect.ACT), ctx).decision is AuthorityDecision.ALLOW_NOTIFY


def test_explicit_commit_does_not_ask_again_but_draft_does():
    engine = AuthorityEngine()
    intent = ActionIntent("send", Effect.COMMIT, recipients=("alice@example.com",))
    yes = AuthorityContext(request=RequestAuthority.compile("Send it to alice@example.com"))
    no = AuthorityContext(request=RequestAuthority.compile("Draft it for alice@example.com"))
    assert engine.decide(intent, yes).decision is AuthorityDecision.ALLOW_NOTIFY
    assert engine.decide(intent, no).decision is AuthorityDecision.ASK


def test_person_bound_verification_remains_needs_person_but_otp_fill_is_not_classified_as_person():
    engine = AuthorityEngine()
    ctx = AuthorityContext(request=RequestAuthority.compile("log in and verify my account"))
    person = ActionIntent("person_verification", Effect.RESTRICTED,
                          reason="passkey requires the person")
    assert engine.decide(person, ctx).decision is AuthorityDecision.NEEDS_PERSON
    otp = intent_for("browser_type", {"label": "Email verification code", "text": "123456"},
                     risk="external", target="https://example.test")
    assert otp.effect is Effect.PREPARE


def test_browser_intents_separate_preparation_commit_and_spending():
    assert intent_for("browser_advance", {"ref": "e1"}, risk="external").effect is Effect.PREPARE
    assert intent_for("browser_type", {"label": "Title", "text": "hello"},
                      risk="external").effect is Effect.PREPARE
    assert intent_for("browser_type", {"text": "hello", "submit": True},
                      risk="external").effect is Effect.COMMIT
    assert intent_for("browser_click", {"text": "Send"}, risk="external").action == "send"
    assert intent_for("browser_click", {"text": "Buy now"},
                      risk="external").effect is Effect.RESTRICTED
    assert intent_for("browser_click", {"ref": "e7"}, risk="external").effect is Effect.COMMIT


def test_review_mode_still_asks_for_external_preparation():
    result = AuthorityEngine().decide(
        ActionIntent("enter_data", Effect.PREPARE), AuthorityContext(mode="review"))
    assert result.decision is AuthorityDecision.ASK


def test_durable_grant_is_bounded_and_revocable(tmp_path):
    store = AuthorityStore(str(tmp_path / "authority.db"))
    grant = store.add(scope=GrantScope.PROJECT, action="publish", project="collie",
                      target="https://example.test", expires_at=int(time.time()) + 60)
    intent = ActionIntent("publish", Effect.COMMIT, target="https://example.test")
    ctx = AuthorityContext(project="collie")
    assert grant_matches(grant, intent, ctx)
    result = AuthorityEngine(store).decide(intent, ctx)
    assert result.decision is AuthorityDecision.ALLOW_NOTIFY
    assert result.grant_id == grant.id
    assert store.revoke(grant.id)
    assert AuthorityEngine(store).decide(intent, ctx).decision is AuthorityDecision.ASK
    store.close()


def test_grant_does_not_expand_recipient_amount_or_project(tmp_path):
    store = AuthorityStore(str(tmp_path / "authority.db"))
    grant = store.add(scope=GrantScope.PROJECT, action="purchase", project="p",
                      recipients=("vendor@example.com",), max_amount=50, currency="USD")
    good = ActionIntent("purchase", Effect.RESTRICTED, recipients=("vendor@example.com",),
                        amount=40, currency="USD")
    assert grant_matches(grant, good, AuthorityContext(project="p"))
    assert not grant_matches(grant, good, AuthorityContext(project="other"))
    assert not grant_matches(grant, ActionIntent(
        "purchase", Effect.RESTRICTED, recipients=("else@example.com",), amount=40,
        currency="USD"), AuthorityContext(project="p"))
    assert not grant_matches(grant, ActionIntent(
        "purchase", Effect.RESTRICTED, recipients=("vendor@example.com",), amount=51,
        currency="USD"), AuthorityContext(project="p"))
    store.close()


def test_mcp_read_hint_only_helps_after_manifest_approval():
    class Tool:
        _annotations = {"readOnlyHint": True}
        _server = "calendar"

    tool = Tool()
    assert intent_for("mcp__calendar__list", {}, risk="external", tool=tool).effect is Effect.COMMIT
    tool._authority_manifest_approved = True
    assert intent_for("mcp__calendar__list", {}, risk="external", tool=tool).effect is Effect.OBSERVE
