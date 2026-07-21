"""Job — the delegate's core object, and the registry the executor consults.

verifier.py decides if an outcome happened; observe.py gets the evidence;
actions.py gates and performs one irreversible action. This ties them to a
durable Job (the plan's object #1) with an honest state machine, and a
Capability registry so the model-free executor can run a CONFIRMED action by
name — which is what makes `collie confirm <nonce>` possible: the human approves,
and host code (not a model) looks up how to execute and verify.

State machine (only the orchestrator moves a job; a model never marks itself done):

    queued -> running -> waiting  (durable wait: timer / email / page change)
                 |          |
                 |          +----> needs_you  (a gated action or a blocker)
                 |
                 +----> done_verified   (fresh independent evidence satisfied the check)
                 +----> done_accepted   (human eyeballed it; NO machine evidence)
                 +----> failed          (evidence refuted, or an unrecoverable blocker)
                 +----> cancelled

The verified/accepted split is load-bearing: an action whose done-check came
back INCONCLUSIVE (could not observe) or that was only human-eyeballed can reach
done_accepted at most — never done_verified. The ledger counts only verified.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from . import leash as _leash
from .actions import ActionStore, EXECUTED, PENDING, RefusedError
from .verifier import FAILED, INCONCLUSIVE, NOT_ARMED, VERIFIED, Verdict

QUEUED = "queued"
RUNNING = "running"
WAITING = "waiting"
NEEDS_YOU = "needs_you"
DONE_VERIFIED = "done_verified"
DONE_ACCEPTED = "done_accepted"
FAILED_S = "failed"
CANCELLED = "cancelled"

_TERMINAL = {DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED}


@dataclass
class Job:
    job_id: str
    goal: str
    leash: dict = field(default_factory=dict)
    state: str = QUEUED
    result: str = ""
    created_at: int = 0
    updated_at: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL


# ── capability registry: name -> how to execute + how to verify ─────────────
@dataclass
class Capability:
    """A typed action the executor can run by name. `execute(record)->result` is
    the real side effect; `verify(record,result)->Verdict` is its done-check.
    reversible=False means a failed post-check compensates (if given) instead of
    a blind retry — the verifier.repairable() rule at the job level."""
    name: str
    execute: object                       # fn(ActionRecord) -> result
    verify: object = None                 # fn(ActionRecord, result) -> Verdict
    reversible: bool = False
    risk: str = "irreversible"
    compensate: object = None             # optional fn(ActionRecord) -> None


_REGISTRY: dict = {}


def register(cap: Capability):
    _REGISTRY[cap.name] = cap
    return cap


def get_capability(name: str) -> Capability:
    return _REGISTRY.get(name)


def clear_registry():
    _REGISTRY.clear()


# ── job store (same on-disk db family as actions/receipts) ──────────────────
class JobStore:
    def __init__(self, path: str = None):
        path = path or os.path.expanduser("~/.collie/jobs.db")
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs(
            job_id TEXT PRIMARY KEY, goal TEXT, leash_json TEXT, state TEXT,
            result TEXT, created_at INTEGER, updated_at INTEGER)""")
        self.db.commit()

    def create(self, job_id: str, goal: str, leash: dict = None) -> Job:
        now = int(time.time())
        with self._lock:
            self.db.execute(
                "INSERT INTO jobs(job_id,goal,leash_json,state,result,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (job_id, goal, json.dumps(leash or {}, ensure_ascii=False),
                 QUEUED, "", now, now))
            self.db.commit()
        return Job(job_id, goal, leash or {}, QUEUED, "", now, now)

    def get(self, job_id: str) -> Job:
        r = self.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not r:
            return None
        return Job(r["job_id"], r["goal"], json.loads(r["leash_json"] or "{}"),
                   r["state"], r["result"], r["created_at"], r["updated_at"])

    def set_state(self, job_id: str, state: str, result: str = None):
        with self._lock:
            if result is None:
                self.db.execute("UPDATE jobs SET state=?,updated_at=? WHERE job_id=?",
                                (state, int(time.time()), job_id))
            else:
                self.db.execute("UPDATE jobs SET state=?,result=?,updated_at=? WHERE job_id=?",
                                (state, result, int(time.time()), job_id))
            self.db.commit()

    def list(self, state: str = None):
        q, a = "SELECT * FROM jobs", ()
        if state:
            q, a = q + " WHERE state=?", (state,)
        return [Job(r["job_id"], r["goal"], json.loads(r["leash_json"] or "{}"),
                    r["state"], r["result"], r["created_at"], r["updated_at"])
                for r in self.db.execute(q + " ORDER BY created_at", a)]

    def close(self):
        self.db.close()


