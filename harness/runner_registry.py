"""The closed set of workers Collie can hand a task to, and what is true of each here.

``runner_specs`` says what a runner *is*; the runners themselves say how to drive
one; this module is the hub in between.  It owns three things and deliberately
nothing else:

* **The closed table.**  :data:`SPECS` is a ``MappingProxyType``, not a plugin
  registry.  Every key that exists here was reviewed — its billing route, its
  approval story, its sandbox — and a key that has not been reviewed cannot be
  added at runtime.  Keys that arrive in a later phase are already listed, with
  ``phase`` set, so ``collie runners`` can show what is coming without the
  selector ever being able to hand one of them real work: their probes report
  ``usable() == False`` and a detail starting with
  :data:`~harness.runner_specs.NOT_IMPLEMENTED_PREFIX`.
* **The probe.**  :func:`probe` answers "is this worker installed, logged in, and
  on which billing route", for one host, right now.  Two rules shape it.  It
  never touches the network — not even on the ``live`` path, which only runs an
  official CLI's own already-redacted status command.  And it never reads a
  credential *value*: a login is classified by which file exists, which kind of
  login that file describes, and when it expires.  A probe that cannot answer
  says ``unknown`` and stays unusable; it never guesses "probably fine", because
  the whole point of the billing rules downstream (H5) is that an unevidenced
  route is refused rather than optimistically charged.
* **The construction.**  :func:`make_runner` turns a key into a live runner
  object.  ``collie`` is deliberately not constructible here: it is not an
  external worker, it is the harness this process already is.

Results are cached for 60 seconds per (key, live, provider).  A probe costs a
``which`` plus a ``--version`` subprocess, and the CLI, the web capabilities
endpoint and the selector all ask for the same rows within one keystroke; the
TTL is short enough that ``codex login`` in another terminal is visible almost
immediately, and long enough that one page render does not fork six processes.

The default ``RUNNER=collie`` path must not pay for any of this.  Nothing here
runs at import time, and ``cmd_run`` only asks for the probes of the candidates
it actually has (``request_from_run`` returns just ``("collie",)`` by default),
so a normal run never probes an external CLI at all.
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import shutil
import threading
import time
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any, Callable

from . import __version__, claude_code_runner, plat, subscription_guard
from .agent_runners import CodexExecRunner, _display_path
from .codex_app_server_runner import CodexAppServerRunner
from .codex_sdk_runner import CodexSdkRunner
from .pi_rpc_runner import PiRpcRunner
from .runner_specs import (
    BILLING_MODE_OF,
    CURRENT_PHASE,
    HarnessSpec,
    NOT_IMPLEMENTED_PREFIX,
    RunnerCapabilities,
    CapabilityHandshake,
    RunnerProbe,
    RunnerUnavailableError,
    family_of_provider,
)

# How long a probe row stays fresh.  Also the ``ttl_s`` written into every probe,
# so a caller that persists one can tell how stale it is without asking us.
PROBE_TTL_S = 60.0


def _reject_json_constant(value: str) -> Any:
    """Reject JavaScript numeric extensions at every metadata trust boundary."""
    raise ValueError("non-standard JSON constant: %s" % value)


# --- declared capabilities --------------------------------------------------
# `claude-code` declares its own capabilities in `claude_code_runner`, next to the
# code that would have to implement any of them.  The rest are declared here
# because their runners either predate this layer (`CodexExecRunner`) or do not
# exist yet — a phase-2/3 key must still be able to show an honest capability row
# in `collie runners` before anyone writes its transport.
COLLIE_CAPABILITIES = RunnerCapabilities(
    protocol="collie-native",
    protocol_version=__version__,
    session_create=True,
    session_resume=True,
    session_fork=False,
    streaming=True,
    cursor_replay=False,
    steer=True,
    follow_up=True,
    cancel="native+process-tree",
    approval_round_trip=True,       # the gate IS the approval channel
    usage_tokens=True,
    usage_cost=True,
    quota_signals=False,            # phase 2, once provider rate-limit headers are collected
    request_gate=True,              # mission.reserve_model_request — collie only
    native_goal=False,
    native_scheduler=False,
    # `confinement` describes a *sandbox*, and Collie's boundary is not one: it is
    # the gate plus the Mission leash, which decide per action rather than per
    # process.  H9 only consults this field when a worker has no approval
    # round-trip, which Collie does, so "none" here costs nothing and claiming a
    # sandbox Collie does not have would cost a great deal.
    confinement="none",
    tools=frozenset({"code", "bash", "browser", "desktop", "mcp",
                     "web_search", "email", "slack"}),
    needs_git_workspace=False,      # collie works in any directory; git only improves evidence
    # The three transports in this field name ways to hand a prompt to a *child
    # process*.  Collie has no child to hand it to — the harness runs in this
    # process — so none of them is the truth and "native" says so plainly.
    prompt_transport="native",
    windows_native=True,            # shipped and tested on all three platforms
)

CODEX_EXEC_CAPABILITIES = RunnerCapabilities(
    protocol="codex-exec-jsonl",
    protocol_version="",            # pinned by `collie runners compat`, not by hand
    session_create=True,
    session_resume=True,            # `codex exec resume <thread_id>`
    session_fork=False,
    streaming=True,                 # complete exec --json records arrive live
    cursor_replay=False,
    steer=False,                    # a new instruction means the next resume
    follow_up=False,
    cancel="process-tree",
    # `codex exec` answers every approval request with reject_server_request, so
    # there is no channel back into Collie's gate.  The sandbox is what makes the
    # runner offerable for shell work at all (H9).
    approval_round_trip=False,
    usage_tokens=True,              # turn.completed.usage, accumulated by _merge_usage
    usage_cost=False,               # Codex reports tokens, never dollars
    # `exec` itself has no quota frame; the selector performs the app-server's
    # read-only account/rateLimits/read handshake as a companion signal.
    quota_signals=True,
    request_gate=False,
    native_goal=False,              # `exec` has no goals surface; app-server does
    native_scheduler=False,
    confinement="workspace-write",
    tools=frozenset({"code", "bash"}),
    needs_git_workspace=True,
    prompt_transport="stdin",
    capability_handshake=True,
    windows_native=None,            # installed here, but no turn has been verified yet
)

# --- phase 2 and phase 3 declarations --------------------------------------
# App Server is implemented by ``codex_app_server_runner``.  Phase-3 values are
# declarations only; their probes stay fail-closed until those adapters pass the
# same conformance matrix.
CODEX_APP_SERVER_CAPABILITIES = RunnerCapabilities(
    protocol="codex-appserver-jsonrpc", session_create=True, session_resume=True,
    streaming=True, steer=True, cancel="native+process-tree",
    approval_round_trip=True,       # item/*/requestApproval -> Collie's gate
    usage_tokens=False,             # turn/completed usage shape unverified
    quota_signals=True,             # the only runner that reports quota
    # The server binary has a goal surface, but this adapter neither initializes nor
    # calls it.  Capabilities describe the adapter Collie can reach, not every method
    # the child happens to implement; the conformance test proves those methods stay
    # absent from the wire.
    native_goal=False,
    confinement="workspace-write", tools=frozenset({"code", "bash"}),
    prompt_transport="rpc", interactions=frozenset({"approval"}),
    capability_handshake=True, windows_native=None)

CODEX_SDK_CAPABILITIES = RunnerCapabilities(
    protocol="openai-codex-python-sdk-sidecar",
    session_create=True, session_resume=True, session_fork=True,
    # The sanitized single-request sidecar is for background work.  Interactive
    # streaming/steering stays on codex-app-server, whose owned stdio transport
    # can keep a bidirectional turn alive.
    streaming=False, steer=False, follow_up=False,
    cancel="process-tree", approval_round_trip=False,
    usage_tokens=True, usage_cost=False, quota_signals=True,
    confinement="workspace-write", tools=frozenset({"code", "bash"}),
    prompt_transport="sdk-sidecar",
    inputs=frozenset({"text", "image_url", "image_file"}),
    compact=True, capability_handshake=True, windows_native=None)

PI_RPC_CAPABILITIES = RunnerCapabilities(
    protocol="pi-rpc-lfjsonl", session_create=True, session_resume=True,
    session_fork=True, streaming=True, steer=True, follow_up=True,
    cancel="native+process-tree", approval_round_trip=False,
    usage_tokens=True, usage_cost=True,
    confinement="tools-allowlist", tools=frozenset({"code"}),
    prompt_transport="rpc", inputs=frozenset({"text"}), compact=True,
    interactions=frozenset({"user_input", "select", "confirm"}),
    capability_handshake=True, windows_native=None)

PRIME_RPC_CAPABILITIES = RunnerCapabilities(
    protocol="prime-rpc-lfjsonl", session_create=True,
    session_resume=False,           # --no-session: a daemon would own the session
    streaming=True, steer=True, cancel="native+process-tree",
    approval_round_trip=False, usage_tokens=False,
    native_scheduler=True,          # daemon/heartbeat/schedule exist (H11)
    confinement="container", tools=frozenset({"code", "bash"}),
    prompt_transport="rpc", windows_native=False)

HERMES_GATEWAY_CAPABILITIES = RunnerCapabilities(
    protocol="hermes-gateway-jsonrpc", session_create=True, session_resume=True,
    session_fork=True, streaming=True, steer=True, follow_up=True,
    cancel="native+process-tree",
    approval_round_trip=True,       # approval.request -> approval.respond
    usage_tokens=True, confinement="container", tools=frozenset({"code", "bash"}),
    prompt_transport="rpc", inputs=frozenset({"text", "image_file"}),
    compact=True,
    interactions=frozenset({"approval", "user_input", "select", "confirm"}),
    capability_handshake=True, windows_native=None)

HERMES_ACP_CAPABILITIES = RunnerCapabilities(
    protocol="hermes-acp-jsonrpc", session_create=True, session_resume=True,
    streaming=True, steer=False, cancel="native+process-tree",
    approval_round_trip=True,       # session/request_permission
    usage_tokens=True, confinement="container", tools=frozenset({"code", "bash"}),
    prompt_transport="rpc", windows_native=False)


# --- the closed table -------------------------------------------------------
_SPECS: dict[str, HarnessSpec] = {
    "collie": HarnessSpec(
        key="collie",
        label="Collie's own harness",
        kind="native",
        binary="",                  # in-process: there is no CLI to resolve
        credential_family="",       # follows the configured PROVIDER
        caps=COLLIE_CAPABILITIES,
        env_policy="native",
        guard_alias="",             # the existing _subscription_preflight owns this route
        phase=1,
        default_timeout_s=900.0,
    ),
    "codex-exec": HarnessSpec(
        key="codex-exec",
        label="OpenAI Codex CLI (codex exec)",
        kind="external",
        binary="codex",
        version_argv=("codex", "--version"),
        # The only build the argv and the event dialect were diffed against.  An
        # older CLI is rejected with the version in the message rather than
        # failing halfway through a turn on a flag it never had.
        min_version="0.149.0",
        credential_family="codex",
        caps=CODEX_EXEC_CAPABILITIES,
        env_policy="codex",
        guard_alias="codex-cli",
        phase=1,
        default_timeout_s=900.0,
        notes=(
            "codex exec rejects every approval request (fail-closed); shell work "
            "is bounded by --sandbox workspace-write, not by Collie's gate",
            "complete exec --json events stream live; partial token deltas are "
            "not emitted by this CLI protocol",
            "Windows: writes are refused intermittently when launched through "
            "Collie's process-tree owner even with the sandbox overrides in "
            "place (1 of 4 runs succeeded, 2026-08-22 / 0.149.0). The turn still "
            "exits 0, so trust the verification gate, not the worker's summary",
        ),
    ),
    "claude-code": HarnessSpec(
        key="claude-code",
        label="Claude Code (claude -p)",
        kind="external",
        binary=claude_code_runner.BINARY,
        version_argv=(claude_code_runner.BINARY, "--version"),
        min_version="2.1.221",
        credential_family=claude_code_runner.CREDENTIAL_FAMILY,
        # Declared by the module that implements it — one source, not a copy.
        caps=claude_code_runner.CAPABILITIES,
        env_policy=claude_code_runner.ENV_POLICY,
        guard_alias="claude-code",
        phase=1,
        default_timeout_s=900.0,
        notes=(
            "claude --max-turns: unverified (absent from `claude --help` 2.1.221; never passed)",
            'claude --setting-sources "": unverified',
            "no shell: the tool allowlist is Read,Edit,Write,Grep,Glob because there "
            "is no approval channel back into Collie's gate",
        ),
    ),
    "codex-sdk": HarnessSpec(
        key="codex-sdk",
        label="OpenAI Codex Python SDK (isolated sidecar)",
        kind="external",
        binary="openai_codex",
        version_argv=(),
        credential_family="codex",
        caps=CODEX_SDK_CAPABILITIES,
        env_policy="codex",
        guard_alias="codex-cli",
        phase=2,
        default_timeout_s=900.0,
        notes=(
            "optional install: pip install collie-harness[codex]",
            "official SDK pinned runtime; SDK is hosted in a sanitized child process",
            "multimodal input, native thread resume/fork, compaction, and token usage",
            "background adapter denies all approvals; use codex-app-server for interactive approval/steer",
        ),
    ),
    "codex-app-server": HarnessSpec(
        key="codex-app-server",
        label="OpenAI Codex app-server (JSON-RPC)",
        kind="external",
        binary="codex",
        version_argv=("codex", "--version"),
        min_version="0.149.0",
        credential_family="codex",
        caps=CODEX_APP_SERVER_CAPABILITIES,
        env_policy="codex",
        guard_alias="codex-cli",
        phase=2,
        notes=(
            "Codex documents app-server as experimental; this adapter pins local stdio and runs conformance before trust",
            "experimental WebSocket transport is not used",
            "host MCP/plugins/web/hooks/memory/multi-agent/project instructions are disabled",
            "approval requests round-trip to a callback and fail closed to decline",
            "thread/goal/* is a second control plane and is never called",
            "turn/completed usage shape: unverified",
        ),
    ),
    "pi-rpc": HarnessSpec(
        key="pi-rpc",
        label="Pi (pi --mode rpc)",
        kind="external",
        binary="pi",
        version_argv=("pi", "--version"),
        credential_family="collie-sidecar",
        caps=PI_RPC_CAPABILITIES,
        env_policy="sidecar-harness",
        guard_alias="",             # billing route is Pi's own; unevidenced here
        phase=2,
        notes=(
            "bash is disabled; explicit tools: read,edit,write,grep,find,ls",
            "extensions, skills, prompt templates, project context files, and project trust prompts are disabled",
            "native RPC steer/follow-up/abort, sessions, fork, compaction, usage and cost",
            "no tool-level approval exists (--no-approve is project trust, not review)",
            "the Claude route bills as extra usage: refused under no-paid-overage",
        ),
    ),
    "prime-rpc": HarnessSpec(
        key="prime-rpc",
        label="Prime (prime-agent --mode rpc)",
        kind="external",
        binary="prime-agent",
        version_argv=("prime-agent", "--version"),
        credential_family="collie-sidecar",
        caps=PRIME_RPC_CAPABILITIES,
        env_policy="sidecar-harness",
        guard_alias="",
        phase=3,
        notes=(
            "container only",
            "--no-session is kept: a resumable session would be owned by Prime's daemon",
            "prime get_session_stats fields: unverified",
        ),
    ),
    "hermes-gateway": HarnessSpec(
        key="hermes-gateway",
        label="Hermes (tui_gateway JSON-RPC)",
        kind="external",
        binary="hermes",
        version_argv=("hermes", "--version"),
        credential_family="collie-sidecar",
        caps=HERMES_GATEWAY_CAPABILITIES,
        env_policy="sidecar-harness",
        guard_alias="",
        phase=3,
        notes=(
            "wire adapter implemented; real launch requires an explicit docker/podman/nerdctl command",
            "bare gateway launch is refused because it inherits Hermes plugins, skills, MCP, schedulers, secrets, and shell",
            "sudo and secret requests are always answered empty; approval defaults to deny",
            "runtime session ids rotate; receipts retain only stored_session_id",
            "Hermes' own BillingRoute is not Collie evidence: billing class stays unknown",
        ),
    ),
    "hermes-acp": HarnessSpec(
        key="hermes-acp",
        label="Hermes ACP (hermes acp)",
        kind="external",
        binary="hermes",
        version_argv=("hermes", "--version"),
        credential_family="collie-sidecar",
        caps=HERMES_ACP_CAPABILITIES,
        env_policy="sidecar-harness",
        guard_alias="",
        phase=3,
        notes=(
            "fallback for hermes-gateway; needs the hermes[acp] extra, absent on this host",
            "set_session_model must never be triggered by a worker",
        ),
    ),
}

# Closed on purpose: `SPECS[key]` is a lookup, never a registration point.  A new
# worker is a reviewed edit to this file, not a runtime plugin.
SPECS: Mapping[str, HarnessSpec] = MappingProxyType(_SPECS)


# --- compat report ----------------------------------------------------------
# Which conformance column speaks for which declared capability.  Only columns
# whose failure has an unambiguous capability meaning are listed: `framing` says
# a chaotic stream does not crash the runner, which is not the same claim as
# `streaming`, and guessing the mapping would silently take a working capability
# away.  Unlisted columns still gate the "verified <date>" badge.
_CHECK_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "one_turn": ("session_create",),
    "resume": ("session_resume",),
    "cancel": ("cancel",),
    "usage": ("usage_tokens", "usage_cost"),
    "approval": ("approval_round_trip",),
}

# Columns whose PASS on a Windows host is what makes `windows_native` true.
_WINDOWS_EVIDENCE = ("one_turn", "handshake")

_PASS = "PASS"
_DOWNGRADING = ("FAIL", "UNVERIFIED")

# key -> {"date": str, "downgraded": tuple[str, ...], "verified": bool,
#         "windows_native": bool | None}
_COMPAT: dict[str, dict[str, Any]] = {}

_LOCK = threading.RLock()
# (key, live, provider) -> RunnerProbe
_CACHE: dict[tuple[str, bool, str], RunnerProbe] = {}


# Where `collie runners compat` leaves a report for the selector to read back.
# Without a standing location the conformance loop only half closes: the matrix
# writes a file, nobody folds it in, and `windows_native` stays None forever --
# which H10 reads as "unverified", so Auto refuses every external worker on
# Windows no matter what the pool says.
COMPAT_REPORT_NAME = "runner-compat.json"
_AUTOLOADED = False


def default_compat_report_path() -> str:
    """The path `collie runners compat` writes to and the registry reads back."""
    state = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
    return os.path.join(state, COMPAT_REPORT_NAME)


def autoload_compat_report() -> dict[str, tuple[str, ...]]:
    """Fold in the host's stored report once per process; never raise.

    Called from :func:`probe`, so every consumer -- `collie runners`, the
    selector, the web surface -- sees the same evidence without having to
    remember to load it.  A missing file is the normal state of a fresh install
    and is silent.  A *corrupt* file is also swallowed here, unlike the explicit
    `apply_compat_report(path)` call: a broken file in the standing location must
    not take down every `collie run`, and the operator still gets a loud error
    the moment they point the command at it by hand.
    """
    global _AUTOLOADED
    with _LOCK:
        if _AUTOLOADED:
            return {}
        _AUTOLOADED = True
    try:
        return apply_compat_report(default_compat_report_path())
    except Exception:
        return {}


def reset_cache() -> None:
    """Drop every cached probe row (tests, and after a compat report is applied)."""
    global _AUTOLOADED
    with _LOCK:
        _CACHE.clear()
        _AUTOLOADED = False


def _report_rows(report: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Normalize a compat report into ``{runner: {check: STATUS}}``.

    The report is written by ``runner_compat`` and edited by nobody, but it is
    still data from another tool and a shape change there must not take the whole
    registry down.  Both spellings this repo uses are accepted: a mapping of
    runner key to row, and a list of rows carrying their own key.
    """
    raw = report.get("runners")
    if raw is None:
        raw = report.get("results") or {}
    rows: dict[str, dict[str, str]] = {}
    items: list[tuple[str, Any]] = []
    if isinstance(raw, Mapping):
        items = [(str(key), value) for key, value in raw.items()]
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, Mapping):
                key = str(entry.get("runner") or entry.get("key") or "")
                if key:
                    items.append((key, entry))
    for key, value in items:
        if not isinstance(value, Mapping):
            continue
        checks = value.get("checks")
        if not isinstance(checks, Mapping):
            checks = value.get("results")
        if not isinstance(checks, Mapping):
            continue
        row: dict[str, str] = {}
        for name, result in checks.items():
            # A column is either a bare status string or an object carrying one
            # alongside timing and an error summary.
            status = result.get("status") if isinstance(result, Mapping) else result
            row[str(name)] = str(status or "").strip().upper()
        rows[key] = row
    return rows


