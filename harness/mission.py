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
    or a control move (wait N / needs_authorization / needs_human / done). The container gates + runs it,
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
from datetime import datetime, timezone
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
WAIT, DONE, NEEDS_HUMAN, NEEDS_AUTHORIZATION, UPDATE_COVERAGE = (
    "wait", "done", "needs_human", "needs_authorization", "update_coverage")
_AWAITING = "awaiting-confirm"

_AUTH_RISK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_PERSON_REQUIRED_AUTH = {
    "captcha", "biometric", "kyc", "identity_proof", "legal_signature",
    "payment", "spending", "person_required_mfa", "security_key",
}
_COVERAGE_TERMINAL = {"completed", "exhausted", "scheduled", "deferred", "skipped"}
_COVERAGE_OPEN = {"pending", "active", "attempted", "blocked"}
_BLOCKER_KINDS = {
    "policy", "eligibility", "missing_authority", "technical", "deadline",
    "no_suitable_action",
}


def _setting_on(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _standing_authority() -> dict:
    """Return the user's reusable, non-secret Mission authority profile.

    This intentionally stores an age *band*, never a birth date.  The returned
    facts are explicit user claims from Settings, not model inferences.  Hard
    person/security/legal boundaries are listed so the decider cannot mistake
    Hands-off mode for authority to solve or attest anything it encounters.
    """
    from . import settings
    try:
        age = int(settings.get("PROFILE_AGE_BAND", "unset"))
    except (TypeError, ValueError):
        age = 0
    max_risk = str(settings.get("MAX_AUTO_AUTH_RISK", "medium") or "medium").lower()
    if max_risk not in ("low", "medium"):
        max_risk = "medium"
    claims = {}
    for threshold in (16, 18, 21):
        claims["age_at_least_%d" % threshold] = bool(age >= threshold)
    return {
        "source": "user_settings",
        "auto_apply_profile_claims": _setting_on(
            settings.get("AUTO_APPLY_PROFILE_CLAIMS", "off")),
        "defer_missing_authorizations": _setting_on(
            settings.get("DEFER_MISSING_AUTHORIZATIONS", "on")),
        "max_auto_risk": max_risk,
        "claims": claims,
        "never_auto": sorted(_PERSON_REQUIRED_AUTH),
    }


def _authorization_request(args, reason="") -> dict:
    """Normalize one model request into a stable, receipt-safe authorization key."""
    args = dict(args or {})
    summary = str(args.get("summary") or reason or "authorization required").strip()[:1000]
    kind = str(args.get("kind") or args.get("category") or "routine").strip().lower()
    kind = re.sub(r"[^a-z0-9_.-]", "_", kind)[:80] or "routine"
    claim = str(args.get("claim") or "").strip().lower()
    if not claim:
        age = re.search(r"(?:at least|age(?:d)?|满)\s*(16|18|21)|"
                        r"(16|18|21)\s*(?:years? old|岁)", summary, re.I)
        if age:
            claim = "age_at_least_%s" % next(x for x in age.groups() if x)
    if claim and not re.fullmatch(r"[a-z0-9_.-]{1,100}", claim):
        claim = ""
    risk = str(args.get("risk") or "medium").strip().lower()
    if risk not in _AUTH_RISK:
        risk = "medium"
    raw_url = str(args.get("url") or "").strip()
    domain = str(args.get("domain") or args.get("platform") or "").strip().lower()
    if raw_url:
        domain = (urlsplit(raw_url).hostname or domain).lower()
    domain = re.sub(r"[^a-z0-9.-]", "", domain)[:253]
    operation = str(args.get("operation") or args.get("button") or
                    args.get("action") or "authorize").strip().lower()[:120]
    material = {"kind": kind, "claim": claim, "risk": risk,
                "domain": domain, "operation": operation}
    key = hashlib.sha256(json.dumps(
        material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:20]
    return {"id": "auth_" + key, **material, "summary": summary,
            "blocking": bool(args.get("blocking")), "requested_at": int(time.time())}


def _resolved_authorization(request, resolved) -> dict | None:
    """Return an equal-or-stronger explicit grant for the same semantic request.

    Model wording is not authorization identity.  A confirmed claim such as a
    Product Hunt age attestation remains the same grant when the planner later
    changes the operation phrase or asks at a lower risk level.  Claim-less
    requests stay strict so unrelated missing facts on one site cannot alias.
    """
    request = dict(request or {})
    request_id = str(request.get("id") or "")
    request_kind = str(request.get("kind") or "")
    request_claim = str(request.get("claim") or "")
    request_domain = str(request.get("domain") or "").lower()
    request_operation = re.sub(r"\s+", " ", str(request.get("operation") or "").lower()).strip()
    request_risk = _AUTH_RISK.get(str(request.get("risk") or "medium"), 1)
    for raw in resolved or []:
        if not isinstance(raw, dict) or not raw.get("resolution"):
            continue
        item = dict(raw)
        if request_id and str(item.get("id") or "") == request_id:
            return item
        if str(item.get("kind") or "") != request_kind:
            continue
        if str(item.get("domain") or "").lower() != request_domain:
            continue
        if _AUTH_RISK.get(str(item.get("risk") or "medium"), 1) < request_risk:
            continue
        item_claim = str(item.get("claim") or "")
        if request_claim:
            if item_claim == request_claim:
                return item
            continue
        item_operation = re.sub(
            r"\s+", " ", str(item.get("operation") or "").lower()).strip()
        if request_operation and item_operation == request_operation:
            return item
    return None


def _followup_request(args, reason="", now=None) -> dict:
    """Normalize an explicitly branch-scoped wait into durable case state."""
    args = dict(args or {})
    branch = str(args.get("branch") or args.get("key") or "").strip()
    if not branch:
        return {}
    branch = re.sub(r"\s+", " ", branch)[:180]
    try:
        seconds = max(1, min(int(args.get("seconds", 3600)), 31536000))
    except (TypeError, ValueError):
        seconds = 3600
    now = int(time.time() if now is None else now)
    stable = hashlib.sha256(branch.lower().encode("utf-8")).hexdigest()[:20]
    return {
        "id": "followup_%s" % stable,
        "branch": branch,
        "summary": str(args.get("summary") or reason or branch).strip()[:500],
        "seconds": seconds,
        "due_at": now + seconds,
        "scheduled_at": now,
        "status": "scheduled",
    }


def _campaign_coverage(case) -> list[dict]:
    """Return bounded, normalized campaign branches from durable Mission state."""
    rows = []
    for raw in (dict(case or {}).get("_campaign_coverage") or []):
        if not isinstance(raw, dict):
            continue
        branch = re.sub(r"\s+", " ", str(raw.get("branch") or "").strip())[:180]
        if not branch:
            continue
        status = str(raw.get("status") or "pending").strip().lower()
        if status not in (_COVERAGE_OPEN | _COVERAGE_TERMINAL):
            status = "pending"
        rows.append({**raw, "branch": branch, "status": status,
                     "required": bool(raw.get("required", True))})
    return rows[-40:]


def _open_campaign_coverage(case) -> list[dict]:
    return [row for row in _campaign_coverage(case)
            if row.get("required", True) and row.get("status") not in _COVERAGE_TERMINAL]


def _coverage_branch_index(rows, branch):
    """Match an exact branch, or one unique lifecycle-word alias.

    Models commonly call a ``launch`` branch ``signup`` or ``onboarding`` once
    they reach that phase.  Accepting only a unique normalized match preserves
    deterministic coverage accounting without letting a fuzzy label close the
    wrong channel.
    """
    branch = str(branch or "").strip()
    exact = [i for i, row in enumerate(rows)
             if str(row.get("branch") or "").lower() == branch.lower()]
    if len(exact) == 1:
        return exact[0]

    def key(value):
        words = re.findall(r"[a-z0-9]+", str(value or "").lower())
        lifecycle = {"launch", "signup", "sign", "up", "onboarding",
                     "presence", "account", "campaign", "branch", "assets"}
        return " ".join(word for word in words if word not in lifecycle)

    wanted = key(branch)
    aliases = [i for i, row in enumerate(rows)
               if wanted and key(row.get("branch")) == wanted]
    return aliases[0] if len(aliases) == 1 else None


def _standing_authorizes(request, authority) -> tuple[bool, str]:
    """Resolve only a deterministic match to an explicit standing fact.

    A model cannot make an unsafe request delegable by labeling it low-risk:
    person/security/legal categories are a hard deny, and an asserted fact must
    exist in the user's settings profile.  Ordinary publish/send authority stays
    in the existing payload-bound Leash and Verification Gate.
    """
    request, authority = dict(request or {}), dict(authority or {})
    kind = str(request.get("kind") or "routine")
    if kind in _PERSON_REQUIRED_AUTH:
        return False, "%s always requires the person" % kind
    risk = str(request.get("risk") or "medium")
    ceiling = str(authority.get("max_auto_risk") or "medium")
    if _AUTH_RISK.get(risk, 1) > _AUTH_RISK.get(ceiling, 1):
        return False, "%s risk exceeds the %s standing ceiling" % (risk, ceiling)
    claim = str(request.get("claim") or "")
    if claim:
        if not authority.get("auto_apply_profile_claims"):
            return False, "automatic profile claims are disabled"
        if not bool((authority.get("claims") or {}).get(claim)):
            return False, "the exact profile claim is not confirmed"
        return True, "exact user-confirmed profile claim"
    return False, "no exact standing grant matches this request"


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
        "pending_authorizations", "resolved_authorizations",
        "pending_followups", "_due_followups", "resolved_followups",
        "_campaign_coverage",
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
    priority = ("_authority", "_standing_authority", "_mission_summary", "human_updates",
                "pending_authorizations", "resolved_authorizations",
                "_campaign_coverage", "pending_followups", "_due_followups", "_activity_ledger",
                "_do_not_repeat", "browse_sites", "_recent_results",
                "_recent_events", "_checkpoint")
    ordered = {key: case[key] for key in priority if key in case}
    ordered.update({key: value for key, value in case.items() if key not in ordered})
    raw = json.dumps(ordered, ensure_ascii=False, default=str)
    limit = max(1000, int(limit))
    if len(raw) <= limit:
        return raw
    # Preserve every priority field in bounded form, then spend the remainder on
    # older context.  The explicit marker prevents the model treating it as full.
    budgets = {"_authority": 1000, "_standing_authority": 1000,
               "_mission_summary": 900, "human_updates": 700,
               "pending_authorizations": 1000, "resolved_authorizations": 600,
               "_campaign_coverage": 1800,
               "pending_followups": 1000, "_due_followups": 800,
               "_activity_ledger": 2200, "_do_not_repeat": 900,
               "browse_sites": 1900, "_recent_results": 1200,
               "_recent_events": 900, "_checkpoint": 300}
    head = {}
    for key in priority:
        if key not in ordered:
            continue
        if key == "browse_sites":
            head[key] = _bounded_mapping_values(ordered[key], budgets[key])
        elif key in ("human_updates", "_campaign_coverage", "pending_followups", "_due_followups",
                     "_activity_ledger", "_do_not_repeat", "_recent_results",
                     "_recent_events"):
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
    encoded = json.dumps(head, ensure_ascii=False, default=str)
    # Never hand the planner byte-sliced JSON.  With several recovery-critical
    # fields present at once, independent per-field budgets can exceed the total
    # envelope. Repeatedly shrink the largest value while retaining every field
    # name; the final context is both bounded and syntactically valid.
    for _ in range(80):
        if len(encoded) <= limit:
            return encoded
        candidates = [(len(json.dumps(value, ensure_ascii=False, default=str)), key)
                      for key, value in head.items() if key != "_context_truncated"]
        if not candidates:
            break
        size, key = max(candidates)
        value = head[key]
        if isinstance(value, list) and len(value) > 1:
            head[key] = value[-max(1, len(value) // 2):]
        else:
            head[key] = _bounded_json(value, max(64, int(size * .55)))
        newer = json.dumps(head, ensure_ascii=False, default=str)
        if len(newer) >= len(encoded):
            head[key] = {"truncated": True}
            newer = json.dumps(head, ensure_ascii=False, default=str)
        encoded = newer
    if len(encoded) <= limit:
        return encoded
    # The minimum envelope is far below the enforced 1,000-character floor, but
    # retain a valid fail-closed fallback if a future field name changes that.
    return json.dumps({"_context_truncated": True,
                       "_mission_summary": "working context exceeded its safe envelope"},
                      ensure_ascii=False)


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
            parent_mission_id TEXT NOT NULL DEFAULT '',
            progress_seq INTEGER NOT NULL DEFAULT 0,
            progress_at INTEGER NOT NULL DEFAULT 0,
            active_phase TEXT NOT NULL DEFAULT '',
            active_since INTEGER NOT NULL DEFAULT 0,
            run_started_at INTEGER NOT NULL DEFAULT 0,
            active_wall_ms INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_tokens INTEGER NOT NULL DEFAULT 0,
            model_calls INTEGER NOT NULL DEFAULT 0,
            turns INTEGER NOT NULL DEFAULT 0,
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
                          ("external_run_id", "TEXT NOT NULL DEFAULT ''"),
                          ("parent_mission_id", "TEXT NOT NULL DEFAULT ''"),
                          ("model_calls", "INTEGER NOT NULL DEFAULT 0"),
                          ("turns", "INTEGER NOT NULL DEFAULT 0")):
            if col not in runtime_cols:
                try:
                    self.db.execute(
                        "ALTER TABLE mission_runtime ADD COLUMN %s %s" % (col, decl))
                except sqlite3.OperationalError:
                    # Multiple long-lived Collie processes may race the same
                    # additive upgrade; accept only the peer-already-added case.
                    if col not in {r[1] for r in self.db.execute(
                            "PRAGMA table_info(mission_runtime)")}:
                        raise
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS mission_runtime_parent "
            "ON mission_runtime(parent_mission_id)")
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
        # Idempotent crash-safe repair: decision reservations are the durable
        # logical call/turn receipts.  MAX never rewinds a newer counter (and
        # remains valid if calls and turns later diverge), while a process dying
        # after ALTER but before this UPDATE is repaired on the next open.
        for col in ("model_calls", "turns"):
            self.db.execute(
                "UPDATE mission_runtime SET %s=MAX(%s,(SELECT COUNT(*) "
                "FROM mission_events e WHERE e.mission_id=mission_runtime.mission_id "
                "AND e.kind='decision'))" % (col, col))
        # Specialist ancestry is budget authority, not mutable conversational
        # context.  New rows persist it directly below.  Existing databases from
        # before that column existed get one conservative migration from the
        # creation-time case snapshot; later set_case() calls never rewrite it.
        legacy_specialists = self.db.execute(
            "SELECT r.mission_id,r.lane,r.external_run_id,m.case_json "
            "FROM mission_runtime r JOIN missions m ON m.mission_id=r.mission_id "
            "WHERE COALESCE(r.parent_mission_id,'')='' AND (r.lane='specialist' "
            "OR COALESCE(r.external_run_id,'')<>'' "
            "OR INSTR(m.case_json,'\"_specialist_run_id\"')>0)"
        ).fetchall()
        for row in legacy_specialists:
            legacy_case = _jl(row["case_json"])
            parent_mid = str(legacy_case.get("_parent_mission_id") or "")
            specialist_hint = (row["lane"] == "specialist" or
                               bool(row["external_run_id"]) or
                               bool(legacy_case.get("_specialist_run_id")))
            if not specialist_hint:
                continue
            # Mark a hinted legacy row as delegated even when its parent is
            # missing. _lineage_locked() will then fail closed instead of
            # silently granting it a fresh root budget.
            self.db.execute(
                "UPDATE mission_runtime SET lane='specialist' WHERE mission_id=?",
                (row["mission_id"],))
            if parent_mid and self.db.execute(
                    "SELECT 1 FROM missions WHERE mission_id=?", (parent_mid,)).fetchone():
                self.db.execute(
                    "UPDATE mission_runtime SET parent_mission_id=? WHERE mission_id=? "
                    "AND COALESCE(parent_mission_id,'')=''",
                    (parent_mid, row["mission_id"]))
        self.db.commit()

    def create(self, mission_id, goal, leash=None, case=None, *, lane="mission",
               external_run_id="") -> Mission:
        now, case = int(time.time()), dict(case or {})
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                parent_mid = str(case.get("_parent_mission_id") or "") \
                    if str(lane or "mission") == "specialist" else ""
                if str(lane or "mission") == "specialist" and not parent_mid:
                    raise ValueError("specialist Mission requires a durable parent Mission")
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
                    "lane,external_run_id,parent_mission_id) VALUES(?,?,?,?,?,?,?)",
                    (mission_id, now, "created", len(_js(case).encode("utf-8")),
                     str(lane or "mission")[:40], str(external_run_id or "")[:100],
                     parent_mid))
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

    def _lineage_locked(self, mission_id):
        """Return actor -> root budget rows, or a fail-closed lineage error.

        ``parent_mission_id`` is immutable authority metadata.  The recursive
        query deliberately joins both runtime and Mission rows so a missing
        ancestor, a corrupt cycle, or a legacy specialist with no parent cannot
        silently turn a delegated Mission into a fresh budget root.
        """
        rows = self.db.execute(
            "WITH RECURSIVE lineage(mission_id,parent_mission_id,lane,depth) AS ("
            "SELECT mission_id,parent_mission_id,lane,0 FROM mission_runtime "
            "WHERE mission_id=? UNION ALL "
            "SELECT r.mission_id,r.parent_mission_id,r.lane,lineage.depth+1 "
            "FROM mission_runtime r JOIN lineage "
            "ON r.mission_id=lineage.parent_mission_id WHERE lineage.depth<63) "
            "SELECT lineage.mission_id,lineage.parent_mission_id,lineage.lane,"
            "lineage.depth,m.leash_json,m.created_at FROM lineage JOIN missions m "
            "ON m.mission_id=lineage.mission_id ORDER BY lineage.depth",
            (mission_id,)).fetchall()
        if not rows:
            return [], "mission no longer exists"
        ids = [row["mission_id"] for row in rows]
        if len(ids) != len(set(ids)) or rows[-1]["parent_mission_id"]:
            return [], "mission budget lineage is corrupt or incomplete"
        for row in rows:
            if row["lane"] == "specialist" and not row["parent_mission_id"]:
                return [], "specialist budget lineage is missing its parent"
        return rows, ""

    def _aggregate_runtime_locked(self, mission_id):
        row = self.db.execute(
            "WITH RECURSIVE subtree(mission_id) AS (SELECT ? UNION "
            "SELECT r.mission_id FROM mission_runtime r JOIN subtree "
            "ON r.parent_mission_id=subtree.mission_id) "
            "SELECT COALESCE(SUM(r.active_wall_ms),0) active_wall_ms,"
            "COALESCE(SUM(r.input_tokens),0) input_tokens,"
            "COALESCE(SUM(r.output_tokens),0) output_tokens,"
            "COALESCE(SUM(r.cache_tokens),0) cache_tokens,"
            "COALESCE(SUM(r.model_calls),0) model_calls,"
            "COALESCE(SUM(r.turns),0) turns,"
            "COALESCE(SUM(r.model_cost_microusd),0) model_cost_microusd,"
            "COALESCE(SUM(r.retry_count),0) retry_count,"
            "COALESCE(SUM(r.storage_bytes),0) storage_bytes "
            "FROM mission_runtime r JOIN subtree ON subtree.mission_id=r.mission_id",
            (mission_id,)).fetchone()
        out = dict(row) if row else {}
        out["model_cost_usd"] = out.get("model_cost_microusd", 0) / 1_000_000.0
        return out

    def aggregate_runtime(self, mission_id):
        """Return usage for this Mission plus every durable descendant."""
        with self._lock:
            lineage, error = self._lineage_locked(mission_id)
            if error:
                return {}
            return self._aggregate_runtime_locked(lineage[0]["mission_id"])

    @staticmethod
    def _runtime_budget_reason(leash, rt, created_at, now):
        total_tokens = (int(rt.get("input_tokens", 0)) +
                        int(rt.get("output_tokens", 0)) +
                        int(rt.get("cache_tokens", 0)))
        checks = (
            (int(leash.get("max_model_tokens", 2_000_000)) > 0 and
             total_tokens >= int(leash.get("max_model_tokens", 2_000_000)),
             "mission model-token budget exhausted"),
            (float(leash.get("max_model_cost_usd", 25.0)) > 0 and
             float(rt.get("model_cost_usd", 0.0)) >=
             float(leash.get("max_model_cost_usd", 25.0)),
             "mission model-cost budget exhausted"),
            (int(leash.get("max_model_calls", leash.get("max_total_steps", 1000))) > 0 and
             int(rt.get("model_calls", 0)) >=
             int(leash.get("max_model_calls", leash.get("max_total_steps", 1000))),
             "mission model-call budget exhausted"),
            (int(leash.get("max_active_wall_seconds", 21600)) > 0 and
             int(rt.get("active_wall_ms", 0)) >=
             int(leash.get("max_active_wall_seconds", 21600)) * 1000,
             "mission active wall-time budget exhausted"),
            (int(leash.get("max_elapsed_seconds", 2592000)) > 0 and
             now - int(created_at or now) >=
             int(leash.get("max_elapsed_seconds", 2592000)),
             "mission elapsed-time budget exhausted"),
            (int(leash.get("max_retries", 128)) > 0 and
             int(rt.get("retry_count", 0)) >= int(leash.get("max_retries", 128)),
             "mission retry budget exhausted"),
            (int(leash.get("max_storage_bytes", 5_000_000)) > 0 and
             int(rt.get("storage_bytes", 0)) >=
             int(leash.get("max_storage_bytes", 5_000_000)),
             "mission durable-storage budget exhausted"),
        )
        return next((reason for hit, reason in checks if hit), "")

    def _budget_reason_locked(self, mission_id, now):
        lineage, error = self._lineage_locked(mission_id)
        if error:
            return error
        for row in lineage:
            aggregate = self._aggregate_runtime_locked(row["mission_id"])
            reason = self._runtime_budget_reason(
                _jl(row["leash_json"]), aggregate, row["created_at"], now)
            if reason:
                return reason if row["mission_id"] == mission_id else \
                    "ancestor %s: %s" % (row["mission_id"], reason)
        return ""

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
        """Return the first self-or-ancestor subtree budget that is exhausted."""
        now = int(now if now is not None else time.time())
        with self._lock:
            return self._budget_reason_locked(mission_id, now)

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
                "SELECT w.wait_id,w.mission_id,w.fire_at FROM mission_waits w "
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
                               {"wake_wait_id": r["wait_id"],
                                "wake_fire_at": r["fire_at"]})
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

    def do_not_repeat(self, mission_id, limit=20):
        """Describe consequential actions protected by durable semantic keys.

        The model never needs the opaque hash itself.  It needs the durable fact
        that a concrete external action was already attempted/completed and must
        not be synthesized again under a fresh wording or browser element ref.
        """
        with self._lock:
            rows = self.db.execute(
                "SELECT k.nonce,k.state,k.at,s.name,s.verdict FROM mission_action_keys k "
                "LEFT JOIN mission_steps s ON s.mission_id=k.mission_id AND "
                "s.nonce=k.nonce WHERE k.mission_id=? AND k.state NOT IN "
                "('reserved','materialized') ORDER BY k.at DESC LIMIT ?",
                (mission_id, int(limit))).fetchall()
        return [{"capability": str(r["name"] or "external action")[:120],
                 "status": str(r["verdict"] or r["state"] or "attempted")[:80],
                 "at": int(r["at"] or 0),
                 "instruction": "Do not repeat this external action."}
                for r in reversed(rows)]

    def completed_action_nonces(self, mission_id, limit=200):
        """Return exact receipt identities for terminal semantic action keys.

        Unlike :meth:`do_not_repeat`, this is an internal verifier seam: opaque
        nonces never enter model context or the UI.
        """
        with self._lock:
            rows = self.db.execute(
                "SELECT nonce FROM mission_action_keys WHERE mission_id=? AND "
                "state NOT IN ('reserved','materialized') AND COALESCE(nonce,'')<>'' "
                "ORDER BY at DESC LIMIT ?", (mission_id, int(limit))).fetchall()
        return [str(r["nonce"]) for r in rows]

    def activity_ledger(self, mission_id, limit=24):
        """Return a compact human/model-readable view of the append-only audit log."""
        protected = set()
        with self._lock:
            rows = self.db.execute(
                "SELECT nonce FROM mission_action_keys WHERE mission_id=? AND "
                "state NOT IN ('reserved','materialized')", (mission_id,)).fetchall()
            protected = {str(r["nonce"] or "") for r in rows if r["nonce"]}

        entries = []
        for event in self.events(mission_id, 240):
            kind, name = str(event.get("kind") or ""), str(event.get("name") or "")
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            status, summary = "", ""
            if kind == "result":
                verdict = str(payload.get("verdict") or "").lower()
                status = {VERIFIED: "completed", FAILED: "failed",
                          INCONCLUSIVE: "uncertain"}.get(verdict, verdict or "recorded")
                summary = str(payload.get("reason") or "%s %s" % (name, status))
            elif kind == "goal_verification":
                verdict = str(payload.get("verdict") or "").lower()
                status = {VERIFIED: "completed", FAILED: "failed",
                          INCONCLUSIVE: "uncertain"}.get(verdict, verdict or "recorded")
                summary = str(payload.get("reason") or "Mission completion checked")
            elif kind == "authorization":
                status = "completed" if name == "standing_resolved" else "authorization_waiting"
                summary = str(payload.get("summary") or payload.get("reason") or
                              payload.get("claim") or "Authorization recorded")
            elif kind == "followup":
                status = ("scheduled" if name == "scheduled" else
                          str(payload.get("verdict") or "checked"))
                summary = str(payload.get("summary") or payload.get("reason") or
                              "Follow-up %s" % name)
            elif kind == "control" and name == WAIT:
                status = "scheduled"
                summary = str(payload.get("reason") or "Mission wake scheduled")
            elif kind == "control" and name in (NEEDS_HUMAN, "invalid"):
                status = "waiting_input" if name == NEEDS_HUMAN else "failed"
                summary = str(payload.get("summary") or payload.get("reason") or name)
            elif kind in ("watchdog", "gate"):
                status = "failed" if kind == "gate" else "uncertain"
                summary = str(payload.get("reason") or payload.get("error") or name)
            if not status:
                continue
            entry = {"at": int(event.get("at") or 0), "kind": kind,
                     "capability": name[:120], "status": status[:80],
                     "summary": " ".join(summary.split())[:500]}
            if event.get("nonce") in protected:
                entry["do_not_repeat"] = True
            entries.append(entry)
        return entries[-max(1, int(limit)):]

    def reserve_action(self, mission_id, action_key, irreversible, leash, name,
                       payload, run_token):
        """Atomically fence ownership and every ancestor's subtree quotas."""
        now = int(time.time())
        reservation_id = secrets.token_hex(16)
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                owner = self.db.execute(
                    "SELECT 1 FROM missions WHERE mission_id=? AND state=? AND run_token=?",
                    (mission_id, RUNNING, run_token)).fetchone()
                if not owner:
                    self.db.commit()
                    return False, "mission run ownership lost before action reservation", 0
                lineage, lineage_error = self._lineage_locked(mission_id)
                if lineage_error:
                    self.db.commit()
                    return False, lineage_error, 0
                runtime_reason = self._budget_reason_locked(mission_id, now)
                if runtime_reason:
                    self.db.commit()
                    return False, runtime_reason, 0
                if irreversible:
                    for budget in lineage:
                        budget_mid = budget["mission_id"]
                        budget_leash = _jl(budget["leash_json"])
                        max_irrev = int(budget_leash.get(
                            "max_irreversible_actions", 100))
                        hourly = int(budget_leash.get("actions_per_hour", 12))
                        base = (
                            "WITH RECURSIVE subtree(mission_id) AS (SELECT ? UNION "
                            "SELECT r.mission_id FROM mission_runtime r JOIN subtree "
                            "ON r.parent_mission_id=subtree.mission_id) ")
                        irrev = self.db.execute(
                            base +
                            "SELECT COUNT(*) n FROM mission_events p JOIN subtree "
                            "ON subtree.mission_id=p.mission_id "
                            "WHERE p.kind='proposed_irreversible' AND NOT EXISTS ("
                            "SELECT 1 FROM mission_events r WHERE r.mission_id=p.mission_id "
                            "AND r.kind='retracted_irreversible' AND r.nonce=p.nonce)",
                            (budget_mid,)).fetchone()["n"]
                        prefix = "" if budget_mid == mission_id else \
                            "ancestor %s: " % budget_mid
                        if irrev >= max_irrev:
                            self.db.commit()
                            return False, prefix + \
                                "mission irreversible-action budget exhausted", 0
                        recent = self.db.execute(
                            base +
                            "SELECT p.at FROM mission_events p JOIN subtree "
                            "ON subtree.mission_id=p.mission_id "
                            "WHERE p.kind='proposed_irreversible' AND p.at>? "
                            "AND NOT EXISTS (SELECT 1 FROM mission_events r "
                            "WHERE r.mission_id=p.mission_id "
                            "AND r.kind='retracted_irreversible' AND r.nonce=p.nonce) "
                            "ORDER BY p.at",
                            (budget_mid, now - 3600)).fetchall()
                        if hourly <= 0:
                            self.db.commit()
                            return False, prefix + \
                                "mission external-action rate limit reached", 0
                        if len(recent) >= hourly:
                            self.db.commit()
                            return False, prefix + \
                                "mission external-action rate limit reached", \
                                recent[0]["at"] + 3600
                if action_key:
                    # A semantic key for an irreversible action fences the whole
                    # durable campaign, not merely the specialist which happened
                    # to propose it.  BEGIN IMMEDIATE serializes this read+insert
                    # across MissionStore connections, so two siblings cannot
                    # both observe an empty slot.  Querying the live lineage/tree
                    # also covers rows written by the legacy per-Mission schema
                    # without a destructive table rewrite.
                    if irreversible:
                        root_mission_id = lineage[-1]["mission_id"]
                        old = self.db.execute(
                            "WITH RECURSIVE subtree(mission_id) AS (SELECT ? UNION "
                            "SELECT r.mission_id FROM mission_runtime r JOIN subtree "
                            "ON r.parent_mission_id=subtree.mission_id) "
                            "SELECT k.state FROM mission_action_keys k JOIN subtree "
                            "ON subtree.mission_id=k.mission_id WHERE k.action_key=? "
                            "LIMIT 1", (root_mission_id, action_key)).fetchone()
                    else:
                        old = self.db.execute(
                            "SELECT state FROM mission_action_keys WHERE mission_id=? "
                            "AND action_key=?", (mission_id, action_key)).fetchone()
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
            except Exception:
                self.db.rollback()
                raise
        return True, "", 0

    def reserve_decision(self, mission_id, leash):
        """Reserve one model turn against this Mission and every ancestor."""
        now = int(time.time())
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                lineage, lineage_error = self._lineage_locked(mission_id)
                if lineage_error or self._budget_reason_locked(mission_id, now):
                    self.db.commit()
                    return False
                for budget in lineage:
                    cap = int(_jl(budget["leash_json"]).get("max_total_steps", 1000))
                    total = self.db.execute(
                        "WITH RECURSIVE subtree(mission_id) AS (SELECT ? UNION "
                        "SELECT r.mission_id FROM mission_runtime r JOIN subtree "
                        "ON r.parent_mission_id=subtree.mission_id) "
                        "SELECT COUNT(*) n FROM mission_events e JOIN subtree "
                        "ON subtree.mission_id=e.mission_id WHERE e.kind='decision'",
                        (budget["mission_id"],)).fetchone()["n"]
                    if total >= cap:
                        self.db.commit()
                        return False
                self.db.execute(
                    "INSERT INTO mission_events(mission_id,kind,name,payload_json,at) "
                    "VALUES(?,?,?,?,?)", (mission_id, "decision", "model", "{}", now))
                self.db.execute(
                    "UPDATE mission_runtime SET model_calls=model_calls+1,turns=turns+1 "
                    "WHERE mission_id=?", (mission_id,))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
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

    def inherit_completed_action_keys(self, source_mission_id, target_mission_id):
        """Fence a successor from replaying predecessor actions with known outcomes.

        Reserved/materialized rows may represent an action that never fired or an
        outcome-uncertain boundary, so they are deliberately not copied.  Completed
        rows keep their semantic key and receipt nonce but shed run ownership: a new
        worker can inspect them, never acquire them as its own reservation.
        """
        with self._lock:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO mission_action_keys("
                "mission_id,action_key,nonce,state,at,owner_token,reservation_id) "
                "SELECT ?,action_key,nonce,state,at,'','' FROM mission_action_keys "
                "WHERE mission_id=? AND state NOT IN ('reserved','materialized')",
                (target_mission_id, source_mission_id))
            self.db.commit()
        return cur.rowcount

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
    'needs_authorization' (structured args), 'needs_human' (args.summary), 'done'.
    """

    # A runaway decider (loops forever choosing reversible actions) must not spin.
    # This is a per-dispatch planning slice, not a human-intervention threshold:
    # the durable driver yields and automatically resumes within campaign bounds.
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
                 control=None, hooks=None, completion_guard=None):
        self.store = store
        self.actions = actions
        self.decider = decider
        self.goal_verifier = goal_verifier
        self.lane = str(lane or "mission")
        self.control = control
        self.hooks = hooks
        self.completion_guard = completion_guard
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
        mission_fn = getattr(verifier, "verify_mission", None)
        if callable(mission_fn):
            result = mission_fn(m, self.store.events(m.mission_id, 50),
                                self.store.steps(m.mission_id))
            return result if isinstance(result, Verdict) else Verdict(
                INCONCLUSIVE, "goal verifier returned no typed evidence verdict")
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

    @staticmethod
    def _deadline_epoch(leash):
        """Return the leash's hard UTC deadline as epoch seconds, when present."""
        raw = (leash or {}).get("expires")
        if raw in (None, ""):
            return 0
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return int(raw)
        try:
            parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except (TypeError, ValueError, OverflowError):
            return 0

    def _verify_and_finish_goal(self, mission_id, token, mission, reason, step_timeout):
        """Run the one independent goal-verification path and settle the Mission."""
        self.store.record_step(mission_id, DONE, "", "reported")
        self.store.record_event(mission_id, "control", DONE,
                                payload={"reason": reason})
        self.store.record_checkpoint(
            mission_id, token, "goal_verifying", {"reason": reason}, case=mission.case)
        goal_outcome = self._bounded_call(
            lambda: self._goal_verdict(mission), step_timeout,
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
             "evidence": evidence}, case=mission.case)
        if verdict.status == VERIFIED:
            return self._finish(mission_id, token, DONE_VERIFIED,
                                verdict.reason or "goal independently verified")
        if verdict.status == FAILED:
            return self._finish(mission_id, token, FAILED_S,
                                verdict.reason or "goal verification failed")
        return self._finish(
            mission_id, token, NEEDS_YOU,
            verdict.reason or reason or
            "model reports done; no independent goal evidence")

    def _finish_at_deadline(self, mission_id, token, mission, step_timeout):
        """Close timers at the hard deadline, then verify without another action."""
        now = int(time.time())
        case = dict(mission.case)
        pending = [dict(x) for x in (case.get("pending_followups") or [])
                   if isinstance(x, dict)]
        due = [dict(x) for x in (case.get("_due_followups") or [])
               if isinstance(x, dict)]
        resolved = list(case.get("resolved_followups") or [])
        for item in pending + due:
            item.update({"status": "deadline_elapsed", "checked_at": now})
            resolved.append(item)
        case["pending_followups"] = []
        case["_due_followups"] = []
        case["resolved_followups"] = resolved[-20:]

        rows = _campaign_coverage(case)
        for index, item in enumerate(rows):
            previous_status = str(item.get("status") or "pending")
            if previous_status == "scheduled":
                closed = dict(item)
                closed.update({
                    "status": "completed",
                    "updated_at": now,
                    "summary": (str(item.get("summary") or "").rstrip(". ") +
                                ". Monitoring window ended at the authorized hard deadline.").lstrip(". "),
                })
                rows[index] = closed
                continue
            if previous_status in _COVERAGE_OPEN:
                closed = dict(item)
                closed.update({
                    "status": "exhausted",
                    "updated_at": now,
                    "blocker_kind": "deadline",
                    "alternatives_tried": list(item.get("alternatives_tried") or []) + [
                        "The authorized Mission window ended before another route could complete."],
                    "summary": (str(item.get("summary") or "").rstrip(". ") +
                                ". Unresolved when the authorized hard deadline elapsed.").lstrip(". "),
                })
                rows[index] = closed
        if rows:
            case["_campaign_coverage"] = rows
        case["signal"] = "Hard deadline reached; no new external action may start."
        if not self.store.set_case_owned(mission_id, token, case):
            return self._lost_state(mission_id, token)
        self.store.record_event(
            mission_id, "control", "deadline_reached",
            payload={"expires": (mission.leash or {}).get("expires"),
                     "closed_followups": len(pending) + len(due)})
        current = self.store.get(mission_id)
        return self._verify_and_finish_goal(
            mission_id, token, current,
            "hard deadline reached; final goal verification", step_timeout)

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

    def _refresh_authorizations(self, mission_id, token, mission):
        """Apply newly saved standing facts without stopping independent work."""
        authority = _standing_authority()
        case = dict((mission or self.store.get(mission_id)).case)
        pending = [dict(x) for x in (case.get("pending_authorizations") or [])
                   if isinstance(x, dict)]
        resolved = list(case.get("resolved_authorizations") or [])
        keep, changed = [], False
        for request in pending:
            existing = _resolved_authorization(request, resolved)
            if existing:
                changed = True
                self.store.record_event(
                    mission_id, "authorization", "resolved_reused",
                    str(request.get("id") or ""),
                    {"kind": request.get("kind"), "claim": request.get("claim"),
                     "domain": request.get("domain"),
                     "matched_id": existing.get("id"),
                     "reason": "equal-or-stronger Mission authorization was already resolved"})
                continue
            ok, why = _standing_authorizes(request, authority)
            if not ok:
                keep.append(request)
                continue
            item = {**request, "resolved_at": int(time.time()),
                    "resolution": "standing_authority", "reason": why}
            resolved.append(item)
            changed = True
            self.store.record_event(
                mission_id, "authorization", "standing_resolved",
                nonce=str(request.get("id") or ""),
                payload={"kind": request.get("kind"), "claim": request.get("claim"),
                         "domain": request.get("domain"), "reason": why})
        if changed:
            case["pending_authorizations"] = keep
            case["resolved_authorizations"] = resolved[-20:]
            if not self.store.set_case_owned(mission_id, token, case):
                return None, authority
            mission = self.store.get(mission_id)
        return mission, authority

    def _refresh_followups(self, mission_id, token, mission):
        """Promote elapsed branch timers into explicit due work for the planner."""
        case = dict((mission or self.store.get(mission_id)).case)
        pending = [dict(x) for x in (case.get("pending_followups") or [])
                   if isinstance(x, dict)]
        due = [dict(x) for x in (case.get("_due_followups") or [])
               if isinstance(x, dict)]
        now, keep, changed = int(time.time()), [], False
        checkpoint = self.store.latest_checkpoint(mission_id)
        woke_from_timer = bool(
            checkpoint and checkpoint.get("phase") == "claimed" and
            (checkpoint.get("payload") or {}).get("wake_wait_id"))
        forced_due_id = ""
        if woke_from_timer and pending:
            forced_due_id = str(min(
                pending, key=lambda x: int(x.get("due_at") or 0)).get("id") or "")
        known_due = {str(x.get("id") or "") for x in due}
        for item in pending:
            if (int(item.get("due_at") or 0) > now and
                    str(item.get("id") or "") != forced_due_id):
                keep.append(item)
                continue
            item["status"] = "due"
            item["surfaced_at"] = now
            if str(item.get("id") or "") not in known_due:
                due.append(item)
                known_due.add(str(item.get("id") or ""))
            changed = True
            self.store.record_event(
                mission_id, "followup", "due", str(item.get("id") or ""),
                {"branch": item.get("branch"), "summary": item.get("summary"),
                 "due_at": item.get("due_at")})
        if not changed:
            return mission
        case["pending_followups"] = keep[-20:]
        case["_due_followups"] = due[-20:]
        if not self.store.set_case_owned(mission_id, token, case):
            return None
        return self.store.get(mission_id)

    def _consume_due_followup(self, mission_id, token, capability, verdict, args=None):
        """Resolve only the due branch explicitly bound to this completed check."""
        current = self.store.get(mission_id)
        if not current:
            return False
        case = dict(current.case)
        due = [dict(x) for x in (case.get("_due_followups") or [])
               if isinstance(x, dict)]
        if not due:
            return True
        binding = str((args or {}).get("followup_branch") or
                      (args or {}).get("followup_id") or "").strip().lower()
        index = next((i for i, x in enumerate(due)
                      if binding and binding in
                      (str(x.get("branch") or "").strip().lower(),
                       str(x.get("id") or "").strip().lower())), None)
        if index is None:
            return True
        item = due.pop(index)
        item.update({"status": "checked", "checked_at": int(time.time()),
                     "capability": str(capability or "")[:120],
                     "verdict": str(verdict or "")[:80]})
        resolved = list(case.get("resolved_followups") or [])
        resolved.append(item)
        case["_due_followups"] = due[-20:]
        case["resolved_followups"] = resolved[-20:]
        if not self.store.set_case_owned(mission_id, token, case):
            return False
        self.store.record_event(
            mission_id, "followup", "checked", str(item.get("id") or ""),
            {"branch": item.get("branch"), "summary": item.get("summary"),
             "capability": capability, "verdict": verdict})
        return True

    def _handle_wait(self, mission_id, token, mission, args, reason):
        """Schedule one branch without pausing unrelated work.

        Legacy/unscoped waits, provider backoff, and explicit blocking waits retain
        whole-Mission semantics.  A named branch is deferred once; if the planner
        immediately repeats it, that is deterministic evidence that no independent
        work is currently available and the Mission sleeps until the earliest timer.
        """
        args = dict(args or {})
        try:
            secs = max(1, min(int(args.get("seconds", 3600)), 31536000))
        except (TypeError, ValueError):
            secs = 3600
        now = int(time.time())
        deadline = self._deadline_epoch(mission.leash)
        if deadline:
            secs = max(1, min(secs, max(1, deadline - now)))
        args["seconds"] = secs
        request = _followup_request(args, reason)
        if request and deadline:
            request["due_at"] = min(int(request.get("due_at") or deadline), deadline)
        open_coverage = _open_campaign_coverage(mission.case)
        if not request or args.get("blocking") or args.get("transient"):
            if open_coverage and not args.get("transient"):
                case = dict(mission.case)
                names = [str(x.get("branch") or "") for x in open_coverage[:6]]
                case["signal"] = (
                    "Whole-Mission wait refused: required campaign coverage remains: " +
                    ", ".join(names))[:800]
                if not self.store.set_case_owned(mission_id, token, case):
                    return self._lost_state(mission_id, token)
                self.store.record_event(
                    mission_id, "coverage", "wait_refused",
                    payload={"open": len(open_coverage), "branches": names,
                             "requested_blocking": bool(args.get("blocking"))})
                return "_continue"
            self.store.schedule_wait(mission_id, now + secs)
            self.store.record_event(
                mission_id, "control", WAIT,
                payload={"seconds": secs, "reason": reason,
                         "blocking": bool(args.get("blocking")),
                         "transient": bool(args.get("transient"))})
            return self._finish(mission_id, token, WAITING,
                                reason or "waiting %ss" % secs)

        case = dict(mission.case)
        pending = [dict(x) for x in (case.get("pending_followups") or [])
                   if isinstance(x, dict)]
        due = [dict(x) for x in (case.get("_due_followups") or [])
               if isinstance(x, dict)]
        previous = next((x for x in pending if x.get("id") == request["id"]), None)
        recent = [e for e in self.store.events(mission_id, 8)
                  if e.get("kind") != "decision"]
        last = recent[-1] if recent else {}
        immediate_repeat = bool(
            previous and last.get("kind") == "followup" and
            last.get("name") == "scheduled" and
            last.get("nonce") == request["id"])

        if previous:
            request["scheduled_at"] = int(previous.get("scheduled_at") or
                                          request["scheduled_at"])
            request["attempts"] = int(previous.get("attempts") or 1) + 1
            if immediate_repeat:
                request["due_at"] = int(previous.get("due_at") or request["due_at"])
            pending = [request if x.get("id") == request["id"] else x for x in pending]
        else:
            request["attempts"] = 1
            pending.append(request)
        due = [x for x in due if x.get("id") != request["id"]]
        case["pending_followups"] = pending[-20:]
        case["_due_followups"] = due[-20:]
        open_coverage = _open_campaign_coverage(case)
        if immediate_repeat and open_coverage:
            names = [str(x.get("branch") or "") for x in open_coverage[:6]]
            case["signal"] = (
                "Monitoring branch scheduled, but whole-Mission wait refused because "
                "required campaign coverage remains: " + ", ".join(names))[:800]
        if not self.store.set_case_owned(mission_id, token, case):
            return self._lost_state(mission_id, token)
        self.store.record_step(mission_id, WAIT, request["id"], "scheduled")
        self.store.record_event(
            mission_id, "followup", "scheduled", request["id"],
            {"branch": request["branch"], "summary": request["summary"],
             "seconds": request["seconds"], "due_at": request["due_at"],
             "attempts": request["attempts"]})

        if not immediate_repeat:
            return "_continue"
        if open_coverage:
            self.store.record_step(mission_id, WAIT, request["id"], "coverage-open")
            self.store.record_event(
                mission_id, "coverage", "wait_refused", request["id"],
                {"open": len(open_coverage),
                 "branches": [str(x.get("branch") or "") for x in open_coverage[:6]],
                 "scheduled_branch": request["branch"]})
            return "_continue"
        wake_at = min(int(x.get("due_at") or now + 60) for x in pending)
        if deadline:
            wake_at = min(wake_at, deadline)
        self.store.schedule_wait(mission_id, wake_at)
        self.store.record_event(
            mission_id, "control", WAIT,
            payload={"seconds": max(0, wake_at - int(time.time())),
                     "reason": reason or request["summary"],
                     "branches": len(pending)})
        return self._finish(mission_id, token, WAITING,
                            reason or request["summary"])

    def _handle_coverage_update(self, mission_id, token, mission, args, reason):
        """Update one predeclared campaign branch without trusting it as evidence."""
        args = dict(args or {})
        branch = re.sub(r"\s+", " ", str(args.get("branch") or "").strip())[:180]
        status = str(args.get("status") or "").strip().lower()
        detail = str(args.get("summary") or args.get("evidence") or reason or "").strip()[:1000]
        allowed = _COVERAGE_OPEN | _COVERAGE_TERMINAL
        case = dict(mission.case)
        rows = _campaign_coverage(case)
        index = _coverage_branch_index(rows, branch)
        invalid = ""
        if not branch or index is None:
            invalid = "coverage branch is not present in the required campaign backlog"
        elif status not in allowed:
            invalid = "coverage status must be one of %s" % ", ".join(sorted(allowed))
        elif status in _COVERAGE_TERMINAL and not detail:
            invalid = "terminal coverage status requires a concise reason or evidence"
        elif status == "blocked":
            blocker_kind = str(args.get("blocker_kind") or "").strip().lower()
            alternatives = [str(x).strip()[:300]
                            for x in (args.get("alternatives_tried") or [])
                            if str(x).strip()]
            prior = rows[index] if index is not None else {}
            prior_alternatives = {str(x).strip() for x in
                                  (prior.get("alternatives_tried") or [])
                                  if str(x).strip()}
            if blocker_kind not in (_BLOCKER_KINDS - {"deadline"}):
                invalid = ("blocked coverage requires blocker_kind: " +
                           ", ".join(sorted(_BLOCKER_KINDS - {"deadline"})))
            elif not alternatives:
                invalid = "blocked coverage requires the attempted route in alternatives_tried"
            elif str(prior.get("status") or "") == "blocked" and not (
                    set(alternatives) - prior_alternatives):
                invalid = ("repeated blocked coverage requires a distinct new route in "
                           "alternatives_tried, or a durable scheduled retry")
        elif status == "exhausted":
            blocker_kind = str(args.get("blocker_kind") or "").strip().lower()
            alternatives = [str(x).strip()[:300]
                            for x in (args.get("alternatives_tried") or [])
                            if str(x).strip()]
            permanent = blocker_kind in {"policy", "eligibility", "deadline"}
            if blocker_kind not in _BLOCKER_KINDS:
                invalid = ("exhausted coverage requires blocker_kind: " +
                           ", ".join(sorted(_BLOCKER_KINDS)))
            elif len(set(alternatives)) < (1 if permanent else 2):
                invalid = ("exhausted coverage requires auditable alternatives_tried "
                           "(%d distinct route(s) for %s)" %
                           (1 if permanent else 2, blocker_kind))
        elif status == "scheduled":
            followups = [dict(x) for x in (case.get("pending_followups") or [])
                         if isinstance(x, dict)]
            if not any(str(x.get("branch") or "").lower() == branch.lower()
                       for x in followups):
                invalid = "scheduled coverage requires a matching durable follow-up"
        if invalid:
            case["signal"] = ("Coverage update refused for %s: %s" %
                              (branch or "unnamed branch", invalid))[:800]
            if not self.store.set_case_owned(mission_id, token, case):
                return self._lost_state(mission_id, token)
            self.store.record_event(
                mission_id, "coverage", "update_refused",
                payload={"branch": branch, "status": status, "reason": invalid})
            return "_continue"

        now = int(time.time())
        requested_branch = branch
        item = dict(rows[index])
        branch = str(item.get("branch") or branch)
        item.update({"status": status, "updated_at": now})
        if detail:
            item["summary"] = detail
        if status == "blocked":
            item["blocker_kind"] = str(args.get("blocker_kind") or
                                       item.get("blocker_kind") or "technical")[:80]
            attempts = [str(x).strip()[:300]
                        for x in (item.get("alternatives_tried") or [])
                        if str(x).strip()]
            attempts.extend(str(x).strip()[:300]
                            for x in (args.get("alternatives_tried") or [])
                            if str(x).strip())
            item["alternatives_tried"] = list(dict.fromkeys(attempts))[-12:]
            item["blocked_attempts"] = int(item.get("blocked_attempts") or 0) + 1
            case["signal"] = (
                "Coverage branch %s is temporarily blocked and remains open. "
                "Try a distinct compliant route, schedule a retry, or provide "
                "auditable exhaustion evidence." % branch)[:800]
        elif status == "exhausted":
            item["blocker_kind"] = str(args.get("blocker_kind") or "")[:80]
            item["alternatives_tried"] = list(dict.fromkeys(
                str(x).strip()[:300] for x in (args.get("alternatives_tried") or [])
                if str(x).strip()))[-12:]
        rows[index] = item
        case["_campaign_coverage"] = rows[-40:]
        if status != "blocked":
            case.pop("signal", None)
        if not self.store.set_case_owned(mission_id, token, case):
            return self._lost_state(mission_id, token)
        self.store.record_step(mission_id, UPDATE_COVERAGE, branch, status)
        self.store.record_event(
            mission_id, "coverage", "updated", branch,
            {"branch": branch, "requested_branch": requested_branch,
             "status": status, "summary": detail,
             "blocker_kind": item.get("blocker_kind"),
             "alternatives_tried": item.get("alternatives_tried")})
        self.store.record_checkpoint(
            mission_id, token, "coverage_updated",
            {"branch": branch, "status": status}, case=case)
        return "_continue"

    def _handle_authorization(self, mission_id, token, mission, args, reason):
        """Resolve or defer one authorization request at branch scope.

        Missing authority is recorded in the Mission case and global status, then
        the decider gets another turn to pursue an independent branch.  Repeating
        the same unresolved request immediately is treated as proof that no other
        branch is currently available and parks the Mission instead of spinning.
        """
        request = _authorization_request(args, reason)
        authority = _standing_authority()
        ok, why = _standing_authorizes(request, authority)
        case = dict(mission.case)
        pending = [dict(x) for x in (case.get("pending_authorizations") or [])
                   if isinstance(x, dict)]
        resolved = list(case.get("resolved_authorizations") or [])
        existing = _resolved_authorization(request, resolved)
        if existing:
            pending = [x for x in pending
                       if not _resolved_authorization(x, [existing])]
            case["pending_authorizations"] = pending
            case["resolved_authorizations"] = resolved[-20:]
            if not self.store.set_case_owned(mission_id, token, case):
                return self._lost_state(mission_id, token)
            self.store.record_step(
                mission_id, NEEDS_AUTHORIZATION, request["id"], "already-authorized")
            self.store.record_event(
                mission_id, "authorization", "resolved_reused", request["id"],
                {"kind": request["kind"], "claim": request["claim"],
                 "domain": request["domain"], "matched_id": existing.get("id"),
                 "resolution": existing.get("resolution")})
            return "_continue"
        previous = next((x for x in pending if x.get("id") == request["id"]), None)
        recent_non_decisions = [e for e in self.store.events(mission_id, 8)
                                if e.get("kind") != "decision"]
        last_nondecision = recent_non_decisions[-1] if recent_non_decisions else {}
        immediate_repeat = bool(
            previous and last_nondecision.get("kind") == "authorization" and
            last_nondecision.get("name") == "deferred" and
            last_nondecision.get("nonce") == request["id"])
        if ok:
            pending = [x for x in pending if x.get("id") != request["id"]]
            resolved.append({**request, "resolved_at": int(time.time()),
                             "resolution": "standing_authority", "reason": why})
            case["pending_authorizations"] = pending
            case["resolved_authorizations"] = resolved[-20:]
            if not self.store.set_case_owned(mission_id, token, case):
                return self._lost_state(mission_id, token)
            self.store.record_step(
                mission_id, NEEDS_AUTHORIZATION, request["id"], "standing-authorized")
            self.store.record_event(
                mission_id, "authorization", "standing_resolved", request["id"],
                {"kind": request["kind"], "claim": request["claim"],
                 "domain": request["domain"], "reason": why})
            return "_continue"

        if previous:
            request["requested_at"] = previous.get("requested_at", request["requested_at"])
            request["attempts"] = int(previous.get("attempts", 1)) + 1
            pending = [request if x.get("id") == request["id"] else x for x in pending]
        else:
            request["attempts"] = 1
            pending.append(request)
        case["pending_authorizations"] = pending[-20:]
        if not self.store.set_case_owned(mission_id, token, case):
            return self._lost_state(mission_id, token)
        self.store.record_step(mission_id, NEEDS_AUTHORIZATION, request["id"], "pending")
        self.store.record_event(
            mission_id, "authorization", "deferred", request["id"],
            {"kind": request["kind"], "claim": request["claim"],
             "risk": request["risk"], "domain": request["domain"],
             "summary": request["summary"], "reason": why})

        should_park = (request.get("blocking") or immediate_repeat or
                       not authority.get("defer_missing_authorizations"))
        if should_park:
            return self._finish(
                mission_id, token, NEEDS_YOU,
                request["summary"] or "authorization is the only remaining dependency")
        return "_continue"

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

        def public_handle(key, child):
            """A bounded public account handle is profile data, not a secret."""
            return (bool(re.search(r"user.?name|public.?handle|profile.?handle", str(key), re.I))
                    and isinstance(child, str)
                    and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", child)))

        placeholder = re.compile(
            r"(?:n/?a|none|null|unknown|unavailable|missing|not\s+(?:available|known|"
            r"configured|provided|entered)|redacted|\[redacted\]|<redacted>)",
            re.I)
        email_value = re.compile(
            r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
            re.I)

        def phone_value(text):
            candidate = str(text or "")
            return (sum(ch.isdigit() for ch in candidate) >= 7 and
                    bool(re.search(r"\+?[\d(][\d\s().-]{5,}\d", candidate)))

        def keyed_sensitive_value(key, child):
            """Require a concrete value shape, not merely a sensitive-field word."""
            label = str(key)
            if not sensitive_key.search(label) or public_handle(label, child):
                return False
            if child in (None, "", [], {}, False):
                return False
            if isinstance(child, str):
                value = child.strip()
                if not value or placeholder.fullmatch(value):
                    return False
                if re.search(r"e.?mail", label, re.I):
                    return bool(email_value.search(value))
                if re.search(r"phone|mobile", label, re.I):
                    return phone_value(value)
                if re.search(r"otp|one.?time|verification.?code|passcode", label, re.I):
                    return bool(re.fullmatch(r"[A-Za-z0-9-]{4,12}", value))
            return True

        def embedded_sensitive_value(text):
            """Recognize actual inline secret/PII values in natural-language goals."""
            value = str(text or "")
            if email_value.search(value):
                return True
            phone = re.search(
                r"(?:phone|mobile)(?:\s+number)?\s*(?:is|=|:)\s*"
                r"(?P<value>\+?[\d(][\d\s().-]{5,}\d)", value, re.I)
            if phone and phone_value(phone.group("value")):
                return True
            if re.search(
                    r"(?:otp|one.?time(?:\s+code)?|verification.?code|passcode)\s*"
                    r"(?:is|=|:)\s*[A-Za-z0-9-]{4,12}\b", value, re.I):
                return True
            if re.search(
                    r"(?:password|secret|api.?key|access.?token)\s*(?:is|=|:)\s*"
                    r"(?!(?:n/?a|none|null|unknown|unavailable|missing|not\b|redacted\b))"
                    r"\S{4,}", value, re.I):
                return True
            if re.search(
                    r"(?:card(?:\s+number)?|cvv|cvc)\s*(?:is|=|:)\s*"
                    r"(?:\d[ -]?){3,19}\d", value, re.I):
                return True
            return False

        def sensitive_path(value, path=""):
            if isinstance(value, dict):
                for key, child in value.items():
                    if str(key).startswith("_"):
                        continue
                    child_path = (path + "." + str(key)).strip(".")
                    if keyed_sensitive_value(key, child):
                        return child_path
                    found = sensitive_path(child, child_path)
                    if found:
                        return found
            elif isinstance(value, list):
                for i, child in enumerate(value):
                    found = sensitive_path(child, "%s[%d]" % (path, i))
                    if found:
                        return found
            elif isinstance(value, str) and embedded_sensitive_value(value):
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
                m, standing = self._refresh_authorizations(mission_id, token, m)
                if m is None:
                    return self._lost_state(mission_id, token)
                m = self._refresh_followups(mission_id, token, m)
                if m is None:
                    return self._lost_state(mission_id, token)
                step_timeout = max(0.05, float(m.leash.get("max_step_seconds", 600)))
                deadline = self._deadline_epoch(m.leash)
                if deadline and int(time.time()) >= deadline:
                    return self._finish_at_deadline(
                        mission_id, token, m, step_timeout)
                exhausted = self.store.budget_reason(mission_id)
                if exhausted:
                    return self._finish(mission_id, token, NEEDS_YOU, exhausted)
                if not self.store.reserve_decision(mission_id, m.leash):
                    return self._finish(mission_id, token, NEEDS_YOU,
                                        "mission model-turn budget exhausted")
                model_case = dict(m.case)
                model_case["_authority"] = m.leash
                model_case["_standing_authority"] = standing
                model_case["_activity_ledger"] = self.store.activity_ledger(
                    mission_id, 24)
                model_case["_do_not_repeat"] = self.store.do_not_repeat(
                    mission_id, 20)
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
                # This checkpoint exists only to prove recovery is at a safe,
                # model-only boundary.  Persisting raw decision args here would
                # write a credential/PII value before _bound_refusal can reject
                # it, so retain structure rather than values.
                public_args = decision.get("args") or {}
                public_decision = {
                    "action": decision.get("action"),
                    "arg_keys": sorted(str(k) for k in public_args)
                    if isinstance(public_args, dict) else [],
                }
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
                if action == UPDATE_COVERAGE:
                    routed = self._handle_coverage_update(
                        mission_id, token, m, args, reason)
                    if routed == "_continue":
                        continue
                    return routed
                if action == DONE:
                    open_coverage = _open_campaign_coverage(m.case)
                    if open_coverage:
                        names = [str(x.get("branch") or "") for x in open_coverage[:6]]
                        case = dict(m.case)
                        case["signal"] = (
                            "Completion refused: required campaign coverage remains: " +
                            ", ".join(names))[:800]
                        if not self.store.set_case_owned(mission_id, token, case):
                            return self._lost_state(mission_id, token)
                        self.store.record_event(
                            mission_id, "coverage", "completion_refused",
                            payload={"open": len(open_coverage), "branches": names})
                        continue
                    if self.completion_guard is not None:
                        try:
                            blocked = self.completion_guard(mission_id, m) or {}
                        except Exception as exc:
                            blocked = {
                                "reason": "completion guard failed closed: %s: %s" %
                                          (type(exc).__name__, exc),
                                "seconds": 60,
                            }
                        if blocked:
                            blocked = blocked if isinstance(blocked, dict) else {
                                "reason": str(blocked)}
                            try:
                                seconds = int(blocked.get("seconds") or 60)
                            except (TypeError, ValueError):
                                seconds = 60
                            seconds = max(1, min(3600, seconds))
                            reason = str(blocked.get("reason") or
                                         "unfinished delegated work remains")[:500]
                            self.store.schedule_wait(
                                mission_id, int(time.time()) + seconds)
                            self.store.record_event(
                                mission_id, "agent", "completion_blocked",
                                payload={"reason": reason, "seconds": seconds})
                            return self._finish(mission_id, token, WAITING, reason)
                    blocking_auth = [
                        x for x in (m.case.get("pending_authorizations") or [])
                        if isinstance(x, dict) and x.get("blocking")]
                    if blocking_auth:
                        summary = str(blocking_auth[0].get("summary") or
                                      "authorization required")[:500]
                        self.store.record_event(
                            mission_id, "authorization", "all_remaining_blocked",
                            payload={"count": len(blocking_auth), "summary": summary})
                        return self._finish(
                            mission_id, token, NEEDS_YOU,
                            "%d authorization request(s) remain; next: %s" %
                            (len(blocking_auth), summary))
                    pending_followups = [
                        x for x in (m.case.get("pending_followups") or [])
                        if isinstance(x, dict)]
                    if pending_followups:
                        wake_at = min(int(x.get("due_at") or time.time() + 60)
                                      for x in pending_followups)
                        deadline = self._deadline_epoch(m.leash)
                        if deadline:
                            wake_at = min(wake_at, deadline)
                        self.store.schedule_wait(mission_id, wake_at)
                        self.store.record_event(
                            mission_id, "control", WAIT,
                            payload={"reason": "scheduled follow-ups remain",
                                     "seconds": max(0, wake_at - int(time.time())),
                                     "branches": len(pending_followups)})
                        return self._finish(
                            mission_id, token, WAITING,
                            "%d scheduled follow-up(s) remain" % len(pending_followups))
                    due_followups = [
                        x for x in (m.case.get("_due_followups") or [])
                        if isinstance(x, dict)]
                    if due_followups:
                        self.store.schedule_wait(mission_id, int(time.time()) + 1)
                        return self._finish(
                            mission_id, token, WAITING,
                            "driver reported done before checking %d due follow-up(s); "
                            "continuing automatically" %
                            len(due_followups))
                    return self._verify_and_finish_goal(
                        mission_id, token, m, reason, step_timeout)
                if action == NEEDS_HUMAN:
                    summary = str(args.get("summary") or reason or "")
                    planner_unavailable = (
                        str(reason).strip().lower() == "decider unavailable" and
                        summary.strip().lower() ==
                        "could not decide the next step automatically")
                    open_coverage = _open_campaign_coverage(m.case)
                    if planner_unavailable and open_coverage:
                        recent = [e for e in self.store.events(mission_id, 20)
                                  if e.get("kind") == "watchdog" and
                                  e.get("name") == "planner_unavailable"]
                        delay = min(900, 30 * (2 ** min(len(recent), 5)))
                        if not decision.get("_retry"):
                            self.store.account_runtime(
                                mission_id, token, retries=1)
                        self.store.schedule_wait(
                            mission_id, int(time.time()) + delay)
                        self.store.record_event(
                            mission_id, "watchdog", "planner_unavailable",
                            payload={
                                "retry_seconds": delay,
                                "open_coverage": len(open_coverage),
                                "branches": [str(x.get("branch") or "")
                                             for x in open_coverage[:6]],
                            })
                        return self._finish(
                            mission_id, token, WAITING,
                            "planner response unavailable; automatic retry scheduled")
                    self.store.record_step(mission_id, NEEDS_HUMAN, "", NEEDS_HUMAN)
                    self.store.record_event(mission_id, "control", NEEDS_HUMAN,
                                            payload={"summary": summary})
                    return self._finish(
                        mission_id, token, NEEDS_YOU,
                        summary or "needs your input")
                if action == NEEDS_AUTHORIZATION:
                    routed = self._handle_authorization(
                        mission_id, token, m, args, reason)
                    if routed == "_continue":
                        continue
                    return routed
                if action == WAIT:
                    routed = self._handle_wait(mission_id, token, m, args, reason)
                    if routed == "_continue":
                        continue
                    return routed

                cap = self._capability(action)
                if not cap:
                    return self._finish(mission_id, token, FAILED_S,
                                        f"unknown action {action!r}")
                # Snapshot the capability's reversibility once. A MissionService
                # can share registry-backed capability objects with concurrent
                # dispatchers; verdict routing must use the exact classification
                # that reserved this action, not a later mutable registry lookup.
                action_reversible = bool(cap.reversible)
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
                is_poll_read = cap.name in ("observe", "agent.poll")
                next_read_target = (self._observe_target(args) if cap.name == "observe" else
                                    ("agent", str(args.get("run_id") or "subtree"))
                                    if cap.name == "agent.poll" else None)
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
                        source_workspace = os.path.normcase(os.path.realpath(
                            str(m.case.get("_resource_source_workspace") or "")))
                        writable_roots = [
                            item.get("id") or item.get("path")
                            for item in specialist_scope if isinstance(item, dict) and
                            item.get("kind") == "file" and item.get("mode") == "write" and
                            os.path.isdir(str(item.get("id") or item.get("path") or ""))]
                        full_workspace_grant = False
                        for root in writable_roots:
                            try:
                                canonical = os.path.normcase(os.path.realpath(str(root)))
                                full_workspace_grant = bool(source_workspace) and (
                                    os.path.commonpath([canonical, source_workspace]) == canonical)
                            except (OSError, ValueError):
                                full_workspace_grant = False
                            if full_workspace_grant:
                                break
                        if not full_workspace_grant:
                            return self._finish(
                                mission_id, token, NEEDS_YOU,
                                "specialist code needs a directory write resource covering its "
                                "full source workspace; narrower/read-only scope cannot be "
                                "enforced by the isolated code child without expanding authority")
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
                    mission_id, action_key, not action_reversible, m.leash, cap.name,
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
                     "reversible": action_reversible,
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
                    if not self._consume_due_followup(
                            mission_id, token, cap.name, verdict.status, args):
                        return self._lost_state(mission_id, token)
                    self.store.record_checkpoint(
                        mission_id, token, "folded",
                        {"capability": cap.name, "nonce": nonce},
                        case=self.store.get(mission_id).case)
                elif not self._consume_due_followup(
                        mission_id, token, cap.name, verdict.status, args):
                    return self._lost_state(mission_id, token)
                # PAUSING may commit the completed result above, but must settle
                # before any verdict routing or next model/action boundary.
                if not self.store.owns_run(mission_id, token):
                    return self._lost_state(mission_id, token)
                if verdict.status in (FAILED, INCONCLUSIVE) and action_reversible:
                    # A reversible primitive that failed or could not be verified
                    # is actionable diagnostic evidence, not a reason to stop all
                    # independent Mission branches.  The planner may repair it or
                    # move on; submit preconditions still reject any consequential
                    # action whose newest preparation is not verified, and the
                    # cumulative retry/turn budgets stop pathological loops.
                    current = self.store.get(mission_id)
                    case = dict(current.case if current else {})
                    failures = list(case.get("_recent_failures") or [])
                    failures.append({"at": int(time.time()), "capability": cap.name,
                                     "verdict": verdict.status,
                                     "reason": str(verdict.reason or "")[:1000],
                                     "result": _compact_event(result, 2000)})
                    case["_recent_failures"] = failures[-8:]
                    if not self.store.set_case_owned(mission_id, token, case):
                        return self._lost_state(mission_id, token)
                    self.store.account_runtime(mission_id, token, retries=1)
                    self.store.record_checkpoint(
                        mission_id, token, "reversible_issue",
                        {"capability": cap.name, "verdict": verdict.status,
                         "reason": str(verdict.reason or "")[:500]},
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

            current = self.store.get(mission_id)
            if not current:
                return self._lost_state(mission_id, token)
            deadline = self._deadline_epoch(current.leash)
            now = int(time.time())
            if deadline and now >= deadline:
                return self._finish_at_deadline(
                    mission_id, token, current, step_timeout)
            coverage = _campaign_coverage(current.case)
            open_coverage = _open_campaign_coverage(current.case)
            if open_coverage:
                wake_at = now + 5
                if deadline:
                    wake_at = min(wake_at, deadline)
                self.store.schedule_wait(mission_id, wake_at)
                self.store.record_event(
                    mission_id, "coverage", "planning_slice_yielded",
                    payload={"open": len(open_coverage),
                             "branches": [str(x.get("branch") or "")
                                          for x in open_coverage[:6]]})
                return self._finish(
                    mission_id, token, WAITING,
                    "%d campaign branch(es) remain; continuing in the next planning slice" %
                    len(open_coverage))
            pending_followups = [
                x for x in (current.case.get("pending_followups") or [])
                if isinstance(x, dict)]
            due_followups = [
                x for x in (current.case.get("_due_followups") or [])
                if isinstance(x, dict)]
            if pending_followups or due_followups:
                wake_at = (now + 1 if due_followups else
                           min(int(x.get("due_at") or now + 60)
                               for x in pending_followups))
                if deadline:
                    wake_at = min(wake_at, deadline)
                self.store.schedule_wait(mission_id, wake_at)
                self.store.record_event(
                    mission_id, "control", WAIT,
                    payload={"reason": "scheduled follow-ups remain after planning slice",
                             "seconds": max(0, wake_at - now),
                             "branches": len(pending_followups) + len(due_followups)})
                return self._finish(
                    mission_id, token, WAITING,
                    "%d scheduled follow-up(s) remain" %
                    (len(pending_followups) + len(due_followups)))
            blocking_auth = [
                x for x in (current.case.get("pending_authorizations") or [])
                if isinstance(x, dict) and x.get("blocking")]
            if blocking_auth:
                return self._finish(
                    mission_id, token, NEEDS_YOU,
                    str(blocking_auth[0].get("summary") or
                        "blocking authorization required")[:500])
            if coverage:
                return self._verify_and_finish_goal(
                    mission_id, token, current,
                    "required campaign coverage reached terminal states", step_timeout)

            wake_at = now + 5
            if deadline:
                wake_at = min(wake_at, deadline)
            self.store.schedule_wait(mission_id, wake_at)
            self.store.record_event(
                mission_id, "control", "planning_slice_yielded",
                payload={"seconds": max(0, wake_at - now)})
            return self._finish(
                mission_id, token, WAITING,
                "planning slice exhausted; continuing automatically")
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
        # Confirmation authorizes this exact payload; it does not mint fresh
        # campaign budget. A sibling may have exhausted a shared ancestor while
        # this action was parked, so re-check the aggregate ledger at the final
        # pre-fire boundary.
        exhausted = self.store.budget_reason(mission_id)
        if exhausted:
            return self._finish(mission_id, token, NEEDS_YOU, exhausted)
        rec = self.actions.get(nonce)
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
            if not self._consume_due_followup(
                    mission_id, token, name, verdict.status,
                    rec.args if rec else {}):
                return self._lost_state(mission_id, token)
            self.store.record_checkpoint(
                mission_id, token, "folded", {"capability": name, "nonce": nonce},
                case=self.store.get(mission_id).case)
        elif not self._consume_due_followup(
                mission_id, token, name, verdict.status,
                rec.args if rec else {}):
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
    default_may = ["research", "compose", "observe", "agent.*", "web.*",
                   "browse", "browse.*", "verification.*"]
    known = {"spend_max_usd", "allowed_domains", "max_total_steps",
             "max_irreversible_actions", "actions_per_hour", "max_model_tokens",
             "max_model_cost_usd", "max_model_calls", "max_active_wall_seconds",
             "max_elapsed_seconds",
             "max_step_seconds", "max_retries", "max_storage_bytes", "checkpoint_keep",
             "human_escalate_seconds", "human_timeout_seconds", "workspace_mode",
             "max_specialists", "max_specialist_depth"}
    unknown = sorted(set(bounds) - known)
    if unknown:
        raise ValueError("unenforced Mission leash bound(s): " + ", ".join(unknown))
    for key in ("max_total_steps", "max_irreversible_actions", "actions_per_hour",
                "max_model_tokens", "max_model_calls", "max_active_wall_seconds",
                "max_elapsed_seconds",
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
             "max_model_calls": 1000,
             "max_active_wall_seconds": 21_600,
             "max_elapsed_seconds": 2_592_000,
             "max_step_seconds": 600,
             # Broad, long-running Missions routinely encounter recoverable site
             # and form drift across many branches. Keep retries bounded, but do
             # not size the campaign-wide envelope like a single short errand.
             "max_retries": 128,
             "max_storage_bytes": 5_000_000,
             "checkpoint_keep": 64,
             "human_escalate_seconds": 3_600,
             "human_timeout_seconds": 86_400,
             "max_specialists": 4,
             "max_specialist_depth": 2,
             "workspace_mode": "current"}
    if expires:
        # Leash evaluation compares canonical UTC timestamps lexically. API callers
        # commonly have an epoch deadline; accepting it without normalization stores
        # an int that crashes the first primitive-catalog evaluation (str > int).
        if isinstance(expires, (int, float)) and not isinstance(expires, bool):
            expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires))
        elif isinstance(expires, str):
            expires = expires.strip()
        else:
            raise ValueError("Mission leash expires must be an ISO timestamp or epoch seconds")
        if expires:
            leash["expires"] = expires
    leash.update(bounds)
    if "max_model_calls" not in bounds:
        leash["max_model_calls"] = int(leash.get("max_total_steps", 1000))
    return leash


