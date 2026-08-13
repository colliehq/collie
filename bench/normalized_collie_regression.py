"""Auditable Collie-only regression over the frozen normalized benchmark cells.

This driver deliberately does not produce a harness ranking.  It pairs the eight
Collie cells from a completed four-arm baseline with eight fresh Collie attempts,
while reusing the normalized evaluator's Docker isolation, subscription sidecar,
external patch reconstruction, and hidden grader.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import normalized_harness_rank as rank  # noqa: E402


SUITE_ID = "collie-normalized-regression-v1"
CLAIM = "collie_contract_repair_regression_on_frozen_normalized_tasks"
COMPARISON_LABEL = "collie_only_regression_not_cross_harness_ranking"
DEFAULT_BASELINE_RESULT_DIR = (
    rank.RESULTS_ROOT / "normalized-harness-v1-886831826162"
)
RESULTS_ROOT = rank.RESULTS_ROOT
TEMP_ROOT = rank.TEMP_ROOT
SIDECAR_IMAGE_TAG = "collie-normalized-sidecar:regression-v1"
HARNESS_IMAGE_TAG = "collie-normalized-harness:regression-v1"
REPETITIONS = 4

# ``_build_images`` exports the complete harness directory from committed HEAD.
# Keep explicit hashes for every file that owns this regression's new repair,
# accounting, persistence, and evaluator boundary, in addition to the original
# normalized-suite sources.
SOURCE_PATHS = tuple(sorted(set(rank.SOURCE_PATHS + (
    "bench/normalized_collie_regression.py",
    "harness/cli.py",
    "harness/loop.py",
    "harness/providers.py",
    "harness/recorder.py",
))))

USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
REPORTED_USAGE_FIELDS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_creation": "cache_creation_input_tokens",
}
BASELINE_RECEIPT_FIELDS = (
    "result_dir",
    "suite_sha256",
    "manifest_sha256",
    "summary_sha256",
    "selected_results",
)


def _utc_now() -> str:
    return rank._utc_now()


def _canonical(value: object) -> bytes:
    return rank._canonical_bytes(value)


def _sha_bytes(value: bytes) -> str:
    return rank._sha_bytes(value)


def _sha_file(path: Path) -> str:
    return rank._sha_file(path)


def _load_json(path: Path) -> dict[str, Any]:
    return rank._load_json(path)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    rank._atomic_json(path, value)


def _committed_source_revision_and_hashes(
        *, require_clean: bool) -> tuple[str, dict[str, str]]:
    """Bind the regression to the exact committed bytes used by Docker."""
    revision = rank._run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True).stdout.strip()
    tracked = rank._run(
        ["git", "ls-files", "--error-unmatch", "--", *SOURCE_PATHS], cwd=ROOT)
    if tracked.returncode:
        raise RuntimeError("commit every Collie regression source before launch")
    if require_clean:
        dirty = rank._run(
            ["git", "diff", "--quiet", "HEAD", "--", *SOURCE_PATHS], cwd=ROOT)
        if dirty.returncode:
            raise RuntimeError("commit every Collie regression source before launch")
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision, *SOURCE_PATHS],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if archive.returncode:
        raise RuntimeError("could not hash committed Collie regression sources")
    return revision, rank._hash_archive_members(archive.stdout, SOURCE_PATHS)


def _validate_baseline_tasks(manifest: Mapping[str, Any]) -> None:
    expected = {
        str(task["task_id"]): {
            "task_sha256": rank.task_sha256(task),
            "fixture_sha256": rank.canonical_sha256(task["fixture_files"]),
            "grader_sha256": _sha_bytes(task["hidden_grader"].encode("utf-8")),
        }
        for task in rank.TASKS
    }
    observed: dict[str, dict[str, Any]] = {}
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        raise RuntimeError("baseline task evidence is missing")
    for item in tasks:
        if not isinstance(item, dict) or not isinstance(item.get("task_id"), str):
            raise RuntimeError("baseline task evidence is malformed")
        task_id = str(item["task_id"])
        if task_id in observed:
            raise RuntimeError("baseline task evidence is duplicated")
        observed[task_id] = {
            key: item.get(key) for key in (
                "task_sha256", "fixture_sha256", "grader_sha256")
        }
    if observed != expected:
        raise RuntimeError("baseline task hashes do not match the frozen tasks")


def _regression_plan_from_manifest(
        manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Select and relabel the eight historical Collie ranking cells."""
    source_plan = manifest.get("ranking_plan")
    if not isinstance(source_plan, list):
        raise RuntimeError("baseline ranking plan is missing")
    selected = [row for row in source_plan
                if isinstance(row, dict) and row.get("arm") == "collie"]
    if len(selected) != len(rank.TASKS) * REPETITIONS:
        raise RuntimeError("baseline does not contain eight Collie ranking cells")

    expected_tasks = {str(task["task_id"]): rank.task_sha256(task)
                      for task in rank.TASKS}
    seen: set[tuple[str, int]] = set()
    plan: list[dict[str, Any]] = []
    for slot, source in enumerate(selected, 1):
        task_id = source.get("task_id")
        repetition = source.get("repetition")
        position = source.get("position")
        source_run_id = source.get("run_id")
        source_slot = source.get("slot")
        if (task_id not in expected_tasks
                or source.get("task_sha256") != expected_tasks.get(str(task_id))
                or not isinstance(repetition, int) or isinstance(repetition, bool)
                or repetition not in range(1, REPETITIONS + 1)
                or not isinstance(position, int) or isinstance(position, bool)
                or position not in range(1, len(rank.ARMS) + 1)
                or not isinstance(source_slot, int) or isinstance(source_slot, bool)
                or not isinstance(source_run_id, str) or not source_run_id
                or source.get("phase") != "ranking"
                or source.get("attempt") != 1):
            raise RuntimeError("baseline Collie ranking cell is malformed")
        key = (str(task_id), repetition)
        if key in seen:
            raise RuntimeError("baseline Collie task repetition is duplicated")
        seen.add(key)
        plan.append({
            "slot": slot,
            "run_id": "regress-%02d-%s-r%d-p%d-collie" % (
                slot, task_id, repetition, position),
            "task_id": task_id,
            "task_sha256": expected_tasks[str(task_id)],
            "repetition": repetition,
            # Historical four-arm position is retained only for pairing.  Actual
            # regression launches are sequential and have no ranking position.
            "position": position,
            "arm": "collie",
            "attempt": 1,
            "phase": "collie_regression",
            "source_run_id": source_run_id,
            "source_slot": source_slot,
        })
    expected_pairs = {(task_id, repetition)
                      for task_id in expected_tasks
                      for repetition in range(1, REPETITIONS + 1)}
    if seen != expected_pairs:
        raise RuntimeError("baseline Collie plan is not two tasks by four repetitions")
    return plan


