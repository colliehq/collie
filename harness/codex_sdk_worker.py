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
        config = CodexConfig(cwd=workspace, config_overrides=_OVERRIDES,
                             client_name="collie", client_title="Collie")
        with Codex(config=config) as codex:
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
            result = thread.run(_input(request.get("input") or {}, workspace))
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
