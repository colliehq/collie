"""Front-door router — the classifying "head" that types each message and routes it.

Ordinary messages get ONE cheap model call that classifies them into a small,
principled set of interactive task kinds; the caller then routes to the right executor. The
taxonomy is not ad-hoc — it is two orthogonal axes from the literature,
discretized into three kinds (see docs/ROUTER_DESIGN.md for citations):

  AXIS 1 — know vs do (Parasuraman/Sheridan/Wickens 2000 information-vs-action
           stages; Kirsh & Maglio 1994 epistemic-vs-pragmatic action; Searle
           assertives-vs-directives): separates CHAT from CODE+MISSION.
  AXIS 2 — reversibility of the action (Amodei et al. 2016 side-effects/safe-
           exploration; Krakovna et al. 2019 reachability): separates reversible
           workspace edits (CODE) from consequential, possibly irreversible
           real-world action (MISSION).

  chat    — produce information (answer / explain / find out on the web). Research
            lives HERE (epistemic, read-only) — never its own top-level kind.
  code    — create/modify/debug code or files in the workspace (reversible).
  mission — a durable, multi-step real-world errand, entered ONLY through an
            explicit `/mission ...` (or legacy `/delegate ...`) command.

The irreversible route is command-only. Even if the classifier calls ordinary
language a mission, it is defensively collapsed to chat. This makes starting a
durable campaign an unambiguous user action rather than a probabilistic guess.

Honesty about the model: the model is a HARD dependency of every route (chat,
code, and mission all need it). So if the model is genuinely unavailable, we do
NOT silently fall back to a heuristic — we raise ModelUnavailable and the caller
says so. The ONLY fallback is: the model responded but its label was unparseable
(the model IS up) -> route chat, the cheapest working path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import time

KINDS = ("chat", "code", "mission")

# Retained for import compatibility with older clients. Mission is now command-only,
# so no model confidence value can activate it.
MISSION_THRESHOLD = 0.7

# The router's default model on anthropic providers (Sonnet: fast + capable; the
# whole haiku/sonnet/opus set scored 28/28 on the battery, so this trades latency,
# not accuracy). Override up (opus) or down (haiku) via COLLIE_ROUTER_MODEL.
DEFAULT_ROUTER_MODEL = "claude-sonnet-4-6"


# The front-door label is only one input to execution.  Keeping the rest of the
# decision in one value prevents the web, mobile, and Pack paths from each
# inventing a slightly different meaning for "Auto".
_AXES = ("intent", "quality", "verification", "workspace", "strategy", "effort", "speed")
_VALID = {
    "intent": ("build", "plan", "test", "review"),
    "quality": ("quick", "balanced", "thorough"),
    "verification": ("auto", "required"),
    "workspace": ("current", "isolated"),
    "strategy": ("single", "pack"),
}

_HARD_TASK = re.compile(
    r"\b(?:security|auth(?:entication|orization)?|permission|migration|schema|database|"
    r"architecture|concurren(?:cy|t)|race condition|deadlock|distributed|production|release|"
    r"refactor|multi[- ]?file|breaking change|rollback|data loss|performance regression|"
    r"flaky|intermittent|root cause|end[- ]to[- ]end)\b|"
    r"(?:安全|认证|授权|权限|迁移|架构|并发|竞态|死锁|分布式|生产|发布|重构|"
    r"多文件|破坏性变更|回滚|数据丢失|性能回归|不稳定|根因|端到端|系统性|全面)",
    re.I,
)
_CODE_ACTION = re.compile(
    r"\b(?:add|build|change|create|debug|delete|edit|fix|implement|modify|patch|refactor|"
    r"remove|rename|replace|ship|update|write)\b|(?:^|[\\/])[^\s]+\.[a-z0-9]{1,8}\b|"
    r"(?:添加|新增|构建|创建|调试|删除|编辑|修复|实现|修改|补丁|重构|移除|重命名|替换|更新|编写)",
    re.I,
)
_TINY_TASK = re.compile(
    r"\b(?:typo|copy|comment|docs?|string|label|rename|one[- ]line|tiny|small|"
    r"format(?:ting)?)\b|(?:错别字|文案|注释|文档|字符串|标签|重命名|一行|小改|微调|格式化)",
    re.I,
)


@dataclass(frozen=True)
class RunDecision:
    """The resolved, auditable contract for one interactive execution.

    ``sources`` says who chose each field (user/config/router/policy/safety).
    ``reasons`` is intentionally plain text so it can be shown in a receipt and
    persisted without needing the policy code that produced it.
    """

    provider: str
    model: str
    effort: str
    speed: str
    billing_multiplier: float | None
    intent: str
    quality: str
    verification: str
    workspace: str
    strategy: str
    route_kind: str
    complexity: str
    explicit: tuple[str, ...] = ()
    sources: dict[str, str] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "speed": self.speed,
            "billing_multiplier": self.billing_multiplier,
            "intent": self.intent,
            "quality": self.quality,
            "verification": self.verification,
            "workspace": self.workspace,
            "strategy": self.strategy,
            "route_kind": self.route_kind,
            "complexity": self.complexity,
            "explicit": list(self.explicit),
            "sources": dict(self.sources),
            "reasons": list(self.reasons),
        }


def parse_explicit_axes(value) -> tuple[str, ...]:
    """Normalize a query-string/list/set of explicitly chosen run axes."""
    if isinstance(value, str):
        value = value.split(",")
    return tuple(sorted({str(v).strip().lower() for v in (value or ())
                         if str(v).strip().lower() in _AXES}))


@dataclass(frozen=True)
class FailureSignal:
    """Did the most recent COMPLETED run actually fail?

    Structured run receipts are the only evidence.  Prose is not: an assistant
    that says "the error is fixed" is reporting success, and a model failure need
    not appear in the transcript at all, so scanning messages for words like
    "error" escalated resolved problems and missed real ones.

    The signal is deliberately transient — it reflects the latest conclusive
    receipt only, so one failure escalates the next run and the next clean
    receipt clears it again.
    """

    failed: bool = False
    reason: str = ""
    source: str = "none"          # "receipt" | "none"

    def __bool__(self) -> bool:
        return self.failed


NO_FAILURE = FailureSignal()

# How far back a conclusive outcome is looked for.  Older receipts describe a
# different problem and must not pin every later message to the biggest model.
_RECEIPT_SCAN = 8

# A row is a run OUTCOME (and therefore able to clear an escalation) only if it
# carries at least one verdict field.  A decision-only row is a routing note, not
# evidence that anything ran.
_OUTCOME_KEYS = ("error", "verified", "verification_evidence", "attempts",
                 "applied", "check_pass", "winner")


def _evidence_verdict(evidence: dict):
    """('failed'|'clean'|None, reason) for one verification-evidence blob.

    ``None`` means inconclusive.  Crucially, "no check was run" is inconclusive,
    NOT a failure: web and terminal callers frequently have no repo check to
    detect, and a missing check must never escalate the next turn.
    """
    if evidence.get("cancelled") or evidence.get("canceled"):
        return None, ""
    executed = evidence.get("executed")
    if isinstance(executed, bool):
        ran = executed
    else:                                   # receipts written before ``executed``
        command = evidence.get("command")
        ran = (isinstance(command, str) and bool(command.strip()) and
               isinstance(evidence.get("exit_code"), int))
    if not ran or evidence.get("freshness") == "not_run":
        return None, ""
    passed = evidence.get("passed")
    if passed is True:
        return "clean", ""
    if passed is False:
        return "failed", "the executed check did not pass"
    return None, ""                          # malformed verdict: do not guess


def _receipt_verdict(row):
    """('failed'|'clean'|None, reason) for one persisted run receipt."""
    if not isinstance(row, dict):
        return None, ""                      # malformed row: not evidence of anything
    if row.get("canceled") or row.get("cancelled"):
        return None, ""                      # a user stop is not a failed attempt
    error = row.get("error")
    if error is not None and not isinstance(error, str):
        return None, ""                      # malformed outcome: refuse to guess either way
    if isinstance(error, str) and error.strip():
        return "failed", error.strip()[:120]
    evidence = row.get("verification_evidence")
    if isinstance(evidence, dict):
        verdict, reason = _evidence_verdict(evidence)
        if verdict:
            return verdict, reason
    elif evidence is not None:
        return None, ""                      # malformed evidence
    if any(key in row for key in _OUTCOME_KEYS):
        return "clean", ""
    return None, ""


def failure_signal(receipts) -> FailureSignal:
    """Resolve run receipts into the typed, transient failure signal."""
    if isinstance(receipts, FailureSignal):
        return receipts
    try:
        rows = list(receipts or [])[-_RECEIPT_SCAN:]
    except TypeError:                        # not iterable == no evidence
        return NO_FAILURE
    for row in reversed(rows):
        verdict, reason = _receipt_verdict(row)
        if verdict == "failed":
            return FailureSignal(True, reason or "the last run failed", "receipt")
        if verdict == "clean":
            return NO_FAILURE
    return NO_FAILURE


def _infer_kind(text: str, route_kind: str | None) -> str:
    kind = (route_kind or "").strip().lower()
    if kind in ("chat", "code"):
        return kind
    # Mobile and API clients may not call /api/route.  This is not allowed to
    # create a mission; it only chooses the read-only vs workspace executor.
    return "code" if _CODE_ACTION.search(text or "") else "chat"


def _complexity(text: str, kind: str, signal: FailureSignal) -> str:
    if signal.failed or _HARD_TASK.search(text or "") or len(text or "") > 900:
        return "hard"
    if len(text or "") < 180 and (kind == "chat" or _TINY_TASK.search(text or "")):
        return "simple"
    return "standard"


def _automatic_model(provider: str, kind: str, complexity: str, quality: str) -> str:
    """Choose capability inside *provider*; never cross a trust/billing boundary."""
    p = (provider or "").lower()
    if p in ("codex-oauth", "codex-sub", "codex"):
        # Terra handles fast scans/everyday tasks; Sol is reserved for work whose
        # ambiguity or prior failure justifies its latency.  A pinned Sol still
        # wins below, and can run at low/medium effort when the task is small.
        if quality == "quick" or complexity == "simple":
            return "gpt-5.6-luna"
        return "gpt-5.6-sol" if complexity == "hard" else "gpt-5.6-terra"
    if p in ("anthropic-oauth", "claude-sub"):
        if quality == "quick" or complexity == "simple":
            return "claude-haiku-4-5-20251001"
        return "claude-opus-5" if complexity == "hard" else "claude-sonnet-5"
    from .providers import provider_default_model
    return provider_default_model(provider)


def resolve_run_decision(text: str, provider: str, model: str | None = None,
                         effort: str = "auto", speed: str = "standard",
                         route_kind: str | None = None,
                         intent: str = "build", quality: str = "balanced",
                         verification: str = "auto", workspace: str = "current",
                         strategy: str = "single", explicit_axes=(), history=None,
                         receipts=None) -> RunDecision:
    """Resolve one task-aware execution policy.

    Priority is explicit per-run choice > configured model/effort > task policy >
    provider default.  Provider is never changed here: doing so would silently
    change credentials, billing, and data policy.  Unsupported effort levels are
    capability-resolved by the provider layer and the downgrade is recorded.

    ``receipts`` are structured run outcomes (or an already-resolved
    :class:`FailureSignal`) and are the ONLY source of failure escalation.
    ``history`` is accepted for call-site compatibility and no longer contributes
    to that decision — see :class:`FailureSignal`.
    """
    provider = (provider or "").strip()
    if not provider:
        raise ValueError("provider is required")
    requested = {
        "intent": (intent or "build").strip().lower(),
        "quality": (quality or "balanced").strip().lower(),
        "verification": (verification or "auto").strip().lower(),
        "workspace": (workspace or "current").strip().lower(),
        "strategy": (strategy or "single").strip().lower(),
    }
    for axis, allowed in _VALID.items():
        if requested[axis] not in allowed:
            raise ValueError("%s must be %s" % (axis, " or ".join(allowed)))
    explicit = set(parse_explicit_axes(explicit_axes))
    kind = _infer_kind(text or "", route_kind)
    signal = failure_signal(receipts)
    complexity = _complexity(text or "", kind, signal)
    routing_quality = (requested["quality"] if "quality" in explicit else
                       ("thorough" if complexity == "hard" else "balanced"))
    sources = {"provider": "configured"}
    reasons = ["provider: kept configured %s; automatic routing never crosses providers" % provider]

    configured_model = (model or "").strip()
    if configured_model:
        resolved_model = configured_model
        sources["model"] = "configured"
        reasons.append("model: kept the explicitly configured model %s" % resolved_model)
    else:
        resolved_model = _automatic_model(provider, kind, complexity, routing_quality)
        sources["model"] = "task-policy"
        reasons.append("model: selected %s inside %s for a %s %s task" %
                       (resolved_model, provider, complexity, kind))

    # Selecting a Build-only execution axis is also an unambiguous request to
    # build, even when a cheap classifier called the prose informational.
    build_implied = (("workspace" in explicit and requested["workspace"] == "isolated") or
                     ("verification" in explicit and requested["verification"] == "required") or
                     ("strategy" in explicit and requested["strategy"] == "pack"))
    if "intent" in explicit:
        resolved_intent = requested["intent"]
        sources["intent"] = "user"
    elif build_implied:
        resolved_intent = "build"
        sources["intent"] = "implied-user-choice"
    else:
        # Plan is a user-requested read-only contract, not the default shape of every question.
        # Route kind still drives model/depth selection and keeps consequential work fenced, while
        # ordinary chat stays in the normal run so a diagnosis can continue into a requested fix
        # without an artificial Plan handoff.  Explicit Plan/Review/Test choices still win above.
        resolved_intent = "build"
        sources["intent"] = "router"
    reasons.append("intent: %s (%s)" % (resolved_intent, sources["intent"]))

    if "quality" in explicit:
        resolved_quality = requested["quality"]
        sources["quality"] = "user"
    else:
        resolved_quality = routing_quality
        sources["quality"] = "task-policy"
    reasons.append("quality: %s (%s)" % (resolved_quality, sources["quality"]))

    # Required is a hard gate: it fails an otherwise successful edit that did not
    # produce an executed post-edit assertion.  Keywords describe the SUBJECT of a
    # task ("auth", "migration"), never whether evidence exists, so they are not
    # allowed to impose that gate — an ordinary auth fix stays Auto.  Required is
    # therefore only ever a choice someone made.
    if "verification" in explicit:
        resolved_verification = requested["verification"]
        sources["verification"] = "user"
    elif requested["verification"] == "required":
        # No default anywhere in the product is "required", so a caller that asks
        # for it (benchmark protocol, legacy web mode, an API client) meant it
        # even without naming the axis.  Never silently downgrade that.
        resolved_verification = "required"
        sources["verification"] = "caller-request"
    else:
        resolved_verification = "auto"
        sources["verification"] = "task-policy"
    reasons.append("verification: %s (%s%s)" % (
        resolved_verification, sources["verification"],
        "; Required is only ever an explicit choice"
        if sources["verification"] == "task-policy" else ""))

    # Isolation and Pack affect where writes land and how much work is spent.
    # They therefore stay conservative unless the user explicitly chose them.
    for axis, safe in (("workspace", "current"), ("strategy", "single")):
        if axis in explicit:
            value, source = requested[axis], "user"
        else:
            value, source = safe, "safety-default"
        if axis == "workspace":
            resolved_workspace = value
        else:
            resolved_strategy = value
        sources[axis] = source
        reasons.append("%s: %s (%s)" % (axis, value, source))

    desired_effort = (effort or "auto").strip().lower()
    if desired_effort in ("", "auto"):
        desired_effort = ("low" if resolved_quality == "quick" else
                          {"simple": "low", "standard": "medium", "hard": "high"}[complexity])
        sources["effort"] = "task-policy"
    else:
        sources["effort"] = "user-or-config"
    from .providers import resolve_reasoning_effort
    resolved_effort, effort_note = resolve_reasoning_effort(
        provider, resolved_model, desired_effort)
    reasons.append("effort: %s (%s%s)" % (
        resolved_effort, sources["effort"], "; " + effort_note if effort_note else ""))
    from .providers import resolve_speed_tier
    resolved_speed, caps = resolve_speed_tier(provider, resolved_model, speed)
    sources["speed"] = "user" if resolved_speed == "fast" or "speed" in explicit else "default"
    reasons.append("speed: %s (%s%s)" % (
        resolved_speed, sources["speed"],
        "; " + caps["fast_note"] if resolved_speed == "fast" else ""))
    if signal.failed:
        reasons.append(
            "complexity: escalated to hard after the last run receipt failed (%s); "
            "the next clean run clears this" % signal.reason)
    else:
        reasons.append("complexity: %s from task scope and risk" % complexity)

    return RunDecision(
        provider=provider, model=resolved_model, effort=resolved_effort,
        speed=resolved_speed, billing_multiplier=(
            caps["fast_billing_multiplier"] if resolved_speed == "fast" else 1.0),
        intent=resolved_intent, quality=resolved_quality,
        verification=resolved_verification, workspace=resolved_workspace,
        strategy=resolved_strategy, route_kind=kind, complexity=complexity,
        explicit=tuple(sorted(explicit)), sources=sources, reasons=tuple(reasons))

_PREFIX = re.compile(r"^\s*/(mission|delegate|code|chat)\s+(.*)", re.I | re.S)
_JSON = re.compile(r"\{.*\}", re.S)

_SYS = (
    "You are collie's front-door router. Classify the user's message into ONE task kind so it "
    "routes to the right executor. Decide on two axes:\n"
    "  AXIS 1 (know vs do): does the message ask you to PRODUCE INFORMATION (answer / explain / "
    "find something out), or to TAKE ACTION that changes something?\n"
    "  AXIS 2 (only if it is an action): change a REVERSIBLE artifact in the current code workspace, "
    "or take a CONSEQUENTIAL real-world action that may be irreversible and/or wait for events?\n\n"
    "Kinds:\n"
    "- \"chat\": produce information — answer, explain, compare, discuss, or research/find something "
    "out on the web. No workspace change, no real-world action. (Research is chat.)\n"
    "- \"code\": create, modify, or debug code or files in the current workspace. Reversible.\n"
    "- \"mission\": a durable, multi-step real-world errand that may take IRREVERSIBLE actions "
    "(send, publish, buy, apply, book, pay) or wait for external events (a reply, availability). "
    "e.g. 'sell my car', 'email X and follow up', 'watch this listing and tell me when it drops'.\n\n"
    "Rules:\n"
    "- Bias toward the cheaper, reversible kind when unsure: chat over code, code over mission. "
    "Only choose \"mission\" when it clearly asks you to act in the world over time.\n"
    "- confidence (0..1) = how sure you are; a genuinely ambiguous message gets LOW confidence.\n"
    "- goal = a short normalized imperative (essential for mission; echo the ask for others).\n\n"
    "Examples:\n"
    "  'why is this test flaky?' -> {\"kind\":\"chat\",\"confidence\":0.9}\n"
    "  'add a --json flag to the CLI' -> {\"kind\":\"code\",\"confidence\":0.9}\n"
    "  'sell my 2018 Corolla on marketplace, local only' -> {\"kind\":\"mission\",\"confidence\":0.95}\n"
    "  'find me a cheap flight to Tokyo next month' -> {\"kind\":\"chat\",\"confidence\":0.6} (finding out = chat)\n\n"
    "Reply with STRICT JSON only and nothing else:\n"
    '{"kind": "chat|code|mission", "goal": "<short imperative>", '
    '"confidence": 0.0, "reason": "<one short clause>"}')


class ModelUnavailable(RuntimeError):
    """The model could not be reached — every route needs it, so the caller must
    surface this, NOT route somewhere as if it worked."""


def prefix_override(text: str):
    """Explicit user override: '/mission …' '/code …' '/chat …' ('/delegate' == mission).
    Returns (kind, stripped_text) or None. Handled BEFORE any model call (zero latency)."""
    from .missioncmd import parse as parse_mission
    mission = parse_mission(text or "")
    if mission is not None:
        # Body is retained for backward compatibility; classify() replaces it
        # with structured command/goal fields below.
        body = re.sub(r"^\s*/(?:mission|delegate)\b", "", text or "",
                      count=1, flags=re.I).strip()
        return "mission", body
    m = _PREFIX.match(text or "")
    if not m:
        return None
    word = m.group(1).lower()
    kind = "mission" if word in ("mission", "delegate") else word
    return kind, m.group(2).strip()


def _parse(txt: str):
    m = _JSON.search(txt or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _backoff(attempt: int) -> float:
    # short, front-door-friendly: a user is waiting on their message.
    return min(0.5 * (2 ** attempt), 4.0)


def _decide(comp, text: str) -> dict:
    """Turn a successful completion into the routing decision (parse + threshold)."""
    plan = _parse(getattr(comp, "text", "") or "")
    if not plan or plan.get("kind") not in KINDS:
        # the model IS up but its label is unusable -> the cheapest working path.
        # NOT a heuristic classifier: it only fires when the model already answered.
        return {"kind": "chat", "goal": text, "confidence": 0.0,
                "reason": "classification unparsed", "source": "fallback", "abstained": False}
    kind = plan["kind"]
    try:
        conf = float(plan.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    goal = (plan.get("goal") or text).strip()
    reason = (plan.get("reason") or "")[:200]
    # Mission is EXPLICIT-ONLY. A model label can never start durable work.
    if kind == "mission":
        return {"kind": "chat", "goal": text, "confidence": conf, "reason": reason,
                "source": "model", "abstained": False}
    return {"kind": kind, "goal": goal, "confidence": conf, "reason": reason,
            "source": "model", "abstained": False}


def classify(text: str, provider, ctx: dict = None, retries: int = 3, _sleep=None) -> dict:
    """Classify `text` -> a routing decision dict:
        {kind, goal, confidence, reason, source, abstained[, suggested]}
      source: 'override' (explicit prefix) | 'model' | 'fallback' (model up, label unusable)

    Retries TRANSIENT model errors (HTTP 529 overloaded / 429 / timeouts — the same
    class collie's loop retries, via providers.classify_error) with a short backoff,
    since the front door must ride out an overload rather than fail the user's first
    message. Raises ModelUnavailable only on a TERMINAL error (auth / bad request /
    no provider) or after the transient retries are exhausted (persistent overload
    == effectively down) — never a silent heuristic fallback.
    """
    text = (text or "").strip()
    ov = prefix_override(text)
    if ov:
        kind, body = ov
        out = {"kind": kind, "goal": body or text, "confidence": 1.0,
               "reason": "explicit prefix", "source": "override", "abstained": False}
        if kind == "mission":
            from .missioncmd import parse
            command = parse(text)
            if command:
                out.update({"mission_command": command.action,
                            "mission_id": command.mission_id,
                            "autonomous": command.autonomous,
                            "goal": command.goal})
                if command.error:
                    out["command_error"] = command.error
        return out

    if provider is None:
        raise ModelUnavailable("no model provider configured")
    from .providers import classify_error       # lazy: keep router import light
    sleep = _sleep or time.sleep
    last = "model unavailable"
    for attempt in range(max(1, retries + 1)):
        try:
            comp = provider.complete(_SYS, [{"role": "user", "content": text}], [])
        except Exception as e:                    # a RAISING provider = hard failure, don't spin
            raise ModelUnavailable(f"{type(e).__name__}: {e}")
        if getattr(comp, "stop_reason", "") != "error":
            return _decide(comp, text)
        # error-as-data: is it transient (retry) or terminal (give up now)?
        detail = (getattr(comp, "error_detail", "") or getattr(comp, "text", "") or "model error")
        status = getattr(comp, "error_status", 0)
        last = detail[:200]
        if classify_error(detail, status) == "retryable" and attempt < retries:
            sleep(_backoff(attempt))
            continue
        raise ModelUnavailable(last)              # terminal, or transient after retries exhausted
    raise ModelUnavailable(last)
