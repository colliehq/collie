"""Provider-neutral messages, request authority and failure accounting.

This module owns the canonical definitions shared by the kernel and optional
model adapters. Importing it neither constructs a provider nor discovers accounts.
The legacy harness.providers exports refer to these same objects.
"""
from __future__ import annotations
import contextvars
import json
import math
import re
import time
import urllib.error
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

def est_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate (~4 chars/token). Seam: swap tiktoken."""
    if not text:
        return 0
    return max(1, len(text) // 4)

@dataclass
class ToolCall:
    id: str
    name: str
    args: dict

@dataclass
class Usage:
    """Provider-reported token usage, normalized to the Anthropic convention.

    Invariant (the semantic anchor the cache ledger and prefix-measurement both rely on):
        input_tokens   = UNCACHED input only (cached bytes are NOT re-counted here)
        full input sent this request = input_tokens + cache_read + cache_creation
    Every provider adapter must normalize to this — OpenAI's `prompt_tokens` INCLUDES the
    cached portion, so `_openai_usage` subtracts it back out (else total double-counts).
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read += other.cache_read
        self.cache_creation += other.cache_creation

def _openai_usage(u: dict) -> Usage:
    """OpenAI-compatible `usage` dict -> normalized Usage. `prompt_tokens` INCLUDES cached
    tokens, so subtract cached_tokens to keep input_tokens UNCACHED (Anthropic convention);
    the full input is then input + cache_read + cache_creation, with no double count."""
    u = u if isinstance(u, dict) else {}
    details = u.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}

    def count(value) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    cached = count(details.get("cached_tokens", 0))
    # Content-free Collie sidecars can preserve cache-write accounting through this optional
    # extension. Generic OpenAI-compatible providers omit it and retain their existing semantics.
    cache_creation = count(u.get("cache_creation_input_tokens", 0))
    prompt = count(u.get("prompt_tokens", 0))
    return Usage(input_tokens=max(0, prompt - cached - cache_creation),
                 output_tokens=count(u.get("completion_tokens", 0)),
                 cache_read=cached, cache_creation=cache_creation)

@dataclass
class Completion:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "end_turn"   # "end_turn" | "tool_use" | "error" | "length"
    error_status: int = 0           # HTTP status on an error completion (0 = none/unknown)
    error_detail: str = ""          # raw error body/text — classify_error reads THIS, not prose
    # Stable, content-free provider/bridge error identifier when one is available.  Keeping this
    # separate from prose lets the host distinguish a model response-contract miss from an
    # unrelated HTTP 422 without guessing from status text.
    error_code: str = ""
    # Physical provider/CLI requests consumed to produce this logical completion.
    # Most adapters issue exactly one; format-repairing adapters must report more.
    # An adapter that failed BEFORE issuing anything (a denied request reservation, a
    # refusal before the worker was spawned) reports 0 — read it with issued_requests().
    request_count: int = 1
    # Extended-thinking blocks ({"type":"thinking","thinking":..,"signature":..} /
    # {"type":"redacted_thinking","data":..}) returned this turn. When thinking is enabled with
    # tool use, the API REQUIRES the signed thinking block to be replayed as the first block of
    # the assistant turn on the next request — so the loop stores these and _to_anthropic prepends
    # them. Empty when thinking is off (the normal path).
    thinking_blocks: list = field(default_factory=list)
    retry_at: int = 0              # provider-attested quota reset (UTC epoch seconds)
    # A structural refusal category, never the rejected assistant text. Consumers
    # allowlist known categories before logging or emitting this provider metadata.
    # Append fields to preserve existing positional request/thinking accounting.
    contract_reason: str = ""

def provider_retry_at(value, now=None) -> int:
    """An upcoming provider reset, or zero for absent/stale/invalid metadata.

    This is machine metadata, never a date extracted from assistant prose.
    Eight days covers weekly windows without accepting unbounded waits.
    """
    now = time.time() if now is None else now
    return value if type(value) is int and now < value <= now + 8 * 86400 else 0

