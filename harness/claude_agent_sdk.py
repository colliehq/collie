"""Experimental Claude Agent SDK transport for Collie's model loop.

The SDK is deliberately isolated in a short-lived worker process.  The core
package remains stdlib-only, no ``claude -p`` command is involved, and the SDK
is configured as a one-message reasoner with every foreign tool surface empty.
Collie still owns the system prompt, tool protocol, loop, and request budget.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .providers import (ClaudeCliProvider, Completion, ModelProvider, Usage,
                        _parse_response_envelope, content_text, provider_default_model)


_MAX_STDOUT = 2 * 1024 * 1024
_MAX_STDERR = 128 * 1024

# Attachment transport bounds.  The canonical bundle limit (24MB) is a storage
# limit, not a model-request limit: everything here has to survive one JSON
# stdin write to a short-lived worker.  Every bound refuses explicitly; the
# latest user message's images are never truncated to fit.
_IMAGE_MEDIA_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
_MAX_IMAGE_B64 = 5 * 1024 * 1024
_MAX_REQUEST_IMAGES = 16
_MAX_REQUEST_IMAGE_B64 = 8 * 1024 * 1024
_MAX_MULTIMODAL_REQUEST_BYTES = 12 * 1024 * 1024


# Provider-rejection categories the worker may report (the SDK's stable
# ``AssistantMessage.error`` literals), mapped to the status and the wording
# Collie's existing classify_error() already understands, so a rate limit stays
# retryable while a billing or auth refusal stays terminal.  The provider's own
# numeric ``api_error_status`` is deliberately not consulted: nothing here needs
# it, and a provider-supplied number must not be able to turn a terminal
# category into a retryable one.
_PROVIDER_ERROR_CATEGORIES = {
    "authentication_failed": (401, "authentication_error"),
    "billing_error": (402, "billing"),
    "invalid_request": (400, "invalid request"),
    "rate_limit": (429, "rate limit"),
    "server_error": (500, "server error"),
}


class _ProviderRejected(RuntimeError):
    """A physically issued request the provider refused.

    Any reported usage and the stable category travel with
    the failure instead of being flattened into a generic worker exit.  This is
    never a response-contract miss: the model produced no answer to repair.
    """

    def __init__(self, category: str, usage, api_key_source: str):
        super().__init__("provider rejected the request: " + category)
        self.category = category
        self.usage = usage
        self.api_key_source = api_key_source


class _AttachmentRefused(ValueError):
    """A refusal raised before any physical model request is spent.

    Malformed, unsupported, or over-budget attachments must fail visibly.  The
    one thing this transport may never do is quietly deliver a conversation
    whose pixels were dropped, since the caller cannot tell the difference
    between "the model looked and disagreed" and "the model never saw it".
    """


def _sniffed_media_type(raw: bytes) -> str:
    """Identify the container of decoded image bytes; "" when unrecognized."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _checked_image(block) -> dict:
    """Validate one canonical image block for model-facing transport.

    Never quotes the attachment itself: a refusal message is logged by the
    caller, and base64 payload bytes must not reach any log or transcript.
    """
    media_type = block.get("media_type") or "image/png"
    data = block.get("data")
    if not isinstance(media_type, str) or media_type not in _IMAGE_MEDIA_TYPES:
        raise _AttachmentRefused(
            "unsupported image media type; supported: " + ", ".join(_IMAGE_MEDIA_TYPES))
    if not isinstance(data, str) or not data:
        raise _AttachmentRefused("image attachment is missing base64 data")
    if len(data) > _MAX_IMAGE_B64:
        raise _AttachmentRefused(
            "one image attachment exceeds %d base64 bytes; nothing was truncated"
            % _MAX_IMAGE_B64)
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _AttachmentRefused("invalid base64 image data") from exc
    if not raw:
        raise _AttachmentRefused("image attachment decoded to no bytes")
    sniffed = _sniffed_media_type(raw)
    if sniffed != media_type:
        # A declared type the model cannot decode is worse than a refusal: the
        # request would burn quota and come back describing nothing.
        raise _AttachmentRefused(
            "image data does not match its declared %s media type" % media_type)
    return {"media_type": media_type, "data": data}


