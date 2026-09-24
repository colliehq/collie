"""Durable waiting + catch-up-on-wake — the colliejobd substrate (plan §5.2).

A delegate spends most of its life WAITING (a timer, an email, a page change).
Waiting must be durable state, not a live process (plan rule 8): the machine can
sleep or reboot. So a wait is a row on disk; on wake the daemon processes every
overdue wait (catch-up-on-wake), which on WSL2 is the honest semantics — the VM
stops when Windows sleeps, and we reconcile when it comes back, rather than
pretending to be 24/7.

The load-bearing, fully-tested piece is tick(now): fire every due wait by DRIVING
its action through the Executor (a reversible in-scope action runs; an
irreversible one parks in needs_you for confirm). serve() is a thin loop around
tick() for the daemon; the daemon holds no long-lived model process.

Timer waits are implemented here. Email/page-change waits need live credentials
and are the documented next step; they schedule the same way (a due predicate
instead of a fire_at), so this table is their substrate too.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

from .actions import EXECUTING, PENDING, RefusedError
from .jobs import Executor, WAITING, FAILED_S, NEEDS_YOU, RECOVERY_REQUIRED

PENDING_W = "pending"
CLAIMED_W = "claimed"
FIRED_W = "fired"
_LEASE_SECONDS = 300


class Scheduler:
    def __init__(self, actions, jobs, db_path: str = None):
        self.actions = actions
        self.jobs = jobs
        self.executor = Executor(actions, jobs)
        path = db_path or os.path.expanduser("~/.collie/jobs.db")
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
        self.db.execute("""CREATE TABLE IF NOT EXISTS waits(
            wait_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, nonce TEXT,
            kind TEXT, fire_at INTEGER, state TEXT, created_at INTEGER,
            fired_at INTEGER, claimed_at INTEGER DEFAULT 0,
            lease_until INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '')""")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(waits)")}
        for name, decl in (("claimed_at", "INTEGER DEFAULT 0"),
                           ("lease_until", "INTEGER DEFAULT 0"),
                           ("attempts", "INTEGER DEFAULT 0"),
                           ("last_error", "TEXT DEFAULT ''")):
            if name not in cols:
                self.db.execute("ALTER TABLE waits ADD COLUMN %s %s" % (name, decl))
        # Startup reconciliation: a process may have died after claiming but before recording the
        # verdict. Expired claims go back to pending and are safely retried through the action
        # store's nonce/idempotency boundary.
        now = int(time.time())
        self.db.execute(
            "UPDATE waits SET state=?,claimed_at=0,lease_until=0 "
            "WHERE state=? AND lease_until<=?", (PENDING_W, CLAIMED_W, now))
        self.db.commit()

    def schedule(self, job_id: str, nonce: str, fire_at: int, kind: str = "timer",
                 now: int = None) -> int:
        """Park a proposed action until fire_at; the job goes to WAITING."""
        if not isinstance(job_id, str) or not isinstance(nonce, str) or not nonce:
            raise ValueError("scheduled wait needs a string job id and action nonce")
        if isinstance(fire_at, bool) or not isinstance(fire_at, int) or fire_at < 0:
            raise ValueError("fire_at must be a non-negative integer timestamp")
        if now is None:
            now = int(time.time())
        elif isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("schedule time must be a non-negative integer timestamp")
        if not isinstance(kind, str) or not kind or len(kind) > 40:
            raise ValueError("wait kind must be a non-empty bounded string")
        if job_id:
            job = self.jobs.get(job_id)
            if not job or job.terminal or job.state == RECOVERY_REQUIRED:
                raise ValueError("scheduled wait job is missing, terminal, or recovering")
            action = self.actions.get(nonce)
            if (not action or action.job_id != job_id or
                    action.state not in (PENDING, EXECUTING)):
                raise ValueError(
                    "scheduled wait must own one pending or interrupted action for the same job")
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                old = self.db.execute(
                    "SELECT wait_id,job_id,kind,fire_at FROM waits WHERE nonce=? "
                    "ORDER BY wait_id LIMIT 1", (nonce,)).fetchone()
                if old:
                    if (old["job_id"] != job_id or old["kind"] != kind or
                            old["fire_at"] != fire_at):
                        raise ValueError("action nonce is already bound to a different wait")
                    wid = old["wait_id"]
                else:
                    cur = self.db.execute(
                        "INSERT INTO waits(job_id,nonce,kind,fire_at,state,created_at,fired_at)"
                        " VALUES(?,?,?,?,?,?,0)",
                        (job_id, nonce, kind, fire_at, PENDING_W, now))
                    wid = cur.lastrowid
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        if job_id:
            self.jobs.set_state(job_id, WAITING, f"waiting until {fire_at}")
        return wid

    def due(self, now: int):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM waits WHERE fire_at<=? AND "
            "(state=? OR (state=? AND lease_until<=?)) ORDER BY fire_at",
            (int(now), PENDING_W, CLAIMED_W, int(now)))]

    def tick(self, now: int = None) -> int:
        """Fire every due wait by driving its action. Returns how many fired.
        This IS catch-up-on-wake: called on daemon start it clears all overdue
        waits at once. A drive that refuses (leash/parked) still marks the wait
        fired — the job carries the resulting state (needs_you/failed)."""
        now = int(now if now is not None else time.time())
        fired = 0
        for w in self.due(now):
            # ATOMIC lease: only ONE ticker (a concurrent daemon + `wake`) may drive a wait.  It is
            # not marked fired until drive returns. A crash leaves a recoverable expired claim,
            # instead of the old terminal "fired" row that silently lost the action.
            with self._lock:
                claimed = self.db.execute(
                    "UPDATE waits SET state=?,claimed_at=?,lease_until=?,attempts=attempts+1 "
                    "WHERE wait_id=? AND (state=? OR (state=? AND lease_until<=?))",
                    (CLAIMED_W, now, now + _LEASE_SECONDS, w["wait_id"],
                     PENDING_W, CLAIMED_W, now))
                self.db.commit()
            if claimed.rowcount != 1:
                continue
            try:
                self.executor.drive(w["nonce"])
            except RefusedError as e:
                # a due wait that can't be driven must SURFACE as failed, never be
                # silently orphaned in WAITING (that would look like a reminder
                # that just vanished). Anti-fabrication defense-in-depth.
                if w.get("job_id"):
                    current = self.actions.get(w["nonce"])
                    if current and current.state == EXECUTING:
                        # It may have died after the side effect started but before its receipt.
                        # Never call that "dropped" or retry it automatically.
                        self.jobs.set_state(
                            w["job_id"], NEEDS_YOU,
                            "wait outcome unknown after interrupted execution; inspect before retrying")
                    else:
                        self.jobs.set_state(w["job_id"], FAILED_S, f"wait dropped: {e}")
                with self._lock:
                    self.db.execute(
                        "UPDATE waits SET state=?,fired_at=?,lease_until=0,last_error=? "
                        "WHERE wait_id=? AND state=?", (FIRED_W, now, str(e)[:500],
                                                       w["wait_id"], CLAIMED_W))
                    self.db.commit()
                fired += 1
            except Exception as e:
                # Release for the next tick. Executor/action nonces provide the idempotency fence if
                # a process died after the side effect but before this bookkeeping step.
                with self._lock:
                    self.db.execute(
                        "UPDATE waits SET state=?,claimed_at=0,lease_until=0,last_error=? "
                        "WHERE wait_id=? AND state=?", (PENDING_W,
                                                       "%s: %s" % (type(e).__name__, e),
                                                       w["wait_id"], CLAIMED_W))
                    self.db.commit()
                continue
            else:
                with self._lock:
                    self.db.execute(
                        "UPDATE waits SET state=?,fired_at=?,lease_until=0,last_error='' "
                        "WHERE wait_id=? AND state=?", (FIRED_W, now, w["wait_id"], CLAIMED_W))
                    self.db.commit()
                fired += 1
        return fired

    def pending_waits(self):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM waits WHERE state=? ORDER BY fire_at", (PENDING_W,))]

    def serve(self, interval: float = 60.0, now_fn=time.time, stop=None,
              extra_tick=None, heartbeat=None):
        """Catch up immediately, then tick on an interval.

        ``extra_tick`` runs in a non-overlapping worker so a slow Mission model or
        browser call cannot delay ordinary reminders.  Shutdown waits for that
        worker's current boundary before callers close its durable stores.
        ``stop`` is a callable for tests / clean shutdown. ``heartbeat(now, extra_busy)`` is called
        from the main lane every tick, so a long Mission tick cannot make the daemon look gone.
        """
        extra_worker = [None]
        extra_lock = threading.Lock()

        def _run_extra(now):
            try:
                extra_tick(now)
            finally:
                with extra_lock:
                    extra_worker[0] = None

        def _tick():
            now = int(now_fn())
            self.tick(now)
            if heartbeat is not None:
                try:
                    heartbeat(now, extra_worker[0] is not None)
                except Exception:
                    pass
            if extra_tick:
                # A Mission tick can spend minutes in a model/browser call. Run
                # it in its own lane so ordinary reminders remain punctual, and
                # never overlap two Mission scans in this process.
                with extra_lock:
                    if extra_worker[0] is None:
                        t = threading.Thread(target=_run_extra, args=(now,),
                                             name="mission-tick", daemon=True)
                        extra_worker[0] = t
                        t.start()

        try:
            _tick()                                  # catch-up-on-wake
            while not (stop and stop()):
                time.sleep(interval)
                _tick()
        finally:
            t = extra_worker[0]
            if t:
                # The caller owns resources used by extra_tick and closes them as
                # soon as serve() returns.  A timed join could therefore close a
                # live Mission SQLite connection underneath this worker.
                t.join()

    def close(self):
        self.db.close()
