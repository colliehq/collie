"""Container-only Hermes TUI Gateway JSON-RPC adapter.

Hermes exposes a rich stdio protocol, but the gateway also loads the selected
Hermes profile's plugins, skills, MCP servers, schedulers and shell tools.  A
bare ``python -m tui_gateway.entry`` therefore is not an acceptable Collie
worker boundary.  This module implements and tests the wire protocol while
requiring an explicit container launch command for real processes.  Registry
admission remains phase-gated until that image/profile passes conformance.

Hermes has two session identities.  RPCs use a short process-local runtime id;
resume/fork durability uses ``stored_session_id``.  ``RunnerSnapshot.thread_id``
always stores the latter and the live id never escapes the active transport.
"""
from __future__ import annotations

from collections import deque
import math
import os
import re
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


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_CONTAINER_ENGINES = frozenset({"docker", "docker.exe", "podman", "podman.exe",
                                "nerdctl", "nerdctl.exe"})
_MAX_EVENTS = 2_000


def _timeout(value: Any, default: float) -> float:
    answer = default if value is None else float(value)
    if not math.isfinite(answer) or answer <= 0:
        raise ValueError("timeout_s must be positive")
    return answer


def _text(value: str | runner_specs.RunInput) -> runner_specs.RunInput:
    item = runner_specs.RunInput.from_value(value)
    if item.image_urls:
        raise ValueError("Hermes Gateway accepts local image files, not image URLs")
    return item


TransportFactory = Callable[[Sequence[str], str, Mapping[str, str]], RpcTransport]
InteractionCallback = Callable[[runner_specs.PendingInteraction], Any]


