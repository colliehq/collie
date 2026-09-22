"""Portable high-concurrency normalized four-arm harness comparison.

This is an *adapted loop* benchmark, not a native product ranking.  Four agent
harnesses -- current Collie, Prime, Pi, and Hermes -- each keep their own agent
loop, system prompt, and local tool surface, but every model turn crosses the
same evaluator-owned OpenAI-compatible sidecar.  That sidecar delegates one turn
at a time to the official Claude Agent SDK using the operator's existing Claude
login.  No arm ever sees a credential.

Nothing in either source repository is modified.  The runner assembles a staged
code tree under ``--root`` that replaces the pinned benchmark image's old Collie
source with the *current* Collie ``harness`` package, copies the three tested
normalized adapter modules plus the tested ``subscription_sidecar`` beside it,
and rewrites the frozen ``claude-opus-4-8`` model constants to ``claude-opus-5``.
Both containers mount that staged tree read-only over ``/opt/collie``; the images
themselves are reused unchanged so their pinned Prime/Pi/Hermes/SDK
dependencies stay exactly as tested.

Isolation contract, per attempt:
  * a fresh evaluator-owned *internal* bridge network carries agent->sidecar;
  * a second attempt-scoped bridge gives the sidecar (and only the sidecar)
    egress for the official SDK;
  * no host port is ever published;
  * the sidecar alone mounts the user's Claude credential file, read-only;
  * the agent container gets workspace/input/output/state plus the read-only
    staged code tree, and no provider credential in its environment;
  * hidden graders and gold files never enter any container.

The runner does not launch experiments on its own schedule: a parent coordinates
total concurrency.  ``--concurrency`` bounds only this process.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import datetime as dt
import difflib
import hashlib
import importlib.util
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


# --------------------------------------------------------------------------
# Frozen identities and pins
# --------------------------------------------------------------------------

ARMS = ("collie", "prime", "pi", "hermes")
COLLIE_SHELL_ABLATION = False

MODEL = "claude-opus-5"
LEGACY_MODEL = "claude-opus-4-8"
REASONING_EFFORT = "high"

DEFAULT_REQUESTS_PER_ATTEMPT = 48        # explicit physical model requests
DEFAULT_ATTEMPT_SECONDS = 900            # outer per-attempt wall ceiling
DEFAULT_CONCURRENCY = 4
DEFAULT_REPETITIONS = 1
SIDECAR_REQUEST_SECONDS = 300            # per physical request, as tested
DEADLINE_MARGIN_SECONDS = 150            # setup + patch + grading headroom

DEFAULT_BENCH_REPO = Path(r"C:\workspace\collie-bench-new")
DEFAULT_COLLIE_REPO = Path(r"C:\workspace\collie")
PINNED_COLLIE_COMMIT = "8577c10a33370bf84ae9cf953db64354be92a30d"

SIDECAR_IMAGE_TAG = "collie-normalized-sidecar:v1"
HARNESS_IMAGE_TAG = "collie-normalized-harness:v1"

# Runtime pins baked into the two existing images.  These are asserted, not
# assumed: the images predate this runner and are the reason the old tags are
# reused rather than rebuilt.
EXPECTED_IMAGE_PINS = {
    "claude_agent_sdk": "0.2.136",
    "pi": "0.84.1",
    "prime": "0.7.2",
    "prime_commit": "0987c1ba7637cbcb99afe9efe1180b838a0aa958",
    "hermes": "0.15.2",
}

# Staged from the tested benchmark repository, unchanged apart from the frozen
# model-constant rewrite recorded in stage-patches/.
STAGED_ADAPTERS = (
    "bench/normalized_harness_worker.py",
    "bench/normalized_prime_pi.py",
    "bench/normalized_hermes.py",
)
STAGED_SIDECAR = "harness/subscription_sidecar.py"
MODEL_PATCH_FILES = (
    "harness/subscription_sidecar.py",
    "bench/normalized_prime_pi.py",
    "bench/normalized_hermes.py",
)
MODEL_REWRITES = (
    (LEGACY_MODEL, MODEL),
    ("Claude Opus 4.8 (normalized subscription transport)",
     "Claude Opus 5 (normalized subscription transport)"),
)

# Files that must never appear inside a staged tree: they carry the held-out
# graders and reference solutions of the original suite.
FORBIDDEN_STAGE_NAMES = ("subscription_rank_tasks.py", "current_product_worker.py")

CLAIM = "exploratory_adapted_harness_same_subscription_transport_comparison"
COMPARISON_LABEL = "adapted_harness_same_transport_not_native_product_ranking"
SUITE_ID = "collie-normalized-batch-v1"

TASK_KEYS = frozenset(
    {"task_id", "prompt", "fixture_files", "gold_files", "hidden_grader"})
MAX_PATCH_BYTES = 1024 * 1024
MAX_LOG_BYTES = 512 * 1024

# Worker error codes that indicate the shared evaluator transport broke rather
# than one arm's adapter.  Everything else that fails to produce a scoreable
# receipt is attributed to the arm's adapter and reported separately.
EVALUATOR_INFRA_ERRORS = frozenset({
    "normalized_transport_auth_failure",
    "normalized_transport_unavailable",
    "normalized_model_route_failure",
    "sidecar_unavailable",
    "auth_rejected",
    "model_unavailable",
})
WALL_TIMEOUT_ERRORS = frozenset({
    "harness_wall_timeout", "hermes_wall_timeout", "timeout",
})

STATUSES = ("valid_resolved", "valid_unresolved", "invalid_adapter",
            "invalid_wall_timeout", "invalid_infrastructure", "not_attempted")


class EvaluatorError(RuntimeError):
    """A fail-closed evaluator precondition, safe to print."""


# --------------------------------------------------------------------------
# Small deterministic helpers (canonical JSON, atomic writes, subprocess)
# --------------------------------------------------------------------------

def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_text(value: str) -> str:
    return _sha_bytes(value.encode("utf-8"))


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write(path, _canonical_bytes(value) + b"\n")


def _atomic_text(path: Path, value: str) -> None:
    _atomic_write(path, value.encode("utf-8"))


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise EvaluatorError("JSON evidence at %s is not an object" % path.name)
    return value


def _run(command: Sequence[str], *, cwd: Path | None = None, timeout: float = 60,
         check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command), cwd=str(cwd) if cwd else None, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
        check=False,
    )
    if check and result.returncode:
        raise EvaluatorError("command failed (%d): %s" % (
            result.returncode, " ".join(command[:3])))
    return result


def _git_revision(repo: Path) -> str:
    result = _run(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=30)
    if result.returncode:
        return ""
    return result.stdout.strip()


def _git_dirty(repo: Path) -> bool:
    result = _run(["git", "-C", str(repo), "status", "--porcelain"], timeout=60)
    return bool(result.returncode) or bool(result.stdout.strip())


# Secrets must never reach a persisted log.  Agent containers hold no
# credential at all; this is defense in depth for sidecar stderr.
_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)(access_token|refresh_token|id_token|api[_-]?key|"
               r"client_secret|password)(\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]{8,}"),
)


def _scrub(text: str) -> str:
    value = text[:MAX_LOG_BYTES]
    value = _SECRET_PATTERNS[0].sub("[redacted-token]", value)
    value = _SECRET_PATTERNS[1].sub("Bearer [redacted]", value)
    value = _SECRET_PATTERNS[2].sub(r"\1\2[redacted]", value)
    return value


def _safe_code(value: object, fallback: str) -> str:
    text = str(value or "")
    return text if re.fullmatch(r"[a-z0-9_.:-]{1,80}", text) else fallback


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-")
    return (cleaned or "task")[:48]


# --------------------------------------------------------------------------
# Reuse of the tested evaluator-side task helpers
# --------------------------------------------------------------------------

def load_bench_task_helpers(bench_repo: Path) -> types.ModuleType:
    """Load the tested task fixture/grader helpers *by path*.

    ``bench/subscription_rank_tasks.py`` is stdlib-only and its generic helpers
    (``materialize_task``, ``canonical_sha256``, ``task_sha256``,
    ``_validate_task_data``, ``_run_hidden_grader``) operate on any task mapping.
    It is loaded by file path rather than by package import so that the old
    repository's ``harness`` package can never shadow the current Collie source
    on ``sys.path``.  Its frozen ``TASKS`` tuple is never used here.
    """
    path = bench_repo / "bench" / "subscription_rank_tasks.py"
    if not path.is_file():
        raise EvaluatorError("tested task helpers are missing: %s" % path)
    spec = importlib.util.spec_from_file_location(
        "collie_bench_task_helpers", path)
    if spec is None or spec.loader is None:
        raise EvaluatorError("could not load the tested task helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("materialize_task", "canonical_sha256", "task_sha256",
                 "_validate_task_data", "_run_hidden_grader"):
        if not hasattr(module, name):
            raise EvaluatorError("tested task helper %s is unavailable" % name)
    return module


def _module_constants(path: Path, names: Sequence[str]) -> dict[str, Any]:
    """Read module-level literal constants without importing the module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    wanted = set(names)
    found: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    missing = sorted(wanted - set(found))
    if missing:
        raise EvaluatorError("constants not found in %s: %s"
                             % (path.name, ", ".join(missing)))
    return found


def shared_evaluator_prompt(bench_repo: Path) -> str:
    """The byte-identical evaluator-owned prompt prefix used by every arm."""
    path = bench_repo / "bench" / "current_product_worker.py"
    if not path.is_file():
        raise EvaluatorError("tested shared prompt module is missing: %s" % path)
    value = _module_constants(path, ["SHARED_EVALUATOR_PROMPT"])[
        "SHARED_EVALUATOR_PROMPT"]
    if not isinstance(value, str) or not value.strip():
        raise EvaluatorError("tested shared prompt is not a non-empty string")
    return value


