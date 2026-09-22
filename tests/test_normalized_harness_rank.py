import datetime as dt
import hashlib
import json
import io
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest


NOW = dt.datetime(2026, 8, 12, 20, 0, tzinfo=dt.timezone.utc)


def _canonical_sha(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def test_archive_source_hashes_use_exported_bytes_not_worktree_bytes():
    from bench.normalized_harness_rank import _hash_archive_members

    raw = io.BytesIO()
    payloads = {"a.py": b"one\r\ntwo\r\n", "b.txt": b"opaque\x00bytes"}
    with tarfile.open(fileobj=raw, mode="w:") as bundle:
        for name, payload in payloads.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            bundle.addfile(member, io.BytesIO(payload))

    hashes = _hash_archive_members(raw.getvalue(), tuple(payloads))

    assert hashes == {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in payloads.items()
    }
    assert hashes["a.py"] != hashlib.sha256(
        payloads["a.py"].replace(b"\r\n", b"\n")).hexdigest()


def test_schedule_admits_every_arm_and_rotates_four_arm_ranking():
    from bench.normalized_harness_rank import ARMS, canonical_plan

    admission = canonical_plan(99, admission=True)
    plan = canonical_plan(3)

    assert [row["arm"] for row in admission] == list(ARMS)
    assert len(admission) == 4
    assert len(plan) == 24
    assert len({row["run_id"] for row in plan}) == 24
    assert all(row["attempt"] == 1 and row["phase"] == "ranking" for row in plan)
    assert [row["arm"] for row in plan[:12]] == [
        "collie", "prime", "pi", "hermes",
        "prime", "pi", "hermes", "collie",
        "pi", "hermes", "collie", "prime",
    ]
    assert {arm: sum(row["position"] == 1 and row["arm"] == arm for row in plan)
            for arm in ARMS} == {
            "collie": 2, "prime": 2, "pi": 1, "hermes": 1,
        }

    balanced = canonical_plan()
    assert len(balanced) == 32
    for arm in ARMS:
        assert [sum(row["position"] == position and row["arm"] == arm
                    for row in balanced) for position in range(1, 5)] == [2, 2, 2, 2]


def test_agent_command_uses_only_internal_network_and_no_credential_mount(tmp_path):
    from bench.normalized_harness_rank import _agent_create_command

    paths = [tmp_path / name for name in ("work", "input", "output", "state")]
    for path in paths:
        path.mkdir()
    command = _agent_create_command(
        "agent-image", "agent-name", "internal-net", {"arm": "pi"},
        paths[0], paths[1], paths[2], paths[3])

    assert command[:5] == ["docker", "create", "--name", "agent-name", "--init"]
    assert command[command.index("--network") + 1] == "internal-net"
    assert "bridge" not in command
    assert not any("credential" in value.lower() for value in command)
    assert "http://inference:8765/v1" in command


def test_sidecar_command_mounts_credential_and_ledger_without_host_port(tmp_path):
    from bench.normalized_harness_rank import _sidecar_command

    credential = tmp_path / "credentials.json"
    credential.write_text("opaque", encoding="utf-8")
    ledger = tmp_path / "ledger"
    ledger.mkdir()

    command = _sidecar_command(
        "sidecar-image", "inference", "internal-net", credential, ledger)

    assert command[command.index("--network") + 1] == "internal-net"
    assert "/home/runner/.claude/.credentials.json" in " ".join(command)
    assert "/ledger" in command
    assert any(value.startswith("/home/runner/.claude:rw,nosuid,noexec")
               for value in command)
    assert command[command.index("--max-requests") + 1] == "12"
    assert "-p" not in command and "--publish" not in command
    assert command[-1] == "--allow-private-peers"


def test_network_attestation_rejects_agent_on_external_bridge(monkeypatch):
    from bench import normalized_harness_rank as rank

    monkeypatch.setattr(rank, "_docker_inspect", lambda name: {
        "Mounts": [
            {"Destination": "/workspace", "RW": True},
            {"Destination": "/input", "RW": False},
            {"Destination": "/output", "RW": True},
            {"Destination": "/state", "RW": True},
        ],
        "NetworkSettings": {"Networks": {"internal": {}, "egress": {}}},
        "Config": {"Env": []},
    })

    with pytest.raises(RuntimeError, match="network attachment"):
        rank._attest_agent("agent", "internal")


def test_network_attestation_rejects_sidecar_host_port(monkeypatch):
    from bench import normalized_harness_rank as rank

    monkeypatch.setattr(rank, "_docker_inspect", lambda name: {
        "Mounts": [
            {"Destination": "/home/runner/.claude/.credentials.json", "RW": False},
            {"Destination": "/ledger", "RW": True},
        ],
        "NetworkSettings": {"Networks": {"internal": {}, "egress": {}}},
        "HostConfig": {
            "PortBindings": {"8765/tcp": [{"HostPort": "8765"}]},
            "Tmpfs": {"/home/runner/.claude": (
                "rw,nosuid,noexec,uid=10001,gid=10001,mode=0700,size=67108864")},
        },
    })

    with pytest.raises(RuntimeError, match="host port"):
        rank._attest_sidecar("sidecar", "internal", "egress")


def test_sidecar_attestation_requires_isolated_writable_claude_home(monkeypatch):
    from bench import normalized_harness_rank as rank

    base = {
        "Mounts": [
            {"Destination": "/home/runner/.claude/.credentials.json", "RW": False},
            {"Destination": "/ledger", "RW": True},
        ],
        "NetworkSettings": {"Networks": {"internal": {}, "egress": {}}},
        "HostConfig": {"PortBindings": None, "Tmpfs": {}},
    }
    monkeypatch.setattr(rank, "_docker_inspect", lambda name: base)

    with pytest.raises(RuntimeError, match="Claude home"):
        rank._attest_sidecar("sidecar", "internal", "egress")

    base["HostConfig"]["Tmpfs"] = {
        "/home/runner/.claude": (
            "rw,nosuid,noexec,uid=10001,gid=10001,mode=0700,size=67108864")}
    assert rank._attest_sidecar(
        "sidecar", "internal", "egress")["host_ports_published"] is False


def test_attempt_network_must_be_internal_bridge(monkeypatch):
    from bench import normalized_harness_rank as rank

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps([{
            "Name": "attempt", "Driver": "bridge", "Internal": True,
        }]))

    monkeypatch.setattr(rank, "_run", fake_run)
    assert rank._attest_internal_network("attempt") == {
        "driver": "bridge", "internal": True}

    def external(*args, **kwargs):
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps([{
            "Name": "attempt", "Driver": "bridge", "Internal": False,
        }]))

    monkeypatch.setattr(rank, "_run", external)
    with pytest.raises(RuntimeError, match="internal bridge"):
        rank._attest_internal_network("attempt")


