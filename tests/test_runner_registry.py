"""The registry is the hub: everything downstream believes what a probe says.

So the tests here are about honesty rather than plumbing. A probe row decides
whether a worker is offered work at all (H3), which login pays for it (H5), and
what capabilities the selector may rely on — and it is assembled from a `which`,
a `--version`, a login file and, on the live path, an official CLI's own status
output. Four properties matter enough to pin:

* **Closed.** The key set is exhaustive and reviewed; it is also disjoint from
  the provider names, because `--runner codex-exec` and `--provider codex` are
  different axes and a shared name would make one of them ambiguous.
* **Read-only and offline.** No probe reads a credential value and none touches
  the network — asserted by putting real-looking tokens in fake login files and
  grepping the serialized probe for them, and by making `urlopen` fatal.
* **Unknown stays unknown.** Not installed, not logged in, no billing evidence:
  each comes back as a row saying so, never as an optimistic default and never
  as an exception that takes the whole table down.
* **A declared capability is not a verified one.** `apply_compat_report` is the
  only thing that can turn a claim into evidence, and it can only ever take
  capabilities away.

No real CLI is launched: `shutil.which` and `subprocess.run` are faked, and the
live status command is injected.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request

import pytest

from harness import runner_registry, settings
from harness.runner_specs import (
    BILLING_CLASSES,
    BILLING_MODE_OF,
    CURRENT_PHASE,
    NOT_IMPLEMENTED_PREFIX,
    RunnerUnavailableError,
)


PHASE_1_KEYS = ("collie", "codex-exec", "claude-code")
ALL_KEYS = PHASE_1_KEYS + ("codex-app-server", "pi-rpc", "prime-rpc",
                           "hermes-gateway", "hermes-acp")

# Deliberately shaped like the real thing: an OAuth blob whose token would be
# obvious in any output that leaked it.
CLAUDE_TOKEN = "sk-ant-oat01-CLAUDETOKENSHOULDNEVERAPPEAR"
CODEX_TOKEN = ("eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjQxMDI0NDQ4MDB9."
               "CODEXTOKENSHOULDNEVERAPPEAR")


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test starts with an empty probe cache and no compat report applied."""
    runner_registry.reset_cache()
    runner_registry._COMPAT.clear()
    yield
    runner_registry.reset_cache()
    runner_registry._COMPAT.clear()


@pytest.fixture
def empty_home(tmp_path, monkeypatch):
    """Point every home-shaped variable at an empty directory.

    HOME alone is not enough: `os.path.expanduser` reads USERPROFILE on Windows,
    and both CLIs honour their own override variable.
    """
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    return home


@pytest.fixture
def no_network(monkeypatch):
    """A probe that opens a socket fails this suite instead of the user's quota."""
    def explode(*args, **kwargs):
        raise AssertionError("a probe must never touch the network")
    monkeypatch.setattr(urllib.request, "urlopen", explode)


def fake_which(installed):
    """`shutil.which` over a fixed inventory of binaries."""
    calls = []

    def which(name, *args, **kwargs):
        calls.append(name)
        return installed.get(os.path.basename(str(name)))
    which.calls = calls
    return which


def fake_run(versions):
    """`subprocess.run` answering only `<cli> --version`."""
    def run(argv, *args, **kwargs):
        assert list(argv)[1:] == ["--version"], argv
        text = versions.get(os.path.basename(str(argv[0])))
        if text is None:
            raise AssertionError("unexpected CLI call: %r" % (argv,))
        return subprocess.CompletedProcess(argv, 0, stdout=text + "\n", stderr="")
    return run


@pytest.fixture
def installed_clis(monkeypatch):
    """Both phase-1 CLIs on PATH, answering --version at the pinned versions."""
    which = fake_which({"codex": "/usr/bin/codex", "claude": "/usr/bin/claude",
                        "pi": "/usr/bin/pi"})
    monkeypatch.setattr(runner_registry.shutil, "which", which)
    monkeypatch.setattr(subprocess, "run",
                        fake_run({"codex": "codex-cli 0.149.0",
                                  "claude": "2.1.221 (Claude Code)"}))
    return which


