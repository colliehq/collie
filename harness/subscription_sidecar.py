"""Internal OpenAI-compatible sidecar for subscription-native benchmarks.

The sidecar is deliberately small and stdlib-only.  It accepts one OpenAI
``chat/completions`` turn, serializes the caller-owned system prompt,
conversation, and tools into a brand-neutral text protocol, and delegates that
single turn to Collie's existing :class:`ClaudeAgentSdkProvider` worker path.
The SDK worker remains the authority for model-route and ``apiKeySource=none``
attestation.  The caller's harness remains the owner of its agent loop and tool
execution.

This server is not a general proxy.  It defaults to loopback, uses a fixed
non-secret bearer sentinel, and exposes one frozen model.  The benchmark runner
may explicitly admit RFC1918 peers when the server and harness are separated by
an evaluator-owned internal Docker network; Docker publishes no host port.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as _datetime
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping


MODEL = "claude-opus-4-8"
BEARER_SENTINEL = "subscription-sidecar-internal-only-v1"
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_PROMPT_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MESSAGES = 256
MAX_TOOLS = 128
MAX_TEXT_BYTES = 512 * 1024
READ_TIMEOUT_SECONDS = 15
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


class SidecarError(Exception):
    """A safe HTTP-facing error which never contains prompt or credential text."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)


class RequestCancelled(Exception):
    pass


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_usage(value: Any) -> dict[str, int]:
    value = value if isinstance(value, Mapping) else {}
    fields = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cache_read_input_tokens": "cache_read_input_tokens",
        "cache_creation_input_tokens": "cache_creation_input_tokens",
    }
    usage: dict[str, int] = {}
    for output, source in fields.items():
        raw = value.get(source, 0)
        if isinstance(raw, bool):
            raw = 0
        try:
            number = int(raw or 0)
        except (TypeError, ValueError, OverflowError):
            number = 0
        usage[output] = max(0, number)
    return usage


class ReceiptLedger:
    """Crash-safe append-only ledger made of immutable atomic receipt files."""

    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sequence = 0

    def _append(self, event: Mapping[str, Any]) -> Path:
        payload = _canonical(dict(event)) + b"\n"
        if len(payload) > 32 * 1024:
            raise RuntimeError("sidecar receipt exceeded its safety limit")
        with self._lock:
            self._sequence += 1
            leaf = "%020d-%s.json" % (self._sequence, uuid.uuid4().hex)
            target = self.directory / leaf
            temp = self.directory / ("." + leaf + ".tmp")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            fd = os.open(str(temp), flags, 0o600)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("short receipt write")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                if target.exists():
                    raise RuntimeError("sidecar receipt id collision")
                os.replace(str(temp), str(target))
                with contextlib.suppress(OSError):
                    parent_fd = os.open(str(self.directory), os.O_RDONLY)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temp.unlink()
            return target

    def reserve(self, request_id: str, request_sha256: str,
                prompt_sha256: str, request_bytes: int) -> None:
        self._append({
            "schema_version": 1,
            "event": "reserved",
            "request_id": request_id,
            "created_at_utc": _utc_now(),
            "model": MODEL,
            "request_sha256": request_sha256,
            "prompt_sha256": prompt_sha256,
            "request_bytes": int(request_bytes),
        })

    def settle(self, request_id: str, outcome: str, duration_ms: int,
               usage: Mapping[str, Any] | None = None,
               error_code: str = "") -> None:
        row: dict[str, Any] = {
            "schema_version": 1,
            "event": "settled",
            "request_id": request_id,
            "created_at_utc": _utc_now(),
            "model": MODEL,
            "outcome": str(outcome),
            "duration_ms": max(0, int(duration_ms)),
            "usage": _safe_usage(usage),
        }
        if error_code:
            row["error_code"] = str(error_code)[:80]
        self._append(row)

    def reject_budget(self, request_id: str, max_requests: int) -> None:
        """Record a deterministic evaluator budget stop without a model call."""
        self._append({
            "schema_version": 1,
            "event": "budget_exhausted",
            "request_id": request_id,
            "created_at_utc": _utc_now(),
            "model": MODEL,
            "max_requests": int(max_requests),
        })


