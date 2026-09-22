"""Public, non-secret Comfy connection state for Collie's product surfaces.

Comfy already ships first-party Cloud and local MCP servers.  Collie should use
those maintained contracts instead of growing a second, partial Comfy API
client.  This module only answers what is available on this machine and can add
the official local stdio server after its executable already exists.
"""
from __future__ import annotations

import json
import os
import shutil
import urllib.request


CLOUD_MCP_URL = "https://cloud.comfy.org/mcp"
CLOUD_APP_URL = "https://cloud.comfy.org"
LOCAL_APP_URL = "http://127.0.0.1:8188"
DESKTOP_DOCS_URL = "https://docs.comfy.org/installation/desktop/windows"
MCP_DOCS_URL = "https://docs.comfy.org/agent-tools/mcp"


def _local_server(timeout: float = 0.65) -> dict:
    """Probe the standard local endpoint and return a bounded public summary."""
    try:
        request = urllib.request.Request(
            LOCAL_APP_URL + "/system_stats",
            headers={"Accept": "application/json", "User-Agent": "Collie-Comfy/0.1"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(512_000)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("unexpected ComfyUI response")
    except Exception as exc:
        return {"reachable": False, "url": LOCAL_APP_URL,
                "error": type(exc).__name__}

    system = payload.get("system") if isinstance(payload.get("system"), dict) else {}
    devices = payload.get("devices") if isinstance(payload.get("devices"), list) else []
    public_devices = []
    for row in devices[:8]:
        if not isinstance(row, dict):
            continue
        public_devices.append({
            "name": str(row.get("name") or row.get("type") or "device")[:160],
            "type": str(row.get("type") or "")[:80],
            "vram_total": row.get("vram_total") if isinstance(row.get("vram_total"), int) else None,
        })
    return {
        "reachable": True,
        "url": LOCAL_APP_URL,
        "comfy_version": str(system.get("comfyui_version") or "")[:80],
        "python_version": str(system.get("python_version") or "")[:120],
        "devices": public_devices,
    }


def _mcp_row(name: str) -> dict | None:
    try:
        from . import mcpclient
        return next((dict(row) for row in mcpclient.status()
                     if row.get("name") == name), None)
    except Exception:
        return None


def _configured_local_bins() -> tuple[bool, bool]:
    """Detect an isolated official local install without returning its private paths."""
    try:
        from . import mcpclient
        cfg = mcpclient._load_config().get("comfy-local") or {}
    except Exception:
        return False, False
    if not isinstance(cfg, dict):
        return False, False
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    command = cfg.get("command")
    comfy_bin = env.get("COMFY_BIN")
    return (
        isinstance(command, str) and os.path.isfile(command),
        isinstance(comfy_bin, str) and os.path.isfile(comfy_bin),
    )


def snapshot() -> dict:
    """Return Comfy readiness without exposing credentials or local config paths."""
    cloud = _mcp_row("comfy-cloud")
    local = _mcp_row("comfy-local") or _mcp_row("comfy-mcp")
    comfy_cli = shutil.which("comfy")
    comfy_mcp = shutil.which("comfy-mcp")
    configured_mcp, configured_cli = _configured_local_bins()
    local_server = _local_server()
    cloud_auth = str((cloud or {}).get("auth") or "not-configured")
    cloud_enabled = bool((cloud or {}).get("enabled", True)) if cloud is not None else False
    local_enabled = bool((local or {}).get("enabled", True)) if local is not None else False
    return {
        "cloud": {
            "configured": cloud is not None,
            "connected": cloud_enabled and cloud_auth in {"oauth", "header", "none"},
            "enabled": cloud_enabled,
            "auth": cloud_auth,
            "tools": (cloud or {}).get("tools"),
            "mcp_url": CLOUD_MCP_URL,
            "app_url": CLOUD_APP_URL,
        },
        "local": {
            **local_server,
            "mcp_configured": local is not None,
            "mcp_enabled": local_enabled,
            "mcp_tools": (local or {}).get("tools"),
            "cli_installed": bool(comfy_cli) or configured_cli,
            "mcp_installed": bool(comfy_mcp) or configured_mcp,
        },
        "links": {"desktop_docs": DESKTOP_DOCS_URL, "mcp_docs": MCP_DOCS_URL},
    }


def refresh_connections() -> dict:
    """Refresh configured Comfy tool contracts and return bounded public results."""
    from . import mcpclient

    configured = mcpclient._load_config()
    names = [name for name in ("comfy-cloud", "comfy-local", "comfy-mcp")
             if isinstance(configured.get(name), dict)
             and mcpclient.enabled(configured[name])]
    refreshed, errors = [], []
    for name in names:
        try:
            tools = mcpclient.refresh_server(name)
            refreshed.append({"server": name, "tools": len(tools)})
        except Exception as exc:
            errors.append({"server": name, "error": "%s: %s" % (
                type(exc).__name__, str(exc)[:300])})
    return {"ok": not errors, "refreshed": refreshed, "errors": errors,
            "status": snapshot()}


def add_local_connection() -> dict:
    """Register an already-installed official ``comfy-mcp`` executable.

    Installation stays a separate, explicit user action: silently pip-installing
    an executable would widen Collie's code-execution surface and mutate an
    unrelated Python environment.
    """
    from . import mcpclient

    command = shutil.which("comfy-mcp")
    if not command:
        raise ValueError("comfy-mcp is not installed; follow the official local MCP setup first")
    cfg = {"command": os.path.abspath(command)}
    comfy_bin = shutil.which("comfy")
    if comfy_bin:
        cfg["env"] = {"COMFY_BIN": os.path.abspath(comfy_bin)}
    existing = mcpclient._load_config().get("comfy-local")
    if existing:
        return {"ok": True, "already_configured": True, "server": "comfy-local"}
    error = mcpclient.add_server("comfy-local", cfg, replace=False)
    if error:
        raise ValueError(error)
    return {"ok": True, "server": "comfy-local", "note": "takes effect on the next Collie run"}
