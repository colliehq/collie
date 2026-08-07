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

from .actions import RefusedError
from .jobs import Executor, WAITING, FAILED_S

PENDING_W = "pending"
FIRED_W = "fired"


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
            fired_at INTEGER)""")
        self.db.commit()

    def schedule(self, job_id: str, nonce: str, fire_at: int, kind: str = "timer",
                 now: int = None) -> int:
        """Park a proposed action until fire_at; the job goes to WAITING."""
        now = int(now if now is not None else time.time())
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO waits(job_id,nonce,kind,fire_at,state,created_at,fired_at)"
                " VALUES(?,?,?,?,?,?,0)",
                (job_id, nonce, kind, int(fire_at), PENDING_W, now))
            self.db.commit()
            wid = cur.lastrowid
        if job_id:
            self.jobs.set_state(job_id, WAITING, f"waiting until {fire_at}")
        return wid

    def due(self, now: int):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM waits WHERE state=? AND fire_at<=? ORDER BY fire_at",
            (PENDING_W, int(now)))]

    def tick(self, now: int = None) -> int:
        """Fire every due wait by driving its action. Returns how many fired.
        This IS catch-up-on-wake: called on daemon start it clears all overdue
        waits at once. A drive that refuses (leash/parked) still marks the wait
        fired — the job carries the resulting state (needs_you/failed)."""
        now = int(now if now is not None else time.time())
        fired = 0
        for w in self.due(now):
            # ATOMIC claim: only ONE ticker (a concurrent daemon + `wake`) may drive
            # a given wait. Claim pending->fired BEFORE driving; if we lost the race
            # (rowcount 0), skip — the winner drives it. Prevents double-fire.
            with self._lock:
                claimed = self.db.execute(
                    "UPDATE waits SET state=?,fired_at=? WHERE wait_id=? AND state=?",
                    (FIRED_W, now, w["wait_id"], PENDING_W))
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
                    self.jobs.set_state(w["job_id"], FAILED_S, f"wait dropped: {e}")
            fired += 1                                 # already claimed FIRED above
        return fired

    def pending_waits(self):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM waits WHERE state=? ORDER BY fire_at", (PENDING_W,))]

    def serve(self, interval: float = 60.0, now_fn=time.time, stop=None,
              extra_tick=None):
        """Catch up immediately, then tick on an interval.

        ``extra_tick`` runs in a non-overlapping worker so a slow Mission model or
        browser call cannot delay ordinary reminders.  Shutdown waits for that
        worker's current boundary before callers close its durable stores.
        ``stop`` is a callable for tests / clean shutdown.
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