def load_current_subscription_guard(collie_repo: Path) -> Callable[..., Any]:
    """Import the *current* Collie subscription guard for the launch receipt."""
    root = str(collie_repo.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    from harness.subscription_guard import check_subscription_guard  # noqa: E402
    return check_subscription_guard


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

def _normalized_task(raw: Mapping[str, Any], index: int) -> tuple[dict[str, Any], list[str]]:
    """Coerce one supplied task into the tested frozen task schema."""
    if not isinstance(raw, Mapping):
        raise EvaluatorError("task %d is not an object" % index)
    keys = set(raw)
    if keys != TASK_KEYS:
        raise EvaluatorError(
            "task %d keys must be exactly %s (got %s)"
            % (index, sorted(TASK_KEYS), sorted(keys)))
    notes: list[str] = []
    task: dict[str, Any] = {}
    task["task_id"] = str(raw["task_id"])
    for field in ("prompt", "hidden_grader"):
        value = raw[field]
        if not isinstance(value, str) or not value.strip():
            raise EvaluatorError("task %s %s must be non-empty text"
                                 % (task["task_id"], field))
        if not value.endswith("\n"):
            value += "\n"
            notes.append("%s:%s:trailing_newline_added" % (task["task_id"], field))
        task[field] = value
    for field in ("fixture_files", "gold_files"):
        files = raw[field]
        if not isinstance(files, Mapping) or not files:
            raise EvaluatorError("task %s %s must be a non-empty mapping"
                                 % (task["task_id"], field))
        normalized: dict[str, str] = {}
        for relative, content in files.items():
            if not isinstance(content, str):
                raise EvaluatorError("task %s %s[%s] must be text"
                                     % (task["task_id"], field, relative))
            if not content.endswith("\n"):
                content += "\n"
                notes.append("%s:%s:%s:trailing_newline_added"
                             % (task["task_id"], field, relative))
            normalized[str(relative)] = content
        task[field] = normalized
    return task, notes


def load_tasks(path: Path, helpers: types.ModuleType) -> tuple[list[dict[str, Any]], list[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise EvaluatorError("--tasks must contain a non-empty JSON list")
    tasks: list[dict[str, Any]] = []
    notes: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        task, task_notes = _normalized_task(item, index)
        if task["task_id"] in seen:
            raise EvaluatorError("duplicate task_id: %s" % task["task_id"])
        seen.add(task["task_id"])
        # The tested validator enforces canonical POSIX paths, NUL-free
        # newline-terminated text, gold ⊆ fixture, and digest round-tripping.
        helpers._validate_task_data(task)
        tasks.append(task)
        notes.extend(task_notes)
    return tasks, notes


def task_self_check(tasks: Sequence[Mapping[str, Any]],
                    helpers: types.ModuleType) -> list[dict[str, Any]]:
    """Prove every pristine fixture fails and every gold overlay passes."""
    receipts: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="normalized-batch-selfcheck-") as temp:
        root = Path(temp)
        for index, task in enumerate(tasks):
            baseline_dir = root / ("baseline-%d" % index)
            helpers.materialize_task(task, baseline_dir)
            baseline = helpers._run_hidden_grader(task, baseline_dir)
            if baseline.returncode == 0:
                raise EvaluatorError(
                    "baseline unexpectedly passes its hidden grader: %s"
                    % task["task_id"])
            gold_dir = root / ("gold-%d" % index)
            helpers.materialize_task(task, gold_dir, gold=True)
            gold = helpers._run_hidden_grader(task, gold_dir)
            if gold.returncode != 0:
                detail = (gold.stderr or gold.stdout or "")[-400:]
                raise EvaluatorError("gold fails for %s: %s"
                                     % (task["task_id"], detail))
            receipts.append({
                "task_id": task["task_id"],
                "task_sha256": helpers.task_sha256(task),
                "fixture_sha256": helpers.canonical_sha256(task["fixture_files"]),
                "gold_sha256": helpers.canonical_sha256(task["gold_files"]),
                "grader_sha256": _sha_text(str(task["hidden_grader"])),
                "prompt_sha256": _sha_text(str(task["prompt"])),
                "baseline_returncode": int(baseline.returncode),
                "baseline_fails": True,
                "gold_passes": True,
            })
    return receipts


# --------------------------------------------------------------------------
# Staging: current Collie harness + tested adapters + model rewrite
# --------------------------------------------------------------------------

_STAGE_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", "*.pyd", ".mypy_cache", ".pytest_cache",
    ".DS_Store")


def _tree_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            manifest[path.relative_to(root).as_posix()] = _sha_file(path)
    return manifest


def _apply_model_rewrite(path: Path) -> dict[str, Any]:
    before = path.read_text(encoding="utf-8")
    after = before
    counts: dict[str, int] = {}
    for old, new in MODEL_REWRITES:
        counts[old] = after.count(old)
        after = after.replace(old, new)
    if counts.get(LEGACY_MODEL, 0) < 1:
        raise EvaluatorError(
            "%s carries no %s constant to rewrite" % (path.name, LEGACY_MODEL))
    if LEGACY_MODEL in after:
        raise EvaluatorError("%s still references %s after rewrite"
                             % (path.name, LEGACY_MODEL))
    if MODEL not in after:
        raise EvaluatorError("%s does not reference %s after rewrite"
                             % (path.name, MODEL))
    path.write_text(after, encoding="utf-8", newline="")
    diff = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="a/" + path.name, tofile="b/" + path.name))
    return {
        "replacements": {key: value for key, value in counts.items() if value},
        "sha256_before": _sha_text(before),
        "sha256_after": _sha_text(after),
        "diff": diff,
    }


def build_stage(root: Path, collie_repo: Path, bench_repo: Path) -> dict[str, Any]:
    """Assemble the read-only ``/opt/collie`` overlay mounted into both images.

    Layout: ``harness/`` is the *current* Collie package; ``harness/
    subscription_sidecar.py`` is replaced by the tested sidecar; ``bench/``
    holds the three tested normalized adapter modules.  Only the frozen model
    constants are rewritten, and each rewrite is preserved as a unified diff.
    """
    stage_root = root / "stage"
    stage = stage_root / "opt-collie"
    patch_dir = root / "stage-patches"

    source_harness = collie_repo / "harness"
    if not source_harness.is_dir():
        raise EvaluatorError("current Collie harness package not found: %s"
                             % source_harness)

    build = Path(tempfile.mkdtemp(prefix="normalized-stage-", dir=str(root)))
    receipts: dict[str, Any] = {}
    try:
        target_harness = build / "harness"
        shutil.copytree(source_harness, target_harness, ignore=_STAGE_IGNORE)
        (build / "bench").mkdir()

        replaced = target_harness / "subscription_sidecar.py"
        current_sidecar_sha = _sha_file(replaced) if replaced.is_file() else ""
        tested_sidecar = bench_repo / STAGED_SIDECAR
        if not tested_sidecar.is_file():
            raise EvaluatorError("tested sidecar module is missing: %s"
                                 % tested_sidecar)
        shutil.copyfile(tested_sidecar, replaced)
        receipts["subscription_sidecar_source"] = {
            "from": "tested_bench_repo",
            "tested_sha256": _sha_file(tested_sidecar),
            "replaced_current_collie_sha256": current_sidecar_sha,
            "identical_to_current_collie":
                current_sidecar_sha == _sha_file(tested_sidecar),
        }

        for relative in STAGED_ADAPTERS:
            source = bench_repo / relative
            if not source.is_file():
                raise EvaluatorError("tested adapter module is missing: %s" % source)
            shutil.copyfile(source, build / relative)

        if COLLIE_SHELL_ABLATION:
            worker_file = build / 'bench/normalized_harness_worker.py'
            original_worker = worker_file.read_text(encoding='utf-8')
            needle = '"read_file", "write_file", "edit_file", "grep", "glob",'
            if original_worker.count(needle) != 1:
                raise EvaluatorError('Collie tool-profile patch no longer matches')
            worker_file.write_text(original_worker.replace(needle, needle + ' "bash",'), encoding='utf-8')
            receipts['collie_tool_profile'] = {
                'name': 'file-tools-plus-shell-ablation',
                'source_sha256': _sha_text(original_worker),
                'patched_sha256': _sha_file(worker_file),
                'change': 'Only add bash to retained tools; preserve prompt, loop, verification and budget settings.'}

        rewrites: dict[str, Any] = {}
        patches: dict[str, str] = {}
        for relative in MODEL_PATCH_FILES:
            receipt = _apply_model_rewrite(build / relative)
            patches[relative] = receipt.pop("diff")
            rewrites[relative] = receipt
        receipts["model_rewrite"] = {
            "from": LEGACY_MODEL, "to": MODEL, "files": rewrites}

        # The staged tree must not smuggle graders, gold files, or the frozen
        # suite into any container.
        for path in build.rglob("*"):
            if path.is_file() and path.name in FORBIDDEN_STAGE_NAMES:
                raise EvaluatorError("held-out material entered staging: %s"
                                     % path.name)

        constants = _module_constants(
            build / STAGED_SIDECAR, ["MODEL", "BEARER_SENTINEL"])
        if constants["MODEL"] != MODEL:
            raise EvaluatorError("staged sidecar MODEL is not %s" % MODEL)
        receipts["sidecar_constants"] = {
            "model": constants["MODEL"],
            "bearer_sentinel_sha256": _sha_text(str(constants["BEARER_SENTINEL"])),
        }
        receipts["bearer_sentinel"] = str(constants["BEARER_SENTINEL"])

        for relative in ("bench/normalized_prime_pi.py", "bench/normalized_hermes.py"):
            value = _module_constants(build / relative, ["MODEL"])["MODEL"]
            if value != MODEL:
                raise EvaluatorError("staged %s MODEL is not %s" % (relative, MODEL))

        manifest = _tree_manifest(build)
        receipts["file_sha256"] = manifest
        receipts["stage_sha256"] = _sha_bytes(_canonical_bytes(manifest))

        if stage.exists():
            existing = _sha_bytes(_canonical_bytes(_tree_manifest(stage)))
            if existing != receipts["stage_sha256"]:
                raise EvaluatorError(
                    "an existing --root stage differs from the freshly staged "
                    "tree; use a new --root instead of overwriting it")
            shutil.rmtree(build, ignore_errors=True)
        else:
            stage_root.mkdir(parents=True, exist_ok=True)
            os.replace(build, stage)
            for relative, diff in patches.items():
                _atomic_text(patch_dir / (relative.replace("/", "__") + ".patch"),
                             diff)
    finally:
        if build.exists():
            shutil.rmtree(build, ignore_errors=True)

    receipts["path"] = str(stage.resolve())
    receipts["mount_destination"] = "/opt/collie"
    return receipts


