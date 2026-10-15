"""Isolated receipts/authority/freshness fixtures; no native model or user apps."""
import io
import json
import os
import sys

import pytest

from harness.evidence_bridge import EvidenceBridge, main, serve
from harness.task_evidence_service import EvidenceConfigError, TaskEvidenceService, scope_snapshot
from harness.task_evidence_store import EvidenceStoreError, task_workspace_key


def configured(tmp_path, verifier=None):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
    command = '"%s" -c "print(7)"' % sys.executable.replace("\\", "/")
    config = {"version": 1, "workspaces": {"demo": {
        "root": str(root), "goal": "Implement the user-approved source change",
        "scope": ["source.py"], "checks": {"unit": {"command": command, "timeout_s": 20}}}}}
    service = TaskEvidenceService(config, str(tmp_path / "state"), verifier=verifier)
    return service, root, config


def passing(*args, **kwargs):
    return {"executed": True, "process_tree_terminated": True, "passed": True,
            "command_passed": True, "exit_code": 0, "freshness": "fresh", "source": "user_configured"}


def open_task(service):
    return service.open_task("demo", "unit")["task_id"]


def test_scope_handoff_fresh_stale_and_unobserved(tmp_path):
    service, root, _ = configured(tmp_path, passing)
    tid = open_task(service)
    assert service.prepare_handoff(tid)["freshness"] == "unknown"
    assert service.verify_task(tid)["receipt"]["passed"]
    bundle = service.prepare_handoff(tid)
    assert bundle["verified"] and bundle["freshness"] == "fresh"
    assert bundle["native_session_migration"] is False
    (root / "outside.txt").write_text("outside configured scope", encoding="utf-8")
    assert service.prepare_handoff(tid)["freshness"] == "fresh"
    (root / "source.py").write_text("VALUE = 2\n", encoding="utf-8")
    bundle = service.prepare_handoff(tid)
    assert bundle["freshness"] == "stale" and not bundle["verified"]
    assert bundle["verification"]["exit_code"] == 0


def test_check_changing_its_scope_cannot_certify(tmp_path):
    def edit(command, cwd, **kwargs):
        with open(os.path.join(cwd, "source.py"), "w", encoding="utf-8") as stream:
            stream.write("changed during check")
        return passing()
    service, _, _ = configured(tmp_path, edit)
    result = service.verify_task(open_task(service))
    assert result["receipt"]["freshness"] == "stale"
    assert not result["receipt"]["passed"]
    assert not result["recovery_required"]
    bundle = service.prepare_handoff(result["receipt"]["task_id"])
    assert not bundle["verified"] and bundle["freshness"] == "stale"


