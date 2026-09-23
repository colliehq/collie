"""What `make_provider(..., subscription_only=True)` is allowed to hand back.

`subscription_only` is a BILLING constraint: the caller is saying "use the Claude/ChatGPT
plan I am logged into, and do not spend metered API credit". The Claude routes each enforce
it (login-store-only credential, stripped child env, request authority), but the factory
used to accept the flag and then build a provider that had never heard of it — `anthropic`
(metered API key), the OpenAI-compatible presets, `ollama`, `mock` and any plugin. The
constraint was silently discarded exactly where it mattered most.

These cases pin the factory's own behaviour: an explicit True is refused for every route
that cannot honour it, BEFORE that route's constructor or plugin discovery runs, and absent
or False changes nothing at all. Nothing here contacts a provider.
"""
import types

import pytest

from harness import providers
from harness.providers import make_provider


def _boom(label):
    def constructor(*args, **kwargs):
        raise AssertionError("%s must not be constructed under subscription_only" % label)
    return constructor


# --------------------------------------------------------------------------- #
#  Refused: the routes that cannot honour the constraint
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name, attr", [
    ("anthropic", "AnthropicProvider"),          # metered Anthropic API key
    ("openai", "OpenAICompatProvider"),          # metered OPENAI_API_KEY
    ("gemini", "OpenAICompatProvider"),          # metered GEMINI_API_KEY
    ("deepseek", "OpenAICompatProvider"),
    ("ollama", "OllamaProvider"),                # local: real, but not a subscription
    ("mock", "MockProvider"),                    # local: real, but not a subscription
])
def test_a_route_that_cannot_honor_the_constraint_is_refused_before_it_is_built(
        name, attr, monkeypatch):
    """The refusal happens at the factory; the provider object is never constructed."""
    monkeypatch.setattr(providers, attr, _boom(name))

    with pytest.raises(ValueError) as excinfo:
        make_provider(name, subscription_only=True)

    message = str(excinfo.value)
    assert "subscription_only" in message, message
    assert name in message, message


def test_a_plugin_is_refused_before_it_is_discovered_or_loaded(monkeypatch):
    """Plugins carry no subscription-policy contract, so none may be consulted.

    Discovery itself IMPORTS third-party modules (entry points / COLLIE_PROVIDER_PLUGINS),
    so "refuse after looking" would already have run the plugin's code.
    """
    monkeypatch.setattr(providers, "_plugin_providers",
                        _boom("plugin discovery"))

    with pytest.raises(ValueError, match="subscription_only"):
        make_provider("acme-llm", subscription_only=True)


def test_an_unknown_name_becomes_the_policy_error_rather_than_a_provider_hunt(monkeypatch):
    """Under an explicit True the answer is the same whatever the name turns out to be."""
    monkeypatch.setattr(providers, "_plugin_providers", _boom("plugin discovery"))

    with pytest.raises(ValueError) as excinfo:
        make_provider("no-such-provider", subscription_only=True)

    message = str(excinfo.value)
    assert "no-such-provider" in message, message
    # The refusal names the routes that DO honour the constraint, so the caller can act.
    assert "claude-cli" in message and "codex-oauth" in message, message


# --------------------------------------------------------------------------- #
#  Unchanged: absent and explicit False
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kwargs", [{}, {"subscription_only": False}])
def test_absent_and_explicit_false_still_build_the_routes_they_always_built(
        kwargs, monkeypatch):
    """Backward compatibility is the other half of the fix: False is not an opt-in."""
    assert isinstance(make_provider("mock", **kwargs), providers.MockProvider)
    assert isinstance(make_provider("ollama", **kwargs), providers.OllamaProvider)

    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key-nothing-is-sent")
    openai = make_provider("openai", **kwargs)
    assert isinstance(openai, providers.OpenAICompatProvider)
    assert getattr(openai, "subscription_only", False) is False


def test_without_the_constraint_plugins_are_still_consulted_and_still_diagnosed(monkeypatch):
    """An unattended False must keep the existing unknown-provider diagnostics verbatim."""
    consulted = []

    def plugins():
        consulted.append(True)
        return {}, ["acme_plugin: No module named 'acme_plugin'"]

    monkeypatch.setattr(providers, "_plugin_providers", plugins)

    with pytest.raises(ValueError) as excinfo:
        make_provider("no-such-provider", subscription_only=False)

    message = str(excinfo.value)
    assert consulted, "plugin discovery still runs when no constraint was asked for"
    assert "unknown provider: no-such-provider" in message, message
    assert "plugin load errors" in message, message


