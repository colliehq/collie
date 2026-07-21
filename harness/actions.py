"""Confirm-token + deterministic executor + receipts — the irreversible-action seam.

The verifier (verifier.py) decides whether an outcome happened; this module
decides whether an irreversible action is ALLOWED to happen and then performs it
WITHOUT a model in the loop. It is the §5.1/§5.2 spine of the delegate plan:

  1. A gated action never executes in the step that proposes it. The proposing
     step materializes the exact action (propose -> nonce) and stops; the run
     exits needs_you. State lives on disk, so it survives the proposing process
     dying (the whole reason the confirm boundary IS a step boundary).
  2. A human approves the materialized record (confirm(nonce)) — approving a
     concrete payload, not an English sentence.
  3. A deterministic executor (execute()) runs the approved action verbatim,
     runs the done-check, and writes a receipt. No model reasons here, so an
     injected page cannot talk the executor into a different action.

Six guarantees, each pinned by tests/test_actions.py:
  - single-use     an approved nonce fires the side effect AT MOST once (no double-send)
  - durable        propose/confirm survive process restart (on-disk SQLite)
  - payload-bound  the args are hashed at propose; tampering before execute is refused
  - TOCTOU-safe    if the world diverged from the approved snapshot, execute refuses
  - fail-closed    an unconfirmed (merely proposed) nonce cannot execute
  - evidenced      every execution writes a receipt carrying the done-check verdict

The side effect and the done-check are INJECTED (side_effect_fn, donecheck_fn),
so this layer is fully testable with a counter + a fixture and performs no real
irreversible action itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from . import redact as _redact
from .verifier import INCONCLUSIVE, Verdict

# action lifecycle
PENDING = "pending"      # materialized, awaiting human confirm
APPROVED = "approved"    # human confirmed; executor may run it once
EXECUTING = "executing"  # claimed by the executor (single-use latch)
EXECUTED = "executed"    # side effect fired; receipt written
REFUSED = "refused"      # rejected by a guard (never fired)
EXPIRED = "expired"      # TTL elapsed before confirm


def _j(o) -> str:
    return json.dumps(o or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(capability: str, args: dict, leash_id: str = "", job_id: str = "",
            risk: str = "", snapshot: dict = None) -> str:
    """Bind the payload AND its authority-bearing fields (leash, job, risk,
    snapshot), so tampering any of them after propose is caught at execute.

    Honest limit: this is a plain SHA-256 — an attacker who can write the DB can
    recompute it. Real integrity needs an HMAC keyed by a secret held OUTSIDE the
    state DB (env/keyring); that is the intended follow-up. Widening the digest is
    cheap defense-in-depth that catches naive/partial tampers today."""
    payload = "\x00".join([capability, _j(args), leash_id or "", job_id or "",
                           risk or "", _j(snapshot)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ActionRecord:
    nonce: str
    capability: str
    args: dict
    digest: str
    risk: str
    state: str
    job_id: str = ""
    leash_id: str = ""
    snapshot: dict = field(default_factory=dict)
    created_at: int = 0
    expires_at: int = 0


@dataclass
class Receipt:
    """The durable answer to: what did collie do, under which leash, who approved
    it, and how was it verified. Written for every execute() attempt that fires
    (or is refused after approval)."""
    nonce: str
    capability: str
    approved: bool
    verdict: str
    verdict_reason: str
    evidence: str = ""
    args_redacted: str = ""
    job_id: str = ""
    leash_id: str = ""
    fired: bool = False
    created_at: int = 0


class RefusedError(Exception):
    """A guard rejected the action; the side effect did NOT fire."""


class ActionStore:
    def __init__(self, path: str = None):
        path = path or os.path.expanduser("~/.collie/actions.db")
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
        self._init()

    def _init(self):
        c = self.db.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS pending_actions(
            nonce TEXT PRIMARY KEY, job_id TEXT, capability TEXT, args_json TEXT,
            digest TEXT, risk TEXT, leash_id TEXT, snapshot_json TEXT, state TEXT,
            created_at INTEGER, expires_at INTEGER, decided_at INTEGER,
            executed_at INTEGER, attempted_at INTEGER, refuse_reason TEXT)""")
        try:  # guarded migration for a db created before attempted_at existed
            c.execute("ALTER TABLE pending_actions ADD COLUMN attempted_at INTEGER")
        except sqlite3.OperationalError:
            pass
        c.execute("""CREATE TABLE IF NOT EXISTS receipts(
            receipt_id INTEGER PRIMARY KEY AUTOINCREMENT, nonce TEXT, job_id TEXT,
            capability TEXT, args_redacted TEXT, leash_id TEXT, approved INTEGER,
            fired INTEGER, verdict TEXT, verdict_reason TEXT, evidence TEXT,
            created_at INTEGER)""")
        self.db.commit()

    # ── propose: materialize the exact action, return a payload-bound nonce ──
    def propose(self, capability: str, args: dict, risk: str = "irreversible",
                job_id: str = "", leash_id: str = "", snapshot: dict = None,
                ttl_s: int = 86400) -> str:
        nonce = secrets.token_hex(16)
        now = int(time.time())
        with self._lock:
            self.db.execute(
                """INSERT INTO pending_actions(nonce,job_id,capability,args_json,digest,
                     risk,leash_id,snapshot_json,state,created_at,expires_at,
                     decided_at,executed_at,attempted_at,refuse_reason)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,0,0,0,'')""",
                (nonce, job_id, capability, json.dumps(args or {}, ensure_ascii=False),
                 _digest(capability, args, leash_id, job_id, risk, snapshot), risk, leash_id,
                 json.dumps(snapshot or {}, ensure_ascii=False), PENDING,
                 now, now + int(ttl_s)))
            self.db.commit()
        return nonce

    def _row(self, nonce):
        cur = self.db.execute("SELECT * FROM pending_actions WHERE nonce=?", (nonce,))
        return cur.fetchone()

    def get(self, nonce) -> ActionRecord:
        r = self._row(nonce)
        if not r:
            return None
        return ActionRecord(
            nonce=r["nonce"], capability=r["capability"],
            args=json.loads(r["args_json"] or "{}"), digest=r["digest"],
            risk=r["risk"], state=r["state"], job_id=r["job_id"],
            leash_id=r["leash_id"], snapshot=json.loads(r["snapshot_json"] or "{}"),
            created_at=r["created_at"], expires_at=r["expires_at"])

    # ── confirm: a human approves the concrete record (single transition) ──
    def confirm(self, nonce) -> ActionRecord:
        now = int(time.time())
        with self._lock:
            r = self._row(nonce)
            if not r:
                raise RefusedError("unknown nonce")
            if r["state"] != PENDING:
                raise RefusedError(f"not pending (state={r['state']})")
            if r["expires_at"] and now > r["expires_at"]:
                self.db.execute("UPDATE pending_actions SET state=?,decided_at=? WHERE nonce=?",
                                (EXPIRED, now, nonce))
                self.db.commit()
                raise RefusedError("expired before confirm")
            self.db.execute("UPDATE pending_actions SET state=?,decided_at=? WHERE nonce=?",
                            (APPROVED, now, nonce))
            self.db.commit()
        return self.get(nonce)

    # ── execute: the deterministic, model-free executor ──
    def execute(self, nonce, side_effect_fn, donecheck_fn=None,
                unchanged_fn=None, redact_fn=None) -> Receipt:
        """Perform an APPROVED action exactly once, then verify + write a receipt.

        side_effect_fn(record) -> anything   the real irreversible action
        donecheck_fn(record, result) -> Verdict   post-action verification
        unchanged_fn(record) -> bool         TOCTOU: True iff world still matches
                                             the approved snapshot (else refuse)
        redact_fn(args) -> str               redact args before they hit a receipt
        """
        now = int(time.time())
        with self._lock:
            r = self._row(nonce)
            if not r:
                raise RefusedError("unknown nonce")
            # fail-closed: only an APPROVED action may execute (not pending/executed/…)
            if r["state"] != APPROVED:
                raise RefusedError(f"not approved for execution (state={r['state']})")
            # payload binding: capability/args AND authority fields must be intact
            args = json.loads(r["args_json"] or "{}")
            if _digest(r["capability"], args, r["leash_id"], r["job_id"], r["risk"],
                       json.loads(r["snapshot_json"] or "{}")) != r["digest"]:
                self._refuse(nonce, "payload digest mismatch (tampered)", now)
                raise RefusedError("payload digest mismatch (tampered)")
            # single-use latch + durable attempt marker in ONE txn: atomically
            # claim APPROVED -> EXECUTING and stamp attempted_at. A second
            # concurrent/duplicate execute sees a non-APPROVED row and is refused,
            # so the side effect can never fire twice (no double-send); attempted_at
            # makes a crash-after-fire distinguishable from crash-before-fire.
            claimed = self.db.execute(
                "UPDATE pending_actions SET state=?,attempted_at=? WHERE nonce=? AND state=?",
                (EXECUTING, now, nonce, APPROVED))
            self.db.commit()
            if claimed.rowcount != 1:
                raise RefusedError("already claimed (single-use)")
            record = self.get(nonce)

        # TOCTOU: outside the lock (may do I/O). If the world diverged from what
        # the human approved, refuse WITHOUT firing and roll the latch back to
        # approved so a later re-check can proceed.
        if unchanged_fn is not None:
            try:
                still = bool(unchanged_fn(record))
            except Exception:
                still = False
            if not still:
                with self._lock:
                    self.db.execute("UPDATE pending_actions SET state=? WHERE nonce=?",
                                    (APPROVED, nonce))
                    self.db.commit()
                self._write_receipt(record, approved=True, fired=False,
                                    verdict=Verdict(INCONCLUSIVE,
                                                    "world diverged from approved snapshot"),
                                    redact_fn=redact_fn)
                raise RefusedError("world diverged from approved snapshot (TOCTOU)")

        # fire the real side effect exactly once
        result = side_effect_fn(record)

        verdict = donecheck_fn(record, result) if donecheck_fn else \
            Verdict(INCONCLUSIVE, "no done-check declared")

        # finalize: terminal state AND the evidenced receipt land in ONE commit,
        # so a crash cannot leave a fired action EXECUTED without its receipt.
        rc, params = self._mk_receipt(record, approved=True, fired=True,
                                      verdict=verdict, redact_fn=redact_fn)
        with self._lock:
            self.db.execute("UPDATE pending_actions SET state=?,executed_at=? WHERE nonce=?",
                            (EXECUTED, int(time.time()), nonce))
            self.db.execute(
                """INSERT INTO receipts(nonce,job_id,capability,args_redacted,leash_id,
                     approved,fired,verdict,verdict_reason,evidence,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""", params)
            self.db.commit()
        return rc

    # ── receipts ──
    def _refuse(self, nonce, reason, now):
        self.db.execute("UPDATE pending_actions SET state=?,refuse_reason=? WHERE nonce=?",
                        (REFUSED, reason, nonce))
        self.db.commit()

    def _mk_receipt(self, record: ActionRecord, approved: bool, fired: bool,
                    verdict: Verdict, redact_fn=None):
        """Build the Receipt + its INSERT params. Redaction is on by DEFAULT: with
        no redact_fn, args are scrubbed of pattern-matched secrets (tokens/keys)
        via redact.py before they land in the receipts DB. This is defense-in-depth
        — it does not catch non-pattern PII (e.g. a raw card number), so callers
        handling such data should pass a stricter redact_fn."""
        ev = "; ".join(getattr(o, "detail", str(o)) for o in (verdict.evidence or ()))
        raw = json.dumps(record.args, ensure_ascii=False)
        args_redacted = redact_fn(raw) if redact_fn else _redact.redact(raw, {})
        rc = Receipt(nonce=record.nonce, capability=record.capability, approved=approved,
                     verdict=verdict.status, verdict_reason=verdict.reason, evidence=ev,
                     args_redacted=args_redacted, job_id=record.job_id,
                     leash_id=record.leash_id, fired=fired, created_at=int(time.time()))
        params = (rc.nonce, rc.job_id, rc.capability, rc.args_redacted, rc.leash_id,
                  int(approved), int(fired), rc.verdict, rc.verdict_reason, rc.evidence,
                  rc.created_at)
        return rc, params

    def _write_receipt(self, record: ActionRecord, approved: bool, fired: bool,
                       verdict: Verdict, redact_fn=None) -> Receipt:
        rc, params = self._mk_receipt(record, approved, fired, verdict, redact_fn)
        with self._lock:
            self.db.execute(
                """INSERT INTO receipts(nonce,job_id,capability,args_redacted,leash_id,
                     approved,fired,verdict,verdict_reason,evidence,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""", params)
            self.db.commit()
        return rc

    def receipts(self, nonce=None):
        q = "SELECT * FROM receipts"
        args = ()
        if nonce:
            q += " WHERE nonce=?"
            args = (nonce,)
        return [dict(r) for r in self.db.execute(q + " ORDER BY receipt_id", args)]

    def pending(self):
        """Actions materialized but not yet decided — the confirm inbox."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM pending_actions WHERE state=? ORDER BY created_at", (PENDING,))]

    def list(self, state=None):
        q, a = "SELECT * FROM pending_actions", ()
        if state:
            q, a = q + " WHERE state=?", (state,)
        return [dict(r) for r in self.db.execute(q + " ORDER BY created_at", a)]

    def close(self):
        self.db.close()
