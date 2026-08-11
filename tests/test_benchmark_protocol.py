import copy
import json
from pathlib import Path

import pytest

from harness.benchmark_protocol import (
    _holm_adjust,
    build_plan,
    load_manifest,
    manifest_fingerprint,
    read_jsonl,
    sha256_file,
    summarize,
    validate_manifest,
)


def _manifest(tmp_path: Path) -> dict:
    tasks = tmp_path / "tasks.txt"
    tasks.write_text("task-a\ntask-b\n", encoding="utf-8")
    controls = {}
    for index, name in enumerate(("prompt", "tool_contract", "dependency_lock",
                                  "sandbox_policy", "retry_policy",
                                  "concurrency_policy"), 1):
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps({"kind": name, "version": index}) + "\n",
                        encoding="utf-8")
        controls[name + "_file"] = path.name
        controls[name + "_sha256"] = sha256_file(path)
    controls["environment_digest"] = "sha256:" + "3" * 64
    controls["context_window_tokens"] = 32_768
    return {
        "schema_version": 1,
        "name": "collie-controlled-smoke",
        "track": "controlled",
        "dataset": {
            "name": "fixture",
            "revision": "d" * 40,
            "grader_revision": "e" * 40,
            "container_digest": "sha256:" + "a" * 64,
            "tasks_file": "tasks.txt",
            "tasks_sha256": sha256_file(tasks),
        },
        "model": {
            "provider": "test-provider",
            "id": "frozen-model",
            "snapshot": "model-build-2026-08-11",
            "endpoint": "https://model.invalid/v1",
            "reasoning_effort": "high",
            "temperature": 0,
            "top_p": 1,
        },
        "controls": controls,
        "budget": {
            "scope": "root_plus_descendants",
            "wall_seconds": 100,
            "model_calls": 10,
            "turns": 10,
            "input_tokens": 10_000,
            "output_tokens": 2_000,
            "cache_tokens": 10_000,
            "cost_usd": 5,
        },
        "execution": {
            "repetitions": 3,
            "seeds": [101, 202, 303],
            "pass_at": 1,
            "attempts_per_task": 1,
            "network": "disabled",
            "memory": "fresh_per_run",
            "refine": False,
            "native_prompt_extensions": False,
            "schedule": "counterbalanced_latin_square",
            "schedule_seed": 7,
            "max_parallel_runs": 1,
        },
        "harnesses": [
            {"name": "collie", "revision": "c" * 40,
             "command": ["collie", "run"], "trace_format": "jsonl",
             "model_source": "manifest", "budget_source": "manifest",
             "usage_source": "independent-meter", "usage_meter_revision": "a" * 40,
             "usage_receipt_format": "collie-benchmark-usage-v1",
             "includes_subagents": True,
             "seed_source": "manifest"},
            {"name": "peer", "revision": "f" * 40,
             "command": ["prime-agent", "--mode", "json"], "trace_format": "jsonl",
             "model_source": "manifest", "budget_source": "manifest",
             "usage_source": "independent-meter", "usage_meter_revision": "a" * 40,
             "usage_receipt_format": "collie-benchmark-usage-v1",
             "includes_subagents": True,
             "seed_source": "manifest"},
        ],
    }


def _artifact(tmp_path: Path, name: str, content: str) -> tuple[str, str]:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return name, sha256_file(path)


