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


def _leash_for(capability: str) -> dict:
    family = (capability or "").split(".")[0] or capability
    return {"may": [f"{family}.*"]}


def _heuristic(text: str) -> dict:
    """No-model fallback. Maps to note-taking ONLY when the request actually asks
    to note/remember something; otherwise it says honestly that it can't do it,
    rather than silently writing an un-doable request (e.g. 'book a flight') as a
    note — the exact silent-wrong-completion failure the whole design rejects."""
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
    return {"capability": None,
            "clarify": "我现在只会记笔记(试试「记一下 …」)。发邮件、订票、挂单这类真能力还没接上——"
                       "接上前我不会假装做了。",
            "source": "heuristic"}


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
                if plan and cap and get_capability(cap):
                    return {"capability": cap,
                            "args": plan.get("args") or {},
                            "goal": plan.get("goal") or text[:60],
                            "leash": _leash_for(cap),
                            "source": "model"}
                if plan and cap is None and plan.get("clarify"):
                    return {"capability": None, "clarify": plan["clarify"], "source": "model"}
                # model picked an unregistered capability -> fall through to heuristic
        except Exception:
            pass
    return _heuristic(text)
