import base64
import json
import threading

import pytest

from harness.oauth_owner import RefreshBusyError, RefreshOwner


def _jwt(exp):
    enc = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return "%s.%s.x" % (enc({"alg": "none"}), enc({"exp": exp}))


def test_refresh_owner_is_exclusive_and_os_released(tmp_path):
    credential = str(tmp_path / "auth.json")
    first = RefreshOwner(credential, timeout=.1).acquire()
    try:
        with pytest.raises(RefreshBusyError):
            RefreshOwner(credential, timeout=.02, poll_interval=.005).acquire()
    finally:
        first.close()
    with RefreshOwner(credential, timeout=.1):
        pass


def test_codex_concurrent_near_expiry_has_one_refresh_owner(tmp_path, monkeypatch):
    from harness import codex_oauth

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    expired = _jwt(1)
    refreshed = _jwt(4_000_000_000)
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {
        "access_token": expired, "refresh_token": "rotate-me", "account_id": "acct",
    }}), encoding="utf-8")
    calls = []
    barrier = threading.Barrier(2)

    def refresh(token):
        calls.append(token)
        return {"access_token": refreshed, "refresh_token": "new-refresh"}

    monkeypatch.setattr(codex_oauth, "_refresh", refresh)
    results = []
    def run():
        barrier.wait()
        results.append(codex_oauth._fresh_access_token()[0])
    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [refreshed, refreshed]
    assert calls == ["rotate-me"]
    saved = json.loads((tmp_path / "auth.json").read_text(encoding="utf-8"))
    assert saved["tokens"]["refresh_token"] == "new-refresh"