def test_unknown_check_blocks_new_tasks_in_same_workspace(tmp_path):
    calls = []
    def unknown(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("may have started")
    service, _, _ = configured(tmp_path, unknown)
    tid = open_task(service)
    result = service.verify_task(tid)
    assert result["accepted"] and result["recovery_required"]
    assert result["receipt"]["freshness"] == "unknown"
    other = open_task(service)
    with pytest.raises(EvidenceStoreError, match="unknown check effect"):
        service.verify_task(other)
    assert len(calls) == 1
    bundle = service.prepare_handoff(other)
    assert bundle["recovery_required"] and not bundle["verified"]


def test_receipt_write_failure_keeps_boundary_and_does_not_ack(tmp_path, monkeypatch):
    service, _, _ = configured(tmp_path, passing)
    tid = open_task(service)
    original = service.store._write
    def fail_receipt(key, obj):
        if obj["task_evidence"]["tasks"][tid]["receipts"]:
            raise EvidenceStoreError("simulated receipt fsync failure")
        return original(key, obj)
    monkeypatch.setattr(service.store, "_write", fail_receipt)
    with pytest.raises(EvidenceStoreError):
        service.verify_task(tid)
    assert service.store.recovery(task_workspace_key(tid))["recovery_required"]
    assert service.store.read_task(tid)["receipts"] == []


def test_boundary_write_failure_does_not_start_check(tmp_path, monkeypatch):
    calls = []
    service, _, _ = configured(tmp_path, lambda *a, **k: calls.append(a))
    tid = open_task(service)
    def fail(*a, **k):
        raise EvidenceStoreError("simulated boundary failure")
    monkeypatch.setattr(service.store, "_write", fail)
    with pytest.raises(EvidenceStoreError):
        service.verify_task(tid)
    assert calls == []


def test_handoff_persistence_failure_is_not_accepted(tmp_path, monkeypatch):
    service, _, _ = configured(tmp_path, passing)
    tid = open_task(service)
    service.verify_task(tid)
    def fail(*a, **k):
        raise EvidenceStoreError("handoff write failed")
    monkeypatch.setattr(service.store, "_write", fail)
    with pytest.raises(EvidenceStoreError):
        service.prepare_handoff(tid)
    assert service.store.read_task(tid)["handoffs"] == []


def test_configured_alias_cannot_bypass_physical_workspace_fence(tmp_path):
    service, _, _ = configured(tmp_path, lambda *a, **k: {
        "executed": True, "process_tree_terminated": False, "passed": False})
    service.registry["alias"] = service.registry["demo"]
    service.verify_task(open_task(service))
    tid = service.open_task("alias", "unit")["task_id"]
    with pytest.raises(EvidenceStoreError, match="unknown check effect"):
        service.verify_task(tid)


def test_fingerprint_detects_new_deleted_and_missing_scope_files(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    scope = ["later.py"]
    missing = scope_snapshot(str(root), scope)
    assert missing["complete"]
    (root / "later.py").write_text("new", encoding="utf-8")
    created = scope_snapshot(str(root), scope)
    assert created["complete"] and created["digest"] != missing["digest"]
    (root / "later.py").unlink()
    assert scope_snapshot(str(root), scope)["digest"] == missing["digest"]


def test_file_contents_cannot_impersonate_another_file_header(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    a, b = root / "a", root / "b"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    header = b"b\0" + str(b.stat().st_mode).encode("ascii") + b"\0"
    b.write_bytes(header + b"y")
    before = scope_snapshot(str(root), ["a", "b"])
    a.write_bytes(b"x" + header)
    b.write_bytes(b"y")
    after = scope_snapshot(str(root), ["a", "b"])
    assert before["complete"] and after["complete"]
    assert before["digest"] != after["digest"]


def test_registered_command_only_and_config_change_refuses(tmp_path):
    service, _, _ = configured(tmp_path, passing)
    with pytest.raises(EvidenceConfigError):
        service.open_task("unregistered", "unit")
    with pytest.raises(EvidenceConfigError):
        service.open_task("demo", "arbitrary-shell")
    tid = open_task(service)
    service.registry["demo"]["checks"]["unit"]["command"] = "changed by user config"
    with pytest.raises(EvidenceConfigError, match="registration changed"):
        service.verify_task(tid)


def test_invalid_scope_and_in_workspace_state_refused(tmp_path):
    _, root, config = configured(tmp_path, passing)
    with pytest.raises(EvidenceConfigError, match="outside"):
        TaskEvidenceService(config, str(root / "state"))
    config["workspaces"]["demo"]["scope"] = ["../escape"]
    with pytest.raises(EvidenceConfigError, match="traversal"):
        TaskEvidenceService(config, str(tmp_path / "other-state"))


def test_scope_link_is_unknown_and_not_executed(tmp_path):
    service, root, _ = configured(tmp_path, passing)
    outside = tmp_path / "outside.py"
    outside.write_text("outside", encoding="utf-8")
    (root / "source.py").unlink()
    try:
        (root / "source.py").symlink_to(outside)
    except OSError:
        pytest.skip("local platform cannot create symlinks")
    assert scope_snapshot(str(root), ["source.py"])["complete"] is False
    with pytest.raises(EvidenceStoreError, match="incomplete"):
        service.verify_task(open_task(service))


def test_incomplete_handoff_never_reports_verified(tmp_path, monkeypatch):
    service, _, _ = configured(tmp_path, passing)
    tid = open_task(service)
    service.verify_task(tid)
    from harness import task_evidence_service as module
    monkeypatch.setattr(module, "scope_snapshot", lambda *a: {"digest": "x", "complete": False})
    result = service.prepare_handoff(tid)
    assert result["freshness"] == "unknown" and not result["verified"]


def test_corrupt_journal_is_not_treated_as_empty(tmp_path):
    service, _, _ = configured(tmp_path, passing)
    tid = open_task(service)
    path = service.store._path(task_workspace_key(tid))
    with open(path, "w", encoding="utf-8") as stream:
        stream.write('{"active_run":')
    with pytest.raises(EvidenceStoreError):
        service.prepare_handoff(tid)


def test_workspace_lease_refuses_concurrent_check(tmp_path):
    service, _, _ = configured(tmp_path, passing)
    tid = open_task(service)
    key = task_workspace_key(tid)
    # A different thread, not a reentrant same-thread lease, must be refused.
    import concurrent.futures
    with service.store.hold(key):
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(service.verify_task, tid)
            with pytest.raises(Exception, match="being executed|running|executed"):
                future.result(timeout=5)


def test_stdio_protocol_rejects_forged_arguments_and_notifications(tmp_path):
    service, _, _ = configured(tmp_path, passing)
    incoming = io.StringIO('\n'.join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "open_task", "arguments": {"workspace_id": "demo", "check_id": "unit"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "open_task", "arguments": {"workspace_id": "demo", "check_id": "unit", "cwd": "forged", "command": "forged", "goal": "forged"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "open_task", "arguments": {"workspace_id": "demo", "check_id": "unit"}}}),
        '{"jsonrpc":"2.0","id":5,"id":6,"method":"ping"}',
    ]) + '\n')
    output = io.StringIO()
    serve(service, incoming, output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == 5
    assert len(responses[1]["result"]["tools"]) == 3
    assert responses[2]["error"]["code"] == -32602
    tid = responses[3]["result"]["structuredContent"]["task_id"]
    assert responses[4]["error"]["code"] == -32700
    key = task_workspace_key(tid)
    assert len(service.store._read(key)["task_evidence"]["tasks"]) == 1


def test_mcp_failed_receipt_is_error_without_acceptance(tmp_path, monkeypatch):
    service, _, _ = configured(tmp_path, passing)
    tid = open_task(service)
    bridge = EvidenceBridge(service)
    bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
    def fail(*a, **k):
        raise EvidenceStoreError("private command and path must not leak")
    monkeypatch.setattr(service.store, "finish", fail)
    response = bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "verify_task", "arguments": {"task_id": tid}}})
    result = response["result"]
    assert result["isError"] and "structuredContent" not in result
    assert "private command" not in result["content"][0]["text"]
    assert service.store.recovery(task_workspace_key(tid))["recovery_required"]


def test_core_verifier_real_local_process_receipt(tmp_path):
    # One tiny local Python process exercises the existing process-tree verifier.
    service, _, _ = configured(tmp_path)
    result = service.verify_task(open_task(service))
    receipt = result["receipt"]
    assert receipt["execution"]["executed"] is True
    assert receipt["execution"]["exit_code"] == 0
    assert receipt["execution"]["process_tree_terminated"] is True
    assert receipt["passed"] and not result["recovery_required"]


def test_file_configuration_explicit_state_and_main_args(tmp_path, monkeypatch):
    _, _, config = configured(tmp_path, passing)
    config["state_dir"] = str(tmp_path / "bridge-state")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    service = TaskEvidenceService.from_file(str(path))
    assert service.store.directory.startswith(config["state_dir"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n'))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert main(["--config", str(path)]) == 0
    assert json.loads(sys.stdout.getvalue())["result"] == {}
