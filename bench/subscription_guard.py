"""Fail-closed preflight for subscription-only benchmark runs.

The guard deliberately does not read credential files or values belonging to
provider, proxy, or unrelated environment names.  It checks override *names*,
copies only ordinary process-location variables into a minimal child environment,
then asks the official CLIs for their already-redacted login status.  Successful
calls return a small allowlisted receipt; raw stdout/stderr and tokens are never
copied into it.
"""
from __future__ import annotations

import datetime as _datetime
import json
import math
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from typing import Any


RECEIPT_FORMAT = "collie-subscription-guard-v1"
SCHEMA_VERSION = 1
CODEX_EVIDENCE_MAX_AGE_SECONDS = 15 * 60
_STATUS_OUTPUT_MAX_CHARS = 16 * 1024

_CLAUDE_COMMAND = ("claude", "auth", "status", "--json")
_CODEX_COMMAND = ("codex", "login", "status")

# Presence is enough to deny.  An empty value is still an ambiguous shell
# override, so the operator must unset it before a benchmark can run.  Reject
# every provider-prefixed setting, not just today's known key names: model,
# account, proxy, and credential variables added by a future CLI must fail
# closed until reviewed here.
_FORBIDDEN_ENV_NAMES = frozenset({
    "ALL_PROXY",
    "CURL_CA_BUNDLE",
    "GLOBAL_AGENT_HTTP_PROXY",
    "GLOBAL_AGENT_HTTPS_PROXY",
    "GRPC_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NODE_EXTRA_CA_CERTS",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "NO_PROXY",
    "NPM_CONFIG_HTTPS_PROXY",
    "NPM_CONFIG_PROXY",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
})
_FORBIDDEN_ENV_PREFIXES = ("ANTHROPIC_", "OPENAI_", "CLAUDE_", "CODEX_",
                           "AZURE_OPENAI_")

# The status subprocess receives only ordinary process-location variables.  In
# particular it never inherits unrelated cloud credentials, Node injection
# flags, provider variables, or network proxies from the benchmark process.
_STATUS_CHILD_ENV_NAMES = frozenset({
    "APPDATA", "COMSPEC", "HOME", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA",
    "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERPROFILE",
    "WINDIR",
})


class SubscriptionGuardError(RuntimeError):
    """A denied preflight with a safe, machine-readable receipt."""

    def __init__(self, reason: str, receipt: dict[str, Any]):
        self.reason = reason
        self.receipt = receipt
        # Never append command output, environment values, or caught exceptions.
        super().__init__("subscription benchmark guard denied: %s" % reason)


