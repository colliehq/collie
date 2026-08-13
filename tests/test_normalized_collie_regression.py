from __future__ import annotations

import datetime as dt
import json
from pathlib import Path


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _baseline_stub(tmp_path: Path):
    from bench import normalized_collie_regression as regression
    from bench import normalized_harness_rank as rank

    root = tmp_path / "baseline"
    suite = "b" * 64
    ranking_plan = rank.canonical_plan(4)
    manifest = {
        "schema_version": 1,
        "suite_id": "collie-normalized-harness-v1",
        "suite_sha256": suite,
        "model": rank.MODEL,
        "reasoning_effort": "high",
        "repetitions_per_task_arm": 4,
        "physical_model_request_budget_per_attempt": 12,
        "ranking_plan": ranking_plan,
        "tasks": regression._task_evidence(),
    }
    _write_json(root / "manifest.json", manifest)
    _write_json(root / "summary.json", {
        "suite_sha256": suite,
        "validation_errors": [],
        "billing_post_run_verified": True,
        "scores": {"collie": {"attempts": 8, "resolved": 8}},
    })
    for row in ranking_plan:
        if row["arm"] != "collie":
            continue
        _write_json(root / "runs" / row["run_id"] / "result.json", {
            **row,
            "suite_sha256": suite,
            "status": "valid_resolved",
            "resolved": True,
            "patch_sha256": "a" * 64,
        })
    return root


def _ledger_rows(run_id: str, *, contract_error: bool = False):
    from bench import normalized_harness_rank as rank

    usage_one = {
        "input_tokens": 7,
        "output_tokens": 2,
        "cache_read_input_tokens": 1,
        "cache_creation_input_tokens": 3,
    }
    rows = [{
        "schema_version": 1,
        "event": "reserved",
        "request_id": run_id + "-request-1",
        "created_at_utc": "2026-08-13T00:00:00Z",
        "model": rank.MODEL,
        "request_sha256": "1" * 64,
        "prompt_sha256": "2" * 64,
        "request_bytes": 100,
    }, {
        "schema_version": 1,
        "event": "settled",
        "request_id": run_id + "-request-1",
        "created_at_utc": "2026-08-13T00:00:01Z",
        "model": rank.MODEL,
        "outcome": "completed",
        "duration_ms": 10,
        "usage": usage_one,
        **({"error_code": "response_contract_error"} if contract_error else {}),
    }]
    if contract_error:
        rows.extend([{
            "schema_version": 1,
            "event": "reserved",
            "request_id": run_id + "-request-2",
            "created_at_utc": "2026-08-13T00:00:02Z",
            "model": rank.MODEL,
            "request_sha256": "3" * 64,
            "prompt_sha256": "4" * 64,
            "request_bytes": 101,
        }, {
            "schema_version": 1,
            "event": "settled",
            "request_id": run_id + "-request-2",
            "created_at_utc": "2026-08-13T00:00:03Z",
            "model": rank.MODEL,
            "outcome": "completed",
            "duration_ms": 11,
            "usage": usage_one,
        }])
    return rows


def _materialize_regression_results(root: Path, plan, suite: str):
    from bench import normalized_collie_regression as regression
    from bench import normalized_harness_rank as rank

    for index, row in enumerate(plan):
        run_dir = root / "runs" / row["run_id"]
        ledger_dir = root / "evaluator-ledgers" / row["run_id"]
        contract_error = index == 0
        ledger_rows = _ledger_rows(row["run_id"], contract_error=contract_error)
        for event_index, event in enumerate(ledger_rows, 1):
            _write_json(ledger_dir / ("%020d-event.json" % event_index), event)
        ledger = rank._validate_sidecar_ledger(ledger_dir)
        patch = "diff --git a/a.py b/a.py\n+fixed\n"
        patch_bytes = patch.encode()
        patch_sha = regression._sha_bytes(patch_bytes)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "patch.diff").write_bytes(patch_bytes)
        task = next(task for task in rank.TASKS
                    if task["task_id"] == row["task_id"])
        grader = {
            "format": "collie-normalized-harness-grader-v1",
            "outcome": "graded",
            "resolved": True,
            "returncode": 0,
            "success_marker_verified": True,
            "failure_detail": "",
            "task_sha256": rank.task_sha256(task),
            "fixture_sha256": rank.canonical_sha256(task["fixture_files"]),
            "grader_sha256": regression._sha_bytes(
                task["hidden_grader"].encode("utf-8")),
            "patch_sha256": patch_sha,
            "graded_at_utc": "2026-08-13T00:00:04Z",
        }
        _write_json(run_dir / "grader.json", grader)
        usage = ledger["usage"]
        reported = {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cache_read": usage["cache_read_input_tokens"],
            "cache_creation": usage["cache_creation_input_tokens"],
            "total_tokens": sum(usage.values()),
            "model_calls": ledger["physical_requests"],
            "contract_repairs": 1 if contract_error else 0,
        }
        _write_json(run_dir / "result.json", {
            **row,
            "schema_version": 1,
            "suite_sha256": suite,
            "status": "valid_resolved",
            "resolved": True,
            "duration_ms": 100,
            "patch_sha256": patch_sha,
            "patch_bytes": len(patch_bytes),
            "sidecar_request_evidence": ledger,
            "reported_usage": reported,
            "grader": grader,
        })


