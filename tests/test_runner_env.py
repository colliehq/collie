"""The child environment of an external worker is a billing and secrecy boundary.

Four things are asserted here, and each one stands for a way this has already
gone wrong somewhere in the tree:

* the five inventories that grew independently (bench prime/pi, bench hermes,
  swe.py's two lists, subscription_guard's forbidden names + status child env)
  really are merged — imported from their real modules, so adding a name there
  and forgetting this module fails the build rather than the invoice;
* a credential in the parent environment never reaches the child, and the
  *value* never reaches the receipt;
* the variables a Windows child cannot start without are still inherited — a
  hygiene rule that breaks every spawn is not a hygiene rule, it is an outage;
* a route override is refused before launch instead of degraded around.

No subprocess is created and no CLI is consulted: every parent environment here
is an explicit dict.
"""
from __future__ import annotations

import json
import os

import pytest

from bench.normalized_hermes import _SAFE_ENV_KEYS
from bench.normalized_prime_pi import FORBIDDEN_AUTH_ENV, _SAFE_INHERITED_ENV
from harness import runner_env, subscription_guard
from harness.runner_env import (
    POLICIES,
    BillingOverrideError,
    allowlist,
    assert_no_billing_override,
    child_env,
)
from harness.swe import _NON_CLAUDE_KEYS, _NON_CODEX_KEYS


SECRET = "sk-ant-not-a-real-key-000000000000"

# A plausible Windows parent environment: the process-location variables a
# child needs, plus the credentials and overrides it must never see.
WINDOWS_REQUIRED = {
    "APPDATA": r"C:\Users\dev\AppData\Roaming",
    "COMSPEC": r"C:\Windows\system32\cmd.exe",
    "HOMEDRIVE": "C:",
    "HOMEPATH": r"\Users\dev",
    "LOCALAPPDATA": r"C:\Users\dev\AppData\Local",
    "PATH": r"C:\Windows\system32;C:\Program Files\nodejs",
    "PATHEXT": ".COM;.EXE;.BAT;.CMD",
    "PROGRAMDATA": r"C:\ProgramData",
    "PROGRAMFILES": r"C:\Program Files",
    "SYSTEMDRIVE": "C:",
    "SYSTEMROOT": r"C:\Windows",
    "TEMP": r"C:\Users\dev\AppData\Local\Temp",
    "TMP": r"C:\Users\dev\AppData\Local\Temp",
    "USERPROFILE": r"C:\Users\dev",
    "WINDIR": r"C:\Windows",
}


def _parent(**overrides: str) -> dict[str, str]:
    env = dict(WINDOWS_REQUIRED)
    env.update(overrides)
    return env


# --------------------------------------------------------------------------
# the merge of the five sources
# --------------------------------------------------------------------------

def test_status_child_env_names_kept():
    """subscription_guard already decided which names a status child may keep."""
    allowed = allowlist("native")
    missing = sorted(n for n in subscription_guard._STATUS_CHILD_ENV_NAMES if n not in allowed)
    assert missing == [], "guard allows these for a status child but the worker policy drops them"


def test_bench_safe_env_keys_kept():
    """Both benchmark allowlists are subsets of the worker allowlist."""
    allowed = allowlist("native")
    assert sorted(n for n in _SAFE_INHERITED_ENV if n not in allowed) == []
    assert sorted(n for n in _SAFE_ENV_KEYS if n not in allowed) == []


@pytest.mark.parametrize("policy", POLICIES)
def test_forbidden_auth_env_rejected(policy):
    """Every name the four denylists know about is stripped, under every policy.

    The parent here holds all of them at once, which is the case that matters:
    a machine with a leftover key from some other project must not turn a
    subscription worker into a metered one.
    """
    forbidden = sorted(set(FORBIDDEN_AUTH_ENV) | set(_NON_CLAUDE_KEYS) | set(_NON_CODEX_KEYS))
    env, receipt = child_env(policy, environ=_parent(**{n: SECRET for n in forbidden}))
    leaked = sorted(n for n in forbidden if n in env)
    assert leaked == []
    assert SECRET not in json.dumps(env)
    assert sorted(n for n in forbidden if n not in receipt["stripped"]) == []


