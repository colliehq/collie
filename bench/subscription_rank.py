"""Crash-safe 12-attempt Opus subscription product ranking.

This is deliberately a small, exploratory *product* comparison: Collie using the official
Claude CLI as its reasoner versus native Claude Code, both requesting the rolling ``opus`` alias.
The agent containers never receive the hidden graders or reference implementations.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
from typing import Any, Iterator, Mapping
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.subscription_rank_tasks import (  # noqa: E402
    TASKS,
    canonical_sha256,
    materialize_task,
    self_check as task_self_check,
    task_by_id,
    task_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = ROOT / ".bench-tmp"
RESULTS_ROOT = ROOT / "bench" / "results"
DOCKERFILE = ROOT / "bench" / "subscription-rank.Dockerfile"
WORKER = ROOT / "bench" / "subscription_rank_worker.py"
MODEL_ALIAS = "opus"
IMAGE_TAG = "collie-subscription-rank:v1"
BASE_IMAGE = (
    "node:22.22.0-bookworm-slim@sha256:"
    "dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94"
)
CLAUDE_CLI_VERSION = "2.1.221"
ARMS = ("collie", "claude")
REPETITIONS = 3
MAX_TURNS = 12
AGENT_WALL_SECONDS = 300
GRADER_WALL_SECONDS = 30
PLANNED_LAUNCHES = len(TASKS) * REPETITIONS * len(ARMS)
CLAIM = "observed_restricted_product_ranking_on_2_frozen_synthetic_tasks"
VALID_STATUSES = frozenset({"valid_resolved", "valid_unresolved"})
INVALID_STATUSES = frozenset({
    "invalid_infrastructure", "invalid_unknown", "not_admitted",
})
RANKING_SOURCE_PATHS = (
    ".dockerignore", ".gitignore", "bench/subscription-rank.Dockerfile",
    "bench/subscription_guard.py", "bench/subscription_rank.py",
    "bench/subscription_rank_tasks.py", "bench/subscription_rank_worker.py",
    "harness/loop.py", "harness/recorder.py", "harness/swe.py",
    "tests/test_subscription_product_eval.py", "tests/test_subscription_rank.py",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("wb") as handle:
        handle.write(_canonical_bytes(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("JSON receipt is not an object: %s" % path)
    return value


def canonical_plan() -> list[dict[str, Any]]:
    """Return the frozen AB/BA schedule (each arm appears first exactly three times)."""
    plan: list[dict[str, Any]] = []
    slot = 0
    for task_index, task in enumerate(TASKS):
        for repetition in range(1, REPETITIONS + 1):
            order = ARMS if (task_index + repetition - 1) % 2 == 0 else tuple(reversed(ARMS))
            for position, arm in enumerate(order, 1):
                slot += 1
                plan.append({
                    "slot": slot,
                    "run_id": "%02d-%s-r%d-p%d-%s" % (
                        slot, task["task_id"], repetition, position, arm),
                    "task_id": task["task_id"],
                    "task_sha256": task_sha256(task),
                    "repetition": repetition,
                    "position": position,
                    "arm": arm,
                    "attempt": 1,
                })
    if len(plan) != 12 or len({row["run_id"] for row in plan}) != 12:
        raise AssertionError("ranking schedule must contain exactly twelve unique launches")
    for arm in ARMS:
        if sum(row["position"] == 1 and row["arm"] == arm for row in plan) != 3:
            raise AssertionError("AB/BA schedule is not position-balanced")
    return plan


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 60,
         check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=str(cwd) if cwd else None, capture_output=True,
                            text=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError("command failed (%d): %s" % (result.returncode, command[0]))
    return result


def _docker_mount(source: Path, destination: str, *, readonly: bool = False) -> str:
    value = "type=bind,src=%s,dst=%s" % (source.resolve(), destination)
    return value + (",readonly" if readonly else "")


def _container_base(image: str, *, network: str) -> list[str]:
    return [
        "docker", "run", "--network", network, "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--memory", "3g", "--cpus", "2",
        "--pids-limit", "256", "--read-only", "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=268435456", "--env", "HOME=/home/runner",
        "--env", "PYTHONDONTWRITEBYTECODE=1", image,
    ]


def _cleanup_ephemeral_container(name: str) -> bool:
    if _run(["docker", "inspect", name], timeout=10).returncode != 0:
        return True
    return _remove_container(name)


def _run_ephemeral_container(command: list[str], *, purpose: str,
                             timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a named container and prove it is absent again, including on host timeout."""
    name = "collie-rank-%s-%s" % (purpose, uuid.uuid4().hex[:12])
    named = command[:2] + ["--name", name] + command[2:]
    result: subprocess.CompletedProcess[str] | None = None
    primary_error: BaseException | None = None
    try:
        result = _run(named, timeout=timeout)
    except BaseException as exc:
        primary_error = exc
    cleanup_ok = _cleanup_ephemeral_container(name)
    if not cleanup_ok:
        raise RuntimeError("container cleanup could not be confirmed: %s" % purpose)
    if primary_error is not None:
        raise primary_error
    assert result is not None
    return result


def _git_revision_and_clean() -> str:
    revision = _run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True).stdout.strip()
    tracked = _run(["git", "ls-files", "--error-unmatch", *RANKING_SOURCE_PATHS], cwd=ROOT)
    clean = _run(["git", "diff", "--quiet", "HEAD", "--", *RANKING_SOURCE_PATHS], cwd=ROOT)
    if tracked.returncode or clean.returncode:
        raise RuntimeError("refusing to rank uncommitted benchmark sources; commit them first")
    return revision


