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
import time

from .actions import (APPROVED, EXECUTED, EXECUTING, EXPIRED, PENDING, REFUSED,
                      ActionStore, RefusedError)
from .jobs import (CANCELLED, DONE_ACCEPTED, DONE_VERIFIED, FAILED_S, NEEDS_YOU,
                   PAUSED, PAUSING, QUEUED, RECONCILING, RECOVERY_REQUIRED,
                   RUNNING, WAITING)
from .mission import (MissionDriver, MissionStore, ModelDecider, create_mission,
                      world_leash)
from .primitives import register_primitives


def _provider_name() -> str:
    # mirrors webapp._provider(): the Settings-panel provider is applied into the
    # env before a request, so this is the same provider the chat GUI runs on.
    return os.environ.get("COLLIE_PROVIDER", "")


def _clean(d: dict) -> dict:
    """Drop the injected `_case` context from args/case before it hits the UI."""
    return {k: v for k, v in (d or {}).items() if k not in ("_case", "_leash")}


class MissionService:
    def __init__(self, base: str = None, decider=None, provider: str = None,
                 model: str = None, stub=None, state_dir: str = None):
        # base isolates tests; production uses the shared ~/.collie stores (so a
        # mission is visible to `collie jobs` / jobsweb too).
        state_dir = state_dir or os.environ.get("COLLIE_STATE_DIR") or \
            os.path.expanduser("~/.collie")
        mission_path = (base + ".missions") if base else os.path.join(state_dir, "jobs.db")
        action_path = (base + ".actions") if base else os.path.join(state_dir, "actions.db")
        self.store = MissionStore(mission_path)
        self.actions = ActionStore(action_path)
        self._decider = decider
        self._provider = provider or _provider_name()
        self._model = model or os.environ.get("COLLIE_MODEL") or None
        self._stub = stub
        self._prov = None
        self._runtime_ready = False
        self._capabilities = None

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

    def _driver(self) -> MissionDriver:
        self._ensure_runtime()
        dec = self._decider or ModelDecider(self._prov)
        return MissionDriver(self.store, self.actions, dec,
                             capabilities=self._capabilities)

    # ── commands ──
    def start(self, goal: str, autonomous: bool = False, case: dict = None, **bounds) -> dict:
        """Persist first and return the id immediately; /run or the daemon claims it."""
        mid = "msn_" + secrets.token_hex(6)
        create_mission(self.store, mid, goal, case=case or {},
                       leash=world_leash(autonomous=autonomous, **bounds))
        return self.status(mid)

    def run(self, mid: str) -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
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
        try:
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
        return self.status(mid) if target else {
            **self.status(mid), "error": f"cannot resume from {m.state}"}

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

    def cancel(self, mid: str) -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if m.state in (DONE_VERIFIED, DONE_ACCEPTED, FAILED_S):
            return {**self.status(mid), "error": f"cannot cancel terminal mission ({m.state})"}
        reason = "cancelled; an in-flight action may still finish" if m.state in (RUNNING, PAUSING) \
            else "cancelled by user"
        self.store.cancel(mid, reason)
        self.actions.refuse_for_job(mid, "mission cancelled")
        _name, nonce = self.store.last_parked(mid)
        if nonce:
            self.store.resolve_parked(nonce, CANCELLED)
        return self.status(mid)

    def accept(self, mid: str) -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        _name, nonce = self.store.last_parked(mid)
        if m.state != NEEDS_YOU or nonce or not self.store.accept_handoff(mid):
            return {**self.status(mid), "error": f"cannot accept from {m.state}"}
        return self.status(mid)

    def continue_after_human(self, mid: str, note: str = "") -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        _name, nonce = self.store.last_parked(mid)
        if m.state != NEEDS_YOU or nonce or not self.store.continue_handoff(mid, note):
            return {**self.status(mid), "error": f"cannot continue from {m.state}"}
        return self.status(mid)

    def check(self, mid: str) -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if m.state != WAITING:
            return {**self.status(mid), "error": f"cannot check from {m.state}"}
        try:
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
        if not self.store.list(state=QUEUED) and not self.store.due_waits(at):
            return self.status(mid) if mid else {"advanced": 0, "recovered": recovered}
        n = self._driver().tick_missions(at)
        return self.status(mid) if mid else {"advanced": n, "recovered": recovered}

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
        recovery_actions = []
        if m.state in (RECOVERY_REQUIRED, RECONCILING):
            recovery_actions = [
                {"nonce": r.get("nonce"), "capability": r.get("capability"),
                 "state": r.get("state"), "args": _clean(json.loads(
                     r.get("args_json") or "{}"))}
                for r in self.actions.list()
                if r.get("job_id") == mid and r.get("state") in
                   (PENDING, APPROVED, EXECUTING, EXECUTED)]
        return {
            "mission_id": mid, "goal": m.goal, "state": m.state, "result": m.result,
            "created_at": m.created_at, "updated_at": m.updated_at,
            "case": _clean(m.case),
            "steps": [{"name": s["name"], "verdict": s["verdict"]}
                      for s in self.store.steps(mid)],
            "recent_events": self.store.events(mid, 20),
            "inbox": inbox,                       # non-null -> render a Confirm button
            "needs_human": (m.state == NEEDS_YOU and inbox is None and
                            not action_in_flight),  # -> Accept hand-off
            "action_in_flight": action_in_flight,
            "next_wake_at": next_wait["fire_at"] if next_wait else None,
            "controls": controls,
            "recovery_actions": recovery_actions,
            "receipts": [{"capability": r["capability"], "verdict": r["verdict"],
                          "fired": bool(r["fired"])}
                         for r in self.actions.receipts() if r.get("job_id") == mid],
        }

    def missions(self) -> list:
        return [{"mission_id": m.mission_id, "goal": m.goal, "state": m.state,
                 "result": m.result, "updated_at": m.updated_at,
                 "controls": self.status(m.mission_id).get("controls", [])}
                for m in reversed(self.store.list())]

    def close(self):
        self.store.close()
        self.actions.close()