def test_proxy_and_loader_overrides_are_stripped():
    """A proxy or a preloaded library rewrites the run without touching a key."""
    hostile = {
        "HTTPS_PROXY": "http://192.0.2.7:8080",
        "NODE_EXTRA_CA_CERTS": r"C:\tmp\mitm.pem",
        "NODE_TLS_REJECT_UNAUTHORIZED": "0",
        "NODE_OPTIONS": "--require C:/tmp/hook.js",
        "LD_PRELOAD": "/tmp/hook.so",
        "PYTHONPATH": "/tmp/sitecustomize",
    }
    env, receipt = child_env("claude", environ=_parent(**hostile))
    assert [n for n in hostile if n in env] == []
    assert sorted(hostile) == sorted(n for n in receipt["stripped"] if n in hostile)


def test_parent_session_markers_are_stripped():
    """Collie is usually started from inside one of these CLIs (swe.py's bug)."""
    env, receipt = child_env("codex", environ=_parent(
        CODEX_THREAD_ID="thr_123", CODEX_PERMISSION_PROFILE="read-only",
        CODEX_CI="1", CODEX_SESSION_ID="session-parent",
        CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="cli"))
    assert "CODEX_THREAD_ID" not in env
    assert "CODEX_PERMISSION_PROFILE" not in env
    assert "CODEX_CI" not in env and "CODEX_SESSION_ID" not in env
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert "CODEX_THREAD_ID" in receipt["stripped"]
    assert "CODEX_CI" in receipt["stripped"]
    assert "CLAUDECODE" in receipt["stripped"]


@pytest.mark.parametrize("policy", POLICIES)
def test_allowlist_never_overlaps_the_sensitive_inventory(policy):
    """The two tables cannot disagree: an allowlisted name is never sensitive.

    CODEX_HOME is the one name that appears in both, and it only resolves in
    favour of the allowlist under the codex policy — that exemption is written
    down once, in `_POLICY_PASSTHROUGH`, and nowhere else.
    """
    for name in allowlist(policy):
        assert not runner_env._is_sensitive(name, policy), name
    assert runner_env._is_sensitive("CODEX_HOME", "claude")
    assert not runner_env._is_sensitive("CODEX_HOME", "codex")


# --------------------------------------------------------------------------
# the receipt
# --------------------------------------------------------------------------

def test_env_receipt_only_key_names():
    env, receipt = child_env("claude", environ=_parent(
        ANTHROPIC_API_KEY=SECRET, GITHUB_TOKEN="ghp_" + "0" * 36))
    assert sorted(receipt) == ["allowed", "stripped"]
    assert all(isinstance(n, str) for n in receipt["allowed"] + receipt["stripped"])
    assert receipt["allowed"] == sorted(receipt["allowed"])
    assert receipt["stripped"] == sorted(receipt["stripped"])
    blob = json.dumps(receipt)
    assert SECRET not in blob and "ghp_" not in blob
    # No value from the parent survives into the receipt either — not even the
    # harmless ones, because "only key names" has to be checkable in one line.
    for value in WINDOWS_REQUIRED.values():
        assert value not in blob
    # `allowed` is the complete description of what the child can see.
    assert receipt["allowed"] == sorted(env)


def test_receipt_does_not_list_ordinary_unrelated_names():
    """Only deliberate removals are reported; noise would bury the two lines
    that matter."""
    _, receipt = child_env("native", environ=_parent(CHOCOLATEYINSTALL=r"C:\choco"))
    assert "CHOCOLATEYINSTALL" not in receipt["stripped"]
    assert "CHOCOLATEYINSTALL" not in receipt["allowed"]


# --------------------------------------------------------------------------
# the child still starts
# --------------------------------------------------------------------------

def test_windows_required_keys_survive_with_their_values():
    env, _ = child_env("native", environ=_parent(ANTHROPIC_API_KEY=SECRET))
    for name, value in WINDOWS_REQUIRED.items():
        assert env.get(name) == value, "%s is required for a child process to start" % name


def test_no_color_is_injected():
    env, receipt = child_env("native", environ=_parent())
    assert env["NO_COLOR"] == "1"
    assert "NO_COLOR" in receipt["allowed"]


def test_home_override_rewrites_both_spellings(tmp_path):
    env, _ = child_env("codex", environ=_parent(), home=str(tmp_path))
    resolved = os.path.abspath(str(tmp_path))
    assert env["HOME"] == resolved
    assert env["USERPROFILE"] == resolved
    drive, rest = os.path.splitdrive(resolved)
    if drive:
        # Leaving the inherited pair in place would point `~` at the real home.
        assert env["HOMEDRIVE"] == drive and env["HOMEPATH"] == rest
    else:
        assert "HOMEDRIVE" not in env and "HOMEPATH" not in env