def _results(manifest: dict, plan: list[dict], tmp_path: Path) -> list[dict]:
    harnesses = {h["name"]: h for h in manifest["harnesses"]}
    rows = []
    for planned in plan:
        started_at = 1_700_000_000_000 + planned["schedule_index"] * 10_001
        finished_at = started_at + 10_000
        trace, trace_hash = _artifact(
            tmp_path, planned["run_id"] + ".trace.jsonl", '{"event":"done"}\n')
        patch, patch_hash = _artifact(
            tmp_path, planned["run_id"] + ".patch",
            "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n+new\n")
        resolved = planned["harness"] == "collie" or planned["task_id"] == "task-a"
        receipt = {
            "format": "collie-benchmark-grader-v1",
            "schema_version": 1,
            "manifest_sha256": planned["manifest_sha256"],
            "run_id": planned["run_id"],
            "task_id": planned["task_id"],
            "dataset_revision": manifest["dataset"]["revision"],
            "grader_revision": manifest["dataset"]["grader_revision"],
            "container_digest": manifest["dataset"]["container_digest"],
            "patch_sha256": patch_hash,
            "resolved": resolved,
        }
        grader, grader_hash = _artifact(
            tmp_path, planned["run_id"] + ".grader.json",
            json.dumps(receipt, sort_keys=True) + "\n")
        harness = harnesses[planned["harness"]]
        model = manifest["model"] if manifest["track"] == "controlled" else harness["model"]
        usage = {"scope": "root_plus_descendants", "wall_seconds": 10,
                 "model_calls": 2, "turns": 2, "input_tokens": 100,
                 "output_tokens": 20, "cache_tokens": 40, "cost_usd": .25}
        usage_receipt = {
            "format": "collie-benchmark-usage-v1",
            "schema_version": 1,
            "manifest_sha256": planned["manifest_sha256"],
            "run_id": planned["run_id"],
            "task_id": planned["task_id"],
            "schedule_index": planned["schedule_index"],
            "started_at_unix_ms": started_at,
            "finished_at_unix_ms": finished_at,
            "harness": planned["harness"],
            "harness_revision": harness["revision"],
            "model": copy.deepcopy(model),
            "meter": {"source": harness["usage_source"],
                      "revision": harness["usage_meter_revision"]},
            "trace_sha256": trace_hash,
            "patch_sha256": patch_hash,
            "scope": "root_plus_descendants",
            "includes_subagents": True,
            "usage": copy.deepcopy(usage),
        }
        usage_path, usage_hash = _artifact(
            tmp_path, planned["run_id"] + ".usage.json",
            json.dumps(usage_receipt, sort_keys=True) + "\n")
        rows.append({
            **{key: planned[key] for key in
               ("run_id", "manifest_sha256", "task_id", "seed", "repetition", "harness",
                "schedule_cell", "harness_position", "schedule_index")},
            "harness_revision": harness["revision"],
            "model_snapshot": model["snapshot"],
            "model": copy.deepcopy(model),
            "usage_source": harness["usage_source"],
            "usage_meter_revision": harness["usage_meter_revision"],
            "attempt": 1,
            "started_at_unix_ms": started_at,
            "finished_at_unix_ms": finished_at,
            "resolved": resolved,
            "usage": usage,
            "trace_path": trace, "trace_sha256": trace_hash,
            "patch_path": patch, "patch_sha256": patch_hash,
            "grader_path": grader, "grader_sha256": grader_hash,
            "usage_path": usage_path, "usage_sha256": usage_hash,
        })
    return rows


def test_controlled_manifest_builds_deterministic_balanced_plan(tmp_path):
    manifest = _manifest(tmp_path)
    verdict = validate_manifest(manifest, tmp_path)
    assert verdict.ok, verdict.errors

    first = build_plan(manifest, tmp_path)
    second = build_plan(copy.deepcopy(manifest), tmp_path)

    assert first == second
    assert len(first) == 12
    assert len({row["run_id"] for row in first}) == 12
    assert {row["manifest_sha256"] for row in first} == {manifest_fingerprint(manifest)}
    assert {row["harness"] for row in first} == {"collie", "peer"}
    assert [row["schedule_index"] for row in first] == list(range(1, 13))
    assert all(sum(row["harness"] == name and row["harness_position"] == position
                   for row in first) == 3
               for name in ("collie", "peer") for position in (1, 2))