def issued_requests(comp, maximum: int | None = None, default: int = 1) -> int:
    """The physical provider requests a completion says it issued — ONE policy for every ledger.

    `Completion.request_count` documents requests that really left the host, so an adapter that
    failed before issuing one (the SDK's denied request reservation, an attachment refused before
    spawn) honestly reports 0 and must add 0 to the run's model-call ledger: a reservation the
    gate refused is not provider usage. A valid count above 1 (a format-repairing adapter's real
    attempts) is counted in full.

    Everything the host cannot READ as a count — absent, None, bool, str, non-finite or
    non-integral, negative, or beyond `maximum` where the caller documents a ceiling — falls back
    to the conservative `default` of one request, which is the accounting this code has always
    used for a provider that did not describe its usage: an adapter that ran and then lied about
    or omitted its count is far likelier to have spent a request than to have spent none.
    (`bounded_int`-compatible: integral floats are accepted, bools and strings are not.)
    """
    return request_count_of(getattr(comp, "request_count", None), maximum, default)

def request_count_of(value, maximum: int | None = None, default: int = 1) -> int:
    """`issued_requests` policy on a bare number — for a count already carried out of a
    completion (the critic's, held on the Harness between the review and its accounting)."""
    if isinstance(value, bool) or isinstance(value, str):
        return default
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return default
        value = int(value)
    if not isinstance(value, int):
        return default
    if value < 0 or (maximum is not None and value > maximum):
        return default
    return value

def content_text(content) -> str:
    """The plain-text of a message's content (drops images). str -> itself; list -> its text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text").strip()
    return str(content or "")

def _error_completion(name: str, err, usage=None, status: int = 0) -> Completion:
    """CONTRACT helper: turn any transport/API/parse failure into an error Completion instead of
    raising, so the loop has ONE failure path (point 4). Carries error_status/error_detail for the
    host's retry classifier (point 5)."""
    detail = ""
    error_code = ""
    if isinstance(err, urllib.error.HTTPError):
        status = status or err.code
        try:
            # Error responses are bounded before parsing.  The normalized subscription sidecar
            # includes only a content-free error code plus numeric usage here, allowing a request
            # which physically completed but failed response bridging to remain budget-visible.
            detail = err.read(64 * 1024).decode("utf-8", "ignore")
        except Exception:
            detail = str(err)
        try:
            payload = json.loads(detail)
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else ""
            if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
                error_code = code
            if usage is None and isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
                usage = _openai_usage(payload["usage"])
        except Exception:
            # Error normalization must never turn a provider failure back into an exception.  A
            # malformed optional usage object degrades to zero usage while status/detail survive.
            pass
        detail = detail[:1200]
        # Keep the rate-limit headers WITH the failure. "You're out of extra usage" says a request
        # was refused for want of capacity but not which bucket was full, and the answer is only
        # observable at the moment of refusal — a check afterwards reads a window that has already
        # moved on (8% utilization, 67 seconds after a rejection claiming none). Without this the
        # only way to explain an intermittent refusal is to guess.
        try:
            hs = {k.lower(): v for k, v in err.headers.items()}
            rl = {k: v for k, v in hs.items()
                  if "ratelimit" in k or k in ("retry-after", "anthropic-organization-id")}
            if rl:
                detail += " | limits: " + json.dumps(rl, sort_keys=True)
        except Exception:
            pass
        from .redact import redact as _redact_error
        detail = _redact_error(detail, {})[:1200]
        msg = "HTTP %d: %s" % (err.code, detail)
    else:
        from .redact import redact as _redact_error
        detail = _redact_error(str(err)[:1200], {})
        msg = "%s: %s" % (type(err).__name__, detail)
    # 300 was enough for a message and not for the evidence behind it: the body alone already fills
    # it, so appending the limit headers above would have written them straight into the truncation.
    return Completion(text="ERROR(%s): %s" % (name, msg), stop_reason="error",
                      usage=usage or Usage(), error_status=status, error_detail=detail[:1200],
                      error_code=error_code)

_OVERFLOW_RE = re.compile(
    r"prompt is too long|request_too_large"          # Anthropic 400/413
    r"|maximum context length|context.?length.?exceeded|exceeds the context window"  # OpenAI/DeepSeek
    r"|reduce the length of the messages"            # Groq
    r"|exceeds the available context size", re.I)     # ollama / llama.cpp

_NOT_OVERFLOW_RE = re.compile(r"rate.?limit|too many requests|throttl", re.I)

_TERMINAL_RE = re.compile(
    r"insufficient_quota|insufficient balance|quota exceeded|out of budget|billing"  # DeepSeek 402='Insufficient Balance'
    r"|invalid.?api.?key|authentication_error|permission", re.I)

