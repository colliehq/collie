import datetime as dt
import json
import subprocess

import pytest

from bench.subscription_guard import (
    CODEX_EVIDENCE_MAX_AGE_SECONDS,
    SubscriptionGuardError,
    check_subscription_guard,
)


NOW = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)


class Runner:
    def __init__(self, stdout, *, returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def claude_status(**updates):
    value = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": "max",
    }
    value.update(updates)
    return json.dumps(value)


def codex_evidence(observed_at="2026-08-12T11:55:00Z", **updates):
    value = {
        "credits_remaining": 0,
        "auto_reload": False,
        "observed_at_utc": observed_at,
    }
    value.update(updates)
    return value


@pytest.mark.parametrize(("reported", "expected"), [
    ("Pro", "pro"),
    ("max", "max"),
    ("Max 20x", "max"),
])
def test_claude_requires_first_party_paid_plan_and_returns_redacted_receipt(reported, expected):
    runner = Runner(claude_status(subscriptionType=reported, email="owner@example.test"))

    receipt = check_subscription_guard(
        "claude-code", environ={}, runner=runner, now_utc=NOW)

    assert receipt["verdict"] == "allow"
    assert receipt["auth"] == {
        "status": "authenticated", "method": "claude.ai",
        "api_provider": "firstParty", "plan": expected,
    }
    assert receipt["environment"] == {
        "override_check": "passed",
        "forbidden_environment_name_count": 0,
        "status_child_environment": "allowlist",
    }
    assert runner.calls == [("claude", "auth", "status", "--json")]


@pytest.mark.parametrize(("updates", "reason"), [
    ({"loggedIn": False}, "claude_not_logged_in"),
    ({"authMethod": "apiKey"}, "claude_auth_method_not_claude_ai"),
    ({"apiProvider": "bedrock"}, "claude_api_provider_not_first_party"),
    ({"subscriptionType": "free"}, "claude_plan_not_pro_or_max"),
    ({"subscriptionType": None}, "claude_plan_not_pro_or_max"),
])
def test_claude_fails_closed_for_every_auth_dimension(updates, reason):
    runner = Runner(claude_status(**updates))

    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard("claude", environ={}, runner=runner, now_utc=NOW)

    assert caught.value.reason == reason
    assert caught.value.receipt["verdict"] == "deny"


@pytest.mark.parametrize("extra", [
    {"accessToken": "sk-ant-private"},
    {"auth": {"refresh_token": "private-refresh-token"}},
    {"account": {"credentials": {"value": "private-credential"}}},
])
def test_claude_status_containing_credential_fields_is_rejected_without_echo(extra):
    status = json.loads(claude_status())
    status.update(extra)
    serialized_status = json.dumps(status)

    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard(
            "claude", environ={}, runner=Runner(serialized_status), now_utc=NOW)

    assert caught.value.reason == "auth_status_contains_credential_fields"
    receipt = json.dumps(caught.value.receipt)
    assert "sk-ant-private" not in receipt
    assert "private-refresh-token" not in receipt
    assert "private-credential" not in receipt


@pytest.mark.parametrize("conflict", [
    {"auth_method": "apiKey"},
    {"api_provider": "bedrock"},
    {"account": {"plan": "pro"}},
    {"authenticated": False},
])
def test_claude_conflicting_semantic_aliases_are_ambiguous_not_first_match(conflict):
    status = json.loads(claude_status())
    status.update(conflict)

    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard(
            "claude", environ={}, runner=Runner(json.dumps(status)), now_utc=NOW)

    assert caught.value.reason == "claude_auth_status_ambiguous"
    assert caught.value.receipt["details"]["field"] in {
        "logged_in", "auth_method", "api_provider", "plan",
    }


def test_equivalent_claude_aliases_may_coexist_without_weakening_checks():
    status = json.loads(claude_status())
    status.update({"auth_method": "CLAUDE.AI", "api_provider": "first-party",
                   "account": {"plan": "Max 20x"}, "authenticated": True})

    receipt = check_subscription_guard(
        "claude", environ={}, runner=Runner(json.dumps(status)), now_utc=NOW)

    assert receipt["verdict"] == "allow"


def test_unknown_claude_status_fields_fail_closed_without_echoing_values():
    secret = "private-diagnostic-value"
    status = json.loads(claude_status())
    status["newUnreviewedField"] = secret

    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard(
            "claude", environ={}, runner=Runner(json.dumps(status)), now_utc=NOW)

    assert caught.value.reason == "claude_auth_status_fields_invalid"
    assert secret not in json.dumps(caught.value.receipt)