def load_baseline(result_dir: Path = DEFAULT_BASELINE_RESULT_DIR
                  ) -> dict[str, Any]:
    """Validate and bind the completed four-arm baseline and its Collie cells."""
    root = result_dir.resolve()
    manifest_path = root / "manifest.json"
    summary_path = root / "summary.json"
    if not root.is_dir() or not manifest_path.is_file() or not summary_path.is_file():
        raise RuntimeError("baseline normalized result directory is incomplete")
    manifest = _load_json(manifest_path)
    summary = _load_json(summary_path)
    suite_sha = manifest.get("suite_sha256")
    if (not isinstance(suite_sha, str) or len(suite_sha) != 64
            or summary.get("suite_sha256") != suite_sha):
        raise RuntimeError("baseline manifest and summary identities differ")
    if (summary.get("validation_errors") != []
            or summary.get("billing_post_run_verified") is not True):
        raise RuntimeError("baseline is not a completed validated benchmark")
    if (manifest.get("suite_id") != "collie-normalized-harness-v1"
            or manifest.get("model") != rank.MODEL
            or manifest.get("reasoning_effort") != "high"
            or manifest.get("repetitions_per_task_arm") != REPETITIONS
            or manifest.get("physical_model_request_budget_per_attempt")
            != rank.DEFAULT_MAX_TURNS):
        raise RuntimeError("baseline normalized protocol does not match this regression")
    _validate_baseline_tasks(manifest)
    plan = _regression_plan_from_manifest(manifest)

    selected_results: list[dict[str, Any]] = []
    for row in plan:
        source_run_id = str(row["source_run_id"])
        path = root / "runs" / source_run_id / "result.json"
        if not path.is_file():
            raise RuntimeError("baseline Collie result is missing")
        result = _load_json(path)
        if (result.get("suite_sha256") != suite_sha
                or result.get("run_id") != source_run_id
                or result.get("arm") != "collie"
                or result.get("task_id") != row["task_id"]
                or result.get("repetition") != row["repetition"]
                or result.get("position") != row["position"]
                or result.get("status") not in {
                    "valid_resolved", "valid_unresolved"}):
            raise RuntimeError("baseline Collie result binding is invalid")
        resolved = result["status"] == "valid_resolved"
        patch_sha = result.get("patch_sha256")
        if (result.get("resolved") is not resolved
                or not isinstance(patch_sha, str) or len(patch_sha) != 64):
            raise RuntimeError("baseline Collie outcome evidence is invalid")
        selected_results.append({
            "regression_run_id": row["run_id"],
            "source_run_id": source_run_id,
            "task_id": row["task_id"],
            "repetition": row["repetition"],
            "historical_position": row["position"],
            "status": result["status"],
            "resolved": resolved,
            "result_sha256": _sha_file(path),
            "patch_sha256": patch_sha,
        })
    scores = summary.get("scores")
    collie_score = scores.get("collie") if isinstance(scores, dict) else None
    if (not isinstance(collie_score, dict)
            or collie_score.get("attempts") != len(selected_results)
            or collie_score.get("resolved") != sum(
                row["resolved"] for row in selected_results)):
        raise RuntimeError("baseline Collie summary does not match its results")
    return {
        "result_dir": str(root),
        "suite_sha256": suite_sha,
        "manifest_sha256": _sha_file(manifest_path),
        "summary_sha256": _sha_file(summary_path),
        "selected_results": selected_results,
        "plan": plan,
    }


