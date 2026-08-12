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
import fnmatch
import hashlib
import math
import os
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

from .actions import (APPROVED, EXECUTED, EXECUTING, EXPIRED, PENDING, REFUSED,
                      ActionStore, RefusedError)
from .jobs import (CANCELLED, DONE_ACCEPTED, DONE_VERIFIED, FAILED_S, NEEDS_YOU,
                   PAUSED, PAUSING, QUEUED, RECONCILING, RECOVERY_REQUIRED,
                   RUNNING, WAITING, Capability)
from .mission import (_campaign_coverage, _compact_case_storage,
                      _open_campaign_coverage,
                      _resolved_authorization, MissionDriver, MissionStore,
                      ModelDecider, ResourceBusy, create_mission, world_leash)
from .primitives import register_primitives
from .verifier import (MissionGoalVerifier, FAILED as VERIFY_FAILED,
                       VERIFIED as VERIFY_VERIFIED, Verdict)


def _hook_manager(cwd: str, state_dir: str):
    """Construct HookManager against this service's state without mutating process env."""
    from .hooks import HookManager
    wanted = os.path.abspath(os.path.expanduser(state_dir))
    return HookManager(cwd, state_dir=wanted)


def _provider_name() -> str:
    # mirrors webapp._provider(): the Settings-panel provider is applied into the
    # env before a request, so this is the same provider the chat GUI runs on.
    return os.environ.get("COLLIE_PROVIDER", "")


_SUBSCRIPTION_PROVIDERS = {
    "claude-agent-sdk": "claude-agent-sdk",
    "claude-sdk": "claude-agent-sdk",
    "anthropic-oauth": "anthropic-oauth",
    "claude-sub": "anthropic-oauth",
    "claude-cli": "claude-cli",
    "cli": "claude-cli",
    "codex-oauth": "codex-oauth",
    "codex-sub": "codex-oauth",
    "codex": "codex-oauth",
}

_LOCAL_PROVIDERS = {"mock", "ollama"}

# Overnight is deliberately narrower than the general provider catalog.  The
# admitted route is Anthropic's official Claude Agent SDK with a literal Collie
# system prompt and every SDK tool/config/plugin surface disabled. Collie still
# owns the loop, tools, durable leash, and verification; the worker owns one
# model inference. Raw OAuth and metered/API/CLI fallback are never admitted.
_OVERNIGHT_SUBSCRIPTION_PROVIDERS = {"claude-agent-sdk"}

# The ordinary Mission defaults remain deliberately broad and long-lived.  This
# preset is different: it is a bounded, unattended execution window whose whole
# authority is frozen into the Mission row before the daemon can claim it.
_OVERNIGHT_BOUNDS = {
    "max_total_steps": 4_000,
    "max_model_tokens": 8_000_000,
    "max_model_calls": 4_000,
    # This leash is marginal charge, not equivalent list-price.  Subscription
    # calls account $0 here and retain their equivalent value separately; a
    # routing regression that starts recording metered spend trips almost
    # immediately instead of silently running all night.
    "max_model_cost_usd": 0.01,
    "max_active_wall_seconds": 43_200,
    # Twelve hours means active work, not "twelve hours since the laptop was
    # closed".  Keep a seven-day catch-up window so sleep/reboot time does not
    # consume the useful runtime budget.
    "max_elapsed_seconds": 7 * 24 * 60 * 60,
    # A code slice is internally capped at three model turns plus one host
    # verification command.  This outer watchdog leaves enough room for the
    # provider's own bounded retry while still fencing a genuinely stuck tree.
    "max_step_seconds": 1_800,
    "max_retries": 512,
    "max_storage_bytes": 20_000_000,
    "checkpoint_keep": 256,
    "max_specialists": 8,
}

def _subscription_guard_environment() -> dict:
    """Return the real parent environment so routing/TLS overrides cannot hide.

    The guard itself gives auth-status subprocesses a small allowlist. Filtering
    here used to make every forbidden-variable check vacuous while direct urllib
    inference still inherited ambient TLS trust configuration.
    """
    return dict(os.environ)


def _canonical_provider(name: str) -> str:
    name = str(name or "").strip().lower()
    return _SUBSCRIPTION_PROVIDERS.get(name, name)


def _billing_mode(provider: str) -> str:
    provider = _canonical_provider(provider)
    if provider in set(_SUBSCRIPTION_PROVIDERS.values()):
        return "subscription"
    if provider in _LOCAL_PROVIDERS:
        return "local"
    return "metered" if provider else "unconfigured"


def _execution_profile(provider: str, model: str | None, *, overnight: bool) -> dict:
    """Return the non-secret provider/model route frozen into a durable Mission."""
    provider = _canonical_provider(provider)
    from .providers import provider_default_model
    model = str(model or provider_default_model(provider) or "").strip()
    billing = _billing_mode(provider)
    if not provider:
        raise ValueError("durable Mission requires a configured model provider")
    if not model:
        raise ValueError("durable Mission requires an explicit, frozen model")
    if overnight and (billing != "subscription" or
                      provider not in _OVERNIGHT_SUBSCRIPTION_PROVIDERS):
        raise ValueError(
            "overnight Mission requires the official Claude Agent SDK "
            "(claude-agent-sdk) with Collie's system prompt; raw OAuth, claude -p, "
            "metered, and provider fallback routes are forbidden")
    if overnight and not model.lower().startswith("claude-opus-"):
        raise ValueError(
            "native overnight Mission requires an explicit Claude Opus model; "
            "Sonnet/Haiku and rolling aliases are not silently substituted")
    return {
        "version": 1,
        "profile": "overnight" if overnight else "durable-code",
        "provider": provider,
        "model": model,
        "billing_mode": billing,
        "subscription_only": billing == "subscription",
        "allow_provider_fallback": False,
    }


