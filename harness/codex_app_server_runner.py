"""Interactive Codex App Server runner with a fail-closed JSON-RPC client.

The app-server protocol is the rich-client surface behind Codex integrations.
Unlike ``codex exec`` it is bidirectional: a turn can be steered, interrupted,
and can ask the host to approve a command or file change.  This adapter only
uses local stdio.  Codex documents the app-server command itself as
experimental, so compatibility is earned through Collie's pinned version and
conformance report rather than called stable.  WebSocket, dynamic tools, and
client-supplied auth are intentionally outside its trust boundary.

The host Codex configuration is not a worker policy.  App Server currently has
no ``--ignore-user-config`` flag, so the launch pins empty MCP/plugin tables and
disables web search, hooks, memories, multi-agent tools, apps, and project
instructions.  ``--strict-config`` turns version drift in any of those controls
into a launch failure rather than silently inheriting a broader host policy.
"""
from __future__ import annotations

from collections import deque
import json
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from . import __version__, plat, runner_env, runner_specs
from .agent_runners import (
    RecoveryRequiredError,
    RunnerEvent,
    RunnerSnapshot,
    _clean_error,
    _mutation,
    _prompt,
    _snapshot,
    _terminate_owned_process,
    _windows_sandbox_override,
    _workspace,
)
from .verification import workspace_snapshot


_MAX_WIRE_CHARS = 2 * 1024 * 1024
_MAX_PAYLOAD_CHARS = 128_000
_MAX_EVENTS = 2_000
_THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "file-change",
}
_DECISIONS = frozenset({"accept", "acceptForSession", "decline", "cancel"})


def _safe_thread_id(value: Any) -> str:
    text = str(value or "")
    return text if _THREAD_ID.fullmatch(text) else ""


def _finite_timeout(value: Any, default: float) -> float:
    answer = default if value is None else float(value)
    if not math.isfinite(answer) or answer <= 0:
        raise ValueError("timeout_s must be positive")
    return answer


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number is forbidden: %s" % value)


class RpcTransport(Protocol):
    """Small injection seam used by protocol tests and the real subprocess."""

    def send(self, message: Mapping[str, Any]) -> None: ...
    def receive(self, timeout_s: float) -> dict[str, Any]: ...
    def terminate(self, timeout_s: float = 5.0) -> bool: ...
    def close(self) -> bool: ...
    @property
    def returncode(self) -> int | None: ...
    @property
    def stderr(self) -> str: ...


