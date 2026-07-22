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
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from . import leash as _leash
from .actions import ActionStore, RefusedError
from .jobs import (CANCELLED, DONE_ACCEPTED, DONE_VERIFIED, FAILED_S, NEEDS_YOU,
                   QUEUED, RUNNING, WAITING, all_capabilities, get_capability)
from .verifier import FAILED, Verdict

_TERMINAL = {DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED}
# control moves the decider can return instead of a primitive name
WAIT, DONE, NEEDS_HUMAN = "wait", "done", "needs_human"
_AWAITING = "awaiting-confirm"


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


# ── mission store (same on-disk db family as jobs/actions) ──────────────────
class MissionStore:
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
        self.db.execute("""CREATE TABLE IF NOT EXISTS missions(
            mission_id TEXT PRIMARY KEY, goal TEXT, leash_json TEXT, case_json TEXT,
            state TEXT, result TEXT, created_at INTEGER, updated_at INTEGER)""")
        # the campaign audit trail: one row per action the model chose + its verdict
        self.db.execute("""CREATE TABLE IF NOT EXISTS mission_steps(
            step_id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT, name TEXT,
            nonce TEXT, verdict TEXT, at INTEGER)""")
        # the durable loop: a mission's own wait table (separate from scheduler's
        # action-waits, so colliejobd's action tick never mis-drives a loop tick).
        self.db.execute("""CREATE TABLE IF NOT EXISTS mission_waits(
            wait_id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT,
            fire_at INTEGER, state TEXT, created_at INTEGER)""")
        self.db.commit()

    def create(self, mission_id, goal, leash=None, case=None) -> Mission:
        now = int(time.time())
        with self._lock:
            self.db.execute(
                "INSERT INTO missions(mission_id,goal,leash_json,case_json,state,"
                "result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (mission_id, goal, _js(leash or {}), _js(case or {}), QUEUED, "",
                 now, now))
            self.db.commit()
        return Mission(mission_id, goal, leash or {}, case or {}, QUEUED, "", now, now)

    def get(self, mission_id) -> Mission:
        r = self.db.execute("SELECT * FROM missions WHERE mission_id=?",
                            (mission_id,)).fetchone()
        if not r:
            return None
        return Mission(r["mission_id"], r["goal"], _jl(r["leash_json"]),
                       _jl(r["case_json"]), r["state"], r["result"],
                       r["created_at"], r["updated_at"])

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

    def list(self, state=None):
        q, a = "SELECT mission_id FROM missions", ()
        if state:
            q, a = q + " WHERE state=?", (state,)
        return [self.get(r["mission_id"]) for r in self.db.execute(q + " ORDER BY created_at", a)]

    def close(self):
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

    def __init__(self, store: MissionStore, actions: ActionStore, decider):
        self.store = store
        self.actions = actions
        self.decider = decider

    def _primitives(self):
        """What the decider may choose from — the registered neutral primitives,
        as {name, risk, reversible, description, args_hint}. Domain-agnostic."""
        return [{"name": c.name, "risk": c.risk, "reversible": c.reversible,
                 "description": c.description, "args": c.args_hint}
                for c in all_capabilities()]

    def _execute(self, nonce, cap):
        """Run an APPROVED action's real side effect once and capture its result
        (the receipt carries only the verdict; the campaign needs the payload)."""
        captured = {}

        def _side(rec):
            res = cap.execute(rec)
            captured["r"] = res
            return res

        receipt = self.actions.execute(nonce, side_effect_fn=_side,
                                       donecheck_fn=cap.verify)
        return Verdict(receipt.verdict, receipt.verdict_reason), captured.get("r")

    def _fold(self, m, name, result):
        """Merge an action's result into the case: under its own name, plus any
        top-level keys it explicitly promoted via result['case']."""
        case = self.store.get(m.mission_id).case
        if isinstance(result, dict):
            case[name] = {k: v for k, v in result.items() if k != "case"} or result
            if isinstance(result.get("case"), dict):
                case.update(result["case"])
        elif result is not None:
            case[name] = result
        self.store.set_case(m.mission_id, case)

    def advance(self, mission_id) -> str:
        """Drive the mission until it must stop: a gate (needs_you), a wait
        (WAITING), a hand-off (needs_you), completion, or failure. Re-entrant: safe
        to call again after a loop tick or a confirm+resume."""
        m = self.store.get(mission_id)
        if not m or m.state == NEEDS_YOU or m.terminal:
            return m.state if m else FAILED_S
        self.store.set_state(mission_id, RUNNING)
        reads = 0                                     # consecutive reversible reads (anti-poll-spin)
        for _ in range(self.max_steps):
            m = self.store.get(mission_id)
            decision = self.decider(m.goal, m.case, self._primitives()) or {}
            action = decision.get("action")
            args = decision.get("args") or {}
            reason = decision.get("reason") or ""

            if action in (None, DONE):
                self.store.set_state(mission_id, DONE_VERIFIED, reason or "goal reached")
                return DONE_VERIFIED
            if action == NEEDS_HUMAN:
                self.store.record_step(mission_id, NEEDS_HUMAN, "", NEEDS_HUMAN)
                self.store.set_state(mission_id, NEEDS_YOU,
                                     args.get("summary") or reason or "needs your input")
                return NEEDS_YOU
            if action == WAIT:
                secs = int(args.get("seconds", 3600))
                self.store.schedule_wait(mission_id, int(time.time()) + secs)
                self.store.set_state(mission_id, WAITING, reason or f"waiting {secs}s")
                return WAITING

            cap = get_capability(action)
            if not cap:
                self.store.set_state(mission_id, FAILED_S, f"unknown action {action!r}")
                return FAILED_S
            dec = _leash.evaluate(m.leash, cap.name, cap.risk)
            if dec.denied:
                self.store.set_state(mission_id, FAILED_S, f"leash denied: {dec.reason}")
                return FAILED_S
            # anti-poll-spin backstop: a monitor should read then WAIT, not read in a
            # tight loop. After read_streak_cap consecutive reversible reads, force a
            # durable wait (in the world each read is a slow, costly browser fetch).
            if cap.reversible and reads >= self.read_streak_cap:
                self.store.schedule_wait(mission_id, int(time.time()) + self.read_wait_s)
                self.store.set_state(mission_id, WAITING,
                                     f"paced: waited after {reads} reads before more {cap.name}")
                return WAITING

            # give the primitive read access to what the mission already knows —
            # a compose sees the facts, an observe sees its own prior poll count.
            call_args = dict(args, _case=m.case)
            nonce = self.actions.propose(cap.name, call_args, risk=cap.risk,
                                         job_id=mission_id, leash_id=mission_id)
            # irreversible + not pre-authorized -> materialize and park for confirm
            if dec.decision == _leash.ASK:
                self.store.record_step(mission_id, cap.name, nonce, _AWAITING)
                self.store.set_state(mission_id, NEEDS_YOU,
                                     f"confirm needed: {cap.name} — {reason}"[:200])
                return NEEDS_YOU

            # ALLOW: the leash IS the authority — auto-confirm and run now
            self.actions.confirm(nonce)
            verdict, result = self._execute(nonce, cap)
            self.store.record_step(mission_id, cap.name, nonce, verdict.status)
            if verdict.status == FAILED:
                self.store.set_state(mission_id, FAILED_S,
                                     f"{cap.name} failed: {verdict.reason}")
                return FAILED_S
            self._fold(m, cap.name, result)
            reads = reads + 1 if cap.reversible else 0    # reset the read streak on any write
            # loop: ask the decider for the next action

        self.store.set_state(mission_id, NEEDS_YOU,
                             "step budget exhausted — needs your input")
        return NEEDS_YOU

    def resume(self, mission_id) -> str:
        """Continue a mission parked in needs_you AFTER a human confirmed the parked
        action (actions.confirm(nonce)) or took over a hand-off. Runs the approved
        action if any, then drives the rest."""
        m = self.store.get(mission_id)
        if not m or m.state != NEEDS_YOU:
            return m.state if m else FAILED_S
        name, nonce = self.store.last_parked(mission_id)
        if not nonce:
            # a needs_human hand-off: no gated action to run — the human took over.
            self.store.set_state(mission_id, DONE_ACCEPTED, "handed off to human")
            return DONE_ACCEPTED
        cap = get_capability(name)
        try:
            verdict, result = self._execute(nonce, cap)
        except RefusedError as e:
            # not yet confirmed / a guard blocked it — stay parked, surface why.
            self.store.set_state(mission_id, NEEDS_YOU, f"still blocked: {e}")
            return NEEDS_YOU
        self.store.resolve_parked(nonce, verdict.status)   # flip the awaiting row to its real verdict
        if verdict.status == FAILED:
            self.store.set_state(mission_id, FAILED_S, f"{name} failed: {verdict.reason}")
            return FAILED_S
        self._fold(m, name, result)
        self.store.set_state(mission_id, RUNNING)   # clear needs_you so advance drives on
        return self.advance(mission_id)

    def tick_missions(self, now=None) -> int:
        """Re-enter every mission whose durable wait is due. The one-line wiring for
        colliejobd (plan §5.2): the daemon owns no model — it wakes due campaigns,
        and advance() asks the model for the next action. Returns how many advanced."""
        now = int(now if now is not None else time.time())
        n = 0
        for w in self.store.due_waits(now):
            if not self.store.claim_wait(w["wait_id"]):
                continue
            m = self.store.get(w["mission_id"])
            if m and m.state == WAITING:
                self.advance(w["mission_id"])
                n += 1
        return n


