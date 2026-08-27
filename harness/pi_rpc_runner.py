"""Constrained Pi JSON-RPC runner.

Pi's RPC protocol has native prompt, steer, follow-up, abort, compaction and
session commands.  It does not have a tool approval round-trip, so Collie never
enables Pi's ``bash`` tool: the explicit allowlist is read/edit/write/grep/find/ls
and all project extensions, skills, templates and context files are disabled.
"""
from __future__ import annotations

from collections import deque
import json
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Mapping, Sequence

from . import plat, runner_env, runner_specs
from .agent_runners import (
    RecoveryRequiredError,
    RunnerEvent,
    RunnerSnapshot,
    _clean_error,
    _display_path,
    _mutation,
    _snapshot,
    _workspace,
)
from .codex_app_server_runner import AppServerStdioTransport, RpcTransport
from .verification import workspace_snapshot


_SESSION_ID = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_TOOLS = "read,edit,write,grep,find,ls"
_MAX_EVENTS = 2_000


def _timeout(value: Any, default: float) -> float:
    result = default if value is None else float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("timeout_s must be positive")
    return result


def _text(value: str | runner_specs.RunInput) -> str:
    item = runner_specs.RunInput.from_value(value)
    if item.image_urls or item.image_files:
        raise ValueError("Pi RPC adapter currently accepts text only; image input was not sent")
    return item.text


TransportFactory = Callable[[Sequence[str], str, Mapping[str, str]], RpcTransport]
InteractionCallback = Callable[[runner_specs.PendingInteraction], Any]