class HermesGatewayRunner:
    """Hermes protocol adapter that cannot launch outside a container by default."""

    key = "hermes-gateway"
    credential_family = "collie-sidecar"

    def __init__(self, *, container_argv: Sequence[str] = (), model: str = "",
                 default_timeout_s: float = 900.0,
                 env_policy: str = "sidecar-harness",
                 snapshotter: Callable[[str], dict[str, Any]] = workspace_snapshot,
                 transport_factory: TransportFactory | None = None,
                 interaction_callback: InteractionCallback | None = None,
                 event_callback: Callable[[RunnerEvent], Any] | None = None,
                 max_events: int = _MAX_EVENTS):
        self.default_timeout_s = _timeout(default_timeout_s, 900.0)
        runner_env.allowlist(env_policy)
        self.container_argv = tuple(str(part) for part in container_argv)
        if transport_factory is None:
            if not self.container_argv:
                raise runner_specs.RunnerUnavailableError(
                    "Hermes Gateway requires an explicit constrained container command")
            engine = os.path.basename(self.container_argv[0]).lower()
            if engine not in _CONTAINER_ENGINES:
                raise runner_specs.RunnerUnavailableError(
                    "Hermes Gateway launch must use docker, podman, or nerdctl")
        self.model = str(model or "")
        self.env_policy = env_policy
        self.snapshotter = snapshotter
        self.transport_factory = transport_factory
        self.interaction_callback = interaction_callback
        self._event_callback = event_callback
        self.max_events = max(1, int(max_events))
        self.last_env_receipt: dict[str, list[str]] = {"allowed": [], "stripped": []}
        self._run_lock = threading.Lock()
        self._active_lock = threading.RLock()
        self._active: RpcTransport | None = None
        self._active_session_id = ""
        self._active_turn = False
        self._cancel_requested = False

    def set_event_callback(self, callback: Callable[[RunnerEvent], Any] | None) -> None:
        self._event_callback = callback

    def set_interaction_callback(self, callback: InteractionCallback | None) -> None:
        self.interaction_callback = callback

    def handshake(self) -> runner_specs.CapabilityHandshake:
        from .runner_registry import HERMES_GATEWAY_CAPABILITIES
        return runner_specs.CapabilityHandshake(
            runner=self.key, protocol=HERMES_GATEWAY_CAPABILITIES.protocol,
            protocol_version="", capabilities=HERMES_GATEWAY_CAPABILITIES,
            source="hermes-gateway.ready", negotiated_at=time.time())

    def start(self, prompt: str | runner_specs.RunInput, workspace: str, *,
              timeout_s: float | None = None) -> RunnerSnapshot:
        return self._operate(None, "run", _text(prompt), _workspace(workspace),
                             timeout_s)

    def resume(self, snapshot: RunnerSnapshot,
               prompt: str | runner_specs.RunInput, *,
               timeout_s: float | None = None) -> RunnerSnapshot:
        self._validate(snapshot)
        return self._operate(snapshot, "run", _text(prompt), snapshot.workspace,
                             timeout_s)

    def compact(self, snapshot: RunnerSnapshot, *,
                timeout_s: float | None = None) -> RunnerSnapshot:
        self._validate(snapshot)
        return self._operate(snapshot, "compact", None, snapshot.workspace, timeout_s)

    def fork(self, snapshot: RunnerSnapshot, *,
             timeout_s: float | None = None) -> RunnerSnapshot:
        self._validate(snapshot)
        return self._operate(snapshot, "fork", None, snapshot.workspace, timeout_s)

    def steer_current(self, message: str | runner_specs.RunInput) -> bool:
        return self._send_live("session.steer", _text(message).text)

    def follow_up_current(self, message: str | runner_specs.RunInput) -> bool:
        return self._send_live("prompt.submit", _text(message).text)

    def _send_live(self, method: str, text: str) -> bool:
        with self._active_lock:
            if self._active is None or not self._active_turn or not self._active_session_id:
                return False
            try:
                self._active.send({
                    "jsonrpc": "2.0", "id": "collie-live-" + uuid.uuid4().hex,
                    "method": method,
                    "params": {"session_id": self._active_session_id, "text": text},
                })
                return True
            except Exception:
                return False

    def cancel_current(self) -> bool:
        with self._active_lock:
            transport = self._active
            session_id = self._active_session_id
            if transport is None:
                return False
            self._cancel_requested = True
            if session_id:
                try:
                    transport.send({
                        "jsonrpc": "2.0", "id": "collie-interrupt-" + uuid.uuid4().hex,
                        "method": "session.interrupt",
                        "params": {"session_id": session_id},
                    })
                    return True
                except Exception:
                    pass
            try:
                return bool(transport.terminate())
            except Exception:
                return False

    def cancel_for(self, key: str = "") -> bool:
        return self.cancel_current()

    def probe(self, *, now: float | None = None) -> runner_specs.RunnerProbe:
        now = time.time() if now is None else float(now)
        if self.container_argv:
            engine = shutil.which(self.container_argv[0]) or ""
            return runner_specs.RunnerProbe(
                key=self.key, installed=bool(engine),
                executable_path=_display_path(engine) if engine else "",
                login="unknown", billing_class="unknown",
                billing_mode="unconfigured", probed_at=now,
                detail=("constrained container command configured; live Hermes auth and "
                        "billing remain intentionally unevidenced" if engine else
                        "configured container engine is not installed"))
        return runner_specs.RunnerProbe(
            key=self.key, installed=False, probed_at=now,
            detail="Hermes adapter has no constrained container command")

    def _validate(self, snapshot: RunnerSnapshot) -> None:
        if snapshot.runner != self.key:
            raise ValueError("snapshot belongs to %s, not %s" %
                             (snapshot.runner, self.key))
        if snapshot.recovery_required:
            raise RecoveryRequiredError(
                "the previous Hermes turn may have partially mutated the workspace; "
                "inspect or roll back before resuming")
        if _workspace(snapshot.workspace) != snapshot.workspace:
            raise ValueError("snapshot workspace is not canonical")
        if not _SESSION_ID.fullmatch(snapshot.thread_id or ""):
            raise ValueError("snapshot has no safe Hermes stored session id")

    def _transport(self, workspace: str, env: Mapping[str, str]) -> RpcTransport:
        if self.transport_factory is not None:
            return self.transport_factory(self.container_argv, workspace, env)
        return AppServerStdioTransport(self.container_argv, cwd=workspace, env=env)

    def _operate(self, prior: RunnerSnapshot | None, action: str,
                 run_input: runner_specs.RunInput | None, workspace: str,
                 timeout_s: float | None) -> RunnerSnapshot:
        timeout = _timeout(timeout_s, self.default_timeout_s)
        runner_env.assert_no_billing_override(os.environ, self.credential_family)
        env, self.last_env_receipt = runner_env.child_env(self.env_policy)
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("this Hermes runner already has an active operation")
        before = _snapshot(self.snapshotter, workspace)
        started_at = time.time()
        previous = tuple(prior.events) if prior else ()
        cursor = prior.cursor if prior else 0
        events: deque[RunnerEvent] = deque(maxlen=self.max_events)
        pending: list[runner_specs.PendingInteraction] = []
        usage = dict(prior.usage) if prior else {}
        final_output = prior.final_output if prior else ""
        stored_id = prior.thread_id if prior else ""
        runtime_id = ""
        error = ""
        settled = False
        cancelled = False
        transport: RpcTransport | None = None
        close_ok = False
        deadline = time.monotonic() + timeout
        terminal: dict[str, Any] | None = None

        def publish(frame: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
            nonlocal cursor
            event_type, payload, session_id = self._event(frame)
            cursor += 1
            clean = runner_specs.redact_value(frame)
            event = RunnerEvent(cursor=cursor, type=(event_type or "rpc")[:256],
                                payload=clean if isinstance(clean, dict) else {},
                                at=time.time())
            events.append(event)
            if self._event_callback is not None:
                try:
                    self._event_callback(event)
                except Exception:
                    pass
            return event_type, payload, session_id

        def receive() -> dict[str, Any]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Hermes Gateway operation timed out")
            value = transport.receive(remaining) if transport is not None else {}
            if not isinstance(value, dict):
                raise runner_specs.RunnerProtocolError(
                    "Hermes Gateway message is not an object")
            return value

        def observe(frame: dict[str, Any]) -> None:
            nonlocal terminal, final_output, usage, error
            event_type, payload, session_id = publish(frame)
            if runtime_id and session_id and session_id != runtime_id:
                return
            if event_type in ("approval.request", "clarify.request",
                              "sudo.request", "secret.request"):
                self._resolve_interaction(transport, runtime_id, event_type,
                                          payload, pending)
            elif event_type == "message.complete":
                terminal = payload
                final_output = str(payload.get("text") or final_output)
                usage = self._usage(payload.get("usage"), usage)
                if str(payload.get("status") or "").lower() in ("error", "failed"):
                    error = _clean_error(str(payload.get("error") or
                                             payload.get("text") or "Hermes turn failed"))
            elif event_type == "error":
                error = error or _clean_error(str(payload.get("message") or
                                                   "Hermes Gateway error"))

        def request(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
            request_id = "collie-" + uuid.uuid4().hex
            assert transport is not None
            transport.send({"jsonrpc": "2.0", "id": request_id,
                            "method": method, "params": dict(params)})
            while True:
                frame = receive()
                if frame.get("id") == request_id:
                    publish(frame)
                    rpc_error = frame.get("error")
                    if rpc_error:
                        detail = (rpc_error.get("message") if isinstance(rpc_error, dict)
                                  else str(rpc_error))
                        raise runner_specs.RunnerProtocolError(
                            "Hermes %s failed: %s" % (method, detail or "unknown"))
                    result = frame.get("result")
                    if not isinstance(result, dict):
                        raise runner_specs.RunnerProtocolError(
                            "Hermes %s returned no object result" % method)
                    return result
                observe(frame)

        try:
            transport = self._transport(workspace, env)
            with self._active_lock:
                self._active = transport
                self._active_session_id = ""
                self._active_turn = False
                self._cancel_requested = False

            ready = False
            while not ready:
                frame = receive()
                event_type, _payload, _sid = publish(frame)
                ready = event_type == "gateway.ready"

            if prior is None:
                params: dict[str, Any] = {"cwd": workspace, "source": "collie"}
                if self.model:
                    params["model"] = self.model
                session = request("session.create", params)
            else:
                session = request("session.resume", {
                    "session_id": prior.thread_id, "cwd": workspace,
                    "source": "collie", "omit_messages": True,
                })
            runtime_id = str(session.get("session_id") or "")
            stored_id = str(session.get("stored_session_id") or
                            session.get("session_key") or
                            session.get("resumed") or stored_id)
            if not _SESSION_ID.fullmatch(runtime_id) or not _SESSION_ID.fullmatch(stored_id):
                raise runner_specs.RunnerProtocolError(
                    "Hermes returned an unsafe runtime or stored session id")
            with self._active_lock:
                self._active_session_id = runtime_id

            if action == "run":
                assert run_input is not None
                for image_file in run_input.image_files:
                    path = os.path.realpath(image_file)
                    try:
                        common = os.path.commonpath((workspace, path))
                    except ValueError:
                        common = ""
                    if common != workspace or not os.path.isfile(path):
                        raise ValueError("Hermes image file must exist inside the workspace")
                    request("image.attach", {"session_id": runtime_id, "path": path})
                with self._active_lock:
                    self._active_turn = True
                request("prompt.submit", {"session_id": runtime_id,
                                          "text": run_input.text})
                while terminal is None and not error:
                    observe(receive())
                settled = terminal is not None and not error and not pending
            elif action == "compact":
                result = request("session.compress", {"session_id": runtime_id})
                publish({"type": "context.compacted", "payload": result,
                         "session_id": runtime_id})
                settled = str(result.get("status") or "compressed") != "aborted"
            else:
                result = request("session.branch", {"session_id": runtime_id})
                child_runtime = str(result.get("session_id") or "")
                child_stored = str(result.get("stored_session_id") or "")
                if (not _SESSION_ID.fullmatch(child_runtime)
                        or not _SESSION_ID.fullmatch(child_stored)
                        or child_stored == stored_id):
                    raise runner_specs.RunnerProtocolError(
                        "Hermes branch returned an unsafe or unchanged session id")
                publish({"type": "session.forked", "payload": result,
                         "session_id": child_runtime})
                stored_id = child_stored
                settled = True
        except Exception as exc:
            error = error or _clean_error("%s: %s" % (type(exc).__name__, exc))
        finally:
            with self._active_lock:
                cancelled = self._cancel_requested
                self._active_turn = False
                self._active_session_id = ""
                self._active = None
            if transport is not None:
                try:
                    close_ok = bool(transport.close())
                except Exception:
                    close_ok = False
            self._run_lock.release()

        after = _snapshot(self.snapshotter, workspace)
        mutated, complete = _mutation(before, after)
        if transport is not None and not close_ok:
            settled = False
            error = error or "Hermes container process-tree extinction was not confirmed"
        if cancelled:
            settled = False
            error = "Hermes operation was cancelled"
        if pending:
            settled = False
            error = error or "Hermes requested host interaction that was not resolved"
        recovery = not settled and (mutated or not complete)
        return RunnerSnapshot(
            runner=self.key, workspace=workspace, thread_id=stored_id,
            cursor=cursor, events=(previous + tuple(events))[-self.max_events:],
            usage=usage, settled=settled,
            exit_code=getattr(transport, "returncode", None), error=error,
            recovery_required=recovery, mutated=mutated,
            mutation_check_complete=complete,
            workspace_digest=str(after.get("tree_digest") or ""),
            final_output=final_output, timed_out="timed out" in error.lower(),
            cancelled=cancelled,
            invocation=(prior.invocation if prior else 0) + (1 if action == "run" else 0),
            started_at=started_at, finished_at=time.time(),
            pending_interactions=tuple(pending))

    @staticmethod
    def _event(frame: Mapping[str, Any]) -> tuple[str, dict[str, Any], str]:
        if frame.get("method") == "event" and isinstance(frame.get("params"), dict):
            value = frame["params"]
        elif isinstance(frame.get("type"), str):
            value = frame
        else:
            return "", {}, ""
        payload = value.get("payload")
        payload = dict(payload) if isinstance(payload, dict) else {}
        return (str(value.get("type") or ""), payload,
                str(value.get("session_id") or payload.get("session_id") or ""))

    def _resolve_interaction(self, transport: RpcTransport | None, session_id: str,
                             kind: str, payload: dict[str, Any],
                             pending: list[runner_specs.PendingInteraction]) -> None:
        request_id = str(payload.get("request_id") or "")
        interaction = runner_specs.PendingInteraction(
            interaction_id=request_id, runner=self.key, kind=kind,
            prompt=str(payload.get("question") or payload.get("description") or
                       payload.get("prompt") or ""),
            options=tuple(str(item) for item in (payload.get("choices") or ())),
            raw=payload, created_at=time.time())
        answer: Any = None
        if self.interaction_callback is not None and kind not in ("sudo.request",
                                                                  "secret.request"):
            try:
                answer = self.interaction_callback(interaction)
            except Exception:
                answer = None
        if transport is None or not session_id:
            pending.append(interaction)
            return
        response_id = "collie-interaction-" + uuid.uuid4().hex
        if kind == "approval.request":
            choice = str(answer or "deny")
            if choice not in ("once", "session", "deny"):
                choice = "deny"
            method = "approval.respond"
            params = {"session_id": session_id, "choice": choice}
        elif kind == "clarify.request":
            method = "clarify.respond"
            params = {"request_id": request_id, "answer": str(answer or "")[:4_000]}
            if answer is None:
                pending.append(interaction)
        elif kind == "sudo.request":
            method = "sudo.respond"
            params = {"request_id": request_id, "password": ""}
        else:
            method = "secret.respond"
            params = {"request_id": request_id, "value": ""}
        transport.send({"jsonrpc": "2.0", "id": response_id,
                        "method": method, "params": params})

    @staticmethod
    def _usage(value: Any, prior: Mapping[str, Any]) -> dict[str, int | float]:
        if not isinstance(value, dict):
            return dict(prior)
        result: dict[str, int | float] = {}
        fields = {
            "input": "input_tokens", "input_tokens": "input_tokens",
            "output": "output_tokens", "output_tokens": "output_tokens",
            "cache_read_tokens": "cached_input_tokens",
            "cache_write_tokens": "cache_write_input_tokens",
            "estimated_cost": "cost_usd",
        }
        for source, target in fields.items():
            raw = value.get(source)
            if (isinstance(raw, (int, float)) and not isinstance(raw, bool)
                    and math.isfinite(float(raw)) and raw >= 0):
                result[target] = float(raw) if target == "cost_usd" else int(raw)
        return result or dict(prior)


__all__ = ["HermesGatewayRunner"]
