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

from dataclasses import dataclass, field
import json
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
_TOKEN_SECRET = re.compile(
    r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+|\b(?:sk|sess)-[A-Za-z0-9_-]{12,}\b"
)
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
    for line in (stderr or "").splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in _POLICY_REFUSALS):
            return _clean_error(line.strip(), limit=300)
    return ""


def _windows_sandbox_override() -> list[str]:
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
    """
    if not plat.is_windows():
        return []
    return ["-c", 'windows.sandbox="unelevated"',
            "-c", "windows.sandbox_private_desktop=false"]

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

try:
    request = json.loads(sys.stdin.read())
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
        return cls(cursor=max(0, int(value.get("cursor", 0))),
                   type=str(value.get("type") or "unknown"),
                   payload=dict(value.get("payload") or {}),
                   at=float(value.get("at") or 0.0))


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
    usage: dict[str, int] = field(default_factory=dict)
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
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RunnerSnapshot":
        usage = {}
        for key, amount in dict(value.get("usage") or {}).items():
            if isinstance(amount, (int, float)) and amount >= 0:
                usage[str(key)] = int(amount)
        exit_code = value.get("exit_code")
        return cls(
            runner=str(value.get("runner") or ""),
            workspace=os.path.realpath(os.path.abspath(str(value.get("workspace") or "."))),
            thread_id=str(value.get("thread_id") or ""),
            cursor=max(0, int(value.get("cursor") or 0)),
            events=tuple(RunnerEvent.from_dict(item)
                         for item in (value.get("events") or []) if isinstance(item, dict)),
            usage=usage,
            settled=bool(value.get("settled")),
            exit_code=int(exit_code) if isinstance(exit_code, (int, float)) else None,
            error=str(value.get("error") or ""),
            recovery_required=bool(value.get("recovery_required")),
            mutated=bool(value.get("mutated")),
            mutation_check_complete=bool(value.get("mutation_check_complete")),
            workspace_digest=str(value.get("workspace_digest") or ""),
            final_output=str(value.get("final_output") or ""),
            timed_out=bool(value.get("timed_out")),
            cancelled=bool(value.get("cancelled")),
            invocation=max(0, int(value.get("invocation") or 0)),
            started_at=float(value.get("started_at") or 0.0),
            finished_at=float(value.get("finished_at") or 0.0),
        )


@dataclass(frozen=True)
class ProcessOutcome:
    """Result returned by an injectable process transport."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    cancelled: bool = False


class ProcessRunner(Protocol):
    def run(self, argv: Sequence[str], *, cwd: str, stdin_text: str,
            timeout_s: float, on_process: Callable[[Any], bool | None],
            env: Mapping[str, str] | None = None) -> ProcessOutcome:
        """Run ``argv`` to completion under caller-owned cancellation.

        ``env`` is the *complete* environment for the child (as built by
        :func:`harness.runner_env.child_env`), not a set of additions.  ``None``
        means "inherit this process's environment", which is what every caller
        did before the selection layer existed and is kept so an embedder's own
        transport keeps working unchanged.
        """
        ...


