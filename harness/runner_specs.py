"""Contract types for Collie's harness-selection layer.

Collie can carry out a task with its own harness (``collie``) or hand the work to
an external coding-agent CLI (``codex-exec``, ``claude-code``, ...).  Everything
the selection layer, the runners, the CLI and the receipts exchange is declared
here, in one stdlib-only module, so the registry, the selector, each runner and
the tests agree on field names without importing one another.

Three rules shape these types:

* **No credential ever becomes a value.**  A probe carries metadata (which file,
  which plan, when the JWT expires) and never a token.  Every free-text field
  that can quote a runner's stdout is pushed through :func:`redact_text` in
  ``__post_init__``, and the dataclasses are frozen so a redacted value cannot be
  edited back into a raw one afterwards.  ``tests/test_runner_specs.py`` asserts
  the serialized form of every type against the secret patterns.
* **Unknown is ``None``, never ``0``.**  A runner that reports no usage must not
  look like a run that cost nothing.  :class:`RunnerUsage` keeps every field
  optional and :attr:`RunnerUsage.known` is the single gate on accounting; the
  same rule reaches :func:`snapshot_to_run_result`, which leaves the token
  columns NULL rather than writing a comfortable zero.
* **These field names are the JSON keys.**  They land verbatim in the session
  receipt, in ``decision_payload["runner"]`` and in the compat report, so
  renaming one is a wire change and not a refactor.

The module deliberately imports almost nothing: :class:`~harness.recorder.RunResult`
(pure dataclass) and :mod:`harness.costs` (a price table).  ``providers.Usage`` is
imported lazily inside the one method that needs it, because ``providers`` pulls
in the whole model transport stack and this module is imported by the CLI on the
default ``RUNNER=collie`` path.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from . import costs


def _mapping(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}


def _items(value: Any) -> tuple:
    return tuple(value) if isinstance(value, (list, tuple, set, frozenset)) else ()


def _strict_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return bool(default) if value is None else False


def _finite_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return number
    return float(default)


def _finite_int(value: Any, default: int = 0, *, minimum: int | None = None,
                maximum: int = (1 << 63) - 1) -> int:
    number = _finite_float(value, float(default))
    answer = max(-maximum, min(maximum, int(number)))
    return max(minimum, answer) if minimum is not None else answer
from .recorder import RunResult


# --- phase gate -------------------------------------------------------------
# Runners are introduced in phases; a spec whose ``phase`` is beyond this is
# declared but not selectable, so the registry can carry tomorrow's keys without
# them ever being handed real work today.
CURRENT_PHASE = 2


# --- billing ----------------------------------------------------------------
# The five observable billing classes.  "unknown" is a first-class value, not a
# failure: a runner whose route we cannot evidence must be rejected by the
# no-paid-overage rules rather than optimistically assumed to be included.
BILLING_CLASSES = (
    "subscription_allowance",
    "paid_overage",
    "api_metered",
    "local",
    "unknown",
)

# Projection onto the four values Mission already persists (missionweb._billing_mode).
# Keeping the projection here rather than in missionweb means a new billing class
# cannot be added without deciding what it means for an existing durable Mission.
BILLING_MODE_OF = {
    "subscription_allowance": "subscription",
    "paid_overage": "metered",
    "api_metered": "metered",
    "local": "local",
    "unknown": "unconfigured",
}


# --- surfaces ---------------------------------------------------------------
SURFACES = (
    "run", "web", "pack", "mission-code", "repl", "tui", "acp", "loop",
    "slack", "delegate", "automation",
)

# Which surfaces may hand work to an *external* runner, per phase.  Everything
# not listed is locked to ``collie`` by hard rule H1 — repl/tui/acp re-decide the
# route every turn, slack has no approver, and loop feeds ``res.messages`` back in
# (an external runner returns none).
EXTERNAL_ALLOWED_SURFACES = {
    # The phase-one CLIs now have host-owned Web/Pack/Mission call sites.  Later
    # runner *protocols* remain phase-gated by their HarnessSpec.phase.
    1: ("run", "web", "pack", "mission-code"),
    2: ("run", "web", "pack", "mission-code"),
    3: ("run", "web", "pack", "mission-code", "delegate", "automation"),
}


# --- canonical events -------------------------------------------------------
# A closed whitelist.  Each runner dialect is translated into exactly these types
# so the web/CLI projection has one vocabulary to handle; deliberately no
# ``item.*`` (a Codex-ism) and no ``protocol.error`` (framing failures are
# ``runner.error`` like every other failure).
CANONICAL_TYPES = frozenset({
    "session.started", "session.resumed", "session.state",
    "turn.started", "turn.yielded", "turn.failed", "turn.cancelled",
    "tool.started", "tool.completed", "file.changed", "message.completed",
    "approval.requested", "approval.resolved",
    "interaction.requested", "interaction.resolved", "message.accepted",
    "context.compacted",
    "usage.updated", "rate_limit.reached", "auth.required", "runner.error",
})


# --- redaction --------------------------------------------------------------
# Value-level redaction.  ``agent_runners._clean_error`` already scrubs Codex's
# own error text; this is the same guarantee applied to every string that reaches
# a receipt, a probe detail, an event payload or an approval record, whatever
# runner produced it.  The whole match is replaced (not just the token) so the
# serialized form contains neither the secret nor the "Bearer " marker the test
# suite greps for.
_REDACTED = "[redacted]"
_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|x-api-key|cookie|set-cookie)\s*:[^\r\n]*")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRETS = re.compile(
    r"\b(?:sk|sess)-[A-Za-z0-9_-]{4,}"           # OpenAI / Anthropic style keys, session ids
    r"|\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"   # Stripe secret/restricted keys
    r"|\bgsk_[A-Za-z0-9]{20,}"                   # Groq
    r"|\b(?:xai|bai)-[A-Za-z0-9-]{20,}"          # xAI / Boson
    r"|\bAIza[0-9A-Za-z_-]{35}\b"                # Google
    r"|\bya29\.[0-9A-Za-z_-]{20,}"               # Google OAuth
    r"|\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{8,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"            # GitHub tokens
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}"            # Slack tokens
    r"|\bAKIA[0-9A-Z]{16}\b"                     # AWS access key id
    r"|\beyJ[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_.=-]+)*"   # JWT (base64 '{"' header)
)
_ASSIGNED_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret[_-]?key|"
    r"client[_-]?secret|password|passwd)\b\s*['\"]?\s*[=:]\s*['\"]?"
    r"([A-Za-z0-9_\-./+]{16,})['\"]?")
_SECRET_FLAG = re.compile(
    r"(?i)(?:--?(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|token))\s+(?:['\"])?([A-Za-z0-9_\-./+]{16,})(?:['\"])?")
_URL_CREDENTIAL = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@")
_PAYLOAD_LIMIT = 4_000


def redact_text(text: Any, limit: int = 16_000) -> str:
    """Mask credential-shaped substrings and bound the length.

    Applied to anything that quotes a runner's output.  It is a net, not a proof:
    the real guarantee is that Collie never *reads* a credential in the first
    place (runners read their own login files).  This catches the case where a
    runner echoes one back at us in an error message.
    """
    value = str(text or "")
    if not value:
        return ""
    value = _AUTH_HEADER.sub(lambda m: "%s: %s" % (m.group(1), _REDACTED), value)
    value = _BEARER.sub(_REDACTED, value)
    value = _SECRETS.sub(_REDACTED, value)
    value = _ASSIGNED_SECRET.sub(
        lambda match: match.group(0).replace(match.group(1), _REDACTED), value)
    value = _SECRET_FLAG.sub(
        lambda match: match.group(0).replace(match.group(1), _REDACTED), value)
    value = _URL_CREDENTIAL.sub(lambda match: match.group(1) + _REDACTED + "@", value)
    return value[:limit]


class _RedactionStructureError(ValueError):
    pass


def _redact_structure(value: Any, limit: int, depth: int,
                      seen: set[int]) -> Any:
    if depth > 64:
        raise _RedactionStructureError("max-depth")
    if isinstance(value, str):
        return redact_text(value, limit)
    if isinstance(value, float) and not math.isfinite(value):
        return "[non-finite]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if not isinstance(value, (dict, list, tuple)):
        return redact_text(repr(value), limit)

    marker = id(value)
    if marker in seen:
        raise _RedactionStructureError("cycle")
    seen.add(marker)
    try:
        if isinstance(value, dict):
            return {
                redact_text(key, min(limit, 256)):
                    _redact_structure(item, limit, depth + 1, seen)
                for key, item in value.items()
            }
        return [_redact_structure(item, limit, depth + 1, seen)
                for item in value]
    finally:
        seen.discard(marker)


def redact_value(value: Any, limit: int = _PAYLOAD_LIMIT) -> Any:
    """Recursively redact a JSON-ish value and bound its serialized size.

    Mirrors ``agent_runners._bounded_payload``: an oversized payload degrades to a
    truncated preview rather than being dropped, because a truncated tool result
    still explains what the runner did while an absent one does not.
    """
    try:
        cleaned = _redact_structure(value, limit, 0, set())
    except (_RedactionStructureError, RecursionError) as exc:
        reason = str(exc) if isinstance(exc, _RedactionStructureError) else "max-depth"
        return {"truncated": True, "reason": reason}
    if not isinstance(cleaned, (dict, list)):
        return cleaned
    try:
        encoded = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False)
    except (TypeError, ValueError):
        return {"truncated": True, "preview": redact_text(repr(cleaned), limit)}
    if len(encoded) <= limit:
        return cleaned
    return {"truncated": True, "preview": encoded[:limit]}


def stable_digest(value: Any) -> str:
    """sha256 over a canonical JSON encoding — stable across dict orderings.

    Used for ``probe_digest`` / ``signals_digest`` / ``events_digest``: a receipt
    records *which* evidence a decision was made on without inlining all of it.
    """
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        encoded = repr(value)
    return hashlib.sha256(encoded.encode("utf-8", "replace")).hexdigest()


# --- provider families ------------------------------------------------------
# A credential family is "which login pays for this".  It is coarser than the
# provider: claude-agent-sdk, anthropic-oauth and claude-cli all spend the same
# Claude subscription, so a fallback between them is not a billing-route change,
# while `anthropic` (an API key) is a different payer entirely and becomes
# ``api:anthropic``.  Table mirrors missionweb._SUBSCRIPTION_PROVIDERS /
# _LOCAL_PROVIDERS; duplicated rather than imported because missionweb drags in
# the whole Mission service.
_PROVIDER_FAMILIES = {
    "claude-agent-sdk": "claude",
    "claude-sdk": "claude",
    "anthropic-oauth": "claude",
    "claude-sub": "claude",
    "claude-cli": "claude",
    "cli": "claude",
    "codex-oauth": "codex",
    "codex-sub": "codex",
    "codex": "codex",
    "ollama": "local",
    "mock": "local",
}


def family_of_provider(provider: str) -> str:
    """``"anthropic-oauth"`` -> ``"claude"``; ``"deepseek"`` -> ``"api:deepseek"``."""
    name = str(provider or "").strip().lower()
    if not name:
        return ""
    known = _PROVIDER_FAMILIES.get(name)
    if known:
        return known
    return "api:" + name


# --- errors -----------------------------------------------------------------
class RunnerError(RuntimeError):
    """Base for every failure raised by the runner-selection layer."""


class RunnerUnavailableError(RunnerError):
    """The requested runner is not installed, not logged in, or gated by phase."""


class RunnerProtocolError(RunnerError):
    """A runner spoke something we could not frame, decode, or correlate."""


class RunnerMessageDeliveryError(RunnerError):
    """A sent message was rejected or its receipt is unknown; do not replay it."""

    def __init__(self, message: str, *, delivery: str = "unknown"):
        super().__init__(message)
        self.delivery = "rejected" if delivery == "rejected" else "unknown"


class RunnerBillingError(RunnerError):
    """A billing route could not be evidenced, or an env override would change it.

    Raised before a process starts.  Silently spending a different budget than the
    one the operator consented to is the failure this whole layer exists to avoid.
    """


class RunnerSelectionError(RunnerError):
    """No candidate survived the hard filters and the caller cannot fall back."""


# --- capability & spec ------------------------------------------------------
@dataclass(frozen=True)
class RunnerCapabilities:
    """What a runner can do, as *declared*.

    Declared values are optimistic by nature, so ``runner_registry`` intersects
    them with the last conformance report before a probe reports them: a
    capability that has never been verified on this host gets downgraded to
    False rather than believed.
    """

    protocol: str
    protocol_version: str = ""      # version/commit we actually diffed against; "" = unpinned
    session_create: bool = True
    session_resume: bool = False
    session_fork: bool = False
    streaming: bool = False
    cursor_replay: bool = False
    steer: bool = False
    follow_up: bool = False
    cancel: str = "process-tree"    # native+process-tree | process-tree | none
    approval_round_trip: bool = False
    usage_tokens: bool = False
    usage_cost: bool = False
    quota_signals: bool = False
    request_gate: bool = False      # per-request reservation (Mission leash); collie only
    native_goal: bool = False       # a second control plane — must be proven disabled
    native_scheduler: bool = False
    confinement: str = "none"       # none | tools-allowlist | workspace-write | container
    tools: frozenset[str] = frozenset()
    needs_git_workspace: bool = True
    prompt_transport: str = "stdin"  # stdin | argv | rpc
    inputs: frozenset[str] = frozenset({"text"})  # text | image_url | image_file
    compact: bool = False
    interactions: frozenset[str] = frozenset()  # approval | user_input | select | confirm
    capability_handshake: bool = False
    windows_native: bool | None = None   # None = unverified; the compat report decides

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "protocol_version": self.protocol_version,
            "session_create": self.session_create,
            "session_resume": self.session_resume,
            "session_fork": self.session_fork,
            "streaming": self.streaming,
            "cursor_replay": self.cursor_replay,
            "steer": self.steer,
            "follow_up": self.follow_up,
            "cancel": self.cancel,
            "approval_round_trip": self.approval_round_trip,
            "usage_tokens": self.usage_tokens,
            "usage_cost": self.usage_cost,
            "quota_signals": self.quota_signals,
            "request_gate": self.request_gate,
            "native_goal": self.native_goal,
            "native_scheduler": self.native_scheduler,
            "confinement": self.confinement,
            "tools": sorted(self.tools),
            "needs_git_workspace": self.needs_git_workspace,
            "prompt_transport": self.prompt_transport,
            "inputs": sorted(self.inputs),
            "compact": self.compact,
            "interactions": sorted(self.interactions),
            "capability_handshake": self.capability_handshake,
            "windows_native": self.windows_native,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunnerCapabilities":
        value = _mapping(value)
        windows_native = value.get("windows_native")
        return cls(
            protocol=str(value.get("protocol") or ""),
            protocol_version=str(value.get("protocol_version") or ""),
            session_create=_strict_bool(value.get("session_create"), True),
            session_resume=_strict_bool(value.get("session_resume")),
            session_fork=_strict_bool(value.get("session_fork")),
            streaming=_strict_bool(value.get("streaming")),
            cursor_replay=_strict_bool(value.get("cursor_replay")),
            steer=_strict_bool(value.get("steer")),
            follow_up=_strict_bool(value.get("follow_up")),
            cancel=str(value.get("cancel") or "process-tree"),
            approval_round_trip=_strict_bool(value.get("approval_round_trip")),
            usage_tokens=_strict_bool(value.get("usage_tokens")),
            usage_cost=_strict_bool(value.get("usage_cost")),
            quota_signals=_strict_bool(value.get("quota_signals")),
            request_gate=_strict_bool(value.get("request_gate")),
            native_goal=_strict_bool(value.get("native_goal")),
            native_scheduler=_strict_bool(value.get("native_scheduler")),
            confinement=str(value.get("confinement") or "none"),
            tools=frozenset(str(item) for item in _items(value.get("tools"))),
            needs_git_workspace=_strict_bool(value.get("needs_git_workspace"), True),
            prompt_transport=str(value.get("prompt_transport") or "stdin"),
            inputs=frozenset(str(item) for item in
                             (_items(value.get("inputs")) or ("text",))),
            compact=_strict_bool(value.get("compact")),
            interactions=frozenset(
                str(item) for item in _items(value.get("interactions"))),
            capability_handshake=_strict_bool(value.get("capability_handshake")),
            windows_native=(None if windows_native is None else
                            _strict_bool(windows_native)),
        )


@dataclass(frozen=True)
class RunInput:
    """Runner-neutral turn input.

    Images stay references at this boundary.  The adapter that owns the target
    protocol decides whether a data URL or a canonical local path is valid; an
    adapter must never silently drop an unsupported image and run only the text.
    """

    text: str
    image_urls: tuple[str, ...] = ()
    image_files: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        text = str(self.text or "")
        if not text.strip() or "\x00" in text:
            raise ValueError("run input text must be non-empty and contain no NUL")
        object.__setattr__(self, "text", text)
        urls = []
        remaining = 16 * 1024 * 1024
        for item in _items(self.image_urls)[:8]:
            url = str(item)
            if len(url) > 8 * 1024 * 1024 or len(url) > remaining:
                raise ValueError("run input images exceed the 16 MiB envelope")
            urls.append(url)
            remaining -= len(url)
        object.__setattr__(self, "image_urls", tuple(urls))
        object.__setattr__(self, "image_files", tuple(
            str(item)[:4_000] for item in _items(self.image_files)[:8]))
        object.__setattr__(self, "metadata", redact_value(_mapping(self.metadata)))

    @property
    def kinds(self) -> frozenset[str]:
        kinds = {"text"}
        if self.image_urls:
            kinds.add("image_url")
        if self.image_files:
            kinds.add("image_file")
        return frozenset(kinds)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "image_urls": list(self.image_urls),
                "image_files": list(self.image_files),
                "metadata": dict(self.metadata)}

    @classmethod
    def from_value(cls, value: Any) -> "RunInput":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(text=value)
        value = _mapping(value)
        return cls(text=str(value.get("text") or ""),
                   image_urls=tuple(str(item) for item in
                                    _items(value.get("image_urls"))),
                   image_files=tuple(str(item) for item in
                                     _items(value.get("image_files"))),
                   metadata=_mapping(value.get("metadata")))


@dataclass(frozen=True)
class PendingInteraction:
    """A non-terminal runner pause that requires a host response."""

    interaction_id: str
    runner: str
    kind: str
    prompt: str = ""
    options: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)
    created_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "prompt", redact_text(self.prompt))
        object.__setattr__(self, "options", tuple(
            redact_text(item, 1_000) for item in _items(self.options)[:128]))
        object.__setattr__(self, "raw", redact_value(_mapping(self.raw)))

    def to_dict(self) -> dict[str, Any]:
        return {"interaction_id": self.interaction_id, "runner": self.runner,
                "kind": self.kind, "prompt": self.prompt,
                "options": list(self.options), "raw": dict(self.raw),
                "created_at": self.created_at}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PendingInteraction":
        value = _mapping(value)
        return cls(interaction_id=str(value.get("interaction_id") or ""),
                   runner=str(value.get("runner") or ""),
                   kind=str(value.get("kind") or ""),
                   prompt=str(value.get("prompt") or ""),
                   options=tuple(str(item) for item in
                                 _items(value.get("options"))),
                   raw=_mapping(value.get("raw")),
                   created_at=_finite_float(value.get("created_at"), 0.0))


@dataclass(frozen=True)
class QueuedTurnMessage:
    """A host instruction whose delivery semantics must not be guessed."""

    message_id: str
    mode: str  # steer | follow_up
    text: str
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if self.mode not in ("steer", "follow_up"):
            raise ValueError("turn message mode must be steer or follow_up")
        text = str(self.text or "")
        if not text.strip() or "\x00" in text:
            raise ValueError("turn message text must be non-empty and contain no NUL")
        object.__setattr__(self, "text", redact_text(text, 4_000))
        object.__setattr__(self, "message_id", str(self.message_id or "")[:256])

    @classmethod
    def from_value(cls, value: Any) -> "QueuedTurnMessage":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(message_id="", mode="steer", text=value)
        value = _mapping(value)
        return cls(message_id=str(value.get("message_id") or value.get("id") or ""),
                   mode=str(value.get("mode") or "steer"),
                   text=str(value.get("text") or value.get("message") or ""),
                   created_at=_finite_float(value.get("created_at"), 0.0))

    def to_dict(self) -> dict[str, Any]:
        return {"message_id": self.message_id, "mode": self.mode,
                "text": self.text, "created_at": self.created_at}


@dataclass(frozen=True)
class CapabilityHandshake:
    """Live adapter manifest intersected with Collie's declared contract."""

    runner: str
    protocol: str
    protocol_version: str
    capabilities: RunnerCapabilities
    source: str = "adapter"
    negotiated_at: float = 0.0

    def require(self, *, needs: frozenset[str] = frozenset(),
                input_kinds: frozenset[str] = frozenset({"text"})) -> None:
        missing_inputs = sorted(set(input_kinds) - set(self.capabilities.inputs))
        if missing_inputs:
            raise RunnerProtocolError(
                "%s cannot accept input kinds: %s" %
                (self.runner, ", ".join(missing_inputs)))
        missing_tools = sorted(set(needs) - set(self.capabilities.tools))
        if missing_tools:
            raise RunnerProtocolError(
                "%s does not provide required tools: %s" %
                (self.runner, ", ".join(missing_tools)))

    def to_dict(self) -> dict[str, Any]:
        return {"runner": self.runner, "protocol": self.protocol,
                "protocol_version": self.protocol_version,
                "capabilities": self.capabilities.to_dict(),
                "source": self.source, "negotiated_at": self.negotiated_at}


