"""Official Codex Python SDK adapter with a sanitized sidecar boundary."""
from __future__ import annotations

from collections import deque
import importlib.metadata
import importlib.util
import json
import os
import sys
import threading
import time
from typing import Any, Callable, Mapping

from . import runner_env, runner_specs
from .agent_runners import (
    ProcessOutcome,
    ProcessRunner,
    RecoveryRequiredError,
    RunnerEvent,
    RunnerSnapshot,
    SubprocessRunner,
    _THREAD_ID,
    _clean_error,
    _codex_login_state,
    _finite_number,
    _mutation,
    _run_process,
    _snapshot,
    _terminate_owned_process,
    _workspace,
)
from .verification import workspace_snapshot


class CodexSdkRunner:
    """Background-oriented SDK runner.

    The official SDK is intentionally hosted out of process.  Its ``env``
    config adds values to the current environment, while Collie's billing
    contract requires a complete allowlisted environment with ambient provider
    keys and endpoint overrides removed before the SDK starts.
    """

    key = "codex-sdk"
    credential_family = "codex"

    def __init__(self, *, model: str = "", process_runner: ProcessRunner | None = None,
                 snapshotter: Callable[[str], dict[str, Any]] = workspace_snapshot,
                 default_timeout_s: float = 900.0, max_events: int = 2_000,
                 max_event_chars: int = 128_000, env_policy: str = "codex",
                 event_callback: Callable[[RunnerEvent], Any] | None = None):
        if not _finite_number(default_timeout_s) or float(default_timeout_s) <= 0:
            raise ValueError("default_timeout_s must be positive")
        runner_env.allowlist(env_policy)
        self.model = str(model or "")
        self.process_runner = process_runner or SubprocessRunner()
        self.snapshotter = snapshotter
        self.default_timeout_s = float(default_timeout_s)
        self.max_events = max(1, int(max_events))
        self.max_event_chars = max(1_024, int(max_event_chars))
        self.env_policy = env_policy
        self._event_callback = event_callback
        self.last_env_receipt: dict[str, list[str]] = {"allowed": [], "stripped": []}
        self._run_lock = threading.Lock()
        self._active_condition = threading.Condition(threading.Lock())
        self._active_process: Any = None
        self._starting = False
        self._cancel_requested = False

    def set_event_callback(self, callback: Callable[[RunnerEvent], Any] | None) -> None:
        self._event_callback = callback

    def handshake(self) -> runner_specs.CapabilityHandshake:
        from .runner_registry import CODEX_SDK_CAPABILITIES
        return runner_specs.CapabilityHandshake(
            runner=self.key, protocol=CODEX_SDK_CAPABILITIES.protocol,
            protocol_version=self.probe().version,
            capabilities=CODEX_SDK_CAPABILITIES, source="official-sdk",
            negotiated_at=time.time())

    def start(self, prompt: str | runner_specs.RunInput, workspace: str, *,
              timeout_s: float | None = None) -> RunnerSnapshot:
        return self._invoke(None, "run", runner_specs.RunInput.from_value(prompt),
                            _workspace(workspace), timeout_s)

    def resume(self, snapshot: RunnerSnapshot,
               prompt: str | runner_specs.RunInput, *,
               timeout_s: float | None = None) -> RunnerSnapshot:
        self._validate_snapshot(snapshot)
        return self._invoke(snapshot, "run", runner_specs.RunInput.from_value(prompt),
                            snapshot.workspace, timeout_s)

    def fork(self, snapshot: RunnerSnapshot, *,
             timeout_s: float | None = None) -> RunnerSnapshot:
        self._validate_snapshot(snapshot)
        return self._invoke(snapshot, "fork", None, snapshot.workspace, timeout_s)

    def compact(self, snapshot: RunnerSnapshot, *,
                timeout_s: float | None = None) -> RunnerSnapshot:
        self._validate_snapshot(snapshot)
        return self._invoke(snapshot, "compact", None, snapshot.workspace, timeout_s)

    def steer_current(self, message: str | runner_specs.RunInput) -> bool:
        # This sidecar is deliberately single-request.  The interactive App
        # Server adapter owns mid-turn steering and approval round-trips.
        return False

    def follow_up_current(self, message: str | runner_specs.RunInput) -> bool:
        return False

    def cancel_current(self) -> bool:
        deadline = time.monotonic() + 5.0
        with self._active_condition:
            if self._active_process is None and not self._starting:
                return False
            self._cancel_requested = True
            while self._active_process is None and self._starting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._active_condition.wait(remaining)
            proc = self._active_process
            if proc is None:
                return True
        return _terminate_owned_process(proc, max(0.0, deadline - time.monotonic()))

    def cancel_for(self, key: str = "") -> bool:
        return self.cancel_current()

    def probe(self, *, now: float | None = None,
              codex_home: str | None = None) -> runner_specs.RunnerProbe:
        now = time.time() if now is None else float(now)
        installed = importlib.util.find_spec("openai_codex") is not None
        version = ""
        if installed:
            try:
                version = importlib.metadata.version("openai-codex")
            except importlib.metadata.PackageNotFoundError:
                installed = False
        if not installed:
            return runner_specs.RunnerProbe(
                key=self.key, installed=False, probed_at=now,
                detail="the optional openai-codex Python SDK is not installed; "
                       "install collie-harness[codex]")
        home = codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        login, detail, evidence = _codex_login_state(os.path.join(home, "auth.json"), now)
        billing = "api_metered" if evidence.get("login_kind") == "api-key" else "unknown"
        return runner_specs.RunnerProbe(
            key=self.key, installed=True, executable_path="openai_codex",
            version=version, login=login, billing_class=billing,
            billing_mode=runner_specs.BILLING_MODE_OF.get(billing, "unconfigured"),
            billing_evidence=evidence, probed_at=now, detail=detail)

    def _validate_snapshot(self, snapshot: RunnerSnapshot) -> None:
        if snapshot.runner != self.key:
            raise ValueError("snapshot belongs to %s, not %s" %
                             (snapshot.runner, self.key))
        if snapshot.recovery_required:
            raise RecoveryRequiredError(
                "the previous SDK turn may have partially mutated the workspace; "
                "inspect or roll back before resuming")
        if _workspace(snapshot.workspace) != snapshot.workspace:
            raise ValueError("snapshot workspace is not canonical")
        if not snapshot.thread_id or not _THREAD_ID.fullmatch(snapshot.thread_id):
            raise ValueError("snapshot has no safe Codex thread id")

    def _invoke(self, prior: RunnerSnapshot | None, action: str,
                turn_input: runner_specs.RunInput | None, workspace: str,
                timeout_s: float | None) -> RunnerSnapshot:
        timeout = self.default_timeout_s if timeout_s is None else float(timeout_s)
        if not _finite_number(timeout) or timeout <= 0:
            raise ValueError("timeout_s must be positive")
        if turn_input is not None:
            turn_input = self._validate_input(turn_input, workspace)
            self.handshake().require(input_kinds=turn_input.kinds)
        runner_env.assert_no_billing_override(os.environ, self.credential_family)
        env, self.last_env_receipt = runner_env.child_env(self.env_policy)
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("this Codex SDK runner already has an active operation")

        request: dict[str, Any] = {
            "action": action, "workspace": workspace, "model": self.model,
            "thread_id": prior.thread_id if prior else "",
        }
        if turn_input is not None:
            request["input"] = turn_input.to_dict()
        worker = os.path.join(os.path.dirname(__file__), "codex_sdk_worker.py")
        argv = [sys.executable, worker]
        before = _snapshot(self.snapshotter, workspace)
        started_at = time.time()
        prior_cursor = prior.cursor if prior else 0
        process_started = False
        raised: Exception | None = None
        outcome: ProcessOutcome | None = None
        with self._active_condition:
            self._active_process = None
            self._starting = True
            self._cancel_requested = False
            self._active_condition.notify_all()

        def register(proc: Any) -> bool:
            nonlocal process_started
            process_started = True
            with self._active_condition:
                self._active_process = proc
                self._starting = False
                allowed = not self._cancel_requested
                self._active_condition.notify_all()
                return allowed

        live_cursor = prior_cursor

        def live(record: str) -> None:
            nonlocal live_cursor
            if self._event_callback is None:
                return
            for raw in record.split("\n"):
                if not raw.strip():
                    continue
                live_cursor += 1
                event, _ = self._event(raw.rstrip("\r"), live_cursor)
                try:
                    self._event_callback(event)
                except Exception:
                    pass

        try:
            try:
                outcome = _run_process(
                    self.process_runner, argv, cwd=workspace,
                    stdin_text=json.dumps(request, ensure_ascii=False, allow_nan=False),
                    timeout_s=timeout, on_process=register, env=env,
                    on_stdout=live)
                if not isinstance(outcome, ProcessOutcome):
                    raise TypeError("process runner must return ProcessOutcome")
            except Exception as exc:
                raised = exc
        finally:
            with self._active_condition:
                cancelled = self._cancel_requested
                self._active_process = None
                self._starting = False
                self._active_condition.notify_all()
            self._run_lock.release()

        finished_at = time.time()
        after = _snapshot(self.snapshotter, workspace)
        mutated, mutation_complete = _mutation(before, after)
        previous_events = tuple(prior.events) if prior else ()
        previous_usage = dict(prior.usage) if prior else {}
        invocation = (prior.invocation if prior else 0) + (1 if action == "run" else 0)
        if raised is not None:
            return RunnerSnapshot(
                runner=self.key, workspace=workspace,
                thread_id=prior.thread_id if prior else "", cursor=prior_cursor,
                events=previous_events, usage=previous_usage, settled=False,
                error=_clean_error("%s: %s" % (type(raised).__name__, raised)),
                recovery_required=mutated or (process_started and not mutation_complete),
                mutated=mutated, mutation_check_complete=mutation_complete,
                workspace_digest=str(after.get("tree_digest") or ""),
                final_output=prior.final_output if prior else "",
                cancelled=cancelled, invocation=invocation,
                started_at=started_at, finished_at=finished_at)

        assert outcome is not None
        events, malformed, terminal = self._events(outcome.stdout, prior_cursor)
        all_events = (previous_events + tuple(events))[-self.max_events:]
        thread_id = str(terminal.get("thread_id") or
                        (prior.thread_id if prior else ""))
        if not _THREAD_ID.fullmatch(thread_id):
            malformed = True
        status = str(terminal.get("status") or "")
        cancelled = bool(cancelled or outcome.cancelled)
        settled = bool(outcome.exit_code == 0 and not outcome.timed_out and
                       not cancelled and not malformed and status == "completed")
        error = ""
        if outcome.timed_out:
            error = "Codex SDK operation exceeded its %.1fs wall timeout" % timeout
        elif cancelled:
            error = "Codex SDK operation was cancelled"
        elif outcome.output_truncated:
            error = "Codex SDK output exceeded Collie's bounded capture limit"
        elif malformed:
            error = "Codex SDK sidecar emitted invalid or inconsistent JSONL state"
        elif not settled:
            error = _clean_error(terminal.get("error") or outcome.stderr) or \
                "Codex SDK operation did not complete"
        usage = dict(previous_usage)
        usage.update(self._usage(terminal.get("usage")))
        final_output = str(terminal.get("final_output") or
                           (prior.final_output if prior else ""))
        recovery = (not settled) and (mutated or (process_started and not mutation_complete))
        return RunnerSnapshot(
            runner=self.key, workspace=workspace, thread_id=thread_id,
            cursor=prior_cursor + len(events), events=all_events, usage=usage,
            settled=settled, exit_code=outcome.exit_code,
            error=_clean_error(error), recovery_required=recovery,
            mutated=mutated, mutation_check_complete=mutation_complete,
            workspace_digest=str(after.get("tree_digest") or ""),
            final_output=final_output, timed_out=bool(outcome.timed_out),
            cancelled=cancelled, invocation=invocation,
            started_at=started_at, finished_at=finished_at)

    @staticmethod
    def _validate_input(value: runner_specs.RunInput,
                        workspace: str) -> runner_specs.RunInput:
        for url in value.image_urls:
            if not str(url).startswith("data:image/"):
                raise ValueError("Codex SDK image URLs must be data:image URLs")
        files = []
        for raw in value.image_files:
            path = os.path.realpath(os.path.abspath(str(raw)))
            try:
                inside = os.path.commonpath((workspace, path)) == workspace
            except ValueError:
                inside = False
            if not inside or not os.path.isfile(path):
                raise ValueError(
                    "Codex SDK image files must exist inside the workspace")
            files.append(path)
        return runner_specs.RunInput(text=value.text, image_urls=value.image_urls,
                                     image_files=tuple(files),
                                     metadata=value.metadata)

    def _events(self, stdout: str, cursor: int
                ) -> tuple[list[RunnerEvent], bool, dict[str, Any]]:
        events: deque[RunnerEvent] = deque(maxlen=self.max_events)
        malformed = False
        terminal: dict[str, Any] = {}
        count = 0
        # The sidecar is LFJSONL.  ``str.splitlines()`` also splits literal
        # U+2028/U+2029 inside valid JSON strings and would turn one record into
        # two protocol errors, so only LF is a frame boundary.
        for raw in str(stdout or "").split("\n"):
            raw = raw[:-1] if raw.endswith("\r") else raw
            if not raw.strip():
                continue
            count += 1
            event, bad = self._event(raw, cursor + count)
            events.append(event)
            malformed = malformed or bad
            if event.type == "collie.sdk.result":
                if terminal:
                    malformed = True
                terminal = dict(event.payload)
            elif terminal:
                malformed = True
        if not terminal:
            malformed = True
        return list(events), malformed, terminal

    def _event(self, raw: str, cursor: int) -> tuple[RunnerEvent, bool]:
        bad = False
        try:
            if len(raw) > self.max_event_chars:
                raise ValueError("record too large")
            value = json.loads(raw, parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError("non-finite JSON number: " + item)))
            if not isinstance(value, dict):
                raise ValueError("record is not an object")
        except (ValueError, json.JSONDecodeError, RecursionError):
            bad = True
            value = {"type": "protocol.invalid_json", "preview": raw[:4_096]}
        payload = runner_specs.redact_value(value, self.max_event_chars)
        if not isinstance(payload, dict):
            bad = True
            payload = {"type": "protocol.invalid_json"}
        return RunnerEvent(cursor=cursor,
                           type=str(value.get("type") or "unknown")[:256],
                           payload=payload, at=time.time()), bad

    @staticmethod
    def _usage(value: Any) -> dict[str, int | float]:
        if not isinstance(value, dict):
            return {}
        source = value.get("total") if isinstance(value.get("total"), dict) else value
        aliases = {
            "input_tokens": ("input_tokens", "inputTokens"),
            "cached_input_tokens": ("cached_input_tokens", "cachedInputTokens"),
            "output_tokens": ("output_tokens", "outputTokens"),
            "reasoning_output_tokens": ("reasoning_output_tokens", "reasoningOutputTokens"),
        }
        result: dict[str, int | float] = {}
        for target, names in aliases.items():
            amount = next((source.get(name) for name in names if name in source), None)
            if _finite_number(amount) and float(amount) >= 0:
                result[target] = int(amount)
        return result


__all__ = ["CodexSdkRunner"]
