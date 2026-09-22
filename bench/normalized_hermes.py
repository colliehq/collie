"""Hermes launch isolation and trace admission for normalized benchmarks.

This module deliberately does not import Hermes.  It prepares a fresh
``HERMES_HOME`` that points Hermes' native agent loop at a benchmark-owned,
OpenAI-compatible Claude Agent SDK sidecar.  The sidecar owns subscription
authentication; Hermes receives only a fixed, non-secret internal bearer.

The JSONL trace contract consumed here is intentionally small.  Producers emit
``run_start``, paired ``sdk_request``/``sdk_response`` events, paired
``tool_call``/``tool_result`` events, one ``final`` event, and one ``run_end``.
Admission fails closed when a request is rerouted, an SDK foreign surface is
present, or Hermes invokes anything outside its native file/shell toolsets.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from typing import Any, Mapping
from urllib.parse import urlsplit


MODEL = "claude-opus-4-8"
HERMES_VERSION = "0.15.2"
PROVIDER_NAME = "normalized-sdk"
PROVIDER_ID = "custom:" + PROVIDER_NAME
API_KEY_SENTINEL = "subscription-sidecar-internal-only-v1"
TRACE_SCHEMA = "collie-normalized-hermes-trace-v1"

_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_TRACE_BYTES = 8 * 1024 * 1024
_SAFE_ENV_KEYS = frozenset({
    "COMSPEC", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "PATHEXT",
    "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR",
})
_NATIVE_TOOLS = frozenset({
    "terminal", "process", "read_file", "write_file", "patch", "search_files",
})
_DISABLED_TOOLSETS = (
    "browser", "clarify", "code_execution", "computer_use", "context_engine",
    "cronjob", "delegation", "discord", "discord_admin", "homeassistant",
    "image_gen", "kanban", "memory", "messaging", "session_search", "skills",
    "skills_hub", "spotify", "todo", "tts", "vision", "web", "x_search",
    "yuanbao",
)
_EVENT_ALIASES = {
    "hermes_start": "run_start",
    "model_request": "sdk_request",
    "model_response": "sdk_response",
    "tool_start": "tool_call",
    "tool_end": "tool_result",
    "hermes_final": "final",
    "terminal": "run_end",
    "done": "run_end",
}
_SENSITIVE_NAME = re.compile(
    r"(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|PROXY|CERT|SSL|TLS|NODE_OPTIONS|PYTHONPATH)",
    re.IGNORECASE,
)


class HermesIsolationError(ValueError):
    """The requested launch would not be fresh or transport-isolated."""


class HermesTraceError(ValueError):
    """The trace cannot be parsed as the bounded JSONL contract."""


@dataclass(frozen=True)
class HermesLaunch:
    home: Path
    config_path: Path
    workspace: Path
    endpoint: str
    argv: tuple[str, ...]
    env: dict[str, str]
    config_sha256: str
    model: str = MODEL
    provider: str = PROVIDER_ID
    api_key_sentinel: str = API_KEY_SENTINEL


@dataclass(frozen=True)
class HermesProcessOutput:
    ok: bool
    final_text: str
    returncode: int
    stderr: str
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HermesTraceSummary:
    admitted: bool
    terminal_status: str
    terminal_exit_code: int | None
    final_text: str
    physical_requests: int
    completed_requests: int
    tool_calls: int
    completed_tools: int
    native_edit_calls: int
    native_tool_names: tuple[str, ...]
    local_tool_observed: bool
    usage: dict[str, int]
    trace_sha256: str
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HermesStateSummary:
    admitted: bool
    physical_requests: int
    tool_calls: int
    completed_tools: int
    native_edit_calls: int
    native_tool_names: tuple[str, ...]
    usage: dict[str, int]
    errors: tuple[str, ...]


def _run_receipt(
    *,
    started: float,
    returncode: int | None,
    error_category: str,
    trace: HermesTraceSummary | None = None,
    state: HermesStateSummary | None = None,
    config_sha256: str = "",
) -> dict[str, Any]:
    """Build the intentionally redacted worker-facing receipt."""
    trace = trace or HermesTraceSummary(
        admitted=False,
        terminal_status="",
        terminal_exit_code=None,
        final_text="",
        physical_requests=0,
        completed_requests=0,
        tool_calls=0,
        completed_tools=0,
        native_edit_calls=0,
        native_tool_names=(),
        local_tool_observed=False,
        usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        trace_sha256="",
        errors=(),
    )
    if state is not None:
        admitted = state.admitted
        physical_requests = state.physical_requests
        completed_requests = state.physical_requests
        tool_calls = state.tool_calls
        completed_tools = state.completed_tools
        native_edit_calls = state.native_edit_calls
        native_tool_names = state.native_tool_names
        usage = state.usage
        terminal_status = "completed" if state.admitted else ""
        terminal_exit_code = 0 if state.admitted else None
    else:
        admitted = trace.admitted
        physical_requests = trace.physical_requests
        completed_requests = trace.completed_requests
        tool_calls = trace.tool_calls
        completed_tools = trace.completed_tools
        native_edit_calls = trace.native_edit_calls
        native_tool_names = trace.native_tool_names
        usage = trace.usage
        terminal_status = trace.terminal_status
        terminal_exit_code = trace.terminal_exit_code
    return {
        "worker_outcome": "candidate" if not error_category else "invalid_infrastructure",
        "error_category": error_category,
        "returncode": returncode,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "native_evidence_admitted": admitted and not error_category,
        "physical_model_requests": physical_requests,
        "completed_model_requests": completed_requests,
        "native_tool_calls_observed": tool_calls,
        "completed_native_tool_calls_observed": completed_tools,
        "successful_native_tool_calls_observed": completed_tools,
        "native_tool_names": list(native_tool_names),
        "tool_evidence": {
            "native_tool_calls": tool_calls,
            "native_edit_calls": native_edit_calls,
            "terminal_observed": returncode is not None,
        },
        "usage": dict(usage),
        "terminal_status": terminal_status,
        "terminal_exit_code": terminal_exit_code,
        "trace_sha256": trace.trace_sha256,
        "config_sha256": config_sha256,
        "runtime": {
            "product": "hermes-agent",
            "version": HERMES_VERSION,
            "entrypoint": "hermes -z",
            "provider": PROVIDER_ID,
            "model": MODEL,
            "transport": "openai_chat_completions",
            "profile": "ephemeral",
        },
    }


def _yaml_string(value: str) -> str:
    # JSON strings are a strict subset of YAML scalars and avoid an optional
    # PyYAML dependency in the benchmark launcher.
    return json.dumps(value, ensure_ascii=False)


def _validated_endpoint(endpoint: str) -> str:
    value = str(endpoint or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HermesIsolationError("Hermes endpoint must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HermesIsolationError("Hermes endpoint must not carry credentials, query, or fragment")
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        raise HermesIsolationError("Hermes endpoint must end in /v1")
    return value


def render_hermes_config(endpoint: str, *, max_turns: int = 30) -> str:
    """Return the complete isolated Hermes YAML configuration.

    Only the ``terminal`` and ``file`` native toolsets remain model-visible.
    The explicit disabled list is defense in depth against platform defaults or
    future composite-toolset expansion.
    """
    endpoint = _validated_endpoint(endpoint)
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
        raise HermesIsolationError("max_turns must be a positive integer")
    disabled = "\n".join("      - " + item for item in _DISABLED_TOOLSETS)
    return f"""# Generated by Collie's normalized harness benchmark. Do not merge into a user profile.