@dataclass(frozen=True)
class HarnessSpec:
    """The static, host-independent description of one runner."""

    key: str
    label: str
    kind: str                                # native | external
    binary: str = ""
    version_argv: tuple[str, ...] = ()
    min_version: str = ""
    credential_family: str = ""              # "" = follows the configured PROVIDER (collie)
    caps: RunnerCapabilities = RunnerCapabilities(protocol="collie-native")
    env_policy: str = "native"
    guard_alias: str = ""                    # subscription_guard alias; "" -> billing unknown
    phase: int = 1
    default_timeout_s: float = 900.0
    notes: tuple[str, ...] = ()              # known limits, shown verbatim in receipts

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "binary": self.binary,
            "version_argv": list(self.version_argv),
            "min_version": self.min_version,
            "credential_family": self.credential_family,
            "caps": self.caps.to_dict(),
            "env_policy": self.env_policy,
            "guard_alias": self.guard_alias,
            "phase": self.phase,
            "default_timeout_s": self.default_timeout_s,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "HarnessSpec":
        value = _mapping(value)
        return cls(
            key=str(value.get("key") or ""),
            label=str(value.get("label") or ""),
            kind=str(value.get("kind") or "external"),
            binary=str(value.get("binary") or ""),
            version_argv=tuple(str(item) for item in _items(value.get("version_argv"))),
            min_version=str(value.get("min_version") or ""),
            credential_family=str(value.get("credential_family") or ""),
            caps=RunnerCapabilities.from_dict(value.get("caps") or {}),
            env_policy=str(value.get("env_policy") or "native"),
            guard_alias=str(value.get("guard_alias") or ""),
            phase=_finite_int(value.get("phase"), 1, minimum=1),
            default_timeout_s=max(.001, _finite_float(
                value.get("default_timeout_s"), 900.0)),
            notes=tuple(str(item) for item in _items(value.get("notes"))),
        )


