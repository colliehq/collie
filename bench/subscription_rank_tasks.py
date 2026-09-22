"""Frozen local tasks for the small subscription-native product ranking.

The compared agents receive only ``fixture_files`` and ``prompt``.  The evaluator keeps
``hidden_grader`` and ``gold_files`` outside each agent workspace.  This module contains no
model invocation code; ``self_check`` only proves that each pristine fixture fails its grader
and that the corresponding reference implementation passes it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
from typing import Any, Mapping


Task = dict[str, Any]


TASKS: tuple[Task, ...] = (
    {
        "task_id": "local-audit-request-id-v1",
        "prompt": """Extend auditflow so an optional request_id is preserved end to end.

Requirements:
- AuditRecord must have a request_id field whose value is str | None and whose default is None.
  Existing three-positional-argument construction must remain valid, and AuditRecord must remain
  immutable.
- decode_record must read the optional "request_id" payload key. If the key is absent its value
  is None. Preserve a supplied value unchanged, including an empty string.
- redact_record must continue replacing secret with "[redacted]" and must preserve request_id
  when it constructs the replacement record.
- format_record must keep its current output exactly unchanged when request_id is None. Otherwise
  append exactly " request=<request_id>"; an empty request_id therefore still appends " request=".
- render_audit must reflect all of the above while never exposing the original secret.

Keep the existing module names and public imports. Edit the source code now; do not only describe
the changes.
""",
        "fixture_files": {
            "auditflow/__init__.py": """from .decode import decode_record
from .formatting import format_record
from .model import AuditRecord
from .pipeline import render_audit
from .redact import redact_record

__all__ = [
    "AuditRecord",
    "decode_record",
    "redact_record",
    "format_record",
    "render_audit",
]
""",
            "auditflow/model.py": """from dataclasses import dataclass


@dataclass(frozen=True)
class AuditRecord:
    actor: str
    action: str
    secret: str
""",
            "auditflow/decode.py": """from collections.abc import Mapping

from .model import AuditRecord


def decode_record(payload: Mapping[str, object]) -> AuditRecord:
    return AuditRecord(
        actor=str(payload["actor"]),
        action=str(payload["action"]),
        secret=str(payload["secret"]),
    )
""",
            "auditflow/redact.py": """from .model import AuditRecord


def redact_record(record: AuditRecord) -> AuditRecord:
    return AuditRecord(
        actor=record.actor,
        action=record.action,
        secret="[redacted]",
    )
""",
            "auditflow/formatting.py": """from .model import AuditRecord


def format_record(record: AuditRecord) -> str:
    return f"{record.actor} {record.action} secret={record.secret}"
""",
            "auditflow/pipeline.py": """from collections.abc import Mapping

from .decode import decode_record
from .formatting import format_record
from .redact import redact_record


def render_audit(payload: Mapping[str, object]) -> str:
    return format_record(redact_record(decode_record(payload)))
""",
        },
        "hidden_grader": """from dataclasses import FrozenInstanceError

from auditflow import (
    AuditRecord,
    decode_record,
    format_record,
    redact_record,
    render_audit,
)

legacy = AuditRecord("ana", "login", "top-secret")
assert legacy.request_id is None
assert format_record(legacy) == "ana login secret=top-secret"

try:
    legacy.actor = "changed"
except FrozenInstanceError:
    pass
else:
    raise AssertionError("AuditRecord must remain frozen")

missing = decode_record({
    "actor": "ana",
    "action": "login",
    "secret": "top-secret",
})
assert missing == AuditRecord("ana", "login", "top-secret", None)

present = decode_record({
    "actor": "ben",
    "action": "export",
    "secret": "token-1",
    "request_id": "req-42",
})
assert present == AuditRecord("ben", "export", "token-1", "req-42")

blank = decode_record({
    "actor": "cy",
    "action": "read",
    "secret": "token-2",
    "request_id": "",
})
assert blank.request_id == ""
assert format_record(blank) == "cy read secret=token-2 request="

redacted = redact_record(present)
assert redacted == AuditRecord("ben", "export", "[redacted]", "req-42")
assert present.secret == "token-1"

