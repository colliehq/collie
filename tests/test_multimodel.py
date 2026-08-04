"""Backends fail early and by name, and the critic can be a genuinely different model."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import catalog, providers, swe


def _stub_credentials(monkeypatch, blob):
    """Stub the credential SOURCE, not the filesystem: on macOS the same blob lives in the login
    Keychain and no file is read, so a HOME-based fixture would test nothing there (and could pick
    up the developer's real login)."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(providers, "claude_credentials", lambda: blob)


def _oauth(expires_at=None):
    inner = {"accessToken": "sk-ant-oat-test"}
    if expires_at is not None:
        inner["expiresAt"] = expires_at
    return {"claudeAiOauth": inner}


def test_expired_subscription_token_is_named_not_a_bare_401(monkeypatch):
    import time as _t
    _stub_credentials(monkeypatch, _oauth(int((_t.time() - 3600) * 1000)))   # expired an hour ago

    assert providers.claude_oauth_expired(), "an hour-old expiry must read as expired"
    # A THIRD state: the token is present, so "not-logged-in" would be the wrong answer.
    assert catalog.probe_auth("anthropic-oauth") == "expired"
    problem = catalog.auth_problem("anthropic-oauth")
    assert "expired" in problem.lower() and "claude" in problem.lower(), problem


def test_a_live_token_is_not_reported_expired(monkeypatch):
    import time as _t
    _stub_credentials(monkeypatch, _oauth(int((_t.time() + 86400) * 1000)))
    assert not providers.claude_oauth_expired()
    assert catalog.probe_auth("anthropic-oauth") == "ok"


def test_missing_expiry_never_blocks_a_working_login(monkeypatch):
    """An older credential blob has no expiresAt. Absence is not evidence of expiry."""
    _stub_credentials(monkeypatch, _oauth())
    assert not providers.claude_oauth_expired()

    # An env-supplied token carries no expiry either, and must not be second-guessed.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-env")
    assert not providers.claude_oauth_expired()

    # Nor may a malformed value be read as "long expired".
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    _stub_credentials(monkeypatch, {"claudeAiOauth": {"accessToken": "t", "expiresAt": "soon"}})
    assert not providers.claude_oauth_expired()


def test_preflight_names_every_unusable_member_once(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", tempfile.mkdtemp())        # no auth.json there
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    problems = catalog.preflight(["mock", ("openai", "gpt-4o-mini"), "codex-oauth", "openai"])
    joined = " | ".join(problems)
    assert "mock" not in joined, "a usable member must not be reported"
    assert sum("openai:" in p for p in problems) == 1, "a repeated provider reports once: %s" % joined
    assert "OPENAI_API_KEY" in joined, joined
    assert "codex login" in joined, joined


def test_critic_backend_does_not_carry_a_model_across_providers(monkeypatch):
    monkeypatch.delenv("COLLIE_CRITIC_PROVIDER", raising=False)
    monkeypatch.delenv("COLLIE_CRITIC_MODEL", raising=False)
    assert swe.critic_backend("deepseek", "deepseek-chat") == ("deepseek", "deepseek-chat")

    # Switching provider alone must fall to THAT provider's default, not send deepseek-chat
    # to Anthropic.
    monkeypatch.setenv("COLLIE_CRITIC_PROVIDER", "anthropic-oauth")
    assert swe.critic_backend("deepseek", "deepseek-chat") == ("anthropic-oauth", None)

    # Naming both is taken literally.
    monkeypatch.setenv("COLLIE_CRITIC_MODEL", "claude-opus-5")
    assert swe.critic_backend("deepseek", "deepseek-chat") == ("anthropic-oauth", "claude-opus-5")

    # Naming only a model keeps the author's provider — a sibling model of the same backend.
    monkeypatch.delenv("COLLIE_CRITIC_PROVIDER")
    assert swe.critic_backend("deepseek", "deepseek-chat") == ("deepseek", "claude-opus-5")


def test_an_unbuildable_critic_backend_raises_instead_of_silently_reusing_the_author(monkeypatch):
    """Falling back would look identical to a working cross-model critic. That is the one
    outcome a measurement must never produce."""
    monkeypatch.setenv("COLLIE_CRITIC_PROVIDER", "no-such-provider")
    monkeypatch.delenv("COLLIE_CRITIC_MODEL", raising=False)
    try:
        swe._critic_provider("deepseek", "deepseek-chat")
    except RuntimeError as exc:
        assert "no-such-provider" in str(exc) and "never happened" in str(exc)
    else:
        raise AssertionError("a broken critic backend must not fall back silently")


def test_critic_uses_the_second_model_when_one_is_set():
    from harness.loop import Harness

    class Recording:
        def __init__(self, name, text):
            self.name, self.text_out, self.calls = name, text, 0

        def complete(self, system, messages, tool_schemas, on_text=None):
            self.calls += 1
            return providers.Completion(text=self.text_out,
                                        usage=providers.Usage(input_tokens=1, output_tokens=1),
                                        stop_reason="end_turn")

    author = Recording("author", "CORRECT")
    reviewer = Recording("reviewer", "CONCERN: the sibling call site is untouched")
    h = Harness.__new__(Harness)                 # only _run_critic is under test
    h.provider = author
    h.critic_provider = reviewer
    ok, objection = h._run_critic("an issue", "a diff")
    assert (author.calls, reviewer.calls) == (0, 1), "the review must not run on the author's model"
    assert ok is False and "sibling call site" in objection

    h.critic_provider = None                     # unset -> author's model, as before
    ok, _ = h._run_critic("an issue", "a diff")
    assert author.calls == 1 and ok is True
