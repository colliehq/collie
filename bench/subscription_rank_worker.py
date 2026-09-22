"""One disposable, subscription-only ranking attempt.

This file is copied into the agent image.  Task fixtures are mounted at /workspace and the
evaluator's hidden grader is deliberately absent.  The outer runner owns scheduling, timeout,
grading, and immutable receipts; this worker owns exactly one product invocation.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, "/opt/collie")

from bench.subscription_guard import SubscriptionGuardError, check_subscription_guard  # noqa: E402
from harness import swe  # noqa: E402


SCHEMA_VERSION = 1
_PERMISSION_MARKERS = (
    "approve the write", "permission mode", "filesystem access", "access is sorted",
    "cannot edit", "can't edit", "unable to edit", "permission denied",
)
_INFRA_MARKERS = (
    "rate limit", "rate_limit", "429", "overloaded", "529", "quota", "usage limit",
    "authentication", "not logged in", "unauthorized", "forbidden", "billing",
    "network error", "connection error", "service unavailable", "internal server error",
)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                  allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _safe_code(text: str) -> str:
    """Map provider prose to a non-secret, fixed diagnostic category."""
    lower = (text or "").lower()
    if any(marker in lower for marker in _PERMISSION_MARKERS):
        return "workspace_permission_denied"
    if any(marker in lower for marker in _INFRA_MARKERS):
        return "provider_or_quota_failure"
    return "provider_or_adapter_failure"


def _patch(workspace: Path) -> tuple[str, str]:
    try:
        return swe.make_patch(str(workspace)), ""
    except Exception:
        return "", "patch_collection_failed"


def _collie(workspace: Path, prompt: str, model: str, max_turns: int,
            output_dir: Path) -> dict[str, Any]:
    os.environ["COLLIE_DATA_DIR"] = str(output_dir / "state")
    started = time.monotonic()
    try:
        result = swe.predict_collie(
            str(workspace), prompt, provider="claude-cli", model=model,
            max_turns=max_turns, benchmark_safe=True,
        )
    except Exception as exc:
        patch, patch_error = _patch(workspace)
        return {
            "worker_outcome": "invalid_infrastructure",
            "error_code": patch_error or _safe_code(type(exc).__name__ + ": " + str(exc)),
            "patch": patch,
            "usage": {},
            "turns_exhausted": False,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    patch, patch_error = _patch(workspace)
    usage = {}
    for key in ("turns", "input_tokens", "output_tokens", "total_tokens", "cache_read",
                "cache_creation", "cache_miss_tokens", "cost_usd"):
        value = getattr(result, key, None)
        if value is not None:
            usage["api_equivalent_cost_usd" if key == "cost_usd" else key] = value
    reported_error = (getattr(result, "error", "") or "").strip()
    error_code = patch_error or (_safe_code(reported_error) if reported_error else "")
    if not error_code and not {"input_tokens", "output_tokens"}.issubset(usage):
        error_code = "usage_receipt_missing"
    return {
        "worker_outcome": "invalid_infrastructure" if error_code else "candidate",
        "error_code": error_code,
        "patch": patch,
        "usage": usage,
        "turns_exhausted": bool(getattr(result, "turns_exhausted", False)),
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def _claude(workspace: Path, prompt: str, model: str, max_turns: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = swe.predict_claude_code(
            str(workspace), prompt, model=model, max_turns=max_turns,
        )
    except Exception as exc:
        patch, patch_error = _patch(workspace)
        return {
            "worker_outcome": "invalid_infrastructure",
            "error_code": patch_error or _safe_code(type(exc).__name__ + ": " + str(exc)),
            "patch": patch,
            "usage": {},
            "turns_exhausted": False,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }

    patch, patch_error = _patch(workspace)
    data: dict[str, Any] = {}
    try:
        parsed = json.loads(result.stdout or "")
        if isinstance(parsed, dict):
            data = parsed
        else:
            patch_error = patch_error or "claude_output_invalid"
    except (TypeError, ValueError):
        patch_error = patch_error or "claude_output_invalid"

    result_text = str(data.get("result") or "")
    subtype = str(data.get("subtype") or "").lower()
    turns_exhausted = "max_turn" in subtype
    error_code = patch_error
    if turns_exhausted:
        # The declared turn ceiling is a normal unresolved product outcome, even when the CLI
        # represents it with a non-zero process exit.
        pass
    elif result.returncode != 0:
        error_code = error_code or _safe_code((result.stderr or "") + "\n" + result_text)
    elif data.get("is_error") is True and not turns_exhausted:
        error_code = error_code or _safe_code(result_text + "\n" + subtype)
    elif not patch and any(marker in result_text.lower() for marker in _PERMISSION_MARKERS):
        error_code = error_code or "workspace_permission_denied"

    usage: dict[str, Any] = {}
    raw_usage = data.get("usage") or {}
    if isinstance(raw_usage, dict):
        aliases = {
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "cache_read_input_tokens": "cache_read",
            "cache_creation_input_tokens": "cache_creation",
        }
        for source, target in aliases.items():
            if isinstance(raw_usage.get(source), (int, float)) and not isinstance(
                    raw_usage.get(source), bool):
                usage[target] = raw_usage[source]
    if isinstance(data.get("total_cost_usd"), (int, float)):
        usage["api_equivalent_cost_usd"] = data["total_cost_usd"]
    if isinstance(data.get("num_turns"), int):
        usage["turns"] = data["num_turns"]
    if not error_code and not {"input_tokens", "output_tokens"}.issubset(usage):
        error_code = "usage_receipt_missing"

    return {
        "worker_outcome": "invalid_infrastructure" if error_code else "candidate",
        "error_code": error_code,
        "patch": patch,
        "usage": usage,
        "turns_exhausted": turns_exhausted,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def _version() -> str:
    result = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                            timeout=10, check=False)
    return (result.stdout or result.stderr or "").strip()[:120]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="subscription_rank_worker")
    parser.add_argument("--arm", choices=("collie", "claude"))
    parser.add_argument("--task-json", default="/input/task.json")
    parser.add_argument("--workspace", default="/workspace")
    parser.add_argument("--output", default="/output/worker.json")
    parser.add_argument("--model", default="opus")
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)

    output = Path(args.output)
    try:
        guard = check_subscription_guard("claude-code")
    except SubscriptionGuardError as exc:
        _atomic_json(output, {
            "schema_version": SCHEMA_VERSION,
            "worker_outcome": "not_admitted",
            "error_code": exc.reason,
            "subscription_guard": exc.receipt,
        })
        return 2

    cli_version = _version()

    if args.check_only:
        _atomic_json(output, {
            "schema_version": SCHEMA_VERSION,
            "worker_outcome": "preflight_ok",
            "subscription_guard": guard,
            "claude_cli_version": cli_version,
        })
        return 0
    if args.arm is None or not 1 <= args.max_turns <= 12:
        parser.error("--arm is required and --max-turns must be in [1, 12]")
    task = json.loads(Path(args.task_json).read_text(encoding="utf-8"))
    if not isinstance(task, dict) or not isinstance(task.get("prompt"), str):
        raise ValueError("task-json must contain a prompt string")
    # Persist admission before the model call.  If the outer wall cap kills this worker, the
    # evaluator can still prove that this exact slot passed the subscription-only guard rather
    # than guessing from a suite-level preflight.
    _atomic_json(output.with_name("admission.json"), {
        "schema_version": SCHEMA_VERSION,
        "worker_outcome": "admitted",
        "arm": args.arm,
        "task_id": task.get("task_id"),
        "model_alias": args.model,
        "subscription_guard": guard,
        "claude_cli_version": cli_version,
    })

    if args.arm == "collie":
        row = _collie(Path(args.workspace), task["prompt"], args.model, args.max_turns,
                      output.parent)
    else:
        row = _claude(Path(args.workspace), task["prompt"], args.model, args.max_turns)
    row.update({
        "schema_version": SCHEMA_VERSION,
        "arm": args.arm,
        "task_id": task.get("task_id"),
        "model_alias": args.model,
        "subscription_guard": guard,
        "claude_cli_version": cli_version,
        "patch_bytes": len(row.get("patch") or ""),
    })
    _atomic_json(output, row)
    return 0 if row["worker_outcome"] == "candidate" else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
