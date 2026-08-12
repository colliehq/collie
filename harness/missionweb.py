"""MissionService — drive missions over HTTP for `collie web` (the NL front door).

`collie web` (webui/index.html) is the interaction surface the user asked to keep;
this is the thin service behind its mission commands. The user types a goal in
plain words; this starts a campaign, drives it with the REAL model
(mission.ModelDecider over the configured provider), and exposes
status / run / confirm / pause / resume / cancel / check so the chat UI can show progress, gate an
irreversible action, and carry the campaign on.

It owns no policy of its own: the container (mission.py) keeps durability, the
gate, authority (leash), and evidence; this only marshals goal-in / status-out.
It uses the SAME ~/.collie stores as `collie jobs` / jobsweb, so a mission started
by mouth is visible on every surface. The decider is injectable so tests run
deterministically at $0 (a scripted decider); production builds
ModelDecider(make_provider(<the configured provider>)).
"""

from __future__ import annotations

import json
import fnmatch
import hashlib
import os
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

from .actions import (APPROVED, EXECUTED, EXECUTING, EXPIRED, PENDING, REFUSED,
                      ActionStore, RefusedError)
from .jobs import (CANCELLED, DONE_ACCEPTED, DONE_VERIFIED, FAILED_S, NEEDS_YOU,
                   PAUSED, PAUSING, QUEUED, RECONCILING, RECOVERY_REQUIRED,
                   RUNNING, WAITING, Capability)
from .mission import (_campaign_coverage, _open_campaign_coverage,
                      _resolved_authorization, MissionDriver, MissionStore,
                      ModelDecider, ResourceBusy, create_mission, world_leash)
from .primitives import register_primitives
from .verifier import FAILED as VERIFY_FAILED, VERIFIED as VERIFY_VERIFIED, Verdict


def _hook_manager(cwd: str, state_dir: str):
    """Construct HookManager against this service's state without mutating process env."""
    from .hooks import HookManager
    wanted = os.path.abspath(os.path.expanduser(state_dir))
    return HookManager(cwd, state_dir=wanted)


def _provider_name() -> str:
    # mirrors webapp._provider(): the Settings-panel provider is applied into the
    # env before a request, so this is the same provider the chat GUI runs on.
    return os.environ.get("COLLIE_PROVIDER", "")


def _clean(d: dict) -> dict:
    """Drop the injected `_case` context from args/case before it hits the UI."""
    return {k: v for k, v in (d or {}).items() if k not in ("_case", "_leash")}


def _short(value, limit=500):
    value = " ".join(str(value or "").split())
    return value[:limit]


def _mission_summary(mission, steps, receipts, runtime, inbox, next_wait, activity=None):
    """Build a bounded, deterministic operator view without another model call."""
    case = mission.case or {}
    resolved_auth = [x for x in case.get("resolved_authorizations", [])
                     if isinstance(x, dict)]
    pending_auth = [x for x in case.get("pending_authorizations", [])
                    if isinstance(x, dict) and
                    not _resolved_authorization(x, resolved_auth)][-8:]
    coverage = _campaign_coverage(case)
    open_coverage = _open_campaign_coverage(case)
    completed = []
    for item in (activity or []):
        if item.get("status") == "completed":
            label = _short(item.get("summary") or item.get("capability"), 160)
            if label and label not in completed:
                completed.append(label)
    failed = 0
    for step in steps:
        verdict = str(step.get("verdict") or "").lower()
        name = _short(step.get("name"), 100)
        if (not activity and verdict in ("verified", "standing-authorized") and
                name and name not in completed):
            completed.append(name)
        if verdict in ("failed", "inconclusive"):
            failed += 1
    verified_receipts = sum(1 for r in receipts if r.get("verdict") == "verified")
    pending = len(pending_auth) + len(open_coverage) + (1 if inbox else 0)
    phase = _short(runtime.get("active_phase"), 160)
    if mission.state in (RUNNING, PAUSING, RECONCILING) and open_coverage:
        current = "Working on campaign branch: %s" % _short(
            open_coverage[0].get("branch"), 180)
    elif mission.state in (RUNNING, PAUSING, RECONCILING):
        current = phase or _short(mission.result, 500)
    else:
        current = _short(mission.result, 500) or phase
    if inbox:
        next_step = "Confirm the prepared %s action" % _short(inbox.get("capability"), 100)
    elif open_coverage:
        next_step = "Continue campaign branch: %s" % _short(
            open_coverage[0].get("branch"), 180)
    elif pending_auth:
        next_step = _short(pending_auth[0].get("summary"), 500)
        if mission.state not in (NEEDS_YOU, PAUSED):
            next_step += " (authorization is waiting; independent work continues)"
    elif next_wait:
        next_step = "Re-check at %s" % next_wait.get("fire_at")
    elif mission.state == QUEUED:
        next_step = "Start the next Mission step"
    elif mission.state in (RUNNING, PAUSING, RECONCILING):
        next_step = _short(runtime.get("active_phase"), 160) or "Continue the active step"
    elif mission.state == DONE_ACCEPTED:
        next_step = "Return this Mission to Collie if more work remains"
    elif mission.state == DONE_VERIFIED:
        next_step = "No remaining work; completion was independently verified"
    else:
        next_step = "Review the Mission state"
    blocker = ""
    if mission.state == NEEDS_YOU:
        blocker = (_short(mission.result, 500) or
                   (_short(pending_auth[0].get("summary"), 500) if pending_auth else "") or
                   "A person-required step")
    return {
        "title": _short(mission.goal, 300),
        "current": current or "Ready",
        "completed": completed[-8:],
        "next": next_step,
        "blocker": blocker,
        "authorization_waiting": len(pending_auth),
        "coverage": {
            "total": len(coverage),
            "open": len(open_coverage),
            "next": [_short(x.get("branch"), 120) for x in open_coverage[:5]],
        },
        "progress": {"verified": len(completed) + verified_receipts,
                     "pending": pending, "failed": failed},
    }