def _report_is_windows(report: Mapping[str, Any]) -> bool | None:
    """Did this report come from a Windows host?  ``None`` when it does not say."""
    for name in ("os_name", "os", "platform", "system"):
        value = report.get(name)
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("os_name") or value.get("system")
        if isinstance(value, str) and value.strip():
            lowered = value.strip().lower()
            if lowered in ("nt", "windows", "win32", "win"):
                return True
            return False
    return None


def apply_compat_report(path: str) -> dict[str, tuple[str, ...]]:
    """Fold the last conformance report into every future probe.

    A declared capability is a claim; this is the only thing that turns it into
    evidence.  Any column recorded ``FAIL`` **or** ``UNVERIFIED`` downgrades the
    capabilities it speaks for to False (``cancel`` to ``"none"``), so a worker
    that has never been shown to resume on this host is not offered to the
    selector as resumable.  Downgrades only — a report can never talk a
    capability *up* — with one deliberate exception, ``windows_native``, which
    :class:`~harness.runner_specs.RunnerCapabilities` documents as the field the
    compat report decides: a Windows report whose handshake and one-turn columns
    passed sets it True, and a Windows report that failed either sets it False.

    Returns ``{runner: (downgraded capability names, ...)}`` for the caller to
    show.  A path that does not exist returns ``{}`` and changes nothing — never
    having run the matrix is the normal state of a fresh install — but a file
    that exists and cannot be parsed raises, because silently ignoring the report
    someone just pointed us at is how an unverified capability gets believed.
    """
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            report = json.load(handle, parse_constant=_reject_json_constant)
        if not isinstance(report, dict):
            raise ValueError("compat report is not a JSON object")
    except (OSError, ValueError) as exc:
        raise ValueError("could not read compat report %s: %s: %s"
                         % (_display_path(path), type(exc).__name__, exc)) from None

    date = str(report.get("date") or report.get("generated_at_utc")
               or report.get("generated_at") or "")[:32]
    on_windows = _report_is_windows(report)
    applied: dict[str, tuple[str, ...]] = {}
    with _LOCK:
        for key, row in _report_rows(report).items():
            if key not in SPECS:
                continue            # a report from a newer Collie: ignore, do not crash
            downgraded: set[str] = set()
            for check, status in row.items():
                if status in _DOWNGRADING:
                    downgraded.update(_CHECK_CAPABILITIES.get(check, ()))
            windows_native: bool | None = None
            if on_windows:
                evidence = [row[name] for name in _WINDOWS_EVIDENCE if name in row]
                if evidence:
                    windows_native = all(status == _PASS for status in evidence)
            _COMPAT[key] = {
                "date": date,
                "downgraded": tuple(sorted(downgraded)),
                # "verified" means the matrix actually ran here and something
                # passed — a row of SKIPs is not a verification.
                # A later-phase declaration may pass only the read-only adapter
                # admission fingerprint.  That is useful evidence, but never a
                # compatibility badge and never a reason to make it selectable.
                "verified": (SPECS[key].phase <= CURRENT_PHASE and
                             any(status == _PASS for status in row.values())),
                "windows_native": windows_native,
            }
            applied[key] = _COMPAT[key]["downgraded"]
        _CACHE.clear()              # cached rows carry the old capability view
    return applied