def _task_evidence() -> list[dict[str, Any]]:
    return [{
        "task_id": task["task_id"],
        "task_sha256": rank.task_sha256(task),
        "fixture_sha256": rank.canonical_sha256(task["fixture_files"]),
        "grader_sha256": _sha_bytes(task["hidden_grader"].encode("utf-8")),
    } for task in rank.TASKS]


def _manifest_core(
        *, revision: str, source_hashes: Mapping[str, str],
        sidecar_image: str, harness_image: str, wall_seconds: int,
        baseline: Mapping[str, Any], guard: Mapping[str, Any],
        image_preflight: Mapping[str, Any],
        claude_evidence: Mapping[str, Any]) -> dict[str, Any]:
    plan = [dict(row) for row in baseline["plan"]]
    baseline_receipt = {key: baseline[key] for key in BASELINE_RECEIPT_FIELDS}
    return {
        "schema_version": 1,
        "suite_id": SUITE_ID,
        "claim": CLAIM,
        "scope": "paired_collie_regression",
        "publishable": False,
        "comparison_label": COMPARISON_LABEL,
        "ranking": None,
        "ranking_withheld": True,
        "ranking_withheld_reason": COMPARISON_LABEL,
        "git_revision": revision,
        "source_sha256": dict(source_hashes),
        "images": {"sidecar": sidecar_image, "harness": harness_image},
        "dockerfile_sha256": {
            "sidecar": source_hashes["bench/normalized-sidecar.Dockerfile"],
            "harness": source_hashes["bench/normalized-harness.Dockerfile"],
        },
        "worker_sha256": source_hashes["bench/normalized_harness_worker.py"],
        "driver_sha256": source_hashes["bench/normalized_collie_regression.py"],
        "image_preflight": dict(image_preflight),
        "baseline": baseline_receipt,
        "tasks": _task_evidence(),
        "model": rank.MODEL,
        "reasoning_effort": "high",
        "delivered_prompt_prefix_sha256": _sha_bytes(
            rank.SHARED_EVALUATOR_PROMPT.encode("utf-8")),
        "prompt_contract": "same_frozen_evaluator_prompt_and_tasks_as_baseline",
        "arm": {
            "harness": "collie",
            "model": rank.MODEL,
            "transport": "same evaluator sidecar",
            "deployment": "adapted_not_product_default",
        },
        "repetitions_per_task": REPETITIONS,
        "attempts": len(plan),
        "physical_model_request_budget_per_attempt": rank.DEFAULT_MAX_TURNS,
        "agent_wall_seconds": wall_seconds,
        "regression_plan": plan,
        "launch_policy": "eight_paired_collie_cells_no_admission_no_ranking",
        "transport": {
            "surface": "OpenAI-compatible internal sidecar backed by Claude Agent SDK",
            "one_sdk_turn_per_physical_sidecar_request": True,
            "claude_p_invoked": False,
            "api_key_fallback_disabled": True,
            "model_route_attested_per_request": True,
            "request_ledger": "evaluator_owned_reserved_and_settled_receipts",
        },
        "network": {
            "agent_external_network": False,
            "host_port_published": False,
            "per_attempt_internal_and_egress_networks": True,
        },
        "credential_isolation": {
            "sidecar_only": True,
            "agent_mount_or_environment": False,
        },
        "fresh_git_workspace_per_attempt": True,
        "gold_and_hidden_grader_visible_to_agent": False,
        "guard_receipt_sha256": _sha_bytes(_canonical(guard)),
        "billing": {
            "track": "claude_subscription_same_transport",
            "launch_ui_evidence": dict(claude_evidence),
            "post_run_ui_recheck_required": True,
            "actual_marginal_charge_observed": False,
        },
    }


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _ledger_contract_error_ordinals(directory: Path) -> list[int]:
    rows = [_load_json(path) for path in sorted(directory.iterdir())]
    order: dict[str, int] = {}
    for row in rows:
        if row.get("event") == "reserved":
            order[str(row.get("request_id"))] = len(order) + 1
    return [order[str(row.get("request_id"))] for row in rows
            if (row.get("event") == "settled"
                and row.get("error_code") == "response_contract_error"
                and str(row.get("request_id")) in order)]