# ── the model decider: NL goal + case + primitives -> the next action ───────
_SYS = (
    "You are collie's mission driver. You are pursuing ONE goal over time. Given the "
    "goal, what you already know (the case), and the actions available, choose the "
    "SINGLE next action. Reply with STRICT JSON and nothing else:\n"
    '{"action": <a primitive name | "wait" | "update_coverage" | "needs_authorization" | "needs_human" | "done">, '
    '"args": {..}, "reason": "<one short clause>"}\n'
    "Rules: use only a listed primitive. CASE.human_updates are durable user/operator "
    "steering in chronological order: the newest explicit instruction overrides conflicting "
    "older GOAL wording, but never expands the Leash or bypasses a security boundary. If a newer "
    "update authorizes ordinary account creation, being signed out by itself is not a terminal "
    "blocker: attempt the normal signup/sign-in path before recording the exact remaining blocker. "
    "To let only one workstream wait, pick 'wait' "
    "with args.seconds plus a stable args.branch name; Collie schedules that branch and "
    "continues other independent work. Immediately repeat that same wait only when no "
    "independent work remains. Set args.blocking=true only for a whole-Mission wait. "
    "When CASE contains _campaign_coverage, it is a REQUIRED breadth contract. Work every "
    "required pending/active/attempted branch before asking the whole Mission to wait or finish. "
    "Unless a due follow-up or a concrete prerequisite requires otherwise, choose the first "
    "required open branch in that list so the durable priority order is honored. "
    "Include args.campaign_branch with the exact branch name on research/compose/browse actions. "
    "After a branch has a concrete outcome, choose update_coverage with args.branch, args.status "
    "(completed, blocked, exhausted, scheduled, deferred, skipped, pending, active, or attempted), "
    "and a concise args.summary explaining the evidence. blocked means the current route failed "
    "but the branch REMAINS OPEN: try a distinct compliant route, alternate sign-in/account setup, "
    "another suitable venue, a different reversible tool, or schedule a retry. Include "
    "args.blocker_kind and args.alternatives_tried. Use exhausted only when compliant alternatives "
    "are genuinely exhausted: the host requires blocker_kind plus auditable alternatives_tried. "
    "Never treat CAPTCHA bypass, impersonation, deceptive account creation, policy evasion, or "
    "duplicate posting as a workaround. scheduled is valid only after a matching named "
    "wait was durably created. The host refuses wait/done while required coverage remains open. "
    "When CASE contains _due_followups, perform the oldest due check before unrelated "
    "work and copy its branch exactly into that action's args.followup_branch; do not "
    "report done until it has been checked. Use 'needs_authorization' for "
    "a missing permission/fact with "
    "args.summary, args.kind, args.risk (low/medium/high/critical), optional args.claim, "
    "args.domain and args.operation. It is branch-scoped: default args.blocking=false so "
    "the request enters Needs You while you continue independent work; set blocking=true "
    "only after every remaining path depends on it. Use 'needs_human' only for a genuinely "
    "person-required step or when no independent path remains. 'done' only when the goal is "
    "actually achieved. Irreversible actions "
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
    "A reversible failed or inconclusive action is recorded in CASE._recent_failures and does not "
    "stop unrelated branches. Repair it once when useful, then pursue independent work instead of "
    "repeating the same unavailable observation.\n"
    "For 'compose', put the writing request in args.instruction and supporting material in "
    "args.facts. Use args.text ONLY when it already contains the complete, final, ready-to-use "
    "copy. Never put an instruction such as 'write/create/draft a post' in args.text.\n"
    "Use credentials, email/phone identities, signed-in sessions, and verification-code inboxes "
    "that the user has already connected and authorized; routine signup fields, OTP retrieval, "
    "Next buttons, and authorized publish/send actions are ordinary work inside the leash. "
    "A public brand username/handle is non-secret profile data. When ordinary account setup needs "
    "one and no preference is saved, prefer the product/companion brand from GOAL/CASE; never "
    "invent a person's identity or use an inferred personal name. "
    "A phone explicitly connected as Collie's assigned work line may be used for messages, calls, "
    "voicemail, and routine provider settings within the Mission goal and Leash; releasing or "
    "transferring the number, purchases, and Google-account security changes require new authority. "
    "For an already-requested code from a connected inbox, choose 'verification.fill' with the "
    "exact service name; it reads and fills the code internally without exposing it to you. "
    "Never persist a credential or OTP in the case, event log, action args, or summary. The case may contain "
    "_standing_authority: exact user-confirmed facts there are reusable up to the stated risk ceiling; "
    "perform the matching ordinary browser work instead of asking again. If the fact or permission is "
    "missing, choose 'needs_authorization' and continue another branch. A CAPTCHA or MFA challenge "
    "that explicitly requires a person, biometric/KYC, legal signature, security key, or spending "
    "boundary stays 'needs_human'. Preserve the current step so the Mission can continue after the "
    "user handles it. Never attempt to bypass, outsource, or misrepresent a platform security check.\n"
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