@pytest.mark.parametrize("name", [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "OPENAI_API_KEY",
    "OPENAI_AUTH_TOKEN",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_MODEL",
    "CODEX_API_KEY",
    "CODEX_HOME",
    "CODEX_PERMISSION_PROFILE",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
    "ANTHROPIC_EXPERIMENTAL_ENDPOINT",
    "HTTP_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "NPM_CONFIG_HTTPS_PROXY",
    "GLOBAL_AGENT_HTTP_PROXY",
    "NODE_EXTRA_CA_CERTS",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "SSL_CERT_FILE",
])
def test_any_billing_or_routing_override_name_denies_without_reading_values(name):
    secret = "do-not-copy-this-value"

    class KeysOnly(dict):
        def __getitem__(self, _key):
            raise AssertionError("guard must not read environment values")

        def get(self, _key, _default=None):
            raise AssertionError("guard must not read environment values")

    environ = KeysOnly({name: secret})
    runner = Runner(claude_status())

    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard("claude", environ=environ, runner=runner, now_utc=NOW)

    assert caught.value.reason == "billing_or_routing_override_present"
    assert caught.value.receipt["details"]["forbidden_environment_name_count"] == 1
    assert name not in json.dumps(caught.value.receipt)
    assert secret not in json.dumps(caught.value.receipt)
    assert secret not in str(caught.value)
    assert runner.calls == []


def test_empty_override_value_is_still_ambiguous_and_denied():
    with pytest.raises(SubscriptionGuardError):
        check_subscription_guard(
            "claude", environ={"ANTHROPIC_API_KEY": ""},
            runner=Runner(claude_status()), now_utc=NOW)


def test_codex_requires_chatgpt_and_fresh_zero_credit_non_reload_evidence():
    runner = Runner("Logged in using ChatGPT\n")
    evidence = codex_evidence()

    receipt = check_subscription_guard(
        "codex", account_evidence=evidence, environ={}, runner=runner, now_utc=NOW)

    assert receipt["verdict"] == "allow"
    assert receipt["auth"] == {"status": "authenticated", "method": "ChatGPT"}
    assert receipt["account_evidence"] == {
        "credits_remaining": 0,
        "auto_reload": False,
        "observed_at_utc": "2026-08-12T11:55:00Z",
        "expires_at_utc": "2026-08-12T12:10:00Z",
    }
    assert runner.calls == [("codex", "login", "status")]


def test_codex_account_evidence_rejects_extra_or_credential_fields_without_echoing():
    secret = "private-token-in-key"
    evidence = codex_evidence(**{secret: "private-token-value"})
    runner = Runner("Logged in using ChatGPT")

    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard(
            "codex", account_evidence=evidence, environ={},
            runner=runner, now_utc=NOW)

    assert caught.value.reason == "codex_account_evidence_fields_invalid"
    serialized = json.dumps(caught.value.receipt)
    assert secret not in serialized
    assert "private-token-value" not in serialized
    assert runner.calls == []


def test_codex_accepts_current_cli_redacted_status_channel():
    runner = Runner("", stderr="Logged in using ChatGPT\n")

    receipt = check_subscription_guard(
        "codex", account_evidence=codex_evidence(), environ={},
        runner=runner, now_utc=NOW)

    assert receipt["verdict"] == "allow"
    assert receipt["auth"] == {"status": "authenticated", "method": "ChatGPT"}


def test_auth_status_with_both_output_channels_is_ambiguous_and_denied():
    runner = Runner("Logged in using ChatGPT\n", stderr="warning")

    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard(
            "codex", account_evidence=codex_evidence(), environ={},
            runner=runner, now_utc=NOW)

    assert caught.value.reason == "auth_status_output_invalid"


@pytest.mark.parametrize("stdout", ["x" * (16 * 1024 + 1), "{}\x00"])
def test_oversized_or_nul_status_output_fails_closed(stdout):
    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard(
            "claude", environ={}, runner=Runner(stdout), now_utc=NOW)
    assert caught.value.reason == "auth_status_output_invalid"


