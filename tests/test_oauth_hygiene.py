"""Regression tests for removed OAuth impersonation and proxy paths."""

import json
import subprocess

import pytest


class _AnthropicResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({
            "content": [{"type": "text", "text": "ok"}],
            "usage": {},
            "stop_reason": "end_turn",
        }).encode()


def test_experimental_raw_oauth_never_uses_claude_code_identity_or_proxy(monkeypatch):
    from harness.providers import AnthropicOAuthProvider

    seen = {}
    provider = AnthropicOAuthProvider.__new__(AnthropicOAuthProvider)
    provider.name = "anthropic-oauth"
    provider.model = "claude-opus-4-8"
    provider.max_tokens = 128
    provider.effort = "default"
    provider.speed = "standard"
    provider.API = "https://legacy-proxy.invalid/v1/messages"
    provider.subscription_only = False

    monkeypatch.setattr(
        "harness.providers._read_oauth_token", lambda **_kwargs: "private-token")
    monkeypatch.setattr(
        "harness.providers.claude_oauth_expired", lambda **_kwargs: False)

    def urlopen(request, timeout):
        seen.update(request=request, timeout=timeout)
        return _AnthropicResponse()

    monkeypatch.setattr("harness.providers.urllib.request.urlopen", urlopen)

    completion = provider.complete(
        "COLLIE SYSTEM", [{"role": "user", "content": "work"}], [])

    request = seen["request"]
    body = json.loads(request.data)
    assert completion.text == "ok"
    assert request.full_url == provider.OFFICIAL_API
    assert body["system"][0]["text"] == "COLLIE SYSTEM"
    assert "Claude Code" not in json.dumps(body)
    assert request.headers["Anthropic-beta"] == "oauth-2025-04-20"
    assert request.headers["User-agent"] == "collie/anthropic-oauth-experimental"
    assert "X-app" not in request.headers
    assert "claude-code" not in json.dumps(dict(request.headers)).lower()


def test_raw_oauth_model_discovery_uses_collie_identity(monkeypatch):
    from harness import catalog, providers

    seen = {}
    catalog._disc_cache.clear()
    monkeypatch.setattr(providers, "_read_oauth_token", lambda: "private-token")

    def http_json(url, headers, timeout=3.0):
        seen.update(url=url, headers=headers, timeout=timeout)
        return {"data": [{"id": "claude-test"}]}

    monkeypatch.setattr(catalog, "_http_json", http_json)

    assert catalog.discover("anthropic-oauth") == ["claude-test"]
    assert seen["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert seen["headers"]["user-agent"] == "collie/anthropic-oauth-experimental"
    assert "x-app" not in seen["headers"]
    assert "claude-code" not in json.dumps(seen["headers"]).lower()


def test_pi_adapter_uses_pi_login_without_proxy_extension():
    from harness.adapters import PiAdapter

    adapter = PiAdapter()
    cmd = adapter.build_cmd("do the work", "claude-opus-4-8")

    assert cmd == [
        "pi", "-p", "--no-session", "--model", "claude-opus-4-8", "do the work"]
    assert adapter.extra_env == {}
    assert "--provider" not in cmd
    assert "--extension" not in cmd


@pytest.mark.parametrize("provider,model", [
    ("", "claudesub/claude-opus-4-8"),
    ("claudesub", "claude-opus-4-8"),
])
def test_pi_adapter_rejects_removed_claudesub_provider(monkeypatch, provider, model):
    from harness.adapters import PiAdapter

    monkeypatch.setenv("PI_PROVIDER", provider)
    with pytest.raises(ValueError, match="claudesub.*removed"):
        PiAdapter().build_cmd("do the work", model)


def test_swe_pi_uses_documented_provider_without_proxy(monkeypatch, tmp_path):
    from harness import swe

    seen = {}
    monkeypatch.setenv("SWE_PI_PROVIDER", "anthropic")
    monkeypatch.setenv("SWE_PI_MODEL", "claude-opus-4-8")

    def run_cli(cmd, workdir, timeout=0):
        seen.update(cmd=cmd, workdir=workdir, timeout=timeout)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(swe, "_run_cli", run_cli)
    swe.predict_pi(str(tmp_path), "fix it", timeout=17)

    assert seen["cmd"][seen["cmd"].index("--provider") + 1] == "anthropic"
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "claude-opus-4-8"
    assert "--extension" not in seen["cmd"]


def test_swe_pi_fails_closed_for_removed_claudesub(monkeypatch, tmp_path):
    from harness import swe

    monkeypatch.setenv("SWE_PI_PROVIDER", " ClaudeSub ")
    monkeypatch.setattr(
        swe, "_run_cli", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("removed provider must stop before spawning Pi")))

    with pytest.raises(ValueError, match="claudesub.*removed"):
        swe.predict_pi(str(tmp_path), "fix it")


def test_swe_pi_fails_closed_for_claudesub_model_prefix(monkeypatch, tmp_path):
    from harness import swe

    monkeypatch.setenv("SWE_PI_PROVIDER", "anthropic")
    monkeypatch.setenv("SWE_PI_MODEL", "claudesub/claude-opus-4-8")
    monkeypatch.setattr(
        swe, "_run_cli", lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("removed provider must stop before spawning Pi")))

    with pytest.raises(ValueError, match="removed provider.*claudesub"):
        swe.predict_pi(str(tmp_path), "fix it")
