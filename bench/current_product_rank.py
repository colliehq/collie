"""CURRENT exploratory Collie Agent SDK versus native Codex product benchmark.

This driver intentionally cannot produce a publishable harness ranking.  The
products use different models and tool semantics.  It runs one admission cell
per arm before a configurable AB/BA schedule over two frozen synthetic tasks,
creates a new Git repository for every attempt, keeps hidden graders outside the
agent container, and journals each launch before starting it.
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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.current_product_worker import (  # noqa: E402
    CODEX_MODEL, COLLIE_MODEL, SHARED_EVALUATOR_PROMPT,
)
from bench.subscription_rank_tasks import (  # noqa: E402
    TASKS, canonical_sha256, materialize_task, self_check as task_self_check,
    task_by_id, task_sha256,
)
from harness.subscription_guard import check_subscription_guard  # noqa: E402


ARMS = ("collie", "codex")
DEFAULT_REPETITIONS = 3
DEFAULT_MAX_TURNS = 12
DEFAULT_WALL_SECONDS = 900
EVIDENCE_MAX_AGE_SECONDS = 15 * 60
RESULTS_ROOT = ROOT / "bench" / "results"
TEMP_ROOT = ROOT / ".bench-tmp"
DOCKERFILE = ROOT / "bench" / "current-product.Dockerfile"
WORKER = ROOT / "bench" / "current_product_worker.py"
IMAGE_TAG = "collie-current-product-rank:v1"
CLAIM = "current_exploratory_subscription_native_product_comparison"
SOURCE_PATHS = (
    "bench/current-product.Dockerfile", "bench/current_product_rank.py",
    "bench/current_product_worker.py", "bench/subscription_rank_tasks.py",
    "harness/claude_agent_sdk.py", "harness/claude_agent_worker.py",
    "harness/subscription_guard.py", "harness/swe.py",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


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
        raise RuntimeError("JSON evidence is not an object")
    return value


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 60,
         check: bool = False, env: Mapping[str, str] | None = None
         ) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=str(cwd) if cwd else None, capture_output=True,
                            text=True, timeout=timeout, check=False, env=env)
    if check and result.returncode:
        raise RuntimeError("command failed (%d): %s" % (result.returncode, command[0]))
    return result


def canonical_plan(repetitions: int = DEFAULT_REPETITIONS,
                   *, admission: bool = False) -> list[dict[str, Any]]:
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    plan: list[dict[str, Any]] = []
    tasks = TASKS[:1] if admission else TASKS
    reps = range(1, 2) if admission else range(1, repetitions + 1)
    for task_index, task in enumerate(tasks):
        for repetition in reps:
            order = ARMS if (task_index + repetition - 1) % 2 == 0 else tuple(reversed(ARMS))
            for position, arm in enumerate(order, 1):
                slot = len(plan) + 1
                prefix = "admit" if admission else "rank"
                plan.append({
                    "slot": slot,
                    "run_id": "%s-%02d-%s-r%d-p%d-%s" % (
                        prefix, slot, task["task_id"], repetition, position, arm),
                    "task_id": task["task_id"],
                    "task_sha256": task_sha256(task),
                    "repetition": repetition,
                    "position": position,
                    "arm": arm,
                    "attempt": 1,
                    "phase": "admission" if admission else "ranking",
                })
    return plan


def _source_revision_and_hashes(*, require_clean: bool) -> tuple[str, dict[str, str]]:
    revision = _run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True).stdout.strip()
    if require_clean:
        tracked = _run(["git", "ls-files", "--error-unmatch", *SOURCE_PATHS], cwd=ROOT)
        dirty = _run(["git", "diff", "--quiet", "HEAD", "--", *SOURCE_PATHS], cwd=ROOT)
        if tracked.returncode or dirty.returncode:
            raise RuntimeError("commit the benchmark sources before a live launch")
    hashes = {relative: _sha_file(ROOT / relative) for relative in SOURCE_PATHS}
    return revision, hashes


def _build_image(tag: str, revision: str) -> str:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    context = Path(tempfile.mkdtemp(prefix="current-rank-build-", dir=TEMP_ROOT))
    try:
        archive = subprocess.run(
            ["git", "archive", "--format=tar", revision, "harness",
             "bench/current_product_worker.py", "bench/paired_eval.py",
             "bench/current-product.Dockerfile"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if archive.returncode:
            raise RuntimeError("could not create committed benchmark image context")
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
            base = context.resolve()
            for member in bundle.getmembers():
                target = (context / member.name).resolve()
                if target != base and base not in target.parents:
                    raise RuntimeError("unsafe path in Git archive")
            bundle.extractall(context)
        shutil.copyfile(context / "bench" / "current-product.Dockerfile",
                        context / "Dockerfile")
        _run(["docker", "build", "--pull=false", "-t", tag, "."], cwd=context,
             timeout=900, check=True)
    finally:
        shutil.rmtree(context, ignore_errors=True)
    image_id = _run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
                    check=True).stdout.strip()
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise RuntimeError("Docker returned an invalid image id")
    return image_id


def _codex_auth_path() -> Path:
    source_home = os.environ.get("CODEX_HOME")
    root = Path(source_home).expanduser() if source_home else Path.home() / ".codex"
    path = (root / "auth.json").resolve()
    if not path.is_file():
        raise RuntimeError("Codex ChatGPT auth.json is unavailable")
    return path


def _claude_credentials_path() -> Path:
    path = (Path.home() / ".claude" / ".credentials.json").resolve()
    if not path.is_file():
        raise RuntimeError("Claude plan credential file is unavailable")
    return path


def _codex_version(image: str) -> str:
    result = _run(["docker", "run", "--rm", "--network", "none",
                   "--entrypoint", "codex", image, "--version"],
                  timeout=30, check=True)
    value = (result.stdout or result.stderr).strip()
    if not value:
        raise RuntimeError("Codex version is unavailable")
    return value[:160]


def _image_preflight(image: str) -> dict[str, str]:
    """Prove the frozen image can start without exposing evaluator-owned material."""
    script = (
        "import importlib.metadata as m\n"
        "from pathlib import Path\n"
        "import harness.swe\n"
        "import bench.current_product_worker\n"
        "assert not Path('/opt/collie/bench/subscription_rank_tasks.py').exists()\n"
        "Path('/tmp/write-canary').write_text('ok', encoding='utf-8')\n"
        "print(m.version('claude-agent-sdk'))\n"
    )
    result = _run([
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16777216",
        "--entrypoint", "python", image, "-c", script,
    ], timeout=60, check=True)
    sdk_version = result.stdout.strip()
    if sdk_version != "0.2.136":
        raise RuntimeError("unexpected Claude Agent SDK image version")
    return {
        "claude_agent_sdk_version": sdk_version,
        "worker_import": "ok",
        "write_canary": "ok",
        "evaluator_task_module_in_image": "absent",
        "network": "none",
    }


def _status_environment(**extra: str) -> dict[str, str]:
    allowed = {
        "APPDATA", "COMSPEC", "HOME", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA",
        "PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USERPROFILE",
        "WINDIR",
    }
    value = {key: item for key, item in os.environ.items()
             if key.upper() in allowed and isinstance(item, str)}
    value.update(extra)
    return value


def _guard_receipts(codex_evidence: Mapping[str, Any], codex_auth: Path
                     ) -> dict[str, dict[str, Any]]:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    preflight_home = Path(tempfile.mkdtemp(prefix="codex-guard-home-", dir=TEMP_ROOT)).resolve()
    try:
        # The benchmark does not inspect or fingerprint credential bytes.
        shutil.copyfile(codex_auth, preflight_home / "auth.json")
        if os.name != "nt":
            os.chmod(preflight_home / "auth.json", 0o600)
        # These values identify the trusted parent Codex session and its local
        # permission profile.  They are not copied into the benchmark child.
        # Keep every other ambient CODEX_/OPENAI_ key visible to the guard so
        # routing, API-key, and billing overrides still fail closed.
        parent_metadata = {
            "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_PERMISSION_PROFILE",
            "CODEX_THREAD_ID",
        }
        codex_environment = {
            key: value for key, value in os.environ.items()
            if key.upper() not in parent_metadata
        }
        if "CODEX_HOME" in codex_environment:
            raise RuntimeError(
                "unset ambient CODEX_HOME before benchmark launch; the suite owns an isolated one")
        codex_environment["CODEX_HOME"] = str(preflight_home)
        return {
            "collie": check_subscription_guard(
                "claude-agent-sdk", model=COLLIE_MODEL, require_direct_probe=False,
                environ=os.environ),
            "codex": check_subscription_guard(
                "codex-cli", model=CODEX_MODEL, account_evidence=codex_evidence,
                environ=codex_environment,
                expected_codex_home=str(preflight_home)),
        }
    finally:
        shutil.rmtree(preflight_home, ignore_errors=True)


def _prepare_git_fixture(task: Mapping[str, Any], workspace: Path) -> tuple[str, str]:
    materialize_task(task, workspace)
    for arguments in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "current-rank@collie.run"],
        ["git", "config", "user.name", "Collie Current Rank"],
        ["git", "add", "-A"],
        ["git", "commit", "--quiet", "-m", "frozen baseline"],
    ):
        _run(arguments, cwd=workspace, check=True)
    commit = _run(["git", "rev-parse", "HEAD"], cwd=workspace, check=True).stdout.strip()
    tree = _run(["git", "rev-parse", "HEAD^{tree}"], cwd=workspace, check=True).stdout.strip()
    return commit, tree


def _grade(task: Mapping[str, Any], workspace: Path, patch_sha: str) -> dict[str, Any]:
    grader_root = Path(tempfile.mkdtemp(prefix="current-hidden-grader-"))
    try:
        grader = grader_root / "grader.py"
        wrapper = "import sys\nsys.path.insert(0, %r)\n" % str(workspace) + str(
            task["hidden_grader"])
        grader.write_text(wrapper, encoding="utf-8", newline="\n")
        result = _run([sys.executable, "-I", str(grader)], cwd=workspace, timeout=30)
        if result.returncode not in (0, 1):
            return {"outcome": "grader_infrastructure_error", "resolved": None,
                    "patch_sha256": patch_sha}
        return {
            "format": "collie-current-product-grader-v1",
            "outcome": "graded", "resolved": result.returncode == 0,
            "returncode": result.returncode,
            "task_sha256": task_sha256(task),
            "fixture_sha256": canonical_sha256(task["fixture_files"]),
            "grader_sha256": _sha_bytes(str(task["hidden_grader"]).encode("utf-8")),
            "patch_sha256": patch_sha,
            "graded_at_utc": _utc_now(),
        }
    finally:
        shutil.rmtree(grader_root, ignore_errors=True)


def _docker_mount(source: Path, destination: str, *, readonly: bool = False) -> str:
    value = "type=bind,src=%s,dst=%s" % (source.resolve(), destination)
    return value + (",readonly" if readonly else "")


def _container_command(image: str, row: Mapping[str, Any], workspace: Path,
                       input_dir: Path, output_dir: Path, state_dir: Path,
                       credential: Path) -> list[str]:
    auth_destination = ("/input/claude-credentials.json" if row["arm"] == "collie"
                        else "/input/auth.json")
    return [
        # tini is PID 1 so the Agent SDK's deliberately orphaned watchdog is
        # reaped after group termination.  Without a subreaper, the zombie keeps
        # killpg(..., 0) true and Collie's ownership proof correctly fails closed.
        "docker", "run", "--rm", "--init", "--network", "bridge", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--memory", "3g", "--cpus", "2",
        "--pids-limit", "256", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=268435456",
        "--tmpfs", "/home/runner:rw,nosuid,size=67108864",
        "--env", "HOME=/home/runner", "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--mount", _docker_mount(workspace, "/workspace"),
        "--mount", _docker_mount(input_dir, "/input", readonly=True),
        "--mount", _docker_mount(output_dir, "/output"),
        "--mount", _docker_mount(state_dir, "/state"),
        "--mount", _docker_mount(credential, auth_destination, readonly=True),
        image,
        "--arm", str(row["arm"]), "--task-json", "/input/task.json",
        "--workspace", "/workspace", "--run-dir", "/output",
        "--state-dir", "/state", "--output", "/output/worker.json",
        "--max-turns", str(DEFAULT_MAX_TURNS),
    ]


def _remove_container(name: str) -> bool:
    _run(["docker", "rm", "--force", name], timeout=30)
    return _run(["docker", "inspect", name], timeout=10).returncode != 0


def _external_patch(workspace: Path) -> str:
    _run(["git", "add", "-A", "--", "."], cwd=workspace, check=True)
    patch = _run(["git", "diff", "--binary", "--cached", "HEAD", "--"],
                 cwd=workspace, check=True).stdout
    return patch if len(patch.encode("utf-8")) <= 1024 * 1024 else ""


def _worker_codex_evidence(guard: Mapping[str, Any]) -> dict[str, Any]:
    admitted = guard.get("account_evidence") or {}
    return {key: admitted.get(key)
            for key in ("credits_remaining", "auto_reload", "observed_at_utc")}


def _run_one(image: str, suite_sha: str, row: Mapping[str, Any],
             guard: Mapping[str, Any], codex_version: str,
             claude_credential: Path, codex_auth: Path, suite_temp: Path,
             result_root: Path, wall_seconds: int) -> dict[str, Any]:
    task = task_by_id(str(row["task_id"]))
    run_dir = result_root / "runs" / str(row["run_id"])
    run_dir.mkdir(parents=True, exist_ok=False)
    reservation = {
        **row, "schema_version": 1, "suite_sha256": suite_sha,
        "reserved_at_utc": _utc_now(), "state": "reserved",
    }
    _atomic_json(run_dir / "reservation.json", reservation)
    reservation_sha = _sha_file(run_dir / "reservation.json")

    root = suite_temp / str(row["run_id"])
    workspace, input_dir, output_dir, state_dir = (
        root / "workspace", root / "input", run_dir, root / "state")
    for path in (workspace, input_dir, state_dir):
        path.mkdir(parents=True, exist_ok=False)
    baseline_commit, baseline_tree = _prepare_git_fixture(task, workspace)
    prompt = SHARED_EVALUATOR_PROMPT + str(task["prompt"])
    worker_input: dict[str, Any] = {
        **row, "prompt": task["prompt"], "delivered_prompt": prompt,
        "delivered_prompt_sha256": _sha_bytes(prompt.encode("utf-8")),
        "model": COLLIE_MODEL if row["arm"] == "collie" else CODEX_MODEL,
        "wall_seconds": wall_seconds,
        "guard_receipt": dict(guard),
        "guard_receipt_sha256": _sha_bytes(_canonical_bytes(guard)),
        "runtime_version": codex_version if row["arm"] == "codex" else None,
    }
    if row["arm"] == "codex":
        worker_input["codex_auth_source"] = "/input/auth.json"
    else:
        worker_input["claude_credential_source"] = "/input/claude-credentials.json"
    _atomic_json(input_dir / "task.json", worker_input)
    placeholder = (input_dir / "auth.json" if row["arm"] == "codex"
                   else input_dir / "claude-credentials.json")
    placeholder.write_bytes(b"")
    credential = claude_credential if row["arm"] == "collie" else codex_auth
    command = _container_command(
        image, row, workspace, input_dir, output_dir, state_dir, credential)
    container_name = "collie-current-%s-%s" % (
        suite_sha[:10], _sha_bytes(str(row["run_id"]).encode("utf-8"))[:12])
    command[2:2] = ["--name", container_name]

    timed_out = False
    cleanup_ok = True
    try:
        process = _run(command, timeout=wall_seconds + 45)
    except subprocess.TimeoutExpired:
        timed_out = True
        cleanup_ok = _remove_container(container_name)
        process = None
    else:
        if _run(["docker", "inspect", container_name], timeout=10).returncode == 0:
            cleanup_ok = _remove_container(container_name)
    worker_path = output_dir / "worker.json"
    worker = _load_json(worker_path) if worker_path.is_file() else {}
    patch = worker.get("patch") if isinstance(worker.get("patch"), str) else ""
    if not patch:
        try:
            patch = _external_patch(workspace)
        except Exception:
            patch = ""
    _atomic_text(run_dir / "patch.diff", patch)
    patch_sha = _sha_bytes(patch.encode("utf-8"))
    grader = (_grade(task, workspace, patch_sha)
              if worker.get("worker_outcome") == "candidate" else
              {"outcome": "not_run", "resolved": None, "patch_sha256": patch_sha})
    status = "invalid_infrastructure"
    error_code = str(worker.get("error_code") or "")
    if timed_out:
        error_code = "outer_wall_timeout"
    elif not cleanup_ok:
        error_code = "container_cleanup_unconfirmed"
    elif not worker:
        error_code = "worker_receipt_missing"
    elif process is not None and process.returncode not in (0, 2):
        error_code = "agent_container_exit"
    elif worker.get("worker_outcome") == "candidate" and grader.get("outcome") == "graded":
        status = "valid_resolved" if grader.get("resolved") is True else "valid_unresolved"
        error_code = "" if status == "valid_resolved" else "hidden_contract_failed"
    _atomic_json(run_dir / "grader.json", grader)
    _atomic_json(run_dir / "usage.json", {
        "schema_version": 1, "suite_sha256": suite_sha, "run_id": row["run_id"],
        "usage": worker.get("usage") if isinstance(worker.get("usage"), dict) else {},
    })
    terminal = {
        **row, "schema_version": 1, "suite_sha256": suite_sha,
        "status": status, "resolved": status == "valid_resolved",
        "error_code": error_code,
        "reservation_sha256": reservation_sha,
        "baseline_commit": baseline_commit,
        "baseline_tree": baseline_tree,
        "delivered_prompt_sha256": worker_input["delivered_prompt_sha256"],
        "patch_sha256": patch_sha, "patch_bytes": len(patch.encode("utf-8")),
        "duration_ms": worker.get("duration_ms"),
        "usage": worker.get("usage") if isinstance(worker.get("usage"), dict) else {},
        "runtime": worker.get("runtime") if isinstance(worker.get("runtime"), dict) else {},
        "request_evidence": (worker.get("request_evidence")
                             if isinstance(worker.get("request_evidence"), list) else []),
        "grader": grader, "completed_at_utc": _utc_now(),
    }
    _atomic_json(run_dir / "result.json", terminal)
    shutil.rmtree(root, ignore_errors=True)
    return terminal


def summarize(plan: list[dict[str, Any]], rows: list[dict[str, Any]],
              suite_sha: str, *, require_post_run_billing: bool = False) -> dict[str, Any]:
    expected = {row["run_id"]: row for row in plan}
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        run_id = str(row.get("run_id") or "")
        if run_id in seen or run_id not in expected:
            errors.append({"run_id": run_id, "error": "unexpected_or_duplicate_run"})
            continue
        seen.add(run_id)
        planned = expected[run_id]
        for key in ("slot", "task_id", "task_sha256", "repetition", "position",
                    "arm", "attempt", "phase"):
            if row.get(key) != planned.get(key):
                errors.append({"run_id": run_id, "error": key + "_mismatch"})
        if row.get("suite_sha256") != suite_sha:
            errors.append({"run_id": run_id, "error": "suite_mismatch"})
        if row.get("status") not in ("valid_resolved", "valid_unresolved"):
            errors.append({"run_id": run_id, "error": "invalid_attempt"})
    for missing in sorted(set(expected) - seen):
        errors.append({"run_id": missing, "error": "missing_run"})

    scores = None
    ranking = None
    computed_ranking = None
    if not errors and len(rows) == len(plan):
        scores = {}
        for arm in ARMS:
            selected = [row for row in rows if row["arm"] == arm]
            solved = sum(row["resolved"] is True for row in selected)
            durations = [float(row["duration_ms"]) for row in selected
                         if isinstance(row.get("duration_ms"), (int, float))]
            scores[arm] = {
                "resolved": solved, "attempts": len(selected),
                "solve_rate": solved / len(selected),
                "median_duration_ms": statistics.median(durations) if durations else None,
            }
        computed_ranking = sorted(
            ({"arm": arm, "score": scores[arm]["solve_rate"]} for arm in ARMS),
            key=lambda item: (-item["score"], item["arm"]),
        )
        for item in computed_ranking:
            item["rank"] = 1 + sum(
                other["score"] > item["score"] for other in computed_ranking)
        if not require_post_run_billing:
            ranking = computed_ranking
    return {
        "schema_version": 1, "suite_sha256": suite_sha,
        "claim": CLAIM, "scope": "exploratory", "publishable": False,
        "comparison_label": "subscription_native_product_comparison_not_harness_only",
        "ranking_withheld": bool(errors) or require_post_run_billing,
        "ranking_withheld_reason": (
            "validation_errors" if errors else
            "post_run_billing_ui_recheck_pending" if require_post_run_billing else None),
        "billing_post_run_verified": not require_post_run_billing,
        "validation_errors": errors,
        "scores": scores, "ranking": ranking,
        "limitations": [
            "two synthetic tasks are insufficient for a general capability claim",
            "arms use different model families and native product tool semantics",
            "Codex CLI does not expose independently verified internal request count",
            "subscription quota consumption is not a metered billing receipt",
        ],
        "generated_at_utc": _utc_now(),
    }


def _manifest(revision: str, source_hashes: Mapping[str, str], image_id: str,
               repetitions: int, wall_seconds: int, plans: Mapping[str, Any],
               guards: Mapping[str, Any], codex_version: str,
               image_preflight: Mapping[str, str]) -> dict[str, Any]:
    return {
        "schema_version": 1, "suite_id": "collie-current-product-v1",
        "claim": CLAIM, "scope": "exploratory", "publishable": False,
        "comparison_label": "subscription_native_product_comparison_not_harness_only",
        "git_revision": revision, "source_sha256": dict(source_hashes),
        "image_id": image_id, "dockerfile_sha256": _sha_file(DOCKERFILE),
        "worker_sha256": _sha_file(WORKER),
        "image_preflight": dict(image_preflight),
        "tasks": [{
            "task_id": task["task_id"], "task_sha256": task_sha256(task),
            "fixture_sha256": canonical_sha256(task["fixture_files"]),
            "grader_sha256": _sha_bytes(task["hidden_grader"].encode("utf-8")),
        } for task in TASKS],
        "delivered_prompt_prefix_sha256": _sha_bytes(
            SHARED_EVALUATOR_PROMPT.encode("utf-8")),
        "prompt_contract": "byte_identical_evaluator_owned_user_message_per_task",
        "system_and_tool_surfaces": "product_native_and_not_resource_matched",
        "arms": {
            "collie": {
                "product": "Collie", "model": COLLIE_MODEL,
                "surface": "collie_loop_plus_official_claude_agent_sdk",
                "claude_p_invoked": False, "internal_retries": 0,
                "physical_requests": "durably_reserved_and_settled_per_request",
                "guard_receipt_sha256": _sha_bytes(_canonical_bytes(guards["collie"])),
            },
            "codex": {
                "product": "Codex CLI", "model": CODEX_MODEL,
                "model_status": "explicitly_requested; reroute invalidates attempt",
                "reasoning_effort": "high",
                "surface": "codex_exec_json_ephemeral_workspace_write",
                "auth": "ChatGPT login copied mechanically into fresh CODEX_HOME",
                "user_config_and_rules": "ignored",
                "foreign_surfaces": "explicitly_disabled_and_trace_rejected",
                "codex_cli_version": codex_version,
                "guard_receipt_sha256": _sha_bytes(_canonical_bytes(guards["codex"])),
                "internal_model_requests": "not_observed_by_cli",
            },
        },
        "repetitions_per_task_arm": repetitions,
        "agent_wall_seconds": wall_seconds,
        "admission_plan": plans["admission"], "ranking_plan": plans["ranking"],
        "launch_policy": "one_rep_per_arm_admission_then_full_counterbalanced_schedule",
        "network": "bridge_for_product_inference; evaluator_hidden_grader_outside_agent",
        "fresh_git_workspace_per_attempt": True,
        "container_subreaper": "docker_init_tini",
        "gold_and_hidden_grader_visible_to_agent": False,
        "billing": {
            "track": "subscription_native_product",
            "api_key_fallback_disabled": True,
            "actual_marginal_charge_observed": False,
        },
    }


def execute(*, repetitions: int, wall_seconds: int,
            codex_evidence: Mapping[str, Any], preflight_only: bool = False,
            image_tag: str = IMAGE_TAG,
            claude_account_evidence: Mapping[str, Any] | None = None) -> int:
    task_self_check()
    normalized_claude_evidence = dict(claude_account_evidence or {})
    normalized_claude_evidence["observed_at_utc"] = _parse_recent_evidence_timestamp(
        normalized_claude_evidence.get("observed_at_utc"), label="Claude launch")
    revision, source_hashes = _source_revision_and_hashes(require_clean=not preflight_only)
    plans = {"admission": canonical_plan(1, admission=True),
             "ranking": canonical_plan(repetitions)}
    image_id = _build_image(image_tag, revision)
    image_preflight = _image_preflight(image_id)
    codex_version = _codex_version(image_id)
    if codex_version != "codex-cli 0.147.0":
        raise RuntimeError("unexpected Codex CLI image version")
    claude_credential = _claude_credentials_path()
    codex_auth = _codex_auth_path()
    guards = _guard_receipts(codex_evidence, codex_auth)
    core = _manifest(revision, source_hashes, image_id, repetitions, wall_seconds,
                     plans, guards, codex_version, image_preflight)
    core["billing"]["claude_suite_launch_evidence"] = normalized_claude_evidence
    core["billing"]["post_run_ui_recheck_required"] = True
    suite_sha = _sha_bytes(_canonical_bytes(core))
    if preflight_only:
        print(json.dumps({
            "outcome": "preflight_ok", "publishable": False,
            "suite_sha256": suite_sha, "image_id": image_id,
            "admission_launches": len(plans["admission"]),
            "ranking_launches": len(plans["ranking"]),
            "guards": {arm: {"provider": receipt["provider"],
                              "verdict": receipt["verdict"]}
                       for arm, receipt in guards.items()},
        }, ensure_ascii=False, indent=2))
        return 0

    result_root = RESULTS_ROOT / ("current-product-v1-" + suite_sha[:12])
    suite_temp = TEMP_ROOT / ("current-product-v1-" + suite_sha[:12])
    result_root.mkdir(parents=True, exist_ok=True)
    suite_temp.mkdir(parents=True, exist_ok=True)
    _atomic_json(result_root / "manifest.json", {
        **core, "suite_sha256": suite_sha, "created_at_utc": _utc_now(),
    })
    rows: list[dict[str, Any]] = []
    for phase in ("admission", "ranking"):
        for row in plans[phase]:
            terminal = _run_one(
                image_id, suite_sha, row, guards[row["arm"]], codex_version,
                claude_credential, codex_auth, suite_temp, result_root, wall_seconds)
            rows.append(terminal)
            print("[%s %02d] %-6s %-29s %s" % (
                phase, row["slot"], row["arm"], row["task_id"], terminal["status"]),
                flush=True)
            if phase == "admission" and terminal["status"] not in (
                    "valid_resolved", "valid_unresolved"):
                summary = summarize(
                    plans["admission"], rows, suite_sha, require_post_run_billing=True)
                _atomic_json(result_root / "summary.json", summary)
                print("admission failed; ranking launches were not consumed")
                return 2
    ranking_rows = [row for row in rows if row["phase"] == "ranking"]
    summary = summarize(
        plans["ranking"], ranking_rows, suite_sha, require_post_run_billing=True)
    summary["admission"] = summarize(
        plans["admission"], [row for row in rows if row["phase"] == "admission"], suite_sha)
    _atomic_json(result_root / "summary.json", summary)
    print("results: %s" % result_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["validation_errors"] else 2


def _parse_recent_evidence_timestamp(value: object, *, label: str,
                                     not_before: dt.datetime | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(label + " timestamp is missing")
    try:
        observed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(label + " timestamp is invalid") from exc
    if observed.tzinfo is None:
        raise RuntimeError(label + " timestamp must include a UTC offset")
    observed = observed.astimezone(dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    if observed > now + dt.timedelta(seconds=60):
        raise RuntimeError(label + " timestamp is in the future")
    if (now - observed).total_seconds() > EVIDENCE_MAX_AGE_SECONDS:
        raise RuntimeError(label + " evidence is stale")
    if not_before is not None and observed < not_before:
        raise RuntimeError(label + " observation predates the benchmark")
    return observed.isoformat().replace("+00:00", "Z")


def finalize_billing(result_root: Path, *, codex_evidence: Mapping[str, Any],
                     claude_evidence: Mapping[str, Any]) -> int:
    """Release a result's ranking only after fresh, fail-closed UI observations."""
    root = result_root.resolve()
    allowed = RESULTS_ROOT.resolve()
    if root.parent != allowed or not root.is_dir():
        raise RuntimeError("result directory is outside the benchmark results root")
    manifest_path = root / "manifest.json"
    summary_path = root / "summary.json"
    manifest = _load_json(manifest_path)
    summary = _load_json(summary_path)
    if manifest.get("suite_sha256") != summary.get("suite_sha256"):
        raise RuntimeError("manifest and summary suite identities differ")
    created = dt.datetime.fromisoformat(
        str(manifest["created_at_utc"]).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    completed = dt.datetime.fromisoformat(
        str(summary["generated_at_utc"]).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    not_before = max(created, completed)
    codex_observed = _parse_recent_evidence_timestamp(
        codex_evidence.get("observed_at_utc"), label="Codex post-run", not_before=not_before)
    claude_observed = _parse_recent_evidence_timestamp(
        claude_evidence.get("observed_at_utc"), label="Claude post-run", not_before=not_before)
    safe = (
        codex_evidence.get("credits_remaining") == 0
        and codex_evidence.get("auto_reload") is False
        and claude_evidence.get("usage_credits_enabled") is False
        and claude_evidence.get("auto_reload") is False
        and claude_evidence.get("period_spend_usd") == 0
    )
    receipt = {
        "schema_version": 1,
        "suite_sha256": manifest["suite_sha256"],
        "outcome": "verified_safe" if safe else "unsafe_or_incomplete",
        "codex": {
            "observed_at_utc": codex_observed,
            "credits_remaining": codex_evidence.get("credits_remaining"),
            "auto_reload": codex_evidence.get("auto_reload"),
        },
        "claude": {
            "observed_at_utc": claude_observed,
            "usage_credits_enabled": claude_evidence.get("usage_credits_enabled"),
            "auto_reload": claude_evidence.get("auto_reload"),
            "period_spend_usd": claude_evidence.get("period_spend_usd"),
        },
        "verified_at_utc": _utc_now(),
    }
    receipt_path = root / "post-run-billing.json"
    _atomic_json(receipt_path, receipt)
    summary["billing_post_run_verified"] = safe
    summary["post_run_billing_receipt_sha256"] = _sha_file(receipt_path)
    if safe and not summary.get("validation_errors"):
        scores = summary.get("scores") or {}
        ranking = sorted(
            ({"arm": arm, "score": scores[arm]["solve_rate"]} for arm in ARMS),
            key=lambda item: (-item["score"], item["arm"]),
        )
        for item in ranking:
            item["rank"] = 1 + sum(
                other["score"] > item["score"] for other in ranking)
        summary["ranking"] = ranking
        summary["ranking_withheld"] = False
        summary["ranking_withheld_reason"] = None
    else:
        summary["ranking"] = None
        summary["ranking_withheld"] = True
        summary["ranking_withheld_reason"] = (
            "validation_errors" if summary.get("validation_errors") else
            "post_run_billing_ui_recheck_failed")
    summary["generated_at_utc"] = _utc_now()
    _atomic_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["ranking_withheld"] else 2


def _codex_evidence(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "credits_remaining": args.codex_credits_remaining,
        "auto_reload": False if args.codex_auto_reload_off else None,
        "observed_at_utc": args.codex_evidence_observed_at,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="current_product_rank")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--finalize-billing", type=Path, metavar="RESULT_DIR")
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--wall-seconds", type=int, default=DEFAULT_WALL_SECONDS)
    parser.add_argument("--codex-credits-remaining", type=float)
    parser.add_argument("--codex-auto-reload-off", action="store_true")
    parser.add_argument("--codex-evidence-observed-at")
    parser.add_argument("--claude-evidence-observed-at")
    parser.add_argument("--claude-usage-credits-off", action="store_true")
    parser.add_argument("--claude-auto-reload-off", action="store_true")
    parser.add_argument("--claude-period-spend-usd", type=float)
    args = parser.parse_args(argv)
    if args.finalize_billing is not None:
        if (args.codex_credits_remaining is None
                or args.claude_period_spend_usd is None):
            parser.error("post-run Codex and Claude billing evidence is required")
        return finalize_billing(
            args.finalize_billing,
            codex_evidence=_codex_evidence(args),
            claude_evidence={
                "observed_at_utc": args.claude_evidence_observed_at,
                "usage_credits_enabled": False if args.claude_usage_credits_off else None,
                "auto_reload": False if args.claude_auto_reload_off else None,
                "period_spend_usd": args.claude_period_spend_usd,
            },
        )
    if args.wall_seconds < 30:
        parser.error("--wall-seconds must be at least 30")
    if (args.codex_credits_remaining is None or not args.codex_evidence_observed_at
            or not args.claude_evidence_observed_at
            or args.claude_period_spend_usd is None):
        parser.error("fresh Codex and Claude launch evidence is required")
    if (not args.claude_usage_credits_off or not args.claude_auto_reload_off
            or args.claude_period_spend_usd != 0):
        parser.error("Claude launch evidence must show credits/reload off and zero spend")
    return execute(
        repetitions=args.repetitions, wall_seconds=args.wall_seconds,
        codex_evidence=_codex_evidence(args), preflight_only=args.preflight_only,
        claude_account_evidence={
            "observed_at_utc": args.claude_evidence_observed_at,
            "plan": "Max 20x", "usage_credits_enabled": False,
            "auto_reload": False, "period_spend_usd": 0,
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