def compat_status(key: str) -> str:
    """``"verified <date>"`` when the matrix ran here for ``key``, else ``"unverified"``.

    Loads the host's stored report on first use exactly like :func:`probe`, so a
    caller that only wants the status line does not have to probe first to get a
    truthful one.
    """
    autoload_compat_report()
    entry = _COMPAT.get(key)
    if not entry or not entry.get("verified"):
        return "unverified"
    date = entry.get("date") or ""
    return ("verified %s" % date).strip()


def _capabilities_for(spec: HarnessSpec) -> dict[str, Any]:
    """Declared capabilities ∧ the last compat report — the only view probes show."""
    caps = spec.caps.to_dict()
    entry = _COMPAT.get(spec.key)
    if not entry:
        return caps
    for name in entry.get("downgraded", ()):
        if name == "cancel":
            caps["cancel"] = "none"
        elif isinstance(caps.get(name), bool):
            caps[name] = False
    windows_native = entry.get("windows_native")
    if windows_native is not None:
        caps["windows_native"] = bool(windows_native)
    return caps


# --- billing ----------------------------------------------------------------
def _configured_provider() -> str:
    """The Brain the native harness would run on, read the way every entry point does."""
    from . import settings
    return settings.get("PROVIDER", "anthropic") or ""


def _collie_billing(provider: str) -> tuple[str, dict[str, Any]]:
    """Which class pays for a native run, on ``missionweb._billing_mode``'s terms.

    That function (missionweb.py:122-128) is the durable definition — a Mission
    row already records its four values — but importing missionweb drags in the
    whole Mission service, so the mapping is reproduced through
    ``runner_specs.family_of_provider``, whose provider table mirrors
    ``_SUBSCRIPTION_PROVIDERS``/``_LOCAL_PROVIDERS`` name for name.  The
    projection back through ``BILLING_MODE_OF`` returns exactly the same four
    strings, and ``tests/test_runner_registry.py`` pins the two against each
    other so a provider added to one list cannot drift from the other.
    """
    family = family_of_provider(provider)
    evidence: dict[str, Any] = {"source": "settings:PROVIDER", "provider": provider,
                                "credential_family": family}
    if not family:
        return "unknown", {"source": "settings:PROVIDER", "provider": ""}
    if family in ("claude", "codex"):
        return "subscription_allowance", evidence
    if family == "local":
        return "local", evidence
    return "api_metered", evidence