def completed(stdout="", stderr=""):
    return subprocess.CompletedProcess(("cli",), 0, stdout=stdout, stderr=stderr)


# --- the table --------------------------------------------------------------
def test_keys_closed_and_disjoint_from_providers():
    assert tuple(runner_registry.SPECS) == ALL_KEYS
    # Closed means closed: a plugin cannot register a worker at runtime, because a
    # worker is a reviewed billing route and not a drop-in.
    with pytest.raises(TypeError):
        runner_registry.SPECS["rogue"] = runner_registry.SPECS["collie"]

    from harness import providers
    provider_names = set(providers.OPENAI_COMPAT_PRESETS) | {
        "mock", "anthropic", "anthropic-oauth", "claude-sub", "codex-oauth",
        "codex-sub", "codex", "claude-cli", "cli", "claude-agent-sdk",
        "claude-sdk", "ollama"}
    provider_names |= {opt["value"] for row in settings.SCHEMA
                       if row["key"] == "PROVIDER" for opt in row["options"]}
    # Worker and Brain are different axes; a shared name would make `--runner
    # codex` and `--provider codex` read as the same choice.
    assert provider_names.isdisjoint(set(runner_registry.SPECS))

    for key, spec in runner_registry.SPECS.items():
        assert spec.key == key
        assert spec.kind in ("native", "external")
        assert spec.label
        assert spec.phase >= 1
        # Every external worker declares which login pays for it; only the native
        # one follows the configured PROVIDER.
        assert bool(spec.credential_family) == (spec.kind == "external")


def test_option_keys_are_the_phase_one_runners():
    assert runner_registry.option_keys() == PHASE_1_KEYS
    for key in runner_registry.option_keys():
        assert runner_registry.SPECS[key].phase <= CURRENT_PHASE


def test_settings_runner_options_match_specs():
    """The Settings panel may not offer a worker the registry does not have."""
    row = next(r for r in settings.SCHEMA if r["key"] == "RUNNER")
    values = tuple(opt["value"] for opt in row["options"])
    # `auto` is a request to choose, not a runner, so it is the one extra value.
    assert "auto" in values
    assert tuple(v for v in values if v != "auto") == runner_registry.option_keys()
    assert row["default"] in runner_registry.option_keys()
    pool_row = next(r for r in settings.SCHEMA if r["key"] == "RUNNER_POOL")
    assert pool_row["default"] in runner_registry.option_keys()


# --- probing ----------------------------------------------------------------
def test_probe_not_installed(monkeypatch, empty_home, no_network):
    monkeypatch.setattr(runner_registry.shutil, "which", fake_which({}))
    for key in ("codex-exec", "claude-code"):
        row = runner_registry.probe(key)
        assert row.key == key
        assert row.installed is False
        assert row.usable() is False
        assert "not installed" in row.detail        # says why, does not raise
        assert row.version == ""
        assert row.billing_class == "unknown"
        assert row.billing_mode == "unconfigured"
    # The native harness is this process: it cannot be missing from PATH.
    assert runner_registry.probe("collie", provider="anthropic").usable() is True


def test_probe_unknown_key_is_a_row_not_an_exception():
    row = runner_registry.probe("does-not-exist")
    assert row.installed is False
    assert row.usable() is False
    assert "unknown runner" in row.detail


