"""Approval surfaces — how a gate's "ask" reaches a human, and their answer comes back.

An approver is `fn(tool_name, args, decision) -> Outcome`. The loop knows nothing about
which one is attached; that is what lets a terminal run, an editor run and (later) a
phone answer the same question.

Every approver here shows `args` as the model wrote them — placeholders and all. The loop
hands over the pre-restore arguments on purpose (see Harness._authorize); an approver must
never go looking for the real values to make a prettier card.
"""

from __future__ import annotations

import json

from .gate import Outcome


def describe(tool_name: str, args: dict, decision=None) -> str:
    """One line of what is about to happen. Long values are truncated, because an
    approval prompt nobody reads is not an approval."""
    parts = []
    for k, v in (args or {}).items():
        s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
        s = " ".join(str(s).split())
        if len(s) > 70:
            s = s[:69] + "…"
        parts.append("%s=%s" % (k, s))
    line = "%s(%s)" % (tool_name, ", ".join(parts))
    return line[:400]


def _card(tool_name: str, args: dict, decision) -> list:
    target = getattr(decision, "target", None)
    reason = getattr(decision, "reason", "") or "needs approval"
    lines = ["", "  collie wants to: %s" % describe(tool_name, args),
             "  why it is asking: %s" % reason]
    if target:
        lines.append("  on: %s" % target)
    return lines


def tty_approver(read_line=None, write=print, gate=None):
    """Terminal approver for the REPL and headless-with-a-tty runs.

    `read_line` is injected so the TUI can hand over its own input pump rather than
    fighting it for stdin. EOF / a closed stdin answers no — an unanswerable prompt is
    not consent.
    """
    if read_line is None:
        read_line = lambda: input("  allow? [y]es / [a]lways / [N]o: ")   # noqa: E731

    def approve(tool_name, args, decision):
        for line in _card(tool_name, args, decision):
            write(line)
        offer = gate.standing_rule_offer(tool_name, decision.target) if gate else None
        if offer:
            write("  'always' would allow: %s (this run only)" % offer)
        try:
            ans = (read_line() or "").strip().lower()
        except (EOFError, KeyboardInterrupt, OSError):
            return Outcome.REJECT_ONCE
        if ans in ("y", "yes"):
            return Outcome.ALLOW_ONCE
        if ans in ("a", "always") and offer:
            return Outcome.ALLOW_ALWAYS
        if ans in ("a", "always"):
            # No rule can be pinned for this call, so "always" cannot mean what it says.
            # Grant the one call rather than silently minting a blanket permission.
            write("  (this call can't carry a standing rule — allowing once)")
            return Outcome.ALLOW_ONCE
        if ans in ("never", "n!"):
            return Outcome.REJECT_ALWAYS
        return Outcome.REJECT_ONCE

    return approve


def desktop_approver(fallback=None):
    """Ask whoever is at the machine, via the native dialog.

    plat.ask_allow_deny returns None for "there was nobody to ask" — a headless box, no
    GUI, or a timeout — and its contract says None means UNDECIDED, never denied. That is
    right for the pairing prompt it was built for, which still has an in-app card behind
    it. It is NOT right here: an undecided tool call must not run. So None falls through
    to `fallback` if one is attached, and otherwise refuses.
    """
    from . import plat

    def approve(tool_name, args, decision):
        answer = plat.ask_allow_deny(
            "Collie needs your approval",
            "%s\n\n%s%s" % (describe(tool_name, args),
                            getattr(decision, "reason", ""),
                            ("\n\nOn: %s" % decision.target) if getattr(decision, "target", None) else ""),
            allow="Allow", deny="Don't")
        if answer is None:
            if fallback is not None:
                return fallback(tool_name, args, decision)
            return Outcome.REJECT_ONCE
        return Outcome.ALLOW_ONCE if answer else Outcome.REJECT_ONCE

    return approve


def auto_approver(outcome=Outcome.ALLOW_ONCE):
    """For tests and for a caller that has already obtained consent out of band. Never
    wired to a surface by default — if this is reachable without someone choosing it,
    that is a bug."""
    def approve(_tool_name, _args, _decision):
        return outcome
    return approve
