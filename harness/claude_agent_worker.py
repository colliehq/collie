"""Private subprocess entry point for :mod:`harness.claude_agent_sdk`.

Imports the optional dependency only inside the worker, so importing Collie's
stdlib-only provider registry never requires the Claude Agent SDK.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import os
import signal
import sys


# Deliberately duplicated from the transport instead of imported: this file runs
# as a script with the harness directory removed from sys.path, and host data is
# trusted but still re-validated here so a malformed request fails loudly at the
# process boundary rather than inside the SDK.
_IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
_MAX_IMAGE_B64 = 5 * 1024 * 1024
_MAX_REQUEST_IMAGES = 16
_MAX_TEXT_REQUEST_BYTES = 4 * 1024 * 1024
_MAX_MULTIMODAL_REQUEST_BYTES = 12 * 1024 * 1024

# Structured-response mode (worker protocols 3 and 4).  The provider enforces
# Collie's {tool|answer} envelope through one synthetic formatter tool; the
# names below are the SDK's observed, stable stream shape for it.  Everything
# about that shape is re-validated here: a formatter which is invoked twice,
# receipted by a different id, or contradicted by the terminal result is a
# formatting failure and never a completed answer.
_STRUCTURED_FORMAT = "structured"
_FORMATTER_TOOL = "StructuredOutput"
_FORMATTER_RECEIPT = "Structured output provided successfully"

# The formatter validates the model's response against the requested schema and
# receipts a failed tool result when it does not match.  That is the structured
# form of a response-contract miss -- the model answered, in Collie's own
# envelope, with something the schema refuses -- so it is reported upward under
# this stable, content-free category instead of collapsing into a generic worker
# failure the host can only treat as fatal.
_FORMATTER_REFUSED = "formatter_refused_response"

_SDK_ENV = {
    "CLAUDE_CODE_MAX_RETRIES": "0",
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
}

_PARENT_DEATH_FD_FLAG = "--collie-parent-death-fd"
_PARENT_PID_FLAG = "--collie-parent-pid"
_EXTERNAL_OWNER_FLAG = "--collie-external-process-owner"

# Stable, content-free rejection categories the SDK documents on
# ``AssistantMessage.error`` (its ``AssistantMessageError`` literal).  A request
# rejected this way was physically issued, and the category is the
# only thing that tells a rate limit apart from a malformed request, so it is
# reported upward instead of being collapsed into a generic worker failure.
# ``unknown`` is deliberately absent: it carries no classification, so it stays
# on the fail-closed generic path.
_PROVIDER_ERRORS = ("authentication_failed", "billing_error", "invalid_request",
                    "rate_limit", "server_error")

# The CLI exits non-zero on purpose after emitting an error result, and the SDK
# replaces that trailing ProcessError with this exact text while iterating
# (claude_agent_sdk/_internal/query.py).  Only this known post-result exception
# may be absorbed, and only once a rejection and its terminal result are both
# validated; every other exception still propagates.
_SDK_POST_RESULT_ERROR = "Claude Code returned an error result:"


class _StructuredContractRejected(Exception):
    """The formatter refused the one model response it was given.

    Exactly one raw model response was physically issued, and the provider's own
    formatter validated it against Collie's schema and refused it.  Nothing
    about the transport failed, so the stable category and the usage measured
    before the stream was stopped travel upward, and the host can spend its one
    corrective turn exactly as it does for an unparseable plain-mode envelope.
    Neither the refused response nor the formatter's report crosses this
    boundary: the classification is the whole payload.
    """

    def __init__(self, usage: dict, api_key_source: str):
        super().__init__("SDK structured formatter refused the model response")
        self.usage = usage
        self.api_key_source = api_key_source


def _parent_death_args(argv) -> tuple[int, int]:
    """Parse the private POSIX lifetime channel passed by the transport.

    The read descriptor is a kernel capability, not a PID liveness guess.  Its
    only writer remains in the direct Collie parent, so EOF identifies that
    exact process lifetime even if the numeric PID is later reused.
    """
    values = list(argv or [])
    if (len(values) != 4 or values[0] != _PARENT_DEATH_FD_FLAG or
            values[2] != _PARENT_PID_FLAG):
        raise RuntimeError("worker is missing its parent-death ownership channel")
    try:
        read_fd = int(values[1])
        parent_pid = int(values[3])
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("worker parent-death ownership channel is invalid") from exc
    if read_fd < 3 or parent_pid <= 1:
        raise RuntimeError("worker parent-death ownership channel is invalid")
    return read_fd, parent_pid


def _kill_own_process_group(process_group: int) -> None:
    """Kill only the process group this caller is currently a member of."""
    process_group = int(process_group)
    if process_group <= 1 or os.getpgrp() != process_group:
        raise RuntimeError("worker process-group ownership changed")
    os.killpg(process_group, signal.SIGKILL)
    # SIGKILL includes this process, so returning would mean the kernel did not
    # honour the ownership kill and must never be treated as success.
    raise RuntimeError("worker process-group termination did not take effect")


def _watch_parent_pipe(read_fd: int, process_group: int) -> None:
    """Fork-child body: EOF from the exact parent kills the complete SDK tree."""
    try:
        # Never consume the model request or keep the worker's diagnostics open.
        # Popen(close_fds=True, pass_fds=(read_fd,)) guarantees there are no
        # other inherited descriptors at this pre-SDK boundary.
        for fd in (0, 1, 2):
            try:
                os.close(fd)
            except OSError:
                pass
        while True:
            try:
                value = os.read(read_fd, 1)
            except InterruptedError:
                continue
            except OSError:
                # Losing the capability unexpectedly is indistinguishable from
                # losing its owner; fail closed inside our own isolated group.
                value = b""
            if not value:
                break
        _kill_own_process_group(process_group)
    except BaseException:
        # The watchdog must never wander into the worker entry point.  If the
        # group identity check rejected a kill, exiting is safer than targeting
        # a numeric PGID which may now belong to an unrelated process.
        os._exit(125)


def _arm_linux_parent_death_signal(expected_parent_pid: int) -> None:
    """Ask Linux to kill the direct worker as soon as its creator dies.

    The independent pipe watchdog remains responsible for all descendants; the
    kernel signal makes the direct worker converge immediately even if Python is
    blocked.  Rechecking PPID closes the documented prctl arm-after-death race.
    """
    if not sys.platform.startswith("linux"):
        return
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                      ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(1, int(signal.SIGKILL), 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        errno = ctypes.get_errno()
        raise OSError(errno, "could not arm Linux parent-death signal")
    if os.getppid() != int(expected_parent_pid):
        _kill_own_process_group(os.getpgrp())


def _arm_posix_parent_death(read_fd: int, expected_parent_pid: int) -> int:
    """Arm crash cleanup before any SDK import, socket, or runtime spawn.

    Each SDK worker must lead a fresh session.  That gives the watchdog a group
    which cannot contain the Collie daemon or an unrelated/reused PID.  The
    watchdog is a separate fork child so it survives a direct worker crash long
    enough to reap an inherited Claude runtime when the parent channel closes.
    """
    if os.name == "nt" or not hasattr(os, "fork"):
        raise RuntimeError("POSIX parent-death ownership is unavailable")
    worker_pid = os.getpid()
    process_group = os.getpgrp()
    if (process_group != worker_pid or
            (hasattr(os, "getsid") and os.getsid(0) != worker_pid)):
        raise RuntimeError("worker does not own an isolated process group")
    if os.getppid() != int(expected_parent_pid):
        _kill_own_process_group(process_group)

    try:
        watchdog_pid = os.fork()
    except BaseException:
        try:
            os.close(read_fd)
        except OSError:
            pass
        raise
    if watchdog_pid == 0:
        _watch_parent_pipe(read_fd, process_group)
        os._exit(126)

    os.close(read_fd)
    _arm_linux_parent_death_signal(expected_parent_pid)
    # Portable arm-after-death check for macOS and defence in depth on Linux.
    if os.getppid() != int(expected_parent_pid):
        _kill_own_process_group(process_group)
    return int(watchdog_pid)


def _arm_parent_death_from_argv(argv=None) -> int | None:
    """Install POSIX ownership, while leaving the Windows Job path untouched."""
    values = sys.argv[1:] if argv is None else argv
    if os.name == "nt":
        if values:
            raise RuntimeError("Windows worker received POSIX ownership arguments")
        return None
    if list(values) == [_EXTERNAL_OWNER_FLAG]:
        # A Mission code worker deliberately keeps nested transports inside its
        # already-owned group.  Its durable PGID receipt is the crash owner; a
        # nested watchdog must not kill that group when an ordinary model call
        # finishes.  Reject this mode unless the worker truly inherited a group
        # led by another process.
        if os.getpgrp() == os.getpid():
            raise RuntimeError("external process owner was not inherited")
        return None
    read_fd, parent_pid = _parent_death_args(values)
    return _arm_posix_parent_death(read_fd, parent_pid)


def _field(value, name, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _message_kind(message) -> str:
    value = str(_field(message, "type", "") or "").lower()
    if value:
        return value
    name = type(message).__name__.lower()
    if name.endswith("message"):
        name = name[:-7]
    return name


def _response_tools(value) -> list:
    """The validated tool allowlist a structured request may name."""
    if not isinstance(value, list) or not value:
        raise RuntimeError("structured worker request is missing its response tools")
    names = []
    for name in value:
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("structured worker request has an invalid response tool")
        if name in names:
            raise RuntimeError("structured worker request repeats a response tool")
        names.append(name)
    return names


def _response_schema(names) -> dict:
    """Collie's {tool|answer} envelope as a provider-enforced JSON schema.

    A top-level ``anyOf`` is rejected by the provider with HTTP 400, so the
    alternation lives one level down under a required ``response`` wrapper.
    Tool *arguments* stay an open object: their schema is owned by Collie's host
    executor, and inventing a stricter one here would reject valid calls.
    """
    return {
        "type": "object",
        "properties": {
            "response": {
                "type": "object",
                "anyOf": [
                    {"type": "object",
                     "properties": {"answer": {"type": "string"}},
                     "required": ["answer"], "additionalProperties": False},
                    {"type": "object",
                     "properties": {"tool": {"type": "string", "enum": list(names)},
                                    "args": {"type": "object"}},
                     "required": ["tool", "args"], "additionalProperties": False},
                ],
            },
        },
        "required": ["response"],
        "additionalProperties": False,
    }


def _build_options(sdk, request: dict):
    extra_args = {
        "safe-mode": None,
        "no-session-persistence": None,
        "disable-slash-commands": None,
    }
    kwargs = {
        "model": request["model"],
        "fallback_model": None,
        "system_prompt": request["system_prompt"],
        "setting_sources": [],
        "tools": [],
        "allowed_tools": [],
        "mcp_servers": {},
        "strict_mcp_config": True,
        "skills": [],
        "plugins": [],
        "agents": {},
        "max_turns": 1,
        "extra_args": extra_args,
        "env": dict(_SDK_ENV),
    }
    effort = str(request.get("effort") or "default").lower()
    if effort not in ("", "default", "auto", "provider-default"):
        kwargs["effort"] = effort
    if request.get("response_format") == _STRUCTURED_FORMAT:
        # A consumer stopping at a failed formatter receipt cannot prevent the
        # CLI from already starting its next request. Disable that native retry
        # path explicitly; only Collie's separately reserved repair may run.
        # Documented CLI env control, verified with CLI 2.1.221 / SDK 0.2.136.
        kwargs["env"]["MAX_STRUCTURED_OUTPUT_RETRIES"] = "0"
        # Structured mode only.  The tool-less planner keeps a byte-identical
        # plain configuration, because its own action contract is not this
        # envelope and a schema would turn a valid plan into a failure.
        kwargs["output_format"] = {
            "type": "json_schema",
            "schema": _response_schema(_response_tools(request.get("response_tools"))),
        }
        # Match raw model-response identity against Assistant fragments as an
        # additional check on the one-response contract. SDK num_turns also
        # counts the formatter receipt, which is not a model response.
        # The extra traffic is one short-lived worker's discarded deltas, and
        # none of it is accumulated or streamed onward.
        kwargs["include_partial_messages"] = True
    return sdk.ClaudeAgentOptions(**kwargs)


def _sniffed_media_type(raw: bytes) -> str:
    """Identify the container of decoded image bytes; "" when unrecognized."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _anthropic_content(content) -> list:
    """Canonical worker blocks -> Anthropic content blocks for stream input.

    Base64 stays in memory the whole way: no attachment is ever written to a
    temporary file, so error and cancellation paths have nothing to clean up.
    Failure messages quote block kinds and media types only, never payload bytes.
    """
    if not isinstance(content, list) or not content:
        raise RuntimeError("worker request content must be a non-empty list")
    blocks = []
    images = 0
    for raw_block in content:
        if not isinstance(raw_block, dict):
            raise RuntimeError("worker request content block is not an object")
        kind = raw_block.get("type")
        if kind == "text":
            if set(raw_block) != {"type", "text"}:
                raise RuntimeError("worker request text block has unexpected fields")
            text = raw_block["text"]
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("worker request text block is empty")
            blocks.append({"type": "text", "text": text})
        elif kind == "image":
            if set(raw_block) != {"type", "media_type", "data"}:
                raise RuntimeError("worker request image block has unexpected fields")
            media_type, data = raw_block["media_type"], raw_block["data"]
            if not isinstance(media_type, str) or media_type not in _IMAGE_MEDIA_TYPES:
                raise RuntimeError("worker request image block has an unsupported "
                                   "media type")
            if not isinstance(data, str) or not data or len(data) > _MAX_IMAGE_B64:
                raise RuntimeError("worker request image block has invalid base64 size")
            try:
                decoded = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise RuntimeError("worker request image block is not valid base64") from exc
            if _sniffed_media_type(decoded) != media_type:
                raise RuntimeError("worker request image block does not match its "
                                   "declared media type")
            images += 1
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": media_type, "data": data}})
        else:
            raise RuntimeError("worker request has an unsupported content block type")
    if not images:
        raise RuntimeError("multimodal worker request carries no image block")
    if images > _MAX_REQUEST_IMAGES:
        raise RuntimeError("worker request exceeds the image count limit")
    return blocks