def test_default_baseline_selects_exact_eight_paired_collie_cells():
    from bench import normalized_collie_regression as regression

    baseline = regression.load_baseline()
    plan = baseline["plan"]

    assert len(plan) == 8
    assert all(row["arm"] == "collie" and row["phase"] == "collie_regression"
               for row in plan)
    assert [row["source_slot"] for row in plan] == [1, 8, 11, 14, 17, 24, 27, 30]
    for task_id in {row["task_id"] for row in plan}:
        selected = [row for row in plan if row["task_id"] == task_id]
        assert [row["repetition"] for row in selected] == [1, 2, 3, 4]
        assert [row["position"] for row in selected] == [1, 4, 3, 2]


def test_regression_source_set_covers_repair_accounting_and_driver():
    from bench import normalized_collie_regression as regression

    assert {
        "bench/normalized_collie_regression.py",
        "bench/normalized_harness_rank.py",
        "bench/normalized_harness_worker.py",
        "harness/subscription_sidecar.py",
        "harness/claude_agent_sdk.py",
        "harness/claude_agent_worker.py",
        "harness/loop.py",
        "harness/providers.py",
        "harness/recorder.py",
        "harness/cli.py",
    }.issubset(set(regression.SOURCE_PATHS))


def test_summary_validates_eight_runs_and_never_produces_ranking(tmp_path):
    from bench import normalized_collie_regression as regression

    baseline_root = _baseline_stub(tmp_path)
    baseline = regression.load_baseline(baseline_root)
    suite = "c" * 64
    result_root = tmp_path / "regression"
    _materialize_regression_results(result_root, baseline["plan"], suite)

    summary = regression.summarize_regression(
        baseline["plan"], result_root, suite, baseline)

    assert summary["validation_errors"] == []
    assert summary["regression"]["attempts"] == 8
    assert summary["regression"]["resolved"] == 8
    assert summary["regression"]["contract_repairs"] == 1
    assert summary["regression"]["contract_error_settlements"] == 1
    assert summary["paired"]["attempts"] == 8
    assert summary["paired"]["complete"] is True
    assert summary["paired"]["net_resolved_delta"] == 0
    assert summary["ranking"] is None
    assert summary["ranking_withheld"] is True
    assert summary["ranking_withheld_reason"] == regression.COMPARISON_LABEL


def test_summary_rejects_usage_and_patch_evidence_mismatch(tmp_path):
    from bench import normalized_collie_regression as regression

    baseline_root = _baseline_stub(tmp_path)
    baseline = regression.load_baseline(baseline_root)
    suite = "d" * 64
    result_root = tmp_path / "regression"
    _materialize_regression_results(result_root, baseline["plan"], suite)
    first = baseline["plan"][0]["run_id"]
    result_path = result_root / "runs" / first / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["reported_usage"]["cache_creation"] += 1
    result["patch_bytes"] += 1
    result["grader"]["grader_sha256"] = "0" * 64
    _write_json(result_path, result)
    grader_path = result_root / "runs" / first / "grader.json"
    grader = json.loads(grader_path.read_text(encoding="utf-8"))
    grader["grader_sha256"] = "0" * 64
    _write_json(grader_path, grader)

    summary = regression.summarize_regression(
        baseline["plan"], result_root, suite, baseline)
    errors = {item["error"] for item in summary["validation_errors"]
              if item["run_id"] == first}

    assert "usage_cache_creation_parity" in errors
    assert "patch_evidence_mismatch" in errors
    assert "grader_task_evidence_mismatch" in errors
    assert summary["ranking"] is None


def test_summary_rejects_extra_attempt_and_tampered_pairing(tmp_path):
    from bench import normalized_collie_regression as regression

    baseline_root = _baseline_stub(tmp_path)
    baseline = regression.load_baseline(baseline_root)
    suite = "7" * 64
    result_root = tmp_path / "regression"
    _materialize_regression_results(result_root, baseline["plan"], suite)
    (result_root / "runs" / "unplanned-ninth-attempt").mkdir(parents=True)
    plan = [dict(row) for row in baseline["plan"]]
    plan[0]["source_run_id"] = "different-baseline-cell"

    summary = regression.summarize_regression(plan, result_root, suite, baseline)
    errors = {item["error"] for item in summary["validation_errors"]}

    assert "unexpected_run_artifact" in errors
    assert "baseline_plan_binding_mismatch" in errors
    assert summary["ranking"] is None


