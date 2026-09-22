"""Private process boundary for the optional official Codex Python SDK.

This module is not a user-facing CLI.  ``CodexSdkRunner`` starts it with a
Collie-sanitized complete environment and sends exactly one JSON request on
stdin.  Keeping the SDK in this child is important: ``CodexConfig.env`` extends
the SDK process environment; it does not remove ambient API keys or endpoint
overrides from the parent process.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any


_OVERRIDES = (
    "mcp_servers={}",
    "plugins={}",
    'web_search="disabled"',
    "project_doc_max_bytes=0",
    "features.hooks=false",
    "features.memories=false",
    "features.multi_agent=false",
    "features.apps=false",
)

# Codex downgrades an explicitly requested workspace-write sandbox to read-only
# on Windows whenever no Windows sandbox level is configured
# (``config/src/config_toml.rs`` -> ``derive_permission_profile``: the
# ``windows_sandbox_level == Disabled`` branch rewrites WorkspaceWrite to
# ReadOnly).  The level defaults to ``Disabled`` when ``[windows] sandbox`` is
# absent (``core/src/config/mod.rs``), so without these two overrides the turn
# silently writes nothing.  They mirror ``_windows_sandbox_override()`` in
# ``agent_runners``; see its docstring for why ``unelevated`` and a shared
# desktop are the only choices Collie can make on the user's behalf.
_WINDOWS_OVERRIDES = (
    'windows.sandbox="unelevated"',
    "windows.sandbox_private_desktop=false",
)


def _overrides() -> tuple[str, ...]:
    return _OVERRIDES + (_WINDOWS_OVERRIDES if os.name == "nt" else ())


def _decline_approval(_method: str, _params: Any) -> dict[str, Any]:
    """Never approve anything from inside the sidecar.

    ``ApprovalMode.deny_all`` already asks the server not to escalate, but the
    SDK's own fallback handler (``client.py`` ->
    ``CodexClient._default_approval_handler``) answers command-execution and
    file-change approvals with ``{"decision": "accept"}``, and ``Codex()``
    exposes no constructor argument for replacing it.  Collie's approval
    channel is the interactive App Server adapter, so this sidecar declines.
    """
    return {"decision": "decline"}


def _pin_declining_approval_handler(codex: Any) -> None:
    client = getattr(codex, "_client", None)
    if client is None or not callable(getattr(client, "_approval_handler", None)):
        raise RuntimeError(
            "this openai-codex build does not expose the approval handler this "
            "sidecar must replace; Collie will not run it with an unknown "
            "approval policy")
    client._approval_handler = _decline_approval


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                allow_nan=False) + "\n")
    sys.stdout.flush()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    fields = getattr(value, "__dict__", None)
    if isinstance(fields, dict):
        return {str(key): _jsonable(item) for key, item in fields.items()
                if not str(key).startswith("_")}
    return str(value)


def _request() -> dict[str, Any]:
    try:
        value = json.loads(sys.stdin.read(), parse_constant=lambda raw: (_ for _ in ()).throw(
            ValueError("non-finite JSON number: " + raw)))
    except Exception as exc:
        raise ValueError("invalid worker request: %s" % exc) from exc
    if not isinstance(value, dict):
        raise ValueError("worker request must be an object")
    return value


def _input(value: dict[str, Any], workspace: str) -> Any:
    from openai_codex import ImageInput, LocalImageInput, TextInput

    text = str(value.get("text") or "")
    if not text.strip() or "\x00" in text:
        raise ValueError("turn text must be non-empty")
    items: list[Any] = [TextInput(text=text)]
    for url in value.get("image_urls") or ():
        url = str(url)
        if not url.startswith("data:image/"):
            raise ValueError("Codex SDK image URLs must be data:image URLs")
        items.append(ImageInput(url=url))
    for path in value.get("image_files") or ():
        path = os.path.realpath(os.path.abspath(str(path)))
        try:
            inside = os.path.commonpath((workspace, path)) == workspace
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(path):
            raise ValueError("local image must exist inside the workspace")
        items.append(LocalImageInput(path=path))
    return items[0] if len(items) == 1 else items


def _thread(codex: Any, request: dict[str, Any], workspace: str) -> Any:
    from openai_codex import ApprovalMode, Sandbox

    thread_id = str(request.get("thread_id") or "")
    action = str(request.get("action") or "run")
    kwargs = {
        "approval_mode": ApprovalMode.deny_all,
        "cwd": workspace,
        "sandbox": Sandbox.workspace_write,
    }
    model = str(request.get("model") or "")
    if model:
        kwargs["model"] = model
    if action == "fork":
        if not thread_id:
            raise ValueError("fork requires thread_id")
        return codex.thread_fork(thread_id, **kwargs)
    if thread_id:
        return codex.thread_resume(thread_id, **kwargs)
    return codex.thread_start(**kwargs)


def main() -> int:
    try:
        from openai_codex import Codex, CodexConfig

        request = _request()
        workspace = os.path.realpath(os.path.abspath(str(request.get("workspace") or "")))
        if not os.path.isdir(workspace):
            raise ValueError("workspace is not a directory")
        action = str(request.get("action") or "run")
        if action not in ("run", "fork", "compact"):
            raise ValueError("unsupported worker action: %s" % action)
        # Use the runtime shipped by the pinned official SDK distribution.
        # It has its own versioned cli-bin dependency and needs no PATH CLI.
        config = CodexConfig(cwd=workspace, config_overrides=_overrides(),
                             client_name="collie", client_title="Collie")
        with Codex(config=config) as codex:
            _pin_declining_approval_handler(codex)
            thread = _thread(codex, request, workspace)
            thread_id = str(thread.id)
            if action == "fork":
                _emit({"type": "session.forked", "thread_id": thread_id})
                _emit({"type": "collie.sdk.result", "action": action,
                       "thread_id": thread_id, "status": "completed"})
                return 0
            if action == "compact":
                result = thread.compact()
                _emit({"type": "context.compacted", "thread_id": thread_id,
                       "result": _jsonable(result)})
                _emit({"type": "collie.sdk.result", "action": action,
                       "thread_id": thread_id, "status": "completed"})
                return 0

            _emit({"type": ("session.resumed" if request.get("thread_id") else
                             "session.started"), "thread_id": thread_id})
            _emit({"type": "turn.started", "thread_id": thread_id})
            try:
                result = thread.run(_input(request.get("input") or {}, workspace))
            except Exception as exc:
                # ``Thread.run`` does not return a failed turn: the SDK's
                # collector raises for it (``_run.py`` ->
                # ``_raise_for_failed_turn``), and a dropped transport raises
                # too.  The thread exists and is resumable either way, so the
                # sidecar still ends with its one terminal record instead of
                # letting the parent read a truncated stream as a protocol
                # fault and forget the thread id.
                detail = "%s: %s" % (type(exc).__name__, exc)
                _emit({"type": "turn.failed", "thread_id": thread_id,
                       "status": "failed", "error": detail})
                _emit({"type": "collie.sdk.result", "action": action,
                       "thread_id": thread_id, "status": "failed",
                       "error": detail})
                return 1
            for item in list(getattr(result, "items", ()) or ()):
                _emit({"type": "item.completed", "item": _jsonable(item)})
            usage = _jsonable(getattr(result, "usage", None))
            if usage:
                _emit({"type": "usage.updated", "usage": usage})
            status = _jsonable(getattr(result, "status", "")) or "unknown"
            error = _jsonable(getattr(result, "error", None))
            final = str(getattr(result, "final_response", None) or "")
            _emit({"type": "turn.completed" if status == "completed" else "turn.failed",
                   "thread_id": thread_id, "status": status, "error": error})
            _emit({"type": "collie.sdk.result", "action": action,
                   "thread_id": thread_id, "status": status, "error": error,
                   "final_output": final, "usage": usage})
            return 0 if status == "completed" else 1
    except Exception as exc:
        _emit({"type": "collie.sdk.error", "error": "%s: %s" %
              (type(exc).__name__, exc)})
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