def _iso_utc(value: _datetime.datetime) -> str:
    return value.astimezone(_datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _checked_at(now_utc: _datetime.datetime | None) -> _datetime.datetime:
    value = now_utc or _datetime.datetime.now(_datetime.timezone.utc)
    if not isinstance(value, _datetime.datetime) or value.tzinfo is None:
        raise ValueError("now_utc must be a timezone-aware datetime")
    if value.utcoffset() != _datetime.timedelta(0):
        raise ValueError("now_utc must be UTC")
    return value.astimezone(_datetime.timezone.utc)


def _base_receipt(provider: str, checked_at: _datetime.datetime) -> dict[str, Any]:
    return {
        "format": RECEIPT_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "provider": provider,
        "checked_at_utc": _iso_utc(checked_at),
        "verdict": "deny",
    }


def _deny(receipt: dict[str, Any], reason: str, **safe_details: Any) -> None:
    receipt["verdict"] = "deny"
    receipt["reason"] = reason
    if safe_details:
        receipt["details"] = safe_details
    raise SubscriptionGuardError(reason, receipt)


def _looks_like_override(name: str) -> bool:
    upper = name.upper()
    return upper in _FORBIDDEN_ENV_NAMES or upper.startswith(_FORBIDDEN_ENV_PREFIXES)


def _check_environment(receipt: dict[str, Any], environ: Mapping[str, str]) -> None:
    # Iterate keys only: secret values are intentionally never fetched.
    found = 0
    for name in environ:
        if not isinstance(name, str):
            _deny(receipt, "environment_name_invalid")
        found += int(_looks_like_override(name))
    if found:
        _deny(receipt, "billing_or_routing_override_present",
              forbidden_environment_name_count=found)
    receipt["environment"] = {
        "override_check": "passed",
        "forbidden_environment_name_count": 0,
        "status_child_environment": "allowlist",
    }


def _status_child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Copy only non-credential process-location values for the status CLI."""
    child = {"NO_COLOR": "1"}
    for name in environ:
        if not isinstance(name, str):
            raise ValueError("environment name must be a string")
        upper = name.upper()
        if upper not in _STATUS_CHILD_ENV_NAMES:
            continue
        value = environ[name]
        if not isinstance(value, str):
            raise ValueError("environment value must be a string")
        child[upper] = value
    return child


def _default_runner(argv: tuple[str, ...], environ: Mapping[str, str]
                    ) -> subprocess.CompletedProcess[str]:
    # npm-installed CLIs are commonly ``.cmd`` shims on Windows.  Resolve the
    # executable without invoking a shell so status checks stay non-injectable.
    child_env = _status_child_environment(environ)
    executable = shutil.which(argv[0], path=child_env.get("PATH", os.defpath))
    if not executable:
        raise FileNotFoundError(argv[0])
    command = [executable, *argv[1:]]
    return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=10, check=False, env=child_env)


def _run_status(receipt: dict[str, Any], argv: tuple[str, ...],
                runner: Callable[[tuple[str, ...]], Any]) -> str:
    try:
        result = runner(argv)
        returncode = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except Exception:
        _deny(receipt, "auth_status_command_failed", command=list(argv))
    if type(returncode) is not int or returncode != 0:
        _deny(receipt, "auth_status_command_failed", command=list(argv))
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        _deny(receipt, "auth_status_output_invalid", command=list(argv))
    if (len(stdout) > _STATUS_OUTPUT_MAX_CHARS
            or len(stderr) > _STATUS_OUTPUT_MAX_CHARS
            or "\x00" in stdout or "\x00" in stderr):
        _deny(receipt, "auth_status_output_invalid", command=list(argv))
    # Codex 0.146 writes its already-redacted `Logged in using ChatGPT` status to stderr, while
    # Claude writes JSON to stdout.  Accept exactly one populated channel; the provider-specific
    # parser still requires an exact allowlisted payload, and ambiguous dual-channel output fails
    # closed.  Neither raw channel is ever copied into a receipt or exception.
    populated = [value for value in (stdout, stderr) if value.strip()]
    if len(populated) != 1:
        _deny(receipt, "auth_status_output_invalid", command=list(argv))
    return populated[0]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate object key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite number")


def _json_object(receipt: dict[str, Any], text: str) -> dict[str, Any]:
    try:
        value = json.loads(text, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError, RecursionError):
        _deny(receipt, "auth_status_output_invalid", command=list(_CLAUDE_COMMAND))
    if not isinstance(value, dict):
        _deny(receipt, "auth_status_output_invalid", command=list(_CLAUDE_COMMAND))
    return value


_MISSING = object()


def _at(value: Mapping[str, Any], *path: str) -> Any:
    current: Any = value
    for part in path:
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _semantic_value(receipt: dict[str, Any], value: Mapping[str, Any],
                    paths: tuple[tuple[str, ...], ...], field: str,
                    normalize: Callable[[Any], str]) -> str | None:
    """Return one normalized semantic value, rejecting conflicting aliases."""
    found = [_at(value, *path) for path in paths]
    normalized = [normalize(item) for item in found if item is not _MISSING]
    if not normalized:
        return None
    if len(set(normalized)) != 1:
        _deny(receipt, "claude_auth_status_ambiguous", field=field)
    return normalized[0]


def _contains_credential_field(value: Any) -> bool:
    """Inspect JSON key names only; never copy a possibly secret value."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, Mapping):
            for key, child in item.items():
                compact = re.sub(r"[^a-z0-9]", "", key.lower())
                if any(marker in compact for marker in (
                        "apikey", "authorization", "cookie", "credential", "password",
                        "secret", "token")):
                    return True
                pending.append(child)
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _claude_status_shape_allowed(value: Mapping[str, Any]) -> bool:
    top_level = {
        "account", "apiProvider", "api_provider", "auth", "authenticated",
        "authMethod", "auth_method", "email", "loggedIn", "logged_in",
        "loginMethod", "orgId", "orgName", "organizationId", "organizationName",
        "plan", "planType", "subscriptionType", "subscription_type", "tier",
    }
    if not set(value).issubset(top_level):
        return False
    nested = {
        "auth": {"apiProvider", "loggedIn", "method", "provider"},
        "account": {"plan", "subscriptionType"},
    }
    for field, allowed in nested.items():
        if field not in value:
            continue
        child = value[field]
        if not isinstance(child, Mapping) or not set(child).issubset(allowed):
            return False
    return True


def _bool_status(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "invalid"


def _compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower()) if isinstance(value, str) else ""


def _claude_plan(value: Any) -> str:
    compact = _compact(value)
    if compact in {"pro", "claudepro"}:
        return "pro"
    if compact in {"max", "claudemax", "max5x", "max20x",
                   "claudemax5x", "claudemax20x"}:
        return "max"
    return ""


def _check_claude(receipt: dict[str, Any],
                  runner: Callable[[tuple[str, ...]], Any]) -> None:
    status = _json_object(receipt, _run_status(receipt, _CLAUDE_COMMAND, runner))
    if _contains_credential_field(status):
        _deny(receipt, "auth_status_contains_credential_fields")
    if not _claude_status_shape_allowed(status):
        _deny(receipt, "claude_auth_status_fields_invalid")
    logged_in = _semantic_value(
        receipt, status,
        (("loggedIn",), ("logged_in",), ("authenticated",),
         ("auth", "loggedIn")),
        "logged_in", _bool_status)
    method = _semantic_value(
        receipt, status,
        (("authMethod",), ("auth_method",), ("loginMethod",),
         ("auth", "method")),
        "auth_method", _compact)
    provider = _semantic_value(
        receipt, status,
        (("apiProvider",), ("api_provider",), ("auth", "apiProvider"),
         ("auth", "provider")),
        "api_provider", _compact)
    plan = _semantic_value(
        receipt, status,
        (("subscriptionType",), ("subscription_type",), ("plan",),
         ("planType",), ("tier",), ("account", "subscriptionType"),
         ("account", "plan")),
        "plan", _claude_plan)
    if logged_in != "true":
        _deny(receipt, "claude_not_logged_in")
    if method != "claudeai":
        _deny(receipt, "claude_auth_method_not_claude_ai")
    if provider != "firstparty":
        _deny(receipt, "claude_api_provider_not_first_party")
    if not plan:
        _deny(receipt, "claude_plan_not_pro_or_max")
    receipt["auth"] = {
        "status": "authenticated",
        "method": "claude.ai",
        "api_provider": "firstParty",
        "plan": plan,
    }


def _parse_utc_timestamp(receipt: dict[str, Any], value: Any) -> _datetime.datetime:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _deny(receipt, "codex_account_evidence_timestamp_invalid")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = _datetime.datetime.fromisoformat(text)
    except ValueError:
        _deny(receipt, "codex_account_evidence_timestamp_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() != _datetime.timedelta(0):
        _deny(receipt, "codex_account_evidence_timestamp_not_utc")
    return parsed.astimezone(_datetime.timezone.utc)


def _check_codex_evidence(receipt: dict[str, Any], evidence: Mapping[str, Any] | None,
                          now: _datetime.datetime) -> None:
    if not isinstance(evidence, Mapping):
        _deny(receipt, "codex_account_evidence_required")
    required_fields = {"credits_remaining", "auto_reload", "observed_at_utc"}
    fields = list(evidence)
    if (any(not isinstance(field, str) for field in fields)
            or set(fields) != required_fields):
        _deny(receipt, "codex_account_evidence_fields_invalid")
    credits = evidence.get("credits_remaining")
    if (not isinstance(credits, (int, float)) or isinstance(credits, bool)
            or not math.isfinite(float(credits)) or float(credits) != 0.0):
        _deny(receipt, "codex_credits_remaining_must_be_zero")
    if evidence.get("auto_reload") is not False:
        _deny(receipt, "codex_auto_reload_must_be_false")
    observed = _parse_utc_timestamp(receipt, evidence.get("observed_at_utc"))
    if observed > now:
        _deny(receipt, "codex_account_evidence_from_future")
    expires = observed + _datetime.timedelta(seconds=CODEX_EVIDENCE_MAX_AGE_SECONDS)
    if now > expires:
        _deny(receipt, "codex_account_evidence_expired")
    # Copy only the three approved facts; ignore every other caller-supplied field.
    receipt["account_evidence"] = {
        "credits_remaining": 0,
        "auto_reload": False,
        "observed_at_utc": _iso_utc(observed),
        "expires_at_utc": _iso_utc(expires),
    }


def _check_codex(receipt: dict[str, Any], evidence: Mapping[str, Any] | None,
                 now: _datetime.datetime,
                 runner: Callable[[tuple[str, ...]], Any]) -> None:
    # Check caller evidence first so stale/unsafe state does not even invoke a CLI.
    _check_codex_evidence(receipt, evidence, now)
    status = _run_status(receipt, _CODEX_COMMAND, runner).strip()
    # Codex 0.146's exact redacted status.  Do not accept prose which merely
    # contains "ChatGPT"; an API-key or custom-provider line must never pass.
    if status != "Logged in using ChatGPT":
        _deny(receipt, "codex_login_not_chatgpt")
    receipt["auth"] = {"status": "authenticated", "method": "ChatGPT"}


def check_subscription_guard(
        provider: str, *,
        account_evidence: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
        runner: Callable[[tuple[str, ...]], Any] | None = None,
        now_utc: _datetime.datetime | None = None) -> dict[str, Any]:
    """Authorize one official-CLI subscription benchmark invocation.

    ``provider`` accepts ``claude``/``claude-code`` or ``codex``/``codex-cli``.
    Codex callers must pass freshly observed account evidence with exactly the
    required safety facts.  On denial, :class:`SubscriptionGuardError` carries
    the same redacted receipt shape that can be persisted as audit evidence.
    """
    aliases = {
        "claude": "claude-code",
        "claude-code": "claude-code",
        "codex": "codex-cli",
        "codex-cli": "codex-cli",
    }
    canonical = aliases.get(provider.strip().lower()) if isinstance(provider, str) else None
    try:
        checked_at = _checked_at(now_utc)
    except (TypeError, ValueError):
        receipt = _base_receipt(
            canonical or "unsupported", _datetime.datetime.now(_datetime.timezone.utc))
        _deny(receipt, "check_time_invalid")
    receipt = _base_receipt(canonical or "unsupported", checked_at)
    if canonical is None:
        _deny(receipt, "unsupported_provider")
    source_environ = os.environ if environ is None else environ
    if not isinstance(source_environ, Mapping):
        _deny(receipt, "environment_invalid")
    _check_environment(receipt, source_environ)
    command_runner = runner or (
        lambda argv: _default_runner(argv, source_environ))
    if canonical == "claude-code":
        _check_claude(receipt, command_runner)
    else:
        _check_codex(receipt, account_evidence, checked_at, command_runner)
    receipt["verdict"] = "allow"
    receipt.pop("reason", None)
    receipt.pop("details", None)
    return receipt


__all__ = [
    "CODEX_EVIDENCE_MAX_AGE_SECONDS",
    "RECEIPT_FORMAT",
    "SCHEMA_VERSION",
    "SubscriptionGuardError",
    "check_subscription_guard",
]