# --------------------------------------------------------------------------
# Docker plumbing
# --------------------------------------------------------------------------

def _mount(source: Path, destination: str, *, readonly: bool = False) -> str:
    resolved = str(source.resolve())
    if "," in resolved or "=" in resolved:
        raise EvaluatorError(
            "path %r cannot be expressed as a Docker --mount source" % resolved)
    value = "type=bind,src=%s,dst=%s" % (resolved, destination)
    return value + (",readonly" if readonly else "")


def _image_identity(tag: str) -> dict[str, Any]:
    raw = _run(["docker", "image", "inspect", tag], timeout=60)
    if raw.returncode:
        raise EvaluatorError(
            "required image %s is not present; this runner reuses the existing "
            "pinned tags and never rebuilds them" % tag)
    value = json.loads(raw.stdout)
    if not isinstance(value, list) or len(value) != 1:
        raise EvaluatorError("docker image inspect returned invalid evidence")
    entry = value[0]
    image_id = str(entry.get("Id") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise EvaluatorError("docker returned an invalid image id for %s" % tag)
    return {
        "tag": tag,
        "id": image_id,
        "repo_digests": entry.get("RepoDigests") or [],
        "created": str(entry.get("Created") or ""),
    }


_SIDECAR_PREFLIGHT = r'''
import importlib.metadata as m
from pathlib import Path
import json
import harness
import harness.subscription_sidecar as s
assert s.MODEL == "%(model)s", s.MODEL
assert not Path("/opt/collie/bench/subscription_rank_tasks.py").exists()
assert not Path("/opt/collie/bench/current_product_worker.py").exists()
print(json.dumps({
    "claude_agent_sdk": m.version("claude-agent-sdk"),
    "collie": harness.__version__,
    "sidecar_model": s.MODEL,
}, sort_keys=True))
'''

_HARNESS_PREFLIGHT = r'''
import json, os, subprocess, tempfile
from pathlib import Path
import harness
import harness.subscription_sidecar as s
import bench.normalized_harness_worker as w
import bench.normalized_prime_pi as pp
import bench.normalized_hermes as nh

assert s.MODEL == "%(model)s", s.MODEL
assert pp.MODEL == "%(model)s", pp.MODEL
assert nh.MODEL == "%(model)s", nh.MODEL
assert w.MODEL == "%(model)s", w.MODEL
assert not Path("/opt/collie/bench/subscription_rank_tasks.py").exists()
assert not Path("/opt/collie/bench/current_product_worker.py").exists()

# Prove the current Collie arm can actually be constructed offline with every
# knob the tested worker sets.  No model request is made; the network is none.
state = tempfile.mkdtemp(dir="/tmp")
os.environ["COLLIE_DATA_DIR"] = os.path.join(state, "collie-data")
from harness import cli as harness_cli
harness_cli.DATA = os.path.join(state, "collie-data")
work = tempfile.mkdtemp(dir="/tmp")
h = harness_cli.make_harness(work, provider="mock", project="preflight",
                             code_search=False, embed="hash")
# These two supported optional overrides are read with getattr in loop.py;
# the worker assigns them after construction, so constructor presence is not a gate.
h.force_ratio = 0.55
h.hard_ratio = 0.76
knobs = ["max_turns", "max_model_calls", "max_retries", "retry_base",
         "overflow_recovery", "self_verify", "force_edit", "force_ratio",
         "hard_ratio", "hooks", "provider", "registry", "composer"]
missing = [k for k in knobs if not hasattr(h, k)]
assert not missing, "current Collie harness lacks: %%s" %% missing
assert hasattr(h.registry, "retain")
for k in ["auto_prefetch", "include_project_rules", "include_skills", "identity"]:
    assert hasattr(h.composer, k), k
for r in (getattr(h, "memory", None), getattr(h, "recorder", None)):
    try:
        r.close()
    except Exception:
        pass

pi = json.loads(Path("/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/package.json").read_text())["version"]
prime = json.loads(Path("/opt/prime-agent/packages/coding-agent/package.json").read_text())["version"]
commit = subprocess.run(["git", "-C", "/opt/prime-agent", "rev-parse", "HEAD"],
                        check=True, capture_output=True, text=True).stdout.strip()
hermes = subprocess.run(["/opt/hermes-venv/bin/python", "-c",
                         "import importlib.metadata as m; print(m.version('hermes-agent'))"],
                        check=True, capture_output=True, text=True).stdout.strip()
print(json.dumps({"collie": harness.__version__, "pi": pi, "prime": prime,
                  "prime_commit": commit, "hermes": hermes}, sort_keys=True))
'''


def image_preflight(sidecar_image: str, harness_image: str,
                    stage: Path) -> dict[str, Any]:
    """Import-check both images with the staged tree mounted and no network."""
    mount = _mount(stage, "/opt/collie", readonly=True)
    sidecar_raw = _run([
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=67108864",
        "--mount", mount, "--entrypoint", "python", sidecar_image,
        "-c", _SIDECAR_PREFLIGHT % {"model": MODEL},
    ], timeout=180)
    if sidecar_raw.returncode:
        raise EvaluatorError("sidecar image preflight failed:\n%s"
                             % _scrub(sidecar_raw.stderr)[-1500:])
    sidecar = json.loads(sidecar_raw.stdout.strip().splitlines()[-1])
    if sidecar.get("claude_agent_sdk") != EXPECTED_IMAGE_PINS["claude_agent_sdk"]:
        raise EvaluatorError("unexpected Claude Agent SDK version in %s: %s"
                             % (sidecar_image, sidecar.get("claude_agent_sdk")))

    harness_raw = _run([
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=268435456",
        "--tmpfs", "/home/runner:rw,nosuid,size=67108864",
        "--mount", mount, "--entrypoint", "python3", harness_image,
        "-c", _HARNESS_PREFLIGHT % {"model": MODEL},
    ], timeout=300)
    if harness_raw.returncode:
        raise EvaluatorError("harness image preflight failed:\n%s"
                             % _scrub(harness_raw.stderr)[-3000:])
    versions = json.loads(harness_raw.stdout.strip().splitlines()[-1])
    for key in ("pi", "prime", "prime_commit", "hermes"):
        if versions.get(key) != EXPECTED_IMAGE_PINS[key]:
            raise EvaluatorError(
                "unexpected pinned %s in %s: %s (expected %s)"
                % (key, harness_image, versions.get(key), EXPECTED_IMAGE_PINS[key]))
    if versions.get("collie") != sidecar.get("collie"):
        raise EvaluatorError("staged Collie version differs between images")
    return {
        "sidecar": {
            "claude_agent_sdk_version": sidecar["claude_agent_sdk"],
            "staged_collie_version": sidecar["collie"],
            "staged_sidecar_model": sidecar["sidecar_model"],
            "held_out_material": "absent",
            "network": "none",
        },
        "harness": {
            **versions,
            "staged_collie_arm_constructible_offline": True,
            "held_out_material": "absent",
            "network": "none",
        },
    }


def _docker_inspect(name: str) -> dict[str, Any]:
    value = json.loads(_run(["docker", "inspect", name], timeout=45,
                            check=True).stdout)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise EvaluatorError("docker inspect returned invalid evidence")
    return value[0]


def _mount_map(inspect: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    mounts = inspect.get("Mounts")
    if not isinstance(mounts, list):
        raise EvaluatorError("docker mount evidence is missing")
    result: dict[str, Mapping[str, Any]] = {}
    for item in mounts:
        if not isinstance(item, Mapping):
            raise EvaluatorError("docker mount evidence is malformed")
        destination = str(item.get("Destination") or "")
        if not destination or destination in result:
            raise EvaluatorError("docker mount evidence is malformed")
        result[destination] = item
    return result


def _network_names(inspect: Mapping[str, Any]) -> set[str]:
    settings = inspect.get("NetworkSettings")
    networks = settings.get("Networks") if isinstance(settings, Mapping) else None
    if not isinstance(networks, Mapping):
        raise EvaluatorError("docker network evidence is missing")
    return {str(key) for key in networks}


def _attest_network(name: str, *, internal: bool) -> dict[str, Any]:
    value = json.loads(_run(["docker", "network", "inspect", name], timeout=45,
                            check=True).stdout)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], Mapping):
        raise EvaluatorError("docker network inspect returned invalid evidence")
    network = value[0]
    if (network.get("Name") != name or network.get("Driver") != "bridge"
            or network.get("Internal") is not internal):
        raise EvaluatorError("attempt network %s is not the expected bridge" % name)
    return {"driver": "bridge", "internal": internal, "attempt_scoped": True}


