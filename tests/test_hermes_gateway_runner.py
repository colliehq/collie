from __future__ import annotations

from collections import deque
import os

import pytest

from harness import runner_env, runner_specs
from harness.agent_runners import RunnerSnapshot
from harness.hermes_gateway_runner import HermesGatewayRunner


class FakeGateway:
    def __init__(self, *, approval=False):
        self.sent = []
        self.incoming = deque([{
            "jsonrpc": "2.0", "method": "event",
            "params": {"type": "gateway.ready", "payload": {}},
        }])
        self.returncode = 0
        self.stderr = ""
        self.approval = approval

    def send(self, message):
        message = dict(message)
        self.sent.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "session.create":
            result = {"session_id": "live1234", "stored_session_id": "stored-parent"}
        elif method == "session.resume":
            result = {"session_id": "live5678", "resumed": "stored-parent"}
        elif method == "prompt.submit":
            result = {"status": "streaming"}
            if self.approval:
                self.incoming.append({
                    "jsonrpc": "2.0", "method": "event",
                    "params": {"type": "approval.request",
                               "session_id": "live1234",
                               "payload": {"request_id": "approve1",
                                           "command": "dangerous"}},
                })
            else:
                self._complete("live1234" if any(
                    row.get("method") == "session.create" for row in self.sent)
                    else "live5678")
        elif method == "approval.respond":
            result = {"resolved": True}
            self._complete("live1234")
        elif method == "session.compress":
            result = {"status": "compressed", "usage": {"total": 5}}
        elif method == "session.branch":
            result = {"session_id": "childlive", "stored_session_id": "stored-child",
                      "parent": "stored-parent"}
        elif method in ("session.steer", "session.interrupt"):
            result = {"status": "queued"}
        elif method == "image.attach":
            result = {"attached": True}
        else:
            result = {"status": "ok"}
        self.incoming.appendleft({"jsonrpc": "2.0", "id": request_id,
                                  "result": result})

    def _complete(self, session_id):
        self.incoming.append({
            "jsonrpc": "2.0", "method": "event",
            "params": {"type": "message.complete", "session_id": session_id,
                       "payload": {"text": "Hermes finished", "status": "complete",
                                   "usage": {"input": 8, "output": 3}}},
        })

    def receive(self, timeout_s):
        if not self.incoming:
            raise TimeoutError("fake gateway is empty")
        return self.incoming.popleft()

    def terminate(self, timeout_s=5):
        return True

    def close(self):
        return True


def _snap(_root):
    return {"tree_digest": "same", "snapshot_complete": True}


def _env(monkeypatch):
    monkeypatch.setattr(runner_env, "assert_no_billing_override",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(runner_env, "child_env", lambda *_a, **_k:
                        ({"PATH": os.environ.get("PATH", "")},
                         {"allowed": ["PATH"], "stripped": []}))


def test_real_gateway_requires_known_container_engine():
    with pytest.raises(runner_specs.RunnerUnavailableError):
        HermesGatewayRunner()
    with pytest.raises(runner_specs.RunnerUnavailableError):
        HermesGatewayRunner(container_argv=("python", "-m", "tui_gateway.entry"))


def test_gateway_keeps_stored_identity_and_usage(monkeypatch, tmp_path):
    _env(monkeypatch)
    gateway = FakeGateway()
    runner = HermesGatewayRunner(transport_factory=lambda *_a: gateway,
                                 snapshotter=_snap)
    result = runner.start("do it", str(tmp_path))

    assert result.settled
    assert result.thread_id == "stored-parent"
    assert result.final_output == "Hermes finished"
    assert result.usage == {"input_tokens": 8, "output_tokens": 3}
    assert any(row.get("method") == "session.create" for row in gateway.sent)


def test_gateway_denies_approval_when_no_callback(monkeypatch, tmp_path):
    _env(monkeypatch)
    gateway = FakeGateway(approval=True)
    runner = HermesGatewayRunner(transport_factory=lambda *_a: gateway,
                                 snapshotter=_snap)
    result = runner.start("do it", str(tmp_path))

    response = next(row for row in gateway.sent
                    if row.get("method") == "approval.respond")
    assert response["params"] == {"session_id": "live1234", "choice": "deny"}
    assert result.settled


def test_gateway_resume_fork_and_compact_use_durable_parent(monkeypatch, tmp_path):
    _env(monkeypatch)
    prior = RunnerSnapshot(runner="hermes-gateway", workspace=str(tmp_path.resolve()),
                           thread_id="stored-parent", settled=True)
    gateways = []

    def factory(*_args):
        gateway = FakeGateway()
        gateways.append(gateway)
        return gateway

    runner = HermesGatewayRunner(transport_factory=factory, snapshotter=_snap)
    forked = runner.fork(prior)
    compacted = runner.compact(prior)

    assert forked.settled and forked.thread_id == "stored-child"
    assert compacted.settled and compacted.thread_id == "stored-parent"
    resume = next(row for row in gateways[0].sent
                  if row.get("method") == "session.resume")
    assert resume["params"]["session_id"] == "stored-parent"
    assert any(event.type == "context.compacted" for event in compacted.events)
