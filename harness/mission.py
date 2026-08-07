"""Mission — a durable, gated, verified CONTAINER for an open-ended world errand.

A `Job` (jobs.py) is ONE verified action. A real errand — sell my car, book a
dentist, chase a refund — is a CAMPAIGN that runs for days. The wrong way to add
that is a template per errand (a `marketplace.py`, a `dentist.py`): templates
don't scale, and a fixed menu of typed steps is the opposite of "全能". The whole
point of an omni-capable delegate is that the MODEL generalizes — it decides the
flow from the goal, we don't script it.

So Mission does NOT hold a plan. It holds only what a raw model loop CANNOT give
itself, and lets the model drive everything else:

  what the CONTAINER owns (deterministic, domain-agnostic, the reason this isn't
  just a ReAct loop):
    1. DURABILITY — the case (shared state) is on disk; the campaign survives
       process death and machine sleep, and re-enters on wake (a week-long errand
       cannot live in one live model loop).
    2. THE GATE — an irreversible action never fires in the step that proposes it
       (actions.py): it materializes, a human confirms the concrete payload out of
       band, a model-free executor runs it. A model driving a browser in-loop must
       never click "pay"/"publish" itself.
    3. AUTHORITY — the leash (deterministic code) bounds what may run; autonomy is
       the leash ("may reply to buyers, price ≥ X, local only"), not a flag.
    4. EVIDENCE — done is an independent observation, never the model's self-report.

  what the MODEL owns (via the injected `decider`):
    the entire flow. Each advance, the container asks the decider "given this goal
    and what you know so far (the case), what is the ONE next action?" — a neutral
    primitive (primitives.py: research / compose / observe / web.submit / web.send),
    or a control move (wait N / needs_human / done). The container gates + runs it,
    folds the result into the case, and asks again. No per-errand code.

`decider(goal, case, primitives) -> {"action","args","reason"}`. Production wires
a ModelDecider(provider); tests wire a scripted or case-driven function. Either
way the container's gate/durability/evidence guarantees are identical.
"""

from __future__ import annotations

import json
import hashlib
import fnmatch
import os
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from dataclasses import dataclass, field

from . import leash as _leash
from .actions import ActionStore, RefusedError
from .jobs import (CANCELLED, DONE_ACCEPTED, DONE_VERIFIED, FAILED_S, NEEDS_YOU,
                   PAUSED, PAUSING, QUEUED, RECONCILING, RECOVERY_REQUIRED,
                   RUNNING, WAITING,
                   all_capabilities, get_capability)
from .verifier import FAILED, VERIFIED, Verdict

_TERMINAL = {DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED}
# control moves the decider can return instead of a primitive name
WAIT, DONE, NEEDS_HUMAN = "wait", "done", "needs_human"
_AWAITING = "awaiting-confirm"


class ResourceBusy(RefusedError):
    pass


@dataclass
class Mission:
    mission_id: str
    goal: str                                    # the errand in the user's words
    leash: dict = field(default_factory=dict)    # authority bounds (autonomy lives here)
    case: dict = field(default_factory=dict)     # shared durable state the model reads
    state: str = QUEUED
    result: str = ""
    created_at: int = 0
    updated_at: int = 0
    paused_from: str = ""
    run_token: str = ""
    lease_until: int = 0

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL


def _js(o):
    return json.dumps(o or {}, ensure_ascii=False)


def _jl(s):
    try:
        return json.loads(s) if s else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _compact_event(value, limit=4000):
    """Bound an append-only ledger row so a long campaign cannot grow explosively."""
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raw = str(value)
    return value if len(raw) <= limit else {"summary": raw[:limit], "truncated": True}