# --- login state (metadata only) --------------------------------------------
def _claude_credentials_path() -> str:
    """Where Claude Code keeps its OAuth blob on this host, if it keeps a file."""
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(home, ".credentials.json")


def _claude_login_state(now: float) -> tuple[str, str, dict[str, Any]]:
    """Classify the Claude Code login from file metadata; no value is ever read.

    ``ClaudeCodeRunner.probe`` deliberately stops at ``which`` + ``--version``:
    the runner's job is to drive the CLI, not to have an opinion about logins.
    The registry is where the selector's H3 rule reads its login state, so the
    file check lives here — and it is a *shape* check.  ``claudeAiOauth.expiresAt``
    is an ordinary millisecond timestamp; the tokens beside it are never copied
    out of the parsed object, never returned, and never logged.

    macOS is the case that makes this careful: Claude Code stores the same blob in
    the login Keychain and writes no file at all (``providers.claude_credentials``),
    so a missing file there is not evidence of a missing login — it is a reason to
    say ``unknown`` and let ``--live`` answer.
    """
    path = _claude_credentials_path()
    shown = _display_path(path)
    evidence: dict[str, Any] = {"source": "file:%s" % shown}
    if not os.path.isfile(path):
        if plat.is_macos():
            evidence["login_kind"] = "keychain-or-none"
            return ("unknown",
                    "no Claude Code credential file at %s; on macOS the login lives in "
                    "the Keychain, so run `collie runners probe claude-code --live` to "
                    "settle it" % shown, evidence)
        evidence["login_kind"] = "none"
        return ("not-logged-in", "no Claude Code login at %s; run `claude login`" % shown,
                evidence)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle, parse_constant=_reject_json_constant)
        if not isinstance(value, dict):
            raise ValueError("credentials file is not an object")
    except Exception as exc:
        evidence["login_kind"] = "unreadable"
        return ("unknown", "could not read %s: %s" % (shown, type(exc).__name__), evidence)

    oauth = value.get("claudeAiOauth")
    oauth = oauth if isinstance(oauth, dict) else {}
    if not oauth:
        evidence["login_kind"] = "other"
        return ("unknown",
                "%s holds no claude.ai OAuth login; `collie runners probe claude-code "
                "--live` can say what it is" % shown, evidence)
    evidence["login_kind"] = "claude.ai"
    expires_at = oauth.get("expiresAt")
    has_refresh = bool(oauth.get("refreshToken"))     # presence only; never the value
    if (isinstance(expires_at, (int, float)) and
            not isinstance(expires_at, bool) and math.isfinite(float(expires_at))):
        # Claude Code writes milliseconds.  Recorded as seconds so every probe in
        # this module speaks one unit.
        seconds = float(expires_at) / 1000.0
        evidence["expires_at"] = seconds
        if seconds <= now and not has_refresh:
            return ("expired",
                    "the Claude Code login in %s expired and has no refresh token; "
                    "run `claude login`" % shown, evidence)
    elif expires_at is not None:
        evidence["login_kind"] = "malformed"
        return ("unknown", "%s has a malformed Claude Code login expiry" % shown,
                evidence)
    return ("ok", "", evidence)