def test_probe_never_reads_credentials_or_network(monkeypatch, empty_home,
                                                  no_network, installed_clis):
    """Real-looking tokens in both login files; neither may reach a probe."""
    claude_dir = empty_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": CLAUDE_TOKEN, "refreshToken": "rt-secret",
                          "expiresAt": 4102444800000}}), encoding="utf-8")
    codex_dir = empty_home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text(json.dumps({
        "tokens": {"access_token": CODEX_TOKEN, "refresh_token": "rt-secret"}}),
        encoding="utf-8")

    rows = runner_registry.probe_all(provider="anthropic")
    serialized = json.dumps({key: row.to_dict() for key, row in rows.items()})
    for secret in (CLAUDE_TOKEN, CODEX_TOKEN, "SHOULDNEVERAPPEAR", "rt-secret"):
        assert secret not in serialized
    # The metadata that *is* allowed out: which file, which kind of login, when
    # it expires — enough to tell "signed in" from "will stop at a prompt".
    assert rows["claude-code"].login == "ok"
    assert rows["claude-code"].billing_evidence["login_kind"] == "claude.ai"
    assert rows["codex-exec"].login == "ok"
    assert rows["codex-exec"].billing_evidence["login_kind"] == "chatgpt"
    # Signed in is not evidence of which plan pays: that needs --live.
    assert rows["claude-code"].billing_class == "unknown"
    assert rows["codex-exec"].billing_class == "unknown"


def test_probe_survives_an_empty_home(monkeypatch, empty_home, no_network,
                                      installed_clis):
    rows = runner_registry.probe_all(provider="anthropic")
    assert set(rows) == set(ALL_KEYS)
    assert rows["codex-exec"].login == "not-logged-in"
    assert rows["codex-exec"].usable() is False
    claude = rows["claude-code"]
    # macOS keeps the same blob in the Keychain, so "no file" is not "no login"
    # there — the row says unknown instead of claiming a fact it does not have.
    assert claude.login in ("not-logged-in", "unknown")
    assert claude.usable() is False
    assert claude.detail


def test_probe_survives_an_unreadable_login_file(monkeypatch, empty_home,
                                                 no_network, installed_clis):
    (empty_home / ".claude").mkdir()
    (empty_home / ".claude" / ".credentials.json").write_text("{not json",
                                                              encoding="utf-8")
    (empty_home / ".codex").mkdir()
    (empty_home / ".codex" / "auth.json").write_text("{not json", encoding="utf-8")
    rows = runner_registry.probe_all(keys=("claude-code", "codex-exec"))
    for row in rows.values():
        assert row.login == "unknown"
        assert row.usable() is False
        assert row.detail


def test_probe_cache_ttl(monkeypatch, empty_home, no_network, installed_clis):
    first = runner_registry.probe("codex-exec", now=1_000.0)
    calls = len(installed_clis.calls)
    assert calls > 0
    again = runner_registry.probe("codex-exec", now=1_030.0)
    assert again is first                                   # inside the 60s window
    assert len(installed_clis.calls) == calls
    later = runner_registry.probe("codex-exec", now=1_061.0)
    assert later is not first
    assert len(installed_clis.calls) > calls
    assert later.ttl_s == runner_registry.PROBE_TTL_S
    # The live row is cached separately: a metadata answer must never be served
    # to a caller that asked for the status command to be run.
    live = runner_registry.probe("codex-exec", live=True, now=1_061.0,
                                 status_runner=lambda argv: completed(
                                     stderr="Logged in using ChatGPT"))
    assert live is not later
    assert runner_registry.probe("codex-exec", now=1_061.0) is later


def test_probe_all_skips_unknown_keys(empty_home, no_network, installed_clis):
    rows = runner_registry.probe_all(keys=("claude-code", "nope"))
    assert list(rows) == ["claude-code"]


def test_collie_is_always_usable_and_bills_like_missionweb(no_network):
    from harness import missionweb
    for provider in ("anthropic", "anthropic-oauth", "claude-agent-sdk", "codex",
                     "ollama", "mock", "deepseek"):
        runner_registry.reset_cache()
        row = runner_registry.probe("collie", provider=provider)
        assert row.installed is True
        assert row.login == "n/a"
        assert row.usable() is True
        assert row.billing_class in BILLING_CLASSES
        assert row.billing_mode == BILLING_MODE_OF[row.billing_class]
        # The durable definition of a billing mode lives in missionweb; this
        # module reproduces it rather than importing the Mission service, so the
        # two are pinned against each other here.
        assert row.billing_mode == missionweb._billing_mode(provider)
    # No provider at all is "unconfigured", not "free": nothing is claimed about
    # who pays.  (`probe(provider="")` means "use the configured PROVIDER", which
    # is a different question, so the mapping is checked directly.)
    assert runner_registry._collie_billing("")[0] == "unknown"
    assert BILLING_MODE_OF["unknown"] == missionweb._billing_mode("")