def _validate_result_artifacts(
        expected: Mapping[str, Any], result_root: Path,
        suite_sha: str) -> tuple[dict[str, Any] | None, list[str]]:
    run_id = str(expected["run_id"])
    errors: list[str] = []
    run_dir = result_root / "runs" / run_id
    result_path = run_dir / "result.json"
    if not result_path.is_file():
        return None, ["missing_result"]
    try:
        result = _load_json(result_path)
    except Exception:
        return None, ["result_json_invalid"]

    for key in (
            "slot", "run_id", "task_id", "task_sha256", "repetition",
            "position", "arm", "attempt", "phase", "source_run_id",
            "source_slot"):
        if result.get(key) != expected.get(key):
            errors.append(key + "_mismatch")
    if result.get("suite_sha256") != suite_sha:
        errors.append("suite_mismatch")
    if result.get("status") not in {"valid_resolved", "valid_unresolved"}:
        errors.append("invalid_attempt")

    ledger_dir = result_root / "evaluator-ledgers" / run_id
    try:
        ledger = rank._validate_sidecar_ledger(ledger_dir)
    except Exception:
        ledger = {}
        errors.append("sidecar_ledger_invalid")
    embedded = result.get("sidecar_request_evidence")
    if not isinstance(embedded, dict) or ledger != embedded:
        errors.append("sidecar_ledger_summary_mismatch")

    physical = ledger.get("physical_requests")
    if (not _nonnegative_int(physical) or physical < 1
            or physical > rank.DEFAULT_MAX_TURNS):
        errors.append("physical_request_cap_invalid")
    if (ledger.get("reserved_requests") != physical
            or ledger.get("settled_requests") != physical):
        errors.append("request_settlement_mismatch")
    if ledger.get("outcomes") != {"completed": physical}:
        errors.append("physical_request_outcome_invalid")

    usage = ledger.get("usage")
    reported = result.get("reported_usage")
    if (not isinstance(usage, dict) or set(usage) != set(USAGE_FIELDS)
            or any(not _nonnegative_int(usage.get(key)) for key in USAGE_FIELDS)):
        errors.append("ledger_usage_invalid")
        usage = {key: 0 for key in USAGE_FIELDS}
    if not isinstance(reported, dict):
        errors.append("reported_usage_invalid")
        reported = {}
    for reported_key, ledger_key in REPORTED_USAGE_FIELDS.items():
        if (not _nonnegative_int(reported.get(reported_key))
                or reported.get(reported_key) != usage[ledger_key]):
            errors.append("usage_%s_parity" % reported_key)
    total = sum(usage.values())
    if (not _nonnegative_int(reported.get("total_tokens"))
            or reported.get("total_tokens") != total):
        errors.append("usage_total_parity")
    if (not _nonnegative_int(reported.get("model_calls"))
            or reported.get("model_calls") != physical):
        errors.append("reported_model_calls_parity")
    repairs = reported.get("contract_repairs")
    if not _nonnegative_int(repairs) or repairs not in (0, 1):
        errors.append("contract_repairs_invalid")
        repairs = 0

    try:
        contract_ordinals = _ledger_contract_error_ordinals(ledger_dir)
    except Exception:
        contract_ordinals = []
        errors.append("contract_error_evidence_invalid")
    if len(contract_ordinals) > 2:
        errors.append("contract_error_bound_exceeded")
    if not contract_ordinals and repairs != 0:
        errors.append("contract_repair_without_error")
    if contract_ordinals:
        first = contract_ordinals[0]
        if first < rank.DEFAULT_MAX_TURNS:
            if repairs != 1:
                errors.append("contract_error_not_repaired")
            if not _nonnegative_int(physical) or physical <= first:
                errors.append("contract_repair_request_missing")
        elif repairs != 0:
            errors.append("contract_repair_after_request_cap")

    patch_path = run_dir / "patch.diff"
    grader_path = run_dir / "grader.json"
    if not patch_path.is_file():
        errors.append("patch_missing")
        patch = b""
    else:
        patch = patch_path.read_bytes()
    if (result.get("patch_sha256") != _sha_bytes(patch)
            or result.get("patch_bytes") != len(patch)):
        errors.append("patch_evidence_mismatch")
    try:
        grader = _load_json(grader_path)
    except Exception:
        grader = {}
        errors.append("grader_evidence_missing")
    if grader != result.get("grader"):
        errors.append("grader_receipt_mismatch")
    if (grader.get("outcome") != "graded"
            or grader.get("patch_sha256") != result.get("patch_sha256")):
        errors.append("grader_patch_mismatch")
    resolved = result.get("status") == "valid_resolved"
    if (result.get("resolved") is not resolved
            or grader.get("resolved") is not resolved):
        errors.append("grader_resolution_mismatch")
    task = next((item for item in rank.TASKS
                 if item.get("task_id") == expected.get("task_id")), None)
    if task is None:
        errors.append("grader_task_evidence_missing")
    else:
        frozen_task_evidence = {
            "task_sha256": rank.task_sha256(task),
            "fixture_sha256": rank.canonical_sha256(task["fixture_files"]),
            "grader_sha256": _sha_bytes(
                str(task["hidden_grader"]).encode("utf-8")),
        }
        if any(grader.get(key) != value
               for key, value in frozen_task_evidence.items()):
            errors.append("grader_task_evidence_mismatch")
    if grader.get("format") != "collie-normalized-harness-grader-v1":
        errors.append("grader_format_invalid")
    if grader.get("success_marker_verified") is not resolved:
        errors.append("grader_success_marker_mismatch")
    if resolved and grader.get("returncode") != 0:
        errors.append("grader_returncode_mismatch")

    metric = {
        "run_id": run_id,
        "source_run_id": expected["source_run_id"],
        "task_id": expected["task_id"],
        "repetition": expected["repetition"],
        "historical_position": expected["position"],
        "status": result.get("status"),
        "resolved": resolved,
        "duration_ms": result.get("duration_ms"),
        "physical_requests": physical,
        "contract_repairs": repairs,
        "contract_error_settlements": len(contract_ordinals),
        "patch_sha256": result.get("patch_sha256"),
        "grader_sha256": (
            _sha_file(grader_path) if grader_path.is_file() else None),
        "result_sha256": _sha_file(result_path),
    }
    return metric, sorted(set(errors))