def test_attempt_egress_network_must_be_noninternal_bridge(monkeypatch):
    from bench import normalized_harness_rank as rank

    monkeypatch.setattr(rank, "_run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stderr="", stdout=json.dumps([{
            "Name": "egress", "Driver": "bridge", "Internal": False,
        }])))
    assert rank._attest_egress_network("egress") == {
        "driver": "bridge", "internal": False, "attempt_scoped": True}


def _ledger_event(event, request_id="req_1", **extra):
    base = {
        "schema_version": 1,
        "event": event,
        "request_id": request_id,
        "created_at_utc": "2026-08-12T20:00:00Z",
        "model": "claude-opus-4-8",
    }
    base.update(extra)
    return base


def _write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_sidecar_ledger_requires_reserved_settled_fixed_model_and_usage(tmp_path):
    from bench.normalized_harness_rank import _validate_sidecar_ledger

    _write_json(tmp_path / "0001.json", _ledger_event(
        "reserved", request_sha256="a" * 64, prompt_sha256="b" * 64,
        request_bytes=123))
    _write_json(tmp_path / "0002.json", _ledger_event(
        "settled", outcome="completed", duration_ms=20,
        usage={
            "input_tokens": 11, "output_tokens": 5,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2,
        }))

    result = _validate_sidecar_ledger(tmp_path)

    assert result["physical_requests"] == 1
    assert result["reserved_requests"] == result["settled_requests"] == 1
    assert result["outcomes"] == {"completed": 1}
    assert result["usage"]["input_tokens"] == 11
    assert len(result["ledger_sha256"]) == 64