def _stream_prompt(blocks):
    """One user turn as SDK stream input; the only supported multimodal path.

    ``query(prompt=str)`` can carry text alone.  The SDK's streaming input is
    the documented shape that reaches the CLI as a real user message whose
    content is a block list, which is what carries pixels.
    """
    async def stream():
        yield {
            "type": "user",
            "session_id": "",
            "message": {"role": "user", "content": blocks},
            "parent_tool_use_id": None,
        }
    return stream()


def _is_empty(value) -> bool:
    return value in (None, [], {}, "")


def _validate_init(data, expected_model: str, structured: bool = False) -> str:
    if not isinstance(data, dict):
        raise RuntimeError("SDK init payload is not an object")
    for key in ("tools", "skills", "plugins", "agents", "slash_commands",
                "mcp_servers"):
        if key not in data:
            raise RuntimeError("SDK init did not attest an empty %s surface" % key)
        if structured and key == "tools":
            if data[key] != [_FORMATTER_TOOL]:
                raise RuntimeError("SDK init did not attest the structured formatter surface")
            continue
        if _is_empty(data.get(key)):
            continue
        # Structured mode's only admissible surface is the SDK's own synthetic
        # response formatter.  Every other surface, and any additional tool
        # beside the formatter, is still a foreign capability.
        raise RuntimeError("SDK init exposed a non-empty %s surface" % key)
    source_keys = [key for key in ("apiKeySource", "api_key_source")
                   if key in data]
    if not source_keys:
        raise RuntimeError("SDK init did not attest an API key source")
    if len(source_keys) != 1:
        raise RuntimeError("SDK init reported ambiguous API key sources")
    source = data[source_keys[0]]
    if not isinstance(source, str) or not source.strip():
        raise RuntimeError("SDK init reported an invalid API key source")
    normalized = source.strip().lower().replace("_", "-")
    # The official Agent SDK currently reports ``none`` when its bundled
    # first-party runtime uses the user's Claude plan login rather than an API
    # key.  The outer subscription guard separately requires a firstParty
    # claude.ai Pro/Max auth status.  Do not guess at undocumented aliases:
    # newly observed values must be reviewed before overnight admission.
    if normalized != "none":
        raise RuntimeError("SDK init reported a disallowed API key source")
    actual_model = str(data.get("model") or "").strip()
    if actual_model != expected_model:
        raise RuntimeError("SDK init model did not match the frozen route")
    return normalized


