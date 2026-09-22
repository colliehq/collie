import base64
import json
import hashlib

import pytest

from harness.online import (AuthenticationRequired, ConnectionBrokerClient, DataClass,
                            DeviceEnrollmentClient, OnlineClient, OnlineStore,
                            export_seal_key, import_seal_key, new_device_enrollment)
from harness.mcpbroker import BrokerMCPTool, register_broker_connections
from harness.tools import ToolCtx, ToolRegistry


def connected(store):
    return store.connect(
        base_url="https://api.collie.test", user_id="u1", workspace_id="w1",
        device_id="d1", device_name="Laptop", access_token="access",
        refresh_token="refresh", access_expires_at=9999999999,
        refresh_expires_at=9999999999)


def test_local_mode_is_complete_and_does_not_require_profile(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db"))
    obj = store.put(object_id="m1", object_type="memory", project_id="p1",
                    data_class=DataClass.DEVICE_ONLY, content={"text": "private"})
    assert obj.content == {"text": "private"}
    assert store.pending() == []
    assert OnlineClient(store).sync_once() == {
        "mode": "local", "pushed": 0, "pulled": 0, "conflicts": 0}
    store.close()


def test_secret_never_enters_sync_store(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db"))
    with pytest.raises(ValueError, match="Vault"):
        store.put(object_id="secret", object_type="memory", project_id="p1",
                  data_class=DataClass.SECRET, content="token")
    store.close()


def test_cloud_and_sealed_objects_enter_idempotent_outbox(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    store.put(object_id="a", object_type="memory", project_id="p",
              data_class=DataClass.CLOUD_INDEXED, content={"text": "hello"})
    store.put(object_id="b", object_type="memory", project_id="p",
              data_class=DataClass.SEALED, content={"ciphertext": "opaque"})
    pending = store.pending()
    assert [row["object_id"] for row in pending] == ["a", "b"]
    assert len({row["operation_id"] for row in pending}) == 2
    assert pending[1]["content"]["alg"] == "A256GCM"
    assert "opaque" not in json.dumps(pending[1])
    store.close()


def test_push_ack_updates_version_and_removes_only_exact_operation(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    store.put(object_id="a", object_type="memory", project_id="p",
              data_class=DataClass.CLOUD_INDEXED, content={"n": 1})
    first = store.pending()[0]
    store.put(object_id="a", object_type="memory", project_id="p",
              data_class=DataClass.CLOUD_INDEXED, content={"n": 2})
    store.mark_pushed([{"operation_id": first["operation_id"], "object_id": "a", "version": 1}], [])
    assert len(store.pending()) == 1
    assert store.get("a").version == 1
    store.close()


def test_conflict_preserves_local_sibling_and_applies_server_version(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    store.put(object_id="a", object_type="memory", project_id="p",
              data_class=DataClass.CLOUD_INDEXED, content={"side": "local"})
    op = store.pending()[0]
    store.mark_pushed([], [{
        "operation_id": op["operation_id"],
        "server": {"object_id": "a", "object_type": "memory", "project_id": "p",
                   "data_class": "cloud_indexed", "content": {"side": "server"},
                   "version": 2, "updated_at": 10, "tombstone": False},
    }])
    siblings = store.list_objects(project_id="p", include_deleted=True)
    assert {x.conflict_of for x in siblings} == {"", "a"}
    assert store.get("a").content == {"side": "server"}
    store.close()


def test_pull_never_overwrites_dirty_local_object(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    store.put(object_id="a", object_type="memory", project_id="p",
              data_class=DataClass.SEALED, content={"local": True})
    result = store.apply_pull([{
        "object_id": "a", "object_type": "memory", "project_id": "p",
        "data_class": "sealed", "content": {"remote": True}, "version": 3,
        "updated_at": 20, "tombstone": False}], 5)
    assert result == {"applied": 0, "ignored": 1, "cursor": 5}
    assert store.get("a").content == {"local": True}
    with pytest.raises(ValueError, match="backwards"):
        store.apply_pull([], 4)
    store.close()


def test_disconnect_can_keep_or_forget_local_mirror(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    store.put(object_id="a", object_type="memory", project_id="p",
              data_class=DataClass.CLOUD_INDEXED, content={"x": 1})
    store.disconnect()
    assert store.get("a") is not None and not store.connected()
    with pytest.raises(AuthenticationRequired):
        store.apply_pull([], 0)
    connected(store); store.disconnect(forget_mirror=True)
    assert store.get("a") is None
    store.close()


def test_device_enrollment_uses_pkce_challenge_without_reusing_secret():
    first = new_device_enrollment("Laptop")
    second = new_device_enrollment("Laptop")
    assert first["challenge"] != first["verifier"]
    assert first["challenge"] != second["challenge"]
    assert len(first["nonce"]) == 32


def test_device_enrollment_client_keeps_verifier_local_and_finishes_profile(tmp_path):
    class Fake(DeviceEnrollmentClient):
        def __init__(self):
            self.base_url = "https://api.collie.test"; self.timeout = 1; self.calls = []
        def _post(self, path, value):
            self.calls.append((path, value))
            if path.endswith("/start"):
                return 201, {"device_code": "dc", "user_code": "ABCD-EFGH",
                             "verification_uri": "https://app.collie.test/devices"}
            return 200, {"user_id": "u", "workspace_id": "w", "device_id": "d",
                         "device_name": "Laptop", "access_token": "access-secret-7319",
                         "refresh_token": "refresh-secret-7319",
                         "access_expires_at": 10, "refresh_expires_at": 20}
    client = Fake(); enrollment = client.start("Laptop", public_key="pub")
    assert "verifier" not in client.calls[0][1]
    result = client.poll(enrollment)
    assert client.calls[1][1] == {"device_code": "dc", "verifier": enrollment["verifier"]}
    store = OnlineStore(str(tmp_path / "online.db"))
    profile = client.finish(store, enrollment, result)
    assert profile.device_id == "d" and store.tokens()["refresh_token"] == "refresh-secret-7319"
    assert "refresh-secret-7319" not in (tmp_path / "online.db").read_bytes().decode("latin1")
    assert json.loads((tmp_path / "online-credentials.json").read_text())["refresh_token"] == "refresh-secret-7319"
    store.close()


def test_workspace_switch_rotates_credentials_and_clears_only_public_catalogs(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    store.upsert_project("old-project", "Old")
    store.cache_connections([_connection()])
    store.put(object_id="local", object_type="memory", project_id="old-project",
              data_class=DataClass.DEVICE_ONLY, content={"kept": True})
    class Fake(OnlineClient):
        def _request(self, method, path, body=None, authenticated=True):
            assert (method, path, body) == (
                "POST", "/v1/workspaces/select", {"workspace_id": "team-2"})
            return {"workspace_id": "team-2", "access_token": "new-access",
                    "refresh_token": "new-refresh", "access_expires_at": 30,
                    "refresh_expires_at": 40}
    profile = Fake(store).switch_workspace("team-2")
    assert profile.workspace_id == "team-2"
    assert store.tokens()["refresh_token"] == "new-refresh"
    assert store.projects() == [] and store.connections() == []
    assert store.get("local").content == {"kept": True}
    store.close()


def _connection(effect="observe", allowed=("observe", "prepare", "act")):
    return {"id": "conn-12345678", "name": "Shared Mail", "status": "active",
            "manifest_digest": "digest", "reviewed_digest": "digest",
            "policy": {"allowed_effects": list(allowed)}, "manifest": [{
                "name": "send_mail", "effect": effect, "action": "send",
                "description": "Send mail", "input_schema": {"type": "object"},
                "annotations": {}}]}


def _direct_connection():
    row = _connection()
    raw = json.dumps(row["manifest"], ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    row.update(scope="personal", project_id="", transport="device_direct",
               endpoint="https://mcp.example.test/mcp", manifest_digest=digest,
               reviewed_digest=digest)
    return row


def test_connection_cache_and_registration_expose_reviewed_manifest_only(tmp_path):
    path = str(tmp_path / "online.db")
    store = OnlineStore(path); connected(store)
    store.cache_connections([_connection()]); store.close()
    registry = ToolRegistry()
    names = register_broker_connections(registry, store_path=path)
    assert names == ["mcp__online_shared_mail_conn1234__send_mail"]
    tool = registry.get(names[0])
    intent = tool._collie_intent({"to": "person@example.com"})
    assert intent.effect.value == "observe" and intent.connection_id == "conn-12345678"


def test_broker_tool_uses_call_id_and_one_time_grant_for_commit(tmp_path, monkeypatch):
    path = str(tmp_path / "online.db")
    store = OnlineStore(path); connected(store); store.close()
    calls = []
    monkeypatch.setattr(ConnectionBrokerClient, "grant", lambda self, cid, tool, args, **kw:
                        calls.append(("grant", cid, tool, args)) or {"grant_id": "g1"})
    monkeypatch.setattr(ConnectionBrokerClient, "invoke", lambda self, cid, tool, args, **kw:
                        calls.append(("invoke", cid, tool, args, kw)) or
                        {"result": {"content": [{"type": "text", "text": "sent"}]}})
    tool = BrokerMCPTool(_connection("commit"), _connection("commit")["manifest"][0],
                         store_path=path)
    ctx = ToolCtx(cwd=str(tmp_path), project="p", memory=None, tool_call_id="call-42")
    assert tool.run({"to": "person@example.com"}, ctx) == "sent"
    assert calls[0][0] == "grant"
    assert calls[1][4]["grant_id"] == "g1"
    assert len(calls[1][4]["idempotency_key"]) == 64
    first_key = calls[1][4]["idempotency_key"]
    calls.clear(); tool.run({"to": "person@example.com"}, ctx)
    assert calls[1][4]["idempotency_key"] == first_key


def test_broker_client_never_caches_credential_body(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    class Fake(OnlineClient):
        def _request(self, method, path, body=None, authenticated=True):
            assert path.endswith("/credential") and body == {"credential": {"access_token": "secret"}}
            return {"ok": True}
    assert ConnectionBrokerClient(Fake(store)).put_credential(
        "c1", {"access_token": "secret"}) == {"ok": True}
    assert "secret" not in (tmp_path / "online.db").read_bytes().decode("latin1")
    store.close()


def test_device_direct_credential_is_e2e_sealed_and_bound_to_reviewed_definition(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    connection = _direct_connection(); store.cache_connections([connection])
    class Fake(OnlineClient):
        sealed = None
        def _request(self, method, path, body=None, authenticated=True):
            if method == "POST":
                self.sealed = body["sealed_credential"]
                return {"ok": True, "credential": "e2e_ciphertext"}
            return {"sealed_credential": self.sealed}
    client = Fake(store); broker = ConnectionBrokerClient(client)
    broker.put_credential(connection["id"], {"access_token": "direct-secret"},
                          connection=connection)
    assert "direct-secret" not in json.dumps(client.sealed)
    assert broker.get_credential(connection["id"], connection=connection)["access_token"] == "direct-secret"
    registry = ToolRegistry()
    assert register_broker_connections(registry, store_path=str(tmp_path / "online.db")) == [
        "mcp__online_shared_mail_conn1234__send_mail"]
    changed = dict(connection, endpoint="https://evil.example.test/mcp")
    with pytest.raises(Exception, match="not bound"):
        broker.get_credential(connection["id"], connection=changed)
    changed_manifest = dict(connection, manifest=[dict(connection["manifest"][0], description="Injected")])
    with pytest.raises(Exception, match="digest is invalid"):
        broker.get_credential(connection["id"], connection=changed_manifest)
    store.cache_connections([changed_manifest])
    assert register_broker_connections(ToolRegistry(), store_path=str(tmp_path / "online.db")) == []
    store.close()


def test_device_direct_invoke_uses_local_replay_ledger_and_never_sends_token_to_online(tmp_path, monkeypatch):
    from harness import mcpclient
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    connection = _direct_connection(); store.cache_connections([connection])
    class Fake(OnlineClient):
        sealed = None
        def _request(self, method, path, body=None, authenticated=True):
            if method == "POST" and path.endswith("/credential"):
                self.sealed = body["sealed_credential"]
                return {"ok": True}
            if method == "GET" and path.endswith("/credential"):
                return {"sealed_credential": self.sealed}
            raise AssertionError((method, path, body))
    calls = []
    class Direct:
        def __init__(self, name, cfg):
            assert cfg["headers"]["Authorization"] == "Bearer direct-secret"
        def call_tool(self, tool, args):
            calls.append((tool, args)); return {"content": [{"type": "text", "text": "ok"}]}
        def close(self): pass
    monkeypatch.setattr(mcpclient, "_safe_oauth_url", lambda value: True)
    monkeypatch.setattr(mcpclient, "_HTTPConnection", Direct)
    client = Fake(store); broker = ConnectionBrokerClient(client)
    broker.put_credential(connection["id"], {"access_token": "direct-secret"}, connection=connection)
    first = broker.invoke(connection["id"], "send_mail", {"to": "a@example.com"},
                          idempotency_key="direct-call-123")
    second = broker.invoke(connection["id"], "send_mail", {"to": "a@example.com"},
                           idempotency_key="direct-call-123")
    assert first["state"] == "completed" and second["already_settled"] is True
    assert calls == [("send_mail", {"to": "a@example.com"})]
    assert "direct-secret" not in (tmp_path / "online.db").read_bytes().decode("latin1")
    store.close()


def test_publish_local_mcp_defaults_to_e2e_device_direct(tmp_path, monkeypatch):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    manifest = [{"name": "read", "effect": "observe", "action": "external_change",
                 "description": "Read", "input_schema": {}, "annotations": {}}]
    monkeypatch.setattr("harness.online.export_local_mcp_connection", lambda name, **kw: (
        {"name": name, "endpoint": "https://mcp.example.test/mcp", "manifest": manifest},
        {"access_token": "share-once"}))
    raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    row = {"id": "connection-direct", "name": "mail", "scope": "personal", "project_id": "",
           "transport": "device_direct", "endpoint": "https://mcp.example.test/mcp",
           "manifest": manifest, "manifest_digest": digest, "reviewed_digest": digest,
           "policy": {"allowed_effects": ["observe", "prepare", "act"]}, "status": "active"}
    class Fake(OnlineClient):
        def __init__(self, value): super().__init__(value); self.credential = None
        def _request(self, method, path, body=None, authenticated=True):
            if path == "/v1/connections" and method == "POST":
                assert body["transport"] == "device_direct"; return {"connection": dict(row, status="review_required", reviewed_digest="")}
            if path.endswith("/review"):
                return {"connection": row}
            if path.endswith("/credential"):
                self.credential = body["sealed_credential"]; return {"ok": True}
            if path == "/v1/connections" and method == "GET":
                return {"connections": [row]}
            raise AssertionError((method, path))
    client = Fake(store)
    assert ConnectionBrokerClient(client).publish_local_mcp("mail")["transport"] == "device_direct"
    assert "share-once" not in json.dumps(client.credential)
    store.close()


def test_sync_refreshes_project_and_connection_catalogs(tmp_path):
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    class Fake(OnlineClient):
        def _request(self, method, path, body=None, authenticated=True):
            if path.startswith("/v1/sync/pull"):
                return {"events": [], "cursor": 0}
            if path == "/v1/projects":
                return {"projects": [{"id": "p1", "name": "Shared", "role": "owner", "updated_at": 9}]}
            if path == "/v1/connections":
                return {"connections": [_connection()]}
            if path == "/v1/nodes":
                return {"nodes": [{"id": "d1", "name": "Laptop", "status": "online"}]}
            if path == "/v1/missions":
                return {"missions": [{"id": "m1", "goal": "Shared work", "state": "queued"}]}
            if path == "/v1/schedules":
                return {"schedules": [{"id": "s1", "name": "Daily", "cadence": "daily"}]}
            raise AssertionError(path)
    result = Fake(store).sync_once()
    assert result["projects"] == result["connections"] == 1
    assert result["nodes"] == result["missions"] == result["schedules"] == 1
    assert store.projects()[0]["project_id"] == "p1"
    assert store.connections()[0]["id"] == "conn-12345678"
    store.close()


def test_sealed_pull_requires_same_user_held_recovery_key(tmp_path):
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first = OnlineStore(str(first_dir / "online.db")); connected(first)
    first.put(object_id="sealed-1", object_type="memory", project_id="p",
              data_class=DataClass.SEALED, content={"private": "only devices"})
    event = dict(first.pending()[0], version=1)
    recovery = export_seal_key(str(first_dir / "online.db"))
    second = OnlineStore(str(second_dir / "online.db")); connected(second)
    with pytest.raises(Exception, match="recovery key"):
        second.apply_pull([event], 1)
    import_seal_key(recovery, str(second_dir / "online.db"))
    assert second.apply_pull([event], 1)["applied"] == 1
    assert second.get("sealed-1").content == {"private": "only devices"}
    assert "only devices" not in json.dumps(event)
    first.close(); second.close()


def test_online_requests_are_bound_to_the_device_key_and_exact_body(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from harness.online import _unb64, generate_device_key
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    seen = {}
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, _limit): return b'{"projects":[]}'
    def urlopen(request, timeout=0):
        seen["request"] = request
        return Response()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert OnlineClient(store)._request("POST", "/v1/test?q=1", {"x": 1}) == {"projects": []}
    request = seen["request"]
    timestamp = request.get_header("X-collie-device-timestamp")
    nonce = request.get_header("X-collie-device-nonce")
    body_hash = __import__("base64").urlsafe_b64encode(hashlib.sha256(request.data).digest()).decode().rstrip("=")
    message = "POST\n/v1/test?q=1\n%s\n%s\n%s" % (body_hash, timestamp, nonce)
    key = generate_device_key(str(tmp_path / "online-device-key.json"))
    Ed25519PublicKey.from_public_bytes(_unb64(key["public_key"])).verify(
        _unb64(request.get_header("X-collie-device-signature")), message.encode())
    store.close()