# ── mission store (same on-disk db family as jobs/actions) ──────────────────
class MissionStore:
    def __init__(self, path: str = None):
        path = path or os.path.join(
            os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie"),
            "jobs.db")
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
        self.db.execute("""CREATE TABLE IF NOT EXISTS missions(
            mission_id TEXT PRIMARY KEY, goal TEXT, leash_json TEXT, case_json TEXT,
            state TEXT, result TEXT, created_at INTEGER, updated_at INTEGER,
            paused_from TEXT NOT NULL DEFAULT '', run_token TEXT NOT NULL DEFAULT '',
            lease_until INTEGER NOT NULL DEFAULT 0)""")
        for col, decl in (
                ("paused_from", "TEXT NOT NULL DEFAULT ''"),
                ("run_token", "TEXT NOT NULL DEFAULT ''"),
                ("lease_until", "INTEGER NOT NULL DEFAULT 0")):
            try:  # guarded migration for databases created while Mission was disabled
                self.db.execute("ALTER TABLE missions ADD COLUMN %s %s" % (col, decl))
            except sqlite3.OperationalError:
                pass
        # Rows from the pre-lease Mission prototype can be RUNNING with no owner
        # token forever. They are uncertain, not safely rerunnable.
        self.db.execute(
            "UPDATE missions SET state=?,result=?,updated_at=? WHERE state=? "
            "AND COALESCE(run_token,'')='' AND COALESCE(lease_until,0)=0",
            (RECOVERY_REQUIRED,
             "legacy runner had no ownership record; inspect and reconcile",
             int(time.time()), RUNNING))
        # the campaign audit trail: one row per action the model chose + its verdict
        self.db.execute("""CREATE TABLE IF NOT EXISTS mission_steps(
            step_id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT, name TEXT,
            nonce TEXT, verdict TEXT, at INTEGER)""")
        # the durable loop: a mission's own wait table (separate from scheduler's
        # action-waits, so colliejobd's action tick never mis-drives a loop tick).
        self.db.execute("""CREATE TABLE IF NOT EXISTS mission_waits(
            wait_id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT,
            fire_at INTEGER, state TEXT, created_at INTEGER)""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS mission_waits_due "
            "ON mission_waits(state,fire_at)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS mission_resource_leases(
            resource TEXT PRIMARY KEY, mission_id TEXT NOT NULL, token TEXT NOT NULL,
            lease_until INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS mission_events(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT NOT NULL,
            kind TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', nonce TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}', at INTEGER NOT NULL)""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS mission_events_recent ON mission_events(mission_id,event_id)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS mission_action_keys(
            mission_id TEXT NOT NULL, action_key TEXT NOT NULL, nonce TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL, at INTEGER NOT NULL,
            owner_token TEXT NOT NULL DEFAULT '', reservation_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(mission_id,action_key))""")
        for col in ("owner_token", "reservation_id"):
            try:
                self.db.execute(
                    "ALTER TABLE mission_action_keys ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % col)
            except sqlite3.OperationalError:
                pass
        self.db.commit()

    def create(self, mission_id, goal, leash=None, case=None) -> Mission:
        now = int(time.time())
        with self._lock:
            self.db.execute(
                "INSERT INTO missions(mission_id,goal,leash_json,case_json,state,"
                "result,created_at,updated_at,paused_from,run_token,lease_until) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (mission_id, goal, _js(leash or {}), _js(case or {}), QUEUED, "",
                 now, now, "", "", 0))
            self.db.commit()
        return Mission(mission_id, goal, leash or {}, case or {}, QUEUED, "", now, now)

    def get(self, mission_id) -> Mission:
        r = self.db.execute("SELECT * FROM missions WHERE mission_id=?",
                            (mission_id,)).fetchone()
        if not r:
            return None
        return Mission(r["mission_id"], r["goal"], _jl(r["leash_json"]),
                       _jl(r["case_json"]), r["state"], r["result"],
                       r["created_at"], r["updated_at"], r["paused_from"],
                       r["run_token"], r["lease_until"])

    def _set(self, mission_id, **cols):
        cols["updated_at"] = int(time.time())
        sets = ",".join(f"{k}=?" for k in cols)
        with self._lock:
            self.db.execute(f"UPDATE missions SET {sets} WHERE mission_id=?",
                            (*cols.values(), mission_id))
            self.db.commit()

    def set_state(self, mission_id, state, result=None):
        self._set(mission_id, state=state, result=result) if result is not None \
            else self._set(mission_id, state=state)

    def set_case(self, mission_id, case):
        self._set(mission_id, case_json=_js(case))

    def claim_run(self, mission_id, expected=(QUEUED,), lease_s=300):
        """Atomically acquire the one active driver slot for a mission.

        We intentionally do not steal an expired token automatically: after a hard
        crash an external action may have fired without its receipt being committed.
        A user can pause/cancel and explicitly reconcile that uncertain RUNNING state.
        """
        token = secrets.token_hex(16)
        now = int(time.time())
        states = tuple(expected or ())
        if not states:
            return None
        marks = ",".join("?" for _ in states)
        with self._lock:
            cur = self.db.execute(
                "UPDATE missions SET state=?,run_token=?,lease_until=?,updated_at=? "
                "WHERE mission_id=? AND state IN (%s) AND COALESCE(run_token,'')=''" % marks,
                (RUNNING, token, now + int(lease_s), now, mission_id, *states))
            self.db.commit()
        return token if cur.rowcount == 1 else None

    def owns_run(self, mission_id, token, renew_s=300):
        """Renew a live claim and report whether it may start another action.

        PAUSING deliberately does not count as runnable.  The heartbeat uses
        ``renew_run`` below so a long primitive can finish its current boundary
        without making the lease look abandoned.
        """
        now = int(time.time())
        with self._lock:
            cur = self.db.execute(
                "UPDATE missions SET lease_until=?,updated_at=? "
                "WHERE mission_id=? AND state=? AND run_token=?",
                (now + int(renew_s), now, mission_id, RUNNING, token))
            self.db.commit()
        return cur.rowcount == 1

    def renew_run(self, mission_id, token, renew_s=300):
        """Renew ownership while RUNNING or cooperatively PAUSING."""
        now = int(time.time())
        with self._lock:
            cur = self.db.execute(
                "UPDATE missions SET lease_until=?,updated_at=? "
                "WHERE mission_id=? AND state IN (?,?) AND run_token=?",
                (now + int(renew_s), now, mission_id, RUNNING, PAUSING, token))
            self.db.commit()
        return cur.rowcount == 1

    def owns_claim(self, mission_id, token, renew_s=300):
        """The current worker may commit the result of an already-started action.

        This is intentionally broader than :meth:`owns_run`: PAUSING forbids a
        *new* action, but the same token must durably fold a side effect that
        finished after pause was requested.  Cancellation clears the token and
        therefore still fences all stale mutation.
        """
        return self.renew_run(mission_id, token, renew_s)

    def recover_stale_runs(self, now=None):
        """Surface crashed workers for explicit reconciliation after their heartbeat expires.

        We do not blindly rerun: an external action might have fired immediately
        before process death.  RECOVERY_REQUIRED is intentionally distinct from a
        normal human hand-off, so ordinary ``continue`` cannot duplicate it.
        """
        now = int(now if now is not None else time.time())
        with self._lock:
            cur = self.db.execute(
                "UPDATE missions SET state=?,run_token='',lease_until=0,result=?,updated_at=? "
                "WHERE state IN (?,?) AND COALESCE(run_token,'')<>'' "
                "AND lease_until>0 AND lease_until<=?",
                (RECOVERY_REQUIRED,
                 "runner heartbeat expired; inspect the external system and receipts, "
                 "then explicitly reconcile or cancel",
                 now, RUNNING, PAUSING, now))
            self.db.commit()
        return cur.rowcount

    def claim_resource(self, resource, mission_id, lease_s=300):
        """Cross-process lease for a shared external surface (browser/account)."""
        token, now = secrets.token_hex(16), int(time.time())
        with self._lock:
            self.db.execute(
                "DELETE FROM mission_resource_leases WHERE resource=? AND lease_until<=? "
                "AND mission_id NOT IN (SELECT mission_id FROM missions WHERE state IN (?,?))",
                (resource, now, RUNNING, PAUSING))
            try:
                self.db.execute(
                    "INSERT INTO mission_resource_leases(resource,mission_id,token,lease_until,updated_at) "
                    "VALUES(?,?,?,?,?)", (resource, mission_id, token,
                                           now + int(lease_s), now))
                self.db.commit()
                return token
            except sqlite3.IntegrityError:
                self.db.rollback()
                return None

    def claim_execution(self, nonce, mission_id, run_token, lease_s=300):
        """Create an execution latch only while this exact Mission claim is live.

        The latch and lifecycle check share the Mission SQLite transaction.  A
        recovery fence that wins first therefore prevents ActionStore EXECUTING;
        a worker that wins first leaves a renewable latch which reconciliation
        must wait out instead of deleting its browser lease/idempotency key.
        """
        resource = "mission-action:" + str(nonce)
        token, now = secrets.token_hex(16), int(time.time())
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            owner = self.db.execute(
                "SELECT 1 FROM missions WHERE mission_id=? AND state=? AND run_token=?",
                (mission_id, RUNNING, run_token)).fetchone()
            if not owner:
                self.db.rollback()
                return None, None
            try:
                self.db.execute(
                    "INSERT INTO mission_resource_leases(resource,mission_id,token,lease_until,updated_at) "
                    "VALUES(?,?,?,?,?)", (resource, mission_id, token,
                                           now + int(lease_s), now))
                self.db.commit()
                return resource, token
            except sqlite3.IntegrityError:
                self.db.rollback()
                return None, None

    def active_resources(self, mission_id, now=None):
        now = int(now if now is not None else time.time())
        return [dict(r) for r in self.db.execute(
            "SELECT resource,token,lease_until FROM mission_resource_leases "
            "WHERE mission_id=? AND lease_until>? ORDER BY resource",
            (mission_id, now)).fetchall()]

    def renew_resource(self, resource, mission_id, token, lease_s=300):
        now = int(time.time())
        with self._lock:
            cur = self.db.execute(
                "UPDATE mission_resource_leases SET lease_until=?,updated_at=? "
                "WHERE resource=? AND mission_id=? AND token=?",
                (now + int(lease_s), now, resource, mission_id, token))
            self.db.commit()
        return cur.rowcount == 1

    def release_resource(self, resource, mission_id, token):
        with self._lock:
            cur = self.db.execute(
                "DELETE FROM mission_resource_leases WHERE resource=? AND mission_id=? AND token=?",
                (resource, mission_id, token))
            self.db.commit()
        return cur.rowcount == 1

    def release_resources_for_mission(self, mission_id):
        with self._lock:
            cur = self.db.execute(
                "DELETE FROM mission_resource_leases WHERE mission_id=?", (mission_id,))
            self.db.commit()
        return cur.rowcount

    def finish_run(self, mission_id, token, state, result=None):
        """Token-guarded transition; a stale worker cannot overwrite pause/cancel."""
        now = int(time.time())
        vals = [state]
        extra = ""
        if result is not None:
            extra = ",result=?"
            vals.append(result)
        vals.extend([now, mission_id, RUNNING, token])
        with self._lock:
            cur = self.db.execute(
                "UPDATE missions SET state=?,run_token='',lease_until=0%s,updated_at=? "
                "WHERE mission_id=? AND state=? AND run_token=?" % extra, vals)
            self.db.commit()
        return cur.rowcount == 1

    def set_case_owned(self, mission_id, token, case):
        now = int(time.time())
        with self._lock:
            cur = self.db.execute(
                "UPDATE missions SET case_json=?,updated_at=? "
                "WHERE mission_id=? AND state IN (?,?) AND run_token=?",
                (_js(case), now, mission_id, RUNNING, PAUSING, token))
            self.db.commit()
        return cur.rowcount == 1

    def park_for_confirm(self, mission_id, token, name, nonce, result):
        """Atomically publish an awaiting row and NEEDS_YOU lifecycle state.

        If pause wins the SQLite write lock first, no awaiting row is created and
        the caller can safely revoke/release its not-yet-fired proposal. If this
        transaction wins first, a later pause records paused_from=NEEDS_YOU and
        resume restores the exact confirmation inbox.
        """
        now = int(time.time())
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            cur = self.db.execute(
                "UPDATE missions SET state=?,run_token='',lease_until=0,result=?,updated_at=? "
                "WHERE mission_id=? AND state=? AND run_token=?",
                (NEEDS_YOU, (result or "confirm needed")[:200], now,
                 mission_id, RUNNING, token))
            if cur.rowcount:
                self.db.execute(
                    "INSERT INTO mission_steps(mission_id,name,nonce,verdict,at) VALUES(?,?,?,?,?)",
                    (mission_id, name, nonce, _AWAITING, now))
            self.db.commit()
        return cur.rowcount == 1

    def settle_pausing(self, mission_id, token):
        """Owner acknowledgement: the current action boundary is now quiescent."""
        now = int(time.time())
        with self._lock:
            cur = self.db.execute(
                "UPDATE missions SET state=?,run_token='',lease_until=0,result=?,updated_at=? "
                "WHERE mission_id=? AND state=? AND run_token=?",
                (PAUSED, "paused at an action boundary", now,
                 mission_id, PAUSING, token))
            self.db.commit()
        return cur.rowcount == 1

    def pause(self, mission_id):
        """Cooperatively pause at the next action boundary, preserving where to resume."""
        now = int(time.time())
        with self._lock:
            # A RUNNING owner keeps its token until it acknowledges the next
            # boundary.  Resume is therefore impossible while its side effect is
            # still in flight, which prevents an old and a new worker overlapping.
            cur = self.db.execute(
                "UPDATE missions SET state=?,paused_from=?,result=?,updated_at=? "
                "WHERE mission_id=? AND state=? AND COALESCE(run_token,'')<>''",
                (PAUSING, QUEUED, "pause requested; waiting for current action boundary",
                 now, mission_id, RUNNING))
            if cur.rowcount == 0:
                active = (QUEUED, WAITING, NEEDS_YOU)
                marks = ",".join("?" for _ in active)
                cur = self.db.execute(
                    "UPDATE missions SET state=?,paused_from=state,run_token='',lease_until=0,"
                    "result=?,updated_at=? WHERE mission_id=? AND state IN (%s)" % marks,
                    (PAUSED, "paused by user", now, mission_id, *active))
            self.db.commit()
        return cur.rowcount == 1

    def resume_paused(self, mission_id):
        now = int(time.time())
        with self._lock:
            r = self.db.execute(
                "SELECT paused_from FROM missions WHERE mission_id=? AND state=?",
                (mission_id, PAUSED)).fetchone()
            if not r:
                return None
            target = r["paused_from"] or QUEUED
            if target == RUNNING:
                target = QUEUED
            cur = self.db.execute(
                "UPDATE missions SET state=?,paused_from='',result=?,updated_at=? "
                "WHERE mission_id=? AND state=?",
                (target, "resumed by user", now, mission_id, PAUSED))
            self.db.commit()
        return target if cur.rowcount == 1 else None

    def cancel(self, mission_id, result="cancelled by user"):
        """Terminal, idempotent cancellation plus durable-wait cleanup."""
        now = int(time.time())
        nonterminal = (QUEUED, RUNNING, PAUSING, WAITING, NEEDS_YOU, PAUSED,
                       RECOVERY_REQUIRED, RECONCILING)
        marks = ",".join("?" for _ in nonterminal)
        with self._lock:
            # Keep the state read, transition, and resource decision under one
            # cross-connection write lock.  Otherwise a daemon can claim QUEUED
            # between the read and UPDATE and cancellation can delete the browser
            # lease from underneath its already-running primitive.
            self.db.execute("BEGIN IMMEDIATE")
            cur = self.db.execute(
                "UPDATE missions SET state=?,run_token='',lease_until=0,result=?,updated_at=? "
                "WHERE mission_id=? AND state IN (%s)" % marks,
                (CANCELLED, result[:200], now, mission_id, *nonterminal))
            if cur.rowcount:
                self.db.execute(
                    "UPDATE mission_waits SET state='cancelled' "
                    "WHERE mission_id=? AND state='pending'", (mission_id,))
                # Never remove a live execution/browser lease on cancellation;
                # the owner may already be inside an external side effect. It
                # releases at its boundary, or expires for safe later reclamation.
                self.db.execute(
                    "DELETE FROM mission_resource_leases WHERE mission_id=? AND lease_until<=?",
                    (mission_id, now))
            self.db.commit()
        m = self.get(mission_id)
        return bool(m and m.state == CANCELLED)

    def begin_reconcile(self, mission_id, note="", lease_s=300):
        """Fence a recovery while ActionStore cleanup happens in another DB.

        RECONCILING is persistent and non-runnable, so a service crash is safe and
        the same explicit command can resume the cleanup.  It also closes the gap
        where a daemon could claim QUEUED before old approvals were revoked.
        """
        now, token = int(time.time()), secrets.token_hex(16)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            r = self.db.execute(
                "SELECT state,case_json FROM missions WHERE mission_id=?",
                (mission_id,)).fetchone()
            if not r:
                self.db.commit()
                return False
            if r["state"] == RECONCILING:
                cur = self.db.execute(
                    "UPDATE missions SET run_token=?,lease_until=?,updated_at=? "
                    "WHERE mission_id=? AND state=? AND "
                    "(COALESCE(run_token,'')='' OR lease_until<=?)",
                    (token, now + int(lease_s), now, mission_id, RECONCILING, now))
                self.db.commit()
                return token if cur.rowcount == 1 else None
            if r["state"] != RECOVERY_REQUIRED:
                self.db.commit()
                return None
            case = _jl(r["case_json"])
            updates = case.get("human_updates")
            if not isinstance(updates, list):
                updates = []
            updates.append({"at": now, "note": (note or
                "recovery inspected; safe to continue")[:500], "recovery": True})
            case["human_updates"] = updates[-20:]
            cur = self.db.execute(
                "UPDATE missions SET state=?,case_json=?,run_token=?,lease_until=?,result=?,updated_at=? "
                "WHERE mission_id=? AND state=?",
                (RECONCILING, _js(case), token, now + int(lease_s),
                 "recovery cleanup in progress", now,
                 mission_id, RECOVERY_REQUIRED))
            self.db.commit()
        return token if cur.rowcount == 1 else None

    def release_reconcile(self, mission_id, token):
        """Release a failed/busy cleanup owner while keeping the durable fence."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE missions SET run_token='',lease_until=0 WHERE mission_id=? "
                "AND state=? AND run_token=?", (mission_id, RECONCILING, token))
            self.db.commit()
        return cur.rowcount == 1

    def owns_reconcile(self, mission_id, token, renew_s=300):
        """Renew a live recovery-cleanup lease without reviving an expired owner."""
        now = int(time.time())
        with self._lock:
            cur = self.db.execute(
                "UPDATE missions SET lease_until=?,updated_at=? WHERE mission_id=? "
                "AND state=? AND run_token=? AND lease_until>?",
                (now + int(renew_s), now, mission_id, RECONCILING, token, now))
            self.db.commit()
        return cur.rowcount == 1

    def finish_reconcile(self, mission_id, token):
        """Publish QUEUED only after cleanup; losing callers touch no new lease."""
        now = int(time.time())
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            owner = self.db.execute(
                "SELECT 1 FROM missions WHERE mission_id=? AND state=? "
                "AND run_token=? AND lease_until>?",
                (mission_id, RECONCILING, token, now)).fetchone()
            if not owner:
                self.db.commit()
                return False
            active = self.db.execute(
                "SELECT 1 FROM mission_resource_leases WHERE mission_id=? "
                "AND lease_until>? LIMIT 1", (mission_id, now)).fetchone()
            if active:
                self.db.commit()
                return False
            cur = self.db.execute(
                "UPDATE missions SET state=?,run_token='',lease_until=0,result=?,updated_at=? "
                "WHERE mission_id=? AND state=? AND run_token=?",
                (QUEUED, "uncertain run explicitly reconciled", now,
                 mission_id, RECONCILING, token))
            if cur.rowcount:
                # This is the only cross-table publication boundary. Resolve any
                # confirmation row from the uncertain run before QUEUED becomes
                # runnable; an EXECUTED/EXECUTING ActionStore row remains in the
                # receipts/key ledger, but can no longer strand a later done state
                # behind a stale confirmation inbox.
                self.db.execute(
                    "UPDATE mission_steps SET verdict='reconciled-uncertain' "
                    "WHERE mission_id=? AND verdict=?", (mission_id, _AWAITING))
                # A crash between reserve_action() and ActionStore.propose()/bind
                # proves no nonce was materialized. Clear only these orphan rows,
                # inside the owner-token transaction, so a stale reconciler cannot
                # delete a same-key reservation made by a fresh run.
                orphans = self.db.execute(
                    "SELECT reservation_id FROM mission_action_keys WHERE mission_id=? "
                    "AND state='reserved' AND COALESCE(nonce,'')=''",
                    (mission_id,)).fetchall()
                for orphan in orphans:
                    reservation_id = orphan["reservation_id"] or ""
                    if reservation_id:
                        # The event ledger stays append-only. Quota queries ignore
                        # a proposal only when this compensating event proves its
                        # exact reservation never materialized in ActionStore.
                        self.db.execute(
                            "INSERT INTO mission_events(mission_id,kind,name,nonce,payload_json,at) "
                            "VALUES(?,?,?,?,?,?)",
                            (mission_id, "retracted_irreversible", "reconcile",
                             reservation_id, _js({"reason": "never materialized"}), now))
                self.db.execute(
                    "DELETE FROM mission_action_keys WHERE mission_id=? "
                    "AND state='reserved' AND COALESCE(nonce,'')=''", (mission_id,))
                self.db.execute(
                    "DELETE FROM mission_resource_leases WHERE mission_id=?", (mission_id,))
            self.db.commit()
        return cur.rowcount == 1

    def reconcile_recovery(self, mission_id, note=""):
        """Store-only compatibility helper; services use the fenced two phases."""
        token = self.begin_reconcile(mission_id, note)
        return bool(token and self.finish_reconcile(mission_id, token))

    def accept_handoff(self, mission_id):
        now = int(time.time())
        with self._lock:
            cur = self.db.execute(
                "UPDATE missions SET state=?,result=?,updated_at=? "
                "WHERE mission_id=? AND state=? AND COALESCE(run_token,'')=''",
                (DONE_ACCEPTED, "handed off to human", now, mission_id, NEEDS_YOU))
            self.db.commit()
        return cur.rowcount == 1

    def continue_handoff(self, mission_id, note=""):
        """Return a human-assisted hand-off to Collie without declaring it done."""
        now = int(time.time())
        with self._lock:
            r = self.db.execute(
                "SELECT case_json FROM missions WHERE mission_id=? AND state=? "
                "AND COALESCE(run_token,'')=''",
                (mission_id, NEEDS_YOU)).fetchone()
            parked = self.db.execute(
                "SELECT 1 FROM mission_steps WHERE mission_id=? AND verdict=? LIMIT 1",
                (mission_id, _AWAITING)).fetchone()
            if not r or parked:
                return False
            case = _jl(r["case_json"])
            updates = case.get("human_updates")
            if not isinstance(updates, list):
                updates = []
            updates.append({"at": now, "note": (note or "human step completed")[:500]})
            case["human_updates"] = updates[-20:]
            cur = self.db.execute(
                "UPDATE missions SET state=?,case_json=?,result=?,updated_at=? "
                "WHERE mission_id=? AND state=? AND COALESCE(run_token,'')=''",
                (QUEUED, _js(case), "human step completed; ready to continue", now,
                 mission_id, NEEDS_YOU))
            self.db.commit()
        return cur.rowcount == 1

    def record_step(self, mission_id, name, nonce, verdict):
        with self._lock:
            self.db.execute(
                "INSERT INTO mission_steps(mission_id,name,nonce,verdict,at)"
                " VALUES(?,?,?,?,?)",
                (mission_id, name, nonce, verdict, int(time.time())))
            self.db.commit()

    def last_parked(self, mission_id):
        """The newest still-unresolved gated action (name, nonce) awaiting confirm."""
        r = self.db.execute(
            "SELECT name,nonce FROM mission_steps WHERE mission_id=? AND verdict=? "
            "ORDER BY step_id DESC LIMIT 1", (mission_id, _AWAITING)).fetchone()
        return (r["name"], r["nonce"]) if r else (None, None)

    def resolve_parked(self, nonce, verdict):
        with self._lock:
            self.db.execute(
                "UPDATE mission_steps SET verdict=? WHERE nonce=? AND verdict=?",
                (verdict, nonce, _AWAITING))
            self.db.commit()

    def steps(self, mission_id):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM mission_steps WHERE mission_id=? ORDER BY step_id",
            (mission_id,))]

    # ── the durable loop table ──
    def schedule_wait(self, mission_id, fire_at):
        with self._lock:
            self.db.execute(
                "UPDATE mission_waits SET state='superseded' "
                "WHERE mission_id=? AND state='pending'", (mission_id,))
            self.db.execute(
                "INSERT INTO mission_waits(mission_id,fire_at,state,created_at)"
                " VALUES(?,?,?,?)", (mission_id, int(fire_at), "pending", int(time.time())))
            self.db.commit()

    def due_waits(self, now):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM mission_waits WHERE state='pending' AND fire_at<=? "
            "ORDER BY fire_at", (int(now),))]

    def claim_wait(self, wait_id):
        with self._lock:
            cur = self.db.execute(
                "UPDATE mission_waits SET state='fired' WHERE wait_id=? AND state='pending'",
                (wait_id,))
            self.db.commit()
        return cur.rowcount == 1

    def next_wait(self, mission_id):
        r = self.db.execute(
            "SELECT * FROM mission_waits WHERE mission_id=? AND state='pending' "
            "ORDER BY fire_at LIMIT 1", (mission_id,)).fetchone()
        return dict(r) if r else None

    def claim_due_wait(self, now, mission_id=None, force=False, lease_s=300):
        """Claim a wait and its mission run slot in one transaction.

        A paused mission therefore keeps its pending wake instead of having a
        daemon consume it before noticing the pause.
        """
        now = int(now)
        where = ["w.state='pending'", "m.state=?", "COALESCE(m.run_token,'')='' "]
        args = [WAITING]
        if mission_id:
            where.append("w.mission_id=?")
            args.append(mission_id)
        if not force:
            where.append("w.fire_at<=?")
            args.append(now)
        token = secrets.token_hex(16)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            r = self.db.execute(
                "SELECT w.wait_id,w.mission_id FROM mission_waits w "
                "JOIN missions m ON m.mission_id=w.mission_id WHERE "
                + " AND ".join(where) + " ORDER BY w.fire_at LIMIT 1", args).fetchone()
            if not r:
                self.db.commit()
                return None
            mc = self.db.execute(
                "UPDATE missions SET state=?,run_token=?,lease_until=?,updated_at=? "
                "WHERE mission_id=? AND state=? AND COALESCE(run_token,'')=''",
                (RUNNING, token, now + int(lease_s), now, r["mission_id"], WAITING))
            wc = self.db.execute(
                "UPDATE mission_waits SET state='fired' WHERE wait_id=? AND state='pending'",
                (r["wait_id"],))
            if mc.rowcount != 1 or wc.rowcount != 1:
                self.db.rollback()
                return None
            self.db.commit()
        return r["mission_id"], token

    def record_event(self, mission_id, kind, name="", nonce="", payload=None):
        with self._lock:
            self.db.execute(
                "INSERT INTO mission_events(mission_id,kind,name,nonce,payload_json,at) "
                "VALUES(?,?,?,?,?,?)",
                (mission_id, kind, name or "", nonce or "",
                 _js(_compact_event(payload or {})), int(time.time())))
            self.db.commit()

    def events(self, mission_id, limit=20):
        rows = self.db.execute(
            "SELECT kind,name,nonce,payload_json,at FROM mission_events "
            "WHERE mission_id=? ORDER BY event_id DESC LIMIT ?",
            (mission_id, int(limit))).fetchall()
        return [{"kind": r["kind"], "name": r["name"], "nonce": r["nonce"],
                 "payload": _jl(r["payload_json"]), "at": r["at"]}
                for r in reversed(rows)]

    def reserve_action(self, mission_id, action_key, irreversible, leash, name,
                       payload, run_token):
        """Atomically fence ownership and enforce totals/rates/idempotency."""
        now = int(time.time())
        max_irrev = int((leash or {}).get("max_irreversible_actions", 100))
        hourly = int((leash or {}).get("actions_per_hour", 12))
        reservation_id = secrets.token_hex(16)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            owner = self.db.execute(
                "SELECT 1 FROM missions WHERE mission_id=? AND state=? AND run_token=?",
                (mission_id, RUNNING, run_token)).fetchone()
            if not owner:
                self.db.commit()
                return False, "mission run ownership lost before action reservation", 0
            if irreversible:
                irrev = self.db.execute(
                    "SELECT COUNT(*) n FROM mission_events p WHERE p.mission_id=? "
                    "AND p.kind='proposed_irreversible' AND NOT EXISTS ("
                    "SELECT 1 FROM mission_events r WHERE r.mission_id=p.mission_id "
                    "AND r.kind='retracted_irreversible' AND r.nonce=p.nonce)",
                    (mission_id,)).fetchone()["n"]
                if irrev >= max_irrev:
                    self.db.commit()
                    return False, "mission irreversible-action budget exhausted", 0
                recent = self.db.execute(
                    "SELECT p.at FROM mission_events p WHERE p.mission_id=? "
                    "AND p.kind='proposed_irreversible' AND p.at>? AND NOT EXISTS ("
                    "SELECT 1 FROM mission_events r WHERE r.mission_id=p.mission_id "
                    "AND r.kind='retracted_irreversible' AND r.nonce=p.nonce) ORDER BY p.at",
                    (mission_id, now - 3600)).fetchall()
                if len(recent) >= hourly:
                    self.db.commit()
                    return False, "mission external-action rate limit reached", recent[0]["at"] + 3600
            if action_key:
                old = self.db.execute(
                    "SELECT state FROM mission_action_keys WHERE mission_id=? AND action_key=?",
                    (mission_id, action_key)).fetchone()
                if old:
                    self.db.commit()
                    return False, "duplicate external action blocked (%s)" % old["state"], 0
                self.db.execute(
                    "INSERT INTO mission_action_keys(mission_id,action_key,state,at,"
                    "owner_token,reservation_id) VALUES(?,?,?,?,?,?)",
                    (mission_id, action_key, "reserved", now,
                     run_token, reservation_id))
            kind = "proposed_irreversible" if irreversible else "proposed"
            self.db.execute(
                "INSERT INTO mission_events(mission_id,kind,name,nonce,payload_json,at) "
                "VALUES(?,?,?,?,?,?)", (mission_id, kind, name, reservation_id,
                                          _js(_compact_event(payload or {})), now))
            self.db.commit()
        return True, "", 0

    def reserve_decision(self, mission_id, leash):
        """Durable model-turn ceiling (persists across every wait/restart)."""
        now = int(time.time())
        cap = int((leash or {}).get("max_total_steps", 1000))
        with self._lock:
            total = self.db.execute(
                "SELECT COUNT(*) n FROM mission_events WHERE mission_id=? AND kind='decision'",
                (mission_id,)).fetchone()["n"]
            if total >= cap:
                return False
            self.db.execute(
                "INSERT INTO mission_events(mission_id,kind,name,payload_json,at) "
                "VALUES(?,?,?,?,?)", (mission_id, "decision", "model", "{}", now))
            self.db.commit()
        return True

    def bind_action_key(self, mission_id, action_key, nonce, run_token):
        if not action_key:
            return True
        with self._lock:
            cur = self.db.execute(
                "UPDATE mission_action_keys SET nonce=?,state='materialized' "
                "WHERE mission_id=? AND action_key=? AND owner_token=? "
                "AND state='reserved' AND EXISTS (SELECT 1 FROM missions m "
                "WHERE m.mission_id=? AND m.state=? AND m.run_token=?)",
                (nonce, mission_id, action_key, run_token,
                 mission_id, RUNNING, run_token))
            self.db.commit()
        return cur.rowcount == 1

    def _append_action_retractions(self, mission_id, rows, now, reason):
        """Append quota compensations for exact reservations proven not to fire.

        Caller holds ``_lock`` and an open write transaction.
        """
        for row in rows:
            reservation_id = row["reservation_id"] or ""
            if reservation_id:
                self.db.execute(
                    "INSERT INTO mission_events(mission_id,kind,name,nonce,payload_json,at) "
                    "VALUES(?,?,?,?,?,?)",
                    (mission_id, "retracted_irreversible", "release",
                     reservation_id, _js({"reason": reason}), now))

    def release_action_key(self, mission_id, action_key, run_token):
        """Release only this run's proven-unfired reservation (ABA-safe)."""
        if not action_key:
            return True
        now = int(time.time())
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT reservation_id FROM mission_action_keys "
                "WHERE mission_id=? AND action_key=? AND owner_token=? "
                "AND state IN ('reserved','materialized') "
                "AND EXISTS (SELECT 1 FROM missions m WHERE m.mission_id=? "
                "AND m.state IN (?,?) AND m.run_token=?)",
                (mission_id, action_key, run_token,
                 mission_id, RUNNING, PAUSING, run_token)).fetchall()
            cur = self.db.execute(
                "DELETE FROM mission_action_keys WHERE mission_id=? AND action_key=? "
                "AND owner_token=? AND state IN ('reserved','materialized') "
                "AND EXISTS (SELECT 1 FROM missions m WHERE m.mission_id=? "
                "AND m.state IN (?,?) AND m.run_token=?)",
                (mission_id, action_key, run_token,
                 mission_id, RUNNING, PAUSING, run_token))
            if cur.rowcount:
                self._append_action_retractions(
                    mission_id, rows, now, "proven no side effect before release")
            self.db.commit()
        return cur.rowcount == 1

    def release_action_nonces(self, mission_id, nonces):
        nonces = [n for n in (nonces or []) if n]
        if not nonces:
            return 0
        marks = ",".join("?" for _ in nonces)
        now = int(time.time())
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT reservation_id FROM mission_action_keys WHERE mission_id=? "
                "AND nonce IN (%s) AND state='materialized'" % marks,
                (mission_id, *nonces)).fetchall()
            cur = self.db.execute(
                "DELETE FROM mission_action_keys WHERE mission_id=? AND nonce IN (%s) "
                "AND state='materialized'" % marks, (mission_id, *nonces))
            if cur.rowcount:
                self._append_action_retractions(
                    mission_id, rows, now, "action record proves no side effect")
            self.db.commit()
        return cur.rowcount

    def complete_action_key(self, mission_id, nonce, state="executed"):
        with self._lock:
            self.db.execute(
                "UPDATE mission_action_keys SET state=? WHERE mission_id=? AND nonce=?",
                (state, mission_id, nonce))
            self.db.commit()

    def list(self, state=None):
        q, a = "SELECT mission_id FROM missions", ()
        if state:
            q, a = q + " WHERE state=?", (state,)
        return [self.get(r["mission_id"]) for r in self.db.execute(q + " ORDER BY created_at", a)]

    def close(self):
        # Coordinate with a heartbeat that may already be inside SQLite.  The
        # heartbeat also catches a close that won the race, so no daemon thread
        # can leak a ProgrammingError during shutdown.
        with self._lock:
            self.db.close()


