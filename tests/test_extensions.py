import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from harness.extensions import ExtensionError, ExtensionStore, _Lock, _pid_alive, validate_package
from harness.context import ContextComposer
from harness.hooks import HookManager
from harness.skills import discover_skills


ROOT = Path(__file__).resolve().parents[1]


def _platform():
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _package(tmp_path, *, version="1.0.0", ext_id="acme.release", network=None,
             hooks=False, secret_value=None):
    root = tmp_path / (ext_id + "-" + version)
    skill = root / "skills" / "release" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: acme-release\ndescription: Prepare an audited Acme release.\n---\n\n# Release\n",
        encoding="utf-8",
    )
    files = ["skills/release/SKILL.md"]
    components = {"skills": ["skills/release/SKILL.md"], "hooks": [],
                  "connections": [], "templates": [], "assets": []}
    permissions = {"filesystem": [], "network": list(network or []), "credentials": [],
                   "browser": [], "desktop": [], "external_actions": [],
                   "host_hooks": hooks}
    if hooks:
        hook = root / "hooks.json"
        hook.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
            "type": "command", "command": "%s -c \"print('{}')\"" % sys.executable
        }]}]}}), encoding="utf-8")
        files.append("hooks.json"); components["hooks"] = ["hooks.json"]
    if network:
        components["connections"] = [{"name": "issues", "url": "https://api.acme.test/v1",
                                       "description": "Acme issues", "auth": "none"}]
    manifest = {
        "schema_version": 1,
        "id": ext_id,
        "name": "Acme Release",
        "version": version,
        "publisher": "Acme",
        "description": "Release workflow without executable model tools.",
        "license": "MIT",
        "collie": {"min_version": "0.1.0", "max_version": "99.0.0"},
        "platforms": [_platform()],
        "files": sorted(files),
        "components": components,
        "permissions": permissions,
        "data": {"retention": "none", "export": True, "uninstall": "remove"},
        "verification": [{"kind": "pytest", "command": "pytest -q"}],
    }
    if secret_value is not None:
        manifest["verification"][0]["api_token"] = secret_value
    (root / "collie-extension.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return root


def test_install_is_inert_until_exact_scopes_are_approved(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    source = _package(tmp_path, hooks=True)
    store = ExtensionStore(str(state))

    plan = store.plan(str(source))
    assert plan["id"] == "acme.release"
    assert plan["diff"]["permissions_changed"]
    installed = store.install(str(source))
    assert not installed["enabled"]
    assert installed["versions"][0]["trust_state"] == "unreviewed"
    assert discover_skills(str(tmp_path)) == []
    assert not HookManager(str(tmp_path), state_dir=str(state)).active

    with pytest.raises(ExtensionError, match="not approved"):
        store.enable("acme.release")
    enabled = store.enable("acme.release", approve=True)
    assert enabled["enabled"] and enabled["active_version"] == "1.0.0"
    skills = discover_skills(str(tmp_path))
    assert [skill["name"] for skill in skills] == ["acme-release"]
    assert skills[0]["trusted"] is True
    assert HookManager(str(tmp_path), state_dir=str(state)).events() == ["Stop"]


def test_digest_pin_and_tamper_detection_fail_closed(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    source = _package(tmp_path)
    store = ExtensionStore(str(state))
    report = validate_package(str(source))
    with pytest.raises(ExtensionError, match="provenance pin"):
        store.install(str(source), expected_digest="0" * 64)

    installed = store.install(str(source), expected_digest=report["digest"])
    assert installed["versions"][0]["trust_state"] == "digest_pinned"
    store.enable("acme.release", approve=True)
    composer = ContextComposer(None, None)
    assert "acme-release" in composer._skill_index(str(tmp_path))
    package = state / "extensions" / "packages" / "acme.release" / "1.0.0"
    (package / "skills" / "release" / "SKILL.md").write_text("tampered", encoding="utf-8")
    row = store.get("acme.release")
    assert not row["versions"][0]["integrity_ok"]
    assert store.active_records() == []
    assert discover_skills(str(tmp_path)) == []
    assert "acme-release" not in composer._skill_index(str(tmp_path))


def test_added_nested_skill_invalidates_the_approved_package(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    source = _package(tmp_path)
    store = ExtensionStore(str(state))
    store.install(str(source), approve=True)
    store.enable("acme.release")

    nested = (state / "extensions" / "packages" / "acme.release" / "1.0.0" /
              "skills" / "release" / "nested" / "SKILL.md")
    nested.parent.mkdir()
    nested.write_text(
        "---\nname: unapproved-nested\ndescription: Must remain inert.\n---\n",
        encoding="utf-8",
    )

    row = store.get("acme.release")
    assert row["versions"][0]["integrity_ok"] is False
    assert store.active_records() == []
    assert discover_skills(str(tmp_path)) == []


def test_declared_nested_skill_must_also_be_an_explicit_component(tmp_path):
    source = _package(tmp_path)
    nested = source / "skills" / "release" / "nested" / "SKILL.md"
    nested.parent.mkdir()
    nested.write_text(
        "---\nname: nested\ndescription: Must be explicitly exported.\n---\n",
        encoding="utf-8",
    )
    manifest_path = source / "collie-extension.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append("skills/release/nested/SKILL.md")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ExtensionError, match="explicit components.skills"):
        validate_package(str(source))


def test_reinstall_can_record_review_of_identical_existing_bytes(tmp_path):
    store = ExtensionStore(str(tmp_path / "state"))
    source = _package(tmp_path)
    assert store.install(str(source))["versions"][0]["approved"] is False
    reviewed = store.install(str(source), approve=True)
    assert reviewed["versions"][0]["approved"] is True
    assert reviewed["versions"][0]["trust_state"] == "locally_reviewed"
    assert store.enable("acme.release")["enabled"] is True


def test_update_permission_diff_rollback_and_revocation(tmp_path):
    state = tmp_path / "state"
    first = _package(tmp_path, version="1.0.0")
    second = _package(tmp_path, version="1.1.0", network=["api.acme.test"])
    store = ExtensionStore(str(state))
    store.install(str(first), approve=True)
    store.enable("acme.release")
    plan = store.plan(str(second))
    assert plan["diff"]["from_version"] == "1.0.0"
    assert plan["diff"]["permissions_changed"]
    assert plan["permissions"]["network"] == ["api.acme.test"]
    store.install(str(second))
    with pytest.raises(ExtensionError, match="not approved"):
        store.enable("acme.release", "1.1.0")
    store.enable("acme.release", "1.1.0", approve=True)
    assert store.rollback("acme.release")["active_version"] == "1.0.0"

    digest = next(row["digest"] for row in store.get("acme.release")["versions"]
                  if row["version"] == "1.1.0")
    # Re-enable the newer version, then revoke its exact bytes: active use stops immediately.
    store.enable("acme.release", "1.1.0")
    store.revoke("acme.release", digest, "publisher security advisory")
    assert not store.get("acme.release")["enabled"]
    with pytest.raises(ExtensionError, match="revoked"):
        store.enable("acme.release", "1.1.0")


def test_rollback_never_activates_a_version_that_was_not_active_before(tmp_path):
    store = ExtensionStore(str(tmp_path / "state"))
    first = _package(tmp_path, version="1.0.0")
    second = _package(tmp_path, version="2.0.0")
    store.install(str(first), approve=True)
    store.enable("acme.release", "1.0.0")
    store.install(str(second), approve=True)

    with pytest.raises(ExtensionError, match="previously active"):
        store.rollback("acme.release")
    assert store.get("acme.release")["active_version"] == "1.0.0"


def test_revocation_requires_matching_id_and_refreshes_long_lived_consumers(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    source = _package(tmp_path, hooks=True)
    store = ExtensionStore(str(state))
    store.install(str(source), approve=True)
    store.enable("acme.release")
    manager = HookManager(str(tmp_path), state_dir=str(state))
    composer = ContextComposer(None, None)
    assert manager.active and manager.events() == ["Stop"]
    assert "acme-release" in composer._skill_index(str(tmp_path))

    digest = store.get("acme.release")["versions"][0]["digest"]
    with pytest.raises(ExtensionError, match="not installed"):
        store.revoke("typo.release", digest, "wrong id")
    assert store.get("acme.release")["enabled"] is True

    store.revoke("acme.release", digest, "publisher advisory")
    assert store.get("acme.release")["enabled"] is False
    assert manager.active is False
    assert manager.events() == []
    assert "acme-release" not in composer._skill_index(str(tmp_path))


def test_component_and_identity_changes_are_reviewable_without_relabeling_active_version(tmp_path):
    state = tmp_path / "state"
    first = _package(tmp_path, version="1.0.0")
    second = _package(tmp_path, version="1.1.0")
    for source, kind in ((first, "assets"), (second, "templates")):
        content = source / "content" / "release.md"
        content.parent.mkdir()
        content.write_text("release template\n", encoding="utf-8")
        manifest_path = source / "collie-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append("content/release.md")
        manifest["files"].sort()
        manifest["components"][kind] = ["content/release.md"]
        if source == second:
            manifest["name"] = "Acme Release Next"
            manifest["description"] = "A renamed next version awaiting review."
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    store = ExtensionStore(str(state))
    first_report = validate_package(str(first))
    second_report = validate_package(str(second))
    assert first_report["scope_hash"] != second_report["scope_hash"]
    store.install(str(first), approve=True)
    store.enable("acme.release", "1.0.0")
    plan = store.plan(str(second))
    assert plan["diff"]["components_changed"] is True
    assert plan["diff"]["identity_changed"] is True

    store.install(str(second))
    current = store.get("acme.release")
    assert current["name"] == "Acme Release"
    assert current["description"] == "Release workflow without executable model tools."

    manifest_path = second / "collie-extension.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "1.2.0"
    manifest["publisher"] = "Different Publisher"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ExtensionError, match="publisher"):
        store.install(str(second))


def test_uninstall_requires_explicit_disable_and_cleans_registry(tmp_path):
    store = ExtensionStore(str(tmp_path / "state"))
    source = _package(tmp_path)
    store.install(str(source), approve=True)
    store.enable("acme.release")
    with pytest.raises(ExtensionError, match="disable"):
        store.uninstall("acme.release")
    store.disable("acme.release")
    assert store.uninstall("acme.release") == {
        "id": "acme.release", "removed_versions": ["1.0.0"]}
    assert store.list() == []


def test_manifest_rejects_undeclared_content_embedded_secrets_and_bad_connections(tmp_path):
    undeclared = _package(tmp_path, ext_id="acme.undeclared")
    (undeclared / "payload.py").write_text("print('surprise')", encoding="utf-8")
    with pytest.raises(ExtensionError, match="undeclared"):
        validate_package(str(undeclared))

    secret = _package(tmp_path, ext_id="acme.secret", secret_value="do-not-store-this")
    with pytest.raises(ExtensionError, match="never embed"):
        validate_package(str(secret))

    connection = _package(tmp_path, ext_id="acme.connection", network=["api.acme.test"])
    manifest_path = connection / "collie-extension.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["components"]["connections"][0]["url"] = "http://api.acme.test/v1"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ExtensionError, match="https"):
        validate_package(str(connection))

    huge_version = _package(tmp_path, ext_id="acme.huge-version")
    manifest_path = huge_version / "collie-extension.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "1.0." + ("9" * 500)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ExtensionError, match="at most 128"):
        validate_package(str(huge_version))


def test_library_cli_covers_review_activation_and_confirmed_removal(tmp_path, capsys):
    from harness.cli import main

    state = tmp_path / "state"
    source = _package(tmp_path)
    digest = validate_package(str(source))["digest"]
    common = ["--state-dir", str(state)]
    assert main(["library", "validate", str(source), *common]) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["digest"] == digest

    assert main(["library", "install", str(source), "--digest", digest,
                 "--approve", "--enable", *common]) == 0
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["enabled"] is True
    assert main(["library", "uninstall", "acme.release", *common]) == 2
    assert "repeat with --yes" in capsys.readouterr().err
    assert main(["library", "disable", "acme.release", *common]) == 0
    capsys.readouterr()
    assert main(["library", "uninstall", "acme.release", "--yes", *common]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed_versions"] == ["1.0.0"]

    scaffold = tmp_path / "starter"
    assert main(["library", "scaffold", str(scaffold), "--id", "org.acme.starter",
                 "--name", "Acme Starter", "--publisher", "Acme", *common]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["manifest"]["id"] == "org.acme.starter"
    assert validate_package(str(scaffold))["digest"] == created["digest"]


def test_cli_install_enable_activates_the_version_just_reviewed(tmp_path, capsys):
    from harness.cli import main

    state = tmp_path / "state"
    older = _package(tmp_path, version="1.0.0")
    newer = _package(tmp_path, version="2.0.0")
    common = ["--state-dir", str(state)]

    assert main(["library", "install", str(newer), *common]) == 0
    capsys.readouterr()
    assert main(["library", "install", str(older), "--approve", "--enable", *common]) == 0
    activated = json.loads(capsys.readouterr().out)
    assert activated["active_version"] == "1.0.0"
    versions = {row["version"]: row for row in activated["versions"]}
    assert versions["1.0.0"]["approved"] is True
    assert versions["2.0.0"]["approved"] is False


def test_shipped_example_is_a_valid_cross_platform_package():
    report = validate_package(str(ROOT / "examples" / "extensions" / "release-helper"))
    assert report["manifest"]["id"] == "org.collie.release-helper"
    assert len(report["digest"]) == 64


def test_semver_selection_duplicate_json_and_orphan_recovery(tmp_path):
    state = tmp_path / "state"
    beta2 = _package(tmp_path, version="1.0.0-beta.2")
    beta10 = _package(tmp_path, version="1.0.0-beta.10")
    store = ExtensionStore(str(state))
    store.install(str(beta2), approve=True)
    store.install(str(beta10), approve=True)
    assert store.enable("acme.release")["active_version"] == "1.0.0-beta.10"

    duplicate = tmp_path / "duplicate"; duplicate.mkdir()
    (duplicate / "collie-extension.json").write_text(
        '{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ExtensionError, match="duplicate key"):
        validate_package(str(duplicate))

    orphan_state = tmp_path / "orphan-state"
    source = _package(tmp_path, ext_id="acme.orphan")
    destination = (orphan_state / "extensions" / "packages" /
                   "acme.orphan" / "1.0.0")
    shutil.copytree(source, destination)
    recovered = ExtensionStore(str(orphan_state)).install(str(source))
    assert recovered["id"] == "acme.orphan"


def test_library_lock_recovers_dead_owner_without_stealing_live_owner(tmp_path):
    path = tmp_path / ".lock"
    path.write_text("999999999 0\n", encoding="ascii")
    with _Lock(str(path), timeout=.1):
        assert path.exists()
    assert not path.exists()

    path.write_text("%d 0\n" % os.getpid(), encoding="ascii")
    os.utime(path, (0, 0))
    assert _pid_alive(os.getpid())
    with pytest.raises(ExtensionError, match="still running"):
        with _Lock(str(path), timeout=.05):
            pass


def test_registry_inner_shape_errors_are_controlled(tmp_path):
    state = tmp_path / "state"
    root = state / "extensions"
    root.mkdir(parents=True)
    (root / "registry.json").write_text(json.dumps({
        "schema_version": 1,
        "extensions": {"acme.bad": {"versions": []}},
        "revocations": {},
        "audit": [],
    }), encoding="utf-8")
    with pytest.raises(ExtensionError, match="invalid extension record"):
        ExtensionStore(str(state)).list()