model:
  default: {_yaml_string(MODEL)}
  provider: {_yaml_string(PROVIDER_ID)}
  api_mode: chat_completions
providers:
  {PROVIDER_NAME}:
    api: {_yaml_string(endpoint)}
    transport: chat_completions
    default_model: {_yaml_string(MODEL)}
    api_key: {_yaml_string(API_KEY_SENTINEL)}
    discover_models: false
fallback_providers: []
platform_toolsets:
  cli: [terminal, file]
agent:
  max_turns: {max_turns}
  disabled_toolsets:
{disabled}
memory:
  memory_enabled: false
  user_profile_enabled: false
  nudge_interval: 0
  flush_min_turns: 0
skills:
  creation_nudge_interval: 0
  external_dirs: []
mcp_servers: {{}}
delegation:
  orchestrator_enabled: false
  inherit_mcp_toolsets: false
  max_spawn_depth: 1
  max_concurrent_children: 1
compression:
  enabled: false
streaming:
  enabled: false
telemetry:
  shared_metrics:
    enabled: false
hooks: {{}}
hooks_auto_accept: false
terminal:
  backend: local
  cwd: "."
  home_mode: profile
  docker_forward_env: []
  timeout: 180
  lifetime_seconds: 300
kanban:
  review_dispatch: false