def _attachments(messages) -> list:
    """Validated canonical images, in conversation order, newest message last."""
    last_user = -1
    for index, message in enumerate(messages):
        if str(message.get("role") or "") == "user":
            last_user = index
    found = []
    for index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        role = str(message.get("role") or "")
        images = []
        for block in content:
            if not isinstance(block, dict):
                raise _AttachmentRefused("conversation content block is not an object")
            kind = str(block.get("type") or "")
            if kind == "text":
                continue
            if kind != "image":
                raise _AttachmentRefused(
                    "unsupported conversation content block type: %s"
                    % (kind or "unknown")[:40])
            if role != "user":
                raise _AttachmentRefused(
                    "image attachments are only supported on user messages")
            images.append(_checked_image(block))
        for position, entry in enumerate(images, 1):
            entry.update(message=index, position=position, count=len(images),
                         latest=(index == last_user))
            found.append(entry)
    return found


def _apply_attachment_budget(entries) -> None:
    """Fit the request to its byte/count bounds, newest attachments first.

    The current turn's images are mandatory — a caller that just attached a
    screenshot must either get it delivered or get an error.  Older images are
    dropped oldest-first and only with a visible in-conversation marker, so the
    model is told an earlier attachment is absent rather than being left to
    mistake some other image for it.
    """
    latest = [entry for entry in entries if entry["latest"]]
    history = [entry for entry in entries if not entry["latest"]]
    used = sum(len(entry["data"]) for entry in latest)
    if len(latest) > _MAX_REQUEST_IMAGES or used > _MAX_REQUEST_IMAGE_B64:
        raise _AttachmentRefused(
            "the latest message's %d image attachments exceed this request's "
            "budget of %d images / %d base64 bytes; nothing was truncated"
            % (len(latest), _MAX_REQUEST_IMAGES, _MAX_REQUEST_IMAGE_B64))
    kept = len(latest)
    exhausted = False
    for entry in reversed(history):
        size = len(entry["data"])
        if (exhausted or kept + 1 > _MAX_REQUEST_IMAGES
                or used + size > _MAX_REQUEST_IMAGE_B64):
            # Everything older than the first drop goes too: a conversation
            # that keeps image 1 and drops image 2 reads as if image 1 were the
            # recent one.
            exhausted = True
            entry["dropped"] = True
            continue
        used += size
        kept += 1


def _marked_messages(messages, entries, nonce: str) -> list:
    """Render attachments as placeholders inside the existing text protocol.

    Collie's prompt text (tool list, response contract, role labels) stays owned
    by one implementation.  Each image becomes a labelled placeholder line in
    its own message, which the splice below replaces with a real image block —
    so an attachment cannot drift away from the message it belongs to.
    """
    by_message = {}
    for entry in entries:
        by_message.setdefault(entry["message"], []).append(entry)
    marked = []
    for index, message in enumerate(messages):
        images = by_message.get(index)
        if not images:
            marked.append(message)
            continue
        lines = []
        text = content_text(message.get("content"))
        if text:
            lines.append(text)
        for entry in images:
            label = "attachment %d of %d (%s)" % (
                entry["position"], entry["count"], entry["media_type"])
            if entry.get("dropped"):
                lines.append("[%s was omitted: this request's attachment budget "
                             "was reached]" % label)
                continue
            entry["token"] = "collie-attachment-%s-%d-%d" % (
                nonce, index, entry["position"])
            lines.append("[%s follows]" % label)
            lines.append(entry["token"])
        copy = dict(message)
        copy["content"] = "\n".join(lines)
        marked.append(copy)
    return marked


def _spliced_blocks(text: str, entries) -> list:
    """Placeholder text -> ordered text/image blocks; never leaks a placeholder."""
    kept = [entry for entry in entries if entry.get("token")]
    if not kept:
        raise _AttachmentRefused(
            "every image attachment was dropped by this request's budget")
    for entry in kept:
        if text.count(entry["token"]) != 1:
            raise _AttachmentRefused("attachment placement could not be verified")
    blocks = []
    rest = text
    for entry in kept:
        head, separator, rest = rest.partition(entry["token"])
        if not separator:
            raise _AttachmentRefused("attachment order could not be verified")
        if head.strip():
            blocks.append({"type": "text", "text": head})
        blocks.append({"type": "image", "media_type": entry["media_type"],
                       "data": entry["data"]})
    if rest.strip():
        blocks.append({"type": "text", "text": rest})
    return blocks


def _reject_json_constant(value):
    raise ValueError("non-finite JSON number is forbidden: %s" % value)