def _build_image(tag: str, revision: str) -> str:
    # Build from the committed tree, not the shared working directory.  Unrelated user edits may
    # coexist in this checkout and must neither be reverted nor leak into the frozen agent image.
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision, "harness",
         "bench/subscription_guard.py", "bench/subscription_rank_worker.py",
         "bench/subscription-rank.Dockerfile"],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if archive.returncode:
        raise RuntimeError("could not create committed Docker build context")
    context = Path(tempfile.mkdtemp(prefix="subscription-rank-build-", dir=TEMP_ROOT))
    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
            for member in bundle.getmembers():
                target = (context / member.name).resolve()
                if context.resolve() not in target.parents and target != context.resolve():
                    raise RuntimeError("unsafe path in git archive")
            bundle.extractall(context)
        _run(["docker", "build", "--pull=false", "-f",
              "bench/subscription-rank.Dockerfile", "-t", tag, "."],
             cwd=context, timeout=600, check=True)
    finally:
        shutil.rmtree(context, ignore_errors=True)
    result = _run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], check=True)
    image_id = result.stdout.strip()
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise RuntimeError("Docker returned an invalid image ID")
    return image_id


def _credential_path() -> Path:
    path = Path.home() / ".claude" / ".credentials.json"
    if not path.is_file():
        raise RuntimeError("Claude subscription credential file is unavailable")
    return path.resolve()


def _fresh_home(root: Path) -> Path:
    home = root / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=False)
    # The nested read-only credential bind needs a target below the outer home bind on Docker
    # Desktop.  The placeholder is never read because the real file is mounted over it.
    (home / ".claude" / ".credentials.json").write_text("", encoding="utf-8")
    return home


def _agent_mounts(home: Path, workspace: Path, input_dir: Path, output_dir: Path,
                  credential: Path) -> list[str]:
    return [
        "--mount", _docker_mount(home, "/home/runner"),
        "--mount", _docker_mount(credential, "/home/runner/.claude/.credentials.json",
                                  readonly=True),
        "--mount", _docker_mount(workspace, "/workspace"),
        "--mount", _docker_mount(input_dir, "/input", readonly=True),
        "--mount", _docker_mount(output_dir, "/output"),
    ]


def _isolation_canary(image: str, temp_root: Path) -> dict[str, Any]:
    root = temp_root / "isolation-canary"
    workspace = root / "workspace"
    output = root / "output"
    workspace.mkdir(parents=True)
    output.mkdir()
    script = (
        "from pathlib import Path; "
        "assert not Path('/evaluator').exists(); "
        "assert not Path('/grader').exists(); "
        "assert not Path('/host-repo').exists(); "
        "assert not Path('/opt/collie/bench/subscription_rank_tasks.py').exists(); "
        "Path('/workspace/canary.txt').write_text('ok', encoding='utf-8')"
    )
    command = _container_base(image, network="none")
    command[command.index(image):command.index(image)] = [
        "--mount", _docker_mount(workspace, "/workspace"),
        "--mount", _docker_mount(output, "/output"),
        "--entrypoint", "python3",
    ]
    command += ["-c", script]
    result = _run_ephemeral_container(command, purpose="isolation", timeout=30)
    ok = result.returncode == 0 and (workspace / "canary.txt").read_text(
        encoding="utf-8") == "ok"
    if not ok:
        raise RuntimeError("agent filesystem isolation canary failed")
    return {
        "worker_identity_can_write_fixture": True,
        "evaluator_mount_absent": True,
        "hidden_task_module_absent_from_image": True,
        "network": "none_during_canary",
    }


def _subscription_preflight(image: str, credential: Path,
                            temp_root: Path) -> dict[str, Any]:
    root = temp_root / "subscription-preflight"
    workspace = root / "workspace"
    input_dir = root / "input"
    output = root / "output"
    for path in (workspace, input_dir, output):
        path.mkdir(parents=True)
    home = _fresh_home(root)
    command = _container_base(image, network="bridge")
    insertion = command.index(image)
    command[insertion:insertion] = _agent_mounts(
        home, workspace, input_dir, output, credential)
    command += ["--check-only"]
    result = _run_ephemeral_container(command, purpose="auth", timeout=30)
    receipt_path = output / "worker.json"
    if result.returncode or not receipt_path.is_file():
        raise RuntimeError("subscription preflight failed")
    receipt = _load_json(receipt_path)
    if (receipt.get("worker_outcome") != "preflight_ok"
            or receipt.get("claude_cli_version") != CLAUDE_CLI_VERSION + " (Claude Code)"
            or (receipt.get("subscription_guard") or {}).get("verdict") != "allow"):
        raise RuntimeError("subscription preflight receipt is invalid")
    return receipt


def _grader_command(image: str, workspace: Path, grader_dir: Path) -> list[str]:
    command = _container_base(image, network="none")
    insertion = command.index(image)
    command[insertion:insertion] = [
        "--mount", _docker_mount(workspace, "/workspace", readonly=True),
        "--mount", _docker_mount(grader_dir, "/grader", readonly=True),
        "--workdir", "/workspace", "--entrypoint", "python3",
    ]
    command += ["-I", "/grader/grader.py"]
    return command


