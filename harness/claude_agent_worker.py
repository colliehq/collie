"""Private subprocess entry point for :mod:`harness.claude_agent_sdk`.

Imports the optional dependency only inside the worker, so importing Collie's
stdlib-only provider registry never requires the Claude Agent SDK.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys


_SDK_ENV = {
    "CLAUDE_CODE_MAX_RETRIES": "0",
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
}

_PARENT_DEATH_FD_FLAG = "--collie-parent-death-fd"
_PARENT_PID_FLAG = "--collie-parent-pid"
_EXTERNAL_OWNER_FLAG = "--collie-external-process-owner"


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
    return sdk.ClaudeAgentOptions(**kwargs)


def _is_empty(value) -> bool:
    return value in (None, [], {}, "")


def _validate_init(data, expected_model: str) -> str:
    if not isinstance(data, dict):
        raise RuntimeError("SDK init payload is not an object")
    for key in ("tools", "skills", "plugins", "agents", "slash_commands",
                "mcp_servers"):
        if key not in data:
            raise RuntimeError("SDK init did not attest an empty %s surface" % key)
        if not _is_empty(data.get(key)):
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


def _usage_dict(value) -> dict:
    value = value if isinstance(value, dict) else {}
    return {
        "input_tokens": int(value.get("input_tokens", 0) or 0),
        "output_tokens": int(value.get("output_tokens", 0) or 0),
        "cache_read_input_tokens": int(value.get("cache_read_input_tokens", 0) or 0),
        "cache_creation_input_tokens": int(value.get("cache_creation_input_tokens", 0) or 0),
    }


async def _query(request: dict, sdk) -> dict:
    options = _build_options(sdk, request)
    init_seen = False
    api_key_source = ""
    assistant_id = ""
    assistant_seen = False
    assistant_text = ""
    usage = {}
    result_seen = False

    async for message in sdk.query(prompt=request["prompt"], options=options):
        kind = _message_kind(message)
        if kind == "system" and str(_field(message, "subtype", "")).lower() == "init":
            if init_seen:
                raise RuntimeError("SDK emitted more than one init message")
            if assistant_seen or result_seen:
                raise RuntimeError("SDK emitted init after response content")
            api_key_source = _validate_init(
                _field(message, "data", {}), request["model"])
            init_seen = True
        elif kind == "assistant":
            if not init_seen:
                raise RuntimeError("SDK emitted assistant content before validated init")
            if result_seen:
                raise RuntimeError("SDK emitted assistant content after result")
            if _field(message, "error"):
                raise RuntimeError("SDK assistant reported an error")
            message_id = (_field(message, "id") or _field(message, "message_id")
                          or _field(message, "uuid"))
            if not isinstance(message_id, str) or not message_id.strip():
                raise RuntimeError("SDK Assistant message is missing an id")
            message_id = message_id.strip()
            # The SDK emits thinking and text as separate AssistantMessage
            # fragments with one shared Anthropic message_id.  That is still
            # one model answer. A second distinct message id would be another
            # assistant turn and must fail the one-request/one-turn contract.
            if assistant_id and message_id != assistant_id:
                raise RuntimeError(
                    "SDK emitted more than one Assistant message id")
            assistant_id = message_id
            assistant_seen = True
            assistant_text += _assistant_text(message)
        elif kind == "result":
            if not init_seen:
                raise RuntimeError("SDK emitted result before validated init")
            if not assistant_seen:
                raise RuntimeError("SDK emitted result before Assistant message")
            if result_seen:
                raise RuntimeError("SDK emitted more than one result message")
            result_seen = True
            if _field(message, "is_error", False):
                raise RuntimeError("SDK result reported an error")
            turns = int(_field(message, "num_turns", 0) or 0)
            if turns > 1:
                raise RuntimeError("SDK exceeded the one-turn limit")
            usage = _field(message, "usage", {}) or {}

    if not init_seen:
        raise RuntimeError("SDK did not emit a validated init message")
    if not result_seen:
        raise RuntimeError("SDK did not emit a result message")
    if not assistant_seen or not assistant_id:
        raise RuntimeError("SDK did not emit exactly one Assistant message id")
    return {"ok": True, "text": assistant_text, "usage": _usage_dict(usage),
            "api_key_source": api_key_source}


def _read_request() -> dict:
    raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise RuntimeError("worker request exceeded the safety limit")
    request = json.loads(raw.decode("utf-8"))
    if not isinstance(request, dict) or request.get("protocol") != 1:
        raise RuntimeError("invalid worker protocol")
    for key in ("model", "system_prompt", "prompt"):
        if not isinstance(request.get(key), str):
            raise RuntimeError("worker request is missing %s" % key)
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
    except Exception as exc:
        # Parent applies Collie's secret redactor before surfacing this bounded text.
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:1000])}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