def test_collie_probe_follows_the_configured_provider(monkeypatch, no_network):
    monkeypatch.setattr(runner_registry, "_configured_provider", lambda: "ollama")
    row = runner_registry.probe("collie")
    assert row.billing_class == "local"
    assert row.billing_evidence["provider"] == "ollama"


# --- the live path ----------------------------------------------------------
def test_live_claude_reads_plan_from_the_cli_status(monkeypatch, empty_home,
                                                    no_network, installed_clis):
    status = json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                         "apiProvider": "firstParty", "subscriptionType": "max"})
    row = runner_registry.probe("claude-code", live=True,
                                status_runner=lambda argv: completed(stdout=status))
    assert row.login == "ok"
    assert row.billing_class == "subscription_allowance"
    assert row.billing_mode == "subscription"
    assert row.billing_evidence["plan"] == "max"
    assert row.billing_evidence["source"] == "claude auth status"
    # An observation is not an attestation: nobody has said this plan has no
    # paid overage, so H5 still has something to refuse.
    assert row.overage_attested is False
    assert row.usable() is True


def test_live_claude_non_first_party_is_metered_not_broken(empty_home, no_network,
                                                           installed_clis):
    status = json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                         "apiProvider": "bedrock", "subscriptionType": "max"})
    row = runner_registry.probe("claude-code", live=True,
                                status_runner=lambda argv: completed(stdout=status))
    # A real login on a different payer: usable, but billed per request.
    assert row.login == "ok"
    assert row.billing_class == "api_metered"
    assert row.billing_mode == "metered"
    assert "per request" in row.detail


def test_live_claude_not_logged_in(empty_home, no_network, installed_clis):
    status = json.dumps({"loggedIn": False})
    row = runner_registry.probe("claude-code", live=True,
                                status_runner=lambda argv: completed(stdout=status))
    assert row.login == "not-logged-in"
    assert row.billing_class == "unknown"
    assert row.usable() is False


def test_live_status_failure_leaves_the_row_unevidenced(empty_home, no_network,
                                                        installed_clis):
    def broken(argv):
        raise FileNotFoundError(argv[0])
    row = runner_registry.probe("claude-code", live=True, status_runner=broken)
    assert row.billing_class == "unknown"
    assert row.detail


def test_live_codex_exact_status_line_is_the_only_evidence(empty_home, no_network,
                                                           installed_clis):
    row = runner_registry.probe(
        "codex-exec", live=True,
        status_runner=lambda argv: completed(stderr="Logged in using ChatGPT"))
    assert row.billing_class == "subscription_allowance"
    assert row.billing_evidence["method"] == "ChatGPT"
    # H5 dates codex evidence (subscription_guard's 15-minute bound), so the row
    # has to carry when it was observed.
    assert row.billing_evidence["observed_at"] > 0

    runner_registry.reset_cache()
    other = runner_registry.probe(
        "codex-exec", live=True,
        status_runner=lambda argv: completed(
            stderr="Logged in as someone@example.com using an API key"))
    assert other.billing_class == "unknown"
    # The status line is never quoted: a non-matching one is exactly the line
    # that can carry an account address.
    assert "example.com" not in json.dumps(other.to_dict())