def _grade(image: str, task: Mapping[str, Any], workspace: Path,
           grader_dir: Path) -> dict[str, Any]:
    grader_dir.mkdir(parents=True, exist_ok=False)
    wrapper = "import sys\nsys.path.insert(0, '/workspace')\n" + task["hidden_grader"]
    grader_file = grader_dir / "grader.py"
    grader_file.write_text(wrapper, encoding="utf-8", newline="\n")
    try:
        result = _run_ephemeral_container(
            _grader_command(image, workspace, grader_dir), purpose="grader",
            timeout=GRADER_WALL_SECONDS)
    except subprocess.TimeoutExpired:
        return {"outcome": "grader_infrastructure_error", "error_code": "grader_timeout",
                "resolved": None}
    if result.returncode == 0:
        return {"outcome": "graded", "returncode": 0, "resolved": True}
    if result.returncode == 1:
        return {"outcome": "graded", "returncode": 1, "resolved": False}
    return {"outcome": "grader_infrastructure_error",
            "error_code": "grader_container_exit_%d" % result.returncode,
            "resolved": None}


def _task_preflight(image: str, temp_root: Path) -> list[dict[str, Any]]:
    # Run the in-process structural check and then independently validate both states in the
    # exact networkless grader container used after each agent attempt.
    task_self_check()
    receipts = []
    for index, task in enumerate(TASKS):
        root = temp_root / ("task-preflight-%d" % index)
        baseline = root / "baseline"
        gold_source = root / "gold-source"
        gold_evaluator = root / "gold-evaluator"
        _prepare_git_fixture(task, baseline)
        _prepare_git_fixture(task, gold_source)
        materialize_task(task, gold_source, gold=True)
        gold_patch_result = _run(
            ["git", "diff", "--binary", "HEAD", "--"], cwd=gold_source, check=True)
        gold_patch = gold_patch_result.stdout
        if not gold_patch:
            raise RuntimeError("gold patch is unexpectedly empty: %s" % task["task_id"])
        _prepare_git_fixture(task, gold_evaluator)
        if not _apply_patch(gold_evaluator, gold_patch, root / "gold.diff"):
            raise RuntimeError("gold patch pipeline failed: %s" % task["task_id"])
        baseline_grade = _grade(image, task, baseline, root / "baseline-grader")
        gold_grade = _grade(image, task, gold_evaluator, root / "gold-grader")
        if baseline_grade.get("resolved") is not False or gold_grade.get("resolved") is not True:
            raise RuntimeError("task red/green preflight failed: %s" % task["task_id"])
        receipts.append({
            "task_id": task["task_id"],
            "task_sha256": task_sha256(task),
            "fixture_sha256": canonical_sha256(task["fixture_files"]),
            "prompt_sha256": _sha_bytes(task["prompt"].encode("utf-8")),
            "grader_sha256": _sha_bytes(task["hidden_grader"].encode("utf-8")),
            "gold_sha256": canonical_sha256(task["gold_files"]),
            "gold_patch_sha256": _sha_bytes(gold_patch.encode("utf-8")),
            "baseline_resolved": False,
            "gold_resolved": True,
        })
    return receipts


def _manifest_core(image_id: str, revision: str, plan: list[dict[str, Any]],
                   task_receipts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "suite_id": "collie-opus-subscription-local-v1",
        "claim": CLAIM,
        "scope": "exploratory",
        "publishable": False,
        "track": "subscription_native_product",
        "git_revision": revision,
        "runner_sha256": _sha_file(Path(__file__)),
        "worker_sha256": _sha_file(WORKER),
        "dockerfile_sha256": _sha_file(DOCKERFILE),
        "image_id": image_id,
        "base_image": BASE_IMAGE,
        "claude_cli_version": CLAUDE_CLI_VERSION,
        "model_alias": MODEL_ALIAS,
        "model_alias_is_rolling": True,
        "arms": list(ARMS),
        "tasks": task_receipts,
        "repetitions_per_task_arm": REPETITIONS,
        "max_turns": MAX_TURNS,
        "agent_wall_seconds": AGENT_WALL_SECONDS,
        "grader_wall_seconds": GRADER_WALL_SECONDS,
        "planned_launches": PLANNED_LAUNCHES,
        "schedule": plan,
        "ranking_rule": "task_balanced_hidden_test_solve_rate; exact_ties_share_rank",
        "evaluator_retries": 0,
        "collie_loop_retries": 0,
        "native_product_internal_retries": "not_controlled_or_observed",
        "seed_control": "provider_unavailable",
        "timeout_usage_rule": (
            "A confirmed outer wall timeout is a valid unresolved correctness outcome when the "
            "per-slot subscription admission receipt exists; missing efficiency usage remains "
            "unknown and is never imputed."
        ),
        "billing": {
            "expected_marginal_charge_usd": 0,
            "metered_api_fallback_disabled": True,
            "api_equivalent_cost_is_not_a_charge": True,
        },
    }


def _prepare_git_fixture(task: Mapping[str, Any], workspace: Path) -> None:
    materialize_task(task, workspace)
    for arguments in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "ranking@collie.run"],
        ["git", "config", "user.name", "Collie Ranking"],
        ["git", "add", "-A"],
        ["git", "commit", "--quiet", "-m", "frozen baseline"],
    ):
        _run(arguments, cwd=workspace, check=True)