def _execution_profile_digest(profile: dict) -> str:
    """Canonical immutable pin stored in the Mission leash, outside mutable case state."""
    encoded = json.dumps(
        dict(profile or {}), ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clean(d: dict) -> dict:
    """Drop the injected `_case` context from args/case before it hits the UI."""
    return {k: v for k, v in (d or {}).items() if k not in ("_case", "_leash")}


def _short(value, limit=500):
    value = " ".join(str(value or "").split())
    return value[:limit]


def _mission_summary(mission, steps, receipts, runtime, inbox, next_wait, activity=None):
    """Build a bounded, deterministic operator view without another model call."""
    case = mission.case or {}
    resolved_auth = [x for x in case.get("resolved_authorizations", [])
                     if isinstance(x, dict)]
    pending_auth = [x for x in case.get("pending_authorizations", [])
                    if isinstance(x, dict) and
                    not _resolved_authorization(x, resolved_auth)][-8:]
    coverage = _campaign_coverage(case)
    open_coverage = _open_campaign_coverage(case)
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
    pending = len(pending_auth) + len(open_coverage) + (1 if inbox else 0)
    phase = _short(runtime.get("active_phase"), 160)
    if mission.state in (RUNNING, PAUSING, RECONCILING) and open_coverage:
        current = "Working on campaign branch: %s" % _short(
            open_coverage[0].get("branch"), 180)
    elif mission.state in (RUNNING, PAUSING, RECONCILING):
        current = phase or _short(mission.result, 500)
    else:
        current = _short(mission.result, 500) or phase
    if inbox:
        next_step = "Confirm the prepared %s action" % _short(inbox.get("capability"), 100)
    elif open_coverage:
        next_step = "Continue campaign branch: %s" % _short(
            open_coverage[0].get("branch"), 180)
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
        blocker = (_short(mission.result, 500) or
                   (_short(pending_auth[0].get("summary"), 500) if pending_auth else "") or
                   "A person-required step")
    return {
        "title": _short(mission.goal, 300),
        "current": current or "Ready",
        "completed": completed[-8:],
        "next": next_step,
        "blocker": blocker,
        "authorization_waiting": len(pending_auth),
        "coverage": {
            "total": len(coverage),
            "completed": sum(1 for x in coverage if x.get("status") == "completed"),
            "closed": sum(1 for x in coverage if x.get("status") in
                          ("completed", "exhausted", "scheduled", "deferred", "skipped")),
            "open": len(open_coverage),
            "next": [_short(x.get("branch"), 120) for x in open_coverage[:5]],
        },
        "progress": {"verified": len(completed) + verified_receipts,
                     "pending": pending, "failed": failed},
    }


def _mission_report(mission, summary, activity, receipts, runtime):
    """Return a stable, redacted progress feed suitable for UI and integrations.

    The report deliberately derives from coverage, the compact activity ledger,
    receipts, and runtime counters.  It never exports raw Mission case, model
    messages, browser args, credentials, or checkpoint payloads.
    """
    case = mission.case or {}
    coverage = _campaign_coverage(case)
    resolved_auth = [x for x in case.get("resolved_authorizations", [])
                     if isinstance(x, dict)]
    pending_auth = [x for x in case.get("pending_authorizations", [])
                    if isinstance(x, dict) and
                    not _resolved_authorization(x, resolved_auth)][-8:]
    terminal = {"completed", "exhausted", "scheduled", "deferred", "skipped"}
    branches = []
    for row in coverage:
        branches.append({
            "branch": _short(row.get("branch"), 160),
            "status": _short(row.get("status") or "pending", 40),
            "summary": _short(row.get("summary"), 500),
            "blocker_kind": _short(row.get("blocker_kind"), 80),
            "updated_at": int(row.get("updated_at") or 0),
        })
    log = [{
        "at": int(item.get("at") or 0),
        "kind": _short(item.get("kind"), 60),
        "capability": _short(item.get("capability"), 120),
        "status": _short(item.get("status") or "recorded", 80),
        "summary": _short(item.get("summary"), 500),
        "protected_from_repeat": bool(item.get("do_not_repeat")),
    } for item in (activity or [])[-24:]]
    receipt_counts = {
        "total": len(receipts),
        "verified": sum(1 for x in receipts if x.get("verdict") == "verified"),
        "failed": sum(1 for x in receipts if x.get("verdict") == "failed"),
        "uncertain": sum(1 for x in receipts if x.get("verdict") == "inconclusive"),
        "execution_attempted": sum(1 for x in receipts if x.get("fired")),
    }
    completed = sum(1 for x in coverage if x.get("status") == "completed")
    closed = sum(1 for x in coverage if x.get("status") in terminal)
    revision = int(runtime.get("progress_seq") or 0)
    updated_at = max(
        int(mission.updated_at or 0), int(runtime.get("progress_at") or 0),
        max((x["at"] for x in log), default=0))
    needs_you = [{
        "domain": _short(x.get("domain"), 160),
        "summary": _short(x.get("summary") or x.get("operation") or x.get("kind"), 500),
        "blocking": bool(x.get("blocking")),
    } for x in pending_auth]
    report = {
        "format_version": 1,
        "mission_id": mission.mission_id,
        "title": summary.get("title") or _short(mission.goal, 300),
        "state": mission.state,
        "revision": revision,
        "updated_at": updated_at,
        "deadline": _short((mission.leash or {}).get("expires"), 80),
        "current": summary.get("current") or "Ready",
        "next": summary.get("next") or "Review the Mission state",
        "blocker": summary.get("blocker") or "",
        "coverage": {
            "total": len(coverage), "completed": completed,
            "closed": closed, "open": len(coverage) - closed,
            "branches": branches,
        },
        "receipts": receipt_counts,
        "needs_you": needs_you,
        "log": log,
        "runtime": {
            "model_calls": int(runtime.get("model_calls") or 0),
            "turns": int(runtime.get("turns") or 0),
            "retry_count": int(runtime.get("retry_count") or 0),
            "model_cost_usd": float(runtime.get("model_cost_usd") or 0.0),
            "equivalent_model_cost_usd": float(
                runtime.get("equivalent_model_cost_usd") or 0.0),
        },
    }
    lines = [
        "# Mission progress: %s" % report["title"],
        "",
        "- Mission: `%s`" % mission.mission_id,
        "- State: %s" % mission.state,
        "- Updated: %s" % updated_at,
        "- Coverage: %d completed, %d closed, %d open, %d total" %
        (completed, closed, len(coverage) - closed, len(coverage)),
        "- Verified receipts: %d of %d" %
        (receipt_counts["verified"], receipt_counts["total"]),
        "- Model charge: $%.4f marginal ($%.4f API-equivalent)" %
        (report["runtime"]["model_cost_usd"],
         report["runtime"]["equivalent_model_cost_usd"]),
        "",
        "## Current",
        report["current"],
        "",
        "## Next",
        report["next"],
    ]
    if report["blocker"]:
        lines.extend(["", "## Blocking issue", report["blocker"]])
    if branches:
        lines.extend(["", "## Channel coverage"])
        for item in branches:
            detail = (" — " + item["summary"]) if item["summary"] else ""
            lines.append("- [%s] %s%s" %
                         (item["status"], item["branch"] or "Unnamed branch", detail))
    if needs_you:
        lines.extend(["", "## Needs you"])
        for item in needs_you:
            scope = (item["domain"] + ": ") if item["domain"] else ""
            suffix = " (blocking)" if item["blocking"] else " (non-blocking)"
            lines.append("- %s%s%s" % (scope, item["summary"], suffix))
    if log:
        lines.extend(["", "## Recent activity"])
        for item in log[-12:]:
            lines.append("- [%s] %s" % (item["status"], item["summary"] or
                         item["capability"] or "Activity recorded"))
    report["markdown"] = "\n".join(lines)[:32000]
    return report


class MissionService:
    def __init__(self, base: str = None, decider=None, provider: str = None,
                 model: str = None, stub=None, state_dir: str = None,
                 goal_verifier=None, mission_workers: int = None, run_tree=None,
                 hooks=None, specialist_workers: int = None,
                 subscription_guard=None):
        # base isolates tests; production uses the shared ~/.collie stores (so a
        # mission is visible to `collie jobs` / jobsweb too).
        # A custom ``base`` is primarily the deterministic test/embedding seam;
        # keep every implicit store beside it unless the caller explicitly names
        # a shared state directory.  Production does not pass ``base`` and all
        # three durable databases therefore live under the same state directory.
        state_dir = state_dir or os.environ.get("COLLIE_STATE_DIR") or \
            (os.path.dirname(os.path.abspath(base)) if base else
             os.path.expanduser("~/.collie"))
        self._state_dir = os.path.realpath(os.path.abspath(os.path.expanduser(state_dir)))
        mission_path = (base + ".missions") if base else os.path.join(state_dir, "jobs.db")
        action_path = (base + ".actions") if base else os.path.join(state_dir, "actions.db")
        self.store = MissionStore(mission_path)
        self.actions = ActionStore(action_path)
        self._decider = decider
        self._provider = provider or _provider_name()
        self._model = model or os.environ.get("COLLIE_MODEL") or None
        self._stub = stub
        self._goal_verifier = (goal_verifier if goal_verifier is not None else
                               MissionGoalVerifier(self.store, self.actions))
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
        if subscription_guard is None:
            from .subscription_guard import check_subscription_guard
            subscription_guard = check_subscription_guard
        self._subscription_guard = subscription_guard
        if self._run_tree is not None and self._hooks is not None and \
                getattr(self._run_tree, "hooks", None) is None:
            self._run_tree.hooks = self._hooks
        self._prov = None
        self._subscription_only = False
        self._runtime_ready = False
        self._capabilities = None
        self._code_process = None
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
            self._prov = make_provider(
                self._provider, self._model,
                subscription_only=self._subscription_only)
            if self._subscription_only:
                # Providers interpret this as a hard route property, not just
                # cost accounting.  The direct Claude OAuth provider binds the
                # official endpoint and disables ambient proxies; no CLI/API-key
                # fallback is permitted.
                self._prov.subscription_only = True
        if stub:
            self._capabilities = register_primitives(stub=True)
        else:
            if self._prov is None and self._provider:
                from .providers import make_provider
                self._prov = make_provider(
                    self._provider, self._model,
                    subscription_only=self._subscription_only)
                if self._subscription_only:
                    self._prov.subscription_only = True
            from .webact import get_actuator
            self._capabilities = register_primitives(
                stub=False, actuator=get_actuator(), provider=self._prov)
        # These capabilities are service-bound rather than globally registered:
        # their executor must address this exact durable TaskTree/MissionStore.
        self._capabilities = self._tasktree_guarded_capabilities(
            self._capabilities) + self._agent_capabilities()
        if not stub:
            # Native Mission code always crosses a killable process boundary.
            # The generic Mission watchdog can therefore terminate the whole
            # child tree instead of fencing an unkillable Python thread that may
            # continue editing after ownership has moved on.
            from .codeworker import CodeSliceProcessRunner
            from .primitives import _code_verify, _real_code
            code_process = CodeSliceProcessRunner(
                session_dir=os.path.join(self._state_dir, "mission-code-sessions"))
            self._code_process = code_process
            wrapped = []
            for cap in self._capabilities:
                if cap.name != "code":
                    wrapped.append(cap)
                    continue
                cap = replace(cap, execute=_real_code(code_process), verify=_code_verify)
                # MissionDriver's watchdog asks the Capability itself for a
                # cancellation hook.  Capability is intentionally an open
                # dataclass, so attach the process-tree terminator here.
                cap.cancel_current = code_process.cancel_current
                cap.cancel_for = code_process.cancel_for
                wrapped.append(cap)
            self._capabilities = wrapped
        self._runtime_ready = True

    def _subscription_preflight(self, profile, billing_safety=None,
                                require_live_probe=True) -> dict:
        """Prove the frozen official subscription route before unattended work.

        Auth status alone cannot expose the account-side paid-overage toggle.
        Therefore the user must explicitly attest that it is disabled; Collie
        then removes every API/proxy override from the effective child
        environment, asks the official client for redacted login status, and
        at creation separately proves that the official runtime accepts Opus
        with Collie's replacement prompt. Runnable-boundary checks are local and
        quota-free; every actual inference still needs durable request authority.
        The returned receipt contains no credential material and is safe to
        persist with the Mission.
        """
        profile = dict(profile or {})
        safety = dict(billing_safety or {})
        if not safety.get("paid_overage_disabled"):
            raise RuntimeError(
                "overnight subscription mode requires an explicit confirmation "
                "that paid usage credits/overage and auto-reload are disabled")
        provider = _canonical_provider(profile.get("provider"))
        if provider == "claude-agent-sdk":
            guard_provider = "claude-agent-sdk"
            account_evidence = None
        elif provider == "codex-oauth":
            guard_provider = "codex-cli"
            raw = safety.get("account_evidence")
            if not isinstance(raw, dict):
                raise RuntimeError(
                    "Codex overnight mode requires fresh zero-credit/auto-reload-off "
                    "account evidence")
            account_evidence = {
                key: raw.get(key) for key in
                ("credits_remaining", "auto_reload", "observed_at_utc")
            }
        else:
            raise RuntimeError("overnight subscription provider is unsupported")
        try:
            receipt = self._subscription_guard(
                guard_provider, account_evidence=account_evidence,
                environ=_subscription_guard_environment(),
                model=str(profile.get("model") or ""),
                require_direct_probe=bool(require_live_probe))
        except Exception as exc:
            reason = str(getattr(exc, "reason", "") or type(exc).__name__)
            raise RuntimeError("subscription preflight denied: %s" % reason) from exc
        if not isinstance(receipt, dict) or receipt.get("verdict") != "allow":
            raise RuntimeError("subscription preflight returned no allow receipt")
        if require_live_probe:
            # This receipt includes the one live inference that established the
            # SDK init/auth attestation for this Mission.  A specialist is a new
            # Mission and therefore gets its own creation receipt.
            creation_receipt = receipt
        else:
            # Runnable-boundary checks intentionally do not spend another
            # inference.  Preserve the original observed attestation instead
            # of overwriting it with a no-probe status receipt.  ``guard_receipt``
            # is the legacy fallback for Missions created before this field was
            # introduced.
            try:
                safety_version = int(safety.get("version", 1) or 1)
            except (TypeError, ValueError, OverflowError):
                raise RuntimeError("subscription billing safety version is invalid")
            creation_receipt = safety.get("creation_guard_receipt")
            if safety_version < 2 and creation_receipt is None:
                creation_receipt = safety.get("guard_receipt")
            if (not isinstance(creation_receipt, dict) or
                    creation_receipt.get("verdict") != "allow"):
                raise RuntimeError(
                    "subscription preflight has no preserved creation allow receipt")
        return {
            "version": 2,
            "paid_overage_disabled": True,
            "marginal_charge_policy": "subscription-allowance-or-stop",
            "account_evidence": account_evidence,
            "creation_guard_receipt": json.loads(json.dumps(
                creation_receipt, ensure_ascii=False)),
            "guard_receipt": receipt,
        }

    def _activate_execution_profile(self, mission) -> None:
        """Select a frozen Mission route before constructing its model runtime.

        A settings change while Collie is asleep must not silently move an
        unattended run from a login-backed route to a metered API. The profile
        contains no credential; providers still read credentials from their
        normal secure source at execution time.
        """
        profile = (mission.case or {}).get("execution_profile") if mission else None
        if not isinstance(profile, dict):
            return
        provider = _canonical_provider(profile.get("provider"))
        model = str(profile.get("model") or "").strip()
        profile_kind = str(profile.get("profile") or "")
        subscription_only = bool(profile.get("subscription_only"))
        billing = _billing_mode(provider)
        if profile_kind not in ("durable-code", "overnight"):
            raise RuntimeError("frozen Mission execution profile kind is invalid")
        try:
            canonical = _execution_profile(
                provider, model, overnight=(profile_kind == "overnight"))
        except ValueError as exc:
            raise RuntimeError(
                "frozen Mission execution profile is invalid: %s" % exc) from exc
        if profile != canonical:
            raise RuntimeError("frozen Mission execution profile is not canonical")
        pinned = str((mission.leash or {}).get("execution_profile_sha256") or "")
        if profile_kind == "overnight" and not pinned:
            raise RuntimeError("frozen overnight Mission has no immutable route pin")
        if pinned and pinned != _execution_profile_digest(profile):
            raise RuntimeError(
                "frozen Mission execution profile failed its immutable route pin")
        code_profile = (mission.case or {}).get("code_profile")
        if isinstance(code_profile, dict) and \
                bool(code_profile.get("overnight")) != (profile_kind == "overnight"):
            raise RuntimeError("frozen Mission code and execution profiles disagree")
        if profile.get("allow_provider_fallback") is not False:
            raise RuntimeError("frozen Mission execution profile permits provider fallback")
        if str(profile.get("billing_mode") or "") != billing:
            raise RuntimeError("frozen Mission billing route does not match its provider")
        if subscription_only and billing != "subscription":
            raise RuntimeError("frozen Mission subscription route is invalid")
        if billing == "subscription" and not subscription_only:
            raise RuntimeError("frozen subscription Mission permits metered fallback")
        if not provider:
            raise RuntimeError("frozen Mission has no model provider")
        if profile_kind == "overnight":
            if provider not in _OVERNIGHT_SUBSCRIPTION_PROVIDERS:
                raise RuntimeError(
                    "frozen overnight Mission no longer names the admitted official route")
            # Re-run the official-client auth check at every runnable boundary.
            # A durable receipt from creation is audit evidence, not permission
            # to keep running after the user logs out or changes billing state.
            refreshed = self._subscription_preflight(
                profile, (mission.case or {}).get("billing_safety"),
                require_live_probe=False)
            if not self.store.patch_case(
                    mission.mission_id, {"billing_safety": refreshed},
                    allowed_states=(QUEUED, WAITING, NEEDS_YOU)):
                raise RuntimeError("Mission left its runnable boundary during route activation")
            mission = self.store.get(mission.mission_id) or mission
        if self._runtime_ready:
            from .providers import provider_default_model
            active_provider = _canonical_provider(self._provider)
            active = (active_provider, str(
                self._model or provider_default_model(active_provider) or "").strip(),
                bool(self._subscription_only))
            wanted = (provider, model, subscription_only)
            if active != wanted:
                # MissionService is long-lived in colliejobd.  Rebuild only at
                # this between-run boundary; never keep using the daemon's prior
                # provider merely because it already initialized capabilities.
                self._prov = None
                self._capabilities = None
                self._runtime_ready = False
        self._provider = provider
        self._model = model or None
        self._subscription_only = subscription_only

    def _driver(self, *, lane="mission", control=None) -> MissionDriver:
        self._ensure_runtime()
        dec = self._decider or ModelDecider(self._prov)
        if control is None:
            control = self._mission_control
        return MissionDriver(self.store, self.actions, dec,
                             capabilities=self._capabilities,
                             goal_verifier=self._goal_verifier, lane=lane,
                             control=control, hooks=self._hooks,
                             completion_guard=self._agent_completion_guard)

    def _specialist_run(self, mid):
        runtime = self.store.runtime(mid)
        run_id = runtime.get("external_run_id") if runtime.get("lane") == "specialist" else ""
        return self._run_tree.get(run_id) if self._run_tree is not None and run_id else None

    def _mission_run_id(self, mission):
        """Resolve the TaskTree identity for either a root or specialist Mission."""
        if not mission or self._run_tree is None:
            return ""
        runtime = self.store.runtime(mission.mission_id)
        if runtime.get("lane") == "specialist":
            return str(runtime.get("external_run_id") or
                       (mission.case or {}).get("_specialist_run_id") or "")
        return str((mission.case or {}).get("_run_id") or "")

    def _project_mission_usage(self, mid, run_id=""):
        """Project one Mission's absolute *own* runtime into TaskTree."""
        if self._run_tree is None:
            return []
        mission = self.store.get(mid)
        if not mission:
            return []
        run_id = str(run_id or self._mission_run_id(mission) or "")
        if not run_id:
            return []
        runtime = self.store.runtime(mid)
        return self._run_tree.project_mission_usage(
            run_id, mid,
            input_tokens=runtime.get("input_tokens", 0),
            output_tokens=runtime.get("output_tokens", 0),
            cache_tokens=runtime.get("cache_tokens", 0),
            model_calls=runtime.get("model_calls", 0),
            turns=runtime.get("turns", 0),
            model_cost_microusd=runtime.get("model_cost_microusd", 0),
            wall_ms=runtime.get("active_wall_ms", 0),
            retries=runtime.get("retry_count", 0))

    def _reconcile_tasktree_usage(self, mid=None, limit=None):
        """Catch up cross-database usage gaps without double charging descendants."""
        if self._run_tree is None:
            return {"projected": 0, "errors": [], "exhausted": []}
        if mid:
            mission = self.store.get(mid)
            run_id = self._mission_run_id(mission)
            rows = self._run_tree.tree(run_id).get("flat", []) if run_id else []
        else:
            rows = self._run_tree.usage_reconciliation_runs()
        projected, errors, exhausted, seen = 0, [], [], set()
        candidates = rows if limit is None else rows[:max(1, int(limit))]
        for run in candidates:
            run_id = str(run.get("run_id") or "")
            mission_id = str(run.get("mission_id") or "")
            if not run_id or not mission_id or run_id in seen:
                continue
            seen.add(run_id)
            try:
                exhausted.extend(self._project_mission_usage(mission_id, run_id))
                projected += 1
            except (ValueError, sqlite3.Error) as exc:
                errors.append({"run_id": run_id, "mission_id": mission_id,
                               "error": "%s: %s" % (type(exc).__name__, exc)})
        return {"projected": projected, "errors": errors, "exhausted": exhausted}

    def _tasktree_guarded_capabilities(self, capabilities):
        """Bind code workspace ownership to this service's durable run tree.

        TaskTree resources are scheduling/delegation authority.  They are not a
        generic sandbox for research, browser, messaging, or other capabilities;
        those tools keep their own leash and capability-specific containment.
        Code is the one current primitive whose entire writable workspace can be
        checked here, before the action latch or code runner is entered.
        """
        guarded = []
        for capability in capabilities:
            if capability.name != "code":
                guarded.append(capability)
                continue
            resource = capability.resource

            def guarded_resource(record, original=resource):
                self._assert_tasktree_code_access(record)
                return original(record) if callable(original) else original

            guarded.append(replace(capability, resource=guarded_resource))
        return guarded

    def _assert_tasktree_code_access(self, record):
        """Fail closed unless the bound run still owns its source workspace."""
        if self._run_tree is None:
            raise RefusedError("code authority denied: durable run tree is unavailable")
        mission = self.store.get(record.job_id)
        if not mission:
            raise RefusedError("code authority denied: Mission is missing")
        run_id = self._mission_run_id(mission)
        if not run_id:
            # A code-only Mission may not have used agent.spawn yet. Attach the
            # same least-authority root so every MissionService code action has a
            # caller identity for can_access(), not an unguarded legacy path.
            run_id = self._ensure_agent_root(mission)
            mission = self.store.get(record.job_id) or mission
        run = self._run_tree.get(run_id) if run_id else None
        if not run or run.get("mission_id") != record.job_id:
            raise RefusedError("code authority denied: Mission has no bound run")
        if (run.get("status") in ("completed", "failed", "cancelled", "cancel_requested") or
                run.get("cancel_requested")):
            raise RefusedError("code authority denied: bound run is stopping or terminal")
        if run.get("parent_run_id") and (
                run.get("status") != "running" or not run.get("owner_token")):
            raise RefusedError("code authority denied: specialist run is not the active owner")

        case = mission.case or {}
        if run.get("parent_run_id"):
            parent = self._run_tree.get(run.get("parent_run_id") or "") or {}
            source_workspace = (case.get("_resource_source_workspace") or
                                parent.get("workspace") or "")
        else:
            source_workspace = run.get("workspace") or case.get("_isolated_workspace") or ""
        if not source_workspace:
            raise RefusedError("code authority denied: source workspace is not bound")
        allowed, reason = self._run_tree.can_access(
            run_id, {"kind": "file", "id": source_workspace}, "write")
        if allowed:
            return
        if str(reason).startswith("write ownership delegated to "):
            raise ResourceBusy("delegated code workspace busy: %s" % reason)
        raise RefusedError("code authority denied: %s" % reason)

    def _agent_caller(self, mid):
        mission = self.store.get(mid)
        if not mission:
            return None, None, "unknown mission"
        if mission.state in (DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
            return mission, None, "terminal Mission cannot control specialists"
        run_id = self._mission_run_id(mission)
        if self._run_tree is not None and not run_id:
            run_id = self._ensure_agent_root(mission)
            mission = self.store.get(mid) or mission
        run = self._run_tree.get(run_id) if self._run_tree is not None and run_id else None
        if not run or run.get("mission_id") != mid:
            return mission, None, "mission has no bound durable run tree"
        if run.get("status") in ("completed", "failed", "cancelled", "cancel_requested") or \
                run.get("cancel_requested"):
            return mission, None, "calling run is stopping or terminal"
        return mission, run, ""

    @staticmethod
    def _agent_root_id(mission_id):
        return "run_root_" + hashlib.sha256(
            str(mission_id).encode("utf-8", "replace")).hexdigest()[:16]

    def _ensure_agent_root(self, mission):
        """Lazily attach the least authority justified by a bound Mission workspace."""
        if not mission or self._run_tree is None:
            return ""
        case = dict(mission.case or {})
        current = str(case.get("_run_id") or "")
        if current and self._run_tree.get(current):
            return current
        run_id = self._agent_root_id(mission.mission_id)
        bound_workspace = str(case.get("_isolated_workspace") or "")
        if bound_workspace:
            bound_workspace = os.path.realpath(os.path.abspath(bound_workspace))
        is_bound = bool(bound_workspace and os.path.isdir(bound_workspace))
        workspace = bound_workspace if is_bound else ""
        workspace_mode = self._workspace_authority_mode(mission)
        resources = ([{"kind": "file", "id": workspace, "mode": workspace_mode}]
                     if is_bound else [])
        run = self._run_tree.get(run_id)
        if run:
            if (run.get("parent_run_id") or run.get("mission_id") != mission.mission_id or
                    run.get("task") != str(mission.goal)[:4000] or
                    run.get("leash") != dict(mission.leash or {}) or
                    run.get("workspace_mode") != "worktree"):
                raise ValueError("deterministic Mission root is bound to different authority")
            if is_bound and not run.get("workspace"):
                if run.get("resources"):
                    run = self._run_tree.bind_workspace(
                        run_id, workspace, owns_workspace=False)
                    if not run:
                        raise ValueError("deterministic Mission root workspace binding raced")
                else:
                    run = self._run_tree.initialize_root_workspace_authority(
                        run_id, workspace, workspace_mode)
            elif is_bound and (
                    os.path.normcase(os.path.realpath(run.get("workspace") or "")) !=
                    os.path.normcase(workspace)):
                raise ValueError("deterministic Mission root workspace conflicts with binding")
            elif not is_bound and run.get("workspace"):
                # Recover a host-created root whose TaskTree commit won before
                # the Mission case attachment committed.
                case["_isolated_workspace"] = run["workspace"]
        else:
            run = self._run_tree.create_root(
                mission.goal, mission.leash, resources, run_id=run_id,
                mission_id=mission.mission_id, workspace=workspace,
                workspace_mode="worktree")
        case["_run_id"] = run_id
        saved = (self.store.set_case_owned(mission.mission_id, mission.run_token, case)
                 if mission.run_token else self.store.set_case(mission.mission_id, case))
        if not saved:
            return ""
        self.store.record_checkpoint(
            mission.mission_id, mission.run_token, "run_tree_lazily_attached",
            {"run_id": run_id, "resources": run.get("resources") or []}, case=case,
            allow_unowned=not bool(mission.run_token))
        return run_id

    @staticmethod
    def _workspace_authority_mode(mission):
        may = list(((mission.leash if mission else {}) or {}).get("may") or [])
        return "write" if any(
            fnmatch.fnmatchcase("code", str(pattern)) for pattern in may) else "read"

    @staticmethod
    def _agent_verify(_record, result):
        if isinstance(result, dict) and result.get("ok"):
            return Verdict(VERIFY_VERIFIED, "scoped durable agent operation recorded")
        error = (result or {}).get("error") if isinstance(result, dict) else result
        return Verdict(VERIFY_FAILED, str(error or "agent operation was refused")[:500])

    def _agent_capabilities(self):
        """Model-facing graph primitives; every mutation is descendant-scoped."""
        def execute_spawn(record):
            args = _clean(record.args)
            if args.get("provider") or args.get("model"):
                return {"ok": False,
                        "error": "specialist provider/model is inherited and cannot be overridden"}
            if args.get("workspace"):
                return {"ok": False,
                        "error": "specialist workspace is provisioned by the container"}
            return self.agent_spawn(
                record.job_id, args.get("role") or "specialist", args.get("task") or "",
                leash=args.get("leash"), resources=args.get("resources"),
                operation_id=record.nonce)

        def execute_send(record):
            args = _clean(record.args)
            return self.agent_send(
                record.job_id, str(args.get("run_id") or ""),
                str(args.get("text") or ""))

        def execute_poll(record):
            args = _clean(record.args)
            return self.agent_poll(record.job_id, str(args.get("run_id") or ""))

        def execute_cancel(record):
            args = _clean(record.args)
            return self.agent_cancel(record.job_id, str(args.get("run_id") or ""))

        return [
            Capability(
                name="agent.spawn", execute=execute_spawn, verify=self._agent_verify,
                reversible=True, risk="read",
                description=("Delegate one scoped task to a durable specialist. Its leash, "
                             "resources, budgets, provider and depth can only inherit or narrow; "
                             "resources are scheduling authority (not a universal tool sandbox), "
                             "and after spawning, wait rather than polling in a tight loop."),
                args_hint='{"role","task","resources":[{"kind":"file","id":"...",'
                          '"mode":"read"}],"leash":{"may":["research"]}}'),
            Capability(
                name="agent.send", execute=execute_send, verify=self._agent_verify,
                reversible=True, risk="read",
                description="Send durable steering text to one descendant specialist.",
                args_hint='{"run_id","text"}'),
            Capability(
                name="agent.poll", execute=execute_poll, verify=self._agent_verify,
                reversible=True, risk="read",
                description=("Inspect descendant status and consume structured completed results; "
                             "completed children also wake a waiting parent automatically."),
                args_hint='{"run_id":"optional descendant; omit for whole subtree"}'),
            Capability(
                name="agent.cancel", execute=execute_cancel, verify=self._agent_verify,
                reversible=True, risk="read",
                description="Cancel one descendant specialist and all authority below it.",
                args_hint='{"run_id"}'),
        ]

    def _fold_child_results(self, mid, run_id, mission_token):
        """Fold, then ack: replay after a cross-database crash is harmless."""
        mission = self.store.get(mid)
        if (not mission or not mission_token or mission.run_token != mission_token or
                mission.state not in (RUNNING, PAUSING)):
            return 0
        messages = self._run_tree.claim_child_results(run_id, mid)
        if not messages:
            return 0
        case = dict(mission.case or {})
        stored_results = case.get("specialist_results")
        stored_results = stored_results if isinstance(stored_results, list) else []
        results = [dict(item) for item in stored_results if isinstance(item, dict)]
        known_at_entry = {int(item.get("message_id")) for item in results
                          if str(item.get("message_id") or "").isdigit()}
        known = set(known_at_entry)
        added = []
        for message in messages:
            message_id = int(message["message_id"])
            if message_id in known:
                continue
            payload = message.get("payload") or {}
            entry = {
                "message_id": message_id,
                "run_id": str(payload.get("run_id") or message.get("sender_run_id") or "")[:100],
                "mission_id": str(payload.get("mission_id") or "")[:100],
                "role": str(payload.get("role") or "specialist")[:80],
                "state": str(payload.get("state") or "")[:40],
                "result": str(payload.get("result") or "")[:4000],
                "artifacts": list(payload.get("artifacts") or [])[:12],
                "observation": payload.get("observation")
                               if isinstance(payload.get("observation"), dict) else {},
                "received_at": int(message.get("created_at") or time.time()),
            }
            results.append(entry)
            added.append(entry)
            known.add(message_id)
        if added:
            case["specialist_results"] = results[-20:]
            if not self.store.set_case_owned(mid, mission_token, case):
                return 0
        # If an id was present on entry, a prior case write already committed it.
        # Every newly added id is also safe once this call's set_case_owned
        # succeeds. Do not derive safety from the bounded results[-20:] view: a
        # replayed old id can be deliberately trimmed when it arrives alongside
        # newer outcomes and must still be acknowledged.
        safe_to_ack = known_at_entry | {item["message_id"] for item in added}
        for message in messages:
            if int(message["message_id"]) in safe_to_ack:
                self._run_tree.ack_child_result(
                    run_id, mid, int(message["message_id"]))
        if added:
            self.store.record_event(
                mid, "agent", "child_result",
                payload={"count": len(added),
                         "run_ids": [item["run_id"] for item in added]})
            self.store.record_checkpoint(
                mid, mission_token, "child_results_folded",
                {"message_ids": [item["message_id"] for item in added]}, case=case)
        return len(added)

    def _mission_control(self, mid):
        mission = self.store.get(mid)
        run_id = self._mission_run_id(mission)
        if mission and run_id and mission.run_token:
            self._fold_child_results(mid, run_id, mission.run_token)
        return {}

    def _wake_parents_with_child_results(self):
        """Wake event-driven waits; do not disturb pause, human or terminal gates."""
        if self._run_tree is None:
            return {"normal": 0, "specialists": 0}
        normal, specialists = 0, 0
        for run in self._run_tree.list_runs():
            mid = str(run.get("mission_id") or "")
            if not mid or not self._run_tree.has_child_results(run["run_id"], mid):
                continue
            mission = self.store.get(mid)
            if not mission or mission.state != WAITING:
                continue
            runtime = self.store.runtime(mid)
            if runtime.get("lane") == "specialist":
                if self._run_tree.requeue_waiting(run["run_id"]):
                    specialists += 1
            else:
                self._activate_execution_profile(mission)
                self._driver().wake(mid, force=True)
                normal += 1
        return {"normal": normal, "specialists": specialists}

    def _agent_completion_guard(self, mid, mission):
        """A parent cannot declare victory while delegated authority is still live."""
        if self._run_tree is None:
            return {}
        run_id = self._mission_run_id(mission)
        if not run_id:
            return {}
        return self._run_tree.completion_blocker(run_id, mid)

    def _linked_descendant_mission_ids(self, parent_mid, run_id):
        descendants = set()
        if self._run_tree is not None and run_id:
            for row in self._run_tree.tree(run_id).get("flat", []):
                child_mid = str(row.get("mission_id") or "")
                if child_mid and child_mid != parent_mid:
                    descendants.add(child_mid)
        known = {parent_mid}
        candidates = self.store.list()
        changed = True
        while changed:
            changed = False
            for child in candidates:
                linked_parent = str((child.case or {}).get("_parent_mission_id") or "")
                if linked_parent in known and child.mission_id not in known:
                    known.add(child.mission_id)
                    descendants.add(child.mission_id)
                    changed = True
        return descendants

    def _cancel_linked_descendant_missions(self, parent_mid, run_id, reason):
        """Mirror a failed TaskTree subtree fence into durable Mission rows."""
        descendants = self._linked_descendant_mission_ids(parent_mid, run_id)
        for child_mid in sorted(descendants):
            child = self.store.get(child_mid)
            if not child or child.state in (
                    DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
                continue
            self._cancel_record(
                child_mid, str(reason or "cancelled because ancestor Mission failed")[:4000],
                user_requested=False, parent_mission_id=parent_mid)
        return len(descendants)

    def _fence_failed_mission_tree(self, mid):
        mission = self.store.get(mid)
        if not mission or mission.state != FAILED_S or self._run_tree is None:
            return False
        run_id = self._mission_run_id(mission)
        if run_id:
            self._project_mission_usage(mid, run_id)
        run = self._run_tree.get(run_id) if run_id else None
        if run and not run.get("parent_run_id"):
            self._run_tree.fail_mission_root(
                run_id, mid, mission.result or "Mission failed")
        elif run:
            # A specialist Mission can commit FAILED just before its dispatcher
            # projects that outcome through the still-owned TaskTree lease.  A
            # restart has no safe owner token, so fence the whole subtree through
            # the durable cancellation protocol and require any live worker to ack.
            self._run_tree.request_cancel(run_id, run.get("parent_run_id") or "")
        linked = self._cancel_linked_descendant_missions(
            mid, run_id, "cancelled because ancestor Mission %s failed" % mid)
        return bool(run or linked)

    def _failed_mission_tree_needs_fence(self, mission):
        """Return true only when another reconciliation pass can change state."""
        if not mission or mission.state != FAILED_S or self._run_tree is None:
            return False
        run_id = self._mission_run_id(mission)
        run = self._run_tree.get(run_id) if run_id else None
        if run:
            for row in self._run_tree.tree(run_id).get("flat", []):
                status = row.get("status")
                if status not in ("completed", "failed", "cancelled", "cancel_requested"):
                    return True
        for child_mid in self._linked_descendant_mission_ids(
                mission.mission_id, run_id):
            child = self.store.get(child_mid)
            if child and child.state not in (
                    DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
                return True
        return False

    def _fence_failed_mission_trees(self, limit=64):
        fenced = 0
        for mission in self.store.list(state=FAILED_S):
            if not self._failed_mission_tree_needs_fence(mission):
                continue
            fenced += int(self._fence_failed_mission_tree(mission.mission_id))
            if fenced >= max(1, int(limit)):
                break
        return fenced

    def _complete_successful_mission_tree(self, mid):
        """Project an authoritative successful root Mission into TaskTree."""
        mission = self.store.get(mid)
        if (not mission or mission.state not in (DONE_VERIFIED, DONE_ACCEPTED) or
                self._run_tree is None):
            return False
        run_id = self._mission_run_id(mission)
        if not run_id:
            return False
        self._project_mission_usage(mid, run_id)
        run = self._run_tree.get(run_id)
        if not run or run.get("parent_run_id"):
            # Specialist runs are completed by their scoped dispatcher while it
            # still owns the TaskTree lease.  This is only the ownerless root seam.
            return False
        return self._run_tree.complete_mission_root(
            run_id, mid, mission.result or "Mission completed")

    def _sync_terminal_mission_tree(self, mid):
        mission = self.store.get(mid)
        if not mission:
            return False
        if mission.state == FAILED_S:
            return self._fence_failed_mission_tree(mid)
        if mission.state in (DONE_VERIFIED, DONE_ACCEPTED):
            return self._complete_successful_mission_tree(mid)
        return False

    def _complete_successful_mission_trees(self, limit=64):
        """Repair the crash window after Mission success but before root projection."""
        if self._run_tree is None:
            return 0
        completed = 0
        examined = 0
        for run in self._run_tree.list_runs():
            if (run.get("parent_run_id") or
                    run.get("status") in ("completed", "failed", "cancelled")):
                continue
            mid = str(run.get("mission_id") or "")
            mission = self.store.get(mid) if mid else None
            if not mission or mission.state not in (DONE_VERIFIED, DONE_ACCEPTED):
                continue
            examined += 1
            completed += int(self._complete_successful_mission_tree(mid))
            if examined >= max(1, int(limit)):
                break
        return completed

    def _sync_terminal_mission_trees(self, limit=64):
        return {
            "failed": self._fence_failed_mission_trees(limit),
            "completed": self._complete_successful_mission_trees(limit),
        }

    def _specialist_artifacts(self, run, child_mission):
        """Return references only, restricted to declared resources/workspace."""
        from .tasktree import normalize_artifact_refs
        case = child_mission.case or {}
        raw = []
        for value in (case.get("artifact_refs"), case.get("artifacts")):
            if isinstance(value, (list, tuple)):
                raw.extend(value)
            elif isinstance(value, (str, dict)):
                raw.append(value)
        refs = normalize_artifact_refs(raw)
        roots = []
        if run.get("workspace") and run.get("owns_workspace"):
            roots.append(os.path.realpath(run["workspace"]))
        for resource in run.get("resources") or []:
            if resource.get("kind") == "file":
                roots.append(os.path.realpath(str(resource.get("id") or "")))
        safe = []
        for ref in refs:
            uri = str(ref.get("uri") or "")
            if uri and not uri.lower().startswith(
                    ("collie://", "https://", "http://", "urn:")):
                continue
            if ref.get("path"):
                path = os.path.realpath(os.path.abspath(ref["path"]))
                try:
                    if not any(os.path.commonpath([root, path]) == root for root in roots):
                        continue
                except ValueError:
                    continue
                ref = dict(ref, path=path)
            safe.append(ref)
        safe.insert(0, {
            "kind": "specialist_result",
            "name": "%s result" % run.get("role", "specialist"),
            "uri": "collie://runs/%s" % run["run_id"],
        })
        if run.get("workspace") and run.get("owns_workspace"):
            safe.insert(0, {
                "kind": "workspace", "name": "%s output" % run.get("role", "specialist"),
                "uri": "collie://runs/%s/workspace" % run["run_id"],
                "path": os.path.realpath(run["workspace"]),
            })
        return normalize_artifact_refs(safe)

    # ── commands ──
    @staticmethod
    def _inherit_execution_contract(source, target_case):
        """Copy immutable code/billing authority into a fresh audit successor."""
        source_case = dict(getattr(source, "case", {}) or {})
        for key in ("execution_profile", "code_profile", "billing_safety"):
            value = source_case.get(key)
            if isinstance(value, dict):
                target_case[key] = json.loads(json.dumps(value, ensure_ascii=False))
        for key in ("_isolated_workspace", "code_baseline_tree_digest",
                    "code_expected_tree_digest"):
            value = source_case.get(key)
            if isinstance(value, str) and value:
                target_case[key] = value
        code_profile = target_case.get("code_profile")
        if isinstance(code_profile, dict):
            session_id = str(source_case.get("code_session_id") or
                             code_profile.get("session_id") or "")
            if session_id:
                code_profile["session_id"] = session_id
        return target_case

    def _bind_successor_workspace(self, mission_id):
        mission = self.store.get(mission_id)
        workspace = str((mission.case or {}).get("_isolated_workspace") or "") \
            if mission else ""
        if not workspace or self._run_tree is None:
            return True
        run_id = self._ensure_agent_root(mission)
        if not run_id:
            self.store.set_state(
                mission_id, FAILED_S, "successor code workspace authority could not be bound")
            return False
        return True

    def start(self, goal: str, autonomous: bool | None = None,
              case: dict = None, *, code: bool = False, workspace: str = "",
              overnight: bool = False, verify_command: str = "",
              no_paid_overage: bool = False, billing_evidence: dict = None,
              provider: str = "", model: str = "",
              **bounds) -> dict:
        """Persist first and return the id immediately; /run or the daemon claims it.

        ``None`` means use the user's Mission default.  Keeping this resolution at
        the service boundary makes Web, CLI, mobile and future surfaces agree;
        explicit True/False remains the per-Mission override and keeps API callers
        deterministic.
        """
        if (not isinstance(code, bool) or not isinstance(overnight, bool) or
                not isinstance(no_paid_overage, bool)):
            raise ValueError("Mission code and overnight options must be booleans")
        if billing_evidence is not None and not isinstance(billing_evidence, dict):
            raise ValueError("Mission billing_evidence must be an object")
        if not isinstance(provider, str) or not isinstance(model, str):
            raise ValueError("Mission provider and model must be strings")
        provider = provider.strip()
        model = model.strip()
        if ("\x00" in provider or "\x00" in model or len(provider) > 120 or
                len(model) > 240):
            raise ValueError("Mission provider/model override is invalid")
        if workspace and not code:
            raise ValueError("Mission workspace requires code mode")
        if verify_command and not code:
            raise ValueError("Mission verify_command requires code mode")
        if workspace:
            try:
                workspace = os.fspath(workspace)
            except TypeError:
                raise ValueError("Mission workspace must be a filesystem path")
            if "\x00" in workspace:
                raise ValueError("Mission workspace contains an invalid NUL byte")
            workspace = os.path.realpath(os.path.abspath(os.path.expanduser(workspace)))
            if not os.path.isdir(workspace):
                raise ValueError("Mission code workspace does not exist")
        verify_command = str(verify_command or "").strip()
        if "\x00" in verify_command or len(verify_command) > 2000:
            raise ValueError("Mission verify_command must be at most 2000 characters without NUL")
        if overnight and not no_paid_overage:
            raise ValueError(
                "overnight Mission requires --no-paid-overage after disabling paid "
                "usage credits/overage and auto-reload in the provider account")
        if overnight and code and not workspace:
            raise ValueError("overnight code Mission requires an existing workspace")
        if overnight:
            for key, value in _OVERNIGHT_BOUNDS.items():
                requested = bounds.get(key, value)
                try:
                    requested = (float(requested) if key == "max_model_cost_usd"
                                 else int(requested))
                except (TypeError, ValueError):
                    raise ValueError("overnight Mission bound %s is invalid" % key)
                if isinstance(requested, float) and not math.isfinite(requested):
                    raise ValueError("overnight Mission bound %s must be finite" % key)
                # The overnight preset is a ceiling. API/CLI callers may make a
                # run tighter, but can never turn the preset into a larger or
                # longer unattended authority envelope.
                bounds[key] = min(requested, value)
        if autonomous is None:
            from . import settings
            autonomous = settings.get("MISSION_APPROVAL_MODE", "smart") == "smart"
        case = dict(case or {})
        may = bounds.pop("may", None)
        mid = "msn_" + secrets.token_hex(6)
        if code:
            default_may = ["research", "compose", "observe", "agent.*", "web.*",
                           "browse", "browse.*", "verification.*"]
            may = list(default_may if may is None else may)
            if "code" not in may:
                may.append("code")
            profile = _execution_profile(
                provider or self._provider, model or self._model,
                overnight=overnight)
            case["execution_profile"] = profile
            if overnight:
                try:
                    case["billing_safety"] = self._subscription_preflight(
                        profile, {
                            "paid_overage_disabled": no_paid_overage,
                            "account_evidence": dict(billing_evidence or {}),
                        })
                except RuntimeError as exc:
                    raise ValueError(str(exc)) from exc
                if not verify_command:
                    from .verification import detect_verification_commands
                    proposals = detect_verification_commands(workspace)
                    verify_command = str(
                        proposals[0].get("command") if proposals else "").strip()
                if not verify_command:
                    raise ValueError(
                        "overnight code Mission requires an explicit or detected "
                        "verification command")
            baseline_digest = ""
            if workspace:
                from .verification import workspace_snapshot
                baseline = workspace_snapshot(workspace)
                if overnight and not baseline.get("snapshot_complete"):
                    raise ValueError(
                        "overnight code workspace could not be snapshotted completely")
                baseline_digest = str(baseline.get("tree_digest") or "")
                if overnight and not baseline_digest:
                    raise ValueError("overnight code workspace has no stable baseline digest")
            from .primitives import _code_session_id
            case["code_profile"] = {
                "durable": True,
                "overnight": bool(overnight),
                "verify_command": verify_command,
                "slice_turns": 3 if overnight else 0,
                "verify_timeout_seconds": 300,
                "max_session_storage_bytes": 15_000_000 if overnight else 0,
                "session_id": _code_session_id(mid, workspace) if workspace else "",
            }
            if baseline_digest:
                case["code_baseline_tree_digest"] = baseline_digest
                case["code_expected_tree_digest"] = baseline_digest
            if workspace:
                case["_isolated_workspace"] = workspace
        elif overnight:
            profile = _execution_profile(
                provider or self._provider, model or self._model,
                overnight=True)
            case["execution_profile"] = profile
            try:
                case["billing_safety"] = self._subscription_preflight(
                    profile, {
                        "paid_overage_disabled": no_paid_overage,
                        "account_evidence": dict(billing_evidence or {}),
                    })
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc
        if isinstance(case.get("execution_profile"), dict):
            bounds["execution_profile_sha256"] = _execution_profile_digest(
                case["execution_profile"])
        # Durable jobs get their own worktree by default.  The Web/CLI provisioner
        # binds its canonical path later through bind_workspace(); ordinary world
        # Missions pay no cost for this until they actually choose ``code``.
        bounds.setdefault("workspace_mode", "isolated")
        create_mission(self.store, mid, goal, case=case,
                       leash=world_leash(may=may, autonomous=autonomous, **bounds))
        if code and workspace and self._run_tree is not None:
            # Complete the authority binding before returning a runnable id.  The
            # root points at the user's existing directory with owns_workspace=0;
            # neither Mission cancellation nor TaskTree cleanup may delete it.
            try:
                run_id = self._ensure_agent_root(self.store.get(mid))
                if not run_id:
                    raise ValueError("durable code root could not be attached")
            except Exception as exc:
                self.store.set_state(
                    mid, FAILED_S, "code workspace authority could not be bound: %s" % exc)
                raise ValueError("Mission code workspace binding failed: %s" % exc)
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
        run_id = str((m.case or {}).get("_run_id") or "")
        if self._run_tree is not None and not run_id:
            candidate_id = self._agent_root_id(mid)
            candidate = self._run_tree.get(candidate_id)
            if candidate:
                if (candidate.get("parent_run_id") or candidate.get("mission_id") != mid or
                        candidate.get("task") != str(m.goal)[:4000] or
                        candidate.get("leash") != dict(m.leash or {}) or
                        candidate.get("workspace_mode") != "worktree"):
                    return {**self.status(mid),
                            "error": "workspace authority refused: orphan root identity conflicts"}
                run_id = candidate_id
        if self._run_tree is not None and run_id:
            try:
                run = self._run_tree.get(run_id) or {}
                if run.get("resources"):
                    bound = self._run_tree.bind_workspace(
                        run_id, canonical, owns_workspace=False)
                    if not bound:
                        raise ValueError("declared root workspace is already bound elsewhere")
                else:
                    self._run_tree.initialize_root_workspace_authority(
                        run_id, canonical, self._workspace_authority_mode(m))
            except ValueError as exc:
                return {**self.status(mid), "error": "workspace authority refused: %s" % exc}
        case = dict(m.case)
        case["_isolated_workspace"] = canonical
        if run_id:
            case["_run_id"] = run_id
        if not self.store.set_case(mid, case):
            return {**self.status(mid),
                    "error": "workspace authority initialized but Mission binding raced"}
        self.store.record_checkpoint(
            mid, "", "workspace_bound", {"workspace": canonical},
            case=case, allow_unowned=True)
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
        run_id = self._agent_root_id(mid)
        try:
            run = self._run_tree.create_root(
                m.goal, m.leash, resources, run_id=run_id,
                mission_id=mid, workspace=workspace,
                workspace_mode="worktree")
        except ValueError as exc:
            return {**self.status(mid), "error": "run-tree creation refused: %s" % exc}
        case = dict(m.case)
        case["_run_id"] = run["run_id"]
        if workspace:
            case["_isolated_workspace"] = os.path.realpath(os.path.abspath(workspace))
        self.store.set_case(mid, case)
        self.store.record_checkpoint(
            mid, "", "run_tree_created", {"run_id": run["run_id"]},
            case=case, allow_unowned=True)
        self._project_mission_usage(mid, run["run_id"])
        return self._run_tree.tree(run["run_id"])

    def spawn_specialist(self, mid: str, role: str, task: str, *, leash=None,
                         resources=None, workspace: str = "") -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if m.state in (DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
            return {**self.status(mid),
                    "error": "terminal Mission cannot spawn a specialist"}
        run_id = self._mission_run_id(m)
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

    def agent_spawn(self, mid: str, role: str, task: str, *, leash=None,
                    resources=None, operation_id: str = "") -> dict:
        """Container-provisioned model entry for ``agent.spawn``."""
        mission, parent, error = self._agent_caller(mid)
        if error:
            return {"ok": False, "error": error, "mission_id": mid}
        task = str(task or "").strip()[:4000]
        if not task:
            return {"ok": False, "error": "specialist task is empty", "mission_id": mid}
        if resources is None or not isinstance(resources, (list, tuple)):
            return {"ok": False,
                    "error": "agent.spawn requires an explicit resources list",
                    "mission_id": mid}
        try:
            from .tasktree import narrow_leash, normalize_resources
            scoped = normalize_resources(resources)
            effective_leash = narrow_leash(parent.get("leash") or {}, leash)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "mission_id": mid}
        physical_workspace = str(parent.get("workspace") or "")
        if not physical_workspace and not scoped:
            # Resource-free specialists may use cwd as an execution directory;
            # it is deliberately not persisted as root filesystem authority.
            physical_workspace = os.getcwd()
        if not physical_workspace:
            return {"ok": False,
                    "error": "calling run has no container-bound workspace",
                    "mission_id": mid}
        file_write = any(item.get("kind") == "file" and item.get("mode") == "write"
                         for item in scoped)
        if file_write:
            # The current code child is rooted at the whole isolated worktree. Until it can bind
            # several independent path roots, claiming that a subdirectory-only grant confines it
            # would be false authority. Require a write root that covers the source workspace.
            source_workspace = str(
                (mission.case or {}).get("_resource_source_workspace") or
                parent.get("workspace") or "")
            if not source_workspace:
                return {"ok": False,
                        "error": "file-writing specialist has no logical source workspace",
                        "mission_id": mid}
            workspace_root = os.path.normcase(os.path.realpath(source_workspace))
            covers_workspace = False
            for item in scoped:
                if item.get("kind") != "file" or item.get("mode") != "write":
                    continue
                try:
                    item_root = os.path.normcase(os.path.realpath(item.get("id") or ""))
                    covers_workspace = (os.path.commonpath(
                        [item_root, workspace_root]) == item_root)
                except (OSError, ValueError):
                    covers_workspace = False
                if covers_workspace:
                    break
            if not covers_workspace:
                return {
                    "ok": False,
                    "error": ("file-writing specialist needs a directory write resource "
                              "covering its full parent workspace; narrower scopes are not "
                              "yet enforceable by the isolated code runner"),
                    "mission_id": mid,
                }
        try:
            # Read-only specialists may share the stable parent checkout.  A file
            # writer starts unbound and can run only after the container provisions
            # an isolated git worktree.
            semantic = json.dumps({
                "parent": parent["run_id"], "role": str(role or "specialist")[:80],
                "task": task, "resources": scoped, "leash": effective_leash,
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            # An action nonce identifies one intentional model operation. Semantic
            # content remains TaskTree's replay/collision check, but must not merge
            # two deliberate identical delegations. Direct host calls have no
            # action nonce, so retain semantic idempotency for that legacy seam.
            identity = (json.dumps({"parent": parent["run_id"],
                                    "operation_id": str(operation_id)},
                                   ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"))
                        if operation_id else semantic)
            stable_run_id = "run_agent_" + hashlib.sha256(
                identity.encode("utf-8", "replace")).hexdigest()[:16]
            child = self._run_tree.spawn_specialist(
                parent["run_id"], role, task, leash=leash, resources=scoped,
                workspace="" if file_write else physical_workspace,
                workspace_mode="worktree", run_id=stable_run_id)
            if child.get("status") in ("completed", "failed", "cancelled"):
                return {
                    "ok": child.get("status") == "completed",
                    "run_id": child["run_id"], "mission_id": child.get("mission_id") or "",
                    "parent_run_id": child["parent_run_id"], "role": child["role"],
                    "status": child["status"], "result": child.get("result") or "",
                    "replayed": True,
                }
            if file_write and not child.get("workspace"):
                prepared = self._run_tree.provision_worktree(
                    child["run_id"], physical_workspace)
                if prepared.get("busy"):
                    current = self._run_tree.get(child["run_id"]) or child
                    if current.get("status") in ("completed", "failed", "cancelled"):
                        return {
                            "ok": current.get("status") == "completed",
                            "run_id": current["run_id"],
                            "mission_id": current.get("mission_id") or "",
                            "parent_run_id": current["parent_run_id"],
                            "role": current["role"], "status": current["status"],
                            "result": current.get("result") or "", "replayed": True,
                        }
                    return {
                        "ok": True, "run_id": child["run_id"], "mission_id": "",
                        "parent_run_id": child["parent_run_id"], "role": child["role"],
                        "status": child["status"], "provisioning": True,
                        "resources": child.get("resources") or [],
                        "authority": {
                            "may": list((child.get("leash") or {}).get("may") or []),
                            "provider": "inherited"},
                    }
                if not prepared.get("ok"):
                    self._run_tree.cancel_descendant(parent["run_id"], child["run_id"])
                    return {"ok": False,
                            "error": str(prepared.get("error") or
                                         "isolated specialist worktree could not be provisioned")[:500],
                            "run_id": child["run_id"], "mission_id": mid}
                child = prepared.get("run") or self._run_tree.get(child["run_id"])
                if not child or not child.get("workspace"):
                    raise ValueError("isolated specialist workspace binding raced or was lost")
            child = self._create_specialist_mission(mid, child)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "mission_id": mid}
        return {
            "ok": True,
            "run_id": child["run_id"],
            "mission_id": child.get("mission_id") or "",
            "parent_run_id": child["parent_run_id"],
            "role": child["role"],
            "status": child["status"],
            "resources": child.get("resources") or [],
            "authority": {"may": list((child.get("leash") or {}).get("may") or []),
                          "provider": "inherited"},
        }

    def agent_send(self, mid: str, run_id: str, text: str) -> dict:
        mission, caller, error = self._agent_caller(mid)
        if error:
            return {"ok": False, "error": error, "mission_id": mid}
        if not run_id:
            return {"ok": False, "error": "agent.send requires run_id", "mission_id": mid}
        try:
            message_id = self._run_tree.send_to_descendant(
                caller["run_id"], run_id, text)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "mission_id": mid}
        if message_id is None:
            return {"ok": False, "error": "target specialist is terminal",
                    "run_id": run_id, "mission_id": mid}
        return {"ok": True, "run_id": run_id, "message_id": message_id,
                "queued": True}

    def agent_poll(self, mid: str, run_id: str = "") -> dict:
        mission, caller, error = self._agent_caller(mid)
        if error:
            return {"ok": False, "error": error, "mission_id": mid}
        target = run_id or caller["run_id"]
        if target != caller["run_id"] and not self._run_tree.is_descendant(
                caller["run_id"], target):
            return {"ok": False,
                    "error": "specialist target is outside caller descendant scope",
                    "mission_id": mid, "run_id": target}
        if mission.run_token:
            self._fold_child_results(mid, caller["run_id"], mission.run_token)
            mission = self.store.get(mid)
        tree = self._run_tree.tree(target)
        runs = [{
            "run_id": row["run_id"], "parent_run_id": row["parent_run_id"],
            "role": row["role"], "status": row["status"],
            "progress_seq": row["progress_seq"], "progress_at": row["progress_at"],
            "result": str(row.get("result") or "")[:1000],
        } for row in tree.get("flat", [])]
        visible = {row["run_id"] for row in runs}
        stored_results = (mission.case or {}).get("specialist_results")
        stored_results = stored_results if isinstance(stored_results, list) else []
        results = [item for item in stored_results
                   if isinstance(item, dict) and item.get("run_id") in visible]
        return {"ok": True, "run_id": target, "runs": runs,
                "results": results[-20:]}

    def _cancel_bound_specialist_missions(self, run_id: str, reason: str,
                                          parent_mission_id: str = "") -> int:
        """Fence every Mission/process represented by a cancelled run subtree.

        TaskTree cancellation revokes delegated scheduling authority, while the
        Mission row owns the code-process lifetime.  Updating only TaskTree would
        leave a claimed child Mission free to keep editing until its next model
        boundary, so both durable representations are cancelled together.
        """
        if self._run_tree is None:
            return 0
        cancelled = 0
        for row in self._run_tree.tree(run_id).get("flat", []):
            child_mid = str(row.get("mission_id") or "")
            child = self.store.get(child_mid) if child_mid else None
            if not child or child.state in (
                    DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
                continue
            cancelled += int(self._cancel_record(
                child_mid, reason, user_requested=False,
                parent_mission_id=parent_mission_id))
        return cancelled

    def agent_cancel(self, mid: str, run_id: str) -> dict:
        mission, caller, error = self._agent_caller(mid)
        if error:
            return {"ok": False, "error": error, "mission_id": mid}
        if not run_id:
            return {"ok": False, "error": "agent.cancel requires run_id", "mission_id": mid}
        if not self._run_tree.is_descendant(caller["run_id"], run_id):
            return {"ok": False,
                    "error": "specialist target is outside caller descendant scope",
                    "mission_id": mid, "run_id": run_id}
        target = self._run_tree.get(run_id)
        bound_cancelled = self._cancel_bound_specialist_missions(
            run_id, "cancelled by delegating Mission", parent_mission_id=mid)
        if target and target.get("status") in ("completed", "failed", "cancelled"):
            return {"ok": True, "run_id": run_id, "status": target["status"],
                    "already_terminal": True,
                    "bound_missions_cancelled": bound_cancelled}
        try:
            changed = self._run_tree.cancel_descendant(caller["run_id"], run_id)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "mission_id": mid}
        # Close the cross-store creation race.  A concurrent spawn may have
        # committed its Mission after the first tree snapshot but before the
        # TaskTree cancellation transaction.  The TaskTree fence prevents any
        # later spawn; this second sweep terminates every Mission that became
        # visible in that window.  _create_specialist_mission performs the
        # inverse final-state check for a Mission committing after this sweep.
        bound_cancelled += self._cancel_bound_specialist_missions(
            run_id, "cancelled by delegating Mission", parent_mission_id=mid)
        current = self._run_tree.get(run_id)
        return {"ok": bool(changed or bound_cancelled), "run_id": run_id,
                "status": current.get("status") if current else "missing",
                "bound_missions_cancelled": bound_cancelled,
                **({} if changed or bound_cancelled else
                   {"error": "target specialist is unavailable"})}

    def _create_specialist_mission(self, parent_mid, run):
        """Materialize a scoped specialist as a real Mission lane, not a TODO row."""
        workspace = run.get("workspace") or ""
        if not workspace:
            return run
        child_mid = "spc_" + run["run_id"].replace("run_", "")
        if run.get("mission_id") and run.get("mission_id") != child_mid:
            raise ValueError("specialist run is bound to a different Mission")
        # Reserve the deterministic cross-database identity first. A crash now
        # leaves a TaskTree row that the orphan pass can materialize, rather than
        # an unaddressable Mission row or an ambiguous second child.
        if not run.get("mission_id"):
            if not self._run_tree.bind_mission(run["run_id"], child_mid):
                current = self._run_tree.get(run["run_id"])
                if not current or current.get("mission_id") != child_mid:
                    raise ValueError("specialist Mission binding raced with another owner")
            run = self._run_tree.get(run["run_id"]) or run
        parent_run = self._run_tree.get(run.get("parent_run_id") or "") or {}
        parent_mid = str(parent_mid or parent_run.get("mission_id") or "")
        parent_mission = self.store.get(parent_mid)
        source_workspace = str(
            ((parent_mission.case or {}).get("_resource_source_workspace")
             if parent_mission else "") or parent_run.get("workspace") or "")
        existing = self.store.get(child_mid)
        if not existing:
            case = {
                "_isolated_workspace": workspace,
                "_specialist_run_id": run["run_id"],
                "_parent_mission_id": parent_mid,
                "_resource_scope": run.get("resources") or [],
                "_resource_source_workspace": source_workspace,
                "role": run.get("role") or "specialist",
            }
            if parent_mission:
                parent_case = dict(parent_mission.case or {})
                execution_profile = parent_case.get("execution_profile")
                if isinstance(execution_profile, dict):
                    case["execution_profile"] = json.loads(json.dumps(
                        execution_profile, ensure_ascii=False))
                billing_safety = parent_case.get("billing_safety")
                if isinstance(billing_safety, dict):
                    case["billing_safety"] = json.loads(json.dumps(
                        billing_safety, ensure_ascii=False))
                    if isinstance(execution_profile, dict) and \
                            execution_profile.get("profile") == "overnight":
                        # A parent's allow receipt is evidence, not transferable
                        # authority. Re-prove the route for this runnable child.
                        case["billing_safety"] = self._subscription_preflight(
                            execution_profile, case["billing_safety"])
                code_profile = parent_case.get("code_profile")
                if isinstance(code_profile, dict):
                    child_profile = json.loads(json.dumps(
                        code_profile, ensure_ascii=False))
                    from .primitives import _code_session_id
                    child_profile["session_id"] = _code_session_id(
                        child_mid, workspace)
                    case["code_profile"] = child_profile
                    from .verification import workspace_snapshot
                    baseline = workspace_snapshot(workspace)
                    if (execution_profile or {}).get("profile") == "overnight" and \
                            not baseline.get("snapshot_complete"):
                        raise ValueError(
                            "overnight specialist workspace could not be snapshotted completely")
                    digest = str(baseline.get("tree_digest") or "")
                    if digest:
                        case["code_baseline_tree_digest"] = digest
                        case["code_expected_tree_digest"] = digest
            try:
                create_mission(
                    self.store, child_mid, run["task"], case=case, leash=run["leash"],
                    lane="specialist", external_run_id=run["run_id"])
            except sqlite3.IntegrityError:
                # Another dispatcher may have repaired the same crash window.
                if not self.store.get(child_mid):
                    raise
        runtime = self.store.runtime(child_mid)
        if (str(runtime.get("external_run_id") or "") != run["run_id"] or
                str(runtime.get("parent_mission_id") or "") != str(parent_mid or "")):
            raise ValueError("specialist Mission id is bound to different authority")
        # Complete the other half of the cancellation handshake.  The Mission
        # insert and TaskTree cancellation live in separate SQLite databases, so
        # cancellation can fence the run after bind_mission(), then perform its
        # post-fence sweep just before this Mission commits.  Re-read both sides
        # after the commit and make the new Mission terminal when its authority
        # has already been revoked.  Conversely, if this check wins the race,
        # the cancellation path's post-fence sweep observes and cancels it.
        current_run = self._run_tree.get(run["run_id"])
        current_parent = self.store.get(parent_mid) if parent_mid else None
        run_stopping = (
            not current_run or
            current_run.get("status") in (
                "completed", "failed", "cancelled", "cancel_requested") or
            bool(current_run.get("cancel_requested")))
        parent_stopping = (
            not current_parent or current_parent.state in (
                DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED))
        if run_stopping or parent_stopping:
            # If the parent Mission reached a terminal state before TaskTree was
            # fenced, revoke the child run here as well.  request_cancel is
            # idempotent when the cancellation path already won.
            if current_run and not run_stopping:
                self._run_tree.request_cancel(run["run_id"])
            child = self.store.get(child_mid)
            if child and child.state not in (
                    DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
                self._cancel_record(
                    child_mid,
                    "cancelled because specialist authority was revoked during creation",
                    user_requested=False, parent_mission_id=parent_mid)
            current_run = self._run_tree.get(run["run_id"])
        return current_run

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
        usage = self._reconcile_tasktree_usage(mid)
        return {
            "mission_id": mid,
            "available": self._run_tree is not None,
            "attached": bool(run_id),
            "path": getattr(self._run_tree, "path", None),
            "tree": self._run_tree.tree(run_id) if self._run_tree and run_id
                    else {"root": None, "flat": []},
            "usage_projection_errors": usage["errors"],
        }

    def inspect_specialist(self, run_id: str, event_limit: int = 100) -> dict:
        """Inspect one run, its descendant tree and recent durable events."""
        if self._run_tree is None:
            return {"error": "no durable run-tree store configured", "run_id": run_id}
        run = self._run_tree.get(run_id)
        if not run:
            return {"error": "unknown specialist run", "run_id": run_id}
        usage = self._reconcile_tasktree_usage(run.get("mission_id") or "") \
            if run.get("mission_id") else {"errors": []}
        run = self._run_tree.get(run_id) or run
        return {"run": run, "tree": self._run_tree.tree(run_id),
                "events": self._run_tree.events(run_id, event_limit),
                "usage_projection_errors": usage["errors"]}

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
        if not self._run_tree.get(run_id):
            return {"error": "unknown or terminal specialist run", "run_id": run_id}
        bound_cancelled = self._cancel_bound_specialist_missions(
            run_id, "specialist cancelled by operator")
        changed = self._run_tree.request_cancel(run_id, sender_run_id)
        # See agent_cancel: fence first, then sweep once more for a Mission that
        # committed between the initial TaskTree snapshot and this transaction.
        bound_cancelled += self._cancel_bound_specialist_missions(
            run_id, "specialist cancelled by operator")
        if not changed and not bound_cancelled:
            return {"error": "unknown or terminal specialist run", "run_id": run_id}
        result = self.inspect_specialist(run_id)
        result["bound_missions_cancelled"] = bound_cancelled
        return result

    def run(self, mid: str) -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        try:
            self._activate_execution_profile(m)
        except RuntimeError as exc:
            return {**self.status(mid), "error": "run unavailable: %s" % exc}
        usage = self._reconcile_tasktree_usage(mid)
        if usage["errors"]:
            return {**self.status(mid),
                    "error": "usage reconciliation failed closed",
                    "usage_projection_errors": usage["errors"]}
        if self._specialist_run(mid):
            return {**self.status(mid),
                    "error": "specialist Missions run only through their scoped dispatcher"}
        if m.state != QUEUED:
            # Idempotent for the common Web-vs-daemon claim race: if somebody else
            # already advanced it, return the live state instead of a false failure.
            self._sync_terminal_mission_tree(mid)
            return self.status(mid)
        try:
            self._driver().advance(mid)
        except Exception as e:
            return {**self.status(mid), "error": f"run unavailable: {e}"}
        finally:
            self._reconcile_tasktree_usage(mid)
        self._sync_terminal_mission_tree(mid)
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
            self._activate_execution_profile(m)
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
        self._sync_terminal_mission_tree(mid)
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
        # Repair the cross-store crash window before even considering a successor.
        # Once the failed root is fenced, no new descendants can be added; running
        # workers must durably acknowledge cancellation before retry is safe.
        self._fence_failed_mission_tree(mid)
        predecessor_run_id = self._mission_run_id(m)
        if predecessor_run_id and self._run_tree is not None:
            unsettled = [row for row in self._run_tree.tree(predecessor_run_id).get("flat", [])
                         if row.get("status") not in
                         ("completed", "failed", "cancelled")]
            if unsettled:
                summary = ", ".join("%s:%s" % (
                    row.get("role") or "specialist", row.get("status") or "unknown")
                                    for row in unsettled[:6])
                return {
                    **self.status(mid),
                    "error": "cannot retry until predecessor specialists settle (%s)" % summary,
                }
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
        # A failed row is immutable, but the successor must retain durable
        # control-plane contracts at top level. Keeping these only inside the
        # namespaced predecessor makes the planner lose campaign coverage and
        # branch-scoped authorizations exactly when recovery is most important.
        for key in (
                "_campaign_coverage", "pending_authorizations",
                "resolved_authorizations", "pending_followups",
                "_due_followups", "resolved_followups"):
            value = (m.case or {}).get(key)
            if isinstance(value, (list, dict)):
                case[key] = json.loads(json.dumps(value, ensure_ascii=False))
        self._inherit_execution_contract(m, case)
        successor = "msn_" + secrets.token_hex(6)
        create_mission(self.store, successor, m.goal, case=case, leash=dict(m.leash))
        if not self._bind_successor_workspace(successor):
            return self.status(successor)
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

    def _inspect_code_recovery(self, mission):
        """Read the two durable code journals without granting either authority.

        The Mission case is the publication boundary; session receipts are an
        append-before-publish WAL.  A completed reconciliation receipt may
        therefore legitimately be newer than ``code_expected_tree_digest``
        after a crash.  ``receipt_expected`` follows only a contiguous chain so
        that such a crash can be retried without blessing unrelated drift.
        """
        case = dict(mission.case or {})
        profile = case.get("code_profile")
        profile = profile if isinstance(profile, dict) else {}
        session_id = str(case.get("code_session_id") or
                         profile.get("session_id") or "")
        workspace = str(case.get("_isolated_workspace") or "")
        case_expected = str(case.get("code_expected_tree_digest") or
                            case.get("code_baseline_tree_digest") or "")
        baseline = str(case.get("code_baseline_tree_digest") or case_expected)
        codeish = bool(profile or session_id or
                       case.get("code_recovery_required") or case_expected)
        if not codeish:
            return {"is_code": False, "requires_resolution": False}

        from . import sessions
        session_dir = os.path.join(self._state_dir, "mission-code-sessions")
        checked = (sessions.load_checked(session_id, directory=session_dir)
                   if session_id else {"status": "missing", "session": None})
        if checked.get("status") == "invalid":
            return {
                "is_code": True, "invalid": True, "session_id": session_id,
                "session_dir": session_dir,
                "error": "durable code session is corrupt or unreadable; inspect or cancel it",
            }
        saved = checked.get("session") or {}
        receipts = list(saved.get("run_receipts") or [])
        receipt_expected = case_expected
        for receipt in receipts:
            kind = receipt.get("kind")
            receipt_sid = str(receipt.get("session_id") or "")
            receipt_mid = str(receipt.get("mission_id") or "")
            if ((receipt_sid and receipt_sid != session_id) or
                    (receipt_mid and receipt_mid != mission.mission_id)):
                return {
                    "is_code": True, "invalid": True,
                    "session_id": session_id, "session_dir": session_dir,
                    "error": "durable code receipt identity does not match this Mission",
                }
            receipt_baseline = str(receipt.get("baseline_tree_digest") or "")
            if (kind in ("mission_code_baseline", "mission_code_slice",
                         "mission_code_reconciled") and
                    baseline and receipt_baseline and
                    receipt_baseline != baseline):
                return {
                    "is_code": True, "invalid": True,
                    "session_id": session_id, "session_dir": session_dir,
                    "error": "durable code receipt baseline does not match this Mission",
                }
            if kind not in ("mission_code_slice", "mission_code_reconciled"):
                continue
            if kind == "mission_code_reconciled" and (
                    receipt.get("resolution") != "completed" or
                    receipt.get("snapshot_complete") is not True):
                return {
                    "is_code": True, "invalid": True,
                    "session_id": session_id, "session_dir": session_dir,
                    "error": "durable code reconciliation receipt is malformed",
                }
            before = str(receipt.get("pre_tree_digest") or "")
            after = str(receipt.get("post_tree_digest") or "")
            if before and after and before == receipt_expected:
                receipt_expected = after

        active = saved.get("active_run")
        session_uncertain = bool(
            isinstance(active, dict) and
            active.get("state") in ("executing_tool", "external_action"))
        current = ""
        if workspace:
            if not os.path.isdir(workspace):
                return {
                    "is_code": True, "invalid": True,
                    "session_id": session_id, "session_dir": session_dir,
                    "error": "bound code workspace is missing; cancel or restore it before recovery",
                }
            from .verification import workspace_snapshot
            snapshot = workspace_snapshot(workspace)
            if not snapshot.get("snapshot_complete"):
                return {
                    "is_code": True, "invalid": True,
                    "session_id": session_id, "session_dir": session_dir,
                    "error": "bound code workspace could not be snapshotted completely",
                }
            current = str(snapshot.get("tree_digest") or "")
        drift = bool(case_expected and current and current != case_expected)
        required = bool(case.get("code_recovery_required") or
                        session_uncertain or drift)
        if required and (not workspace or not case_expected or not current):
            return {
                "is_code": True, "invalid": True,
                "session_id": session_id, "session_dir": session_dir,
                "error": "code recovery has no complete workspace byte boundary; cancel or inspect storage",
            }
        return {
            "is_code": True, "invalid": False,
            "requires_resolution": required,
            "session_id": session_id, "session_dir": session_dir,
            "session_uncertain": session_uncertain,
            "session": saved, "receipts": receipts,
            "workspace": workspace, "baseline": baseline,
            "case_expected": case_expected,
            "receipt_expected": receipt_expected or case_expected,
            "current": current, "drift": drift,
        }

    def _patch_code_recovery_owned(self, mid, token, expected_before, updates):
        """CAS code byte-boundary state under the live reconciliation token."""
        now = int(time.time())
        updates = dict(updates or {})
        with self.store._lock:
            try:
                self.store.db.execute("BEGIN IMMEDIATE")
                row = self.store.db.execute(
                    "SELECT case_json FROM missions WHERE mission_id=? AND state=? "
                    "AND run_token=? AND lease_until>?",
                    (mid, RECONCILING, token, now)).fetchone()
                if not row:
                    self.store.db.rollback()
                    return False
                case = json.loads(row["case_json"] or "{}")
                if not isinstance(case, dict):
                    self.store.db.rollback()
                    return False
                live_expected = str(case.get("code_expected_tree_digest") or
                                    case.get("code_baseline_tree_digest") or "")
                if live_expected != str(expected_before or ""):
                    self.store.db.rollback()
                    return False
                case.update(updates)
                case = _compact_case_storage(case)
                cur = self.store.db.execute(
                    "UPDATE missions SET case_json=?,updated_at=? WHERE mission_id=? "
                    "AND state=? AND run_token=? AND lease_until>?",
                    (json.dumps(case, ensure_ascii=False, separators=(",", ":")),
                     now, mid, RECONCILING, token, now))
                self.store.db.commit()
                return cur.rowcount == 1
            except Exception:
                self.store.db.rollback()
                raise

    def reconcile(self, mid: str, note: str = "",
                  code_resolution: str = "") -> dict:
        """Acknowledge a crash-uncertain external action after manual inspection."""
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        if m.state not in (RECOVERY_REQUIRED, RECONCILING):
            return {**self.status(mid), "error": f"cannot reconcile from {m.state}"}
        code = self._inspect_code_recovery(m)
        if code_resolution == "cancel":
            # ``sessions.reconcile_recovery(..., cancel)`` only closes the
            # transcript's uncertain tool boundary.  The user's cancellation is
            # also a lifecycle decision: fence the Mission/process and its
            # delegated tree instead of publishing QUEUED below.
            if (not code.get("invalid") and code.get("session_uncertain") and
                    code.get("session_id")):
                from . import sessions
                try:
                    sessions.reconcile_recovery(
                        code["session_id"], "cancel", note=note, confirmed=True,
                        directory=code["session_dir"])
                except (KeyError, ValueError):
                    pass  # terminal Mission cancellation remains the safe outcome
            return self.cancel(mid)
        if code.get("invalid"):
            return {**self.status(mid), "error": code.get("error")}
        if (code.get("requires_resolution") and
                code_resolution not in ("completed", "not_fired")):
            return {
                **self.status(mid),
                "error": (
                    "code workspace/session requires inspection; retry reconcile with "
                    "code_resolution=completed, not_fired, or cancel"),
            }
        if (code.get("requires_resolution") and code_resolution == "not_fired" and
                code.get("current") != code.get("case_expected")):
            return {
                **self.status(mid),
                "error": (
                    "code_resolution=not_fired is unsafe because the current workspace "
                    "digest does not equal the original expected digest"),
            }
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
            # Re-read both journals after acquiring the durable Mission fence.
            # The preflight above is diagnostic only and grants no authority.
            owned_mission = self.store.get(mid)
            code = self._inspect_code_recovery(owned_mission)
            if code.get("invalid"):
                self.store.release_reconcile(mid, reconcile_token)
                return {**self.status(mid), "error": code.get("error")}
            if (code.get("requires_resolution") and
                    code_resolution not in ("completed", "not_fired")):
                self.store.release_reconcile(mid, reconcile_token)
                return {
                    **self.status(mid),
                    "error": "code recovery still requires an explicit completed/not_fired outcome",
                }
            if (code.get("requires_resolution") and
                    code_resolution == "not_fired" and
                    code.get("current") != code.get("case_expected")):
                self.store.release_reconcile(mid, reconcile_token)
                return {
                    **self.status(mid),
                    "error": "code_resolution=not_fired refused because workspace bytes drifted",
                }
            if code.get("requires_resolution"):
                from . import sessions
                if code.get("session_uncertain"):
                    try:
                        sessions.reconcile_recovery(
                            code["session_id"], code_resolution, note=note,
                            confirmed=True, directory=code["session_dir"])
                    except (KeyError, ValueError) as exc:
                        self.store.release_reconcile(mid, reconcile_token)
                        return {**self.status(mid),
                                "error": "code session reconciliation failed: %s" % exc}
                case_updates = {"code_recovery_required": False}
                if code.get("session_id"):
                    case_updates["code_session_id"] = code["session_id"]
                if code_resolution == "completed":
                    receipt = {
                        "kind": "mission_code_reconciled",
                        "mission_id": mid,
                        "session_id": code.get("session_id") or "",
                        "baseline_tree_digest": code.get("baseline") or
                                                code.get("case_expected") or "",
                        "pre_tree_digest": code.get("receipt_expected") or
                                           code.get("case_expected") or "",
                        "post_tree_digest": code.get("current") or "",
                        "snapshot_complete": True,
                        "resolution": "completed",
                        "note": str(note or "")[:1000],
                        "at": int(time.time()),
                    }
                    already = any(
                        row.get("kind") == receipt["kind"] and
                        str(row.get("mission_id") or "") == mid and
                        str(row.get("pre_tree_digest") or "") ==
                        receipt["pre_tree_digest"] and
                        str(row.get("post_tree_digest") or "") ==
                        receipt["post_tree_digest"] and
                        row.get("resolution") == "completed"
                        for row in code.get("receipts") or [])
                    if not already and not sessions.append_run_receipt(
                            code["session_id"], receipt, limit=128,
                            directory=code["session_dir"]):
                        self.store.release_reconcile(mid, reconcile_token)
                        return {**self.status(mid),
                                "error": "could not persist the code reconciliation receipt"}
                    confirmed_session = sessions.load_checked(
                        code["session_id"], directory=code["session_dir"])
                    active = ((confirmed_session.get("session") or {}).get(
                        "active_run") if confirmed_session.get("status") == "ok"
                              else None)
                    if (confirmed_session.get("status") != "ok" or
                            (isinstance(active, dict) and active.get("state") in
                             ("executing_tool", "external_action"))):
                        self.store.release_reconcile(mid, reconcile_token)
                        return {**self.status(mid),
                                "error": "code session changed during reconciliation; inspect again"}
                    # The filesystem is not transactionally lockable with the
                    # journals.  Detect a concurrent edit before publishing the
                    # recorded digest into Mission state; the WAL receipt lets a
                    # later explicit reconciliation continue from this boundary.
                    from .verification import workspace_snapshot
                    confirmed = workspace_snapshot(code["workspace"])
                    if (not confirmed.get("snapshot_complete") or
                            str(confirmed.get("tree_digest") or "") !=
                            code.get("current")):
                        self.store.release_reconcile(mid, reconcile_token)
                        return {**self.status(mid),
                                "error": "workspace changed during reconciliation; inspect again"}
                    case_updates["code_expected_tree_digest"] = code["current"]
                if not self._patch_code_recovery_owned(
                        mid, reconcile_token, code.get("case_expected"), case_updates):
                    self.store.release_reconcile(mid, reconcile_token)
                    return {**self.status(mid),
                            "error": "code recovery state changed concurrently; inspect before retrying"}
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

    def _cancel_code_worker(self, mid):
        """Terminate one Mission's local or cross-process code worker."""
        if self._code_process is None:
            from .codeworker import CodeSliceProcessRunner
            runner = CodeSliceProcessRunner(
                session_dir=os.path.join(
                    self._state_dir, "mission-code-sessions"))
        else:
            runner = self._code_process
        return runner.cancel_current(mid)

    def _cancel_model_worker(self, mid):
        """Terminate this Mission's active provider transport, when supported."""
        owner = getattr(self._decider, "provider", None) if self._decider else None
        owner = owner or self._prov
        return MissionDriver._cancel_call(owner, mid) if owner is not None else False

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
        # Lifecycle fencing wins first; then terminate the owned OS tree before
        # the cancel call returns.  A Web request may be in another process from
        # jobd, so CodeSliceProcessRunner also consults its durable/named owner.
        try:
            if self._cancel_model_worker(mid):
                self.store.record_event(
                    mid, "control", "model_process_cancelled",
                    payload={"scope": "mission"})
        except Exception as exc:
            self.store.record_event(
                mid, "control", "model_process_cancel_failed",
                payload={"error": "%s: %s" % (type(exc).__name__, exc)})
        try:
            if self._cancel_code_worker(mid):
                self.store.record_event(
                    mid, "control", "code_process_cancelled",
                    payload={"cross_process": self._code_process is None})
        except Exception as exc:
            self.store.record_event(
                mid, "control", "code_process_cancel_failed",
                payload={"error": "%s: %s" % (type(exc).__name__, exc)})
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
        if m.state != NEEDS_YOU or nonce:
            return {**self.status(mid), "error": f"cannot accept from {m.state}"}
        blocked = self._agent_completion_guard(mid, m)
        if blocked:
            reason = (blocked.get("reason") if isinstance(blocked, dict) else str(blocked))
            return {
                **self.status(mid),
                "error": "cannot accept while delegated work is unsettled: %s" %
                         str(reason or "unfinished delegated work remains")[:500],
            }
        if not self.store.accept_handoff(mid):
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
        self._sync_terminal_mission_tree(mid)
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
            self._inherit_execution_contract(m, case)
            successor = "msn_" + secrets.token_hex(6)
            create_mission(self.store, successor, m.goal, case=case, leash=dict(m.leash))
            if not self._bind_successor_workspace(successor):
                return self.status(successor)
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
            self._activate_execution_profile(m)
            specialist = self._specialist_run(mid)
            if specialist:
                self._run_tree.requeue_waiting(specialist["run_id"])
                self._tick_specialists(int(time.time()))
            else:
                self._driver().wake(mid, force=True)
        except Exception as e:
            return {**self.status(mid), "error": f"check unavailable: {e}"}
        self._sync_terminal_mission_tree(mid)
        return self.status(mid)

    def tick(self, mid: str = None, now=None) -> dict:
        """Fire any due durable re-checks now (the 'check inbox now' button; also
        what colliejobd calls on wake)."""
        import time
        at = int(now if now is not None else time.time())
        profile_target = None
        root_profile_mission = None
        profile_routes = 0
        if mid:
            selected = self.store.get(mid)
            if selected:
                try:
                    self._activate_execution_profile(selected)
                except RuntimeError as exc:
                    return {**self.status(mid), "error": "tick unavailable: %s" % exc}
        else:
            # A global dispatcher may share one provider runtime across several
            # claimed rows.  Activate a frozen route only when all ready frozen
            # Missions agree; otherwise stop before any model call rather than
            # running one of them on another Mission's billing route.
            ready = list(self.store.list(state=QUEUED))
            due_ids = {row[0] if isinstance(row, (list, tuple)) else
                       row.get("mission_id") for row in self.store.due_waits(at)}
            ready.extend(m for m in self.store.list() if m.mission_id in due_ids)
            profiles = {}
            for item in ready:
                profile = (item.case or {}).get("execution_profile")
                if isinstance(profile, dict):
                    key = (_canonical_provider(profile.get("provider")),
                           str(profile.get("model") or "").strip(),
                           bool(profile.get("subscription_only")))
                    profiles.setdefault(key, item)
            if len(profiles) > 1:
                # One MissionService owns one live provider runtime, but the
                # daemon may supervise Missions frozen to different products.
                # Dispatch the least-recently-updated row explicitly, then let
                # the next daemon tick choose another route.  Never claim a
                # mixed batch under whichever provider happened to be warm.
                unique = {item.mission_id: item for item in ready}
                profile_target = min(
                    unique.values(), key=lambda item: (item.updated_at, item.mission_id))
                profile_routes = len(profiles)
            elif profiles:
                try:
                    root_profile_mission = next(iter(profiles.values()))
                    self._activate_execution_profile(root_profile_mission)
                except RuntimeError as exc:
                    return {"advanced": 0, "error": "tick unavailable: %s" % exc}
        usage_reconciliation = self._reconcile_tasktree_usage()
        # A code child can outlive the daemon process that owned the Mission
        # lease (notably a POSIX process group).  Terminate it while the stale
        # Mission row is still write-locked and owned; only then clear the token
        # or expose a safe model-only boundary as QUEUED.
        recovered = self.store.recover_stale_runs(
            at, before_transition=self._cancel_code_worker)
        escalations = self.store.escalate_human_waits(at)
        specialists = 0
        parent_wakes = {"normal": 0, "specialists": 0}
        # One child can wake a waiting specialist which then completes and wakes
        # its own parent. Depth is leash-bounded; four passes cover the default
        # graph while retaining a hard dispatcher bound.
        for _ in range(4):
            specialists += self._tick_specialists(at)
            woke = self._wake_parents_with_child_results()
            parent_wakes["normal"] += woke["normal"]
            parent_wakes["specialists"] += woke["specialists"]
            if not woke["specialists"]:
                break
        self._sync_terminal_mission_trees()
        if not self.store.list(state=QUEUED) and not self.store.due_waits(at):
            if mid:
                return {**self.status(mid), "escalations": [e for e in escalations
                                                              if e["mission_id"] == mid]}
            return {"advanced": 0, "specialists_advanced": specialists,
                    "parents_resumed": parent_wakes,
                    "recovered": recovered,
                    "usage_reconciliation": usage_reconciliation,
                    "escalations": escalations}
        if profile_target is not None:
            before = self.store.get(profile_target.mission_id)
            if before and before.state == QUEUED:
                self.run(before.mission_id)
            elif before and before.state == WAITING:
                self.check(before.mission_id)
            after = self.store.get(profile_target.mission_id)
            advanced = int(bool(after and before and
                                (after.updated_at != before.updated_at or
                                 after.state != before.state)))
            self._sync_terminal_mission_trees()
            return {"advanced": advanced, "specialists_advanced": specialists,
                    "parents_resumed": parent_wakes, "recovered": recovered,
                    "usage_reconciliation": self._reconcile_tasktree_usage(),
                    "escalations": escalations,
                    "profile_routes_ready": profile_routes,
                    "profile_routed_mission": profile_target.mission_id}
        if root_profile_mission is not None:
            # Specialist batches may have selected another frozen route above.
            # Restore the root batch route immediately before its claims/model
            # calls; never rely on the service remaining warm in one profile.
            self._activate_execution_profile(
                self.store.get(root_profile_mission.mission_id) or root_profile_mission)
        n = self._driver().tick_missions(at, max_workers=self._mission_workers)
        usage_reconciliation = self._reconcile_tasktree_usage()
        self._sync_terminal_mission_trees()
        if mid:
            return {**self.status(mid), "escalations": [e for e in escalations
                                                         if e["mission_id"] == mid]}
        return {"advanced": n, "specialists_advanced": specialists,
                "parents_resumed": parent_wakes,
                "recovered": recovered,
                "usage_reconciliation": usage_reconciliation,
                "escalations": escalations}

    def _specialist_control(self, run_id, token):
        run = self._run_tree.get(run_id)
        child_mid = str((run or {}).get("mission_id") or "")
        child = self.store.get(child_mid) if child_mid else None
        if child and child.run_token:
            self._fold_child_results(child_mid, run_id, child.run_token)
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
        try:
            if not child_mid or not self.store.get(child_mid):
                self._run_tree.block(
                    run_id, token,
                    "specialist runner has no bound Mission/worktree", needs_you=True)
                return
            self._activate_execution_profile(self.store.get(child_mid))
            self._ensure_runtime()
            # Catch up any Mission accounting committed before an earlier process
            # died.  Reconcile the whole campaign (including the root Mission's
            # own usage) before the TaskTree ancestor budget gate.
            root_run = self._run_tree.get(run.get("root_run_id") or "") or run
            usage = self._reconcile_tasktree_usage(
                root_run.get("mission_id") or child_mid)
            if usage["errors"]:
                raise RuntimeError("usage reconciliation failed closed: %s" %
                                   usage["errors"][0]["error"])
            exhausted = list(dict.fromkeys(tuple(item) for item in usage["exhausted"]))
            budget = "; ".join("%s: %s" % item for item in exhausted) or \
                self._run_tree.budget_reason(run_id)
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
                state = driver.wake(
                    child_mid,
                    force=(self._run_tree.has_child_results(run_id, child_mid) or
                           self._run_tree.has_messages(run_id, "steer")))
            elif child.state == NEEDS_YOU:
                _name, nonce = self.store.last_parked(child_mid)
                record = self.actions.get(nonce) if nonce else None
                state = driver.resume(child_mid) if record and record.state == APPROVED \
                    else child.state
            else:
                state = child.state
            exhausted = self._project_mission_usage(child_mid, run_id)
            current_run = self._run_tree.get(run_id) or {}
            if (current_run.get("status") == "cancel_requested" or
                    current_run.get("cancel_requested")):
                self._cancel_record(
                    child_mid,
                    "cancelled at specialist execution boundary; no new code action started",
                    user_requested=False,
                    parent_mission_id=str((self.store.get(child_mid).case or {}).get(
                        "_parent_mission_id") or ""))
                self._run_tree.cancel_owned(
                    run_id, token, "cancelled at specialist execution boundary")
                return
            if exhausted and state not in (DONE_VERIFIED, DONE_ACCEPTED, FAILED_S, CANCELLED):
                self._run_tree.block(
                    run_id, token,
                    "; ".join("%s: %s" % item for item in exhausted), needs_you=True)
            elif state in (DONE_VERIFIED, DONE_ACCEPTED):
                child_record = self.store.get(child_mid)
                if not self._run_tree.complete(
                        run_id, token, child_record.result,
                        artifacts=self._specialist_artifacts(run, child_record),
                        observation={"mission_state": state,
                                     "verified": state == DONE_VERIFIED,
                                     "accepted": state == DONE_ACCEPTED}):
                    self._run_tree.block(
                        run_id, token, "TaskCompleted hook blocked specialist completion",
                        needs_you=True)
            elif state == FAILED_S:
                self._run_tree.fail(run_id, token, self.store.get(child_mid).result)
                self._cancel_linked_descendant_missions(
                    child_mid, run_id,
                    "cancelled because ancestor Mission %s failed" % child_mid)
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
            if child_mid and self.store.get(child_mid):
                try:
                    # Idempotent absolute reconciliation also covers a driver
                    # exception after Mission accounting committed.
                    self._project_mission_usage(child_mid, run_id)
                except Exception as exc:
                    self._run_tree.mark_recovery(
                        run_id, token, "usage projection failed: %s: %s" %
                        (type(exc).__name__, exc))
            stop.set()
            beat.join(timeout=2)

    def _reconcile_specialist_orphans(self, limit):
        """Repair bounded spawn crash windows before any specialist claim."""
        if self._run_tree is None:
            return 0
        from .tasktree import (CANCEL_REQUESTED as T_CANCEL_REQUESTED,
                               CANCELLED as T_CANCELLED, COMPLETED as T_COMPLETED,
                               FAILED as T_FAILED, WORKSPACE_REQUIRED)
        repaired = 0
        candidates = []
        for run in self._run_tree.list_runs(specialists_only=True):
            if (run.get("status") in (T_COMPLETED, T_FAILED, T_CANCELLED,
                                      T_CANCEL_REQUESTED) or
                    run.get("cancel_requested")):
                continue
            if (run.get("status") == WORKSPACE_REQUIRED or
                    (run.get("workspace") and
                     (not run.get("mission_id") or
                      not self.store.get(run.get("mission_id") or "")))):
                candidates.append(run)
            if len(candidates) >= max(1, int(limit)):
                break
        for candidate in candidates:
            run_id = candidate["run_id"]
            phase = "workspace" if candidate.get("status") == WORKSPACE_REQUIRED \
                else "mission"
            try:
                run = self._run_tree.get(run_id) or candidate
                parent = self._run_tree.get(run.get("parent_run_id") or "") or {}
                parent_mid = str(parent.get("mission_id") or "")
                if run.get("status") == WORKSPACE_REQUIRED:
                    parent_workspace = str(parent.get("workspace") or "")
                    if not parent_workspace:
                        raise ValueError("parent workspace is unavailable for worktree recovery")
                    prepared = self._run_tree.provision_worktree(
                        run_id, parent_workspace)
                    if prepared.get("busy"):
                        continue
                    if not prepared.get("ok"):
                        raise ValueError(str(
                            prepared.get("error") or
                            "isolated specialist worktree could not be recovered"))
                    run = prepared.get("run") or self._run_tree.get(run_id)
                    phase = "mission"
                if not run or not run.get("workspace"):
                    raise ValueError("specialist workspace recovery did not bind a workspace")
                if (not run.get("mission_id") or
                        not self.store.get(run.get("mission_id") or "")):
                    run = self._create_specialist_mission(parent_mid, run)
                if not run or not run.get("mission_id"):
                    raise ValueError("specialist Mission recovery did not bind a Mission")
                repaired += 1
            except Exception as exc:
                self._run_tree.mark_orphan_needs_you(
                    run_id, "specialist orphan recovery failed: %s: %s" %
                    (type(exc).__name__, exc), phase=phase)
        return repaired

    def _tick_specialists(self, now):
        """Claim and actually execute scoped child Missions; never strand queued rows."""
        if self._run_tree is None:
            return 0
        from .tasktree import (BLOCKED as T_BLOCKED, NEEDS_YOU as T_NEEDS_YOU,
                               PAUSED as T_PAUSED, QUEUED as T_QUEUED,
                               RECOVERY_REQUIRED as T_RECOVERY, WAITING as T_WAITING)
        workers = self._specialist_workers if self._specialist_workers is not None else \
            int(os.environ.get("COLLIE_SPECIALIST_WORKERS", "4"))
        workers = max(1, min(8, int(workers)))
        self._reconcile_specialist_orphans(workers)
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
        # A service has one live provider runtime.  Run only one frozen route per
        # dispatch batch, but leave other specialist rows queued for the next
        # pass; this prevents parallel children from racing provider/model/auth
        # mutation on the shared MissionService.
        selected_route = None
        selected_mission = None
        for run in queued:
            child = self.store.get(run.get("mission_id") or "")
            profile = (child.case or {}).get("execution_profile") if child else None
            if isinstance(profile, dict):
                route = (_canonical_provider(profile.get("provider")),
                         str(profile.get("model") or "").strip(),
                         bool(profile.get("subscription_only")))
            else:
                route = (_canonical_provider(self._provider), str(self._model or ""),
                         bool(self._subscription_only))
            if selected_route is None:
                selected_route, selected_mission = route, child
            if route != selected_route:
                continue
        if selected_mission and isinstance(
                (selected_mission.case or {}).get("execution_profile"), dict):
            self._activate_execution_profile(selected_mission)
            self._ensure_runtime()
        matching = []
        for run in queued:
            child = self.store.get(run.get("mission_id") or "")
            profile = (child.case or {}).get("execution_profile") if child else None
            route = ((_canonical_provider(profile.get("provider")),
                      str(profile.get("model") or "").strip(),
                      bool(profile.get("subscription_only")))
                     if isinstance(profile, dict) else
                     (_canonical_provider(self._provider), str(self._model or ""),
                      bool(self._subscription_only)))
            if route == selected_route:
                matching.append(run)
        claimed = []
        for run in matching[:workers]:
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
        usage_reconciliation = self._reconcile_tasktree_usage(mid)
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
        aggregate_runtime = self.store.aggregate_runtime(mid)
        activity = self.store.activity_ledger(mid, 24)
        checkpoint = self.store.latest_checkpoint(mid)
        run_tree = None
        if self._run_tree and m.case.get("_run_id"):
            run_tree = self._run_tree.tree(m.case["_run_id"])
        pending_hooks = list(getattr(self._hooks, "pending", ()) or ())
        code_session_recovery = None
        code_session_id = str((m.case or {}).get("code_session_id") or
                              ((m.case or {}).get("code_profile") or {}).get(
                                  "session_id") or "")
        if m.state in (RECOVERY_REQUIRED, RECONCILING) and code_session_id:
            inspected = self._inspect_code_recovery(m)
            if inspected.get("invalid"):
                code_session_recovery = {
                    "recovery_required": True,
                    "reason": str(inspected.get("error") or "")[:500],
                    "allowed_resolutions": ["cancel"],
                }
            elif inspected.get("requires_resolution"):
                reason = (
                    "workspace bytes differ from the last published Collie boundary"
                    if inspected.get("drift") else
                    "the interrupted code run requires an explicit inspected outcome")
                code_session_recovery = {
                    "recovery_required": True,
                    "reason": reason,
                    "allowed_resolutions": ["completed", "not_fired", "cancel"],
                }
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
        summary = _mission_summary(
            m, steps, receipts, runtime, inbox, next_wait, activity)
        return {
            "mission_id": mid, "goal": m.goal, "state": m.state, "result": m.result,
            "created_at": m.created_at, "updated_at": m.updated_at,
            "case": _clean(m.case),
            "summary": summary,
            "report": _mission_report(m, summary, activity, receipts, runtime),
            "steps": steps,
            "activity": activity,
            "recent_events": self.store.events(mid, 20),
            "inbox": inbox,                       # non-null -> render a Confirm button
            "needs_human": (m.state == NEEDS_YOU and inbox is None and
                            not action_in_flight),  # -> Accept hand-off
            "action_in_flight": action_in_flight,
            "next_wake_at": next_wait["fire_at"] if next_wait else None,
            "runtime": runtime,
            "aggregate_runtime": aggregate_runtime,
            "usage_projection_errors": usage_reconciliation["errors"],
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
            "code_session_recovery": code_session_recovery,
            "recovery_actions": recovery_actions,
            "receipts": receipts,
        }

    def report(self, mid: str) -> dict:
        """Integration-safe Mission progress report without raw case or action args."""
        status = self.status(mid)
        if status.get("error"):
            return status
        return status.get("report") or {"error": "progress report unavailable"}

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
        if self._code_process is not None:
            # A graceful daemon restart must not orphan its own editor process.
            # Do not scan cross-process receipts here: another live service may
            # legitimately own a different Mission in the same state directory.
            self._code_process.cancel_current(include_persisted=False)
        provider = getattr(self._decider, "provider", None) if self._decider else None
        provider = provider or self._prov
        cancel_model = getattr(provider, "cancel_current", None)
        if callable(cancel_model):
            cancel_model()
        self.store.close()
        self.actions.close()
        # Injected stores belong to their caller and may be shared by the Web,
        # daemon, and tests.  Only close the durable backend we constructed.
        if self._owns_run_tree and self._run_tree is not None:
            self._run_tree.close()
        if self._owns_hooks and hasattr(self._hooks, "close"):
            self._hooks.close()