assert (
    render_audit({
        "actor": "ben",
        "action": "export",
        "secret": "token-1",
        "request_id": "req-42",
    })
    == "ben export secret=[redacted] request=req-42"
)
assert (
    render_audit({
        "actor": "ana",
        "action": "login",
        "secret": "top-secret",
    })
    == "ana login secret=[redacted]"
)
assert "top-secret" not in render_audit({
    "actor": "ana",
    "action": "login",
    "secret": "top-secret",
})
""",
        "gold_files": {
            "auditflow/model.py": """from dataclasses import dataclass


@dataclass(frozen=True)
class AuditRecord:
    actor: str
    action: str
    secret: str
    request_id: str | None = None
""",
            "auditflow/decode.py": """from collections.abc import Mapping

from .model import AuditRecord


def decode_record(payload: Mapping[str, object]) -> AuditRecord:
    return AuditRecord(
        actor=str(payload["actor"]),
        action=str(payload["action"]),
        secret=str(payload["secret"]),
        request_id=payload.get("request_id"),
    )
""",
            "auditflow/redact.py": """from .model import AuditRecord


def redact_record(record: AuditRecord) -> AuditRecord:
    return AuditRecord(
        actor=record.actor,
        action=record.action,
        secret="[redacted]",
        request_id=record.request_id,
    )
""",
            "auditflow/formatting.py": """from .model import AuditRecord


def format_record(record: AuditRecord) -> str:
    suffix = "" if record.request_id is None else f" request={record.request_id}"
    return f"{record.actor} {record.action} secret={record.secret}{suffix}"
""",
        },
    },
    {
        "task_id": "local-circuit-breaker-v1",
        "prompt": """Fix CircuitBreaker in breaker.py so it follows this deterministic contract:

- Construction raises ValueError when failure_threshold is less than 1 or cooldown is negative.
- A new breaker is closed, has zero failures, and has opened_at set to None.
- allow(now) returns True while closed.
- Closed-state failures are consecutive: each record_failure(now) increments failures, while any
  record_success() fully resets the breaker. The breaker opens on the failure that makes failures
  equal to failure_threshold, not one failure later, and records that failure's timestamp.
- While open, allow(now) returns False before opened_at + cooldown. At the exact boundary or later,
  one call returns True and moves the breaker to half_open.
- Only one probe may be outstanding: every further allow call while half_open returns False,
  regardless of elapsed time.
- A half-open record_failure(now) immediately reopens the breaker, sets failures to
  failure_threshold, and starts a new cooldown at now.
- record_failure(now) while already open is a no-op and must not extend the cooldown or alter the
  failure count.
- record_success() from any state fully resets state to closed, failures to zero, and opened_at
  to None.

Keep the existing class and public attribute names. Edit the source code now; do not only describe
the fix.
""",
        "fixture_files": {
            "breaker.py": """class CircuitBreaker:
    \"\"\"A deterministic circuit breaker; callers supply timestamps.\"\"\"

    def __init__(self, failure_threshold: int, cooldown: float):
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.state = "closed"
        self.failures = 0
        self.opened_at = None

    def allow(self, now: float) -> bool:
        if self.state == "open" and now > self.opened_at + self.cooldown:
            self.state = "half_open"
            return True
        return self.state != "open"

    def record_success(self) -> None:
        self.state = "closed"

    def record_failure(self, now: float) -> None:
        self.failures += 1
        if self.failures > self.failure_threshold:
            self.state = "open"
            self.opened_at = now
""",
        },
        "hidden_grader": """from breaker import CircuitBreaker


for threshold, cooldown in ((0, 1.0), (-2, 1.0), (1, -0.01)):
    try:
        CircuitBreaker(threshold, cooldown)
    except ValueError:
        pass
    else:
        raise AssertionError(
            f"invalid constructor accepted: {threshold=}, {cooldown=}"
        )

breaker = CircuitBreaker(2, 10.0)
assert breaker.state == "closed"
assert breaker.failures == 0
assert breaker.opened_at is None
assert breaker.allow(0.0) is True