def _apply_patch(workspace: Path, patch: str, patch_file: Path) -> bool:
    """Apply a git diff only inside an evaluator repository rooted at ``workspace``."""
    if not (workspace / ".git").is_dir():
        raise RuntimeError("evaluator workspace is not an independent git repository")
    if not patch:
        return True
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(patch, encoding="utf-8", newline="\n")
    check = _run(["git", "apply", "--check", str(patch_file)], cwd=workspace)
    if check.returncode:
        return False
    applied = _run(["git", "apply", str(patch_file)], cwd=workspace)
    return applied.returncode == 0


def _remove_container(name: str) -> bool:
    result = _run(["docker", "rm", "--force", name], timeout=30)
    inspect = _run(["docker", "inspect", name], timeout=10)
    return result.returncode == 0 and inspect.returncode != 0


def _container_name(suite_sha: str, slot: int) -> str:
    return "collie-rank-%s-%02d" % (suite_sha[:10], slot)


def _stop_orphan_if_present(suite_sha: str, slot: int) -> None:
    name = _container_name(suite_sha, slot)
    if _run(["docker", "inspect", name], timeout=10).returncode == 0:
        if not _remove_container(name):
            raise RuntimeError("could not terminate interrupted attempt container: %s" % name)


def _run_agent(image: str, credential: Path, suite_sha: str,
               row: Mapping[str, Any], run_dir: Path, temp_suite: Path) -> dict[str, Any]:
    task = task_by_id(row["task_id"])
    root = temp_suite / row["run_id"]
    workspace = root / "workspace"
    input_dir = root / "input"
    output = root / "output"
    input_dir.mkdir(parents=True)
    output.mkdir()
    home = _fresh_home(root)
    _prepare_git_fixture(task, workspace)
    _atomic_json(input_dir / "task.json", {
        "task_id": task["task_id"], "prompt": task["prompt"],
        "prompt_sha256": _sha_bytes(task["prompt"].encode("utf-8")),
    })

    reservation = {
        "schema_version": 1,
        "suite_sha256": suite_sha,
        "run_id": row["run_id"],
        "slot": row["slot"],
        "task_id": row["task_id"],
        "task_sha256": row["task_sha256"],
        "repetition": row["repetition"],
        "position": row["position"],
        "arm": row["arm"],
        "attempt": 1,
        "reserved_at_utc": _utc_now(),
    }
    _atomic_json(run_dir / "reservation.json", reservation)
    reservation_sha = _sha_file(run_dir / "reservation.json")

    name = _container_name(suite_sha, row["slot"])
    command = _container_base(image, network="bridge")
    insertion = command.index(image)
    command[insertion:insertion] = ["--name", name, "--stop-timeout", "1"] + _agent_mounts(
        home, workspace, input_dir, output, credential)
    command += [
        "--arm", row["arm"], "--task-json", "/input/task.json",
        "--workspace", "/workspace", "--output", "/output/worker.json",
        "--model", MODEL_ALIAS, "--max-turns", str(MAX_TURNS),
    ]

    timed_out = False
    container_killed = False
    docker_returncode: int | None = None
    try:
        result = _run(command, timeout=AGENT_WALL_SECONDS)
        docker_returncode = result.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        container_killed = _remove_container(name)
    else:
        # The stopped container is kept only long enough to prove its process is gone.  Removing it
        # also prevents any later attempt from observing a predecessor's filesystem.
        removed = _remove_container(name)
        if not removed:
            return _terminal_result(row, suite_sha, reservation_sha,
                                    "invalid_infrastructure", "container_cleanup_failed")

    patch = ""
    worker: dict[str, Any] = {}
    worker_path = output / "worker.json"
    if worker_path.is_file():
        try:
            worker = _load_json(worker_path)
            patch_value = worker.get("patch")
            patch = patch_value if isinstance(patch_value, str) else ""
        except (OSError, ValueError, RuntimeError):
            worker = {}
    admission: dict[str, Any] = {}
    admission_path = output / "admission.json"
    if admission_path.is_file():
        try:
            admission = _load_json(admission_path)
        except (OSError, ValueError, RuntimeError):
            admission = {}
    admission_valid = (
        admission.get("schema_version") == 1
        and admission.get("worker_outcome") == "admitted"
        and admission.get("arm") == row["arm"]
        and admission.get("task_id") == row["task_id"]
        and admission.get("model_alias") == MODEL_ALIAS
        and admission.get("claude_cli_version") == CLAUDE_CLI_VERSION + " (Claude Code)"
        and (admission.get("subscription_guard") or {}).get("verdict") == "allow"
    )
    if admission_valid:
        worker.setdefault("subscription_guard", admission["subscription_guard"])
        worker.setdefault("claude_cli_version", admission["claude_cli_version"])
    _atomic_text(run_dir / "patch.diff", patch)

    if timed_out:
        if not container_killed:
            return _terminal_result(row, suite_sha, reservation_sha,
                                    "invalid_infrastructure", "container_kill_unconfirmed",
                                    patch=patch, worker=worker)
        if not admission_valid:
            return _terminal_result(row, suite_sha, reservation_sha,
                                    "invalid_infrastructure", "admission_receipt_missing",
                                    patch=patch, worker=worker)
        return _terminal_result(row, suite_sha, reservation_sha, "valid_unresolved",
                                "agent_wall_budget_exhausted", patch=patch, worker=worker,
                                grader={"outcome": "not_run_wall_budget", "resolved": False})

    if not worker:
        return _terminal_result(row, suite_sha, reservation_sha,
                                "invalid_infrastructure", "worker_receipt_missing_or_invalid",
                                patch=patch, worker=worker,
                                docker_returncode=docker_returncode)
    if worker.get("worker_outcome") == "candidate" and not admission_valid:
        return _terminal_result(row, suite_sha, reservation_sha,
                                "invalid_infrastructure", "admission_receipt_missing",
                                patch=patch, worker=worker,
                                docker_returncode=docker_returncode)
    if (admission_valid and worker.get("worker_outcome") == "candidate"
            and worker.get("subscription_guard") != admission.get("subscription_guard")):
        return _terminal_result(row, suite_sha, reservation_sha,
                                "invalid_infrastructure", "admission_receipt_mismatch",
                                patch=patch, worker=worker,
                                docker_returncode=docker_returncode)
    worker_outcome = worker.get("worker_outcome")
    if worker_outcome == "not_admitted":
        return _terminal_result(row, suite_sha, reservation_sha, "not_admitted",
                                str(worker.get("error_code") or "subscription_guard_denied"),
                                patch=patch, worker=worker,
                                docker_returncode=docker_returncode)
    if (worker.get("schema_version") != 1 or worker.get("arm") != row["arm"]
            or worker.get("task_id") != row["task_id"]
            or worker.get("model_alias") != MODEL_ALIAS
            or (worker.get("subscription_guard") or {}).get("verdict") != "allow"):
        return _terminal_result(row, suite_sha, reservation_sha,
                                "invalid_infrastructure", "worker_receipt_mismatch",
                                patch=patch, worker=worker,
                                docker_returncode=docker_returncode)
    if worker_outcome != "candidate":
        return _terminal_result(row, suite_sha, reservation_sha,
                                "invalid_infrastructure",
                                str(worker.get("error_code") or "worker_infrastructure_failure"),
                                patch=patch, worker=worker,
                                docker_returncode=docker_returncode)
    if docker_returncode != 0:
        return _terminal_result(row, suite_sha, reservation_sha,
                                "invalid_infrastructure", "agent_container_nonzero_exit",
                                patch=patch, worker=worker,
                                docker_returncode=docker_returncode)

    evaluator = root / "evaluator"
    _prepare_git_fixture(task, evaluator)
    if not _apply_patch(evaluator, patch, root / "candidate.diff"):
        return _terminal_result(row, suite_sha, reservation_sha,
                                "invalid_infrastructure", "patch_application_failed",
                                patch=patch, worker=worker,
                                docker_returncode=docker_returncode)
    grader = _grade(image, task, evaluator, root / "hidden-grader")
    grader.update({
        "task_sha256": row["task_sha256"],
        "fixture_sha256": canonical_sha256(task["fixture_files"]),
        "grader_sha256": _sha_bytes(task["hidden_grader"].encode("utf-8")),
        "patch_sha256": _sha_bytes(patch.encode("utf-8")),
    })
    _atomic_json(run_dir / "grader.json", grader)
    if grader.get("outcome") != "graded":
        return _terminal_result(row, suite_sha, reservation_sha,
                                "invalid_infrastructure",
                                str(grader.get("error_code") or "grader_failure"),
                                patch=patch, worker=worker, grader=grader,
                                docker_returncode=docker_returncode)
    exhausted = bool(worker.get("turns_exhausted"))
    status = ("valid_resolved" if grader.get("resolved") is True and not exhausted
              else "valid_unresolved")
    code = ("" if status == "valid_resolved" else
            "turn_budget_exhausted" if exhausted else "hidden_contract_failed")
    return _terminal_result(row, suite_sha, reservation_sha, status, code,
                            patch=patch, worker=worker, grader=grader,
                            docker_returncode=docker_returncode)


