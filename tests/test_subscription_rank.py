import json
from pathlib import Path
import subprocess
from types import SimpleNamespace


def test_frozen_rank_tasks_have_red_baselines_and_green_gold():
    from bench.subscription_rank_tasks import TASKS, self_check, task_sha256

    digests = self_check()

    assert len(TASKS) == 2
    assert digests == {task["task_id"]: task_sha256(task) for task in TASKS}


def test_claude_turn_ceiling_is_a_candidate_not_infrastructure(monkeypatch, tmp_path):
    from bench import subscription_rank_worker as worker

    payload = {
        "is_error": True,
        "subtype": "error_max_turns",
        "result": "stopped",
        "usage": {"input_tokens": 11, "output_tokens": 7},
        "num_turns": 12,
    }
    completed = subprocess.CompletedProcess(["claude"], 1, json.dumps(payload), "")
    monkeypatch.setattr(worker.swe, "predict_claude_code", lambda *a, **k: completed)
    monkeypatch.setattr(worker, "_patch", lambda workspace: ("diff --git a/x b/x\n", ""))

    row = worker._claude(Path(tmp_path), "prompt", "opus", 12)

    assert row["worker_outcome"] == "candidate"
    assert row["turns_exhausted"] is True
    assert row["error_code"] == ""


def test_claude_missing_usage_is_infrastructure_invalid(monkeypatch, tmp_path):
    from bench import subscription_rank_worker as worker

    payload = {"is_error": False, "result": "done", "usage": {}}
    completed = subprocess.CompletedProcess(["claude"], 0, json.dumps(payload), "")
    monkeypatch.setattr(worker.swe, "predict_claude_code", lambda *a, **k: completed)
    monkeypatch.setattr(worker, "_patch", lambda workspace: ("", ""))

    row = worker._claude(Path(tmp_path), "prompt", "opus", 12)

    assert row["worker_outcome"] == "invalid_infrastructure"
    assert row["error_code"] == "usage_receipt_missing"


def test_worker_error_text_is_reduced_to_stable_code():
    from bench.subscription_rank_worker import _safe_code

    assert _safe_code("HTTP 429: secret diagnostic") == "provider_or_quota_failure"
    assert _safe_code("Please approve the write") == "workspace_permission_denied"
    assert _safe_code("arbitrary provider stack") == "provider_or_adapter_failure"


def test_rank_plan_is_exactly_twelve_and_position_balanced():
    from bench.subscription_rank import canonical_plan

    plan = canonical_plan()

    assert len(plan) == 12
    assert len({row["run_id"] for row in plan}) == 12
    assert all(row["attempt"] == 1 for row in plan)
    assert {arm: sum(row["arm"] == arm and row["position"] == 1 for row in plan)
            for arm in ("collie", "claude")} == {"collie": 3, "claude": 3}


def test_rank_summary_shares_rank_on_equal_hidden_solve_rate():
    from bench.subscription_rank import canonical_plan, summarize

    plan = canonical_plan()
    suite = "a" * 64
    rows = [{**row, "suite_sha256": suite,
             "status": "valid_resolved", "resolved": True, "grader": {},
             "duration_ms": 10, "usage": {"input_tokens": 1, "output_tokens": 1,
                                             "cache_read": 0, "cache_creation": 0}}
            for row in plan]

    summary = summarize(plan, rows, suite)

    assert summary["ranking_withheld"] is False
    assert summary["ranking"] == [
        {"rank": 1, "arm": "claude", "score": 1.0},
        {"rank": 1, "arm": "collie", "score": 1.0},
    ]
    assert summary["paired_cells"] == {
        "both": 6, "neither": 0, "collie_only": 0, "claude_only": 0,
    }


def test_rank_summary_withholds_on_any_invalid_slot():
    from bench.subscription_rank import canonical_plan, summarize

    plan = canonical_plan()
    suite = "b" * 64
    rows = [{**row, "suite_sha256": suite,
             "status": "valid_unresolved", "resolved": False, "grader": {},
             "duration_ms": 10, "usage": {}}
            for row in plan]
    rows[4]["status"] = "invalid_infrastructure"

    summary = summarize(plan, rows, suite)

    assert summary["ranking_withheld"] is True
    assert summary["ranking"] is None
    assert summary["scores"] is None


