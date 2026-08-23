"""Pick the harness that will carry out one task — as a pure function of evidence.

Collie can do the work itself (``collie``) or hand it to an external coding-agent
CLI (``codex-exec``, ``claude-code``, ...).  Which one runs decides whose tool
loop, whose sandbox, whose approval model and — the part that cannot be undone —
*whose login pays*.  So the choice is made here, in one place, by a function that
does no IO at all: it takes a :class:`~harness.runner_specs.HarnessRequest`, the
static specs, the probes the registry already gathered, and returns a
:class:`~harness.runner_specs.HarnessDecision` that can be replayed from a
receipt months later and produce the same answer.

Four properties are load-bearing, and each one shows up as a rule below:

* **The default path costs nothing.**  ``RUNNER=collie`` with no ``--runner``
  makes :meth:`HarnessRequest.candidates` return ``("collie",)``, so the caller
  probes nothing and today's behaviour is unchanged bit-for-bit.  The test
  ``test_default_runner_collie_skips_probe`` pins that.
* **An explicit choice is never substituted.**  A pin (``--runner X``, a roster
  member) or a configured ``RUNNER=X`` that turns out to be unusable produces
  ``error`` and an empty ``runner``; the caller must refuse to run.  Quietly
  running the task on a different account is exactly the failure this layer
  exists to prevent, so there is no "helpful" downgrade.
* **Auto only ever reaches into ``RUNNER_POOL``.**  Writing an external worker
  into the pool *is* the consent to use its billing route; a worker that is not
  in the pool is never picked, however well installed and logged in it is.
* **Collie is the floor.**  Hard rules H1/H2/H4/H6/H8–H11 exist to constrain
  *external* workers, so they are not applied to Collie's own harness — the one
  candidate that is always available.  Only H5 (billing) can reject Collie, and
  that is the existing subscription preflight's job, not a substitution.

Unknown is never optimistic here.  A runner whose billing route cannot be
evidenced is ``unknown`` and loses the no-paid-overage rules; unknown quota
scores ``0`` in the soft sort (not ``0.5``) so an unmeasured runner cannot ride a
constant term to the top.

Phase 1 has no usage signals: ``signals=None`` skips H7 and leaves the two signal
terms at their unknown values.  The interface is already here so phase 2 wires
:mod:`harness.usage_signals` in without touching the rule order.
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .runner_specs import (
    CURRENT_PHASE,
    EXTERNAL_ALLOWED_SURFACES,
    CandidateScore,
    HarnessDecision,
    HarnessRequest,
    HarnessSpec,
    RunnerCapabilities,
    RunnerProbe,
    family_of_provider,
    stable_digest,
)


# --- soft-sort weights ------------------------------------------------------
# Sum = 1.0.  Order matters only for reading; the terms are independent.  The two
# signal-fed terms (route_health, headroom) keep their weight in phase 1 even
# though they are unknown, so turning signals on in phase 2 changes rankings
# rather than re-scaling every score that was ever recorded in a receipt.
_WEIGHTS: dict[str, float] = {
    "pool_order": 0.20,      # where the operator put it in RUNNER_POOL
    "route_health": 0.20,    # auth ok and no recent 429           (signals, phase 2)
    "headroom": 0.20,        # quota left on the route             (signals, phase 2)
    "history": 0.15,         # Laplace-smoothed verified rate on this project
    "fit": 0.10,             # declared capabilities we actually use
    "billing_pref": 0.10,    # prefer an included route when a budget is set
    "native_bias": 0.05,     # ties go to Collie's own harness
}

# Billing classes that cannot produce a surprise invoice.
_SAFE_BILLING = frozenset({"subscription_allowance", "local"})

# Mirrors subscription_guard.CODEX_EVIDENCE_MAX_AGE_SECONDS (:27).  Duplicated
# rather than imported: that module pulls in subprocess/plat and this one is on
# the default ``RUNNER=collie`` import path, where it must stay cheap.
_CODEX_EVIDENCE_MAX_AGE_S = 15 * 60.0

# Phase-2 signal thresholds; settings key RUNNER_QUOTA_GUARD overrides the first.
_DEFAULT_QUOTA_GUARD = 90
_RECENT_429_LIMIT = 3
_COOLDOWN_429_S = 15 * 60.0

# Below this many recorded runs the history term is shrunk to "no opinion".
_HISTORY_MIN_RUNS = 3

_DIGITS = re.compile(r"\d+")


# --- small helpers ----------------------------------------------------------
def _setting(settings: Any, key: str, default: str = "") -> str:
    """Read one setting from the settings module *or* a plain dict (tests)."""
    if settings is None:
        return default
    getter = getattr(settings, "get", None)
    if callable(getter):
        value = getter(key, default)
    else:                                    # pragma: no cover - defensive
        value = default
    return "" if value is None else str(value)


def _positive(value: str) -> float | None:
    """``"0"``/``""``/garbage -> None.  A zero budget means "no limit" here."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _version_key(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in _DIGITS.findall(str(text or ""))[:4])