def test_manifest_rejects_mutable_inputs_and_unmeasured_descendants(tmp_path):
    manifest = _manifest(tmp_path)
    manifest["dataset"]["revision"] = "main"
    manifest["model"]["snapshot"] = "latest"
    manifest["budget"]["scope"] = "root_only"
    manifest["execution"]["repetitions"] = 1
    manifest["execution"]["seeds"] = [1]
    manifest["execution"]["memory"] = "shared"
    manifest["harnesses"][1]["usage_source"] = "unknown"
    manifest["harnesses"][1]["includes_subagents"] = False
    manifest["harnesses"][1]["seed_source"] = "native"

    errors = "\n".join(validate_manifest(manifest, tmp_path).errors)
    assert "dataset.revision must be a full commit" in errors
    assert "model.snapshot must be a digest or immutable dated" in errors
    assert "budget.scope must be root_plus_descendants" in errors
    assert "execution.repetitions must be at least 3" in errors
    assert "fresh_per_run" in errors
    assert "usage_source must be independent-meter" in errors
    assert "includes_subagents must be true" in errors
    assert "seed_source must be manifest" in errors


def test_summary_requires_complete_hashed_evidence_and_reports_paired_stats(tmp_path):
    manifest = _manifest(tmp_path)
    plan = build_plan(manifest, tmp_path)
    results = _results(manifest, plan, tmp_path)

    report = summarize(manifest, plan, results, tmp_path)

    assert report["publishable"] is True
    assert report["claim"] == "harness_effect"
    assert report["harnesses"]["collie"]["pass_at_1"] == 1.0
    assert report["harnesses"]["peer"]["pass_at_1"] == .5
    pair = report["paired"]["collie__vs__peer"]
    assert pair["paired_trials"] == 6
    assert pair["paired_task_clusters"] == 2
    assert pair["both"] == 3 and pair["left_only"] == 3
    assert pair["mean_pass_at_1_difference"] == .5
    assert pair["task_cluster_sign_flip_two_sided_p"] == 1.0
    assert pair["holm_adjusted_p"] == 1.0
    assert report["harnesses"]["peer"]["pass_at_1_task_cluster_bootstrap_95"] == [0.0, 1.0]
    assert report["inference"]["unit"] == "task"


def test_summary_refuses_missing_budget_busting_or_tampered_runs(tmp_path):
    manifest = _manifest(tmp_path)
    plan = build_plan(manifest, tmp_path)
    results = _results(manifest, plan, tmp_path)

    missing = summarize(manifest, plan, results[:-1], tmp_path)
    assert missing["publishable"] is False
    assert any("missing 1 planned" in error for error in missing["evidence_errors"])

    results[0]["usage"]["cost_usd"] = 6
    results[1]["task_id"] = "wrong-task"
    (tmp_path / results[2]["trace_path"]).write_text("tampered", encoding="utf-8")
    invalid = summarize(manifest, plan, results, tmp_path)
    errors = "\n".join(invalid["evidence_errors"])
    assert invalid["publishable"] is False
    assert "exceeded budget.cost_usd" in errors
    assert "metadata.task_id does not match" in errors
    assert "trace artifact: sha256 mismatch" in errors


def test_summary_refuses_selective_plan_wrong_model_and_mismatched_grader(tmp_path):
    manifest = _manifest(tmp_path)
    plan = build_plan(manifest, tmp_path)
    results = _results(manifest, plan, tmp_path)

    selective = summarize(manifest, plan[:-2], results[:-2], tmp_path)
    assert selective["publishable"] is False
    assert any("canonical manifest expansion" in error
               for error in selective["evidence_errors"])

    results[0]["model"]["temperature"] = 0.7
    grader_path = tmp_path / results[1]["grader_path"]
    grader = json.loads(grader_path.read_text(encoding="utf-8"))
    grader["resolved"] = not grader["resolved"]
    grader_path.write_text(json.dumps(grader), encoding="utf-8")
    results[1]["grader_sha256"] = sha256_file(grader_path)
    invalid = summarize(manifest, plan, results, tmp_path)
    errors = "\n".join(invalid["evidence_errors"])
    assert "model configuration does not match" in errors
    assert "receipt.resolved does not match" in errors


