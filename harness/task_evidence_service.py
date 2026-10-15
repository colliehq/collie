"""Application port for user-configured checks and point-in-time handoff evidence.

The host agent owns its edits. This service owns only its explicitly registered
check process and its evidence journal; no memory provider or model is required.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import time
import uuid

from . import verification
from .task_evidence_store import EvidenceStoreError, TaskEvidenceStore, task_workspace_key, workspace_key

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
FILE_CAP = 20_000
BYTE_CAP = 64 * 1024 * 1024


class EvidenceConfigError(ValueError):
    pass


def _digest(value):
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _inside(path, root):
    try:
        return os.path.commonpath((os.path.normcase(path), os.path.normcase(root))) == os.path.normcase(root)
    except ValueError:
        return False


def _link(info):
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def scope_snapshot(root, scope):
    """Bounded exact bytes in configured paths; links/races/unreadable paths are unknown.

    No shell, Git executable, or source code is run to compute this fingerprint.
    Missing paths are included so their later creation invalidates a receipt.
    """
    digest = hashlib.sha256()
    count = 0
    remaining = BYTE_CAP
    complete = True
    seen = set()

    def visit(path):
        nonlocal count, remaining, complete
        if not _inside(os.path.realpath(path), root):
            complete = False
            return
        relative = os.path.relpath(path, root).replace(os.sep, "/")
        if relative in seen:
            return
        seen.add(relative)
        count += 1
        if count > FILE_CAP:
            complete = False
            return
        digest.update(relative.encode("utf-8", "surrogatepass") + b"\0")
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            digest.update(b"missing\0")
            return
        if _link(info):
            complete = False
            digest.update(b"link\0")
            return
        digest.update(str(info.st_mode).encode("ascii") + b"\0")
        if stat.S_ISDIR(info.st_mode):
            names = sorted(os.listdir(path))
            for name in names:
                visit(os.path.join(path, name))
                if not complete:
                    return
            if names != sorted(os.listdir(path)):
                complete = False
        elif stat.S_ISREG(info.st_mode):
            # Length-delimit content: otherwise bytes in one file could mimic
            # the next file's header and produce an identical stream digest.
            digest.update(str(info.st_size).encode("ascii") + b"\0")
            if info.st_size > remaining:
                complete = False
                return
            with open(path, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    complete = False
                    return
                while True:
                    chunk = stream.read(min(1024 * 1024, remaining + 1))
                    if not chunk:
                        break
                    if len(chunk) > remaining:
                        complete = False
                        return
                    digest.update(chunk)
                    remaining -= len(chunk)
                after = os.fstat(stream.fileno())
            named = os.lstat(path)
            if any(getattr(info, field) != getattr(other, field)
                   for other in (opened, after, named)
                   for field in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")):
                complete = False
        else:
            complete = False

    try:
        if _link(os.lstat(root)) or not os.path.isdir(root):
            raise ValueError("workspace is not an ordinary directory")
        for relative in scope:
            path = root
            parts = () if relative == "." else PurePosixPath(relative).parts
            # Reject links in ancestors too, rather than following a scope root.
            for part in parts[:-1]:
                path = os.path.join(path, part)
                if os.path.lexists(path) and _link(os.lstat(path)):
                    raise ValueError("scope ancestor is a link")
            visit(os.path.join(root, *parts))
            if not complete:
                break
    except (OSError, ValueError):
        complete = False
    return {"digest": digest.hexdigest(), "complete": complete, "observed_at": time.time()}


class TaskEvidenceService:
    def __init__(self, config, state_dir, *, verifier=None):
        # verifier injection is an application/test port, never a model argument.
        self._verifier = verifier or verification.run_verification_command
        self.registry = self._registry(config)
        resolved_state = os.path.realpath(os.path.abspath(state_dir))
        if any(_inside(resolved_state, item["root"]) for item in self.registry.values()):
            raise EvidenceConfigError("evidence state must be outside every registered workspace")
        self.store = TaskEvidenceStore(state_dir)

    @classmethod
    def from_file(cls, config_path, state_dir=None):
        if not os.path.isabs(config_path):
            raise EvidenceConfigError("config path must be absolute")
        with open(config_path, encoding="utf-8") as stream:
            config = json.load(stream, parse_constant=lambda _: (_ for _ in ()).throw(
                EvidenceConfigError("non-finite configuration")))
        configured_state = state_dir if state_dir is not None else config.get("state_dir")
        if not isinstance(configured_state, str) or not os.path.isabs(configured_state):
            raise EvidenceConfigError("configure an explicit absolute state_dir")
        service = cls(config, configured_state)
        if any(_inside(os.path.realpath(config_path), item["root"]) for item in service.registry.values()):
            raise EvidenceConfigError("trusted configuration must be outside registered workspaces")
        return service

    @staticmethod
    def _registry(config):
        if not isinstance(config, dict) or type(config.get("version")) is not int or config.get("version") != 1:
            raise EvidenceConfigError("configuration version must be 1")
        rows = config.get("workspaces")
        if not isinstance(rows, dict) or not rows:
            raise EvidenceConfigError("configure at least one workspace")
        result = {}
        for workspace_id, row in rows.items():
            if not isinstance(workspace_id, str) or not _ID.fullmatch(workspace_id) or not isinstance(row, dict):
                raise EvidenceConfigError("invalid workspace registration")
            raw_root = row.get("root")
            if not isinstance(raw_root, str) or not os.path.isabs(raw_root):
                raise EvidenceConfigError("registered root must be absolute")
            root = os.path.realpath(raw_root)
            if not os.path.isdir(root):
                raise EvidenceConfigError("registered root must exist")
            goal = row.get("goal")
            if not isinstance(goal, str) or not goal.strip() or len(goal) > 12_000:
                raise EvidenceConfigError("register a bounded user-authored goal")
            scope = row.get("scope")
            if not isinstance(scope, list) or not scope or len(scope) > 100:
                raise EvidenceConfigError("register a non-empty scope")
            normalized = []
            for path in scope:
                if (not isinstance(path, str) or not path or "\\" in path or "\x00" in path
                        or ":" in path or PurePosixPath(path).is_absolute()
                        or ".." in PurePosixPath(path).parts):
                    raise EvidenceConfigError("scope must contain relative paths without traversal")
                normalized.append(str(PurePosixPath(path)))
            checks = row.get("checks")
            if not isinstance(checks, dict) or not checks:
                raise EvidenceConfigError("register at least one check")
            approved = {}
            for check_id, check in checks.items():
                if not isinstance(check_id, str) or not _ID.fullmatch(check_id) or not isinstance(check, dict):
                    raise EvidenceConfigError("invalid check registration")
                command, timeout = check.get("command"), check.get("timeout_s", 300)
                if not isinstance(command, str) or not command.strip() or len(command) > 8_000:
                    raise EvidenceConfigError("check command must be preconfigured")
                if type(timeout) is not int or not 1 <= timeout <= 900:
                    raise EvidenceConfigError("check timeout must be 1..900 seconds")
                approved[check_id] = {"command": command, "timeout_s": timeout}
            result[workspace_id] = {"root": root, "goal": goal,
                                    "scope": sorted(set(normalized)), "checks": approved}
        return result

    def _plan(self, workspace_id, check_id):
        if workspace_id not in self.registry:
            raise EvidenceConfigError("workspace_id is not registered")
        workspace = self.registry[workspace_id]
        if check_id not in workspace["checks"]:
            raise EvidenceConfigError("check_id is not registered for this workspace")
        check = workspace["checks"][check_id]
        plan = {"workspace_id": workspace_id, "check_id": check_id,
                "root": workspace["root"], "goal": workspace["goal"], "scope": workspace["scope"],
                "check_digest": _digest(check)}
        plan["registration_digest"] = _digest(plan)
        return plan, check

    def _task(self, task_id):
        task = self.store.read_task(task_id)
        plan, check = self._plan(task.get("workspace_id"), task.get("check_id"))
        if (task.get("registration_digest") != plan["registration_digest"] or
                task_workspace_key(task_id) != workspace_key(plan["root"])):
            raise EvidenceConfigError("task registration changed; it is not authority for a new check")
        return task, plan, check

    def open_task(self, workspace_id, check_id):
        plan, _ = self._plan(workspace_id, check_id)
        key = workspace_key(plan["root"])
        with self.store.hold(key):
            task = self.store.create(key, plan)
            return {"accepted": True, "task_id": task["task_id"], "workspace_id": workspace_id,
                    "check_id": check_id, "goal": plan["goal"], "scope": plan["scope"],
                    "freshness": "unknown", "execution_owner": "native host; configured check only is service-owned"}

    def verify_task(self, task_id):
        key = task_workspace_key(task_id)
        with self.store.hold(key) as lease:
            task, plan, check = self._task(task_id)
            before = scope_snapshot(plan["root"], plan["scope"])
            if not before["complete"]:
                raise EvidenceStoreError("scope fingerprint is incomplete; check was not started")
            run_id = uuid.uuid4().hex
            self.store.arm(task_id, run_id, lease, before)
            try:
                execution = self._verifier(check["command"], plan["root"],
                                           timeout=check["timeout_s"], source="user_configured",
                                           after_last_edit=True)
                if not isinstance(execution, dict):
                    raise ValueError("verifier did not return structured evidence")
            except Exception as exc:
                # A raised verifier may have started an effect. Do not guess it
                # did not run, and do not clear its durable boundary.
                execution = {"executed": None, "process_tree_terminated": False,
                             "command_passed": False, "passed": False,
                             "freshness": "unknown", "error_type": type(exc).__name__}
            after = scope_snapshot(plan["root"], plan["scope"])
            comparable = before["complete"] and after["complete"]
            stable = comparable and before["digest"] == after["digest"]
            settled, _ = verification.check_boundary_verdict(execution)
            freshness = "unknown" if not comparable or not settled else "fresh" if stable else "stale"
            passed = bool(freshness == "fresh" and execution.get("executed") is True
                          and execution.get("process_tree_terminated") is True
                          and execution.get("passed") is True)
            receipt = {"receipt_id": "check-" + run_id, "task_id": task_id,
                       "check_id": plan["check_id"], "check_digest": plan["check_digest"],
                       "before": before, "after": after, "freshness": freshness,
                       "passed": passed, "created_at": time.time(), "execution": execution}
            retired = self.store.finish(task_id, run_id, receipt, lease)
            return {"accepted": True, "receipt": receipt, "recovery_required": not retired}

    def prepare_handoff(self, task_id):
        key = task_workspace_key(task_id)
        with self.store.hold(key) as lease:
            task, plan, _ = self._task(task_id)
            snapshot = scope_snapshot(plan["root"], plan["scope"])
            recovery = self.store.recovery(key)
            receipt = task["receipts"][-1] if task["receipts"] else None
            freshness = "unknown"
            if receipt and snapshot["complete"] and not (recovery or {}).get("recovery_required"):
                prior = receipt.get("after") or {}
                if prior.get("complete") and receipt.get("freshness") in ("fresh", "stale"):
                    # Matching the post-check bytes cannot rehabilitate a check
                    # whose input changed while it ran.
                    freshness = ("fresh" if receipt["freshness"] == "fresh" and
                                 prior.get("digest") == snapshot["digest"] else "stale")
            bundle = {"bundle_id": "handoff-" + uuid.uuid4().hex, "task_id": task_id,
                      "workspace_id": plan["workspace_id"], "check_id": plan["check_id"],
                      "goal": plan["goal"], "scope": plan["scope"], "snapshot": snapshot,
                      "freshness": freshness, "verified": bool(receipt and receipt.get("passed")
                                                               and freshness == "fresh"),
                      "receipt_id": receipt.get("receipt_id") if receipt else None,
                      "verification": ({field: (receipt.get("execution") or {}).get(field)
                                        for field in ("executed", "exit_code", "command_passed", "passed",
                                                      "source", "freshness", "output", "process_tree_terminated")}
                                       if receipt else None),
                      "recovery_required": bool((recovery or {}).get("recovery_required")),
                      "recovery": recovery, "created_at": time.time(),
                      "native_session_migration": False,
                      "limitations": ["point-in-time scoped evidence; native edits remain host-owned",
                                      "only the registered check was observed; no all-tools safety guarantee",
                                      "no automatic replay or clearing of unknown effects"]}
            # A second observation detects edits racing bundle preparation.
            latest = scope_snapshot(plan["root"], plan["scope"])
            if not latest["complete"] or latest["digest"] != snapshot["digest"]:
                bundle.update(freshness="unknown", verified=False, snapshot=latest)
            self.store.save_handoff(task_id, bundle, lease)
            return dict(bundle, accepted=True)