# A probe whose detail starts with this is declared-but-not-wired: the key exists
# so `collie runners` can show what is coming, and usable() stays False.
NOT_IMPLEMENTED_PREFIX = "not implemented"


@dataclass(frozen=True)
class RunnerProbe:
    """What is true about a runner *on this host, right now* — metadata only.

    Everything here comes from read-only inspection: ``shutil.which``, a
    ``--version`` call, whether a login file exists and when its JWT expires, and
    (only with ``live=True``) an official CLI's own redacted status output.  No
    credential value is read, and nothing here touches the network.
    """

    key: str
    installed: bool
    executable_path: str = ""
    version: str = ""
    login: str = "unknown"          # ok | missing-key | not-logged-in | expired | unknown | n/a
    billing_class: str = "unknown"
    billing_mode: str = "unconfigured"
    overage_attested: bool = False  # operator attestation, NOT an observation
    billing_evidence: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)   # declared caps ∧ compat report
    compat: str = "unverified"      # "verified <date>" | "unverified"
    probed_at: float = 0.0
    ttl_s: float = 60.0
    detail: str = ""

    def __post_init__(self) -> None:
        # A probe detail quotes CLI stderr, and billing evidence quotes a status
        # payload; both are redacted at construction so no later path can leak
        # them into a receipt.
        object.__setattr__(self, "detail", redact_text(self.detail))
        object.__setattr__(self, "billing_evidence", redact_value(dict(self.billing_evidence or {})))

    def usable(self) -> bool:
        """Installed, authenticated, and actually wired up in this phase."""
        return bool(
            self.installed
            and self.login in ("ok", "n/a")
            and not self.detail.startswith(NOT_IMPLEMENTED_PREFIX)
        )

    def availability(self) -> str:
        """Operator-facing state that does not conflate installed with runnable."""
        if self.detail.startswith(NOT_IMPLEMENTED_PREFIX):
            return "declared-not-runnable"
        if not self.installed:
            return "not-installed"
        if self.login not in ("ok", "n/a"):
            return "needs-login"
        if not self.usable():
            return "not-runnable"
        if str(self.compat or "").lower().startswith("verified"):
            return "runnable-verified"
        return "runnable-unverified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "installed": self.installed,
            "executable_path": self.executable_path,
            "version": self.version,
            "login": self.login,
            "billing_class": self.billing_class,
            "billing_mode": self.billing_mode,
            "overage_attested": self.overage_attested,
            "billing_evidence": dict(self.billing_evidence),
            "capabilities": dict(self.capabilities),
            "compat": self.compat,
            "probed_at": self.probed_at,
            "ttl_s": self.ttl_s,
            "detail": self.detail,
            "usable": self.usable(),
            "runnable": self.usable(),
            "availability": self.availability(),
            "compat_verified": str(self.compat or "").lower().startswith("verified"),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunnerProbe":
        value = _mapping(value)
        raw_caps = _mapping(value.get("capabilities"))
        return cls(
            key=str(value.get("key") or ""),
            installed=_strict_bool(value.get("installed")),
            executable_path=str(value.get("executable_path") or ""),
            version=str(value.get("version") or ""),
            login=str(value.get("login") or "unknown"),
            billing_class=str(value.get("billing_class") or "unknown"),
            billing_mode=str(value.get("billing_mode") or "unconfigured"),
            overage_attested=_strict_bool(value.get("overage_attested")),
            billing_evidence=_mapping(value.get("billing_evidence")),
            capabilities=(RunnerCapabilities.from_dict(raw_caps).to_dict()
                          if raw_caps else {}),
            compat=str(value.get("compat") or "unverified"),
            probed_at=_finite_float(value.get("probed_at"), 0.0),
            ttl_s=max(0.0, _finite_float(value.get("ttl_s"), 60.0)),
            detail=str(value.get("detail") or ""),
        )


