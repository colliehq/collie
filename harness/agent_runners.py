"""Durable adapters for running external coding-agent harnesses.

This module is deliberately separate from :mod:`harness.adapters`: benchmark
adapters are one-shot measurement shims, while an ``AgentRunner`` is a Mission
primitive with resumable state, cancellation, and conservative recovery rules.

The first implementation wraps Codex's documented non-interactive JSONL
interface.  Prompts are sent on stdin (never exposed in the process argv), the
workspace is bounded by Codex's ``workspace-write`` sandbox, and an interrupted
turn that may have changed files is *not* silently replayed.

Two things the host machine would otherwise contribute to a worker are cut off
here rather than trusted:

* **The user's Codex configuration.**  ``~/.codex/config.toml`` can register MCP
  servers, enable web search and relax the sandbox.  It is a document about the
  user's own interactive sessions, and nobody reviewed it for the task being
  delegated, so ``start`` passes the ``--ignore-*``/``--strict-config`` family
  and pins ``web_search`` off (see :meth:`CodexExecRunner.start`).
* **The parent environment.**  It routinely carries an API key or a base-URL
  override belonging to a *different* account than the one Collie probed.  The
  child gets :func:`harness.runner_env.child_env` and nothing else, and a parent
  variable that would re-route the billing refuses the launch outright.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import errno
import inspect
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from . import plat, runner_env, runner_specs
from .verification import workspace_snapshot


_THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_PERSISTED_EVENTS = 10_000
_MAX_PERSISTED_TEXT_CHARS = 1_000_000
_MAX_PERSISTED_PAYLOAD_CHARS = 128_000
_MAX_COUNTER = (1 << 63) - 1


def _finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _safe_counter(value: Any) -> int:
    if not _finite_number(value):
        return 0
    return max(0, min(_MAX_COUNTER, int(value)))


def _safe_float(value: Any) -> float:
    return float(value) if _finite_number(value) else 0.0
# Codex's `turn.completed` usage block (codex-rs exec_events.rs).  The keys are
# accumulated verbatim and only translated into Collie's own accounting shape by
# `runner_specs.usage_to_collie`, so a name Codex renames shows up as a missing
# key (None) rather than as a silent zero.
_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    # Cache *writes* are billed separately from cache reads.  Omitting this key
    # made a cache-priming turn look free.
    "cache_write_input_tokens",
)


# Codex's own wording for a write it refused on policy grounds, copied from
# `codex-rs/core/src/safety.rs` (PATCH_REJECTED_*_REASON).  Matching Codex's
# text rather than a guess keeps this from firing on ordinary tool errors such
# as "apply_patch verification failed", which the model routinely recovers from.
_POLICY_REFUSALS = (
    "rejected by user approval settings",
    "blocked by read-only sandbox",
    # execpolicy refusing a command outright, e.g.
    #   exec_command failed ...: CreateProcess { message: "Rejected(\"... rejected:
    #   blocked by policy\")" }
    # which `--ignore-rules` can produce for an ordinary shell call.
    "blocked by policy",
)


def _policy_refusal(stderr: str) -> str:
    """Return the first policy-refusal line Codex logged, or "" if there is none."""
    text = stderr or ""
    for line in text.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in _POLICY_REFUSALS):
            return _clean_error(line.strip(), limit=300)
    # Native Windows sandbox failures currently take a different route: Codex
    # reaches apply_patch, but the sandboxed token cannot open the workspace and
    # logs a verification failure carrying Win32 ERROR_ACCESS_DENIED.  An
    # ordinary context mismatch also says "apply_patch verification failed", so
    # require both halves before calling this a refusal.  Reproduced with Codex
    # CLI 0.149.0 on Windows 11 on 2026-08-27, both through this runner and by
    # invoking the same isolated argv directly.
    lowered = text.lower()
    if ("apply_patch verification failed" in lowered
            and ("access is denied" in lowered or "os error 5" in lowered)):
        return _clean_error(text, limit=300)
    return ""


def _windows_sandbox_override(
        executable: str,
        version_probe: Callable[[str], tuple[str, str]] | None = None) -> list[str]:
    """Pick a Windows sandbox level explicitly, because the default rejects writes.

    Codex only auto-approves a patch when it can prove a platform sandbox is
    enforcing the workspace boundary (``core/src/safety.rs`` ->
    ``get_platform_sandbox(windows_sandbox_level != Disabled)``).  On Windows
    that level comes from ``[windows] sandbox`` in ``~/.codex/config.toml`` and
    defaults to ``Disabled`` -- so the moment we pass ``--ignore-user-config``
    (which we must, to keep the host's MCP servers out of a Collie worker) every
    write is rejected with "writing is blocked by read-only sandbox" *while the
    process still exits 0*.  Measured on 0.149.0 / Windows 11 on 2026-08-22: the
    same prompt silently changed nothing without this override and applied its
    patch with it.

    ``unelevated`` is the restricted-token sandbox, which needs no administrator
    setup; ``elevated`` would additionally require a one-time admin install, so
    it is never chosen on the user's behalf.  POSIX has Seatbelt/bwrap available
    unconditionally and needs no override.

    ``sandbox_private_desktop`` then has to be turned off, and that one is
    subtle.  Codex defaults it on, so the sandboxed child gets its own window
    station and desktop.  Collie launches every agent through a start gate with
    ``CREATE_NO_WINDOW`` and pipes for stdio -- there is no console and no
    interactive desktop to derive one from -- and the sandbox quietly degrades
    to refusing every write instead of failing loudly.  Measured on Windows 11 /
    0.149.0 on 2026-08-22: identical argv and environment, run as a direct child
    it applied its patch, run under the gate it reported "the workspace is
    read-only" and changed nothing; adding this one override made the same run
    write the file.  Collie's Job Object already owns the process tree, so the
    private desktop was never what bounded the worker.

    That second override is version-scoped by a schema fact, not a preference.
    Codex 0.156.0 (verified 2026-09-22) dropped ``sandbox_private_desktop`` from
    ``WindowsToml`` in ``codex-rs/config/src/types.rs``, which is
    ``#[schemars(deny_unknown_fields)]``, and lists it as a removed setting in
    ``codex-rs/config/src/strict_config.rs``.  Under ``--strict-config`` an
    unknown ``-c`` field is a hard ``io::ErrorKind::InvalidData``, so sending
    the old pair to a 0.156 CLI kills the launch before ``initialize``.

    ``version_probe`` answers for ``executable`` — the file this launch will
    actually run — not for whatever ``codex`` is first on PATH and not for the
    SDK's bundled pin, which is a different binary on a different schedule (see
    :mod:`harness.codex_sdk_worker`).  An unidentifiable CLI keeps the measured
    override, because the two ways to be wrong are not symmetric: keeping the
    key against a 0.156 host fails loudly at startup and changes nothing, while
    dropping it against the 0.149-era host that was actually measured reinstates
    a sandbox that refuses every write while still exiting 0.

    The sandbox *level* is never version-gated: ``derive_permission_profile``
    still downgrades workspace-write to read-only whenever the Windows level is
    ``Disabled``, and 0.156's new ``mxc`` mode maps to ``Disabled``, so
    ``unelevated`` remains the only choice Collie can make on the user's behalf.
    Whether a 0.156 sandbox then *writes* is not settled here; see
    ``docs/runners.md`` for what this gate has and has not been run against.
    """
    if not plat.is_windows():
        return []
    override = ["-c", 'windows.sandbox="unelevated"']
    probe = version_probe or _resolved_cli_version
    version, _error = probe(executable)
    if _needs_private_desktop_override(version):
        override += ["-c", "windows.sandbox_private_desktop=false"]
    return override

# Codex 0.149.0 is not consistent about the name of the usage block on
# `turn.completed`: older builds emit `usage`, newer ones `token_usage`.  Read
# both rather than pick one — the cost of guessing wrong is usage that silently
# reads as zero, which is the one failure this layer exists to prevent.
_USAGE_CONTAINERS = ("usage", "token_usage")


# The target agent must not get even one instruction byte until the parent has
# installed its process-tree owner and published the process to cancel_current.
# A tiny trusted Python gate is used instead of launching Codex directly: the
# gate blocks in ``stdin.read()``; only after ownership/registration succeeds do
# we send the target argv and private prompt.  The target inherits the gate's
# POSIX process group or Windows Job Object.
_START_GATE_SCRIPT = r"""
import json
import os
import subprocess
import sys