def test_preflight_path_builds_manifest_but_launches_no_attempts(monkeypatch, tmp_path):
    from bench import normalized_collie_regression as regression

    baseline_root = _baseline_stub(tmp_path)
    baseline = regression.load_baseline(baseline_root)
    core = {"images": {"sidecar": "s", "harness": "h"}}
    monkeypatch.setattr(regression, "_prepare_launch", lambda **kwargs: {
        "baseline": baseline,
        "guard": {"provider": "claude-agent-sdk", "verdict": "allow"},
        "core": core,
        "suite_sha256": "e" * 64,
    })
    monkeypatch.setattr(regression.rank, "_run_one", lambda *args, **kwargs: (
        (_ for _ in ()).throw(AssertionError("preflight must not launch an attempt"))))

    code = regression.execute(
        baseline_result_dir=baseline_root,
        wall_seconds=900,
        claude_account_evidence={},
        preflight_only=True)

    assert code == 0


def test_execute_launches_only_the_eight_collie_regression_cells(
        monkeypatch, tmp_path):
    from bench import normalized_collie_regression as regression

    baseline_root = _baseline_stub(tmp_path)
    baseline = regression.load_baseline(baseline_root)
    suite = "8" * 64
    results_root = tmp_path / "results"
    temp_root = tmp_path / "temp"
    monkeypatch.setattr(regression, "RESULTS_ROOT", results_root)
    monkeypatch.setattr(regression, "TEMP_ROOT", temp_root)
    monkeypatch.setattr(regression, "_prepare_launch", lambda **kwargs: {
        "baseline": baseline,
        "core": {"suite_id": regression.SUITE_ID},
        "suite_sha256": suite,
        "sidecar_image": "sidecar-image",
        "harness_image": "harness-image",
        "credential": tmp_path / "credentials.json",
    })
    launched = []

    def fake_run_one(_sidecar, _harness, suite_sha, row, _credential,
                     _suite_temp, result_root, _wall_seconds):
        launched.append(dict(row))
        _materialize_regression_results(result_root, [row], suite_sha)
        return {"status": "valid_resolved"}

    monkeypatch.setattr(regression.rank, "_run_one", fake_run_one)

    code = regression.execute(
        baseline_result_dir=baseline_root,
        wall_seconds=900,
        claude_account_evidence={},
        preflight_only=False)

    assert code == 0
    assert len(launched) == 8
    assert all(row["arm"] == "collie" for row in launched)
    assert all(row["phase"] == "collie_regression" for row in launched)
    summary = json.loads((
        results_root / ("normalized-collie-regression-v1-" + suite[:12])
        / "summary.json").read_text(encoding="utf-8"))
    assert summary["ranking"] is None


def test_finalize_billing_revalidates_and_never_releases_ranking(
        monkeypatch, tmp_path):
    from bench import normalized_collie_regression as regression

    monkeypatch.setattr(regression, "RESULTS_ROOT", tmp_path)
    baseline_root = _baseline_stub(tmp_path)
    baseline = regression.load_baseline(baseline_root)
    baseline_receipt = {key: baseline[key] for key in regression.BASELINE_RECEIPT_FIELDS}
    manifest_core = {
        "schema_version": 1,
        "suite_id": regression.SUITE_ID,
        "regression_plan": baseline["plan"],
        "baseline": baseline_receipt,
    }
    suite = regression._sha_bytes(regression._canonical(manifest_core))
    result_root = tmp_path / ("normalized-collie-regression-v1-" + suite[:12])
    _materialize_regression_results(result_root, baseline["plan"], suite)
    now = dt.datetime.now(dt.timezone.utc)
    created = (now - dt.timedelta(seconds=3)).isoformat().replace("+00:00", "Z")
    completed = (now - dt.timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    _write_json(result_root / "manifest.json", {
        **manifest_core,
        "suite_sha256": suite,
        "created_at_utc": created,
    })
    summary = regression.summarize_regression(
        baseline["plan"], result_root, suite, baseline)
    summary["generated_at_utc"] = completed
    _write_json(result_root / "summary.json", summary)
    evidence = {
        "observed_at_utc": now.isoformat().replace("+00:00", "Z"),
        "usage_credits_enabled": False,
        "auto_reload": False,
        "period_spend_usd": 0.0,
    }

    code = regression.finalize_billing(result_root, claude_evidence=evidence)
    finalized = json.loads((result_root / "summary.json").read_text(encoding="utf-8"))

    assert code == 0
    assert finalized["billing_post_run_verified"] is True
    assert finalized["regression_evidence_complete"] is True
    assert finalized["ranking"] is None
    assert finalized["ranking_withheld"] is True
    receipt = json.loads((result_root / "post-run-billing.json").read_text(encoding="utf-8"))
    assert receipt["ranking_released"] is False


def test_cli_passes_unsafe_post_run_evidence_to_finalizer(monkeypatch, tmp_path):
    from bench import normalized_collie_regression as regression

    observed = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    captured = {}

    def fake_finalize(result_root, *, claude_evidence):
        captured["root"] = result_root
        captured["evidence"] = claude_evidence
        return 2

    monkeypatch.setattr(regression, "finalize_billing", fake_finalize)

    code = regression.main([
        "--finalize-billing", str(tmp_path),
        "--claude-evidence-observed-at", observed,
        "--claude-period-spend-usd", "1.25",
    ])

    assert code == 2
    assert captured["root"] == tmp_path
    assert captured["evidence"]["period_spend_usd"] == 1.25