# --- session locator --------------------------------------------------------
@dataclass(frozen=True)
class NativeSessionRef:
    """Where a runner's own conversation lives — an id and a path, never a token.

    A Mission case stores this, not the snapshot: the locator is enough to resume,
    and keeping the transcript out of the durable row keeps the run's text out of
    the Mission database.
    """

    runner: str
    workspace: str
    locator: str
    protocol_version: str
    created_at: float
    workspace_digest: str = ""

    @classmethod
    def from_snapshot(cls, snapshot: Any, *, protocol_version: str = "",
                      created_at: float | None = None) -> "NativeSessionRef":
        """Derive from an ``agent_runners.RunnerSnapshot`` without importing it."""
        if created_at is None:
            created_at = float(getattr(snapshot, "started_at", 0.0) or 0.0)
        return cls(
            runner=str(getattr(snapshot, "runner", "") or ""),
            workspace=str(getattr(snapshot, "workspace", "") or ""),
            locator=str(getattr(snapshot, "thread_id", "") or ""),
            protocol_version=str(protocol_version or ""),
            created_at=float(created_at),
            workspace_digest=str(getattr(snapshot, "workspace_digest", "") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "runner": self.runner,
            "workspace": self.workspace,
            "locator": self.locator,
            "protocol_version": self.protocol_version,
            "created_at": self.created_at,
            "workspace_digest": self.workspace_digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NativeSessionRef":
        value = _mapping(value)
        return cls(
            runner=str(value.get("runner") or ""),
            workspace=str(value.get("workspace") or ""),
            locator=str(value.get("locator") or ""),
            protocol_version=str(value.get("protocol_version") or ""),
            created_at=_finite_float(value.get("created_at"), 0.0),
            workspace_digest=str(value.get("workspace_digest") or ""),
        )


# --- events -----------------------------------------------------------------
@dataclass(frozen=True)
class CanonicalEvent:
    """One runner event, translated into Collie's vocabulary.

    ``type`` must be in :data:`CANONICAL_TYPES`; an unmapped dialect event is a
    translator bug, so construction raises rather than silently inventing a type.
    Translators are expected to catch that and emit ``runner.error`` with the
    original ``native_type``, which is exactly what an operator needs to see.
    """

    cursor: int
    type: str
    native_type: str
    payload: dict
    at: float
    runner: str

    def __post_init__(self) -> None:
        if self.type not in CANONICAL_TYPES:
            raise RunnerProtocolError("not a canonical event type: %r" % (self.type,))
        object.__setattr__(self, "payload", redact_value(dict(self.payload or {})))

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor,
            "type": self.type,
            "native_type": self.native_type,
            "payload": dict(self.payload),
            "at": self.at,
            "runner": self.runner,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CanonicalEvent":
        value = _mapping(value)
        return cls(
            cursor=_finite_int(value.get("cursor"), 0, minimum=0),
            type=str(value.get("type") or ""),
            native_type=str(value.get("native_type") or ""),
            payload=_mapping(value.get("payload")),
            at=_finite_float(value.get("at"), 0.0),
            runner=str(value.get("runner") or ""),
        )


# --- approvals --------------------------------------------------------------
@dataclass(frozen=True)
class ApprovalRequest:
    """A runner asking permission, normalized for Collie's gate.

    ``command`` and ``raw`` are redacted at construction: an approval request is
    the one place a runner hands us a literal command line, and command lines are
    where tokens end up.
    """

    approval_id: str
    runner: str
    kind: str                  # command | file_change | permission | mcp | user_input
    command: str = ""
    cwd: str = ""
    paths: tuple = ()
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", redact_text(self.command))
        object.__setattr__(self, "paths", tuple(str(item) for item in (self.paths or ())))
        object.__setattr__(self, "raw", redact_value(dict(self.raw or {})))

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "runner": self.runner,
            "kind": self.kind,
            "command": self.command,
            "cwd": self.cwd,
            "paths": list(self.paths),
            "raw": dict(self.raw),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ApprovalRequest":
        value = _mapping(value)
        return cls(
            approval_id=str(value.get("approval_id") or ""),
            runner=str(value.get("runner") or ""),
            kind=str(value.get("kind") or ""),
            command=str(value.get("command") or ""),
            cwd=str(value.get("cwd") or ""),
            paths=tuple(str(item) for item in _items(value.get("paths")))[:256],
            raw=_mapping(value.get("raw")),
        )


@dataclass(frozen=True)
class ApprovalDecision:
    """What Collie answered.  ``outcome`` is a ``gate.Outcome`` value verbatim, so
    an audit row from an external runner is comparable with a native one."""

    approval_id: str
    outcome: str               # allow_once | allow_always | reject_once | reject_always
    reason: str
    decided_by: str            # gate | approver | policy-deny | timeout-deny | cmdsafety-deny

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", redact_text(self.reason))

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "outcome": self.outcome,
            "reason": self.reason,
            "decided_by": self.decided_by,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ApprovalDecision":
        value = _mapping(value)
        return cls(
            approval_id=str(value.get("approval_id") or ""),
            outcome=str(value.get("outcome") or ""),
            reason=str(value.get("reason") or ""),
            decided_by=str(value.get("decided_by") or ""),
        )


# --- usage ------------------------------------------------------------------
@dataclass(frozen=True)
class RunnerUsage:
    """Token usage from a runner, normalized to Collie's convention.

    Collie's ``input_tokens`` is UNCACHED input (``providers.Usage``), so any
    dialect that counts cached bytes inside its input figure has them subtracted
    back out here — the same correction ``providers._openai_usage`` makes.

    Every field is optional and ``None`` means *not reported*.  A runner with no
    usage channel must produce ``known == False``, never zeros, so the budget code
    can refuse to account for it instead of recording a free run.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read: int | None = None
    cache_creation: int | None = None
    reasoning_tokens: int | None = None
    cost_usd_reported: float | None = None   # only claude-code reports real $
    source: str = ""     # turn.completed | result.usage | state.db | get_session_stats | session.usage

    @property
    def known(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None

    def to_collie_usage(self):
        """-> ``providers.Usage``, or ``None`` when usage is unknown.

        ``None`` is the caller's instruction not to account for this run at all.
        The cache fields fall back to 0 only once ``known`` is True: a provider
        that reports tokens but no caching (ollama) genuinely read zero cached
        bytes, which is the pre-existing convention in ``providers``.
        """
        if not self.known:
            return None
        from .providers import Usage      # lazy: providers pulls in the transport stack
        return Usage(
            input_tokens=int(self.input_tokens or 0),
            output_tokens=int(self.output_tokens or 0),
            cache_read=int(self.cache_read or 0),
            cache_creation=int(self.cache_creation or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd_reported": self.cost_usd_reported,
            "source": self.source,
            "known": self.known,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunnerUsage":
        value = _mapping(value)
        return cls(
            input_tokens=_opt_int(value.get("input_tokens")),
            output_tokens=_opt_int(value.get("output_tokens")),
            cache_read=_opt_int(value.get("cache_read")),
            cache_creation=_opt_int(value.get("cache_creation")),
            reasoning_tokens=_opt_int(value.get("reasoning_tokens")),
            cost_usd_reported=_opt_float(value.get("cost_usd_reported")),
            source=str(value.get("source") or ""),
        )


def _opt_int(value: Any) -> int | None:
    """int() that keeps "absent" distinguishable from zero."""
    if isinstance(value, bool) or value is None:
        return None
    if (isinstance(value, (int, float)) and math.isfinite(float(value))
            and 0 <= float(value) <= (1 << 63) - 1):
        return int(value)
    return None


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if (isinstance(value, (int, float)) and math.isfinite(float(value))
            and float(value) >= 0):
        return float(value)
    return None


def _first_int(raw: dict, *names: str) -> int | None:
    for name in names:
        if name in raw:
            found = _opt_int(raw.get(name))
            if found is not None:
                return found
    return None


# Which usage dialect a runner key speaks.  Kept next to the translator rather
# than in the registry so a new key cannot be added without deciding how its
# tokens are counted.
_USAGE_DIALECTS = {
    "collie": "collie",
    "codex-exec": "codex",
    "codex-sdk": "codex",
    "codex-app-server": "codex",
    "claude-code": "claude",
    "hermes-gateway": "hermes",
    "hermes-acp": "hermes",
    "pi-rpc": "pi",
    "prime-rpc": "prime",
}


def usage_to_collie(runner_key: str, raw: dict) -> RunnerUsage:
    """Translate one runner's usage report into :class:`RunnerUsage`.

    The only interesting case is Codex: ``input_tokens`` there INCLUDES the cached
    portion (``cached_input_tokens``), exactly like OpenAI's ``prompt_tokens``, so
    the cached part is subtracted back out to keep Collie's "input = uncached"
    invariant.  Failing to do that double-counts the prefix in every cache-ledger
    metric — the same bug ``providers._openai_usage`` and ``codex_oauth._consume``
    already correct for.

    An unrecognized runner, or a report missing the fields, yields all-``None``.
    """
    raw = dict(raw or {})
    # Codex 0.149.x has been seen nesting the counters under `token_usage`; unwrap
    # it here so both shapes translate identically.
    nested = raw.get("token_usage")
    if isinstance(nested, dict):
        merged = dict(nested)
        merged.update({k: v for k, v in raw.items() if k != "token_usage"})
        raw = merged

    dialect = _USAGE_DIALECTS.get(str(runner_key or ""), "")

    if dialect == "codex":
        cache_read = _first_int(raw, "cached_input_tokens")
        total_input = _first_int(raw, "input_tokens")
        uncached = None
        if total_input is not None:
            uncached = max(0, total_input - (cache_read or 0))
        return RunnerUsage(
            input_tokens=uncached,
            output_tokens=_first_int(raw, "output_tokens"),
            cache_read=cache_read,
            cache_creation=_first_int(raw, "cache_write_input_tokens"),
            # Codex counts reasoning inside output_tokens; this is the breakdown, not an addend.
            reasoning_tokens=_first_int(raw, "reasoning_output_tokens"),
            source="turn.completed",
        )

    if dialect == "claude":
        # The terminal object from `--output-format stream-json`, or its usage member.
        inner = raw.get("usage")
        counts = dict(inner) if isinstance(inner, dict) else raw
        return RunnerUsage(
            input_tokens=_first_int(counts, "input_tokens"),
            output_tokens=_first_int(counts, "output_tokens"),
            cache_read=_first_int(counts, "cache_read_input_tokens"),
            cache_creation=_first_int(counts, "cache_creation_input_tokens"),
            cost_usd_reported=_opt_float(raw.get("total_cost_usd")),
            source="result.usage",
        )

    if dialect == "hermes":
        # Two shapes: the gateway's session.usage{input,output} and state.db's
        # sessions(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens).
        from_state = "input_tokens" in raw or "cache_read_tokens" in raw
        return RunnerUsage(
            input_tokens=_first_int(raw, "input_tokens", "input"),
            output_tokens=_first_int(raw, "output_tokens", "output"),
            cache_read=_first_int(raw, "cache_read_tokens"),
            cache_creation=_first_int(raw, "cache_write_tokens"),
            source="state.db" if from_state else "session.usage",
        )

    if dialect == "pi":
        tokens = raw.get("tokens")
        counts = dict(tokens) if isinstance(tokens, dict) else raw
        return RunnerUsage(
            input_tokens=_first_int(counts, "input"),
            output_tokens=_first_int(counts, "output"),
            cache_read=_first_int(counts, "cacheRead"),
            cache_creation=_first_int(counts, "cacheWrite"),
            source="get_session_stats",
        )

    if dialect == "prime":
        # Prime exposes get_session_stats too, but its field names have never been
        # diffed against a real response.  Reporting unverified numbers would be
        # worse than reporting none, so this stays unknown until conformance says
        # otherwise.
        return RunnerUsage(source="get_session_stats")

    if dialect == "collie":
        return RunnerUsage(
            input_tokens=_first_int(raw, "input_tokens"),
            output_tokens=_first_int(raw, "output_tokens"),
            cache_read=_first_int(raw, "cache_read"),
            cache_creation=_first_int(raw, "cache_creation"),
            source="session.usage",
        )

    return RunnerUsage()


def equivalent_cost_usd(model: str, usage: RunnerUsage) -> float | None:
    """List-price value of this usage, or ``None`` when it cannot be stated.

    "Equivalent" because a subscription run's marginal charge is $0; this is what
    the same tokens would have cost on the metered route.  ``None`` when usage is
    unknown or the model has no price entry — a made-up $0.00 next to a real run
    is precisely the misprice ``costs.price_for`` already warns about.
    """
    if not usage.known:
        return None
    name = str(model or "").strip()
    if not name:
        return None
    prices = costs.price_for(name)
    if not any(prices):
        return None
    return costs.cost_usd(
        name,
        int(usage.input_tokens or 0),
        int(usage.output_tokens or 0),
        int(usage.cache_read or 0),
        int(usage.cache_creation or 0),
    )


# --- receipt ----------------------------------------------------------------
@dataclass(frozen=True)
class RunnerReceipt:
    """The runner section of a session receipt — what actually happened.

    Every accounting and terminal field is required on purpose.  A receipt with
    an empty ``billing_class`` or a defaulted ``usage_known`` would be a comfortable
    lie in exactly the place this design promises the truth.  The additive
    interaction/handshake fields default only so older serialized receipts remain
    readable; every new slice writes them explicitly.
    """

    runner: str
    runner_version: str
    runner_protocol: str
    runner_protocol_version: str
    billing_class: str
    billing_mode: str
    credential_family: str
    decision: dict                      # HarnessDecision.to_dict()
    native_session: dict                # NativeSessionRef.to_dict()
    usage: dict
    usage_known: bool
    cost_usd_reported: float | None
    cost_usd_equivalent: float | None
    model: str                          # as the runner reported it; "" when it did not
    settled: bool                       # stopped cleanly this turn — NOT "verified"
    recovery_required: bool
    mutated: bool | None                # None when the mutation check did not complete
    events_digest: str
    event_count: int
    approvals: tuple                    # ApprovalDecision.to_dict(), bounded
    env_receipt: dict                   # {"allowed": [names], "stripped": [names]}
    interactions: tuple = ()            # unresolved PendingInteraction.to_dict(), bounded
    capability_handshake: dict = field(default_factory=dict)
    fallback_from: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        normalized_usage = RunnerUsage.from_dict(_mapping(self.usage))
        recovery_required = self.recovery_required is True
        object.__setattr__(self, "decision", redact_value(_mapping(self.decision)))
        object.__setattr__(self, "native_session",
                           redact_value(_mapping(self.native_session)))
        object.__setattr__(self, "usage", normalized_usage.to_dict())
        object.__setattr__(self, "usage_known",
                           self.usage_known is True and normalized_usage.known)
        object.__setattr__(self, "cost_usd_reported",
                           _opt_float(self.cost_usd_reported))
        object.__setattr__(self, "cost_usd_equivalent",
                           _opt_float(self.cost_usd_equivalent))
        object.__setattr__(self, "model", redact_text(self.model, 256))
        object.__setattr__(self, "settled",
                           self.settled is True and not recovery_required)
        object.__setattr__(self, "recovery_required", recovery_required)
        object.__setattr__(self, "mutated", None if self.mutated is None else
                           self.mutated is True)
        object.__setattr__(self, "event_count", _finite_int(
            self.event_count, 0, minimum=0))
        object.__setattr__(self, "error", redact_text(self.error))
        object.__setattr__(self, "approvals", tuple(
            redact_value(_mapping(item)) for item in _items(self.approvals)[-256:]
            if isinstance(item, dict)))
        object.__setattr__(self, "env_receipt",
                           redact_value(_mapping(self.env_receipt)))
        object.__setattr__(self, "interactions", tuple(
            redact_value(_mapping(item)) for item in _items(self.interactions)[-256:]
            if isinstance(item, dict)))
        object.__setattr__(self, "capability_handshake",
                           redact_value(_mapping(self.capability_handshake)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "runner": self.runner,
            "runner_version": self.runner_version,
            "runner_protocol": self.runner_protocol,
            "runner_protocol_version": self.runner_protocol_version,
            "billing_class": self.billing_class,
            "billing_mode": self.billing_mode,
            "credential_family": self.credential_family,
            "decision": dict(self.decision or {}),
            "native_session": dict(self.native_session or {}),
            "usage": dict(self.usage or {}),
            "usage_known": self.usage_known,
            "cost_usd_reported": self.cost_usd_reported,
            "cost_usd_equivalent": self.cost_usd_equivalent,
            "model": self.model,
            "settled": self.settled,
            "recovery_required": self.recovery_required,
            "mutated": self.mutated,
            "events_digest": self.events_digest,
            "event_count": self.event_count,
            "approvals": [dict(item) for item in self.approvals],
            "env_receipt": dict(self.env_receipt or {}),
            "interactions": [dict(item) for item in self.interactions],
            "capability_handshake": dict(self.capability_handshake or {}),
            "fallback_from": self.fallback_from,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunnerReceipt":
        value = _mapping(value)
        mutated = value.get("mutated")
        approvals = _items(value.get("approvals"))[-256:]
        interactions = _items(value.get("interactions"))[-256:]
        return cls(
            runner=str(value.get("runner") or ""),
            runner_version=str(value.get("runner_version") or ""),
            runner_protocol=str(value.get("runner_protocol") or ""),
            runner_protocol_version=str(value.get("runner_protocol_version") or ""),
            billing_class=str(value.get("billing_class") or "unknown"),
            billing_mode=str(value.get("billing_mode") or "unconfigured"),
            credential_family=str(value.get("credential_family") or ""),
            decision=_mapping(value.get("decision")),
            native_session=_mapping(value.get("native_session")),
            usage=_mapping(value.get("usage")),
            usage_known=_strict_bool(value.get("usage_known")),
            cost_usd_reported=_opt_float(value.get("cost_usd_reported")),
            cost_usd_equivalent=_opt_float(value.get("cost_usd_equivalent")),
            model=str(value.get("model") or ""),
            settled=_strict_bool(value.get("settled")),
            recovery_required=_strict_bool(value.get("recovery_required")),
            mutated=None if mutated is None else _strict_bool(mutated),
            events_digest=str(value.get("events_digest") or ""),
            event_count=_finite_int(value.get("event_count"), 0, minimum=0),
            approvals=tuple(_mapping(item) for item in approvals
                            if isinstance(item, dict)),
            env_receipt=_mapping(value.get("env_receipt")),
            interactions=tuple(_mapping(item) for item in interactions
                               if isinstance(item, dict)),
            capability_handshake=_mapping(value.get("capability_handshake")),
            fallback_from=str(value.get("fallback_from") or ""),
            error=str(value.get("error") or ""),
        )


# --- selection request / decision -------------------------------------------
@dataclass(frozen=True)
class HarnessRequest:
    """The one and only input to :func:`runner_select.decide`.

    Every entry point (run / web / pack / mission-code) builds one of these; the
    selector is then a pure function of it, which is what makes the decision
    reproducible from the receipt.
    """

    surface: str
    intent: str                 # build | plan | test | review
    route_kind: str             # chat | code
    needs: frozenset[str]
    workspace: str              # current | isolated | mission | ""
    workspace_path: str = ""
    workspace_is_git: bool | None = None
    strategy: str = "single"
    provider: str = ""
    model: str | None = None
    provider_family: str = ""
    pin: str = ""               # --runner / ?runner= / roster member — never falls back
    configured: str = "collie"  # settings RUNNER
    pool: tuple[str, ...] = ("collie",)   # settings RUNNER_POOL — consent list for auto
    no_paid_overage: bool = False
    subscription_only: bool = False
    overnight: bool = False
    max_cost_usd: float | None = None
    max_total_tokens: int | None = None
    has_approver: bool = False
    gate_mode: str = ""
    os_name: str = "nt"
    phase: int = CURRENT_PHASE
    history_window_days: int = 14

    def candidates(self) -> tuple[str, ...]:
        """The keys the selector is allowed to consider, in preference order.

        A pin or an explicit ``RUNNER=<key>`` narrows to exactly one candidate, so
        the default ``RUNNER=collie`` path never probes anything — that is what
        keeps the untouched configuration at literally zero added cost.  Only
        ``auto`` opens the pool, and ``collie`` is always appended as the backstop.
        It is still subject to hard billing rules: a filtered-empty pool refuses
        rather than overriding ``no_paid_overage``/``subscription_only``.
        """
        if self.pin:
            return (self.pin,)
        configured = self.configured or "collie"
        if configured != "auto":
            return (configured,)
        ordered = [key for key in self.pool if key]
        if "collie" not in ordered:
            ordered.append("collie")
        seen: set[str] = set()
        out: list[str] = []
        for key in ordered:
            if key not in seen:
                seen.add(key)
                out.append(key)
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "intent": self.intent,
            "route_kind": self.route_kind,
            "needs": sorted(self.needs),
            "workspace": self.workspace,
            "workspace_path": self.workspace_path,
            "workspace_is_git": self.workspace_is_git,
            "strategy": self.strategy,
            "provider": self.provider,
            "model": self.model,
            "provider_family": self.provider_family,
            "pin": self.pin,
            "configured": self.configured,
            "pool": list(self.pool),
            "no_paid_overage": self.no_paid_overage,
            "subscription_only": self.subscription_only,
            "overnight": self.overnight,
            "max_cost_usd": self.max_cost_usd,
            "max_total_tokens": self.max_total_tokens,
            "has_approver": self.has_approver,
            "gate_mode": self.gate_mode,
            "os_name": self.os_name,
            "phase": self.phase,
            "history_window_days": self.history_window_days,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "HarnessRequest":
        value = _mapping(value)
        is_git = value.get("workspace_is_git")
        model = value.get("model")
        return cls(
            surface=str(value.get("surface") or ""),
            intent=str(value.get("intent") or ""),
            route_kind=str(value.get("route_kind") or ""),
            needs=frozenset(str(item) for item in _items(value.get("needs"))),
            workspace=str(value.get("workspace") or ""),
            workspace_path=str(value.get("workspace_path") or ""),
            workspace_is_git=(None if is_git is None else _strict_bool(is_git)),
            strategy=str(value.get("strategy") or "single"),
            provider=str(value.get("provider") or ""),
            model=None if model is None else str(model),
            provider_family=str(value.get("provider_family") or ""),
            pin=str(value.get("pin") or ""),
            configured=str(value.get("configured") or "collie"),
            pool=tuple(str(item) for item in
                       (_items(value.get("pool")) or ("collie",))),
            no_paid_overage=_strict_bool(value.get("no_paid_overage")),
            subscription_only=_strict_bool(value.get("subscription_only")),
            overnight=_strict_bool(value.get("overnight")),
            max_cost_usd=_opt_float(value.get("max_cost_usd")),
            max_total_tokens=_opt_int(value.get("max_total_tokens")),
            has_approver=_strict_bool(value.get("has_approver")),
            gate_mode=str(value.get("gate_mode") or ""),
            os_name=str(value.get("os_name") or "nt"),
            phase=_finite_int(value.get("phase"), CURRENT_PHASE, minimum=1),
            history_window_days=_finite_int(
                value.get("history_window_days"), 14, minimum=0),
        )


@dataclass(frozen=True)
class CandidateScore:
    """One candidate's full worksheet — kept whole in the receipt.

    ``terms`` is never pruned: a decision the operator disagrees with should be
    arguable from the receipt alone, and that requires seeing the term that won.
    """

    key: str
    eligible: bool
    hard_reasons: tuple[str, ...]     # "H#: detail" for each rule that rejected it
    score: float
    terms: dict[str, float]
    soft_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "eligible": self.eligible,
            "hard_reasons": list(self.hard_reasons),
            "score": self.score,
            "terms": dict(self.terms),
            "soft_reasons": list(self.soft_reasons),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CandidateScore":
        value = _mapping(value)
        raw_terms = _mapping(value.get("terms"))
        return cls(
            key=str(value.get("key") or ""),
            eligible=_strict_bool(value.get("eligible")),
            hard_reasons=tuple(str(item) for item in _items(value.get("hard_reasons"))),
            score=_finite_float(value.get("score"), 0.0),
            terms={str(k): _finite_float(v, 0.0)
                   for k, v in list(raw_terms.items())[:256]},
            soft_reasons=tuple(str(item) for item in _items(value.get("soft_reasons"))),
        )


@dataclass(frozen=True)
class HarnessDecision:
    """Which runner will do the work, and why.

    An empty ``runner`` together with a non-empty ``error`` is the fail-closed
    outcome: the caller must refuse to run (exit 2 / ``done{error}``) rather than
    quietly substituting a different one, because a substitution can change which
    account pays.
    """

    runner: str
    source: str                 # user | configured | roster | task-policy | safety-default | fallback
    credential_family: str
    billing_class: str
    billing_mode: str
    reasons: tuple[str, ...]
    rejected: dict              # key -> "H#: detail"
    candidates: tuple[CandidateScore, ...]
    fallback_chain: tuple[str, ...]   # same family AND same billing class only; empty for pins
    probe: dict                 # the chosen runner's RunnerProbe.to_dict()
    probe_digest: str           # sha256 over every candidate probe considered
    signals_digest: str = ""
    error: str = ""
    read_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "error", redact_text(self.error))
        if type(self.read_only) is not bool:
            raise ValueError("read_only must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "runner": self.runner,
            "source": self.source,
            "credential_family": self.credential_family,
            "billing_class": self.billing_class,
            "billing_mode": self.billing_mode,
            "reasons": list(self.reasons),
            "rejected": dict(self.rejected or {}),
            "candidates": [item.to_dict() for item in self.candidates],
            "fallback_chain": list(self.fallback_chain),
            "probe": dict(self.probe or {}),
            "probe_digest": self.probe_digest,
            "signals_digest": self.signals_digest,
            "error": self.error,
            "read_only": self.read_only,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "HarnessDecision":
        value = _mapping(value)
        return cls(
            runner=str(value.get("runner") or ""),
            source=str(value.get("source") or ""),
            credential_family=str(value.get("credential_family") or ""),
            billing_class=str(value.get("billing_class") or "unknown"),
            billing_mode=str(value.get("billing_mode") or "unconfigured"),
            reasons=tuple(str(item) for item in _items(value.get("reasons"))),
            rejected={str(k): str(v) for k, v in _mapping(
                value.get("rejected")).items()},
            candidates=tuple(CandidateScore.from_dict(item)
                             for item in _items(value.get("candidates"))
                             if isinstance(item, dict)),
            fallback_chain=tuple(str(item) for item in _items(
                value.get("fallback_chain"))),
            probe=_mapping(value.get("probe")),
            probe_digest=str(value.get("probe_digest") or ""),
            signals_digest=str(value.get("signals_digest") or ""),
            error=str(value.get("error") or ""),
            read_only=value.get("read_only", False),
        )


# --- snapshot -> RunResult --------------------------------------------------
def snapshot_to_run_result(snapshot: Any, spec: HarnessSpec, probe: RunnerProbe, *,
                           decision: HarnessDecision | None = None,
                           model: str = "", provider: str = "",
                           task_id: str = "") -> RunResult:
    """Project an ``agent_runners.RunnerSnapshot`` onto the recorder's RunResult.

    Pure: no I/O, no clock, no process.  ``run_adhoc`` and ``run_mission_slice``
    both go through here so a run recorded by either looks identical in runs.db.

    Two invariants worth stating out loud:

    * ``verified`` is always False here.  Settling cleanly is not verification —
      only the host verifier that runs *after* the runner exits may set it, which
      is the whole point of Collie's evidence reversal.
    * When usage is unknown the token columns are left ``None`` (SQL NULL), not 0.
      A zero would make an unmeasured external run average into the dashboard as a
      free one.  Callers that format these with ``%d`` must handle None.

    ``model``/``provider``/``task_id`` are keyword extras the snapshot cannot
    supply: the runner protocols in phase 1 do not echo back which model answered,
    so the caller passes what it asked for (or "" — a Brain row must not invent a
    model name).
    """
    # Pairing a codex probe (or a decision naming a third runner) with a claude
    # spec would mislabel which account paid for the run.  That must be a crash,
    # not a footnote in a receipt nobody reads.
    if probe.key and spec.key and probe.key != spec.key:
        raise RunnerError("probe/spec mismatch: %r vs %r" % (probe.key, spec.key))
    if decision is not None and decision.runner and decision.runner != spec.key:
        raise RunnerError("decision/spec mismatch: %r vs %r" % (decision.runner, spec.key))

    usage = usage_to_collie(spec.key, dict(getattr(snapshot, "usage", None) or {}))
    started = float(getattr(snapshot, "started_at", 0.0) or 0.0)
    finished = float(getattr(snapshot, "finished_at", 0.0) or 0.0)
    wall_ms = int(max(0.0, finished - started) * 1000) if started and finished else 0

    if usage.known:
        input_tokens: int | None = int(usage.input_tokens or 0)
        output_tokens: int | None = int(usage.output_tokens or 0)
        cache_read: int | None = int(usage.cache_read or 0)
        cache_creation: int | None = int(usage.cache_creation or 0)
        total_tokens: int | None = input_tokens + output_tokens + cache_read + cache_creation
        cost_usd = equivalent_cost_usd(model, usage)
    else:
        input_tokens = output_tokens = cache_read = cache_creation = total_tokens = None
        cost_usd = None

    settled = bool(getattr(snapshot, "settled", False))
    error = redact_text(getattr(snapshot, "error", "") or "")
    # A stop is a terminal fact of its own, and it is the snapshot — not the
    # error string — that records it.  Dropping it here made every consumer that
    # reads `RunResult.canceled`/`stop_reason` (`recorder.run_stop_reason`, the
    # Web `done` event, the durable run receipt) describe a stopped worker turn
    # as an ordinary failure — and, when the worker's protocol still reported a
    # clean terminal event after the interrupt (an App Server `turn/completed`
    # racing `turn/interrupt`, which leaves `error` empty), as a COMPLETED one.
    cancelled = bool(getattr(snapshot, "cancelled", False))
    return RunResult(
        task_id=task_id,
        harness=spec.key,                    # runs.db groups by this; runner keys never
                                             # collide with the benchmark adapter names
        model=model,
        provider=provider or spec.credential_family,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read=cache_read,
        cache_creation=cache_creation,
        # Each start/resume of an external runner is one Collie turn; the runner's
        # internal turns are its own business and are not comparable with ours.
        turns=max(0, int(getattr(snapshot, "invocation", 0) or 0)),
        wall_ms=wall_ms,
        success=bool(settled and not error
                     and not getattr(snapshot, "timed_out", False)
                     and not cancelled
                     and not getattr(snapshot, "recovery_required", False)),
        canceled=cancelled,
        # Kept in the vocabulary every surface already speaks; "" leaves the
        # verdict to `run_stop_reason`, exactly as before.
        stop_reason="canceled" if cancelled else "",
        verified=False,                      # host verifier only — see docstring
        cost_usd=cost_usd,
        answer=str(getattr(snapshot, "final_output", "") or ""),
        error=error,
        retry_at=getattr(snapshot, "retry_at", 0),
        messages=[],                         # an external runner keeps its own thread
    )


__all__ = [
    "ApprovalDecision", "ApprovalRequest", "BILLING_CLASSES", "BILLING_MODE_OF",
    "CANONICAL_TYPES", "CURRENT_PHASE", "CandidateScore", "CanonicalEvent",
    "CapabilityHandshake",
    "EXTERNAL_ALLOWED_SURFACES", "HarnessDecision", "HarnessRequest", "HarnessSpec",
    "NOT_IMPLEMENTED_PREFIX", "NativeSessionRef", "PendingInteraction",
    "QueuedTurnMessage", "RunInput",
    "RunnerBillingError",
    "RunnerCapabilities", "RunnerError", "RunnerProbe", "RunnerProtocolError",
    "RunnerReceipt", "RunnerSelectionError", "RunnerUnavailableError", "RunnerUsage",
    "SURFACES", "equivalent_cost_usd", "family_of_provider", "redact_text",
    "redact_value", "snapshot_to_run_result", "stable_digest", "usage_to_collie",
]