def _text_content(value: Any, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        # OpenAI Chat Completions permits text-only content as an array of
        # typed parts. Prime/Pi preserve that native representation even for
        # an ordinary string prompt, whereas Collie sends a scalar string.
        # Normalize the two equivalent encodings without admitting image or
        # other multimodal payloads into this text-only frozen benchmark.
        parts: list[str] = []
        for part in value:
            if (not isinstance(part, Mapping) or part.get("type") != "text"
                    or not isinstance(part.get("text"), str)):
                raise SidecarError(
                    400, "invalid_request", "%s must contain only text parts" % field)
            parts.append(str(part["text"]))
        text = "".join(parts)
    else:
        raise SidecarError(400, "invalid_request", "%s must be text" % field)
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise SidecarError(413, "input_too_large", "%s is too large" % field)
    return text


def _normalize_tool_call(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SidecarError(400, "invalid_request", "assistant tool_calls must be objects")
    function = value.get("function")
    if value.get("type", "function") != "function" or not isinstance(function, Mapping):
        raise SidecarError(400, "invalid_request", "only function tool calls are supported")
    name = function.get("name")
    arguments = function.get("arguments", "{}")
    if not isinstance(name, str) or not name or not isinstance(arguments, str):
        raise SidecarError(400, "invalid_request", "assistant tool call is invalid")
    if len(arguments.encode("utf-8")) > MAX_TEXT_BYTES:
        raise SidecarError(413, "input_too_large", "assistant tool arguments are too large")
    return {
        "id": str(value.get("id") or "history_%d" % index)[:128],
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _normalize_messages(value: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(value, list) or not value:
        raise SidecarError(400, "invalid_request", "messages must be a non-empty array")
    if len(value) > MAX_MESSAGES:
        raise SidecarError(413, "input_too_large", "too many messages")
    system_parts: list[str] = []
    history: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise SidecarError(400, "invalid_request", "messages must contain objects")
        role = item.get("role")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise SidecarError(400, "invalid_request", "message role is unsupported")
        content = _text_content(item.get("content"), "message content")
        if role in {"system", "developer"}:
            system_parts.append(content)
            continue
        message: dict[str, Any] = {"role": role, "content": content}
        if role == "assistant" and item.get("tool_calls") is not None:
            calls = item.get("tool_calls")
            if not isinstance(calls, list):
                raise SidecarError(400, "invalid_request", "tool_calls must be an array")
            message["tool_calls"] = [
                _normalize_tool_call(call, call_index)
                for call_index, call in enumerate(calls)
            ]
        if role == "tool":
            call_id = item.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise SidecarError(400, "invalid_request", "tool message needs tool_call_id")
            message["tool_call_id"] = call_id[:128]
            if isinstance(item.get("name"), str):
                message["name"] = item["name"][:128]
        history.append(message)
    if not history:
        raise SidecarError(400, "invalid_request", "messages contain no conversation turn")
    system = "\n\n".join(part for part in system_parts if part)
    return system or "Follow the caller's instructions.", history


def _normalize_tools(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SidecarError(400, "invalid_request", "tools must be an array")
    if len(value) > MAX_TOOLS:
        raise SidecarError(413, "input_too_large", "too many tools")
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or item.get("type") != "function":
            raise SidecarError(400, "invalid_request", "only function tools are supported")
        function = item.get("function")
        if not isinstance(function, Mapping):
            raise SidecarError(400, "invalid_request", "function tool is invalid")
        name = function.get("name")
        if not isinstance(name, str) or not name or len(name) > 128 or name in names:
            raise SidecarError(400, "invalid_request", "function tool name is invalid")
        description = function.get("description", "")
        parameters = function.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(description, str) or not isinstance(parameters, Mapping):
            raise SidecarError(400, "invalid_request", "function tool schema is invalid")
        normalized = {
            "name": name,
            "description": description,
            "parameters": dict(parameters),
        }
        strict = function.get("strict")
        if strict is not None:
            if not isinstance(strict, bool):
                raise SidecarError(400, "invalid_request", "function tool strict must be boolean")
            normalized["strict"] = strict
        if len(_canonical(normalized)) > 128 * 1024:
            raise SidecarError(413, "input_too_large", "function tool schema is too large")
        names.add(name)
        result.append(normalized)
    return result


def serialize_turn(body: Mapping[str, Any]) -> tuple[str, str, set[str]]:
    """Return literal system prompt, brand-neutral single-turn prompt, tool names."""
    system, history = _normalize_messages(body.get("messages"))
    tools = _normalize_tools(body.get("tools"))
    protocol = {
        "conversation": history,
        "tools": tools,
    }
    prompt = (
        "Use the following caller-owned conversation and tool definitions to produce "
        "the next assistant turn. Tool execution belongs to the caller.\n\n"
        "INPUT_JSON:\n" + _canonical(protocol).decode("utf-8") + "\n\n"
        "RESPONSE_CONTRACT:\n"
        "Return exactly one JSON object and no other text. To request a tool, return "
        '{"tool":"<tool name>","args":{...}}. To answer, return '
        '{"answer":"<answer text>"}. Do not add keys, Markdown, or commentary.'
    )
    if len(system.encode("utf-8")) + len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise SidecarError(413, "input_too_large", "serialized prompt is too large")
    return system, prompt, {tool["name"] for tool in tools}


@dataclasses.dataclass(frozen=True)
class TurnResult:
    kind: str
    answer: str = ""
    tool: str = ""
    args: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    usage: Mapping[str, Any] = dataclasses.field(default_factory=dict)


def _strict_result(data: Mapping[str, Any], allowed_tools: set[str]) -> TurnResult:
    if data.get("api_key_source") != "none":
        raise RuntimeError("subscription auth attestation is missing")
    text = data.get("text")
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise RuntimeError("SDK worker returned invalid assistant text")
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("assistant violated the sidecar response contract") from exc
    usage = _safe_usage(data.get("usage"))
    if isinstance(value, dict) and set(value) == {"answer"} and isinstance(value["answer"], str):
        return TurnResult("answer", answer=value["answer"], usage=usage)
    if (isinstance(value, dict) and set(value) == {"tool", "args"}
            and isinstance(value["tool"], str) and isinstance(value["args"], dict)
            and value["tool"] in allowed_tools):
        return TurnResult("tool", tool=value["tool"], args=value["args"], usage=usage)
    raise RuntimeError("assistant violated the sidecar response contract")


class SdkTransport:
    """Thin adapter over the existing owned Claude Agent SDK worker path."""

    def __init__(self, timeout: int = 180):
        from .claude_agent_sdk import ClaudeAgentSdkProvider

        self.provider = ClaudeAgentSdkProvider(
            model=MODEL, timeout=int(timeout), effort="high", subscription_only=True)

    def invoke(self, system: str, prompt: str, scope: str,
               cancel_event: threading.Event) -> Mapping[str, Any]:
        provider = self.provider
        registration = provider._register_pending(scope)
        try:
            if cancel_event.is_set():
                provider.cancel_for(scope)()
                raise RequestCancelled()
            return provider._run_worker(
                provider._worker_request(system, prompt),
                cancel_scope=scope, registration=registration)
        except RuntimeError as exc:
            if cancel_event.is_set():
                raise RequestCancelled() from exc
            raise
        finally:
            provider._retire_pending(registration)

    def cancel(self, scope: str) -> bool:
        return bool(self.provider.cancel_for(scope)())


class SidecarService:
    def __init__(self, ledger: ReceiptLedger, transport: Any | None = None,
                 *, max_requests: int = 0):
        if (not isinstance(max_requests, int) or isinstance(max_requests, bool)
                or max_requests < 0):
            raise ValueError("sidecar max_requests must be a non-negative integer")
        self.ledger = ledger
        self.transport = transport or SdkTransport()
        self.max_requests = max_requests
        self._requests_started = 0
        self._budget_rejection_recorded = False
        self._active: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _activate(self, request_id: str) -> threading.Event:
        event = threading.Event()
        exhausted = False
        with self._lock:
            if request_id in self._active:
                raise SidecarError(409, "request_conflict", "request id is already active")
            if self.max_requests and self._requests_started >= self.max_requests:
                exhausted = True
                record_rejection = not self._budget_rejection_recorded
                self._budget_rejection_recorded = True
            else:
                record_rejection = False
                self._requests_started += 1
                self._active[request_id] = event
        if exhausted:
            if record_rejection:
                self.ledger.reject_budget(request_id, self.max_requests)
            raise SidecarError(
                429, "request_budget_exhausted",
                "evaluator model-request budget is exhausted")
        return event

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            event = self._active.get(request_id)
            if event is None:
                return False
            event.set()
        try:
            self.transport.cancel(request_id)
        except Exception:
            pass
        return True

    def complete(self, body: Mapping[str, Any], raw_body: bytes,
                 request_id: str) -> TurnResult:
        system, prompt, tools = serialize_turn(body)
        prompt_bytes = (system + "\n" + prompt).encode("utf-8")
        cancel_event = self._activate(request_id)
        started = time.monotonic()
        usage: Mapping[str, Any] = {}
        outcome = "error"
        error_code = "transport_error"
        try:
            self.ledger.reserve(request_id, _sha256(raw_body), _sha256(prompt_bytes),
                                len(raw_body))
            if cancel_event.is_set():
                raise RequestCancelled()
            data = self.transport.invoke(system, prompt, request_id, cancel_event)
            if cancel_event.is_set():
                raise RequestCancelled()
            if not isinstance(data, Mapping) or not data.get("ok"):
                raise RuntimeError("SDK worker failed")
            result = _strict_result(data, tools)
            usage = result.usage
            outcome = "completed"
            error_code = ""
            return result
        except RequestCancelled:
            outcome = "cancelled"
            error_code = "request_cancelled"
            raise SidecarError(409, error_code, "request was cancelled")
        except TimeoutError as exc:
            outcome = "timeout"
            error_code = "request_timeout"
            raise SidecarError(504, error_code, "subscription transport timed out") from exc
        except SidecarError:
            raise
        except Exception as exc:
            error_code = ("response_contract_error" if "response contract" in str(exc)
                          else "transport_error")
            raise SidecarError(502, error_code, "subscription transport failed") from exc
        finally:
            duration = round((time.monotonic() - started) * 1000)
            try:
                self.ledger.settle(request_id, outcome, duration, usage, error_code)
            finally:
                with self._lock:
                    self._active.pop(request_id, None)


def _openai_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    value = _safe_usage(usage)
    prompt = (value["input_tokens"] + value["cache_read_input_tokens"]
              + value["cache_creation_input_tokens"])
    completion = value["output_tokens"]
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {
            "cached_tokens": value["cache_read_input_tokens"],
        },
    }


def _completion(request_id: str, result: TurnResult) -> dict[str, Any]:
    created = int(time.time())
    message: dict[str, Any] = {"role": "assistant", "content": result.answer}
    finish = "stop"
    if result.kind == "tool":
        call_id = "call_" + uuid.uuid4().hex
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {
                    "name": result.tool,
                    "arguments": _canonical(dict(result.args)).decode("utf-8"),
                },
            }],
        }
        finish = "tool_calls"
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": created,
        "model": MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": _openai_usage(result.usage),
    }


