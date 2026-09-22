import json


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.payload if limit < 0 else self.payload[:limit]


def test_comfy_catalog_is_first_party_and_aliases_resolve():
    from harness import mcpclient

    for name in ("comfy", "comfyui", "Comfy Cloud"):
        hit = mcpclient.known(name)
        assert hit["name"] == "comfy-cloud"
        assert hit["url"] == "https://cloud.comfy.org/mcp"
        assert not hit.get("byo_client")


def test_comfy_snapshot_is_bounded_and_never_returns_executable_paths(monkeypatch):
    from harness import comfy_integration as comfy

    monkeypatch.setattr(comfy.urllib.request, "urlopen", lambda *_a, **_k: _Response({
        "system": {"comfyui_version": "0.9.1", "python_version": "3.12.9"},
        "devices": [{"name": "cuda:0 NVIDIA RTX", "type": "cuda",
                     "vram_total": 32 * 1024**3, "secret": "do-not-return"}],
    }))
    monkeypatch.setattr(comfy, "_mcp_row", lambda name: (
        {"name": name, "auth": "oauth", "enabled": True, "tools": 23}
        if name == "comfy-cloud" else None))
    monkeypatch.setattr(comfy.shutil, "which", lambda name: "C:/private/" + name + ".exe")

    result = comfy.snapshot()
    assert result["cloud"]["connected"] is True
    assert result["cloud"]["tools"] == 23
    assert result["local"]["reachable"] is True
    assert result["local"]["devices"][0]["vram_total"] == 32 * 1024**3
    serialized = json.dumps(result)
    assert "do-not-return" not in serialized
    assert "C:/private" not in serialized


def test_comfy_snapshot_detects_isolated_configured_install(monkeypatch):
    from harness import comfy_integration as comfy
    from harness import mcpclient

    monkeypatch.setattr(comfy, "_local_server", lambda: {
        "reachable": True, "url": comfy.LOCAL_APP_URL,
    })
    monkeypatch.setattr(comfy, "_mcp_row", lambda name: (
        {"name": name, "auth": "none", "enabled": True, "tools": 39}
        if name == "comfy-local" else None))
    monkeypatch.setattr(comfy.shutil, "which", lambda _name: None)
    monkeypatch.setattr(mcpclient, "_load_config", lambda: {"comfy-local": {
        "command": "C:/isolated/comfy-mcp.exe",
        "env": {"COMFY_BIN": "C:/isolated/comfy.exe"},
    }})
    monkeypatch.setattr(comfy.os.path, "isfile", lambda path: path.startswith("C:/isolated/"))

    result = comfy.snapshot()
    assert result["local"]["mcp_configured"] is True
    assert result["local"]["mcp_installed"] is True
    assert result["local"]["cli_installed"] is True
    assert "isolated" not in json.dumps(result)


def test_add_local_connection_requires_installed_official_server(monkeypatch):
    from harness import comfy_integration as comfy
    from harness import mcpclient

    monkeypatch.setattr(comfy.shutil, "which", lambda _name: None)
    try:
        comfy.add_local_connection()
    except ValueError as exc:
        assert "comfy-mcp is not installed" in str(exc)
    else:
        raise AssertionError("missing comfy-mcp must be refused")

    seen = {}
    monkeypatch.setattr(comfy.shutil, "which", lambda name: "C:/bin/%s.exe" % name)
    monkeypatch.setattr(mcpclient, "_load_config", lambda: {})
    monkeypatch.setattr(mcpclient, "add_server",
                        lambda name, cfg, replace=False: seen.update(name=name, cfg=cfg) or "")
    result = comfy.add_local_connection()
    assert result["server"] == "comfy-local"
    assert seen["cfg"]["command"].endswith("comfy-mcp.exe")
    assert seen["cfg"]["env"]["COMFY_BIN"].endswith("comfy.exe")