# ── the driver: model decides the flow, container gates + persists it ───────
class MissionDriver:
    """Advance a mission by repeatedly asking the decider for the next action and
    running it through the leash gate. Model-free at EXECUTION (each primitive
    runs deterministically); the model only chooses what to do next.

    `decider(goal, case, primitives) -> {"action","args","reason"}` where action is
    a registered primitive name OR a control move: 'wait' (args.seconds),
    'needs_human' (args.summary), 'done'.
    """

    # a runaway decider (loops forever choosing reversible actions) must not spin;
    # after this many actions in one advance, park for the human. A durable errand
    # makes progress in a few actions then waits — it does not need dozens per wake.
    max_steps = 40
    # anti-poll-spin: after this many CONSECUTIVE reversible reads (e.g. observe the
    # inbox again and again), force a durable wait instead of reading in a tight loop.
    # In the world each read is a real, slow browser fetch — polling 40x is wrong;
    # a monitor should read, then WAIT. Resets when an irreversible action fires.
    read_streak_cap = 3
    read_wait_s = 3600

    def __init__(self, store: MissionStore, actions: ActionStore, decider,
                 capabilities=None):
        self.store = store
        self.actions = actions
        self.decider = decider
        self.capabilities = ({c.name: c for c in capabilities}
                             if capabilities is not None else None)

    def _capabilities(self):
        return list(self.capabilities.values()) if self.capabilities is not None \
            else all_capabilities()

    def _capability(self, name):
        return self.capabilities.get(name) if self.capabilities is not None \
            else get_capability(name)

    def _primitives(self, leash=None):
        """What the decider may choose from — the registered neutral primitives,
        as {name, risk, reversible, description, args_hint}. Domain-agnostic."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return [{"name": c.name, "risk": c.risk, "reversible": c.reversible,
                 "description": c.description, "args": c.args_hint}
                for c in self._capabilities()
                if not _leash.evaluate(leash or {}, c.name, c.risk,
                                       now_iso=now).denied]

    def _execute(self, nonce, cap, run_token):
        """Run an APPROVED action's real side effect once and capture its result
        (the receipt carries only the verdict; the campaign needs the payload)."""
        captured = {}
        rec = self.actions.get(nonce)
        resource_spec = getattr(cap, "resource", None)
        resource = resource_spec(rec) if callable(resource_spec) else resource_spec
        resource_token = None
        resource_hb = None
        execution_resource, execution_token = self.store.claim_execution(
            nonce, rec.job_id, run_token)
        if not execution_token:
            raise RefusedError("mission execution fence lost before side effect")
        execution_hb = None

        def start_lease_heartbeat(name, token, thread_name):
            stop = threading.Event()

            def renew():
                while not stop.wait(20):
                    try:
                        if not self.store.renew_resource(
                                name, rec.job_id, token):
                            return
                    except (sqlite3.Error, RuntimeError):
                        return

            thread = threading.Thread(target=renew, name=thread_name, daemon=True)
            thread.start()
            return stop, thread

        execution_hb = start_lease_heartbeat(
            execution_resource, execution_token, "mission-execution-heartbeat")

        def _side(rec):
            res = cap.execute(rec)
            captured["r"] = res
            return res

        try:
            if resource:
                resource_token = self.store.claim_resource(resource, rec.job_id)
                if not resource_token:
                    raise ResourceBusy(f"external resource busy: {resource}")
                resource_hb = start_lease_heartbeat(
                    resource, resource_token, "mission-resource-heartbeat")
            receipt = self.actions.execute(
                nonce, side_effect_fn=_side, donecheck_fn=cap.verify,
                unchanged_fn=getattr(cap, "unchanged", None))
            return Verdict(receipt.verdict, receipt.verdict_reason), captured.get("r")
        finally:
            if resource_hb:
                self._stop_heartbeat(*resource_hb)
            if resource_token:
                self.store.release_resource(resource, rec.job_id, resource_token)
            if execution_hb:
                self._stop_heartbeat(*execution_hb)
            self.store.release_resource(
                execution_resource, rec.job_id, execution_token)

    def _fold(self, m, name, result, token=None):
        """Merge an action's result into the case: under its own name, plus any
        top-level keys it explicitly promoted via result['case']."""
        case = self.store.get(m.mission_id).case
        if isinstance(result, dict):
            case[name] = {k: v for k, v in result.items() if k != "case"} or result
            if isinstance(result.get("case"), dict):
                case.update(result["case"])
        elif result is not None:
            case[name] = result
        return self.store.set_case_owned(m.mission_id, token, case) if token \
            else self.store.set_case(m.mission_id, case)

    def _state(self, mission_id, fallback=FAILED_S):
        m = self.store.get(mission_id)
        return m.state if m else fallback

    @staticmethod
    def _action_key(cap, args, snapshot):
        if cap.reversible:
            return ""
        # A model-supplied idempotency label is not trusted authority: allowing it
        # to replace semantic identity lets the same action use a new label on each
        # turn and fire repeatedly. Browser tab ids and ephemeral DOM refs have the
        # same problem after reopening an otherwise identical target.
        verification_only = {
            "_case", "_leash", "reason", "idempotency_key",
            "success_text", "success_url_contains", "expect_title",
        }
        semantic_args = getattr(cap, "semantic_args", None)
        if semantic_args is None:
            raise ValueError(
                "irreversible capability %s has no semantic_args projection" % cap.name)
        if callable(semantic_args):
            clean = semantic_args({k: v for k, v in (args or {}).items()
                                   if k not in verification_only})
        else:
            clean = {k: (args or {}).get(k) for k in semantic_args
                     if k in (args or {})}
        snap = snapshot or {}
        stable_target = {
            k: snap.get(k) for k in ("url", "button", "form_digest")
            if snap.get(k) not in (None, "")
        }
        target_line = str(snap.get("target") or "")
        if target_line:
            target_line = re.sub(r"\[(?:e|ref[:=]?)?\d+\]", "", target_line,
                                 flags=re.I)
            stable_target["target"] = " ".join(target_line.split())
        material = {"capability": cap.name, "args": clean,
                    "target": stable_target}
        raw = json.dumps(material, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _bound_refusal(leash, cap, args, snapshot):
        """Capability-independent deterministic target checks."""
        sensitive_key = re.compile(
            r"pass(word|code)?|secret|token|api.?key|otp|one.?time|"
            r"verification.?code|cvv|cvc|card.?number|ssn|social.?security|"
            r"e.?mail|phone|mobile|street.?address|postal|zip.?code|birth|dob|"
            r"user.?name", re.I)

        def sensitive_path(value, path=""):
            if isinstance(value, dict):
                for key, child in value.items():
                    if str(key).startswith("_"):
                        continue
                    child_path = (path + "." + str(key)).strip(".")
                    if sensitive_key.search(str(key)) and child not in (None, "", [], {}):
                        return child_path
                    found = sensitive_path(child, child_path)
                    if found:
                        return found
            elif isinstance(value, list):
                for i, child in enumerate(value):
                    found = sensitive_path(child, "%s[%d]" % (path, i))
                    if found:
                        return found
            elif isinstance(value, str) and re.search(
                    r"(?:password|passcode|email|phone|otp|card(?: number)?)\s*"
                    r"(?:is|=|:)\s*\S+", value, re.I):
                return path or "text"
            return ""

        secret_at = sensitive_path(args or {})
        if secret_at:
            return ("human-required: credential/PII field %s must be entered in the browser "
                    "without persisting it in Mission state" % secret_at)
        allowed = (leash or {}).get("allowed_domains")
        urls = [str((args or {}).get(k) or "") for k in ("url", "target")]
        urls.append(str((snapshot or {}).get("url") or ""))
        if cap.reversible:
            for raw_url in urls:
                u = urlsplit(raw_url)
                if re.search(
                        r"(?:^|[/?&=])(?:log-?out|sign-?out|unsubscribe|delete|remove|"
                        r"deactivate|activate|verify|confirm)(?:[/?&=]|$)",
                        u.path + "?" + u.query, re.I):
                    return "consequential navigation requires an irreversible gated capability"
        hosts = [urlsplit(u).hostname or "" for u in urls if u]
        if allowed:
            pats = [str(x).lower() for x in allowed]
            for host in hosts:
                if not any(fnmatch.fnmatchcase(host.lower(), p) for p in pats):
                    return "target domain %r is outside leash.allowed_domains" % host
        # Generic publish primitives are intentionally not payment primitives.
        trigger = " ".join(str((args or {}).get(k) or "")
                           for k in ("button", "submit", "submit_selector"))
        if cap.risk != "pay" and re.search(
                r"\b(pay|purchase|buy|checkout|place[-_ ]?order)\b", trigger, re.I):
            return "commerce requires a dedicated pay capability with a bound amount"
        if cap.risk == "pay" and not any((args or {}).get(k) not in (None, "", 0, "0")
                                          for k in ("spend_usd", "amount_usd")):
            return "payment amount must be explicit and payload-bound"
        return ""

    def _finish(self, mission_id, token, state, result=None):
        if not self.store.finish_run(mission_id, token, state, result):
            return self._lost_state(mission_id, token)
        return self._state(mission_id, state)

    def _lost_state(self, mission_id, token):
        # PAUSING becomes resumable only after the owner reaches this boundary.
        self.store.settle_pausing(mission_id, token)
        return self._state(mission_id)

    def _start_heartbeat(self, mission_id, token):
        stop = threading.Event()

        def beat():
            while not stop.wait(20):
                try:
                    if not self.store.renew_run(mission_id, token):
                        return
                except (sqlite3.Error, RuntimeError):
                    return

        thread = threading.Thread(target=beat, name="mission-heartbeat", daemon=True)
        thread.start()
        return stop, thread

    @staticmethod
    def _stop_heartbeat(stop, thread):
        stop.set()
        thread.join(timeout=2)

    def advance(self, mission_id) -> str:
        """Drive the mission until it must stop: a gate (needs_you), a wait
        (WAITING), a hand-off (needs_you), completion, or failure. Re-entrant: safe
        to call again after a loop tick or a confirm+resume."""
        m = self.store.get(mission_id)
        if not m or m.state in (NEEDS_YOU, PAUSED, PAUSING,
                                RECOVERY_REQUIRED, WAITING) or m.terminal:
            return m.state if m else FAILED_S
        token = self.store.claim_run(mission_id, expected=(QUEUED,))
        if not token:
            return self._state(mission_id)
        return self._drive_claimed(mission_id, token)

    def _drive_claimed(self, mission_id, token, heartbeat=True) -> str:
        """Drive a mission whose RUNNING slot has already been atomically claimed."""
        reads = 0                                     # consecutive reversible reads (anti-poll-spin)
        heartbeat_pair = self._start_heartbeat(mission_id, token) if heartbeat else None
        try:
            # A confirmed action may be waiting only because the shared browser
            # profile was busy.  Retry that exact, already-approved nonce before
            # asking the model to propose anything new.
            parked_name, parked_nonce = self.store.last_parked(mission_id)
            parked_rec = self.actions.get(parked_nonce) if parked_nonce else None
            if parked_rec and parked_rec.state == "approved":
                return self._run_parked_inner(
                    mission_id, token, parked_name, parked_nonce)
            for _ in range(self.max_steps):
                if not self.store.owns_run(mission_id, token):
                    return self._lost_state(mission_id, token)
                m = self.store.get(mission_id)
                if not self.store.reserve_decision(mission_id, m.leash):
                    return self._finish(mission_id, token, NEEDS_YOU,
                                        "mission model-turn budget exhausted")
                model_case = dict(m.case)
                model_case["_authority"] = m.leash
                model_case["_recent_events"] = self.store.events(mission_id, 20)
                decision = self.decider(m.goal, model_case, self._primitives(m.leash)) or {}
                # A pause/cancel arriving during the model call wins before another
                # primitive is proposed or fired.
                if not self.store.owns_run(mission_id, token):
                    return self._lost_state(mission_id, token)
                action = decision.get("action")
                args = decision.get("args") or {}
                reason = decision.get("reason") or ""

                if action is None:
                    self.store.record_event(mission_id, "control", "invalid", payload={"reason": reason})
                    return self._finish(mission_id, token, NEEDS_YOU,
                                        "driver returned no next action")
                if action == DONE:
                    # A model report is not evidence and is not human acceptance.
                    self.store.record_step(mission_id, DONE, "", "reported")
                    self.store.record_event(mission_id, "control", DONE,
                                            payload={"reason": reason})
                    return self._finish(mission_id, token, NEEDS_YOU,
                                        reason or "model reports done; review and accept")
                if action == NEEDS_HUMAN:
                    self.store.record_step(mission_id, NEEDS_HUMAN, "", NEEDS_HUMAN)
                    self.store.record_event(mission_id, "control", NEEDS_HUMAN,
                                            payload={"summary": args.get("summary") or reason})
                    return self._finish(
                        mission_id, token, NEEDS_YOU,
                        args.get("summary") or reason or "needs your input")
                if action == WAIT:
                    try:
                        secs = max(1, min(int(args.get("seconds", 3600)), 31536000))
                    except (TypeError, ValueError):
                        secs = 3600
                    self.store.schedule_wait(mission_id, int(time.time()) + secs)
                    self.store.record_event(mission_id, "control", WAIT,
                                            payload={"seconds": secs, "reason": reason,
                                                     "transient": bool(args.get("transient"))})
                    return self._finish(mission_id, token, WAITING,
                                        reason or f"waiting {secs}s")

                cap = self._capability(action)
                if not cap:
                    return self._finish(mission_id, token, FAILED_S,
                                        f"unknown action {action!r}")
                spend = args.get("spend_usd", args.get("amount_usd", 0))
                try:
                    spend = float(spend or 0)
                except (TypeError, ValueError):
                    spend = 0.0
                dec = _leash.evaluate(
                    m.leash, cap.name, cap.risk, spend_usd=spend,
                    now_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                if dec.denied:
                    return self._finish(mission_id, token, FAILED_S,
                                        f"leash denied: {dec.reason}")
                if cap.reversible and reads >= self.read_streak_cap:
                    self.store.schedule_wait(mission_id, int(time.time()) + self.read_wait_s)
                    return self._finish(
                        mission_id, token, WAITING,
                        f"paced: waited after {reads} reads before more {cap.name}")

                call_args = dict(args, _case=m.case, _leash=m.leash)
                bound_refusal = self._bound_refusal(m.leash, cap, call_args, {})
                if bound_refusal:
                    state = NEEDS_YOU if bound_refusal.startswith("human-required:") else FAILED_S
                    return self._finish(mission_id, token, state,
                                        ("needs your input: " if state == NEEDS_YOU else
                                         "leash denied: ") + bound_refusal.split(": ", 1)[-1])
                snapshot_fn = getattr(cap, "snapshot", None)
                snapshot = snapshot_fn(call_args, mission_id) if callable(snapshot_fn) else {}
                bound_refusal = self._bound_refusal(m.leash, cap, call_args, snapshot)
                if bound_refusal:
                    state = NEEDS_YOU if bound_refusal.startswith("human-required:") else FAILED_S
                    return self._finish(mission_id, token, state,
                                        ("needs your input: " if state == NEEDS_YOU else
                                         "leash denied: ") + bound_refusal.split(": ", 1)[-1])
                action_key = self._action_key(cap, call_args, snapshot)
                ok, why, retry_at = self.store.reserve_action(
                    mission_id, action_key, not cap.reversible, m.leash, cap.name,
                    {"args": {k: v for k, v in call_args.items()
                              if k not in ("_case", "_leash")},
                     "target": snapshot}, token)
                if not ok:
                    if retry_at:
                        self.store.schedule_wait(mission_id, retry_at)
                        return self._finish(mission_id, token, WAITING, why)
                    if not self.store.owns_run(mission_id, token):
                        return self._lost_state(mission_id, token)
                    return self._finish(mission_id, token, NEEDS_YOU, why)
                # Close the long-snapshot -> ActionStore gap as tightly as
                # possible. reserve_action already checked this token inside its
                # transaction; this second boundary catches recovery/pause before
                # a proposal row is materialized in the separate database.
                if not self.store.owns_run(mission_id, token):
                    self.store.release_action_key(mission_id, action_key, token)
                    return self._lost_state(mission_id, token)
                nonce = self.actions.propose(cap.name, call_args, risk=cap.risk,
                                             job_id=mission_id, leash_id=mission_id,
                                             snapshot=snapshot)
                if not self.store.bind_action_key(
                        mission_id, action_key, nonce, token):
                    self.actions.refuse(nonce, "mission ownership changed before binding")
                    self.store.release_action_key(mission_id, action_key, token)
                    return self._lost_state(mission_id, token)
                if not self.store.owns_run(mission_id, token):
                    self.actions.refuse(nonce, "mission paused or cancelled")
                    self.store.release_action_key(mission_id, action_key, token)
                    return self._lost_state(mission_id, token)
                if dec.decision == _leash.ASK:
                    parked_result = f"confirm needed: {cap.name} — {reason}"[:200]
                    if self.store.park_for_confirm(
                            mission_id, token, cap.name, nonce, parked_result):
                        return self._state(mission_id, NEEDS_YOU)
                    # Pause/cancel won the lifecycle transaction before the
                    # confirmation inbox became visible. No side effect fired, so
                    # retire the proposal/key and let the winning state settle.
                    self.actions.refuse(nonce, "mission paused or cancelled before confirmation")
                    self.store.release_action_key(mission_id, action_key, token)
                    return self._lost_state(mission_id, token)

                self.actions.confirm(nonce)
                if not self.store.owns_run(mission_id, token):
                    self.actions.refuse(nonce, "mission paused or cancelled")
                    self.store.release_action_key(mission_id, action_key, token)
                    return self._lost_state(mission_id, token)
                try:
                    verdict, result = self._execute(nonce, cap, token)
                except ResourceBusy:
                    self.actions.refuse(nonce, "shared external resource busy; retry scheduled")
                    self.store.release_action_key(mission_id, action_key, token)
                    if not self.store.owns_run(mission_id, token):
                        return self._lost_state(mission_id, token)
                    self.store.schedule_wait(mission_id, int(time.time()) + 5)
                    return self._finish(mission_id, token, WAITING,
                                        "shared external resource busy; retrying shortly")
                except RefusedError as e:
                    # The action latch was never acquired (pause/recovery won), or
                    # the approved world snapshot diverged before firing. Both are
                    # proven no-side-effect paths and may release the semantic key.
                    self.actions.refuse(nonce, "action did not reach execution: %s" % e)
                    self.store.release_action_key(mission_id, action_key, token)
                    if not self.store.owns_run(mission_id, token):
                        return self._lost_state(mission_id, token)
                    return self._finish(mission_id, token, NEEDS_YOU,
                                        "still blocked before execution: %s" % e)
                self.store.record_step(mission_id, cap.name, nonce, verdict.status)
                self.store.complete_action_key(mission_id, nonce, verdict.status)
                self.store.record_event(
                    mission_id, "result", cap.name, nonce,
                    {"verdict": verdict.status, "reason": verdict.reason,
                     "result": _compact_event(result, 2000)})
                # An already-started primitive may finish after cancel. Preserve its
                # receipt, but never let its stale worker mutate campaign state/case.
                if not self.store.owns_claim(mission_id, token):
                    return self._lost_state(mission_id, token)
                if verdict.status == VERIFIED:
                    if not self._fold(m, cap.name, result, token=token):
                        return self._lost_state(mission_id, token)
                # PAUSING may commit the completed result above, but must settle
                # before any verdict routing or next model/action boundary.
                if not self.store.owns_run(mission_id, token):
                    return self._lost_state(mission_id, token)
                if verdict.status == FAILED:
                    return self._finish(mission_id, token, FAILED_S,
                                        f"{cap.name} failed: {verdict.reason}")
                if verdict.status != VERIFIED:
                    return self._finish(
                        mission_id, token, NEEDS_YOU,
                        f"{cap.name} fired but remains uncertain: {verdict.reason}")
                reads = reads + 1 if cap.reversible else 0

            return self._finish(mission_id, token, NEEDS_YOU,
                                "step budget exhausted — needs your input")
        except Exception as e:
            return self._finish(
                mission_id, token, FAILED_S,
                f"mission driver failed: {type(e).__name__}: {e}"[:200])
        finally:
            if heartbeat_pair:
                self._stop_heartbeat(*heartbeat_pair)

    def confirm_and_resume(self, mission_id, nonce) -> str:
        """Approve exactly this mission's parked payload, execute it, and continue."""
        m = self.store.get(mission_id)
        if not m or m.state != NEEDS_YOU:
            return m.state if m else FAILED_S
        name, parked = self.store.last_parked(mission_id)
        rec = self.actions.get(nonce)
        if parked != nonce or not rec or rec.job_id != mission_id or rec.leash_id != mission_id:
            return NEEDS_YOU
        token = self.store.claim_run(mission_id, expected=(NEEDS_YOU,))
        if not token:
            return self._state(mission_id)
        # Re-check after the claim; another control request may have won the race.
        heartbeat_pair = self._start_heartbeat(mission_id, token)
        try:
            if not self.store.owns_run(mission_id, token):
                return self._lost_state(mission_id, token)
            try:
                if rec.state == "pending":
                    self.actions.confirm(nonce)
                elif rec.state != "approved":
                    raise RefusedError(f"not confirmable (state={rec.state})")
            except RefusedError as e:
                return self._finish(mission_id, token, NEEDS_YOU,
                                    f"confirm refused: {e}")
            return self._run_parked(mission_id, token, name, nonce,
                                    heartbeat=False)
        finally:
            self._stop_heartbeat(*heartbeat_pair)

    def _run_parked(self, mission_id, token, name, nonce, heartbeat=True):
        heartbeat_pair = self._start_heartbeat(mission_id, token) if heartbeat else None
        try:
            return self._run_parked_inner(mission_id, token, name, nonce)
        finally:
            if heartbeat_pair:
                self._stop_heartbeat(*heartbeat_pair)

    def _run_parked_inner(self, mission_id, token, name, nonce):
        cap = self._capability(name)
        if not cap:
            return self._finish(mission_id, token, FAILED_S,
                                f"unknown parked action {name!r}")
        if not self.store.owns_run(mission_id, token):
            rec = self.actions.get(nonce)
            if self.actions.refuse(nonce, "mission paused or cancelled"):
                self.store.resolve_parked(nonce, "paused-before-execute")
                self.store.release_action_nonces(mission_id, [nonce])
            return self._lost_state(mission_id, token)
        try:
            verdict, result = self._execute(nonce, cap, token)
        except ResourceBusy:
            # Confirmation remains bound to this exact approved payload.  Back off
            # durably and retry it on wake; do not strand RUNNING or ask the model
            # to synthesize a second action.
            if not self.store.owns_run(mission_id, token):
                return self._lost_state(mission_id, token)
            self.store.schedule_wait(mission_id, int(time.time()) + 5)
            return self._finish(mission_id, token, WAITING,
                                "shared external resource busy; confirmed action will retry")
        except RefusedError as e:
            if "world diverged" in str(e):
                self.actions.refuse(nonce, "approved target changed before execution")
                self.store.resolve_parked(nonce, "target-changed")
                rec = self.actions.get(nonce)
                if rec:
                    # It did not fire; a freshly prepared target may be proposed.
                    self.store.release_action_nonces(mission_id, [nonce])
            return self._finish(mission_id, token, NEEDS_YOU, f"still blocked: {e}")
        self.store.resolve_parked(nonce, verdict.status)   # flip the awaiting row to its real verdict
        self.store.complete_action_key(mission_id, nonce, verdict.status)
        self.store.record_event(
            mission_id, "result", name, nonce,
            {"verdict": verdict.status, "reason": verdict.reason,
             "result": _compact_event(result, 2000)})
        if not self.store.owns_claim(mission_id, token):
            return self._lost_state(mission_id, token)
        m = self.store.get(mission_id)
        if verdict.status == VERIFIED:
            if not self._fold(m, name, result, token=token):
                return self._lost_state(mission_id, token)
        if not self.store.owns_run(mission_id, token):
            return self._lost_state(mission_id, token)
        if verdict.status == FAILED:
            return self._finish(mission_id, token, FAILED_S,
                                f"{name} failed: {verdict.reason}")
        if verdict.status != VERIFIED:
            return self._finish(
                mission_id, token, NEEDS_YOU,
                f"{name} fired but remains uncertain: {verdict.reason}")
        return self._drive_claimed(mission_id, token, heartbeat=False)

    def accept_handoff(self, mission_id) -> str:
        """Explicitly end a needs_human hand-off; never overloaded as resume."""
        name, nonce = self.store.last_parked(mission_id)
        if nonce:
            return NEEDS_YOU
        self.store.accept_handoff(mission_id)
        return self._state(mission_id)

    def resume(self, mission_id) -> str:
        """Compatibility for callers that already approved a parked nonce.

        New control surfaces use confirm_and_resume() and accept_handoff()
        explicitly; lifecycle resume means PAUSED -> its prior state.
        """
        m = self.store.get(mission_id)
        if not m or m.state != NEEDS_YOU:
            return m.state if m else FAILED_S
        name, nonce = self.store.last_parked(mission_id)
        if not nonce:
            return self.accept_handoff(mission_id)
        rec = self.actions.get(nonce)
        if not rec or rec.state != "approved":
            return NEEDS_YOU
        token = self.store.claim_run(mission_id, expected=(NEEDS_YOU,))
        return self._run_parked(mission_id, token, name, nonce) if token \
            else self._state(mission_id)

    def tick_missions(self, now=None) -> int:
        """Re-enter every mission whose durable wait is due. The one-line wiring for
        colliejobd (plan §5.2): the daemon owns no model — it wakes due campaigns,
        and advance() asks the model for the next action. Returns how many advanced."""
        now = int(now if now is not None else time.time())
        n = 0
        # Creation is persistence-first. Either the Web's /run request or this
        # daemon scan claims a queued mission; the SQL token makes the loser a no-op.
        for m in self.store.list(state=QUEUED):
            before = m.state
            self.advance(m.mission_id)
            if before == QUEUED and self.store.get(m.mission_id).state != QUEUED:
                n += 1
        while True:
            claimed = self.store.claim_due_wait(now)
            if not claimed:
                break
            mid, token = claimed
            self._drive_claimed(mid, token)
            n += 1
        return n

    def wake(self, mission_id, now=None, force=True) -> str:
        """Wake only the named WAITING mission; force=True implements Check now."""
        now = int(now if now is not None else time.time())
        claimed = self.store.claim_due_wait(now, mission_id=mission_id, force=force)
        if not claimed:
            return self._state(mission_id)
        mid, token = claimed
        return self._drive_claimed(mid, token)