# --- live status (an official CLI's own redacted output) --------------------
def _default_status_runner(guard_provider: str) -> Callable[[tuple[str, ...]], Any]:
    """The callable ``subscription_guard._run_status`` expects.

    Reuses the guard's own launcher, which resolves the CLI without a shell and
    gives it a small allowlisted environment (``_STATUS_CHILD_ENV_NAMES``) — the
    status subprocess therefore inherits no proxy, no provider variable and no
    credential from this process, exactly as it does on the benchmark path.
    """
    def run(argv: tuple[str, ...]) -> Any:
        return subscription_guard._default_runner(argv, os.environ, guard_provider)
    return run


# `subscription_guard._check_claude` denies with a machine-readable reason; each
# one says something specific about the route, so they are translated rather than
# collapsed into "not usable".  A login that is real but not first-party is not a
# broken login — it is a *metered* one, and the difference is who gets billed.
_CLAUDE_DENIALS: dict[str, tuple[str, str, str]] = {
    "claude_not_logged_in": (
        "not-logged-in", "unknown",
        "`claude auth status` reports no login; run `claude login`"),
    "claude_auth_method_not_claude_ai": (
        "ok", "api_metered",
        "`claude auth status` reports a login that is not claude.ai; that route bills "
        "per request"),
    "claude_api_provider_not_first_party": (
        "ok", "api_metered",
        "`claude auth status` reports a non-first-party API provider; that route bills "
        "per request"),
    "claude_plan_not_pro_or_max": (
        "ok", "unknown",
        "`claude auth status` reports no Pro/Max plan, so which account pays is "
        "unevidenced"),
}