def reject_constant(value):
    raise ValueError("non-finite JSON number is forbidden: %s" % value)

try:
    request = json.loads(sys.stdin.read(), parse_constant=reject_constant)
    argv = request.get("argv")
    prompt = request.get("stdin_text")
    if (not isinstance(argv, list) or not argv or
            not all(isinstance(item, str) and item for item in argv) or
            not isinstance(prompt, str)):
        raise ValueError("invalid gated process request")
    child = subprocess.Popen(
        argv, stdin=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace",
        **({"creationflags": 0x08000000} if os.name == "nt" else {}))
    child.communicate(input=prompt)
    raise SystemExit(child.returncode if child.returncode is not None else 125)
except SystemExit:
    raise
except BaseException as exc:
    sys.stderr.write("gated agent launch failed: %s: %s\n" %
                     (type(exc).__name__, exc))
    raise SystemExit(125)
"""


@dataclass(frozen=True)
class RunnerEvent:
    """One canonical, cursor-addressable event emitted by an agent runner."""

    cursor: int
    type: str
    payload: dict[str, Any]
    at: float

    def to_dict(self) -> dict[str, Any]:
        return {"cursor": self.cursor, "type": self.type,
                "payload": self.payload, "at": self.at}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunnerEvent":
        value = value if isinstance(value, dict) else {}
        payload = value.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        payload = runner_specs.redact_value(
            _bounded_payload(payload, _MAX_PERSISTED_PAYLOAD_CHARS),
            _MAX_PERSISTED_PAYLOAD_CHARS)
        if not isinstance(payload, dict):
            payload = {"type": "unknown", "truncated": True}
        event_type = runner_specs.redact_text(
            value.get("type") or "unknown", 256)
        return cls(cursor=_safe_counter(value.get("cursor")),
                   type=event_type,
                   payload=payload,
                   at=_safe_float(value.get("at")))


@dataclass(frozen=True)
class RunnerSnapshot:
    """Serializable state required to inspect or resume an external harness.

    ``events`` is a bounded tail, while ``cursor`` remains monotonic even after
    old events are compacted.  ``usage`` is cumulative across all turns in the
    thread.  The terminal fields describe the most recent invocation.
    """

    runner: str
    workspace: str
    thread_id: str = ""
    cursor: int = 0
    events: tuple[RunnerEvent, ...] = ()
    usage: dict[str, int | float] = field(default_factory=dict)
    settled: bool = False
    exit_code: int | None = None
    error: str = ""
    recovery_required: bool = False
    mutated: bool = False
    mutation_check_complete: bool = False
    workspace_digest: str = ""
    final_output: str = ""
    timed_out: bool = False
    cancelled: bool = False
    invocation: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    terminal_state: str = ""  # completed | failed | cancelled | interrupted | waiting
    pending_interactions: tuple[runner_specs.PendingInteraction, ...] = ()
    retry_at: int = 0  # provider-attested quota reset; persisted across restarts

    def __post_init__(self) -> None:
        if type(self.retry_at) is not int or not 0 < self.retry_at < 2 ** 53:
            object.__setattr__(self, "retry_at", 0)
        pending: list[runner_specs.PendingInteraction] = []
        for item in tuple(self.pending_interactions or ())[-256:]:
            if isinstance(item, runner_specs.PendingInteraction):
                pending.append(item)
            elif isinstance(item, dict):
                pending.append(runner_specs.PendingInteraction.from_dict(item))
        object.__setattr__(self, "pending_interactions", tuple(pending))
        state = str(self.terminal_state or "")
        if state not in ("", "completed", "failed", "cancelled", "interrupted", "waiting"):
            state = ""
        if pending:
            state = "waiting"
            object.__setattr__(self, "settled", False)
        elif not state:
            if self.settled:
                state = "completed"
            elif self.cancelled:
                state = "cancelled"
            elif self.timed_out or self.recovery_required:
                state = "interrupted"
            elif self.error:
                state = "failed"
        object.__setattr__(self, "terminal_state", state)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runner": self.runner,
            "workspace": self.workspace,
            "thread_id": self.thread_id,
            "cursor": self.cursor,
            "events": [event.to_dict() for event in self.events],
            "usage": dict(self.usage),
            "settled": self.settled,
            "exit_code": self.exit_code,
            "error": self.error,
            "recovery_required": self.recovery_required,
            "mutated": self.mutated,
            "mutation_check_complete": self.mutation_check_complete,
            "workspace_digest": self.workspace_digest,
            "final_output": self.final_output,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "invocation": self.invocation,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "terminal_state": self.terminal_state,
            "pending_interactions": [item.to_dict()
                                     for item in self.pending_interactions],
            **({"retry_at": self.retry_at} if self.retry_at else {}),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunnerSnapshot":
        value = value if isinstance(value, dict) else {}
        usage = {}
        raw_usage = value.get("usage")
        if isinstance(raw_usage, dict):
            for key, amount in list(raw_usage.items())[:256]:
                if (_finite_number(amount) and float(amount) >= 0
                        and float(amount) <= _MAX_COUNTER):
                    # Preserve Claude's dollar float.  Integer counters remain
                    # integers, so ordinary snapshots still round-trip exactly.
                    usage[str(key)[:256]] = (int(amount)
                                              if float(amount).is_integer()
                                              else float(amount))
        exit_code = value.get("exit_code")
        raw_events = value.get("events")
        raw_events = raw_events if isinstance(raw_events, (list, tuple)) else ()
        raw_events = raw_events[-_MAX_PERSISTED_EVENTS:]
        raw_interactions = value.get("pending_interactions")
        raw_interactions = (raw_interactions
                            if isinstance(raw_interactions, (list, tuple)) else ())
        workspace = str(value.get("workspace") or "")
        workspace = (os.path.realpath(os.path.abspath(workspace))
                     if workspace and "\x00" not in workspace else "")
        return cls(
            runner=str(value.get("runner") or "")[:256],
            workspace=workspace,
            thread_id=str(value.get("thread_id") or "")[:256],
            cursor=_safe_counter(value.get("cursor")),
            events=tuple(RunnerEvent.from_dict(item)
                         for item in raw_events if isinstance(item, dict)),
            usage=usage,
            settled=value.get("settled") is True,
            exit_code=(int(exit_code) if _finite_number(exit_code)
                       and abs(float(exit_code)) <= _MAX_COUNTER else None),
            error=runner_specs.redact_text(value.get("error"), 4_000),
            recovery_required=value.get("recovery_required") is True,
            mutated=value.get("mutated") is True,
            mutation_check_complete=value.get("mutation_check_complete") is True,
            workspace_digest=str(value.get("workspace_digest") or "")[:512],
            final_output=runner_specs.redact_text(
                value.get("final_output"), _MAX_PERSISTED_TEXT_CHARS),
            timed_out=value.get("timed_out") is True,
            cancelled=value.get("cancelled") is True,
            invocation=_safe_counter(value.get("invocation")),
            started_at=_safe_float(value.get("started_at")),
            finished_at=_safe_float(value.get("finished_at")),
            terminal_state=str(value.get("terminal_state") or ""),
            pending_interactions=tuple(
                runner_specs.PendingInteraction.from_dict(item)
                for item in raw_interactions[-256:]
                if isinstance(item, dict)),
            retry_at=value.get("retry_at", 0),
        )


@dataclass(frozen=True)
class ProcessOutcome:
    """Result returned by an injectable process transport."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    output_truncated: bool = False