# ── leash builder: authority bounds, NOT an errand template ──────────────────
def world_leash(may=None, autonomous=False, expires=None, **bounds) -> dict:
    """Build a mission leash. `may` defaults to the neutral primitive families, so
    a mission can research/compose/observe and act on the web WITHIN the gate.
    `autonomous=True` pre-authorizes the irreversible primitives (still within the
    other bounds); otherwise they park for confirm. Only bounds enforced by
    deterministic host checks should be supplied; opaque metadata is not authority."""
    default_may = ["research", "compose", "observe", "web.*",
                   "browse", "browse.*"]
    known = {"spend_max_usd", "allowed_domains", "max_total_steps",
             "max_irreversible_actions", "actions_per_hour"}
    unknown = sorted(set(bounds) - known)
    if unknown:
        raise ValueError("unenforced Mission leash bound(s): " + ", ".join(unknown))
    for key in ("max_total_steps", "max_irreversible_actions", "actions_per_hour"):
        if key in bounds:
            try:
                bounds[key] = int(bounds[key])
            except (TypeError, ValueError):
                raise ValueError("Mission leash %s must be a positive integer" % key)
            if bounds[key] < 1:
                raise ValueError("Mission leash %s must be a positive integer" % key)
    if "allowed_domains" in bounds:
        if not isinstance(bounds["allowed_domains"], (list, tuple)) or not all(
                isinstance(x, str) and x.strip() for x in bounds["allowed_domains"]):
            raise ValueError("Mission leash allowed_domains must be a non-empty string list")
        bounds["allowed_domains"] = [x.strip().lower() for x in bounds["allowed_domains"]]
    if "spend_max_usd" in bounds:
        try:
            bounds["spend_max_usd"] = max(0.0, float(bounds["spend_max_usd"]))
        except (TypeError, ValueError):
            raise ValueError("Mission leash spend_max_usd must be numeric")
    leash = {"may": sorted(default_may if may is None else may),
             "irreversible": "allow" if autonomous else "confirm",
             # Durable campaign-wide limits; unlike max_steps-per-advance these
             # survive every wait, restart, and competing daemon.
             "max_total_steps": 1000,
             "max_irreversible_actions": 100,
             "actions_per_hour": 12}
    if expires:
        leash["expires"] = expires
    leash.update(bounds)
    return leash