updates:
  pre_update_backup: false
"""


def _ensure_fresh_home(home: Path) -> None:
    if home.exists():
        if not home.is_dir():
            raise HermesIsolationError("HERMES_HOME exists and is not a directory")
        try:
            next(home.iterdir())
        except StopIteration:
            pass
        else:
            raise HermesIsolationError("HERMES_HOME must be absent or empty")
    home.mkdir(parents=True, exist_ok=True)
    for name in ("logs", "memories", "plugins", "sessions", "skills", "user-home"):
        (home / name).mkdir()
    (home / ".env").write_text("", encoding="utf-8", newline="\n")


def _sanitized_env(source: Mapping[str, str], home: Path) -> dict[str, str]:
    env = {
        key: value for key, value in source.items()
        if key.upper() in _SAFE_ENV_KEYS and isinstance(value, str)
    }
    isolated_user_home = str((home / "user-home").resolve())
    env.update({
        "CI": "1",
        "DO_NOT_TRACK": "1",
        "HERMES_DISABLE_TELEMETRY": "1",
        "HERMES_HOME": str(home.resolve()),
        "HERMES_INFERENCE_MODEL": MODEL,
        "HERMES_INFERENCE_PROVIDER": PROVIDER_ID,
        "HOME": isolated_user_home,
        "NO_COLOR": "1",
        "OTEL_SDK_DISABLED": "true",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "TERM": "dumb",
        "USERPROFILE": isolated_user_home,
        "XDG_CACHE_HOME": str((home / "user-home" / ".cache").resolve()),
        "XDG_CONFIG_HOME": str((home / "user-home" / ".config").resolve()),
        "XDG_DATA_HOME": str((home / "user-home" / ".local" / "share").resolve()),
    })
    # This assertion protects future edits to the allowlist: a credential-like
    # ambient variable must never accidentally cross into Hermes.
    leaked = sorted(key for key in env if _SENSITIVE_NAME.search(key))
    permitted = {"HERMES_DISABLE_TELEMETRY", "OTEL_SDK_DISABLED"}
    leaked = [key for key in leaked if key not in permitted]
    if leaked:
        raise HermesIsolationError("sensitive environment variable survived: " + ", ".join(leaked))
    return env


def build_hermes_launch(
    home: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
    endpoint: str,
    prompt: str,
    *,
    hermes_executable: str = "hermes",
    max_turns: int = 30,
    source_env: Mapping[str, str] | None = None,
) -> HermesLaunch:
    """Create a fresh profile and return the exact non-interactive launch."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise HermesIsolationError("prompt must be a non-empty string")
    if "\x00" in prompt:
        raise HermesIsolationError("prompt contains NUL")
    workspace_path = Path(workspace).resolve()
    if not workspace_path.is_dir():
        raise HermesIsolationError("workspace must be an existing directory")
    home_path = Path(home).resolve()
    if home_path == workspace_path or home_path in workspace_path.parents:
        raise HermesIsolationError("HERMES_HOME must not contain the benchmark workspace")
    _ensure_fresh_home(home_path)
    endpoint = _validated_endpoint(endpoint)
    config = render_hermes_config(endpoint, max_turns=max_turns)
    config_path = home_path / "config.yaml"
    config_path.write_text(config, encoding="utf-8", newline="\n")
    env = _sanitized_env(os.environ if source_env is None else source_env, home_path)
    argv = (
        str(hermes_executable), "-z", prompt,
        "--provider", PROVIDER_ID,
        "--model", MODEL,
        "--toolsets", "terminal,file",
        "--ignore-rules",
    )
    digest = hashlib.sha256(config.encode("utf-8")).hexdigest()
    return HermesLaunch(
        home=home_path,
        config_path=config_path,
        workspace=workspace_path,
        endpoint=endpoint,
        argv=argv,
        env=env,
        config_sha256=digest,
    )