# ── the executor: run a CONFIRMED action by capability name, set job state ──
class Executor:
    """Model-free. Given a confirmed nonce, look the capability up by name and
    drive actions.execute with its real side effect + done-check, then map the
    verdict onto the job's terminal state.

    verdict -> job state:
        VERIFIED     -> done_verified
        FAILED       -> failed (+ compensate if the capability declared one)
        INCONCLUSIVE -> needs_you   (could not observe — a human must look;
                        the action already fired, so this is NOT a silent retry)
        NOT_ARMED    -> done_accepted (nothing to verify)
    """

    def __init__(self, actions: ActionStore, jobs: JobStore):
        self.actions = actions
        self.jobs = jobs

    def _apply(self, tgt: str, cap: Capability, rec, status: str, reason: str):
        if not tgt:
            return
        if status == VERIFIED:
            self.jobs.set_state(tgt, DONE_VERIFIED, reason)
        elif status == FAILED:
            if not cap.reversible and cap.compensate:
                try:
                    cap.compensate(rec)
                except Exception:
                    pass
            self.jobs.set_state(tgt, FAILED_S, reason)
        elif status == NOT_ARMED:
            # a fired action that produced NO armed evidence is a mis-authored
            # verify, not a real no-op. For an irreversible action that already
            # fired, never read that as success — a human must look. Reserve
            # done_accepted for a reversible no-op / explicit human accept.
            self.jobs.set_state(tgt, DONE_ACCEPTED if cap.reversible else NEEDS_YOU, reason)
        else:  # INCONCLUSIVE — fired but could not confirm; a human must look
            self.jobs.set_state(tgt, NEEDS_YOU, reason)

    def drive(self, nonce: str, redact_fn=None, unchanged_fn=None) -> Verdict:
        """Autonomous entry: evaluate the leash and act accordingly, so a
        reversible in-scope action runs with no human, while an irreversible one
        stops for a confirm token.

          DENY  -> refuse (never executes)
          ASK   -> irreversible: if not yet confirmed, park the job in needs_you
                   and DON'T execute (returns INCONCLUSIVE 'awaiting confirm');
                   once a human has confirmed, execute.
          ALLOW -> reversible & in scope: host auto-approves (the leash IS the
                   authority) and executes — no human in the loop.
        """
        rec = self.actions.get(nonce)
        if not rec:
            raise RefusedError("unknown nonce")
        cap = get_capability(rec.capability)
        if not cap:
            raise RefusedError(f"no registered capability {rec.capability!r}")
        tgt = rec.job_id
        job = self.jobs.get(tgt) if tgt else None
        dec = _leash.evaluate(job.leash if job else {}, rec.capability, cap.risk)
        if dec.denied:
            raise RefusedError(f"leash denied: {dec.reason}")
        cur = self.actions.get(nonce)
        if dec.decision == _leash.ASK and cur.state == PENDING:
            if tgt:
                self.jobs.set_state(tgt, NEEDS_YOU, "awaiting confirm for irreversible action")
            return Verdict(INCONCLUSIVE, "awaiting human confirm (irreversible)")
        if cur.state == PENDING and dec.decision == _leash.ALLOW:
            self.actions.confirm(nonce)          # host auto-approve: leash-authorized reversible
        return self.run_confirmed(nonce, job_id=tgt, redact_fn=redact_fn,
                                  unchanged_fn=unchanged_fn)

    def run_confirmed(self, nonce: str, job_id: str = "", redact_fn=None,
                      unchanged_fn=None) -> Verdict:
        rec = self.actions.get(nonce)
        if not rec:
            raise RefusedError("unknown nonce")
        cap = get_capability(rec.capability)
        if not cap:
            raise RefusedError(f"no registered capability {rec.capability!r}")
        # self-bind to the action's own job so a caller cannot mis-attribute the
        # verdict to a different job (falls back to the passed job_id).
        tgt = rec.job_id or job_id
        # leash gate: a DENY blocks execution even for a confirmed action — the
        # human's confirm answers ASK (irreversible), it does not override an
        # out-of-scope capability or an expired/over-budget leash.
        job = self.jobs.get(tgt) if tgt else None
        if job:
            dec = _leash.evaluate(job.leash, rec.capability, cap.risk)
            if dec.denied:
                raise RefusedError(f"leash denied: {dec.reason}")
        try:
            receipt = self.actions.execute(
                nonce,
                side_effect_fn=cap.execute,
                donecheck_fn=cap.verify,
                unchanged_fn=unchanged_fn,
                redact_fn=redact_fn)
        except RefusedError:
            # RECONCILE: if the action already fired+receipted (a crash between
            # execute and set_state left the job stuck non-terminal), converge the
            # job from the stored receipt instead of stranding it or re-firing.
            # Any other refusal (unconfirmed / tamper / TOCTOU / single-use before
            # any fire) genuinely blocks — re-raise.
            fired = [x for x in self.actions.receipts(nonce) if x.get("fired")]
            cur = self.actions.get(nonce)
            if fired and cur and cur.state == EXECUTED:
                last = fired[-1]
                self._apply(tgt, cap, rec, last["verdict"], last["verdict_reason"])
                return Verdict(last["verdict"], last["verdict_reason"])
            raise

        self._apply(tgt, cap, rec, receipt.verdict, receipt.verdict_reason)
        return Verdict(receipt.verdict, receipt.verdict_reason)
