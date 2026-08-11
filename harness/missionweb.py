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
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .actions import (APPROVED, EXECUTED, EXECUTING, EXPIRED, PENDING, REFUSED,
                      ActionStore, RefusedError)
from .jobs import (CANCELLED, DONE_ACCEPTED, DONE_VERIFIED, FAILED_S, NEEDS_YOU,
                   PAUSED, PAUSING, QUEUED, RECONCILING, RECOVERY_REQUIRED,
                   RUNNING, WAITING)
from .mission import (MissionDriver, MissionStore, ModelDecider, create_mission,
                      world_leash)
from .primitives import register_primitives


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
    pending_auth = [x for x in case.get("pending_authorizations", [])
                    if isinstance(x, dict)][-8:]
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
    pending = len(pending_auth) + (1 if inbox else 0)
    current = _short(mission.result, 500) or _short(runtime.get("active_phase"), 160)
    if inbox:
        next_step = "Confirm the prepared %s action" % _short(inbox.get("capability"), 100)
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
        blocker = (_short(pending_auth[0].get("summary"), 500) if pending_auth
                   else _short(mission.result, 500) or "A person-required step")
    return {
        "title": _short(mission.goal, 300),
        "current": current or "Ready",
        "completed": completed[-8:],
        "next": next_step,
        "blocker": blocker,
        "authorization_waiting": len(pending_auth),
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
        self._runtime_ready = True

    def _driver(self, *, lane="mission", control=None) -> MissionDriver:
        self._ensure_runtime()
        dec = self._decider or ModelDecider(self._prov)
        return MissionDriver(self.store, self.actions, dec,
                             capabilities=self._capabilities,
                             goal_verifier=self._goal_verifier, lane=lane,
                             control=control, hooks=self._hooks)

    def _specialist_run(self, mid):
        runtime = self.store.runtime(mid)
        run_id = runtime.get("external_run_id") if runtime.get("lane") == "specialist" else ""
        return self._run_tree.get(run_id) if self._run_tree is not None and run_id else None

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
        case = dict(m.case)
        case["_isolated_workspace"] = canonical
        self.store.set_case(mid, case)
        self.store.record_checkpoint(
            mid, "", "workspace_bound", {"workspace": canonical},
            case=case, allow_unowned=True)
        if self._run_tree and case.get("_run_id"):
            self._run_tree.bind_workspace(case["_run_id"], canonical,
                                          owns_workspace=False)
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
        run = self._run_tree.create_root(
            m.goal, m.leash, resources, mission_id=mid, workspace=workspace,
            workspace_mode="worktree")
        case = dict(m.case)
        case["_run_id"] = run["run_id"]
        if workspace:
            case["_isolated_workspace"] = os.path.realpath(os.path.abspath(workspace))
        self.store.set_case(mid, case)
        self.store.record_checkpoint(
            mid, "", "run_tree_created", {"run_id": run["run_id"]},
            case=case, allow_unowned=True)
        return self._run_tree.tree(run["run_id"])

    def spawn_specialist(self, mid: str, role: str, task: str, *, leash=None,
                         resources=None, workspace: str = "") -> dict:
        m = self.store.get(mid)
        run_id = m.case.get("_run_id") if m else ""
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
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

    def _create_specialist_mission(self, parent_mid, run):
        """Materialize a scoped specialist as a real Mission lane, not a TODO row."""
        if run.get("mission_id"):
            return run
        workspace = run.get("workspace") or ""
        if not workspace:
            return run
        child_mid = "spc_" + run["run_id"].replace("run_", "")
        if not self.store.get(child_mid):
            case = {
                "_isolated_workspace": workspace,
                "_specialist_run_id": run["run_id"],
                "_parent_mission_id": parent_mid,
                "_resource_scope": run.get("resources") or [],
                "role": run.get("role") or "specialist",
            }
            create_mission(
                self.store, child_mid, run["task"], case=case, leash=run["leash"],
                lane="specialist", external_run_id=run["run_id"])
        if not self._run_tree.bind_mission(run["run_id"], child_mid):
            raise ValueError("specialist Mission binding raced with another owner")
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
        return {
            "mission_id": mid,
            "available": self._run_tree is not None,
            "attached": bool(run_id),
            "path": getattr(self._run_tree, "path", None),
            "tree": self._run_tree.tree(run_id) if self._run_tree and run_id
                    else {"root": None, "flat": []},
        }

    def inspect_specialist(self, run_id: str, event_limit: int = 100) -> dict:
        """Inspect one run, its descendant tree and recent durable events."""
        if self._run_tree is None:
            return {"error": "no durable run-tree store configured", "run_id": run_id}
        run = self._run_tree.get(run_id)
        if not run:
            return {"error": "unknown specialist run", "run_id": run_id}
        return {"run": run, "tree": self._run_tree.tree(run_id),
                "events": self._run_tree.events(run_id, event_limit)}

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
        if self._specialist_run(mid):
            return {**self.status(mid),
                    "error": "specialist Missions run only through their scoped dispatcher"}
        if m.state != QUEUED:
            # Idempotent for the common Web-vs-daemon claim race: if somebody else
            # already advanced it, return the live state instead of a false failure.
            return self.status(mid)
        try:
            self._driver().advance(mid)
        except Exception as e:
            return {**self.status(mid), "error": f"run unavailable: {e}"}
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
        if m.state != NEEDS_YOU or nonce or not self.store.accept_handoff(mid):
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
        return self.status(mid)

    def tick(self, mid: str = None, now=None) -> dict:
        """Fire any due durable re-checks now (the 'check inbox now' button; also
        what colliejobd calls on wake)."""
        import time
        at = int(now if now is not None else time.time())
        recovered = self.store.recover_stale_runs(at)
        escalations = self.store.escalate_human_waits(at)
        specialists = self._tick_specialists(at)
        if not self.store.list(state=QUEUED) and not self.store.due_waits(at):
            if mid:
                return {**self.status(mid), "escalations": [e for e in escalations
                                                             if e["mission_id"] == mid]}
            return {"advanced": 0, "specialists_advanced": specialists,
                    "recovered": recovered,
                    "escalations": escalations}
        n = self._driver().tick_missions(at, max_workers=self._mission_workers)
        if mid:
            return {**self.status(mid), "escalations": [e for e in escalations
                                                         if e["mission_id"] == mid]}
        return {"advanced": n, "specialists_advanced": specialists,
                "recovered": recovered, "escalations": escalations}

    def _specialist_control(self, run_id, token):
        run = self._run_tree.get(run_id)
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
        before = self.store.runtime(child_mid) if child_mid else {}
        try:
            if not child_mid or not self.store.get(child_mid):
                self._run_tree.block(
                    run_id, token,
                    "specialist runner has no bound Mission/worktree", needs_you=True)
                return
            budget = self._run_tree.budget_reason(run_id)
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
                state = driver.wake(child_mid, force=False)
            elif child.state == NEEDS_YOU:
                _name, nonce = self.store.last_parked(child_mid)
                record = self.actions.get(nonce) if nonce else None
                state = driver.resume(child_mid) if record and record.state == APPROVED \
                    else child.state
            else:
                state = child.state
            after = self.store.runtime(child_mid)
            exhausted = self._run_tree.account_usage(
                run_id, token,
                input_tokens=max(0, int(after.get("input_tokens", 0)) -
                                 int(before.get("input_tokens", 0))),
                output_tokens=max(0, int(after.get("output_tokens", 0)) -
                                  int(before.get("output_tokens", 0))),
                cost_usd=max(0.0, float(after.get("model_cost_usd", 0.0)) -
                             float(before.get("model_cost_usd", 0.0))),
                wall_ms=max(0, int(after.get("active_wall_ms", 0)) -
                            int(before.get("active_wall_ms", 0))),
                retries=max(0, int(after.get("retry_count", 0)) -
                            int(before.get("retry_count", 0))))
            if exhausted and state not in (DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
                self._run_tree.block(
                    run_id, token,
                    "; ".join("%s: %s" % item for item in exhausted), needs_you=True)
            elif state in (DONE_VERIFIED, DONE_ACCEPTED):
                if not self._run_tree.complete(
                        run_id, token, self.store.get(child_mid).result):
                    self._run_tree.block(
                        run_id, token, "TaskCompleted hook blocked specialist completion",
                        needs_you=True)
            elif state == FAILED_S:
                self._run_tree.fail(run_id, token, self.store.get(child_mid).result)
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
            stop.set()
            beat.join(timeout=2)

    def _tick_specialists(self, now):
        """Claim and actually execute scoped child Missions; never strand queued rows."""
        if self._run_tree is None:
            return 0
        from .tasktree import (BLOCKED as T_BLOCKED, NEEDS_YOU as T_NEEDS_YOU,
                               PAUSED as T_PAUSED, QUEUED as T_QUEUED,
                               RECOVERY_REQUIRED as T_RECOVERY, WAITING as T_WAITING)
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
        workers = self._specialist_workers if self._specialist_workers is not None else \
            int(os.environ.get("COLLIE_SPECIALIST_WORKERS", "4"))
        workers = max(1, min(8, int(workers)))
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