def _attest_sidecar(name: str, internal: str, egress: str) -> dict[str, Any]:
    evidence = _docker_inspect(name)
    if _network_names(evidence) != {internal, egress}:
        raise EvaluatorError("sidecar network attachment mismatch")
    mounts = _mount_map(evidence)
    expected = {"/home/runner/.claude/.credentials.json", "/ledger", "/opt/collie"}
    if set(mounts) != expected:
        raise EvaluatorError("sidecar mount isolation mismatch")
    if (mounts["/home/runner/.claude/.credentials.json"].get("RW") is not False
            or mounts["/opt/collie"].get("RW") is not False
            or mounts["/ledger"].get("RW") is not True):
        raise EvaluatorError("sidecar mount mode mismatch")
    host = evidence.get("HostConfig")
    bindings = host.get("PortBindings") if isinstance(host, Mapping) else None
    if bindings not in (None, {}):
        raise EvaluatorError("sidecar unexpectedly published a host port")
    tmpfs = host.get("Tmpfs") if isinstance(host, Mapping) else None
    options = tmpfs.get("/home/runner/.claude") if isinstance(tmpfs, Mapping) else None
    required = {"rw", "nosuid", "noexec", "uid=10001", "gid=10001", "mode=0700"}
    if not isinstance(options, str) or not required.issubset(set(options.split(","))):
        raise EvaluatorError("sidecar Claude home is not safely isolated")
    return {
        "networks": ["evaluator_internal", "evaluator_attempt_egress"],
        "host_ports_published": False,
        "mounts": ["claude_credential_read_only", "evaluator_ledger",
                   "staged_code_read_only"],
        "claude_home": "isolated_tmpfs_with_nested_read_only_credential",
    }


_FORBIDDEN_AGENT_ENV = (
    "ANTHROPIC_API_KEY=", "ANTHROPIC_OAUTH_TOKEN=", "CLAUDE_CODE_OAUTH_TOKEN=",
    "OPENAI_API_KEY=", "OPENROUTER_API_KEY=", "GEMINI_API_KEY=",
    "GOOGLE_API_KEY=", "AWS_SECRET_ACCESS_KEY=", "GH_TOKEN=", "GITHUB_TOKEN=",
)


def _attest_agent(name: str, internal: str) -> dict[str, Any]:
    evidence = _docker_inspect(name)
    if _network_names(evidence) != {internal}:
        raise EvaluatorError("agent network attachment mismatch")
    mounts = _mount_map(evidence)
    expected = {"/workspace", "/input", "/output", "/state", "/opt/collie"}
    if set(mounts) != expected:
        raise EvaluatorError("agent mount isolation mismatch")
    if any("credential" in item.lower() for item in mounts):
        raise EvaluatorError("agent received a credential mount")
    if (mounts["/input"].get("RW") is not False
            or mounts["/opt/collie"].get("RW") is not False
            or any(mounts[path].get("RW") is not True
                   for path in ("/workspace", "/output", "/state"))):
        raise EvaluatorError("agent mount mode isolation mismatch")
    config = evidence.get("Config")
    environment = config.get("Env") if isinstance(config, Mapping) else []
    if not isinstance(environment, list) or any(
            str(item).startswith(_FORBIDDEN_AGENT_ENV) for item in environment):
        raise EvaluatorError("agent received a provider credential environment variable")
    return {
        "networks": ["evaluator_internal"],
        "external_network": False,
        "credential_mount": False,
        "staged_code_mount_read_only": True,
        "workspace_input_output_state_mounts_only": True,
    }


def _remove_container(name: str) -> bool:
    _run(["docker", "rm", "--force", name], timeout=60)
    return _run(["docker", "inspect", name], timeout=30).returncode != 0


def _remove_network(name: str) -> bool:
    _run(["docker", "network", "rm", name], timeout=60)
    return _run(["docker", "network", "inspect", name], timeout=30).returncode != 0


def _container_logs(name: str) -> str:
    result = _run(["docker", "logs", "--tail", "2000", name], timeout=60)
    return _scrub((result.stdout or "") + (result.stderr or ""))


def _claude_credentials_path() -> Path:
    path = (Path.home() / ".claude" / ".credentials.json").resolve()
    if not path.is_file():
        raise EvaluatorError(
            "the operator's Claude login file (~/.claude/.credentials.json) is "
            "unavailable; the evaluator-owned sidecar needs it read-only")
    return path


# --------------------------------------------------------------------------
# Sidecar ledger validation (physical model requests)
# --------------------------------------------------------------------------

def validate_ledger(directory: Path, budget: int,
                    max_transport_errors: int) -> dict[str, Any]:
    """Validate every physical request receipt; return a prompt-free summary."""
    if not directory.is_dir():
        raise EvaluatorError("sidecar ledger directory is missing")
    entries = sorted(directory.iterdir())
    if not entries or any(not path.is_file() or path.suffix != ".json"
                          for path in entries):
        raise EvaluatorError("sidecar ledger is empty or holds partial evidence")
    rows = [_load_json_object(path) for path in entries]

    reserved: dict[str, int] = {}
    settled: dict[str, int] = {}
    budget_rejected: set[str] = set()
    outcomes: dict[str, int] = {}
    error_codes: dict[str, int] = {}
    usage = {key: 0 for key in ("input_tokens", "output_tokens",
                                "cache_read_input_tokens",
                                "cache_creation_input_tokens")}
    for index, row in enumerate(rows):
        event = row.get("event")
        request_id = row.get("request_id")
        if (not isinstance(request_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", request_id)
                or row.get("model") != MODEL):
            raise EvaluatorError("sidecar request identity or model mismatch")
        if event == "reserved":
            allowed = {"schema_version", "event", "request_id", "created_at_utc",
                       "model", "request_sha256", "prompt_sha256", "request_bytes"}
            if set(row) != allowed or request_id in reserved:
                raise EvaluatorError("sidecar reservation is malformed or duplicated")
            if (not re.fullmatch(r"[0-9a-f]{64}", str(row.get("request_sha256")))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("prompt_sha256")))
                    or not isinstance(row.get("request_bytes"), int)
                    or isinstance(row.get("request_bytes"), bool)
                    or int(row["request_bytes"]) <= 0):
                raise EvaluatorError("sidecar reservation fields are invalid")
            reserved[request_id] = index
        elif event == "settled":
            allowed = {"schema_version", "event", "request_id", "created_at_utc",
                       "model", "outcome", "duration_ms", "usage"}
            if "error_code" in row:
                allowed.add("error_code")
            if set(row) != allowed or request_id in settled:
                raise EvaluatorError("sidecar settlement is malformed or duplicated")
            outcome = str(row.get("outcome") or "")
            if outcome not in {"completed", "error", "cancelled", "timeout"}:
                raise EvaluatorError("sidecar settlement outcome is invalid")
            duration = row.get("duration_ms")
            if (not isinstance(duration, int) or isinstance(duration, bool)
                    or duration < 0):
                raise EvaluatorError("sidecar settlement duration is invalid")
            values = row.get("usage")
            if not isinstance(values, dict) or set(values) != set(usage):
                raise EvaluatorError("sidecar settlement usage is missing")
            for key, value in values.items():
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise EvaluatorError("sidecar settlement usage is invalid")
                usage[key] += value
            settled[request_id] = index
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            code = _safe_code(row.get("error_code"), "")
            if code:
                error_codes[code] = error_codes.get(code, 0) + 1
        elif event == "budget_exhausted":
            allowed = {"schema_version", "event", "request_id", "created_at_utc",
                       "model", "max_requests"}
            if (set(row) != allowed or row.get("max_requests") != budget
                    or request_id in budget_rejected
                    or request_id in reserved or request_id in settled):
                raise EvaluatorError("sidecar budget receipt is malformed")
            budget_rejected.add(request_id)
            outcomes["budget_exhausted"] = outcomes.get("budget_exhausted", 0) + 1
        else:
            raise EvaluatorError("sidecar ledger contains an unknown event")

    if not reserved or set(reserved) != set(settled):
        raise EvaluatorError("not every physical request was reserved and settled")
    if any(reserved[key] >= settled[key] for key in reserved):
        raise EvaluatorError("a sidecar settlement preceded its reservation")
    if len(reserved) > budget:
        raise EvaluatorError("sidecar physical-request budget was exceeded")
    completed = outcomes.get("completed", 0)
    if completed < 1 or sum(usage.values()) < 1:
        raise EvaluatorError("sidecar has no completed request with usage evidence")
    failed = len(reserved) - completed
    if failed > max_transport_errors:
        raise EvaluatorError(
            "sidecar recorded %d non-completed physical requests (limit %d)"
            % (failed, max_transport_errors))
    stops = outcomes.get("budget_exhausted", 0)
    if stops and len(reserved) != budget:
        raise EvaluatorError("sidecar budget-stop evidence is inconsistent")
    return {
        "schema_version": 1,
        "model": MODEL,
        "physical_requests": len(reserved),
        "reserved_requests": len(reserved),
        "settled_requests": len(settled),
        "completed_requests": completed,
        "failed_requests": failed,
        "budget": budget,
        "budget_stop_observed": bool(stops),
        "outcomes": dict(sorted(outcomes.items())),
        "settlement_error_codes": dict(sorted(error_codes.items())),
        "usage": usage,
        "ledger_sha256": _sha_bytes(_canonical_bytes(rows)),
    }


def validate_worker_receipt(worker: Mapping[str, Any], expected: Mapping[str, Any],
                            arm: str) -> dict[str, Any]:
    if worker.get("worker_outcome") not in {"candidate", "product_failure"}:
        raise EvaluatorError("agent worker did not produce a scoreable receipt")
    bindings = {
        "run_id": expected["run_id"], "task_id": expected["task_id"],
        "arm": arm, "model": MODEL,
        "delivered_prompt_sha256": expected["delivered_prompt_sha256"],
    }
    if any(worker.get(key) != value for key, value in bindings.items()):
        raise EvaluatorError("agent worker receipt binding mismatch")
    runtime = worker.get("runtime")
    if (not isinstance(runtime, dict) or not runtime.get("product")
            or runtime.get("model") != MODEL):
        raise EvaluatorError("agent runtime evidence is missing")
    if not isinstance(worker.get("usage"), dict):
        raise EvaluatorError("agent-reported usage is malformed")
    if not isinstance(worker.get("patch"), str):
        raise EvaluatorError("worker patch evidence is malformed")
    duration = worker.get("duration_ms")
    if (not isinstance(duration, (int, float)) or isinstance(duration, bool)
            or duration < 0):
        raise EvaluatorError("agent duration evidence is missing")
    evidence = worker.get("tool_evidence")
    if not isinstance(evidence, dict):
        raise EvaluatorError("agent terminal evidence is missing")
    terminal = evidence.get("terminal_observed") is True
    if worker.get("worker_outcome") == "candidate" and not terminal:
        raise EvaluatorError("agent terminal evidence is missing")
    result: dict[str, Any] = {"terminal_observed": terminal}
    for key in ("native_tool_calls", "native_edit_calls"):
        value = evidence.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise EvaluatorError("agent native-tool evidence is malformed")
        result[key] = value
    return result