# ── leash builder: authority bounds, NOT an errand template ──────────────────
def world_leash(may=None, autonomous=False, expires=None, **bounds) -> dict:
    """Build a mission leash. `may` defaults to the neutral primitive families, so
    a mission can research/compose/observe and act on the web WITHIN the gate.
    `autonomous=True` pre-authorizes the irreversible primitives (still within the
    other bounds); otherwise they park for confirm. Extra `bounds` (price_floor,
    local_only, spend_max_usd, …) are policy the caller sets — domain values, not a
    template — and are carried on the leash for verifiers/primitives to enforce."""
    leash = {"may": sorted(may or ["research", "compose", "observe", "web.*", "browse", "browse.*"]),
             "irreversible": "allow" if autonomous else "confirm"}
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
    "is gated for the user's confirm.\n")


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
            if getattr(comp, "stop_reason", "") != "error":
                import re
                m = re.search(r"\{.*\}", getattr(comp, "text", "") or "", re.S)
                if m:
                    plan = json.loads(m.group(0))
                    if isinstance(plan, dict) and plan.get("action"):
                        return plan
        except Exception:
            pass
        # any failure -> hand back to the human rather than guess an action
        return {"action": NEEDS_HUMAN,
                "args": {"summary": "could not decide the next step automatically"},
                "reason": "decider unavailable"}


def create_mission(store: MissionStore, mission_id, goal, case=None, leash=None) -> Mission:
    """Start a campaign from a goal in the user's words + an intake case + a leash.
    No per-errand template: the decider generalizes the flow from here."""
    return store.create(mission_id, goal, leash=leash or world_leash(), case=case or {})
