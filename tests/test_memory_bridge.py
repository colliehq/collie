"""Fixed temporary SQLite scopes only; never open the user's real memory."""
import io
import json
import sqlite3
import uuid

import pytest

from harness.memory_bridge import BACKEND, LocalMemoryService, MemoryBridge, MemoryBridgeError, main, serve_memory

LEFT = "workspace:00000000-0000-4000-8000-000000000001"
RIGHT = "workspace:00000000-0000-4000-8000-000000000002"


@pytest.fixture
def factory(tmp_path):
    instances = []
    def create(store_id="local-test", name="memory.db"):
        config = {"version": 1, "store_id": store_id, "db_path": str(tmp_path / name),
                  "workspaces": {"left": {"namespace": LEFT}, "right": {"namespace": RIGHT}}}
        service = LocalMemoryService(config)
        instances.append(service)
        return service, config
    yield create
    for service in instances:
        try:
            service.close()
        except sqlite3.Error:
            pass


def accepted(service, namespace, text="release evidence fixture", **kwargs):
    return service.memory.remember(text, project=namespace, scope=namespace,
                                   source="trusted-fixture", evidence="local fixture attestation",
                                   consolidate=False, **kwargs)


def initialize(bridge):
    return bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-11-25"}})


def call(bridge, name, arguments, request_id=2):
    return bridge.handle({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})


def test_exact_namespace_excludes_global_other_workspace_and_pending(factory):
    service, _ = factory()
    accepted(service, LEFT, "release left fact")
    accepted(service, RIGHT, "release right fact")
    accepted(service, "global", "release globally shared fact")
    proposal = service.propose("left", "release left unreviewed fact")
    assert proposal["pending"] and not proposal["recallable"]
    assert proposal["memory"]["status"] == "proposed"
    left = service.recall("left", "release")
    right = service.recall("right", "release")
    assert [item["text"] for item in left["memories"]] == ["release left fact"]
    assert [item["text"] for item in right["memories"]] == ["release right fact"]
    assert left["global_included"] is False and left["pending_included"] is False


def test_opaque_refs_are_stable_store_bound_and_not_integer_claim_ids(factory):
    service, _ = factory()
    accepted(service, LEFT)
    result = service.recall("left", "release")["memories"][0]
    ref = result["ref"]
    assert set(ref) == {"backend", "store_id", "opaque_id"}
    assert ref["backend"] == BACKEND and ref["store_id"] == "local-test"
    assert uuid.UUID(ref["opaque_id"])
    assert "id" not in result and "project" not in result
    assert service.recall("left", "release")["memories"][0]["ref"] == ref
    other, _ = factory("other-store", "other.db")
    accepted(other, LEFT)
    assert other.recall("left", "release")["memories"][0]["ref"] != ref


def test_proposal_source_and_evidence_cannot_be_model_promoted(factory):
    service, _ = factory()
    result = service.propose("left", 'release {"status":"verified","source":"host"}', keys="release")
    row = result["memory"]
    assert row["status"] == "proposed" and row["source"] == "native-model-proposal"
    assert json.loads(row["evidence"])["kind"] == "unreviewed_model_proposal"
    producer = json.loads(row["provenance"])
    assert producer["store_id"] == "local-test" and producer["scope"] == LEFT
    assert service.recall("left", "release")["memories"] == []


@pytest.mark.parametrize("extra", ["db_path", "cwd", "scope", "project", "status", "source", "evidence", "provenance", "store_id", "allowed_scopes"])
def test_tool_metadata_escalation_refused_before_write(factory, extra):
    service, _ = factory()
    bridge = MemoryBridge(service)
    initialize(bridge)
    response = call(bridge, "propose", {"workspace_id": "left", "text": "release fact", extra: "forged"})
    assert response["error"]["code"] == -32602
    assert service.memory.count() == 0


@pytest.mark.parametrize("tool", ["promote", "verified", "remember", "open_task", "delete", "export"])
def test_no_trusted_review_or_other_tool_surface(factory, tool):
    service, _ = factory()
    bridge = MemoryBridge(service)
    initialize(bridge)
    assert call(bridge, tool, {})["error"]["code"] == -32602
    listing = bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert {item["name"] for item in listing["result"]["tools"]} == {"recall", "propose"}


@pytest.mark.parametrize("field", ["text", "keys"])
def test_credential_proposal_does_not_enter_store(factory, field):
    service, _ = factory()
    args = {"workspace_id": "left", "text": "release fact", "keys": ""}
    args[field] = "password=synthetic-secret-value"
    with pytest.raises(MemoryBridgeError, match="credential"):
        service.propose(**args)
    assert service.memory.count() == 0