def _assistant_text(message) -> str:
    content = _field(message, "content", []) or []
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        kind = str(_field(block, "type", "") or type(block).__name__).lower()
        if "text" in kind:
            text = _field(block, "text", "")
            if isinstance(text, str):
                parts.append(text)
        elif "tool" in kind:
            raise RuntimeError("SDK assistant attempted foreign tool use")
    return "".join(parts)


def _block_kind(block) -> str:
    return str(_field(block, "type", "") or type(block).__name__).lower()


def _formatter_call(message, seen_id: str, seen_input):
    """Accept at most one structured-formatter tool use in an Assistant message.

    Any other tool name is a foreign tool call.  Assistant prose accompanying
    the formatter is deliberately dropped: only the validated canonical
    response may reach Collie, so partial or contradictory narration cannot be
    mistaken for the answer.
    """
    content = _field(message, "content", []) or []
    if isinstance(content, str):
        content = []
    for block in content:
        kind = _block_kind(block)
        if "tool_use" in kind or "tooluse" in kind:
            if _field(block, "name", "") != _FORMATTER_TOOL:
                raise RuntimeError("SDK assistant attempted foreign tool use")
            if seen_id:
                raise RuntimeError("SDK invoked the structured formatter more than once")
            block_id = _field(block, "id", "")
            if not isinstance(block_id, str) or not block_id.strip():
                raise RuntimeError("SDK structured formatter call is missing its id")
            value = _field(block, "input", None)
            if not isinstance(value, dict):
                raise RuntimeError("SDK structured formatter call has invalid input")
            seen_id, seen_input = block_id.strip(), value
        elif "tool" in kind:
            raise RuntimeError("SDK assistant attempted foreign tool use")
    return seen_id, seen_input