def test_a_plugin_route_is_still_built_when_no_constraint_was_asked_for(monkeypatch):
    """The refusal is scoped to the explicit True; plugins otherwise work as before."""
    built = {}

    def factory(model):
        built["model"] = model
        return providers.MockProvider()

    monkeypatch.setattr(providers, "_plugin_providers",
                        lambda: ({"acme-llm": factory}, []))

    assert isinstance(make_provider("acme-llm", "acme-1"), providers.MockProvider)
    assert built["model"] == "acme-1"


# --------------------------------------------------------------------------- #
#  Allowed: the subscription routes
# --------------------------------------------------------------------------- #

def test_the_claude_routes_still_receive_the_flag_they_enforce(monkeypatch):
    """Claude CLI takes the flag for real; the other two are checked at the call site."""
    provider = make_provider("claude-cli", "opus", subscription_only=True)
    assert provider.subscription_only is True

    seen = {}
    monkeypatch.setattr(providers, "AnthropicOAuthProvider",
                        lambda **kw: seen.setdefault("oauth", kw))
    make_provider("anthropic-oauth", "claude-opus-4-8", subscription_only=True)
    assert seen["oauth"]["subscription_only"] is True

    sdk = pytest.importorskip("harness.claude_agent_sdk")
    monkeypatch.setattr(sdk, "ClaudeAgentSdkProvider",
                        lambda **kw: seen.setdefault("sdk", kw))
    make_provider("claude-agent-sdk", "claude-opus-4-8", subscription_only=True)
    assert seen["sdk"]["subscription_only"] is True


@pytest.mark.parametrize("name", ["codex-oauth", "codex-sub", "codex"])
def test_the_codex_aliases_are_admitted_because_their_source_has_no_api_key_route(
        name, monkeypatch):
    """Admitted on evidence from `harness.codex_oauth`, not on the alias name.

    The provider takes no `subscription_only` parameter, so nothing is forwarded: it is
    allowed because the ONE route it has is already the ChatGPT-plan route.
    """
    codex_oauth = pytest.importorskip("harness.codex_oauth")
    seen = {}
    def construct(_self, **kw):
        seen["codex"] = kw
    monkeypatch.setattr(codex_oauth.CodexOAuthProvider, "__init__", construct)

    make_provider(name, "gpt-5.6-terra", subscription_only=True)

    assert seen["codex"]["model"] == "gpt-5.6-terra"
    assert "subscription_only" not in seen["codex"], (
        "the constructor has no such parameter; admitting the alias must not invent one")


def test_an_overridden_codex_endpoint_cannot_claim_the_subscription_route(monkeypatch):
    from harness import codex_oauth
    monkeypatch.setattr(codex_oauth.CodexOAuthProvider, "URL",
                        "https://unreviewed.example/responses")
    monkeypatch.setattr(codex_oauth.CodexOAuthProvider, "__init__", _boom("Codex endpoint"))
    with pytest.raises(ValueError, match="subscription_only"):
        make_provider("codex", subscription_only=True)


def test_codex_endpoint_overrides_remain_available_without_the_constraint(monkeypatch):
    from harness import codex_oauth
    monkeypatch.setattr(codex_oauth.CodexOAuthProvider, "URL",
                        "https://custom.example/responses")
    monkeypatch.setattr(codex_oauth.CodexOAuthProvider, "__init__", lambda _self, **kw: None)
    assert make_provider("codex", subscription_only=False).URL == "https://custom.example/responses"


def test_the_codex_source_is_a_chatgpt_plan_route_with_no_api_key_credential():
    """The evidence behind the decision above, read from the module itself."""
    codex_oauth = pytest.importorskip("harness.codex_oauth")

    assert codex_oauth.BASE_URL.startswith("https://chatgpt.com/backend-api/codex")
    assert codex_oauth.CodexOAuthProvider.URL.endswith("/responses")

    headers = codex_oauth.CodexOAuthProvider._headers(
        types.SimpleNamespace(_session_id="s"), "oauth-access-token", "acct-1")
    assert headers["authorization"] == "Bearer oauth-access-token"
    assert headers["ChatGPT-Account-Id"] == "acct-1"
    assert not [k for k in headers if "api-key" in k.lower()]

    # `codex login` may leave an OPENAI_API_KEY in auth.json; this route never adopts it.
    access, account, _claims = codex_oauth._token_and_account(
        {"OPENAI_API_KEY": "sk-metered", "tokens": {}})
    assert access == "" and account == ""
