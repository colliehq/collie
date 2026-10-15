"""Core-owned durable task evidence, using the existing session journal primitives.

One journal/lease per physical workspace fences checks across task IDs. This is
not a native agent transcript and does not claim ownership of native edits.
All paths are explicit; this module never uses the user's default sessions.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import time
import uuid

from . import run_ownership, sessions, verification

_TASK_ID = re.compile(r"te-([0-9a-f]{32})\.([0-9a-f]{32})\Z")
MAX_TASKS = 200
MAX_RECEIPTS = 100


class EvidenceStoreError(RuntimeError):
    """A failed durable operation must not be acknowledged as accepted."""


def workspace_key(root):
    path = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    return hashlib.sha256(os.fsencode(path)).hexdigest()[:32]


def task_workspace_key(task_id):
    match = _TASK_ID.fullmatch(task_id) if isinstance(task_id, str) else None
    if not match:
        raise EvidenceStoreError("invalid task_id")
    return match.group(1)


class TaskEvidenceStore:
    def __init__(self, state_dir):
        if not isinstance(state_dir, (str, os.PathLike)) or not os.path.isabs(state_dir):
            raise EvidenceStoreError("state_dir must be an explicit absolute directory")
        self.directory = os.path.join(os.path.realpath(state_dir), "task-evidence")
        os.makedirs(self.directory, exist_ok=True)

    def _sid(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key):
            raise EvidenceStoreError("invalid workspace identity")
        return "evidence-" + key

    def hold(self, key):
        return run_ownership.hold(self._sid(key), label="task-evidence",
                                  directory=self.directory)

    def _path(self, key):
        path = sessions._path(self._sid(key), self.directory)
        if not path:
            raise EvidenceStoreError("invalid evidence journal path")
        return path

    def _read(self, key, root=None):
        path = self._path(key)
        if not os.path.exists(path):
            if root is None:
                raise EvidenceStoreError("task journal is missing")
            return {"id": self._sid(key), "project": "task-evidence", "cwd": root,
                    "messages": [], "run_receipts": [], "updated": time.time(),
                    "task_evidence": {"version": 1, "tasks": {}}}
        try:
            obj = sessions._validate_raw(sessions._load_raw(path), self._sid(key))
            data = obj["task_evidence"]
            if data.get("version") != 1 or not isinstance(data.get("tasks"), dict):
                raise ValueError("invalid task evidence structure")
            if len(data["tasks"]) > MAX_TASKS:
                raise ValueError("task journal exceeds its bound")
            if root is not None and os.path.normcase(obj["cwd"]) != os.path.normcase(root):
                raise ValueError("workspace identity changed")
            return obj
        except (KeyError, TypeError, ValueError) as exc:
            raise EvidenceStoreError("task journal requires inspection") from exc

    def _write(self, key, obj):
        obj["updated"] = time.time()
        # Serialize before touching disk, then reuse fsync + atomic replacement.
        try:
            sessions._atomic_dump(obj, self._path(key))
        except Exception as exc:
            raise EvidenceStoreError("task evidence was not durably accepted") from exc

    def create(self, key, plan):
        path = self._path(key)
        with sessions._locked(path):
            obj = self._read(key, plan["root"])
            tasks = obj["task_evidence"]["tasks"]
            if len(tasks) >= MAX_TASKS:
                raise EvidenceStoreError("task journal is full; inspect it locally")
            task_id = "te-" + key + "." + uuid.uuid4().hex
            task = dict(copy.deepcopy(plan), task_id=task_id, created_at=time.time(),
                        receipts=[], handoffs=[])
            tasks[task_id] = task
            self._write(key, obj)
            return copy.deepcopy(task)

    def read_task(self, task_id):
        key = task_workspace_key(task_id)
        with sessions._locked(self._path(key)):
            obj = self._read(key)
            task = obj["task_evidence"]["tasks"].get(task_id)
            if not isinstance(task, dict) or task.get("task_id") != task_id:
                raise EvidenceStoreError("unknown task_id")
            if not isinstance(task.get("receipts"), list) or not isinstance(task.get("handoffs"), list):
                raise EvidenceStoreError("task evidence requires inspection")
            return copy.deepcopy(task)

    def recovery(self, key):
        # Missing/corrupt state is not an empty, replay-safe ledger.
        with sessions._locked(self._path(key)):
            self._read(key)
            return sessions.recovery_state(self._sid(key), directory=self.directory)

    def arm(self, task_id, run_id, lease, snapshot):
        key = task_workspace_key(task_id)
        run_ownership.validate(lease, self._sid(key), directory=self.directory)
        with sessions._locked(self._path(key)):
            obj = self._read(key)
            if sessions._recovery_required(obj.get("active_run")):
                raise EvidenceStoreError("workspace has an unknown check effect; inspect before retrying")
            task = obj["task_evidence"]["tasks"].get(task_id)
            if not isinstance(task, dict):
                raise EvidenceStoreError("unknown task_id")
            obj["active_run"] = {"run_id": run_id, "turn": 0, "state": "external_action",
                                 "updated": time.time(),
                                 "detail": {"tool_name": verification.CHECK_BOUNDARY_TOOL,
                                            "surface": "task-evidence", "task_id": task_id,
                                            "scope_digest": snapshot.get("digest", "")}}
            self._write(key, obj)
            state = sessions.recovery_state(self._sid(key), directory=self.directory)
            if not state or not state.get("recovery_required") or state.get("run_id") != run_id:
                raise EvidenceStoreError("check boundary did not become durable")

    def finish(self, task_id, run_id, receipt, lease):
        key = task_workspace_key(task_id)
        run_ownership.validate(lease, self._sid(key), directory=self.directory)
        with sessions._locked(self._path(key)):
            obj = self._read(key)
            active = obj.get("active_run") or {}
            if (active.get("run_id") != run_id or
                    (active.get("detail") or {}).get("task_id") != task_id):
                raise EvidenceStoreError("this check does not own the workspace boundary")
            task = obj["task_evidence"]["tasks"][task_id]
            task["receipts"] = (task["receipts"] + [copy.deepcopy(receipt)])[-MAX_RECEIPTS:]
            # Receipt persistence and retirement are one atomic journal commit.
            # Any write failure leaves the previous durable unknown boundary.
            retire, _ = verification.check_boundary_verdict(receipt["execution"])
            if retire:
                obj.pop("active_run", None)
            self._write(key, obj)
            return retire

    def save_handoff(self, task_id, bundle, lease):
        key = task_workspace_key(task_id)
        run_ownership.validate(lease, self._sid(key), directory=self.directory)
        with sessions._locked(self._path(key)):
            obj = self._read(key)
            task = obj["task_evidence"]["tasks"].get(task_id)
            if not isinstance(task, dict):
                raise EvidenceStoreError("unknown task_id")
            task["handoffs"] = (task["handoffs"] + [copy.deepcopy(bundle)])[-20:]
            self._write(key, obj)
