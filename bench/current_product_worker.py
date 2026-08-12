"""Isolated arm worker for the current Collie-versus-Codex product benchmark.

The evaluator passes only a public task prompt and a fresh Git workspace.  Hidden
grader code never enters this process.  Collie's arm uses the official Claude
Agent SDK provider owned by Collie's loop; Codex uses its native ephemeral exec
surface.  This module does not run unless the benchmark driver explicitly starts
it with a preflight receipt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Mapping
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.paired_eval import _codex_adapter_error, _codex_usage  # noqa: E402
from harness import __version__, swe  # noqa: E402
from harness.subscription_guard import check_subscription_guard  # noqa: E402


COLLIE_MODEL = "claude-opus-4-8"
CODEX_MODEL = "gpt-5.6-sol"
MAX_PATCH_BYTES = 1024 * 1024
SHARED_EVALUATOR_PROMPT = (
    "Resolve this software issue by editing the repository's SOURCE code in the current "
    "directory. Continue until source files have actually changed; do not stop at a plan. "
    "Never edit test files. Make a focused, complete fix for every requirement in the issue. "
    "Do not install packages, create a virtual environment, use the network, or access files "
    "outside this repository. Do not run the full test suite: an evaluator-owned hidden grader "
    "runs after you exit. Use only the product's admitted local repository tools.\n\nISSUE:\n"
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("wb") as handle:
        handle.write(_canonical_bytes(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("benchmark input is not a JSON object")
    return value


def _safe_error(value: object) -> str:
    """Reduce diagnostics to stable categories without persisting provider text."""
    lower = str(value or "").lower()
    if any(marker in lower for marker in (
            "quota", "rate limit", "usage limit", "capacity", "overloaded")):
        return "provider_or_quota_failure"
    if any(marker in lower for marker in (
            "permission", "read-only", "readonly", "operation not permitted")):
        return "workspace_permission_failure"
    if "timeout" in lower or "timed out" in lower:
        return "agent_wall_timeout"
    return "provider_or_adapter_failure"


class RequestLedger:
    """Crash-safe, one-file-per-transition physical request journal."""

    def __init__(self, root: Path, *, run_id: str, model: str):
        self.root = root
        self.run_id = run_id
        self.model = model
        self.root.mkdir(parents=True, exist_ok=True)
        if any(self.root.iterdir()):
            raise RuntimeError("request ledger must be fresh")

    def reserve(self, purpose: str) -> str:
        request_id = "%s-r%s" % (self.run_id, uuid.uuid4().hex[:16])
        request_dir = self.root / request_id
        request_dir.mkdir(parents=False, exist_ok=False)
        _atomic_json(request_dir / "reservation.json", {
            "schema_version": 1,
            "run_id": self.run_id,
            "request_id": request_id,
            "purpose": str(purpose or ""),
            "model": self.model,
            "reserved_at_utc": _utc_now(),
            "state": "reserved",
        })
        return request_id

    def settle(self, request_id: str, outcome: str = "completed") -> None:
        request_dir = self.root / str(request_id)
        reservation = request_dir / "reservation.json"
        settlement = request_dir / "settlement.json"
        if request_dir.parent != self.root or not reservation.is_file():
            raise RuntimeError("unknown request reservation")
        if settlement.exists():
            raise RuntimeError("request is already settled")
        normalized = str(outcome or "error")
        if normalized not in ("completed", "error"):
            normalized = "error"
        _atomic_json(settlement, {
            "schema_version": 1,
            "run_id": self.run_id,
            "request_id": str(request_id),
            "outcome": normalized,
            "settled_at_utc": _utc_now(),
            "state": "settled",
        })

    def evidence(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for request_dir in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not request_dir.is_dir():
                raise RuntimeError("unexpected request-ledger entry")
            reservation_path = request_dir / "reservation.json"
            settlement_path = request_dir / "settlement.json"
            reservation = _load_json(reservation_path)
            settlement = _load_json(settlement_path) if settlement_path.is_file() else None
            if (reservation.get("request_id") != request_dir.name
                    or reservation.get("run_id") != self.run_id
                    or reservation.get("model") != self.model):
                raise RuntimeError("request reservation identity mismatch")
            if settlement is not None and (
                    settlement.get("request_id") != request_dir.name
                    or settlement.get("run_id") != self.run_id):
                raise RuntimeError("request settlement identity mismatch")
            rows.append({
                "request_id": request_dir.name,
                "purpose": reservation.get("purpose"),
                "reservation_sha256": _sha_bytes(reservation_path.read_bytes()),
                "settlement_sha256": (
                    _sha_bytes(settlement_path.read_bytes()) if settlement is not None else None),
                "outcome": settlement.get("outcome") if settlement is not None else None,
            })
        return rows


def _usage_from_collie(result: object, request_count: int) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "model_requests": request_count,
        "scope": "collie_root_no_subagents",
    }
    for key in ("turns", "input_tokens", "output_tokens", "total_tokens",
                "cache_read", "cache_creation", "cache_miss_tokens"):
        value = getattr(result, key, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            usage[key] = value
    cost = getattr(result, "cost_usd", None)
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        usage["api_equivalent_cost_usd"] = cost
    return usage


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


def run_collie(task: Mapping[str, Any], workspace: Path, run_dir: Path,
               state_dir: Path, max_turns: int) -> dict[str, Any]:
    ledger = RequestLedger(run_dir / "requests", run_id=str(task["run_id"]),
                           model=COLLIE_MODEL)
    state_dir.mkdir(parents=True, exist_ok=True)
    credential_source = Path(str(task["claude_credential_source"])).resolve()
    if not credential_source.is_file():
        raise RuntimeError("Claude plan credential source is unavailable")
    claude_home = state_dir / "claude-home"
    (claude_home / ".claude").mkdir(parents=True, exist_ok=False)
    credential_target = claude_home / ".claude" / ".credentials.json"
    # Copy credential bytes without opening, hashing, logging, or returning them.
    shutil.copyfile(credential_source, credential_target)
    if os.name != "nt":
        os.chmod(credential_target, 0o600)
    prior = {key: os.environ.get(key) for key in ("HOME", "COLLIE_DATA_DIR")}
    os.environ["HOME"] = str(claude_home)
    os.environ["COLLIE_DATA_DIR"] = str(state_dir / "data")
    started = time.monotonic()
    result: object | None = None
    error_code = ""
    try:
        result = swe.predict_collie(
            str(workspace), str(task["prompt"]), provider="claude-agent-sdk",
            model=COLLIE_MODEL, max_turns=max_turns, benchmark_safe=True,
            request_gate=ledger.reserve, request_complete=ledger.settle,
            request_scope=str(task["run_id"]),
            complete_prompt=str(task["delivered_prompt"]),
            benchmark_effort="high",
        )
        reported = str(getattr(result, "error", "") or "").strip()
        if reported:
            error_code = _safe_error(reported)
    except Exception as exc:
        error_code = _safe_error("%s: %s" % (type(exc).__name__, exc))
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    patch, patch_error = _collect_patch(workspace)
    error_code = error_code or patch_error
    try:
        requests = ledger.evidence()
    except Exception:
        requests = []
        error_code = error_code or "request_ledger_invalid"
    if not requests:
        error_code = error_code or "model_request_reservation_missing"
    elif any(row.get("outcome") != "completed" for row in requests):
        error_code = error_code or "model_request_settlement_incomplete"
    usage = _usage_from_collie(result, len(requests)) if result is not None else {}
    if not error_code and not {"input_tokens", "output_tokens"}.issubset(usage):
        error_code = "usage_receipt_missing"
    return {
        "worker_outcome": "invalid_infrastructure" if error_code else "candidate",
        "error_code": error_code,
        "patch": patch,
        "usage": usage,
        "request_evidence": requests,
        "turns_exhausted": bool(getattr(result, "turns_exhausted", False)),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "runtime": {
            "product": "collie",
            "collie_version": __version__,
            "provider": "claude-agent-sdk",
            "model": COLLIE_MODEL,
            "agent_loop_owner": "collie",
            "auth_attestation": "api_key_source_none_enforced_per_response",
            "internal_retries": 0,
            "reasoning_effort": "high",
        },
    }


def _codex_trace_verdict(stdout: str, expected_model: str) -> tuple[str, str]:
    terminal = ""
    forbidden_markers = (
        "web_search", "mcp", "plugin", "app_tool", "subagent", "sub_agent",
        "multi_agent", "goal.created", "goal.updated", "tool_suggest",
        "browser", "computer_use", "image_generation",
    )
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "").lower()
        encoded = json.dumps(event, ensure_ascii=True, sort_keys=True).lower()
        if any(marker in encoded for marker in forbidden_markers):
            return terminal, "codex_forbidden_surface_observed"
        observed_model = event.get("model")
        if (isinstance(observed_model, str) and observed_model.strip()
                and observed_model.strip() != expected_model):
            return terminal, "codex_model_rerouted"
        if any(marker in kind for marker in ("reroute", "fallback")):
            return terminal, "codex_model_rerouted"
        if kind == "turn.completed":
            terminal = "completed"
        elif kind in ("turn.failed", "error") or kind.endswith(".error"):
            terminal = "failed"
    return terminal, ""


def run_codex(task: Mapping[str, Any], workspace: Path, state_dir: Path) -> dict[str, Any]:
    started = time.monotonic()
    cli = None
    isolated_guard = None
    error_code = ""
    try:
        auth_source = Path(str(task["codex_auth_source"])).resolve()
        if not auth_source.is_file() or auth_source.name != "auth.json":
            raise RuntimeError("Codex auth source is unavailable")
        codex_home = state_dir.resolve() / "codex-home"
        codex_home.mkdir(parents=True, exist_ok=False)
        auth_target = codex_home / "auth.json"
        # Credential bytes are copied mechanically. They are never opened by
        # this benchmark, hashed, logged, or included in its evidence.
        shutil.copyfile(auth_source, auth_target)
        if os.name != "nt":
            os.chmod(auth_target, 0o600)
        prior = {key: os.environ.get(key) for key in ("CODEX_HOME", "HOME")}
        os.environ["CODEX_HOME"] = str(codex_home)
        os.environ["HOME"] = str(state_dir.resolve() / "home")
        Path(os.environ["HOME"]).mkdir(parents=True, exist_ok=False)
        try:
            isolated_environment = {
                key: value for key, value in os.environ.items()
                if key.upper() in {
                    "CODEX_HOME", "HOME", "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR",
                    "TEMP", "TMP", "TMPDIR", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
                    "COMSPEC", "HOMEDRIVE", "HOMEPATH",
                }
            }
            isolated_guard = check_subscription_guard(
                "codex-cli", codex_launch_receipt=task["guard_receipt"],
                environ=isolated_environment, expected_codex_home=str(codex_home))
            cli = swe.predict_codex(
                str(workspace), str(task["prompt"]), model=CODEX_MODEL,
                timeout=int(task["wall_seconds"]),
                complete_prompt=str(task["delivered_prompt"]),
            )
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(state_dir.resolve(), ignore_errors=True)
    except Exception as exc:
        error_code = _safe_error("%s: %s" % (type(exc).__name__, exc))

    usage: dict[str, Any] = {}
    if cli is not None:
        if cli.returncode != 0:
            error_code = error_code or _safe_error(
                "%s\n%s" % (cli.stdout or "", cli.stderr or ""))
        error_code = error_code or _codex_adapter_error(cli.stdout or "", cli.stderr or "")
        terminal, trace_error = _codex_trace_verdict(cli.stdout or "", CODEX_MODEL)
        error_code = error_code or trace_error
        if not error_code and terminal != "completed":
            error_code = "codex_terminal_receipt_missing"
        usage = _codex_usage(cli.stdout or "")
        if usage:
            usage["scope"] = "codex_product_reported_aggregate"
            usage["internal_model_requests"] = None

    patch, patch_error = _collect_patch(workspace)
    error_code = error_code or patch_error
    if not error_code and not {"input_tokens", "output_tokens"}.issubset(usage):
        error_code = "usage_receipt_missing"
    return {
        "worker_outcome": "invalid_infrastructure" if error_code else "candidate",
        "error_code": error_code,
        "patch": patch,
        "usage": usage,
        "request_evidence": [],
        "isolated_guard_receipt": isolated_guard if cli is not None else None,
        "turns_exhausted": False,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "runtime": {
            "product": "codex",
            "provider": "native-codex-exec",
            "model": CODEX_MODEL,
            "auth_method": "ChatGPT",
            "session": "ephemeral",
            "sandbox": "workspace-write",
            "user_config": "ignored",
            "repository_rules": "ignored",
            "internal_model_requests": "not_observed_by_cli",
            "reasoning_effort": "high",
            "codex_cli_version": task.get("runtime_version"),
        },
    }


def _validate_task(value: Mapping[str, Any], arm: str) -> None:
    required = {"run_id", "task_id", "task_sha256", "prompt", "model",
                "delivered_prompt", "delivered_prompt_sha256",
                "wall_seconds", "guard_receipt", "guard_receipt_sha256"}
    if not required.issubset(value):
        raise RuntimeError("worker input is incomplete")
    expected_model = COLLIE_MODEL if arm == "collie" else CODEX_MODEL
    expected_provider = "claude-agent-sdk" if arm == "collie" else "codex-cli"
    if value.get("model") != expected_model:
        raise RuntimeError("worker input model does not match the frozen arm")
    receipt = value.get("guard_receipt")
    if not isinstance(receipt, dict) or receipt.get("verdict") != "allow":
        raise RuntimeError("worker input has no admitted subscription receipt")
    if receipt.get("provider") != expected_provider:
        raise RuntimeError("worker subscription receipt belongs to another provider")
    if _sha_bytes(_canonical_bytes(receipt)) != value.get("guard_receipt_sha256"):
        raise RuntimeError("worker subscription receipt digest mismatch")
    delivered = value.get("delivered_prompt")
    if (not isinstance(delivered, str) or
            _sha_bytes(delivered.encode("utf-8")) != value.get("delivered_prompt_sha256")):
        raise RuntimeError("worker delivered-prompt digest mismatch")
    if arm == "codex" and "codex_auth_source" not in value:
        raise RuntimeError("worker Codex auth source is missing")
    if arm == "collie" and "claude_credential_source" not in value:
        raise RuntimeError("worker Claude credential source is missing")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="current_product_worker")
    parser.add_argument("--arm", choices=("collie", "codex"), required=True)
    parser.add_argument("--task-json", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-turns", type=int, required=True)
    args = parser.parse_args(argv)

    output = args.output.resolve()
    started_at = _utc_now()
    try:
        task = _load_json(args.task_json.resolve())
        _validate_task(task, args.arm)
        workspace = args.workspace.resolve()
        run_dir = args.run_dir.resolve()
        if not (workspace / ".git").is_dir():
            raise RuntimeError("worker workspace is not a fresh Git repository")
        if args.arm == "collie":
            result = run_collie(
                task, workspace, run_dir, args.state_dir.resolve(), args.max_turns)
        else:
            result = run_codex(task, workspace, args.state_dir.resolve())
        result.update({
            "schema_version": 1,
            "run_id": task["run_id"],
            "task_id": task["task_id"],
            "task_sha256": task["task_sha256"],
            "arm": args.arm,
            "model": task["model"],
            "started_at_utc": started_at,
            "finished_at_utc": _utc_now(),
        })
    except Exception as exc:
        result = {
            "schema_version": 1,
            "arm": args.arm,
            "worker_outcome": "invalid_infrastructure",
            "error_code": _safe_error("%s: %s" % (type(exc).__name__, exc)),
            "patch": "",
            "usage": {},
            "request_evidence": [],
            "started_at_utc": started_at,
            "finished_at_utc": _utc_now(),
        }
    _atomic_json(output, result)
    return 0 if result.get("worker_outcome") == "candidate" else 2


if __name__ == "__main__":
    raise SystemExit(main())
