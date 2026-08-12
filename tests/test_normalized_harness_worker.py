from __future__ import annotations

import hashlib
import json

import pytest

from bench import normalized_harness_worker as worker
from harness.providers import Completion, Usage


def test_endpoint_accepts_only_the_internal_sidecar_route():
    assert worker._validated_endpoint("http://inference:8765/v1") == (
        "http://inference:8765/v1")
    for value in (
        "https://inference:8765/v1", "http://example.com:8765/v1",
        "http://inference:8765", "http://user@inference:8765/v1",
    ):
        with pytest.raises(RuntimeError):
            worker._validated_endpoint(value)


def test_execute_binds_identity_and_keeps_empty_patch_as_valid_unresolved(
        tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    run_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    prompt = "make the source change"
    task = {
        "arm": "pi", "model": worker.MODEL, "run_id": "run-1",
        "task_id": "task-1", "sidecar_bearer": worker.BEARER_SENTINEL,
        "delivered_prompt": prompt,
        "delivered_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "wall_seconds": 30,
    }
    monkeypatch.setattr(worker, "_run_prime_or_pi", lambda *args, **kwargs: {
        "worker_outcome": "candidate", "error_code": "", "usage": {},
        "tool_evidence": {
            "native_tool_calls": 0, "native_edit_calls": 0,
            "terminal_observed": True,
        },
        "runtime": {"product": "pi", "model": worker.MODEL},
    })
    monkeypatch.setattr(worker, "_collect_patch", lambda _workspace: ("", ""))

    result = worker.execute(
        task, workspace, run_dir, state_dir,
        "http://inference:8765/v1", 12)

    assert result["worker_outcome"] == "candidate"
    assert result["patch"] == ""
    assert result["run_id"] == "run-1"
    assert result["delivered_prompt_sha256"] == task["delivered_prompt_sha256"]
    assert result["tool_evidence"]["terminal_observed"] is True


def test_execute_fails_closed_on_bearer_mismatch(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    task = {
        "arm": "collie", "model": worker.MODEL,
        "sidecar_bearer": "wrong", "delivered_prompt": "x",
        "delivered_prompt_sha256": hashlib.sha256(b"x").hexdigest(),
        "wall_seconds": 30,
    }
    with pytest.raises(RuntimeError, match="bearer"):
        worker.execute(task, workspace, tmp_path / "run", tmp_path / "state",
                       "http://inference:8765/v1", 12)


def test_product_failures_are_scoreable_but_transport_failures_are_invalid():
    assert worker._worker_outcome_for_error("") == "candidate"
    assert worker._worker_outcome_for_error("harness_exit_nonzero") == (
        "product_failure")
    assert worker._worker_outcome_for_error("model_or_tool_error") == (
        "product_failure")
    assert worker._worker_outcome_for_error("sidecar_unavailable") == (
        "invalid_infrastructure")
    assert worker._worker_outcome_for_error("hermes_wall_timeout") == (
        "invalid_infrastructure")
    assert worker._safe_error(
        "HTTP 422: assistant response was not bridgeable") == (
        "response_contract_error")
    assert worker._worker_outcome_for_error("response_contract_error") == (
        "product_failure")


def test_collie_binds_module_level_data_to_attempt_state(tmp_path, monkeypatch):
    from harness import cli

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("sentinel", encoding="utf-8")
    monkeypatch.setattr(cli, "DATA", str(blocked))

    class FakeProvider:
        name = "normalized-subscription-sidecar"
        model = worker.MODEL
        reports_cache = False

        def __init__(self, *_args, **_kwargs):
            pass

        def complete(self, *_args, **_kwargs):
            return Completion(text="done", usage=Usage(), stop_reason="end_turn")

    monkeypatch.setattr(worker, "OpenAICompatProvider", FakeProvider)

    receipt = worker._run_collie(
        workspace, state_dir, "http://inference:8765/v1", "inspect", 1)

    assert receipt["worker_outcome"] == "candidate"
    assert (state_dir / "collie-data" / "runs.db").is_file()
    assert cli.DATA == str(blocked)


def test_main_writes_bounded_invalid_receipt(tmp_path):
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps({"arm": "collie"}), encoding="utf-8")
    output = tmp_path / "worker.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    code = worker.main([
        "--arm", "collie", "--task-json", str(task_path),
        "--workspace", str(workspace), "--run-dir", str(tmp_path / "run"),
        "--state-dir", str(tmp_path / "state"), "--output", str(output),
        "--endpoint", "http://inference:8765/v1", "--max-turns", "12",
    ])
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert code == 2
    assert receipt["worker_outcome"] == "invalid_infrastructure"
    encoded = json.dumps(receipt).lower()
    assert "make the source change" not in encoded
    assert "must-not-persist" not in encoded
