"""Isolated four-arm worker for the normalized subscription benchmark.

The worker owns exactly one harness process/loop and a writable fixture.  It
never receives Claude credentials: every model request goes to the evaluator's
internal OpenAI-compatible sidecar.  Hidden graders and the sidecar ledger stay
outside this process.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping
from urllib.parse import urlsplit
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import __version__, swe  # noqa: E402
from harness.providers import OpenAICompatProvider  # noqa: E402
from harness.subscription_sidecar import BEARER_SENTINEL, MODEL  # noqa: E402


ARMS = ("collie", "prime", "pi", "hermes")
MAX_PATCH_BYTES = 1024 * 1024


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("worker input must be a JSON object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("wb") as handle:
        handle.write(_canonical(dict(value)) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _safe_error(value: object) -> str:
    text = str(value or "").lower()
    if any(marker in text for marker in (
            "response_contract_error", "not bridgeable", "unprocessable entity",
            "status 422", "http 422")):
        return "response_contract_error"
    if "timeout" in text or "timed out" in text:
        return "harness_wall_timeout"
    if any(marker in text for marker in (
            "401", "403", "unauthorized", "authentication", "api key")):
        return "normalized_transport_auth_failure"
    if any(marker in text for marker in (
            "connection refused", "sidecar", "fetch failed", "connect")):
        return "normalized_transport_unavailable"
    if "model" in text and any(marker in text for marker in (
            "mismatch", "unavailable", "not found", "unknown")):
        return "normalized_model_route_failure"
    return "harness_or_transport_failure"


_INFRA_ERROR_MARKERS = (
    "timeout", "sidecar", "transport", "auth", "credential", "model_route",
    "model_unavailable", "executable_missing", "launch_failed", "isolation_failed",
    "process_output_invalid", "native_state_invalid", "wall_limit_invalid",
)


def _worker_outcome_for_error(error: str) -> str:
    """Keep product-loop failures as scoreable losses; fail only shared infrastructure."""
    value = str(error or "").strip().lower()
    if not value:
        return "candidate"
    if any(marker in value for marker in _INFRA_ERROR_MARKERS):
        return "invalid_infrastructure"
    return "product_failure"


def _validated_endpoint(value: object) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {
            "inference", "127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("worker sidecar endpoint is outside the admitted internal route")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("worker sidecar endpoint contains forbidden components")
    if parsed.path.rstrip("/") != "/v1" or not parsed.port:
        raise RuntimeError("worker sidecar endpoint must be an explicit /v1 endpoint")
    return endpoint


def _collect_patch(workspace: Path) -> tuple[str, str]:
    try:
        patch = swe.make_patch(str(workspace), max_len=MAX_PATCH_BYTES)
    except Exception:
        return "", "patch_collection_failed"
    if not isinstance(patch, str):
        return "", "patch_collection_failed"
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        return "", "patch_size_limit_exceeded"
    return patch, ""


def _usage_from_collie(result: object) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    for key in ("turns", "model_calls", "input_tokens", "output_tokens",
                "total_tokens", "cache_read", "cache_creation",
                "cache_miss_tokens"):
        value = getattr(result, key, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            usage[key] = value
    return usage


def _tool_name(call: object) -> str:
    if isinstance(call, Mapping):
        return str(call.get("name") or "")
    return str(getattr(call, "name", "") or "")


def _collie_tool_evidence(result: object) -> dict[str, Any]:
    calls: list[str] = []
    for message in (getattr(result, "messages", None) or []):
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        calls.extend(name for name in (
            _tool_name(call) for call in (message.get("tool_calls") or [])) if name)
    return {
        "native_tool_calls": len(calls),
        "native_edit_calls": sum(name in {"edit_file", "write_file"} for name in calls),
        "native_tool_names": sorted(set(calls)),
        "terminal_observed": result is not None,
    }


def _run_collie(workspace: Path, state_dir: Path, endpoint: str,
                prompt: str, max_turns: int) -> dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    prior = {key: os.environ.get(key) for key in (
        "COLLIE_DATA_DIR", "COLLIE_SIDECAR_BEARER", "COLLIE_HTTP_TIMEOUT")}
    os.environ["COLLIE_DATA_DIR"] = str(state_dir / "collie-data")
    os.environ["COLLIE_SIDECAR_BEARER"] = BEARER_SENTINEL
    os.environ["COLLIE_HTTP_TIMEOUT"] = "180"
    # harness.cli resolves DATA once, at import time.  Importing it before the
    # evaluator-owned state path is installed makes a containerized run fall
    # back to ~/.collie, which is both outside the attempt state mount and may
    # be read-only.  Assign DATA explicitly as well so an earlier import by an
    # embedder/test cannot defeat the per-attempt isolation boundary.
    from harness import cli as harness_cli

    prior_cli_data = harness_cli.DATA
    harness_cli.DATA = str(state_dir / "collie-data")
    harness = None
    result: object | None = None
    try:
        harness = harness_cli.make_harness(
            str(workspace), provider="mock", project="normalized-benchmark",
            code_search=False, embed="hash")
        provider = OpenAICompatProvider(
            endpoint, "COLLIE_SIDECAR_BEARER", MODEL,
            name="normalized-subscription-sidecar")
        provider.effort = "high"
        harness.provider = provider
        harness.registry.retain([
            "read_file", "write_file", "edit_file", "grep", "glob",
        ])
        harness.composer.auto_prefetch = False
        harness.composer.include_project_rules = False
        harness.composer.include_skills = False
        harness.composer.identity = "You are Collie, a coding agent in a frozen evaluation."
        harness.max_turns = int(max_turns)
        harness.max_retries = 0
        harness.retry_base = 0.0
        harness.overflow_recovery = False
        harness.hooks = None
        harness.self_verify = False
        harness.force_edit = True
        harness.force_ratio = 0.55
        harness.hard_ratio = 0.76
        result = harness.run("normalized-benchmark", prompt, consolidate=False)
        reported = str(getattr(result, "error", "") or "").strip()
        # Consuming the harness turn budget is a legitimate unresolved result.
        error = "" if (not reported or getattr(result, "turns_exhausted", False)) else _safe_error(reported)
        return {
            "worker_outcome": _worker_outcome_for_error(error),
            "error_code": error,
            "usage": _usage_from_collie(result),
            "tool_evidence": _collie_tool_evidence(result),
            "runtime": {
                "product": "collie",
                "version": __version__,
                "agent_loop_owner": "collie",
                "provider": "normalized-subscription-sidecar",
                "model": MODEL,
                "reasoning_effort": "high",
            },
        }
    except Exception as exc:
        return {
            "worker_outcome": _worker_outcome_for_error(
                _safe_error("%s: %s" % (type(exc).__name__, exc))),
            "error_code": _safe_error("%s: %s" % (type(exc).__name__, exc)),
            "usage": _usage_from_collie(result) if result is not None else {},
            "tool_evidence": _collie_tool_evidence(result) if result is not None else {
                "native_tool_calls": 0, "native_edit_calls": 0,
                "native_tool_names": [],
            },
            "runtime": {
                "product": "collie", "version": __version__,
                "agent_loop_owner": "collie", "model": MODEL,
            },
        }
    finally:
        if harness is not None:
            for resource in (getattr(harness, "memory", None),
                             getattr(harness, "recorder", None)):
                try:
                    resource.close()
                except Exception:
                    pass
        harness_cli.DATA = prior_cli_data
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_prime_or_pi(arm: str, workspace: Path, state_dir: Path,
                     endpoint: str, prompt: str, wall_seconds: int) -> dict[str, Any]:
    from bench.normalized_prime_pi import (
        prepare_pi, prepare_prime, run_pi, run_prime,
    )

    prepare = prepare_prime if arm == "prime" else prepare_pi
    execute = run_prime if arm == "prime" else run_pi
    launch = prepare(
        state_dir / (arm + "-profile"), endpoint=endpoint,
        workspace=workspace, prompt=prompt,
    )
    receipt = execute(launch, timeout_s=wall_seconds)
    error = str(receipt.get("safe_error_category") or "")
    return {
        "worker_outcome": _worker_outcome_for_error(error),
        "error_code": error,
        "usage": {},
        "tool_evidence": {
            **dict(receipt.get("tool_evidence") or {}),
            "terminal_observed": (
                dict(receipt.get("tool_evidence") or {}).get("terminal_observed") is True
                or receipt.get("agent_end_observed") is True),
        },
        "runtime": dict(receipt.get("runtime") or {}),
    }


def _run_hermes(workspace: Path, state_dir: Path, endpoint: str,
                prompt: str, max_turns: int, wall_seconds: int) -> dict[str, Any]:
    from bench.normalized_hermes import run_hermes

    receipt = run_hermes(
        state_dir / "hermes-profile", workspace, endpoint, prompt,
        max_turns=max_turns, wall_seconds=wall_seconds,
    )
    error = str(receipt.get("safe_error_category") or receipt.get("error_category") or "")
    return {
        "worker_outcome": _worker_outcome_for_error(error),
        "error_code": error,
        "usage": dict(receipt.get("usage") or {}),
        "tool_evidence": dict(receipt.get("tool_evidence") or {}),
        "runtime": dict(receipt.get("runtime") or {}),
    }


def execute(task: Mapping[str, Any], workspace: Path, run_dir: Path,
            state_dir: Path, endpoint: str, max_turns: int) -> dict[str, Any]:
    started = time.monotonic()
    arm = str(task.get("arm") or "")
    if arm not in ARMS:
        raise RuntimeError("worker arm is invalid")
    if task.get("model") != MODEL:
        raise RuntimeError("worker model is not the frozen model")
    if task.get("sidecar_bearer") != BEARER_SENTINEL:
        raise RuntimeError("worker bearer sentinel mismatch")
    prompt = task.get("delivered_prompt")
    if not isinstance(prompt, str) or not prompt:
        raise RuntimeError("worker delivered prompt is missing")
    prompt_sha = _sha256(prompt.encode("utf-8"))
    if task.get("delivered_prompt_sha256") != prompt_sha:
        raise RuntimeError("worker delivered prompt identity mismatch")
    if not workspace.is_dir() or not (workspace / ".git").exists():
        raise RuntimeError("worker workspace is not the frozen Git fixture")
    endpoint = _validated_endpoint(endpoint)
    run_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    wall_seconds = int(task.get("wall_seconds") or 0)
    if wall_seconds < 1:
        raise RuntimeError("worker wall limit is invalid")

    try:
        if arm == "collie":
            core = _run_collie(workspace, state_dir, endpoint, prompt, max_turns)
        elif arm in {"prime", "pi"}:
            core = _run_prime_or_pi(
                arm, workspace, state_dir, endpoint, prompt, wall_seconds)
        else:
            core = _run_hermes(
                workspace, state_dir, endpoint, prompt, max_turns, wall_seconds)
    except Exception as exc:
        core = {
            "worker_outcome": "invalid_infrastructure",
            "error_code": _safe_error("%s: %s" % (type(exc).__name__, exc)),
            "usage": {},
            "tool_evidence": {"native_tool_calls": 0, "native_edit_calls": 0},
            "runtime": {"product": arm, "model": MODEL},
        }

    patch, patch_error = _collect_patch(workspace)
    # This patch is diagnostic only. The evaluator reconstructs the
    # authoritative diff from a pristine snapshot outside this container, so
    # an arm that mutates its own Git metadata cannot invalidate the suite.
    evidence = core.get("tool_evidence")
    if not isinstance(evidence, dict):
        evidence = {"native_tool_calls": 0, "native_edit_calls": 0}
    for key in ("native_tool_calls", "native_edit_calls"):
        value = evidence.get(key, 0)
        evidence[key] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    evidence["terminal_observed"] = evidence.get("terminal_observed") is True
    outcome = str(core.get("worker_outcome") or "invalid_infrastructure")
    if outcome not in {"candidate", "product_failure", "invalid_infrastructure"}:
        outcome = "invalid_infrastructure"
    return {
        "schema_version": 1,
        "worker_outcome": outcome,
        "error_code": str(core.get("error_code") or ""),
        "arm": arm,
        "model": MODEL,
        "run_id": str(task.get("run_id") or ""),
        "task_id": str(task.get("task_id") or ""),
        "delivered_prompt_sha256": prompt_sha,
        "patch": patch,
        "worker_patch_error": patch_error,
        "usage": dict(core.get("usage") or {}),
        "request_evidence": [],
        "tool_evidence": evidence,
        "runtime": dict(core.get("runtime") or {}),
        "turns_exhausted": bool(core.get("turns_exhausted", False)),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "completed_at_utc": _utc_now(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="normalized-harness-worker")
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--task-json", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--max-turns", type=int, default=12)
    args = parser.parse_args(argv)
    if args.max_turns < 1:
        parser.error("--max-turns must be positive")
    try:
        task = _load_object(args.task_json)
        if task.get("arm") != args.arm:
            raise RuntimeError("worker argv/input arm mismatch")
        result = execute(
            task, args.workspace.resolve(), args.run_dir.resolve(),
            args.state_dir.resolve(), args.endpoint, args.max_turns)
    except Exception as exc:
        result = {
            "schema_version": 1,
            "worker_outcome": "invalid_infrastructure",
            "error_code": _safe_error("%s: %s" % (type(exc).__name__, exc)),
            "arm": args.arm,
            "model": MODEL,
            "run_id": "",
            "task_id": "",
            "delivered_prompt_sha256": "",
            "patch": "",
            "usage": {},
            "request_evidence": [],
            "tool_evidence": {"native_tool_calls": 0, "native_edit_calls": 0},
            "runtime": {"product": args.arm, "model": MODEL},
            "duration_ms": 0,
            "completed_at_utc": _utc_now(),
        }
    _atomic_json(args.output.resolve(), result)
    return 0 if result.get("worker_outcome") == "candidate" else 2


if __name__ == "__main__":
    raise SystemExit(main())
