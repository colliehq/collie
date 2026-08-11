"""Risk classes for tools — the declared side-effect category the gate reads.

Risk is DATA, in one auditable table, not a predicate scattered across the tools. You
can read this file and know the whole policy; that is the point of keeping it here
rather than as a flag on each Tool subclass.

Four classes, ordered by what they can reach:

  read          no side effect anywhere            — always allowed
  write_local   mutates this machine's files       — scoped to the writable roots
  exec          runs a command on this machine     — mode-gated
  external      an effect that LEAVES this machine — the part that needs a human

`external` is the one that matters for collie specifically. A cloud agent's worst
case is a bad edit; collie drives the user's REAL logged-in browser and their real
desktop, so `browser_click` can send mail, post, buy, or delete — under their cookies,
with no further authentication. That is the boundary this module exists to name.

TWO THINGS ARE DELIBERATE AND EASY TO GET WRONG:

1. The fallback is EXTERNAL, not READ. collie's tool set is not closed — MCP servers
   register tools at runtime, `enable_capability` adds more mid-session, and provider
   plugins can ship their own. An unclassified tool must fail CLOSED. (A closed-set
   agent can afford to default to read; we cannot.)

2. Browser and desktop risk depends on the ARGUMENTS, not the tool name. `browser_click`
   on a nav link and `browser_click` on "Send" are the same tool with the same schema.
   So the unit of authority here is (tool, target) — see `target_for` — where the target
   is the page origin or the app. "Always allow browser actions on localhost:5173" is
   expressible; "always allow browser_click" deliberately is not.
"""

from __future__ import annotations

import fnmatch
import urllib.parse
from enum import Enum
from typing import Any, Callable, Optional


class RiskClass(str, Enum):
    READ = "read"
    WRITE_LOCAL = "write_local"
    EXEC = "exec"
    EXTERNAL = "external"


# --------------------------------------------------------------------------- #
# The table. Every built-in tool collie registers appears here exactly once;
# tests/test_risk.py walks the live registry and fails if one is missing, so a
# new tool cannot reach users without someone deciding what it can reach.
# --------------------------------------------------------------------------- #
_BASE: dict[str, RiskClass] = {
    # -- read ---------------------------------------------------------------
    "read_file": RiskClass.READ,
    "glob": RiskClass.READ,
    "grep": RiskClass.READ,
    "code_search": RiskClass.READ,
    "memory_search": RiskClass.READ,
    "screenshot": RiskClass.READ,
    "load_tools": RiskClass.READ,
    "plan": RiskClass.READ,
    "mcpctl_status": RiskClass.READ,
    # web_fetch/web_search leave the machine, but only to READ a public URL: no
    # session, no cookies, nothing mutated. Gating them would stop ordinary
    # research and buy nothing — the injection risk they DO carry is already
    # handled where it belongs, by the untrusted-content fence in webfetch.py.
    "web_fetch": RiskClass.READ,
    "web_search": RiskClass.READ,
    # browser reads: observing the page, not touching it.
    "browser_read": RiskClass.READ,
    "browser_snapshot": RiskClass.READ,
    "browser_links": RiskClass.READ,
    "browser_fields": RiskClass.READ,
    "browser_console": RiskClass.READ,
    "browser_screenshot": RiskClass.READ,
    "browser_tabs": RiskClass.READ,
    # desktop reads: reading the UI tree, not driving it.
    "desktop_apps": RiskClass.READ,
    "desktop_inspect": RiskClass.READ,
    "desktop_read": RiskClass.READ,
    # -- write_local --------------------------------------------------------
    "write_file": RiskClass.WRITE_LOCAL,
    "edit_file": RiskClass.WRITE_LOCAL,
    "remember": RiskClass.WRITE_LOCAL,
    "undo": RiskClass.WRITE_LOCAL,
    # -- exec ---------------------------------------------------------------
    "bash": RiskClass.EXEC,
    "run_in_env": RiskClass.EXEC,
    "execute_code": RiskClass.EXEC,
    # -- external -----------------------------------------------------------
    # Browser writes. Every one of these acts inside the user's logged-in session.
    "browser_open": RiskClass.EXTERNAL,
    "browser_click": RiskClass.EXTERNAL,
    # Narrower than browser_click and extension-enforced, but it still acts in
    # the user's real authenticated session, so the global policy stays strict.
    "browser_advance": RiskClass.EXTERNAL,
    "browser_type": RiskClass.EXTERNAL,
    "browser_press": RiskClass.EXTERNAL,
    "browser_hover": RiskClass.EXTERNAL,
    "browser_drag": RiskClass.EXTERNAL,
    "browser_pick": RiskClass.EXTERNAL,
    "browser_upload": RiskClass.EXTERNAL,
    "browser_eval": RiskClass.EXTERNAL,
    "browser_script": RiskClass.EXTERNAL,
    "browser_reload_extension": RiskClass.EXTERNAL,
    # Desktop writes: driving someone's real applications.
    "desktop_click": RiskClass.EXTERNAL,
    "desktop_type": RiskClass.EXTERNAL,
    "desktop_launch": RiskClass.EXTERNAL,
    "desktop_focus": RiskClass.EXTERNAL,
    "desktop_menu": RiskClass.EXTERNAL,
    # Changing what collie itself can do next.
    "enable_capability": RiskClass.EXTERNAL,
    "delegate": RiskClass.EXTERNAL,
    "mcpctl_add": RiskClass.EXTERNAL,
    # Opens the user's browser to authorize a service and registers whatever tools it
    # exposes into THIS session — the single widest-reaching call collie has. Its own
    # description already says it "requires their explicit agreement first"; now that is
    # enforced rather than asked of the model.
    "mcpctl_connect": RiskClass.EXTERNAL,
    "mcpctl_remove": RiskClass.EXTERNAL,
    # Asymmetry worth naming: tools.py's capability layer deliberately leaves turning a
    # server OFF ungated ("being able to disable a misbehaving server should never need a
    # permission dance"), and this is stricter — one name covers both directions, so the
    # safe direction gets asked too. Kept strict rather than making risk argument-dependent
    # for a single tool; the cost is one prompt on an action nobody performs in a loop.
    "mcpctl_set_enabled": RiskClass.EXTERNAL,
}

