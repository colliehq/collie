"""Release packaging invariants that must hold even on a maintainer's live machine."""

from fnmatch import fnmatchcase
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _table_strings(text: str, heading: str) -> list[str]:
    body = text.split(heading, 1)[1].split("\n[", 1)[0]
    body = "\n".join(line.split("#", 1)[0] for line in body.splitlines())
    return re.findall(r'"([^"]+)"', body)


def test_package_data_never_captures_browser_auth_and_ships_oauth_adapters():
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    patterns = _table_strings(config, "[tool.setuptools.package-data]")
    excluded = _table_strings(config, "[tool.setuptools.exclude-package-data]")

    assert "browser_ext/*" not in patterns
    assert not any(fnmatchcase("browser_ext/token.txt", pattern) for pattern in patterns)
    assert {"browser_ext/token.txt", "browser_ext/auth.js", "browser_ext/*.txt"} <= set(excluded)

    required = (
        "browser_ext/background.js",
        "browser_ext/manifest.json",
        "browser_ext/icon128.png",
        "oauth_ext/pi-oauth-proxy.js",
        "oauth_ext/opencode.jsonc",
    )
    for relative in required:
        assert (ROOT / "harness" / relative).is_file(), relative
        assert any(fnmatchcase(relative, pattern) for pattern in patterns), relative


def test_installer_upgrade_cleanup_is_targeted_to_owned_runtime_packages():
    iss = (ROOT / "installer" / "collie.iss").read_text(encoding="utf-8")
    section = iss.split("[InstallDelete]", 1)[1].split("[Icons]", 1)[0]
    delete_lines = [line.strip() for line in section.splitlines()
                    if line.lstrip().startswith("Type:")]

    assert len(delete_lines) == 4
    assert any("site-packages\\harness\"" in line for line in delete_lines)
    assert any("collie_harness-*.dist-info" in line for line in delete_lines)
    assert any("site-packages\\pip\"" in line for line in delete_lines)
    assert any("pip-*.dist-info" in line for line in delete_lines)
    assert all('Name: "{app}\\python\\Lib\\site-packages\\' in line
               for line in delete_lines)
    assert all(".collie" not in line.lower() and "{user" not in line.lower()
               for line in delete_lines)
    assert not any(line.endswith('site-packages\"') for line in delete_lines)


def test_payload_build_fails_closed_and_verifies_code_metadata_and_assets():
    script = (ROOT / "installer" / "build_payload.ps1").read_text(encoding="utf-8")

    for label in (
        'Assert-NativeExit "bootstrap pip"',
        'Assert-NativeExit "install payload build dependencies"',
        'Assert-NativeExit "install Collie into payload"',
        'Assert-NativeExit "verify bundled Collie"',
        'Assert-NativeExit "verify bundled pip"',
    ):
        assert label in script
    assert 'metadata.version("collie-harness")' in script
    assert "payload version mismatch" in script
    assert "exactly one Collie dist-info" in script
    assert "private browser credential leaked" in script
    assert '("browser_ext/token.txt", "browser_ext/auth.js")' in script
    assert "collie-payload-verify-" in script
    assert "Set-Content -LiteralPath $verifyPath" in script
    assert '$ver = & (Join-Path $py "python.exe") -c $verify' not in script
    assert '"harness.supervisor", "harness.automations", "harness.ops"' in script
    assert "refusing to remove a path outside the payload runtime" in script
    assert "refusing to remove a non-generated repository path" in script
    assert 'Remove-RepoBuildArtifact (Join-Path $repo "build")' in script
    assert 'Remove-RepoBuildArtifact (Join-Path $repo "collie_harness.egg-info")' in script


def test_top_level_installer_build_checks_every_native_generator():
    script = (ROOT / "installer" / "build.ps1").read_text(encoding="utf-8")
    assert "branding-art generation failed" in script
    assert "language-data generation failed" in script
    assert 'if ($LASTEXITCODE -ne 0) { throw "iscc failed" }' in script


def test_installer_has_one_owner_for_slack_recovery():
    iss = (ROOT / "installer" / "collie.iss").read_text(encoding="utf-8")
    assert "harness.supervisor run" in iss
    assert "glob.glob(os.path.expanduser('~/.collie/slack-*.pyw'))" not in iss
    assert "Slack listeners are discovered and adopted by the supervisor" in iss