class ProcessRunner(Protocol):
    def run(self, argv: Sequence[str], *, cwd: str, stdin_text: str,
            timeout_s: float, on_process: Callable[[Any], bool | None],
            env: Mapping[str, str] | None = None,
            on_stdout: Callable[[str], Any] | None = None) -> ProcessOutcome:
        """Run ``argv`` to completion under caller-owned cancellation.

        ``env`` is the *complete* environment for the child (as built by
        :func:`harness.runner_env.child_env`), not a set of additions.  ``None``
        means "inherit this process's environment", which is what every caller
        did before the selection layer existed and is kept so an embedder's own
        transport keeps working unchanged.  A transport that accepts
        ``on_stdout`` calls it for each complete stdout record as it arrives;
        callers feature-test that optional extension so older injected
        transports remain valid.
        """
        ...


class AgentRunner(Protocol):
    def start(self, prompt: str | runner_specs.RunInput, workspace: str, *,
              timeout_s: float | None = None
              ) -> RunnerSnapshot:
        ...

    def resume(self, snapshot: RunnerSnapshot,
               prompt: str | runner_specs.RunInput, *,
               timeout_s: float | None = None) -> RunnerSnapshot:
        ...

    def fork(self, snapshot: RunnerSnapshot, *,
             timeout_s: float | None = None) -> RunnerSnapshot:
        ...

    def compact(self, snapshot: RunnerSnapshot, *,
                timeout_s: float | None = None) -> RunnerSnapshot:
        ...

    def steer_current(self, message: str | runner_specs.RunInput) -> bool:
        ...

    def follow_up_current(self, message: str | runner_specs.RunInput) -> bool:
        ...

    def cancel_current(self) -> bool:
        ...


def _run_process(process_runner: ProcessRunner, argv: Sequence[str], *, cwd: str,
                 stdin_text: str, timeout_s: float,
                 on_process: Callable[[Any], bool | None],
                 env: Mapping[str, str] | None,
                 on_stdout: Callable[[str], Any] | None = None) -> ProcessOutcome:
    """Call a transport, opting into its backwards-compatible stream hook.

    ProcessRunner was public before live delivery existed and tests/embedders
    supply strict-signature implementations.  Signature inspection avoids
    catching a ``TypeError`` thrown *inside* a transport and misclassifying it
    as an old implementation.
    """
    kwargs: dict[str, Any] = {
        "cwd": cwd, "stdin_text": stdin_text, "timeout_s": timeout_s,
        "on_process": on_process, "env": env,
    }
    if on_stdout is not None:
        try:
            parameters = inspect.signature(process_runner.run).parameters.values()
            supports = any(parameter.name == "on_stdout" or
                           parameter.kind == inspect.Parameter.VAR_KEYWORD
                           for parameter in parameters)
        except (TypeError, ValueError):
            supports = False
        if supports:
            kwargs["on_stdout"] = on_stdout
    return process_runner.run(tuple(argv), **kwargs)


class RecoveryRequiredError(RuntimeError):
    """Raised when replaying a possibly half-applied mutation would be unsafe."""


def _process_wait(proc: Any, timeout_s: float) -> bool:
    """Confirm the direct process exited; deliberately reject an unknown state."""
    wait = getattr(proc, "wait", None)
    if not callable(wait):
        # Injectable process doubles have no OS process behind them.  Production
        # Popen objects always expose wait(), so this compatibility path cannot
        # weaken the real process-tree guarantee.
        return True
    try:
        wait(timeout=max(0.0, float(timeout_s)))
        return True
    except Exception:
        return False


def _terminate_posix_group(proc: Any, timeout_s: float) -> bool:
    """Kill and then prove extinction of the group captured before launch."""
    pgid = int(getattr(proc, "_collie_process_group", 0) or 0)
    if pgid <= 1:
        # Custom process transports used by embedders/tests may not establish a
        # group.  The production transport always records one on POSIX.
        plat.kill_tree(proc)
        return _process_wait(proc, timeout_s)
    try:
        os.killpg(pgid, getattr(signal, "SIGKILL", 9))
    except ProcessLookupError:
        setattr(proc, "_collie_tree_extinct", True)
        return True
    except PermissionError:
        pass            # Darwin answers EPERM for a group of killed, unreaped members: reap, probe
    except OSError:
        return False
    _process_wait(proc, min(1.0, timeout_s))
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            setattr(proc, "_collie_tree_extinct", True)
            return True
        except PermissionError:
            # Darwin, again, while a killed group whose leader is gone is still disappearing
            # (tool_process waits this out too). Giving up on the first EPERM made a timed-out
            # run on a loaded macOS host report a tree it could not confirm gone. Only ESRCH is
            # proof; keep waiting for it until the deadline.
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(.01)


def _terminate_owned_process(proc: Any, timeout_s: float = 5.0) -> bool:
    """Terminate an owned agent process tree and return only after extinction.

    ``kill`` delivery is not completion evidence.  Windows asks the Job Object
    for ``ActiveProcesses == 0``; POSIX polls the dedicated process group until
    ``killpg(..., 0)`` reports ESRCH.  The per-process lock serializes a user
    cancellation with transport timeout/finally cleanup.
    """
    lock = getattr(proc, "_collie_tree_lock", None)
    if lock is None:
        lock = threading.RLock()
        setattr(proc, "_collie_tree_lock", lock)
    with lock:
        if bool(getattr(proc, "_collie_tree_extinct", False)):
            return True
        owner = getattr(proc, "_collie_kill_job", None)
        if owner is not None:
            confirmed = False
            try:
                terminate_and_wait = getattr(owner, "terminate_and_wait", None)
                if callable(terminate_and_wait):
                    confirmed = bool(terminate_and_wait(timeout_s=timeout_s))
                else:
                    # Compatibility for injected owners.  A production Windows
                    # Job exposes terminate_and_wait and never takes this path.
                    delivered = bool(owner.terminate())
                    wait_extinct = getattr(owner, "wait_extinct", None)
                    confirmed = bool(wait_extinct(timeout_s=timeout_s)) \
                        if callable(wait_extinct) else bool(
                            delivered and _process_wait(proc, timeout_s))
            except Exception:
                confirmed = False
            setattr(proc, "_collie_tree_extinct", confirmed)
            return confirmed
        if not plat.is_windows():
            return _terminate_posix_group(proc, timeout_s)
        # Production refuses to run without a Job.  This fallback exists only
        # for injected ProcessRunner implementations and still confirms the
        # direct process rather than reporting success immediately after kill.
        plat.kill_tree(proc)
        confirmed = _process_wait(proc, timeout_s)
        setattr(proc, "_collie_tree_extinct", confirmed)
        return confirmed


