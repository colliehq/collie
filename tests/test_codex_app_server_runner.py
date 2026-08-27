from __future__ import annotations

from collections import deque
import os

import pytest

from harness.agent_runners import RecoveryRequiredError, RunnerSnapshot
from harness.codex_app_server_runner import CodexAppServerRunner, gate_approval_callback
from harness.gate import Gate, Mode, Outcome


class ScriptedTransport:
    def __init__(self, *, approval=True, terminal="completed", thread_id="thr_123"):
        self.sent = []
        self.incoming = deque()
        self.approval = approval
        self.terminal = terminal
        self.thread_id = thread_id
        self.approval_answer = None
        self._returncode = 0

    @property
    def returncode(self):
        return self._returncode

    @property
    def stderr(self):
        return ""

    def send(self, message):
        message = dict(message)
        self.sent.append(message)
        method = message.get("method")
        if method == "initialize":
            self.incoming.append({"id": message["id"], "result": {
                "userAgent": "codex-test", "platformFamily": "windows",
                "platformOs": "windows"}})
        elif method in ("thread/start", "thread/resume"):
            self.incoming.append({"id": message["id"], "result": {
                "thread": {"id": self.thread_id}}})
            self.incoming.append({"method": "thread/started", "params": {
                "thread": {"id": self.thread_id}}})
        elif method == "turn/start":
            self.incoming.append({"id": message["id"], "result": {
                "turn": {"id": "turn_1", "status": "inProgress"}}})
            self.incoming.append({"method": "turn/started", "params": {
                "threadId": self.thread_id,
                "turn": {"id": "turn_1", "status": "inProgress"}}})
            if self.approval:
                self.incoming.append({
                    "id": 91,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": self.thread_id, "turnId": "turn_1",
                               "itemId": "item_1", "command": "python -m pytest",
                               "availableDecisions": ["accept", "acceptForSession",
                                                      "decline", "cancel"]},
                })
            else:
                self._finish()
        elif message.get("id") == 91 and "result" in message:
            self.approval_answer = message["result"]
            self._finish()

    def _finish(self):
        self.incoming.append({"method": "item/completed", "params": {
            "threadId": self.thread_id, "turnId": "turn_1",
            "item": {"id": "msg_1", "type": "agentMessage",
                     "text": "done", "phase": "final_answer"}}})
        turn = {"id": "turn_1", "status": self.terminal}
        if self.terminal == "failed":
            turn["error"] = {"message": "model failed"}
        self.incoming.append({"method": "turn/completed", "params": {
            "threadId": self.thread_id, "turn": turn}})

    def receive(self, timeout_s):
        if not self.incoming:
            raise TimeoutError("script is empty")
        return self.incoming.popleft()

    def terminate(self, timeout_s=5.0):
        return True

    def close(self):
        return True


def _snapshot(_root):
    return {"tree_digest": "same", "snapshot_complete": True}


def _runner(transport, **kwargs):
    captured = {}

    def factory(argv, cwd, env):
        captured.update(argv=tuple(argv), cwd=cwd, env=dict(env))
        return transport

    runner = CodexAppServerRunner(
        executable="codex", snapshotter=_snapshot,
        transport_factory=factory, **kwargs)
    return runner, captured


def test_stable_stdio_handshake_is_isolated_and_declines_by_default(tmp_path):
    transport = ScriptedTransport()
    runner, captured = _runner(transport)

    result = runner.start("fix it", str(tmp_path))

    assert result.settled is True
    assert result.thread_id == "thr_123"
    assert result.final_output == "done"
    assert transport.approval_answer == {"decision": "decline"}
    methods = [row.get("method") for row in transport.sent if row.get("method")]
    assert methods[:4] == ["initialize", "initialized", "thread/start", "turn/start"]
    argv = captured["argv"]
    assert argv[1:4] == ("app-server", "--stdio", "--strict-config")
    for override in (
        "mcp_servers={}", "plugins={}", 'web_search="disabled"',
        "project_doc_max_bytes=0", "features.hooks=false",
        "features.memories=false", "features.multi_agent=false",
        "features.apps=false",
    ):
        assert override in argv
    start = next(row for row in transport.sent if row.get("method") == "thread/start")
    assert start["params"]["sandbox"] == "workspace-write"
    assert start["params"]["approvalPolicy"] == "on-request"
    assert start["params"]["cwd"] == os.path.realpath(str(tmp_path))
    types = [event.type for event in result.events]
    assert "approval.requested" in types
    assert "approval.resolved" in types
    assert "turn/completed" in types