def test_library_promotes_comfy_without_hiding_existing_capabilities(monkeypatch):
    from harness import capability_library, comfy_integration

    monkeypatch.setattr(comfy_integration, "snapshot", lambda: {
        "cloud": {"connected": True, "tools": 27},
        "local": {"mcp_configured": False, "mcp_tools": None},
    })
    rows = capability_library._builtins()
    comfy = next(row for row in rows if row["id"] == "comfy")
    assert comfy["status"] == "ready" and comfy["tools"] == 27
    assert comfy["action"] == "comfy"
    assert {row["id"] for row in rows} >= {"code-workspace", "meetings", "comfy"}


def test_disabled_comfy_connections_are_not_reported_ready(monkeypatch):
    from harness import capability_library, comfy_integration

    monkeypatch.setattr(comfy_integration, "_local_server", lambda: {
        "reachable": True, "url": comfy_integration.LOCAL_APP_URL,
    })
    monkeypatch.setattr(comfy_integration, "_mcp_row", lambda name: {
        "name": name, "auth": "oauth" if name == "comfy-cloud" else "none",
        "enabled": False, "tools": 41,
    } if name in ("comfy-cloud", "comfy-local") else None)
    monkeypatch.setattr(comfy_integration.shutil, "which", lambda _name: None)
    monkeypatch.setattr(comfy_integration, "_configured_local_bins", lambda: (False, False))

    status = comfy_integration.snapshot()
    assert status["cloud"]["connected"] is False
    assert status["local"]["mcp_enabled"] is False
    row = next(item for item in capability_library._builtins() if item["id"] == "comfy")
    assert row["status"] == "setup" and row["tools"] == 0


def test_refresh_connections_reports_each_server_without_exposing_config(monkeypatch):
    from harness import comfy_integration, mcpclient

    monkeypatch.setattr(mcpclient, "_load_config", lambda: {
        "comfy-cloud": {"url": comfy_integration.CLOUD_MCP_URL},
        "comfy-local": {"command": "C:/private/comfy-mcp.exe"},
    })
    monkeypatch.setattr(mcpclient, "refresh_server", lambda name: [
        {"name": "tool-%s" % name},
    ])
    monkeypatch.setattr(comfy_integration, "snapshot", lambda: {"safe": True})

    result = comfy_integration.refresh_connections()
    assert result["ok"] is True
    assert result["refreshed"] == [
        {"server": "comfy-cloud", "tools": 1},
        {"server": "comfy-local", "tools": 1},
    ]
    assert "private" not in json.dumps(result)


def test_refresh_connections_skips_disabled_servers(monkeypatch):
    from harness import comfy_integration, mcpclient

    monkeypatch.setattr(mcpclient, "_load_config", lambda: {
        "comfy-cloud": {"url": comfy_integration.CLOUD_MCP_URL, "enabled": False},
        "comfy-local": {"command": "comfy-mcp"},
    })
    called = []
    monkeypatch.setattr(mcpclient, "refresh_server", lambda name: called.append(name) or [])
    monkeypatch.setattr(comfy_integration, "snapshot", lambda: {"safe": True})

    result = comfy_integration.refresh_connections()
    assert called == ["comfy-local"]
    assert result["ok"] is True


def test_refresh_connections_reports_failure_without_leaking_config(monkeypatch):
    from harness import comfy_integration, mcpclient

    monkeypatch.setattr(mcpclient, "_load_config", lambda: {
        "comfy-local": {"command": "C:/private/comfy-mcp.exe"},
    })
    monkeypatch.setattr(
        mcpclient, "refresh_server",
        lambda _name: (_ for _ in ()).throw(RuntimeError("server unavailable")))
    monkeypatch.setattr(comfy_integration, "snapshot", lambda: {"safe": True})

    result = comfy_integration.refresh_connections()
    assert result["ok"] is False
    assert result["errors"] == [{
        "server": "comfy-local", "error": "RuntimeError: server unavailable",
    }]
    assert "private" not in str(result)