def _is_older(version: str, minimum: str) -> bool:
    """True only when both sides parse and ``version`` is genuinely lower.

    An unparseable version is not evidence of being too old — the probe already
    records it verbatim, and rejecting on a formatting difference would take a
    working runner away for no reason.
    """
    left, right = _version_key(version), _version_key(minimum)
    if not left or not right:
        return False
    return left < right


def _effective_caps(spec: HarnessSpec, probe: RunnerProbe | None) -> RunnerCapabilities:
    """Declared capabilities ∧ what the last conformance report confirmed.

    ``probe.capabilities`` is written by ``runner_registry`` as the *already
    intersected* view, so it wins when present.  Keys the probe does not carry
    (or extra bookkeeping keys such as ``double_control``) leave the declared
    value alone.
    """
    if probe is None or not probe.capabilities:
        return spec.caps
    merged = spec.caps.to_dict()
    for name, value in probe.capabilities.items():
        if name in merged:
            merged[name] = value
    return RunnerCapabilities.from_dict(merged)


def _is_native(key: str, spec: HarnessSpec | None) -> bool:
    return key == "collie" or (spec is not None and spec.kind == "native")


def _explicit(req: HarnessRequest) -> bool:
    """The operator named this worker (``--runner`` / ``RUNNER=<key>`` / roster).

    Explicit choices are held to the same safety rules but are trusted on the
    *unverified* ones (H10): the person asking for it can see the warning in the
    reasons, whereas Auto must not gamble on an unverified host silently.
    """
    return bool(req.pin) or (req.configured or "collie") != "auto"


def _family_of(req: HarnessRequest, spec: HarnessSpec | None) -> str:
    """A spec with no credential family follows the configured PROVIDER (collie)."""
    declared = (spec.credential_family if spec else "") or ""
    if declared:
        return declared
    return req.provider_family or family_of_provider(req.provider)


def _synth_collie_probe() -> RunnerProbe:
    """Collie's own harness is in-process: installed by definition.

    Only used when a caller hands us probes that do not include ``collie`` —
    the auto path must still be able to fall back onto it.
    """
    return RunnerProbe(key="collie", installed=True, login="n/a",
                       detail="probe not supplied; collie runs in-process")