# --- phase gate -------------------------------------------------------------
def test_phase_gate_marks_unusable(installed_clis, empty_home, no_network):
    future = [key for key, spec in runner_registry.SPECS.items()
              if spec.phase > CURRENT_PHASE]
    assert future                                  # tomorrow's keys are declared today
    for key in future:
        row = runner_registry.probe(key)
        assert row.detail.startswith(NOT_IMPLEMENTED_PREFIX)
        assert row.usable() is False
        assert key not in runner_registry.option_keys()
        with pytest.raises(RunnerUnavailableError):
            runner_registry.make_runner(key)
        # Declared capabilities are still shown, so `collie runners` can say what
        # is coming instead of printing a row of blanks.
        assert row.capabilities["protocol"]


def test_placeholder_probe_launches_nothing(monkeypatch, empty_home, no_network):
    monkeypatch.setattr(runner_registry.shutil, "which",
                        fake_which({"codex": "/usr/bin/codex"}))

    def explode(*args, **kwargs):
        raise AssertionError("a phase-gated probe must not run a CLI")
    monkeypatch.setattr(subprocess, "run", explode)
    row = runner_registry.probe("codex-app-server")
    assert row.installed is True                   # the binary is there …
    assert row.version == ""                       # … but nothing was launched
    assert row.usable() is False


# --- construction -----------------------------------------------------------
def test_make_runner_builds_the_declared_runner(installed_clis):
    from harness.agent_runners import CodexExecRunner
    from harness.claude_code_runner import ClaudeCodeRunner

    codex = runner_registry.make_runner("codex-exec", model="gpt-5.6", timeout_s=30)
    assert isinstance(codex, CodexExecRunner)
    assert codex.model == "gpt-5.6"
    assert codex.default_timeout_s == 30
    assert codex.env_policy == runner_registry.SPECS["codex-exec"].env_policy

    claude = runner_registry.make_runner("claude-code")
    assert isinstance(claude, ClaudeCodeRunner)
    assert claude.default_timeout_s == runner_registry.SPECS["claude-code"].default_timeout_s
    assert claude.env_policy == runner_registry.SPECS["claude-code"].env_policy


def test_make_runner_refuses_collie_and_unknown_keys():
    # collie is not an external worker; a caller asking for one took the wrong
    # branch and should hear about it here, not as an AttributeError later.
    with pytest.raises(ValueError, match="not an external runner"):
        runner_registry.make_runner("collie")
    with pytest.raises(ValueError, match="unknown runner"):
        runner_registry.make_runner("nope")


# --- compat report ----------------------------------------------------------
def _report(tmp_path, **rows):
    path = tmp_path / "runner-compat.json"
    path.write_text(json.dumps({
        "date": "2026-08-21", "os_name": "posix",
        "runners": {key: {"checks": value} for key, value in rows.items()}}),
        encoding="utf-8")
    return str(path)


def test_apply_compat_report_downgrades(tmp_path, empty_home, no_network,
                                        installed_clis):
    before = runner_registry.probe("claude-code", now=1_000.0)
    assert before.capabilities["session_resume"] is True
    assert before.compat == "unverified"

    path = _report(tmp_path, **{"claude-code": {
        "probe": "PASS", "one_turn": "PASS", "resume": "FAIL",
        "usage": "UNVERIFIED", "cancel": "PASS"}})
    applied = runner_registry.apply_compat_report(path)
    assert applied["claude-code"] == ("session_resume", "usage_cost", "usage_tokens")

    # Same clock: the row changed because applying a report drops the cache, not
    # because the TTL expired.
    after = runner_registry.probe("claude-code", now=1_000.0)
    assert after.capabilities["session_resume"] is False
    assert after.capabilities["usage_tokens"] is False
    assert after.capabilities["usage_cost"] is False
    assert after.capabilities["session_create"] is True     # PASS is left alone
    assert after.capabilities["cancel"] == "process-tree"
    assert after.compat == "verified 2026-08-21"
    assert runner_registry.compat_status("codex-exec") == "unverified"
    # The declared table is untouched: a report describes this host, not the code.
    assert runner_registry.SPECS["claude-code"].caps.session_resume is True