# --------------------------------------------------------------------------
# Evaluator-side workspace handling and hidden grading
# --------------------------------------------------------------------------

def _prepare_git_fixture(task: Mapping[str, Any], workspace: Path,
                         helpers: types.ModuleType) -> tuple[str, str]:
    helpers.materialize_task(task, workspace)
    for arguments in (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "normalized-batch@collie.run"],
        ["git", "config", "user.name", "Collie Normalized Batch"],
        ["git", "add", "-A"],
        ["git", "commit", "--quiet", "-m", "frozen baseline"],
    ):
        _run(arguments, cwd=workspace, timeout=120, check=True)
    commit = _run(["git", "rev-parse", "HEAD"], cwd=workspace, check=True).stdout.strip()
    tree = _run(["git", "rev-parse", "HEAD^{tree}"], cwd=workspace,
                check=True).stdout.strip()
    return commit, tree


def external_patch(workspace: Path, baseline: Path) -> str:
    """Reconstruct the candidate diff outside the container.

    The agent may mutate its own ``.git`` freely; this receipt is rebuilt from a
    pristine evaluator-owned snapshot, so it cannot be forged from inside.
    """
    if not baseline.is_dir():
        raise EvaluatorError("evaluator baseline snapshot is missing")
    comparison = Path(tempfile.mkdtemp(prefix="normalized-batch-patch-"))
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
            ["git", "config", "user.email", "normalized-batch@collie.run"],
            ["git", "config", "user.name", "Collie Normalized Batch"],
            ["git", "add", "-A"],
            ["git", "commit", "--quiet", "-m", "pristine evaluator baseline"],
        ):
            _run(arguments, cwd=repository, timeout=120, check=True)
        for child in repository.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        # Stage the emptied worktree first: otherwise a same-size file rewritten
        # inside the filesystem timestamp granularity can match Git's cached
        # stat tuple and be skipped without rehashing.
        _run(["git", "add", "-A"], cwd=repository, timeout=120, check=True)
        for child in candidate.iterdir():
            target = repository / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
        _run(["git", "add", "-A"], cwd=repository, timeout=120, check=True)
        patch = _run(["git", "diff", "--binary", "--cached", "HEAD", "--"],
                     cwd=repository, timeout=120, check=True).stdout
        if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
            raise EvaluatorError("candidate patch exceeded the evaluator limit")
        return patch
    finally:
        shutil.rmtree(comparison, ignore_errors=True)


def grade(task: Mapping[str, Any], workspace: Path, patch_sha: str,
          helpers: types.ModuleType) -> dict[str, Any]:
    """Run the host-held hidden grader against the agent's workspace."""
    grader_root = Path(tempfile.mkdtemp(prefix="normalized-batch-grader-"))
    try:
        grader = grader_root / "grader.py"
        marker = grader_root / "success.marker"
        nonce = uuid.uuid4().hex
        wrapper = "import sys\nsys.path.insert(0, %r)\n" % str(workspace)
        suffix = ("\nfrom pathlib import Path as _EvaluatorPath\n"
                  "_EvaluatorPath(%r).write_text(%r, encoding='utf-8')\n"
                  % (str(marker), nonce))
        # Trusted authored graders end with sys.exit(main()). Let that normal
        # success reach our marker, while an early exit from imported candidate
        # code still cannot create it. Preserve nonzero exit semantics.
        grader_body = str(task["hidden_grader"])
        grader_body = grader_body.replace(
            '    sys.exit(main())',
            '    _trusted_grader_exit = main()\n'
            '    if _trusted_grader_exit not in (None, 0):\n'
            '        raise SystemExit(_trusted_grader_exit)')
        grader.write_text(wrapper + grader_body + suffix,
                          encoding="utf-8", newline="\n")
        try:
            result = _run([sys.executable, "-I", str(grader)], cwd=workspace,
                          timeout=60)
            marker_ok = marker.is_file() and marker.read_text(encoding="utf-8") == nonce
            resolved = result.returncode == 0 and marker_ok
            returncode: int | None = result.returncode
            detail = "" if resolved else (
                "hidden_contract_failed" if result.returncode in (0, 1)
                else "candidate_process_failed")
        except subprocess.TimeoutExpired:
            resolved = False
            returncode = None
            detail = "candidate_grader_timeout"
        return {
            "format": "collie-normalized-batch-grader-v1",
            "outcome": "graded",
            "resolved": resolved,
            "returncode": returncode,
            "success_marker_verified": resolved,
            "failure_detail": detail,
            "task_sha256": helpers.task_sha256(task),
            "fixture_sha256": helpers.canonical_sha256(task["fixture_files"]),
            "grader_sha256": _sha_text(str(task["hidden_grader"])),
            "patch_sha256": patch_sha,
            "graded_at_utc": _utc_now(),
        }
    finally:
        shutil.rmtree(grader_root, ignore_errors=True)


# --------------------------------------------------------------------------
# Plan: randomized, balanced arm order
# --------------------------------------------------------------------------

def _latin_square(rng: random.Random) -> list[tuple[str, ...]]:
    """Four permutations in which each arm occupies each position exactly once."""
    base = list(ARMS)
    rng.shuffle(base)
    rows = [tuple(base[shift:] + base[:shift]) for shift in range(len(base))]
    rng.shuffle(rows)
    return rows


def build_plan(tasks: Sequence[Mapping[str, Any]], repetitions: int, seed: int,
               helpers: types.ModuleType) -> list[dict[str, Any]]:
    """Every (task, repetition) cell runs all four arms in a randomized order.

    Arm counts are therefore exactly balanced by construction, and consecutive
    blocks of four cells form a Latin square so each arm also occupies each
    launch position equally often.
    """
    if repetitions < 1:
        raise EvaluatorError("--repetitions must be a positive integer")
    rng = random.Random(seed)
    cells = [(task, repetition) for task in tasks
             for repetition in range(1, repetitions + 1)]
    orders: list[tuple[str, ...]] = []
    while len(orders) < len(cells):
        orders.extend(_latin_square(rng))
    orders = orders[:len(cells)]

    plan: list[dict[str, Any]] = []
    for (task, repetition), order in zip(cells, orders):
        for position, arm in enumerate(order, 1):
            slot = len(plan) + 1
            plan.append({
                "slot": slot,
                "run_id": "batch-%04d-%s-r%d-p%d-%s" % (
                    slot, _slug(task["task_id"]), repetition, position, arm),
                "task_id": task["task_id"],
                "task_sha256": helpers.task_sha256(task),
                "repetition": repetition,
                "position": position,
                "arm": arm,
                "attempt": 1,
                "phase": "ranking",
            })
    counts = {arm: sum(row["arm"] == arm for row in plan) for arm in ARMS}
    if len(set(counts.values())) != 1:
        raise EvaluatorError("plan is not arm-balanced: %s" % counts)
    return plan


# --------------------------------------------------------------------------
# One attempt
# --------------------------------------------------------------------------

class Suite:
    """Immutable per-suite configuration shared by every concurrent attempt."""

    def __init__(self, *, args: argparse.Namespace, stage: Path,
                 helpers: types.ModuleType, tasks: Mapping[str, Any],
                 prompt_prefix: str, bearer: str, credential: Path,
                 result_root: Path, temp_root: Path, suite_sha: str) -> None:
        self.args = args
        self.stage = stage
        self.helpers = helpers
        self.tasks = tasks
        self.prompt_prefix = prompt_prefix
        self.bearer = bearer
        self.credential = credential
        self.result_root = result_root
        self.temp_root = temp_root
        self.suite_sha = suite_sha
        self.endpoint = "http://inference:8765/v1"
        self.stop = threading.Event()
        self.stop_reason = ""
        self._write_lock = threading.Lock()
        self._print_lock = threading.Lock()

    def note(self, message: str) -> None:
        with self._print_lock:
            print(message, flush=True)

    def append_row(self, row: Mapping[str, Any]) -> None:
        payload = _canonical_bytes(row) + b"\n"
        with self._write_lock:
            path = self.result_root / "attempts.jsonl"
            with path.open("ab") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())

    def halt(self, reason: str) -> None:
        if not self.stop.is_set():
            self.stop_reason = reason
            self.stop.set()


def _sidecar_command(suite: Suite, name: str, network: str,
                     ledger: Path) -> list[str]:
    args = suite.args
    return [
        "docker", "run", "--detach", "--name", name, "--init",
        "--network", network, "--network-alias", "inference",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", args.sidecar_memory, "--cpus", str(args.sidecar_cpus),
        "--pids-limit", "192",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=268435456",
        "--tmpfs",
        "/home/runner/.claude:rw,nosuid,noexec,uid=10001,gid=10001,mode=0700,size=67108864",
        "--mount", _mount(suite.credential,
                          "/home/runner/.claude/.credentials.json", readonly=True),
        "--mount", _mount(ledger, "/ledger"),
        "--mount", _mount(suite.stage, "/opt/collie", readonly=True),
        args.sidecar_image,
        "--bind", "0.0.0.0", "--port", "8765",
        "--ledger-dir", "/ledger",
        "--timeout", str(args.request_seconds),
        "--max-requests", str(args.requests_per_attempt),
        "--allow-private-peers",
    ]