def _live_claude(base: RunnerProbe, now: float,
                 status_runner: Callable[[tuple[str, ...]], Any] | None) -> RunnerProbe:
    """Ask Claude Code for its own already-redacted login state.

    The parsing is ``subscription_guard._check_claude`` verbatim — the same
    allowlisted field shape, the same credential-field refusal, the same plan
    normalisation — because a second parser for the same payload is a second
    place for "logged in" to mean something slightly different.
    """
    receipt = subscription_guard._base_receipt(
        "claude-code", subscription_guard._checked_at(None))
    runner = status_runner or _default_status_runner("claude-code")
    try:
        subscription_guard._check_claude(receipt, runner)
    except subscription_guard.SubscriptionGuardError as exc:
        login, billing_class, detail = _CLAUDE_DENIALS.get(
            exc.reason,
            (base.login, "unknown",
             "could not read `claude auth status` (%s)" % exc.reason))
        return dataclasses.replace(
            base, login=login, billing_class=billing_class,
            billing_mode=BILLING_MODE_OF.get(billing_class, "unconfigured"),
            detail=(base.detail + "; " + detail).strip("; "))
    except Exception as exc:
        return dataclasses.replace(
            base, detail=(base.detail + "; could not run `claude auth status`: %s"
                          % type(exc).__name__).strip("; "))
    plan = str((receipt.get("auth") or {}).get("plan") or "")
    return dataclasses.replace(
        base, login="ok", billing_class="subscription_allowance",
        billing_mode=BILLING_MODE_OF["subscription_allowance"],
        billing_evidence={"source": "claude auth status", "plan": plan,
                          "observed_at": now})


def _live_codex(base: RunnerProbe, now: float,
                status_runner: Callable[[tuple[str, ...]], Any] | None) -> RunnerProbe:
    """Ask the Codex CLI for its own already-redacted login state.

    ``subscription_guard._check_codex`` is not called as a whole: it requires
    freshly observed *account* evidence (zero credits, auto-reload off) that only
    an operator can supply, and a probe has none.  What is reused is the part
    that reads the CLI — ``_run_status`` with the guard's bounded, single-channel
    output rules — and the guard's exact-match rule for the one status line that
    evidences the ChatGPT subscription route.  Anything else leaves the class
    where the metadata probe put it: the status text is never quoted into a
    probe, because a non-matching line is the one that can carry an email
    address.
    """
    receipt = subscription_guard._base_receipt(
        "codex-cli", subscription_guard._checked_at(None))
    runner = status_runner or _default_status_runner("codex-cli")
    try:
        status = subscription_guard._run_status(
            receipt, subscription_guard._CODEX_COMMAND, runner).strip()
    except subscription_guard.SubscriptionGuardError as exc:
        return dataclasses.replace(
            base, detail=(base.detail + "; could not read `codex login status` (%s)"
                          % exc.reason).strip("; "))
    except Exception as exc:
        return dataclasses.replace(
            base, detail=(base.detail + "; could not run `codex login status`: %s"
                          % type(exc).__name__).strip("; "))
    if status == "Logged in using ChatGPT":
        evidence = dict(base.billing_evidence)
        evidence.update({"source": "codex login status", "method": "ChatGPT",
                         "observed_at": now})
        return dataclasses.replace(
            base, login="ok", billing_class="subscription_allowance",
            billing_mode=BILLING_MODE_OF["subscription_allowance"],
            billing_evidence=evidence)
    if base.billing_class == "api_metered":
        return base                 # auth.json already said it is an API-key login
    return dataclasses.replace(
        base, billing_class="unknown", billing_mode="unconfigured",
        detail=(base.detail + "; `codex login status` did not report the ChatGPT "
                "subscription route, so the billing route is unevidenced").strip("; "))