def test_product_track_cannot_be_mislabeled_harness_effect(tmp_path):
    manifest = _manifest(tmp_path)
    manifest["track"] = "product"
    manifest["execution"]["memory"] = "native"
    common = manifest.pop("model")
    for index, harness in enumerate(manifest["harnesses"]):
        harness["model_source"] = "native_manifest"
        harness["model"] = copy.deepcopy(common)
        harness["model"]["id"] = "native-model-%d" % index
        harness["model"]["snapshot"] = "native-build-%d-2026-08-11" % index
    verdict = validate_manifest(manifest, tmp_path)
    assert verdict.ok
    assert any("system comparison" in warning for warning in verdict.warnings)

    plan = build_plan(manifest, tmp_path)
    report = summarize(manifest, plan, _results(manifest, plan, tmp_path), tmp_path)
    assert report["claim"] == "system_comparison"
    assert report["publishable"] is True


def test_manifest_requires_frozen_bundle_meter_schedule_and_integer_counts(tmp_path):
    manifest = _manifest(tmp_path)
    manifest["dataset"]["revision"] = "refs/heads/main"
    manifest["budget"]["model_calls"] = 1.5
    manifest["execution"]["schedule"] = "harness-major"
    manifest["controls"]["context_window_tokens"] = True
    manifest["harnesses"][1]["usage_meter_revision"] = "stable"
    errors = "\n".join(validate_manifest(manifest, tmp_path).errors)
    assert "dataset.revision must be a full commit" in errors
    assert "budget.model_calls must be a finite positive integer" in errors
    assert "execution.schedule must be counterbalanced_latin_square" in errors
    assert "context_window_tokens must be a positive integer" in errors
    assert "usage_meter_revision must be a full commit" in errors

    self_reported = _manifest(tmp_path)
    self_reported["harnesses"][1]["usage_source"] = "harness-self-report"
    assert any("usage_source must be independent-meter" in error
               for error in validate_manifest(self_reported, tmp_path).errors)

    invalid_model = _manifest(tmp_path)
    invalid_model["model"].update(
        provider=[], id={}, endpoint=123, reasoning_effort=[])
    model_errors = "\n".join(validate_manifest(invalid_model, tmp_path).errors)
    for field in ("provider", "id", "endpoint", "reasoning_effort"):
        assert "model.%s must be a non-empty string" % field in model_errors

    malformed_harnesses = _manifest(tmp_path)
    malformed_harnesses["harnesses"][0]["name"] = 1
    malformed_harnesses["harnesses"][1]["usage_source"] = []
    malformed_errors = "\n".join(
        validate_manifest(malformed_harnesses, tmp_path).errors)
    assert "name must be a non-empty string" in malformed_errors
    assert "usage_source must be independent-meter" in malformed_errors

    fresh = _manifest(tmp_path)
    prompt = tmp_path / fresh["controls"]["prompt_file"]
    prompt.write_text("tampered\n", encoding="utf-8")
    assert any("controls.prompt sha256 mismatch" in error
               for error in validate_manifest(fresh, tmp_path).errors)

    booleans = _manifest(tmp_path)
    booleans["schema_version"] = True
    booleans["execution"]["pass_at"] = True
    booleans["execution"]["attempts_per_task"] = True
    booleans["execution"]["max_parallel_runs"] = True
    boolean_errors = "\n".join(validate_manifest(booleans, tmp_path).errors)
    assert "schema_version must be 1" in boolean_errors
    assert "execution.pass_at must be 1" in boolean_errors
    assert "execution.attempts_per_task must be 1" in boolean_errors
    assert "execution.max_parallel_runs must be 1" in boolean_errors


def test_summary_rejects_nonfinite_fractional_and_boolean_usage(tmp_path):
    manifest = _manifest(tmp_path)
    plan = build_plan(manifest, tmp_path)
    results = _results(manifest, plan, tmp_path)
    results[0]["attempt"] = True
    results[1]["usage"]["model_calls"] = 1.5
    results[2]["usage"]["cost_usd"] = float("nan")

    report = summarize(manifest, plan, results, tmp_path)
    errors = "\n".join(report["evidence_errors"])
    assert report["publishable"] is False
    assert report["statistics_withheld"] is True
    assert "is not pass@1" in errors
    assert "model_calls must be a non-negative integer" in errors
    assert "cost_usd must be a finite non-negative number" in errors
    assert report["harnesses"]["collie"]["pass_at_1"] is None