def _agent_create_command(suite: Suite, name: str, network: str, workspace: Path,
                          input_dir: Path, output_dir: Path,
                          state_dir: Path, arm: str) -> list[str]:
    args = suite.args
    return [
        "docker", "create", "--name", name, "--init", "--network", network,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--memory", args.agent_memory, "--cpus", str(args.agent_cpus),
        "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=268435456",
        "--tmpfs", "/home/runner:rw,nosuid,size=268435456",
        "--env", "HOME=/home/runner", "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--mount", _mount(workspace, "/workspace"),
        "--mount", _mount(input_dir, "/input", readonly=True),
        "--mount", _mount(output_dir, "/output"),
        "--mount", _mount(state_dir, "/state"),
        "--mount", _mount(suite.stage, "/opt/collie", readonly=True),
        args.harness_image,
        "--arm", arm, "--task-json", "/input/task.json",
        "--workspace", "/workspace", "--run-dir", "/output",
        "--state-dir", "/state", "--output", "/output/worker.json",
        "--endpoint", suite.endpoint,
        "--max-turns", str(args.requests_per_attempt),
    ]


def _wait_sidecar(name: str, timeout: float = 45.0) -> None:
    script = (
        "import json,urllib.request\n"
        "v=json.load(urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=2))\n"
        "assert v=={'status':'ok','model':'%s'}, v\n" % MODEL
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _run(["docker", "exec", name, "python", "-c", script],
                timeout=15).returncode == 0:
            return
        time.sleep(0.25)
    raise EvaluatorError("sidecar health check did not become ready")


def _classify_invalid(error_code: str) -> str:
    if error_code in EVALUATOR_INFRA_ERRORS:
        return "invalid_infrastructure"
    if error_code in WALL_TIMEOUT_ERRORS:
        return "invalid_wall_timeout"
    return "invalid_adapter"


def run_attempt(suite: Suite, row: Mapping[str, Any]) -> dict[str, Any]:
    args = suite.args
    arm = str(row["arm"])
    run_id = str(row["run_id"])
    task = suite.tasks[str(row["task_id"])]
    helpers = suite.helpers

    run_dir = suite.result_root / "runs" / run_id
    raw_dir = run_dir / "raw"
    ledger_dir = suite.result_root / "evaluator-ledgers" / run_id
    # exist_ok=False: a prior attempt's artifacts are never overwritten.
    run_dir.mkdir(parents=True, exist_ok=False)
    raw_dir.mkdir(parents=True, exist_ok=False)
    ledger_dir.mkdir(parents=True, exist_ok=False)

    _atomic_json(run_dir / "reservation.json", {
        **row, "schema_version": 1, "suite_sha256": suite.suite_sha,
        "reserved_at_utc": _utc_now(), "state": "reserved",
    })
    reservation_sha = _sha_file(run_dir / "reservation.json")

    root = suite.temp_root / run_id
    workspace, baseline = root / "workspace", root / "baseline"
    input_dir, state_dir = root / "input", root / "state"
    for path in (workspace, input_dir, state_dir):
        path.mkdir(parents=True, exist_ok=False)

    baseline_commit, baseline_tree = _prepare_git_fixture(task, workspace, helpers)
    helpers.materialize_task(task, baseline)

    prompt = suite.prompt_prefix + str(task["prompt"])
    worker_input: dict[str, Any] = {
        **row,
        "schema_version": 1,
        "prompt": task["prompt"],
        "delivered_prompt": prompt,
        "delivered_prompt_sha256": _sha_text(prompt),
        "model": MODEL,
        "wall_seconds": args.attempt_seconds,
        "endpoint": suite.endpoint,
        "sidecar_bearer": suite.bearer,
    }
    _atomic_json(input_dir / "task.json", worker_input)

    suffix = _sha_text(suite.suite_sha + run_id)[:12]
    internal = "cnb-net-" + suffix
    egress = "cnb-egress-" + suffix
    sidecar_name = "cnb-inference-" + suffix
    agent_name = "cnb-agent-" + suffix

    internal_made = egress_made = sidecar_made = agent_made = False
    agent_exit: int | None = None
    timed_out = False
    orchestration_error = ""
    cleanup_ok = True
    network_evidence: dict[str, Any] = {}
    started = time.monotonic()
    try:
        _run(["docker", "network", "create", "--driver", "bridge", "--internal",
              internal], timeout=60, check=True)
        internal_made = True
        network_evidence["attempt_network"] = _attest_network(internal, internal=True)

        _run(["docker", "network", "create", "--driver", "bridge", egress],
             timeout=60, check=True)
        egress_made = True
        network_evidence["attempt_egress"] = _attest_network(egress, internal=False)

        _run(_sidecar_command(suite, sidecar_name, internal, ledger_dir),
             timeout=90, check=True)
        sidecar_made = True
        _run(["docker", "network", "connect", egress, sidecar_name],
             timeout=60, check=True)
        network_evidence["sidecar"] = _attest_sidecar(sidecar_name, internal, egress)
        _wait_sidecar(sidecar_name)

        _run(_agent_create_command(suite, agent_name, internal, workspace,
                                   input_dir, run_dir, state_dir, arm),
             timeout=90, check=True)
        agent_made = True
        _run(["docker", "start", agent_name], timeout=60, check=True)
        network_evidence["agent"] = _attest_agent(agent_name, internal)

        remaining = max(1.0, args.attempt_seconds - (time.monotonic() - started))
        waited = _run(["docker", "wait", agent_name], timeout=remaining, check=True)
        try:
            agent_exit = int(waited.stdout.strip())
        except ValueError as exc:
            raise EvaluatorError("docker wait returned an invalid exit code") from exc

        sidecar_state = _docker_inspect(sidecar_name).get("State")
        if (not isinstance(sidecar_state, Mapping)
                or sidecar_state.get("Running") is not True):
            raise EvaluatorError("sidecar exited before attempt completion")
    except subprocess.TimeoutExpired:
        timed_out = True
        orchestration_error = "outer_wall_timeout"
    except Exception as exc:  # noqa: BLE001 - reduced to a safe code below
        orchestration_error = "attempt_orchestration_failure"
        _atomic_text(raw_dir / "orchestration-error.txt",
                     _scrub("%s: %s" % (type(exc).__name__, exc)))
    finally:
        for made, name, leaf in ((agent_made, agent_name, "agent.log"),
                                 (sidecar_made, sidecar_name, "sidecar.log")):
            if not made:
                continue
            try:
                _atomic_text(raw_dir / leaf, _container_logs(name))
            except Exception:  # noqa: BLE001 - a log capture must never block cleanup
                pass
        if agent_made:
            cleanup_ok = _remove_container(agent_name) and cleanup_ok
        if sidecar_made:
            cleanup_ok = _remove_container(sidecar_name) and cleanup_ok
        if internal_made:
            cleanup_ok = _remove_network(internal) and cleanup_ok
        if egress_made:
            cleanup_ok = _remove_network(egress) and cleanup_ok

    worker: dict[str, Any] = {}
    worker_path = run_dir / "worker.json"
    if worker_path.is_file():
        try:
            worker = _load_json_object(worker_path)
        except Exception:  # noqa: BLE001 - a malformed receipt is an invalid attempt
            worker = {}

    try:
        patch = external_patch(workspace, baseline)
    except Exception:  # noqa: BLE001
        patch = ""
        if not orchestration_error:
            orchestration_error = "evaluator_patch_collection_failure"
    _atomic_text(run_dir / "patch.diff", patch)
    patch_sha = _sha_text(patch)

    ledger_summary: dict[str, Any] = {}
    ledger_error = ""
    try:
        ledger_summary = validate_ledger(
            ledger_dir, args.requests_per_attempt, args.max_transport_errors)
    except Exception as exc:  # noqa: BLE001
        ledger_error = "sidecar_ledger_invalid"
        _atomic_text(raw_dir / "ledger-error.txt", _scrub(str(exc)))

    tool_evidence: dict[str, Any] = {}
    receipt_error = ""
    try:
        tool_evidence = validate_worker_receipt(worker, worker_input, arm)
    except Exception:  # noqa: BLE001
        receipt_error = _safe_code(worker.get("error_code"), "agent_worker_invalid")

    grader: dict[str, Any] = {"outcome": "not_run", "resolved": None,
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
    elif receipt_error:
        error_code = receipt_error
        status = _classify_invalid(error_code)
    elif ((worker.get("worker_outcome") == "candidate" and agent_exit != 0)
          or (worker.get("worker_outcome") == "product_failure" and agent_exit != 2)):
        error_code = "agent_container_exit"
    else:
        # Transport, receipt, exit, and isolation evidence are all valid, so the
        # evaluator grades the resulting workspace regardless of how the harness
        # described its own terminal state: an arm may report a final model or
        # tool error after already writing a correct fix.
        grader = grade(task, workspace, patch_sha, helpers)
        status = ("valid_resolved" if grader.get("resolved") is True
                  else "valid_unresolved")
        error_code = ("" if status == "valid_resolved" else
                      _safe_code(grader.get("failure_detail")
                                 or worker.get("error_code"),
                                 "hidden_contract_failed"))

    _atomic_json(run_dir / "grader.json", grader)

    terminal = {
        **row,
        "schema_version": 1,
        "suite_sha256": suite.suite_sha,
        "status": status,
        "resolved": status == "valid_resolved",
        "error_code": error_code,
        "worker_outcome": worker.get("worker_outcome"),
        "worker_error_code": _safe_code(worker.get("error_code"), ""),
        "agent_exit_code": agent_exit,
        "reservation_sha256": reservation_sha,
        "baseline_commit": baseline_commit,
        "baseline_tree": baseline_tree,
        "delivered_prompt_sha256": worker_input["delivered_prompt_sha256"],
        "patch_sha256": patch_sha,
        "patch_bytes": len(patch.encode("utf-8")),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "worker_duration_ms": worker.get("duration_ms"),
        "usage": ledger_summary.get("usage", {}),
        "reported_usage": (worker.get("usage")
                           if isinstance(worker.get("usage"), dict) else {}),
        "worker_patch_sha256": _sha_text(str(worker.get("patch") or "")),
        "runtime": (worker.get("runtime")
                    if isinstance(worker.get("runtime"), dict) else {}),
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


def _not_attempted(row: Mapping[str, Any], suite_sha: str,
                   reason: str) -> dict[str, Any]:
    return {
        **row,
        "schema_version": 1,
        "suite_sha256": suite_sha,
        "status": "not_attempted",
        "resolved": False,
        "error_code": reason,
        "completed_at_utc": _utc_now(),
    }


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

def summarize(plan: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]],
              suite_sha: str, *, stop_reason: str) -> dict[str, Any]:
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
                    "arm", "attempt"):
            if row.get(key) != planned.get(key):
                errors.append({"run_id": run_id, "error": key + "_mismatch"})
        if row.get("suite_sha256") != suite_sha:
            errors.append({"run_id": run_id, "error": "suite_mismatch"})
    for missing in sorted(set(expected) - seen):
        errors.append({"run_id": missing, "error": "missing_run"})

    scores: dict[str, Any] = {}
    for arm in ARMS:
        selected = [row for row in rows if row.get("arm") == arm]
        buckets = {status: sum(row.get("status") == status for row in selected)
                   for status in STATUSES}
        valid = buckets["valid_resolved"] + buckets["valid_unresolved"]
        durations = [float(row["duration_ms"]) for row in selected
                     if isinstance(row.get("duration_ms"), (int, float))
                     and row.get("status") in ("valid_resolved", "valid_unresolved")]
        requests = [int(row.get("sidecar_request_evidence", {}).get(
            "physical_requests", 0)) for row in selected
            if isinstance(row.get("sidecar_request_evidence"), dict)]
        scores[arm] = {
            "planned": sum(row["arm"] == arm for row in plan),
            "attempts_recorded": len(selected),
            **buckets,
            "valid_attempts": valid,
            "resolved": buckets["valid_resolved"],
            "solve_rate_over_valid": (buckets["valid_resolved"] / valid
                                      if valid else None),
            "median_valid_duration_ms": (statistics.median(durations)
                                         if durations else None),
            "total_physical_requests": sum(requests),
        }

    invalid_total = sum(scores[arm]["invalid_adapter"]
                        + scores[arm]["invalid_wall_timeout"]
                        + scores[arm]["invalid_infrastructure"]
                        + scores[arm]["not_attempted"] for arm in ARMS)
    valid_counts = {scores[arm]["valid_attempts"] for arm in ARMS}
    withheld_reason = None
    if errors:
        withheld_reason = "validation_errors"
    elif invalid_total:
        withheld_reason = "invalid_or_unattempted_cells_present"
    elif len(valid_counts) != 1 or 0 in valid_counts:
        withheld_reason = "unequal_valid_attempts_per_arm"

    ranking = None
    if withheld_reason is None:
        ranking = sorted(
            ({"arm": arm, "score": scores[arm]["solve_rate_over_valid"]}
             for arm in ARMS),
            key=lambda item: (-float(item["score"]), item["arm"]))
        for item in ranking:
            item["rank"] = 1 + sum(float(other["score"]) > float(item["score"])
                                   for other in ranking)

    return {
        "schema_version": 1,
        "suite_id": SUITE_ID,
        "suite_sha256": suite_sha,
        "claim": CLAIM,
        "scope": "exploratory",
        "publishable": False,
        "comparison_label": COMPARISON_LABEL,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "stopped_early_reason": stop_reason or None,
        "validation_errors": errors,
        "scores": scores,
        "ranking": ranking,
        "ranking_withheld": ranking is None,
        "ranking_withheld_reason": withheld_reason,
        "invalid_accounting": {
            "invalid_adapter_is_reported_separately_from_unresolved": True,
            "invalid_adapter": {arm: scores[arm]["invalid_adapter"] for arm in ARMS},
            "invalid_wall_timeout": {arm: scores[arm]["invalid_wall_timeout"]
                                     for arm in ARMS},
            "invalid_infrastructure": {arm: scores[arm]["invalid_infrastructure"]
                                       for arm in ARMS},
            "not_attempted": {arm: scores[arm]["not_attempted"] for arm in ARMS},
        },
        "limitations": [
            "this is an adapted loop comparison, not a native product ranking",
            "all four harnesses are adapted to one evaluator-owned compatibility sidecar",
            "system prompts, loop policies, context handling, and local tool surfaces differ",
            "only the evaluator-owned user message is byte-identical; model-visible prompts differ",
            "native tool and edit counts are diagnostic and not comparable across harnesses",
            "Prime, Pi, and Hermes run at their image pins while Collie runs at current source",
            "subscription quota consumption is not a metered billing receipt",
            "a small local task set cannot establish a general capability claim",
        ],
        "generated_at_utc": _utc_now(),
    }


