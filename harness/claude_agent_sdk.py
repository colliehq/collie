"""Experimental Claude Agent SDK transport for Collie's model loop.

The SDK is deliberately isolated in a short-lived worker process.  The core
package remains stdlib-only, no ``claude -p`` command is involved, and the SDK
is configured as a one-message reasoner with every foreign tool surface empty.
Collie still owns the system prompt, tool protocol, loop, and request budget.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .providers import (ClaudeCliProvider, Completion, ModelProvider, Usage,
                        _parse_answer_json, _parse_tool_json)


_MAX_STDOUT = 2 * 1024 * 1024
_MAX_STDERR = 128 * 1024


def _sanitized_worker_env(source=None) -> dict[str, str]:
    """Minimal process environment; credentials/config overrides never cross.

    The SDK worker can discover the official Claude login store itself.  Passing
    provider, proxy, or TLS variables would let ambient shell state silently
    change either billing authority or the destination of a bearer credential.
    """
    source = dict(os.environ if source is None else source)
    allowed = {
        "APPDATA", "COMSPEC", "HOME", "HOMEDRIVE", "HOMEPATH",
        "LANG", "LC_ALL", "LC_CTYPE", "LOCALAPPDATA", "LOGNAME",
        "PATH", "PATHEXT", "PROGRAMDATA", "PROGRAMFILES",
        "PROGRAMFILES(X86)", "PROGRAMW6432", "SHELL", "SYSTEMDRIVE",
        "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USER", "USERNAME",
        "USERPROFILE", "WINDIR",
    }
    clean = {key: value for key, value in source.items()
             if key.upper() in allowed and isinstance(value, str)}
    clean["NO_COLOR"] = "1"
    clean["PYTHONIOENCODING"] = "utf-8"
    clean["PYTHONUNBUFFERED"] = "1"
    return clean


def _safe_failure(value) -> str:
    from .redact import redact
    text = redact(str(value or ""), {})
    return " ".join(text.replace("\x00", " ").split())[:1200]


def _terminate_worker_tree(proc, timeout_s: float = 5.0) -> bool:
    """Terminate an SDK worker and return only after its owned tree is extinct."""
    # Keep the process-tree contract in one implementation.  The import stays
    # lazy so importing the optional provider remains cheap and stdlib-only.
    from .agent_runners import _terminate_owned_process
    return _terminate_owned_process(proc, timeout_s=timeout_s)


class ClaudeAgentSdkProvider(ModelProvider):
    """One SDK query per logical completion, with Collie's JSON tool protocol."""

    name = "claude-agent-sdk"
    supports_request_gate = True

    def __init__(self, model: str = "opus", timeout: int = 180,
                 effort: str | None = None, subscription_only: bool = False):
        self.model = "claude-agent-sdk:" + model
        self._model = model
        self.timeout = int(timeout)
        self.effort = effort or "default"
        self.subscription_only = bool(subscription_only)
        self._process_condition = threading.Condition(threading.RLock())
        self._active_runs: dict[str, dict] = {}

    def _prompt(self, messages, tool_schemas) -> str:
        # Reuse the already-tested text protocol; this is not the CLI transport.
        return ClaudeCliProvider._prompt(self, messages, tool_schemas)

    @staticmethod
    def _plain_prompt(messages) -> str:
        """Serialize a caller-owned conversation without imposing Harness JSON.

        Mission's planner passes no model tools and defines its own action schema
        in the system prompt. Adding the code-loop ``{tool|answer}`` envelope in
        that case changes a valid ``action`` into ``tool`` and makes the planner
        fail closed. Tool-bearing Harness calls continue to use ``_prompt``.
        """
        lines = ["# Conversation so far:"]
        for message in messages:
            role = str(message.get("role") or "user").capitalize()
            content = message.get("content", "")
            lines.append("%s: %s" % (role, str(content)))
        lines.append("\nRespond to the latest user message according to the system prompt.")
        return "\n".join(lines)

    def _worker_request(self, system, prompt) -> dict:
        return {
            "protocol": 1,
            "model": self._model,
            "system_prompt": system,
            "prompt": prompt,
            "effort": self.effort,
        }

    def _register_pending(self, scope: str):
        """Publish a cancellable call before its durable request reservation.

        Prompt serialization happens after the reservation and may itself block
        on caller-owned data.  Publishing this pending state first closes the
        gap where ``cancel_for(scope)`` could otherwise observe no worker, return,
        and then allow the already-reserved call to reach ``Popen`` later.
        """
        invocation = uuid.uuid4().hex
        state = {
            "scope": str(scope or ""), "proc": None,
            "cancel_requested": False, "done": False,
            # No process exists while pending, so cancellation can immediately
            # prove extinction.  _run_worker flips this before committing start.
            "tree_extinct": True, "phase": "pending",
        }
        with self._process_condition:
            self._active_runs[invocation] = state
        return invocation, state

    def _set_pending_scope(self, registration, scope: str) -> None:
        """Attach the reservation-id fallback without replacing Mission scope."""
        invocation, state = registration
        with self._process_condition:
            if self._active_runs.get(invocation) is state and not state["scope"]:
                state["scope"] = str(scope or "")

    def _retire_pending(self, registration) -> None:
        """Retire a call that failed or returned before _run_worker adopted it."""
        invocation, state = registration
        with self._process_condition:
            if self._active_runs.get(invocation) is not state:
                return
            state["done"] = True
            state["tree_extinct"] = bool(
                state["tree_extinct"] or state["proc"] is None)
            self._active_runs.pop(invocation, None)
            self._process_condition.notify_all()

    def _cancel(self, scope: str | None) -> bool:
        """Cancel matching workers and prove their OS process trees are extinct."""
        deadline = time.monotonic() + 5.0
        with self._process_condition:
            states = [state for state in self._active_runs.values()
                      if scope is None or state["scope"] == scope]
            if not states:
                return False
            for state in states:
                state["cancel_requested"] = True
                if state.get("phase") == "pending" and state["proc"] is None:
                    # The shared condition is the start latch: _run_worker must
                    # atomically adopt this state before Popen.  A pending state
                    # can therefore be fenced and proven process-free without
                    # waiting for prompt construction to return.
                    state["tree_extinct"] = True
            # Once start is committed, wait until the trusted worker is either
            # owned and published or failed before receiving any prompt bytes.
            while any(state.get("phase") != "pending" and
                      state["proc"] is None and not state["done"]
                      for state in states):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._process_condition.wait(remaining)
            targets = [(state, state["proc"]) for state in states]

        confirmed = []
        for state, proc in targets:
            if proc is None:
                ok = bool(
                    state["tree_extinct"] and
                    (state.get("phase") == "pending" or state["done"]))
            else:
                ok = _terminate_worker_tree(
                    proc, timeout_s=max(0.0, deadline - time.monotonic()))
            with self._process_condition:
                state["tree_extinct"] = bool(state["tree_extinct"] or ok)
                self._process_condition.notify_all()
            confirmed.append(ok)
        return bool(confirmed) and all(confirmed)

    def cancel_current(self) -> bool:
        """Cancel every active call on this provider instance."""
        return self._cancel(None)

    def cancel_for(self, request_scope):
        """Return a Mission-compatible canceller scoped to one Mission ID."""
        scope = str(request_scope or "")
        return lambda: self._cancel(scope)

    def _run_worker(self, request: dict, cancel_scope: str = "",
                    registration=None) -> dict:
        """Run the optional SDK in a prompt-gated, kernel-owned process tree."""
        from . import plat

        # Use the installed/source file directly. A code Mission changes cwd to
        # the target workspace and the sanitized environment intentionally drops
        # PYTHONPATH, so ``python -m harness...`` is not reliably importable.
        worker = os.path.join(os.path.dirname(__file__), "claude_agent_worker.py")
        # Isolated mode prevents the target workspace, PYTHON* variables, and
        # user-site startup hooks from influencing the trusted transport worker.
        # The optional SDK is installed in Collie's interpreter environment.
        cmd = [sys.executable, "-I", worker]
        popen_kw = {}
        popen_kw.update(plat.new_group_kwargs())
        popen_kw.update(plat.no_window_kwargs())
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
        with self._process_condition:
            if registration is None:
                invocation = uuid.uuid4().hex
                state = {"scope": str(cancel_scope or ""), "proc": None,
                         "cancel_requested": False, "done": False,
                         "tree_extinct": False, "phase": "starting"}
                self._active_runs[invocation] = state
            else:
                invocation, state = registration
                if self._active_runs.get(invocation) is not state:
                    raise RuntimeError(
                        "Claude Agent SDK worker registration was lost")
                if cancel_scope:
                    state["scope"] = str(cancel_scope)
                if state["cancel_requested"]:
                    # cancel_for() already returned process-free proof for this
                    # pending call.  Consume its tombstone without crossing the
                    # physical worker boundary.
                    state["done"] = True
                    state["tree_extinct"] = True
                    self._active_runs.pop(invocation, None)
                    self._process_condition.notify_all()
                    raise RuntimeError("Claude Agent SDK worker was cancelled")
                # This transition and cancel_for's tombstone publication share
                # one lock.  After it, cancellation waits for Popen publication;
                # before it, cancellation returns quickly and Popen is forbidden.
                state["phase"] = "starting"
                state["tree_extinct"] = False

        proc = None
        owner = None
        parent_death_read = parent_death_write = None
        raw = raw_err = b""
        raised = None
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                # Create the exact-parent capability inside the cleanup region,
                # after the pending-call tombstone has been consumed and start
                # committed. Both ends are therefore closed on every later
                # setup/Popen/ownership failure.
                if not plat.is_windows():
                    if popen_kw.get("start_new_session"):
                        parent_death_read, parent_death_write = os.pipe()
                        cmd += ["--collie-parent-death-fd", str(parent_death_read),
                                "--collie-parent-pid", str(os.getpid())]
                        popen_kw["pass_fds"] = (parent_death_read,)
                    else:
                        # Nested Mission workers inherit the outer durable PGID owner.
                        cmd += ["--collie-external-process-owner"]
                # The worker blocks in stdin.read().  It cannot import the SDK,
                # open a socket, or spawn its bundled runtime until ownership is
                # established and the request is deliberately released below.
                try:
                    proc = subprocess.Popen(
                        cmd, stdin=subprocess.PIPE, stdout=stdout_file, stderr=stderr_file,
                        env=_sanitized_worker_env(), cwd=os.getcwd(), **popen_kw)
                finally:
                    # Only the child receives the read capability.  Keeping a
                    # second reader in Collie is unnecessary and complicates FD
                    # accounting; the write end stays live until cleanup below.
                    # Never retry an ambiguous POSIX close: clear our reference
                    # even on EINTR so later cleanup cannot close a reused FD.
                    if parent_death_read is not None:
                        read_fd = parent_death_read
                        parent_death_read = None
                        try:
                            os.close(read_fd)
                        except Exception as exc:
                            raise RuntimeError(
                                "Claude Agent SDK parent-death descriptor cleanup failed") from exc
                proc._collie_tree_lock = threading.RLock()
                if not plat.is_windows() and popen_kw.get("start_new_session"):
                    proc._collie_process_group = int(proc.pid)
                try:
                    owner = plat.attach_kill_on_close_job(proc)
                    if plat.is_windows() and owner is None:
                        raise RuntimeError("Windows Job Object was not created")
                    if owner is not None:
                        proc._collie_kill_job = owner
                except Exception:
                    # No prompt has been sent, so proving the trusted direct
                    # worker exited is sufficient even if Job assignment failed.
                    plat.kill_tree(proc)
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    if proc.poll() is None:
                        raise RuntimeError(
                            "Claude Agent SDK worker ownership failed and cleanup "
                            "could not be confirmed")
                    raise RuntimeError(
                        "Claude Agent SDK worker process-tree ownership could not "
                        "be established")

                with self._process_condition:
                    state["proc"] = proc
                    state["phase"] = "active"
                    cancelled_before_release = bool(state["cancel_requested"])
                    self._process_condition.notify_all()
                if cancelled_before_release:
                    if not _terminate_worker_tree(proc):
                        raise RuntimeError(
                            "Claude Agent SDK cancellation could not prove process-tree "
                            "extinction")
                    raise RuntimeError("Claude Agent SDK worker was cancelled")
                try:
                    proc.communicate(input=payload, timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    if not _terminate_worker_tree(proc):
                        raise RuntimeError(
                            "Claude Agent SDK worker timed out and process-tree "
                            "extinction could not be confirmed")
                    raise TimeoutError("Claude Agent SDK worker timed out")
                stdout_file.seek(0)
                raw = stdout_file.read(_MAX_STDOUT + 1)
                stderr_file.seek(0)
                raw_err = stderr_file.read(_MAX_STDERR + 1)
            except BaseException as exc:
                raised = exc
            finally:
                tree_extinct = proc is None
                if proc is not None:
                    tree_extinct = _terminate_worker_tree(proc)
                cleanup_error = None
                if parent_death_write is not None:
                    try:
                        os.close(parent_death_write)
                    except Exception as exc:
                        # Do not retry EINTR: POSIX permits the descriptor to
                        # have closed already, so a retry could hit a reused FD.
                        # Keep retiring every other ownership surface and fail
                        # closed after state removal.
                        cleanup_error = cleanup_error or exc
                    finally:
                        parent_death_write = None
                if parent_death_read is not None:
                    try:
                        os.close(parent_death_read)
                    except Exception as exc:
                        cleanup_error = cleanup_error or exc
                    finally:
                        parent_death_read = None
                if owner is not None:
                    try:
                        owner.close()
                    except Exception as exc:
                        cleanup_error = cleanup_error or exc
                        tree_extinct = False
                with self._process_condition:
                    state["done"] = True
                    state["tree_extinct"] = bool(
                        state["tree_extinct"] or tree_extinct)
                    self._active_runs.pop(invocation, None)
                    self._process_condition.notify_all()
                if not tree_extinct and raised is None:
                    raised = RuntimeError(
                        "Claude Agent SDK worker process-tree extinction could not "
                        "be confirmed")
                if cleanup_error is not None and raised is None:
                    raised = RuntimeError(
                        "Claude Agent SDK worker ownership cleanup failed")
        if raised is not None:
            raise raised
        if len(raw) > _MAX_STDOUT:
            raise RuntimeError("Claude Agent SDK worker output exceeded the safety limit")
        if len(raw_err) > _MAX_STDERR:
            raise RuntimeError("Claude Agent SDK worker stderr exceeded the safety limit")
        stderr = raw_err.decode("utf-8", "replace")
        if proc.returncode != 0:
            detail = stderr
            if raw:
                try:
                    failed = json.loads(raw.decode("utf-8"))
                    if isinstance(failed, dict):
                        detail = failed.get("error") or detail
                except Exception:
                    detail = detail or raw.decode("utf-8", "replace")
            raise RuntimeError("Claude Agent SDK worker exited %d%s" % (
                proc.returncode, (": " + _safe_failure(detail)) if detail else ""))
        try:
            result = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("Claude Agent SDK worker returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError("Claude Agent SDK worker returned an invalid result")
        if not result.get("ok"):
            raise RuntimeError("Claude Agent SDK worker failed%s" % (
                (": " + _safe_failure(result.get("error"))) if result.get("error") else ""))
        return result

    def complete(self, system, messages, tool_schemas, on_text=None):
        request_gate, request_complete = self.current_request_authority()
        request_gate = request_gate or getattr(self, "request_gate", None)
        request_complete = request_complete or getattr(self, "request_complete", None)
        if self.subscription_only and not callable(request_gate):
            return Completion(
                text="ERROR(claude-agent-sdk): model request authority is missing",
                stop_reason="error", error_detail="model request authority is missing",
                request_count=0)

        # Publish the Mission-scoped call before requesting durable authority.
        # In particular, no reserved call may still be invisible to a
        # concurrent cancel_for(scope) while prompt construction is pending.
        cancel_scope = self.current_request_scope()
        registration = self._register_pending(cancel_scope)
        request_id = ""
        status = "error"
        try:
            if callable(request_gate):
                try:
                    request_id = request_gate("claude_agent_sdk")
                except Exception as exc:
                    detail = "model request reservation failed: " + _safe_failure(exc)
                    return Completion(text="ERROR(claude-agent-sdk): " + detail,
                                      stop_reason="error", error_detail=detail,
                                      request_count=0)
                if not request_id:
                    detail = "model request reservation denied"
                    return Completion(text="ERROR(claude-agent-sdk): " + detail,
                                      stop_reason="error", error_detail=detail,
                                      request_count=0)
                if not cancel_scope:
                    cancel_scope = request_id
                    self._set_pending_scope(registration, cancel_scope)

            prompt = (self._prompt(messages, tool_schemas) if tool_schemas else
                      self._plain_prompt(messages))
            data = self._run_worker(
                self._worker_request(system, prompt), cancel_scope=cancel_scope,
                registration=registration)
            api_key_source = data.get("api_key_source")
            if api_key_source != "none":
                raise RuntimeError(
                    "Claude Agent SDK response is missing its reviewed auth attestation")
            text = data.get("text")
            if not isinstance(text, str):
                raise RuntimeError("Claude Agent SDK response is missing assistant text")
            usage_data = data.get("usage") or {}
            if not isinstance(usage_data, dict):
                raise RuntimeError("Claude Agent SDK response has invalid usage")
            usage = Usage(
                input_tokens=int(usage_data.get("input_tokens", 0) or 0),
                output_tokens=int(usage_data.get("output_tokens", 0) or 0),
                cache_read=int(usage_data.get("cache_read_input_tokens", 0) or 0),
                cache_creation=int(usage_data.get("cache_creation_input_tokens", 0) or 0),
            )
            tool_call = _parse_tool_json(text)
            if tool_call:
                status = "completed"
                completion = Completion(tool_calls=[tool_call], usage=usage,
                                        stop_reason="tool_use", request_count=1)
                completion.api_key_source = api_key_source
                return completion
            answer = _parse_answer_json(text)
            if answer is not None:
                status = "completed"
                if on_text and answer:
                    try:
                        on_text(answer)
                    except Exception:
                        pass
                completion = Completion(text=answer, usage=usage,
                                        stop_reason="end_turn", request_count=1)
                completion.api_key_source = api_key_source
                return completion
            # Provider.complete is also used by Mission's planner, whose own
            # system contract asks for an action-shaped JSON object rather than
            # Harness's {tool|answer} envelope. Preserve any such plain model
            # text exactly, matching the other provider adapters; the caller
            # remains responsible for validating its own schema.
            status = "completed"
            if on_text and text:
                try:
                    on_text(text)
                except Exception:
                    pass
            completion = Completion(text=text, usage=usage,
                                    stop_reason="end_turn", request_count=1)
            completion.api_key_source = api_key_source
            return completion
        except Exception as exc:
            detail = _safe_failure(exc)
            return Completion(text="ERROR(claude-agent-sdk): " + detail,
                              stop_reason="error", error_detail=detail,
                              request_count=1 if request_id or not self.subscription_only else 0)
        finally:
            self._retire_pending(registration)
            if request_id and callable(request_complete):
                try:
                    request_complete(request_id, status)
                except Exception:
                    pass
