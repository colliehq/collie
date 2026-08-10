"""The gate — allow / deny / ask for one proposed tool call.

The gate only DECIDES. The loop routes a `needs_user` decision to whatever surface is
attached (TUI prompt, web card, ACP's native permission request, a phone) and records
the answer. That split is what lets attended and unattended runs share one code path.

WHY THE DEFAULT MODE IS `project`, NOT `interactive`
----------------------------------------------------
Agents that live in a scratch directory and are handed folders one at a time can afford
to ask before every write and every command. collie cannot: you run `collie -p "fix the
bug"` inside your repo, and **that is the consent**. Asking again is noise, and an agent
that interrupts every `pytest` is not usable for the work collie exists to do.

So the boundary is drawn somewhere else. In `project` mode:

    reading                       — always fine
    writing / running INSIDE cwd  — covered by the consent you gave by launching here
    writing OUTSIDE cwd           — ask
    anything reaching OFF-machine — ask, every time, until a rule says otherwise

That last line is the one that matters. collie drives the user's real logged-in browser
and their real desktop; `browser_click` can send, post, buy, or delete under their
cookies. Nothing else in the tool set has that reach, and until now nothing gated it.

WHAT PATH SCOPING IS AND IS NOT
-------------------------------
In `project` mode `bash` runs unrestricted inside cwd, so `write_file` refusing a path
outside cwd is not a containment boundary — a determined agent writes the same bytes
with `sh -c`. It is a SAFETY net against the common accident (a mis-resolved or
hallucinated absolute path), not a security claim. The security boundary here is
`external`. Saying otherwise would be dressing up a convenience as a defence.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from .risk import (
    NO_STANDING_RULE,
    RiskClass,
    RiskOverrides,
    classify,
    is_consequential,
    target_for,
)

# Shell metacharacters that turn one allowlisted command into several. An allowlist entry
# runs WITHOUT asking, so prefix matching alone is unsafe: an entry for `git status` would
# auto-run `git status && rm -rf ~`. Any of these disqualifies the command from the
# allowlist and sends it to the human instead.
_SHELL_OPERATORS = (";", "&", "|", ">", "<", "`", "$(", "(", "\n", "\r")


def _has_shell_operators(command: str) -> bool:
    return any(op in command for op in _SHELL_OPERATORS)


class Mode(str, Enum):
    PLAN = "plan"                # read-only: explore and propose, change nothing
    REVIEW = "review"            # read-only findings tied to existing artifacts
    TEST = "test"                # read + allowlisted verification commands; never write
    PROJECT = "project"          # default — see the module docstring
    INTERACTIVE = "interactive"  # ask before every consequential call
    AUTO = "auto"                # allow everything (CI, benchmarks, sandboxes)


READ_ONLY_MODES = frozenset({Mode.PLAN, Mode.REVIEW})


class Outcome(str, Enum):
    """Deliberately the four values of ACP's PermissionOptionKind, so the editor
    adapter is a pass-through and Zed/JetBrains/neovim render their native prompt."""
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"      # mints a (tool, target) rule for this run
    REJECT_ONCE = "reject_once"
    REJECT_ALWAYS = "reject_always"    # stop asking for this tool; deny for this run


ALLOWING = frozenset({Outcome.ALLOW_ONCE, Outcome.ALLOW_ALWAYS})


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    needs_user: bool = False
    rule: str = ""          # set when a standing rule allowed it, so audit can cite it
    risk: str = ""
    target: Optional[str] = None
    # The rule an "always" answer would create, or "" when this call cannot carry one.
    # Surfaces read it to decide whether to OFFER "always" at all — an "always" button
    # that quietly degrades to allow-once is a lie told in the user's own interface.
    rule_offer: str = ""
    # Set by the loop, not the gate: the model's tool-call id. It is the idempotency key
    # for a parked approval, so a reconnecting surface finds the same question rather
    # than asking a second time.
    call_id: str = ""


@dataclass
class Gate:
    cwd: Path
    mode: Mode = Mode.PROJECT
    roots: list = field(default_factory=list)          # extra writable dirs
    allowed_commands: list = field(default_factory=list)
    # (tool, target) pairs approved for the rest of this run, and tools the user
    # rejected with "never ask again".
    session_rules: set = field(default_factory=set)
    session_denied: set = field(default_factory=set)
    risk_overrides: Optional[RiskOverrides] = None
    origin_lookup: Optional[Callable[[], str]] = None

    def __post_init__(self) -> None:
        self.cwd = Path(self.cwd).expanduser().resolve()

    # -- the decision -------------------------------------------------------
    def evaluate(self, tool_name: str, args: dict, tool: Any = None) -> Decision:
        args = args or {}
        risk = classify(tool_name, tool, self.risk_overrides)
        d = lambda ok, why, **kw: Decision(ok, why, risk=risk.value, **kw)   # noqa: E731

        if not is_consequential(risk):
            return d(True, "read")

        if self.mode in READ_ONLY_MODES:
            return d(False, "%s mode is read-only" % self.mode.value)

        if self.mode is Mode.TEST:
            if risk is RiskClass.EXEC:
                command = str(args.get("command") or args.get("cmd") or "")
                if self._command_allowed(command):
                    return d(True, "test mode: detected verification command")
                return d(False, "test mode only runs the proposed verification command")
            # Reads returned above. Everything else is a write or an external
            # side effect, neither of which Test is authorized to perform.
            return d(False, "test mode is read-only except for verification")

        if tool_name in self.session_denied:
            return d(False, "denied for this run")

        # Path scoping applies in every mode that is not read-only, including auto:
        # a mis-resolved path is an accident, and an accident does not care about mode.
        if risk is RiskClass.WRITE_LOCAL:
            path = args.get("path")
            if path is not None and not self._under_root(str(path)):
                if self.mode is Mode.AUTO:
                    return d(False, "path is outside the writable roots: %s" % path)
                return d(False, "writes outside %s need approval" % self.cwd,
                         needs_user=True, target=str(path))

        if self.mode is Mode.AUTO:
            return d(True, "auto mode")

        if risk is RiskClass.EXEC:
            command = str(args.get("command") or args.get("cmd") or "")
            if self._command_allowed(command):
                return d(True, "command on allowlist")
            # `project` mode: running things inside your own project is the whole job.
            if self.mode is Mode.PROJECT:
                return d(True, "project mode: commands run in %s" % self.cwd)
            return d(False, "running commands needs approval", needs_user=True)

        if risk is RiskClass.WRITE_LOCAL:
            if self.mode is Mode.PROJECT:
                return d(True, "project mode: writes inside %s" % self.cwd)
            return d(False, "writing files needs approval", needs_user=True)

        # -- external -------------------------------------------------------
        target = target_for(tool_name, args, self.origin_lookup)
        if target and (tool_name, target) in self.session_rules:
            rule = "%s → %s" % (tool_name, target)
            return d(True, "allowed by rule: " + rule, rule=rule, target=target)
        return d(False, "acts outside this machine", needs_user=True, target=target,
                 rule_offer=self.standing_rule_offer(tool_name, target) or "")

    # -- outcomes -----------------------------------------------------------
    def apply_outcome(self, outcome: "Outcome", tool_name: str, target: Optional[str]) -> None:
        """Record what the human chose, so the rest of the run honours it."""
        if outcome is Outcome.ALLOW_ALWAYS:
            # A rule needs something concrete to be pinned to. Without a target
            # "always" would mean "always, anywhere" — which is what we refuse to
            # let anyone express. No target, no rule: it degrades to allow-once.
            if target and tool_name not in NO_STANDING_RULE:
                self.session_rules.add((tool_name, target))
        elif outcome is Outcome.REJECT_ALWAYS:
            self.session_denied.add(tool_name)

    def standing_rule_offer(self, tool_name: str, target: Optional[str]) -> Optional[str]:
        """The rule an "always" answer would create, or None when the call cannot
        carry one (so the surface hides the option instead of offering a lie)."""
        if not target or tool_name in NO_STANDING_RULE:
            return None
        return "%s → %s" % (tool_name, target)

    # -- helpers ------------------------------------------------------------
    def _writable_roots(self) -> list:
        out = [self.cwd]
        for r in self.roots or []:
            try:
                out.append(Path(r).expanduser().resolve())
            except (OSError, ValueError):
                continue
        return out

    def _under_root(self, path: str) -> bool:
        try:
            p = Path(path).expanduser()
            cand = p.resolve() if p.is_absolute() else (self.cwd / p).resolve()
        except (OSError, ValueError):
            return False
        for root in self._writable_roots():
            try:
                cand.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _command_allowed(self, command: str) -> bool:
        """Two stages, and both are load-bearing. Reject anything carrying shell
        operators outright, then require an entry's tokens to be an exact argv PREFIX
        of the command's — so `git status` matches `git status -s`, but never
        `git statusfoo` and never a bare `git`."""
        if not command or _has_shell_operators(command):
            return False
        try:
            argv = shlex.split(command)
        except ValueError:
            return False        # unbalanced quotes: not something to auto-run
        if not argv:
            return False
        for allowed in self.allowed_commands or []:
            try:
                prefix = shlex.split(str(allowed))
            except ValueError:
                continue
            if prefix and argv[:len(prefix)] == prefix:
                return True
        return False


def mode_from_env(default: Mode = Mode.PROJECT) -> Mode:
    """COLLIE_MODE=plan|review|test|project|interactive|auto. An unrecognised value falls back to
    the default rather than failing the run — but never silently to something laxer."""
    raw = (os.environ.get("COLLIE_MODE") or "").strip().lower()
    try:
        return Mode(raw) if raw else default
    except ValueError:
        return default
