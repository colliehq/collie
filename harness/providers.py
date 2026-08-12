"""Model provider seam.

A ModelProvider takes (system, messages, tool_schemas) and returns a Completion.
Everything above this layer is provider-agnostic, so the same loop runs against
a deterministic MockProvider ($0, for testing the plumbing) or the real
AnthropicProvider (for real task runs and CC comparison), or a future local model.

Internal message shape (provider-neutral):
    {"role": "user"|"assistant", "content": str}
    {"role": "assistant", "tool_calls": [ToolCall, ...]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
import uuid
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
    the full input is then input + cache_read, no double count."""
    u = u or {}
    cached = (u.get("prompt_tokens_details", {}) or {}).get("cached_tokens", 0) or 0
    return Usage(input_tokens=max(0, (u.get("prompt_tokens", 0) or 0) - cached),
                 output_tokens=u.get("completion_tokens", 0) or 0,
                 cache_read=cached)


@dataclass
class Completion:
    text: str = ""
    tool_calls: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = "end_turn"   # "end_turn" | "tool_use" | "error" | "length"
    error_status: int = 0           # HTTP status on an error completion (0 = none/unknown)
    error_detail: str = ""          # raw error body/text — classify_error reads THIS, not prose
    # Extended-thinking blocks ({"type":"thinking","thinking":..,"signature":..} /
    # {"type":"redacted_thinking","data":..}) returned this turn. When thinking is enabled with
    # tool use, the API REQUIRES the signed thinking block to be replayed as the first block of
    # the assistant turn on the next request — so the loop stores these and _to_anthropic prepends
    # them. Empty when thinking is off (the normal path).
    thinking_blocks: list = field(default_factory=list)


_LENGTH_STOPS = {"length", "max_tokens"}   # provider-specific output-truncation reasons


def _norm_stop(sr: str) -> str:
    """Canonicalize a provider's output-truncation reason to 'length' (point 1)."""
    return "length" if sr in _LENGTH_STOPS else (sr or "end_turn")


# --------------------------------------------------------------------------- #
#  Multimodal content. A message's `content` is normally a plain str; a user can also attach images,
#  in which case content is a LIST of canonical blocks: {"type":"text","text":…} and
#  {"type":"image","media_type":"image/png","data":"<base64>"}. Each provider re-shapes the image
#  block into its own vision format; text-only paths (memory, titles, non-vision providers) read the
#  text via content_text() so a list never crashes them or leaks a base64 blob.
# --------------------------------------------------------------------------- #
def content_text(content) -> str:
    """The plain-text of a message's content (drops images). str -> itself; list -> its text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text").strip()
    return str(content or "")


def _has_images(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "image" and b.get("data") for b in content)


def _anthropic_content(content):
    """Canonical blocks -> Anthropic content blocks (text stays text; image -> base64 source)."""
    blocks = []
    for b in content:
        if not isinstance(b, dict):
            blocks.append({"type": "text", "text": str(b)})
        elif b.get("type") == "image" and b.get("data"):
            blocks.append({"type": "image", "source": {"type": "base64",
                           "media_type": b.get("media_type", "image/png"), "data": b["data"]}})
        elif (b.get("text") or "").strip():
            blocks.append({"type": "text", "text": b["text"]})
    return blocks or "(no content)"


def _mark_cache_block(msg):
    """Return a copy of an Anthropic message with an ephemeral cache_control breakpoint on its LAST
    content block (string content is promoted to a text block so the marker has somewhere to live)."""
    c = msg.get("content")
    if isinstance(c, list) and c:
        c2 = list(c)
        c2[-1] = {**c2[-1], "cache_control": {"type": "ephemeral"}}
        return {**msg, "content": c2}
    if isinstance(c, str):
        return {**msg, "content": [{"type": "text", "text": c or "(no content)",
                                    "cache_control": {"type": "ephemeral"}}]}
    return msg


def _apply_history_cache(msgs, stable_upto):
    """Add ONE rolling cache_control breakpoint inside the conversation history so the large, growing
    message prefix caches turn-to-turn instead of being re-billed in full every turn (the difference
    between ~2% and ~90% cache hit on long runs). Anthropic caches the prefix UP TO the breakpoint and
    matches the longest identical prefix from a prior request — so the breakpoint MUST land on a
    byte-stable message. The composer elides (mutates) tool outputs older than its recent window, so
    the stable region is exactly messages[:stable_upto] (stable_upto = ComposeMeta.elide_from). We mark
    the last message of that region. For short, un-elided threads (stable_upto<=0) the whole history is
    append-only and stable, so we mark the final message instead. Mutates msgs in place. No-op if empty.
    """
    n = len(msgs)
    if n == 0:
        return
    bp = (stable_upto - 1) if (stable_upto and stable_upto > 0) else (n - 1)
    bp = max(0, min(bp, n - 1))
    msgs[bp] = _mark_cache_block(msgs[bp])


def _openai_content(content):
    """Canonical blocks -> OpenAI vision format (text parts + image_url data URIs)."""
    parts = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "image" and b.get("data"):
            parts.append({"type": "image_url", "image_url": {
                "url": "data:%s;base64,%s" % (b.get("media_type", "image/png"), b["data"])}})
        elif isinstance(b, dict) and (b.get("text") or "").strip():
            parts.append({"type": "text", "text": b["text"]})
    return parts or [{"type": "text", "text": "(no content)"}]


def _error_completion(name: str, err, usage=None, status: int = 0) -> Completion:
    """CONTRACT helper: turn any transport/API/parse failure into an error Completion instead of
    raising, so the loop has ONE failure path (point 4). Carries error_status/error_detail for the
    host's retry classifier (point 5)."""
    detail = ""
    if isinstance(err, urllib.error.HTTPError):
        status = status or err.code
        try:
            detail = err.read().decode("utf-8", "ignore")[:300]
        except Exception:
            detail = str(err)
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
        msg = "HTTP %d: %s" % (err.code, detail)
    else:
        detail = str(err)
        msg = "%s: %s" % (type(err).__name__, detail)
    # 300 was enough for a message and not for the evidence behind it: the body alone already fills
    # it, so appending the limit headers above would have written them straight into the truncation.
    return Completion(text="ERROR(%s): %s" % (name, msg), stop_reason="error",
                      usage=usage or Usage(), error_status=status, error_detail=detail[:1200])


# ---- error classification (pure data — NO retry policy here; the host owns policy, pi retry.ts) --
# One overflow classifier (point 9); point 5's classify_error calls it for the 'overflow' arm.
# Each pattern is annotated with the wording/provider that motivated it (lab-notebook discipline);
# 'too many tokens' is deliberately withheld until a real run_id shows it (openrouter/Bedrock proxy
# wording, not Anthropic/OpenAI) — add it back with the incident, not speculatively.
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
    r"|eof occurred|temporarily unavailable|server.?error|internal.?error"
    r"|service.?unavailable|stream error|stream ended", re.I)
_RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504, 522, 524, 529}


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


def classify_error(detail: str, status: int = 0) -> str:
    """'retryable' | 'terminal' | 'overflow' from an error completion's detail+status (point 5).
    Pure — no policy. Priority: overflow-text > terminal-text > retryable(status|text) > terminal
    (unknown fails fast: never burn backoff blind, matching pi where no-match means no retry)."""
    t = detail or ""
    if is_overflow(t):
        return "overflow"
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