# `desktop_control` / `screen_capture` / `mcp_manage` are CAPABILITY names (tools.py
# _GATED_CAPS), not tools — they are what `enable_capability` switches on. That is
# exactly why `enable_capability` is external: it widens collie's reach. They are
# deliberately absent from the table above; adding them would be dead policy.

# Glob rules for tools whose names are not known until runtime. Most specific wins
# (see `_specificity`), so a user override of `mcp__fs__read_*` beats this.
_PATTERNS: tuple[tuple[str, RiskClass], ...] = (
    # An MCP server's `create_page` and `delete_database` look alike from here, so
    # every MCP tool is external until the USER says otherwise (see overrides).
    ("mcp__*", RiskClass.EXTERNAL),
)

# Tools that can never carry a standing "allow every time" rule, whatever their
# target. Arbitrary JS in a logged-in page is that origin's full authority — one
# approval would hand over the account, not a click. Same reasoning as the rule
# that shell commands are re-asked forever.
NO_STANDING_RULE = frozenset({
    "browser_eval", "browser_script",
    "bash", "run_in_env", "execute_code",
    "enable_capability", "mcpctl_add", "mcpctl_remove", "mcpctl_set_enabled",
})

# A user-local resolver: tool name -> RiskClass, or None to defer to the table.
RiskOverrides = Callable[[str], Optional["RiskClass"]]


def classify(tool_name: str, tool: Any = None,
             overrides: Optional[RiskOverrides] = None) -> RiskClass:
    """The effective risk of a call.

    Order: user-local override > the table above > a glob rule > the tool's own
    declared `.risk` (how an out-of-tree/plugin tool classifies itself) > EXTERNAL.

    The tool's self-declaration ranks BELOW the table on purpose: a plugin must not
    be able to talk its way down the ladder past a decision made here.
    """
    if overrides is not None:
        ov = overrides(tool_name)
        if ov is not None:
            return ov
    base = _BASE.get(tool_name)
    if base is not None:
        return base
    best, best_score = None, -1
    for pattern, risk in _PATTERNS:
        if fnmatch.fnmatchcase(tool_name, pattern):
            score = _specificity(pattern)
            if score > best_score:
                best, best_score = risk, score
    if best is not None:
        return best
    declared = getattr(tool, "risk", None)
    if isinstance(declared, RiskClass):
        return declared
    if declared is not None:
        try:
            return RiskClass(str(declared))
        except ValueError:
            pass  # a garbage self-declaration falls through to the safe default
    return RiskClass.EXTERNAL


def is_classified(tool_name: str) -> bool:
    """True when this name is covered by a DECISION — the table or a glob rule — rather
    than by the fallback.

    `classify` returns EXTERNAL for both, which is safe but indistinguishable, so this is
    what "has anyone actually thought about this tool?" has to ask. The test that walks
    the live registry and `collie risk`'s "not in the table" list both depend on the
    difference: an MCP tool covered by the `mcp__*` rule is classified; a new built-in
    nobody has touched is not, and should be visible as such.
    """
    if tool_name in _BASE:
        return True
    return any(fnmatch.fnmatchcase(tool_name, p) for p, _ in _PATTERNS)


def _specificity(pattern: str) -> int:
    """More literal characters = more specific; an exact pattern beats any glob."""
    literal = sum(1 for c in pattern if c not in "*?[]")
    return literal + (0 if any(c in pattern for c in "*?[") else 1000)


def is_consequential(risk: RiskClass) -> bool:
    """Anything but a pure read is the gate's business."""
    return risk is not RiskClass.READ


# --------------------------------------------------------------------------- #
# Targets — what an "allow every time" would actually be pinned to.
# --------------------------------------------------------------------------- #
def origin_of(url: str) -> str:
    """scheme://host[:port] for a URL, or "" if it has no origin worth pinning."""
    try:
        p = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return ""
    if not p.scheme or not p.netloc:
        return ""
    return "%s://%s" % (p.scheme.lower(), p.netloc.lower())


def target_for(tool_name: str, args: dict, origin_lookup=None) -> Optional[str]:
    """The concrete thing a rule for this call would name — a page origin, an app —
    or None when the call cannot be pinned to one (then it is asked every time).

    `origin_lookup` is a zero-arg callable returning the browser's CURRENT origin. It
    is a live call, never a cached value: a cache is exactly how this gets bypassed —
    the model navigates elsewhere and the stale origin still reads as the approved one.
    """
    args = args or {}
    if tool_name in NO_STANDING_RULE:
        return None
    if tool_name == "browser_open":
        # Navigation names its own destination; that is the thing being authorized,
        # not wherever the tab happens to be right now.
        return origin_of(str(args.get("url") or "")) or None
    if tool_name.startswith("browser_"):
        if origin_lookup is None:
            return None
        try:
            return origin_of(str(origin_lookup() or "")) or None
        except Exception:
            return None
    if tool_name.startswith("desktop_"):
        app = str(args.get("app") or args.get("process") or args.get("window") or "").strip()
        return app.lower() or None
    return None