def _plan_validation_errors(
        plan: list[dict[str, Any]], baseline: Mapping[str, Any]
        ) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    expected_plan = baseline.get("plan")
    if isinstance(expected_plan, list) and plan != expected_plan:
        errors.append({"run_id": "", "error": "baseline_plan_binding_mismatch"})
    if len(plan) != len(rank.TASKS) * REPETITIONS:
        errors.append({"run_id": "", "error": "regression_plan_size_invalid"})
    expected_pairs = {
        (str(task["task_id"]), repetition)
        for task in rank.TASKS
        for repetition in range(1, REPETITIONS + 1)
    }
    observed_pairs: set[tuple[str, int]] = set()
    observed_ids: set[str] = set()
    for index, row in enumerate(plan, 1):
        run_id = str(row.get("run_id") or "")
        repetition = row.get("repetition")
        normalized_repetition = (
            repetition
            if isinstance(repetition, int) and not isinstance(repetition, bool)
            else -1)
        pair = (str(row.get("task_id") or ""), normalized_repetition)
        if (not run_id or run_id in observed_ids
                or pair in observed_pairs
                or pair not in expected_pairs
                or row.get("slot") != index
                or row.get("arm") != "collie"
                or row.get("phase") != "collie_regression"
                or row.get("attempt") != 1):
            errors.append({"run_id": run_id, "error": "regression_plan_cell_invalid"})
        observed_ids.add(run_id)
        observed_pairs.add(pair)
    if observed_pairs != expected_pairs:
        errors.append({"run_id": "", "error": "regression_plan_pairs_invalid"})
    return errors


