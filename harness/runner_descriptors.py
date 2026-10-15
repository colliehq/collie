"""Reviewed static Claude runner metadata; no SDK or process adapter imports."""
from __future__ import annotations
from typing import Any
from . import runner_specs

KEY = "claude-code"


BINARY = "claude"


PROTOCOL = "claude-print-stream-json"


CREDENTIAL_FAMILY = "claude"


ENV_POLICY = "claude"


DEFAULT_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Grep", "Glob")


READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")


CAPABILITIES = runner_specs.RunnerCapabilities(
    protocol=PROTOCOL,
    protocol_version="",            # pinned by `collie runners compat`, not by hand
    session_create=True,
    session_resume=True,            # --resume <uuid>
    session_fork=False,             # --fork-session exists; deliberately not used
    streaming=True,                 # complete native records are delivered live
    cursor_replay=False,
    steer=False,
    follow_up=False,                # a follow-up here means a whole new invocation
    cancel="process-tree",          # no native interrupt in -p mode
    approval_round_trip=False,      # the reason the tool list has no shell
    usage_tokens=True,
    usage_cost=True,                # total_cost_usd — the only runner that reports $
    quota_signals=False,
    request_gate=False,
    native_goal=False,
    native_scheduler=False,
    confinement="tools-allowlist",
    tools=frozenset({"code"}),
    needs_git_workspace=True,
    prompt_transport="stdin",
    windows_native=None,            # the compat report decides; .cmd shim resolved by which()
)


EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


_AUTO_EFFORT = frozenset({"", "auto", "default", "provider-default"})


def normalize_effort(value: Any) -> str:
    """Return the ``--effort`` level to pass, or ``""`` for "let the CLI decide".

    Strict on purpose, and in one place: an effort the person selected is either
    carried to the worker exactly or refused loudly.  Silently substituting the
    CLI default is the failure mode this function exists to prevent, so anything
    that is neither an Auto spelling nor a documented level raises.
    """
    level = str(value or "").strip().lower()
    if level in _AUTO_EFFORT:
        return ""
    if level not in EFFORT_LEVELS:
        # Same sentence `providers.resolve_reasoning_effort` uses, so one wrong
        # value reads the same whether a Brain or a worker refused it.
        raise ValueError("reasoning effort must be auto, %s, or %s"
                         % (", ".join(EFFORT_LEVELS[:-1]), EFFORT_LEVELS[-1]))
    return level