breaker.record_failure(1.0)
assert breaker.state == "closed"
assert breaker.failures == 1

breaker.record_success()
assert breaker.state == "closed"
assert breaker.failures == 0
assert breaker.opened_at is None

breaker.record_failure(2.0)
breaker.record_failure(3.0)
assert breaker.state == "open"
assert breaker.failures == 2
assert breaker.opened_at == 3.0

assert breaker.allow(12.999) is False
assert breaker.state == "open"
assert breaker.allow(13.0) is True
assert breaker.state == "half_open"
assert breaker.allow(13.0) is False
assert breaker.allow(99.0) is False

breaker.record_failure(100.0)
assert breaker.state == "open"
assert breaker.failures == 2
assert breaker.opened_at == 100.0

breaker.record_failure(105.0)
assert breaker.state == "open"
assert breaker.failures == 2
assert breaker.opened_at == 100.0
assert breaker.allow(109.999) is False
assert breaker.allow(110.0) is True

breaker.record_success()
assert breaker.state == "closed"
assert breaker.failures == 0
assert breaker.opened_at is None
assert breaker.allow(110.0) is True

single = CircuitBreaker(1, 0.0)
single.record_failure(4.0)
assert single.state == "open"
assert single.failures == 1
assert single.opened_at == 4.0
assert single.allow(4.0) is True
assert single.state == "half_open"
assert single.allow(4.0) is False

single.record_success()
assert single.state == "closed"
assert single.failures == 0
assert single.opened_at is None

late_success = CircuitBreaker(1, 5.0)
late_success.record_failure(8.0)
assert late_success.state == "open"
late_success.record_success()
assert late_success.state == "closed"
assert late_success.failures == 0
assert late_success.opened_at is None
""",
        "gold_files": {
            "breaker.py": """class CircuitBreaker:
    \"\"\"A deterministic circuit breaker; callers supply timestamps.\"\"\"

    def __init__(self, failure_threshold: int, cooldown: float):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if cooldown < 0:
            raise ValueError("cooldown must not be negative")
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self.state = "closed"
        self.failures = 0
        self.opened_at = None

    def allow(self, now: float) -> bool:
        if self.state == "closed":
            return True
        if self.state == "half_open":
            return False
        if now >= self.opened_at + self.cooldown:
            self.state = "half_open"
            return True
        return False

    def record_success(self) -> None:
        self.state = "closed"
        self.failures = 0
        self.opened_at = None

    def record_failure(self, now: float) -> None:
        if self.state == "open":
            return
        if self.state == "half_open":
            self.state = "open"
            self.failures = self.failure_threshold
            self.opened_at = now
            return
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.state = "open"
            self.opened_at = now
""",
        },
    },
)


_TASK_KEYS = {"task_id", "prompt", "fixture_files", "hidden_grader", "gold_files"}


def canonical_json_bytes(value: object) -> bytes:
    """Return the one canonical UTF-8 JSON representation used by task fingerprints."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    """Return a lowercase SHA-256 hex digest of ``canonical_json_bytes(value)``."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def task_sha256(task: Mapping[str, object]) -> str:
    """Fingerprint the complete task, including held-out grader and reference files."""
    return canonical_sha256(task)


def task_by_id(task_id: str) -> Task:
    """Look up a frozen task specification by its stable identifier."""
    for task in TASKS:
        if task["task_id"] == task_id:
            return task
    raise KeyError(task_id)


def _relative_parts(raw_path: str) -> tuple[str, ...]:
    if not isinstance(raw_path, str) or not raw_path or "\\" in raw_path:
        raise ValueError("task paths must be non-empty canonical POSIX paths")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or str(path) != raw_path or ".." in path.parts:
        raise ValueError("unsafe or non-canonical task path: %r" % raw_path)
    if not path.parts or ":" in path.parts[0]:
        raise ValueError("unsafe task path: %r" % raw_path)
    return path.parts