def test_usage_receipt_and_artifact_identity_are_fail_closed(tmp_path):
    manifest = _manifest(tmp_path)
    plan = build_plan(manifest, tmp_path)
    results = _results(manifest, plan, tmp_path)

    usage_path = tmp_path / results[0]["usage_path"]
    receipt = json.loads(usage_path.read_text(encoding="utf-8"))
    receipt["includes_subagents"] = False
    usage_path.write_text(json.dumps(receipt), encoding="utf-8")
    results[0]["usage_sha256"] = sha256_file(usage_path)
    results[1]["trace_path"] = results[0]["trace_path"]
    results[1]["trace_sha256"] = results[0]["trace_sha256"]

    report = summarize(manifest, plan, results, tmp_path)
    errors = "\n".join(report["evidence_errors"])
    assert report["publishable"] is False
    assert "receipt.includes_subagents does not match" in errors
    assert "trace artifact reuses" in errors


def test_artifact_format_and_relative_path_are_enforced(tmp_path):
    manifest = _manifest(tmp_path)
    plan = build_plan(manifest, tmp_path)
    results = _results(manifest, plan, tmp_path)
    patch_path = tmp_path / results[0]["patch_path"]
    patch_path.write_text("not a git patch\n", encoding="utf-8")
    results[0]["patch_sha256"] = sha256_file(patch_path)
    results[1]["grader_path"] = str((tmp_path / results[1]["grader_path"]).resolve())

    report = summarize(manifest, plan, results, tmp_path)
    errors = "\n".join(report["evidence_errors"])
    assert "patch artifact: non-empty patch must contain" in errors
    assert "grader artifact: path must be non-empty and relative" in errors


def test_mode_only_git_patch_is_valid_evidence(tmp_path):
    manifest = _manifest(tmp_path)
    plan = build_plan(manifest, tmp_path)
    results = _results(manifest, plan, tmp_path)
    row = results[0]
    patch_path = tmp_path / row["patch_path"]
    patch_path.write_text(
        "diff --git a/script.sh b/script.sh\nold mode 100644\nnew mode 100755\n",
        encoding="utf-8")
    row["patch_sha256"] = sha256_file(patch_path)
    for kind in ("grader", "usage"):
        receipt_path = tmp_path / row[kind + "_path"]
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["patch_sha256"] = row["patch_sha256"]
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        row[kind + "_sha256"] = sha256_file(receipt_path)

    assert summarize(manifest, plan, results, tmp_path)["publishable"] is True