_RETRYABLE_RE = re.compile(
    r"overloaded|rate.?limit|too many requests|throttl|timed?.?out|timeout"  # throttl ← Bedrock ThrottlingException
    r"|connection (reset|refused|aborted|error)|remote end closed|incomplete read"
    # Windows words a reset and an abort differently (WinError 10054 / 10053), and a real run on
    # 2026-08-01 stopped on the first one as "no known pattern" instead of being retried.
    r"|forcibly closed by the remote host|aborted by the software in your host"
    r"|winerror 1005[34]|connectionreseterror|connectionabortederror"
    # Windows' connect timeout (10060) never says "timed out", and its refusal (10061) never
    # says "connection refused"; the POSIX wordings already matched. IncompleteRead is the
    # exception's name, with no space.
    r"|did not properly respond after a period of time|winerror 1006[01]|actively refused"
    r"|incompleteread"
    r"|eof occurred|temporarily unavailable|server.?error|internal.?error"
    r"|service.?unavailable|stream error|stream ended", re.I)

_RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504, 522, 524, 529}

_PROTOCOL_CODES = {"response_contract_error"}

_PROTOCOL_RE = re.compile(
    r"response_contract_error|assistant response was not bridgeable"
    r"|assistant (?:violated|did not match) (?:the )?(?:sidecar )?response contract",
    re.I)

def is_known_terminal(text: str) -> bool:
    """True iff the text matches a failure we RECOGNISE as fatal (bad key, no quota, no permission).

    classify_error() returns 'terminal' both for those and for anything it does not recognise at
    all — the fall-through at the end. Same word, two very different confidences, and the report
    read the same either way. Callers that show a human why a run stopped need to be able to say
    "this is fatal" apart from "we have never seen this and did not retry it".
    """
    return bool(_TERMINAL_RE.search(text or ""))

def is_overflow(text: str) -> bool:
    """True iff the error text means the INPUT exceeded the model's context window (point 9).
    Exclude rate-limit/throttle wording first (those say 'too many tokens' but aren't overflow)."""
    t = text or ""
    return bool(_OVERFLOW_RE.search(t)) and not _NOT_OVERFLOW_RE.search(t)

_EXHAUSTED_RE = re.compile(
    r"usage.?limit.?(reached|exceeded)|quota.?(exceeded|exhausted)|insufficient.?quota"
    # OpenAI puts the noun last ("exceeded your current quota"); requiring quota-then-verb missed
    # the single most common metered exhaustion string there is. Bounded so it cannot span clauses.
    r"|exceeded your .{0,24}quota"
    r"|out of credits?|credit balance is too low"
    r"|monthly.?(limit|quota).?(reached|exceeded)", re.I)

def is_exhausted(text: str) -> bool:
    """True iff the failure means this provider's ALLOWANCE is spent — not that the call went wrong.

    Worth separating from a rate limit even though both arrive as 429, because the remedy inverts.
    "Too many requests" means slow down, and waiting a few seconds fixes it. `usage_limit_reached`
    on a flat plan means come back in two days: every retry is a guaranteed identical refusal, and
    the backoff spent discovering that is pure waste — three attempts, three walls of the same JSON,
    repeated at whoever asks next for as long as the window lasts.
    """
    return bool(_EXHAUSTED_RE.search(text or ""))

def classify_error(detail: str, status: int = 0, code: str = "") -> str:
    """Classify an error completion without owning retry policy.

    'protocol' | 'overflow' | 'exhausted' | 'retryable' | 'terminal' from detail+status+code
    (point 5). Pure — no policy. Priority: overflow-text > protocol(code|422) > exhausted-text >
    terminal-text > retryable(status|text) > terminal (unknown fails fast: never burn backoff
    blind).

    ``protocol`` is a completed model response that could not be bridged to the active structured
    response contract.  It is deliberately distinct from transport retryability: the host may give
    the model one corrective turn, with no network backoff.  Unknown failures still fail fast.

    'exhausted' sits ABOVE both terminal and retryable deliberately. Its text can match either —
    "quota" reads as terminal, its 429 reads as retryable — and neither answer is useful: one gives
    up without saying the plan returns, the other retries something that cannot succeed until it
    does. Callers that only know 'retryable' keep working: everything else already falls through to
    their terminal path, which is the right handling for a spent plan. The one caller that must
    know the difference is the loop: a provider-attested reset (Completion.retry_at) is still
    honoured as a wait for an exhausted plan, not spent as blind retries — see Harness.run.
    """
    t = detail or ""
    if is_overflow(t):
        return "overflow"
    if code in _PROTOCOL_CODES or (status == 422 and _PROTOCOL_RE.search(t)):
        return "protocol"
    if is_exhausted(t):
        return "exhausted"
    if _TERMINAL_RE.search(t):
        return "terminal"
    if status in _RETRYABLE_HTTP or _RETRYABLE_RE.search(t):
        return "retryable"
    return "terminal"