class AppServerStdioTransport:
    """Owned, bounded JSONL transport for one local app-server process."""

    def __init__(self, argv: Sequence[str], *, cwd: str,
                 env: Mapping[str, str] | None = None,
                 max_wire_chars: int = _MAX_WIRE_CHARS):
        self.max_wire_chars = max(65_536, int(max_wire_chars))
        self._messages: queue.Queue[Any] = queue.Queue(maxsize=256)
        self._stderr_parts: list[str] = []
        self._stderr_chars = 0
        self._write_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        group_kwargs = plat.new_group_kwargs()
        kwargs = {} if env is None else {"env": dict(env)}
        self._proc = subprocess.Popen(
            [str(part) for part in argv], cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            **kwargs, **group_kwargs, **plat.no_window_kwargs())
        self._proc._collie_tree_lock = threading.RLock()
        if not plat.is_windows() and group_kwargs.get("start_new_session"):
            self._proc._collie_process_group = int(self._proc.pid)
        self._owner = None
        try:
            self._owner = plat.attach_kill_on_close_job(self._proc)
            if self._owner is not None:
                self._proc._collie_kill_job = self._owner
        except Exception:
            plat.kill_tree(self._proc)
            try:
                self._proc.wait(timeout=5.0)
            except Exception:
                pass
            raise RuntimeError("could not establish App Server process-tree ownership")
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, name="collie-appserver-stdout", daemon=True)
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="collie-appserver-stderr", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    @property
    def returncode(self) -> int | None:
        return getattr(self._proc, "returncode", None)

    @property
    def stderr(self) -> str:
        return runner_specs.redact_text("".join(self._stderr_parts), 4_000)

    def _put(self, value: Any) -> None:
        while True:
            try:
                self._messages.put(value, timeout=.2)
                return
            except queue.Full:
                if self._closed:
                    return

    def _read_stdout(self) -> None:
        try:
            assert self._proc.stdout is not None
            while True:
                line = self._proc.stdout.readline(self.max_wire_chars + 1)
                if line == "":
                    break
                if len(line) > self.max_wire_chars or not line.endswith("\n"):
                    # Drain the rest of an oversized physical record before
                    # publishing the failure, so its tail cannot become a fake
                    # second JSON-RPC message.
                    while line and not line.endswith("\n"):
                        line = self._proc.stdout.readline(self.max_wire_chars + 1)
                    self._put(runner_specs.RunnerProtocolError(
                        "App Server emitted an oversized or unterminated JSONL record"))
                    continue
                try:
                    value = json.loads(line, parse_constant=_reject_constant)
                    if not isinstance(value, dict):
                        raise ValueError("message is not an object")
                    self._put(value)
                except Exception as exc:
                    self._put(runner_specs.RunnerProtocolError(
                        "invalid App Server JSONL: %s" % type(exc).__name__))
        finally:
            self._put(None)

    def _read_stderr(self) -> None:
        try:
            assert self._proc.stderr is not None
            while True:
                chunk = self._proc.stderr.read(4096)
                if chunk == "":
                    return
                remaining = 16_384 - self._stderr_chars
                if remaining > 0:
                    kept = chunk[:remaining]
                    self._stderr_parts.append(kept)
                    self._stderr_chars += len(kept)
        except Exception:
            return

    def send(self, message: Mapping[str, Any]) -> None:
        wire = json.dumps(dict(message), ensure_ascii=True,
                          separators=(",", ":"), allow_nan=False) + "\n"
        if len(wire) > self.max_wire_chars:
            raise runner_specs.RunnerProtocolError("App Server request is too large")
        with self._write_lock:
            if self._closed or self._proc.poll() is not None:
                raise EOFError("App Server is not running")
            assert self._proc.stdin is not None
            self._proc.stdin.write(wire)
            self._proc.stdin.flush()

    def receive(self, timeout_s: float) -> dict[str, Any]:
        try:
            value = self._messages.get(timeout=max(0.001, float(timeout_s)))
        except queue.Empty:
            raise TimeoutError("App Server response timed out")
        if value is None:
            detail = self.stderr
            raise EOFError("App Server closed its output%s" %
                           ((": " + detail) if detail else ""))
        if isinstance(value, BaseException):
            raise value
        return value

    def terminate(self, timeout_s: float = 5.0) -> bool:
        with self._close_lock:
            if self._closed:
                return bool(getattr(self._proc, "_collie_tree_extinct", False)
                            or self._proc.poll() is not None)
            confirmed = _terminate_owned_process(self._proc, timeout_s=timeout_s)
            self._closed = True
            if self._owner is not None:
                try:
                    self._owner.close()
                except Exception:
                    confirmed = False
            return bool(confirmed)

    def close(self) -> bool:
        # Closing stdin gives the stdio server a graceful EOF first.  Process
        # ownership is still proved below; a background descendant cannot keep
        # the runner alive after the turn is considered settled.
        with self._write_lock:
            if not self._closed and self._proc.stdin is not None:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
        try:
            self._proc.wait(timeout=.5)
        except Exception:
            pass
        answer = self.terminate()
        self._stdout_thread.join(timeout=2.0)
        self._stderr_thread.join(timeout=2.0)
        return bool(answer and not self._stdout_thread.is_alive()
                    and not self._stderr_thread.is_alive())


ApprovalCallback = Callable[[str, dict[str, Any]], str]
TransportFactory = Callable[[Sequence[str], str, Mapping[str, str]], RpcTransport]