class AgentRunner(Protocol):
    def start(self, prompt: str, workspace: str, *, timeout_s: float | None = None
              ) -> RunnerSnapshot:
        ...

    def resume(self, snapshot: RunnerSnapshot, prompt: str, *,
               timeout_s: float | None = None) -> RunnerSnapshot:
        ...

    def cancel_current(self) -> bool:
        ...


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
            return False
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

    def run(self, argv: Sequence[str], *, cwd: str, stdin_text: str,
            timeout_s: float, on_process: Callable[[Any], bool | None],
            env: Mapping[str, str] | None = None) -> ProcessOutcome:
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
                    ensure_ascii=True, separators=(",", ":"))
                try:
                    stdout, stderr = proc.communicate(input=request, timeout=timeout_s)
                    outcome = ProcessOutcome(
                        stdout=stdout or "", stderr=stderr or "",
                        exit_code=proc.returncode)
                except subprocess.TimeoutExpired as exc:
                    confirmed = _terminate_owned_process(proc)
                    stdout = _text(exc.stdout)
                    stderr = _text(exc.stderr)
                    try:
                        tail_out, tail_err = proc.communicate(timeout=5)
                        # A second communicate() returns the complete buffered
                        # stream on supported Python platforms, not necessarily
                        # just a suffix.  Do not duplicate JSONL events/usage.
                        stdout = tail_out or stdout
                        stderr = tail_err or stderr
                    except Exception:
                        pass
                    if not confirmed:
                        raise RuntimeError(
                            "Codex timed out and process-tree extinction could "
                            "not be confirmed")
                    outcome = ProcessOutcome(
                        stdout=stdout, stderr=stderr,
                        exit_code=proc.returncode, timed_out=True)
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
                 max_event_chars: int = 128_000, env_policy: str = "codex"):
        if default_timeout_s <= 0:
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
        # {"allowed": [names], "stripped": [names]} for the most recent turn —
        # names only, so the caller can copy it straight into a run receipt.
        self.last_env_receipt: dict[str, list[str]] = {"allowed": [], "stripped": []}
        self._run_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active_condition = threading.Condition(self._active_lock)
        self._active_process: Any = None
        self._starting = False
        self._cancel_requested = False

    def start(self, prompt: str, workspace: str, *, timeout_s: float | None = None
              ) -> RunnerSnapshot:
        root = _workspace(workspace)
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
        argv = [self._executable(), "exec", "--json", "--sandbox", "workspace-write",
                "--cd", root, "--ignore-user-config", "--ignore-rules",
                "--strict-config", "-c", 'approval_policy="never"',
                "-c", 'web_search="disabled"']
        argv += _windows_sandbox_override()
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
        # `resume` does not expose the top-level --sandbox flag.  An explicit
        # config override keeps the resumed turn at the same workspace-write
        # boundary even if the user's global default later changes; the same
        # argument applies to web_search, which `~/.codex/config.toml` can turn
        # on for every invocation.  `--ignore-user-config`/`--ignore-rules` were
        # verified against 0.149.0 on 2026-08-22 (resume returned the same
        # thread id and applied its patch), so the resumed turn gets the same
        # host-config isolation as the first one rather than silently falling
        # back to the user's MCP servers and rules half way through a thread.
        argv = [self._executable(), "exec", "resume", "--json",
                "--ignore-user-config", "--ignore-rules",
                "-c", 'sandbox_mode="workspace-write"',
                "-c", 'approval_policy="never"',
                "-c", 'web_search="disabled"']
        argv += _windows_sandbox_override()
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
        if timeout <= 0:
            raise ValueError("timeout_s must be positive")
        # Refuse *before* a process exists.  An OPENAI_BASE_URL or CODEX_API_KEY
        # in the parent shell would silently re-route or re-bill this turn, and
        # the operator unsetting it is cheaper than anyone reconstructing which
        # account paid.  Names only ever leave this call, never values.
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

        try:
            try:
                outcome = self.process_runner.run(
                    tuple(argv), cwd=workspace, stdin_text=prompt,
                    timeout_s=timeout, on_process=register, env=env)
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
        prior_cursor = prior.cursor if prior else 0
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
        parsed, invalid_json = self._events(outcome.stdout, prior_cursor)
        all_events = (prior_events + tuple(parsed))[-self.max_events:]
        cursor = prior_cursor + len(parsed)
        new_thread_ids = [str(event.payload.get("thread_id") or "")
                          for event in parsed if event.type == "thread.started"]
        protocol_error = invalid_json
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

        terminal = ""
        terminal_error = ""
        for event in parsed:
            if event.type == "turn.completed":
                terminal = "completed"
            elif event.type == "turn.failed":
                terminal = "failed"
                terminal_error = _event_error(event.payload)

        usage = _merge_usage(prior_usage, parsed)
        final_output = _final_output(parsed) or (prior.final_output if prior else "")
        exit_code = outcome.exit_code
        timed_out = bool(outcome.timed_out)
        settled = (exit_code == 0 and terminal == "completed" and not timed_out
                   and not cancelled and not protocol_error and bool(thread_id))
        error = ""
        if timed_out:
            error = "Codex turn exceeded its %.1fs wall timeout" % timeout
        elif cancelled:
            error = "Codex turn was cancelled"
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

    def _events(self, stdout: str, prior_cursor: int) -> tuple[list[RunnerEvent], bool]:
        events = []
        invalid = False
        for raw in (stdout or "").splitlines():
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("event is not an object")
            except (ValueError, json.JSONDecodeError):
                invalid = True
                value = {"type": "protocol.invalid_json",
                         "preview": raw[: min(self.max_event_chars, 4_096)]}
            payload = _bounded_payload(value, self.max_event_chars)
            event_type = str(value.get("type") or "unknown")
            events.append(RunnerEvent(cursor=prior_cursor + len(events) + 1,
                                      type=event_type, payload=payload, at=time.time()))
        return events, invalid


def _workspace(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("workspace must be a non-empty path")
    root = os.path.realpath(os.path.abspath(value))
    if not os.path.isdir(root):
        raise ValueError("workspace does not exist or is not a directory: %s" % root)
    return root


def _prompt(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("prompt must be non-empty")
    if "\x00" in value:
        raise ValueError("prompt contains a NUL byte")
    return value


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
             if isinstance(value, (int, float))}
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
                if isinstance(value, (int, float)) and value >= 0:
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
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= limit:
            return json.loads(encoded)
        return {"type": str(value.get("type") or "unknown"), "truncated": True,
                "preview": encoded[:limit]}
    except (TypeError, ValueError):
        return {"type": str(value.get("type") or "unknown"), "truncated": True,
                "preview": repr(value)[:limit]}


def _clean_error(value: Any, limit: int = 4_000) -> str:
    text = str(value or "").strip()
    text = _TOKEN_SECRET.sub(lambda match: (match.group(1) or "") + "[redacted]", text)
    return text[:limit]


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
            value = json.load(handle)
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


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


__all__ = [
    "AgentRunner", "CodexExecRunner", "ProcessOutcome", "ProcessRunner",
    "RecoveryRequiredError", "RunnerEvent", "RunnerSnapshot", "SubprocessRunner",
]
