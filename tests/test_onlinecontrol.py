import json
import time

from harness.online import OnlineStore
from harness.onlinecontrol import create_project, snapshot


def test_snapshot_is_truthful_in_local_mode_and_never_returns_tokens(tmp_path):
    value = snapshot(str(tmp_path))
    assert value["mode"] == "local" and value["cloud_llm_default"] == "off"
    assert "token" not in json.dumps(value).casefold()


def test_snapshot_exposes_public_identity_but_not_session_secrets(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db"))
    store.connect(base_url="https://api.collie.test", user_id="u", workspace_id="w",
                  device_id="d", device_name="Laptop", access_token="top-secret",
                  refresh_token="refresh-secret", access_expires_at=10, refresh_expires_at=20)
    store.close()
    value = snapshot(str(tmp_path))
    assert value["mode"] == "connected" and value["device"]["name"] == "Laptop"
    assert value["trusted_devices"][0]["device_id"] == "d"
    assert value["trusted_devices"][0]["source"] == "this_device"
    assert "secret" not in json.dumps(value)


def test_snapshot_only_exposes_unexpired_public_pairing_fields(tmp_path):
    (tmp_path / "online-pairing.json").write_text(json.dumps({
        "user_code": "ABCD-EFGH", "verification_uri_complete": "https://app/devices?code=x",
        "verifier": "must-not-leak", "expires_at": int(time.time()) + 60}), encoding="utf-8")
    value = snapshot(str(tmp_path))
    assert value["pairing"]["user_code"] == "ABCD-EFGH"
    assert "verifier" not in value["pairing"]


def test_first_project_is_created_and_bound_in_one_explicit_action(tmp_path, monkeypatch):
    store = OnlineStore(str(tmp_path / "online.db"))
    store.connect(base_url="https://api.collie.test", user_id="u", workspace_id="w",
                  device_id="d", device_name="Laptop", access_token="access",
                  refresh_token="refresh", access_expires_at=10, refresh_expires_at=20)
    store.close()

    def fake_create(client, name):
        return client.store.upsert_project("project-one", name, role="owner")

    monkeypatch.setattr("harness.online.OnlineClient.create_project", fake_create)
    value = create_project(str(tmp_path), name="Collie", memory_data_class="sealed")

    assert value["project"]["project_id"] == "project-one"
    assert value["binding"]["local_project"] == "Collie"
    assert value["binding"]["memory_data_class"] == "sealed"
    assert value["online"]["project_bindings"][0]["project_id"] == "project-one"