def _terminal_result(row: Mapping[str, Any], suite_sha: str, reservation_sha: str,
                     status: str, error_code: str, *, patch: str = "",
                     worker: Mapping[str, Any] | None = None,
                     grader: Mapping[str, Any] | None = None,
                     docker_returncode: int | None = None) -> dict[str, Any]:
    if status not in VALID_STATUSES | INVALID_STATUSES:
        raise ValueError("unknown terminal status")
    worker = worker or {}
    usage = worker.get("usage") if isinstance(worker.get("usage"), dict) else {}
    return {
        "schema_version": 1,
        "suite_sha256": suite_sha,
        "run_id": row["run_id"],
        "slot": row["slot"],
        "task_id": row["task_id"],
        "task_sha256": row["task_sha256"],
        "repetition": row["repetition"],
        "position": row["position"],
        "arm": row["arm"],
        "attempt": 1,
        "status": status,
        "resolved": status == "valid_resolved",
        "error_code": error_code,
        "reservation_sha256": reservation_sha,
        "patch_sha256": _sha_bytes(patch.encode("utf-8")),
        "patch_bytes": len(patch.encode("utf-8")),
        "duration_ms": worker.get("duration_ms"),
        "turns_exhausted": bool(worker.get("turns_exhausted", False)),
        "usage": usage,
        "grader": grader or {"outcome": "not_run", "resolved": None},
        "docker_returncode": docker_returncode,
        "subscription_guard": worker.get("subscription_guard"),
        "claude_cli_version": worker.get("claude_cli_version"),
        "completed_at_utc": _utc_now(),
    }