def test_apply_compat_report_can_only_take_capabilities_away(tmp_path, empty_home,
                                                             no_network,
                                                             installed_clis):
    path = _report(tmp_path, **{"codex-exec": {
        "usage": "PASS", "approval": "PASS", "resume": "PASS"}})
    runner_registry.apply_compat_report(path)
    caps = runner_registry.probe("codex-exec").capabilities
    # codex exec rejects every approval request; a PASS on some other host's
    # report cannot talk that capability up.
    assert caps["approval_round_trip"] is False
    assert caps["usage_tokens"] is True


def test_apply_compat_report_downgrades_cancel_to_none(tmp_path, empty_home,
                                                       no_network, installed_clis):
    path = _report(tmp_path, **{"codex-exec": {"cancel": "FAIL"}})
    runner_registry.apply_compat_report(path)
    caps = runner_registry.probe("codex-exec").capabilities
    assert caps["cancel"] == "none"


def test_apply_compat_report_decides_windows_native(tmp_path, empty_home,
                                                    no_network, installed_clis):
    assert runner_registry.probe("codex-exec").capabilities["windows_native"] is None
    path = tmp_path / "win.json"
    path.write_text(json.dumps({
        "date": "2026-08-21", "os_name": "nt",
        "runners": {"codex-exec": {"checks": {"handshake": "PASS",
                                              "one_turn": "PASS"}}}}),
        encoding="utf-8")
    runner_registry.apply_compat_report(str(path))
    assert runner_registry.probe("codex-exec").capabilities["windows_native"] is True


def test_apply_compat_report_missing_file_changes_nothing(tmp_path, empty_home,
                                                          no_network, installed_clis):
    assert runner_registry.apply_compat_report(str(tmp_path / "absent.json")) == {}
    assert runner_registry.probe("claude-code").compat == "unverified"


def test_apply_compat_report_rejects_a_broken_file(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    # Silently ignoring the report someone just pointed at is how an unverified
    # capability gets believed.
    with pytest.raises(ValueError):
        runner_registry.apply_compat_report(str(path))


def test_apply_compat_report_ignores_unknown_runners(tmp_path, empty_home,
                                                     no_network, installed_clis):
    path = _report(tmp_path, **{"from-the-future": {"resume": "FAIL"}})
    assert runner_registry.apply_compat_report(path) == {}

def test_a_stored_compat_report_is_folded_in_without_being_asked(tmp_path, monkeypatch):
    """The conformance loop has to close on its own, or it does not close.

    `collie runners compat` leaves its answer in the state directory; if nothing
    reads it back, `windows_native` stays None forever, H10 treats that as
    unverified, and Auto refuses every external worker on Windows no matter what
    the pool says.  Probing must pick the report up by itself.
    """
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    runner_registry.reset_cache()
    report = {
        "date": "2026-08-22",
        "os": "nt",
        "runners": {
            "codex-exec": {"checks": {"probe": "PASS", "handshake": "PASS",
                                      "one_turn": "PASS", "resume": "PASS"}},
        },
    }
    path = tmp_path / runner_registry.COMPAT_REPORT_NAME
    path.write_text(json.dumps(report), encoding="utf-8")

    probe = runner_registry.probe("codex-exec")

    assert runner_registry.compat_status("codex-exec").startswith("verified")
    assert probe.compat.startswith("verified")


def test_a_missing_report_is_the_normal_state(tmp_path, monkeypatch):
    """A fresh install has never run the matrix; that must be silent."""
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    runner_registry.reset_cache()
    assert runner_registry.probe("codex-exec").compat == "unverified"


def test_a_corrupt_report_does_not_take_down_probing(tmp_path, monkeypatch):
    """Explicitly pointing at a broken file still raises; the standing one cannot.

    A file nobody asked for must never be able to break `collie run`.
    """
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    runner_registry.reset_cache()
    path = tmp_path / runner_registry.COMPAT_REPORT_NAME
    path.write_text("{not json", encoding="utf-8")

    assert runner_registry.probe("codex-exec").compat == "unverified"
    with pytest.raises(ValueError):
        runner_registry.apply_compat_report(str(path))