def _sse_chunks(request_id: str, result: TurnResult,
                include_usage: bool) -> list[bytes]:
    created = int(time.time())
    base = {"id": request_id, "object": "chat.completion.chunk",
            "created": created, "model": MODEL}
    chunks: list[dict[str, Any]] = [{
        **base,
        "choices": [{"index": 0, "delta": {"role": "assistant"},
                     "finish_reason": None}],
    }]
    finish = "stop"
    if result.kind == "answer":
        chunks.append({**base, "choices": [{"index": 0,
                       "delta": {"content": result.answer}, "finish_reason": None}]})
    else:
        finish = "tool_calls"
        chunks.append({**base, "choices": [{"index": 0, "delta": {
            "tool_calls": [{"index": 0, "id": "call_" + uuid.uuid4().hex,
                            "type": "function", "function": {
                                "name": result.tool,
                                "arguments": _canonical(dict(result.args)).decode("utf-8"),
                            }}]}, "finish_reason": None}]})
    chunks.append({**base, "choices": [{"index": 0, "delta": {},
                   "finish_reason": finish}]})
    if include_usage:
        chunks.append({**base, "choices": [], "usage": _openai_usage(result.usage)})
    encoded = [b"data: " + _canonical(chunk) + b"\n\n" for chunk in chunks]
    encoded.append(b"data: [DONE]\n\n")
    if sum(map(len, encoded)) > MAX_RESPONSE_BYTES:
        raise SidecarError(502, "output_too_large", "sidecar response is too large")
    return encoded


class SidecarHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SubscriptionSidecar/1"

    @property
    def service(self) -> SidecarService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, _format: str, *args: Any) -> None:
        return

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(READ_TIMEOUT_SECONDS)

    def _internal_peer(self) -> bool:
        try:
            address = ipaddress.ip_address(self.client_address[0])
            return address.is_loopback or (
                bool(getattr(self.server, "allow_private_peers", False))
                and address.is_private
            )
        except ValueError:
            return False

    def _authorized(self) -> bool:
        expected = "Bearer " + BEARER_SENTINEL
        actual = self.headers.get("Authorization", "")
        return hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8"))

    def _send_json(self, status: int, value: Mapping[str, Any]) -> None:
        payload = _canonical(value)
        if len(payload) > MAX_RESPONSE_BYTES:
            status = 500
            payload = _canonical({"error": {"code": "output_too_large",
                                             "message": "sidecar response is too large"}})
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _error(self, error: SidecarError) -> None:
        self._send_json(error.status, {"error": {"type": "sidecar_error",
                                                  "code": error.code,
                                                  "message": error.message}})

    def _guard(self, require_auth: bool = True) -> bool:
        if not self._internal_peer():
            self._error(SidecarError(403, "internal_only", "sidecar is internal-only"))
            return False
        if require_auth and not self._authorized():
            self._error(SidecarError(401, "unauthorized", "invalid bearer sentinel"))
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/healthz"}:
            if self._guard(require_auth=False):
                self._send_json(200, {"status": "ok", "model": MODEL})
            return
        if path in {"/v1/models", "/models"}:
            if self._guard():
                self._send_json(200, {"object": "list", "data": [{
                    "id": MODEL, "object": "model", "owned_by": "subscription-sidecar",
                }]})
            return
        self._error(SidecarError(404, "not_found", "endpoint not found"))

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._guard():
            return
        prefix = "/v1/requests/"
        path = self.path.split("?", 1)[0]
        if not path.startswith(prefix):
            self._error(SidecarError(404, "not_found", "endpoint not found"))
            return
        request_id = path[len(prefix):]
        if not _REQUEST_ID.fullmatch(request_id):
            self._error(SidecarError(400, "invalid_request", "request id is invalid"))
            return
        cancelled = self.service.cancel(request_id)
        self._send_json(200 if cancelled else 404,
                        {"id": request_id, "cancelled": cancelled})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        if self.path.split("?", 1)[0] != "/v1/chat/completions":
            self._error(SidecarError(404, "not_found", "endpoint not found"))
            return
        try:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise SidecarError(411, "length_required", "Content-Length is required")
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise SidecarError(400, "invalid_request", "Content-Length is invalid") from exc
            if length < 0 or length > MAX_REQUEST_BYTES:
                raise SidecarError(413, "input_too_large", "request body is too large")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise SidecarError(400, "invalid_request", "request body is incomplete")
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SidecarError(400, "invalid_json", "request body is not valid JSON") from exc
            if not isinstance(body, dict):
                raise SidecarError(400, "invalid_request", "request body must be an object")
            if body.get("model") != MODEL:
                raise SidecarError(400, "model_mismatch", "only the frozen model is available")
            stream = body.get("stream", False)
            if not isinstance(stream, bool):
                raise SidecarError(400, "invalid_request", "stream must be boolean")
            options = body.get("stream_options") or {}
            if not isinstance(options, Mapping):
                raise SidecarError(400, "invalid_request", "stream_options must be an object")
            include_usage = options.get("include_usage", False)
            if not isinstance(include_usage, bool):
                raise SidecarError(400, "invalid_request",
                                   "stream_options.include_usage must be boolean")
            supplied_id = self.headers.get("X-Request-ID", "")
            request_id = supplied_id if _REQUEST_ID.fullmatch(supplied_id) else (
                "chatcmpl_" + uuid.uuid4().hex)
            result = self.service.complete(body, raw, request_id)
            if not stream:
                self._send_json(200, _completion(request_id, result))
                return
            chunks = _sse_chunks(request_id, result, include_usage)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                self.service.cancel(request_id)
            self.close_connection = True
        except SidecarError as exc:
            self._error(exc)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return


class SidecarServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: SidecarService,
                 *, allow_private_peers: bool = False):
        bind_address = ipaddress.ip_address(address[0])
        if not bind_address.is_loopback and not (
                allow_private_peers and bind_address.is_unspecified):
            raise ValueError("subscription sidecar may only bind to loopback")
        self.service = service
        self.allow_private_peers = bool(allow_private_peers)
        super().__init__(address, SidecarHandler)


def make_server(bind: str, port: int, ledger_dir: str | os.PathLike[str],
                timeout: int = 180, transport: Any | None = None,
                *, allow_private_peers: bool = False,
                max_requests: int = 0) -> SidecarServer:
    if int(timeout) <= 0:
        raise ValueError("subscription sidecar timeout must be positive")
    service = SidecarService(
        ReceiptLedger(ledger_dir),
        transport=(transport if transport is not None else SdkTransport(timeout=timeout)),
        max_requests=max_requests,
    )
    return SidecarServer((bind, int(port)), service,
                         allow_private_peers=allow_private_peers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="subscription-sidecar")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ledger-dir", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--max-requests", type=int, default=0,
                        help="maximum accepted model requests; zero means unlimited")
    parser.add_argument("--allow-private-peers", action="store_true",
                        help="admit authenticated RFC1918 peers on an internal network")
    args = parser.parse_args(argv)
    server = make_server(
        args.bind, args.port, args.ledger_dir, args.timeout,
        allow_private_peers=args.allow_private_peers,
        max_requests=args.max_requests,
    )
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