class SubprocessRunner:
    """Killable, shell-free subprocess transport used in production."""

    def __init__(self, *, max_output_chars: int = 16 * 1024 * 1024):
        if int(max_output_chars) < 65_536:
            raise ValueError("max_output_chars must be at least 65536")
        self.max_output_chars = int(max_output_chars)

    def run(self, argv: Sequence[str], *, cwd: str, stdin_text: str,
            timeout_s: float, on_process: Callable[[Any], bool | None],
            env: Mapping[str, str] | None = None,
            on_stdout: Callable[[str], Any] | None = None) -> ProcessOutcome:
        target_argv = [str(item) for item in argv]
        if not target_argv or not all(target_argv):
            raise ValueError("agent argv must contain non-empty strings")
        group_kwargs = plat.new_group_kwargs()
        # The environment goes on the *gate*, not on the target: the gate spawns
        # the target with no env= of its own, so the target inherits exactly this
        # mapping.  Setting it here rather than inside `_START_GATE_SCRIPT` means
        # the credential-stripping applies to the gate interpreter too, and there
        # is only one place where a name could leak through.
        #
        # `None` keeps the pre-selection-layer behaviour byte for byte: Popen
        # inherits the parent environment when env is not passed at all.
        child_kwargs = {} if env is None else {"env": dict(env)}
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _START_GATE_SCRIPT],
            cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            **child_kwargs, **group_kwargs, **plat.no_window_kwargs())
        proc._collie_tree_lock = threading.RLock()
        if not plat.is_windows() and group_kwargs.get("start_new_session"):
            # Capture the group while the trusted gate leader is alive.  Once it
            # exits, getpgid(proc.pid) cannot rediscover background descendants.
            proc._collie_process_group = int(proc.pid)
        owner = None
        try:
            owner = plat.attach_kill_on_close_job(proc)
            if owner is not None:
                proc._collie_kill_job = owner
        except Exception:
            # The trusted gate has not received a target request, so killing its
            # direct process is sufficient even if Job assignment itself failed.
            plat.kill_tree(proc)
            _process_wait(proc, 5.0)
            raise RuntimeError("could not establish Codex process-tree ownership")
        outcome = None
        raised = None
        try:
            # Registration is the start latch.  Returning False means a cancel
            # arrived during launch; never send the request, so Codex never starts.
            if on_process(proc) is False:
                if not _terminate_owned_process(proc):
                    raise RuntimeError(
                        "Codex start was cancelled but process-tree extinction "
                        "could not be confirmed")
                outcome = ProcessOutcome(exit_code=getattr(proc, "returncode", None),
                                         cancelled=True)
            else:
                request = json.dumps(
                    {"argv": target_argv, "stdin_text": stdin_text},
                    ensure_ascii=True, separators=(",", ":"), allow_nan=False)
                # Always drain through the bounded path.  Direct AgentRunner and
                # quota/conformance callers may not ask for live events, but a
                # hostile or broken CLI must not gain unbounded host memory just
                # because no UI callback was installed.
                outcome = self._streaming_communicate(
                    proc, request, timeout_s, on_stdout or (lambda _record: None))
        except BaseException as exc:
            raised = exc
        finally:
            # A successful CLI can still leave a background writer.  Reap the
            # complete owned tree before its JSONL result becomes settled state.
            confirmed = _terminate_owned_process(proc)
            try:
                if owner is not None:
                    owner.close()
            finally:
                if not confirmed and raised is None:
                    raised = RuntimeError(
                        "Codex process-tree extinction could not be confirmed")
        if raised is not None:
            raise raised
        assert outcome is not None
        return outcome

    def _streaming_communicate(self, proc: Any, request: str, timeout_s: float,
                               on_stdout: Callable[[str], Any]) -> ProcessOutcome:
        """Drain both pipes while delivering complete stdout records live.

        The same collected strings are returned and parsed again after process
        extinction, so live delivery cannot weaken settled-state validation.
        Stderr is drained concurrently to prevent a verbose CLI deadlocking on
        a full pipe, but is never exposed to a consumer before redaction.
        """
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        truncated = [False]

        def drain(stream: Any, parts: list[str], callback=None) -> None:
            retained = 0
            pending: list[str] = []
            pending_complete = True
            try:
                while True:
                    # ``readline()`` without a size bound first allocates the
                    # whole physical line.  A hostile CLI can therefore defeat
                    # the capture limit with one multi-gigabyte, newline-free
                    # record even though we retain only 16 MiB afterwards.
                    # A sized readline preserves low-latency LF delivery while
                    # keeping the transient allocation bounded too.
                    chunk = stream.readline(65_536)
                    if chunk == "":
                        if callback is not None and pending and pending_complete:
                            try:
                                callback("".join(pending))
                            except Exception:
                                pass
                        break
                    remaining = self.max_output_chars - retained
                    if remaining > 0:
                        kept = chunk[:remaining]
                        parts.append(kept)
                        retained += len(kept)
                    else:
                        kept = ""
                    whole_chunk = len(kept) == len(chunk)
                    if not whole_chunk:
                        truncated[0] = True
                        pending_complete = False
                    if callback is not None and pending_complete:
                        pending.append(kept)
                    # Never publish a partial JSONL record.  A sized readline
                    # may return several chunks for one physical record, so
                    # assemble only until LF (or EOF above), and discard the
                    # pending copy as soon as the capture limit cuts it.
                    if chunk.endswith("\n"):
                        if callback is not None and pending and pending_complete:
                            try:
                                callback("".join(pending))
                            except Exception:
                                pass
                        pending.clear()
                        pending_complete = True
                    elif not pending_complete:
                        pending.clear()
            except Exception:
                # A pipe closed during cancellation is normal.  Exit status and
                # the complete parse below remain the source of truth.
                pass

        out_thread = threading.Thread(
            target=drain, args=(proc.stdout, stdout_parts, on_stdout),
            name="collie-runner-stdout", daemon=True)
        err_thread = threading.Thread(
            target=drain, args=(proc.stderr, stderr_parts),
            name="collie-runner-stderr", daemon=True)
        out_thread.start()
        err_thread.start()
        timed_out = False
        try:
            try:
                proc.stdin.write(request)
                proc.stdin.flush()
            except BrokenPipeError:
                # Match communicate(): stderr/exit status explains a gate that
                # died before accepting its request.
                pass
            except OSError as exc:
                # communicate() also ignores EINVAL: Windows reports a child
                # that exited before reading stdin that way (bpo-19612), not as
                # a broken pipe. Raising it replaced the CLI's own stderr and
                # exit code with a bare "[Errno 22] Invalid argument".
                if exc.errno != errno.EINVAL:
                    raise
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            try:
                proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                if not _terminate_owned_process(proc):
                    raise RuntimeError(
                        "Codex timed out and process-tree extinction could not be confirmed")
                _process_wait(proc, 5.0)
        finally:
            out_thread.join(timeout=5.0)
            err_thread.join(timeout=5.0)
        if out_thread.is_alive() or err_thread.is_alive():
            raise RuntimeError("agent output pipes did not close after process extinction")
        return ProcessOutcome(
            stdout="".join(stdout_parts), stderr="".join(stderr_parts),
            exit_code=getattr(proc, "returncode", None), timed_out=timed_out,
            output_truncated=truncated[0])


