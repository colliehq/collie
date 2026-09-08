"""Claude Code as an external Collie worker (``claude -p --output-format stream-json``).

This is the third and last thing in the repository that shells out to ``claude``,
and it is deliberately unlike the other two.  ``providers.ClaudeCliProvider`` and
``claude_agent_sdk`` are *Brains*: Collie owns the loop and asks Claude for one
inference.  ``adapters.ClaudeCodeAdapter`` is a *benchmark* shim and is allowed
``bypassPermissions`` because it only ever runs inside a throwaway container.  A
runner is neither: Claude Code owns its own loop and edits the user's real files,
so every flag here is chosen to keep that loop inside a boundary Collie can
describe afterwards.

What that means concretely:

* **No shell.**  ``--tools``/``--allowedTools`` are pinned to the five file tools.
  Claude Code has no approval channel we can route back into Collie's gate
  (``harness/gate.py``), so a tool it can run is a tool that runs unreviewed.
  The selection layer's H9 rule only offers this runner work whose needs are
  ``{"code"}`` *because* the tool list says so; ``_check_tools`` refuses to let a
  caller widen it and quietly turn that declaration into a lie.
* **The locator is ours, not Claude's.**  ``--session-id <uuid4>`` pins the
  session before the process starts, so a turn that is killed mid-write still
  leaves a resumable id in the snapshot.  Discovering the id from the result
  object would lose exactly the case the id exists for.
* **Fail closed on billing.**  ``assert_no_billing_override`` runs before any
  process exists: an ambient ``ANTHROPIC_API_KEY`` would silently move the charge
  from the user's subscription to their API account while the receipt still said
  "subscription", and that is not undoable after the fact.

Flags that are *not* here, and why: ``--max-turns`` would impose another fixed
turn ceiling on a product task. The comparison benchmark uses that optional
flag, but the product runner deliberately does not. No permission-prompt tool
is connected to Collie's approval channel. ``--no-session-persistence`` would
make ``resume`` impossible; ``bypassPermissions``
and the ``--dangerously-*`` family are the thing this module exists to avoid.
``_assert_safe_argv`` re-checks all of them at call time, because the argv is
assembled from constructor arguments and a review of this file is not evidence
about what a caller passed.

Process ownership, the start gate and the extinction proof are not reimplemented
here: :class:`~harness.agent_runners.SubprocessRunner` already does all three, and
a second implementation would be a second thing to get wrong.  This module
supplies the argv, the environment and the result parsing, and nothing else.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
import re
import shutil
import threading
import time
import uuid
from typing import Any, Callable

from . import runner_env, runner_specs
from .agent_runners import (
    ProcessOutcome,
    ProcessRunner,
    RecoveryRequiredError,
    RunnerEvent,
    RunnerSnapshot,
    SubprocessRunner,
    _bounded_payload,
    _cli_version,
    _clean_error,
    _finite_number,
    _lf_records,
    _mutation,
    _prompt,
    _reject_json_constant,
    _run_process,
    _snapshot,
    _terminate_owned_process,
    _workspace,
)
from .verification import workspace_snapshot
from .providers import provider_retry_at


KEY = "claude-code"
BINARY = "claude"
PROTOCOL = "claude-print-stream-json"
CREDENTIAL_FAMILY = "claude"
ENV_POLICY = "claude"

# The five file tools.  Adding to this list is a capability change, not a
# configuration change — see the module docstring and `_check_tools`.
DEFAULT_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Grep", "Glob")
READ_ONLY_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")

# What this runner claims it can do.  `runner_registry` intersects the claim with
# the last conformance report before showing it to anyone, so an unverified True
# here becomes a False there rather than a promise.  Declared once, in the module
# that would have to implement any of it.
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

# Tool names that would hand Claude Code a shell, a network fetch or a subagent.
# Compared upper-cased so a caller cannot slip one past on capitalization.
_UNREVIEWABLE_TOOLS = frozenset({
    "BASH", "BASHOUTPUT", "KILLSHELL", "KILLBASH", "TASK", "AGENT",
    "WEBFETCH", "WEBSEARCH", "COMPUTER", "EXECUTE",
})
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

# `--session-id` accepts a UUID.  Matching it exactly is also what keeps a
# resumed id — which arrives from a snapshot on disk — out of argv position.
_SESSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Counters from the result object's `usage` member, in Claude's own spelling.
# They stay in that spelling inside `RunnerSnapshot.usage`, because
# `runner_specs.usage_to_collie("claude-code", ...)` is what translates them and
# translating twice would drop the cache fields on the floor.
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

# argv elements that must never appear, whatever the caller asked for.
_BANNED_ARGV = frozenset({
    "--dangerously-skip-permissions", "--max-turns", "--no-session-persistence",
    "bypasspermissions",
})
_BANNED_PREFIX = "--dangerously"


class _QuotaTurn:
    """Current invocation's native quota and file-tool completion receipts.

    Model prose and old persisted events never establish a timer.  A completed
    file edit followed by a quota rejection can resume; an unmatched or failed
    tool receipt still requires inspection of the workspace.
    """

    def __init__(self):
        self.windows = {}
        self.rate_limited = False
        self.pending = set()
        self.seen = set()
        self.tools_complete = True
        self.saw_write = False

    def observe(self, value):
        kind = value.get("type")
        if kind == "rate_limit_event":
            info = value.get("rate_limit_info")
            if not isinstance(info, dict):
                return
            window = info.get("rateLimitType", info.get("rate_limit_type"))
            if window not in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
                return
            status = info.get("status")
            reset = provider_retry_at(info.get("resetsAt", info.get("resets_at")))
            if status == "rejected" and reset:
                self.windows[window] = reset
            else:
                self.windows.pop(window, None)
        elif kind in ("assistant", "user"):
            if kind == "assistant":
                self.rate_limited = value.get("error") == "rate_limit"
            message = value.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            if not isinstance(blocks, list):
                return
            for block in blocks:
                if not isinstance(block, dict):
                    self.tools_complete = False
                    continue
                if block.get("type") == "tool_use":
                    call_id = block.get("id")
                    name = block.get("name")
                    if (kind != "assistant" or not isinstance(call_id, str) or not call_id
                            or call_id in self.seen or name not in DEFAULT_TOOLS
                            or len(self.seen) >= 10000):
                        self.tools_complete = False
                        continue
                    self.seen.add(call_id)
                    self.pending.add(call_id)
                    self.saw_write = self.saw_write or name in ("Write", "Edit")
                elif block.get("type") == "tool_result":
                    call_id = block.get("tool_use_id")
                    if (kind != "user" or not isinstance(call_id, str)
                            or call_id not in self.pending
                            or block.get("is_error", False) is not False):
                        self.tools_complete = False
                        continue
                    self.pending.remove(call_id)

    def reset(self):
        return max(self.windows.values(), default=0) if self.rate_limited else 0


def _check_tools(tools: Any) -> tuple[str, ...]:
    """Validate the tool allowlist, refusing anything Collie could not review."""
    names = tuple(str(item).strip() for item in (tools or ()))
    if not names:
        raise ValueError("claude-code needs at least one tool")
    for name in names:
        if not _TOOL_NAME.fullmatch(name):
            # Also stops `--flag`, `mcp__server__*` and comma smuggling: the list
            # is joined with commas and handed to two flags.
            raise ValueError("not a Claude Code tool name: %r" % name)
        if name.lower().startswith("mcp"):
            # `--strict-mcp-config` means no MCP server is configured for this
            # worker, so an mcp__* tool is either dead or reaching a server
            # Collie never approved.
            raise ValueError("claude-code runs with --strict-mcp-config; %r cannot "
                             "be allowed" % name)
        if name.upper() in _UNREVIEWABLE_TOOLS:
            raise ValueError(
                "%s would let claude-code act with no approval path back into "
                "Collie's gate; this runner declares confinement=tools-allowlist "
                             "and the selector offers it work on that basis" % name)
        if name not in DEFAULT_TOOLS:
            raise ValueError("claude-code only supports the reviewed file tools: %s" %
                             ", ".join(DEFAULT_TOOLS))
    return names


def _assert_safe_argv(argv: list[str]) -> None:
    """Last check before a process is created; see the module docstring."""
    for item in argv:
        lowered = item.lower()
        if lowered in _BANNED_ARGV or lowered.startswith(_BANNED_PREFIX):
            raise ValueError("refusing to start claude-code with %r" % item)


def _budget(value: float) -> str:
    """Format a dollar cap for ``--max-budget-usd`` without exponent notation."""
    return ("%.6f" % float(value)).rstrip("0").rstrip(".") or "0"


class ClaudeCodeRunner:
    """Resumable Claude Code runner backed by ``claude -p --output-format stream-json``.

    One active child at a time, same contract as
    :class:`~harness.agent_runners.CodexExecRunner`: the caller persists the
    returned :class:`~harness.agent_runners.RunnerSnapshot` and hands it back to
    :meth:`resume`.

    ``environ`` is the *parent* environment the child's one is built from; it
    defaults to :data:`os.environ` at call time and exists so callers and tests
    can pass an explicit mapping instead of mutating the real one.
    """

    key = KEY
    credential_family = CREDENTIAL_FAMILY

    def __init__(self, *, executable: str = BINARY, model: str = "",
                 speed: str = "standard",
                 max_budget_usd: float | None = None,
                 tools: tuple[str, ...] = DEFAULT_TOOLS,
                 process_runner: ProcessRunner | None = None,
                 snapshotter: Callable[[str], dict[str, Any]] = workspace_snapshot,
                 default_timeout_s: float = 900.0,
                 environ: Mapping[str, str] | None = None,
                 session_ids: Callable[[], str] | None = None,
                 max_events: int = 2_000, max_event_chars: int = 128_000,
                 env_policy: str = ENV_POLICY,
                 event_callback: Callable[[RunnerEvent], Any] | None = None):
        if not _finite_number(default_timeout_s) or float(default_timeout_s) <= 0:
            raise ValueError("default_timeout_s must be positive")
        if (max_budget_usd is not None and
                (not _finite_number(max_budget_usd) or float(max_budget_usd) <= 0)):
            raise ValueError("max_budget_usd must be positive when set")
        # Validate the policy name now: a typo that only surfaced at launch time
        # would strand a half-built Mission slice instead of failing the caller.
        runner_env.allowlist(env_policy)
        self.env_policy = env_policy
        self.executable = executable
        self.model = model
        self.speed = str(speed or "standard").strip().lower()
        if self.speed not in ("standard", "fast"):
            raise ValueError("Claude Code speed must be standard or fast")
        self.max_budget_usd = None if max_budget_usd is None else float(max_budget_usd)
        self.tools = _check_tools(tools)
        self.process_runner = process_runner or SubprocessRunner()
        self.snapshotter = snapshotter
        self.default_timeout_s = float(default_timeout_s)
        self._environ = environ
        self._session_ids = session_ids or (lambda: str(uuid.uuid4()))
        self.max_events = max(1, int(max_events))
        self.max_event_chars = max(1_024, int(max_event_chars))
        self._event_callback = event_callback
        # {"allowed": [names], "stripped": [names]} for the most recent turn —
        # names only, so the caller can copy it straight into a run receipt.
        self.last_env_receipt: dict[str, list[str]] = {"allowed": [], "stripped": []}
        self._run_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active_condition = threading.Condition(self._active_lock)
        self._active_process: Any = None
        self._starting = False
        self._cancel_requested = False

    def set_event_callback(self, callback: Callable[[RunnerEvent], Any] | None) -> None:
        """Set the best-effort projection for complete native stream records."""
        self._event_callback = callback

    # --- public API ---------------------------------------------------------
    def start(self, prompt: str, workspace: str, *, timeout_s: float | None = None
              ) -> RunnerSnapshot:
        root = _workspace(workspace)
        session_id = str(self._session_ids() or "")
        if not _SESSION_ID.fullmatch(session_id):
            raise ValueError("session id factory did not produce a UUID")
        return self._invoke(None, self._argv(session_id, resume=False), prompt,
                            root, timeout_s, session_id)

    def resume(self, snapshot: RunnerSnapshot, prompt: str, *,
               timeout_s: float | None = None) -> RunnerSnapshot:
        if snapshot.runner != self.key:
            raise ValueError("snapshot belongs to %s, not %s" % (snapshot.runner, self.key))
        if snapshot.recovery_required:
            raise RecoveryRequiredError(
                "the previous Claude Code turn may have left a partial workspace "
                "mutation; inspect or roll back the workspace before resuming")
        root = _workspace(snapshot.workspace)
        if root != snapshot.workspace:
            raise ValueError("snapshot workspace is not canonical")
        session_id = snapshot.thread_id or ""
        if not _SESSION_ID.fullmatch(session_id):
            raise ValueError("snapshot has no safe Claude Code session id")
        return self._invoke(snapshot, self._argv(session_id, resume=True), prompt,
                            root, timeout_s, session_id)

    def cancel_current(self) -> bool:
        """Kill the owned process tree; True only on confirmed extinction."""
        deadline = time.monotonic() + 5.0
        with self._active_condition:
            if self._active_process is None and not self._starting:
                return False
            self._cancel_requested = True
            # A cancel can land after the invocation lock but before the trusted
            # gate is registered.  Wait for registration: `register` then sees
            # _cancel_requested and never releases the real target.
            while self._active_process is None and self._starting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._active_condition.wait(remaining)
            proc = self._active_process
            if proc is None:
                # The transport failed before creating a child: the invocation is
                # gone and no `claude` can start later.
                return True
        return _terminate_owned_process(
            proc, timeout_s=max(0.0, deadline - time.monotonic()))

    def cancel_for(self, key: str = "") -> bool:
        """Cancel this runner's active turn, whatever ``key`` the caller holds.

        Same contract as :meth:`~harness.agent_runners.CodexExecRunner.cancel_for`:
        the registry cancels by runner key, this runner owns at most one turn
        (``_run_lock``), so the instance *is* the answer and ``key`` is accepted
        and ignored rather than validated — refusing a mismatched key would turn
        "cancel everything" into a silent no-op on the runner the user meant.
        """
        return self.cancel_current()

    def probe(self, *, now: float | None = None) -> runner_specs.RunnerProbe:
        """Report what is true about the Claude Code CLI here — metadata only.

        ``shutil.which`` and one ``claude --version``.  Nothing else: no login
        file is opened and no network call is made.

        Deliberately **not** here: ``claude auth status --json``.  That is a live
        probe, so it belongs to the registry's ``--live`` path, which is also
        where ``billing_class`` is decided — and it is the only thing that can
        decide it, because Claude Code keeps its login in the OS keychain or in
        ``~/.claude``, and neither the presence of a file nor the fact that the
        binary exists says which plan pays.  A metadata-only probe therefore
        reports ``login="unknown"`` and is not ``usable()`` on its own.
        """
        now = time.time() if now is None else float(now)
        capabilities = CAPABILITIES.to_dict()
        resolved = shutil.which(self.executable) or ""
        if not resolved:
            return runner_specs.RunnerProbe(
                key=self.key, installed=False, capabilities=capabilities,
                probed_at=now,
                detail="the Claude Code CLI (%s) is not installed or not on PATH; "
                       "install it or pass an absolute path" % self.executable)
        # Shared with CodexExecRunner: one place decides what "installed but the
        # CLI will not answer" looks like in a probe row.
        version, version_error = _cli_version(resolved)
        return runner_specs.RunnerProbe(
            key=self.key, installed=True, executable_path=resolved, version=version,
            capabilities=capabilities, probed_at=now, detail=version_error)

    # --- argv ---------------------------------------------------------------
    def _argv(self, session_id: str, *, resume: bool) -> list[str]:
        tools = ",".join(self.tools)
        argv = [
            self._executable(),
            "-p",                                # non-interactive; prompt on stdin
            "--output-format", "stream-json",    # live JSONL plus one terminal result
            "--verbose",                         # required by Claude for stream-json
            "--permission-mode", "acceptEdits",  # edits inside the tool allowlist, nothing else
            "--tools", tools,                    # the set that exists at all …
            "--allowedTools", tools,             # … and the set allowed without a prompt
            "--safe-mode",
            "--no-chrome",                       # never inherit the user's browser bridge
            "--disable-slash-commands",          # task text cannot invoke a skill/command
            "--prompt-suggestions", "false",     # no unsolicited next-prompt event/surface
            "--strict-mcp-config",               # the user's MCP servers are not this worker's
        ]
        if self.speed == "fast":
            # Claude Code's non-interactive `-p` mode does not inherit an
            # interactive `/fast` toggle for the session. Its documented wire
            # contract is a session-local settings object.
            argv += ["--settings", '{"fastMode":true}']
        # The locator is pinned on start and reused on resume: same uuid, two
        # different flags.  `--fork-session` is never passed, so the thread the
        # snapshot names is the thread that continues.
        argv += ["--resume", session_id] if resume else ["--session-id", session_id]
        if self.model:
            argv += ["--model", self.model]
        if self.max_budget_usd is not None:
            # The only budget that can be enforced *inside* an external worker.
            argv += ["--max-budget-usd", _budget(self.max_budget_usd)]
        _assert_safe_argv(argv)
        return argv

    def _executable(self) -> str:
        # Resolve the npm shim now: on Windows CreateProcess ignores PATHEXT, so
        # a bare "claude" raises FileNotFoundError in ~0.2s and a comparison run
        # records that as "the other harness produced nothing"
        # (tests/test_core.py::test_every_agent_cli_is_resolved_on_path_before_exec).
        # Fake process runners intentionally do not need a real CLI on PATH.
        if not isinstance(self.process_runner, SubprocessRunner):
            return self.executable
        resolved = shutil.which(self.executable)
        if not resolved:
            raise FileNotFoundError("Claude Code CLI is not installed or not on PATH")
        return resolved

    def _parent_env(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    # --- one invocation -----------------------------------------------------
    def _invoke(self, prior: RunnerSnapshot | None, argv: list[str], prompt: str,
                workspace: str, timeout_s: float | None, session_id: str
                ) -> RunnerSnapshot:
        prompt = _prompt(prompt)
        timeout = self.default_timeout_s if timeout_s is None else float(timeout_s)
        if not _finite_number(timeout) or timeout <= 0:
            raise ValueError("timeout_s must be positive")

        # Before anything is created.  A wrongly-billed run cannot be refunded by
        # a later error message, so this raises rather than degrading — and it
        # raises BillingOverrideError, not FileNotFoundError, so `run_adhoc`'s
        # start-time fallback does not treat it as "try another runner": the
        # override would apply to that one too.
        parent = self._parent_env()
        runner_env.assert_no_billing_override(parent, self.credential_family)
        # The complete child environment, not a set of additions: the worker gets
        # the allowlist and nothing else, so it reads its own Claude Code login
        # rather than a credential Collie happened to be started with.
        env, env_receipt = runner_env.child_env(self.env_policy, environ=parent)
        self.last_env_receipt = env_receipt

        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("this Claude Code runner already has an active turn")

        with self._active_condition:
            self._cancel_requested = False
            self._starting = True
            self._active_process = None
            self._active_condition.notify_all()
        started_at = time.time()
        before = _snapshot(self.snapshotter, workspace)
        prior_cursor = prior.cursor if prior else 0
        outcome: ProcessOutcome | None = None
        raised: Exception | None = None
        process_started = False

        def register(proc: Any) -> bool:
            nonlocal process_started
            process_started = True
            if getattr(proc, "_collie_tree_lock", None) is None:
                proc._collie_tree_lock = threading.RLock()
            with self._active_condition:
                self._active_process = proc
                self._starting = False
                allowed = not self._cancel_requested
                self._active_condition.notify_all()
                return allowed

        live_cursor = prior_cursor
        live_result_count = 0

        def live_stdout(record: str) -> None:
            nonlocal live_cursor, live_result_count
            callback = self._event_callback
            if callback is None:
                return
            # The wire boundary is LF (with optional CR), not every Unicode
            # character Python calls a line separator.  U+2028 is legal inside
            # a JSON string and must not invent two records.
            for raw in record.split("\n"):
                raw = raw[:-1] if raw.endswith("\r") else raw
                if not raw.strip():
                    continue
                live_cursor += 1
                event, _value, _malformed = self._event_from_line(raw, live_cursor)
                if event.type == "result":
                    live_result_count += 1
                    if live_result_count > 1:
                        event = self._protocol_event(
                            raw, live_cursor, "duplicate terminal result")
                try:
                    callback(event)
                except Exception:
                    pass

        try:
            try:
                outcome = _run_process(
                    self.process_runner, tuple(argv), cwd=workspace,
                    stdin_text=prompt, timeout_s=timeout,
                    on_process=register, env=env, on_stdout=live_stdout)
                if not isinstance(outcome, ProcessOutcome):
                    raise TypeError("process runner must return ProcessOutcome")
            except Exception as exc:  # represented in state; the caller decides retry policy
                raised = exc
        finally:
            # A transport exception before or after registration must not strand
            # a cancellation waiter in the launch state.
            with self._active_condition:
                cancelled = self._cancel_requested
                self._active_process = None
                self._starting = False
                self._active_condition.notify_all()
            self._run_lock.release()

        finished_at = time.time()
        after = _snapshot(self.snapshotter, workspace)
        prior_events = tuple(prior.events) if prior else ()
        prior_usage = dict(prior.usage) if prior else {}
        invocation = (prior.invocation if prior else 0) + 1

        if raised is not None:
            mutated, complete = _mutation(before, after)
            # The session id is still ours even when nothing ran: pinning it up
            # front is what makes a killed start resumable.
            return RunnerSnapshot(
                runner=self.key, workspace=workspace, thread_id=session_id,
                cursor=prior_cursor, events=prior_events, usage=prior_usage,
                settled=False, exit_code=None,
                error=self._error("%s: %s" % (type(raised).__name__, raised)),
                recovery_required=mutated or (process_started and not complete),
                mutated=mutated, mutation_check_complete=complete,
                workspace_digest=str(after.get("tree_digest") or ""),
                final_output=prior.final_output if prior else "", timed_out=False,
                cancelled=cancelled, invocation=invocation,
                started_at=started_at, finished_at=finished_at)

        assert outcome is not None
        cancelled = bool(cancelled or outcome.cancelled)
        data, native_events, protocol_error, native_event_count, reported_ids, quota = self._parse_stream(
            outcome.stdout, prior_cursor)
        protocol_error = protocol_error or bool(outcome.output_truncated)

        thread_id = session_id
        reported = str(data.get("session_id") or "")
        if reported and reported not in reported_ids:
            reported_ids.append(reported)
        mismatched = [value for value in reported_ids if value != session_id]
        if mismatched:
            # `--session-id` was not honoured, so the transcript lives somewhere
            # other than where the snapshot says.  Keep the id that actually holds
            # the work (it is the only one worth resuming) but refuse to call the
            # turn settled: something about this CLI is not what we tested against.
            protocol_error = True
            if _SESSION_ID.fullmatch(mismatched[-1]):
                thread_id = mismatched[-1]

        result_text = runner_specs.redact_text(
            data.get("result"), 1_000_000)
        subtype = str(data.get("subtype") or "").lower()
        # `error_max_turns` / `max_turns` — Claude stopped at its own ceiling.  We
        # never ask for one in this product runner, so seeing this
        # means the ceiling came from the CLI's own default.
        turns_exhausted = "max_turn" in subtype
        is_error = data.get("is_error") is True
        exit_code = outcome.exit_code
        timed_out = bool(outcome.timed_out)
        retry_at = (quota.reset() if (is_error and type(exit_code) is int
                    and exit_code in (0, 1) and reported == session_id
                    and not timed_out and not cancelled and not protocol_error
                    and not turns_exhausted) else 0)

        settled = (exit_code == 0 and not is_error and not turns_exhausted
                   and not timed_out and not cancelled and not protocol_error)
        error = ""
        if timed_out:
            error = "Claude Code turn exceeded its %.1fs wall timeout" % timeout
        elif cancelled:
            error = "Claude Code turn was cancelled"
        elif outcome.output_truncated:
            error = "Claude Code output exceeded Collie's bounded capture limit"
        elif protocol_error:
            error = ("Claude Code emitted an unparseable or inconsistent "
                     "--output-format stream-json result: %s"
                     % (outcome.stderr or outcome.stdout or "(no output)"))
        elif turns_exhausted:
            error = "Claude Code stopped at its own turn limit (subtype=%s)" % subtype
        elif retry_at:
            error = "HTTP 429: Claude Code rate limit; subscription resets at %s UTC" % (
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(retry_at)))
        elif is_error:
            error = result_text or subtype or "Claude Code reported is_error"
        elif exit_code is None:
            error = "Claude Code process exit status unavailable"
        elif exit_code != 0:
            error = outcome.stderr or "Claude Code exited with status %s" % exit_code

        usage = self._usage(prior_usage, data)
        events = prior_events + tuple(native_events)
        mutated, complete = _mutation(before, after)
        recovery = (not settled) and (mutated or (process_started and not complete))
        if (retry_at and complete and quota.tools_complete and not quota.pending
                and (not mutated or quota.saw_write)):
            recovery = False
        elif retry_at and (not quota.tools_complete or quota.pending):
            recovery = True
        return RunnerSnapshot(
            runner=self.key, workspace=workspace, thread_id=thread_id,
            cursor=prior_cursor + native_event_count,
            events=events[-self.max_events:], usage=usage,
            settled=settled, exit_code=exit_code, error=self._error(error),
            recovery_required=recovery, mutated=mutated,
            mutation_check_complete=complete,
            workspace_digest=str(after.get("tree_digest") or ""),
            final_output=result_text or (prior.final_output if prior else ""),
            timed_out=timed_out, cancelled=cancelled, invocation=invocation,
            started_at=started_at, finished_at=finished_at, retry_at=retry_at)

    # --- parsing ------------------------------------------------------------
    def _parse_stream(self, stdout: str, prior_cursor: int
                      ) -> tuple[dict[str, Any], list[RunnerEvent], bool, int,
                                 list[str], _QuotaTurn]:
        """Parse strict LF/CRLF JSONL and return its terminal result object.

        Every non-empty record becomes durable evidence.  Noise is recorded as
        ``protocol.invalid_json`` rather than scavenging a plausible result out
        of model-controlled text.  Exactly one terminal ``type=result`` record
        is required for a settled invocation.
        """
        from collections import deque
        events: deque[RunnerEvent] = deque(maxlen=self.max_events)
        result: dict[str, Any] = {}
        malformed = False
        result_count = 0
        event_count = 0
        reported_ids: list[str] = []
        saw_result = False
        quota = _QuotaTurn()
        for raw in _lf_records(stdout):
            if not raw.strip():
                continue
            event_count += 1
            event, value, invalid = self._event_from_line(
                raw, prior_cursor + event_count)
            if saw_result:
                event = self._protocol_event(
                    raw, event.cursor, "event emitted after terminal result")
                invalid = True
            reported_id = (str(event.payload.get("session_id") or "")
                           if isinstance(event.payload, dict) else "")
            if reported_id and reported_id not in reported_ids:
                if len(reported_ids) < 2:
                    reported_ids.append(reported_id)
                else:
                    malformed = True
            if not invalid and value.get("type") == "result":
                result_count += 1
                if result_count == 1:
                    result = value
                    saw_result = True
                else:
                    event = self._protocol_event(
                        raw, event.cursor, "duplicate terminal result")
                    invalid = True
            if not invalid:
                quota.observe(value)
            events.append(event)
            malformed = malformed or invalid
        if result_count == 0:
            malformed = True
            if events and not any(event.type == "protocol.invalid_json"
                                  for event in events):
                event_count += 1
                events.append(self._protocol_event(
                    "", prior_cursor + event_count,
                    "stream ended without a terminal result"))
        return result, list(events), malformed, event_count, reported_ids, quota

    def _event_from_line(self, raw: str, cursor: int
                         ) -> tuple[RunnerEvent, dict[str, Any], bool]:
        if len(raw) > self.max_event_chars:
            malformed = True
            value = {
                "type": "protocol.event_too_large",
                "reason": "event exceeds %d characters" % self.max_event_chars,
                "preview": runner_specs.redact_text(
                    raw, min(self.max_event_chars, 4_096)),
            }
        else:
            try:
                value = json.loads(raw, parse_constant=_reject_json_constant)
                if not isinstance(value, dict):
                    raise ValueError("event is not an object")
                malformed = False
            except (ValueError, json.JSONDecodeError, RecursionError):
                malformed = True
                value = {
                    "type": "protocol.invalid_json",
                    "preview": runner_specs.redact_text(
                        raw, min(self.max_event_chars, 4_096)),
                }
        payload = runner_specs.redact_value(
            _bounded_payload(value, self.max_event_chars), self.max_event_chars)
        unsafe_structure = bool(
            isinstance(payload, dict) and payload.get("truncated") is True and
            payload.get("reason") in ("max-depth", "cycle", "unsafe-structure"))
        if unsafe_structure:
            malformed = True
            payload = dict(payload, type="protocol.invalid_json")
        if not isinstance(payload, dict):
            payload = {"type": str(value.get("type") or "unknown"),
                       "truncated": True}
        event = RunnerEvent(
            cursor=cursor, type=("protocol.invalid_json" if unsafe_structure else
                                 runner_specs.redact_text(
                                     value.get("type") or "unknown", 256)),
            payload=payload, at=time.time())
        return event, value, malformed

    def _protocol_event(self, raw: str, cursor: int, reason: str) -> RunnerEvent:
        payload = {"type": "protocol.invalid_json", "reason": reason}
        if raw:
            payload["preview"] = runner_specs.redact_text(
                raw, min(self.max_event_chars, 4_096))
        return RunnerEvent(cursor=cursor, type="protocol.invalid_json",
                           payload=payload, at=time.time())

    def _usage(self, prior: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        """Cumulative usage for the thread, in Claude's own field names.

        Token counters add up across invocations, matching
        ``RunnerSnapshot.usage``'s documented "cumulative across all turns".

        ``total_cost_usd`` takes the maximum instead: Claude's result object has
        been observed to report the cost of the whole *session*, not of the turn,
        and adding two session totals together would invent money that was never
        spent.  With one invocation — every phase-1 run — the two agree; the
        conformance matrix's ``usage`` check is what settles the multi-turn case.
        """
        usage: dict[str, Any] = {}
        for key, amount in (prior or {}).items():
            if (_finite_number(amount) and float(amount) >= 0
                    and float(amount) <= (1 << 63) - 1):
                usage[str(key)] = amount
        counts = data.get("usage")
        counts = counts if isinstance(counts, dict) else {}
        for key in _USAGE_KEYS:
            value = counts.get(key)
            if (_finite_number(value) and float(value) >= 0
                    and float(value) <= (1 << 63) - 1):
                usage[key] = int(usage.get(key, 0)) + int(value)
        cost = data.get("total_cost_usd")
        if (_finite_number(cost) and float(cost) >= 0
                and float(cost) <= (1 << 63) - 1):
            # `RunnerSnapshot.from_dict` int-coerces every usage value, so a
            # sub-dollar float does not survive being persisted and read back —
            # it comes back as 0, which would read as "this run was free".  The
            # integer micro-dollar copy does survive, so the prior cost is
            # recovered from whichever of the two is larger.
            prior_cost = max(float(usage.get("total_cost_usd") or 0.0),
                             float(usage.get("cost_micro_usd") or 0) / 1_000_000.0)
            usage["total_cost_usd"] = max(prior_cost, float(cost))
            usage["cost_micro_usd"] = int(round(usage["total_cost_usd"] * 1_000_000))
        return usage

    @staticmethod
    def _error(value: Any) -> str:
        """Both scrubbers, in the order §E prescribes: CLI text is quoted here."""
        return runner_specs.redact_text(_clean_error(value))


__all__ = ["CAPABILITIES", "DEFAULT_TOOLS", "ClaudeCodeRunner", "KEY", "PROTOCOL"]