def parse_hermes_output(stdout: str, stderr: str = "", returncode: int = 0) -> HermesProcessOutput:
    """Parse Hermes quiet single-query output without retaining its prompt."""
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise TypeError("stdout and stderr must be strings")
    if len(stdout.encode("utf-8")) > _MAX_OUTPUT_BYTES or len(stderr.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        raise HermesTraceError("Hermes process output exceeded the safety limit")
    errors: list[str] = []
    if "\x00" in stdout or "\x00" in stderr:
        errors.append("process_output_contains_nul")
    final_text = stdout.strip()
    if int(returncode) != 0:
        errors.append("hermes_nonzero_exit")
    if not final_text:
        errors.append("hermes_final_output_missing")
    lower_stderr = stderr.lower()
    if "traceback (most recent call last)" in lower_stderr:
        errors.append("hermes_traceback")
    return HermesProcessOutput(
        ok=not errors,
        final_text=final_text,
        returncode=int(returncode),
        stderr=stderr.strip(),
        errors=tuple(errors),
    )


def _json_object(line: str, line_number: int) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise HermesTraceError(f"non-finite number on trace line {line_number}: {value}")
    try:
        value = json.loads(line, parse_constant=reject_constant)
    except HermesTraceError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HermesTraceError(f"invalid JSON on trace line {line_number}") from exc
    if not isinstance(value, dict):
        raise HermesTraceError(f"trace line {line_number} is not an object")
    return value


def _event_name(event: Mapping[str, Any]) -> str:
    raw = str(event.get("event") or event.get("type") or "").strip().lower().replace("-", "_")
    return _EVENT_ALIASES.get(raw, raw)


def _nonnegative_int(value: Any, label: str, errors: list[str]) -> int:
    if isinstance(value, bool):
        errors.append(label + "_invalid")
        return 0
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        errors.append(label + "_invalid")
        return 0
    if number < 0:
        errors.append(label + "_invalid")
        return 0
    return number


def _foreign_surface_empty(value: Any) -> bool:
    if value in (None, "", [], {}):
        return True
    if not isinstance(value, Mapping):
        return False
    return all(item in (None, "", [], {}) for item in value.values())


def parse_hermes_trace(trace: str, *, expected_model: str = MODEL) -> HermesTraceSummary:
    """Parse and independently admit one normalized Hermes JSONL trace."""
    if not isinstance(trace, str):
        raise TypeError("trace must be a string")
    raw = trace.encode("utf-8")
    if len(raw) > _MAX_TRACE_BYTES:
        raise HermesTraceError("Hermes trace exceeded the safety limit")
    if "\x00" in trace:
        raise HermesTraceError("Hermes trace contains NUL")
    events = [_json_object(line, i) for i, line in enumerate(trace.splitlines(), 1) if line.strip()]
    if not events:
        raise HermesTraceError("Hermes trace is empty")

    errors: list[str] = []
    starts = 0
    finals = 0
    terminals = 0
    final_text = ""
    terminal_status = ""
    terminal_exit_code: int | None = None
    requests: dict[str, dict[str, Any]] = {}
    responses: set[str] = set()
    tool_calls: dict[str, str] = {}
    tool_results: set[str] = set()
    successful_native_tools: list[str] = []
    edit_call_ids: set[str] = set()
    native_edit_calls = 0
    usage = {"input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    for event in events:
        name = _event_name(event)
        if name == "run_start":
            starts += 1
            if event.get("schema") not in (None, TRACE_SCHEMA):
                errors.append("trace_schema_mismatch")
            if str(event.get("model") or expected_model) != expected_model:
                errors.append("run_model_mismatch")
        elif name == "sdk_request":
            request_id = str(event.get("request_id") or "").strip()
            if not request_id or request_id in requests:
                errors.append("sdk_request_id_invalid")
                continue
            if str(event.get("model") or "") != expected_model:
                errors.append("sdk_request_model_mismatch")
            route = str(event.get("route") or "claude_agent_sdk_subscription").strip()
            if route != "claude_agent_sdk_subscription":
                errors.append("sdk_request_rerouted")
            requests[request_id] = dict(event)
        elif name == "sdk_response":
            request_id = str(event.get("request_id") or "").strip()
            if request_id not in requests or request_id in responses:
                errors.append("sdk_response_unpaired")
                continue
            responses.add(request_id)
            if str(event.get("model") or "") != expected_model:
                errors.append("sdk_response_model_mismatch")
            source = str(event.get("api_key_source") or "").strip().lower().replace("_", "-")
            if source != "none":
                errors.append("sdk_api_key_source_invalid")
            if not _foreign_surface_empty(event.get("foreign_surfaces")):
                errors.append("sdk_foreign_surface_present")
            response_usage = event.get("usage") or {}
            if not isinstance(response_usage, Mapping):
                errors.append("sdk_usage_invalid")
                response_usage = {}
            for key in usage:
                usage[key] += _nonnegative_int(response_usage.get(key, 0), "sdk_usage_" + key, errors)
        elif name == "tool_call":
            call_id = str(event.get("call_id") or event.get("tool_call_id") or "").strip()
            tool = str(event.get("name") or event.get("tool") or "").strip()
            if not call_id or not tool or call_id in tool_calls:
                errors.append("tool_call_invalid")
                continue
            tool_calls[call_id] = tool
            if tool not in _NATIVE_TOOLS:
                errors.append("foreign_or_disabled_tool:" + tool)
            if (tool in {"write_file", "patch"}
                    or event.get("workspace_write") is True
                    or event.get("mutation") == "workspace"):
                edit_call_ids.add(call_id)
        elif name == "tool_result":
            call_id = str(event.get("call_id") or event.get("tool_call_id") or "").strip()
            if call_id not in tool_calls or call_id in tool_results:
                errors.append("tool_result_unpaired")
                continue
            tool_results.add(call_id)
            success = event.get("success") is True or (
                event.get("success") is None and event.get("is_error") is False)
            if success and tool_calls[call_id] in _NATIVE_TOOLS:
                successful_native_tools.append(tool_calls[call_id])
                if (call_id in edit_call_ids
                        or event.get("workspace_write") is True
                        or event.get("mutation") == "workspace"):
                    native_edit_calls += 1
        elif name == "final":
            finals += 1
            text = event.get("text") if "text" in event else event.get("content")
            if not isinstance(text, str) or not text.strip():
                errors.append("trace_final_text_invalid")
            else:
                final_text = text.strip()
        elif name == "run_end":
            terminals += 1
            terminal_status = str(event.get("status") or "").strip().lower()
            try:
                terminal_exit_code = int(event.get("exit_code"))
            except (TypeError, ValueError, OverflowError):
                errors.append("terminal_exit_code_invalid")
                terminal_exit_code = None
            if terminal_status not in {"completed", "success", "succeeded"}:
                errors.append("terminal_status_invalid")
            if terminal_exit_code != 0:
                errors.append("terminal_exit_nonzero")
        elif name in {"usage", "log", "status"}:
            # Informational aggregates/logs are accepted but never trusted for
            # billing or physical-call counts; sdk_response is authoritative.
            continue
        elif name in {"error", "fatal"}:
            errors.append("trace_reported_error")
        else:
            errors.append("unknown_trace_event:" + (name or "missing"))

    if starts != 1:
        errors.append("run_start_count_invalid")
    if finals != 1:
        errors.append("final_event_count_invalid")
    if terminals != 1:
        errors.append("run_end_count_invalid")
    if not requests:
        errors.append("sdk_request_missing")
    if set(requests) != responses:
        errors.append("sdk_request_response_mismatch")
    if set(tool_calls) != tool_results:
        errors.append("tool_call_result_mismatch")
    deduped_errors = tuple(dict.fromkeys(errors))
    return HermesTraceSummary(
        admitted=not deduped_errors,
        terminal_status=terminal_status,
        terminal_exit_code=terminal_exit_code,
        final_text=final_text,
        physical_requests=len(requests),
        completed_requests=len(responses),
        tool_calls=len(tool_calls),
        completed_tools=len(tool_results),
        native_edit_calls=native_edit_calls,
        native_tool_names=tuple(successful_native_tools),
        local_tool_observed=bool(successful_native_tools),
        usage=usage,
        trace_sha256=hashlib.sha256(raw).hexdigest(),
        errors=deduped_errors,
    )


def _tool_call_name_and_id(value: Mapping[str, Any]) -> tuple[str, str]:
    function = value.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
    else:
        name = value.get("name") or value.get("tool_name")
    return str(name or "").strip(), str(
        value.get("id") or value.get("tool_call_id") or "").strip()


def read_hermes_state(home: str | os.PathLike[str]) -> HermesStateSummary:
    """Read only non-content native execution evidence from fresh state.db."""
    db_path = Path(home).resolve() / "state.db"
    if not db_path.is_file() or db_path.stat().st_size > 128 * 1024 * 1024:
        raise HermesTraceError("Hermes state database is missing or oversized")
    # mode=ro sees committed WAL content while preventing adapter writes.
    uri = db_path.as_uri() + "?mode=ro"
    errors: list[str] = []
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        sessions = connection.execute(
            "SELECT id, model, tool_call_count, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, api_call_count "
            "FROM sessions ORDER BY started_at DESC"
        ).fetchall()
        if len(sessions) != 1:
            raise HermesTraceError("fresh Hermes profile must contain exactly one session")
        session = sessions[0]
        rows = connection.execute(
            "SELECT role, tool_call_id, tool_calls, tool_name FROM messages "
            "WHERE session_id = ? ORDER BY id", (session["id"],)
        ).fetchall()
    except (sqlite3.DatabaseError, KeyError, TypeError) as exc:
        raise HermesTraceError("Hermes state database schema is invalid") from exc
    finally:
        if "connection" in locals():
            connection.close()

    calls: dict[str, str] = {}
    for row in rows:
        raw_calls = row["tool_calls"]
        if raw_calls in (None, ""):
            continue
        if not isinstance(raw_calls, str) or len(raw_calls.encode("utf-8")) > _MAX_TRACE_BYTES:
            errors.append("state_tool_calls_invalid")
            continue
        try:
            decoded = json.loads(raw_calls)
        except (json.JSONDecodeError, TypeError, ValueError):
            errors.append("state_tool_calls_invalid")
            continue
        values = decoded if isinstance(decoded, list) else [decoded]
        for value in values:
            if not isinstance(value, Mapping):
                errors.append("state_tool_call_invalid")
                continue
            name, call_id = _tool_call_name_and_id(value)
            if not name or name not in _NATIVE_TOOLS:
                errors.append("foreign_or_disabled_tool:" + (name or "missing"))
                continue
            if call_id:
                if call_id in calls:
                    errors.append("state_tool_call_id_duplicate")
                calls[call_id] = name
            else:
                errors.append("state_tool_call_id_missing")

    result_ids = {
        str(row["tool_call_id"]).strip() for row in rows
        if row["tool_call_id"] not in (None, "")
    }
    missing_results = set(calls) - result_ids
    if missing_results:
        errors.append("state_tool_results_missing")
    for row in rows:
        result_id = str(row["tool_call_id"] or "").strip()
        result_name = str(row["tool_name"] or "").strip()
        if result_id in calls and result_name and result_name != calls[result_id]:
            errors.append("state_tool_result_name_mismatch")
    parsed_count = len(calls)
    stored_count = _nonnegative_int(session["tool_call_count"], "state_tool_count", errors)
    if stored_count != parsed_count:
        errors.append("state_tool_count_mismatch")
    if str(session["model"] or "").strip() != MODEL:
        errors.append("state_model_mismatch")
    api_calls = _nonnegative_int(session["api_call_count"], "state_api_calls", errors)
    if api_calls < 1:
        errors.append("state_api_calls_missing")
    names = tuple(calls.values())
    native_edit_calls = sum(name in {"write_file", "patch"} for name in names)
    usage = {
        "input_tokens": _nonnegative_int(session["input_tokens"], "state_input_tokens", errors),
        "output_tokens": _nonnegative_int(session["output_tokens"], "state_output_tokens", errors),
        "cache_read_input_tokens": _nonnegative_int(
            session["cache_read_tokens"], "state_cache_read_tokens", errors),
        "cache_creation_input_tokens": _nonnegative_int(
            session["cache_write_tokens"], "state_cache_write_tokens", errors),
    }
    deduped = tuple(dict.fromkeys(errors))
    return HermesStateSummary(
        admitted=not deduped,
        physical_requests=api_calls,
        tool_calls=parsed_count,
        completed_tools=len(set(calls) & result_ids),
        native_edit_calls=native_edit_calls,
        native_tool_names=names,
        usage=usage,
        errors=deduped,
    )


def run_hermes(
    home: str | os.PathLike[str],
    workspace: str | os.PathLike[str],
    endpoint: str,
    prompt: str,
    *,
    hermes_executable: str = "hermes",
    max_turns: int = 30,
    wall_seconds: int = 900,
    source_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run isolated Hermes and return a redacted, worker-stable receipt.

    Native execution evidence is read from the fresh profile's SQLite state
    after exit. Neither message content, the prompt, nor raw process output is
    selected from the database, returned, or written by this adapter.
    """
    started = time.monotonic()
    if (not isinstance(wall_seconds, int) or isinstance(wall_seconds, bool)
            or wall_seconds < 1):
        return _run_receipt(
            started=started, returncode=None,
            error_category="hermes_wall_limit_invalid")
    try:
        workspace_path = Path(workspace).resolve()
        launch = build_hermes_launch(
            home, workspace_path, endpoint, prompt,
            hermes_executable=hermes_executable,
            max_turns=max_turns,
            source_env=source_env,
        )
    except (HermesIsolationError, OSError, ValueError):
        return _run_receipt(
            started=started, returncode=None,
            error_category="hermes_isolation_failed")

    try:
        completed = subprocess.run(
            list(launch.argv),
            cwd=str(workspace_path),
            env=launch.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=wall_seconds,
            check=False,
        )
    except FileNotFoundError:
        return _run_receipt(
            started=started, returncode=None,
            error_category="hermes_executable_missing",
            config_sha256=launch.config_sha256)
    except subprocess.TimeoutExpired:
        return _run_receipt(
            started=started, returncode=None,
            error_category="hermes_wall_timeout",
            config_sha256=launch.config_sha256)
    except OSError:
        return _run_receipt(
            started=started, returncode=None,
            error_category="hermes_launch_failed",
            config_sha256=launch.config_sha256)

    try:
        output = parse_hermes_output(
            completed.stdout or "", completed.stderr or "", completed.returncode)
    except (HermesTraceError, TypeError, ValueError):
        return _run_receipt(
            started=started, returncode=completed.returncode,
            error_category="hermes_process_output_invalid",
            config_sha256=launch.config_sha256)
    if not output.ok:
        category = output.errors[0] if output.errors else "hermes_process_failed"
        return _run_receipt(
            started=started, returncode=completed.returncode,
            error_category=category,
            config_sha256=launch.config_sha256)

    try:
        state = read_hermes_state(launch.home)
    except (HermesTraceError, OSError, UnicodeError):
        return _run_receipt(
            started=started, returncode=completed.returncode,
            error_category="hermes_native_state_invalid",
            config_sha256=launch.config_sha256)
    error_category = "" if state.admitted else "hermes_native_state_not_admitted"
    return _run_receipt(
        started=started,
        returncode=completed.returncode,
        error_category=error_category,
        state=state,
        config_sha256=launch.config_sha256,
    )


__all__ = [
    "API_KEY_SENTINEL", "HERMES_VERSION", "HermesIsolationError", "HermesLaunch",
    "HermesProcessOutput", "HermesStateSummary", "HermesTraceError",
    "HermesTraceSummary", "MODEL",
    "PROVIDER_ID", "TRACE_SCHEMA", "build_hermes_launch", "parse_hermes_output",
    "parse_hermes_trace", "read_hermes_state", "render_hermes_config", "run_hermes",
]