def materialize_task(task: Mapping[str, object], destination: str | os.PathLike[str],
                     *, gold: bool = False) -> None:
    """Write a fixture, optionally overlaying its held-out gold files.

    The caller must provide a fresh evaluator-owned directory.  This helper never deletes files.
    Gold files must never be materialized into a compared agent's workspace.
    """
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    fixture_files = task["fixture_files"]
    gold_files = task["gold_files"]
    if not isinstance(fixture_files, Mapping) or not isinstance(gold_files, Mapping):
        raise TypeError("fixture_files and gold_files must be mappings")
    layers = (fixture_files, gold_files) if gold else (fixture_files,)
    for layer in layers:
        for relative, content in layer.items():
            parts = _relative_parts(relative)
            if not isinstance(content, str):
                raise TypeError("task file contents must be strings")
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")


def _run_hidden_grader(task: Mapping[str, object], workdir: Path) -> subprocess.CompletedProcess[str]:
    grader = task["hidden_grader"]
    if not isinstance(grader, str):
        raise TypeError("hidden_grader must be a string")
    wrapper = "import sys\nsys.path.insert(0, %r)\n" % str(workdir) + grader
    return subprocess.run(
        [sys.executable, "-I", "-c", wrapper],
        cwd=workdir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )


def _validate_task_data(task: Mapping[str, object]) -> None:
    if set(task) != _TASK_KEYS:
        raise AssertionError("task keys differ from the frozen schema")
    if not isinstance(task["task_id"], str) or not task["task_id"]:
        raise AssertionError("task_id must be a non-empty string")
    for field in ("prompt", "hidden_grader"):
        value = task[field]
        if not isinstance(value, str) or not value.strip() or not value.endswith("\n"):
            raise AssertionError("%s must be non-empty newline-terminated UTF-8 text" % field)
        value.encode("utf-8")
    fixture_files = task["fixture_files"]
    gold_files = task["gold_files"]
    if not isinstance(fixture_files, Mapping) or not fixture_files:
        raise AssertionError("fixture_files must be a non-empty mapping")
    if not isinstance(gold_files, Mapping) or not gold_files:
        raise AssertionError("gold_files must be a non-empty mapping")
    if not set(gold_files).issubset(fixture_files):
        raise AssertionError("gold_files may only replace files in the baseline fixture")
    for files in (fixture_files, gold_files):
        for relative, content in files.items():
            _relative_parts(relative)
            if (not isinstance(content, str) or not content.endswith("\n")
                    or "\x00" in content):
                raise AssertionError("task files must be NUL-free newline-terminated text")
            content.encode("utf-8")
    encoded = canonical_json_bytes(task)
    if encoded != canonical_json_bytes(json.loads(encoded.decode("utf-8"))):
        raise AssertionError("task canonical JSON does not round-trip deterministically")
    digest = task_sha256(task)
    if len(digest) != 64 or digest != digest.lower():
        raise AssertionError("task digest is not lowercase SHA-256")


def self_check() -> dict[str, str]:
    """Validate task data and prove baseline-fail/reference-pass for every task."""
    if len(TASKS) != 2:
        raise AssertionError("the subscription ranking must contain exactly two tasks")
    task_ids = [task["task_id"] for task in TASKS]
    if len(set(task_ids)) != len(task_ids):
        raise AssertionError("task IDs must be unique")

    digests: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="collie-rank-task-self-check-") as temp_dir:
        temp_root = Path(temp_dir)
        for index, task in enumerate(TASKS):
            _validate_task_data(task)
            task_id = task["task_id"]
            baseline_dir = temp_root / ("baseline-%d" % index)
            materialize_task(task, baseline_dir)
            baseline = _run_hidden_grader(task, baseline_dir)
            if baseline.returncode == 0:
                raise AssertionError("baseline unexpectedly passes: %s" % task_id)

            gold_dir = temp_root / ("gold-%d" % index)
            materialize_task(task, gold_dir, gold=True)
            gold = _run_hidden_grader(task, gold_dir)
            if gold.returncode != 0:
                detail = (gold.stderr or gold.stdout or "")[-500:]
                raise AssertionError("gold fails for %s: %s" % (task_id, detail))
            digests[task_id] = task_sha256(task)
    return digests


if __name__ == "__main__":
    print(json.dumps(self_check(), sort_keys=True, indent=2))
