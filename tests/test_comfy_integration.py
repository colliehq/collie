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