class CodexExecRunner:
    """Resumable Codex CLI runner backed by ``codex exec --json``.

    The runner owns one active child at a time.  A caller persists the returned
    :class:`RunnerSnapshot` in Mission state and passes it back to ``resume``.
    """

    key = "codex-exec"
    credential_family = "codex"

    def __init__(self, *, executable: str = "codex", model: str = "",
                 process_runner: ProcessRunner | None = None,
                 snapshotter: Callable[[str], dict[str, Any]] = workspace_snapshot,
                 default_timeout_s: float = 900.0, max_events: int = 2_000,
                 max_event_chars: int = 128_000, env_policy: str = "codex",
                 event_callback: Callable[[RunnerEvent], Any] | None = None,
                 cli_version_probe: Callable[[str], tuple[str, str]] | None = None):
        if not _finite_number(default_timeout_s) or float(default_timeout_s) <= 0:
            raise ValueError("default_timeout_s must be positive")
        # Validate the policy name now: a typo that only surfaced at launch time
        # would strand a half-built Mission slice instead of failing the caller
        # that got the name wrong.
        runner_env.allowlist(env_policy)
        self.env_policy = env_policy
        self.executable = executable
        self.model = model
        self.process_runner = process_runner or SubprocessRunner()
        self.snapshotter = snapshotter
        self.default_timeout_s = float(default_timeout_s)
        self.max_events = max(1, int(max_events))
        self.max_event_chars = max(1_024, int(max_event_chars))
        self._event_callback = event_callback
        # Seam for the Windows config-schema gate: tests and callers that must
        # not launch the installed CLI supply their own `(version, error)`.
        self._cli_version_probe = cli_version_probe
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
        """Set the best-effort callback used for live JSONL records.

        Snapshots remain authoritative and contain the same records.  The hook
        is only a low-latency projection for surfaces such as Web; exceptions
        raised by it are ignored by the transport.
        """
        self._event_callback = callback

    def start(self, prompt: str, workspace: str, *, timeout_s: float | None = None
              ) -> RunnerSnapshot:
        root = _workspace(workspace)
        # Before anything can spawn: building argv resolves the executable and
        # may run `--version` on it, so the refusal that :meth:`_invoke` states
        # as "before a process exists" has to happen here too.
        runner_env.assert_no_billing_override(os.environ, self.credential_family)
        # Every flag after --cd exists to stop the *host's* Codex configuration
        # from reaching a worker.  `~/.codex/config.toml` is a user document: it
        # can register MCP servers, enable web_search, add project trust rules
        # and set a different default sandbox, and none of that was reviewed for
        # the task Collie is about to hand over.
        #   --ignore-user-config  ignore ~/.codex/config.toml entirely
        #   --ignore-rules        ignore project/user rule files (AGENTS.md-style)
        #   --strict-config       an unparseable/unknown config key is an error,
        #                         not a shrug — a silent fallback here would put
        #                         the worker back on the host's defaults
        #   -c approval_policy=…  exec cannot answer approvals anyway; saying so
        #                         explicitly makes it fail closed instead of
        #                         inheriting an on-request policy that blocks.
        #                         NOTE: this must be a `-c` override, not the
        #                         `--ask-for-approval` flag — `codex exec`
        #                         0.149.0 rejects that flag outright ("error:
        #                         unexpected argument '--ask-for-approval'"),
        #                         which would make every start exit 2.
        #   -c web_search=…       the sandbox bounds the filesystem, not the
        #                         network; a task can otherwise pull in text that
        #                         nobody in this run ever saw
        # `--ephemeral` is deliberately NOT passed: it discards the thread, and
        # `resume` needs the thread id that `thread.started` reports.
        executable = self._executable()
        argv = [executable, "exec", "--json", "--sandbox", "workspace-write",
                "--cd", root, "--ignore-user-config", "--ignore-rules",
                "--strict-config", "-c", 'approval_policy="never"',
                "-c", 'web_search="disabled"']
        argv += _windows_sandbox_override(executable, self._cli_version_probe)
        if self.model:
            argv += ["--model", self.model]
        argv.append("-")
        return self._invoke(None, argv, prompt, root, timeout_s)

    def resume(self, snapshot: RunnerSnapshot, prompt: str, *,
               timeout_s: float | None = None) -> RunnerSnapshot:
        if snapshot.runner != self.key:
            raise ValueError("snapshot belongs to %s, not %s" % (snapshot.runner, self.key))
        if snapshot.recovery_required:
            raise RecoveryRequiredError(
                "the previous Codex turn may have left a partial workspace mutation; "
                "inspect or roll back the workspace before resuming")
        root = _workspace(snapshot.workspace)
        if root != snapshot.workspace:
            raise ValueError("snapshot workspace is not canonical")
        if not snapshot.thread_id or not _THREAD_ID.fullmatch(snapshot.thread_id):
            raise ValueError("snapshot has no safe Codex thread id")
        # Same reason as `start`: the version probe below is a child process.
        runner_env.assert_no_billing_override(os.environ, self.credential_family)
        # `resume` does not expose the top-level --sandbox flag.  An explicit
        # config override keeps the resumed turn at the same workspace-write
        # boundary even if the user's global default later changes; the same
        # argument applies to web_search, which `~/.codex/config.toml` can turn
        # on for every invocation.  `--ignore-user-config`/`--ignore-rules` were
        # verified against 0.149.0 on 2026-08-22 (resume returned the same
        # thread id and applied its patch), so the resumed turn gets the same
        # host-config isolation as the first one rather than silently falling
        # back to the user's MCP servers and rules half way through a thread.
        executable = self._executable()
        argv = [executable, "exec", "resume", "--json",
                "--ignore-user-config", "--ignore-rules",
                "--strict-config",
                "-c", 'sandbox_mode="workspace-write"',
                "-c", 'approval_policy="never"',
                "-c", 'web_search="disabled"']
        argv += _windows_sandbox_override(executable, self._cli_version_probe)
        if self.model:
            argv += ["--model", self.model]
        argv += [snapshot.thread_id, "-"]
        return self._invoke(snapshot, argv, prompt, root, timeout_s)

    def cancel_current(self) -> bool:
        deadline = time.monotonic() + 5.0
        with self._active_condition:
            if self._active_process is None and not self._starting:
                return False
            self._cancel_requested = True
            # Cancellation may win after the invocation lock but before Popen or
            # registration.  Wait for the trusted gate to become owned; register
            # will see _cancel_requested and refuse to release the real target.
            while self._active_process is None and self._starting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._active_condition.wait(remaining)
            proc = self._active_process
            if proc is None:
                # The process transport failed before creating a child.  The
                # requested invocation is gone and no target can start later.
                return True
        return _terminate_owned_process(
            proc, timeout_s=max(0.0, deadline - time.monotonic()))

    def cancel_for(self, key: str = "") -> bool:
        """Cancel this runner's active turn, whatever ``key`` the caller holds.

        The registry cancels by runner key because a caller that only has a
        selection decision has nothing else to name a run with.  This runner owns
        at most one turn at a time (``_run_lock``), so the instance *is* the
        answer and ``key`` carries no additional information — it is accepted and
        ignored rather than validated, because refusing a mismatched key would
        turn "cancel everything" into a silent no-op on exactly the runner the
        user meant.  Multi-session runners added later must key their own table.
        """
        return self.cancel_current()

    def probe(self, *, now: float | None = None, codex_home: str | None = None,
              ) -> runner_specs.RunnerProbe:
        """Report what is true about the Codex CLI on this host — metadata only.

        Read-only and offline by construction: ``shutil.which``, one
        ``codex --version``, and the *shape* of ``~/.codex/auth.json``.  The
        access token is passed to :func:`harness.ops._jwt_exp`, which decodes the
        unsigned ``exp`` claim; neither the token nor any other claim is read,
        stored or returned.

        Deliberately **not** here: ``codex login status``.  That is a live probe
        (it can hit the network and it re-reads the login), so it belongs to the
        registry's ``--live`` path, which is also where ``billing_class`` is
        decided.  A metadata-only probe of an OAuth login therefore reports
        ``billing_class="unknown"``: "signed in" is not evidence of *which* plan
        pays, and pretending otherwise is exactly the optimism the no-paid-overage
        rules exist to refuse.
        """
        now = time.time() if now is None else float(now)
        home = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        auth_path = os.path.join(home, "auth.json")
        resolved = shutil.which(self.executable) or ""
        if not resolved:
            return runner_specs.RunnerProbe(
                key=self.key, installed=False, probed_at=now,
                detail="the Codex CLI (%s) is not installed or not on PATH; "
                       "install it or pass an absolute path" % self.executable)

        version, version_error = _cli_version(resolved)
        login, login_detail, evidence = _codex_login_state(auth_path, now)
        billing_class = "unknown"
        if evidence.get("login_kind") == "api-key":
            # An API key in auth.json is a structural fact about the file, not a
            # reading of its value: that login is metered per request whatever
            # plan the same account also has.
            billing_class = "api_metered"
        return runner_specs.RunnerProbe(
            key=self.key, installed=True, executable_path=resolved, version=version,
            login=login, billing_class=billing_class,
            billing_mode=runner_specs.BILLING_MODE_OF.get(billing_class, "unconfigured"),
            billing_evidence=evidence, probed_at=now,
            detail=version_error or login_detail)

    def _executable(self) -> str:
        # Resolve npm/installer shims now, but permit an explicit absolute path.
        # Fake process runners intentionally do not need a real CLI on PATH.
        if not isinstance(self.process_runner, SubprocessRunner):
            return self.executable
        resolved = shutil.which(self.executable)
        if not resolved:
            raise FileNotFoundError("Codex CLI is not installed or not on PATH")
        return resolved

    def _invoke(self, prior: RunnerSnapshot | None, argv: list[str], prompt: str,
                workspace: str, timeout_s: float | None) -> RunnerSnapshot:
        prompt = _prompt(prompt)
        timeout = self.default_timeout_s if timeout_s is None else float(timeout_s)
        if not _finite_number(timeout) or timeout <= 0:
            raise ValueError("timeout_s must be positive")
        # Refuse *before* a process exists.  An OPENAI_BASE_URL or CODEX_API_KEY
        # in the parent shell would silently re-route or re-bill this turn, and
        # the operator unsetting it is cheaper than anyone reconstructing which
        # account paid.  Names only ever leave this call, never values.  Callers
        # that build argv first (start/resume, which may run `--version`) repeat
        # this check before they do; it stays here so every entry point is
        # covered on its own.
        runner_env.assert_no_billing_override(os.environ, self.credential_family)
        # The complete child environment, not a set of additions: the worker gets
        # the allowlist and nothing else, so it reads its own ~/.codex login
        # rather than a credential Collie happened to be started with.
        env, env_receipt = runner_env.child_env(self.env_policy)
        self.last_env_receipt = env_receipt
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("this Codex runner already has an active turn")

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

        def live_stdout(record: str) -> None:
            nonlocal live_cursor
            callback = self._event_callback
            if callback is None:
                return
            # The wire boundary is LF (with optional CR), not every Unicode
            # character Python calls a line separator. U+2028/U+2029 are legal
            # inside JSON strings and must not invent additional records.
            for raw in record.split("\n"):
                raw = raw[:-1] if raw.endswith("\r") else raw
                if not raw.strip():
                    continue
                live_cursor += 1
                event, _invalid = self._event_from_line(raw, live_cursor)
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
            except Exception as exc:  # represented in state; Mission decides retry policy
                raised = exc
            with self._active_condition:
                cancelled = self._cancel_requested
                self._active_process = None
                self._starting = False
                self._active_condition.notify_all()
        finally:
            # A ProcessRunner exception before/after registration must not strand
            # cancellation waiters in the launch state.
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
        thread_id = prior.thread_id if prior else ""
        invocation = (prior.invocation if prior else 0) + 1

        if raised is not None:
            mutated, complete = _mutation(before, after)
            recovery = mutated or (process_started and not complete)
            return RunnerSnapshot(
                runner=self.key, workspace=workspace, thread_id=thread_id,
                cursor=prior_cursor, events=prior_events, usage=prior_usage,
                settled=False, exit_code=None,
                error=_clean_error("%s: %s" % (type(raised).__name__, raised)),
                recovery_required=recovery, mutated=mutated,
                mutation_check_complete=complete,
                workspace_digest=str(after.get("tree_digest") or ""),
                final_output=prior.final_output if prior else "", timed_out=False,
                cancelled=cancelled, invocation=invocation,
                started_at=started_at, finished_at=finished_at)

        assert outcome is not None
        cancelled = bool(cancelled or outcome.cancelled)
        (parsed, invalid_json, parsed_count, new_thread_ids, terminal,
         terminal_error, usage, final_output) = self._events(
             outcome.stdout, prior_cursor, prior_usage)
        all_events = (prior_events + tuple(parsed))[-self.max_events:]
        cursor = prior_cursor + parsed_count
        protocol_error = invalid_json or bool(outcome.output_truncated)
        for candidate in new_thread_ids:
            if not candidate or not _THREAD_ID.fullmatch(candidate):
                protocol_error = True
                continue
            if thread_id and thread_id != candidate:
                protocol_error = True
            else:
                thread_id = candidate
        if not thread_id:
            protocol_error = True

        final_output = final_output or (prior.final_output if prior else "")
        exit_code = outcome.exit_code
        timed_out = bool(outcome.timed_out)
        settled = (exit_code == 0 and terminal == "completed" and not timed_out
                   and not cancelled and not protocol_error and bool(thread_id))
        error = ""
        if timed_out:
            error = "Codex turn exceeded its %.1fs wall timeout" % timeout
        elif cancelled:
            error = "Codex turn was cancelled"
        elif outcome.output_truncated:
            error = "Codex output exceeded Collie's bounded capture limit"
        elif protocol_error:
            error = "Codex emitted invalid or inconsistent JSONL state"
        elif terminal == "failed":
            error = terminal_error or "Codex reported turn.failed"
        elif exit_code is None:
            error = "Codex process exit status unavailable"
        elif exit_code not in (0, None):
            error = _clean_error(outcome.stderr) or "Codex exited with status %s" % exit_code
        elif terminal != "completed":
            error = _clean_error(outcome.stderr) or "Codex exited without turn.completed"

        mutated, complete = _mutation(before, after)
        if settled and complete and not mutated and _policy_refusal(outcome.stderr):
            # Codex reports a policy-refused write on stderr and then finishes the
            # turn normally: exit 0, `turn.completed`, no error event.  Reported
            # verbatim that is a turn which "succeeded" while changing nothing,
            # and a Mission slice would count it as progress.  Observed on
            # Windows 11 / 0.149.0 on 2026-08-22, where the same argv wrote the
            # file when Codex was a direct child but had every patch refused
            # under this runner's process-tree owner.  The same shape appears
            # whenever the effective sandbox is read-only or the project is not
            # trusted, so treat a refused-and-unchanged turn as a failed one.
            settled = False
            error = ("Codex refused every write under its own sandbox policy and "
                     "the workspace is unchanged: " + _policy_refusal(outcome.stderr))
        abnormal = not settled
        recovery = abnormal and (mutated or (process_started and not complete))
        return RunnerSnapshot(
            runner=self.key, workspace=workspace, thread_id=thread_id,
            cursor=cursor, events=all_events, usage=usage, settled=settled,
            exit_code=exit_code, error=_clean_error(error),
            recovery_required=recovery, mutated=mutated,
            mutation_check_complete=complete,
            workspace_digest=str(after.get("tree_digest") or ""),
            final_output=final_output, timed_out=timed_out, cancelled=cancelled,
            invocation=invocation, started_at=started_at, finished_at=finished_at)

    def _events(self, stdout: str, prior_cursor: int,
                prior_usage: Mapping[str, int] | None = None
                ) -> tuple[list[RunnerEvent], bool, int, list[str], str, str,
                           dict[str, int], str]:
        """Parse a bounded tail while aggregating facts over the whole stream.

        ``max_events`` is a persistence bound, not a parser-afterthought.  The
        old implementation materialized every event and sliced the list only at
        the end, so 16 MiB of tiny JSON records could allocate millions of
        Python objects.  This keeps only the tail while still counting every
        cursor and observing early thread/usage/final-output records.
        """
        events: deque[RunnerEvent] = deque(maxlen=self.max_events)
        invalid = False
        count = 0
        thread_ids: list[str] = []
        terminal = ""
        terminal_error = ""
        terminal_count = 0
        saw_terminal = False
        usage = dict(prior_usage or {})
        final_output = ""
        for raw in _lf_records(stdout):
            if not raw.strip():
                continue
            count += 1
            event, malformed = self._event_from_line(
                raw, prior_cursor + count)
            if saw_terminal:
                malformed = True
            events.append(event)
            invalid = invalid or malformed
            if event.type == "thread.started":
                candidate = str(event.payload.get("thread_id") or "")
                if candidate not in thread_ids:
                    if len(thread_ids) < 2:
                        thread_ids.append(candidate)
                    else:
                        # More than two distinct thread identities is already
                        # inconsistent; retaining every attacker-controlled id
                        # would only create a second unbounded collection.
                        invalid = True
            if event.type == "turn.completed":
                terminal_count += 1
                terminal = "completed"
                saw_terminal = True
            elif event.type == "turn.failed":
                terminal_count += 1
                terminal = "failed"
                terminal_error = _event_error(event.payload)
                saw_terminal = True
            if terminal_count > 1:
                invalid = True
            usage = _merge_usage(usage, (event,))
            final_output = _final_output((event,)) or final_output
        return (list(events), invalid, count, thread_ids, terminal,
                terminal_error, usage, final_output)

    def _event_from_line(self, raw: str, cursor: int) -> tuple[RunnerEvent, bool]:
        if len(raw) > self.max_event_chars:
            malformed = True
            value = {
                "type": "protocol.event_too_large",
                "reason": "event exceeds %d characters" % self.max_event_chars,
                "preview": raw[:min(self.max_event_chars, 4_096)],
            }
        else:
            try:
                value = json.loads(raw, parse_constant=_reject_json_constant)
                if not isinstance(value, dict):
                    raise ValueError("event is not an object")
                malformed = False
            except (ValueError, json.JSONDecodeError, RecursionError):
                malformed = True
                value = {"type": "protocol.invalid_json",
                         "preview": raw[: min(self.max_event_chars, 4_096)]}
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
        return (RunnerEvent(cursor=cursor,
                            type=("protocol.invalid_json" if unsafe_structure else
                                  runner_specs.redact_text(
                                      value.get("type") or "unknown", 256)),
                            payload=payload, at=time.time()), malformed)