class PiRpcRunner:
    key = "pi-rpc"
    credential_family = "collie-sidecar"

    def __init__(self, *, executable: str = "pi", model: str = "",
                 default_timeout_s: float = 900.0,
                 env_policy: str = "sidecar-harness",
                 snapshotter: Callable[[str], dict[str, Any]] = workspace_snapshot,
                 transport_factory: TransportFactory | None = None,
                 max_events: int = _MAX_EVENTS,
                 event_callback: Callable[[RunnerEvent], Any] | None = None,
                 interaction_callback: InteractionCallback | None = None):
        self.default_timeout_s = _timeout(default_timeout_s, 900.0)
        runner_env.allowlist(env_policy)
        self.executable = str(executable or "pi")
        self.model = str(model or "")
        self.env_policy = env_policy
        self.snapshotter = snapshotter
        self.transport_factory = transport_factory
        self.max_events = max(1, int(max_events))
        self._event_callback = event_callback
        self._interaction_callback = interaction_callback
        self.last_env_receipt: dict[str, list[str]] = {"allowed": [], "stripped": []}
        self._run_lock = threading.Lock()
        self._active_lock = threading.RLock()
        self._active: RpcTransport | None = None
        self._active_turn = False
        self._cancel_requested = False
        self._turn_done = threading.Event()

    def set_event_callback(self, callback: Callable[[RunnerEvent], Any] | None) -> None:
        self._event_callback = callback

    def set_interaction_callback(self, callback: InteractionCallback | None) -> None:
        self._interaction_callback = callback

    def handshake(self) -> runner_specs.CapabilityHandshake:
        from .runner_registry import PI_RPC_CAPABILITIES
        probe = self.probe()
        return runner_specs.CapabilityHandshake(
            runner=self.key, protocol=PI_RPC_CAPABILITIES.protocol,
            protocol_version=probe.version,
            capabilities=PI_RPC_CAPABILITIES, source="pi-rpc-get_state",
            negotiated_at=time.time())

    def start(self, prompt: str | runner_specs.RunInput, workspace: str, *,
              timeout_s: float | None = None) -> RunnerSnapshot:
        session_id = uuid.uuid4().hex
        return self._operate(None, "run", _text(prompt), _workspace(workspace),
                             session_id, timeout_s)

    def resume(self, snapshot: RunnerSnapshot,
               prompt: str | runner_specs.RunInput, *,
               timeout_s: float | None = None) -> RunnerSnapshot:
        self._validate(snapshot)
        return self._operate(snapshot, "run", _text(prompt), snapshot.workspace,
                             snapshot.thread_id, timeout_s)

    def compact(self, snapshot: RunnerSnapshot, *,
                timeout_s: float | None = None) -> RunnerSnapshot:
        self._validate(snapshot)
        return self._operate(snapshot, "compact", "", snapshot.workspace,
                             snapshot.thread_id, timeout_s)

    def fork(self, snapshot: RunnerSnapshot, *,
             timeout_s: float | None = None) -> RunnerSnapshot:
        self._validate(snapshot)
        return self._operate(snapshot, "fork", "", snapshot.workspace,
                             snapshot.thread_id, timeout_s)

    def steer_current(self, message: str | runner_specs.RunInput) -> bool:
        return self._send_turn_message("steer", _text(message))

    def follow_up_current(self, message: str | runner_specs.RunInput) -> bool:
        return self._send_turn_message("follow_up", _text(message))

    def _send_turn_message(self, kind: str, text: str) -> bool:
        with self._active_lock:
            transport = self._active
            if transport is None or not self._active_turn:
                return False
            try:
                transport.send({"id": "collie-%s-%s" % (kind, uuid.uuid4().hex),
                                "type": kind, "message": text})
                return True
            except Exception:
                return False

    def cancel_current(self) -> bool:
        with self._active_lock:
            transport = self._active
            if transport is None:
                return False
            self._cancel_requested = True
            try:
                transport.send({"id": "collie-abort", "type": "abort"})
            except Exception:
                pass
        # A successful write only proves that abort was requested.  Wait briefly
        # for ``agent_settled``; a silent or wedged peer is escalated to the
        # owned process tree, and only extinction counts as confirmed cancel.
        if self._turn_done.wait(timeout=.75):
            return True
        try:
            return bool(transport.terminate(max(0.0, 4.25)))
        except Exception:
            return False

    def cancel_for(self, key: str = "") -> bool:
        return self.cancel_current()

    def probe(self, *, now: float | None = None) -> runner_specs.RunnerProbe:
        now = time.time() if now is None else float(now)
        resolved = shutil.which(self.executable) or ""
        if not resolved:
            return runner_specs.RunnerProbe(
                key=self.key, installed=False, probed_at=now,
                detail="Pi is not installed or not on PATH")
        version = ""
        detail = ""
        login = "unknown"
        evidence: dict[str, Any] = {"source": "pi auth check --no-refresh",
                                    "providers_checked": []}
        env, _receipt = runner_env.child_env(self.env_policy)
        try:
            completed = subprocess.run(
                [resolved, "--version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10.0, env=env,
                **plat.no_window_kwargs())
            if completed.returncode == 0:
                version = (completed.stdout or completed.stderr).strip().splitlines()[0][:200]
            else:
                detail = "Pi --version exited with status %s" % completed.returncode
        except Exception as exc:
            detail = "could not run Pi --version: %s" % type(exc).__name__
        reasons: list[str] = []
        for provider in ("openai-codex", "anthropic", "google"):
            try:
                auth = subprocess.run(
                    [resolved, "auth", "check", "--provider", provider, "--json",
                     "--no-refresh"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=10.0, env=env,
                    **plat.no_window_kwargs())
                value = json.loads(auth.stdout or "{}",
                                   parse_constant=lambda raw: (_ for _ in ()).throw(
                                       ValueError("non-finite JSON number: " + raw)))
                status = str(value.get("status") or "") if isinstance(value, dict) else ""
                evidence["providers_checked"].append(provider)
                if auth.returncode == 0 and status == "ready":
                    login = "ok"
                    evidence["ready_provider"] = provider
                    break
                if isinstance(value, dict) and value.get("reason"):
                    reasons.append(str(value["reason"]).replace("_", " "))
            except Exception as exc:
                reasons.append("%s check failed" % provider)
        if login != "ok":
            login = "not-logged-in" if evidence["providers_checked"] else "unknown"
            reason = reasons[0] if reasons else "could not inspect Pi auth"
            detail = (detail + "; " + reason).strip("; ")
        return runner_specs.RunnerProbe(
            key=self.key, installed=True, executable_path=_display_path(resolved),
            version=version, login=login, billing_class="unknown",
            billing_mode="unconfigured", billing_evidence=evidence,
            probed_at=now, detail=detail)

    def _validate(self, snapshot: RunnerSnapshot) -> None:
        if snapshot.runner != self.key:
            raise ValueError("snapshot belongs to %s, not %s" %
                             (snapshot.runner, self.key))
        if snapshot.recovery_required:
            raise RecoveryRequiredError(
                "the previous Pi turn may have partially mutated the workspace; "
                "inspect or roll back before resuming")
        if _workspace(snapshot.workspace) != snapshot.workspace:
            raise ValueError("snapshot workspace is not canonical")
        if not _SESSION_ID.fullmatch(snapshot.thread_id or ""):
            raise ValueError("snapshot has no safe Pi session id")

    def _resolved(self) -> str:
        if self.transport_factory is not None:
            return self.executable
        resolved = shutil.which(self.executable)
        if not resolved:
            raise FileNotFoundError("Pi is not installed or not on PATH")
        return resolved

    def _argv(self, session_id: str, action: str) -> list[str]:
        argv = [self._resolved(), "--mode", "rpc", "--tools", _TOOLS,
                "--no-extensions", "--no-skills", "--no-prompt-templates",
                "--no-context-files", "--no-approve"]
        if self.model:
            argv += ["--model", self.model]
        if action == "fork":
            argv += ["--fork", session_id]
        else:
            argv += ["--session-id", session_id]
        return argv

    def _transport(self, argv: Sequence[str], workspace: str,
                   env: Mapping[str, str]) -> RpcTransport:
        if self.transport_factory is not None:
            return self.transport_factory(tuple(argv), workspace, env)
        return AppServerStdioTransport(tuple(argv), cwd=workspace, env=env)

    def _operate(self, prior: RunnerSnapshot | None, action: str, prompt: str,
                 workspace: str, session_id: str,
                 timeout_s: float | None) -> RunnerSnapshot:
        timeout = _timeout(timeout_s, self.default_timeout_s)
        runner_env.assert_no_billing_override(os.environ, self.credential_family)
        env, self.last_env_receipt = runner_env.child_env(self.env_policy)
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("this Pi runner already has an active operation")
        before = _snapshot(self.snapshotter, workspace)
        started_at = time.time()
        previous = tuple(prior.events) if prior else ()
        cursor = prior.cursor if prior else 0
        events: deque[RunnerEvent] = deque(maxlen=self.max_events)
        pending: list[runner_specs.PendingInteraction] = []
        usage = dict(prior.usage) if prior else {}
        final_output = prior.final_output if prior else ""
        actual_session = ""
        error = ""
        settled = False
        transport: RpcTransport | None = None
        close_ok = False
        deadline = time.monotonic() + timeout

        def publish(value: dict[str, Any]) -> None:
            nonlocal cursor
            cursor += 1
            clean = runner_specs.redact_value(value)
            if not isinstance(clean, dict):
                clean = {"type": "protocol.invalid_json"}
            event = RunnerEvent(cursor=cursor,
                                type=str(value.get("type") or "unknown")[:256],
                                payload=clean, at=time.time())
            events.append(event)
            if self._event_callback is not None:
                try:
                    self._event_callback(event)
                except Exception:
                    pass

        def receive() -> dict[str, Any]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Pi RPC operation timed out")
            value = transport.receive(remaining) if transport is not None else {}
            if not isinstance(value, dict):
                raise runner_specs.RunnerProtocolError("Pi RPC message is not an object")
            publish(value)
            return value

        def response(command: str, request_id: str) -> dict[str, Any]:
            while True:
                value = receive()
                if value.get("type") == "extension_ui_request":
                    self._handle_interaction(transport, value, pending)
                    continue
                if (value.get("type") == "response" and value.get("id") == request_id
                        and value.get("command") == command):
                    if value.get("success") is not True:
                        raise runner_specs.RunnerProtocolError(
                            "Pi %s failed: %s" % (command, value.get("error") or "unknown"))
                    return value

        try:
            transport = self._transport(self._argv(session_id, action), workspace, env)
            with self._active_lock:
                self._active = transport
                self._active_turn = False
                self._cancel_requested = False
                self._turn_done.clear()
            state_id = "collie-state"
            transport.send({"id": state_id, "type": "get_state"})
            state_response = response("get_state", state_id)
            state = state_response.get("data") or {}
            actual_session = str(state.get("sessionId") or "")
            if not _SESSION_ID.fullmatch(actual_session):
                raise runner_specs.RunnerProtocolError("Pi returned an unsafe session id")

            if action == "run":
                prompt_id = "collie-prompt"
                with self._active_lock:
                    self._active_turn = True
                transport.send({"id": prompt_id, "type": "prompt", "message": prompt})
                prompt_ack = False
                agent_settled = False
                while not agent_settled:
                    value = receive()
                    if value.get("type") == "extension_ui_request":
                        self._handle_interaction(transport, value, pending)
                    elif (value.get("type") == "response" and
                          value.get("id") == prompt_id):
                        prompt_ack = value.get("success") is True
                        if not prompt_ack:
                            raise runner_specs.RunnerProtocolError(
                                "Pi prompt failed: %s" % (value.get("error") or "unknown"))
                    elif value.get("type") == "agent_settled":
                        agent_settled = True
                if not prompt_ack:
                    raise runner_specs.RunnerProtocolError(
                        "Pi settled without acknowledging the prompt")
                stats_id = "collie-stats"
                final_id = "collie-final"
                transport.send({"id": stats_id, "type": "get_session_stats"})
                stats = response("get_session_stats", stats_id).get("data") or {}
                transport.send({"id": final_id, "type": "get_last_assistant_text"})
                final = response("get_last_assistant_text", final_id).get("data") or {}
                final_output = str(final.get("text") or final_output)
                usage = self._usage(stats)
                settled = True
            elif action == "compact":
                compact_id = "collie-compact"
                transport.send({"id": compact_id, "type": "compact"})
                response("compact", compact_id)
                publish({"type": "context.compacted", "sessionId": actual_session})
                settled = True
            else:  # fork was performed by the launch argument
                publish({"type": "session.forked", "sessionId": actual_session,
                         "parentSessionId": session_id})
                settled = actual_session != session_id
                if not settled:
                    raise runner_specs.RunnerProtocolError(
                        "Pi fork returned the parent session id")
        except Exception as exc:
            error = _clean_error("%s: %s" % (type(exc).__name__, exc))
        finally:
            with self._active_lock:
                cancelled = self._cancel_requested
                self._active_turn = False
                self._active = None
                self._turn_done.set()
            if transport is not None:
                try:
                    close_ok = bool(transport.close())
                except Exception:
                    close_ok = False
            self._run_lock.release()

        finished_at = time.time()
        after = _snapshot(self.snapshotter, workspace)
        mutated, complete = _mutation(before, after)
        if not close_ok and transport is not None:
            settled = False
            error = error or "Pi process-tree extinction could not be confirmed"
        if cancelled:
            settled = False
            error = "Pi operation was cancelled"
        if pending:
            # A callback may have answered these during the turn.  Anything left
            # means the adapter declined it and the turn is not a clean success.
            settled = False
            error = error or "Pi requested host interaction that was not resolved"
        recovery = not settled and (mutated or not complete)
        return RunnerSnapshot(
            runner=self.key, workspace=workspace,
            thread_id=actual_session or (prior.thread_id if prior else session_id),
            cursor=cursor, events=(previous + tuple(events))[-self.max_events:],
            usage=usage, settled=settled, exit_code=getattr(transport, "returncode", None),
            error=error, recovery_required=recovery, mutated=mutated,
            mutation_check_complete=complete,
            workspace_digest=str(after.get("tree_digest") or ""),
            final_output=final_output, timed_out="timed out" in error.lower(),
            cancelled=cancelled,
            invocation=(prior.invocation if prior else 0) + (1 if action == "run" else 0),
            started_at=started_at, finished_at=finished_at,
            pending_interactions=tuple(pending))

    def _handle_interaction(self, transport: RpcTransport | None,
                            value: dict[str, Any],
                            pending: list[runner_specs.PendingInteraction]) -> None:
        interaction = runner_specs.PendingInteraction(
            interaction_id=str(value.get("id") or ""), runner=self.key,
            kind=str(value.get("method") or "user_input"),
            prompt=str(value.get("title") or value.get("message") or ""),
            options=tuple(str(item) for item in (value.get("options") or ())),
            raw=value, created_at=time.time())
        answer: Any = None
        if self._interaction_callback is not None:
            try:
                answer = self._interaction_callback(interaction)
            except Exception:
                answer = None
        if transport is None:
            pending.append(interaction)
            return
        response: dict[str, Any] = {"type": "extension_ui_response",
                                    "id": interaction.interaction_id}
        if interaction.kind == "confirm" and isinstance(answer, bool):
            response["confirmed"] = answer
        elif isinstance(answer, str):
            response["value"] = answer[:4_000]
        else:
            response["cancelled"] = True
            pending.append(interaction)
        transport.send(response)

    @staticmethod
    def _usage(stats: Any) -> dict[str, int | float]:
        if not isinstance(stats, dict) or not isinstance(stats.get("tokens"), dict):
            return {}
        tokens = stats["tokens"]
        result: dict[str, int | float] = {}
        mapping = {"input": "input_tokens", "output": "output_tokens",
                   "cacheRead": "cached_input_tokens",
                   "cacheWrite": "cache_write_input_tokens"}
        for source, target in mapping.items():
            value = tokens.get(source)
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and math.isfinite(float(value)) and value >= 0:
                result[target] = int(value)
        cost = stats.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) \
                and math.isfinite(float(cost)) and cost >= 0:
            result["cost_usd"] = float(cost)
        return result


__all__ = ["PiRpcRunner"]