def _formatter_receipt(message):
    """``(receipted tool-use id, refused)`` for exactly one formatter result.

    ``refused`` is the formatter's own verdict that the response did not match
    the requested schema.  Only the literal ``True`` flag carries it: a flag
    which is neither boolean nor absent says nothing this transport can
    classify, so it still fails closed rather than being coerced either way.
    The formatter's report is deliberately not read -- the refusal is the fact,
    and its prose quotes the response Collie is refusing to accept.
    """
    content = _field(message, "content", []) or []
    if not isinstance(content, list) or len(content) != 1:
        raise RuntimeError("SDK structured formatter receipt is not a single tool result")
    block = content[0]
    kind = _block_kind(block)
    if "tool_result" not in kind and "toolresult" not in kind:
        raise RuntimeError("SDK emitted a foreign message during structured formatting")
    is_error = _field(block, "is_error", None)
    refused = is_error is True
    if not refused:
        if is_error is not None and is_error is not False:
            raise RuntimeError("SDK structured formatter reported a failed tool result")
        if _field(block, "content", None) != _FORMATTER_RECEIPT:
            raise RuntimeError("SDK structured formatter receipt was not recognised")
    tool_use_id = _field(block, "tool_use_id", "")
    if not isinstance(tool_use_id, str) or not tool_use_id.strip():
        raise RuntimeError("SDK structured formatter receipt is missing its tool id")
    return tool_use_id.strip(), refused