def gate_approval_callback(gate: Any, approver: Callable[..., Any] | None = None
                           ) -> ApprovalCallback:
    """Adapt App Server approval requests to Collie's existing Gate contract.

    The protocol names two operations (a command and a file change), while the
    Gate reasons about Collie's canonical ``bash`` and ``write_file`` tools.  A
    Gate auto-allow is returned immediately; a decision that needs a person is
    handed to the surface's normal TTY/Inbox approver.  Missing details, a
    broken gate, a missing approver, or an invalid answer all decline.
    """
    if gate is None or not callable(getattr(gate, "evaluate", None)):
        raise TypeError("gate must expose evaluate()")

    def callback(kind: str, params: dict[str, Any]) -> str:
        raw = dict(params or {})
        if kind == "command":
            command = str(raw.get("command") or "").strip()
            if not command:
                return "decline"
            tool_name = "bash"
            args = {"command": command}
            if raw.get("cwd"):
                args["cwd"] = str(raw["cwd"])
        elif kind == "file-change":
            # The stable request does not enumerate every patched file.  grantRoot
            # names an out-of-workspace request when present; otherwise the App
            # Server sandbox guarantees the change is rooted in this workspace.
            path = str(raw.get("grantRoot") or getattr(gate, "cwd", "") or "")
            if not path:
                return "decline"
            tool_name = "write_file"
            args = {"path": path}
        else:
            return "decline"
        if raw.get("reason"):
            args["reason"] = str(raw["reason"])

        try:
            decision = gate.evaluate(tool_name, args)
        except Exception:
            return "decline"
        if decision.allowed:
            return "accept"
        if not decision.needs_user or approver is None:
            return "decline"

        request_key = str(raw.get("approvalId") or raw.get("itemId") or "")
        decision.call_id = ("codex-app-server:" + request_key) if request_key else ""
        try:
            from .gate import ALLOWING, Outcome
            answer = approver(tool_name, args, decision)
            outcome = answer if isinstance(answer, Outcome) else Outcome(str(answer))
        except Exception:
            return "decline"
        try:
            gate.apply_outcome(outcome, tool_name, decision.target, decision=decision)
        except Exception:
            return "decline"
        if outcome not in ALLOWING:
            return "decline"
        available = raw.get("availableDecisions")
        standing_offer = None
        try:
            standing_offer = gate.standing_rule_offer(tool_name, decision.target)
        except Exception:
            standing_offer = None
        if (outcome is Outcome.ALLOW_ALWAYS and standing_offer
                and isinstance(available, list)
                and "acceptForSession" in available):
            return "acceptForSession"
        return "accept"

    return callback


