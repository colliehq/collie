"""A safe, user-facing inventory for Collie's capability Library.

The extension registry is only one way Collie gains capabilities.  This module
combines the built-in product surfaces, discovered Skills, configured
connections, recorded workflows, and installed extension packages without
returning secrets or local package paths.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time


_SKILL_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _inside(base: str, *parts: str) -> str:
    base = os.path.abspath(base)
    target = os.path.abspath(os.path.join(base, *parts))
    try:
        if os.path.commonpath([base, target]) != base:
            raise ValueError("capability target escapes the Library")
    except ValueError as exc:
        raise ValueError("capability target escapes the Library") from exc
    return target


def _on(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _skill_origin(path: str, root: str, cwd: str) -> str:
    path = os.path.abspath(path)
    candidates = (
        (os.path.join(cwd, ".collie", "skills"), "project"),
        (os.path.join(cwd, ".agents", "skills"), "project"),
        (os.path.join(root, "skills"), "collie"),
        (os.path.expanduser("~/.claude/skills"), "claude"),
    )
    for base, label in candidates:
        try:
            base = os.path.abspath(base)
            if os.path.commonpath([base, path]) == base:
                return label
        except ValueError:
            continue
    return "extension or custom"


def _builtins() -> list[dict]:
    from . import settings

    browser_mode = settings.get("BROWSER_BRIDGE", "auto")
    try:
        from .tools import _bridge_live_safe
        browser_live = bool(_bridge_live_safe())
    except Exception:
        browser_live = False
    desktop_on = _on(settings.get("DESKTOP_CONTROL", "off"))
    screen_on = _on(settings.get("SCREEN_CAPTURE", "off"))
    try:
        from .comfy_integration import snapshot as comfy_snapshot
        comfy = comfy_snapshot()
        comfy_cloud, comfy_local = comfy["cloud"], comfy["local"]
        comfy_ready = bool(comfy_cloud.get("connected") or (
            comfy_local.get("mcp_configured") and comfy_local.get("mcp_enabled", True)))
        comfy_tools = ((comfy_cloud.get("tools") if comfy_cloud.get("connected") else None)
                       or (comfy_local.get("mcp_tools")
                           if comfy_local.get("mcp_enabled", True) else None) or 0)
    except Exception:
        comfy_ready, comfy_tools = False, 0
    return [
        {
            "id": "code-workspace", "name": "Files and code",
            "description": "Read, search, edit, run checks, plan work, and rewind local changes.",
            "status": "ready", "tools": 10, "action": "new-task",
        },
        {
            "id": "durable-missions", "name": "Durable missions",
            "description": "Keep longer work moving across waits, retries, specialists, and recovery.",
            "status": "ready", "tools": 1, "action": "missions",
        },
        {
            "id": "memory", "name": "Memory and verification",
            "description": "Recall reviewed facts and return checks with explicit scope and evidence.",
            "status": "ready", "tools": 4, "action": "new-task",
        },
        {
            "id": "browser", "name": "Signed-in browser",
            "description": "Research and work in the Chrome sessions already signed in on this computer.",
            "status": ("ready" if browser_live else
                       "off" if str(browser_mode) == "0" else "setup"),
            "tools": 20 if browser_live else 0, "action": "settings-tools",
        },
        {
            "id": "desktop", "name": "Desktop apps",
            "description": "Operate native application controls, windows, keyboard, mouse, and clipboard.",
            "status": "ready" if desktop_on else "permission", "tools": 8,
            "action": "settings-capabilities",
        },
        {
            "id": "screen", "name": "Screen awareness",
            "description": "Capture a window or display so Collie can inspect visual results.",
            "status": "ready" if screen_on else "permission", "tools": 1,
            "action": "settings-capabilities",
        },
        {
            "id": "meetings", "name": "Meeting notes",
            "description": "Record, transcribe, summarize, extract actions, and remind only when useful.",
            "status": "ready", "tools": 1, "action": "meetings",
        },
        {
            "id": "workflow-studio", "name": "Workflow Studio",
            "description": "Record a proven workflow, dry-run it, evaluate it, then approve it as a Skill.",
            "status": "ready", "tools": 1, "action": "studio",
        },
        {
            "id": "comfy", "name": "Comfy visual AI",
            "description": "Search models, nodes and templates, then build and run inspectable image, video, audio and 3D workflows through Comfy's official MCP.",
            "status": "ready" if comfy_ready else "setup", "tools": comfy_tools,
            "action": "comfy",
        },
    ]


def snapshot(root: str, cwd: str | None = None) -> dict:
    """Return the complete safe Library inventory."""
    root = os.path.abspath(os.path.expanduser(root))
    cwd = os.path.abspath(cwd or os.getcwd())
    from .extensions import ExtensionStore
    from .skills import discover_skills
    from .workflow_capture import WorkflowStore

    discovered = discover_skills(cwd, extra_dirs=[os.path.join(root, "skills")])
    skills = [{
        "name": str(row.get("name") or "")[:160],
        "description": str(row.get("description") or "")[:500],
        "trusted": bool(row.get("trusted", True)),
        "origin": _skill_origin(str(row.get("path") or ""), root, cwd),
    } for row in discovered]

    try:
        from . import mcpclient
        mcp = mcpclient.status()
    except Exception:
        mcp = []
    connections = [{
        "id": str(row.get("name") or "")[:160],
        "name": str(row.get("name") or "")[:160],
        "kind": str(row.get("kind") or "mcp")[:40],
        "status": ("off" if row.get("enabled") is False else
                   "setup" if row.get("auth") == "login-needed" else "ready"),
        "tools": row.get("tools") if isinstance(row.get("tools"), int) else None,
        "auth": str(row.get("auth") or "")[:40],
    } for row in mcp if isinstance(row, dict)]
    try:
        from .workidentity import public_connections
        for row in public_connections(root):
            connections.append({
                "id": str(row.get("id") or "")[:160],
                "name": str(row.get("label") or row.get("id") or "")[:160],
                "kind": "work identity",
                "status": "ready" if row.get("connected") else "setup",
                "tools": len(row.get("scopes") or []),
                "auth": "connected" if row.get("connected") else "not connected",
            })
    except Exception:
        pass

    workflows = []
    try:
        for row in WorkflowStore().list():
            evaluation = row.get("evaluation") if isinstance(row.get("evaluation"), dict) else {}
            workflows.append({
                "id": str(row.get("id") or "")[:80],
                "name": str(row.get("name") or "")[:160],
                "description": str(row.get("description") or "")[:500],
                "status": str(row.get("status") or "draft")[:40],
                "event_count": int(row.get("event_count") or 0),
                "updated_at": row.get("updated_at"),
                "eligible": bool(evaluation.get("eligible")),
            })
    except (OSError, RuntimeError, TypeError, ValueError):
        workflows = []

    extensions = ExtensionStore(root).list()
    builtins = _builtins()
    return {
        "builtins": builtins, "skills": skills, "connections": connections,
        "workflows": workflows, "extensions": extensions,
        "summary": {
            "builtins": len(builtins), "skills": len(skills),
            "connections": sum(1 for row in connections if row["status"] == "ready"),
            "workflows": len(workflows), "extensions": len(extensions),
        },
        "add_actions": ["create_skill", "import_package", "add_connection", "record_workflow"],
    }


def create_skill(root: str, *, name, description, instructions) -> dict:
    """Create one user-authored, global Collie Skill without overwriting existing bytes."""
    root = os.path.abspath(os.path.expanduser(root))
    raw_name = str(name or "").strip()
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw_name.lower()).strip("-_")[:64]
    description = str(description or "").replace("\x00", "").strip()
    instructions = str(instructions or "").replace("\x00", "").strip()
    if not _SKILL_SLUG.fullmatch(slug):
        raise ValueError("skill name must contain letters or numbers")
    if not description or len(description) > 500:
        raise ValueError("skill description is required and must be at most 500 characters")
    if not instructions or len(instructions) > 32_000:
        raise ValueError("skill instructions are required and must be at most 32000 characters")
    base = _inside(root, "skills")
    target = _inside(base, slug)
    path = _inside(target, "SKILL.md")
    if os.path.exists(path):
        raise ValueError("a skill with this name already exists")
    os.makedirs(target, exist_ok=True)
    title = raw_name[:120] or slug
    body = ("---\nname: %s\ndescription: %s\n---\n\n# %s\n\n%s\n" %
            (slug, json.dumps(description, ensure_ascii=False), title, instructions))
    fd, temp = tempfile.mkstemp(prefix="skill-", suffix=".tmp", dir=target)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body); handle.flush(); os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            if os.path.exists(temp): os.unlink(temp)
        except OSError:
            pass
    return {"name": slug, "description": description, "origin": "collie",
            "trusted": True, "created_at": time.time()}


def preview_package(root: str, source) -> dict:
    """Validate a local extension and return only material needed for informed approval."""
    from .extensions import ExtensionStore
    source = str(source or "").strip()
    if not source or len(source) > 4096:
        raise ValueError("a local package folder is required")
    plan = ExtensionStore(root).plan(source)
    return {key: plan.get(key) for key in (
        "id", "name", "publisher", "description", "version", "digest", "scope_hash", "diff",
        "permissions", "components", "publisher_signature")}


def install_package(root: str, *, source, digest, confirmed=False) -> dict:
    """Install and enable exactly the package bytes the user previewed."""
    from .extensions import ExtensionStore
    if confirmed is not True:
        raise ValueError("confirmed=true is required after reviewing the package")
    source, digest = str(source or "").strip(), str(digest or "").strip().lower()
    if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", digest):
        raise ValueError("the reviewed SHA-256 digest is required")
    store = ExtensionStore(root)
    installed = store.install(source, expected_digest=digest, approve=True)
    version = str(installed.get("installed_version") or "")
    return store.enable(str(installed.get("id") or ""), version, approve=False)
