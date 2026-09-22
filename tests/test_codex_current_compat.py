"""Contract tests pinned to the shapes official Codex 0.155.1 actually produces.

Every assertion here names the upstream symbol it was read from, at tag
``rust-v0.155.1`` (commit ``be2951ea``).  These are mock contract tests: they
describe the wire and SDK surface Collie must survive, not a live model turn.
"""
from __future__ import annotations

from collections import deque
import enum
import io
import json
import os
import sys
import types

import pytest

from harness import codex_sdk_worker
from harness.codex_app_server_runner import CodexAppServerRunner, gate_approval_callback
from harness.codex_sdk_runner import CodexSdkRunner
from harness.agent_runners import ProcessOutcome
from harness.gate import Gate, Mode, Outcome
from harness.runner_specs import RunInput


def _snapshot(_root):
    return {"tree_digest": "same", "snapshot_complete": True}


# --------------------------------------------------------------------------
# App Server: the stable (non-experimental) protocol surface
# --------------------------------------------------------------------------

class StableSurfaceTransport:
    """The approval request a 0.155.1 server sends a stable client.

    ``availableDecisions`` is declared
    ``#[experimental("item/commandExecution/requestApproval.availableDecisions")]``
    on ``CommandExecutionRequestApprovalParams`` (``app-server-protocol/src/
    protocol/v2/item.rs``), and ``app-server/src/transport.rs`` ->
    ``filter_outgoing_message_for_connection`` calls
    ``strip_experimental_fields()`` for every connection that did not declare
    ``capabilities.experimentalApi``.  Collie's ``initialize`` declares no
    capabilities, so the field is never on the wire and the required fields
    (``itemId``/``threadId``/``turnId``/``startedAtMs``) are all there is.
    """

    def __init__(self):
        self.sent = []
        self.incoming = deque()
        self.approval_answer = None

    returncode = 0
    stderr = ""

    def send(self, message):
        message = dict(message)
        self.sent.append(message)
        method = message.get("method")
        if method == "initialize":
            self.incoming.append({"id": message["id"], "result": {
                "userAgent": "codex/0.155.1", "codexHome": "/tmp/home",
                "platformFamily": "windows", "platformOs": "windows"}})
        elif method == "thread/start":
            self.incoming.append({"id": message["id"], "result": {
                "thread": {"id": "01a0c724-3482-7d60-b2a1-42fa7ec62835"},
                "model": "gpt-6-astra", "modelProvider": "openai",
                "cwd": message["params"]["cwd"], "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "sandbox": {"type": "workspaceWrite"}}})
        elif method == "turn/start":
            self.incoming.append({"id": message["id"], "result": {
                "turn": {"id": "turn_1", "status": "inProgress", "items": []}}})
            self.incoming.append({
                "id": 91,
                "method": "item/commandExecution/requestApproval",
                "params": {"itemId": "item_1", "startedAtMs": 1790047433000,
                           "threadId": "01a0c724-3482-7d60-b2a1-42fa7ec62835",
                           "turnId": "turn_1", "kind": "command",
                           "command": "python -m pytest", "cwd": "/ws"},
            })
        elif message.get("id") == 91 and "result" in message:
            self.approval_answer = message["result"]
            self.incoming.append({"method": "item/completed", "params": {
                "threadId": "01a0c724-3482-7d60-b2a1-42fa7ec62835",
                "turnId": "turn_1", "completedAtMs": 1790047434000,
                "item": {"id": "msg_1", "type": "agentMessage",
                         "text": "done", "phase": "final_answer"}}})
            self.incoming.append({"method": "turn/completed", "params": {
                "threadId": "01a0c724-3482-7d60-b2a1-42fa7ec62835",
                "turn": {"id": "turn_1", "status": "completed", "items": []}}})

    def receive(self, timeout_s):
        if not self.incoming:
            raise TimeoutError("script is empty")
        return self.incoming.popleft()

    def terminate(self, timeout_s=5.0):
        return True

    def close(self):
        return True


def _runner(transport, **kwargs):
    captured = {}

    def factory(argv, cwd, env):
        captured.update(argv=tuple(argv), cwd=cwd, env=dict(env))
        return transport

    return CodexAppServerRunner(executable="codex", snapshotter=_snapshot,
                               transport_factory=factory, **kwargs), captured


def test_launch_argv_uses_only_names_0_155_1_still_recognizes():
    """``--strict-config`` turns a retired name into a launch failure.

    ``codex app-server`` accepts ``--stdio`` (``cli/src/main.rs`` ->
    ``AppServerCommand.stdio``, "equivalent to ``--listen stdio://``") and
    ``--strict-config``, and ``-c`` is ``global = true`` on
    ``CliConfigOverrides`` (``utils/cli/src/config_override.rs``) so it may
    follow the subcommand.  ``config/src/loader/mod.rs`` ->
    ``validate_cli_overrides_strictly`` rejects any ``-c`` key that
    ``ConfigToml`` ignores, or any ``features.*`` key outside
    ``codex_features::is_known_feature_key``.  Each name below was read back
    out of 0.155.1: ``mcp_servers``/``project_doc_max_bytes``/``web_search``/
    ``plugins`` in ``config/src/config_toml.rs``, and ``hooks``/``memories``/
    ``multi_agent``/``apps`` in ``features/src/lib.rs``.
    """
    runner, _ = _runner(StableSurfaceTransport())
    argv = tuple(runner._argv())

    assert argv[1:4] == ("app-server", "--stdio", "--strict-config")
    for override in (
        "mcp_servers={}", "plugins={}", 'web_search="disabled"',
        "project_doc_max_bytes=0", "features.hooks=false",
        "features.memories=false", "features.multi_agent=false",
        "features.apps=false",
    ):
        assert override in argv
    # Nothing that would make the app-server bind a non-stdio transport or
    # inherit a host policy may creep into the launch.
    assert "--listen" not in argv and "--remote-control" not in argv


def test_stable_approval_request_without_available_decisions_still_declines(tmp_path):
    transport = StableSurfaceTransport()
    runner, _ = _runner(transport)

    result = runner.start("fix it", str(tmp_path))

    assert result.settled is True
    assert result.thread_id == "01a0c724-3482-7d60-b2a1-42fa7ec62835"
    assert transport.approval_answer == {"decision": "decline"}
    requested = next(event for event in result.events
                     if event.type == "approval.requested")
    assert requested.payload["kind"] == "command"
    assert "availableDecisions" not in requested.payload


def test_accept_for_session_is_not_filtered_when_the_server_omits_the_list(tmp_path):
    """An absent ``availableDecisions`` is "unrestricted", not "nothing allowed".

    ``acceptForSession`` is a stable variant of both
    ``CommandExecutionApprovalDecision`` and ``FileChangeApprovalDecision``
    (``app-server-protocol/schema/json/*RequestApprovalResponse.json``), so it
    stays answerable on the stable surface.
    """
    transport = StableSurfaceTransport()
    runner, _ = _runner(transport,
                        approval_callback=lambda kind, params: "acceptForSession")

    result = runner.start("fix it", str(tmp_path))

    assert result.settled is True
    assert transport.approval_answer == {"decision": "acceptForSession"}


def test_gate_always_allow_degrades_to_one_accept_on_the_stable_surface(tmp_path):
    """Collie cannot claim a session-scoped grant the server did not offer."""
    gate = Gate(cwd=tmp_path, mode=Mode.INTERACTIVE)
    callback = gate_approval_callback(
        gate, lambda tool, args, decision: Outcome.ALLOW_ALWAYS)

    answer = callback("command", {
        "itemId": "item_1", "startedAtMs": 1790047433000,
        "threadId": "thr", "turnId": "turn_1", "kind": "command",
        "command": "python -m pytest"})

    assert answer == "accept"


def test_write_stdin_approval_without_a_command_is_declined(tmp_path):
    """``kind: writeStdin`` sends a null ``command``.

    ``CommandExecutionApprovalKind`` gained ``writeStdin`` ("input sent to an
    existing terminal") and ``command`` is ``["string", "null"]``
    (``CommandExecutionRequestApprovalParams.json``).  Collie's Gate reasons
    about a command string, so an approval it cannot describe is declined
    rather than mapped onto an empty ``bash`` call.
    """
    callback = gate_approval_callback(Gate(cwd=tmp_path, mode=Mode.PROJECT))

    assert callback("command", {
        "itemId": "item_1", "startedAtMs": 1790047433000, "threadId": "thr",
        "turnId": "turn_1", "kind": "writeStdin", "command": None}) == "decline"


class CancelDecisionTransport(StableSurfaceTransport):
    """A peer that answers a ``cancel`` decision the way 0.155.1 does.

    ``CommandExecutionApprovalDecision::Cancel`` is documented as "User denied
    the command. The turn will also be immediately interrupted", and
    ``handle_turn_interrupted`` (``app-server/src/bespoke_event_handling.rs``)
    reports that interruption as ``turn/completed`` with
    ``TurnStatus::Interrupted`` -- there is no separate terminal notification,
    and ``TurnInterruptResponse`` itself has no required fields.
    """

    def send(self, message):
        message = dict(message)
        if message.get("id") == 91 and "result" in message:
            self.sent.append(message)
            self.approval_answer = message["result"]
            self.incoming.append({"method": "turn/completed", "params": {
                "threadId": "01a0c724-3482-7d60-b2a1-42fa7ec62835",
                "turn": {"id": "turn_1", "status": "interrupted", "items": []}}})
            return
        super().send(message)


def test_interrupted_turn_is_cancelled_without_demanding_recovery(tmp_path):
    transport = CancelDecisionTransport()
    runner, _ = _runner(transport,
                        approval_callback=lambda kind, params: "cancel")

    result = runner.start("fix it", str(tmp_path))

    assert transport.approval_answer == {"decision": "cancel"}
    assert result.cancelled is True
    assert result.settled is False
    assert result.exit_code == 0
    assert result.timed_out is False
    # Nothing in the workspace changed, so an interrupted turn is resumable.
    assert result.recovery_required is False


# --------------------------------------------------------------------------
# Official Python SDK sidecar
# --------------------------------------------------------------------------

class _TurnStatus(enum.Enum):
    """``generated/v2_all.py`` -> ``class TurnStatus(Enum)``: not a ``str``."""

    completed = "completed"
    failed = "failed"
    interrupted = "interrupted"
    in_progress = "inProgress"


class _Model:
    """Stands in for a pydantic ``BaseModel`` the way the SDK returns them."""

    def __init__(self, **fields):
        self._fields = fields
        for name, value in fields.items():
            setattr(self, name, value)

    def model_dump(self, mode="python", by_alias=False, exclude_none=False):
        return {key: value for key, value in self._fields.items()
                if not (exclude_none and value is None)}


class _FakeThread:
    def __init__(self, thread_id, *, fail):
        self.id = thread_id
        self._fail = fail
        self.run_calls = []

    def run(self, turn_input):
        self.run_calls.append(turn_input)
        if self._fail:
            # openai_codex/_run.py -> _raise_for_failed_turn: a failed turn is
            # raised out of Thread.run(), never returned.
            raise RuntimeError("model hit a policy stop")
        return _Model(
            id="turn_1", status=_TurnStatus.completed, error=None,
            final_response="done", items=[_Model(type="agentMessage", text="done")],
            usage=_Model(total={"inputTokens": 12, "cachedInputTokens": 2,
                                "outputTokens": 3, "reasoningOutputTokens": 0,
                                "totalTokens": 17}),
        )


def _fake_sdk(*, fail=False, approval_hook=True, thread_id="thr_0155"):
    """A stand-in for ``openai_codex`` with 0.155.1's construction contract."""
    module = types.ModuleType("openai_codex")
    seen: dict = {}

    class CodexConfig:
        def __init__(self, **fields):
            self.__dict__.update(fields)
            seen["config"] = dict(fields)

    class _Client:
        def __init__(self):
            # client.py -> CodexClient._default_approval_handler accepts.
            self._approval_handler = lambda method, params: {"decision": "accept"}

    class Codex:
        def __init__(self, config=None):
            self.config = config
            self._client = _Client()
            if not approval_hook:
                del self._client._approval_handler
            seen["codex"] = self

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def thread_start(self, **kwargs):
            seen["thread_start"] = dict(kwargs)
            return _FakeThread(thread_id, fail=fail)

        def thread_resume(self, resumed, **kwargs):
            seen["thread_resume"] = (resumed, dict(kwargs))
            return _FakeThread(resumed, fail=fail)

        def thread_fork(self, forked, **kwargs):
            seen["thread_fork"] = (forked, dict(kwargs))
            return _FakeThread(thread_id, fail=fail)

    class ApprovalMode(str, enum.Enum):
        deny_all = "deny_all"
        auto_review = "auto_review"

    class Sandbox(str, enum.Enum):
        read_only = "read-only"
        workspace_write = "workspace-write"
        full_access = "full-access"

    class TextInput:
        def __init__(self, text):
            self.text = text

    class ImageInput:
        def __init__(self, url):
            self.url = url

    class LocalImageInput:
        def __init__(self, path):
            self.path = path

    module.CodexConfig = CodexConfig
    module.Codex = Codex
    module.ApprovalMode = ApprovalMode
    module.Sandbox = Sandbox
    module.TextInput = TextInput
    module.ImageInput = ImageInput
    module.LocalImageInput = LocalImageInput
    return module, seen


def _run_worker(request, module, monkeypatch):
    monkeypatch.setitem(sys.modules, "openai_codex", module)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    code = codex_sdk_worker.main()
    records = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    return code, records


def test_sidecar_preserves_the_sdk_runtime_and_pins_isolation(tmp_path, monkeypatch):
    """The published SDK pins its bundled CLI; PATH must not override it."""
    module, seen = _fake_sdk()
    request = {"action": "run", "workspace": str(tmp_path), "model": "gpt-6-astra",
               "thread_id": "",
               "input": {"text": "hello"}}

    code, records = _run_worker(request, module, monkeypatch)

    assert code == 0
    assert seen["config"].get("codex_bin") is None
    overrides = seen["config"]["config_overrides"]
    assert "mcp_servers={}" in overrides and "features.hooks=false" in overrides
    assert seen["thread_start"]["approval_mode"].value == "deny_all"
    assert seen["thread_start"]["sandbox"].value == "workspace-write"
    assert seen["thread_start"]["model"] == "gpt-6-astra"
    assert records[-1]["type"] == "collie.sdk.result"
    assert records[-1]["status"] == "completed"
    assert records[-1]["thread_id"] == "thr_0155"


def test_sidecar_replaces_the_sdk_default_that_accepts_approvals(tmp_path, monkeypatch):
    module, seen = _fake_sdk()
    request = {"action": "run", "workspace": str(tmp_path),
               "input": {"text": "hello"}}

    code, _ = _run_worker(request, module, monkeypatch)

    assert code == 0
    handler = seen["codex"]._client._approval_handler
    for method in ("item/commandExecution/requestApproval",
                   "item/fileChange/requestApproval"):
        assert handler(method, {}) == {"decision": "decline"}


def test_sidecar_fails_closed_when_the_approval_hook_disappears(tmp_path, monkeypatch):
    module, _ = _fake_sdk(approval_hook=False)
    request = {"action": "run", "workspace": str(tmp_path),
               "input": {"text": "hello"}}

    code, records = _run_worker(request, module, monkeypatch)

    assert code == 125
    assert "approval" in records[-1]["error"]


def test_failed_turn_keeps_a_resumable_thread_id_and_the_real_error(tmp_path, monkeypatch):
    module, _ = _fake_sdk(fail=True)
    request = {"action": "run", "workspace": str(tmp_path),
               "input": {"text": "hello"}}

    code, records = _run_worker(request, module, monkeypatch)

    assert code == 1
    terminal = records[-1]
    assert terminal["type"] == "collie.sdk.result"
    assert terminal["status"] == "failed"
    assert terminal["thread_id"] == "thr_0155"
    assert "policy stop" in terminal["error"]
    assert [row["type"] for row in records].count("collie.sdk.result") == 1


def test_windows_forces_an_explicit_sandbox_level(monkeypatch):
    """Windows rewrites workspace-write to read-only without a sandbox level.

    ``config/src/config_toml.rs`` -> ``derive_permission_profile`` downgrades an
    explicitly requested ``SandboxMode::WorkspaceWrite`` to ``ReadOnly`` while
    ``windows_sandbox_level == Disabled``, and the level is ``Disabled`` unless
    ``[windows] sandbox`` is set (``core/src/config/mod.rs``).
    """
    monkeypatch.setattr(os, "name", "nt")
    windows = codex_sdk_worker._overrides()
    monkeypatch.setattr(os, "name", "posix")
    posix = codex_sdk_worker._overrides()

    assert 'windows.sandbox="unelevated"' in windows
    assert "windows.sandbox_private_desktop=false" in windows
    assert not [row for row in posix if row.startswith("windows.")]


class _FakeProcess:
    def __init__(self, stdout):
        self.stdout = stdout
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        kwargs["on_process"](object())
        return ProcessOutcome(stdout=self.stdout, exit_code=1)


def test_runner_reads_a_failed_sidecar_turn_as_resumable(tmp_path, monkeypatch):
    """A failed turn is a turn, not a protocol fault."""
    from harness import runner_env

    monkeypatch.setattr(runner_env, "assert_no_billing_override", lambda *a, **k: None)
    monkeypatch.setattr(runner_env, "child_env", lambda *a, **k: ({}, {
        "allowed": [], "stripped": []}))
    stdout = "\n".join([
        json.dumps({"type": "session.started", "thread_id": "thr_0155"}),
        json.dumps({"type": "turn.started", "thread_id": "thr_0155"}),
        json.dumps({"type": "turn.failed", "thread_id": "thr_0155",
                    "status": "failed", "error": "RuntimeError: policy stop"}),
        json.dumps({"type": "collie.sdk.result", "action": "run",
                    "thread_id": "thr_0155", "status": "failed",
                    "error": "RuntimeError: policy stop"}),
    ]) + "\n"
    process = _FakeProcess(stdout)
    runner = CodexSdkRunner(process_runner=process, snapshotter=_snapshot)

    result = runner.start(RunInput("hello"), str(tmp_path))

    request = json.loads(process.calls[0][1]["stdin_text"])
    assert "codex_bin" not in request
    assert result.settled is False
    assert result.thread_id == "thr_0155"
    assert "policy stop" in result.error
    assert result.recovery_required is False
    # The snapshot is resumable: a lost thread id would make the next turn
    # start a brand new conversation instead.
    runner._validate_snapshot(result)


def test_installed_sdk_uses_its_matching_bundled_runtime():
    from importlib import metadata
    import subprocess
    cli_bin = pytest.importorskip("codex_cli_bin")
    version = metadata.version("openai-codex")
    assert metadata.version("openai-codex-cli-bin") == version
    result = subprocess.run([str(cli_bin.bundled_codex_path()), "--version"],
                            capture_output=True, text=True, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "codex-cli " + version
