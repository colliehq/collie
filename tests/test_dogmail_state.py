"""Mail identities and task polling must respect the active profile."""
import json

import pytest

from harness import dogmail


def test_mail_uses_active_state_and_preserves_explicit_store(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    monkeypatch.setenv("COLLIE_STATE_DIR", str(profile))
    monkeypatch.setattr(dogmail, "STORE", dogmail._DEFAULT_STORE)
    dogmail.save({"dogs": {"collie": {"address": "collie.owner@example.test"}}})
    assert (profile / "mail.json").exists()
    assert dogmail.load()["dogs"]["collie"]["address"] == "collie.owner@example.test"
    legacy = tmp_path / "legacy.json"
    monkeypatch.setattr(dogmail, "STORE", str(legacy))
    dogmail.save({"source": "legacy"})
    assert dogmail.load() == {"source": "legacy"}
    assert dogmail.load(str(profile))["dogs"]


def test_verified_identity_is_not_overwritten_by_new_claim(tmp_path, monkeypatch):
    original = {"handle": {"verified": True, "name": "owner", "priv": "existing-key"}}
    dogmail.save(original, str(tmp_path))
    monkeypatch.setattr(dogmail, "_post", lambda *a, **k: pytest.fail("must not send a new claim"))
    result = dogmail.claim_handle("new-name", "other@example.test", state_dir=str(tmp_path))
    assert result["ok"] is False
    assert dogmail.load(str(tmp_path)) == original


def _mail_fixture(tmp_path, monkeypatch):
    dogmail.save({"dogs": {"collie": {"address": "collie.owner@example.test",
                                     "priv": dogmail.b64(b"private"), "cursor": 4}}}, str(tmp_path))
    monkeypatch.setattr(dogmail, "relay_public", lambda *a, **k: b"public")
    monkeypatch.setattr(dogmail, "_signed_headers", lambda *a: {})
    monkeypatch.setattr(dogmail, "open_from_relay", lambda *a: b'{"text":"work request"}')
    monkeypatch.setattr(dogmail, "_get", lambda *a, **k: {"ok": True, "messages": [
        {"at": 7, "env": {"cipher": "sealed-message"}}]})


def test_task_poll_can_replay_without_advancing_verification_cursor(tmp_path, monkeypatch):
    _mail_fixture(tmp_path, monkeypatch)
    first = dogmail.fetch(state_dir=str(tmp_path), since=0, advance_cursor=False)
    second = dogmail.fetch(state_dir=str(tmp_path), since=0, advance_cursor=False)
    assert first == second
    assert len(first[0]["id"]) == 64
    assert dogmail.load(str(tmp_path))["dogs"]["collie"]["cursor"] == 4
    dogmail.fetch(state_dir=str(tmp_path))
    assert dogmail.load(str(tmp_path))["dogs"]["collie"]["cursor"] == 7


def test_failed_read_does_not_look_like_an_empty_inbox(tmp_path, monkeypatch):
    _mail_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(dogmail, "_get", lambda *a, **k: {"ok": False, "status": 503})
    with pytest.raises(RuntimeError, match="503"):
        dogmail.fetch(state_dir=str(tmp_path))
    assert dogmail.load(str(tmp_path))["dogs"]["collie"]["cursor"] == 4


def test_undecryptable_mail_does_not_advance_past_unread_input(tmp_path, monkeypatch):
    _mail_fixture(tmp_path, monkeypatch)
    def broken(*args):
        raise ValueError("sealed input failed")
    monkeypatch.setattr(dogmail, "open_from_relay", broken)
    assert dogmail.fetch(state_dir=str(tmp_path))[0]["error"]
    assert dogmail.load(str(tmp_path))["dogs"]["collie"]["cursor"] == 4


def test_new_address_keeps_the_relay_key_pinned(tmp_path, monkeypatch):
    dogmail.save({"handle": {"verified": True, "name": "owner", "priv": dogmail.b64(b"handle-key")}}, str(tmp_path))
    monkeypatch.setattr(dogmail.e2e, "keypair", lambda: (b"private", b"public"))
    monkeypatch.setattr(dogmail, "_get", lambda *a, **k: {"pub": dogmail.b64(b"relay-key")})
    monkeypatch.setattr(dogmail, "cert_tag", lambda *a: b"tag")
    monkeypatch.setattr(dogmail, "_post", lambda *a, **k: {"ok": True})
    assert dogmail.claim_dog("collie", state_dir=str(tmp_path))["ok"]
    assert dogmail.load(str(tmp_path))["relay_pub"] == dogmail.b64(b"relay-key")