# --- probing ----------------------------------------------------------------
def _placeholder_probe(spec: HarnessSpec, now: float) -> RunnerProbe:
    """A row for a key whose phase has not arrived.

    ``shutil.which`` still runs so ``collie runners`` can say "installed, just not
    wired up yet", but nothing is launched and the detail keeps ``usable()``
    False whatever the host happens to have lying around.
    """
    resolved = shutil.which(spec.binary) if spec.binary else ""
    return RunnerProbe(
        key=spec.key, installed=bool(resolved), executable_path=resolved or "",
        capabilities=_capabilities_for(spec), compat=compat_status(spec.key),
        probed_at=now, ttl_s=PROBE_TTL_S,
        detail="%s in this phase: %s arrives in phase %d"
               % (NOT_IMPLEMENTED_PREFIX, spec.key, spec.phase))


def _collie_probe(spec: HarnessSpec, now: float, provider: str) -> RunnerProbe:
    """Collie's own harness: installed by definition, since it is this process."""
    billing_class, evidence = _collie_billing(provider)
    return RunnerProbe(
        key=spec.key, installed=True,
        executable_path="",         # in-process; a path here would only leak one
        version=__version__,
        login="n/a",                # the Brain's credentials are the provider's business
        billing_class=billing_class,
        billing_mode=BILLING_MODE_OF.get(billing_class, "unconfigured"),
        billing_evidence=evidence,
        capabilities=_capabilities_for(spec), compat=compat_status(spec.key),
        probed_at=now, ttl_s=PROBE_TTL_S, detail="")


def _external_probe(spec: HarnessSpec, now: float, live: bool,
                    status_runner: Callable[[tuple[str, ...]], Any] | None) -> RunnerProbe:
    """One external CLI: ``which`` + ``--version`` + login metadata, then ``--live``."""
    if spec.key == "codex-exec":
        base = CodexExecRunner(executable=spec.binary).probe(now=now)
    elif spec.key == "codex-sdk":
        base = CodexSdkRunner().probe(now=now)
    elif spec.key == "codex-app-server":
        base = CodexAppServerRunner(executable=spec.binary).probe(now=now)
    elif spec.key == "pi-rpc":
        base = PiRpcRunner(executable=spec.binary).probe(now=now)
    elif spec.key == "claude-code":
        base = claude_code_runner.ClaudeCodeRunner(executable=spec.binary).probe(now=now)
        if base.installed:
            # The runner reports version metadata only; the login file is read here
            # (see _claude_login_state) so H3 has a state to look at without --live.
            login, detail, evidence = _claude_login_state(now)
            base = dataclasses.replace(
                base, login=login, billing_evidence=evidence,
                detail=(base.detail + "; " + detail).strip("; ") if detail else base.detail)
    else:                           # pragma: no cover - phase gate catches these first
        raise RunnerUnavailableError("no probe implemented for %r" % spec.key)

    base = dataclasses.replace(base, capabilities=_capabilities_for(spec),
                               compat=compat_status(spec.key), ttl_s=PROBE_TTL_S)
    if not live or not base.installed:
        return base
    if spec.key == "claude-code":
        return _live_claude(base, now, status_runner)
    if spec.key in ("codex-exec", "codex-sdk", "codex-app-server"):
        return _live_codex(base, now, status_runner)
    return base


def probe(key: str, *, live: bool = False, now: float | None = None,
          provider: str = "",
          status_runner: Callable[[tuple[str, ...]], Any] | None = None) -> RunnerProbe:
    """What is true about one runner on this host — cached for :data:`PROBE_TTL_S`.

    ``live=False`` (the default) reads metadata only: ``shutil.which``, one
    ``--version``, and whether a login file exists and when it expires.  Nothing
    is launched that could prompt, and the network is never touched.

    ``live=True`` additionally runs the CLI's *own* status command
    (``claude auth status --json`` / ``codex login status``) through
    ``subscription_guard``'s hardened launcher and parser.  That is the only way
    ``billing_class`` becomes anything but ``unknown`` for an external worker:
    being signed in is not evidence of *which* plan pays.

    ``provider`` names the Brain the native harness would run on and only affects
    the ``collie`` row; it defaults to the configured ``PROVIDER``.
    ``status_runner`` is an injection seam for the live status subprocess, used by
    the conformance engine and the tests.

    Never raises.  A key that is not in :data:`SPECS`, a CLI that will not answer
    and a ``HOME`` pointing at an empty directory all come back as a probe with
    ``installed=False`` (or ``login`` unknown) and a detail saying so, because a
    listing that throws tells the operator less than a row that says why.
    """
    autoload_compat_report()
    now = time.time() if now is None else float(now)
    if not math.isfinite(now):
        raise ValueError("probe time must be finite")
    spec = SPECS.get(key)
    if spec is None:
        return RunnerProbe(key=str(key), installed=False, probed_at=now,
                           ttl_s=PROBE_TTL_S,
                           detail="unknown runner %r; `collie runners` lists the "
                                  "keys that exist" % str(key))
    if spec.key == "collie" and not provider:
        try:
            provider = _configured_provider()
        except Exception:
            provider = ""
    cache_key = (spec.key, bool(live), provider if spec.key == "collie" else "")
    cacheable = status_runner is None
    if cacheable:
        with _LOCK:
            cached = _CACHE.get(cache_key)
            age = now - cached.probed_at if cached is not None else -1.0
            if cached is not None and 0 <= age < cached.ttl_s:
                return cached

    try:
        if spec.phase > CURRENT_PHASE:
            result = _placeholder_probe(spec, now)
        elif spec.kind == "native":
            result = _collie_probe(spec, now, provider)
        else:
            result = _external_probe(spec, now, live, status_runner)
    except Exception as exc:
        # A probe is a status row.  Turning "your ~/.codex is a directory, not a
        # file" into a traceback would take the whole `collie runners` table down
        # with it, including the rows that are fine.
        result = RunnerProbe(
            key=spec.key, installed=False,
            capabilities=_capabilities_for(spec), compat=compat_status(spec.key),
            probed_at=now, ttl_s=PROBE_TTL_S,
            detail="could not probe %s: %s: %s" % (spec.key, type(exc).__name__, exc))

    if cacheable:
        with _LOCK:
            _CACHE[cache_key] = result
    return result