def test_actual_start_order_must_follow_counterbalanced_schedule(tmp_path):
    manifest = _manifest(tmp_path)
    plan = build_plan(manifest, tmp_path)
    results = _results(manifest, plan, tmp_path)
    row = results[1]
    row["started_at_unix_ms"] = 1
    receipt_path = tmp_path / row["usage_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["started_at_unix_ms"] = 1
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    row["usage_sha256"] = sha256_file(receipt_path)

    report = summarize(manifest, plan, results, tmp_path)
    assert report["publishable"] is False
    assert any("preceding canonical schedule entry finished" in error
               for error in report["evidence_errors"])


def test_actual_runs_must_not_overlap(tmp_path):
    manifest = _manifest(tmp_path)
    plan = build_plan(manifest, tmp_path)
    results = _results(manifest, plan, tmp_path)
    previous, row = results[0], results[1]
    row["started_at_unix_ms"] = previous["finished_at_unix_ms"] - 1
    receipt_path = tmp_path / row["usage_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["started_at_unix_ms"] = row["started_at_unix_ms"]
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    row["usage_sha256"] = sha256_file(receipt_path)

    report = summarize(manifest, plan, results, tmp_path)
    assert report["publishable"] is False
    assert any("overlapped" in error for error in report["evidence_errors"])


def test_execution_interval_must_be_positive(tmp_path):
    manifest = _manifest(tmp_path)
    plan = build_plan(manifest, tmp_path)
    results = _results(manifest, plan, tmp_path)
    row = results[0]
    row["started_at_unix_ms"] = 0
    row["finished_at_unix_ms"] = 0
    receipt_path = tmp_path / row["usage_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["started_at_unix_ms"] = 0
    receipt["finished_at_unix_ms"] = 0
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    row["usage_sha256"] = sha256_file(receipt_path)
    report = summarize(manifest, plan, results, tmp_path)
    assert report["publishable"] is False
    assert any("must be a positive integer" in error
               for error in report["evidence_errors"])

    results = _results(manifest, plan, tmp_path)
    row = results[0]
    row["finished_at_unix_ms"] = row["started_at_unix_ms"]
    receipt_path = tmp_path / row["usage_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["finished_at_unix_ms"] = row["finished_at_unix_ms"]
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    row["usage_sha256"] = sha256_file(receipt_path)
    report = summarize(manifest, plan, results, tmp_path)
    assert any("non-positive execution interval" in error
               for error in report["evidence_errors"])


def test_boolean_metadata_cannot_impersonate_integer_plan_identity(tmp_path):
    manifest = _manifest(tmp_path)
    plan = build_plan(manifest, tmp_path)
    bool_plan = copy.deepcopy(plan)
    bool_plan[0]["repetition"] = True
    assert any("canonical manifest expansion" in error for error in
               summarize(manifest, bool_plan, [], tmp_path)["evidence_errors"])

    results = _results(manifest, plan, tmp_path)
    results[0]["repetition"] = True
    report = summarize(manifest, plan, results, tmp_path)
    assert any("metadata.repetition does not match" in error
               for error in report["evidence_errors"])


def test_holm_adjustment_is_monotone_and_controls_the_pair_family():
    adjusted = _holm_adjust({"a": .01, "b": .04, "c": .03})
    assert adjusted == {"a": .03, "c": .06, "b": .06}


def test_duplicate_json_keys_are_rejected_in_inputs_and_evidence(tmp_path):
    duplicate_manifest = tmp_path / "duplicate-manifest.json"
    duplicate_manifest.write_text(
        '{"schema_version":1,"schema_version":1}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        load_manifest(duplicate_manifest)

    duplicate_results = tmp_path / "duplicate-results.jsonl"
    duplicate_results.write_text('{"run_id":"a","run_id":"b"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        read_jsonl(duplicate_results)

    overflow_results = tmp_path / "overflow-results.jsonl"
    overflow_results.write_text('{"run_id":"a","overflow":1e999}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="strict finite JSON"):
        read_jsonl(overflow_results)

    manifest = _manifest(tmp_path)
    plan = build_plan(manifest, tmp_path)
    results = _results(manifest, plan, tmp_path)
    row = results[0]
    trace_path = tmp_path / row["trace_path"]
    trace_path.write_text('{"event":"start","event":"done"}\n', encoding="utf-8")
    row["trace_sha256"] = sha256_file(trace_path)
    usage_path = tmp_path / row["usage_path"]
    receipt = json.loads(usage_path.read_text(encoding="utf-8"))
    receipt["trace_sha256"] = row["trace_sha256"]
    usage_path.write_text(json.dumps(receipt), encoding="utf-8")
    row["usage_sha256"] = sha256_file(usage_path)

    report = summarize(manifest, plan, results, tmp_path)
    assert report["publishable"] is False
    assert any("duplicate JSON object key" in error for error in report["evidence_errors"])

    results = _results(manifest, plan, tmp_path)
    row = results[0]
    trace_path = tmp_path / row["trace_path"]
    trace_path.write_text('{"overflow":1e999}\n', encoding="utf-8")
    row["trace_sha256"] = sha256_file(trace_path)
    usage_path = tmp_path / row["usage_path"]
    receipt = json.loads(usage_path.read_text(encoding="utf-8"))
    receipt["trace_sha256"] = row["trace_sha256"]
    usage_path.write_text(json.dumps(receipt), encoding="utf-8")
    row["usage_sha256"] = sha256_file(usage_path)
    report = summarize(manifest, plan, results, tmp_path)
    assert any("strict finite JSON" in error for error in report["evidence_errors"])