def test_approval_callback_can_accept_for_this_session(tmp_path):
    transport = ScriptedTransport()
    seen = []
    runner, _ = _runner(
        transport,
        approval_callback=lambda kind, params: (
            seen.append((kind, params["itemId"])) or "acceptForSession"),
    )

    result = runner.start("fix it", str(tmp_path))

    assert result.settled is True
    assert seen == [("command", "item_1")]
    assert transport.approval_answer == {"decision": "acceptForSession"}


def test_gate_adapter_accepts_enum_answer_and_never_pins_shell(tmp_path):
    gate = Gate(cwd=tmp_path, mode=Mode.INTERACTIVE)
    seen = []
    callback = gate_approval_callback(
        gate, lambda tool, args, decision: (
            seen.append((tool, args, decision)) or Outcome.ALLOW_ALWAYS))

    answer = callback("command", {
        "itemId": "item_1", "command": "python -m pytest",
        "availableDecisions": ["accept", "acceptForSession", "decline"],
    })

    assert answer == "accept"  # arbitrary shell commands never gain a standing rule
    assert seen[0][0] == "bash"
    assert seen[0][2].call_id == "codex-app-server:item_1"


def test_gate_adapter_declines_missing_command_or_unattended_prompt(tmp_path):
    interactive = gate_approval_callback(Gate(cwd=tmp_path, mode=Mode.INTERACTIVE))

    assert interactive("command", {"itemId": "one"}) == "decline"
    assert interactive("command", {
        "itemId": "two", "command": "python -m pytest"}) == "decline"
    assert interactive("unknown", {"itemId": "three"}) == "decline"


def test_gate_adapter_auto_accepts_project_scoped_work(tmp_path):
    callback = gate_approval_callback(Gate(cwd=tmp_path, mode=Mode.PROJECT))

    assert callback("command", {
        "itemId": "one", "command": "python -m pytest"}) == "accept"
    assert callback("file-change", {"itemId": "two"}) == "accept"


def test_resume_uses_persisted_thread_and_preserves_cursor(tmp_path):
    first_transport = ScriptedTransport(approval=False)
    runner, _ = _runner(first_transport)
    first = runner.start("first", str(tmp_path))

    second_transport = ScriptedTransport(approval=False)
    runner.transport_factory = lambda argv, cwd, env: second_transport
    second = runner.resume(first, "second")

    resume = next(row for row in second_transport.sent
                  if row.get("method") == "thread/resume")
    assert resume["params"]["threadId"] == first.thread_id
    assert second.cursor > first.cursor
    assert second.invocation == 2
    assert second.settled is True


def test_resume_refuses_a_recovery_required_snapshot(tmp_path):
    runner, _ = _runner(ScriptedTransport())
    snapshot = RunnerSnapshot(
        runner=runner.key, workspace=os.path.realpath(str(tmp_path)),
        thread_id="thr_123", recovery_required=True)
    with pytest.raises(RecoveryRequiredError):
        runner.resume(snapshot, "again")


def test_failed_terminal_is_not_settled(tmp_path):
    transport = ScriptedTransport(approval=False, terminal="failed")
    runner, _ = _runner(transport)

    result = runner.start("fail", str(tmp_path))

    assert result.settled is False
    assert result.error == "model failed"
    assert result.recovery_required is False