# ── the model decider: NL goal + case + primitives -> the next action ───────
_SYS = (
    "You are collie's mission driver. You are pursuing ONE goal over time. Given the "
    "goal, what you already know (the case), and the actions available, choose the "
    "SINGLE next action. Reply with STRICT JSON and nothing else:\n"
    '{"action": <a primitive name | "wait" | "needs_human" | "done">, '
    '"args": {..}, "reason": "<one short clause>"}\n'
    "Rules: use only a listed primitive; pick 'wait' with args.seconds to poll or "
    "let time pass; 'needs_human' with args.summary to hand a decision back to the "
    "user; 'done' only when the goal is actually achieved. Irreversible actions "
    "(publish/send/pay) will be confirmed by the user unless pre-authorized — "
    "propose them anyway; the gate handles authority.\n"
    "When WAITING on an external event (a reply, availability, a price drop): observe "
    "ONCE, and if nothing has changed use 'wait' (with args.seconds) — do NOT observe/"
    "read repeatedly in a row; a monitor reads, then waits, then reads again later.\n"
    "To ACT on a website (fill a marketplace listing, submit a form, publish a post): use "
    "'browse' with a goal to fill/navigate it (it drives the real browser adaptively and STOPS "
    "before submitting), then 'browse.submit' to click the final Publish/Post — that last click "
    "is gated for the user's confirm.\n"
    "The code capability is not part of the default world leash; it is shown only when the user "
    "explicitly scopes and enables it.\n")