class ModelProvider:
    name = "base"
    model = "base"
    reports_cache = False   # does this provider's usage report prompt-cache hits? (seeds the ledger's
                            # sticky flag so a 100%-from-turn-0 prefix bust is still detected — a bust
                            # reports zero cache fields, so inferring "reports cache" from a nonzero
                            # field would never fire for the worst regression)

    def complete(self, system: str, messages: list, tool_schemas: list, on_text=None) -> Completion:
        """CONTRACT: never raise for a transport / HTTP / JSON-decode failure — return an error
        Completion (stop_reason='error') via _error_completion so the loop has ONE failure path.
        Constructor config errors (missing key) still raise, to fail fast."""
        raise NotImplementedError


def measure_prefix(provider: "ModelProvider", system: str, tool_schemas: list) -> int:
    """Provider-measured prefix size via a two-request differential:
        A = full request (system + tool schemas),  B = bare request (empty system, no tools)
    return (A's total input) - (B's total input) = the tokens the prefix actually costs on THIS
    provider, per its own usage. Copies pi's usage-anchoring idea: trust the provider, never chars/4.
    Returns 0 (unknown) if either side yields no usable usage. Cheap: max_tokens squeezed to a sliver."""
    prev = getattr(provider, "max_tokens", None)
    try:
        if prev is not None:
            try: provider.max_tokens = max(1, prev // 16)
            except Exception: pass
        def _total(sys_txt, schemas):
            try:
                c = provider.complete(sys_txt, [{"role": "user", "content": "."}], schemas)
            except Exception:
                return None                       # AnthropicProvider raises rather than returns error
            if c.stop_reason == "error":
                return None
            u = c.usage
            tot = u.input_tokens + u.cache_read + u.cache_creation
            return tot if tot > 0 else None
        a = _total(system, tool_schemas)
        b = _total(".", [])                        # 1-char sentinel: Anthropic 400s on empty system text
        if a is None or b is None:
            return 0
        return max(0, a - b)
    finally:
        if prev is not None:
            try: provider.max_tokens = prev
            except Exception: pass


# --------------------------------------------------------------------------- #
#  MockProvider — deterministic, $0. Drives the loop with real tool use so the
#  whole harness (tools, memory, context, metrics, dashboard) is testable
#  offline. It is a tiny rule-based "planner", NOT a language model.
# --------------------------------------------------------------------------- #
class MockProvider(ModelProvider):
    name = "mock"
    model = "mock-planner-v1"

    def _first_user_task(self, messages: list) -> str:
        for m in messages:
            if m.get("role") == "user" and m.get("content"):
                return content_text(m["content"])           # str or multimodal-list -> its text
        return ""

    def _has_tool_result(self, messages: list) -> bool:
        return any(m.get("role") == "tool" for m in messages)

    def _n_assistant(self, messages: list) -> int:
        return sum(1 for m in messages if m.get("role") == "assistant")

    def complete(self, system: str, messages: list, tool_schemas: list, on_text=None) -> Completion:
        task = self._first_user_task(messages)
        t = task.lower()
        first_turn = self._n_assistant(messages) == 0

        # token bookkeeping (simulate prefix caching: created once, read after)
        sys_tok = est_tokens(system)
        msg_tok = sum(est_tokens(json.dumps(m, ensure_ascii=False, default=str)) for m in messages)
        usage = Usage(
            input_tokens=msg_tok,
            cache_creation=sys_tok if first_turn else 0,
            cache_read=0 if first_turn else sys_tok,
        )

        if not self._has_tool_result(messages):
            # decide a single tool call from the task
            tc = self._plan(task, t)
            usage.output_tokens = est_tokens(json.dumps(tc.args, ensure_ascii=False)) + 12
            return Completion(tool_calls=[tc], usage=usage, stop_reason="tool_use")

        # a tool result is present -> produce the final answer from it
        last_result = ""
        for m in reversed(messages):
            if m.get("role") == "tool":
                last_result = str(m.get("content", ""))
                break
        answer = self._finalize(task, last_result)
        usage.output_tokens = est_tokens(answer)
        return Completion(text=answer, usage=usage, stop_reason="end_turn")

    def _plan(self, task: str, t: str) -> ToolCall:
        cid = "mock_%d" % int(time.time() * 1000 % 1_000_000)
        if any(k in t for k in ("recall", "what did we decide", "之前", "记得", "past decision", "from memory")):
            return ToolCall(cid, "memory_search", {"query": task, "k": 5})
        if "todo" in t:
            return ToolCall(cid, "grep", {"pattern": "TODO", "path": "."})
        if "python file" in t or ".py" in t or ("count" in t and "file" in t):
            return ToolCall(cid, "bash", {"command": "find . -maxdepth 3 -name '*.py' | wc -l"})
        m = re.search(r"read (?:the file )?([^\s]+)", t)
        if m:
            return ToolCall(cid, "read_file", {"path": m.group(1)})
        return ToolCall(cid, "bash", {"command": "ls -la"})

    def _finalize(self, task: str, result: str) -> str:
        result = result.strip()
        if len(result) > 500:
            result = result[:500] + " …"
        return "Based on the tool output:\n%s" % result


# --------------------------------------------------------------------------- #
#  AnthropicProvider — real Claude API, same interface. Uses ANTHROPIC_API_KEY.
#  Zero third-party deps (stdlib urllib). Sends cache_control on the system
#  prompt so the prefix is cached (the #1 cost lever, per the report).
# --------------------------------------------------------------------------- #
def _parse_anthropic_stream(r, on_text):
    """Parse Anthropic's SSE stream -> (text, tool_calls, usage_dict, stop_reason, error_detail),
    calling on_text(delta) as text arrives (real token streaming). Shared by the API + OAuth
    providers; only used when a caller passes on_text (interactive), so the benchmark path is
    untouched. error_detail carries the raw stream-error message for classify_error (point 5)."""
    text, blocks, stop_reason = "", {}, "end_turn"
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0}
    for raw in r:
        line = raw.decode("utf-8", "ignore").strip()
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except Exception:
            continue
        t = ev.get("type")
        if t == "message_start":
            usage.update(ev.get("message", {}).get("usage", {}) or {})
        elif t == "content_block_start":
            cb = ev.get("content_block", {})
            blocks[ev.get("index")] = {"type": cb.get("type"), "id": cb.get("id"),
                                       "name": cb.get("name"), "json": ""}
        elif t == "content_block_delta":
            d = ev.get("delta", {})
            if d.get("type") == "text_delta":
                piece = d.get("text", "")
                text += piece
                if on_text and piece:
                    try:
                        on_text(piece)
                    except Exception:
                        pass
            elif d.get("type") == "input_json_delta":
                b = blocks.get(ev.get("index"))
                if b is not None:
                    b["json"] += d.get("partial_json", "")
        elif t == "message_delta":
            stop_reason = ev.get("delta", {}).get("stop_reason") or stop_reason
            usage.update(ev.get("usage", {}) or {})
        elif t == "error":
            # Anthropic can emit `event: error` (overloaded_error, etc.) mid-stream AFTER a 200,
            # so no HTTPError is raised. Without this branch the parser returns the partial text as
            # a successful turn and the loop consolidates a truncated answer. Surface it instead.
            err = ev.get("error", {}) or {}
            msg = err.get("message") or err.get("type") or "stream error"
            if on_text:
                try:
                    on_text("\n[stream error: %s]" % msg)
                except Exception:
                    pass
            return (text + "\n[ERROR: %s]" % msg, [], usage, "error", str(msg)[:300])
    calls = []
    for b in blocks.values():
        if b.get("type") == "tool_use":
            try:
                inp = json.loads(b.get("json") or "{}")
            except Exception:
                inp = {}
            calls.append(ToolCall(b.get("id"), b.get("name"), inp))
    return text, calls, usage, stop_reason, ""