def probe_all(keys: Iterable[str] | None = None, *, live: bool = False,
              now: float | None = None, provider: str = "",
              status_runner: Callable[[tuple[str, ...]], Any] | None = None
              ) -> dict[str, RunnerProbe]:
    """Probe exactly ``keys`` (default: every spec), in :data:`SPECS` order.

    Keys that are not in the registry are skipped rather than faked: the selector
    already reports an unknown key as its own hard rejection, and inventing a row
    for one would put a name in the receipt that nothing can explain.
    """
    if keys is None:
        wanted = list(SPECS)
    else:
        asked = {str(key) for key in keys}
        wanted = [key for key in SPECS if key in asked]
    return {key: probe(key, live=live, now=now, provider=provider,
                       status_runner=status_runner)
            for key in wanted}


def list_probes(*, live: bool = False, now: float | None = None,
                provider: str = "") -> list[RunnerProbe]:
    """Every runner's row, in table order — what ``collie runners`` prints."""
    return list(probe_all(live=live, now=now, provider=provider).values())


def option_keys() -> tuple[str, ...]:
    """Runner keys a user may pick today: the specs whose phase has arrived.

    This is the list ``--runner`` and the ``RUNNER`` setting offer (both add
    ``auto``, which is a request to choose rather than a runner).  A phase-2 key
    is real, listed by ``collie runners``, and deliberately not offered here.
    """
    return tuple(key for key, spec in SPECS.items() if spec.phase <= CURRENT_PHASE)


def handshake(key: str, *, live: bool = False, provider: str = "") -> CapabilityHandshake:
    """Return the host-observed capability manifest used for admission.

    This is the same declared-capabilities ∩ conformance-report projection the
    selector consumes, wrapped as an explicit protocol handshake so embedders do
    not need to infer capability truth from a label or runner class.
    """
    spec = SPECS.get(str(key or ""))
    if spec is None:
        raise ValueError("unknown runner: %r" % str(key))
    observed = probe(spec.key, live=live, provider=provider)
    capabilities = RunnerCapabilities.from_dict(
        observed.capabilities or spec.caps.to_dict())
    return CapabilityHandshake(
        runner=spec.key, protocol=capabilities.protocol,
        protocol_version=capabilities.protocol_version or observed.version,
        capabilities=capabilities,
        source="probe+compat" if observed.capabilities else "declaration",
        negotiated_at=observed.probed_at)


def make_runner(key: str, *, model: str = "", speed: str = "standard",
                effort: str = "auto", timeout_s: float | None = None,
                env_policy: str = "", read_only: bool = False) -> Any:
    """Build the runner object for ``key``.

    Raises rather than returning ``None`` for ``collie``: it is not an external
    worker, it is the harness this process already is, and the caller that asked
    for one took the wrong branch.  A ``None`` would surface that mistake later,
    as an ``AttributeError`` somewhere in the slice, instead of here where the
    wrong branch is still on the stack.

    ``env_policy`` defaults to the spec's; passing one is for the caller that
    already resolved the spec and wants that decision to be explicit in the call.

    ``effort`` is validated here and currently forwarded only to claude-code.
    Other adapters do not yet propagate it to their native reasoning controls.
    """
    if type(read_only) is not bool:
        raise ValueError("read_only must be a boolean")
    effort = claude_code_runner.normalize_effort(effort)
    if read_only and key != "claude-code":
        raise ValueError("this worker does not implement a read-only tool policy")
    spec = SPECS.get(key)
    if spec is None:
        raise ValueError("unknown runner: %r" % str(key))
    if spec.kind == "native":
        raise ValueError("collie is not an external runner")
    if spec.phase > CURRENT_PHASE:
        raise RunnerUnavailableError(
            "%s in this phase: %s arrives in phase %d"
            % (NOT_IMPLEMENTED_PREFIX, spec.key, spec.phase))
    timeout = spec.default_timeout_s if timeout_s is None else float(timeout_s)
    policy = env_policy or spec.env_policy
    if spec.key == "codex-exec":
        return CodexExecRunner(executable=spec.binary, model=model,
                               default_timeout_s=timeout, env_policy=policy)
    if spec.key == "codex-sdk":
        return CodexSdkRunner(model=model, default_timeout_s=timeout,
                              env_policy=policy)
    if spec.key == "codex-app-server":
        return CodexAppServerRunner(
            executable=spec.binary, model=model, default_timeout_s=timeout,
            env_policy=policy)
    if spec.key == "claude-code":
        return claude_code_runner.ClaudeCodeRunner(
            executable=spec.binary, model=model, speed=speed, effort=effort,
            tools=(claude_code_runner.READ_ONLY_TOOLS if read_only else
                   claude_code_runner.DEFAULT_TOOLS),
            default_timeout_s=timeout,
            env_policy=policy)
    if spec.key == "pi-rpc":
        return PiRpcRunner(executable=spec.binary, model=model,
                           default_timeout_s=timeout, env_policy=policy)
    raise RunnerUnavailableError("no runner implementation for %r" % spec.key)


__all__ = [
    "CODEX_APP_SERVER_CAPABILITIES", "CODEX_EXEC_CAPABILITIES",
    "CODEX_SDK_CAPABILITIES", "COLLIE_CAPABILITIES",
    "HERMES_ACP_CAPABILITIES", "HERMES_GATEWAY_CAPABILITIES", "PI_RPC_CAPABILITIES",
    "PRIME_RPC_CAPABILITIES", "PROBE_TTL_S", "SPECS", "apply_compat_report",
    "compat_status", "handshake", "list_probes", "make_runner", "option_keys", "probe",
    "probe_all", "reset_cache",
]