def _unexpected_artifact_errors(
        result_root: Path, expected_ids: set[str]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for leaf, code in (
            ("runs", "unexpected_run_artifact"),
            ("evaluator-ledgers", "unexpected_ledger_artifact")):
        directory = result_root / leaf
        if not directory.is_dir():
            continue
        for item in directory.iterdir():
            if not item.is_dir() or item.name not in expected_ids:
                errors.append({"run_id": item.name, "error": code})
    return errors


def summarize_regression(
        plan: list[dict[str, Any]], result_root: Path, suite_sha: str,
        baseline: Mapping[str, Any]) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    metrics: list[dict[str, Any]] = []
    errors.extend(_plan_validation_errors(plan, baseline))
    expected_ids = {str(row.get("run_id") or "") for row in plan}
    errors.extend(_unexpected_artifact_errors(result_root, expected_ids))
    for expected in plan:
        metric, run_errors = _validate_result_artifacts(
            expected, result_root, suite_sha)
        if metric is not None:
            metrics.append(metric)
        errors.extend({"run_id": str(expected.get("run_id") or ""),
                       "error": error} for error in run_errors)

    task_metrics: dict[str, Any] = {}
    for task in rank.TASKS:
        task_id = str(task["task_id"])
        selected = [row for row in metrics if row["task_id"] == task_id]
        task_metrics[task_id] = {
            "attempts": len(selected),
            "resolved": sum(row["resolved"] for row in selected),
            "solve_rate": (
                sum(row["resolved"] for row in selected) / len(selected)
                if selected else None),
        }
    attempts = len(metrics)
    resolved = sum(row["resolved"] for row in metrics)
    baseline_rows = baseline.get("selected_results")
    baseline_rows = baseline_rows if isinstance(baseline_rows, list) else []
    baseline_by_run = {
        str(row.get("regression_run_id")): row
        for row in baseline_rows if isinstance(row, dict)
    }
    paired: list[dict[str, Any]] = []
    for row in metrics:
        historical = baseline_by_run.get(str(row["run_id"]))
        if historical is None:
            continue
        baseline_resolved = historical.get("resolved") is True
        regression_resolved = row["resolved"] is True
        change = (
            "improved" if regression_resolved and not baseline_resolved else
            "regressed" if baseline_resolved and not regression_resolved else
            "unchanged_resolved" if regression_resolved else
            "unchanged_unresolved")
        paired.append({
            "run_id": row["run_id"],
            "source_run_id": row["source_run_id"],
            "task_id": row["task_id"],
            "repetition": row["repetition"],
            "baseline_resolved": baseline_resolved,
            "regression_resolved": regression_resolved,
            "change": change,
        })
    baseline_resolved = sum(
        row.get("resolved") is True for row in baseline_rows
        if isinstance(row, dict))
    return {
        "schema_version": 1,
        "suite_id": SUITE_ID,
        "suite_sha256": suite_sha,
        "claim": CLAIM,
        "scope": "paired_collie_regression",
        "publishable": False,
        "comparison_label": COMPARISON_LABEL,
        "ranking": None,
        "ranking_withheld": True,
        "ranking_withheld_reason": COMPARISON_LABEL,
        "billing_post_run_verified": False,
        "regression_evidence_complete": False,
        "validation_errors": errors,
        "baseline": {
            "suite_sha256": baseline.get("suite_sha256"),
            "attempts": len(baseline_rows),
            "resolved": baseline_resolved,
            "solve_rate": (
                baseline_resolved / len(baseline_rows) if baseline_rows else None),
        },
        "regression": {
            "attempts": attempts,
            "resolved": resolved,
            "solve_rate": resolved / attempts if attempts else None,
            "contract_repairs": sum(int(row["contract_repairs"]) for row in metrics),
            "contract_error_settlements": sum(
                int(row["contract_error_settlements"]) for row in metrics),
            "by_task": task_metrics,
            "runs": metrics,
        },
        "paired": {
            "attempts": len(paired),
            "complete": len(paired) == len(plan),
            "improved": sum(row["change"] == "improved" for row in paired),
            "regressed": sum(row["change"] == "regressed" for row in paired),
            "unchanged": sum(row["change"].startswith("unchanged") for row in paired),
            "net_resolved_delta": resolved - baseline_resolved,
            "runs": paired,
        },
        "generated_at_utc": _utc_now(),
    }


def _normalize_launch_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    evidence = dict(value)
    evidence["observed_at_utc"] = rank._parse_recent_evidence_timestamp(
        evidence.get("observed_at_utc"), label="Claude launch")
    rank._require_safe_claude_evidence(evidence, label="Claude launch")
    return evidence


def _prepare_launch(
        *, baseline_result_dir: Path, wall_seconds: int,
        claude_account_evidence: Mapping[str, Any], require_clean: bool,
        sidecar_image_tag: str, harness_image_tag: str) -> dict[str, Any]:
    rank.task_self_check()
    baseline = load_baseline(baseline_result_dir)
    evidence = _normalize_launch_evidence(claude_account_evidence)
    revision, source_hashes = _committed_source_revision_and_hashes(
        require_clean=require_clean)
    sidecar_image, harness_image = rank._build_images(
        sidecar_image_tag, harness_image_tag, revision)
    image_preflight = rank._image_preflight(sidecar_image, harness_image)
    credential = rank._claude_credentials_path()
    guard = rank._guard_receipt()
    core = _manifest_core(
        revision=revision, source_hashes=source_hashes,
        sidecar_image=sidecar_image, harness_image=harness_image,
        wall_seconds=wall_seconds, baseline=baseline, guard=guard,
        image_preflight=image_preflight, claude_evidence=evidence)
    return {
        "baseline": baseline,
        "evidence": evidence,
        "revision": revision,
        "source_hashes": source_hashes,
        "sidecar_image": sidecar_image,
        "harness_image": harness_image,
        "credential": credential,
        "guard": guard,
        "image_preflight": image_preflight,
        "core": core,
        "suite_sha256": _sha_bytes(_canonical(core)),
    }


def execute(
        *, baseline_result_dir: Path = DEFAULT_BASELINE_RESULT_DIR,
        wall_seconds: int = rank.DEFAULT_WALL_SECONDS,
        claude_account_evidence: Mapping[str, Any],
        preflight_only: bool = False,
        sidecar_image_tag: str = SIDECAR_IMAGE_TAG,
        harness_image_tag: str = HARNESS_IMAGE_TAG) -> int:
    launch = _prepare_launch(
        baseline_result_dir=baseline_result_dir,
        wall_seconds=wall_seconds,
        claude_account_evidence=claude_account_evidence,
        require_clean=not preflight_only,
        sidecar_image_tag=sidecar_image_tag,
        harness_image_tag=harness_image_tag)
    suite_sha = str(launch["suite_sha256"])
    plan = list(launch["baseline"]["plan"])
    if preflight_only:
        print(json.dumps({
            "outcome": "preflight_ok",
            "suite_id": SUITE_ID,
            "suite_sha256": suite_sha,
            "publishable": False,
            "ranking": None,
            "regression_launches": len(plan),
            "images": launch["core"]["images"],
            "baseline_suite_sha256": launch["baseline"]["suite_sha256"],
            "guard": {"provider": launch["guard"].get("provider"),
                      "verdict": launch["guard"].get("verdict")},
        }, ensure_ascii=False, indent=2))
        return 0

    result_root = RESULTS_ROOT / ("normalized-collie-regression-v1-" + suite_sha[:12])
    suite_temp = TEMP_ROOT / ("normalized-collie-regression-v1-" + suite_sha[:12])
    result_root.mkdir(parents=True, exist_ok=False)
    suite_temp.mkdir(parents=True, exist_ok=False)
    _atomic_json(result_root / "manifest.json", {
        **launch["core"], "suite_sha256": suite_sha,
        "created_at_utc": _utc_now(),
    })
    try:
        for row in plan:
            terminal = rank._run_one(
                launch["sidecar_image"], launch["harness_image"], suite_sha,
                row, launch["credential"], suite_temp, result_root, wall_seconds)
            print("[regression %02d] collie  %-29s %s" % (
                row["slot"], row["task_id"], terminal["status"]), flush=True)
            if terminal["status"] not in {"valid_resolved", "valid_unresolved"}:
                print("infrastructure-invalid slot; remaining launches were not consumed")
                break
        summary = summarize_regression(
            plan, result_root, suite_sha, launch["baseline"])
        _atomic_json(result_root / "summary.json", summary)
        print("results: %s" % result_root)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if not summary["validation_errors"] else 2
    finally:
        shutil.rmtree(suite_temp, ignore_errors=True)


def finalize_billing(
        result_root: Path, *, claude_evidence: Mapping[str, Any]) -> int:
    root = result_root.resolve()
    allowed = RESULTS_ROOT.resolve()
    if root.parent != allowed or not root.is_dir():
        raise RuntimeError("result directory is outside the normalized results root")
    manifest_path = root / "manifest.json"
    summary_path = root / "summary.json"
    manifest = _load_json(manifest_path)
    prior_summary = _load_json(summary_path)
    if (manifest.get("suite_id") != SUITE_ID
            or prior_summary.get("suite_id") != SUITE_ID
            or manifest.get("suite_sha256") != prior_summary.get("suite_sha256")):
        raise RuntimeError("regression manifest and summary identities differ")
    manifest_core = {
        key: value for key, value in manifest.items()
        if key not in {"suite_sha256", "created_at_utc"}
    }
    if _sha_bytes(_canonical(manifest_core)) != manifest.get("suite_sha256"):
        raise RuntimeError("regression manifest digest is invalid")
    created = dt.datetime.fromisoformat(
        str(manifest["created_at_utc"]).replace("Z", "+00:00")).astimezone(
            dt.timezone.utc)
    completed = dt.datetime.fromisoformat(
        str(prior_summary["generated_at_utc"]).replace("Z", "+00:00")).astimezone(
            dt.timezone.utc)
    evidence = dict(claude_evidence)
    evidence["observed_at_utc"] = rank._parse_recent_evidence_timestamp(
        evidence.get("observed_at_utc"), label="Claude post-run",
        not_before=max(created, completed))
    safe = True
    try:
        rank._require_safe_claude_evidence(evidence, label="Claude post-run")
    except RuntimeError:
        safe = False

    baseline_receipt = manifest.get("baseline")
    if not isinstance(baseline_receipt, dict):
        raise RuntimeError("regression baseline evidence is missing")
    baseline_path = baseline_receipt.get("result_dir")
    if not isinstance(baseline_path, str) or not baseline_path:
        raise RuntimeError("regression baseline path is missing")
    baseline = load_baseline(Path(baseline_path))
    refreshed_receipt = {
        key: baseline[key] for key in BASELINE_RECEIPT_FIELDS
    }
    if baseline_receipt != refreshed_receipt:
        raise RuntimeError("regression baseline receipt no longer matches its artifacts")
    plan = manifest.get("regression_plan")
    if not isinstance(plan, list):
        raise RuntimeError("regression plan evidence is missing")
    summary = summarize_regression(
        [dict(row) for row in plan if isinstance(row, dict)], root,
        str(manifest["suite_sha256"]), baseline)
    receipt = {
        "schema_version": 1,
        "suite_id": SUITE_ID,
        "suite_sha256": manifest["suite_sha256"],
        "outcome": "verified_safe" if safe else "unsafe_or_incomplete",
        "ranking_released": False,
        "claude": {
            "observed_at_utc": evidence["observed_at_utc"],
            "usage_credits_enabled": evidence.get("usage_credits_enabled"),
            "auto_reload": evidence.get("auto_reload"),
            "period_spend_usd": evidence.get("period_spend_usd"),
        },
        "verified_at_utc": _utc_now(),
    }
    receipt_path = root / "post-run-billing.json"
    _atomic_json(receipt_path, receipt)
    summary["billing_post_run_verified"] = safe
    summary["post_run_billing_receipt_sha256"] = _sha_file(receipt_path)
    summary["regression_evidence_complete"] = (
        safe and not summary["validation_errors"])
    # A Collie-only regression must never mutate the four-arm ranking claim.
    summary["ranking"] = None
    summary["ranking_withheld"] = True
    summary["ranking_withheld_reason"] = COMPARISON_LABEL
    summary["generated_at_utc"] = _utc_now()
    _atomic_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["regression_evidence_complete"] else 2


def _claude_evidence(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "observed_at_utc": args.claude_evidence_observed_at,
        "usage_credits_enabled": False if args.claude_usage_credits_off else None,
        "auto_reload": False if args.claude_auto_reload_off else None,
        "period_spend_usd": args.claude_period_spend_usd,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="normalized_collie_regression")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--finalize-billing", type=Path, metavar="RESULT_DIR")
    parser.add_argument("--baseline-result-dir", type=Path,
                        default=DEFAULT_BASELINE_RESULT_DIR)
    parser.add_argument("--wall-seconds", type=int, default=rank.DEFAULT_WALL_SECONDS)
    parser.add_argument("--claude-evidence-observed-at")
    parser.add_argument("--claude-usage-credits-off", action="store_true")
    parser.add_argument("--claude-auto-reload-off", action="store_true")
    parser.add_argument("--claude-period-spend-usd", type=float)
    args = parser.parse_args(argv)
    if args.wall_seconds < 30:
        parser.error("--wall-seconds must be at least 30")
    evidence = _claude_evidence(args)
    if (not args.claude_evidence_observed_at
            or args.claude_period_spend_usd is None):
        parser.error("fresh Claude billing evidence is required")
    if args.finalize_billing is not None:
        # Post-run evidence must be written even when it is unsafe (for example, a non-zero
        # marginal charge). ``finalize_billing`` records an unsafe receipt and returns non-zero;
        # only launches are rejected before they can consume another request.
        return finalize_billing(args.finalize_billing, claude_evidence=evidence)
    try:
        rank._require_safe_claude_evidence(evidence, label="Claude evidence")
    except RuntimeError as exc:
        parser.error(str(exc))
    return execute(
        baseline_result_dir=args.baseline_result_dir,
        wall_seconds=args.wall_seconds,
        claude_account_evidence=evidence,
        preflight_only=args.preflight_only)


if __name__ == "__main__":
    raise SystemExit(main())
