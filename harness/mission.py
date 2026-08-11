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
import queue
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit
from dataclasses import dataclass, field

from . import leash as _leash
from .actions import ActionStore, RefusedError
from .jobs import (CANCELLED, DONE_ACCEPTED, DONE_VERIFIED, FAILED_S, NEEDS_YOU,
                   PAUSED, PAUSING, QUEUED, RECONCILING, RECOVERY_REQUIRED,
                   RUNNING, WAITING,
                   all_capabilities, get_capability)
from .verifier import FAILED, INCONCLUSIVE, VERIFIED, Verdict

_TERMINAL = {DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED}
# control moves the decider can return instead of a primitive name
WAIT, DONE, NEEDS_HUMAN = "wait", "done", "needs_human"
_AWAITING = "awaiting-confirm"


class ResourceBusy(RefusedError):
    pass


class StepTimedOut(RuntimeError):
    """One bounded model/tool step exceeded its wall-clock authority."""


@dataclass
class _CallOutcome:
    value: object = None
    elapsed_ms: int = 0
    timed_out: bool = False
    cancelled: bool = False
    error: Exception = None


def _bounded_json(value, limit=12000):
    """JSON-safe, bounded checkpoint material.

    Checkpoints are recovery hints, not a second unbounded transcript.  Keep the
    newest prefix intact and make truncation explicit so a resumed driver never
    mistakes missing bytes for complete state.
    """
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = json.dumps(str(value), ensure_ascii=False)
    if len(raw) <= int(limit):
        return value
    return {"summary": raw[:max(0, int(limit) - 64)], "truncated": True}


def _bounded_sequence_tail(value, limit=1800):
    """Bound a timeline while retaining its newest items, in chronological order."""
    if not isinstance(value, list):
        return _bounded_json(value, limit)
    kept = []
    remaining = int(limit)
    for newest_index, item in enumerate(reversed(value)):
        # The latest operator note is the recovery contract. Giving every item
        # one third of the budget truncated an ordinary instruction and silently
        # dropped its URL/final clause.
        item_limit = min(700, max(160, remaining - 8)) if newest_index == 0 else \
            min(500, max(160, remaining - 8))
        bounded = _bounded_json(item, item_limit)
        if (isinstance(item, dict) and isinstance(bounded, dict) and
                bounded.get("truncated")):
            # A giant payload value must not erase the item's small identity
            # fields (marker/capability/at) just because it sorts before them.
            bounded = _bounded_mapping_values(item, item_limit)
        candidate = [bounded] + kept
        encoded_len = len(json.dumps(candidate, ensure_ascii=False, default=str))
        if encoded_len > int(limit):
            break
        kept = candidate
        remaining = max(0, int(limit) - encoded_len)
        if remaining < 160 or len(kept) >= 3:
            break
    if not kept and value:
        kept = [_bounded_json(value[-1], max(160, int(limit) - 32))]
    return kept