def _tc_fields(tc):
    """(id, name, args) from a ToolCall dataclass OR a plain dict. Seeded/continued history can
    carry either shape (sessions serialize to dicts), so EVERY provider's message conversion must
    tolerate both instead of crashing on tc.id — the 'str/dict has no attribute id' class of bug."""
    if isinstance(tc, dict):
        return tc.get("id"), tc.get("name"), tc.get("args") or {}
    return getattr(tc, "id", None), getattr(tc, "name", None), getattr(tc, "args", {}) or {}

def unique_tool_history(messages):
    """Project legacy repeated call IDs without rewriting the saved journal.

    Older CLI adapters derived IDs from response length. Sequential repeated
    IDs can be paired unambiguously; overlapping ones cannot. Stable projected
    IDs let a saved CLI conversation switch providers without invalid history.
    """
    used, pending, out = set(), {}, []
    for index, message in enumerate(messages):
        if message.get("role") == "assistant" and message.get("tool_calls"):
            calls, changed = [], False
            for call_index, call in enumerate(message["tool_calls"]):
                cid, name, args = _tc_fields(call)
                if cid and cid in pending:
                    raise ValueError("conversation has overlapping tool calls with the same ID")
                projected = cid
                if cid and cid in used:
                    projected = "history_" + uuid.uuid5(
                        uuid.NAMESPACE_URL, "%s:%d:%d" % (cid, index, call_index)).hex
                    while projected in used:
                        projected = "history_" + uuid.uuid5(uuid.NAMESPACE_URL, projected).hex
                    changed = True
                used.add(projected)
                if cid:
                    pending[cid] = projected
                calls.append({"id": projected, "name": name, "args": args})
            out.append(dict(message, tool_calls=calls) if changed else message)
        elif message.get("role") == "tool":
            cid = message.get("tool_call_id")
            projected = pending.pop(cid, cid)
            out.append(dict(message, tool_call_id=projected) if projected != cid else message)
        else:
            out.append(message)
    return out

class ModelProvider:
    name = "base"
    model = "base"
    reports_cache = False   # does this provider's usage report prompt-cache hits? (seeds the ledger's
                            # sticky flag so a 100%-from-turn-0 prefix bust is still detected — a bust
                            # reports zero cache fields, so inferring "reports cache" from a nonzero
                            # field would never fire for the worst regression)

    @contextmanager
    def request_authority(self, request_gate, request_complete=None,
                          request_scope=None):
        """Bind one Mission's physical-request authority to this execution context.

        MissionService can run multiple Missions through one provider instance. A
        ContextVar keeps their durable budget reservations from crossing threads,
        unlike temporarily assigning ``provider.request_gate`` on the shared object.
        Isolated code workers retain the attribute seam for backwards compatibility.
        """
        token = _REQUEST_AUTHORITY.set(
            (self, request_gate, request_complete, str(request_scope or "")))
        try:
            yield
        finally:
            _REQUEST_AUTHORITY.reset(token)

    def current_request_authority(self):
        owner, request_gate, request_complete, _request_scope = _REQUEST_AUTHORITY.get()
        if owner is not self:
            return None, None
        return request_gate, request_complete

    def current_request_scope(self) -> str:
        """Return the caller's cancellation scope for this execution context."""
        owner, _request_gate, _request_complete, request_scope = \
            _REQUEST_AUTHORITY.get()
        return request_scope if owner is self else ""

    def complete(self, system: str, messages: list, tool_schemas: list, on_text=None) -> Completion:
        """CONTRACT: never raise for a transport / HTTP / JSON-decode failure — return an error
        Completion (stop_reason='error') via _error_completion so the loop has ONE failure path.
        Constructor config errors (missing key) still raise, to fail fast."""
        raise NotImplementedError

_REQUEST_AUTHORITY = contextvars.ContextVar(
    "collie_model_request_authority", default=(None, None, None, ""))


def exhaustion_message(provider_name: str, detail: str, status: int = 0) -> str:
    """Describe quota exhaustion without discovering credentials or transports."""
    return ("exhausted: %s has spent its allowance.\n  "
            "Wait for its quota to reset, or explicitly choose another configured provider.\n  "
            "provider said:%s %s" % (
                provider_name or "the provider",
                (" HTTP %d" % status) if status else "", (detail or "").strip()[:300]))