def test_home_must_be_a_real_path():
    with pytest.raises(ValueError):
        child_env("native", environ=_parent(), home="")


# --------------------------------------------------------------------------
# policies differ from each other
# --------------------------------------------------------------------------

def test_codex_policy_keeps_codex_home_and_claude_policy_does_not():
    """CODEX_HOME picks which auth.json is read, and Collie's own probe honours
    it — so the codex child must see the same one, while the claude child has
    no business knowing about it."""
    parent = _parent(CODEX_HOME=r"C:\Users\dev\.codex")
    codex_env, codex_receipt = child_env("codex", environ=parent)
    claude_env, claude_receipt = child_env("claude", environ=parent)
    assert codex_env["CODEX_HOME"] == r"C:\Users\dev\.codex"
    assert "CODEX_HOME" not in codex_receipt["stripped"]
    assert "CODEX_HOME" not in claude_env
    assert "CODEX_HOME" in claude_receipt["stripped"]


def test_claude_config_dir_is_not_inherited():
    """Collie reads ~/.claude directly, so honouring the variable would let the
    run use an account the probe never looked at."""
    env, receipt = child_env("claude", environ=_parent(CLAUDE_CONFIG_DIR=r"C:\other"))
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "CLAUDE_CONFIG_DIR" in receipt["stripped"]


def test_sidecar_policy_is_a_placeholder_that_still_works():
    env, _ = child_env("sidecar-harness", environ=_parent())
    assert env["PATH"] == WINDOWS_REQUIRED["PATH"]
    assert "CODEX_HOME" not in env


def test_unknown_policy_is_rejected():
    with pytest.raises(ValueError) as excinfo:
        child_env("yolo", environ=_parent())
    assert "yolo" in str(excinfo.value)


# --------------------------------------------------------------------------
# extra injection
# --------------------------------------------------------------------------

def test_extra_injects_caller_owned_variables():
    env, receipt = child_env("codex", environ=_parent(),
                             extra={"COLLIE_PROCESS_OWNER": "mission-42"})
    assert env["COLLIE_PROCESS_OWNER"] == "mission-42"
    assert "COLLIE_PROCESS_OWNER" in receipt["allowed"]


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "OPENAI_BASE_URL", "NODE_OPTIONS"])
def test_extra_cannot_smuggle_a_sensitive_name(name):
    with pytest.raises(BillingOverrideError):
        child_env("claude", environ=_parent(), extra={name: SECRET})


def test_extra_value_must_be_a_string():
    with pytest.raises(ValueError):
        child_env("native", environ=_parent(), extra={"COLLIE_RUN_ID": 7})


# --------------------------------------------------------------------------
# the parent environment
# --------------------------------------------------------------------------

def test_parent_mapping_is_never_mutated():
    parent = _parent(ANTHROPIC_API_KEY=SECRET)
    before = dict(parent)
    child_env("claude", environ=parent, extra={"COLLIE_RUN_ID": "r1"}, home=os.getcwd())
    assert parent == before


def test_defaults_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("COLLIE_TEST_ONLY_MARKER", "x")
    env, receipt = child_env("claude")
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" in receipt["stripped"]
    # An unrelated name is not inherited either: the policy is an allowlist.
    assert "COLLIE_TEST_ONLY_MARKER" not in env


# --------------------------------------------------------------------------
# assert_no_billing_override
# --------------------------------------------------------------------------

@pytest.mark.parametrize("family,name", [
    ("claude", "ANTHROPIC_API_KEY"),
    ("claude", "ANTHROPIC_AUTH_TOKEN"),
    ("claude", "ANTHROPIC_BASE_URL"),
    ("claude", "CLAUDE_CODE_OAUTH_TOKEN"),
    ("codex", "OPENAI_API_KEY"),
    ("codex", "OPENAI_BASE_URL"),
    ("codex", "AZURE_OPENAI_API_KEY"),
])
def test_assert_no_billing_override_refuses_before_launch(family, name):
    with pytest.raises(BillingOverrideError) as excinfo:
        assert_no_billing_override(_parent(**{name: SECRET}), family)
    message = str(excinfo.value)
    assert name in message, "the user cannot act on a refusal that hides the variable"
    assert SECRET not in message