# --------------------------------------------------------------------------
# Manifest and orchestration
# --------------------------------------------------------------------------

def build_manifest(args: argparse.Namespace, *, stage: Mapping[str, Any],
                   images: Mapping[str, Any], preflight: Mapping[str, Any],
                   task_receipts: Sequence[Mapping[str, Any]],
                   task_notes: Sequence[str], plan: Sequence[Mapping[str, Any]],
                   guard: Mapping[str, Any], prompt_prefix: str,
                   collie_revision: str, bench_revision: str,
                   collie_dirty: bool, bench_dirty: bool) -> dict[str, Any]:
    staged = dict(stage)
    staged.pop("bearer_sentinel", None)
    return {
        "schema_version": 1,
        "suite_id": SUITE_ID,
        "claim": CLAIM,
        "scope": "exploratory",
        "publishable": False,
        "comparison_label": COMPARISON_LABEL,
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "arms": {
            arm: {
                "harness": arm,
                "model": MODEL,
                "transport": "same evaluator-owned Claude Agent SDK sidecar",
                "agent_loop_system_prompt_and_local_tools": "harness_owned",
                "deployment": "adapted_not_product_default",
                "source_pin": ("current_collie_worktree" if arm == "collie"
                               else "pinned_benchmark_image"),
            } for arm in ARMS
        },
        "pins": {
            "current_collie": {
                "repo": str(args.collie_repo.resolve()),
                "revision": collie_revision,
                "expected_revision": args.expect_collie_commit,
                "worktree_dirty": collie_dirty,
                "staged_version": preflight["harness"].get("collie"),
                "role": "arm_under_test_and_sidecar_sdk_transport_host",
            },
            "tested_bench_repo": {
                "repo": str(args.bench_repo.resolve()),
                "revision": bench_revision,
                "worktree_dirty": bench_dirty,
                "role": "source_of_the_three_normalized_adapters_and_the_sidecar",
            },
            "images": dict(images),
            "image_runtime_versions": {
                "pi": preflight["harness"].get("pi"),
                "prime": preflight["harness"].get("prime"),
                "prime_commit": preflight["harness"].get("prime_commit"),
                "hermes": preflight["harness"].get("hermes"),
                "claude_agent_sdk": preflight["sidecar"].get("claude_agent_sdk_version"),
            },
            "expected_image_runtime_versions": dict(EXPECTED_IMAGE_PINS),
            "note": ("Prime/Pi/Hermes and the Claude Agent SDK are the versions "
                     "baked into the pre-existing image tags; only the Collie "
                     "source tree is replaced by the staged mount."),
        },
        "stage": staged,
        "image_preflight": dict(preflight),
        "tasks": list(task_receipts),
        "task_normalizations": list(task_notes),
        "task_source": str(args.tasks.resolve()),
        "delivered_prompt_prefix_sha256": _sha_text(prompt_prefix),
        "prompt_contract": "byte_identical_evaluator_owned_user_message_per_task",
        "transport": {
            "surface": "OpenAI-compatible internal sidecar backed by the official Claude Agent SDK",
            "one_sdk_turn_per_physical_sidecar_request": True,
            "claude_p_invoked": False,
            "raw_oauth_inference_transport": False,
            "api_key_fallback_disabled": True,
            "request_ledger": "evaluator_owned_reserved_and_settled_receipts",
        },
        "budgets": {
            "physical_model_request_budget_per_attempt": args.requests_per_attempt,
            "attempt_wall_seconds": args.attempt_seconds,
            "sidecar_request_seconds": args.request_seconds,
            "max_non_completed_physical_requests_per_attempt": args.max_transport_errors,
        },
        "schedule": {
            "repetitions_per_task_arm": args.repetitions,
            "arm_order": "randomized_balanced_latin_square_per_four_cells",
            "seed": args.seed,
            "concurrency_limit_this_process": args.concurrency,
            "total_concurrency_owner": "parent_coordinator",
            "deadline_utc": args.deadline_utc,
            "plan": list(plan),
        },
        "network": {
            "per_attempt_evaluator_owned_internal_network": True,
            "per_attempt_evaluator_owned_egress_network": True,
            "sidecar_also_connected_to_attempt_egress": True,
            "agent_external_network": False,
            "host_port_published": False,
        },
        "credential_isolation": {
            "sidecar_only": True,
            "credential_mount_mode": "read_only",
            "agent_mount_or_environment": False,
            "installed_products_or_auth_config_modified": False,
        },
        "fresh_git_workspace_and_state_per_attempt": True,
        "gold_and_hidden_grader_visible_to_agent": False,
        "guard_receipt": {
            "provider": guard.get("provider"),
            "verdict": guard.get("verdict"),
            "sha256": _sha_bytes(_canonical_bytes(guard)),
        },
        "billing": {
            "track": "claude_subscription_same_transport",
            "account_extra_usage_expected_off": True,
            "actual_marginal_charge_observed": False,
            "post_run_ui_recheck_recommended": True,
        },
    }


