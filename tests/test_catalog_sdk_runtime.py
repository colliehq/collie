"""The model picker recognizes an SDK-only installation without a global CLI."""
from types import SimpleNamespace

import pytest

from harness import catalog


@pytest.mark.parametrize("windows,binary", [(True, "claude.exe"), (False, "claude")])
def test_sdk_bundle_is_available_without_path_cli(tmp_path, monkeypatch, windows, binary):
    monkeypatch.setattr(catalog, "_plugin_info", lambda: {})
    monkeypatch.setattr(catalog.plat, "is_windows", lambda: windows)
    monkeypatch.setattr(catalog.importlib.util, "find_spec", lambda _: SimpleNamespace(
        submodule_search_locations=[str(tmp_path)]))
    monkeypatch.setattr(catalog.shutil, "which", lambda _: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert catalog.probe_auth("claude-agent-sdk") == "not-logged-in"
    bundled = tmp_path / "_bundled"
    bundled.mkdir()
    (bundled / binary).write_bytes(b"fixture runtime")
    assert catalog.probe_auth("claude-agent-sdk") == "ok"


def test_cli_alone_cannot_replace_missing_sdk(monkeypatch):
    monkeypatch.setattr(catalog, "_plugin_info", lambda: {})
    monkeypatch.setattr(catalog.importlib.util, "find_spec", lambda _: None)
    monkeypatch.setattr(catalog.shutil, "which", lambda _: "claude.exe")
    assert catalog.probe_auth("claude-agent-sdk") == "not-logged-in"