def test_assert_no_billing_override_reports_names_never_values():
    with pytest.raises(BillingOverrideError) as excinfo:
        assert_no_billing_override(
            _parent(ANTHROPIC_API_KEY=SECRET, ANTHROPIC_BASE_URL="http://192.0.2.9"), "claude")
    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in message and "ANTHROPIC_BASE_URL" in message
    assert SECRET not in message and "192.0.2.9" not in message


def test_assert_no_billing_override_presence_is_enough():
    """An empty value is still an ambiguous shell override (guard's rule)."""
    with pytest.raises(BillingOverrideError):
        assert_no_billing_override(_parent(ANTHROPIC_BASE_URL=""), "claude")


def test_assert_no_billing_override_passes_on_a_clean_environment():
    assert_no_billing_override(_parent(), "claude")
    assert_no_billing_override(_parent(), "codex")


def test_assert_no_billing_override_ignores_the_other_family():
    """A leftover OpenAI key cannot misbill a Claude run — stripping it is the
    whole remedy, and refusing would be a false alarm."""
    assert_no_billing_override(_parent(OPENAI_API_KEY=SECRET), "claude")
    assert_no_billing_override(_parent(ANTHROPIC_API_KEY=SECRET), "codex")


def test_assert_no_billing_override_allows_parent_session_markers():
    """Running `collie run --runner codex-exec` from inside a Codex or Claude
    Code terminal must stay possible; those names are stripped, not fatal."""
    assert_no_billing_override(
        _parent(CODEX_THREAD_ID="thr_1", CODEX_HOME=r"C:\Users\dev\.codex"), "codex")
    assert_no_billing_override(
        _parent(CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="cli"), "claude")


def test_assert_no_billing_override_unknown_family_checks_every_vendor():
    """We cannot claim to know which variables an unclassified worker reads."""
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        with pytest.raises(BillingOverrideError):
            assert_no_billing_override(_parent(**{name: SECRET}), "")
    for family in ("collie-sidecar", "brand-new-thing"):
        with pytest.raises(BillingOverrideError):
            assert_no_billing_override(_parent(OPENAI_BASE_URL="http://127.0.0.1:9"), family)


def test_assert_no_billing_override_local_family_has_no_route():
    """A local model has no payer to redirect."""
    assert_no_billing_override(_parent(ANTHROPIC_API_KEY=SECRET, OPENAI_API_KEY=SECRET), "local")


def test_assert_no_billing_override_defaults_to_the_process_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    with pytest.raises(BillingOverrideError):
        assert_no_billing_override(None, "claude")


def test_environment_names_must_be_strings():
    with pytest.raises(ValueError):
        child_env("native", environ={7: "x"})
    with pytest.raises(ValueError):
        assert_no_billing_override({7: "x"}, "claude")

def test_a_claude_code_terminal_does_not_block_its_own_worker():
    """Being started from Claude Code must not refuse a Claude Code worker.

    Claude Code 2.1.221 exports twelve CLAUDE_* variables into any terminal it
    owns -- an entrypoint marker, a version, feature switches, a PID, an IPC
    socket and its token, and the parent's effort.  None of them names an
    account, an endpoint or a key.  A blanket CLAUDE_ prefix rule refused every
    `collie run --runner claude-code` started from inside Claude Code, which is
    precisely the terminal this feature exists to be used from.  They are still
    stripped from the child; only the refusal was wrong.
    """
    session = {
        "CLAUDECODE": "1",
        "CLAUDE_AGENT_SDK_VERSION": "0.2.136",
        "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING": "1",
        "CLAUDE_CODE_ENABLE_TASKS": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_CODE_EXECPATH": "C:/x/claude.js",
        "CLAUDE_CODE_MESSAGING_SOCKET": "pipe-x",
        "CLAUDE_CODE_MESSAGING_TOKEN": "tok-must-not-leak",
        "CLAUDE_CODE_SESSION_ID": "abc",
        "CLAUDE_EFFORT": "high",
        "CLAUDE_PID": "1234",
    }
    parent = _parent(**session)

    assert_no_billing_override(parent, "claude")      # must not raise

    env, receipt = child_env("claude", environ=parent)
    assert not [name for name in env if name.upper().startswith("CLAUDE")]
    for name in session:
        assert name in receipt["stripped"], name
    assert "tok-must-not-leak" not in json.dumps(receipt)


def test_a_real_anthropic_key_is_still_refused():
    """Widening the session exemption must not widen the billing rule."""
    with pytest.raises(BillingOverrideError):
        assert_no_billing_override(_parent(ANTHROPIC_API_KEY=SECRET), "claude")