def _lf_records(value: str):
    """Yield strict LF records without ``str.split``'s all-records list."""
    text = str(value or "")
    start = 0
    while True:
        end = text.find("\n", start)
        if end < 0:
            raw = text[start:]
            if raw:
                yield raw[:-1] if raw.endswith("\r") else raw
            return
        raw = text[start:end]
        yield raw[:-1] if raw.endswith("\r") else raw
        start = end + 1


def _reject_json_constant(value: str):
    """Reject Python's non-standard NaN/Infinity JSON extension on the wire."""
    raise ValueError("non-finite JSON number is forbidden: %s" % value)


def _workspace(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("workspace must be a non-empty path")
    root = os.path.realpath(os.path.abspath(value))
    if not os.path.isdir(root):
        raise ValueError("workspace does not exist or is not a directory: %s" % root)
    return root


def _prompt(value: Any) -> str:
    turn_input = runner_specs.RunInput.from_value(value)
    if turn_input.image_urls or turn_input.image_files:
        raise ValueError("this runner accepts text only; image input was not sent")
    return turn_input.text


def _snapshot(snapshotter: Callable[[str], dict[str, Any]], workspace: str) -> dict[str, Any]:
    try:
        value = snapshotter(workspace)
        return dict(value or {})
    except Exception:
        return {"tree_digest": "", "snapshot_complete": False}


def _mutation(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, bool]:
    left = str(before.get("tree_digest") or "")
    right = str(after.get("tree_digest") or "")
    complete = bool(left and right and before.get("snapshot_complete")
                    and after.get("snapshot_complete"))
    return bool(left and right and left != right), complete


def _merge_usage(prior: dict[str, int], events: Sequence[RunnerEvent]) -> dict[str, int]:
    usage = {str(key): max(0, int(value)) for key, value in prior.items()
             if _finite_number(value) and float(value) >= 0
             and float(value) <= _MAX_COUNTER}
    for event in events:
        if event.type != "turn.completed":
            continue
        for container in _USAGE_CONTAINERS:
            raw = event.payload.get(container)
            if not isinstance(raw, dict) or not raw:
                continue
            for key in _USAGE_KEYS:
                if key not in raw:
                    # Absent stays absent.  `runner_specs.usage_to_collie` reads
                    # a missing key as None and a present one as a count, so
                    # defaulting to 0 here would turn "Codex never told us how
                    # many cache writes there were" into "there were none".
                    continue
                value = raw.get(key)
                if (_finite_number(value) and float(value) >= 0
                        and float(value) <= _MAX_COUNTER):
                    usage[key] = usage.get(key, 0) + int(value)
            # At most one block per event.  A build that emits both spellings is
            # reporting the same tokens twice, and double-counted usage is worse
            # than a name we failed to recognise.
            break
    return usage


def _final_output(events: Sequence[RunnerEvent]) -> str:
    answer = ""
    for event in events:
        payload = event.payload
        if event.type in ("agent_message", "message", "assistant"):
            answer = str(payload.get("text") or payload.get("message") or answer)
        if event.type in ("item.completed", "item.updated"):
            item = payload.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                answer = str(item.get("text") or answer)
    return answer


def _event_error(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("detail") or "")
    return str(error or payload.get("message") or "")


def _bounded_payload(value: dict[str, Any], limit: int) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False)
        if len(encoded) <= limit:
            return json.loads(encoded, parse_constant=_reject_json_constant)
        return {"type": str(value.get("type") or "unknown"), "truncated": True,
                "preview": encoded[:limit]}
    except (TypeError, ValueError, RecursionError):
        return {"type": str(value.get("type") or "unknown"), "truncated": True,
                "reason": "unsafe-structure", "preview": repr(value)[:limit]}


