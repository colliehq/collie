from __future__ import annotations

import json

import pytest

from harness import update


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.value).encode("utf-8")


def _release(tag, *, prerelease=False, draft=False):
    return {
        "tag_name": tag, "body": "notes", "html_url": "https://example.test/" + tag,
        "prerelease": prerelease, "draft": draft,
        "assets": [{"name": "collie.whl", "browser_download_url": "https://example.test/a",
                    "digest": "sha256:" + "a" * 64}],
    }


def test_stable_channel_uses_github_latest_endpoint(monkeypatch):
    seen = []
    monkeypatch.setattr(update.urllib.request, "urlopen", lambda request, timeout: (
        seen.append(request.full_url) or _Response(_release("v1.2.3"))))

    result = update.latest("stable")

    assert seen == [update.API_LATEST]
    assert result["tag"] == "v1.2.3"
    assert result["channel"] == "stable"
    assert result["prerelease"] is False
    assert result["digests"]["collie.whl"].startswith("sha256:")


def test_beta_channel_includes_prereleases_and_ignores_drafts(monkeypatch):
    releases = [
        _release("v1.4.0-beta.2", prerelease=True),
        _release("v9.0.0-beta.1", prerelease=True, draft=True),
        _release("v1.3.9"),
        _release("not-a-version"),
    ]
    seen = []
    monkeypatch.setattr(update.urllib.request, "urlopen", lambda request, timeout: (
        seen.append(request.full_url) or _Response(releases)))

    result = update.latest("beta")

    assert seen == [update.API_RELEASES]
    assert result["tag"] == "v1.4.0-beta.2"
    assert result["prerelease"] is True
    assert result["channel"] == "beta"


def test_beta_channel_still_advances_to_a_later_stable(monkeypatch):
    monkeypatch.setattr(update.urllib.request, "urlopen", lambda *_a, **_k: _Response([
        _release("v1.4.0-beta.2", prerelease=True), _release("v1.4.0")]))
    assert update.latest("beta")["tag"] == "v1.4.0"


def test_channel_validation_and_environment_default(monkeypatch):
    with pytest.raises(ValueError, match="stable or beta"):
        update.latest("nightly")
    monkeypatch.setenv("COLLIE_UPDATE_CHANNEL", "beta")
    monkeypatch.setattr(update.urllib.request, "urlopen", lambda *_a, **_k: _Response([
        _release("v1.0.0-beta.1", prerelease=True)]))
    assert update.latest()["channel"] == "beta"


def test_check_uses_semver_prerelease_precedence(monkeypatch):
    monkeypatch.setattr(update, "__version__", "1.2.3-beta.1")
    monkeypatch.setattr(update, "install_kind", lambda: "pip")
    monkeypatch.setattr(update, "latest", lambda channel=None: {
        **_release("v1.2.3"), "tag": "v1.2.3", "notes": "", "url": "",
        "channel": "beta", "prerelease": False, "assets": {}, "digests": {},
    })
    result = update.check("beta")
    assert result["newer"] is True
    assert result["channel"] == "beta"