# --- D.3 hard filters -------------------------------------------------------
def _hard_reasons(req: HarnessRequest, key: str, spec: HarnessSpec | None,
                  probe: RunnerProbe | None, caps: RunnerCapabilities,
                  signals: Any, now: float) -> tuple[list[str], list[str]]:
    """Run H1–H11 in fixed order; return (rejections, soft notes).

    Every rejection is ``"H#: detail"`` so the receipt says which rule fired, not
    just that something did.  All rules are evaluated (not short-circuited) so
    the candidate worksheet shows every reason it lost; ``rejected[key]`` keeps
    the first, which is the earliest — and therefore most fundamental — one.
    """
    hard: list[str] = []
    soft: list[str] = []
    if spec is None:
        return (["H3: unknown runner (not in the registry)"], soft)
    if probe is None:
        return (["H3: no probe evidence for %s" % key], soft)

    native = _is_native(key, spec)
    external = not native

    # H1 — surface.  repl/tui/acp re-decide the route every turn, slack has no
    # approver, loop feeds res.messages back in; none of that survives handing
    # the turn to a foreign CLI.
    if external:
        allowed = EXTERNAL_ALLOWED_SURFACES.get(
            req.phase, EXTERNAL_ALLOWED_SURFACES[max(EXTERNAL_ALLOWED_SURFACES)])
        if req.surface not in allowed:
            hard.append("H1: surface %r cannot use an external worker in phase %d"
                        % (req.surface, req.phase))

    # H2 — read-only intents.  Plan/review are enforced by Collie's own gate;
    # an external CLI has no way to promise it only read.
    if external and (req.intent in ("plan", "review") or req.route_kind == "chat"):
        hard.append("H2: %s runs under Collie's read-only gate"
                    % (req.intent if req.intent in ("plan", "review") else "chat"))

    # H3 — phase gate, probe, minimum version.  The phase gate applies to every
    # candidate; installed/login/version are questions only an external binary
    # can answer, and asking them of the in-process harness could take the
    # default path away over an unrelated provider credential.
    if spec.phase > req.phase:
        hard.append("H3: arrives in phase %d (this host is phase %d)"
                    % (spec.phase, req.phase))
    elif external and not probe.usable():
        hard.append("H3: %s" % _unusable_detail(probe))
    elif external and spec.min_version and _is_older(probe.version, spec.min_version):
        hard.append("H3: version %s is older than the required %s"
                    % (probe.version, spec.min_version))

    if external:
        # H4 — tool needs.  browser/desktop/mcp/web_search/email/slack live in
        # Collie's registry only; an external worker gets at most code+bash.
        missing = sorted(set(req.needs) - set(caps.tools))
        if missing:
            hard.append("H4: does not provide %s" % ", ".join(missing))

    # H5 — billing.  Unknown is rejected, never assumed included.
    hard.extend(_billing_reasons(req, key, spec, probe, caps, now))

    if external:
        # H6 — workspace.  Change detection is git-derived, so a non-repo
        # workspace is a real (but sometimes acceptable) loss of evidence.
        if not req.workspace:
            hard.append("H6: no workspace to work in")
        elif caps.needs_git_workspace and req.workspace_is_git is False:
            if req.surface in ("mission-code", "pack"):
                hard.append("H6: %s needs a git workspace (use --workspace isolated "
                            "or a worktree)" % req.surface)
            else:
                soft.append("workspace is not a git repository: change detection is "
                            "incomplete")

        # H7 — live route signals.  Phase 1 has none, and unknown never rejects.
        if signals is not None:
            hard.extend(_signal_reasons(req, signals, now))

        # H8 — unattended and durable execution.
        if req.overnight:
            hard.append("H8: overnight runs stay on Collie's own harness "
                        "(no per-request reservation outside it)")
        if req.surface == "mission-code" and not caps.session_resume:
            hard.append("H8: a durable Mission slice needs session_resume")

        # H9 — approvals.  A worker that cannot round-trip an approval may only
        # run when something else bounds it, and never when the operator asked
        # to approve each action personally.
        if "bash" in req.needs and not caps.approval_round_trip:
            if caps.confinement not in ("workspace-write", "container"):
                hard.append("H9: runs commands without approval round-trip and "
                            "without confinement (%s)" % (caps.confinement or "none"))
        if (req.gate_mode == "interactive" and req.has_approver
                and not caps.approval_round_trip):
            hard.append("H9: interactive approval was requested but this worker "
                        "cannot ask")

        # H10 — platform.  False = the compat report says it does not work here;
        # None = nobody has checked, which Auto must not gamble on.
        if req.os_name == "nt" and caps.windows_native is not True:
            if caps.windows_native is False:
                hard.append("H10: not supported natively on Windows")
            elif _explicit(req):
                soft.append("windows support is unverified on this host")
            else:
                hard.append("H10: windows support is unverified (Auto will not "
                            "gamble; name it with --runner to try anyway)")

        # H11 — double control plane.  A runner with its own goals/scheduler may
        # only run once conformance has PROVEN that plane is switched off.
        if caps.native_goal or caps.native_scheduler:
            verdict = str(probe.capabilities.get("double_control") or "")
            if verdict.upper() != "PASS":
                hard.append("H11: has its own goal/scheduler plane and "
                            "double_control is %s" % (verdict or "unverified"))

    return (hard, soft)