class ModelDecider:
    """Production decider: one model call per step. Kept deliberately thin — the
    container owns durability/gate/evidence, so a wrong or malformed reply can only
    pick among registered primitives (a bad JSON parse -> a safe hand-off)."""

    def __init__(self, provider):
        self.provider = provider

    def __call__(self, goal, case, primitives) -> dict:
        cat = "\n".join(
            f"- {p['name']} ({'reversible' if p['reversible'] else 'IRREVERSIBLE'}): "
            f"{p['description']}  args: {p['args'] or '{}'}" for p in primitives)
        ctx = json.dumps(case, ensure_ascii=False)[:4000]
        user = f"GOAL: {goal}\n\nCASE (what you know):\n{ctx}\n\nPRIMITIVES:\n{cat}"
        try:
            comp = self.provider.complete(_SYS, [{"role": "user", "content": user}], [])
            if getattr(comp, "stop_reason", "") == "error":
                from .providers import classify_error
                detail = getattr(comp, "error_detail", "") or getattr(comp, "text", "")
                kind = classify_error(detail, int(getattr(comp, "error_status", 0) or 0))
                if kind == "retryable":
                    recent = [e for e in (case.get("_recent_events") or [])
                              if e.get("kind") == "control" and e.get("name") == WAIT and
                              (e.get("payload") or {}).get("transient")]
                    delay = min(3600, 60 * (2 ** min(len(recent), 6)))
                    return {"action": WAIT,
                            "args": {"seconds": delay, "transient": True},
                            "reason": "temporary model/provider error; retry with backoff"}
            else:
                import re
                m = re.search(r"\{.*\}", getattr(comp, "text", "") or "", re.S)
                if m:
                    plan = json.loads(m.group(0))
                    if isinstance(plan, dict) and plan.get("action"):
                        return plan
        except Exception as e:
            from .providers import classify_error
            if classify_error(str(e)) == "retryable":
                return {"action": WAIT, "args": {"seconds": 60, "transient": True},
                        "reason": "temporary model transport error; retry with backoff"}
        # any failure -> hand back to the human rather than guess an action
        return {"action": NEEDS_HUMAN,
                "args": {"summary": "could not decide the next step automatically"},
                "reason": "decider unavailable"}


def create_mission(store: MissionStore, mission_id, goal, case=None, leash=None) -> Mission:
    """Start a campaign from a goal in the user's words + an intake case + a leash.
    No per-errand template: the decider generalizes the flow from here."""
    return store.create(mission_id, goal, leash=leash or world_leash(), case=case or {})