def _parse_deadline(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluatorError("--deadline-utc is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvaluatorError("--deadline-utc must carry a UTC offset")
    return parsed.astimezone(dt.timezone.utc)


def execute(args: argparse.Namespace) -> int:
    deadline = _parse_deadline(args.deadline_utc)
    if deadline is not None:
        budget = (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if budget < args.attempt_seconds + DEADLINE_MARGIN_SECONDS:
            raise EvaluatorError(
                "--deadline-utc leaves %.0fs, which cannot fit one %ds attempt "
                "plus %ds of evaluator overhead"
                % (budget, args.attempt_seconds, DEADLINE_MARGIN_SECONDS))
    root = args.root.resolve()
    for repo in (args.collie_repo.resolve(), args.bench_repo.resolve()):
        if root == repo or repo in root.parents:
            raise EvaluatorError(
                "--root must live outside both source repositories (got %s)" % root)
    root.mkdir(parents=True, exist_ok=True)

    helpers = load_bench_task_helpers(args.bench_repo)
    prompt_prefix = shared_evaluator_prompt(args.bench_repo)
    tasks, task_notes = load_tasks(args.tasks, helpers)
    task_receipts = task_self_check(tasks, helpers)
    print("task self-check: %d task(s), baseline fails and gold passes for each"
          % len(task_receipts), flush=True)

    collie_revision = _git_revision(args.collie_repo)
    bench_revision = _git_revision(args.bench_repo)
    collie_dirty = _git_dirty(args.collie_repo)
    bench_dirty = _git_dirty(args.bench_repo)
    if args.expect_collie_commit and collie_revision != args.expect_collie_commit:
        message = ("current Collie is at %s but %s was pinned"
                   % (collie_revision or "<unknown>", args.expect_collie_commit))
        if not args.allow_commit_drift:
            raise EvaluatorError(message + "; pass --allow-commit-drift to record "
                                           "the drift and continue")
        print("WARNING: " + message, flush=True)

    stage = build_stage(root, args.collie_repo, args.bench_repo)
    bearer = str(stage.pop("bearer_sentinel"))
    print("staged tree: %s (sha256 %s)" % (stage["path"], stage["stage_sha256"][:16]),
          flush=True)

    images = {
        "sidecar": _image_identity(args.sidecar_image),
        "harness": _image_identity(args.harness_image),
    }
    preflight = image_preflight(args.sidecar_image, args.harness_image,
                                Path(stage["path"]))
    print("image preflight ok: collie=%s pi=%s prime=%s hermes=%s sdk=%s"
          % (preflight["harness"]["collie"], preflight["harness"]["pi"],
             preflight["harness"]["prime"], preflight["harness"]["hermes"],
             preflight["sidecar"]["claude_agent_sdk_version"]), flush=True)

    credential = _claude_credentials_path()
    check_subscription_guard = load_current_subscription_guard(args.collie_repo)
    guard = check_subscription_guard("claude-agent-sdk", model=MODEL,
                                     require_direct_probe=False, environ=os.environ)

    plan = build_plan(tasks, args.repetitions, args.seed, helpers)
    manifest_core = build_manifest(
        args, stage=stage, images=images, preflight=preflight,
        task_receipts=task_receipts, task_notes=task_notes, plan=plan,
        guard=guard, prompt_prefix=prompt_prefix,
        collie_revision=collie_revision, bench_revision=bench_revision,
        collie_dirty=collie_dirty, bench_dirty=bench_dirty)
    suite_sha = _sha_bytes(_canonical_bytes(manifest_core))

    if args.preflight_only:
        print(json.dumps({
            "outcome": "preflight_ok",
            "publishable": False,
            "suite_sha256": suite_sha,
            "stage_sha256": stage["stage_sha256"],
            "images": {key: value["id"] for key, value in images.items()},
            "planned_attempts": len(plan),
            "physical_request_budget_per_attempt": args.requests_per_attempt,
            "guard": {"provider": guard.get("provider"),
                      "verdict": guard.get("verdict")},
        }, ensure_ascii=False, indent=2))
        return 0

    result_root = root / ("results-" + suite_sha[:12])
    temp_root = root / ("work-" + suite_sha[:12])
    if result_root.exists():
        raise EvaluatorError(
            "results for this exact suite already exist at %s; a prior attempt "
            "is never overwritten" % result_root)
    result_root.mkdir(parents=True, exist_ok=False)
    temp_root.mkdir(parents=True, exist_ok=False)
    _atomic_json(result_root / "manifest.json", {
        **manifest_core, "suite_sha256": suite_sha, "created_at_utc": _utc_now()})

    suite = Suite(args=args, stage=Path(stage["path"]), helpers=helpers,
                  tasks={task["task_id"]: task for task in tasks},
                  prompt_prefix=prompt_prefix, bearer=bearer,
                  credential=credential, result_root=result_root,
                  temp_root=temp_root, suite_sha=suite_sha)

    def _admit(row: Mapping[str, Any]) -> str:
        if suite.stop.is_set():
            return suite.stop_reason or "suite_stopped"
        if deadline is not None:
            budget = (deadline - dt.datetime.now(dt.timezone.utc)).total_seconds()
            if budget < args.attempt_seconds + DEADLINE_MARGIN_SECONDS:
                suite.halt("deadline_reached")
                return "deadline_reached"
        return ""

    def _work(row: Mapping[str, Any]) -> dict[str, Any]:
        reason = _admit(row)
        if reason:
            return _not_attempted(row, suite_sha, reason)
        try:
            terminal = run_attempt(suite, row)
        except Exception as exc:  # noqa: BLE001
            terminal = {
                **row, "schema_version": 1, "suite_sha256": suite_sha,
                "status": "invalid_infrastructure", "resolved": False,
                "error_code": "attempt_dispatch_failure",
                "completed_at_utc": _utc_now(),
            }
            _atomic_text(result_root / "runs" / str(row["run_id"]) /
                         "dispatch-error.txt", _scrub(
                             "%s: %s" % (type(exc).__name__, exc)))
        if (terminal["status"] == "invalid_infrastructure"
                and not args.continue_on_infrastructure_invalid):
            suite.halt("infrastructure_invalid:" + str(terminal["run_id"]))
        suite.append_row(terminal)
        suite.note("[%03d/%03d] %-6s %-28s %-22s %s" % (
            row["slot"], len(plan), row["arm"], str(row["task_id"])[:28],
            terminal["status"], terminal.get("error_code") or "-"))
        return terminal

    rows: list[dict[str, Any]] = []
    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, args.concurrency),
                thread_name_prefix="attempt") as pool:
            futures = [pool.submit(_work, row) for row in plan]
            for future in futures:
                rows.append(future.result())
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    rows.sort(key=lambda item: int(item["slot"]))
    summary = summarize(plan, rows, suite_sha, stop_reason=suite.stop_reason)
    _atomic_json(result_root / "summary.json", summary)
    print("results: %s" % result_root, flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["validation_errors"] or suite.stop_reason:
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="normalized_batch",
        description="Adapted four-arm normalized harness comparison over one "
                    "evaluator-owned Claude Agent SDK sidecar.")
    parser.add_argument("--root", type=Path, required=True, metavar="OUTPUTDIR",
                        help="evaluator-owned output directory (staging + results)")
    parser.add_argument("--tasks", type=Path, required=True, metavar="JSON",
                        help="JSON list of {task_id,prompt,fixture_files,"
                             "gold_files,hidden_grader}")
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS,
                        metavar="N", help="repetitions per task per arm")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        metavar="N", help="max concurrent attempts in this process")
    parser.add_argument("--deadline-utc", default=None, metavar="ISO",
                        help="stop dispatching attempts that cannot finish by this time")
    parser.add_argument("--seed", type=int, default=20260907,
                        help="seed for the randomized balanced arm order")
    parser.add_argument("--collie-repo", type=Path, default=DEFAULT_COLLIE_REPO)
    parser.add_argument("--bench-repo", type=Path, default=DEFAULT_BENCH_REPO)
    parser.add_argument("--expect-collie-commit", default=PINNED_COLLIE_COMMIT)
    parser.add_argument("--allow-commit-drift", action="store_true")
    parser.add_argument("--sidecar-image", default=SIDECAR_IMAGE_TAG)
    parser.add_argument("--harness-image", default=HARNESS_IMAGE_TAG)
    parser.add_argument("--requests-per-attempt", type=int,
                        default=DEFAULT_REQUESTS_PER_ATTEMPT,
                        help="explicit physical model request budget per attempt")
    parser.add_argument("--attempt-seconds", type=int,
                        default=DEFAULT_ATTEMPT_SECONDS,
                        help="outer wall ceiling per attempt")
    parser.add_argument("--request-seconds", type=int,
                        default=SIDECAR_REQUEST_SECONDS,
                        help="sidecar timeout for one physical model request")
    parser.add_argument("--max-transport-errors", type=int, default=0,
                        help="non-completed physical requests tolerated per attempt")
    parser.add_argument("--agent-cpus", default="2")
    parser.add_argument("--agent-memory", default="3g")
    parser.add_argument("--sidecar-cpus", default="2")
    parser.add_argument("--sidecar-memory", default="2g")
    parser.add_argument("--continue-on-infrastructure-invalid", action="store_true",
                        help="keep dispatching after an evaluator-infrastructure failure")
    parser.add_argument("--preflight-only", action="store_true",
                        help="stage, verify images and tasks, print the suite id, exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repetitions < 1:
        raise SystemExit("--repetitions must be positive")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be positive")
    if args.requests_per_attempt < 1:
        raise SystemExit("--requests-per-attempt must be positive")
    if args.attempt_seconds < 60:
        raise SystemExit("--attempt-seconds must be at least 60")
    if args.request_seconds < 30:
        raise SystemExit("--request-seconds must be at least 30")
    if args.max_transport_errors < 0:
        raise SystemExit("--max-transport-errors must not be negative")
    try:
        return execute(args)
    except EvaluatorError as exc:
        print("evaluator precondition failed: %s" % exc, file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