def _unusable_detail(probe: RunnerProbe) -> str:
    """Why ``probe.usable()`` said no, in the operator's words."""
    if probe.detail:
        return probe.detail
    if not probe.installed:
        return "not installed"
    return "login is %s" % (probe.login or "unknown")


def _billing_reasons(req: HarnessRequest, key: str, spec: HarnessSpec,
                     probe: RunnerProbe, caps: RunnerCapabilities,
                     now: float) -> list[str]:
    """H5 — the rules that keep a run on the account the operator consented to."""
    out: list[str] = []
    if req.no_paid_overage or req.subscription_only:
        if probe.billing_class not in _SAFE_BILLING:
            out.append("H5: billing class %s cannot be run under no-paid-overage"
                       % probe.billing_class)
        elif not probe.overage_attested:
            out.append("H5: no operator attestation that this route has no paid "
                       "overage")
        elif not probe.billing_evidence:
            out.append("H5: no billing evidence for this route")
        elif _stale_evidence(req, spec, probe, now):
            out.append("H5: billing evidence is older than %d minutes"
                       % int(_CODEX_EVIDENCE_MAX_AGE_S // 60))

    # A token ceiling we cannot measure is not a ceiling.
    if req.max_total_tokens and not caps.usage_tokens:
        out.append("H5: a token budget is set but this worker reports no usage")
    # A dollar ceiling is only meaningless when the route can actually bill.
    if (req.max_cost_usd and not caps.usage_tokens
            and probe.billing_class not in _SAFE_BILLING):
        out.append("H5: a cost budget is set but this worker reports no usage on a "
                   "%s route" % probe.billing_class)
    return out


def _stale_evidence(req: HarnessRequest, spec: HarnessSpec, probe: RunnerProbe,
                    now: float) -> bool:
    """Codex login evidence expires (subscription_guard :27); reuse that bound.

    Only applied to the codex family, and only when the evidence carries a
    timestamp — an undated evidence dict is already rejected above by being
    checked for emptiness, and inventing an age for it would be fiction.
    """
    if _family_of(req, spec) != "codex":
        return False
    observed = probe.billing_evidence.get("observed_at")
    if observed is None:
        observed = probe.probed_at or 0.0
    try:
        observed = float(observed)
    except (TypeError, ValueError):
        return False
    if observed <= 0 or not now:
        return False
    return (now - observed) > _CODEX_EVIDENCE_MAX_AGE_S


def _signal_reasons(req: HarnessRequest, signals: Any, now: float) -> list[str]:
    """H7 — phase-2 route signals.  Duck-typed so a partial snapshot cannot crash.

    Auth problems reject anywhere; cooldown/quota problems reject Auto only — a
    pinned worker belongs to the person who typed its name, and refusing it here
    would substitute our judgement for theirs without an alternative.
    """
    out: list[str] = []
    auth = str(getattr(signals, "auth_status", "") or "")
    if auth in ("expired", "missing-key", "not-logged-in"):
        out.append("H7: auth status is %s" % auth)

    budget = getattr(signals, "mission_budget", None)
    if budget is not None and getattr(budget, "remaining_calls", None) == 0:
        out.append("H7: the mission budget has no model calls left")

    if _explicit(req):
        return out

    cooldown = getattr(signals, "cooldown_until", None)
    if cooldown and now and float(cooldown) > now:
        out.append("H7: route is in cooldown for another %d s"
                   % int(float(cooldown) - now))
    if int(getattr(signals, "recent_429", 0) or 0) >= _RECENT_429_LIMIT:
        out.append("H7: %s recent rate-limit responses"
                   % getattr(signals, "recent_429", 0))
    quota = getattr(signals, "quota", None)
    if quota is not None:
        guard = int(getattr(signals, "quota_guard", 0) or _DEFAULT_QUOTA_GUARD)
        used = _worst_used_percent(quota)
        if used is not None and used >= guard:
            out.append("H7: quota is %d%% used (guard %d%%)" % (used, guard))
        if getattr(quota, "rate_limit_reached_type", None):
            out.append("H7: provider reports rate limit %s reached"
                       % getattr(quota, "rate_limit_reached_type"))
    return out


def _worst_used_percent(quota: Any) -> int | None:
    """The tighter of the two quota windows, or None when neither is known."""
    values = []
    for name in ("primary", "secondary"):
        window = getattr(quota, name, None)
        used = getattr(window, "used_percent", None) if window is not None else None
        if used is not None:
            values.append(int(used))
    return max(values) if values else None


# --- D.4 soft ranking -------------------------------------------------------
def _score(req: HarnessRequest, key: str, probe: RunnerProbe,
           caps: RunnerCapabilities, signals: Any,
           now: float) -> tuple[float, dict[str, float], list[str]]:
    terms: dict[str, float] = {}
    notes: list[str] = []

    pool = [item for item in req.pool if item]
    if key in pool:
        terms["pool_order"] = 1.0 - (pool.index(key) / float(len(pool)))
    else:
        # Collie is always available whether or not it was written down; anything
        # else that is not in the pool has no consent and scores nothing.
        terms["pool_order"] = 0.5 if key == "collie" else 0.0

    terms["route_health"] = _route_health(signals, now)

    headroom, unknown_quota = _headroom(signals)
    terms["headroom"] = headroom
    if unknown_quota:
        notes.append("quota: unknown")

    terms["history"] = _history_term(signals)

    fit = 0.0
    for capable in (caps.steer, caps.session_resume,
                    caps.cancel == "native+process-tree", caps.usage_tokens):
        if capable:
            fit += 0.25
    terms["fit"] = fit

    if req.no_paid_overage or (req.max_cost_usd or 0):
        terms["billing_pref"] = 1.0 if probe.billing_class in _SAFE_BILLING else 0.0
    else:
        # No budget constraint: the term has nothing to say, and saying it at 0.5
        # for everyone keeps it from tipping a comparison it was not asked about.
        terms["billing_pref"] = 0.5

    terms["native_bias"] = 1.0 if key == "collie" else 0.0

    score = sum(_WEIGHTS[name] * value for name, value in terms.items())
    return (score, terms, notes)


def _route_health(signals: Any, now: float) -> float:
    if signals is None:
        return 0.5
    auth = str(getattr(signals, "auth_status", "") or "unknown")
    recent = int(getattr(signals, "recent_429", 0) or 0)
    evidence = getattr(signals, "rate_limit", None)
    observed = getattr(evidence, "observed_at", None) if evidence is not None else None
    if auth != "ok":
        return 0.5
    if not recent:
        return 1.0
    if observed and now and (now - float(observed)) > _COOLDOWN_429_S:
        return 0.5
    return 0.0


def _headroom(signals: Any) -> tuple[float, bool]:
    """Unknown quota is 0, not 0.5.

    A half-point for "we have no idea" is a constant that quietly promotes every
    unmeasured runner; an unmeasured route simply earns nothing on this axis and
    says so in the reasons.
    """
    quota = getattr(signals, "quota", None) if signals is not None else None
    used = _worst_used_percent(quota) if quota is not None else None
    if used is None:
        return (0.0, True)
    return (max(0.0, 1.0 - (used / 100.0)), False)


def _history_term(signals: Any) -> float:
    history = getattr(signals, "history", None) if signals is not None else None
    if history is None:
        return 0.5
    runs = int(getattr(history, "runs", 0) or 0)
    if runs < _HISTORY_MIN_RUNS:
        return 0.5                      # too few runs to have an opinion
    verified = int(getattr(history, "verified", 0) or 0)
    return (verified + 1) / float(runs + 2)     # Laplace


# --- the decision -----------------------------------------------------------
def decide(req: HarnessRequest, specs: Mapping[str, HarnessSpec],
           probes: Mapping[str, RunnerProbe], *, signals: Any = None,
           now: float | None = None) -> HarnessDecision:
    """Choose a runner for ``req``.  Pure: no IO, no probing, no settings read.

    ``specs``/``probes`` are whatever the registry already gathered — this
    function never asks for more, which is why the ``RUNNER=collie`` path can
    hand it a single probe (or none) and pay nothing.
    """
    now = time.time() if now is None else float(now)
    keys = req.candidates()

    scored: list[CandidateScore] = []
    rejected: dict[str, str] = {}
    eligible: list[str] = []
    probe_of: dict[str, RunnerProbe] = {}
    caps_of: dict[str, RunnerCapabilities] = {}
    soft_of: dict[str, list[str]] = {}

    for key in keys:
        spec = specs.get(key)
        probe = probes.get(key)
        if probe is None and key == "collie":
            probe = _synth_collie_probe()
        caps = _effective_caps(spec, probe) if spec is not None else RunnerCapabilities(protocol="")
        hard, soft = _hard_reasons(req, key, spec, probe, caps, signals, now)
        if probe is not None:
            probe_of[key] = probe
        caps_of[key] = caps
        soft_of[key] = soft
        if hard:
            rejected[key] = hard[0]
            scored.append(CandidateScore(key=key, eligible=False,
                                         hard_reasons=tuple(hard), score=0.0,
                                         terms={}, soft_reasons=tuple(soft)))
        else:
            eligible.append(key)

    auto = not req.pin and (req.configured or "collie") == "auto"
    ranked: list[tuple[float, dict[str, float], list[str], str]] = []
    if auto and len(eligible) > 1:
        for key in eligible:
            score, terms, notes = _score(req, key, probe_of[key], caps_of[key],
                                         signals, now)
            ranked.append((score, terms, notes, key))
        pool = [item for item in req.pool if item]

        def _order(row: tuple[float, dict[str, float], list[str], str]):
            key = row[3]
            index = pool.index(key) if key in pool else len(pool)
            return (-row[0], index, 0 if key == "collie" else 1, key)

        ranked.sort(key=_order)
        for score, terms, notes, key in ranked:
            scored.append(CandidateScore(key=key, eligible=True, hard_reasons=(),
                                         score=score, terms=terms,
                                         soft_reasons=tuple(soft_of[key] + notes)))
        chosen = ranked[0][3]
        soft_of[chosen] = soft_of[chosen] + ranked[0][2]
    else:
        for key in eligible:
            scored.append(CandidateScore(key=key, eligible=True, hard_reasons=(),
                                         score=0.0, terms={},
                                         soft_reasons=tuple(soft_of[key])))
        chosen = eligible[0] if eligible else ""

    probe_digest = stable_digest({key: probe.to_dict()
                                  for key, probe in sorted(probe_of.items())})
    signals_digest = ""
    if signals is not None:
        digest = getattr(signals, "digest", None)
        signals_digest = digest() if callable(digest) else stable_digest(
            getattr(signals, "to_dict", dict)())

    if not chosen:
        if not auto:
            return _refusal(req, keys, rejected, scored, probe_digest, signals_digest)
        # Auto: the pool filtered empty, so the task still runs — on Collie.
        # (Collie can only be rejected by H5, which the existing subscription
        # preflight is the right place to fail on, with its own evidence.)
        chosen = "collie"
        probe_of.setdefault("collie", probes.get("collie") or _synth_collie_probe())
        soft_of.setdefault("collie", [])
        soft_of["collie"] = soft_of["collie"] + ["every candidate was rejected"]

    spec = specs.get(chosen)
    probe = probe_of.get(chosen) or _synth_collie_probe()
    source = _source_for(req, chosen, auto, len(eligible))
    family = _family_of(req, spec)
    chain = _fallback_chain(req, chosen, auto, ranked, probe_of, spec, probe, specs)

    reasons = ["runner: %s (%s); family=%s billing=%s"
               % (chosen, source, family or "unknown", probe.billing_class)]
    for key in keys:
        if key in rejected:
            reasons.append("runner rejected %s: %s" % (key, rejected[key]))
    if chain:
        reasons.append("runner fallback chain: %s (same family/billing)"
                       % " → ".join((chosen,) + chain))
    for note in soft_of.get(chosen, []):
        reasons.append(note)

    return HarnessDecision(
        runner=chosen, source=source, credential_family=family,
        billing_class=probe.billing_class, billing_mode=probe.billing_mode,
        reasons=tuple(reasons), rejected=rejected, candidates=tuple(scored),
        fallback_chain=chain, probe=probe.to_dict(), probe_digest=probe_digest,
        signals_digest=signals_digest, error="")


def _refusal(req: HarnessRequest, keys: Sequence[str], rejected: dict[str, str],
             scored: Sequence[CandidateScore], probe_digest: str,
             signals_digest: str) -> HarnessDecision:
    """An explicitly named worker is unusable: refuse, never substitute."""
    key = keys[0] if keys else (req.pin or req.configured or "collie")
    detail = rejected.get(key) or "no candidate survived the hard rules"
    source = "user" if req.pin else "configured"
    if req.pin and req.surface == "pack":
        source = "roster"
    error = ("runner %s is not available: %s — an explicitly chosen worker is "
             "never swapped for another one (that would change which account "
             "pays); fix it or drop the --runner/RUNNER setting" % (key, detail))
    reasons = tuple("runner rejected %s: %s" % (name, rejected[name])
                    for name in keys if name in rejected)
    return HarnessDecision(
        runner="", source=source, credential_family="", billing_class="unknown",
        billing_mode="unconfigured", reasons=reasons, rejected=rejected,
        candidates=tuple(scored), fallback_chain=(), probe={},
        probe_digest=probe_digest, signals_digest=signals_digest, error=error)


def _source_for(req: HarnessRequest, chosen: str, auto: bool,
                survivors: int) -> str:
    """Who is responsible for this choice — the five values of D.2."""
    if req.pin:
        return "roster" if req.surface == "pack" else "user"
    if not auto:
        return "configured"
    if chosen == "collie" and survivors <= 1:
        return "safety-default"       # nothing external survived the filters
    return "task-policy"              # a real ranking happened


def _fallback_chain(req: HarnessRequest, chosen: str, auto: bool, ranked: Sequence,
                    probe_of: Mapping[str, RunnerProbe], spec: HarnessSpec | None,
                    probe: RunnerProbe, specs: Mapping[str, HarnessSpec]) -> tuple[str, ...]:
    """Runners we may switch to *before work starts* — same payer, same billing.

    Empty for a pin/configured/roster choice by construction: those never fall
    back.  Even under Auto the chain is narrow, because a fallback that crosses
    credential families or billing classes is the silent account switch this
    module exists to prevent.
    """
    if not auto or not chosen:
        return ()
    family = _family_of(req, spec)
    out: list[str] = []
    for row in ranked:
        key = row[3]
        if key == chosen:
            continue
        other = probe_of.get(key)
        if other is None:
            continue
        if _family_of(req, specs.get(key)) != family:
            continue
        if other.billing_class != probe.billing_class:
            continue
        out.append(key)
    return tuple(out)


# --- explanation ------------------------------------------------------------
def explain(decision: HarnessDecision) -> list[str]:
    """Human-readable lines, in the style of ``router.py`` reasons.

    The decision already carries its reasons; this adds the scoreboard (only
    interesting when a ranking actually happened) and the refusal, so a caller
    can print one block and have the whole story.
    """
    lines = list(decision.reasons)
    ranked = [item for item in decision.candidates if item.eligible and item.terms]
    if len(ranked) > 1:
        lines.append("runner scores: %s" % ", ".join(
            "%s %.3f" % (item.key, item.score) for item in ranked))
    if decision.error:
        lines.append("runner error: %s" % decision.error)
    return lines


# --- request builders -------------------------------------------------------
def _parse_pool(value: str) -> tuple[str, ...]:
    """``"collie, claude-code"`` -> ``("collie", "claude-code")``, order kept."""
    items: list[str] = []
    for chunk in re.split(r"[,\s]+", value or ""):
        name = chunk.strip()
        if name and name not in items:
            items.append(name)
    return tuple(items) or ("collie",)


def intent_needs_shell(intent: str) -> bool:
    """True only for intents whose work is *running* something, not editing.

    `test` asks the worker to execute the project's checks, which is a shell
    job by definition.  `build`/`plan`/`review` are file work: the host runs the
    verification command itself once the worker is done, so requiring a shell
    from the worker would rule out perfectly good editors for no gain.
    """
    return (intent or "build").strip().lower() == "test"


def request_from_run(args: Any, decision: Any, settings: Any, *, cwd: str,
                     has_approver: bool = False) -> HarnessRequest:
    """Build the selection request for ``collie run``.

    Takes the argparse namespace, the already-resolved ``router.RunDecision``
    and the settings module, and reads nothing else — except one ``isdir`` for
    ``.git``, because whether the workspace is a repository decides how much
    change evidence any worker can produce (H6).

    The important case is the boring one: with ``RUNNER=collie`` and no
    ``--runner`` the returned request's :meth:`HarnessRequest.candidates` is
    ``("collie",)``, so the caller never probes an external CLI and the default
    path stays exactly as fast as it was.
    """
    requested = (getattr(args, "runner", None) or "").strip().lower()
    configured = (_setting(settings, "RUNNER", "collie") or "collie").strip().lower()
    pin = ""
    if requested == "auto":
        configured = "auto"              # --runner auto asks for a choice, not a worker
    elif requested:
        pin = requested

    # What the *worker* has to be able to do, which is not the same as what
    # Collie's own harness is configured with.  `cmd_run` builds the native
    # Harness with exec_code=True, but a delegated worker only has to edit the
    # workspace: the verification command is run by the host afterwards
    # (`cli.run_verification_command`), never inside the worker.  Demanding a
    # shell here unconditionally made H4 reject `claude-code` for every possible
    # invocation -- its tool allowlist is deliberately Read/Edit/Write/Grep/Glob
    # because there is no approval channel back into the gate -- so the whole
    # worker was unreachable while its tests, which construct needs={"code"} by
    # hand, stayed green.
    needs = {"code"}
    if intent_needs_shell(getattr(decision, "intent", "build")):
        needs.add("bash")
    if getattr(args, "web_search", False):
        needs.add("web_search")

    intent = getattr(decision, "intent", "build") or "build"
    gate_mode = (intent if intent in ("plan", "review", "test")
                 else (getattr(args, "mode", None) or ""))

    return HarnessRequest(
        surface="run",
        intent=intent,
        route_kind=getattr(decision, "route_kind", "code") or "code",
        needs=frozenset(needs),
        workspace=getattr(decision, "workspace", "current") or "current",
        workspace_path=cwd,
        workspace_is_git=os.path.isdir(os.path.join(cwd, ".git")),
        strategy=getattr(decision, "strategy", "single") or "single",
        provider=getattr(decision, "provider", "") or "",
        model=getattr(decision, "model", None) or None,
        provider_family=family_of_provider(getattr(decision, "provider", "") or ""),
        pin=pin,
        configured=configured,
        pool=_parse_pool(_setting(settings, "RUNNER_POOL", "collie")),
        # `collie run` has no overage/overnight flags today; Mission supplies them.
        no_paid_overage=bool(getattr(args, "no_paid_overage", False)),
        subscription_only=bool(getattr(args, "subscription_only", False)),
        overnight=False,
        max_cost_usd=_positive(_setting(settings, "MAX_COST", "0")),
        max_total_tokens=(lambda value: int(value) if value else None)(
            _positive(_setting(settings, "MAX_TOTAL_TOKENS", "0"))),
        has_approver=bool(has_approver),
        gate_mode=gate_mode,
        os_name=os.name,
        phase=CURRENT_PHASE,
    )