def _bounded_mapping_values(value, limit=3600, max_items=12):
    """Keep several named facts instead of truncating the first mapping value."""
    if not isinstance(value, dict):
        return _bounded_json(value, limit)
    items = list(value.items())[-max(1, int(max_items)):]
    per_item = max(240, min(900, (int(limit) - 128) // max(1, len(items))))
    return {str(key)[:253]: _bounded_json(item, per_item) for key, item in items}


def _compact_case_storage(case, max_chars=64000):
    """Deterministically compact old case material while preserving newest facts.

    Full action evidence remains in receipts/events/checkpoints.  The case is the
    model's working set, so it favors human updates, recent results and outcome
    flags instead of retaining the oldest giant research blob forever.
    """
    case = dict(case or {})
    recent = list(case.get("_recent_results") or [])[-12:]
    if recent:
        case["_recent_results"] = recent
    updates = list(case.get("human_updates") or [])[-20:]
    if updates:
        case["human_updates"] = updates
    raw = _js(case)
    if len(raw) <= int(max_chars):
        return case

    priority_names = {
        "_mission_summary", "_recent_results", "human_updates", "browse_sites", "signal",
        "observe_count", "submitted", "published", "sent", "url", "draft",
        "code_verified", "coded", "last_sent_to", "_isolated_workspace",
        "_workspace", "_run_id", "_specialist_run_id", "_parent_mission_id",
    }
    out = {k: case[k] for k in case if k in priority_names}
    old_summary = str(case.get("_mission_summary") or "")
    dropped = []
    for key, value in case.items():
        if key in priority_names or str(key).startswith("_"):
            continue
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
        dropped.append("%s=%s" % (key, " ".join(text.split())[:600]))
    summary = (old_summary + ("\n" if old_summary and dropped else "") +
               "\n".join(dropped))[-max(1000, int(max_chars) // 2):]
    if summary:
        out["_mission_summary"] = summary
    # A single recent result can itself be enormous. Bound the complete working
    # set once more, keeping the summary marker explicit.
    if len(_js(out)) > int(max_chars):
        out["_recent_results"] = [_bounded_json(x, 1200) for x in recent[-6:]]
        out["_mission_summary"] = str(out.get("_mission_summary") or "")[-4000:]
    return out


def _model_case_json(case, limit=12000):
    """Serialize the working set with newest/recovery-critical facts first.

    A plain ``json.dumps(case)[:N]`` silently discarded late human updates and
    recent events whenever an old research result was large.  Priority ordering
    makes truncation deterministic and retains the information needed to resume.
    """
    case = dict(case or {})
    priority = ("_authority", "_mission_summary", "human_updates", "browse_sites",
                "_recent_results", "_recent_events", "_checkpoint")
    ordered = {key: case[key] for key in priority if key in case}
    ordered.update({key: value for key, value in case.items() if key not in ordered})
    raw = json.dumps(ordered, ensure_ascii=False, default=str)
    limit = max(1000, int(limit))
    if len(raw) <= limit:
        return raw
    # Preserve every priority field in bounded form, then spend the remainder on
    # older context.  The explicit marker prevents the model treating it as full.
    budgets = {"_authority": 1200, "_mission_summary": 1000, "human_updates": 900,
               "browse_sites": 3500, "_recent_results": 2200,
               "_recent_events": 1500, "_checkpoint": 400}
    head = {}
    for key in priority:
        if key not in ordered:
            continue
        if key == "browse_sites":
            head[key] = _bounded_mapping_values(ordered[key], budgets[key])
        elif key in ("human_updates", "_recent_results", "_recent_events"):
            head[key] = _bounded_sequence_tail(ordered[key], budgets[key])
        else:
            head[key] = _bounded_json(ordered[key], budgets[key])
    head["_context_truncated"] = True
    for key, value in ordered.items():
        if key in head:
            continue
        candidate = dict(head)
        candidate[key] = value
        encoded = json.dumps(candidate, ensure_ascii=False, default=str)
        if len(encoded) > limit:
            break
        head[key] = value
    return json.dumps(head, ensure_ascii=False, default=str)[:limit]


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
        self.path = path
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # A Mission tick may drive several independent campaigns concurrently.
        # RLock serializes one sqlite connection across those worker threads and
        # still permits a guarded helper to call another guarded helper.
        self._lock = threading.RLock()
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
        # Durable execution metadata is deliberately separate from ``missions``.
        # Old databases therefore migrate without rewriting their load-bearing
        # lifecycle rows, and operators can inspect progress/budget state directly.
        self.db.execute("""CREATE TABLE IF NOT EXISTS mission_runtime(
            mission_id TEXT PRIMARY KEY,
            progress_seq INTEGER NOT NULL DEFAULT 0,
            progress_at INTEGER NOT NULL DEFAULT 0,
            active_phase TEXT NOT NULL DEFAULT '',
            active_since INTEGER NOT NULL DEFAULT 0,
            run_started_at INTEGER NOT NULL DEFAULT 0,
            active_wall_ms INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_tokens INTEGER NOT NULL DEFAULT 0,
            model_cost_microusd INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            storage_bytes INTEGER NOT NULL DEFAULT 0,
            checkpoint_seq INTEGER NOT NULL DEFAULT 0,
            human_since INTEGER NOT NULL DEFAULT 0,
            human_escalate_at INTEGER NOT NULL DEFAULT 0,
            human_deadline_at INTEGER NOT NULL DEFAULT 0,
            escalation_level INTEGER NOT NULL DEFAULT 0,
            last_dispatch_at INTEGER NOT NULL DEFAULT 0,
            lane TEXT NOT NULL DEFAULT 'mission',
            external_run_id TEXT NOT NULL DEFAULT '')""")
        runtime_cols = {r[1] for r in self.db.execute("PRAGMA table_info(mission_runtime)")}
        for col, decl in (("lane", "TEXT NOT NULL DEFAULT 'mission'"),
                          ("external_run_id", "TEXT NOT NULL DEFAULT ''")):
            if col not in runtime_cols:
                self.db.execute("ALTER TABLE mission_runtime ADD COLUMN %s %s" % (col, decl))
        self.db.execute("""CREATE TABLE IF NOT EXISTS mission_checkpoints(
            checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT NOT NULL, seq INTEGER NOT NULL, phase TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}', case_json TEXT NOT NULL DEFAULT '{}',
            at INTEGER NOT NULL)""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS mission_checkpoints_recent "
            "ON mission_checkpoints(mission_id,checkpoint_id)")
        self.db.execute(
            "INSERT OR IGNORE INTO mission_runtime(mission_id,progress_at) "
            "SELECT mission_id,updated_at FROM missions")
        self.db.commit()

    def create(self, mission_id, goal, leash=None, case=None, *, lane="mission",
               external_run_id="") -> Mission:
        now, case = int(time.time()), dict(case or {})
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                parent_mid = str(case.get("_parent_mission_id") or "") \
                    if str(lane or "mission") == "specialist" else ""
                if parent_mid:
                    parent = self.db.execute(
                        "SELECT state FROM missions WHERE mission_id=?", (parent_mid,)).fetchone()
                    if not parent or parent["state"] in (
                            DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
                        raise ValueError("specialist parent Mission is stopping or terminal")
                self.db.execute(
                    "INSERT INTO missions(mission_id,goal,leash_json,case_json,state,"
                    "result,created_at,updated_at,paused_from,run_token,lease_until) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (mission_id, goal, _js(leash or {}), _js(case), QUEUED, "",
                     now, now, "", "", 0))
                self.db.execute(
                    "INSERT INTO mission_runtime(mission_id,progress_at,active_phase,storage_bytes,"
                    "lane,external_run_id) VALUES(?,?,?,?,?,?)",
                    (mission_id, now, "created", len(_js(case).encode("utf-8")),
                     str(lane or "mission")[:40], str(external_run_id or "")[:100]))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return Mission(mission_id, goal, leash or {}, case, QUEUED, "", now, now)

    def get(self, mission_id) -> Mission:
        with self._lock:
            r = self.db.execute("SELECT * FROM missions WHERE mission_id=?",
                                (mission_id,)).fetchone()
        if not r:
            return None
        return Mission(r["mission_id"], r["goal"], _jl(r["leash_json"]),
                       _jl(r["case_json"]), r["state"], r["result"],
                       r["created_at"], r["updated_at"], r["paused_from"],
                       r["run_token"], r["lease_until"])

    # -- durable progress / budget ledger ---------------------------------
    def runtime(self, mission_id):
        with self._lock:
            r = self.db.execute(
                "SELECT * FROM mission_runtime WHERE mission_id=?", (mission_id,)).fetchone()
        if not r:
            return {}
        out = dict(r)
        out["model_cost_usd"] = out.get("model_cost_microusd", 0) / 1_000_000.0
        return out

    def _storage_bytes_locked(self, mission_id):
        mission = self.db.execute(
            "SELECT LENGTH(CAST(COALESCE(case_json,'') AS BLOB))+"
            "LENGTH(CAST(COALESCE(leash_json,'') AS BLOB)) n "
            "FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
        events = self.db.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))),0) n "
            "FROM mission_events WHERE mission_id=?",
            (mission_id,)).fetchone()
        checkpoints = self.db.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(payload_json AS BLOB))+"
            "LENGTH(CAST(case_json AS BLOB))),0) n "
            "FROM mission_checkpoints WHERE mission_id=?", (mission_id,)).fetchone()
        return int((mission or {"n": 0})["n"] or 0) + int(events["n"] or 0) + \
            int(checkpoints["n"] or 0)

    def refresh_storage(self, mission_id):
        with self._lock:
            n = self._storage_bytes_locked(mission_id)
            self.db.execute(
                "UPDATE mission_runtime SET storage_bytes=? WHERE mission_id=?", (n, mission_id))
            self.db.commit()
        return n

    def budget_reason(self, mission_id, now=None):
        """Return the first cumulative Mission budget that is exhausted."""
        now = int(now if now is not None else time.time())
        m, rt = self.get(mission_id), self.runtime(mission_id)
        if not m:
            return "mission no longer exists"
        leash = m.leash or {}
        total_tokens = (int(rt.get("input_tokens", 0)) + int(rt.get("output_tokens", 0)) +
                        int(rt.get("cache_tokens", 0)))
        checks = (
            (int(leash.get("max_model_tokens", 2_000_000)) > 0 and
             total_tokens >= int(leash.get("max_model_tokens", 2_000_000)),
             "mission model-token budget exhausted"),
            (float(leash.get("max_model_cost_usd", 25.0)) > 0 and
             float(rt.get("model_cost_usd", 0.0)) >=
             float(leash.get("max_model_cost_usd", 25.0)),
             "mission model-cost budget exhausted"),
            (int(leash.get("max_active_wall_seconds", 21600)) > 0 and
             int(rt.get("active_wall_ms", 0)) >=
             int(leash.get("max_active_wall_seconds", 21600)) * 1000,
             "mission active wall-time budget exhausted"),
            (int(leash.get("max_elapsed_seconds", 2592000)) > 0 and
             now - int(m.created_at or now) >= int(leash.get("max_elapsed_seconds", 2592000)),
             "mission elapsed-time budget exhausted"),
            (int(leash.get("max_retries", 32)) > 0 and
             int(rt.get("retry_count", 0)) >= int(leash.get("max_retries", 32)),
             "mission retry budget exhausted"),
            (int(leash.get("max_storage_bytes", 5_000_000)) > 0 and
             int(rt.get("storage_bytes", 0)) >= int(leash.get("max_storage_bytes", 5_000_000)),
             "mission durable-storage budget exhausted"),
        )
        return next((reason for hit, reason in checks if hit), "")

    def account_runtime(self, mission_id, token="", *, input_tokens=0, output_tokens=0,
                        cache_tokens=0, cost_usd=0.0, wall_ms=0, retries=0):
        """Atomically charge one completed/abandoned step to the campaign.

        A token is optional for recovery bookkeeping.  When supplied, a stale
        worker is fenced and cannot charge a fresh run's budget.
        """
        vals = (max(0, int(wall_ms or 0)), max(0, int(input_tokens or 0)),
                max(0, int(output_tokens or 0)), max(0, int(cache_tokens or 0)),
                max(0, int(round(float(cost_usd or 0.0) * 1_000_000))),
                max(0, int(retries or 0)), mission_id)
        owner = ""
        args = list(vals)
        if token:
            owner = (" AND EXISTS (SELECT 1 FROM missions m WHERE m.mission_id=mission_runtime.mission_id "
                     "AND m.run_token=? AND m.state IN (?,?))")
            args.extend([token, RUNNING, PAUSING])
        with self._lock:
            cur = self.db.execute(
                "UPDATE mission_runtime SET active_wall_ms=active_wall_ms+?,"
                "input_tokens=input_tokens+?,output_tokens=output_tokens+?,"
                "cache_tokens=cache_tokens+?,model_cost_microusd=model_cost_microusd+?,"
                "retry_count=retry_count+? WHERE mission_id=?" + owner, args)
            self.db.commit()
        return cur.rowcount == 1

    def record_checkpoint(self, mission_id, token, phase, payload=None, case=None,
                          allow_unowned=False):
        """Persist a replay/audit boundary and advance the independent progress clock."""
        now = int(time.time())
        m = self.get(mission_id)
        if not m:
            return False
        keep = max(4, min(256, int((m.leash or {}).get("checkpoint_keep", 64))))
        payload_json = _js(_bounded_json(payload or {}, 12000))
        case_json = _js(_bounded_json(case if case is not None else m.case, 16000))
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            if not allow_unowned:
                owner = self.db.execute(
                    "SELECT 1 FROM missions WHERE mission_id=? AND run_token=? "
                    "AND state IN (?,?)", (mission_id, token, RUNNING, PAUSING)).fetchone()
                if not owner:
                    self.db.rollback()
                    return False
            self.db.execute(
                "UPDATE mission_runtime SET progress_seq=progress_seq+1,checkpoint_seq=checkpoint_seq+1,"
                "progress_at=?,active_phase=?,active_since=?,last_dispatch_at=CASE "
                "WHEN ?='claimed' THEN ? ELSE last_dispatch_at END WHERE mission_id=?",
                (now, str(phase)[:80], now, phase, now, mission_id))
            row = self.db.execute(
                "SELECT checkpoint_seq FROM mission_runtime WHERE mission_id=?", (mission_id,)).fetchone()
            seq = int(row["checkpoint_seq"] if row else 0)
            self.db.execute(
                "INSERT INTO mission_checkpoints(mission_id,seq,phase,payload_json,case_json,at) "
                "VALUES(?,?,?,?,?,?)",
                (mission_id, seq, str(phase)[:80], payload_json, case_json, now))
            self.db.execute(
                "DELETE FROM mission_checkpoints WHERE mission_id=? AND checkpoint_id NOT IN "
                "(SELECT checkpoint_id FROM mission_checkpoints WHERE mission_id=? "
                "ORDER BY checkpoint_id DESC LIMIT ?)", (mission_id, mission_id, keep))
            n = self._storage_bytes_locked(mission_id)
            self.db.execute(
                "UPDATE mission_runtime SET storage_bytes=? WHERE mission_id=?", (n, mission_id))
            self.db.commit()
        return True

    def latest_checkpoint(self, mission_id):
        with self._lock:
            r = self.db.execute(
                "SELECT seq,phase,payload_json,case_json,at FROM mission_checkpoints "
                "WHERE mission_id=? ORDER BY checkpoint_id DESC LIMIT 1", (mission_id,)).fetchone()
        if not r:
            return None
        return {"seq": r["seq"], "phase": r["phase"], "payload": _jl(r["payload_json"]),
                "case": _jl(r["case_json"]), "at": r["at"]}

    def _mark_human_locked(self, mission_id, leash, now):
        escalate_s = max(1, int((leash or {}).get("human_escalate_seconds", 3600)))
        timeout_s = max(escalate_s, int((leash or {}).get("human_timeout_seconds", 86400)))
        self.db.execute(
            "UPDATE mission_runtime SET human_since=?,human_escalate_at=?,human_deadline_at=?,"
            "escalation_level=0,active_phase='needs_you',progress_at=? WHERE mission_id=?",
            (now, now + escalate_s, now + timeout_s, now, mission_id))

    def clear_human_wait(self, mission_id):
        with self._lock:
            self.db.execute(
                "UPDATE mission_runtime SET human_since=0,human_escalate_at=0,"
                "human_deadline_at=0,escalation_level=0 WHERE mission_id=?", (mission_id,))
            self.db.commit()

    def escalate_human_waits(self, now=None):
        """Advance durable human-wait escalation clocks.

        Level 1 is a notification/escalation hook.  At the hard deadline the
        Mission fail-closes into PAUSED while preserving its exact approval row;
        Resume returns it to NEEDS_YOU rather than silently denying or executing.
        The returned records are a durable-outbox seam for Web/CLI/phone wiring.
        """
        now = int(now if now is not None else time.time())
        out = []
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT m.mission_id,m.state,r.human_escalate_at,r.human_deadline_at,"
                "r.escalation_level FROM missions m JOIN mission_runtime r "
                "ON r.mission_id=m.mission_id WHERE m.state=? AND r.human_since>0",
                (NEEDS_YOU,)).fetchall()
            for row in rows:
                mid = row["mission_id"]
                if row["human_deadline_at"] and now >= row["human_deadline_at"]:
                    cur = self.db.execute(
                        "UPDATE missions SET state=?,paused_from=?,result=?,updated_at=? "
                        "WHERE mission_id=? AND state=?",
                        (PAUSED, NEEDS_YOU, "paused: human response deadline elapsed",
                         now, mid, NEEDS_YOU))
                    if cur.rowcount:
                        self.db.execute(
                            "UPDATE mission_runtime SET escalation_level=2,active_phase='human_timeout',"
                            "progress_at=? WHERE mission_id=?", (now, mid))
                        out.append({"mission_id": mid, "level": 2, "state": PAUSED,
                                    "reason": "human response deadline elapsed"})
                elif (row["human_escalate_at"] and now >= row["human_escalate_at"] and
                      int(row["escalation_level"] or 0) < 1):
                    self.db.execute(
                        "UPDATE mission_runtime SET escalation_level=1 WHERE mission_id=?", (mid,))
                    out.append({"mission_id": mid, "level": 1, "state": NEEDS_YOU,
                                "reason": "human response overdue"})
            self.db.commit()
        return out

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
        case = _compact_case_storage(case)
        self._set(mission_id, case_json=_js(case))
        self.refresh_storage(mission_id)
        return True

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
            if cur.rowcount == 1:
                self.db.execute(
                    "UPDATE mission_runtime SET progress_seq=progress_seq+1,progress_at=?,"
                    "active_phase='claimed',active_since=?,run_started_at=?,last_dispatch_at=? "
                    "WHERE mission_id=?", (now, now, now, now, mission_id))
            self.db.commit()
        if cur.rowcount == 1:
            self.record_checkpoint(mission_id, token, "claimed", {"from": list(states)})
            return token
        return None

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
        """Renew ownership only while the independent progress clock is healthy.

        The heartbeat is intentionally unable to update ``progress_at``.  A live
        heartbeat around a wedged provider/tool therefore expires instead of
        laundering "thread exists" into "Mission is making progress".
        """
        now = int(time.time())
        with self._lock:
            row = self.db.execute(
                "SELECT m.leash_json,r.progress_at,r.active_wall_ms FROM missions m "
                "JOIN mission_runtime r ON r.mission_id=m.mission_id "
                "WHERE m.mission_id=? AND m.state IN (?,?) AND m.run_token=?",
                (mission_id, RUNNING, PAUSING, token)).fetchone()
            if not row:
                return False
            leash = _jl(row["leash_json"])
            max_idle = max(0.05, float(leash.get("max_step_seconds", 600))) + 5
            if int(row["progress_at"] or 0) and now - int(row["progress_at"]) > max_idle:
                return False
            max_active = int(leash.get("max_active_wall_seconds", 21600))
            if max_active > 0 and int(row["active_wall_ms"] or 0) >= max_active * 1000:
                return False
            cur = self.db.execute(
                "UPDATE missions SET lease_until=?,updated_at=? "
                "WHERE mission_id=? AND state IN (?,?) AND run_token=?",
                (now + int(renew_s), now, mission_id, RUNNING, PAUSING, token))
            self.db.commit()
        return cur.rowcount == 1

    def fence_timed_out(self, mission_id, token, phase, reason):
        """Fence an action whose worker crossed its deadline.

        The worker may still finish in its daemon thread.  Clearing the run token
        prevents it from folding stale state, while its ActionStore receipt remains
        available for explicit reconciliation.
        """
        now = int(time.time())
        with self._lock:
            cur = self.db.execute(
                "UPDATE missions SET state=?,run_token='',lease_until=0,result=?,updated_at=? "
                "WHERE mission_id=? AND state IN (?,?) AND run_token=?",
                (RECOVERY_REQUIRED, str(reason)[:200], now, mission_id, RUNNING, PAUSING, token))
            if cur.rowcount:
                self.db.execute(
                    "UPDATE mission_runtime SET progress_seq=progress_seq+1,progress_at=?,"
                    "active_phase=?,active_since=? WHERE mission_id=?",
                    (now, ("timed_out:" + str(phase))[:80], now, mission_id))
            self.db.commit()
        if cur.rowcount:
            self.record_checkpoint(mission_id, "", "timed_out:" + str(phase),
                                   {"reason": str(reason)[:500]}, allow_unowned=True)
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
        safe_phases = {"deciding", "decision_ready"}
        recovered = 0
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT m.mission_id,COALESCE(r.active_phase,'') phase FROM missions m "
                "LEFT JOIN mission_runtime r ON r.mission_id=m.mission_id "
                "WHERE m.state IN (?,?) AND COALESCE(m.run_token,'')<>'' "
                "AND m.lease_until>0 AND m.lease_until<=?",
                (RUNNING, PAUSING, now)).fetchall()
            for row in rows:
                safe = row["phase"] in safe_phases
                state = QUEUED if safe else RECOVERY_REQUIRED
                result = ("safe model-only boundary recovered; queued to continue" if safe else
                          "runner heartbeat expired; inspect the external system and receipts, "
                          "then explicitly reconcile or cancel")
                cur = self.db.execute(
                    "UPDATE missions SET state=?,run_token='',lease_until=0,result=?,updated_at=? "
                    "WHERE mission_id=? AND state IN (?,?) AND lease_until<=?",
                    (state, result, now, row["mission_id"], RUNNING, PAUSING, now))
                if cur.rowcount:
                    recovered += 1
                    self.db.execute(
                        "UPDATE mission_runtime SET progress_seq=progress_seq+1,progress_at=?,"
                        "active_phase=?,active_since=? WHERE mission_id=?",
                        (now, "recovered_safe" if safe else "recovery_required", now,
                         row["mission_id"]))
            self.db.commit()
        return recovered

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
        with self._lock:
            rows = self.db.execute(
                "SELECT resource,token,lease_until FROM mission_resource_leases "
                "WHERE mission_id=? AND lease_until>? ORDER BY resource",
                (mission_id, now)).fetchall()
        return [dict(r) for r in rows]

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
            if cur.rowcount:
                leash_row = self.db.execute(
                    "SELECT leash_json FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
                if state == NEEDS_YOU:
                    self._mark_human_locked(mission_id, _jl(leash_row["leash_json"]), now)
                else:
                    self.db.execute(
                        "UPDATE mission_runtime SET active_phase=?,progress_at=?,active_since=?,"
                        "human_since=0,human_escalate_at=0,human_deadline_at=0,escalation_level=0 "
                        "WHERE mission_id=?", (state, now, now, mission_id))
            self.db.commit()
        if cur.rowcount:
            self.record_checkpoint(mission_id, "", state,
                                   {"result": str(result or "")[:500]}, allow_unowned=True)
        return cur.rowcount == 1

    def set_case_owned(self, mission_id, token, case):
        now = int(time.time())
        case = _compact_case_storage(case)
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
                leash_row = self.db.execute(
                    "SELECT leash_json FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
                self._mark_human_locked(mission_id, _jl(leash_row["leash_json"]), now)
            if cur.rowcount:
                n = self._storage_bytes_locked(mission_id)
                self.db.execute(
                    "UPDATE mission_runtime SET storage_bytes=? WHERE mission_id=?",
                    (n, mission_id))
            self.db.commit()
        if cur.rowcount:
            self.record_checkpoint(
                mission_id, "", "needs_you", {"action": name, "nonce": nonce,
                                                "reason": (result or "")[:500]},
                allow_unowned=True)
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
            if cur.rowcount:
                self.db.execute(
                    "UPDATE mission_runtime SET active_phase='paused',progress_at=?,active_since=? "
                    "WHERE mission_id=?", (now, now, mission_id))
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
            if cur.rowcount:
                self.db.execute(
                    "UPDATE mission_runtime SET active_phase='paused',progress_at=?,active_since=?,"
                    "human_since=0,human_escalate_at=0,human_deadline_at=0,escalation_level=0 "
                    "WHERE mission_id=?", (now, now, mission_id))
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
            if cur.rowcount:
                if target == NEEDS_YOU:
                    leash_row = self.db.execute(
                        "SELECT leash_json FROM missions WHERE mission_id=?", (mission_id,)).fetchone()
                    self._mark_human_locked(mission_id, _jl(leash_row["leash_json"]), now)
                else:
                    self.db.execute(
                        "UPDATE mission_runtime SET active_phase=?,progress_at=? WHERE mission_id=?",
                        (target, now, mission_id))
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
                self.db.execute(
                    "UPDATE mission_runtime SET active_phase=?,progress_at=?,active_since=?,"
                    "human_since=0,human_escalate_at=0,human_deadline_at=0,escalation_level=0 "
                    "WHERE mission_id=?", (CANCELLED, now, now, mission_id))
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
            if cur.rowcount:
                self.db.execute(
                    "UPDATE mission_runtime SET active_phase=?,progress_at=?,human_since=0,"
                    "human_escalate_at=0,human_deadline_at=0,escalation_level=0 WHERE mission_id=?",
                    (DONE_ACCEPTED, now, mission_id))
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
            if cur.rowcount:
                self.db.execute(
                    "UPDATE mission_runtime SET active_phase=?,progress_at=?,human_since=0,"
                    "human_escalate_at=0,human_deadline_at=0,escalation_level=0 WHERE mission_id=?",
                    (QUEUED, now, mission_id))
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
        with self._lock:
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
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM mission_steps WHERE mission_id=? ORDER BY step_id",
                (mission_id,)).fetchall()
        return [dict(r) for r in rows]

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
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM mission_waits WHERE state='pending' AND fire_at<=? "
                "ORDER BY fire_at", (int(now),)).fetchall()
        return [dict(r) for r in rows]

    def claim_wait(self, wait_id):
        with self._lock:
            cur = self.db.execute(
                "UPDATE mission_waits SET state='fired' WHERE wait_id=? AND state='pending'",
                (wait_id,))
            self.db.commit()
        return cur.rowcount == 1

    def next_wait(self, mission_id):
        with self._lock:
            r = self.db.execute(
                "SELECT * FROM mission_waits WHERE mission_id=? AND state='pending' "
                "ORDER BY fire_at LIMIT 1", (mission_id,)).fetchone()
        return dict(r) if r else None

    def claim_due_wait(self, now, mission_id=None, force=False, lease_s=300, lane=None):
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
        if lane:
            where.append("EXISTS (SELECT 1 FROM mission_runtime r WHERE "
                         "r.mission_id=m.mission_id AND r.lane=?)")
            args.append(str(lane))
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
            self.db.execute(
                "UPDATE mission_runtime SET progress_seq=progress_seq+1,progress_at=?,"
                "active_phase='claimed',active_since=?,run_started_at=?,last_dispatch_at=? "
                "WHERE mission_id=?", (now, now, now, now, r["mission_id"]))
            self.db.commit()
        self.record_checkpoint(r["mission_id"], token, "claimed",
                               {"wake_wait_id": r["wait_id"]})
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
        with self._lock:
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
        with self._lock:
            rows = self.db.execute(q + " ORDER BY created_at", a).fetchall()
        return [self.get(r["mission_id"]) for r in rows]

    def queued_fair(self, limit=32, lane="mission"):
        """Oldest least-recently-dispatched work first (durable round-robin)."""
        with self._lock:
            rows = self.db.execute(
                "SELECT m.mission_id FROM missions m JOIN mission_runtime r "
                "ON r.mission_id=m.mission_id WHERE m.state=? AND r.lane=? "
                "ORDER BY r.last_dispatch_at ASC,m.updated_at ASC,m.created_at ASC LIMIT ?",
                (QUEUED, str(lane), max(1, int(limit)))).fetchall()
        return [self.get(r["mission_id"]) for r in rows]

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
    # anti-poll-spin: after this many CONSECUTIVE reversible reads of the SAME
    # target (e.g. observe one inbox again and again), force a durable wait instead
    # of reading in a tight loop. First reads of different sites are discovery, not
    # polling, and must not make a multi-channel mission sleep for an hour.
    # In the world each read is a real, slow browser fetch — polling 40x is wrong;
    # a monitor should read, then WAIT. Resets when an irreversible action fires.
    read_streak_cap = 3
    read_wait_s = 3600

    @staticmethod
    def _observe_target(args):
        """Return a privacy-safe identity for the resource being polled.

        Expectations deliberately do not participate: changing the search phrase
        while refreshing the same page is still polling. Query strings/fragments
        are omitted because they can contain credentials or user identifiers.
        """
        a = args or {}
        raw_url = str(a.get("url") or a.get("target") or "").strip()
        if raw_url:
            parsed = urlsplit(raw_url)
            target = "%s://%s%s" % (
                (parsed.scheme or "https").lower(),
                (parsed.hostname or "").lower(),
                parsed.path or "/")
        else:
            target = str(a.get("inbox") or a.get("channel") or "default").strip().lower()
        return (bool(a.get("authed") or a.get("inbox")), target)

    @staticmethod
    def _browse_submit_ready(events):
        """A final browser write may follow only the newest independently verified preparation.

        This is a deterministic sequencing invariant, not planner advice: a model may optimistically
        choose Submit after a failed fill, but the container must never materialize that click.
        """
        for event in reversed(list(events or [])):
            if event.get("kind") == "result" and event.get("name") == "browse":
                payload = event.get("payload") or {}
                verdict = str(payload.get("verdict") or "")
                if verdict == VERIFIED:
                    return True, "latest browser preparation independently verified"
                return False, ("latest browser preparation was %s: %s" %
                               (verdict or "not verified",
                                str(payload.get("reason") or "no verification evidence")[:300]))
        return False, "no independently verified browser preparation exists"

    def __init__(self, store: MissionStore, actions: ActionStore, decider,
                 capabilities=None, goal_verifier=None, *, lane="mission",
                 control=None, hooks=None):
        self.store = store
        self.actions = actions
        self.decider = decider
        self.goal_verifier = goal_verifier
        self.lane = str(lane or "mission")
        self.control = control
        self.hooks = hooks
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
            if name == "browse":
                page = result.get("page") or {}
                host = str(page.get("host") or "").strip().lower()
                if re.fullmatch(r"[a-z0-9.-]{1,253}", host):
                    summary = result.get("result")
                    if not isinstance(summary, str):
                        summary = (result.get("case") or {}).get("browse_result") or summary
                    if not isinstance(summary, str):
                        summary = json.dumps(summary, ensure_ascii=False, default=str)
                    sites = dict(case.get("browse_sites") or {})
                    previous = sites.get(host) if isinstance(sites.get(host), dict) else {}
                    observations = list(previous.get("observations") or [])
                    observation = {"at": int(time.time()),
                                   "title": str(page.get("title") or "")[:160],
                                   "summary": summary[:1400]}
                    observations.append(observation)
                    sites[host] = {"latest": observation,
                                   "observations": observations[-2:]}
                    case["browse_sites"] = sites
        elif result is not None:
            case[name] = result
        recent = list(case.get("_recent_results") or [])
        recent.append({"at": int(time.time()), "capability": name,
                       "result": _compact_event(result, 2000)})
        case["_recent_results"] = recent[-12:]
        case = _compact_case_storage(case)
        return self.store.set_case_owned(m.mission_id, token, case) if token \
            else self.store.set_case(m.mission_id, case)

    @staticmethod
    def _cancel_call(owner):
        """Best-effort provider/tool cancellation hook; ownership fencing is primary."""
        for name in ("cancel_current", "cancel_pending", "abort_current"):
            fn = getattr(owner, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                return True
        return False

    def _bounded_call(self, fn, timeout_s, cancel_owner=None):
        """Run one potentially blocking boundary without wedging the dispatcher.

        Python cannot safely kill an arbitrary thread.  The worker is therefore a
        daemon, the durable ownership token is the hard mutation fence, and an
        optional transport cancellation hook is invoked on timeout.  This lets the
        scheduler continue other Missions while a misbehaving library unwinds.
        """
        out = queue.Queue(maxsize=1)
        started = time.monotonic()

        def run():
            try:
                item = _CallOutcome(value=fn())
            except Exception as exc:
                item = _CallOutcome(error=exc)
            item.elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
            try:
                out.put_nowait(item)
            except queue.Full:
                pass

        thread = threading.Thread(target=run, name="mission-bounded-call", daemon=True)
        thread.start()
        try:
            return out.get(timeout=max(0.01, float(timeout_s)))
        except queue.Empty:
            cancelled = self._cancel_call(cancel_owner) if cancel_owner is not None else False
            return _CallOutcome(
                elapsed_ms=max(1, int((time.monotonic() - started) * 1000)),
                timed_out=True, cancelled=cancelled,
                error=StepTimedOut("step exceeded %.2fs wall-clock limit" % float(timeout_s)))

    @staticmethod
    def _usage_from_decision(decision):
        usage = (decision or {}).get("_usage") or {}
        return {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "cache_tokens": int(usage.get("cache_tokens", 0) or 0),
            "cost_usd": float((decision or {}).get("_cost_usd", 0.0) or 0.0),
            "retries": int((decision or {}).get("_retry", 0) or 0),
        }

    @staticmethod
    def _usage_from_result(result):
        usage = result.get("_usage") if isinstance(result, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        return {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
            "cache_tokens": int(usage.get("cache_tokens", 0) or 0),
            "cost_usd": float(usage.get("cost_usd", 0.0) or 0.0),
        }

    def _goal_verdict(self, m):
        verifier = self.goal_verifier
        if verifier is None:
            return Verdict(INCONCLUSIVE,
                           "no independent mission-level goal verifier configured")
        fn = getattr(verifier, "verify", verifier)
        result = fn(m.goal, dict(m.case), self.store.events(m.mission_id, 50),
                    self.store.steps(m.mission_id))
        return result if isinstance(result, Verdict) else Verdict(
            INCONCLUSIVE, "goal verifier returned no typed evidence verdict")

    @staticmethod
    def _goal_evidence(verdict):
        """Return bounded, receipt-safe independent observations from a goal verdict."""
        evidence = []
        for item in tuple(getattr(verdict, "evidence", ()) or ())[:20]:
            if isinstance(item, dict):
                channel, at, ok = item.get("channel"), item.get("at"), item.get("ok")
                asserted, detail = item.get("asserted", False), item.get("detail", "")
            else:
                channel, at, ok = (getattr(item, "channel", ""),
                                   getattr(item, "at", None), getattr(item, "ok", None))
                asserted, detail = (getattr(item, "asserted", False),
                                    getattr(item, "detail", ""))
            channel = str(channel or "").strip()
            if (not channel or channel.lower() in ("model", "self-report", "model-self-report")
                    or not isinstance(at, (int, float)) or isinstance(at, bool)
                    or not isinstance(ok, bool)):
                continue
            evidence.append({"channel": channel[:120], "at": float(at), "ok": ok,
                             "asserted": bool(asserted), "detail": str(detail or "")[:1000]})
        return evidence

    def _dispatch_hook(self, event, payload, subject=""):
        if self.hooks is None:
            return None
        try:
            return self.hooks.dispatch(event, payload, subject=subject)
        except Exception as exc:
            self.store.record_event(
                payload.get("mission_id", ""), "hook", event,
                payload={"error": "%s: %s" % (type(exc).__name__, exc)})
            return None

    def _control_boundary(self, mission_id, token):
        """Consume durable steer/cancel input between model/action boundaries."""
        if self.control is None:
            return ""
        try:
            update = self.control(mission_id) or {}
        except Exception as exc:
            return self._finish(
                mission_id, token, NEEDS_YOU,
                "external control channel failed: %s: %s" % (type(exc).__name__, exc))
        if update.get("cancel"):
            self.store.cancel(mission_id, "cancel acknowledged at a safe action boundary")
            return self._state(mission_id, CANCELLED)
        steers = [str(text).strip() for text in (update.get("steers") or [])
                  if str(text).strip()]
        if steers:
            m = self.store.get(mission_id)
            case = dict(m.case)
            human = list(case.get("human_updates") or [])
            now = int(time.time())
            human.extend({"at": now, "note": text[:1000], "steer": True}
                         for text in steers)
            case["human_updates"] = human[-20:]
            if not self.store.set_case_owned(mission_id, token, case):
                return self._lost_state(mission_id, token)
            self.store.record_event(
                mission_id, "control", "steer", payload={"messages": steers[-10:]})
            self.store.record_checkpoint(
                mission_id, token, "steered", {"messages": steers[-10:]}, case=case)
            return "_steered"
        return ""

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
        if state in _TERMINAL:
            hook = self._dispatch_hook(
                "Stop", {"mission_id": mission_id, "state": state,
                         "result": str(result or "")[:2000]}, subject=state)
            if hook is not None and not getattr(hook, "allowed", True):
                state = NEEDS_YOU
                result = "Stop hook blocked completion: %s" % (
                    getattr(hook, "reason", "policy check did not pass") or
                    "policy check did not pass")
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
        reads = 0                                     # consecutive reads of one target
        read_target = None
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
                controlled = self._control_boundary(mission_id, token)
                if controlled:
                    if controlled == "_steered":
                        continue
                    return controlled
                m = self.store.get(mission_id)
                exhausted = self.store.budget_reason(mission_id)
                if exhausted:
                    return self._finish(mission_id, token, NEEDS_YOU, exhausted)
                if not self.store.reserve_decision(mission_id, m.leash):
                    return self._finish(mission_id, token, NEEDS_YOU,
                                        "mission model-turn budget exhausted")
                model_case = dict(m.case)
                model_case["_authority"] = m.leash
                model_case["_recent_events"] = self.store.events(mission_id, 20)
                checkpoint = self.store.latest_checkpoint(mission_id)
                if checkpoint:
                    model_case["_checkpoint"] = {
                        "seq": checkpoint["seq"], "phase": checkpoint["phase"],
                        "at": checkpoint["at"]}
                self.store.record_checkpoint(
                    mission_id, token, "deciding",
                    {"step": _, "recent_events": model_case["_recent_events"][-5:]},
                    case=m.case)
                step_timeout = max(0.05, float(m.leash.get("max_step_seconds", 600)))
                outcome = self._bounded_call(
                    lambda: self.decider(m.goal, model_case, self._primitives(m.leash)),
                    step_timeout, cancel_owner=getattr(self.decider, "provider", self.decider))
                if outcome.timed_out:
                    self.store.account_runtime(
                        mission_id, token, wall_ms=outcome.elapsed_ms, retries=1)
                    self.store.record_event(
                        mission_id, "watchdog", "decider_timeout",
                        payload={"timeout_seconds": step_timeout,
                                 "cancel_requested": outcome.cancelled})
                    if self.store.budget_reason(mission_id):
                        return self._finish(mission_id, token, NEEDS_YOU,
                                            self.store.budget_reason(mission_id))
                    self.store.schedule_wait(mission_id, int(time.time()) + 60)
                    return self._finish(
                        mission_id, token, WAITING,
                        "model step timed out; retry scheduled without replaying an action")
                if outcome.error is not None:
                    self.store.account_runtime(
                        mission_id, token, wall_ms=outcome.elapsed_ms, retries=1)
                    self.store.record_event(
                        mission_id, "watchdog", "decider_error",
                        payload={"error": "%s: %s" %
                                 (type(outcome.error).__name__, outcome.error)})
                    exhausted = self.store.budget_reason(mission_id)
                    if exhausted:
                        return self._finish(mission_id, token, NEEDS_YOU, exhausted)
                    self.store.schedule_wait(mission_id, int(time.time()) + 60)
                    return self._finish(mission_id, token, WAITING,
                                        "model step failed; retry scheduled")
                decision = outcome.value or {}
                usage = self._usage_from_decision(decision)
                usage["wall_ms"] = outcome.elapsed_ms
                self.store.account_runtime(mission_id, token, **usage)
                public_decision = {k: v for k, v in decision.items()
                                   if not str(k).startswith("_")}
                self.store.record_checkpoint(
                    mission_id, token, "decision_ready", public_decision, case=m.case)
                # A pause/cancel arriving during the model call wins before another
                # primitive is proposed or fired.
                if not self.store.owns_run(mission_id, token):
                    return self._lost_state(mission_id, token)
                controlled = self._control_boundary(mission_id, token)
                if controlled:
                    if controlled == "_steered":
                        continue
                    return controlled
                m = self.store.get(mission_id)
                exhausted = self.store.budget_reason(mission_id)
                if exhausted:
                    return self._finish(mission_id, token, NEEDS_YOU, exhausted)
                action = decision.get("action")
                args = decision.get("args") or {}
                reason = decision.get("reason") or ""

                if action is None:
                    self.store.record_event(mission_id, "control", "invalid", payload={"reason": reason})
                    return self._finish(mission_id, token, NEEDS_YOU,
                                        "driver returned no next action")
                if action == DONE:
                    self.store.record_step(mission_id, DONE, "", "reported")
                    self.store.record_event(mission_id, "control", DONE,
                                            payload={"reason": reason})
                    self.store.record_checkpoint(
                        mission_id, token, "goal_verifying", {"reason": reason}, case=m.case)
                    goal_outcome = self._bounded_call(
                        lambda: self._goal_verdict(m), step_timeout,
                        cancel_owner=self.goal_verifier)
                    self.store.account_runtime(
                        mission_id, token, wall_ms=goal_outcome.elapsed_ms,
                        retries=1 if goal_outcome.timed_out or goal_outcome.error else 0)
                    if goal_outcome.timed_out or goal_outcome.error:
                        verdict = Verdict(
                            INCONCLUSIVE, "mission goal verification timed out" if
                            goal_outcome.timed_out else
                            "mission goal verifier failed: %s" % goal_outcome.error)
                    else:
                        verdict = goal_outcome.value
                    if not isinstance(verdict, Verdict):
                        verdict = Verdict(INCONCLUSIVE,
                                          "mission goal verifier returned no typed verdict")
                    evidence = self._goal_evidence(verdict)
                    if verdict.status == VERIFIED and (
                            not str(verdict.reason or "").strip() or
                            not any(item["ok"] for item in evidence)):
                        verdict = Verdict(
                            INCONCLUSIVE,
                            "goal verifier reported verified without scoped independent evidence")
                        evidence = []
                    self.store.record_event(
                        mission_id, "goal_verification", DONE,
                        payload={"verdict": verdict.status, "reason": verdict.reason,
                                 "evidence": evidence})
                    self.store.record_checkpoint(
                        mission_id, token, "goal_verdict",
                        {"verdict": verdict.status, "reason": verdict.reason,
                         "evidence": evidence}, case=m.case)
                    if verdict.status == VERIFIED:
                        return self._finish(mission_id, token, DONE_VERIFIED,
                                            verdict.reason or "goal independently verified")
                    if verdict.status == FAILED:
                        return self._finish(mission_id, token, FAILED_S,
                                            verdict.reason or "goal verification failed")
                    # A model report by itself remains neither evidence nor human
                    # acceptance.  Inconclusive/not-armed therefore fail closed.
                    return self._finish(
                        mission_id, token, NEEDS_YOU,
                        verdict.reason or reason or
                        "model reports done; no independent goal evidence")
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
                if cap.name == "browse.submit":
                    ready, gate_reason = self._browse_submit_ready(
                        self.store.events(mission_id, 40))
                    if not ready:
                        current = self.store.get(mission_id)
                        case = dict(current.case if current else {})
                        case["signal"] = ("Verification Gate refused browse.submit: " +
                                          gate_reason +
                                          ". Repair and verify the reversible browse step first.")[:800]
                        if not self.store.set_case_owned(mission_id, token, case):
                            return self._lost_state(mission_id, token)
                        self.store.record_event(
                            mission_id, "gate", "browse.submit",
                            payload={"verdict": "refused", "reason": gate_reason})
                        self.store.record_checkpoint(
                            mission_id, token, "submit_precondition_refused",
                            {"reason": gate_reason}, case=case)
                        self.store.account_runtime(mission_id, token, retries=1)
                        continue
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
                # This delay prevents tight inbox polling. It must not throttle
                # local reversible work such as composing several channel-ready
                # deliverables before the first external action.
                is_poll_read = cap.name == "observe"
                next_read_target = self._observe_target(args) if is_poll_read else None
                if is_poll_read and next_read_target != read_target:
                    reads = 0
                if is_poll_read and reads >= self.read_streak_cap:
                    self.store.schedule_wait(mission_id, int(time.time()) + self.read_wait_s)
                    return self._finish(
                        mission_id, token, WAITING,
                        f"paced: waited after {reads} reads before more {cap.name}")

                call_args = dict(args, _case=m.case, _leash=m.leash)
                if cap.name == "code" and m.leash.get("workspace_mode") == "isolated":
                    isolated = m.case.get("_isolated_workspace")
                    if not isolated:
                        return self._finish(
                            mission_id, token, NEEDS_YOU,
                            "isolated code workspace is not provisioned; attach a durable "
                            "_isolated_workspace before continuing")
                    specialist_scope = m.case.get("_resource_scope")
                    if specialist_scope is not None:
                        writable_roots = [
                            item.get("id") or item.get("path")
                            for item in specialist_scope if isinstance(item, dict) and
                            item.get("kind") == "file" and item.get("mode") == "write" and
                            os.path.isdir(str(item.get("id") or item.get("path") or ""))]
                        if not writable_roots:
                            return self._finish(
                                mission_id, token, NEEDS_YOU,
                                "specialist code needs a directory-level write resource; "
                                "the current file-level/read-only scope cannot be enforced by "
                                "the code child without expanding authority")
                    # The provisioner owns creation/cleanup; the Mission owns only
                    # the explicit path and cannot steer the child back to cwd.
                    call_args.pop("cwd", None)
                    call_args["workspace"] = str(isolated)
                bound_refusal = self._bound_refusal(m.leash, cap, call_args, {})
                if bound_refusal:
                    state = NEEDS_YOU if bound_refusal.startswith("human-required:") else FAILED_S
                    return self._finish(mission_id, token, state,
                                        ("needs your input: " if state == NEEDS_YOU else
                                         "leash denied: ") + bound_refusal.split(": ", 1)[-1])
                snapshot_fn = getattr(cap, "snapshot", None)
                self.store.record_checkpoint(
                    mission_id, token, "action_preparing",
                    {"capability": cap.name,
                     "args": {k: v for k, v in call_args.items()
                              if k not in ("_case", "_leash")}}, case=m.case)
                if callable(snapshot_fn):
                    snap_outcome = self._bounded_call(
                        lambda: snapshot_fn(call_args, mission_id), step_timeout,
                        cancel_owner=cap)
                    self.store.account_runtime(
                        mission_id, token, wall_ms=snap_outcome.elapsed_ms,
                        retries=1 if snap_outcome.timed_out or snap_outcome.error else 0)
                    if snap_outcome.timed_out or snap_outcome.error:
                        self.store.record_event(
                            mission_id, "watchdog", "prepare_timeout" if
                            snap_outcome.timed_out else "prepare_error",
                            payload={"capability": cap.name,
                                     "error": str(snap_outcome.error)[:500]})
                        exhausted = self.store.budget_reason(mission_id)
                        if exhausted:
                            return self._finish(mission_id, token, NEEDS_YOU, exhausted)
                        self.store.schedule_wait(mission_id, int(time.time()) + 60)
                        return self._finish(
                            mission_id, token, WAITING,
                            "%s preparation stalled; retry scheduled before any action" % cap.name)
                    snapshot = snap_outcome.value or {}
                else:
                    snapshot = {}
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
                self.store.record_checkpoint(
                    mission_id, token, "executing",
                    {"capability": cap.name, "nonce": nonce}, case=m.case)
                exec_outcome = self._bounded_call(
                    lambda: self._execute(nonce, cap, token), step_timeout,
                    cancel_owner=cap)
                self.store.account_runtime(
                    mission_id, token, wall_ms=exec_outcome.elapsed_ms)
                if exec_outcome.timed_out:
                    self.store.record_event(
                        mission_id, "watchdog", "action_timeout", nonce,
                        {"capability": cap.name, "timeout_seconds": step_timeout,
                         "cancel_requested": exec_outcome.cancelled})
                    self.store.fence_timed_out(
                        mission_id, token, "executing:%s" % cap.name,
                        "%s exceeded its wall-clock limit; outcome requires reconciliation" %
                        cap.name)
                    return self._state(mission_id, RECOVERY_REQUIRED)
                if isinstance(exec_outcome.error, ResourceBusy):
                    self.actions.refuse(nonce, "shared external resource busy; retry scheduled")
                    self.store.release_action_key(mission_id, action_key, token)
                    if not self.store.owns_run(mission_id, token):
                        return self._lost_state(mission_id, token)
                    self.store.schedule_wait(mission_id, int(time.time()) + 5)
                    return self._finish(mission_id, token, WAITING,
                                        "shared external resource busy; retrying shortly")
                if isinstance(exec_outcome.error, RefusedError):
                    e = exec_outcome.error
                    # The action latch was never acquired (pause/recovery won), or
                    # the approved world snapshot diverged before firing. Both are
                    # proven no-side-effect paths and may release the semantic key.
                    self.actions.refuse(nonce, "action did not reach execution: %s" % e)
                    self.store.release_action_key(mission_id, action_key, token)
                    if not self.store.owns_run(mission_id, token):
                        return self._lost_state(mission_id, token)
                    return self._finish(mission_id, token, NEEDS_YOU,
                                        "still blocked before execution: %s" % e)
                if exec_outcome.error is not None:
                    raise exec_outcome.error
                verdict, result = exec_outcome.value
                self.store.account_runtime(mission_id, token,
                                           **self._usage_from_result(result))
                self.store.record_step(mission_id, cap.name, nonce, verdict.status)
                self.store.complete_action_key(mission_id, nonce, verdict.status)
                self.store.record_event(
                    mission_id, "result", cap.name, nonce,
                    {"verdict": verdict.status, "reason": verdict.reason,
                     "result": _compact_event(result, 2000)})
                self.store.record_checkpoint(
                    mission_id, token, "result_recorded",
                    {"capability": cap.name, "nonce": nonce,
                     "verdict": verdict.status}, case=m.case)
                # An already-started primitive may finish after cancel. Preserve its
                # receipt, but never let its stale worker mutate campaign state/case.
                if not self.store.owns_claim(mission_id, token):
                    return self._lost_state(mission_id, token)
                if verdict.status == VERIFIED:
                    if not self._fold(m, cap.name, result, token=token):
                        return self._lost_state(mission_id, token)
                    self.store.record_checkpoint(
                        mission_id, token, "folded",
                        {"capability": cap.name, "nonce": nonce},
                        case=self.store.get(mission_id).case)
                # PAUSING may commit the completed result above, but must settle
                # before any verdict routing or next model/action boundary.
                if not self.store.owns_run(mission_id, token):
                    return self._lost_state(mission_id, token)
                if verdict.status == FAILED and cap.reversible:
                    # A reversible primitive proved that its attempted state did
                    # not satisfy the contract. That is actionable observation,
                    # not a reason to kill a long-running campaign. Fold a bounded
                    # diagnostic and let the planner choose a repaired next step;
                    # cumulative retry/turn budgets still stop pathological loops.
                    current = self.store.get(mission_id)
                    case = dict(current.case if current else {})
                    failures = list(case.get("_recent_failures") or [])
                    failures.append({"at": int(time.time()), "capability": cap.name,
                                     "reason": str(verdict.reason or "")[:1000],
                                     "result": _compact_event(result, 2000)})
                    case["_recent_failures"] = failures[-8:]
                    if not self.store.set_case_owned(mission_id, token, case):
                        return self._lost_state(mission_id, token)
                    self.store.account_runtime(mission_id, token, retries=1)
                    self.store.record_checkpoint(
                        mission_id, token, "reversible_failure",
                        {"capability": cap.name, "reason": str(verdict.reason or "")[:500]},
                        case=case)
                    exhausted = self.store.budget_reason(mission_id)
                    if exhausted:
                        return self._finish(mission_id, token, NEEDS_YOU, exhausted)
                    continue
                if verdict.status == FAILED:
                    return self._finish(mission_id, token, FAILED_S,
                                        f"{cap.name} failed: {verdict.reason}")
                if verdict.status != VERIFIED:
                    return self._finish(
                        mission_id, token, NEEDS_YOU,
                        f"{cap.name} fired but remains uncertain: {verdict.reason}")
                if is_poll_read:
                    reads += 1
                    read_target = next_read_target
                else:
                    reads = 0
                    read_target = None

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
        controlled = self._control_boundary(mission_id, token)
        if controlled == "_steered":
            return self._finish(
                mission_id, token, NEEDS_YOU,
                "steering arrived before the confirmed action; review it before execution")
        if controlled:
            return controlled
        m = self.store.get(mission_id)
        step_timeout = max(0.05, float(m.leash.get("max_step_seconds", 600)))
        self.store.record_checkpoint(
            mission_id, token, "executing",
            {"capability": name, "nonce": nonce, "confirmed": True}, case=m.case)
        outcome = self._bounded_call(
            lambda: self._execute(nonce, cap, token), step_timeout, cancel_owner=cap)
        self.store.account_runtime(mission_id, token, wall_ms=outcome.elapsed_ms)
        if outcome.timed_out:
            self.store.record_event(
                mission_id, "watchdog", "action_timeout", nonce,
                {"capability": name, "timeout_seconds": step_timeout,
                 "cancel_requested": outcome.cancelled})
            self.store.fence_timed_out(
                mission_id, token, "executing:%s" % name,
                "%s exceeded its wall-clock limit; outcome requires reconciliation" % name)
            return self._state(mission_id, RECOVERY_REQUIRED)
        if isinstance(outcome.error, ResourceBusy):
            # Confirmation remains bound to this exact approved payload.  Back off
            # durably and retry it on wake; do not strand RUNNING or ask the model
            # to synthesize a second action.
            if not self.store.owns_run(mission_id, token):
                return self._lost_state(mission_id, token)
            self.store.schedule_wait(mission_id, int(time.time()) + 5)
            return self._finish(mission_id, token, WAITING,
                                "shared external resource busy; confirmed action will retry")
        if isinstance(outcome.error, RefusedError):
            e = outcome.error
            if "world diverged" in str(e):
                self.actions.refuse(nonce, "approved target changed before execution")
                self.store.resolve_parked(nonce, "target-changed")
                rec = self.actions.get(nonce)
                if rec:
                    # It did not fire; a freshly prepared target may be proposed.
                    self.store.release_action_nonces(mission_id, [nonce])
            return self._finish(mission_id, token, NEEDS_YOU, f"still blocked: {e}")
        if outcome.error is not None:
            raise outcome.error
        verdict, result = outcome.value
        self.store.account_runtime(mission_id, token, **self._usage_from_result(result))
        self.store.resolve_parked(nonce, verdict.status)   # flip the awaiting row to its real verdict
        self.store.complete_action_key(mission_id, nonce, verdict.status)
        self.store.record_event(
            mission_id, "result", name, nonce,
            {"verdict": verdict.status, "reason": verdict.reason,
             "result": _compact_event(result, 2000)})
        self.store.record_checkpoint(
            mission_id, token, "result_recorded",
            {"capability": name, "nonce": nonce, "verdict": verdict.status},
            case=m.case)
        if not self.store.owns_claim(mission_id, token):
            return self._lost_state(mission_id, token)
        m = self.store.get(mission_id)
        if verdict.status == VERIFIED:
            if not self._fold(m, name, result, token=token):
                return self._lost_state(mission_id, token)
            self.store.record_checkpoint(
                mission_id, token, "folded", {"capability": name, "nonce": nonce},
                case=self.store.get(mission_id).case)
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

    def tick_missions(self, now=None, max_workers=None, max_batch=None) -> int:
        """Re-enter every mission whose durable wait is due. The one-line wiring for
        colliejobd (plan §5.2): the daemon owns no model — it wakes due campaigns,
        and advance() asks the model for the next action. Returns how many advanced."""
        now = int(now if now is not None else time.time())
        workers = max_workers if max_workers is not None else \
            int(os.environ.get("COLLIE_MISSION_WORKERS", "4"))
        workers = max(1, min(8, int(workers)))
        batch = max(1, min(64, int(max_batch if max_batch is not None else workers)))
        claimed = []
        # Alternate durable wakes and fresh work.  Claims happen before submit so
        # two daemons cannot enqueue the same campaign; batch<=workers prevents a
        # claimed Mission waiting behind a long-running sibling until its lease ages.
        queued = iter(self.store.queued_fair(batch, lane=self.lane))
        prefer_wait = True
        while len(claimed) < batch:
            item = None
            if prefer_wait:
                item = self.store.claim_due_wait(now, lane=self.lane)
            if not item:
                m = next(queued, None)
                if m:
                    token = self.store.claim_run(m.mission_id, expected=(QUEUED,))
                    item = (m.mission_id, token) if token else None
            if not item and not prefer_wait:
                item = self.store.claim_due_wait(now, lane=self.lane)
            if not item:
                # There may have been a claim race; scan the remaining fair rows.
                m = next(queued, None)
                if m:
                    token = self.store.claim_run(m.mission_id, expected=(QUEUED,))
                    item = (m.mission_id, token) if token else None
            if not item:
                break
            claimed.append(item)
            prefer_wait = not prefer_wait
        if not claimed:
            return 0
        if workers == 1:
            for mid, token in claimed:
                self._drive_claimed(mid, token)
            return len(claimed)
        with ThreadPoolExecutor(max_workers=min(workers, len(claimed)),
                                thread_name_prefix="mission") as pool:
            futures = [pool.submit(self._drive_claimed, mid, token)
                       for mid, token in claimed]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    # _drive_claimed fail-closes its Mission.  Keep the dispatcher
                    # alive even if an unforeseen worker exception crosses it.
                    pass
        return len(claimed)

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
             "max_irreversible_actions", "actions_per_hour", "max_model_tokens",
             "max_model_cost_usd", "max_active_wall_seconds", "max_elapsed_seconds",
             "max_step_seconds", "max_retries", "max_storage_bytes", "checkpoint_keep",
             "human_escalate_seconds", "human_timeout_seconds", "workspace_mode",
             "max_specialists", "max_specialist_depth"}
    unknown = sorted(set(bounds) - known)
    if unknown:
        raise ValueError("unenforced Mission leash bound(s): " + ", ".join(unknown))
    for key in ("max_total_steps", "max_irreversible_actions", "actions_per_hour",
                "max_model_tokens", "max_active_wall_seconds", "max_elapsed_seconds",
                "max_step_seconds", "max_retries", "max_storage_bytes", "checkpoint_keep",
                "human_escalate_seconds", "human_timeout_seconds", "max_specialists",
                "max_specialist_depth"):
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
    if "max_model_cost_usd" in bounds:
        try:
            bounds["max_model_cost_usd"] = float(bounds["max_model_cost_usd"])
        except (TypeError, ValueError):
            raise ValueError("Mission leash max_model_cost_usd must be numeric")
        if bounds["max_model_cost_usd"] <= 0:
            raise ValueError("Mission leash max_model_cost_usd must be positive")
    if bounds.get("workspace_mode", "current") not in ("current", "isolated"):
        raise ValueError("Mission leash workspace_mode must be 'current' or 'isolated'")
    if ("human_timeout_seconds" in bounds and "human_escalate_seconds" in bounds and
            bounds["human_timeout_seconds"] < bounds["human_escalate_seconds"]):
        raise ValueError("Mission leash human_timeout_seconds must be >= human_escalate_seconds")
    leash = {"may": sorted(default_may if may is None else may),
             "irreversible": "allow" if autonomous else "confirm",
             # Durable campaign-wide limits; unlike max_steps-per-advance these
             # survive every wait, restart, and competing daemon.
             "max_total_steps": 1000,
             "max_irreversible_actions": 100,
             "actions_per_hour": 12,
             "max_model_tokens": 2_000_000,
             "max_model_cost_usd": 25.0,
             "max_active_wall_seconds": 21_600,
             "max_elapsed_seconds": 2_592_000,
             "max_step_seconds": 600,
             "max_retries": 32,
             "max_storage_bytes": 5_000_000,
             "checkpoint_keep": 64,
             "human_escalate_seconds": 3_600,
             "human_timeout_seconds": 86_400,
             "max_specialists": 4,
             "max_specialist_depth": 2,
             "workspace_mode": "current"}
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
    "The 'observe' args.expect value is a LITERAL substring to find on one known page, not a "
    "question or semantic inspection request. To identify an account, understand page state, or "
    "inspect several platforms, use one separate read-only 'browse' action per site; never ask one "
    "browse child to cross several unrelated sites.\n"
    "To ACT on a website (fill a marketplace listing, submit a form, publish a post): use "
    "'browse' with a goal to fill/navigate it (it drives the real browser adaptively and STOPS "
    "before submitting), then 'browse.submit' to click the final Publish/Post — that last click "
    "is gated for the user's confirm.\n"
    "When using 'browse' only to inspect or navigate without changing a form, set "
    "args.read_only=true. For a fill/draft operation leave it false and provide args.expect so the "
    "fresh form/editor re-read can verify the intended values.\n"
    "A restricted browse child CANNOT see the Mission case, prior draft, or messages. For every "
    "write, embed each COMPLETE exact field value directly in args.goal and repeat the complete "
    "value in args.expect; never say 'use the case draft', 'prepared copy', 'above', or equivalent. "
    "Rich text/body expectations are exact, not prefix checks. Choose browse.submit only when the "
    "newest browse result is verified; after any failed/inconclusive browse, repair it first.\n"
    "For 'compose', put the writing request in args.instruction and supporting material in "
    "args.facts. Use args.text ONLY when it already contains the complete, final, ready-to-use "
    "copy. Never put an instruction such as 'write/create/draft a post' in args.text.\n"
    "Use credentials, email/phone identities, signed-in sessions, and verification-code inboxes "
    "that the user has already connected and authorized; routine signup fields, OTP retrieval, "
    "Next buttons, and authorized publish/send actions are ordinary work inside the leash. Never "
    "persist a credential or OTP in the case, event log, action args, or summary. A CAPTCHA or MFA "
    "challenge that explicitly requires a person, unavailable credentials, or a new identity/consent "
    "decision is a temporary human-assist boundary: choose 'needs_human', say exactly what is waiting "
    "in args.summary, and preserve the current step so the Mission can continue after the user handles "
    "it. Never attempt to bypass, outsource, or misrepresent a platform security check.\n"
    "If the goal names a duration, cadence, monitoring window, or repeated campaign, one successful "
    "action is not completion. Use 'wait' between due actions and keep going until the requested "
    "window or completion condition is actually reached.\n"
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
        ctx = _model_case_json(case, int(os.environ.get("COLLIE_MISSION_CONTEXT_CHARS", "12000")))
        user = f"GOAL: {goal}\n\nCASE (what you know):\n{ctx}\n\nPRIMITIVES:\n{cat}"
        meta = {}
        try:
            comp = self.provider.complete(_SYS, [{"role": "user", "content": user}], [])
            usage = getattr(comp, "usage", None)
            model = getattr(self.provider, "model", "") or ""
            if usage is not None:
                input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
                cache_read = int(getattr(usage, "cache_read", 0) or 0)
                cache_creation = int(getattr(usage, "cache_creation", 0) or 0)
                from .costs import cost_usd
                meta = {
                    "_usage": {"input_tokens": input_tokens,
                               "output_tokens": output_tokens,
                               "cache_tokens": cache_read + cache_creation},
                    "_cost_usd": cost_usd(model, input_tokens, output_tokens,
                                           cache_read, cache_creation),
                    "_model": model,
                }
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
                            "reason": "temporary model/provider error; retry with backoff",
                            "_retry": 1, **meta}
            else:
                import re
                m = re.search(r"\{.*\}", getattr(comp, "text", "") or "", re.S)
                if m:
                    plan = json.loads(m.group(0))
                    if isinstance(plan, dict) and plan.get("action"):
                        plan.update(meta)
                        return plan
        except Exception as e:
            from .providers import classify_error
            if classify_error(str(e)) == "retryable":
                return {"action": WAIT, "args": {"seconds": 60, "transient": True},
                        "reason": "temporary model transport error; retry with backoff",
                        "_retry": 1, **meta}
        # any failure -> hand back to the human rather than guess an action
        return {"action": NEEDS_HUMAN,
                "args": {"summary": "could not decide the next step automatically"},
                "reason": "decider unavailable", **meta}


def create_mission(store: MissionStore, mission_id, goal, case=None, leash=None, *,
                   lane="mission", external_run_id="") -> Mission:
    """Start a campaign from a goal in the user's words + an intake case + a leash.
    No per-errand template: the decider generalizes the flow from here."""
    return store.create(mission_id, goal, leash=leash or world_leash(), case=case or {},
                        lane=lane, external_run_id=external_run_id)
