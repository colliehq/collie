"""Front-door router — the classifying "head" that types each message and routes it.

Every incoming message gets ONE cheap model call that classifies it into a small,
principled set of task kinds; the caller then routes to the right executor. The
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
  mission — a durable, multi-step real-world errand that may take IRREVERSIBLE
            actions (send/publish/buy/apply/book/pay) or wait for external events.

Two design rules taken straight from the literature:
  * calibrated confidence + a per-route threshold, highest for the irreversible
    MISSION route (RouteLLM, Ong et al. 2024) — below it we ABSTAIN to the cheap,
    reversible path (chat) and let the user promote it;
  * an explicit abstain rather than force-fitting every message (CLINC150 out-of-
    scope, Larson et al. 2019).

Honesty about the model: the model is a HARD dependency of every route (chat,
code, and mission all need it). So if the model is genuinely unavailable, we do
NOT silently fall back to a heuristic — we raise ModelUnavailable and the caller
says so. The ONLY fallback is: the model responded but its label was unparseable
(the model IS up) -> route chat, the cheapest working path.
"""

from __future__ import annotations

import json
import re
import time

KINDS = ("chat", "code", "mission")

# Per-route acceptance threshold. Only the irreversible MISSION route is gated:
# routing INTO a consequential campaign demands high confidence; below it we
# abstain to chat (reversible) and surface a one-click "run as mission". code/chat
# share the interactive backend, so their boundary needs no gate.
MISSION_THRESHOLD = 0.7

# The router's default model on anthropic providers (Sonnet: fast + capable; the
# whole haiku/sonnet/opus set scored 28/28 on the battery, so this trades latency,
# not accuracy). Override up (opus) or down (haiku) via COLLIE_ROUTER_MODEL.
DEFAULT_ROUTER_MODEL = "claude-sonnet-4-6"

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
    m = _PREFIX.match(text or "")
    if not m:
        return None
    word = m.group(1).lower()
    # the mission route is disabled (unmanageable in the UI) — '/mission' and '/delegate' run as chat
    kind = "chat" if word in ("mission", "delegate") else word
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
    # the mission route is DISABLED (its live view/kill/manage UI wasn't usable) — anything the model
    # would have called a mission just runs as chat, with no "promote to mission" affordance.
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
        return {"kind": kind, "goal": body or text, "confidence": 1.0,
                "reason": "explicit prefix", "source": "override", "abstained": False}

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