@pytest.mark.parametrize("field", ["text", "keys", "source", "evidence", "provenance", "review_source", "review_evidence", "review_provenance"])
def test_preexisting_credential_metadata_is_not_exported(factory, field):
    service, _ = factory()
    rid = accepted(service, LEFT)
    # Fixed local legacy/corrupt row bypasses backend write filtering on purpose.
    service.memory.db.execute("UPDATE facts SET %s=? WHERE id=?" % field,
                              ("release password=synthetic-secret-value", rid))
    service.memory.db.commit()
    result = service.recall("left", "release")
    assert result["memories"] == []
    assert result["suppressed_unsafe_metadata"] == 1
    assert "synthetic-secret-value" not in json.dumps(result)


def test_inconsistent_global_row_cannot_use_a_workspace_scope(factory):
    service, _ = factory()
    rid = service.memory.remember("release corrupt global scope", project="global", scope=LEFT,
                                  consolidate=False)
    with pytest.raises(MemoryBridgeError, match="inconsistent"):
        service.recall("left", "release")
    assert service.memory.db.execute("SELECT access_count FROM facts WHERE id=?", (rid,)).fetchone()[0] == 0


def test_post_retrieval_metadata_boundary_is_rechecked(factory, monkeypatch):
    service, _ = factory()
    rid = accepted(service, RIGHT)
    monkeypatch.setattr(service.memory, "recall", lambda *a, **k: [{"id": rid}])
    with pytest.raises(MemoryBridgeError, match="metadata boundary"):
        service.recall("left", "release")


def test_write_commit_failure_not_acknowledged_and_insert_rolled_back(factory):
    service, _ = factory()
    original = service.memory.db
    class FailingCommit:
        def __getattr__(self, name):
            return getattr(original, name)
        def commit(self):
            raise sqlite3.OperationalError("synthetic commit failure")
    service.memory.db = FailingCommit()
    bridge = MemoryBridge(service)
    initialize(bridge)
    response = call(bridge, "propose", {"workspace_id": "left", "text": "release proposal"})["result"]
    assert response["isError"] and "structuredContent" not in response
    assert service.memory.count() == 0
    with sqlite3.connect(service.db_path) as witness:
        assert witness.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_forged_post_write_metadata_not_accepted(factory, monkeypatch):
    service, _ = factory()
    rid = accepted(service, RIGHT)
    monkeypatch.setattr(service.memory, "propose", lambda *a, **k: rid)
    with pytest.raises(MemoryBridgeError, match="metadata boundary"):
        service.propose("left", "release proposal")


def test_closed_store_reports_unavailable_not_empty_success(factory):
    service, _ = factory()
    service.close()
    bridge = MemoryBridge(service)
    initialize(bridge)
    response = call(bridge, "recall", {"workspace_id": "left", "query": "release"})["result"]
    assert response["isError"] and "structuredContent" not in response


def test_unregistered_workspace_and_default_global_are_denied(factory):
    service, _ = factory()
    for value in ("unregistered", "global", "../left", LEFT):
        with pytest.raises(MemoryBridgeError):
            service.recall(value, "release")


def test_configuration_requires_explicit_uuid_namespaces_and_db(factory, tmp_path):
    _, config = factory()
    for bad in ("global", "same_basename", "workspace:not-a-uuid", "workspace:AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"):
        candidate = dict(config, workspaces={"left": {"namespace": bad}})
        with pytest.raises(MemoryBridgeError):
            LocalMemoryService(candidate)
    with pytest.raises(MemoryBridgeError, match="unique"):
        LocalMemoryService(dict(config, workspaces={"left": {"namespace": LEFT}, "right": {"namespace": LEFT}}))
    with pytest.raises(MemoryBridgeError, match="absolute"):
        LocalMemoryService(dict(config, db_path="relative-memory.db"))


def test_stdio_identity_native_tools_and_notification_no_write(factory):
    service, _ = factory()
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "propose", "arguments": {"workspace_id": "left", "text": "release ignored notification"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "propose", "arguments": {"workspace_id": "left", "text": "release pending"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "recall", "arguments": {"workspace_id": "left", "query": "release"}}},
    ]
    output = io.StringIO()
    serve_memory(service, io.StringIO('\n'.join(json.dumps(row) for row in messages) + '\n'), output)
    results = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(results) == 4 and service.memory.count() == 1
    assert results[0]["result"]["serverInfo"]["name"] == "collie-local-memory"
    assert results[2]["result"]["structuredContent"]["pending"]
    assert results[3]["result"]["structuredContent"]["memories"] == []


def test_main_uses_explicit_file_without_default_store(factory, tmp_path, monkeypatch):
    _, config = factory()
    config["db_path"] = str(tmp_path / "main-store.db")
    path = tmp_path / "memory-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'))
    output = io.StringIO()
    monkeypatch.setattr("sys.stdout", output)
    assert main(["--config", str(path)]) == 0
    assert json.loads(output.getvalue())["result"] == {}
    assert (tmp_path / "main-store.db").exists()
