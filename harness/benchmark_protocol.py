"""Frozen, budget-aware contract for cross-harness benchmarks.

This module does not spend model quota.  It validates a benchmark manifest,
expands a deterministic task x seed x harness plan, and refuses to call a result
set publishable when evidence is missing or a shared budget was exceeded.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
TRACKS = {"controlled", "product"}
NETWORK_POLICIES = {"disabled", "allowlist"}
USAGE_FIELDS = (
    "wall_seconds", "model_calls", "turns", "input_tokens", "output_tokens",
    "cache_tokens", "cost_usd",
)
COUNT_USAGE_FIELDS = {"model_calls", "turns", "input_tokens", "output_tokens",
                      "cache_tokens"}
CONTROL_FILES = ("prompt", "tool_contract", "dependency_lock", "sandbox_policy",
                 "retry_policy", "concurrency_policy")
SCHEDULE = "counterbalanced_latin_square"
USAGE_RECEIPT_FORMAT = "collie-benchmark-usage-v1"
GRADER_RECEIPT_FORMAT = "collie-benchmark-grader-v1"
INDEPENDENT_USAGE_SOURCE = "independent-meter"
TRACE_FORMAT = "jsonl"
BOOTSTRAP_REPLICATES = 10_000
SIGN_FLIP_MONTE_CARLO_TRIALS = 65_536
ARTIFACT_LIMITS = {
    "trace": 64 * 1024 * 1024,
    "patch": 16 * 1024 * 1024,
    "grader": 2 * 1024 * 1024,
    "usage": 2 * 1024 * 1024,
}
ARTIFACT_SUFFIXES = {
    "trace": (".jsonl",),
    "patch": (".patch", ".diff"),
    "grader": (".json",),
    "usage": (".json",),
}
_HEX_REVISIONS = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MODEL_DATE = re.compile(r"(?<![0-9])(20[0-9]{2}-[0-9]{2}-[0-9]{2})(?![0-9])")


@dataclass(frozen=True)
class Validation:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _reject_constant(value: str):
    raise ValueError("non-finite JSON number is forbidden: %s" % value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key: %s" % key)
        value[key] = item
    return value


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_fingerprint(manifest: dict) -> str:
    value = dict(manifest)
    value.pop("manifest_sha256", None)
    return hashlib.sha256(_canonical(value)).hexdigest()


def load_manifest(path: str | os.PathLike[str]) -> dict:
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream, parse_constant=_reject_constant,
                          object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("benchmark manifest must be a JSON object")
    strict_error = _strict_json_error(value)
    if strict_error:
        raise ValueError("benchmark manifest must be strict finite JSON: %s" % strict_error)
    return value


def _revision_pinned(value: Any) -> bool:
    """Accept only a full commit/object id or content digest, never a ref/tag."""
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    return bool(_HEX_REVISIONS.fullmatch(text) or _SHA256_DIGEST.fullmatch(text))


def _sha256_pinned(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _model_snapshot_pinned(value: Any) -> bool:
    """Provider snapshots may be dated names when no weights digest is exposed."""
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    if _revision_pinned(text):
        return True
    for match in _MODEL_DATE.finditer(text):
        try:
            datetime.date.fromisoformat(match.group(1))
            return True
        except ValueError:
            continue
    return False


def _positive(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and value > 0)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _strict_json_error(value: Any) -> str:
    try:
        _canonical(value)
    except (TypeError, ValueError) as exc:
        return str(exc)
    return ""


def _bounded_manifest_file(base_dir: str | os.PathLike[str], relative: Any,
                           expected_hash: Any) -> str:
    """Validate a content-addressed file contained in the manifest bundle."""
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return "path must be non-empty and relative"
    if not _sha256_pinned(expected_hash):
        return "sha256 must be 64 lowercase hex characters"
    root = Path(base_dir).resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return "path escapes manifest directory"
    if not path.is_file():
        return "file does not exist: %s" % path
    actual = sha256_file(path)
    return "sha256 mismatch: expected %s, got %s" % (expected_hash, actual) \
        if actual != expected_hash else ""


def _validate_model(model: dict, prefix: str, errors: list[str]) -> None:
    for key in ("provider", "id", "snapshot", "endpoint", "reasoning_effort",
                "temperature", "top_p"):
        if key not in model or model.get(key) in (None, ""):
            errors.append("%s.%s is required" % (prefix, key))
    for key in ("provider", "id", "snapshot", "endpoint", "reasoning_effort"):
        value = model.get(key)
        if value not in (None, "") and (
                not isinstance(value, str) or not value.strip()):
            errors.append("%s.%s must be a non-empty string" % (prefix, key))
    if model.get("snapshot") and not _model_snapshot_pinned(model["snapshot"]):
        errors.append("%s.snapshot must be a digest or immutable dated model id" % prefix)
    temperature = model.get("temperature")
    if (not isinstance(temperature, (int, float)) or isinstance(temperature, bool)
            or not math.isfinite(float(temperature)) or not 0 <= temperature <= 2):
        errors.append("%s.temperature must be a finite number from 0 to 2" % prefix)
    top_p = model.get("top_p")
    if (not isinstance(top_p, (int, float)) or isinstance(top_p, bool)
            or not math.isfinite(float(top_p)) or not 0 < top_p <= 1):
        errors.append("%s.top_p must be a finite number greater than 0 and at most 1" % prefix)


def _task_ids(path: Path) -> list[str]:
    with open(path, encoding="utf-8") as stream:
        return [line.strip() for line in stream
                if line.strip() and not line.lstrip().startswith("#")]


def validate_manifest(manifest: dict, base_dir: str | os.PathLike[str] = ".") -> Validation:
    """Return every reproducibility error instead of failing on the first one."""
    errors: list[str] = []
    warnings: list[str] = []
    strict_json = _strict_json_error(manifest)
    if strict_json:
        errors.append("manifest must be strict finite JSON: %s" % strict_json)
    elif "manifest_sha256" in manifest:
        supplied = manifest.get("manifest_sha256")
        if not _sha256_pinned(supplied) or supplied != manifest_fingerprint(manifest):
            errors.append("manifest_sha256 does not match the canonical manifest")
    if not isinstance(manifest.get("name"), str) or not manifest.get("name", "").strip():
        errors.append("name is required")
    track = str(manifest.get("track") or "")
    if type(manifest.get("schema_version")) is not int or \
            manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version must be %d" % SCHEMA_VERSION)
    if track not in TRACKS:
        errors.append("track must be controlled or product")

    dataset = manifest.get("dataset") if isinstance(manifest.get("dataset"), dict) else {}
    for key in ("name", "revision", "grader_revision", "container_digest",
                "tasks_file", "tasks_sha256"):
        if not dataset.get(key):
            errors.append("dataset.%s is required" % key)
    for key in ("revision", "grader_revision"):
        if dataset.get(key) and not _revision_pinned(dataset[key]):
            errors.append("dataset.%s must be a full commit or content digest" % key)
    container = dataset.get("container_digest")
    if container and (not isinstance(container, str)
                      or not _SHA256_DIGEST.fullmatch(container)):
        errors.append("dataset.container_digest must be sha256:<64 lowercase hex>")
    task_hash = str(dataset.get("tasks_sha256") or "").lower()
    if task_hash and (len(task_hash) != 64 or
                      any(c not in "0123456789abcdef" for c in task_hash)):
        errors.append("dataset.tasks_sha256 must be 64 lowercase hex characters")
    task_path = Path(base_dir) / str(dataset.get("tasks_file") or "")
    if dataset.get("tasks_file"):
        task_file_error = _bounded_manifest_file(
            base_dir, dataset.get("tasks_file"), dataset.get("tasks_sha256"))
        if task_file_error:
            errors.append("dataset.tasks_file %s" % task_file_error)
        else:
            ids = _task_ids(task_path)
            if not ids:
                errors.append("dataset.tasks_file contains no task ids")
            elif len(ids) != len(set(ids)):
                errors.append("dataset.tasks_file contains duplicate task ids")
            elif len(ids) < 2:
                errors.append("dataset.tasks_file must contain at least two task clusters")

    if track == "controlled":
        model = manifest.get("model") if isinstance(manifest.get("model"), dict) else {}
        _validate_model(model, "model", errors)
        controls = (manifest.get("controls")
                    if isinstance(manifest.get("controls"), dict) else {})
        for name in CONTROL_FILES:
            for suffix in ("file", "sha256"):
                key = "%s_%s" % (name, suffix)
                if not controls.get(key):
                    errors.append("controls.%s is required" % key)
            if controls.get("%s_file" % name):
                frozen_error = _bounded_manifest_file(
                    base_dir, controls.get("%s_file" % name),
                    controls.get("%s_sha256" % name))
                if frozen_error:
                    errors.append("controls.%s %s" % (name, frozen_error))
        if not _SHA256_DIGEST.fullmatch(str(controls.get("environment_digest") or "")):
            errors.append("controls.environment_digest must be sha256:<64 lowercase hex>")
        if not _positive_int(controls.get("context_window_tokens")):
            errors.append("controls.context_window_tokens must be a positive integer")

    budget = manifest.get("budget") if isinstance(manifest.get("budget"), dict) else {}
    for key in USAGE_FIELDS:
        valid = _positive_int(budget.get(key)) if key in COUNT_USAGE_FIELDS \
            else _positive(budget.get(key))
        if not valid:
            kind = "integer" if key in COUNT_USAGE_FIELDS else "number"
            errors.append("budget.%s must be a finite positive %s" % (key, kind))
    if budget.get("scope") != "root_plus_descendants":
        errors.append("budget.scope must be root_plus_descendants")

    execution = (manifest.get("execution")
                 if isinstance(manifest.get("execution"), dict) else {})
    repetitions = execution.get("repetitions")
    seeds = execution.get("seeds") if isinstance(execution.get("seeds"), list) else []
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 3:
        errors.append("execution.repetitions must be at least 3")
    if any(not isinstance(seed, int) or isinstance(seed, bool) for seed in seeds):
        errors.append("execution.seeds must contain integers")
    if isinstance(repetitions, int) and (len(seeds) != repetitions or
                                        len(set(map(str, seeds))) != len(seeds)):
        errors.append("execution.seeds must contain one unique seed per repetition")
    if type(execution.get("pass_at")) is not int or execution.get("pass_at") != 1:
        errors.append("execution.pass_at must be 1")
    if (type(execution.get("attempts_per_task")) is not int
            or execution.get("attempts_per_task") != 1):
        errors.append("execution.attempts_per_task must be 1")
    if execution.get("network") not in NETWORK_POLICIES:
        errors.append("execution.network must be disabled or allowlist")
    network_allowlist = execution.get("network_allowlist")
    if execution.get("network") == "allowlist":
        if (not isinstance(network_allowlist, list) or not network_allowlist
                or not all(isinstance(host, str) and host and host.strip() == host
                           for host in network_allowlist)):
            errors.append("execution.network_allowlist must be a non-empty host list")
        elif network_allowlist != sorted(set(network_allowlist)):
            errors.append("execution.network_allowlist must be sorted and unique")
    elif execution.get("network") == "disabled" and network_allowlist not in (None, []):
        errors.append("execution.network_allowlist must be empty when network is disabled")
    if execution.get("schedule") != SCHEDULE:
        errors.append("execution.schedule must be %s" % SCHEDULE)
    if not isinstance(execution.get("schedule_seed"), int) or isinstance(
            execution.get("schedule_seed"), bool):
        errors.append("execution.schedule_seed must be an integer")
    if type(execution.get("max_parallel_runs")) is not int or \
            execution.get("max_parallel_runs") != 1:
        errors.append("execution.max_parallel_runs must be 1 for auditable ordering")
    if track == "controlled":
        if execution.get("memory") != "fresh_per_run":
            errors.append("controlled track requires execution.memory=fresh_per_run")
        if execution.get("refine") is not False:
            errors.append("controlled track requires execution.refine=false")
        if execution.get("native_prompt_extensions") is not False:
            errors.append("controlled track requires execution.native_prompt_extensions=false")
    elif execution.get("memory") != "fresh_per_run":
        warnings.append("product track carries memory; label it a system comparison")

    harnesses = manifest.get("harnesses") if isinstance(manifest.get("harnesses"), list) else []
    if len(harnesses) < 2:
        errors.append("at least two harnesses are required")
    names = []
    for index, raw in enumerate(harnesses):
        value = raw if isinstance(raw, dict) else {}
        prefix = "harnesses[%d]" % index
        raw_name = value.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            errors.append(prefix + ".name must be a non-empty string")
            name = ""
        else:
            name = raw_name.strip()
            names.append(name)
            if (name != raw_name
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name)
                    or "__vs__" in name):
                errors.append(prefix + ".name must be an unambiguous portable identifier")
        if not _revision_pinned(value.get("revision")):
            errors.append(prefix + ".revision must be a full commit or content digest")
        command = value.get("command")
        if not isinstance(command, list) or not command or not all(
                isinstance(arg, str) and arg for arg in command):
            errors.append(prefix + ".command must be a non-empty argv array")
        if value.get("trace_format") != TRACE_FORMAT:
            errors.append(prefix + ".trace_format must be %s" % TRACE_FORMAT)
        if value.get("budget_source") != "manifest":
            errors.append(prefix + ".budget_source must be manifest")
        if value.get("usage_source") != INDEPENDENT_USAGE_SOURCE:
            errors.append(prefix + ".usage_source must be " + INDEPENDENT_USAGE_SOURCE)
        if not _revision_pinned(value.get("usage_meter_revision")):
            errors.append(prefix + ".usage_meter_revision must be a full commit or digest")
        if value.get("usage_receipt_format") != USAGE_RECEIPT_FORMAT:
            errors.append(prefix + ".usage_receipt_format must be " + USAGE_RECEIPT_FORMAT)
        if value.get("includes_subagents") is not True:
            errors.append(prefix + ".includes_subagents must be true")
        if track == "controlled":
            if value.get("model_source") != "manifest":
                errors.append(prefix + ".model_source must be manifest")
            if value.get("seed_source") != "manifest":
                errors.append(prefix + ".seed_source must be manifest")
        elif track == "product":
            if value.get("model_source") != "native_manifest":
                errors.append(prefix + ".model_source must be native_manifest")
            native_model = value.get("model") if isinstance(value.get("model"), dict) else {}
            _validate_model(native_model, prefix + ".model", errors)
    if len(names) != len(set(names)):
        errors.append("harness names must be unique")
    if track == "controlled" and harnesses:
        meter_values = []
        meter_values_valid = True
        for harness in harnesses:
            if not isinstance(harness, dict):
                meter_values_valid = False
                continue
            meter = (harness.get("usage_source"),
                     harness.get("usage_meter_revision"),
                     harness.get("usage_receipt_format"))
            if not all(isinstance(item, str) for item in meter):
                meter_values_valid = False
            else:
                meter_values.append(meter)
        if meter_values_valid and len(set(meter_values)) != 1:
            errors.append("controlled track requires one identical independent usage meter")
    return Validation(tuple(errors), tuple(warnings))


def build_plan(manifest: dict, base_dir: str | os.PathLike[str] = ".") -> list[dict]:
    verdict = validate_manifest(manifest, base_dir)
    if not verdict.ok:
        raise ValueError("invalid benchmark manifest:\n- " + "\n- ".join(verdict.errors))
    tasks = _task_ids(Path(base_dir) / manifest["dataset"]["tasks_file"])
    fingerprint = manifest_fingerprint(manifest)
    plan = []
    harnesses = manifest["harnesses"]
    schedule_seed = manifest["execution"]["schedule_seed"]
    cell = 0
    for task_id in tasks:
        for repetition, seed in enumerate(manifest["execution"]["seeds"], 1):
            offset = (cell + schedule_seed) % len(harnesses)
            ordered = harnesses[offset:] + harnesses[:offset]
            for position, harness in enumerate(ordered, 1):
                identity = {"manifest_sha256": fingerprint, "task_id": task_id,
                            "repetition": repetition, "seed": seed,
                            "harness": harness["name"], "schedule_cell": cell + 1,
                            "harness_position": position,
                            "schedule_index": len(plan) + 1}
                run_id = hashlib.sha256(_canonical(identity)).hexdigest()[:24]
                plan.append({**identity, "run_id": run_id,
                             "budget": dict(manifest["budget"])})
            cell += 1
    return plan


def write_jsonl(path: str | os.PathLike[str], rows: Iterable[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False) + "\n")


def read_jsonl(path: str | os.PathLike[str]) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line, parse_constant=_reject_constant,
                                   object_pairs_hook=_unique_object)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError("%s:%d: invalid JSON: %s" %
                                 (path, line_number, exc)) from exc
            if not isinstance(value, dict):
                raise ValueError("%s:%d: row must be an object" % (path, line_number))
            strict_error = _strict_json_error(value)
            if strict_error:
                raise ValueError("%s:%d: row must be strict finite JSON: %s" %
                                 (path, line_number, strict_error))
            rows.append(value)
    return rows


def _expected_model(manifest: dict, harness: dict) -> dict:
    return manifest["model"] if manifest["track"] == "controlled" else harness["model"]


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * q
    low, high = math.floor(index), math.ceil(index)
    if low == high:
        return float(values[low])
    return float(values[low] * (high - index) + values[high] * (index - low))


def _hash_int(*parts: Any) -> int:
    return int.from_bytes(hashlib.sha256(_canonical(list(parts))).digest()[:8], "big")


def _task_cluster_ci(task_rates: dict[str, float], salt: str,
                     replicates: int = BOOTSTRAP_REPLICATES) -> tuple[float, float] | None:
    """Deterministic percentile bootstrap that resamples whole tasks, not seeds."""
    values = [float(task_rates[key]) for key in sorted(task_rates)]
    if len(values) < 2:
        return None
    sampled = []
    for replicate in range(replicates):
        total = sum(values[_hash_int(salt, replicate, draw) % len(values)]
                    for draw in range(len(values)))
        sampled.append(total / len(values))
    return (_percentile(sampled, .025), _percentile(sampled, .975))


def _task_sign_flip_p(differences: list[float], salt: str) -> float:
    """Two-sided paired randomization test with task as the exchangeable unit."""
    values = [float(value) for value in differences if abs(value) > 1e-15]
    if not values:
        return 1.0
    observed = abs(sum(values))
    if len(values) <= 20:
        trials = 1 << len(values)
        extreme = 0
        for mask in range(trials):
            value = sum(item if mask & (1 << index) else -item
                        for index, item in enumerate(values))
            extreme += int(abs(value) + 1e-15 >= observed)
        return extreme / trials
    trials = SIGN_FLIP_MONTE_CARLO_TRIALS
    extreme = 0
    for trial in range(trials):
        value = sum(item if _hash_int(salt, trial, index) & 1 else -item
                    for index, item in enumerate(values))
        extreme += int(abs(value) + 1e-15 >= observed)
    return (extreme + 1) / (trials + 1)


def _holm_adjust(values: dict[str, float]) -> dict[str, float]:
    """Family-wise Holm adjustment for every predeclared harness pair."""
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    adjusted: dict[str, float] = {}
    running = 0.0
    count = len(ordered)
    for rank, (name, value) in enumerate(ordered):
        running = max(running, min(1.0, (count - rank) * float(value)))
        adjusted[name] = running
    return adjusted


def _load_json_object(path: Path) -> dict:
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream, parse_constant=_reject_constant,
                          object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("JSON artifact must be an object")
    strict_error = _strict_json_error(value)
    if strict_error:
        raise ValueError("JSON artifact must be strict finite JSON: %s" % strict_error)
    return value


def _artifact_format_error(path: Path, kind: str, trace_format: str) -> str:
    try:
        if kind == "trace":
            if trace_format != TRACE_FORMAT:
                return "unsupported trace format"
            records = 0
            with open(path, encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    value = json.loads(line, parse_constant=_reject_constant,
                                       object_pairs_hook=_unique_object)
                    if not isinstance(value, dict):
                        return "trace line %d must be a JSON object" % line_number
                    strict_error = _strict_json_error(value)
                    if strict_error:
                        return "trace line %d must be strict finite JSON (%s)" % (
                            line_number, strict_error)
                    records += 1
            if not records:
                return "trace must contain at least one JSON object"
        elif kind == "patch":
            text = path.read_text(encoding="utf-8")
            if text.strip():
                stripped = text.lstrip()
                if not stripped.startswith("diff --git ") or not any(
                        marker in text for marker in ("\n@@ ", "\nnew file mode ",
                                                     "\ndeleted file mode ",
                                                     "\nold mode ", "\nnew mode ",
                                                     "\nrename from ",
                                                     "\nBinary files ",
                                                     "\nGIT binary patch")):
                    return "non-empty patch must contain a git diff and change record"
        else:
            _load_json_object(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return "invalid %s format (%s)" % (kind, exc)
    return ""


def _verified_artifact(root: Path, relative: Any, expected_hash: Any, kind: str,
                       trace_format: str = TRACE_FORMAT) -> tuple[str, Path | None]:
    """Validate containment, size, digest and a minimal interoperable format."""
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        return "path must be non-empty and relative", None
    digest = expected_hash
    if not _sha256_pinned(digest):
        return "sha256 must be 64 lowercase hex characters", None
    root = root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return "path escapes evidence root", None
    if not path.is_file():
        return "file does not exist", None
    if path.suffix.lower() not in ARTIFACT_SUFFIXES[kind]:
        return "file extension does not match %s artifact" % kind, path
    try:
        before = path.stat()
    except OSError as exc:
        return "file cannot be stat'ed (%s)" % exc, path
    size = before.st_size
    if size > ARTIFACT_LIMITS[kind]:
        return "file exceeds %d-byte limit" % ARTIFACT_LIMITS[kind], path
    try:
        actual = sha256_file(path)
        after = path.stat()
    except OSError as exc:
        return "file cannot be hashed (%s)" % exc, path
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return "file changed while it was being hashed", path
    if actual != digest:
        return "sha256 mismatch (%s != %s)" % (actual, digest), path
    format_error = _artifact_format_error(path, kind, trace_format)
    return (format_error, path) if format_error else ("", path)


def _json_equal(left: Any, right: Any) -> bool:
    try:
        return _canonical(left) == _canonical(right)
    except (TypeError, ValueError):
        return False


def _usage_error(usage: Any) -> str:
    if not isinstance(usage, dict):
        return "usage must be an object"
    if usage.get("scope") != "root_plus_descendants":
        return "usage scope must be root_plus_descendants"
    for field in USAGE_FIELDS:
        value = usage.get(field)
        if field in COUNT_USAGE_FIELDS:
            if not _nonnegative_int(value):
                return "%s must be a non-negative integer" % field
        elif (not isinstance(value, (int, float)) or isinstance(value, bool)
              or not math.isfinite(float(value)) or value < 0):
            return "%s must be a finite non-negative number" % field
    return ""


def _grader_receipt_error(root: Path, row: dict, planned: dict, manifest: dict) -> str:
    """Bind the grader verdict to this exact run, patch, grader, and container."""
    path = (root / str(row.get("grader_path") or "")).resolve()
    try:
        receipt = _load_json_object(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return "invalid JSON receipt (%s)" % exc
    except ValueError as exc:
        return "invalid JSON receipt (%s)" % exc
    expected = {
        "format": GRADER_RECEIPT_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "manifest_sha256": manifest_fingerprint(manifest),
        "run_id": row.get("run_id"),
        "task_id": planned["task_id"],
        "dataset_revision": manifest["dataset"]["revision"],
        "grader_revision": manifest["dataset"]["grader_revision"],
        "container_digest": manifest["dataset"]["container_digest"],
        "patch_sha256": row.get("patch_sha256"),
        "resolved": row.get("resolved"),
    }
    for field, value in expected.items():
        if not _json_equal(receipt.get(field), value):
            return "receipt.%s does not match the frozen run" % field
    return ""


def _usage_receipt_error(root: Path, row: dict, planned: dict, manifest: dict,
                         harness: dict) -> str:
    """Bind aggregate usage to the trace and independently pinned meter."""
    path = (root / str(row.get("usage_path") or "")).resolve()
    try:
        receipt = _load_json_object(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return "invalid JSON receipt (%s)" % exc
    expected = {
        "format": USAGE_RECEIPT_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "manifest_sha256": manifest_fingerprint(manifest),
        "run_id": row.get("run_id"),
        "task_id": planned["task_id"],
        "schedule_index": planned["schedule_index"],
        "started_at_unix_ms": row.get("started_at_unix_ms"),
        "finished_at_unix_ms": row.get("finished_at_unix_ms"),
        "harness": planned["harness"],
        "harness_revision": harness["revision"],
        "model": _expected_model(manifest, harness),
        "meter": {"source": harness["usage_source"],
                  "revision": harness["usage_meter_revision"]},
        "trace_sha256": row.get("trace_sha256"),
        "patch_sha256": row.get("patch_sha256"),
        "scope": "root_plus_descendants",
        "includes_subagents": True,
        "usage": row.get("usage"),
    }
    for field, value in expected.items():
        if not _json_equal(receipt.get(field), value):
            return "receipt.%s does not match the frozen run" % field
    receipt_usage_error = _usage_error(receipt.get("usage"))
    return "receipt.usage %s" % receipt_usage_error if receipt_usage_error else ""


def summarize(manifest: dict, plan_rows: list[dict], result_rows: list[dict],
              evidence_dir: str | os.PathLike[str] = ".", *,
              manifest_dir: str | os.PathLike[str] | None = None) -> dict:
    """Compute pass@1 only after validating the complete evidence matrix."""
    manifest_base = evidence_dir if manifest_dir is None else manifest_dir
    canonical_plan = build_plan(manifest, manifest_base)
    fingerprint = manifest_fingerprint(manifest)
    errors: list[str] = []
    plan_json_error = _strict_json_error(plan_rows)
    result_json_error = _strict_json_error(result_rows)
    if plan_json_error:
        errors.append("plan must be strict finite JSON: %s" % plan_json_error)
    if result_json_error:
        errors.append("results must be strict finite JSON: %s" % result_json_error)
    if not _json_equal(plan_rows, canonical_plan):
        errors.append("plan does not exactly match the canonical manifest expansion")
    # Always score against the canonical expansion. A truncated/reordered plan
    # can therefore never select an easier task subset or change run order.
    expected = {row["run_id"]: row for row in canonical_plan}
    actual: dict[str, dict] = {}
    for row in result_rows:
        run_id = str(row.get("run_id") or "")
        if run_id not in expected:
            errors.append("unexpected run_id %s" % (run_id or "<empty>"))
            continue
        if run_id in actual:
            errors.append("duplicate result for run_id %s" % run_id)
            continue
        if row.get("manifest_sha256") != fingerprint:
            errors.append("run %s used a different manifest" % run_id)
        actual[run_id] = row
    missing = sorted(set(expected) - set(actual))
    if missing:
        errors.append("missing %d planned result(s)" % len(missing))

    valid: dict[str, bool] = {}
    evidence_root = Path(evidence_dir).resolve()
    harness_manifest = {h["name"]: h for h in manifest["harnesses"]}
    by_harness = {h["name"]: [] for h in manifest["harnesses"]}
    artifact_owners: list[tuple[Path, str, str]] = []
    for run_id, planned in expected.items():
        row = actual.get(run_id)
        if row is None:
            valid[run_id] = False
            continue
        usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        run_ok = True
        for field in ("harness", "task_id", "seed", "repetition", "schedule_cell",
                      "harness_position", "schedule_index"):
            if not _json_equal(row.get(field), planned.get(field)):
                errors.append("run %s metadata.%s does not match its plan" % (run_id, field))
                run_ok = False
        expected_harness = harness_manifest[planned["harness"]]
        expected_model = _expected_model(manifest, expected_harness)
        if row.get("harness_revision") != expected_harness["revision"]:
            errors.append("run %s used a different harness revision" % run_id)
            run_ok = False
        if row.get("model_snapshot") != expected_model["snapshot"]:
            errors.append("run %s used a different model snapshot" % run_id)
            run_ok = False
        if not _json_equal(row.get("model"), expected_model):
            errors.append("run %s model configuration does not match the manifest" % run_id)
            run_ok = False
        if row.get("usage_source") != expected_harness.get("usage_source"):
            errors.append("run %s usage source does not match the manifest" % run_id)
            run_ok = False
        if row.get("usage_meter_revision") != expected_harness.get("usage_meter_revision"):
            errors.append("run %s usage meter revision does not match the manifest" % run_id)
            run_ok = False
        usage_error = _usage_error(usage)
        if usage_error:
            errors.append("run %s invalid aggregate usage: %s" % (run_id, usage_error))
            run_ok = False
        for field in USAGE_FIELDS:
            value = usage.get(field)
            valid_value = _nonnegative_int(value) if field in COUNT_USAGE_FIELDS else (
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and value >= 0)
            if valid_value and value > manifest["budget"][field]:
                errors.append("run %s exceeded budget.%s (%s > %s)" %
                              (run_id, field, value, manifest["budget"][field]))
                run_ok = False
        if type(row.get("attempt")) is not int or row.get("attempt") != 1:
            errors.append("run %s is not pass@1" % run_id)
            run_ok = False
        if not _positive_int(row.get("started_at_unix_ms")):
            errors.append("run %s started_at_unix_ms must be a positive integer" % run_id)
            run_ok = False
        if not _positive_int(row.get("finished_at_unix_ms")):
            errors.append("run %s finished_at_unix_ms must be a positive integer" % run_id)
            run_ok = False
        elif _positive_int(row.get("started_at_unix_ms")):
            elapsed_ms = row["finished_at_unix_ms"] - row["started_at_unix_ms"]
            if elapsed_ms <= 0:
                errors.append("run %s has a non-positive execution interval" % run_id)
                run_ok = False
        if not isinstance(row.get("resolved"), bool):
            errors.append("run %s resolved must be boolean" % run_id)
            run_ok = False
        artifact_ok: dict[str, bool] = {}
        for kind in ("trace", "patch", "grader", "usage"):
            artifact_error, artifact_path = _verified_artifact(
                evidence_root, row.get(kind + "_path"), row.get(kind + "_sha256"), kind,
                expected_harness["trace_format"])
            if artifact_error:
                errors.append("run %s %s artifact: %s" % (run_id, kind, artifact_error))
                run_ok = False
                artifact_ok[kind] = False
                continue
            duplicate = None
            for prior_path, prior_run, prior_kind in artifact_owners:
                try:
                    same_file = artifact_path == prior_path or os.path.samefile(
                        artifact_path, prior_path)
                except OSError:
                    same_file = artifact_path == prior_path
                if same_file:
                    duplicate = "%s %s artifact" % (prior_run, prior_kind)
                    break
            if duplicate:
                errors.append("run %s %s artifact reuses %s" % (run_id, kind, duplicate))
                run_ok = False
                artifact_ok[kind] = False
            else:
                artifact_owners.append((artifact_path, run_id, kind))
                artifact_ok[kind] = True
        if artifact_ok.get("grader"):
            grader_error = _grader_receipt_error(
                evidence_root, row, planned, manifest)
            if grader_error:
                errors.append("run %s grader artifact: %s" % (run_id, grader_error))
                run_ok = False
        if artifact_ok.get("usage"):
            receipt_error = _usage_receipt_error(
                evidence_root, row, planned, manifest, expected_harness)
            if receipt_error:
                errors.append("run %s usage artifact: %s" % (run_id, receipt_error))
                run_ok = False
        valid[run_id] = run_ok
        if run_ok:
            by_harness[planned["harness"]].append(row)

    last_finished = None
    for planned in canonical_plan:
        row = actual.get(planned["run_id"])
        started = row.get("started_at_unix_ms") if row else None
        finished = row.get("finished_at_unix_ms") if row else None
        if _positive_int(started) and _positive_int(finished):
            if last_finished is not None and started < last_finished:
                errors.append(
                    "run %s overlapped or started before the preceding canonical "
                    "schedule entry finished" %
                    planned["run_id"])
            last_finished = max(last_finished or 0, finished)

    publishable = not errors
    harness_summary = {}
    for name, rows in by_harness.items():
        solved = sum(bool(row.get("resolved")) for row in rows)
        task_outcomes: dict[str, list[bool]] = {}
        for row in rows:
            task_outcomes.setdefault(row["task_id"], []).append(bool(row["resolved"]))
        task_rates = {task: sum(values) / len(values)
                      for task, values in task_outcomes.items()}
        interval = _task_cluster_ci(
            task_rates, "%s:%s:harness-ci" % (fingerprint, name)) if publishable else None
        harness_summary[name] = {
            "valid_runs": len(rows),
            "planned_runs": sum(row["harness"] == name for row in canonical_plan),
            "resolved": solved,
            "task_clusters": len(task_rates),
            "pass_at_1": solved / len(rows) if rows and publishable else None,
            "pass_at_1_task_cluster_bootstrap_95": list(interval) if interval else None,
            "median_cost_usd": statistics.median(
                [float(row["usage"]["cost_usd"]) for row in rows])
                if rows and publishable else None,
            "median_total_tokens": statistics.median([
                float(row["usage"]["input_tokens"]) +
                float(row["usage"]["output_tokens"]) +
                float(row["usage"]["cache_tokens"]) for row in rows])
                if rows and publishable else None,
            "p50_wall_seconds": _percentile(
                [float(row["usage"]["wall_seconds"]) for row in rows], .5)
                if publishable else None,
            "p95_wall_seconds": _percentile(
                [float(row["usage"]["wall_seconds"]) for row in rows], .95)
                if publishable else None,
            "p50_elapsed_seconds": _percentile([
                (row["finished_at_unix_ms"] - row["started_at_unix_ms"]) / 1000.0
                for row in rows], .5) if publishable else None,
            "p95_elapsed_seconds": _percentile([
                (row["finished_at_unix_ms"] - row["started_at_unix_ms"]) / 1000.0
                for row in rows], .95) if publishable else None,
        }

    pairs = {}
    names = [h["name"] for h in manifest["harnesses"]]
    cells = {(row["task_id"], row["seed"], row["harness"]): actual.get(row["run_id"])
             for row in canonical_plan if valid.get(row["run_id"])}
    task_seeds = {(row["task_id"], row["seed"]) for row in canonical_plan}
    raw_pair_p: dict[str, float] = {}
    if publishable:
        for index, left in enumerate(names):
            for right in names[index + 1:]:
                both = neither = left_only = right_only = complete = 0
                for task_seed in task_seeds:
                    a = cells.get((*task_seed, left))
                    b = cells.get((*task_seed, right))
                    if a is None or b is None:
                        continue
                    complete += 1
                    av, bv = bool(a.get("resolved")), bool(b.get("resolved"))
                    both += int(av and bv)
                    neither += int(not av and not bv)
                    left_only += int(av and not bv)
                    right_only += int(bv and not av)
                task_differences = {}
                for task_id in sorted({task for task, _seed in task_seeds}):
                    task_cells = [
                        (cells[(task_id, seed, left)], cells[(task_id, seed, right)])
                        for seed in manifest["execution"]["seeds"]]
                    task_differences[task_id] = (
                        sum(bool(a.get("resolved")) for a, _b in task_cells) /
                        len(task_cells)
                        - sum(bool(b.get("resolved")) for _a, b in task_cells) /
                        len(task_cells))
                pair_name = "%s__vs__%s" % (left, right)
                difference_values = list(task_differences.values())
                nonzero_clusters = sum(
                    abs(value) > 1e-15 for value in difference_values)
                raw_p = _task_sign_flip_p(
                    difference_values, "%s:%s:sign-flip" % (fingerprint, pair_name))
                raw_pair_p[pair_name] = raw_p
                difference_interval = _task_cluster_ci(
                    task_differences, "%s:%s:difference-ci" % (fingerprint, pair_name))
                pairs[pair_name] = {
                    "paired_trials": complete, "both": both, "neither": neither,
                    "left_only": left_only, "right_only": right_only,
                    "paired_task_clusters": len(task_differences),
                    "mean_pass_at_1_difference": (
                        sum(difference_values) / len(difference_values)),
                    "task_cluster_bootstrap_difference_95": list(difference_interval)
                    if difference_interval else None,
                    "task_cluster_sign_flip_two_sided_p": raw_p,
                    "task_cluster_sign_flip_method": "exact"
                    if nonzero_clusters <= 20 else "deterministic_monte_carlo",
                    "task_cluster_sign_flip_trials": (1 << nonzero_clusters)
                    if nonzero_clusters <= 20 else SIGN_FLIP_MONTE_CARLO_TRIALS,
                }
        adjusted = _holm_adjust(raw_pair_p)
        for pair_name, value in adjusted.items():
            pairs[pair_name]["holm_adjusted_p"] = value
    return {"schema_version": SCHEMA_VERSION, "track": manifest["track"],
            "claim": "harness_effect" if manifest["track"] == "controlled"
            else "system_comparison", "manifest_sha256": fingerprint,
            "publishable": publishable, "evidence_errors": errors,
            "statistics_withheld": not publishable,
            "inference": {"unit": "task", "interval": "deterministic cluster bootstrap",
                          "bootstrap_replicates": BOOTSTRAP_REPLICATES,
                          "paired_test": "task-level sign flip",
                          "multiplicity": "Holm family-wise adjustment"},
            "harnesses": harness_summary, "paired": pairs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.benchmark_protocol")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("manifest")
    plan = commands.add_parser("plan")
    plan.add_argument("manifest")
    plan.add_argument("--out", required=True)
    report = commands.add_parser("summarize")
    report.add_argument("manifest")
    report.add_argument("--plan", required=True)
    report.add_argument("--results", required=True)
    report.add_argument("--out", default="")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest).resolve()
    manifest = load_manifest(manifest_path)
    verdict = validate_manifest(manifest, manifest_path.parent)
    for warning in verdict.warnings:
        print("WARN:", warning)
    for error in verdict.errors:
        print("ERROR:", error)
    if not verdict.ok:
        return 2
    print("manifest_sha256=%s" % manifest_fingerprint(manifest))
    if args.command == "validate":
        return 0
    if args.command == "plan":
        plan_rows = build_plan(manifest, manifest_path.parent)
        write_jsonl(args.out, plan_rows)
        print("planned_runs=%d" % len(plan_rows))
        print("maximum_authorized_cost_usd=%.2f" %
              (len(plan_rows) * float(manifest["budget"]["cost_usd"])))
        return 0
    value = summarize(manifest, read_jsonl(args.plan), read_jsonl(args.results),
                      Path(args.results).resolve().parent,
                      manifest_dir=manifest_path.parent)
    output = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                        allow_nan=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(output + "\n")
    print(output)
    return 0 if value["publishable"] else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
