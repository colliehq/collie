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
from .authority import (
    ActionIntent,
    AuthorityContext,
    AuthorityDecision,
    AuthorityEngine,
    RequestAuthority,
    GrantScope,
    intent_dict,
    intent_for,
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
    """Approval answers.

    The original four map directly to ACP. The scoped values are Collie-native and
    degrade to one-call acceptance when an external editor cannot render them.
    """
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"      # mints a (tool, target) rule for this run
    ALLOW_MISSION = "allow_mission"
    ALLOW_WORKFLOW = "allow_workflow"
    ALLOW_PROJECT = "allow_project"
    ALLOW_CONNECTION = "allow_connection"
    REJECT_ONCE = "reject_once"
    REJECT_ALWAYS = "reject_always"    # stop asking for this tool; deny for this run


ALLOWING = frozenset({Outcome.ALLOW_ONCE, Outcome.ALLOW_ALWAYS,
                      Outcome.ALLOW_MISSION, Outcome.ALLOW_WORKFLOW,
                      Outcome.ALLOW_PROJECT, Outcome.ALLOW_CONNECTION})


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
    # Authority v2 describes the intended result rather than only the tool's reach.
    effect: str = ""
    action: str = ""
    authorization_basis: str = ""
    notify: bool = False
    intent: dict = field(default_factory=dict)
    grant_options: tuple[str, ...] = ()


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
    # Persistent navigation policy.  It is intentionally separate from
    # session_rules: opening an ordinary site may be quiet while actions on the
    # page still pass through the normal external-action gate below.
    browser_site_access: str = "ask_every_site"
    browser_sensitive_hosts: tuple = field(default_factory=tuple)
    # Outcome-based authorization is the default user experience.  The legacy
    # risk/path checks above it remain fail-closed and continue to protect read-only,
    # test, path-scope, and unattended Auto modes.
    authority_enabled: bool = True
    authority_mode: str = "hands_off"
    authority_engine: Optional[AuthorityEngine] = None
    authority_context: AuthorityContext = field(default_factory=AuthorityContext)

    def __post_init__(self) -> None:
        self.cwd = Path(self.cwd).expanduser().resolve()
        if self.authority_engine is None:
            self.authority_engine = AuthorityEngine()
        self.authority_context.mode = self.authority_mode

    def begin_request(self, user_message: Any, *, project: str = "",
                      mission_id: str = "", workflow_id: str = "") -> None:
        """Compile authority from one authenticated user message.

        Surfaces call this once per user turn. Tool/page/model output must never reach
        this method; the Harness deliberately invokes it before any model call.
        """
        self.authority_context = AuthorityContext(
            request=RequestAuthority.compile(user_message), project=str(project or ""),
            mission_id=str(mission_id or ""), workflow_id=str(workflow_id or ""),
            mode=self.authority_mode)

    def extend_request(self, user_message: Any) -> None:
        """Add authenticated mid-run steering without accepting model/tool text."""
        extra = RequestAuthority.compile(user_message)
        current = self.authority_context.request
        self.authority_context.request = RequestAuthority(
            actions=frozenset(set(current.actions) | set(extra.actions)),
            recipients=tuple(dict.fromkeys(current.recipients + extra.recipients)),
            # The basis identifies both authenticated messages without retaining either.
            request_sha256=(current.request_sha256[:32] + extra.request_sha256[:32]),
            explicit=current.explicit or extra.explicit)

    # -- the decision -------------------------------------------------------
    def evaluate(self, tool_name: str, args: dict, tool: Any = None) -> Decision:
        args = args or {}
        risk = classify(tool_name, tool, self.risk_overrides, args)
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
            if path is None:
                resolver = getattr(tool, "_local_write_path", None)
                if callable(resolver):
                    try:
                        path = resolver(args)
                    except Exception:
                        # A trusted tool-specific resolver is the only evidence
                        # that an argument such as ``out_dir`` remains inside the
                        # project.  Treating its failure as "no path supplied"
                        # let Project mode fall through to the ordinary local-write
                        # allowance.  Fail closed at the point where that evidence
                        # disappeared; unattended Auto cannot ask, other modes may
                        # offer one explicit approval for this call.
                        return d(False, "write target could not be resolved",
                                 needs_user=self.mode is not Mode.AUTO)
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
        target = None
        try:
            from .mcpclient import MCPTool
            if isinstance(tool, MCPTool):
                target = tool._trusted_target()
        except (ImportError, TypeError, ValueError):
            target = None
        target = target or target_for(tool_name, args, self.origin_lookup)
        if tool_name == "browser_open" and target:
            from .browserpolicy import navigation_allowed_without_prompt
            if navigation_allowed_without_prompt(
                    self.browser_site_access, target, self.browser_sensitive_hosts):
                return d(True, "allowed by persistent browser site-access policy",
                         rule="browser site access → %s" % target, target=target)
            # Site access is also a privacy boundary: opening a sensitive logged-in
            # origin exposes its contents to the configured model. A generic preparation
            # allowance must not silently override the user's navigation policy.
            return d(False, "browser site-access policy requires approval for %s" % target,
                     needs_user=True, target=target,
                     rule_offer=self.standing_rule_offer(tool_name, target) or "")
        if target and (tool_name, target) in self.session_rules:
            rule = "%s → %s" % (tool_name, target)
            return d(True, "allowed by rule: " + rule, rule=rule, target=target)
        if self.mode is Mode.INTERACTIVE:
            return d(False, "interactive mode reviews every consequential action",
                     needs_user=True, target=target,
                     rule_offer=self.standing_rule_offer(tool_name, target) or "")
        if self.authority_enabled and self.authority_engine is not None:
            intent = intent_for(tool_name, args, risk=risk.value, target=target or "", tool=tool)
            result = self.authority_engine.decide(intent, self.authority_context)
            common = {
                "target": target,
                "effect": intent.effect.value,
                "action": intent.action,
                "authorization_basis": result.basis,
                "intent": intent_dict(intent),
                "grant_options": self._grant_options(intent),
            }
            if result.decision in (AuthorityDecision.ALLOW_SILENT,
                                   AuthorityDecision.ALLOW_NOTIFY):
                return d(True, result.reason, notify=result.decision is AuthorityDecision.ALLOW_NOTIFY,
                         rule=("authority: " + result.basis) if result.basis else "", **common)
            if result.decision is AuthorityDecision.DENY:
                return d(False, result.reason, **common)
            return d(False, result.reason, needs_user=True,
                     rule_offer=self.standing_rule_offer(tool_name, target) or "", **common)
        return d(False, "acts outside this machine", needs_user=True, target=target,
                  rule_offer=self.standing_rule_offer(tool_name, target) or "")

    # -- outcomes -----------------------------------------------------------
    def apply_outcome(self, outcome: "Outcome", tool_name: str, target: Optional[str],
                      decision: Optional[Decision] = None) -> None:
        """Record what the human chose, so the rest of the run honours it."""
        if outcome is Outcome.ALLOW_ALWAYS:
            # A rule needs something concrete to be pinned to. Without a target
            # "always" would mean "always, anywhere" — which is what we refuse to
            # let anyone express. No target, no rule: it degrades to allow-once.
            if target and tool_name not in NO_STANDING_RULE:
                self.session_rules.add((tool_name, target))
        elif outcome is Outcome.REJECT_ALWAYS:
            self.session_denied.add(tool_name)
        elif outcome in (Outcome.ALLOW_MISSION, Outcome.ALLOW_WORKFLOW,
                         Outcome.ALLOW_PROJECT, Outcome.ALLOW_CONNECTION):
            if decision is None or not decision.intent or self.authority_engine is None \
                    or self.authority_engine.store is None:
                raise ValueError("persistent authority needs a bounded action intent and store")
            raw = decision.intent
            try:
                intent = ActionIntent(
                    action=str(raw.get("action") or ""), effect=raw.get("effect"),
                    target=str(raw.get("target") or target or ""),
                    account=str(raw.get("account") or ""),
                    recipients=tuple(raw.get("recipients") or ()),
                    resource=str(raw.get("resource") or ""),
                    connection_id=str(raw.get("connection_id") or ""),
                    amount=raw.get("amount"), currency=str(raw.get("currency") or ""),
                    reversible=bool(raw.get("reversible")),
                    confidence=float(raw.get("confidence") or 0),
                    reason=str(raw.get("reason") or ""))
            except (TypeError, ValueError):
                raise ValueError("invalid bounded action intent")
            offered = set(decision.grant_options or ())
            wanted = {
                Outcome.ALLOW_MISSION: GrantScope.MISSION,
                Outcome.ALLOW_WORKFLOW: GrantScope.WORKFLOW,
                Outcome.ALLOW_PROJECT: GrantScope.PROJECT,
                Outcome.ALLOW_CONNECTION: GrantScope.CONNECTION,
            }[outcome]
            if wanted.value not in offered:
                raise ValueError("the requested grant scope was not offered")
            recipients = intent.recipients or self.authority_context.request.recipients
            self.authority_engine.store.add(
                scope=wanted, action=intent.action,
                project=self.authority_context.project if wanted in (
                    GrantScope.PROJECT, GrantScope.WORKFLOW, GrantScope.MISSION) else "",
                mission_id=self.authority_context.mission_id if wanted is GrantScope.MISSION else "",
                workflow_id=self.authority_context.workflow_id if wanted is GrantScope.WORKFLOW else "",
                connection_id=intent.connection_id if wanted is GrantScope.CONNECTION else "",
                target=intent.target, account=intent.account, recipients=recipients,
                max_amount=intent.amount, currency=intent.currency,
                source="approval")

    def _grant_options(self, intent: ActionIntent) -> tuple[str, ...]:
        """Scopes the current card may truthfully mint."""
        if intent.effect.value != "commit" or not intent.action \
                or intent.action == "external_change" or self.authority_engine is None \
                or self.authority_engine.store is None:
            return ()
        out = []
        if self.authority_context.mission_id:
            out.append(GrantScope.MISSION.value)
        if self.authority_context.workflow_id:
            out.append(GrantScope.WORKFLOW.value)
        if self.authority_context.project:
            out.append(GrantScope.PROJECT.value)
        if intent.connection_id:
            out.append(GrantScope.CONNECTION.value)
        return tuple(out)

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