def _usage_counter(value, key):
    raw = value.get(key, 0)
    if raw is None:
        raw = 0
    if (not isinstance(raw, (int, float)) or isinstance(raw, bool)
            or not math.isfinite(float(raw)) or raw < 0
            or not float(raw).is_integer()):
        raise RuntimeError("Claude Agent SDK response has invalid %s usage" % key)
    return int(raw)


def _provider_rejection(payload: dict):
    """A validated provider rejection, or ``None`` to stay on the generic path.

    Every field is re-checked at this process boundary.  A payload that claims
    success, omits the reviewed auth attestation, or carries assistant content
    is not a rejection: those remain fail-closed failures rather than becoming a
    classified, retry-eligible error.
    """
    category = payload.get("provider_error")
    if not isinstance(category, str) or category not in _PROVIDER_ERROR_CATEGORIES:
        return None
    if payload.get("ok") is True:
        raise RuntimeError(
            "Claude Agent SDK worker reported both success and a provider error")
    if payload.get("ok") is not False:
        raise RuntimeError("Claude Agent SDK rejection has an invalid success flag")
    if payload.get("api_key_source") != "none":
        raise RuntimeError(
            "Claude Agent SDK rejection is missing its reviewed auth attestation")
    if "text" in payload or "tool_calls" in payload:
        raise RuntimeError("Claude Agent SDK rejection carried assistant content")
    usage_data = payload.get("usage")
    if not isinstance(usage_data, dict):
        raise RuntimeError("Claude Agent SDK rejection has invalid usage")
    usage = Usage(
        input_tokens=_usage_counter(usage_data, "input_tokens"),
        output_tokens=_usage_counter(usage_data, "output_tokens"),
        cache_read=_usage_counter(usage_data, "cache_read_input_tokens"),
        cache_creation=_usage_counter(usage_data, "cache_creation_input_tokens"),
    )
    return _ProviderRejected(category, usage, "none")


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

    def __init__(self, model: str | None = None, timeout: int = 180,
                 effort: str | None = None, subscription_only: bool = False):
        model = model or provider_default_model(self.name)
        self.model = "claude-agent-sdk:" + model
        self._model = model
        self.timeout = int(timeout)
        self.effort = effort or "default"
        self.subscription_only = bool(subscription_only)
        self._process_condition = threading.Condition(threading.RLock())
        self._active_runs: dict[str, dict] = {}

    def _prompt(self, messages, tool_schemas) -> str:
        # ContextComposer already chooses which tool results to keep or elide.
        # A second, silent 2,000-character cut here hid recent file/error tails
        # and even the composer's omission markers from the model.
        return ClaudeCliProvider._prompt(
            self, messages, tool_schemas, tool_result_limit=None)

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
            # content_text, never str(content): a multimodal message's content is
            # a block list, and stringifying it would paste base64 into prose.
            lines.append("%s: %s" % (role, content_text(message.get("content", ""))))
        lines.append("\nRespond to the latest user message according to the system prompt.")
        return "\n".join(lines)

    def _payload(self, messages, tool_schemas):
        """The model-facing prompt: a plain string, or ordered content blocks.

        Text-only conversations share the same serializer as the text portions
        of multimodal requests, including composer-selected tool results.
        """
        entries = _attachments(messages)
        if not entries:
            return (self._prompt(messages, tool_schemas) if tool_schemas
                    else self._plain_prompt(messages))
        _apply_attachment_budget(entries)
        marked = _marked_messages(messages, entries, uuid.uuid4().hex)
        text = (self._prompt(marked, tool_schemas) if tool_schemas
                else self._plain_prompt(marked))
        if not any(entry.get("token") for entry in entries):
            # Reachable only when the current turn attached nothing and every
            # older image fell outside the budget.  The text protocol still
            # states each omission, so this is neither a silent drop nor a
            # refusal of a turn that attached nothing.
            return text
        return _spliced_blocks(text, entries)

    def _worker_request(self, system, payload) -> dict:
        """Worker protocol 1 = text prompt; protocol 2 = canonical content blocks.

        The version is raised only for multimodal calls, so a worker that
        predates image transport rejects the request outright instead of
        answering about an image it never received.
        """
        request = {
            "protocol": 1,
            "model": self._model,
            "system_prompt": system,
            "prompt": payload,
            "effort": self.effort,
        }
        if isinstance(payload, list):
            request.pop("prompt")
            request["protocol"] = 2
            request["content"] = payload
            size = len(json.dumps(request, ensure_ascii=False,
                                  allow_nan=False).encode("utf-8"))
            if size > _MAX_MULTIMODAL_REQUEST_BYTES:
                raise _AttachmentRefused(
                    "this request is %d bytes with its attachments and exceeds the "
                    "%d byte transport limit; nothing was truncated"
                    % (size, _MAX_MULTIMODAL_REQUEST_BYTES))
        return request

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
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
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
                failed = None
                try:
                    failed = json.loads(raw.decode("utf-8"),
                                        parse_constant=_reject_json_constant)
                except Exception:
                    detail = detail or raw.decode("utf-8", "replace")
                if isinstance(failed, dict):
                    # A rejected request still physically happened.  Recover its
                    # classification and usage before the exit code becomes a
                    # generic, unclassifiable failure.
                    rejection = _provider_rejection(failed)
                    if rejection is not None:
                        raise rejection
                    detail = failed.get("error") or detail
            raise RuntimeError("Claude Agent SDK worker exited %d%s" % (
                proc.returncode, (": " + _safe_failure(detail)) if detail else ""))
        try:
            result = json.loads(raw.decode("utf-8"),
                                parse_constant=_reject_json_constant)
        except Exception as exc:
            raise RuntimeError("Claude Agent SDK worker returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise RuntimeError("Claude Agent SDK worker returned an invalid result")
        if not result.get("ok"):
            raise RuntimeError("Claude Agent SDK worker failed%s" % (
                (": " + _safe_failure(result.get("error"))) if result.get("error") else ""))
        if result.get("provider_error") is not None:
            # A rejection is reported by a non-zero exit only.  A success
            # payload which also names a provider error is forged.
            raise RuntimeError(
                "Claude Agent SDK worker reported both success and a provider error")
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

            payload = self._payload(messages, tool_schemas)
            data = self._run_worker(
                self._worker_request(system, payload), cancel_scope=cancel_scope,
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
                input_tokens=_usage_counter(usage_data, "input_tokens"),
                output_tokens=_usage_counter(usage_data, "output_tokens"),
                cache_read=_usage_counter(usage_data, "cache_read_input_tokens"),
                cache_creation=_usage_counter(usage_data, "cache_creation_input_tokens"),
            )
            allowed_tools = ({str(schema.get("name") or "") for schema in tool_schemas}
                             if tool_schemas else None)
            envelope = _parse_response_envelope(text, allowed_tools=allowed_tools)
            if envelope and envelope[0] == "tool":
                tool_call = envelope[1]
                status = "completed"
                completion = Completion(tool_calls=[tool_call], usage=usage,
                                        stop_reason="tool_use", request_count=1)
                completion.api_key_source = api_key_source
                return completion
            if envelope and envelope[0] == "answer":
                answer = envelope[1]
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
            if tool_schemas:
                # The SDK request itself succeeded and consumed quota, but the result cannot safely
                # drive Collie's executor.  Surface a stable semantic error so Harness can spend at
                # most one separately-authorized corrective turn.  Never stream or persist the
                # rejected assistant text.
                status = "completed"
                completion = Completion(
                    text="ERROR(claude-agent-sdk): response contract error",
                    usage=usage, stop_reason="error", error_status=422,
                    error_code="response_contract_error",
                    error_detail=("response_contract_error: assistant response did not match "
                                  "Collie's tool/answer contract"),
                    request_count=1)
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
        except _ProviderRejected as exc:
            # The provider refused a request Collie physically issued.
            # Report the measured usage and a stable, content-free
            # category so the host's retry policy can tell a rate limit from an
            # auth or billing failure.  No rejected assistant text is streamed
            # or returned, and no structured-response repair turn is implied:
            # the model produced no answer to repair.  The reservation settles
            # as an error because the call yielded no usable response.
            error_status, keyword = _PROVIDER_ERROR_CATEGORIES[exc.category]
            detail = "provider rejected the request: %s (%s)" % (exc.category, keyword)
            completion = Completion(
                text="ERROR(claude-agent-sdk): " + detail, usage=exc.usage,
                stop_reason="error", error_status=error_status,
                error_code="provider_" + exc.category, error_detail=detail,
                request_count=1)
            completion.api_key_source = exc.api_key_source
            return completion
        except _AttachmentRefused as exc:
            # Refused before the worker was spawned: the reservation is released
            # by the finally block and no physical request was consumed.
            detail = "attachment refused: " + _safe_failure(exc)
            return Completion(text="ERROR(claude-agent-sdk): " + detail,
                              stop_reason="error", error_detail=detail,
                              request_count=0)
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
