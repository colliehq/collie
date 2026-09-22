"""Exploratory same-transport comparison of Collie, Prime, Pi, and Hermes.

Every arm owns its native agent loop, system prompt, and local tools, but all
model turns cross the same evaluator-owned OpenAI-compatible sidecar.  That
sidecar delegates one turn at a time to Collie's direct Claude Agent SDK path
using the user's Claude subscription.  The comparison is intentionally marked
non-publishable: the harnesses are adapted, their tool surfaces differ, and two
small synthetic tasks cannot establish a general capability ranking.

The evaluator keeps credentials and hidden graders outside the agent container.
For each attempt it creates an internal Docker network, attaches the sidecar to
that network and then to Docker's external bridge, and leaves the agent attached
only to the internal network.  No sidecar port is published on the host.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Mapping
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.current_product_worker import SHARED_EVALUATOR_PROMPT  # noqa: E402
from bench.subscription_rank_tasks import (  # noqa: E402
    TASKS, canonical_sha256, materialize_task, self_check as task_self_check,
    task_by_id, task_sha256,
)
from harness.subscription_guard import check_subscription_guard  # noqa: E402
from harness.subscription_sidecar import BEARER_SENTINEL, MODEL  # noqa: E402


ARMS = ("collie", "prime", "pi", "hermes")
DEFAULT_REPETITIONS = 4
DEFAULT_MAX_TURNS = 12
DEFAULT_WALL_SECONDS = 900
SIDECAR_REQUEST_SECONDS = 300
EVIDENCE_MAX_AGE_SECONDS = 15 * 60
MAX_PATCH_BYTES = 1024 * 1024
RESULTS_ROOT = ROOT / "bench" / "results"
TEMP_ROOT = ROOT / ".bench-tmp"
SIDECAR_DOCKERFILE = ROOT / "bench" / "normalized-sidecar.Dockerfile"
HARNESS_DOCKERFILE = ROOT / "bench" / "normalized-harness.Dockerfile"
WORKER = ROOT / "bench" / "normalized_harness_worker.py"
SIDECAR_IMAGE_TAG = "collie-normalized-sidecar:v1"
HARNESS_IMAGE_TAG = "collie-normalized-harness:v1"
CLAIM = "exploratory_adapted_harness_same_subscription_transport_comparison"
COMPARISON_LABEL = "adapted_harness_same_transport_not_native_product_ranking"
SOURCE_PATHS = (
    "bench/normalized_harness_rank.py",
    "bench/normalized_harness_worker.py",
    "bench/normalized_prime_pi.py",
    "bench/normalized_hermes.py",
    "bench/normalized-sidecar.Dockerfile",
    "bench/normalized-harness.Dockerfile",
    "bench/current_product_worker.py",
    "bench/subscription_rank_tasks.py",
    "harness/subscription_sidecar.py",
    "harness/claude_agent_sdk.py",
    "harness/claude_agent_worker.py",
    "harness/subscription_guard.py",
    "harness/swe.py",
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


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
    result = subprocess.run(
        command, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        timeout=timeout, check=False, env=env,
    )
    if check and result.returncode:
        raise RuntimeError("command failed (%d): %s" %
                           (result.returncode, command[0]))
    return result


def canonical_plan(repetitions: int = DEFAULT_REPETITIONS,
                   *, admission: bool = False) -> list[dict[str, Any]]:
    """Return admission cells or the frozen four-arm rotating schedule."""
    if (not isinstance(repetitions, int) or isinstance(repetitions, bool)
            or repetitions < 1):
        raise ValueError("repetitions must be a positive integer")
    tasks = TASKS[:1] if admission else TASKS
    reps = (1,) if admission else tuple(range(1, repetitions + 1))
    plan: list[dict[str, Any]] = []
    for task_index, task in enumerate(tasks):
        for repetition in reps:
            rotation = 0 if admission else (
                task_index * repetitions + repetition - 1) % len(ARMS)
            order = ARMS[rotation:] + ARMS[:rotation]
            for position, arm in enumerate(order, 1):
                slot = len(plan) + 1
                phase = "admission" if admission else "ranking"
                plan.append({
                    "slot": slot,
                    "run_id": "%s-%02d-%s-r%d-p%d-%s" % (
                        "admit" if admission else "rank", slot,
                        task["task_id"], repetition, position, arm),
                    "task_id": task["task_id"],
                    "task_sha256": task_sha256(task),
                    "repetition": repetition,
                    "position": position,
                    "arm": arm,
                    "attempt": 1,
                    "phase": phase,
                })
    return plan


def _source_revision_and_hashes(*, require_clean: bool) -> tuple[str, dict[str, str]]:
    revision = _run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                    check=True).stdout.strip()
    if require_clean:
        tracked = _run(["git", "ls-files", "--error-unmatch", *SOURCE_PATHS],
                       cwd=ROOT)
        dirty = _run(["git", "diff", "--quiet", "HEAD", "--", *SOURCE_PATHS],
                     cwd=ROOT)
        if tracked.returncode or dirty.returncode:
            raise RuntimeError("commit the normalized benchmark sources before launch")
    # Images are built from ``git archive revision``, not from the worktree.
    # Hash those exact exported bytes as well.  On Windows, core.autocrlf can
    # otherwise make a clean file's worktree bytes differ from both its Git
    # blob and the Docker build context even though the committed content is
    # the same.
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision, *SOURCE_PATHS],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if archive.returncode:
        raise RuntimeError("could not hash committed benchmark image sources")
    hashes = _hash_archive_members(archive.stdout, SOURCE_PATHS)
    return revision, hashes


def _hash_archive_members(raw: bytes,
                          relative_paths: tuple[str, ...]) -> dict[str, str]:
    """Hash the exact file payloads emitted into a Git archive."""
    hashes: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as bundle:
        for relative in relative_paths:
            try:
                member = bundle.getmember(relative)
            except KeyError as exc:
                raise RuntimeError("committed benchmark source is missing") from exc
            if not member.isfile():
                raise RuntimeError("committed benchmark source is not a file")
            handle = bundle.extractfile(member)
            if handle is None:
                raise RuntimeError("committed benchmark source cannot be read")
            hashes[relative] = _sha_bytes(handle.read())
    return hashes


def _safe_extract_tar(raw: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as bundle:
        base = destination.resolve()
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if target != base and base not in target.parents:
                raise RuntimeError("unsafe path in Git archive")
            if member.issym() and Path(member.linkname).is_absolute():
                raise RuntimeError("unsafe symlink in Git archive")
        bundle.extractall(destination)


def _build_image(tag: str, revision: str, dockerfile: Path,
                 archive_paths: tuple[str, ...]) -> str:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    context = Path(tempfile.mkdtemp(prefix="normalized-build-", dir=TEMP_ROOT))
    try:
        archive = subprocess.run(
            ["git", "archive", "--format=tar", revision, *archive_paths],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if archive.returncode:
            raise RuntimeError("could not create committed benchmark image context")
        _safe_extract_tar(archive.stdout, context)
        relative = dockerfile.relative_to(ROOT)
        shutil.copyfile(context / relative, context / "Dockerfile")
        _run(["docker", "build", "--pull=false", "-t", tag, "."], cwd=context,
             timeout=1800, check=True)
    finally:
        shutil.rmtree(context, ignore_errors=True)
    image_id = _run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
                    check=True).stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError("Docker returned an invalid image id")
    return image_id


def _build_images(sidecar_tag: str, harness_tag: str,
                  revision: str) -> tuple[str, str]:
    sidecar = _build_image(
        sidecar_tag, revision, SIDECAR_DOCKERFILE,
        ("harness", "bench/normalized-sidecar.Dockerfile"),
    )
    harness = _build_image(
        harness_tag, revision, HARNESS_DOCKERFILE,
        ("harness", "bench/normalized_harness_worker.py",
         "bench/normalized_prime_pi.py", "bench/normalized_hermes.py",
         "bench/normalized-harness.Dockerfile"),
    )
    return sidecar, harness


def _image_preflight(sidecar_image: str, harness_image: str) -> dict[str, Any]:
    sidecar_script = (
        "import importlib.metadata as m\n"
        "from pathlib import Path\n"
        "import harness.subscription_sidecar\n"
        "assert not Path('/opt/collie/bench/subscription_rank_tasks.py').exists()\n"
        "print(m.version('claude-agent-sdk'))\n"
    )
    sidecar = _run([
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16777216",
        "--entrypoint", "python", sidecar_image, "-c", sidecar_script,
    ], timeout=60, check=True).stdout.strip()
    if sidecar != "0.2.136":
        raise RuntimeError("unexpected Claude Agent SDK image version")

    harness_script = r'''import json, subprocess
from pathlib import Path
import harness
import bench.normalized_harness_worker
import bench.normalized_prime_pi
import bench.normalized_hermes
assert not Path('/opt/collie/bench/subscription_rank_tasks.py').exists()
pi = json.loads(Path('/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/package.json').read_text())['version']
prime = json.loads(Path('/opt/prime-agent/packages/coding-agent/package.json').read_text())['version']
commit = subprocess.run(['git', '-C', '/opt/prime-agent', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
hermes = subprocess.run(['/opt/hermes-venv/bin/python', '-c', 'import importlib.metadata as m; print(m.version("hermes-agent"))'], check=True, capture_output=True, text=True).stdout.strip()
print(json.dumps({'collie': harness.__version__, 'pi': pi, 'prime': prime, 'prime_commit': commit, 'hermes': hermes}, sort_keys=True))
'''
    raw = _run([
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16777216",
        "--tmpfs", "/home/runner:rw,nosuid,size=16777216",
        "--entrypoint", "python3", harness_image, "-c", harness_script,
    ], timeout=90, check=True).stdout.strip()
    versions = json.loads(raw)
    if (versions.get("pi") != "0.84.1"
            or versions.get("prime") != "0.7.2"
            or versions.get("prime_commit") !=
            "0987c1ba7637cbcb99afe9efe1180b838a0aa958"
            or versions.get("hermes") != "0.15.2"):
        raise RuntimeError("unexpected normalized harness runtime version")
    return {
        "sidecar": {"claude_agent_sdk_version": sidecar,
                    "hidden_task_module": "absent", "network": "none"},
        "harness": {**versions, "worker_import": "ok",
                    "hidden_task_module": "absent", "network": "none"},
    }


def _claude_credentials_path() -> Path:
    path = (Path.home() / ".claude" / ".credentials.json").resolve()
    if not path.is_file():
        raise RuntimeError("Claude plan credential file is unavailable")
    return path


def _guard_receipt() -> dict[str, Any]:
    return check_subscription_guard(
        "claude-agent-sdk", model=MODEL, require_direct_probe=False,
        environ=os.environ,
    )


def _prepare_git_fixture(task: Mapping[str, Any], workspace: Path) -> tuple[str, str]:
    materialize_task(task, workspace)
    for arguments in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "normalized-rank@collie.run"],
        ["git", "config", "user.name", "Collie Normalized Rank"],
        ["git", "add", "-A"],
        ["git", "commit", "--quiet", "-m", "frozen baseline"],
    ):
        _run(arguments, cwd=workspace, check=True)
    commit = _run(["git", "rev-parse", "HEAD"], cwd=workspace,
                  check=True).stdout.strip()
    tree = _run(["git", "rev-parse", "HEAD^{tree}"], cwd=workspace,
                check=True).stdout.strip()
    return commit, tree


def _external_patch(workspace: Path, baseline: Path) -> str:
    if not baseline.is_dir():
        raise RuntimeError("evaluator baseline is missing")
    comparison = Path(tempfile.mkdtemp(prefix="normalized-external-patch-"))
    candidate = comparison / "candidate"
    repository = comparison / "repository"
    try:
        candidate.mkdir()
        for child in workspace.iterdir():
            if child.name == ".git":
                continue
            target = candidate / child.name
            if child.is_dir():
                shutil.copytree(child, target, ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".venv", "venv", "node_modules"))
            else:
                shutil.copy2(child, target)
        shutil.copytree(baseline, repository)
        for arguments in (
            ["git", "init", "--quiet"],
            ["git", "config", "user.email", "normalized-rank@collie.run"],
            ["git", "config", "user.name", "Collie Normalized Rank"],
            ["git", "add", "-A"],
            ["git", "commit", "--quiet", "-m", "pristine evaluator baseline"],
        ):
            _run(arguments, cwd=repository, check=True)
        # Replace the pristine worktree with the captured candidate files. The
        # agent can mutate its own .git freely without affecting this receipt.
        for child in repository.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        # Stage the empty tree before copying the candidate back.  Otherwise a
        # same-size file written within the filesystem timestamp granularity
        # can match Git's cached stat tuple and be skipped without rehashing.
        # Removing it from the index first makes every restored file explicit.
        _run(["git", "add", "-A"], cwd=repository, check=True)
        for child in candidate.iterdir():
            target = repository / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
        _run(["git", "add", "-A"], cwd=repository, check=True)
        patch = _run(
            ["git", "diff", "--binary", "--cached", "HEAD", "--"],
            cwd=repository, check=True).stdout
        if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
            raise RuntimeError("candidate patch exceeded evaluator limit")
        return patch
    finally:
        shutil.rmtree(comparison, ignore_errors=True)


def _grade(task: Mapping[str, Any], workspace: Path,
           patch_sha: str) -> dict[str, Any]:
    grader_root = Path(tempfile.mkdtemp(prefix="normalized-hidden-grader-"))
    try:
        grader = grader_root / "grader.py"
        marker = grader_root / "success.marker"
        nonce = uuid.uuid4().hex
        wrapper = "import sys\nsys.path.insert(0, %r)\n" % str(workspace)
        suffix = ("\nfrom pathlib import Path as _EvaluatorPath\n"
                  "_EvaluatorPath(%r).write_text(%r, encoding='utf-8')\n"
                  % (str(marker), nonce))
        grader.write_text(wrapper + str(task["hidden_grader"]) + suffix,
                          encoding="utf-8", newline="\n")
        try:
            result = _run([sys.executable, "-I", str(grader)], cwd=workspace,
                          timeout=30)
            marker_ok = marker.is_file() and marker.read_text(
                encoding="utf-8") == nonce
            resolved = result.returncode == 0 and marker_ok
            result_code: int | None = result.returncode
            detail = "" if resolved else (
                "hidden_contract_failed" if result.returncode in (0, 1)
                else "candidate_process_failed")
        except subprocess.TimeoutExpired:
            resolved = False
            result_code = None
            detail = "candidate_grader_timeout"
        return {
            "format": "collie-normalized-harness-grader-v1",
            "outcome": "graded", "resolved": resolved,
            "returncode": result_code,
            "success_marker_verified": resolved,
            "failure_detail": detail,
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


def _sidecar_command(image: str, name: str, network: str, credential: Path,
                     ledger: Path) -> list[str]:
    return [
        "docker", "run", "--detach", "--name", name, "--init",
        "--network", network, "--network-alias", "inference",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", "2g", "--cpus", "2", "--pids-limit", "192",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=268435456",
        "--tmpfs",
        "/home/runner/.claude:rw,nosuid,noexec,uid=10001,gid=10001,mode=0700,size=67108864",
        "--mount", _docker_mount(
            credential, "/home/runner/.claude/.credentials.json", readonly=True),
        "--mount", _docker_mount(ledger, "/ledger"),
        image, "--bind", "0.0.0.0", "--port", "8765",
        "--ledger-dir", "/ledger", "--timeout", str(SIDECAR_REQUEST_SECONDS),
        "--max-requests", str(DEFAULT_MAX_TURNS),
        "--allow-private-peers",
    ]


def _agent_create_command(image: str, name: str, network: str,
                          row: Mapping[str, Any], workspace: Path,
                          input_dir: Path, output_dir: Path, state_dir: Path) -> list[str]:
    return [
        "docker", "create", "--name", name, "--init", "--network", network,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", "3g", "--cpus", "2", "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=268435456",
        "--tmpfs", "/home/runner:rw,nosuid,size=134217728",
        "--env", "HOME=/home/runner", "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--mount", _docker_mount(workspace, "/workspace"),
        "--mount", _docker_mount(input_dir, "/input", readonly=True),
        "--mount", _docker_mount(output_dir, "/output"),
        "--mount", _docker_mount(state_dir, "/state"),
        image, "--arm", str(row["arm"]), "--task-json", "/input/task.json",
        "--workspace", "/workspace", "--run-dir", "/output",
        "--state-dir", "/state", "--output", "/output/worker.json",
        "--endpoint", "http://inference:8765/v1",
        "--max-turns", str(DEFAULT_MAX_TURNS),
    ]


def _docker_inspect(name: str) -> dict[str, Any]:
    value = json.loads(_run(["docker", "inspect", name], timeout=30,
                            check=True).stdout)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise RuntimeError("Docker inspect returned invalid evidence")
    return value[0]


def _mount_destinations(inspect: Mapping[str, Any]) -> set[str]:
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list):
        raise RuntimeError("Docker mount evidence is missing")
    return {str(item.get("Destination")) for item in mounts
            if isinstance(item, Mapping)}


def _mount_map(inspect: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list):
        raise RuntimeError("Docker mount evidence is missing")
    result: dict[str, Mapping[str, Any]] = {}
    for item in mounts:
        if not isinstance(item, Mapping):
            raise RuntimeError("Docker mount evidence is malformed")
        destination = str(item.get("Destination") or "")
        if not destination or destination in result:
            raise RuntimeError("Docker mount evidence is malformed")
        result[destination] = item
    return result


def _network_names(inspect: Mapping[str, Any]) -> set[str]:
    settings = inspect.get("NetworkSettings")
    networks = settings.get("Networks") if isinstance(settings, Mapping) else None
    if not isinstance(networks, Mapping):
        raise RuntimeError("Docker network evidence is missing")
    return {str(key) for key in networks}


def _attest_internal_network(name: str) -> dict[str, Any]:
    value = json.loads(_run(["docker", "network", "inspect", name],
                            timeout=30, check=True).stdout)
    if (not isinstance(value, list) or len(value) != 1
            or not isinstance(value[0], Mapping)):
        raise RuntimeError("Docker network inspect returned invalid evidence")
    network = value[0]
    if (network.get("Name") != name or network.get("Driver") != "bridge"
            or network.get("Internal") is not True):
        raise RuntimeError("attempt network is not an evaluator-owned internal bridge")
    return {"driver": "bridge", "internal": True}


def _attest_egress_network(name: str) -> dict[str, Any]:
    value = json.loads(_run(["docker", "network", "inspect", name],
                            timeout=30, check=True).stdout)
    if (not isinstance(value, list) or len(value) != 1
            or not isinstance(value[0], Mapping)):
        raise RuntimeError("Docker network inspect returned invalid evidence")
    network = value[0]
    if (network.get("Name") != name or network.get("Driver") != "bridge"
            or network.get("Internal") is not False):
        raise RuntimeError("attempt egress network is not an isolated bridge")
    return {"driver": "bridge", "internal": False, "attempt_scoped": True}


def _attest_sidecar(name: str, network: str, egress: str) -> dict[str, Any]:
    evidence = _docker_inspect(name)
    if _network_names(evidence) != {network, egress}:
        raise RuntimeError("sidecar network attachment mismatch")
    expected_mounts = {"/home/runner/.claude/.credentials.json", "/ledger"}
    mounts = _mount_map(evidence)
    if set(mounts) != expected_mounts:
        raise RuntimeError("sidecar mount isolation mismatch")
    if (mounts["/home/runner/.claude/.credentials.json"].get("RW") is not False
            or mounts["/ledger"].get("RW") is not True):
        raise RuntimeError("sidecar credential or ledger mount mode mismatch")
    host = evidence.get("HostConfig")
    bindings = host.get("PortBindings") if isinstance(host, Mapping) else None
    if bindings not in (None, {}):
        raise RuntimeError("sidecar unexpectedly published a host port")
    tmpfs = host.get("Tmpfs") if isinstance(host, Mapping) else None
    if not isinstance(tmpfs, Mapping):
        raise RuntimeError("sidecar writable Claude home evidence is missing")
    options = tmpfs.get("/home/runner/.claude")
    required_options = {
        "rw", "nosuid", "noexec", "uid=10001", "gid=10001", "mode=0700",
        "size=67108864",
    }
    if not isinstance(options, str) or not required_options.issubset(
            set(options.split(","))):
        raise RuntimeError("sidecar writable Claude home is not safely isolated")
    return {
        "networks": ["evaluator_internal", "evaluator_attempt_egress"],
        "host_ports_published": False,
        "mounts": ["claude_credential_read_only", "evaluator_ledger"],
        "claude_home": "isolated_tmpfs_with_nested_read_only_credential",
    }


def _attest_agent(name: str, network: str) -> dict[str, Any]:
    evidence = _docker_inspect(name)
    if _network_names(evidence) != {network}:
        raise RuntimeError("agent network attachment mismatch")
    expected = {"/workspace", "/input", "/output", "/state"}
    mounts = _mount_map(evidence)
    destinations = set(mounts)
    if destinations != expected or any("credential" in item.lower()
                                       for item in destinations):
        raise RuntimeError("agent mount isolation mismatch")
    if (mounts["/input"].get("RW") is not False
            or any(mounts[path].get("RW") is not True
                   for path in ("/workspace", "/output", "/state"))):
        raise RuntimeError("agent mount mode isolation mismatch")
    config = evidence.get("Config")
    environment = config.get("Env") if isinstance(config, Mapping) else []
    forbidden = ("ANTHROPIC_API_KEY=", "CLAUDE_CODE_OAUTH_TOKEN=",
                 "OPENAI_API_KEY=")
    if not isinstance(environment, list) or any(
            str(item).startswith(forbidden) for item in environment):
        raise RuntimeError("agent received a provider credential environment variable")
    return {
        "networks": ["evaluator_internal"],
        "external_network": False,
        "credential_mount": False,
        "workspace_input_output_state_mounts_only": True,
    }


def _wait_sidecar(name: str, timeout: float = 15.0) -> None:
    script = (
        "import json,urllib.request\n"
        "v=json.load(urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=2))\n"
        "assert v=={'status':'ok','model':'%s'}\n" % MODEL
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _run(["docker", "exec", name, "python", "-c", script],
                      timeout=5)
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError("sidecar health check did not become ready")


def _remove_container(name: str) -> bool:
    _run(["docker", "rm", "--force", name], timeout=30)
    return _run(["docker", "inspect", name], timeout=10).returncode != 0


def _remove_network(name: str) -> bool:
    _run(["docker", "network", "rm", name], timeout=30)
    return _run(["docker", "network", "inspect", name],
                timeout=10).returncode != 0


def _validate_sidecar_ledger(directory: Path) -> dict[str, Any]:
    """Validate every physical request and return a prompt-free summary."""
    if not directory.is_dir():
        raise RuntimeError("sidecar ledger directory is missing")
    entries = sorted(directory.iterdir())
    if not entries or any(not path.is_file() or path.suffix != ".json"
                          for path in entries):
        raise RuntimeError("sidecar ledger is empty or contains partial evidence")
    rows: list[dict[str, Any]] = []
    for path in entries:
        rows.append(_load_json(path))
    reserved: dict[str, int] = {}
    settled: dict[str, int] = {}
    budget_rejected: set[str] = set()
    outcomes: dict[str, int] = {}
    usage = {key: 0 for key in (
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens")}
    for index, row in enumerate(rows):
        event = row.get("event")
        request_id = row.get("request_id")
        if (not isinstance(request_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", request_id)
                or row.get("model") != MODEL):
            raise RuntimeError("sidecar request identity or model mismatch")
        if event == "reserved":
            allowed = {"schema_version", "event", "request_id", "created_at_utc",
                       "model", "request_sha256", "prompt_sha256", "request_bytes"}
            if set(row) != allowed or request_id in reserved:
                raise RuntimeError("sidecar reservation is malformed or duplicated")
            if (not re.fullmatch(r"[0-9a-f]{64}", str(row.get("request_sha256")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("prompt_sha256")))
                    or not isinstance(row.get("request_bytes"), int)
                    or isinstance(row.get("request_bytes"), bool)
                    or int(row["request_bytes"]) <= 0):
                raise RuntimeError("sidecar reservation fields are invalid")
            reserved[request_id] = index
        elif event == "settled":
            allowed = {"schema_version", "event", "request_id", "created_at_utc",
                       "model", "outcome", "duration_ms", "usage"}
            if "error_code" in row:
                allowed.add("error_code")
            if set(row) != allowed or request_id in settled:
                raise RuntimeError("sidecar settlement is malformed or duplicated")
            outcome = row.get("outcome")
            if outcome not in {"completed", "error", "cancelled", "timeout"}:
                raise RuntimeError("sidecar settlement outcome is invalid")
            duration = row.get("duration_ms")
            if (not isinstance(duration, int) or isinstance(duration, bool)
                    or duration < 0):
                raise RuntimeError("sidecar settlement duration is invalid")
            values = row.get("usage")
            if not isinstance(values, dict) or set(values) != set(usage):
                raise RuntimeError("sidecar settlement usage is missing")
            for key, value in values.items():
                if (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                    raise RuntimeError("sidecar settlement usage is invalid")
                usage[key] += value
            settled[request_id] = index
            outcomes[str(outcome)] = outcomes.get(str(outcome), 0) + 1
        elif event == "budget_exhausted":
            allowed = {"schema_version", "event", "request_id", "created_at_utc",
                       "model", "max_requests"}
            if (set(row) != allowed or row.get("max_requests") != DEFAULT_MAX_TURNS
                    or request_id in budget_rejected
                    or request_id in reserved or request_id in settled):
                raise RuntimeError("sidecar budget receipt is malformed")
            budget_rejected.add(request_id)
            outcomes["budget_exhausted"] = outcomes.get("budget_exhausted", 0) + 1
        else:
            raise RuntimeError("sidecar ledger contains an unknown event")
    if not reserved or set(reserved) != set(settled):
        raise RuntimeError("not every physical request was reserved and settled")
    if any(reserved[key] >= settled[key] for key in reserved):
        raise RuntimeError("sidecar settlement preceded its reservation")
    if outcomes.get("completed", 0) < 1 or sum(usage.values()) < 1:
        raise RuntimeError("sidecar has no completed request with usage evidence")
    if outcomes.get("completed", 0) != len(reserved):
        raise RuntimeError("sidecar contains a non-completed physical request")
    if len(reserved) > DEFAULT_MAX_TURNS:
        raise RuntimeError("sidecar physical-request budget was exceeded")
    budget_stops = outcomes.get("budget_exhausted", 0)
    if budget_stops not in (0, 1) or (
            budget_stops == 1 and len(reserved) != DEFAULT_MAX_TURNS):
        raise RuntimeError("sidecar budget-stop evidence is inconsistent")
    return {
        "schema_version": 1,
        "model": MODEL,
        "physical_requests": len(reserved),
        "reserved_requests": len(reserved),
        "settled_requests": len(settled),
        "outcomes": dict(sorted(outcomes.items())),
        "usage": usage,
        "ledger_sha256": _sha_bytes(_canonical_bytes(rows)),
    }


def _validate_worker_receipt(worker: Mapping[str, Any], task: Mapping[str, Any],
                             arm: str, patch: str) -> dict[str, Any]:
    if worker.get("worker_outcome") not in {"candidate", "product_failure"}:
        raise RuntimeError("agent worker did not produce a scoreable receipt")
    bindings = {
        "run_id": task["run_id"], "task_id": task["task_id"],
        "arm": arm, "model": MODEL,
        "delivered_prompt_sha256": task["delivered_prompt_sha256"],
    }
    if any(worker.get(key) != value for key, value in bindings.items()):
        raise RuntimeError("agent worker receipt binding mismatch")
    runtime = worker.get("runtime")
    if (not isinstance(runtime, dict) or not runtime.get("product")
            or runtime.get("model") != MODEL):
        raise RuntimeError("agent runtime evidence is missing")
    if not isinstance(worker.get("usage"), dict):
        raise RuntimeError("agent-reported usage is malformed")
    if not isinstance(worker.get("patch"), str):
        raise RuntimeError("worker patch evidence is malformed")
    duration = worker.get("duration_ms")
    if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
            or duration < 0):
        raise RuntimeError("agent duration evidence is missing")
    evidence = worker.get("tool_evidence")
    if not isinstance(evidence, dict):
        raise RuntimeError("agent terminal evidence is missing")
    terminal_observed = evidence.get("terminal_observed") is True
    if worker.get("worker_outcome") == "candidate" and not terminal_observed:
        raise RuntimeError("agent terminal evidence is missing")
    result: dict[str, Any] = {"terminal_observed": terminal_observed}
    for key in ("native_tool_calls", "native_edit_calls"):
        value = evidence.get(key)
        if (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise RuntimeError("agent native-tool evidence is malformed")
        result[key] = value
    return result


def _admission_capability_proven(tool_evidence: Mapping[str, Any], patch: str) -> bool:
    """Prove the adapted harness returns a completed terminal receipt.

    Admission is an infrastructure gate, not an unscored extra capability
    sample.  Native tool use, editing, and task resolution are behavioral
    outcomes and belong in the formal score.  A completed transport response
    that terminates before a tool call must therefore remain scoreable rather
    than looking like a broken adapter.
    """
    del patch  # retained in the signature for receipt/test compatibility
    return tool_evidence.get("terminal_observed") is True


def _safe_worker_error(worker: Mapping[str, Any]) -> str:
    value = worker.get("error_code")
    if isinstance(value, str) and re.fullmatch(r"[a-z0-9_.-]{1,80}", value):
        return value
    return "agent_worker_invalid"


def _run_one(sidecar_image: str, harness_image: str, suite_sha: str,
             row: Mapping[str, Any], credential: Path, suite_temp: Path,
             result_root: Path, wall_seconds: int) -> dict[str, Any]:
    task = task_by_id(str(row["task_id"]))
    run_dir = result_root / "runs" / str(row["run_id"])
    ledger_dir = result_root / "evaluator-ledgers" / str(row["run_id"])
    run_dir.mkdir(parents=True, exist_ok=False)
    ledger_dir.mkdir(parents=True, exist_ok=False)
    reservation = {
        **row, "schema_version": 1, "suite_sha256": suite_sha,
        "reserved_at_utc": _utc_now(), "state": "reserved",
    }
    _atomic_json(run_dir / "reservation.json", reservation)
    reservation_sha = _sha_file(run_dir / "reservation.json")

    root = suite_temp / str(row["run_id"])
    workspace, baseline = root / "workspace", root / "baseline"
    input_dir, state_dir = root / "input", root / "state"
    for path in (workspace, input_dir, state_dir):
        path.mkdir(parents=True, exist_ok=False)
    baseline_commit, baseline_tree = _prepare_git_fixture(task, workspace)
    materialize_task(task, baseline)
    prompt = SHARED_EVALUATOR_PROMPT + str(task["prompt"])
    worker_input: dict[str, Any] = {
        **row,
        "schema_version": 1,
        "prompt": task["prompt"],
        "delivered_prompt": prompt,
        "delivered_prompt_sha256": _sha_bytes(prompt.encode("utf-8")),
        "model": MODEL,
        "wall_seconds": wall_seconds,
        "endpoint": "http://inference:8765/v1",
        "sidecar_bearer": BEARER_SENTINEL,
    }
    _atomic_json(input_dir / "task.json", worker_input)

    suffix = _sha_bytes((suite_sha + str(row["run_id"])).encode("utf-8"))[:12]
    network = "collie-norm-net-" + suffix
    egress = "collie-norm-egress-" + suffix
    sidecar_name = "collie-norm-inference-" + suffix
    agent_name = "collie-norm-agent-" + suffix
    network_created = egress_created = sidecar_created = agent_created = False
    agent_exit: int | None = None
    timed_out = False
    orchestration_error = ""
    cleanup_ok = True
    network_evidence: dict[str, Any] = {}
    started = time.monotonic()
    try:
        _run(["docker", "network", "create", "--driver", "bridge", "--internal",
              network], timeout=30, check=True)
        network_created = True
        network_evidence["attempt_network"] = _attest_internal_network(network)
        _run(["docker", "network", "create", "--driver", "bridge", egress],
             timeout=30, check=True)
        egress_created = True
        network_evidence["attempt_egress"] = _attest_egress_network(egress)
        _run(_sidecar_command(sidecar_image, sidecar_name, network,
                              credential, ledger_dir), timeout=45, check=True)
        sidecar_created = True
        _run(["docker", "network", "connect", egress, sidecar_name],
             timeout=30, check=True)
        network_evidence["sidecar"] = _attest_sidecar(
            sidecar_name, network, egress)
        _wait_sidecar(sidecar_name)
        _run(_agent_create_command(
            harness_image, agent_name, network, row, workspace, input_dir,
            run_dir, state_dir), timeout=45, check=True)
        agent_created = True
        _run(["docker", "start", agent_name], timeout=30, check=True)
        network_evidence["agent"] = _attest_agent(agent_name, network)
        remaining = max(1.0, wall_seconds - (time.monotonic() - started))
        waited = _run(["docker", "wait", agent_name], timeout=remaining,
                      check=True)
        try:
            agent_exit = int(waited.stdout.strip())
        except ValueError as exc:
            raise RuntimeError("Docker wait returned an invalid exit code") from exc
        sidecar_state = _docker_inspect(sidecar_name).get("State")
        if not isinstance(sidecar_state, Mapping) or sidecar_state.get("Running") is not True:
            raise RuntimeError("sidecar exited before attempt completion")
    except subprocess.TimeoutExpired:
        timed_out = True
        orchestration_error = "outer_wall_timeout"
    except Exception:
        orchestration_error = "attempt_orchestration_failure"
    finally:
        if agent_created:
            cleanup_ok = _remove_container(agent_name) and cleanup_ok
        if sidecar_created:
            cleanup_ok = _remove_container(sidecar_name) and cleanup_ok
        if network_created:
            cleanup_ok = _remove_network(network) and cleanup_ok
        if egress_created:
            cleanup_ok = _remove_network(egress) and cleanup_ok

    worker_path = run_dir / "worker.json"
    worker = _load_json(worker_path) if worker_path.is_file() else {}
    try:
        patch = _external_patch(workspace, baseline)
    except Exception:
        patch = ""
        if not orchestration_error:
            orchestration_error = "evaluator_patch_collection_failure"
    _atomic_text(run_dir / "patch.diff", patch)
    patch_sha = _sha_bytes(patch.encode("utf-8"))

    ledger_summary: dict[str, Any] = {}
    ledger_error = ""
    try:
        ledger_summary = _validate_sidecar_ledger(ledger_dir)
    except Exception:
        ledger_error = "sidecar_ledger_invalid"

    tool_evidence: dict[str, int] = {}
    worker_error = ""
    try:
        tool_evidence = _validate_worker_receipt(worker, worker_input,
                                                 str(row["arm"]), patch)
    except Exception:
        worker_error = _safe_worker_error(worker)

    grader = {"outcome": "not_run", "resolved": None,
              "patch_sha256": patch_sha}
    status = "invalid_infrastructure"
    error_code = ""
    if timed_out:
        error_code = "outer_wall_timeout"
    elif orchestration_error:
        error_code = orchestration_error
    elif not cleanup_ok:
        error_code = "container_or_network_cleanup_unconfirmed"
    elif ledger_error:
        error_code = ledger_error
    elif worker_error:
        error_code = worker_error
    elif ((worker.get("worker_outcome") == "candidate" and agent_exit != 0)
          or (worker.get("worker_outcome") == "product_failure" and agent_exit != 2)
          or worker.get("worker_outcome") not in {"candidate", "product_failure"}):
        error_code = "agent_container_exit"
    elif row.get("phase") == "admission" and not _admission_capability_proven(
            tool_evidence, patch):
        error_code = "admission_native_edit_capability_unproven"
    else:
        # Once transport, receipt, exit, and isolation evidence are valid, the
        # evaluator grades the resulting workspace regardless of how the
        # harness described its own terminal state.  A harness may report a
        # final model/tool error after already writing a correct solution.
        grader = _grade(task, workspace, patch_sha)
        if grader.get("outcome") == "graded":
            status = ("valid_resolved" if grader.get("resolved") is True
                      else "valid_unresolved")
            error_code = ("" if status == "valid_resolved" else
                          str(grader.get("failure_detail") or
                              worker.get("error_code") or
                              "hidden_contract_failed"))
        else:
            error_code = "hidden_grader_infrastructure_failure"

    _atomic_json(run_dir / "grader.json", grader)
    terminal = {
        **row,
        "schema_version": 1,
        "suite_sha256": suite_sha,
        "status": status,
        "resolved": status == "valid_resolved",
        "error_code": error_code,
        "worker_outcome": worker.get("worker_outcome"),
        "worker_error_code": (
            _safe_worker_error(worker)
            if worker.get("worker_outcome") == "product_failure" else ""),
        "reservation_sha256": reservation_sha,
        "baseline_commit": baseline_commit,
        "baseline_tree": baseline_tree,
        "delivered_prompt_sha256": worker_input["delivered_prompt_sha256"],
        "patch_sha256": patch_sha,
        "patch_bytes": len(patch.encode("utf-8")),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "worker_duration_ms": worker.get("duration_ms"),
        "usage": ledger_summary.get("usage", {}),
        "reported_usage": worker.get("usage") if isinstance(worker.get("usage"), dict) else {},
        "worker_patch_sha256": _sha_bytes(
            str(worker.get("patch") or "").encode("utf-8")),
        "runtime": worker.get("runtime") if isinstance(worker.get("runtime"), dict) else {},
        "sidecar_request_evidence": ledger_summary,
        "tool_evidence": {
            **tool_evidence,
            "terminal_observed": bool(
                isinstance(worker.get("tool_evidence"), dict)
                and worker["tool_evidence"].get("terminal_observed") is True),
        },
        "network_evidence": network_evidence,
        "grader": grader,
        "completed_at_utc": _utc_now(),
    }
    _atomic_json(run_dir / "result.json", terminal)
    shutil.rmtree(root, ignore_errors=True)
    return terminal


def _ranking(scores: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    ranking = sorted(
        ({"arm": arm, "score": scores[arm]["solve_rate"]} for arm in ARMS),
        key=lambda item: (-float(item["score"]), item["arm"]),
    )
    for item in ranking:
        item["rank"] = 1 + sum(
            float(other["score"]) > float(item["score"]) for other in ranking)
    return ranking


def summarize(plan: list[dict[str, Any]], rows: list[dict[str, Any]],
              suite_sha: str, *, require_post_run_billing: bool = False) -> dict[str, Any]:
    expected = {str(row["run_id"]): row for row in plan}
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

    scores: dict[str, Any] | None = None
    computed: list[dict[str, Any]] | None = None
    if not errors and len(rows) == len(plan):
        scores = {}
        for arm in ARMS:
            selected = [row for row in rows if row["arm"] == arm]
            solved = sum(row.get("resolved") is True for row in selected)
            durations = [float(row["duration_ms"]) for row in selected
                         if isinstance(row.get("duration_ms"), (int, float))]
            scores[arm] = {
                "resolved": solved,
                "attempts": len(selected),
                "solve_rate": solved / len(selected),
                "median_duration_ms": statistics.median(durations) if durations else None,
            }
        computed = _ranking(scores)
    ranking = None if require_post_run_billing else computed
    return {
        "schema_version": 1,
        "suite_sha256": suite_sha,
        "claim": CLAIM,
        "scope": "exploratory",
        "publishable": False,
        "comparison_label": COMPARISON_LABEL,
        "ranking_withheld": bool(errors) or require_post_run_billing,
        "ranking_withheld_reason": (
            "validation_errors" if errors else
            "post_run_claude_billing_ui_recheck_pending"
            if require_post_run_billing else None),
        "billing_post_run_verified": not require_post_run_billing,
        "validation_errors": errors,
        "scores": scores,
        "ranking": ranking,
        "limitations": [
            "two synthetic tasks are insufficient for a general capability claim",
            "all four harnesses are adapted to an evaluator-owned compatibility sidecar",
            "system prompts, loop policies, context handling, and local tool surfaces differ",
            "only the initial evaluator-owned user message is byte-identical; model-visible prompts differ",
            "native tool and edit counts are diagnostic and not comparable across harnesses",
            "this does not measure each product's default or native deployment",
            "subscription quota consumption is not a metered billing receipt",
        ],
        "generated_at_utc": _utc_now(),
    }


def summarize_admission(plan: list[dict[str, Any]], rows: list[dict[str, Any]],
                        suite_sha: str) -> dict[str, Any]:
    """Validate admission receipts without producing a capability table."""
    result = summarize(plan, rows, suite_sha)
    result.update({
        "admitted": not result["validation_errors"],
        "scores": None,
        "ranking": None,
        "ranking_withheld": True,
        "ranking_withheld_reason": "admission_is_not_scored",
    })
    return result


def _manifest(revision: str, source_hashes: Mapping[str, str],
              sidecar_image: str, harness_image: str, repetitions: int,
              wall_seconds: int, plans: Mapping[str, Any],
              guard: Mapping[str, Any], image_preflight: Mapping[str, Any],
              claude_evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "suite_id": "collie-normalized-harness-v1",
        "claim": CLAIM,
        "scope": "exploratory",
        "publishable": False,
        "comparison_label": COMPARISON_LABEL,
        "git_revision": revision,
        "source_sha256": dict(source_hashes),
        "images": {"sidecar": sidecar_image, "harness": harness_image},
        "dockerfile_sha256": {
            "sidecar": _sha_file(SIDECAR_DOCKERFILE),
            "harness": _sha_file(HARNESS_DOCKERFILE),
        },
        "worker_sha256": _sha_file(WORKER),
        "image_preflight": dict(image_preflight),
        "tasks": [{
            "task_id": task["task_id"],
            "task_sha256": task_sha256(task),
            "fixture_sha256": canonical_sha256(task["fixture_files"]),
            "grader_sha256": _sha_bytes(task["hidden_grader"].encode("utf-8")),
        } for task in TASKS],
        "model": MODEL,
        "reasoning_effort": "high",
        "delivered_prompt_prefix_sha256": _sha_bytes(
            SHARED_EVALUATOR_PROMPT.encode("utf-8")),
        "prompt_contract": "byte_identical_evaluator_owned_user_message_per_task",
        "transport": {
            "surface": "OpenAI-compatible internal sidecar backed by Claude Agent SDK",
            "one_sdk_turn_per_physical_sidecar_request": True,
            "claude_p_invoked": False,
            "api_key_fallback_disabled": True,
            "model_route_attested_per_request": True,
            "request_ledger": "evaluator_owned_reserved_and_settled_receipts",
        },
        "arms": {
            arm: {
                "harness": arm,
                "model": MODEL,
                "transport": "same evaluator sidecar",
                "agent_loop_system_prompt_and_local_tools": "harness_owned",
                "deployment": "adapted_not_product_default",
            } for arm in ARMS
        },
        "repetitions_per_task_arm": repetitions,
        "physical_model_request_budget_per_attempt": DEFAULT_MAX_TURNS,
        "agent_wall_seconds": wall_seconds,
        "admission_plan": plans["admission"],
        "ranking_plan": plans["ranking"],
        "launch_policy": "one_admission_per_arm_then_four_arm_rotating_schedule",
        "admission_contract": (
            "completed_transport_and_terminal_receipt; native_tool_use_editing_"
            "and_resolution_are_scored_only_in_formal_runs"
        ),
        "network": {
            "per_attempt_evaluator_owned_internal_network": True,
            "per_attempt_evaluator_owned_egress_network": True,
            "sidecar_also_connected_to_attempt_egress": True,
            "agent_external_network": False,
            "host_port_published": False,
        },
        "credential_isolation": {
            "sidecar_only": True,
            "agent_mount_or_environment": False,
        },
        "fresh_git_workspace_per_attempt": True,
        "gold_and_hidden_grader_visible_to_agent": False,
        "guard_receipt_sha256": _sha_bytes(_canonical_bytes(guard)),
        "billing": {
            "track": "claude_subscription_same_transport",
            "launch_ui_evidence": dict(claude_evidence),
            "post_run_ui_recheck_required": True,
            "actual_marginal_charge_observed": False,
        },
    }


def execute(*, repetitions: int, wall_seconds: int,
            claude_account_evidence: Mapping[str, Any],
            preflight_only: bool = False,
            sidecar_image_tag: str = SIDECAR_IMAGE_TAG,
            harness_image_tag: str = HARNESS_IMAGE_TAG) -> int:
    task_self_check()
    normalized_evidence = dict(claude_account_evidence)
    normalized_evidence["observed_at_utc"] = _parse_recent_evidence_timestamp(
        normalized_evidence.get("observed_at_utc"), label="Claude launch")
    _require_safe_claude_evidence(normalized_evidence, label="Claude launch")
    revision, source_hashes = _source_revision_and_hashes(
        require_clean=not preflight_only)
    plans = {
        "admission": canonical_plan(1, admission=True),
        "ranking": canonical_plan(repetitions),
    }
    sidecar_image, harness_image = _build_images(
        sidecar_image_tag, harness_image_tag, revision)
    preflight = _image_preflight(sidecar_image, harness_image)
    credential = _claude_credentials_path()
    guard = _guard_receipt()
    core = _manifest(
        revision, source_hashes, sidecar_image, harness_image, repetitions,
        wall_seconds, plans, guard, preflight, normalized_evidence)
    suite_sha = _sha_bytes(_canonical_bytes(core))
    if preflight_only:
        print(json.dumps({
            "outcome": "preflight_ok",
            "publishable": False,
            "suite_sha256": suite_sha,
            "images": core["images"],
            "admission_launches": len(plans["admission"]),
            "ranking_launches": len(plans["ranking"]),
            "guard": {"provider": guard.get("provider"),
                      "verdict": guard.get("verdict")},
        }, ensure_ascii=False, indent=2))
        return 0

    result_root = RESULTS_ROOT / ("normalized-harness-v1-" + suite_sha[:12])
    suite_temp = TEMP_ROOT / ("normalized-harness-v1-" + suite_sha[:12])
    result_root.mkdir(parents=True, exist_ok=False)
    suite_temp.mkdir(parents=True, exist_ok=False)
    _atomic_json(result_root / "manifest.json", {
        **core, "suite_sha256": suite_sha, "created_at_utc": _utc_now(),
    })
    rows: list[dict[str, Any]] = []
    try:
        for phase in ("admission", "ranking"):
            for row in plans[phase]:
                terminal = _run_one(
                    sidecar_image, harness_image, suite_sha, row, credential,
                    suite_temp, result_root, wall_seconds)
                rows.append(terminal)
                print("[%s %02d] %-7s %-29s %s" % (
                    phase, row["slot"], row["arm"], row["task_id"],
                    terminal["status"]), flush=True)
                if terminal["status"] not in ("valid_resolved", "valid_unresolved"):
                    selected_plan = plans[phase]
                    selected_rows = [item for item in rows if item["phase"] == phase]
                    summary = summarize(
                        selected_plan, selected_rows, suite_sha,
                        require_post_run_billing=True)
                    summary["stopped_after_infrastructure_invalid_slot"] = row["run_id"]
                    _atomic_json(result_root / "summary.json", summary)
                    print("infrastructure-invalid slot; remaining launches were not consumed")
                    return 2
        ranking_rows = [row for row in rows if row["phase"] == "ranking"]
        summary = summarize(
            plans["ranking"], ranking_rows, suite_sha,
            require_post_run_billing=True)
        admission_summary = summarize_admission(
            plans["admission"],
            [row for row in rows if row["phase"] == "admission"], suite_sha)
        summary["admission"] = admission_summary
        _atomic_json(result_root / "summary.json", summary)
        print("results: %s" % result_root)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    finally:
        shutil.rmtree(suite_temp, ignore_errors=True)


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


def _require_safe_claude_evidence(value: Mapping[str, Any], *, label: str) -> None:
    spend = value.get("period_spend_usd")
    safe_spend = (isinstance(spend, (int, float)) and not isinstance(spend, bool)
                  and float(spend) == 0.0)
    if (value.get("usage_credits_enabled") is not False
            or value.get("auto_reload") is not False or not safe_spend):
        raise RuntimeError(label + " must show credits/reload off and zero spend")


def finalize_billing(result_root: Path, *, claude_evidence: Mapping[str, Any]) -> int:
    root = result_root.resolve()
    allowed = RESULTS_ROOT.resolve()
    if root.parent != allowed or not root.is_dir():
        raise RuntimeError("result directory is outside the normalized results root")
    manifest_path = root / "manifest.json"
    summary_path = root / "summary.json"
    manifest = _load_json(manifest_path)
    summary = _load_json(summary_path)
    if manifest.get("suite_sha256") != summary.get("suite_sha256"):
        raise RuntimeError("manifest and summary suite identities differ")
    created = dt.datetime.fromisoformat(
        str(manifest["created_at_utc"]).replace("Z", "+00:00")).astimezone(
            dt.timezone.utc)
    completed = dt.datetime.fromisoformat(
        str(summary["generated_at_utc"]).replace("Z", "+00:00")).astimezone(
            dt.timezone.utc)
    evidence = dict(claude_evidence)
    evidence["observed_at_utc"] = _parse_recent_evidence_timestamp(
        evidence.get("observed_at_utc"), label="Claude post-run",
        not_before=max(created, completed))
    safe = True
    try:
        _require_safe_claude_evidence(evidence, label="Claude post-run")
    except RuntimeError:
        safe = False
    receipt = {
        "schema_version": 1,
        "suite_sha256": manifest["suite_sha256"],
        "outcome": "verified_safe" if safe else "unsafe_or_incomplete",
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
    if safe and not summary.get("validation_errors"):
        summary["ranking"] = _ranking(summary.get("scores") or {})
        summary["ranking_withheld"] = False
        summary["ranking_withheld_reason"] = None
    else:
        summary["ranking"] = None
        summary["ranking_withheld"] = True
        summary["ranking_withheld_reason"] = (
            "validation_errors" if summary.get("validation_errors") else
            "post_run_claude_billing_ui_recheck_failed")
    summary["generated_at_utc"] = _utc_now()
    _atomic_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not summary["ranking_withheld"] else 2


def _claude_evidence(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "observed_at_utc": args.claude_evidence_observed_at,
        "usage_credits_enabled": False if args.claude_usage_credits_off else None,
        "auto_reload": False if args.claude_auto_reload_off else None,
        "period_spend_usd": args.claude_period_spend_usd,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="normalized_harness_rank")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--finalize-billing", type=Path, metavar="RESULT_DIR")
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--wall-seconds", type=int, default=DEFAULT_WALL_SECONDS)
    parser.add_argument("--claude-evidence-observed-at")
    parser.add_argument("--claude-usage-credits-off", action="store_true")
    parser.add_argument("--claude-auto-reload-off", action="store_true")
    parser.add_argument("--claude-period-spend-usd", type=float)
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if args.wall_seconds < 30:
        parser.error("--wall-seconds must be at least 30")
    evidence = _claude_evidence(args)
    if (not args.claude_evidence_observed_at
            or args.claude_period_spend_usd is None):
        parser.error("fresh Claude billing evidence is required")
    try:
        _require_safe_claude_evidence(evidence, label="Claude evidence")
    except RuntimeError as exc:
        parser.error(str(exc))
    if args.finalize_billing is not None:
        return finalize_billing(args.finalize_billing, claude_evidence=evidence)
    return execute(
        repetitions=args.repetitions,
        wall_seconds=args.wall_seconds,
        claude_account_evidence=evidence,
        preflight_only=args.preflight_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