def _clean_error(value: Any, limit: int = 4_000) -> str:
    return runner_specs.redact_text(str(value or "").strip(), limit)


def _display_path(path: str) -> str:
    """``C:\\Users\\alice\\.codex\\auth.json`` -> ``~/.codex/auth.json``.

    Probe evidence is printed by ``collie runners`` and persisted in receipts.
    The absolute form carries the account name of whoever ran it and says nothing
    the tilde form does not.
    """
    home = os.path.expanduser("~")
    absolute = os.path.abspath(path)
    if home and absolute.lower().startswith(os.path.join(home, "").lower()):
        absolute = "~" + os.sep + absolute[len(os.path.join(home, "")):]
    return absolute.replace("\\", "/")


def _cli_version(executable: str, timeout_s: float = 10.0) -> tuple[str, str]:
    """Return ``(version, error)`` from one ``<cli> --version``.

    ``executable`` is an already-resolved absolute path (Windows CreateProcess
    ignores PATHEXT, so a bare ``codex`` would raise FileNotFoundError against
    the npm ``.cmd`` shim).  A failure is reported, never raised: "installed but
    the CLI will not answer" is a probe result the registry has to be able to
    show, not an exception that hides the rest of the row.
    """
    try:
        completed = subprocess.run(
            [executable, "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
            **plat.no_window_kwargs())
    except Exception as exc:
        return "", _clean_error("could not run %s --version: %s: %s"
                                % (os.path.basename(executable), type(exc).__name__, exc))
    if completed.returncode != 0:
        return "", _clean_error(
            "%s --version exited with status %s: %s"
            % (os.path.basename(executable), completed.returncode,
               (completed.stderr or completed.stdout or "").strip()))
    first = (completed.stdout or completed.stderr or "").strip().splitlines()
    return (first[0].strip()[:200] if first else ""), ""


# `codex --version` prints "codex-cli 0.156.0", and only that shape -- or a line
# that is *nothing but* a version token, which `_cli_version` has always reported
# for other builds -- is read.  An arbitrary triple somewhere in an unrelated
# banner ("node v22.1.0") is not a Codex version, and guessing one there would
# silently answer "new schema" for an old CLI, which is the unsafe direction.
_VERSION_TRIPLE = r"v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?:[-+][0-9A-Za-z.+-]*)?"
_CLI_VERSION = re.compile(r"codex-cli\s+" + _VERSION_TRIPLE + r"(?![\w.])",
                          re.IGNORECASE)
_BARE_VERSION = re.compile(r"\A" + _VERSION_TRIPLE + r"\Z")

# Codex 0.156.0 dropped `windows.sandbox_private_desktop` from the config schema.
_PRIVATE_DESKTOP_REMOVED_AT = (0, 156, 0)

# One `--version` per binary per TTL, not per turn.  A *failed* or unreadable
# answer is retried after the shorter cooldown so a transient timeout or an
# unrecognised banner self-heals, while a CLI that will not answer at all still
# cannot add its own timeout to the latency of every start and resume.
_VERSION_RETRY_AFTER_S = 60.0
_VERSION_CACHE_TTL_S = 900.0
_MAX_CACHED_VERSIONS = 32
_VERSION_CACHE: dict[str, tuple[tuple[Any, ...], str, str, float]] = {}
_VERSION_CACHE_LOCK = threading.Lock()


def _parse_cli_version(text: str) -> tuple[int, int, int] | None:
    """Return ``(major, minor, patch)`` from a ``--version`` line, or None.

    Only the ``codex-cli <triple>`` banner and a line that is entirely one
    version token are read; anything else is None, and None keeps the measured
    override.  A pre-release suffix (``0.156.0-alpha.1``) is read as its release
    triple, because Codex ships a schema change in the pre-releases of the
    version that carries it; no other suffix scheme is guessed at.
    """
    text = str(text or "").strip()
    match = _CLI_VERSION.search(text) or _BARE_VERSION.match(text)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _needs_private_desktop_override(version: str) -> bool:
    """Whether this CLI still accepts ``windows.sandbox_private_desktop``.

    Unknown means yes.  See :func:`_windows_sandbox_override` for why the
    unparseable case fails closed onto the measured override rather than onto
    the newer schema.
    """
    parsed = _parse_cli_version(version)
    return parsed is None or parsed < _PRIVATE_DESKTOP_REMOVED_AT


def _executable_identity(executable: str) -> tuple[Any, ...] | None:
    """Identify the *file*, so an upgrade in place invalidates a cached version.

    ``npm -g install`` and the Codex installer both overwrite the same shim
    path, so the path alone is not an identity.  Size plus nanosecond mtime plus
    the creation time Windows reports as ``st_ctime_ns`` cover overwrite,
    delete-and-recreate, and shim replacement; a rewrite that preserved all
    three within one mtime tick would go unnoticed, which is why the cache is an
    optimisation on top of a fail-closed default and never a source of truth.
    """
    try:
        stat = os.stat(executable)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino, stat.st_dev,
            stat.st_ctime_ns)


