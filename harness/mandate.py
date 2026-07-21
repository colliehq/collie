"""Mandate compiler — natural language in, a structured job out (plan §5.2).

This is the delegate's front door: the user says what they want in plain words;
host code (optionally with the model) turns it into ONE concrete job — a
registered capability + its args + a leash — which the executor then drives. It
never invents authority: the leash it proposes is scoped to the chosen
capability's family, and an irreversible capability still parks for confirm.

Two paths, so the NL box works regardless of model availability:
  - model path: the configured provider picks a capability from the REGISTERED
    set and fills args as strict JSON.
  - heuristic fallback (no provider / model error / bad JSON): a note-taking
    request maps to note.append; anything else asks a clarifying question.

The compiler only ever selects a capability that is actually registered, so it
cannot propose something the executor can't safely run.
"""

from __future__ import annotations

import json
import re

from .jobs import all_capabilities, get_capability

_SYS = (
    "You are collie's mandate compiler. Turn the user's request into ONE job using ONLY the "
    "registered capabilities listed. Reply with STRICT JSON and nothing else:\n"
    '{"capability": <name or null>, "args": {..}, "goal": "<short imperative>", '
    '"clarify": "<a question, only if capability is null>"}\n'
    "Pick the single best capability, fill its args from the request, and keep goal short. "
    "If no capability fits, set capability to null and put a brief clarifying question in clarify. "
    "Never invent a capability that is not listed.\n\nREGISTERED CAPABILITIES:\n")

_NOTE_PREFIX = re.compile(r"^\s*(记一下|记[:：]|备忘[:：]?|note[:：]?|todo[:：]?|提醒我?[:：]?)\s*", re.I)
# a request only maps to note-taking when it actually asks to note something —
# otherwise the heuristic must NOT silently write an un-doable request as a note.
_NOTE_CUE = re.compile(
    r"(记一下|记[:：]|记录|备忘|提醒我?|待办|清单|note|todo|jot|remember|remind|"
    r"write (this |it )?down|save this|add to (my )?(list|todo))", re.I)
_JSON = re.compile(r"\{.*\}", re.S)


def _catalog() -> str:
    lines = []
    for c in all_capabilities():
        lines.append(f"- {c.name} ({'reversible' if c.reversible else 'IRREVERSIBLE'}): "
                     f"{c.description or ''}  args: {c.args_hint or '{}'}")
    return "\n".join(lines) or "(none registered)"


# the arg each capability cannot run without — a job missing it is garbage and
# must never be created (it would execute on nothing and could fabricate success).
# reminder.set is intentionally ABSENT: it needs no arg (text->"reminder",
# timing->now+600 both default in everyday._fire_at), and requiring "text" would
# false-drop a valid "remind me at 9am" -> misroute to a note that never fires.
_REQUIRED_ARG = {"note.append": "text", "translate": "text",
                 "web.summarize": "url", "research.web": "query"}


def _args_ok(capability: str, args: dict) -> bool:
    req = _REQUIRED_ARG.get(capability)
    if not req:
        return True
    return bool(str((args or {}).get(req) or "").strip())


def _leash_for(capability: str) -> dict:
    # permit the capability itself AND its family — a no-dot name like "translate"
    # does NOT match the glob "translate.*", so the exact name must be included or
    # the compiled leash would deny its own action.
    family = (capability or "").split(".")[0] or capability
    return {"may": sorted({capability, f"{family}.*"})}


def _heuristic(text: str) -> dict:
    """No-model fallback. A clear note request -> note.append. EVERYTHING ELSE
    falls back to research.web — collie always does something useful (finds out
    how / where / whether) rather than refusing. No dead-ends, no 'I can't'."""
    if get_capability("note.append") and _NOTE_CUE.search(text):
        body = _NOTE_PREFIX.sub("", text).strip() or text.strip()
        low = text.lower()
        fname = ("todo.txt" if any(w in low for w in ("todo", "待办", "清单", "list"))
                 else "notes.txt")
        return {"capability": "note.append",
                "args": {"file": fname, "text": body},
                "goal": (body[:60] or "take a note"),
                "leash": _leash_for("note.append"),
                "source": "heuristic"}
    if get_capability("research.web"):
        return {"capability": "research.web",
                "args": {"query": text},
                "goal": text[:60] or "research",
                "leash": _leash_for("research.web"),
                "source": "heuristic"}
    return {"capability": None,
            "clarify": "no capabilities are registered", "source": "heuristic"}


def _parse(txt: str):
    m = _JSON.search(txt or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def compile(text: str, provider=None) -> dict:
    """Compile NL `text` into a job dict: {capability, args, goal, leash, source}
    or {capability: None, clarify, source}. Falls back to the heuristic on any
    model failure so the NL surface always responds."""
    text = (text or "").strip()
    if not text:
        return {"capability": None, "clarify": "Tell me what to do.", "source": "empty"}

    if provider is not None:
        try:
            comp = provider.complete(_SYS + _catalog(),
                                     [{"role": "user", "content": text}], [])
            if getattr(comp, "stop_reason", "") != "error":
                plan = _parse(getattr(comp, "text", "") or "")
                cap = (plan or {}).get("capability")
                if plan and cap and get_capability(cap) and _args_ok(cap, plan.get("args")):
                    return {"capability": cap,
                            "args": plan.get("args") or {},
                            "goal": plan.get("goal") or text[:60],
                            "leash": _leash_for(cap),
                            "source": "model"}
                # model returned null / an unregistered capability / missing a
                # required arg -> fall through to the heuristic, which routes
                # anything to research with the FULL text (never refuse, never
                # create a garbage job)
        except Exception:
            pass
    return _heuristic(text)
