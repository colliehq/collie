"""Source and asset paths retain their meaning in source and installed layouts."""
from __future__ import annotations

import ast
import importlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from harness import paths


@pytest.fixture
def unbound_defaults(monkeypatch, tmp_path):
    for name in ("COLLIE_DATA_DIR", "COLLIE_STATE_DIR", "COLLIE_SESSIONS_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    previous = paths.set_data_root_resolver(None)
    try:
        yield
    finally:
        paths.set_data_root_resolver(previous)


def _flat_source(checkout):
    module = checkout / "harness" / "module.py"
    module.parent.mkdir(parents=True)
    module.write_text("# fixture only\n", encoding="utf-8")
    (checkout / "pyproject.toml").touch()
    return module


def test_flat_checkout_defaults_to_repo_data_without_component_manifest(tmp_path, monkeypatch,
                                                                      unbound_defaults):
    checkout = tmp_path / "checkout"
    module = _flat_source(checkout)
    assert paths.source_root(str(module)) == str(checkout)
    monkeypatch.setattr(paths, "ROOT", paths.source_root(str(module)))
    assert paths.data_dir() == str(checkout / "data")
    assert not (checkout / "data").exists()
    assert not (checkout / "components").exists()
    # A package alone is insufficient evidence of a source checkout.
    (checkout / "pyproject.toml").unlink()
    assert paths.source_root(str(module)) is None


def test_installed_layout_defaults_to_owned_home(tmp_path, monkeypatch, unbound_defaults):
    module = tmp_path / "site-packages" / "harness" / "paths.py"
    module.parent.mkdir(parents=True)
    module.touch()
    # An unrelated project above site-packages must not redirect installed state.
    (tmp_path / "pyproject.toml").touch()
    assert paths.source_root(str(module)) is None
    monkeypatch.setattr(paths, "ROOT", str(module.parent.parent))
    assert Path(paths.data_dir()) == tmp_path / "home" / ".collie" / "data"
    assert not (tmp_path / "home").exists()


def test_canonical_core_and_cli_agree_on_checkout_root(unbound_defaults):
    # Inspect/execute only CLI's pure ROOT assignment, never its bootstrap or migration.
    source = Path(importlib.util.find_spec("harness.cli").origin)
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "ROOT"
                              for target in node.targets))
    namespace = {"__file__": str(source), "_source_root": paths.source_root, "os": os}
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), str(source), "exec"), namespace)
    assert namespace["ROOT"] == paths.ROOT
    assert Path(paths.ROOT, "pyproject.toml").is_file()
    assert source.parent == Path(paths.ROOT) / "harness"
    assert not Path(paths.ROOT, "components").exists()
    assert paths.data_dir() == str(Path(paths.ROOT) / "data")


def test_editable_asset_uses_owner_without_importing_it(tmp_path, monkeypatch):
    import harness

    owner = tmp_path / "optional-tools" / "harness"
    owner.mkdir(parents=True)
    (owner / "fixture_asset_owner.py").write_text(
        "raise AssertionError('asset lookup must not execute the adapter')\n", encoding="utf-8")
    (owner / "browser_ext").mkdir()
    monkeypatch.setattr(harness, "__path__", [*harness.__path__, str(owner)])
    importlib.invalidate_caches()
    assert "harness.fixture_asset_owner" not in sys.modules
    assert paths.component_resource_dir("harness.fixture_asset_owner", "browser_ext") == str(owner / "browser_ext")
    assert "harness.fixture_asset_owner" not in sys.modules


def test_real_browser_assets_resolve_to_the_unique_runtime_source():
    spec = importlib.util.find_spec("harness.browserbridge")
    assert spec is not None
    resource = Path(paths.component_resource_dir("harness.browserbridge", "browser_ext"))
    assert resource == Path(spec.origin).parent / "browser_ext"
    assert resource == Path(__file__).resolve().parents[1] / "harness" / "browser_ext"
    assert (resource / "manifest.json").is_file()


def test_asset_lookup_refuses_missing_owner_or_traversal():
    with pytest.raises(FileNotFoundError):
        paths.component_resource_dir("harness.uninstalled_fixture_owner", "browser_ext")
    with pytest.raises(ValueError):
        paths.component_resource_dir("harness.browserbridge", "../webui")
    with pytest.raises(ValueError):
        # find_spec on a nested module would import its parent adapter.
        paths.component_resource_dir("harness.browserbridge.nested", "browser_ext")


def test_cross_component_consumers_resolve_assets_and_preserve_unavailable(monkeypatch):
    from harness import avatar, doctor, google_connect, ops, slackbot

    webui = Path(paths.component_resource_dir("harness.webapp", "webui"))
    assert avatar.LOGO == google_connect._LOGO == str(webui / "logo.svg")
    assert slackbot.ICON == str(webui / "collie-icon-512.png")
    manifest = Path(paths.component_resource_dir("harness.browserbridge", "browser_ext")) / "manifest.json"
    import json
    expected = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    assert ops._shipped_extension_version() == doctor._shipped_extension_version() == expected

    def missing(*args, **kwargs):
        raise FileNotFoundError("fixture owner is not installed")

    monkeypatch.setattr(paths, "component_resource_dir", missing)
    assert ops._shipped_extension_version() == doctor._shipped_extension_version() == ""
    monkeypatch.setattr(slackbot, "ICON", "")
    monkeypatch.setattr(slackbot.urllib.request, "urlopen",
                        lambda *args, **kwargs: pytest.fail("missing icon cannot issue a request"))
    assert slackbot.set_icon("synthetic-token", "synthetic-app")


@pytest.mark.parametrize("method,path", [("do_GET", "/api/browser/status"),
                                         ("do_POST", "/api/browser/start")])
def test_missing_browser_assets_return_unavailable_before_start(monkeypatch, method, path):
    from harness import webapp

    def missing(*args, **kwargs):
        raise FileNotFoundError("fixture owner is not installed")

    monkeypatch.setattr(paths, "component_resource_dir", missing)
    handler = SimpleNamespace(path=path, _extension_origin=lambda: "",
                              _host_ok=lambda: True, _peer_ok=lambda parsed: True,
                              _authed=lambda parsed: True,
                              _send_json=lambda body, code=200: (body, code))
    body, code = getattr(webapp.Handler, method)(handler)
    assert code == 503
    assert body == {"error": "browser extension component is unavailable"}