@pytest.mark.parametrize("mutation", [
    "missing_settlement", "wrong_model", "missing_usage", "transport_error"])
def test_sidecar_ledger_fails_closed_on_incomplete_or_rerouted_evidence(tmp_path,
                                                                       mutation):
    from bench.normalized_harness_rank import _validate_sidecar_ledger

    reserved = _ledger_event(
        "reserved", request_sha256="a" * 64, prompt_sha256="b" * 64,
        request_bytes=123)
    settled = _ledger_event(
        "settled", outcome="completed", duration_ms=20,
        usage={
            "input_tokens": 1, "output_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        })
    if mutation == "wrong_model":
        reserved["model"] = "another-model"
    if mutation == "missing_usage":
        settled.pop("usage")
    if mutation == "transport_error":
        settled["outcome"] = "error"
        settled["error_code"] = "transport_error"
    _write_json(tmp_path / "0001.json", reserved)
    if mutation != "missing_settlement":
        _write_json(tmp_path / "0002.json", settled)

    with pytest.raises(RuntimeError):
        _validate_sidecar_ledger(tmp_path)


def test_sidecar_ledger_rejects_zero_usage_even_with_completed_settlement(tmp_path):
    from bench.normalized_harness_rank import _validate_sidecar_ledger

    _write_json(tmp_path / "0001.json", _ledger_event(
        "reserved", request_sha256="a" * 64, prompt_sha256="b" * 64,
        request_bytes=123))
    _write_json(tmp_path / "0002.json", _ledger_event(
        "settled", outcome="completed", duration_ms=20,
        usage={
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }))

    with pytest.raises(RuntimeError, match="usage evidence"):
        _validate_sidecar_ledger(tmp_path)


def test_worker_receipt_binds_arm_model_prompt_patch_and_terminal():
    from bench.normalized_harness_rank import _validate_worker_receipt

    prompt_sha = "c" * 64
    task = {
        "run_id": "run-1", "task_id": "task-1",
        "delivered_prompt_sha256": prompt_sha,
    }
    worker = {
        "worker_outcome": "candidate", "run_id": "run-1", "task_id": "task-1",
        "arm": "hermes", "model": "claude-opus-4-8",
        "delivered_prompt_sha256": prompt_sha,
        "patch": "diff --git a/x b/x\n", "duration_ms": 3,
        "usage": {}, "runtime": {
            "product": "hermes-agent", "model": "claude-opus-4-8"},
        "tool_evidence": {
            "terminal_observed": True,
            "native_tool_calls": 2,
            "native_edit_calls": 1,
        },
    }

    evidence = _validate_worker_receipt(
        worker, task, "hermes", "diff --git a/x b/x\n")
    assert evidence == {
        "native_tool_calls": 2, "native_edit_calls": 1,
        "terminal_observed": True,
    }

    worker["patch"] = "different"
    assert _validate_worker_receipt(worker, task, "hermes", "expected") == {
        "native_tool_calls": 2, "native_edit_calls": 1,
        "terminal_observed": True,
    }


def test_admission_proves_completed_terminal_receipt_not_behavior():
    from bench.normalized_harness_rank import _admission_capability_proven

    assert _admission_capability_proven(
        {"native_tool_calls": 2, "native_edit_calls": 1,
         "terminal_observed": True}, "patch")
    assert _admission_capability_proven(
        {"native_tool_calls": 2, "native_edit_calls": 0,
         "terminal_observed": True}, "")
    assert _admission_capability_proven(
        {"native_tool_calls": 0, "native_edit_calls": 0,
         "terminal_observed": True}, "")
    assert not _admission_capability_proven(
        {"native_tool_calls": 2, "native_edit_calls": 1,
         "terminal_observed": False}, "patch")


def test_external_patch_ignores_agent_git_head_and_uses_pristine_snapshot(tmp_path):
    from bench.normalized_harness_rank import _external_patch

    baseline = tmp_path / "baseline"
    workspace = tmp_path / "workspace"
    baseline.mkdir()
    workspace.mkdir()
    (baseline / "kept.py").write_text("value = 1\n", encoding="utf-8")
    (baseline / "deleted.py").write_text("gone = False\n", encoding="utf-8")
    (workspace / "kept.py").write_text("value = 2\n", encoding="utf-8")
    (workspace / "added.py").write_text("added = True\n", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "HEAD").write_text(
        "ref: refs/heads/agent-rewritten\n", encoding="utf-8")

    patch = _external_patch(workspace, baseline)

    assert "+value = 2" in patch
    assert "+added = True" in patch
    assert "deleted.py" in patch
    assert ".git" not in patch


def test_hidden_grader_requires_trusted_success_marker(tmp_path):
    from bench.normalized_harness_rank import _grade

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "candidate.py").write_text(
        "raise SystemExit(0)\n", encoding="utf-8")
    task = {
        "task_id": "marker-test",
        "fixture_files": {"candidate.py": "raise SystemExit(0)\n"},
        "gold_files": {},
        "hidden_grader": "import candidate\nraise AssertionError('must continue')\n",
    }

    result = _grade(task, workspace, "a" * 64)

    assert result["outcome"] == "graded"
    assert result["resolved"] is False
    assert result["success_marker_verified"] is False


def test_ranking_allows_zero_tool_and_empty_patch_as_valid_unresolved(monkeypatch,
                                                                      tmp_path):
    from bench import normalized_harness_rank as rank

    task = rank.task_by_id(rank.TASKS[0]["task_id"])
    row = rank.canonical_plan(1)[0]
    suite = "e" * 64
    result_root = tmp_path / "results"
    suite_temp = tmp_path / "temp"
    result_root.mkdir()
    suite_temp.mkdir()
    credential = tmp_path / "credential"
    credential.write_text("opaque", encoding="utf-8")

    monkeypatch.setattr(rank, "_run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="0\n", stderr=""))
    monkeypatch.setattr(rank, "_attest_sidecar", lambda *args: {})
    monkeypatch.setattr(rank, "_attest_agent", lambda *args: {})
    monkeypatch.setattr(rank, "_attest_internal_network", lambda *args: {
        "driver": "bridge", "internal": True})
    monkeypatch.setattr(rank, "_attest_egress_network", lambda *args: {
        "driver": "bridge", "internal": False, "attempt_scoped": True})
    monkeypatch.setattr(rank, "_wait_sidecar", lambda *args: None)
    monkeypatch.setattr(rank, "_remove_container", lambda *args: True)
    monkeypatch.setattr(rank, "_remove_network", lambda *args: True)
    monkeypatch.setattr(rank, "_docker_inspect", lambda *args: {
        "State": {"Running": True}})
    monkeypatch.setattr(rank, "_external_patch", lambda workspace, baseline: "")
    monkeypatch.setattr(rank, "_validate_sidecar_ledger", lambda directory: {
        "model": rank.MODEL, "physical_requests": 1,
        "reserved_requests": 1, "settled_requests": 1,
        "outcomes": {"completed": 1}, "usage": {},
        "ledger_sha256": "a" * 64,
    })
    monkeypatch.setattr(rank, "_prepare_git_fixture", lambda *args: (
        "a" * 40, "b" * 40))
    monkeypatch.setattr(rank, "_grade", lambda *args: {
        "outcome": "graded", "resolved": False,
        "patch_sha256": hashlib.sha256(b"").hexdigest(),
    })

    def fake_validate(worker, worker_task, arm, patch):
        return {"native_tool_calls": 0, "native_edit_calls": 0,
                "terminal_observed": True}

    monkeypatch.setattr(rank, "_validate_worker_receipt", fake_validate)
    original_load = rank._load_json

    def fake_load(path):
        if path.name == "worker.json":
            return {
                "worker_outcome": "candidate",
                "duration_ms": 1, "usage": {},
                "runtime": {"product": row["arm"], "model": rank.MODEL},
                "tool_evidence": {"terminal_observed": True},
            }
        return original_load(path)

    monkeypatch.setattr(rank, "_load_json", fake_load)
    monkeypatch.setattr(Path, "is_file", lambda self: (
        True if self.name == "worker.json" else Path.exists(self)))

    result = rank._run_one(
        "sidecar", "agent", suite, row, credential, suite_temp,
        result_root, 30)

    assert row["phase"] == "ranking"
    assert result["status"] == "valid_unresolved"
    assert result["patch_bytes"] == 0
    assert result["tool_evidence"]["native_tool_calls"] == 0


def test_product_failure_workspace_is_still_graded(monkeypatch, tmp_path):
    from bench import normalized_harness_rank as rank

    task = rank.task_by_id(rank.TASKS[0]["task_id"])
    row = rank.canonical_plan(1)[0]
    suite = "c" * 64
    result_root = tmp_path / "results"
    suite_temp = tmp_path / "temp"
    result_root.mkdir()
    suite_temp.mkdir()
    credential = tmp_path / "credential"
    credential.write_text("opaque", encoding="utf-8")

    monkeypatch.setattr(rank, "_run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="2\n", stderr=""))
    monkeypatch.setattr(rank, "_attest_sidecar", lambda *args: {})
    monkeypatch.setattr(rank, "_attest_agent", lambda *args: {})
    monkeypatch.setattr(rank, "_attest_internal_network", lambda *args: {
        "driver": "bridge", "internal": True})
    monkeypatch.setattr(rank, "_attest_egress_network", lambda *args: {
        "driver": "bridge", "internal": False, "attempt_scoped": True})
    monkeypatch.setattr(rank, "_wait_sidecar", lambda *args: None)
    monkeypatch.setattr(rank, "_remove_container", lambda *args: True)
    monkeypatch.setattr(rank, "_remove_network", lambda *args: True)
    monkeypatch.setattr(rank, "_docker_inspect", lambda *args: {
        "State": {"Running": True}})
    monkeypatch.setattr(rank, "_external_patch", lambda *args: "a patch")
    monkeypatch.setattr(rank, "_validate_sidecar_ledger", lambda directory: {
        "model": rank.MODEL, "physical_requests": 1,
        "reserved_requests": 1, "settled_requests": 1,
        "outcomes": {"completed": 1}, "usage": {},
        "ledger_sha256": "a" * 64,
    })
    monkeypatch.setattr(rank, "_prepare_git_fixture", lambda *args: (
        "a" * 40, "b" * 40))
    grade_calls = []

    def fake_grade(task_value, workspace, patch_sha):
        grade_calls.append((task_value, workspace, patch_sha))
        return {"outcome": "graded", "resolved": True,
                "patch_sha256": patch_sha}

    monkeypatch.setattr(rank, "_grade", fake_grade)
    monkeypatch.setattr(rank, "_validate_worker_receipt", lambda *args: {
        "native_tool_calls": 1, "native_edit_calls": 1,
        "terminal_observed": True})
    original_load = rank._load_json

    def fake_load(path):
        if path.name == "worker.json":
            return {
                "worker_outcome": "product_failure",
                "error_code": "model_or_tool_error",
                "duration_ms": 1, "usage": {}, "patch": "a patch",
                "runtime": {"product": row["arm"], "model": rank.MODEL},
                "tool_evidence": {"terminal_observed": True},
            }
        return original_load(path)

    monkeypatch.setattr(rank, "_load_json", fake_load)
    monkeypatch.setattr(Path, "is_file", lambda self: (
        True if self.name == "worker.json" else Path.exists(self)))

    result = rank._run_one(
        "sidecar", "agent", suite, row, credential, suite_temp,
        result_root, 30)

    assert len(grade_calls) == 1
    assert result["status"] == "valid_resolved"
    assert result["resolved"] is True
    assert result["worker_outcome"] == "product_failure"
    assert result["worker_error_code"] == "model_or_tool_error"
    assert result["error_code"] == ""


def test_summary_is_adapted_nonpublishable_four_arm_ranking():
    from bench.normalized_harness_rank import ARMS, canonical_plan, summarize

    plan = canonical_plan(1)
    suite = "f" * 64
    rows = [{**row, "suite_sha256": suite, "status": "valid_unresolved",
             "resolved": False, "duration_ms": 10} for row in plan]
    result = summarize(plan, rows, suite)

    assert result["publishable"] is False
    assert result["comparison_label"] == (
        "adapted_harness_same_transport_not_native_product_ranking")
    assert result["ranking_withheld"] is False
    assert [item["arm"] for item in result["ranking"]] == sorted(ARMS)
    assert all(item["rank"] == 1 for item in result["ranking"])

    rows[0]["status"] = "invalid_infrastructure"
    invalid = summarize(plan, rows, suite)
    assert invalid["ranking_withheld"] is True
    assert invalid["ranking"] is None


def test_summary_withholds_ranking_until_claude_post_run_check():
    from bench.normalized_harness_rank import canonical_plan, summarize

    plan = canonical_plan(1)
    suite = "d" * 64
    rows = [{**row, "suite_sha256": suite, "status": "valid_resolved",
             "resolved": True, "duration_ms": 10} for row in plan]

    result = summarize(plan, rows, suite, require_post_run_billing=True)

    assert result["scores"] is not None
    assert result["ranking"] is None
    assert result["ranking_withheld"] is True
    assert result["ranking_withheld_reason"] == (
        "post_run_claude_billing_ui_recheck_pending")


def test_admission_summary_is_validation_only():
    from bench.normalized_harness_rank import canonical_plan, summarize_admission

    plan = canonical_plan(1, admission=True)
    suite = "b" * 64
    rows = [{**row, "suite_sha256": suite, "status": "valid_unresolved",
             "resolved": False, "duration_ms": 10} for row in plan]
    result = summarize_admission(plan, rows, suite)

    assert result["admitted"] is True
    assert result["scores"] is None
    assert result["ranking"] is None
    assert result["ranking_withheld_reason"] == "admission_is_not_scored"


def test_billing_requires_only_safe_claude_ui_evidence():
    from bench.normalized_harness_rank import _require_safe_claude_evidence

    safe = {
        "usage_credits_enabled": False,
        "auto_reload": False,
        "period_spend_usd": 0,
    }
    _require_safe_claude_evidence(safe, label="launch")

    for key, value in (
        ("usage_credits_enabled", True),
        ("auto_reload", True),
        ("period_spend_usd", 0.01),
    ):
        unsafe = dict(safe)
        unsafe[key] = value
        with pytest.raises(RuntimeError):
            _require_safe_claude_evidence(unsafe, label="launch")


def test_evidence_timestamp_must_be_recent_and_after_benchmark(monkeypatch):
    from bench import normalized_harness_rank as rank

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(rank.dt, "datetime", FrozenDateTime)
    assert rank._parse_recent_evidence_timestamp(
        "2026-08-12T19:59:00Z", label="launch") == "2026-08-12T19:59:00Z"

    for value, not_before in (
        ("2026-08-12T19:44:59Z", None),
        ("2026-08-12T20:02:00Z", None),
        ("2026-08-12T19:59:00Z", NOW),
    ):
        with pytest.raises(RuntimeError):
            rank._parse_recent_evidence_timestamp(
                value, label="evidence", not_before=not_before)


def test_same_delivered_prompt_hash_is_bound_for_all_four_arms():
    from bench.normalized_harness_rank import ARMS, SHARED_EVALUATOR_PROMPT, TASKS

    delivered = SHARED_EVALUATOR_PROMPT + TASKS[0]["prompt"]
    hashes = {arm: hashlib.sha256(delivered.encode("utf-8")).hexdigest()
              for arm in ARMS}
    assert len(set(hashes.values())) == 1