class AnthropicProvider(ModelProvider):
    name = "anthropic"
    reports_cache = True                          # cache_read/creation_input_tokens always reported
    API = "https://api.anthropic.com/v1/messages"

    def __init__(self, model: str = "claude-haiku-4-5-20251001", api_key: str | None = None,
                 max_tokens: int = 0, effort: str | None = None, speed: str = "standard"):
        self.model = model
        # 1024 was the old default and made any edit whose new_string exceeds ~1024 output tokens
        # systematically impossible (infinite retry churn). Default to the shared COLLIE_MAX_TOKENS
        # knob (same env OpenAICompat reads) so big edits fit; explicit arg still wins.
        self.max_tokens = max_tokens or int(os.environ.get("COLLIE_MAX_TOKENS", "8192"))
        requested_effort = effort if effort is not None else (
            os.environ.get("COLLIE_REASONING_EFFORT") or os.environ.get("COLLIE_EFFORT"))
        self.effort, _ = resolve_reasoning_effort(self.name, self.model, requested_effort)
        self.speed, _ = resolve_speed_tier(self.name, self.model, speed)
        self.actual_speed = self.speed
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set (needed for --provider anthropic)")

    def _to_anthropic(self, messages: list) -> list:
        out = []
        for m in messages:
            role = m["role"]
            if role == "tool":
                out.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": str(m.get("content", "")),
                }]})
            elif role == "assistant" and m.get("tool_calls"):
                blocks = []
                # thinking blocks (if any) MUST come first when extended thinking is enabled —
                # the API rejects an assistant turn that starts with text/tool_use before its
                # signed thinking. No-op on the normal (thinking-off) path.
                for tb in (m.get("thinking_blocks") or []):
                    blocks.append(tb)
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    tid, tname, targs = _tc_fields(tc)
                    if not tid:
                        continue                       # unrecoverable (e.g. legacy str) — skip the block
                    blocks.append({"type": "tool_use", "id": tid, "name": tname, "input": targs})
                out.append({"role": "assistant", "content": blocks})
            else:
                content = m.get("content", "")
                if isinstance(content, list):
                    # multimodal (text + attached images) -> Anthropic content blocks
                    content = _anthropic_content(content)
                # Anthropic 400s on an empty assistant text block ("text content blocks must be
                # non-empty"). A model can return end_turn with no text (esp. after a nudge), and
                # the loop echoes that as {"role":"assistant","content":""} — coerce it so the
                # NEXT turn's request doesn't abort the whole run.
                elif role == "assistant" and not str(content).strip():
                    content = "(no output)"
                out.append({"role": role, "content": content})
        return out

    def complete(self, system: str, messages: list, tool_schemas: list, on_text=None) -> Completion:
        anthropic_msgs = self._to_anthropic(messages)
        # 2nd cache breakpoint (system is the 1st): cache the growing conversation prefix too, not
        # just the ~3k system block. Placed on the stable elided prefix — see _apply_history_cache.
        _apply_history_cache(anthropic_msgs, getattr(self, "cache_stable_upto", 0))
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": anthropic_msgs,
        }
        effort = getattr(self, "effort", "default")
        if effort != "default":
            body["output_config"] = {"effort": effort}
        if getattr(self, "speed", "standard") == "fast":
            body["speed"] = "fast"
        if tool_schemas:
            body["tools"] = tool_schemas
        if on_text:
            body["stream"] = True                # real token streaming (interactive only)
        req = urllib.request.Request(
            self.API, data=json.dumps(body).encode(),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            }, method="POST")
        # errors-as-data (point 4): transport/HTTP/parse failures return an error Completion, never
        # raise — the try covers urlopen + stream-parse + json.loads so a mid-stream connect error
        # is also caught. "max_tokens" is normalized to "length" (point 1).
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                if on_text:
                    text, calls, u, sr, edetail = _parse_anthropic_stream(r, on_text)
                    return Completion(
                        text=text, tool_calls=calls, stop_reason=_norm_stop(sr),
                        error_detail=edetail, usage=Usage(
                            input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                            cache_read=u.get("cache_read_input_tokens", 0),
                            cache_creation=u.get("cache_creation_input_tokens", 0)))
                data = json.loads(r.read())
        except Exception as e:
            return _error_completion(self.name, e)

        text, calls = "", []
        for blk in data.get("content", []):
            if blk.get("type") == "text":
                text += blk["text"]
            elif blk.get("type") == "tool_use":
                calls.append(ToolCall(blk["id"], blk["name"], blk.get("input", {})))
        u = data.get("usage", {})
        usage = Usage(
            input_tokens=u.get("input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            cache_read=u.get("cache_read_input_tokens", 0),
            cache_creation=u.get("cache_creation_input_tokens", 0),
        )
        return Completion(text=text, tool_calls=calls, usage=usage,
                          stop_reason=_norm_stop(data.get("stop_reason", "end_turn")))


# --------------------------------------------------------------------------- #
#  AnthropicOAuthProvider — direct /v1/messages using the Max/Pro SUBSCRIPTION OAuth token
#  (from ~/.claude or CLAUDE_CODE_OAUTH_TOKEN) instead of a per-token API key. Same wire
#  format as AnthropicProvider (native tool_use, streaming-free, prompt caching) — the FAST,
#  clean path, unlike the claude -p subprocess. To be accepted, the request must carry
#  Claude Code's identity (Anthropic routes subscription-OAuth by user-agent/headers): Bearer
#  auth + the claude-code betas + a `claude-code/<version>` UA + a Claude-Code system prefix.
#  This is the SAME mechanism Hermes uses (impersonation) — chosen deliberately so collie and
#  Hermes sit on identical footing. Personal use of your own subscription.
_CC_BETAS = ("claude-code-20250219,oauth-2025-04-20,"
             "fine-grained-tool-streaming-2025-05-14,prompt-caching-2024-07-31")
_CC_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
_cc_ver = None


def _claude_version():
    global _cc_ver
    if _cc_ver is None:
        try:
            import subprocess as _sp
            out = _sp.run(["claude", "--version"], capture_output=True, text=True, timeout=5).stdout
            _cc_ver = out.strip().split()[0] if out.strip() else "2.1.138"
        except Exception:
            _cc_ver = "2.1.138"
    return _cc_ver


def claude_credentials():
    """Claude Code's OAuth credential blob, or {}.

    WHERE it lives is per-OS, which is the whole point of this function: Linux and WSL get
    ~/.claude/.credentials.json, but on macOS Claude Code stores the same JSON in the login
    Keychain (service "Claude Code-credentials") and writes no file at all. Reading only the file
    meant a macOS user with a perfectly good Max/Pro login was reported `not-logged-in`, so the
    one-click "connect your subscription" path never worked there.

    Same schema either way — {"claudeAiOauth": {accessToken, refreshToken, expiresAt, ...}} — so
    only the source differs. `security` may raise a Keychain prompt the first time.
    """
    from . import plat
    if plat.is_macos():
        try:
            import subprocess
            out = subprocess.run(["security", "find-generic-password",
                                  "-s", "Claude Code-credentials", "-w"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if out:
                return json.loads(out)
        except Exception:
            pass
        # fall through: a file may still exist if the user pointed CLAUDE_CONFIG_DIR at one
    try:
        with open(os.path.expanduser("~/.claude/.credentials.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def _read_oauth_token():
    t = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if t:
        return t
    return (claude_credentials().get("claudeAiOauth") or {}).get("accessToken", "")


# Collie READS the token Claude Code mints and deliberately never refreshes it: refreshing means
# writing the credential store that Claude Code is also writing, and two processes racing over one
# OAuth token is a worse failure than asking the user to run `claude`. (The Codex path is the other
# way round — see codex_oauth._refresh — because there the CLI hands us the refresh token and
# expects whoever uses it to write auth.json back.)
#
# The consequence is that this token goes stale on its own if Claude Code is not being used, and
# without a check the only symptom is a bare 401 from the API that reads like an outage.
OAUTH_EXPIRED_HINT = (
    "Claude subscription token has expired. Collie reads the token Claude Code mints and does not "
    "refresh it (that would mean two processes writing one credential). Run `claude` once to renew "
    "it, then retry — or set CLAUDE_CODE_OAUTH_TOKEN.")


def _oauth_expiry_ms() -> int:
    """Claude Code's ``expiresAt`` in epoch milliseconds, or 0 for "no opinion".

    0 is returned for an env-supplied token (it carries no expiry) and for a credential blob that
    has no such field. Only a value that has demonstrably passed may block a request; a missing one
    never may, or an older credential format would lock a working login out.
    """
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return 0
    raw = (claude_credentials().get("claudeAiOauth") or {}).get("expiresAt")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def claude_oauth_expired(skew_s: int = 60) -> bool:
    """Whether the subscription token is past (or within ``skew_s`` of) its stated expiry."""
    expires_at = _oauth_expiry_ms()
    return bool(expires_at) and (time.time() + skew_s) * 1000 >= expires_at


class AnthropicOAuthProvider(AnthropicProvider):
    name = "anthropic-oauth"

    def __init__(self, model: str = "claude-opus-4-8", max_tokens: int = 0,
                 effort: str | None = None, speed: str = "standard"):
        self.model = model
        # honour the shared COLLIE_MAX_TOKENS knob (Settings "Max output tokens/turn"), like the parent
        # and every sibling provider — pinning 4096 here made big write_file edits truncate on the
        # subscription path (the user's default), then churn on retries.
        self.max_tokens = max_tokens or int(os.environ.get("COLLIE_MAX_TOKENS", "8192"))
        requested_effort = effort if effort is not None else (
            os.environ.get("COLLIE_REASONING_EFFORT") or os.environ.get("COLLIE_EFFORT"))
        self.effort, _ = resolve_reasoning_effort(self.name, self.model, requested_effort)
        self.speed, _ = resolve_speed_tier(self.name, self.model, speed)
        self.actual_speed = self.speed
        self.api_key = ""                       # not used; OAuth token read per-call (stays fresh)
        if not _read_oauth_token():
            raise RuntimeError("no Claude OAuth token (run `claude` login or set "
                               "CLAUDE_CODE_OAUTH_TOKEN) — needed for --provider anthropic-oauth")
        if claude_oauth_expired():
            raise RuntimeError(OAUTH_EXPIRED_HINT)

    def complete(self, system: str, messages: list, tool_schemas: list, on_text=None) -> Completion:
        # Re-read per call so a refresh Claude Code performs mid-run is picked up without a restart
        # — and re-check expiry for the same reason, since a long run can outlive the token it
        # started with. Naming the real cause beats letting the request come back a bare 401.
        if claude_oauth_expired():
            raise RuntimeError(OAUTH_EXPIRED_HINT)
        token = _read_oauth_token()
        anthropic_msgs = self._to_anthropic(messages)
        # cache the conversation prefix too (3rd breakpoint here: _CC_SYSTEM chains into the collie
        # system block which carries the 1st, and the history gets one — see _apply_history_cache).
        _apply_history_cache(anthropic_msgs, getattr(self, "cache_stable_upto", 0))
        # Extended thinking (COLLIE_THINKING=budget_tokens, e.g. 8000): run Opus "thick" like the
        # real Claude Code does, instead of collie's default no-thinking path. Gap-closer hypothesis
        # (cc/hermes think, collie doesn't). budget MUST be < max_tokens, and max_tokens must leave
        # room for the visible answer on top of the thinking budget -> bump max_tokens accordingly.
        # Adaptive thinking (Opus 4.8's only on-mode): COLLIE_THINKING truthy -> {"type":"adaptive"}.
        # The old {"type":"enabled","budget_tokens":N} form is REMOVED on 4.8 (400). Off by default
        # (collie's lean no-thinking path). Adaptive auto-enables interleaved thinking — no beta.
        _think = (os.environ.get("COLLIE_THINKING", "") or "").strip().lower() \
            not in ("", "0", "off", "false", "no")
        eff_max = max(self.max_tokens, 32000) if _think else self.max_tokens
        body = {
            "model": self.model,
            "max_tokens": eff_max,
            # first system block MUST be the Claude Code identity (subscription-OAuth is
            # validated against it); collie's real instructions follow as a second block.
            "system": [{"type": "text", "text": _CC_SYSTEM},
                       {"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": anthropic_msgs,
        }
        effort = getattr(self, "effort", "default")
        if effort != "default":
            body["output_config"] = {"effort": effort}
        if getattr(self, "speed", "standard") == "fast":
            body["speed"] = "fast"
        if _think:
            body["thinking"] = {"type": "adaptive"}
        if tool_schemas:
            body["tools"] = tool_schemas
        if on_text:
            body["stream"] = True                # real token streaming (interactive only)
        betas = _CC_BETAS   # adaptive thinking needs no beta header on Opus 4.8
        req = urllib.request.Request(
            self.API, data=json.dumps(body).encode(),
            headers={
                "content-type": "application/json",
                "authorization": "Bearer " + token,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": betas,
                "user-agent": "claude-code/%s (external, cli)" % _claude_version(),
                "x-app": "cli",
            }, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                if on_text:
                    text, calls, u, sr, edetail = _parse_anthropic_stream(r, on_text)
                    return Completion(
                        text=text, tool_calls=calls, stop_reason=_norm_stop(sr),
                        error_detail=edetail, usage=Usage(
                            input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                            cache_read=u.get("cache_read_input_tokens", 0),
                            cache_creation=u.get("cache_creation_input_tokens", 0)))
                data = json.loads(r.read())
        except Exception as e:
            return _error_completion(self.name, e)
        text, calls, thinks = "", [], []
        for blk in data.get("content", []):
            bt = blk.get("type")
            if bt == "text":
                text += blk["text"]
            elif bt == "tool_use":
                calls.append(ToolCall(blk["id"], blk["name"], blk.get("input", {})))
            elif bt in ("thinking", "redacted_thinking"):
                # keep the RAW block verbatim (incl. signature) — it must be replayed unmodified.
                thinks.append(blk)
        u = data.get("usage", {})
        usage = Usage(input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                      cache_read=u.get("cache_read_input_tokens", 0),
                      cache_creation=u.get("cache_creation_input_tokens", 0))
        return Completion(text=text, tool_calls=calls, usage=usage, thinking_blocks=thinks,
                          stop_reason=_norm_stop(data.get("stop_reason", "end_turn")))


# --------------------------------------------------------------------------- #
#  ClaudeCliProvider — drive collie's backend with the LATEST Claude model through the
#  official `claude` CLI. This is the ONE sanctioned, non-proxy way to use a Max/Pro
#  SUBSCRIPTION programmatically (verified against Anthropic's own docs + the 2026 bans on
#  header-spoofing proxies): shell out to the real Claude Code binary as a narrow reasoner.
#  Two auth modes, both first-party/legitimate — pick by which env var you set:
#    • SUBSCRIPTION (dev/small runs): `claude setup-token` -> export CLAUDE_CODE_OAUTH_TOKEN;
#      _call() drops ANTHROPIC_API_KEY from the child env so the subscription actually wins.
#    • API KEY (unattended full benchmark runs): export ANTHROPIC_API_KEY (per-token, no
#      subscription rate-window, no ToS grey area).
#  Use model="opus" / "claude-opus-4-8" to break the DeepSeek resolve ceiling (proven
#  model-bound on SWE-bench). collie still runs its OWN loop/tools/memory/context; each turn
#  it delegates only "produce the next step" to `claude -p` with Claude Code's tools disabled
#  (pure reasoner), and a text tool-protocol lets the model drive collie's tools.
#
#  HONEST CAVEAT: `claude -p` always carries Claude Code's own ~11-18K system
#  prefix (you can't strip it via the CLI), so collie-via-CLI *inherits* that prefix —
#  this backend is for "real model, free, no key", NOT for showing collie's lean
#  prefix. For that, use --provider anthropic (raw API) or a local model.
# --------------------------------------------------------------------------- #
class ClaudeCliProvider(ModelProvider):
    name = "claude-cli"
    _OFF = ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "WebFetch",
            "WebSearch", "Task", "TodoWrite", "NotebookEdit"]

    def __init__(self, model: str = "sonnet", timeout: int = 180,
                 effort: str | None = None):
        self.model = "claude-cli:" + model
        self._model = model
        self.timeout = timeout
        requested_effort = effort if effort is not None else (
            os.environ.get("COLLIE_REASONING_EFFORT") or os.environ.get("COLLIE_EFFORT"))
        self.effort, _ = resolve_reasoning_effort(self.name, model, requested_effort)

    def _prompt(self, messages, tool_schemas):
        # NB: collie's system prompt is passed via --system-prompt, NOT embedded here.
        # Embedding collie's agentic "use tools / run tests" language in the -p body made
        # Claude attempt real tool_use (→ error_max_turns); as the system role it's config
        # and the JSON protocol below governs the reply. (Verified.)
        L = ["# Conversation so far:"]
        for m in messages:
            r = m["role"]
            if r == "user":
                # claude-cli is text-only here; send the text (images are dropped with a marker)
                L.append("User: " + content_text(m.get("content", "")) +
                         (" [+image]" if _has_images(m.get("content")) else ""))
            elif r == "assistant":
                for tc in m.get("tool_calls", []):
                    _, tname, targs = _tc_fields(tc)
                    L.append("Assistant called %s(%s)" % (tname, json.dumps(targs, ensure_ascii=False)))
                if m.get("content"):
                    L.append("Assistant: " + m["content"])
            elif r == "tool":
                L.append("Result of %s: %s" % (m.get("name"), str(m.get("content", ""))[:2000]))
        tools = "\n".join("- %s(%s): %s" % (
            t["name"], ",".join((t.get("input_schema", {}).get("properties", {}) or {}).keys()),
            t["description"]) for t in tool_schemas)
        L += ["", "# Tools the executor can run:", tools, "",
              "You are the reasoning engine inside Collie, not a standalone chat assistant. "
              "You cannot inspect or change the workspace except by emitting a tool JSON below. "
              "Until the requested work has actually been performed, an answer JSON is a failure. "
              "On the first turn of a coding task, inspect the workspace with grep, glob, or "
              "read_file; use edit_file/write_file to make the change before answering.", "",
              "# RESPONSE FORMAT (strict):",
              "Reply with EXACTLY ONE JSON object and nothing else — no prose, no markdown "
              "fence, no explanation before or after.",
              'To run a tool:      {"tool":"<name>","args":{...}}',
              'To finish (only when the task is fully done): {"answer":"<final answer>"}',
              "A strong model tends to explain instead of emitting JSON — do NOT. One JSON "
              "object, that is your entire reply. Respond to the latest User message now."]
        return "\n".join(L)

    def _call(self, prompt, system):
        # `--tools ""` disables ALL of Claude Code's built-in tools (the CLI-documented way),
        # so it can't agentic tool-use (which --max-turns 1 would truncate to an error) — it
        # becomes a pure reasoner that emits collie's JSON as TEXT. collie's system goes via
        # --system-prompt (full replacement); NOT --bare (bare never reads the OAuth token).
        # Both the accumulated conversation and Collie's system/tool contract can exceed
        # Windows' ~32K command-line limit.  Keep the conversation on stdin and the system prompt
        # in Claude's documented file input; neither belongs in argv or a process listing.
        cmd = ["claude", "-p", "--output-format", "json", "--safe-mode",
               "--no-session-persistence",
               "--max-turns", "1", "--model", self._model, "--tools", ""]
        effort = getattr(self, "effort", "default")
        if effort != "default":
            cmd += ["--effort", effort]
        # AUTH (the one non-obvious footgun, per the subscription research): claude's auth
        # priority is ANTHROPIC_API_KEY (3rd) > CLAUDE_CODE_OAUTH_TOKEN (5th). So if BOTH are
        # set, the API key silently wins and bills per-token instead of using the Max/Pro
        # subscription. When an OAuth token is present we drop the API key from the child env
        # so the subscription is actually used. If only the API key is set, keep it (the
        # sanctioned per-token path for unattended full runs). Either way = first-party CLI,
        # never a spoofing proxy.
        env = dict(os.environ)
        if getattr(self, "subscription_only", False):
            for name in (
                    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
                    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                    "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_VERTEX"):
                env.pop(name, None)
        elif env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            env.pop("ANTHROPIC_API_KEY", None)
        executable = shutil.which(cmd[0], path=env.get("PATH"))
        if not executable:
            raise FileNotFoundError("claude CLI is not on PATH")
        from . import plat as _plat
        with tempfile.TemporaryDirectory(prefix="collie-claude-cli-") as prompt_dir:
            system_path = os.path.join(prompt_dir, "system.txt")
            with open(system_path, "w", encoding="utf-8", newline="\n") as system_file:
                system_file.write(system)
            full_cmd = [executable] + cmd[1:] + ["--system-prompt-file", system_path]
            r = subprocess.run(full_cmd, capture_output=True, text=True, input=prompt,
                               timeout=self.timeout, env=env, **_plat.no_window_kwargs())
        if r.returncode != 0:
            raise RuntimeError("claude CLI exited %d" % r.returncode)
        data = _cc_json(r.stdout)
        if not isinstance(data, dict):
            raise RuntimeError("claude CLI returned invalid JSON")
        if "is_error" in data and data.get("is_error") is not False:
            raise RuntimeError("claude CLI reported an error")
        if not isinstance(data.get("result"), str):
            raise RuntimeError("claude CLI JSON response is missing a string result")
        u = data.get("usage", {}) or {}
        if not isinstance(u, dict):
            raise RuntimeError("claude CLI JSON response has invalid usage")
        usage = Usage(input_tokens=u.get("input_tokens", 0),
                      output_tokens=u.get("output_tokens", 0),
                      cache_read=u.get("cache_read_input_tokens", 0),
                      cache_creation=u.get("cache_creation_input_tokens", 0))
        return str(data.get("result", "")).strip(), usage

    def complete(self, system, messages, tool_schemas, on_text=None):
        prompt = self._prompt(messages, tool_schemas)
        total = Usage()
        text = ""
        # A strong model sometimes ignores the format and writes prose (seaborn: 35 turns,
        # 0 tool calls). If we can't parse a tool OR an answer, re-prompt sternly once.
        for attempt in range(2):
            try:
                text, u = self._call(prompt, system)
            except Exception as e:
                return Completion(text="ERROR(claude-cli): %s" % e, stop_reason="error")
            total.add(u)
            tc = _parse_tool_json(text)
            if tc:
                return Completion(tool_calls=[tc], usage=total, stop_reason="tool_use")
            ans = _parse_answer_json(text)
            if ans is not None:
                return Completion(text=ans, usage=total, stop_reason="end_turn")
            prompt = (prompt + "\n\n# Your previous reply was NOT a single JSON object "
                      "(you wrote prose). Reply again with ONLY one JSON object — "
                      '{"tool":...} to act, or {"answer":...} to finish. Nothing else.')
        return Completion(text=text, usage=total, stop_reason="end_turn")  # fallback: prose


def _cc_json(stdout: str) -> dict | None:
    stdout = (stdout or "").strip()
    try:
        return json.loads(stdout)
    except Exception:
        for line in reversed(stdout.splitlines()):
            if line.strip().startswith("{"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
    return None


def _json_objects(text: str):
    """Yield JSON objects embedded in text without hand-counting braces.

    Claude is instructed to return one bare object, but the extractor remains tolerant of a
    leading sentence or Markdown fence. ``raw_decode`` is quote-aware, so braces inside file
    contents or a final answer cannot terminate the object early.
    """
    if not isinstance(text, str):
        return
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _end = decoder.raw_decode(text, match.start())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(obj, dict):
            yield obj


def _parse_tool_json(text: str):
    """Extract a {"tool":...,"args":{...}} object, including code containing braces."""
    for obj in _json_objects(text):
        if "tool" in obj:
            return ToolCall("cli_%d" % (len(text) % 100000),
                            obj["tool"], obj.get("args", {}))
    return None


def _parse_answer_json(text: str):
    """Extract {"answer":"..."} -> the answer string, else None (a tool-JSON is not one)."""
    for obj in _json_objects(text):
        if "answer" in obj and "tool" not in obj:
            return str(obj["answer"])
    return None


# --------------------------------------------------------------------------- #
#  OllamaProvider — a LOCAL model server as collie's backend. The real "use a CLI /
#  no paid API" answer: free, offline, real tool-calling, and collie keeps its LEAN
#  prefix (collie owns the whole prompt — no foreign harness bloat, unlike claude -p).
#  Runs on the 5090 via Ollama's native /api/chat. Zero third-party deps (urllib).
# --------------------------------------------------------------------------- #
class OllamaProvider(ModelProvider):
    name = "ollama"

    def __init__(self, model: str = "qwen2.5-coder:7b", host: str = "http://localhost:11434"):
        self.model = "ollama:" + model
        self._model = model
        self.url = host.rstrip("/") + "/api/chat"

    def _to_ollama(self, system, messages):
        out = [{"role": "system", "content": system}]
        for m in messages:
            r = m["role"]
            if r == "tool":
                out.append({"role": "tool", "content": str(m.get("content", ""))})
            elif r == "assistant" and m.get("tool_calls"):
                out.append({"role": "assistant", "content": m.get("content", "") or "",
                            "tool_calls": [{"function": {"name": _tc_fields(tc)[1], "arguments": _tc_fields(tc)[2]}}
                                           for tc in m["tool_calls"]]})
            else:
                c = m.get("content", "")
                if isinstance(c, list):                       # Ollama vision: text + images[] (bare base64)
                    msg = {"role": r, "content": content_text(c)}
                    imgs = [b["data"] for b in c if isinstance(b, dict) and b.get("type") == "image" and b.get("data")]
                    if imgs:
                        msg["images"] = imgs
                    out.append(msg)
                else:
                    out.append({"role": r, "content": c})
        return out

    def complete(self, system, messages, tool_schemas, on_text=None):
        tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
            for t in tool_schemas]
        body = {"model": self._model, "messages": self._to_ollama(system, messages),
                "tools": tools, "stream": False}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read())
        except Exception as e:
            # errors-as-data (point 4): surface the outage as stop_reason="error" instead of
            # treating "ERROR(ollama): ..." as the model's answer and consolidating it into memory.
            return _error_completion(self.name, e)
        msg = data.get("message", {})
        usage = Usage(input_tokens=data.get("prompt_eval_count", 0),
                      output_tokens=data.get("eval_count", 0))
        _trunc = data.get("done_reason") == "length"   # ollama/llama.cpp output-cap (point 1)
        calls = []
        for i, tc in enumerate(msg.get("tool_calls", []) or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    # args={} was misdiagnosed downstream as a MISSING required arg; the real fault
                    # is malformed/truncated JSON. Sentinel lets the loop say so (point 7).
                    args = {"_malformed_args": args[:500]}
            # GLOBALLY-unique id (not "oll_%d" % i) — the per-response index collides across turns,
            # and a continued/cross-provider session would then emit duplicate tool_use ids (400).
            calls.append(ToolCall("oll_%s" % uuid.uuid4().hex[:8], fn.get("name", ""), args))
        content = msg.get("content", "") or ""
        if not calls:                       # local models often emit the call as text
            t = _tool_from_content(content)
            if t:
                calls = [ToolCall("ollc_%s" % uuid.uuid4().hex[:8], t[0], t[1])]
                content = ""
        if calls:
            return Completion(text=content, tool_calls=calls, usage=usage,
                              stop_reason="length" if _trunc else "tool_use")
        return Completion(text=content, usage=usage,
                          stop_reason="length" if _trunc else "end_turn")


def _tool_from_content(text: str):
    """Extract a tool call emitted as text: {"name":..,"arguments":{..}} or
    {"tool":..,"args":{..}} (balanced braces, first object wins)."""
    if not text or "{" not in text:
        return None
    start = text.find("{")
    depth = 0
    for j in range(start, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:j + 1])
                except Exception:
                    return None
                name = obj.get("name") or obj.get("tool")
                args = obj.get("arguments")
                if args is None:
                    args = obj.get("args", {})
                if name and isinstance(args, dict):
                    return name, args
                return None
    return None


# --------------------------------------------------------------------------- #
#  OpenAICompatProvider — any OpenAI-compatible /chat/completions endpoint.
#  Unlocks dozens of cheap strong models (DeepSeek, Qwen, GLM, Kimi, OpenRouter,
#  Groq, …) as collie's raw backend: collie drives its own loop, real strong model, lean
#  prefix, pennies per run. This is the right shape (a completion endpoint), which
#  claude -p is not. Zero third-party deps (urllib).
# --------------------------------------------------------------------------- #
# name -> (base_url, api-key env var, a sensible default model)
OPENAI_COMPAT_PRESETS = {
    # COLLIE_OPENAI_BASE reroutes the openai preset (e.g. through harness.apitap for
    # token metering) without touching provider selection.
    "openai":     (os.environ.get("COLLIE_OPENAI_BASE", "https://api.openai.com/v1"), "OPENAI_API_KEY", "gpt-4o-mini"),
    # Google's official OpenAI-compatibility layer for the Gemini API (ai.google.dev/gemini-api/
    # docs/openai-compatibility) — same /chat/completions shape, function calling included.
    "gemini":     ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-2.5-flash"),
    "deepseek":   ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "deepseek/deepseek-chat"),
    "dashscope":  ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", "qwen2.5-coder-32b-instruct"),
    "qwen":       ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", "qwen2.5-coder-32b-instruct"),
    "moonshot":   ("https://api.moonshot.cn/v1", "MOONSHOT_API_KEY", "moonshot-v1-8k"),
    "groq":       ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "zhipu":      ("https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY", "glm-4-flash"),
    # Volcengine Ark (火山方舟) — Doubao's platform, and an aggregator: the same endpoint also
    # serves GLM, MiniMax, Kimi and DeepSeek, so one preset covers several vendors. No default
    # model is pinned on purpose — Ark identifies models by ids that change per release (and can
    # be per-account endpoint ids), and inventing one produces a 404 that reads like a broken
    # provider. Discovery fills the picker from /models instead.
    "ark":        ("https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY", ""),
    "doubao":     ("https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY", ""),
}


class OpenAICompatProvider(ModelProvider):
    def __init__(self, base_url, api_key_env, model, name="openai-compat",
                 effort: str | None = None, speed: str = "standard"):
        self.base = base_url.rstrip("/") + "/chat/completions"
        self.model = "%s:%s" % (name, model)
        self._model = model
        self.name = name
        requested_effort = effort if effort is not None else os.environ.get("COLLIE_REASONING_EFFORT")
        self.effort, _ = resolve_reasoning_effort(self.name, self._model, requested_effort)
        self.speed, _ = resolve_speed_tier(self.name, self._model, speed)
        self.actual_speed = self.speed
        self.api_key = os.environ.get(api_key_env, "")
        # Coding wants deterministic, focused edits, not creative variance. DeepSeek's
        # default temperature is 1.0 — the source of collie's run-to-run patch variance
        # (same instance editing different files across runs). Default low; override via
        # COLLIE_TEMPERATURE.
        self.temperature = float(os.environ.get("COLLIE_TEMPERATURE", "0.2"))
        self.max_tokens = int(os.environ.get("COLLIE_MAX_TOKENS", "4096"))
        # deepseek/openai return prompt_tokens_details.cached_tokens; others (groq/moonshot/…) may
        # not — seed the ledger's sticky flag only for the known-reporting presets, inference covers
        # the rest once a nonzero cache field appears.
        self.reports_cache = name in ("deepseek", "openai")
        if not self.api_key:
            raise RuntimeError("%s not set (needed for --provider %s)" % (api_key_env, name))

    def _to_openai(self, system, messages):
        out = [{"role": "system", "content": system}]
        for m in messages:
            r = m["role"]
            if r == "tool":
                out.append({"role": "tool", "tool_call_id": m.get("tool_call_id", ""),
                            "content": str(m.get("content", ""))})
            elif r == "assistant" and m.get("tool_calls"):
                _tcs = [_tc_fields(tc) for tc in m["tool_calls"]]
                out.append({"role": "assistant", "content": m.get("content") or None,
                            "tool_calls": [{"id": tid, "type": "function",
                                            "function": {"name": tname,
                                                         "arguments": json.dumps(targs, ensure_ascii=False)}}
                                           for tid, tname, targs in _tcs]})
            else:
                c = m.get("content", "")
                out.append({"role": r, "content": _openai_content(c) if isinstance(c, list) else c})
        return out

    def complete(self, system, messages, tool_schemas, on_text=None):
        tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": t.get("input_schema", {"type": "object", "properties": {}})}}
            for t in tool_schemas]
        body = {"model": self._model, "messages": self._to_openai(system, messages),
                "temperature": self.temperature, "max_tokens": self.max_tokens}
        if self.name == "openai" and self._model.startswith(("gpt-5", "o1", "o3", "o4")):
            # OpenAI reasoning models reject `max_tokens` (they want max_completion_tokens,
            # whose cap also covers hidden reasoning tokens) and 400 on any temperature
            # other than the default — rename the one, drop the other.
            body["max_completion_tokens"] = body.pop("max_tokens")
            body.pop("temperature", None)
            effort = getattr(self, "effort", "default")
            if effort != "default":
                body["reasoning_effort"] = effort
        if getattr(self, "speed", "standard") == "fast":
            body["service_tier"] = "fast"
        if tools:                      # some endpoints 400 on an empty tools array
            body["tools"] = tools
            body["tool_choice"] = "auto"
        req = urllib.request.Request(self.base, data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json",
                                              "authorization": "Bearer " + self.api_key},
                                     method="POST")
        # SINGLE attempt (point 5): the retry/backoff loop is hoisted to the host so ONE bounded,
        # budget-aware, classified policy governs every provider — no more provider-internal 3× loop
        # multiplying with a host retry. errors-as-data: return an error Completion, never raise.
        try:
            with urllib.request.urlopen(
                    req, timeout=float(os.environ.get("COLLIE_HTTP_TIMEOUT", "120"))) as r:
                data = json.loads(r.read())
        except Exception as e:
            return _error_completion(self.name, e)
        tier = str(data.get("service_tier") or "").lower()
        if tier:
            self.actual_speed = "fast" if tier in ("fast", "priority") else "standard"
        choice = (data.get("choices") or [{}])[0]
        ch = choice.get("message", {})
        usage = _openai_usage(data.get("usage", {}))   # normalize: input UNCACHED, no double count
        # AUDIT #7 second half: finish_reason was discarded, so an output-limit truncation was
        # indistinguishable from a clean turn (the DeepSeek benchmark path). Surface it (point 1).
        truncated = choice.get("finish_reason") == "length"
        calls = []
        for tc in ch.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except Exception:
                # malformed/truncated JSON — sentinel instead of {} so the loop reports the REAL
                # fault ("not valid JSON") rather than a misleading "missing required arg" (point 7).
                args = {"_malformed_args": (args if isinstance(args, str) else str(args))[:500]}
            calls.append(ToolCall(tc.get("id") or "oa_%s" % uuid.uuid4().hex[:8], fn.get("name", ""), args))
        if calls:
            return Completion(text=ch.get("content") or "", tool_calls=calls, usage=usage,
                              stop_reason="length" if truncated else "tool_use")
        return Completion(text=ch.get("content") or "", usage=usage,
                          stop_reason="length" if truncated else "end_turn")


def _plugin_providers() -> tuple:
    """(name -> factory, import_errors) for providers supplied from outside this package.

    Two discovery paths, deliberately: an installed distribution declares a ``collie.providers``
    entry point and needs no configuration, while ``COLLIE_PROVIDER_PLUGINS`` (os.pathsep-separated
    module names) loads a plugin straight from a git checkout that has not been installed yet —
    which is how one gets developed.

    A plugin module exposes ``COLLIE_PROVIDERS = {"name": factory}`` where ``factory(model)``
    returns a ModelProvider.

    Import errors are COLLECTED, not raised and not swallowed. One broken plugin must not stop the
    other providers from resolving; but a user who asked for exactly that plugin's provider must be
    told why it is missing, so make_provider puts these in its error. Silent absence is the one
    behaviour this must never have — it reads as "unknown provider" and sends people hunting the
    wrong bug.
    """
    found, errors = {}, []
    for mod_name in (os.environ.get("COLLIE_PROVIDER_PLUGINS") or "").split(os.pathsep):
        mod_name = mod_name.strip()
        if not mod_name:
            continue
        try:
            import importlib
            found.update(getattr(importlib.import_module(mod_name), "COLLIE_PROVIDERS", {}) or {})
        except Exception as e:
            errors.append("%s: %s" % (mod_name, e))
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group="collie.providers"):
            try:
                obj = ep.load()
                # an entry point may point at the mapping itself or at the module holding it
                found.update(obj if isinstance(obj, dict)
                             else (getattr(obj, "COLLIE_PROVIDERS", {}) or {}))
            except Exception as e:
                errors.append("%s: %s" % (ep.name, e))
    except Exception as e:                                  # pragma: no cover - metadata unavailable
        errors.append("entry_points: %s" % e)
    return found, errors


def provider_default_model(name: str) -> str:
    """One source of truth for built-in defaults used by factories and Auto routing."""
    name = (name or "").strip().lower()
    if name == "mock":
        return "mock-planner-v1"
    if name == "anthropic":
        return "claude-haiku-4-5-20251001"
    if name in ("anthropic-oauth", "claude-sub"):
        return "claude-opus-4-8"
    if name in ("codex-oauth", "codex-sub", "codex"):
        return "gpt-5.6-terra"
    if name in ("claude-cli", "cli"):
        return "sonnet"
    if name == "ollama":
        return "qwen2.5-coder:7b"
    if name in OPENAI_COMPAT_PRESETS:
        return OPENAI_COMPAT_PRESETS[name][2]
    return ""


def provider_capabilities(name: str, model: str | None = None) -> dict:
    """Provider/model feature contract used before a run is constructed.

    Unknown/plugin providers get the conservative contract.  In particular,
    Fast is opt-in only where the wire field, eligible model family, and billing
    premium are all known; it is never emulated with a weaker model.
    """
    name = (name or "").strip().lower()
    model = (model or provider_default_model(name) or "").strip()
    reasoning = []
    speed_tiers = ["standard"]
    fast_multiplier = None
    fast_unit = "token-price"
    fast_note = "Fast is not reported by this provider/model"

    if name in ("codex-oauth", "codex-sub", "codex"):
        reasoning = ["low", "medium", "high", "xhigh"]
        codex_fast = (model.startswith("gpt-5.6-") or model.startswith("gpt-5.5-")
                      or model in ("gpt-5.5", "gpt-5.4"))
        if codex_fast:
            speed_tiers.append("fast")
            fast_unit = "subscription-credits"
            if model.startswith("gpt-5.6-") or model.startswith("gpt-5.5"):
                fast_multiplier = 2.5
                fast_note = ("same model at about 1.5x generation speed; "
                             "2.5x Codex credits when the account supports Fast")
            else:
                fast_note = ("same GPT-5.4 model through Codex Fast; the current credit "
                             "premium is account/version dependent")
    elif name == "openai":
        if model.startswith(("gpt-5", "o1", "o3", "o4")):
            reasoning = ["low", "medium", "high", "xhigh"]
        if model.startswith("gpt-5.6-"):
            speed_tiers.append("fast")
            fast_multiplier = 2.0
            fast_note = "same model via service_tier=fast; 2x current API token price"
    elif name in ("anthropic", "anthropic-oauth", "claude-sub"):
        # Current Anthropic effort-capable model families.  Older Haiku/Sonnet
        # models must not receive output_config.effort and fail a whole run with 400.
        if any(tag in model for tag in ("opus-4-8", "opus-5", "sonnet-5", "fable-5")):
            reasoning = ["low", "medium", "high", "max"]
        if any(tag in model for tag in ("opus-4-6", "opus-4-7")):
            speed_tiers.append("fast")
            fast_multiplier = 6.0
            fast_note = "same eligible Opus model via speed=fast; extra-usage billing may be required"
    elif name in ("claude-cli", "cli"):
        reasoning = ["low", "medium", "high", "max"]

    return {
        "provider": name,
        "model": model,
        "reasoning_efforts": reasoning,
        "speed_tiers": speed_tiers,
        "fast_billing_multiplier": fast_multiplier,
        "fast_billing_unit": fast_unit if fast_multiplier is not None else None,
        "fast_note": fast_note,
        # Static capability is known; account/region/admin enablement is checked
        # by the actual request and surfaced, never guessed as available.
        "availability": "account-dependent" if "fast" in speed_tiers else "standard-only",
    }


def resolve_reasoning_effort(name: str, model: str | None, requested: str | None) -> tuple[str, str]:
    requested = (requested or "").strip().lower()
    if requested in ("", "auto", "default", "provider-default"):
        return "default", "provider default"
    known = ("low", "medium", "high", "xhigh", "max")
    if requested not in known:
        raise ValueError("reasoning effort must be auto, low, medium, high, xhigh, or max")
    supported = provider_capabilities(name, model)["reasoning_efforts"]
    if requested not in supported:
        return "default", "%s is unsupported; used provider default" % requested
    return requested, ""


def resolve_speed_tier(name: str, model: str | None, requested: str | None) -> tuple[str, dict]:
    requested = (requested or "standard").strip().lower()
    if requested not in ("standard", "fast"):
        raise ValueError("speed must be standard or fast")
    caps = provider_capabilities(name, model)
    if requested not in caps["speed_tiers"]:
        # Fast carries a real billing/availability consequence.  Never silently
        # turn a user-visible Fast choice into Standard.
        raise ValueError("Fast is not supported for %s:%s" % (name, model or caps["model"]))
    return requested, caps


def make_provider(name: str, model: str | None = None, effort: str | None = None,
                  speed: str = "standard") -> ModelProvider:
    chosen_model = model or provider_default_model(name)
    resolved_speed, _caps = resolve_speed_tier(name, chosen_model, speed)
    if name == "mock":
        return MockProvider()
    if name == "anthropic":
        return AnthropicProvider(model=chosen_model, effort=effort, speed=resolved_speed)
    if name in ("anthropic-oauth", "claude-sub"):
        return AnthropicOAuthProvider(model=chosen_model, effort=effort, speed=resolved_speed)
    if name in ("codex-oauth", "codex-sub", "codex"):     # ChatGPT Codex subscription (gpt-5.6-terra)
        from .codex_oauth import CodexOAuthProvider
        return CodexOAuthProvider(model=chosen_model, effort=effort, speed=resolved_speed)
    if name in ("claude-cli", "cli"):
        return ClaudeCliProvider(model=chosen_model, effort=effort)
    if name == "ollama":
        return OllamaProvider(model=chosen_model)
    if name in OPENAI_COMPAT_PRESETS:
        base, env, default = OPENAI_COMPAT_PRESETS[name]
        return OpenAICompatProvider(base, env, chosen_model or default, name=name,
                                    effort=effort, speed=resolved_speed)
    # Plugins are consulted LAST so a third party cannot shadow a built-in name: someone who asks
    # for `anthropic` must always get this file's Anthropic path, whatever happens to be installed.
    plugins, errors = _plugin_providers()
    if name in plugins:
        return plugins[name](model)
    known = sorted(set(list(plugins) + list(OPENAI_COMPAT_PRESETS)
                       + ["mock", "anthropic", "anthropic-oauth", "codex-oauth",
                          "claude-cli", "ollama"]))
    msg = "unknown provider: %s (known: %s)" % (name, ", ".join(known))
    if errors:
        # The likely case when a plugin-provided name is missing is that the plugin failed to load.
        msg += " · plugin load errors: " + "; ".join(errors)
    raise ValueError(msg)
