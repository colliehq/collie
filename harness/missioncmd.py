"""Pure parser for Mission slash commands.

Keeping management verbs distinct from a start goal prevents a surface from
turning ``/mission cancel msn_x`` into a brand-new Mission whose goal is the
words "cancel msn_x".  Execution remains in MissionService.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_PREFIX = re.compile(r"^\s*/(?:mission|delegate)(?:\s+(.*))?\s*$", re.I | re.S)
_CONTROLS = {"status", "run", "pause", "resume", "cancel", "check",
             "continue", "accept", "reconcile"}


@dataclass(frozen=True)
class MissionCommand:
    action: str
    goal: str = ""
    mission_id: str = ""
    # None = use the user's saved Mission default; True/False are one-run overrides.
    autonomous: bool | None = None
    error: str = ""


def parse(text: str):
    m = _PREFIX.match(text or "")
    if not m:
        return None
    raw = (m.group(1) or "").strip()
    if not raw:
        return MissionCommand("list")
    if raw.lower() in ("list", "ls"):
        return MissionCommand("list")
    if raw.lower() == "help":
        return MissionCommand("help")
    reserved = re.match(r"^(start|list|ls|help)(?:\s+|$)", raw, re.I)
    if reserved and reserved.group(1).lower() == "start" and raw.lower() == "start":
        return MissionCommand("invalid", error="Mission goal required after start")
    if reserved and reserved.group(1).lower() in ("list", "ls", "help"):
        return MissionCommand(
            "invalid", error=f"Mission {reserved.group(1).lower()} takes no arguments")
    control = re.match(r"^(\w+)\s+(\S+)\s*$", raw, re.S)
    if control and control.group(1).lower() in _CONTROLS:
        return MissionCommand(control.group(1).lower(), mission_id=control.group(2))
    if raw.split(None, 1)[0].lower() in _CONTROLS:
        return MissionCommand("invalid", error="Mission control needs exactly one id")
    start = re.match(r"^start\s+(.+)$", raw, re.I | re.S)
    goal = (start.group(1) if start else raw).strip()
    autonomous = None
    mode = re.match(r"^--(auto|autonomous|review|confirm)(?:\s+|$)", goal, re.I)
    if mode:
        autonomous = mode.group(1).lower() in ("auto", "autonomous")
        goal = goal[mode.end():].strip()
    return MissionCommand("start" if goal else "invalid", goal=goal,
                          autonomous=autonomous,
                          error="Mission goal required" if not goal else "")
