import json
from pathlib import Path

import pytest

from harness import settings


def _settings_at(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.delenv("COLLIE_COMPANION_NAME", raising=False)
    monkeypatch.setattr(settings, "_PATH", str(path))
    monkeypatch.setattr(settings, "_cache", {"mtime": -1.0, "data": {}})
    return path


def test_companion_name_is_unicode_safe_canonical_and_clearable(monkeypatch, tmp_path):
    path = _settings_at(monkeypatch, tmp_path)
    saved = settings.update({"COMPANION_NAME": "  小   云  "})
    assert saved["COMPANION_NAME"] == "小 云"
    assert json.loads(path.read_text(encoding="utf-8"))["COMPANION_NAME"] == "小 云"
    assert settings.get("COMPANION_NAME") == "小 云"

    settings.update({"COMPANION_NAME": ""})
    assert "COMPANION_NAME" not in json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("value", [
    "<img src=x onerror=alert(1)>",
    "A\u202eecilA",  # bidi override: visually misleading in device/audit rows
    "x" * 33,
    123,
])
def test_invalid_companion_name_is_rejected_without_clobbering_settings(monkeypatch, tmp_path, value):
    path = _settings_at(monkeypatch, tmp_path)
    settings.save({"LANG": "zh", "COMPANION_NAME": "Rowan"})
    before = path.read_bytes()
    with pytest.raises(ValueError):
        settings.update({"COMPANION_NAME": value})
    assert path.read_bytes() == before


def test_first_party_surfaces_share_versioned_transparent_identity_contract():
    root = Path(__file__).resolve().parents[1]
    index = (root / "harness/webui/index.html").read_text(encoding="utf-8")
    mobile = (root / "harness/webui/mobile.html").read_text(encoding="utf-8")
    remote = (root / "harness/webui/remote.html").read_text(encoding="utf-8")
    ambient = (root / "harness/webui/ambient.html").read_text(encoding="utf-8")
    webapp = (root / "harness/webapp.py").read_text(encoding="utf-8")

    assert 'id="nameOverlay"' in index and 'id="nameInput"' in index
    assert "Slack apps, @handles and mail addresses keep their existing identities" in index
    assert '["COMPANION_NAME"]' in index
    assert 'avatar.png(name, size=256, plate=False)' in webapp
    assert '"avatar": "/api/avatar.png?v=" + avatar_key' in webapp
    for page in (index, mobile, remote, ambient):
        assert "/api/whoami" in page
        assert ".avatar" in page