@pytest.mark.parametrize(("evidence", "reason"), [
    (None, "codex_account_evidence_required"),
    (codex_evidence(credits_remaining=1), "codex_credits_remaining_must_be_zero"),
    (codex_evidence(credits_remaining="0"), "codex_credits_remaining_must_be_zero"),
    (codex_evidence(auto_reload=True), "codex_auto_reload_must_be_false"),
    (codex_evidence(auto_reload=0), "codex_auto_reload_must_be_false"),
    (codex_evidence(observed_at="2026-08-12T11:00:00Z"),
     "codex_account_evidence_expired"),
    (codex_evidence(observed_at="2026-08-12T11:55:00-07:00"),
     "codex_account_evidence_timestamp_not_utc"),
    (codex_evidence(observed_at="not-a-date"),
     "codex_account_evidence_timestamp_invalid"),
    (codex_evidence(observed_at="2026-08-12T12:00:01Z"),
     "codex_account_evidence_from_future"),
])
def test_codex_account_evidence_is_strict_and_checked_before_cli(evidence, reason):
    runner = Runner("Logged in using ChatGPT")

    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard(
            "codex-cli", account_evidence=evidence, environ={}, runner=runner, now_utc=NOW)

    assert caught.value.reason == reason
    assert runner.calls == []


def test_codex_evidence_is_valid_through_its_exact_expiry_boundary():
    observed = NOW - dt.timedelta(seconds=CODEX_EVIDENCE_MAX_AGE_SECONDS)
    runner = Runner("Logged in using ChatGPT")

    receipt = check_subscription_guard(
        "codex", account_evidence=codex_evidence(observed_at=observed.isoformat()),
        environ={}, runner=runner, now_utc=NOW)

    assert receipt["verdict"] == "allow"


@pytest.mark.parametrize("status", [
    "Logged in using an API key",
    "Not logged in",
    "Logged in using ChatGPT\nextra output",
    "Logged in with ChatGPT",
    "logged in using chatgpt",
    "Logged in using ChatGPT account.",
])
def test_codex_rejects_any_login_status_other_than_exact_chatgpt(status):
    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard(
            "codex", account_evidence=codex_evidence(), environ={},
            runner=Runner(status), now_utc=NOW)

    assert caught.value.reason == "codex_login_not_chatgpt"


def test_command_failures_never_echo_stdout_or_stderr():
    secret = "secret-from-cli"
    runner = Runner(secret, returncode=2, stderr="stderr " + secret)

    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard("claude", environ={}, runner=runner, now_utc=NOW)

    serialized = json.dumps(caught.value.receipt)
    assert caught.value.reason == "auth_status_command_failed"
    assert secret not in serialized
    assert secret not in str(caught.value)


def test_malformed_or_duplicate_claude_status_is_denied_without_echoing_it():
    secret = "secret-duplicate-value"
    runner = Runner('{"loggedIn":true,"loggedIn":true,"accessToken":"%s"}' % secret)

    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard("claude", environ={}, runner=runner, now_utc=NOW)

    assert caught.value.reason == "auth_status_output_invalid"
    assert secret not in json.dumps(caught.value.receipt)


def test_unknown_provider_fails_closed_without_invoking_any_command():
    runner = Runner("")
    with pytest.raises(SubscriptionGuardError) as caught:
        check_subscription_guard("pi", environ={}, runner=runner, now_utc=NOW)
    assert caught.value.reason == "unsupported_provider"
    assert runner.calls == []


def test_invalid_environment_key_and_clock_inputs_return_only_safe_denials():
    runner = Runner(claude_status())
    with pytest.raises(SubscriptionGuardError) as bad_environment:
        check_subscription_guard(
            "claude", environ={object(): "private"}, runner=runner, now_utc=NOW)
    assert bad_environment.value.reason == "environment_name_invalid"
    assert "private" not in json.dumps(bad_environment.value.receipt)
    assert runner.calls == []

    with pytest.raises(SubscriptionGuardError) as bad_clock:
        check_subscription_guard(
            "claude", environ={}, runner=runner,
            now_utc=dt.datetime(2026, 8, 12, 12, 0))
    assert bad_clock.value.reason == "check_time_invalid"
    assert runner.calls == []


def test_default_runner_resolves_windows_style_cli_shim_without_a_shell(monkeypatch):
    from bench import subscription_guard as guard

    seen = {}

    class SafeChildEnvironment(dict):
        def __getitem__(self, key):
            if key == "UNRELATED_SECRET":
                raise AssertionError("unrelated secret value must not be read")
            return super().__getitem__(key)

    monkeypatch.setattr(
        guard.shutil, "which", lambda name, path=None: "C:/bin/%s.cmd" % name)

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, claude_status(), "")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)

    receipt = check_subscription_guard(
        "claude", environ=SafeChildEnvironment(
            {"PATH": "C:/bin", "UNRELATED_SECRET": "private"}),
        now_utc=NOW)

    assert receipt["verdict"] == "allow"
    assert seen["argv"] == ["C:/bin/claude.cmd", "auth", "status", "--json"]
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["env"] == {"NO_COLOR": "1", "PATH": "C:/bin"}