class MissionService:
    def __init__(self, base: str = None, decider=None, provider: str = None,
                 model: str = None, stub=None, state_dir: str = None,
                 goal_verifier=None, mission_workers: int = None, run_tree=None,
                 hooks=None, specialist_workers: int = None):
        # base isolates tests; production uses the shared ~/.collie stores (so a
        # mission is visible to `collie jobs` / jobsweb too).
        # A custom ``base`` is primarily the deterministic test/embedding seam;
        # keep every implicit store beside it unless the caller explicitly names
        # a shared state directory.  Production does not pass ``base`` and all
        # three durable databases therefore live under the same state directory.
        state_dir = state_dir or os.environ.get("COLLIE_STATE_DIR") or \
            (os.path.dirname(os.path.abspath(base)) if base else
             os.path.expanduser("~/.collie"))
        mission_path = (base + ".missions") if base else os.path.join(state_dir, "jobs.db")
        action_path = (base + ".actions") if base else os.path.join(state_dir, "actions.db")
        self.store = MissionStore(mission_path)
        self.actions = ActionStore(action_path)
        self._decider = decider
        self._provider = provider or _provider_name()
        self._model = model or os.environ.get("COLLIE_MODEL") or None
        self._stub = stub
        self._goal_verifier = goal_verifier
        self._mission_workers = mission_workers
        self._owns_run_tree = run_tree is None
        self._owns_hooks = hooks is None
        if hooks is None:
            # HookManager treats unreviewed/changed hook files as pending data;
            # constructing a MissionService must never execute or choke on them.
            hooks = _hook_manager(os.getcwd(), state_dir)
        if run_tree is None:
            from .tasktree import TaskTreeStore
            run_tree = TaskTreeStore(os.path.join(state_dir, "tasktree.db"),
                                     hooks=hooks)
        self._run_tree = run_tree
        self._hooks = hooks
        self._specialist_workers = specialist_workers
        if self._run_tree is not None and self._hooks is not None and \
                getattr(self._run_tree, "hooks", None) is None:
            self._run_tree.hooks = self._hooks
        self._prov = None
        self._runtime_ready = False
        self._capabilities = None
        self._closed = False

    def _ensure_runtime(self):
        """Initialize model and primitives lazily; status/list never need a provider."""
        if self._runtime_ready:
            return
        stub = (self._provider == "mock") if self._stub is None else bool(self._stub)
        if self._decider is None:
            if not self._provider:
                raise RuntimeError("no model provider configured for Mission")
            if self._provider == "mock":
                raise RuntimeError("mock provider cannot drive a durable Mission")
            from .providers import make_provider
            self._prov = make_provider(self._provider, self._model)
        if stub:
            self._capabilities = register_primitives(stub=True)
        else:
            if self._prov is None and self._provider:
                from .providers import make_provider
                self._prov = make_provider(self._provider, self._model)
            from .webact import get_actuator
            self._capabilities = register_primitives(
                stub=False, actuator=get_actuator(), provider=self._prov)
        # These capabilities are service-bound rather than globally registered:
        # their executor must address this exact durable TaskTree/MissionStore.
        self._capabilities = self._tasktree_guarded_capabilities(
            self._capabilities) + self._agent_capabilities()
        self._runtime_ready = True

    def _driver(self, *, lane="mission", control=None) -> MissionDriver:
        self._ensure_runtime()
        dec = self._decider or ModelDecider(self._prov)
        if control is None:
            control = self._mission_control
        return MissionDriver(self.store, self.actions, dec,
                             capabilities=self._capabilities,
                             goal_verifier=self._goal_verifier, lane=lane,
                             control=control, hooks=self._hooks,
                             completion_guard=self._agent_completion_guard)

    def _specialist_run(self, mid):
        runtime = self.store.runtime(mid)
        run_id = runtime.get("external_run_id") if runtime.get("lane") == "specialist" else ""
        return self._run_tree.get(run_id) if self._run_tree is not None and run_id else None

    def _mission_run_id(self, mission):
        """Resolve the TaskTree identity for either a root or specialist Mission."""
        if not mission or self._run_tree is None:
            return ""
        runtime = self.store.runtime(mission.mission_id)
        if runtime.get("lane") == "specialist":
            return str(runtime.get("external_run_id") or
                       (mission.case or {}).get("_specialist_run_id") or "")
        return str((mission.case or {}).get("_run_id") or "")

    def _project_mission_usage(self, mid, run_id=""):
        """Project one Mission's absolute *own* runtime into TaskTree."""
        if self._run_tree is None:
            return []
        mission = self.store.get(mid)
        if not mission:
            return []
        run_id = str(run_id or self._mission_run_id(mission) or "")
        if not run_id:
            return []
        runtime = self.store.runtime(mid)
        return self._run_tree.project_mission_usage(
            run_id, mid,
            input_tokens=runtime.get("input_tokens", 0),
            output_tokens=runtime.get("output_tokens", 0),
            cache_tokens=runtime.get("cache_tokens", 0),
            model_calls=runtime.get("model_calls", 0),
            turns=runtime.get("turns", 0),
            model_cost_microusd=runtime.get("model_cost_microusd", 0),
            wall_ms=runtime.get("active_wall_ms", 0),
            retries=runtime.get("retry_count", 0))

    def _reconcile_tasktree_usage(self, mid=None, limit=None):
        """Catch up cross-database usage gaps without double charging descendants."""
        if self._run_tree is None:
            return {"projected": 0, "errors": [], "exhausted": []}
        if mid:
            mission = self.store.get(mid)
            run_id = self._mission_run_id(mission)
            rows = self._run_tree.tree(run_id).get("flat", []) if run_id else []
        else:
            rows = self._run_tree.usage_reconciliation_runs()
        projected, errors, exhausted, seen = 0, [], [], set()
        candidates = rows if limit is None else rows[:max(1, int(limit))]
        for run in candidates:
            run_id = str(run.get("run_id") or "")
            mission_id = str(run.get("mission_id") or "")
            if not run_id or not mission_id or run_id in seen:
                continue
            seen.add(run_id)
            try:
                exhausted.extend(self._project_mission_usage(mission_id, run_id))
                projected += 1
            except (ValueError, sqlite3.Error) as exc:
                errors.append({"run_id": run_id, "mission_id": mission_id,
                               "error": "%s: %s" % (type(exc).__name__, exc)})
        return {"projected": projected, "errors": errors, "exhausted": exhausted}

    def _tasktree_guarded_capabilities(self, capabilities):
        """Bind code workspace ownership to this service's durable run tree.

        TaskTree resources are scheduling/delegation authority.  They are not a
        generic sandbox for research, browser, messaging, or other capabilities;
        those tools keep their own leash and capability-specific containment.
        Code is the one current primitive whose entire writable workspace can be
        checked here, before the action latch or code runner is entered.
        """
        guarded = []
        for capability in capabilities:
            if capability.name != "code":
                guarded.append(capability)
                continue
            resource = capability.resource

            def guarded_resource(record, original=resource):
                self._assert_tasktree_code_access(record)
                return original(record) if callable(original) else original

            guarded.append(replace(capability, resource=guarded_resource))
        return guarded

    def _assert_tasktree_code_access(self, record):
        """Fail closed unless the bound run still owns its source workspace."""
        if self._run_tree is None:
            raise RefusedError("code authority denied: durable run tree is unavailable")
        mission = self.store.get(record.job_id)
        if not mission:
            raise RefusedError("code authority denied: Mission is missing")
        run_id = self._mission_run_id(mission)
        if not run_id:
            # A code-only Mission may not have used agent.spawn yet. Attach the
            # same least-authority root so every MissionService code action has a
            # caller identity for can_access(), not an unguarded legacy path.
            run_id = self._ensure_agent_root(mission)
            mission = self.store.get(record.job_id) or mission
        run = self._run_tree.get(run_id) if run_id else None
        if not run or run.get("mission_id") != record.job_id:
            raise RefusedError("code authority denied: Mission has no bound run")
        if (run.get("status") in ("completed", "failed", "cancelled", "cancel_requested") or
                run.get("cancel_requested")):
            raise RefusedError("code authority denied: bound run is stopping or terminal")
        if run.get("parent_run_id") and (
                run.get("status") != "running" or not run.get("owner_token")):
            raise RefusedError("code authority denied: specialist run is not the active owner")

        case = mission.case or {}
        if run.get("parent_run_id"):
            parent = self._run_tree.get(run.get("parent_run_id") or "") or {}
            source_workspace = (case.get("_resource_source_workspace") or
                                parent.get("workspace") or "")
        else:
            source_workspace = run.get("workspace") or case.get("_isolated_workspace") or ""
        if not source_workspace:
            raise RefusedError("code authority denied: source workspace is not bound")
        allowed, reason = self._run_tree.can_access(
            run_id, {"kind": "file", "id": source_workspace}, "write")
        if allowed:
            return
        if str(reason).startswith("write ownership delegated to "):
            raise ResourceBusy("delegated code workspace busy: %s" % reason)
        raise RefusedError("code authority denied: %s" % reason)

    def _agent_caller(self, mid):
        mission = self.store.get(mid)
        if not mission:
            return None, None, "unknown mission"
        if mission.state in (DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
            return mission, None, "terminal Mission cannot control specialists"
        run_id = self._mission_run_id(mission)
        if self._run_tree is not None and not run_id:
            run_id = self._ensure_agent_root(mission)
            mission = self.store.get(mid) or mission
        run = self._run_tree.get(run_id) if self._run_tree is not None and run_id else None
        if not run or run.get("mission_id") != mid:
            return mission, None, "mission has no bound durable run tree"
        if run.get("status") in ("completed", "failed", "cancelled", "cancel_requested") or \
                run.get("cancel_requested"):
            return mission, None, "calling run is stopping or terminal"
        return mission, run, ""

    @staticmethod
    def _agent_root_id(mission_id):
        return "run_root_" + hashlib.sha256(
            str(mission_id).encode("utf-8", "replace")).hexdigest()[:16]

    def _ensure_agent_root(self, mission):
        """Lazily attach the least authority justified by a bound Mission workspace."""
        if not mission or self._run_tree is None:
            return ""
        case = dict(mission.case or {})
        current = str(case.get("_run_id") or "")
        if current and self._run_tree.get(current):
            return current
        run_id = self._agent_root_id(mission.mission_id)
        bound_workspace = str(case.get("_isolated_workspace") or "")
        if bound_workspace:
            bound_workspace = os.path.realpath(os.path.abspath(bound_workspace))
        is_bound = bool(bound_workspace and os.path.isdir(bound_workspace))
        workspace = bound_workspace if is_bound else ""
        workspace_mode = self._workspace_authority_mode(mission)
        resources = ([{"kind": "file", "id": workspace, "mode": workspace_mode}]
                     if is_bound else [])
        run = self._run_tree.get(run_id)
        if run:
            if (run.get("parent_run_id") or run.get("mission_id") != mission.mission_id or
                    run.get("task") != str(mission.goal)[:4000] or
                    run.get("leash") != dict(mission.leash or {}) or
                    run.get("workspace_mode") != "worktree"):
                raise ValueError("deterministic Mission root is bound to different authority")
            if is_bound and not run.get("workspace"):
                if run.get("resources"):
                    run = self._run_tree.bind_workspace(
                        run_id, workspace, owns_workspace=False)
                    if not run:
                        raise ValueError("deterministic Mission root workspace binding raced")
                else:
                    run = self._run_tree.initialize_root_workspace_authority(
                        run_id, workspace, workspace_mode)
            elif is_bound and (
                    os.path.normcase(os.path.realpath(run.get("workspace") or "")) !=
                    os.path.normcase(workspace)):
                raise ValueError("deterministic Mission root workspace conflicts with binding")
            elif not is_bound and run.get("workspace"):
                # Recover a host-created root whose TaskTree commit won before
                # the Mission case attachment committed.
                case["_isolated_workspace"] = run["workspace"]
        else:
            run = self._run_tree.create_root(
                mission.goal, mission.leash, resources, run_id=run_id,
                mission_id=mission.mission_id, workspace=workspace,
                workspace_mode="worktree")
        case["_run_id"] = run_id
        saved = (self.store.set_case_owned(mission.mission_id, mission.run_token, case)
                 if mission.run_token else self.store.set_case(mission.mission_id, case))
        if not saved:
            return ""
        self.store.record_checkpoint(
            mission.mission_id, mission.run_token, "run_tree_lazily_attached",
            {"run_id": run_id, "resources": run.get("resources") or []}, case=case,
            allow_unowned=not bool(mission.run_token))
        return run_id

    @staticmethod
    def _workspace_authority_mode(mission):
        may = list(((mission.leash if mission else {}) or {}).get("may") or [])
        return "write" if any(
            fnmatch.fnmatchcase("code", str(pattern)) for pattern in may) else "read"

    @staticmethod
    def _agent_verify(_record, result):
        if isinstance(result, dict) and result.get("ok"):
            return Verdict(VERIFY_VERIFIED, "scoped durable agent operation recorded")
        error = (result or {}).get("error") if isinstance(result, dict) else result
        return Verdict(VERIFY_FAILED, str(error or "agent operation was refused")[:500])

    def _agent_capabilities(self):
        """Model-facing graph primitives; every mutation is descendant-scoped."""
        def execute_spawn(record):
            args = _clean(record.args)
            if args.get("provider") or args.get("model"):
                return {"ok": False,
                        "error": "specialist provider/model is inherited and cannot be overridden"}
            if args.get("workspace"):
                return {"ok": False,
                        "error": "specialist workspace is provisioned by the container"}
            return self.agent_spawn(
                record.job_id, args.get("role") or "specialist", args.get("task") or "",
                leash=args.get("leash"), resources=args.get("resources"),
                operation_id=record.nonce)

        def execute_send(record):
            args = _clean(record.args)
            return self.agent_send(
                record.job_id, str(args.get("run_id") or ""),
                str(args.get("text") or ""))

        def execute_poll(record):
            args = _clean(record.args)
            return self.agent_poll(record.job_id, str(args.get("run_id") or ""))

        def execute_cancel(record):
            args = _clean(record.args)
            return self.agent_cancel(record.job_id, str(args.get("run_id") or ""))

        return [
            Capability(
                name="agent.spawn", execute=execute_spawn, verify=self._agent_verify,
                reversible=True, risk="read",
                description=("Delegate one scoped task to a durable specialist. Its leash, "
                             "resources, budgets, provider and depth can only inherit or narrow; "
                             "resources are scheduling authority (not a universal tool sandbox), "
                             "and after spawning, wait rather than polling in a tight loop."),
                args_hint='{"role","task","resources":[{"kind":"file","id":"...",'
                          '"mode":"read"}],"leash":{"may":["research"]}}'),
            Capability(
                name="agent.send", execute=execute_send, verify=self._agent_verify,
                reversible=True, risk="read",
                description="Send durable steering text to one descendant specialist.",
                args_hint='{"run_id","text"}'),
            Capability(
                name="agent.poll", execute=execute_poll, verify=self._agent_verify,
                reversible=True, risk="read",
                description=("Inspect descendant status and consume structured completed results; "
                             "completed children also wake a waiting parent automatically."),
                args_hint='{"run_id":"optional descendant; omit for whole subtree"}'),
            Capability(
                name="agent.cancel", execute=execute_cancel, verify=self._agent_verify,
                reversible=True, risk="read",
                description="Cancel one descendant specialist and all authority below it.",
                args_hint='{"run_id"}'),
        ]

    def _fold_child_results(self, mid, run_id, mission_token):
        """Fold, then ack: replay after a cross-database crash is harmless."""
        mission = self.store.get(mid)
        if (not mission or not mission_token or mission.run_token != mission_token or
                mission.state not in (RUNNING, PAUSING)):
            return 0
        messages = self._run_tree.claim_child_results(run_id, mid)
        if not messages:
            return 0
        case = dict(mission.case or {})
        stored_results = case.get("specialist_results")
        stored_results = stored_results if isinstance(stored_results, list) else []
        results = [dict(item) for item in stored_results if isinstance(item, dict)]
        known_at_entry = {int(item.get("message_id")) for item in results
                          if str(item.get("message_id") or "").isdigit()}
        known = set(known_at_entry)
        added = []
        for message in messages:
            message_id = int(message["message_id"])
            if message_id in known:
                continue
            payload = message.get("payload") or {}
            entry = {
                "message_id": message_id,
                "run_id": str(payload.get("run_id") or message.get("sender_run_id") or "")[:100],
                "mission_id": str(payload.get("mission_id") or "")[:100],
                "role": str(payload.get("role") or "specialist")[:80],
                "state": str(payload.get("state") or "")[:40],
                "result": str(payload.get("result") or "")[:4000],
                "artifacts": list(payload.get("artifacts") or [])[:12],
                "observation": payload.get("observation")
                               if isinstance(payload.get("observation"), dict) else {},
                "received_at": int(message.get("created_at") or time.time()),
            }
            results.append(entry)
            added.append(entry)
            known.add(message_id)
        if added:
            case["specialist_results"] = results[-20:]
            if not self.store.set_case_owned(mid, mission_token, case):
                return 0
        # If an id was present on entry, a prior case write already committed it.
        # Every newly added id is also safe once this call's set_case_owned
        # succeeds. Do not derive safety from the bounded results[-20:] view: a
        # replayed old id can be deliberately trimmed when it arrives alongside
        # newer outcomes and must still be acknowledged.
        safe_to_ack = known_at_entry | {item["message_id"] for item in added}
        for message in messages:
            if int(message["message_id"]) in safe_to_ack:
                self._run_tree.ack_child_result(
                    run_id, mid, int(message["message_id"]))
        if added:
            self.store.record_event(
                mid, "agent", "child_result",
                payload={"count": len(added),
                         "run_ids": [item["run_id"] for item in added]})
            self.store.record_checkpoint(
                mid, mission_token, "child_results_folded",
                {"message_ids": [item["message_id"] for item in added]}, case=case)
        return len(added)

    def _mission_control(self, mid):
        mission = self.store.get(mid)
        run_id = self._mission_run_id(mission)
        if mission and run_id and mission.run_token:
            self._fold_child_results(mid, run_id, mission.run_token)
        return {}

    def _wake_parents_with_child_results(self):
        """Wake event-driven waits; do not disturb pause, human or terminal gates."""
        if self._run_tree is None:
            return {"normal": 0, "specialists": 0}
        normal, specialists = 0, 0
        for run in self._run_tree.list_runs():
            mid = str(run.get("mission_id") or "")
            if not mid or not self._run_tree.has_child_results(run["run_id"], mid):
                continue
            mission = self.store.get(mid)
            if not mission or mission.state != WAITING:
                continue
            runtime = self.store.runtime(mid)
            if runtime.get("lane") == "specialist":
                if self._run_tree.requeue_waiting(run["run_id"]):
                    specialists += 1
            else:
                self._driver().wake(mid, force=True)
                normal += 1
        return {"normal": normal, "specialists": specialists}

    def _agent_completion_guard(self, mid, mission):
        """A parent cannot declare victory while delegated authority is still live."""
        if self._run_tree is None:
            return {}
        run_id = self._mission_run_id(mission)
        if not run_id:
            return {}
        return self._run_tree.completion_blocker(run_id, mid)

    def _linked_descendant_mission_ids(self, parent_mid, run_id):
        descendants = set()
        if self._run_tree is not None and run_id:
            for row in self._run_tree.tree(run_id).get("flat", []):
                child_mid = str(row.get("mission_id") or "")
                if child_mid and child_mid != parent_mid:
                    descendants.add(child_mid)
        known = {parent_mid}
        candidates = self.store.list()
        changed = True
        while changed:
            changed = False
            for child in candidates:
                linked_parent = str((child.case or {}).get("_parent_mission_id") or "")
                if linked_parent in known and child.mission_id not in known:
                    known.add(child.mission_id)
                    descendants.add(child.mission_id)
                    changed = True
        return descendants

    def _cancel_linked_descendant_missions(self, parent_mid, run_id, reason):
        """Mirror a failed TaskTree subtree fence into durable Mission rows."""
        descendants = self._linked_descendant_mission_ids(parent_mid, run_id)
        for child_mid in sorted(descendants):
            child = self.store.get(child_mid)
            if not child or child.state in (
                    DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
                continue
            self._cancel_record(
                child_mid, str(reason or "cancelled because ancestor Mission failed")[:4000],
                user_requested=False, parent_mission_id=parent_mid)
        return len(descendants)

    def _fence_failed_mission_tree(self, mid):
        mission = self.store.get(mid)
        if not mission or mission.state != FAILED_S or self._run_tree is None:
            return False
        run_id = self._mission_run_id(mission)
        if run_id:
            self._project_mission_usage(mid, run_id)
        run = self._run_tree.get(run_id) if run_id else None
        if run and not run.get("parent_run_id"):
            self._run_tree.fail_mission_root(
                run_id, mid, mission.result or "Mission failed")
        elif run:
            # A specialist Mission can commit FAILED just before its dispatcher
            # projects that outcome through the still-owned TaskTree lease.  A
            # restart has no safe owner token, so fence the whole subtree through
            # the durable cancellation protocol and require any live worker to ack.
            self._run_tree.request_cancel(run_id, run.get("parent_run_id") or "")
        linked = self._cancel_linked_descendant_missions(
            mid, run_id, "cancelled because ancestor Mission %s failed" % mid)
        return bool(run or linked)

    def _failed_mission_tree_needs_fence(self, mission):
        """Return true only when another reconciliation pass can change state."""
        if not mission or mission.state != FAILED_S or self._run_tree is None:
            return False
        run_id = self._mission_run_id(mission)
        run = self._run_tree.get(run_id) if run_id else None
        if run:
            for row in self._run_tree.tree(run_id).get("flat", []):
                status = row.get("status")
                if status not in ("completed", "failed", "cancelled", "cancel_requested"):
                    return True
        for child_mid in self._linked_descendant_mission_ids(
                mission.mission_id, run_id):
            child = self.store.get(child_mid)
            if child and child.state not in (
                    DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
                return True
        return False

    def _fence_failed_mission_trees(self, limit=64):
        fenced = 0
        for mission in self.store.list(state=FAILED_S):
            if not self._failed_mission_tree_needs_fence(mission):
                continue
            fenced += int(self._fence_failed_mission_tree(mission.mission_id))
            if fenced >= max(1, int(limit)):
                break
        return fenced

    def _complete_successful_mission_tree(self, mid):
        """Project an authoritative successful root Mission into TaskTree."""
        mission = self.store.get(mid)
        if (not mission or mission.state not in (DONE_VERIFIED, DONE_ACCEPTED) or
                self._run_tree is None):
            return False
        run_id = self._mission_run_id(mission)
        if not run_id:
            return False
        self._project_mission_usage(mid, run_id)
        run = self._run_tree.get(run_id)
        if not run or run.get("parent_run_id"):
            # Specialist runs are completed by their scoped dispatcher while it
            # still owns the TaskTree lease.  This is only the ownerless root seam.
            return False
        return self._run_tree.complete_mission_root(
            run_id, mid, mission.result or "Mission completed")

    def _sync_terminal_mission_tree(self, mid):
        mission = self.store.get(mid)
        if not mission:
            return False
        if mission.state == FAILED_S:
            return self._fence_failed_mission_tree(mid)
        if mission.state in (DONE_VERIFIED, DONE_ACCEPTED):
            return self._complete_successful_mission_tree(mid)
        return False

    def _complete_successful_mission_trees(self, limit=64):
        """Repair the crash window after Mission success but before root projection."""
        if self._run_tree is None:
            return 0
        completed = 0
        examined = 0
        for run in self._run_tree.list_runs():
            if (run.get("parent_run_id") or
                    run.get("status") in ("completed", "failed", "cancelled")):
                continue
            mid = str(run.get("mission_id") or "")
            mission = self.store.get(mid) if mid else None
            if not mission or mission.state not in (DONE_VERIFIED, DONE_ACCEPTED):
                continue
            examined += 1
            completed += int(self._complete_successful_mission_tree(mid))
            if examined >= max(1, int(limit)):
                break
        return completed

    def _sync_terminal_mission_trees(self, limit=64):
        return {
            "failed": self._fence_failed_mission_trees(limit),
            "completed": self._complete_successful_mission_trees(limit),
        }

    def _specialist_artifacts(self, run, child_mission):
        """Return references only, restricted to declared resources/workspace."""
        from .tasktree import normalize_artifact_refs
        case = child_mission.case or {}
        raw = []
        for value in (case.get("artifact_refs"), case.get("artifacts")):
            if isinstance(value, (list, tuple)):
                raw.extend(value)
            elif isinstance(value, (str, dict)):
                raw.append(value)
        refs = normalize_artifact_refs(raw)
        roots = []
        if run.get("workspace") and run.get("owns_workspace"):
            roots.append(os.path.realpath(run["workspace"]))
        for resource in run.get("resources") or []:
            if resource.get("kind") == "file":
                roots.append(os.path.realpath(str(resource.get("id") or "")))
        safe = []
        for ref in refs:
            uri = str(ref.get("uri") or "")
            if uri and not uri.lower().startswith(
                    ("collie://", "https://", "http://", "urn:")):
                continue
            if ref.get("path"):
                path = os.path.realpath(os.path.abspath(ref["path"]))
                try:
                    if not any(os.path.commonpath([root, path]) == root for root in roots):
                        continue
                except ValueError:
                    continue
                ref = dict(ref, path=path)
            safe.append(ref)
        safe.insert(0, {
            "kind": "specialist_result",
            "name": "%s result" % run.get("role", "specialist"),
            "uri": "collie://runs/%s" % run["run_id"],
        })
        if run.get("workspace") and run.get("owns_workspace"):
            safe.insert(0, {
                "kind": "workspace", "name": "%s output" % run.get("role", "specialist"),
                "uri": "collie://runs/%s/workspace" % run["run_id"],
                "path": os.path.realpath(run["workspace"]),
            })
        return normalize_artifact_refs(safe)

    # ── commands ──
    def start(self, goal: str, autonomous: bool | None = None,
              case: dict = None, **bounds) -> dict:
        """Persist first and return the id immediately; /run or the daemon claims it.

        ``None`` means use the user's Mission default.  Keeping this resolution at
        the service boundary makes Web, CLI, mobile and future surfaces agree;
        explicit True/False remains the per-Mission override and keeps API callers
        deterministic.
        """
        if autonomous is None:
            from . import settings
            autonomous = settings.get("MISSION_APPROVAL_MODE", "smart") == "smart"
        mid = "msn_" + secrets.token_hex(6)
        # Durable jobs get their own worktree by default.  The Web/CLI provisioner
        # binds its canonical path later through bind_workspace(); ordinary world
        # Missions pay no cost for this until they actually choose ``code``.
        bounds.setdefault("workspace_mode", "isolated")
        create_mission(self.store, mid, goal, case=case or {},
                       leash=world_leash(autonomous=autonomous, **bounds))
        return self.status(mid)

    def bind_workspace(self, mid: str, path: str) -> dict:
        """Bind an already-provisioned isolated worktree; never creates or deletes it."""
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if m.leash.get("workspace_mode") != "isolated":
            return {**self.status(mid), "error": "mission is not in isolated workspace mode"}
        if m.state in (RUNNING, PAUSING, RECONCILING):
            return {**self.status(mid), "error": "cannot rebind an active mission workspace"}
        canonical = os.path.realpath(os.path.abspath(str(path or "")))
        if not path or not os.path.isdir(canonical):
            return {**self.status(mid), "error": "isolated workspace does not exist"}
        run_id = str((m.case or {}).get("_run_id") or "")
        if self._run_tree is not None and not run_id:
            candidate_id = self._agent_root_id(mid)
            candidate = self._run_tree.get(candidate_id)
            if candidate:
                if (candidate.get("parent_run_id") or candidate.get("mission_id") != mid or
                        candidate.get("task") != str(m.goal)[:4000] or
                        candidate.get("leash") != dict(m.leash or {}) or
                        candidate.get("workspace_mode") != "worktree"):
                    return {**self.status(mid),
                            "error": "workspace authority refused: orphan root identity conflicts"}
                run_id = candidate_id
        if self._run_tree is not None and run_id:
            try:
                run = self._run_tree.get(run_id) or {}
                if run.get("resources"):
                    bound = self._run_tree.bind_workspace(
                        run_id, canonical, owns_workspace=False)
                    if not bound:
                        raise ValueError("declared root workspace is already bound elsewhere")
                else:
                    self._run_tree.initialize_root_workspace_authority(
                        run_id, canonical, self._workspace_authority_mode(m))
            except ValueError as exc:
                return {**self.status(mid), "error": "workspace authority refused: %s" % exc}
        case = dict(m.case)
        case["_isolated_workspace"] = canonical
        if run_id:
            case["_run_id"] = run_id
        if not self.store.set_case(mid, case):
            return {**self.status(mid),
                    "error": "workspace authority initialized but Mission binding raced"}
        self.store.record_checkpoint(
            mid, "", "workspace_bound", {"workspace": canonical},
            case=case, allow_unowned=True)
        return self.status(mid)

    def create_run_tree(self, mid: str, resources, workspace: str = "") -> dict:
        """Attach the durable specialist backend; provisioning remains an explicit seam."""
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if self._run_tree is None:
            return {**self.status(mid), "error": "no durable run-tree store configured"}
        if m.case.get("_run_id"):
            return self._run_tree.tree(m.case["_run_id"])
        run_id = self._agent_root_id(mid)
        try:
            run = self._run_tree.create_root(
                m.goal, m.leash, resources, run_id=run_id,
                mission_id=mid, workspace=workspace,
                workspace_mode="worktree")
        except ValueError as exc:
            return {**self.status(mid), "error": "run-tree creation refused: %s" % exc}
        case = dict(m.case)
        case["_run_id"] = run["run_id"]
        if workspace:
            case["_isolated_workspace"] = os.path.realpath(os.path.abspath(workspace))
        self.store.set_case(mid, case)
        self.store.record_checkpoint(
            mid, "", "run_tree_created", {"run_id": run["run_id"]},
            case=case, allow_unowned=True)
        self._project_mission_usage(mid, run["run_id"])
        return self._run_tree.tree(run["run_id"])

    def spawn_specialist(self, mid: str, role: str, task: str, *, leash=None,
                         resources=None, workspace: str = "") -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if m.state in (DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
            return {**self.status(mid),
                    "error": "terminal Mission cannot spawn a specialist"}
        run_id = self._mission_run_id(m)
        if self._run_tree is None or not run_id:
            return {**self.status(mid), "error": "mission has no durable run tree"}
        try:
            child = self._run_tree.spawn_specialist(
                run_id, role, task, leash=leash, resources=resources,
                workspace=workspace, workspace_mode="worktree")
            if workspace:
                child = self._create_specialist_mission(mid, child)
            return child
        except ValueError as exc:
            return {**self.status(mid), "error": str(exc)}

    def agent_spawn(self, mid: str, role: str, task: str, *, leash=None,
                    resources=None, operation_id: str = "") -> dict:
        """Container-provisioned model entry for ``agent.spawn``."""
        mission, parent, error = self._agent_caller(mid)
        if error:
            return {"ok": False, "error": error, "mission_id": mid}
        task = str(task or "").strip()[:4000]
        if not task:
            return {"ok": False, "error": "specialist task is empty", "mission_id": mid}
        if resources is None or not isinstance(resources, (list, tuple)):
            return {"ok": False,
                    "error": "agent.spawn requires an explicit resources list",
                    "mission_id": mid}
        try:
            from .tasktree import narrow_leash, normalize_resources
            scoped = normalize_resources(resources)
            effective_leash = narrow_leash(parent.get("leash") or {}, leash)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "mission_id": mid}
        physical_workspace = str(parent.get("workspace") or "")
        if not physical_workspace and not scoped:
            # Resource-free specialists may use cwd as an execution directory;
            # it is deliberately not persisted as root filesystem authority.
            physical_workspace = os.getcwd()
        if not physical_workspace:
            return {"ok": False,
                    "error": "calling run has no container-bound workspace",
                    "mission_id": mid}
        file_write = any(item.get("kind") == "file" and item.get("mode") == "write"
                         for item in scoped)
        if file_write:
            # The current code child is rooted at the whole isolated worktree. Until it can bind
            # several independent path roots, claiming that a subdirectory-only grant confines it
            # would be false authority. Require a write root that covers the source workspace.
            source_workspace = str(
                (mission.case or {}).get("_resource_source_workspace") or
                parent.get("workspace") or "")
            if not source_workspace:
                return {"ok": False,
                        "error": "file-writing specialist has no logical source workspace",
                        "mission_id": mid}
            workspace_root = os.path.normcase(os.path.realpath(source_workspace))
            covers_workspace = False
            for item in scoped:
                if item.get("kind") != "file" or item.get("mode") != "write":
                    continue
                try:
                    item_root = os.path.normcase(os.path.realpath(item.get("id") or ""))
                    covers_workspace = (os.path.commonpath(
                        [item_root, workspace_root]) == item_root)
                except (OSError, ValueError):
                    covers_workspace = False
                if covers_workspace:
                    break
            if not covers_workspace:
                return {
                    "ok": False,
                    "error": ("file-writing specialist needs a directory write resource "
                              "covering its full parent workspace; narrower scopes are not "
                              "yet enforceable by the isolated code runner"),
                    "mission_id": mid,
                }
        try:
            # Read-only specialists may share the stable parent checkout.  A file
            # writer starts unbound and can run only after the container provisions
            # an isolated git worktree.
            semantic = json.dumps({
                "parent": parent["run_id"], "role": str(role or "specialist")[:80],
                "task": task, "resources": scoped, "leash": effective_leash,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            # An action nonce identifies one intentional model operation. Semantic
            # content remains TaskTree's replay/collision check, but must not merge
            # two deliberate identical delegations. Direct host calls have no
            # action nonce, so retain semantic idempotency for that legacy seam.
            identity = (json.dumps({"parent": parent["run_id"],
                                    "operation_id": str(operation_id)},
                                   ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"))
                        if operation_id else semantic)
            stable_run_id = "run_agent_" + hashlib.sha256(
                identity.encode("utf-8", "replace")).hexdigest()[:16]
            child = self._run_tree.spawn_specialist(
                parent["run_id"], role, task, leash=leash, resources=scoped,
                workspace="" if file_write else physical_workspace,
                workspace_mode="worktree", run_id=stable_run_id)
            if child.get("status") in ("completed", "failed", "cancelled"):
                return {
                    "ok": child.get("status") == "completed",
                    "run_id": child["run_id"], "mission_id": child.get("mission_id") or "",
                    "parent_run_id": child["parent_run_id"], "role": child["role"],
                    "status": child["status"], "result": child.get("result") or "",
                    "replayed": True,
                }
            if file_write and not child.get("workspace"):
                prepared = self._run_tree.provision_worktree(
                    child["run_id"], physical_workspace)
                if prepared.get("busy"):
                    current = self._run_tree.get(child["run_id"]) or child
                    if current.get("status") in ("completed", "failed", "cancelled"):
                        return {
                            "ok": current.get("status") == "completed",
                            "run_id": current["run_id"],
                            "mission_id": current.get("mission_id") or "",
                            "parent_run_id": current["parent_run_id"],
                            "role": current["role"], "status": current["status"],
                            "result": current.get("result") or "", "replayed": True,
                        }
                    return {
                        "ok": True, "run_id": child["run_id"], "mission_id": "",
                        "parent_run_id": child["parent_run_id"], "role": child["role"],
                        "status": child["status"], "provisioning": True,
                        "resources": child.get("resources") or [],
                        "authority": {
                            "may": list((child.get("leash") or {}).get("may") or []),
                            "provider": "inherited"},
                    }
                if not prepared.get("ok"):
                    self._run_tree.cancel_descendant(parent["run_id"], child["run_id"])
                    return {"ok": False,
                            "error": str(prepared.get("error") or
                                         "isolated specialist worktree could not be provisioned")[:500],
                            "run_id": child["run_id"], "mission_id": mid}
                child = prepared.get("run") or self._run_tree.get(child["run_id"])
                if not child or not child.get("workspace"):
                    raise ValueError("isolated specialist workspace binding raced or was lost")
            child = self._create_specialist_mission(mid, child)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "mission_id": mid}
        return {
            "ok": True,
            "run_id": child["run_id"],
            "mission_id": child.get("mission_id") or "",
            "parent_run_id": child["parent_run_id"],
            "role": child["role"],
            "status": child["status"],
            "resources": child.get("resources") or [],
            "authority": {"may": list((child.get("leash") or {}).get("may") or []),
                          "provider": "inherited"},
        }

    def agent_send(self, mid: str, run_id: str, text: str) -> dict:
        mission, caller, error = self._agent_caller(mid)
        if error:
            return {"ok": False, "error": error, "mission_id": mid}
        if not run_id:
            return {"ok": False, "error": "agent.send requires run_id", "mission_id": mid}
        try:
            message_id = self._run_tree.send_to_descendant(
                caller["run_id"], run_id, text)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "mission_id": mid}
        if message_id is None:
            return {"ok": False, "error": "target specialist is terminal",
                    "run_id": run_id, "mission_id": mid}
        return {"ok": True, "run_id": run_id, "message_id": message_id,
                "queued": True}

    def agent_poll(self, mid: str, run_id: str = "") -> dict:
        mission, caller, error = self._agent_caller(mid)
        if error:
            return {"ok": False, "error": error, "mission_id": mid}
        target = run_id or caller["run_id"]
        if target != caller["run_id"] and not self._run_tree.is_descendant(
                caller["run_id"], target):
            return {"ok": False,
                    "error": "specialist target is outside caller descendant scope",
                    "mission_id": mid, "run_id": target}
        if mission.run_token:
            self._fold_child_results(mid, caller["run_id"], mission.run_token)
            mission = self.store.get(mid)
        tree = self._run_tree.tree(target)
        runs = [{
            "run_id": row["run_id"], "parent_run_id": row["parent_run_id"],
            "role": row["role"], "status": row["status"],
            "progress_seq": row["progress_seq"], "progress_at": row["progress_at"],
            "result": str(row.get("result") or "")[:1000],
        } for row in tree.get("flat", [])]
        visible = {row["run_id"] for row in runs}
        stored_results = (mission.case or {}).get("specialist_results")
        stored_results = stored_results if isinstance(stored_results, list) else []
        results = [item for item in stored_results
                   if isinstance(item, dict) and item.get("run_id") in visible]
        return {"ok": True, "run_id": target, "runs": runs,
                "results": results[-20:]}

    def agent_cancel(self, mid: str, run_id: str) -> dict:
        mission, caller, error = self._agent_caller(mid)
        if error:
            return {"ok": False, "error": error, "mission_id": mid}
        if not run_id:
            return {"ok": False, "error": "agent.cancel requires run_id", "mission_id": mid}
        if not self._run_tree.is_descendant(caller["run_id"], run_id):
            return {"ok": False,
                    "error": "specialist target is outside caller descendant scope",
                    "mission_id": mid, "run_id": run_id}
        target = self._run_tree.get(run_id)
        if target and target.get("status") in ("completed", "failed", "cancelled"):
            return {"ok": True, "run_id": run_id, "status": target["status"],
                    "already_terminal": True}
        try:
            changed = self._run_tree.cancel_descendant(caller["run_id"], run_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "mission_id": mid}
        current = self._run_tree.get(run_id)
        return {"ok": bool(changed), "run_id": run_id,
                "status": current.get("status") if current else "missing",
                **({} if changed else {"error": "target specialist is unavailable"})}

    def _create_specialist_mission(self, parent_mid, run):
        """Materialize a scoped specialist as a real Mission lane, not a TODO row."""
        workspace = run.get("workspace") or ""
        if not workspace:
            return run
        child_mid = "spc_" + run["run_id"].replace("run_", "")
        if run.get("mission_id") and run.get("mission_id") != child_mid:
            raise ValueError("specialist run is bound to a different Mission")
        # Reserve the deterministic cross-database identity first. A crash now
        # leaves a TaskTree row that the orphan pass can materialize, rather than
        # an unaddressable Mission row or an ambiguous second child.
        if not run.get("mission_id"):
            if not self._run_tree.bind_mission(run["run_id"], child_mid):
                current = self._run_tree.get(run["run_id"])
                if not current or current.get("mission_id") != child_mid:
                    raise ValueError("specialist Mission binding raced with another owner")
            run = self._run_tree.get(run["run_id"]) or run
        parent_run = self._run_tree.get(run.get("parent_run_id") or "") or {}
        parent_mid = str(parent_mid or parent_run.get("mission_id") or "")
        parent_mission = self.store.get(parent_mid)
        source_workspace = str(
            ((parent_mission.case or {}).get("_resource_source_workspace")
             if parent_mission else "") or parent_run.get("workspace") or "")
        existing = self.store.get(child_mid)
        if not existing:
            case = {
                "_isolated_workspace": workspace,
                "_specialist_run_id": run["run_id"],
                "_parent_mission_id": parent_mid,
                "_resource_scope": run.get("resources") or [],
                "_resource_source_workspace": source_workspace,
                "role": run.get("role") or "specialist",
            }
            try:
                create_mission(
                    self.store, child_mid, run["task"], case=case, leash=run["leash"],
                    lane="specialist", external_run_id=run["run_id"])
            except sqlite3.IntegrityError:
                # Another dispatcher may have repaired the same crash window.
                if not self.store.get(child_mid):
                    raise
        runtime = self.store.runtime(child_mid)
        if (str(runtime.get("external_run_id") or "") != run["run_id"] or
                str(runtime.get("parent_mission_id") or "") != str(parent_mid or "")):
            raise ValueError("specialist Mission id is bound to different authority")
        return self._run_tree.get(run["run_id"])

    def bind_specialist_workspace(self, run_id: str, path: str) -> dict:
        if self._run_tree is None:
            return {"error": "no durable run-tree store configured", "run_id": run_id}
        try:
            run = self._run_tree.bind_workspace(run_id, path, owns_workspace=False)
            if not run or not run.get("parent_run_id"):
                return {"error": "unknown specialist or workspace cannot be rebound",
                        "run_id": run_id}
            parent = self._run_tree.get(run["parent_run_id"])
            return self._create_specialist_mission(parent.get("mission_id") or "", run)
        except ValueError as exc:
            return {"error": str(exc), "run_id": run_id}

    def inspect_run_tree(self, mid: str) -> dict:
        """Return the durable tree for a Mission without initializing a model.

        An unattached Mission reports a usable backend and an empty tree instead
        of pretending the specialist feature is unavailable.  Root creation is
        still explicit because resources and a worktree are authority decisions.
        """
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        run_id = m.case.get("_run_id")
        usage = self._reconcile_tasktree_usage(mid)
        return {
            "mission_id": mid,
            "available": self._run_tree is not None,
            "attached": bool(run_id),
            "path": getattr(self._run_tree, "path", None),
            "tree": self._run_tree.tree(run_id) if self._run_tree and run_id
                    else {"root": None, "flat": []},
            "usage_projection_errors": usage["errors"],
        }

    def inspect_specialist(self, run_id: str, event_limit: int = 100) -> dict:
        """Inspect one run, its descendant tree and recent durable events."""
        if self._run_tree is None:
            return {"error": "no durable run-tree store configured", "run_id": run_id}
        run = self._run_tree.get(run_id)
        if not run:
            return {"error": "unknown specialist run", "run_id": run_id}
        usage = self._reconcile_tasktree_usage(run.get("mission_id") or "") \
            if run.get("mission_id") else {"errors": []}
        run = self._run_tree.get(run_id) or run
        return {"run": run, "tree": self._run_tree.tree(run_id),
                "events": self._run_tree.events(run_id, event_limit),
                "usage_projection_errors": usage["errors"]}

    def steer_specialist(self, run_id: str, text: str, sender_run_id: str = "") -> dict:
        """Queue a durable steer which is consumed at the next safe boundary."""
        if self._run_tree is None:
            return {"error": "no durable run-tree store configured", "run_id": run_id}
        try:
            message_id = self._run_tree.steer(run_id, text, sender_run_id)
        except ValueError as exc:
            return {"error": str(exc), "run_id": run_id}
        if message_id is None:
            return {"error": "unknown or terminal specialist run", "run_id": run_id}
        return {"run_id": run_id, "message_id": message_id, "queued": True}

    def cancel_specialist(self, run_id: str, sender_run_id: str = "") -> dict:
        """Request cancellation; a running worker acknowledges at a safe boundary."""
        if self._run_tree is None:
            return {"error": "no durable run-tree store configured", "run_id": run_id}
        if not self._run_tree.request_cancel(run_id, sender_run_id):
            return {"error": "unknown or terminal specialist run", "run_id": run_id}
        return self.inspect_specialist(run_id)

    def run(self, mid: str) -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        usage = self._reconcile_tasktree_usage(mid)
        if usage["errors"]:
            return {**self.status(mid),
                    "error": "usage reconciliation failed closed",
                    "usage_projection_errors": usage["errors"]}
        if self._specialist_run(mid):
            return {**self.status(mid),
                    "error": "specialist Missions run only through their scoped dispatcher"}
        if m.state != QUEUED:
            # Idempotent for the common Web-vs-daemon claim race: if somebody else
            # already advanced it, return the live state instead of a false failure.
            self._sync_terminal_mission_tree(mid)
            return self.status(mid)
        try:
            self._driver().advance(mid)
        except Exception as e:
            return {**self.status(mid), "error": f"run unavailable: {e}"}
        finally:
            self._reconcile_tasktree_usage(mid)
        self._sync_terminal_mission_tree(mid)
        return self.status(mid)

    def confirm(self, mid: str, nonce: str) -> dict:
        m = self.store.get(mid)
        name, parked = self.store.last_parked(mid) if m else (None, None)
        rec = self.actions.get(nonce)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if (m.state != NEEDS_YOU or not nonce or parked != nonce or not rec or
                rec.job_id != mid or rec.leash_id != mid):
            return {**self.status(mid), "error": "confirm refused: action does not belong to this mission"}
        specialist = self._specialist_run(mid)
        try:
            if specialist:
                if rec.state == PENDING:
                    self.actions.confirm(nonce)
                elif rec.state != APPROVED:
                    raise RefusedError("specialist action is not confirmable")
                self._run_tree.resume(specialist["run_id"])
                self._tick_specialists(int(time.time()))
            else:
                self._driver().confirm_and_resume(mid, nonce)
        except (RefusedError, RuntimeError) as e:
            return {**self.status(mid), "error": f"confirm refused: {e}"}
        self._sync_terminal_mission_tree(mid)
        return self.status(mid)

    def resume(self, mid: str) -> dict:
        """Lifecycle resume means only PAUSED -> the state it came from."""
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        target = self.store.resume_paused(mid)
        specialist = self._specialist_run(mid)
        if target and specialist:
            self._run_tree.resume(specialist["run_id"])
        return self.status(mid) if target else {
            **self.status(mid), "error": f"cannot resume from {m.state}"}

    def retry(self, mid: str, note: str = "") -> dict:
        """Create a fenced successor for an ordinarily failed Mission.

        A failed row is immutable audit history, so retry never rewinds it.  The
        successor inherits the exact leash and receives bounded predecessor
        context plus receipts so the decider can reconcile already-fired work
        instead of repeating it.  Any still-executing action/resource refuses the
        retry: that is outcome-uncertain recovery, not an ordinary retry.
        """
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if m.state != FAILED_S:
            return {**self.status(mid), "error": f"cannot retry from {m.state}"}
        # Repair the cross-store crash window before even considering a successor.
        # Once the failed root is fenced, no new descendants can be added; running
        # workers must durably acknowledge cancellation before retry is safe.
        self._fence_failed_mission_tree(mid)
        predecessor_run_id = self._mission_run_id(m)
        if predecessor_run_id and self._run_tree is not None:
            unsettled = [row for row in self._run_tree.tree(predecessor_run_id).get("flat", [])
                         if row.get("status") not in
                         ("completed", "failed", "cancelled")]
            if unsettled:
                summary = ", ".join("%s:%s" % (
                    row.get("role") or "specialist", row.get("status") or "unknown")
                                    for row in unsettled[:6])
                return {
                    **self.status(mid),
                    "error": "cannot retry until predecessor specialists settle (%s)" % summary,
                }
        active_resources = self.store.active_resources(mid)
        live_actions = [r for r in self.actions.list()
                        if r.get("job_id") == mid and r.get("state") == EXECUTING]
        # A timed-out reversible child can outlive the process that owned its ActionStore latch.
        # Once the Mission is terminal, has no run token/resource lease, and the latch is older than
        # the action watchdog, retire only the explicitly safe capability set. Consequential actions
        # remain outcome-uncertain forever until inspected through the recovery path.
        if live_actions and not m.run_token and not active_resources:
            min_age = max(60, int((m.leash or {}).get("max_step_seconds", 600)))
            for row in live_actions:
                nonce = str(row.get("nonce") or "")
                if self.actions.retire_stale_reversible(
                        nonce, min_age_s=min_age,
                        reason="stale reversible execution retired before failed-Mission retry"):
                    self.store.record_event(
                        mid, "watchdog", "stale_reversible_retired", nonce=nonce,
                        payload={"capability": row.get("capability"), "min_age_seconds": min_age})
            live_actions = [r for r in self.actions.list()
                            if r.get("job_id") == mid and r.get("state") == EXECUTING]
        if live_actions or active_resources:
            return {**self.status(mid),
                    "error": "cannot retry while predecessor action outcome is uncertain"}

        receipts = [r for r in self.actions.receipts()
                    if r.get("job_id") == mid][-40:]
        receipt_context = [{
            "capability": r.get("capability"),
            "fired": bool(r.get("fired")),
            "verdict": r.get("verdict"),
            "reason": str(r.get("verdict_reason") or "")[:500],
            "evidence": str(r.get("evidence") or "")[:1000],
        } for r in receipts]
        now = int(time.time())
        retry_note = str(note or "").strip()[:2000]
        case = {
            "_retry_of": mid,
            "predecessor": {
                "mission_id": mid,
                "state": m.state,
                "result": str(m.result or "")[:2000],
                "receipts": receipt_context,
                # Namespacing keeps stale browser state from being mistaken for
                # the successor's current page while retaining useful research
                # and composed copy for recovery.
                "case": _clean(m.case),
            },
            "human_updates": [{
                "at": now,
                "recovery": True,
                "note": retry_note or (
                    "Retry the failed predecessor. Inspect predecessor receipts "
                    "before every external action and never duplicate fired work."),
            }],
        }
        # A failed row is immutable, but the successor must retain durable
        # control-plane contracts at top level. Keeping these only inside the
        # namespaced predecessor makes the planner lose campaign coverage and
        # branch-scoped authorizations exactly when recovery is most important.
        for key in (
                "_campaign_coverage", "pending_authorizations",
                "resolved_authorizations", "pending_followups",
                "_due_followups", "resolved_followups"):
            value = (m.case or {}).get(key)
            if isinstance(value, (list, dict)):
                case[key] = json.loads(json.dumps(value, ensure_ascii=False))
        successor = "msn_" + secrets.token_hex(6)
        create_mission(self.store, successor, m.goal, case=case, leash=dict(m.leash))
        inherited = self.store.inherit_completed_action_keys(mid, successor)
        self.store.record_event(
            successor, "control", "retry",
            payload={"predecessor": mid, "note": retry_note,
                     "inherited_action_keys": inherited})
        self.store.record_checkpoint(
            successor, "", "retried",
            {"predecessor": mid, "receipts": len(receipt_context)},
            case=case, allow_unowned=True)
        return self.status(successor)

    def pause(self, mid: str) -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        changed = self.store.pause(mid)
        return self.status(mid) if changed or m.state in (PAUSED, PAUSING) else {
            **self.status(mid), "error": f"cannot pause from {m.state}"}

    def reconcile(self, mid: str, note: str = "") -> dict:
        """Acknowledge a crash-uncertain external action after manual inspection."""
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if m.state not in (RECOVERY_REQUIRED, RECONCILING):
            return {**self.status(mid), "error": f"cannot reconcile from {m.state}"}
        # Snapshot exact action identities while the Mission is still fenced in a
        # non-runnable recovery state.  Never use a later broad job-id update: a
        # cleanup owner can stall past its lease, another owner can finish, and a
        # fresh run can then create a new action before the stale caller wakes.
        # Exact old nonces remain safe to inspect/refuse in that case.
        candidates = [r.get("nonce") for r in self.actions.list()
                      if r.get("job_id") == mid and
                      r.get("state") in (PENDING, APPROVED, REFUSED, EXPIRED)]
        # Validate and CAS the lifecycle state before touching the separate
        # ActionStore.  In particular, an accidental reconcile against an
        # ordinary needs_you Mission must be completely side-effect free.
        reconcile_token = self.store.begin_reconcile(mid, note)
        if not reconcile_token:
            return {**self.status(mid), "error": f"cannot reconcile from {m.state}"}
        try:
            if not self.store.owns_reconcile(mid, reconcile_token):
                return {**self.status(mid),
                        "error": "reconciliation ownership expired; inspect status before retrying"}
            # Anything in the pre-fence snapshot that is still unclaimed is safe
            # to revoke. An APPROVED row may concurrently become EXECUTING; the
            # ActionStore CAS then refuses nothing and its idempotency key stays.
            for nonce in candidates:
                rec = self.actions.get(nonce)
                if rec and rec.state in (PENDING, APPROVED):
                    self.actions.refuse(
                        nonce, "superseded by explicit recovery reconciliation")
            safely_refused = []
            for nonce in candidates:
                rec = self.actions.get(nonce)
                if rec and rec.state in (REFUSED, EXPIRED):
                    safely_refused.append(nonce)
            self.store.release_action_nonces(mid, safely_refused)

            resources = self.store.active_resources(mid)
            if resources:
                self.store.release_reconcile(mid, reconcile_token)
                return {**self.status(mid),
                        "error": "an old external action is still executing; retry reconcile after it settles"}
            if not self.store.finish_reconcile(mid, reconcile_token):
                self.store.release_reconcile(mid, reconcile_token)
                return {**self.status(mid),
                        "error": "reconciliation changed concurrently; inspect status before retrying"}
            return self.status(mid)
        except Exception:
            self.store.release_reconcile(mid, reconcile_token)
            raise

    def _cancel_record(self, mid, reason, *, user_requested, parent_mission_id=""):
        """Cancel one Mission row and its pending authority; safe to repeat after a partial retry."""
        mission = self.store.get(mid)
        if not mission or mission.state in (DONE_VERIFIED, DONE_ACCEPTED, FAILED_S):
            return False
        if mission.state != CANCELLED and self._hooks is not None:
            payload = {"mission_id": mid, "state": CANCELLED, "result": reason,
                       "user_requested": bool(user_requested)}
            if parent_mission_id:
                payload["parent_mission_id"] = parent_mission_id
            try:
                self._hooks.dispatch("Stop", payload, subject=CANCELLED)
            except Exception:
                pass  # cancellation is a safety boundary and cannot be vetoed by an audit hook
        self.store.cancel(mid, reason)
        self.actions.refuse_for_job(mid, "mission cancelled")
        _name, nonce = self.store.last_parked(mid)
        if nonce:
            self.store.resolve_parked(nonce, CANCELLED)
        return True

    def cancel(self, mid: str) -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if m.state in (DONE_VERIFIED, DONE_ACCEPTED, FAILED_S):
            return {**self.status(mid), "error": f"cannot cancel terminal mission ({m.state})"}
        # Snapshot both representations before changing either database. A Mission may be the root
        # of its own tree, a specialist inside another tree, or (at the depth limit) both.
        tree_targets = set()
        for key in ("_run_id", "_specialist_run_id"):
            if str((m.case or {}).get(key) or ""):
                tree_targets.add(str(m.case[key]))
        specialist = self._specialist_run(mid)
        if specialist:
            tree_targets.add(specialist["run_id"])
        descendant_missions = set()
        tree_errors = []
        live_specialist = False

        def collect_bound_missions(run_id):
            nonlocal live_specialist
            try:
                for row in self._run_tree.tree(run_id).get("flat", []):
                    if row.get("status") in ("running", "cancel_requested"):
                        live_specialist = True
                    child_mid = str(row.get("mission_id") or "")
                    if child_mid and child_mid != mid:
                        descendant_missions.add(child_mid)
            except Exception as exc:
                tree_errors.append("%s: %s" % (type(exc).__name__, exc))

        def collect_linked_missions():
            """Follow Mission parent links too, including records not yet bound into the tree."""
            nonlocal live_specialist
            known_parents = {mid}
            candidates = self.store.list()
            changed = True
            while changed:
                changed = False
                for child in candidates:
                    parent_mid = str((child.case or {}).get("_parent_mission_id") or "")
                    if parent_mid in known_parents and child.mission_id not in known_parents:
                        known_parents.add(child.mission_id)
                        descendant_missions.add(child.mission_id)
                        if child.state in (RUNNING, PAUSING):
                            live_specialist = True
                        changed = True

        if self._run_tree is not None:
            for run_id in sorted(tree_targets):
                collect_bound_missions(run_id)
        collect_linked_missions()

        reason = ("cancelled; an in-flight action may still finish"
                  if m.state in (RUNNING, PAUSING) or live_specialist else
                  "cancelled by user")
        self._cancel_record(mid, reason, user_requested=True)

        # This operation is transactionally subtree-wide. Queued descendants become terminal;
        # running descendants are fenced and receive a durable cancel message for their next safe
        # boundary. Re-read afterwards to include a child that won a spawn race before the fence.
        if self._run_tree is not None:
            for run_id in sorted(tree_targets):
                try:
                    self._run_tree.request_cancel(run_id)
                except Exception as exc:
                    tree_errors.append("%s: %s" % (type(exc).__name__, exc))
                collect_bound_missions(run_id)

        # Repeat after fencing the tree to include a child creation that committed just before the
        # cancellation transaction. The atomic parent-state check in MissionStore rejects one that
        # tries to commit after this Mission became terminal.
        collect_linked_missions()
        for child_mid in sorted(descendant_missions):
            child = self.store.get(child_mid)
            if not child or child.state in (DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
                continue
            child_reason = ("cancelled with parent mission; an in-flight action may still finish"
                            if child.state in (RUNNING, PAUSING) else
                            "cancelled with parent mission")
            self._cancel_record(
                child_mid, child_reason, user_requested=False, parent_mission_id=mid)

        result = self.status(mid)
        if tree_errors:
            result["error"] = ("mission cancelled, but specialist cancellation needs retry: " +
                               "; ".join(dict.fromkeys(tree_errors))[:500])
        return result

    def accept(self, mid: str) -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        _name, nonce = self.store.last_parked(mid)
        if m.state != NEEDS_YOU or nonce:
            return {**self.status(mid), "error": f"cannot accept from {m.state}"}
        blocked = self._agent_completion_guard(mid, m)
        if blocked:
            reason = (blocked.get("reason") if isinstance(blocked, dict) else str(blocked))
            return {
                **self.status(mid),
                "error": "cannot accept while delegated work is unsettled: %s" %
                         str(reason or "unfinished delegated work remains")[:500],
            }
        if not self.store.accept_handoff(mid):
            return {**self.status(mid), "error": f"cannot accept from {m.state}"}
        specialist = self._specialist_run(mid)
        if specialist:
            self._run_tree.resume(specialist["run_id"])
            self._tick_specialists(int(time.time()))
        if self._hooks is not None:
            try:
                self._hooks.dispatch(
                    "Stop", {"mission_id": mid, "state": DONE_ACCEPTED,
                             "result": "accepted by user", "user_requested": True},
                    subject=DONE_ACCEPTED)
            except Exception:
                pass
        self._sync_terminal_mission_tree(mid)
        return self.status(mid)

    def continue_after_human(self, mid: str, note: str = "") -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if m.state == DONE_ACCEPTED:
            # Acceptance is immutable audit history. Returning work to Collie
            # therefore creates a successor rather than falsifying that terminal
            # record, while inherited semantic keys prevent replay of fired work.
            live_actions = [r for r in self.actions.list()
                            if r.get("job_id") == mid and r.get("state") == EXECUTING]
            if live_actions or self.store.active_resources(mid):
                return {**self.status(mid),
                        "error": "cannot return while predecessor action outcome is uncertain"}
            prior_receipts = [r for r in self.actions.receipts()
                              if r.get("job_id") == mid][-40:]
            receipt_context = [{
                "capability": r.get("capability"),
                "fired": bool(r.get("fired")),
                "verdict": r.get("verdict"),
                "reason": _short(r.get("verdict_reason"), 500),
                "evidence": _short(r.get("evidence"), 1000),
            } for r in prior_receipts]
            now = int(time.time())
            continuation_note = _short(note, 2000) or (
                "Return control to Collie. Inspect predecessor receipts before every "
                "external action and never duplicate fired work.")
            case = {
                "_continued_from": mid,
                "predecessor": {
                    "mission_id": mid,
                    "state": m.state,
                    "result": _short(m.result, 2000),
                    "receipts": receipt_context,
                    "case": _clean(m.case),
                },
                "human_updates": [{"at": now, "recovery": True,
                                   "note": continuation_note}],
            }
            successor = "msn_" + secrets.token_hex(6)
            create_mission(self.store, successor, m.goal, case=case, leash=dict(m.leash))
            inherited = self.store.inherit_completed_action_keys(mid, successor)
            self.store.record_event(
                successor, "control", "return_to_collie",
                payload={"predecessor": mid, "note": continuation_note,
                         "inherited_action_keys": inherited})
            self.store.record_checkpoint(
                successor, "", "continued",
                {"predecessor": mid, "receipts": len(receipt_context),
                 "inherited_action_keys": inherited},
                case=case, allow_unowned=True)
            return self.status(successor)
        _name, nonce = self.store.last_parked(mid)
        if m.state != NEEDS_YOU or nonce or not self.store.continue_handoff(mid, note):
            return {**self.status(mid), "error": f"cannot continue from {m.state}"}
        specialist = self._specialist_run(mid)
        if specialist:
            self._run_tree.resume(specialist["run_id"])
            self._tick_specialists(int(time.time()))
        return self.status(mid)

    def check(self, mid: str) -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if m.state != WAITING:
            return {**self.status(mid), "error": f"cannot check from {m.state}"}
        try:
            specialist = self._specialist_run(mid)
            if specialist:
                self._run_tree.requeue_waiting(specialist["run_id"])
                self._tick_specialists(int(time.time()))
            else:
                self._driver().wake(mid, force=True)
        except Exception as e:
            return {**self.status(mid), "error": f"check unavailable: {e}"}
        self._sync_terminal_mission_tree(mid)
        return self.status(mid)

    def tick(self, mid: str = None, now=None) -> dict:
        """Fire any due durable re-checks now (the 'check inbox now' button; also
        what colliejobd calls on wake)."""
        import time
        at = int(now if now is not None else time.time())
        usage_reconciliation = self._reconcile_tasktree_usage()
        recovered = self.store.recover_stale_runs(at)
        escalations = self.store.escalate_human_waits(at)
        specialists = 0
        parent_wakes = {"normal": 0, "specialists": 0}
        # One child can wake a waiting specialist which then completes and wakes
        # its own parent. Depth is leash-bounded; four passes cover the default
        # graph while retaining a hard dispatcher bound.
        for _ in range(4):
            specialists += self._tick_specialists(at)
            woke = self._wake_parents_with_child_results()
            parent_wakes["normal"] += woke["normal"]
            parent_wakes["specialists"] += woke["specialists"]
            if not woke["specialists"]:
                break
        self._sync_terminal_mission_trees()
        if not self.store.list(state=QUEUED) and not self.store.due_waits(at):
            if mid:
                return {**self.status(mid), "escalations": [e for e in escalations
                                                              if e["mission_id"] == mid]}
            return {"advanced": 0, "specialists_advanced": specialists,
                    "parents_resumed": parent_wakes,
                    "recovered": recovered,
                    "usage_reconciliation": usage_reconciliation,
                    "escalations": escalations}
        n = self._driver().tick_missions(at, max_workers=self._mission_workers)
        usage_reconciliation = self._reconcile_tasktree_usage()
        self._sync_terminal_mission_trees()
        if mid:
            return {**self.status(mid), "escalations": [e for e in escalations
                                                         if e["mission_id"] == mid]}
        return {"advanced": n, "specialists_advanced": specialists,
                "parents_resumed": parent_wakes,
                "recovered": recovered,
                "usage_reconciliation": usage_reconciliation,
                "escalations": escalations}

    def _specialist_control(self, run_id, token):
        run = self._run_tree.get(run_id)
        child_mid = str((run or {}).get("mission_id") or "")
        child = self.store.get(child_mid) if child_mid else None
        if child and child.run_token:
            self._fold_child_results(child_mid, run_id, child.run_token)
        messages = self._run_tree.claim_messages(run_id, token)
        steers = []
        for message in messages:
            if message["kind"] == "steer":
                text = (message.get("payload") or {}).get("text")
                if text:
                    steers.append(text)
                self._run_tree.ack_message(run_id, token, message["message_id"])
        return {"cancel": bool(run and run.get("cancel_requested")), "steers": steers}

    def _run_specialist(self, run, token):
        run_id, child_mid = run["run_id"], run.get("mission_id") or ""
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(20):
                if not self._run_tree.renew(run_id, token):
                    return

        beat = threading.Thread(target=heartbeat, name="specialist-heartbeat", daemon=True)
        beat.start()
        try:
            if not child_mid or not self.store.get(child_mid):
                self._run_tree.block(
                    run_id, token,
                    "specialist runner has no bound Mission/worktree", needs_you=True)
                return
            # Catch up any Mission accounting committed before an earlier process
            # died.  Reconcile the whole campaign (including the root Mission's
            # own usage) before the TaskTree ancestor budget gate.
            root_run = self._run_tree.get(run.get("root_run_id") or "") or run
            usage = self._reconcile_tasktree_usage(
                root_run.get("mission_id") or child_mid)
            if usage["errors"]:
                raise RuntimeError("usage reconciliation failed closed: %s" %
                                   usage["errors"][0]["error"])
            exhausted = list(dict.fromkeys(tuple(item) for item in usage["exhausted"]))
            budget = "; ".join("%s: %s" % item for item in exhausted) or \
                self._run_tree.budget_reason(run_id)
            if budget:
                self._run_tree.block(run_id, token, budget, needs_you=True)
                return
            driver = self._driver(
                lane="specialist",
                control=lambda _mid: self._specialist_control(run_id, token))
            child = self.store.get(child_mid)
            if child.state == QUEUED:
                state = driver.advance(child_mid)
            elif child.state == WAITING:
                state = driver.wake(
                    child_mid,
                    force=(self._run_tree.has_child_results(run_id, child_mid) or
                           self._run_tree.has_messages(run_id, "steer")))
            elif child.state == NEEDS_YOU:
                _name, nonce = self.store.last_parked(child_mid)
                record = self.actions.get(nonce) if nonce else None
                state = driver.resume(child_mid) if record and record.state == APPROVED \
                    else child.state
            else:
                state = child.state
            exhausted = self._project_mission_usage(child_mid, run_id)
            current_run = self._run_tree.get(run_id) or {}
            if (current_run.get("status") == "cancel_requested" or
                    current_run.get("cancel_requested")):
                self._cancel_record(
                    child_mid,
                    "cancelled at specialist execution boundary; no new code action started",
                    user_requested=False,
                    parent_mission_id=str((self.store.get(child_mid).case or {}).get(
                        "_parent_mission_id") or ""))
                self._run_tree.cancel_owned(
                    run_id, token, "cancelled at specialist execution boundary")
                return
            if exhausted and state not in (DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
                self._run_tree.block(
                    run_id, token,
                    "; ".join("%s: %s" % item for item in exhausted), needs_you=True)
            elif state in (DONE_VERIFIED, DONE_ACCEPTED):
                child_record = self.store.get(child_mid)
                if not self._run_tree.complete(
                        run_id, token, child_record.result,
                        artifacts=self._specialist_artifacts(run, child_record),
                        observation={"mission_state": state,
                                     "verified": state == DONE_VERIFIED,
                                     "accepted": state == DONE_ACCEPTED}):
                    self._run_tree.block(
                        run_id, token, "TaskCompleted hook blocked specialist completion",
                        needs_you=True)
            elif state == FAILED_S:
                self._run_tree.fail(run_id, token, self.store.get(child_mid).result)
                self._cancel_linked_descendant_missions(
                    child_mid, run_id,
                    "cancelled because ancestor Mission %s failed" % child_mid)
            elif state == CANCELLED:
                self._run_tree.cancel_owned(run_id, token, self.store.get(child_mid).result)
            elif state == WAITING:
                self._run_tree.park_waiting(run_id, token, self.store.get(child_mid).result)
            elif state == NEEDS_YOU:
                self._run_tree.block(
                    run_id, token, self.store.get(child_mid).result, needs_you=True)
            elif state == PAUSED:
                self._run_tree.block(run_id, token, self.store.get(child_mid).result)
            elif state in (RECOVERY_REQUIRED, RECONCILING, RUNNING, PAUSING):
                self._run_tree.mark_recovery(
                    run_id, token,
                    self.store.get(child_mid).result or
                    "specialist stopped at an uncertain execution boundary")
            else:
                self._run_tree.block(
                    run_id, token, "specialist stopped in %s" % state, needs_you=True)
        except Exception as exc:
            self._run_tree.mark_recovery(
                run_id, token, "specialist dispatcher failed: %s: %s" %
                (type(exc).__name__, exc))
        finally:
            if child_mid and self.store.get(child_mid):
                try:
                    # Idempotent absolute reconciliation also covers a driver
                    # exception after Mission accounting committed.
                    self._project_mission_usage(child_mid, run_id)
                except Exception as exc:
                    self._run_tree.mark_recovery(
                        run_id, token, "usage projection failed: %s: %s" %
                        (type(exc).__name__, exc))
            stop.set()
            beat.join(timeout=2)

    def _reconcile_specialist_orphans(self, limit):
        """Repair bounded spawn crash windows before any specialist claim."""
        if self._run_tree is None:
            return 0
        from .tasktree import (CANCEL_REQUESTED as T_CANCEL_REQUESTED,
                               CANCELLED as T_CANCELLED, COMPLETED as T_COMPLETED,
                               FAILED as T_FAILED, WORKSPACE_REQUIRED)
        repaired = 0
        candidates = []
        for run in self._run_tree.list_runs(specialists_only=True):
            if (run.get("status") in (T_COMPLETED, T_FAILED, T_CANCELLED,
                                      T_CANCEL_REQUESTED) or
                    run.get("cancel_requested")):
                continue
            if (run.get("status") == WORKSPACE_REQUIRED or
                    (run.get("workspace") and
                     (not run.get("mission_id") or
                      not self.store.get(run.get("mission_id") or "")))):
                candidates.append(run)
            if len(candidates) >= max(1, int(limit)):
                break
        for candidate in candidates:
            run_id = candidate["run_id"]
            phase = "workspace" if candidate.get("status") == WORKSPACE_REQUIRED \
                else "mission"
            try:
                run = self._run_tree.get(run_id) or candidate
                parent = self._run_tree.get(run.get("parent_run_id") or "") or {}
                parent_mid = str(parent.get("mission_id") or "")
                if run.get("status") == WORKSPACE_REQUIRED:
                    parent_workspace = str(parent.get("workspace") or "")
                    if not parent_workspace:
                        raise ValueError("parent workspace is unavailable for worktree recovery")
                    prepared = self._run_tree.provision_worktree(
                        run_id, parent_workspace)
                    if prepared.get("busy"):
                        continue
                    if not prepared.get("ok"):
                        raise ValueError(str(
                            prepared.get("error") or
                            "isolated specialist worktree could not be recovered"))
                    run = prepared.get("run") or self._run_tree.get(run_id)
                    phase = "mission"
                if not run or not run.get("workspace"):
                    raise ValueError("specialist workspace recovery did not bind a workspace")
                if (not run.get("mission_id") or
                        not self.store.get(run.get("mission_id") or "")):
                    run = self._create_specialist_mission(parent_mid, run)
                if not run or not run.get("mission_id"):
                    raise ValueError("specialist Mission recovery did not bind a Mission")
                repaired += 1
            except Exception as exc:
                self._run_tree.mark_orphan_needs_you(
                    run_id, "specialist orphan recovery failed: %s: %s" %
                    (type(exc).__name__, exc), phase=phase)
        return repaired

    def _tick_specialists(self, now):
        """Claim and actually execute scoped child Missions; never strand queued rows."""
        if self._run_tree is None:
            return 0
        from .tasktree import (BLOCKED as T_BLOCKED, NEEDS_YOU as T_NEEDS_YOU,
                               PAUSED as T_PAUSED, QUEUED as T_QUEUED,
                               RECOVERY_REQUIRED as T_RECOVERY, WAITING as T_WAITING)
        workers = self._specialist_workers if self._specialist_workers is not None else \
            int(os.environ.get("COLLIE_SPECIALIST_WORKERS", "4"))
        workers = max(1, min(8, int(workers)))
        self._reconcile_specialist_orphans(workers)
        # Mirror explicit child-Mission recovery/continue commands back into the
        # run tree, and wake only when the child's durable timer is due.
        for run in self._run_tree.list_runs(
                (T_WAITING, T_BLOCKED, T_NEEDS_YOU, T_PAUSED, T_RECOVERY),
                specialists_only=True):
            child = self.store.get(run.get("mission_id") or "")
            if not child:
                continue
            if run["status"] == T_WAITING:
                wake = self.store.next_wait(child.mission_id)
                if child.state == WAITING and wake and int(wake["fire_at"]) <= int(now):
                    self._run_tree.requeue_waiting(run["run_id"])
            elif child.state == QUEUED:
                if run["status"] == T_RECOVERY:
                    self._run_tree.reconcile(run["run_id"], "child Mission reconciled")
                else:
                    self._run_tree.resume(run["run_id"])
        queued = self._run_tree.list_runs(T_QUEUED, specialists_only=True)
        claimed = []
        for run in queued[:workers]:
            lease = max(300, int(float(run["leash"].get("max_step_seconds", 600))) + 60)
            token = self._run_tree.claim(run["run_id"], lease_s=lease)
            if token:
                claimed.append((self._run_tree.get(run["run_id"]), token))
        if len(claimed) == 1:
            self._run_specialist(*claimed[0])
        elif claimed:
            with ThreadPoolExecutor(max_workers=len(claimed),
                                    thread_name_prefix="specialist") as pool:
                futures = [pool.submit(self._run_specialist, run, token)
                           for run, token in claimed]
                for future in as_completed(futures):
                    future.result()
        return len(claimed)

    # ── read ──
    def status(self, mid: str) -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        usage_reconciliation = self._reconcile_tasktree_usage(mid)
        inbox = None
        action_in_flight = False
        if m.state == NEEDS_YOU:
            name, nonce = self.store.last_parked(mid)
            if nonce:                             # a gated action awaiting confirm
                rec = self.actions.get(nonce)
                if rec and rec.expires_at and int(time.time()) > rec.expires_at:
                    self.actions.refuse(nonce, "expired before Mission confirmation")
                    rec = self.actions.get(nonce)
                # A concurrent confirm changes NEEDS_YOU -> RUNNING in the other
                # database.  Re-read before interpreting the ActionStore record;
                # a stale status request must never detach an executing/executed
                # action's durable idempotency key.
                latest = self.store.get(mid)
                if latest and (latest.state != NEEDS_YOU or
                               latest.run_token != m.run_token):
                    m = latest
                elif rec and rec.state in (PENDING, APPROVED):
                    inbox = {"nonce": nonce, "capability": name,
                             "args": _clean(rec.args), "target": rec.snapshot or None,
                             "action_state": rec.state}
                elif rec and rec.state in (REFUSED, EXPIRED):
                    # These two states prove the single-use latch never fired, so
                    # a freshly prepared payload may safely get a new semantic key.
                    self.store.resolve_parked(nonce, rec.state if rec else "missing")
                    self.store.release_action_nonces(mid, [nonce])
                else:
                    # EXECUTING, EXECUTED, a corrupt/missing row, or an unknown
                    # state is outcome-uncertain. Preserve the key and suppress
                    # Continue/Accept until the live owner finishes or recovery
                    # reconciliation/cancellation fences it.
                    action_in_flight = True
                    if rec and rec.state == EXECUTED:
                        self.store.complete_action_key(mid, nonce, EXECUTED)
        next_wait = self.store.next_wait(mid)
        runtime = self.store.runtime(mid)
        aggregate_runtime = self.store.aggregate_runtime(mid)
        activity = self.store.activity_ledger(mid, 24)
        checkpoint = self.store.latest_checkpoint(mid)
        run_tree = None
        if self._run_tree and m.case.get("_run_id"):
            run_tree = self._run_tree.tree(m.case["_run_id"])
        pending_hooks = list(getattr(self._hooks, "pending", ()) or ())
        controls = []
        if m.state == QUEUED:
            controls = ["run", "pause", "cancel"]
        elif m.state == RUNNING:
            controls = ["pause", "cancel"]
        elif m.state == PAUSING:
            controls = ["cancel"]
        elif m.state == WAITING:
            controls = ["check", "pause", "cancel"]
        elif m.state == NEEDS_YOU:
            controls = (["cancel"] if action_in_flight else
                        ((["confirm"] if inbox else ["continue", "accept"]) +
                         ["pause", "cancel"]))
        elif m.state == PAUSED:
            controls = ["resume", "cancel"]
        elif m.state == RECOVERY_REQUIRED:
            controls = ["reconcile", "cancel"]
        elif m.state == RECONCILING:
            controls = ["reconcile", "cancel"]
        elif m.state == FAILED_S:
            controls = ["retry"]
        elif m.state == DONE_ACCEPTED:
            controls = ["continue"]
        recovery_actions = []
        if m.state in (RECOVERY_REQUIRED, RECONCILING):
            recovery_actions = [
                {"nonce": r.get("nonce"), "capability": r.get("capability"),
                 "state": r.get("state"), "args": _clean(json.loads(
                     r.get("args_json") or "{}"))}
                for r in self.actions.list()
                if r.get("job_id") == mid and r.get("state") in
                   (PENDING, APPROVED, EXECUTING, EXECUTED)]
        steps = [{"name": s["name"], "verdict": s["verdict"]}
                 for s in self.store.steps(mid)]
        receipts = [{"capability": r["capability"], "verdict": r["verdict"],
                     "fired": bool(r["fired"])}
                    for r in self.actions.receipts() if r.get("job_id") == mid]
        return {
            "mission_id": mid, "goal": m.goal, "state": m.state, "result": m.result,
            "created_at": m.created_at, "updated_at": m.updated_at,
            "case": _clean(m.case),
            "summary": _mission_summary(
                m, steps, receipts, runtime, inbox, next_wait, activity),
            "steps": steps,
            "activity": activity,
            "recent_events": self.store.events(mid, 20),
            "inbox": inbox,                       # non-null -> render a Confirm button
            "needs_human": (m.state == NEEDS_YOU and inbox is None and
                            not action_in_flight),  # -> Accept hand-off
            "action_in_flight": action_in_flight,
            "next_wake_at": next_wait["fire_at"] if next_wait else None,
            "runtime": runtime,
            "aggregate_runtime": aggregate_runtime,
            "usage_projection_errors": usage_reconciliation["errors"],
            "budget_exhausted": self.store.budget_reason(mid) or None,
            "latest_checkpoint": ({k: checkpoint[k] for k in
                                   ("seq", "phase", "payload", "at")}
                                  if checkpoint else None),
            "workspace_request": (m.leash.get("workspace_mode") == "isolated" and
                                  "code" in (m.leash.get("may") or []) and
                                  not m.case.get("_isolated_workspace")),
            "run_tree": run_tree,
            "tasktree": {
                "available": self._run_tree is not None,
                "attached": bool(m.case.get("_run_id")),
                "path": getattr(self._run_tree, "path", None),
            },
            "hooks": {
                "active": bool(getattr(self._hooks, "active", False)),
                "pending": pending_hooks,
            },
            "controls": controls,
            "recovery_actions": recovery_actions,
            "receipts": receipts,
        }

    def missions(self) -> list:
        return [{"mission_id": m.mission_id, "goal": m.goal, "state": m.state,
                 "result": m.result, "updated_at": m.updated_at,
                 "controls": self.status(m.mission_id).get("controls", [])}
                for m in reversed(self.store.list())
                if self.store.runtime(m.mission_id).get("lane") != "specialist"]

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.store.close()
        self.actions.close()
        # Injected stores belong to their caller and may be shared by the Web,
        # daemon, and tests.  Only close the durable backend we constructed.
        if self._owns_run_tree and self._run_tree is not None:
            self._run_tree.close()
        if self._owns_hooks and hasattr(self._hooks, "close"):
            self._hooks.close()