def _resolved_cli_version(executable: str, *,
                          probe: Callable[[str], tuple[str, str]] | None = None,
                          now: float | None = None) -> tuple[str, str]:
    """Return ``(version, error)`` for the file at ``executable``, cached by identity.

    A path that cannot be stat'd is reported unknown *without* running anything:
    a bare name, an unresolved shim or a test double is not a file this launch
    will execute, and answering with some other ``codex`` on PATH would describe
    the wrong binary.  That also keeps the installed CLI out of unit tests.

    Caching is bounded two ways.  The identity of the file is re-taken on every
    call, so an in-place upgrade is picked up at once — but the file resolved
    here is often a stable npm shim that dispatches to a versioned binary
    elsewhere, and that target can change with no observable change to the shim.
    A good answer therefore expires after ``_VERSION_CACHE_TTL_S``; an answer
    that could not be read or parsed expires after the shorter cooldown, so an
    unrecognised banner or a timed-out probe cannot pin "unknown" for the life
    of the process.
    """
    probe = probe or _cli_version
    now = time.monotonic() if now is None else float(now)
    identity = _executable_identity(executable) if executable else None
    if identity is None:
        return ("", "could not stat %s to identify the Codex CLI"
                % (executable or "<empty>"))
    key = os.path.normcase(os.path.abspath(executable))
    with _VERSION_CACHE_LOCK:
        cached = _VERSION_CACHE.get(key)
        if cached is not None and cached[0] == identity and now < cached[3]:
            return (cached[1], cached[2])
    version, error = probe(executable)
    understood = not error and _parse_cli_version(version) is not None
    expires = now + (_VERSION_CACHE_TTL_S if understood else _VERSION_RETRY_AFTER_S)
    with _VERSION_CACHE_LOCK:
        if len(_VERSION_CACHE) >= _MAX_CACHED_VERSIONS and key not in _VERSION_CACHE:
            # A host has one or two Codex binaries; an unbounded table here would
            # only ever be a leak fed by whatever paths a caller passed.
            _VERSION_CACHE.clear()
        _VERSION_CACHE[key] = (identity, version, error, expires)
    return (version, error)


def _codex_login_state(auth_path: str, now: float) -> tuple[str, str, dict[str, Any]]:
    """Classify ``~/.codex/auth.json`` without reading a credential value.

    Returns ``(login, detail, evidence)`` where ``login`` is one of the
    ``RunnerProbe`` states and ``evidence`` holds only: which file was consulted,
    which *kind* of login it describes, and — for an OAuth login — the ``exp``
    claim of the access token as a unix timestamp.  ``exp`` is a public,
    unsigned, non-secret field of a JWT and is the one thing that distinguishes
    "signed in" from "the CLI will bounce this run to a login prompt".
    """
    from .ops import _jwt_exp  # decodes exp only; shared so there is one decoder

    shown = _display_path(auth_path)
    evidence: dict[str, Any] = {"source": "file:%s" % shown}
    if not os.path.isfile(auth_path):
        evidence["login_kind"] = "none"
        return ("not-logged-in", "no Codex login at %s; run `codex login`" % shown, evidence)
    try:
        with open(auth_path, "r", encoding="utf-8") as handle:
            value = json.load(handle, parse_constant=_reject_json_constant)
        if not isinstance(value, dict):
            raise ValueError("auth.json is not an object")
    except Exception as exc:
        evidence["login_kind"] = "unreadable"
        return ("unknown", _clean_error("could not read %s: %s: %s"
                                        % (shown, type(exc).__name__, exc)), evidence)

    tokens = value.get("tokens")
    tokens = tokens if isinstance(tokens, dict) else {}
    access = tokens.get("access_token")
    if isinstance(access, str) and access:
        evidence["login_kind"] = "chatgpt"
        expires_at = _jwt_exp(access)
        has_refresh = bool(tokens.get("refresh_token"))
        if expires_at:
            evidence["expires_at"] = expires_at
        if expires_at and expires_at <= now and not has_refresh:
            # With a refresh token the CLI renews on its own, so an expired
            # access token is not a reason to reject the runner; without one the
            # next turn stops at a login prompt with the prompt already sent.
            return ("expired",
                    "the Codex login in %s expired and has no refresh token; "
                    "run `codex login`" % shown, evidence)
        return ("ok", "", evidence)

    # API-key login: the key's *presence* is the fact; the value is never read.
    if isinstance(value.get("OPENAI_API_KEY"), str) and value.get("OPENAI_API_KEY"):
        evidence["login_kind"] = "api-key"
        return ("ok", "", evidence)

    evidence["login_kind"] = "none"
    return ("not-logged-in",
            "%s has neither an OAuth token nor an API key; run `codex login`" % shown,
            evidence)

__all__ = [
    "AgentRunner", "CodexExecRunner", "ProcessOutcome", "ProcessRunner",
    "RecoveryRequiredError", "RunnerEvent", "RunnerSnapshot", "SubprocessRunner",
]
