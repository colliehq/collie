"""Public catalogs must work from the repo root or a non-Git source copy.

Only static plugin content is read. No host configuration, account, model, or
real memory is opened by these checks.
"""
import json
from pathlib import Path
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[1]
LEAVES = {
    "codex": (".codex-plugin", "collie-task-evidence", "evidence_bridge", "task-handoff"),
    "codex-memory": (".codex-plugin", "collie-local-memory", "memory_bridge", "local-memory"),
    "claude-code": (".claude-plugin", "collie-task-evidence", "evidence_bridge", "task-handoff"),
    "claude-code-memory": (".claude-plugin", "collie-local-memory", "memory_bridge", "local-memory"),
}
CATALOGS = (
    (".agents/plugins/marketplace.json", {"codex", "codex-memory"}),
    (".claude-plugin/marketplace.json", {"claude-code", "claude-code-memory"}),
)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def check_catalog(root, relative, folders):
    catalog = load(root / relative)
    assert catalog["name"] == "collie"
    assert len(catalog["plugins"]) == 2
    resolved = {}
    for entry in catalog["plugins"]:
        source = entry["source"]
        if isinstance(source, dict):
            assert source["source"] == "local"
            assert entry["policy"]["installation"] == "AVAILABLE"
            assert entry["policy"]["authentication"] == "ON_INSTALL"
            assert entry["category"] == "Productivity"
            source = source["path"]
        assert source.startswith("./") and ".." not in Path(source).parts
        target = (root / source).resolve()
        assert target.is_relative_to(root.resolve())
        assert target.is_dir()
        folder = target.name
        manifest_dir, name, _, _ = LEAVES[folder]
        assert entry["name"] == name
        assert load(target / manifest_dir / "plugin.json")["name"] == name
        assert name not in resolved
        resolved[name] = folder
    assert set(resolved.values()) == folders
    if relative.startswith(".claude-plugin/"):
        assert catalog["owner"]["name"]


@pytest.mark.parametrize("relative,folders", CATALOGS)
def test_root_catalog_resolves_each_host_to_its_two_plugins(relative, folders):
    check_catalog(ROOT, relative, folders)


@pytest.mark.parametrize("relative,folders", CATALOGS)
def test_catalog_works_in_non_git_source_copy(tmp_path, relative, folders):
    # Copy exactly the advertised distribution structure, including dot dirs.
    source = tmp_path / "extracted source"
    source.mkdir()
    shutil.copytree(ROOT / "plugins", source / "plugins")
    for catalog, _ in CATALOGS:
        destination = source / catalog
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / catalog, destination)
    assert not (source / ".git").exists()
    check_catalog(source, relative, folders)


@pytest.mark.parametrize("folder", LEAVES)
def test_leaf_manifest_launches_shared_runtime_with_explicit_config(folder):
    manifest_dir, name, module, skill = LEAVES[folder]
    leaf = ROOT / "plugins" / folder
    manifest = load(leaf / manifest_dir / "plugin.json")
    assert manifest["name"] == name
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert (leaf / "skills" / skill / "SKILL.md").is_file()
    servers = load(leaf / ".mcp.json")["mcpServers"]
    assert len(servers) == 1
    server = next(iter(servers.values()))
    assert server["command"] == "python"
    assert server["args"][:3] == ["-m", "harness." + module, "--config"]
    assert len(server["args"]) == 4
    assert server["args"][3].startswith("REPLACE_WITH_ABSOLUTE_USER_")
    assert not (leaf / "harness").exists()
    assert (leaf / "README.md").is_file()
    assert (leaf / "INSTALL_LOCAL.md").is_file()
    assert (leaf / "config.example.json").is_file()


@pytest.mark.parametrize("folder", ("codex", "codex-memory"))
def test_standalone_codex_example_resolves_from_the_leaf(folder):
    leaf = ROOT / "plugins" / folder
    example = load(leaf / "marketplace.example.json")
    assert len(example["plugins"]) == 1
    entry = example["plugins"][0]
    assert entry["name"] == LEAVES[folder][1]
    assert entry["source"] == {"source": "local", "path": "./"}
    assert (leaf / entry["source"]["path"]).resolve() == leaf.resolve()