def test_rank_summary_rejects_identity_tampering_and_status_boolean_conflict():
    from bench.subscription_rank import canonical_plan, summarize

    suite = "c" * 64
    plan = canonical_plan()
    rows = [{**row, "suite_sha256": suite, "status": "valid_unresolved",
             "resolved": False, "usage": {}, "grader": {}} for row in plan]
    rows[0]["arm"] = "claude"
    rows[1]["resolved"] = True

    summary = summarize(plan, rows, suite)

    assert summary["ranking_withheld"] is True
    assert summary["ranking"] is None
    assert {item["error"] for item in summary["validation_errors"]} >= {
        "arm_mismatch", "status_resolved_mismatch",
    }


def test_gold_diff_uses_same_independent_evaluator_apply_path(tmp_path):
    from bench.subscription_rank import _apply_patch, _prepare_git_fixture
    from bench.subscription_rank_tasks import (
        TASKS, _run_hidden_grader, materialize_task,
    )

    for index, task in enumerate(TASKS):
        source = tmp_path / ("source-%d" % index)
        evaluator = tmp_path / ("evaluator-%d" % index)
        _prepare_git_fixture(task, source)
        materialize_task(task, source, gold=True)
        patch = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--"], cwd=source,
            capture_output=True, text=True, check=True,
        ).stdout
        _prepare_git_fixture(task, evaluator)

        assert patch
        assert _apply_patch(evaluator, patch, tmp_path / ("gold-%d.diff" % index))
        assert _run_hidden_grader(task, evaluator).returncode == 0


def test_artifact_validator_rejects_resolved_row_with_failed_grader(tmp_path):
    from bench.subscription_rank import (
        _artifact_validation_errors, _atomic_json, _atomic_text, _sha_bytes, _sha_file,
        canonical_plan,
    )
    from bench.subscription_rank_tasks import TASKS, canonical_sha256, task_sha256

    suite = "d" * 64
    planned = canonical_plan()[0]
    task = TASKS[0]
    run_dir = tmp_path / "run"
    reservation = {**planned, "schema_version": 1, "suite_sha256": suite,
                   "reserved_at_utc": "2026-01-01T00:00:00Z"}
    _atomic_json(run_dir / "reservation.json", reservation)
    _atomic_text(run_dir / "patch.diff", "")
    _atomic_json(run_dir / "usage.json", {"schema_version": 1, "suite_sha256": suite,
                                           "run_id": planned["run_id"], "usage": {}})
    grader = {
        "outcome": "graded", "returncode": 1, "resolved": False,
        "task_sha256": task_sha256(task),
        "fixture_sha256": canonical_sha256(task["fixture_files"]),
        "grader_sha256": _sha_bytes(task["hidden_grader"].encode()),
        "patch_sha256": _sha_bytes(b""),
    }
    _atomic_json(run_dir / "grader.json", grader)
    result = {
        **planned, "schema_version": 1, "suite_sha256": suite,
        "status": "valid_resolved", "resolved": True, "error_code": "",
        "reservation_sha256": _sha_file(run_dir / "reservation.json"),
        "patch_sha256": _sha_bytes(b""), "patch_bytes": 0,
        "duration_ms": 1, "turns_exhausted": False, "usage": {}, "grader": grader,
        "docker_returncode": 0,
        "subscription_guard": {"verdict": "allow"},
        "claude_cli_version": "2.1.221 (Claude Code)",
    }

    errors = _artifact_validation_errors(planned, result, run_dir, suite)

    assert "resolved_semantics_invalid" in errors


def test_account_evidence_requires_usage_credits_and_auto_reload_off():
    from bench.subscription_rank import _account_evidence

    args = SimpleNamespace(
        usage_credits_off=True, auto_reload_off=True,
        account_evidence_observed_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
        current_session_used_percent=12.0, weekly_used_percent=17.0,
        usage_credits_spent_usd=0.0, current_balance_usd=8.29,
    )

    evidence = _account_evidence(args)

    assert evidence["usage_credits_enabled"] is False
    assert evidence["auto_reload"] is False
    assert evidence["usage_credits_spent_usd"] == 0
