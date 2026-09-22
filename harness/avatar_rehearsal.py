"""Disclosed avatar rehearsal for a Live Copilot session.

The local simulation works without an account. A real avatar is supplied by a narrow external MCP
connection, so Collie's core owns the rehearsal UX and policy but no vendor API or credential.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse

from . import live_copilot, mcpclient


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
_MCP_SERVER = str(os.environ.get("COLLIE_AVATAR_MCP_SERVER") or "avatar").strip()


class AvatarRehearsalError(live_copilot.LiveCopilotError):
    pass


def capabilities() -> dict:
    configured = (mcpclient.server_has_tool(_MCP_SERVER, "avatar_start") and
                  mcpclient.server_has_tool(_MCP_SERVER, "avatar_stop"))
    return {
        "available": True,
        "configured": configured,
        "mode": "mcp" if configured else "simulation",
        "provider": "mcp" if configured else "local",
        "mcp_server": _MCP_SERVER if configured else "",
        "requires_server_key": False,
        "disclosure_required": True,
        "max_live_seconds": 600,
    }


def _rehearsal_script(snapshot: dict, scenario="") -> str:
    context = live_copilot._text(scenario or snapshot.get("context"), 700)
    summary = live_copilot._text(snapshot.get("summary"), 900)
    notes = [live_copilot._text(row.get("text"), 300)
             for row in (snapshot.get("notes") or [])[-4:]
             if live_copilot._text(row.get("text"), 300)]
    lines = [
        "你好，我是 Collie 的 AI 排练化身，这不是一场真实招聘面试。",
        "我可以根据当前 Live Session 的上下文练习讲解，并让你随时打断、追问或纠正。",
    ]
    if context:
        lines.append("本次排练场景是：" + context)
    if summary:
        lines.append("当前理解是：" + summary)
    if notes:
        lines.append("设计中的关键点包括：" + "；".join(notes))
    lines.append("正式讲解时，我会先确认需求，再讲主链路、数据模型、扩展策略和权衡。")
    return live_copilot._text(" ".join(lines), 2_600)


def _mcp_payload(result: dict) -> dict:
    if not isinstance(result, dict):
        raise AvatarRehearsalError("avatar MCP returned an invalid response")
    structured = result.get("structuredContent") or result.get("structured_content")
    if not isinstance(structured, dict):
        blocks = result.get("content") or []
        text = next((row.get("text") for row in blocks
                     if isinstance(row, dict) and row.get("type") == "text"), "")
        try:
            structured = json.loads(text) if text else {}
        except ValueError:
            structured = {}
    if result.get("isError"):
        raise AvatarRehearsalError(
            live_copilot._text(structured.get("error") or "avatar MCP call failed", 500))
    if not isinstance(structured, dict):
        raise AvatarRehearsalError("avatar MCP returned no structured result")
    return structured


def _join_url(url: str, allowed_hosts) -> str:
    parsed = urllib.parse.urlparse(str(url or ""))
    host = (parsed.hostname or "").casefold()
    allowed = [str(value or "").strip(".").casefold() for value in (allowed_hosts or [])]
    safe_host = any(host == value or host.endswith("." + value) for value in allowed if value)
    if parsed.scheme != "https" or not safe_host:
        raise AvatarRehearsalError("avatar MCP returned an unsafe conversation URL")
    return urllib.parse.urlunparse(parsed)


class AvatarRehearsalService:
    def __init__(self, root=None, mcp_call=None, allowed_join_hosts=None):
        self.store = live_copilot.LiveSessionStore(root)
        self.mcp_call = mcp_call or mcpclient.call_server_tool
        cfg = mcpclient.server_configuration(_MCP_SERVER)
        self.allowed_join_hosts = (allowed_join_hosts if allowed_join_hosts is not None else
                                   cfg.get("allowed_join_hosts") or [])

    def start(self, *, scenario="") -> dict:
        snapshot = self.store.snapshot()
        if not snapshot.get("active"):
            raise AvatarRehearsalError("start a Live Copilot session before avatar rehearsal")
        if (snapshot.get("avatar") or {}).get("active"):
            raise AvatarRehearsalError("an avatar rehearsal is already active")
        script = _rehearsal_script(snapshot, scenario)
        if not capabilities()["configured"]:
            avatar = self.store.set_avatar({
                "mode": "simulation", "provider": "local", "script": script,
                "started_at_ms": live_copilot._now_ms(), "conversation_id": "",
            })
            self.store.add_event(source="system", kind="avatar", app="collie",
                                 text="Disclosed AI avatar rehearsal started in local simulation mode.")
            return {"avatar": avatar, "simulation": True, "script": script,
                    "configured": False}

        try:
            result = _mcp_payload(self.mcp_call(
                _MCP_SERVER, "avatar_start",
                {"script": script, "scenario": scenario, "max_live_seconds": 600},
                timeout=30))
        except AvatarRehearsalError:
            raise
        except Exception as exc:
            raise AvatarRehearsalError(
                "avatar MCP is unavailable: %s" % live_copilot._text(exc, 400)) from exc
        conversation_id = str(result.get("conversation_id") or "")
        if not _SAFE_ID.fullmatch(conversation_id):
            raise AvatarRehearsalError("avatar MCP returned an invalid conversation id")
        join_url = _join_url(result.get("join_url"), self.allowed_join_hosts)
        provider = live_copilot._text(result.get("provider") or "external", 80).casefold()
        provider = provider if re.fullmatch(r"[a-z0-9_-]{2,80}", provider) else "external"
        avatar = self.store.set_avatar({
            "mode": "mcp", "provider": provider, "script": script,
            "started_at_ms": live_copilot._now_ms(), "conversation_id": conversation_id,
            "conversation_url": urllib.parse.urlunparse(
                urllib.parse.urlparse(join_url)._replace(query="")),
        })
        self.store.add_event(source="system", kind="avatar", app="collie",
                             text="Disclosed AI avatar rehearsal started through an MCP service.")
        return {"avatar": avatar, "simulation": False, "configured": True,
                "join_url": join_url, "script": script}

    def stop(self, *, reason="user_requested") -> dict:
        snapshot = self.store.snapshot()
        avatar = dict(snapshot.get("avatar") or {})
        remote_error = ""
        conversation_id = str(avatar.get("conversation_id") or "")
        if (avatar.get("active") and avatar.get("mode") == "mcp" and
                _SAFE_ID.fullmatch(conversation_id)):
            try:
                result = self.mcp_call(_MCP_SERVER, "avatar_stop",
                                       {"conversation_id": conversation_id}, timeout=20)
                _mcp_payload(result)
            except Exception as exc:
                remote_error = live_copilot._text(exc, 400)
        stopped = self.store.stop_avatar(reason=reason)
        if snapshot.get("active"):
            try:
                self.store.add_event(source="system", kind="avatar", app="collie",
                                     text="AI avatar rehearsal ended.")
            except live_copilot.LiveCopilotError:
                pass
        return {"avatar": stopped, "remote_error": remote_error}


__all__ = ["AvatarRehearsalError", "AvatarRehearsalService", "capabilities"]