def _canonical_structured(value, allowed) -> str:
    """Validated structured output -> Collie's canonical envelope text.

    Structural validation is done here, at the process boundary, and the host
    still re-parses the returned text with its own envelope validator.
    """
    if not isinstance(value, dict) or set(value) != {"response"}:
        raise RuntimeError("SDK structured output is not a single response wrapper")
    response = value["response"]
    if not isinstance(response, dict):
        raise RuntimeError("SDK structured response is not an object")
    keys = set(response)
    if keys == {"answer"}:
        answer = response["answer"]
        if not isinstance(answer, str):
            raise RuntimeError("SDK structured answer is not a string")
        canonical = {"answer": answer}
    elif keys == {"tool", "args"}:
        name, args = response["tool"], response["args"]
        if not isinstance(name, str) or name not in allowed:
            raise RuntimeError("SDK structured response named a tool outside the allowlist")
        if not isinstance(args, dict):
            raise RuntimeError("SDK structured tool arguments are not an object")
        canonical = {"tool": name, "args": args}
    else:
        raise RuntimeError("SDK structured response is not exactly one tool call or answer")
    try:
        return json.dumps(canonical, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise RuntimeError("SDK structured response is not finite JSON") from exc


def _request_side_usage(value) -> dict:
    """The usage measured when the one model response started.

    ``message_start`` reports the request side in full -- it is the number the
    provider bills for the prompt, and it does not change afterwards.  Its
    ``output_tokens`` is a placeholder which the provider revises in a later
    stream event; on the refusal path that event only arrives once the SDK has
    already begun a repair turn, so the response side is reported as unmeasured
    rather than guessed from a placeholder or from the refused response itself.
    """
    value = value if isinstance(value, dict) else {}
    return _usage_dict({
        "input_tokens": value.get("input_tokens", 0),
        "output_tokens": 0,
        "cache_read_input_tokens": value.get("cache_read_input_tokens", 0),
        "cache_creation_input_tokens": value.get("cache_creation_input_tokens", 0),
    })


def _usage_dict(value) -> dict:
    value = value if isinstance(value, dict) else {}
    def counter(key):
        raw = value.get(key, 0)
        if raw is None:
            raw = 0
        if (not isinstance(raw, (int, float)) or isinstance(raw, bool)
                or not math.isfinite(float(raw)) or raw < 0
                or not float(raw).is_integer()):
            raise RuntimeError("SDK returned invalid %s usage" % key)
        return int(raw)
    return {
        "input_tokens": counter("input_tokens"),
        "output_tokens": counter("output_tokens"),
        "cache_read_input_tokens": counter("cache_read_input_tokens"),
        "cache_creation_input_tokens": counter("cache_creation_input_tokens"),
    }


def _is_post_result_error(exc) -> bool:
    """True only for the SDK's known post-error-result re-raise."""
    return isinstance(exc, Exception) and str(exc).startswith(_SDK_POST_RESULT_ERROR)


async def _query(request: dict, sdk) -> dict:
    options = _build_options(sdk, request)
    prompt = (_stream_prompt(_anthropic_content(request["content"]))
              if "content" in request else request["prompt"])
    structured = request.get("response_format") == _STRUCTURED_FORMAT
    allowed = _response_tools(request.get("response_tools")) if structured else []
    init_seen = False
    api_key_source = ""
    assistant_id = ""
    assistant_seen = False
    assistant_text = ""
    usage = {}
    result_seen = False
    rejected = ""
    formatter_id = ""
    formatter_input = None
    receipts = 0
    model_responses = 0
    model_response_id = ""
    start_usage = {}
    structured_output = None

    try:
        async for message in sdk.query(prompt=prompt, options=options):
            kind = _message_kind(message)
            if structured and result_seen:
                # The terminal result closes a structured turn.  Anything after
                # it is unaccounted-for content, not a late fragment of the one
                # validated response.
                raise RuntimeError("SDK emitted content after the terminal result")
            if structured and kind in ("stream_event", "streamevent"):
                event = _field(message, "event", {}) or {}
                if str(_field(event, "type", "") or "") == "message_start":
                    if not init_seen:
                        raise RuntimeError("SDK emitted a model response before validated init")
                    model_responses += 1
                    if model_responses > 1:
                        raise RuntimeError("SDK emitted more than one model response")
                    raw_message = _field(event, "message", {})
                    model_response_id = _field(raw_message, "id")
                    if not isinstance(model_response_id, str) or not model_response_id.strip():
                        raise RuntimeError("SDK model response is missing its id")
                    start_usage = _field(raw_message, "usage", {})
            elif structured and kind == "user":
                if not init_seen:
                    raise RuntimeError("SDK emitted a tool result before validated init")
                if not formatter_id:
                    raise RuntimeError(
                        "SDK emitted a tool result before the structured formatter call")
                receipt_id, refused = _formatter_receipt(message)
                if receipt_id != formatter_id:
                    raise RuntimeError(
                        "SDK structured formatter receipt did not match its call id")
                receipts += 1
                if receipts > 1:
                    raise RuntimeError(
                        "SDK emitted more than one structured formatter receipt")
                if refused:
                    # The provider refused this response against Collie's own
                    # schema.  Stop the stream here rather than reading on: the
                    # SDK's next act is a hidden repair turn, and letting it
                    # start would spend a second model response that no request
                    # budget authorized and that this worker may not return.
                    # The terminal result never arrives on this path, so the
                    # one-response contract is proved from the raw stream.
                    if model_responses != 1:
                        raise RuntimeError("SDK did not emit exactly one model response")
                    if model_response_id != assistant_id:
                        raise RuntimeError(
                            "SDK model response id did not match its Assistant message")
                    raise _StructuredContractRejected(
                        _request_side_usage(start_usage), api_key_source)
            elif kind == "system" and str(_field(message, "subtype", "")).lower() == "init":
                if init_seen:
                    raise RuntimeError("SDK emitted more than one init message")
                if assistant_seen or result_seen:
                    raise RuntimeError("SDK emitted init after response content")
                api_key_source = _validate_init(
                    _field(message, "data", {}), request["model"], structured)
                init_seen = True
            elif kind == "assistant":
                if not init_seen:
                    raise RuntimeError("SDK emitted assistant content before validated init")
                if result_seen:
                    raise RuntimeError("SDK emitted assistant content after result")
                if rejected:
                    # A rejection ends this turn.  Another assistant message
                    # afterwards is a second model turn and must never be hidden
                    # behind the first one's error classification.
                    raise RuntimeError(
                        "SDK emitted assistant content after a rejected turn")
                error = _field(message, "error")
                message_id = (_field(message, "id") or _field(message, "message_id")
                              or _field(message, "uuid"))
                message_id = (message_id.strip()
                              if isinstance(message_id, str) else "")
                # The SDK emits thinking and text as separate AssistantMessage
                # fragments with one shared Anthropic message_id.  That is still
                # one model answer. A second distinct message id would be another
                # assistant turn and must fail the one-request/one-turn contract.
                if message_id and assistant_id and message_id != assistant_id:
                    raise RuntimeError(
                        "SDK emitted more than one Assistant message id")
                if error:
                    if not isinstance(error, str) or error not in _PROVIDER_ERRORS:
                        raise RuntimeError("SDK assistant reported an error")
                    # A rejected turn carries provider error prose, not a model
                    # answer: its content is never read, accumulated, or
                    # returned.  Observed rejections have no message id, so one
                    # is not required here — only checked when present.
                    rejected = error
                    assistant_seen = True
                    continue
                if not message_id:
                    raise RuntimeError("SDK Assistant message is missing an id")
                assistant_id = message_id
                assistant_seen = True
                if structured:
                    formatter_id, formatter_input = _formatter_call(
                        message, formatter_id, formatter_input)
                else:
                    assistant_text += _assistant_text(message)
            elif kind == "result":
                if not init_seen:
                    raise RuntimeError("SDK emitted result before validated init")
                if not assistant_seen:
                    raise RuntimeError("SDK emitted result before Assistant message")
                if result_seen:
                    raise RuntimeError("SDK emitted more than one result message")
                result_seen = True
                is_error = _field(message, "is_error", False)
                if not isinstance(is_error, bool):
                    raise RuntimeError("SDK result has an invalid error flag")
                if rejected and not is_error:
                    # Observed rejections report subtype "success" with
                    # is_error true.  A result which claims outright success
                    # over a rejected turn contradicts it; believe neither.
                    raise RuntimeError(
                        "SDK result reported success after a rejected turn")
                if is_error and not rejected:
                    raise RuntimeError("SDK result reported an error")
                turns = _field(message, "num_turns", 0)
                if (not isinstance(turns, int) or isinstance(turns, bool)
                        or turns < 0):
                    raise RuntimeError("SDK result has an invalid turn count")
                # Structured mode spends a second accounted turn on the
                # formatter receipt, which is a tool result rather than another
                # model response.  Plain mode keeps its strict one-turn limit.
                if turns > (2 if structured else 1):
                    raise RuntimeError("SDK exceeded the one-turn limit")
                usage = _field(message, "usage", {}) or {}
                if structured:
                    structured_output = _field(message, "structured_output", None)
    except Exception as exc:
        # The SDK raises after already delivering the terminal error result.
        # Absorb that one known exception so the validated classification and
        # measured usage survive; anything else still fails closed.
        if not (rejected and result_seen and _is_post_result_error(exc)):
            raise

    if not init_seen:
        raise RuntimeError("SDK did not emit a validated init message")
    if not result_seen:
        raise RuntimeError("SDK did not emit a result message")
    if rejected:
        # A provider rejection: only the stable category, the measured
        # usage, and the reviewed auth attestation cross the process boundary.
        return {"ok": False, "provider_error": rejected,
                "error": "provider rejected the request: " + rejected,
                "usage": _usage_dict(usage), "api_key_source": api_key_source}
    if not assistant_seen or not assistant_id:
        raise RuntimeError("SDK did not emit exactly one Assistant message id")
    if structured:
        # A formatting failure is never a completed answer: every one of these
        # paths fails the call instead of returning prose the provider never
        # validated against the schema.
        if model_responses != 1:
            raise RuntimeError("SDK did not emit exactly one model response")
        if not formatter_id:
            raise RuntimeError("SDK did not invoke the structured formatter")
        if receipts != 1:
            raise RuntimeError("SDK did not emit exactly one structured formatter receipt")
        if model_response_id != assistant_id:
            raise RuntimeError("SDK model response id did not match its Assistant message")
        # Python equality treats True == 1 and 1 == 1.0. Those are distinct
        # JSON argument values and can produce different host-tool behavior.
        canonical = _canonical_structured(structured_output, allowed)
        formatted = _canonical_structured(formatter_input, allowed)
        if json.dumps(json.loads(canonical), sort_keys=True) != json.dumps(
                json.loads(formatted), sort_keys=True):
            raise RuntimeError("SDK structured output did not match the formatter input")
        return {"ok": True, "text": canonical,
                "usage": _usage_dict(usage), "api_key_source": api_key_source,
                "response_format": _STRUCTURED_FORMAT, "response_tools": allowed}
    return {"ok": True, "text": assistant_text, "usage": _usage_dict(usage),
            "api_key_source": api_key_source}


def _read_request() -> dict:
    raw = sys.stdin.buffer.read(_MAX_MULTIMODAL_REQUEST_BYTES + 1)
    if len(raw) > _MAX_MULTIMODAL_REQUEST_BYTES:
        raise RuntimeError("worker request exceeded the safety limit")
    def reject_constant(value):
        raise ValueError("non-finite JSON number is forbidden: %s" % value)
    request = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    protocol = request.get("protocol") if isinstance(request, dict) else None
    if protocol not in (1, 2, 3, 4) or isinstance(protocol, bool):
        raise RuntimeError("invalid worker protocol")
    for key in ("model", "system_prompt"):
        if not isinstance(request.get(key), str):
            raise RuntimeError("worker request is missing %s" % key)
    # Protocols 3/4 are 1/2 plus provider-enforced structured responses.  The
    # capability is validated here, before the SDK, its runtime, or any socket
    # exists, so an unsupported or half-specified mode never reaches inference.
    structured = protocol in (3, 4)
    if structured:
        if request.get("response_format") != _STRUCTURED_FORMAT:
            raise RuntimeError("invalid worker response format")
        request["response_tools"] = _response_tools(request.get("response_tools"))
    elif "response_format" in request or "response_tools" in request:
        raise RuntimeError("plain worker request must not carry a response format")
    if protocol in (1, 3):
        # The text protocol keeps its original, smaller stdin budget.
        if len(raw) > _MAX_TEXT_REQUEST_BYTES:
            raise RuntimeError("worker request exceeded the safety limit")
        if "content" in request:
            raise RuntimeError("text worker request must not carry content blocks")
        if not isinstance(request.get("prompt"), str):
            raise RuntimeError("worker request is missing prompt")
    else:
        if "prompt" in request:
            raise RuntimeError("multimodal worker request must not carry a prompt string")
        # Validate the whole attachment surface before the SDK, its bundled
        # runtime, or any network socket exists.
        _anthropic_content(request.get("content"))
    return request


def main() -> int:
    try:
        # Arm the exact-parent lifetime channel before reading prompt bytes or
        # importing the optional SDK.  A daemon crash therefore converges both
        # this worker and every SDK runtime which later inherits its group.
        _arm_parent_death_from_argv()
        request = _read_request()
        # The sibling transport adapter is also named ``claude_agent_sdk.py``.
        # When this file is executed directly, Python prepends the harness
        # directory to sys.path and would import that sibling instead of the
        # installed official package. Remove only this script directory before
        # resolving the optional dependency.
        worker_dir = os.path.normcase(os.path.abspath(os.path.dirname(__file__)))
        sys.path[:] = [entry for entry in sys.path
                       if os.path.normcase(os.path.abspath(entry or os.getcwd())) != worker_dir]
        import claude_agent_sdk as sdk  # optional dependency: worker-only lazy import
        result = asyncio.run(_query(request, sdk))
    except _StructuredContractRejected as exc:
        # A response the provider refused against Collie's schema, reported as a
        # classified failure so the parent can tell it apart from a broken
        # transport.  The exit code stays non-zero: this call produced no
        # answer, and only the category, the measured usage and the reviewed
        # auth attestation cross the boundary.
        result = {"ok": False, "structured_error": _FORMATTER_REFUSED,
                  "error": "the structured formatter refused the model response",
                  "usage": exc.usage, "api_key_source": exc.api_key_source}
    except Exception as exc:
        # Parent applies Collie's secret redactor before surfacing this bounded text.
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:1000])}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":"),
                                allow_nan=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