class CodexAppServerRunner:
    """One-turn-per-process interactive Codex App Server runner.

    Threads remain resumable because Codex persists their ids.  The process is
    deliberately not retained between turns: killing the complete owned tree at
    every terminal notification makes durable recovery identical to the other
    AgentRunner implementations and prevents a forgotten server from outliving
    a Mission lease.
    """

    key = "codex-app-server"
    credential_family = "codex"

    def __init__(self, *, executable: str = "codex", model: str = "",
                 default_timeout_s: float = 900.0, env_policy: str = "codex",
                 snapshotter: Callable[[str], dict[str, Any]] = workspace_snapshot,
                 approval_callback: ApprovalCallback | None = None,
                 transport_factory: TransportFactory | None = None,
                 max_events: int = _MAX_EVENTS,
                 event_callback: Callable[[RunnerEvent], Any] | None = None):
        self.default_timeout_s = _finite_timeout(default_timeout_s, 900.0)
        runner_env.allowlist(env_policy)
        self.executable = executable
        self.model = model
        self.env_policy = env_policy
        self.snapshotter = snapshotter
        self.approval_callback = approval_callback
        self.transport_factory = transport_factory
        self.max_events = max(1, int(max_events))
        self._event_callback = event_callback
        self.last_env_receipt: dict[str, list[str]] = {"allowed": [], "stripped": []}
        self._run_lock = threading.Lock()
        self._active_lock = threading.RLock()
        self._active: RpcTransport | None = None
        self._active_thread_id = ""
        self._active_turn_id = ""
        self._turn_done = threading.Event()
        self._cancel_requested = False
        self._write_request_id = 10_000

    def set_event_callback(self, callback: Callable[[RunnerEvent], Any] | None) -> None:
        self._event_callback = callback

    def set_approval_callback(self, callback: ApprovalCallback | None) -> None:
        self.approval_callback = callback

    def start(self, prompt: str, workspace: str, *, timeout_s: float | None = None
              ) -> RunnerSnapshot:
        return self._invoke(None, _prompt(prompt), _workspace(workspace), timeout_s)

    def resume(self, snapshot: RunnerSnapshot, prompt: str, *,
               timeout_s: float | None = None) -> RunnerSnapshot:
        if snapshot.runner != self.key:
            raise ValueError("snapshot belongs to %s, not %s" %
                             (snapshot.runner, self.key))
        if snapshot.recovery_required:
            raise RecoveryRequiredError(
                "the previous App Server turn may have left a partial workspace mutation; "
                "inspect or roll back the workspace before resuming")
        root = _workspace(snapshot.workspace)
        if root != snapshot.workspace:
            raise ValueError("snapshot workspace is not canonical")
        if not _safe_thread_id(snapshot.thread_id):
            raise ValueError("snapshot has no safe App Server thread id")
        return self._invoke(snapshot, _prompt(prompt), root, timeout_s)

    def steer_current(self, prompt: str) -> bool:
        text = _prompt(prompt)
        with self._active_lock:
            transport = self._active
            thread_id = self._active_thread_id
            if transport is None or not thread_id or self._turn_done.is_set():
                return False
            self._write_request_id += 1
            request_id = self._write_request_id
            try:
                transport.send({
                    "method": "turn/steer", "id": request_id,
                    "params": {"threadId": thread_id,
                               "input": [{"type": "text", "text": text}]},
                })
                return True
            except Exception:
                return False

    def cancel_current(self) -> bool:
        deadline = time.monotonic() + 5.0
        with self._active_lock:
            transport = self._active
            if transport is None:
                return False
            self._cancel_requested = True
            thread_id = self._active_thread_id
            turn_id = self._active_turn_id
            if thread_id and turn_id and not self._turn_done.is_set():
                self._write_request_id += 1
                try:
                    transport.send({
                        "method": "turn/interrupt", "id": self._write_request_id,
                        "params": {"threadId": thread_id, "turnId": turn_id},
                    })
                except Exception:
                    pass
        if self._turn_done.wait(timeout=.75):
            return True
        return bool(transport.terminate(
            timeout_s=max(0.0, deadline - time.monotonic())))

    def cancel_for(self, key: str = "") -> bool:
        return self.cancel_current()

    def probe(self, *, now: float | None = None, codex_home: str | None = None
              ) -> runner_specs.RunnerProbe:
        # Login/version parsing is shared with codex exec; only the runner key
        # and protocol capabilities differ in the registry projection.
        from .agent_runners import CodexExecRunner
        base = CodexExecRunner(executable=self.executable).probe(
            now=now, codex_home=codex_home)
        return runner_specs.RunnerProbe.from_dict({**base.to_dict(), "key": self.key})

    def _resolved_executable(self) -> str:
        if self.transport_factory is not None:
            return self.executable
        resolved = shutil.which(self.executable)
        if not resolved:
            raise FileNotFoundError("Codex CLI is not installed or not on PATH")
        return resolved

    def _argv(self) -> list[str]:
        argv = [
            self._resolved_executable(), "app-server", "--stdio", "--strict-config",
            "-c", "mcp_servers={}",
            "-c", "plugins={}",
            "-c", 'web_search="disabled"',
            "-c", "project_doc_max_bytes=0",
            "-c", "features.hooks=false",
            "-c", "features.memories=false",
            "-c", "features.multi_agent=false",
            "-c", "features.apps=false",
        ]
        argv += _windows_sandbox_override()
        return argv

    def _new_transport(self, argv: Sequence[str], root: str,
                       env: Mapping[str, str]) -> RpcTransport:
        if self.transport_factory is not None:
            return self.transport_factory(tuple(argv), root, env)
        return AppServerStdioTransport(tuple(argv), cwd=root, env=env)

    def _invoke(self, prior: RunnerSnapshot | None, prompt: str, root: str,
                timeout_s: float | None) -> RunnerSnapshot:
        timeout = _finite_timeout(timeout_s, self.default_timeout_s)
        runner_env.assert_no_billing_override(os.environ, self.credential_family)
        env, receipt = runner_env.child_env(self.env_policy)
        self.last_env_receipt = receipt
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("this App Server runner already has an active turn")

        started_at = time.time()
        before = _snapshot(self.snapshotter, root)
        prior_events = tuple(prior.events) if prior else ()
        prior_cursor = prior.cursor if prior else 0
        state: dict[str, Any] = {
            "events": deque(prior_events, maxlen=self.max_events),
            "cursor": prior_cursor,
            "responses": {},
            "terminal": "",
            "terminal_error": "",
            "final_output": prior.final_output if prior else "",
            "thread_id": prior.thread_id if prior else "",
            "turn_id": "",
        }
        transport: RpcTransport | None = None
        raised: Exception | None = None
        process_started = False
        close_confirmed = False
        with self._active_lock:
            self._cancel_requested = False
            self._turn_done.clear()
            self._active = None
            self._active_thread_id = state["thread_id"]
            self._active_turn_id = ""
        try:
            transport = self._new_transport(self._argv(), root, env)
            process_started = True
            with self._active_lock:
                self._active = transport
            deadline = time.monotonic() + timeout
            self._request(transport, state, "initialize", {
                "clientInfo": {"name": "collie", "title": "Collie",
                               "version": __version__},
            }, deadline)
            transport.send({"method": "initialized", "params": {}})
            if prior is None:
                params: dict[str, Any] = {
                    "cwd": root, "approvalPolicy": "on-request",
                    "sandbox": "workspace-write", "ephemeral": False,
                }
                if self.model:
                    params["model"] = self.model
                thread_result = self._request(
                    transport, state, "thread/start", params, deadline)
            else:
                params = {
                    "threadId": prior.thread_id, "cwd": root,
                    "approvalPolicy": "on-request", "sandbox": "workspace-write",
                }
                if self.model:
                    params["model"] = self.model
                thread_result = self._request(
                    transport, state, "thread/resume", params, deadline)
            returned_thread = ((thread_result.get("thread") or {}).get("id")
                               if isinstance(thread_result, dict) else "")
            thread_id = _safe_thread_id(returned_thread or state["thread_id"])
            if not thread_id:
                raise runner_specs.RunnerProtocolError(
                    "App Server did not return a safe thread id")
            if prior is not None and thread_id != prior.thread_id:
                raise runner_specs.RunnerProtocolError(
                    "App Server resumed a different thread id")
            state["thread_id"] = thread_id
            with self._active_lock:
                self._active_thread_id = thread_id
            turn_result = self._request(transport, state, "turn/start", {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            }, deadline)
            returned_turn = ((turn_result.get("turn") or {}).get("id")
                             if isinstance(turn_result, dict) else "")
            if returned_turn:
                state["turn_id"] = str(returned_turn)[:256]
                with self._active_lock:
                    self._active_turn_id = state["turn_id"]
            while not state["terminal"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("App Server turn exceeded its wall timeout")
                try:
                    message = transport.receive(min(remaining, 1.0))
                except TimeoutError:
                    continue
                self._handle_message(transport, state, message)
        except Exception as exc:
            raised = exc
        finally:
            self._turn_done.set()
            if transport is not None:
                try:
                    close_confirmed = bool(transport.close())
                except Exception:
                    close_confirmed = False
            with self._active_lock:
                cancelled = self._cancel_requested
                self._active = None
                self._active_thread_id = ""
                self._active_turn_id = ""
            self._run_lock.release()

        finished_at = time.time()
        after = _snapshot(self.snapshotter, root)
        mutated, mutation_complete = _mutation(before, after)
        terminal = str(state["terminal"] or "")
        cancelled = bool(cancelled or terminal == "interrupted")
        error = ""
        timed_out = isinstance(raised, TimeoutError)
        if raised is not None:
            error = _clean_error("%s: %s" % (type(raised).__name__, raised))
        elif terminal == "failed":
            error = _clean_error(state["terminal_error"] or "App Server turn failed")
        elif terminal != "completed":
            error = "App Server turn ended without a completed terminal event"
        elif not close_confirmed:
            error = "App Server process-tree extinction could not be confirmed"
        settled = bool(not error and terminal == "completed" and not cancelled)
        abnormal = not settled
        recovery = bool(abnormal and (mutated or (process_started and not mutation_complete)))
        return RunnerSnapshot(
            runner=self.key, workspace=root,
            thread_id=_safe_thread_id(state["thread_id"]),
            cursor=int(state["cursor"]), events=tuple(state["events"]),
            usage=dict(prior.usage) if prior else {}, settled=settled,
            exit_code=0 if terminal in ("completed", "interrupted") else (
                transport.returncode if transport is not None else None),
            error=error, recovery_required=recovery, mutated=mutated,
            mutation_check_complete=mutation_complete,
            workspace_digest=str(after.get("tree_digest") or ""),
            final_output=str(state["final_output"] or ""), timed_out=timed_out,
            cancelled=cancelled, invocation=(prior.invocation if prior else 0) + 1,
            started_at=started_at, finished_at=finished_at)

    def _send_request(self, transport: RpcTransport, method: str,
                      params: Mapping[str, Any]) -> int:
        self._write_request_id += 1
        request_id = self._write_request_id
        transport.send({"method": method, "id": request_id,
                        "params": dict(params)})
        return request_id

    def _request(self, transport: RpcTransport, state: dict[str, Any],
                 method: str, params: Mapping[str, Any], deadline: float
                 ) -> dict[str, Any]:
        request_id = self._send_request(transport, method, params)
        while True:
            cached = state["responses"].pop(request_id, None)
            if cached is not None:
                message = cached
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("App Server %s response timed out" % method)
                message = transport.receive(remaining)
                if "method" in message:
                    self._handle_message(transport, state, message)
                    continue
                if message.get("id") != request_id:
                    if len(state["responses"]) >= 64:
                        raise runner_specs.RunnerProtocolError(
                            "too many unmatched App Server responses")
                    state["responses"][message.get("id")] = message
                    continue
            if "error" in message:
                detail = message.get("error") or {}
                text = detail.get("message") if isinstance(detail, dict) else detail
                raise runner_specs.RunnerProtocolError(
                    "App Server %s failed: %s" %
                    (method, runner_specs.redact_text(text, 500)))
            result = message.get("result")
            if not isinstance(result, dict):
                raise runner_specs.RunnerProtocolError(
                    "App Server %s returned a non-object result" % method)
            return result

    def _emit(self, state: dict[str, Any], event_type: str,
              payload: Mapping[str, Any]) -> None:
        state["cursor"] += 1
        safe = runner_specs.redact_value(dict(payload), _MAX_PAYLOAD_CHARS)
        if not isinstance(safe, dict):
            safe = {"truncated": True}
        event = RunnerEvent(cursor=state["cursor"], type=str(event_type)[:256],
                            payload=safe, at=time.time())
        state["events"].append(event)
        callback = self._event_callback
        if callback is not None:
            try:
                callback(event)
            except Exception:
                pass

    def _approval_decision(self, kind: str, params: dict[str, Any]) -> str:
        callback = self.approval_callback
        if callback is None:
            return "decline"
        try:
            answer = callback(kind, runner_specs.redact_value(
                params, _MAX_PAYLOAD_CHARS))
        except Exception:
            return "decline"
        answer = str(answer or "")
        if answer not in _DECISIONS:
            return "decline"
        available = params.get("availableDecisions")
        if isinstance(available, list) and available and answer not in available:
            return "decline"
        return answer

    def _handle_message(self, transport: RpcTransport, state: dict[str, Any],
                        message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        if not method:
            if "id" in message:
                if len(state["responses"]) >= 64:
                    raise runner_specs.RunnerProtocolError(
                        "too many unmatched App Server responses")
                state["responses"][message.get("id")] = message
                return
            raise runner_specs.RunnerProtocolError("App Server message has no method")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if "id" in message:  # server-initiated request
            request_id = message.get("id")
            if method in _APPROVAL_METHODS:
                kind = _APPROVAL_METHODS[method]
                requested = dict(params)
                requested.update({"kind": kind, "requestId": request_id})
                self._emit(state, "approval.requested", requested)
                decision = self._approval_decision(kind, params)
                transport.send({"id": request_id,
                                "result": {"decision": decision}})
                self._emit(state, "approval.resolved", {
                    "kind": kind, "requestId": request_id,
                    "decision": decision})
                return
            if method == "item/permissions/requestApproval":
                # Empty grants are the schema-valid least-privilege answer.
                transport.send({"id": request_id, "result": {
                    "permissions": {}, "scope": "turn"}})
                self._emit(state, "approval.resolved", {
                    "kind": "permissions", "requestId": request_id,
                    "decision": "decline"})
                return
            transport.send({"id": request_id, "error": {
                "code": -32601,
                "message": "Collie does not expose this server request",
            }})
            self._emit(state, "runner.error", {
                "method": method, "error": "unsupported server request"})
            return

        self._emit(state, method, params)
        if method == "thread/started":
            candidate = _safe_thread_id((params.get("thread") or {}).get("id"))
            if candidate:
                state["thread_id"] = candidate
        elif method == "turn/started":
            turn = params.get("turn") or {}
            state["turn_id"] = str(turn.get("id") or "")[:256]
            with self._active_lock:
                self._active_turn_id = state["turn_id"]
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                state["final_output"] = runner_specs.redact_text(
                    item["text"], 1_000_000)
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            status = str(turn.get("status") or "")
            state["terminal"] = status
            if status == "failed":
                failure = turn.get("error") or {}
                state["terminal_error"] = runner_specs.redact_text(
                    failure.get("message") if isinstance(failure, dict) else failure,
                    4_000)
            self._turn_done.set()


__all__ = [
    "AppServerStdioTransport", "CodexAppServerRunner", "RpcTransport",
    "gate_approval_callback",
]