def _recover_interrupted(row: Mapping[str, Any], suite_sha: str,
                         run_dir: Path) -> dict[str, Any]:
    reservation = _load_json(run_dir / "reservation.json")
    if (reservation.get("suite_sha256") != suite_sha
            or reservation.get("run_id") != row["run_id"]
            or reservation.get("attempt") != 1):
        raise RuntimeError("reservation receipt mismatch: %s" % row["run_id"])
    return _terminal_result(
        row, suite_sha, _sha_file(run_dir / "reservation.json"), "invalid_unknown",
        "interrupted_after_reservation_no_retry",
    )


def _reset_unreserved_staging(temp_suite: Path, run_id: str) -> None:
    target = (temp_suite / run_id).resolve()
    root = temp_suite.resolve()
    if root not in target.parents:
        raise RuntimeError("unsafe staging path")
    if target.exists():
        shutil.rmtree(target)


def _component_total(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> int | None:
    values = []
    for row in rows:
        usage = row.get("usage") or {}
        if not all(isinstance(usage.get(key), (int, float)) and not isinstance(
                usage.get(key), bool) for key in keys):
            return None
        values.append(sum(usage[key] for key in keys))
    return int(sum(values))


def _result_validation_errors(plan: list[dict[str, Any]],
                              results: list[dict[str, Any]],
                              suite_sha: str) -> list[dict[str, str]]:
    """Validate identity and semantic invariants before any score is computed."""
    expected_by_id = {row["run_id"]: row for row in plan}
    seen: set[str] = set()
    errors: list[dict[str, str]] = []
    identity_fields = (
        "slot", "task_id", "task_sha256", "repetition", "position", "arm", "attempt",
    )
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            errors.append({"run_id": "row-%d" % index, "error": "result_not_object"})
            continue
        run_id = result.get("run_id")
        label = run_id if isinstance(run_id, str) else "row-%d" % index
        if not isinstance(run_id, str) or run_id not in expected_by_id:
            errors.append({"run_id": label, "error": "unknown_run_id"})
            continue
        if run_id in seen:
            errors.append({"run_id": run_id, "error": "duplicate_run_id"})
            continue
        seen.add(run_id)
        expected = expected_by_id[run_id]
        if result.get("suite_sha256") != suite_sha:
            errors.append({"run_id": run_id, "error": "suite_sha256_mismatch"})
        for field in identity_fields:
            if result.get(field) != expected[field]:
                errors.append({"run_id": run_id, "error": field + "_mismatch"})
        status = result.get("status")
        if status not in VALID_STATUSES | INVALID_STATUSES:
            errors.append({"run_id": run_id, "error": "unknown_status"})
        expected_resolved = status == "valid_resolved"
        if type(result.get("resolved")) is not bool or result.get("resolved") != expected_resolved:
            errors.append({"run_id": run_id, "error": "status_resolved_mismatch"})
        if not isinstance(result.get("usage"), dict):
            errors.append({"run_id": run_id, "error": "usage_not_object"})
        if not isinstance(result.get("grader"), dict):
            errors.append({"run_id": run_id, "error": "grader_not_object"})
    for run_id in expected_by_id.keys() - seen:
        errors.append({"run_id": run_id, "error": "missing_result"})
    return errors


def _artifact_validation_errors(planned: Mapping[str, Any], result: Mapping[str, Any],
                                run_dir: Path, suite_sha: str) -> list[str]:
    """Bind a terminal row to its immutable reservation, patch, usage, and grader receipts."""
    errors = [item["error"] for item in _result_validation_errors(
        [dict(planned)], [dict(result)], suite_sha)]
    reservation_path = run_dir / "reservation.json"
    patch_path = run_dir / "patch.diff"
    usage_path = run_dir / "usage.json"
    grader_path = run_dir / "grader.json"
    for path, code in ((reservation_path, "reservation_missing"),
                       (patch_path, "patch_missing"),
                       (usage_path, "usage_receipt_missing"),
                       (grader_path, "grader_receipt_missing")):
        if not path.is_file():
            errors.append(code)
    if errors:
        return sorted(set(errors))
    try:
        reservation = _load_json(reservation_path)
        usage_receipt = _load_json(usage_path)
        grader_receipt = _load_json(grader_path)
    except (OSError, ValueError, RuntimeError):
        return ["artifact_json_invalid"]
    for field in ("suite_sha256", "run_id", "slot", "task_id", "task_sha256",
                  "repetition", "position", "arm", "attempt"):
        expected = suite_sha if field == "suite_sha256" else planned[field]
        if reservation.get(field) != expected:
            errors.append("reservation_%s_mismatch" % field)
    if result.get("reservation_sha256") != _sha_file(reservation_path):
        errors.append("reservation_hash_mismatch")
    patch_bytes = patch_path.read_bytes()
    if result.get("patch_sha256") != _sha_bytes(patch_bytes):
        errors.append("patch_hash_mismatch")
    if result.get("patch_bytes") != len(patch_bytes):
        errors.append("patch_size_mismatch")
    if (usage_receipt.get("suite_sha256") != suite_sha
            or usage_receipt.get("run_id") != planned["run_id"]
            or usage_receipt.get("usage") != result.get("usage")):
        errors.append("usage_receipt_mismatch")
    if grader_receipt != result.get("grader"):
        errors.append("grader_receipt_mismatch")
    status = result.get("status")
    error_code = result.get("error_code")
    task = task_by_id(planned["task_id"])
    expected_grader_bindings = {
        "task_sha256": planned["task_sha256"],
        "fixture_sha256": canonical_sha256(task["fixture_files"]),
        "grader_sha256": _sha_bytes(task["hidden_grader"].encode("utf-8")),
        "patch_sha256": _sha_bytes(patch_bytes),
    }
    if grader_receipt.get("outcome") == "graded":
        for field, expected in expected_grader_bindings.items():
            if grader_receipt.get(field) != expected:
                errors.append("grader_%s_mismatch" % field)
    if status == "valid_resolved":
        if (grader_receipt.get("outcome") != "graded"
                or grader_receipt.get("resolved") is not True
                or bool(result.get("turns_exhausted"))
                or result.get("docker_returncode") != 0
                or error_code != ""):
            errors.append("resolved_semantics_invalid")
    elif status == "valid_unresolved":
        if error_code == "hidden_contract_failed":
            if (grader_receipt.get("outcome") != "graded"
                    or grader_receipt.get("resolved") is not False
                    or bool(result.get("turns_exhausted"))
                    or result.get("docker_returncode") != 0):
                errors.append("hidden_failure_semantics_invalid")
        elif error_code == "turn_budget_exhausted":
            if (grader_receipt.get("outcome") != "graded"
                    or type(grader_receipt.get("resolved")) is not bool
                    or not bool(result.get("turns_exhausted"))
                    or result.get("docker_returncode") != 0):
                errors.append("turn_budget_semantics_invalid")
        elif error_code == "agent_wall_budget_exhausted":
            if (grader_receipt.get("outcome") != "not_run_wall_budget"
                    or grader_receipt.get("resolved") is not False
                    or result.get("docker_returncode") is not None):
                errors.append("wall_budget_semantics_invalid")
        else:
            errors.append("unresolved_error_code_invalid")
    if result.get("status") in VALID_STATUSES:
        if (result.get("subscription_guard") or {}).get("verdict") != "allow":
            errors.append("subscription_admission_missing")
        if result.get("claude_cli_version") != CLAUDE_CLI_VERSION + " (Claude Code)":
            errors.append("cli_version_mismatch")
    return sorted(set(errors))


def summarize(plan: list[dict[str, Any]], results: list[dict[str, Any]],
              suite_sha: str) -> dict[str, Any]:
    by_id = {row.get("run_id"): row for row in results}
    missing = [row["run_id"] for row in plan if row["run_id"] not in by_id]
    invalid = [row for row in results if row.get("status") not in VALID_STATUSES]
    validation_errors = _result_validation_errors(plan, results, suite_sha)
    ranking_withheld = bool(missing or invalid or validation_errors or len(results) != 12)
    scores: dict[str, Any] | None = None
    ranking: list[dict[str, Any]] | None = None
    if not ranking_withheld:
        scores = {}
        for arm in ARMS:
            task_rates = {}
            arm_rows = [row for row in results if row["arm"] == arm]
            for task in TASKS:
                cells = [row for row in arm_rows if row["task_id"] == task["task_id"]]
                task_rates[task["task_id"]] = sum(bool(row["resolved"]) for row in cells) / 3
            score = sum(task_rates.values()) / len(task_rates)
            durations = [row["duration_ms"] for row in arm_rows
                         if isinstance(row.get("duration_ms"), (int, float))]
            scores[arm] = {
                "task_solve_rates": task_rates,
                "task_balanced_solve_rate": score,
                "resolved": sum(bool(row["resolved"]) for row in arm_rows),
                "attempts": len(arm_rows),
                "median_duration_ms": statistics.median(durations) if durations else None,
                "processed_tokens_total": _component_total(
                    arm_rows, ("input_tokens", "output_tokens", "cache_read", "cache_creation")),
            }
        ordered = sorted(ARMS, key=lambda arm: (-scores[arm]["task_balanced_solve_rate"], arm))
        ranking = []
        previous_score = None
        previous_rank = 0
        for index, arm in enumerate(ordered, 1):
            score = scores[arm]["task_balanced_solve_rate"]
            rank = previous_rank if previous_score == score else index
            ranking.append({"rank": rank, "arm": arm, "score": score})
            previous_score, previous_rank = score, rank

    paired = {"both": 0, "neither": 0, "collie_only": 0, "claude_only": 0}
    if not ranking_withheld:
        for task in TASKS:
            for repetition in range(1, 4):
                cell = {row["arm"]: bool(row["resolved"]) for row in results
                        if row["task_id"] == task["task_id"]
                        and row["repetition"] == repetition}
                key = ("both" if cell == {"collie": True, "claude": True}
                       else "neither" if cell == {"collie": False, "claude": False}
                       else "collie_only" if cell.get("collie") else "claude_only")
                paired[key] += 1

    return {
        "schema_version": 1,
        "suite_sha256": suite_sha,
        "claim": CLAIM,
        "scope": "exploratory",
        "publishable": False,
        "ranking_withheld": ranking_withheld,
        "ranking": ranking,
        "scores": scores,
        "paired_cells": paired if not ranking_withheld else None,
        "planned_launches": 12,
        "terminal_results": len(results),
        "missing_run_ids": missing,
        "invalid_run_ids": [row.get("run_id") for row in invalid],
        "validation_errors": validation_errors,
        "status_counts": {status: sum(row.get("status") == status for row in results)
                          for status in sorted(VALID_STATUSES | INVALID_STATUSES)},
        "limitations": [
            "Only two synthetic task clusters; repetitions are not independent tasks.",
            "No confidence interval or statistical-significance claim is warranted.",
            "This compares complete products, not an isolated harness effect.",
            "The opus model name is a rolling alias rather than an immutable snapshot.",
            "Token accounting differs by product and is descriptive only.",
            "Latency and tokens never break a capability tie.",
        ],
        "generated_at_utc": _utc_now(),
    }


@contextmanager
def _suite_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def execute(*, preflight_only: bool = False, image_tag: str = IMAGE_TAG) -> int:
    plan = canonical_plan()
    revision = _git_revision_and_clean()
    credential = _credential_path()
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    preflight_root = Path(tempfile.mkdtemp(prefix="subscription-rank-preflight-", dir=TEMP_ROOT))
    try:
        image_id = _build_image(image_tag, revision)
        isolation = _isolation_canary(image_id, preflight_root)
        subscription = _subscription_preflight(image_id, credential, preflight_root)
        task_receipts = _task_preflight(image_id, preflight_root)
        core = _manifest_core(image_id, revision, plan, task_receipts)
        suite_sha = _sha_bytes(_canonical_bytes(core))
        if preflight_only:
            print(json.dumps({
                "outcome": "preflight_ok", "suite_sha256": suite_sha,
                "image_id": image_id, "isolation": isolation,
                "subscription_guard": subscription["subscription_guard"],
                "tasks": task_receipts,
            }, ensure_ascii=False, indent=2))
            return 0

        result_dir = RESULTS_ROOT / ("subscription-rank-v1-" + suite_sha[:12])
        temp_suite = TEMP_ROOT / ("subscription-rank-v1-" + suite_sha[:12])
        temp_suite.mkdir(parents=True, exist_ok=True)
        manifest_path = result_dir / "manifest.json"
        with _suite_lock(result_dir / ".lock"):
            if manifest_path.exists():
                existing = _load_json(manifest_path)
                existing_core = {key: existing.get(key) for key in core}
                if (existing.get("suite_sha256") != suite_sha
                        or existing_core != core
                        or _sha_bytes(_canonical_bytes(existing_core)) != suite_sha):
                    raise RuntimeError("existing manifest does not match this frozen suite")
            else:
                manifest = dict(core)
                manifest.update({
                    "suite_sha256": suite_sha,
                    "created_at_utc": _utc_now(),
                    "preflight": {"isolation": isolation, "subscription": subscription},
                })
                _atomic_json(manifest_path, manifest)

            rows: list[dict[str, Any]] = []
            for planned in plan:
                run_dir = result_dir / "runs" / planned["run_id"]
                result_path = run_dir / "result.json"
                reservation_path = run_dir / "reservation.json"
                if result_path.exists():
                    terminal = _load_json(result_path)
                elif reservation_path.exists():
                    _stop_orphan_if_present(suite_sha, planned["slot"])
                    terminal = _recover_interrupted(planned, suite_sha, run_dir)
                else:
                    reservations = list((result_dir / "runs").glob("*/reservation.json"))
                    if len(reservations) >= PLANNED_LAUNCHES:
                        raise RuntimeError("twelve launch reservations already exist")
                    _reset_unreserved_staging(temp_suite, planned["run_id"])
                    terminal = _run_agent(
                        image_id, credential, suite_sha, planned, run_dir, temp_suite)
                if not result_path.exists():
                    if not (run_dir / "patch.diff").exists():
                        _atomic_text(run_dir / "patch.diff", "")
                    _atomic_json(run_dir / "usage.json", {
                        "schema_version": 1,
                        "suite_sha256": suite_sha,
                        "run_id": planned["run_id"],
                        "usage": terminal.get("usage") or {},
                    })
                    if not (run_dir / "grader.json").exists():
                        _atomic_json(run_dir / "grader.json", terminal.get("grader") or {
                            "outcome": "not_run", "resolved": None,
                        })
                    _atomic_json(result_path, terminal)
                artifact_errors = _artifact_validation_errors(
                    planned, terminal, run_dir, suite_sha)
                if artifact_errors:
                    _atomic_json(run_dir / "integrity.json", {
                        "schema_version": 1,
                        "suite_sha256": suite_sha,
                        "run_id": planned["run_id"],
                        "verdict": "invalid",
                        "errors": artifact_errors,
                        "checked_at_utc": _utc_now(),
                    })
                    terminal = {
                        **planned,
                        "schema_version": 1,
                        "suite_sha256": suite_sha,
                        "status": "invalid_unknown",
                        "resolved": False,
                        "error_code": "evidence_integrity_failed",
                        "usage": {},
                        "grader": {"outcome": "not_run", "resolved": None},
                        "evidence_validation_errors": artifact_errors,
                    }
                rows.append(terminal)
                print("[%02d/12] %-7s %-29s %s" % (
                    planned["slot"], planned["arm"], planned["task_id"], terminal["status"]),
                    flush=True)
                partial = summarize(plan, rows, suite_sha)
                _atomic_json(result_dir / "summary.partial.json", partial)

            if len(list((result_dir / "runs").glob("*/reservation.json"))) != 12:
                raise RuntimeError("run journal does not contain exactly twelve reservations")
            summary = summarize(plan, rows, suite_sha)
            _atomic_json(result_dir / "summary.json", summary)
            print("results: %s" % result_dir)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 2 if summary["ranking_withheld"] else 0
    finally:
        shutil.rmtree(preflight_root, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="subscription_rank")
    parser.add_argument("--execute", action="store_true",
                        help="consume the frozen twelve subscription attempt slots")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)
    if args.execute == args.preflight_only:
        parser.error("choose exactly one of --execute or --preflight-only")
    return execute(preflight_only=args.preflight_only)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
