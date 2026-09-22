from __future__ import annotations

from collections import deque
import os
import threading
import time

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


class WedgedGateway:
    """A gateway that acknowledges ``session.interrupt`` and keeps working anyway.

    Nothing here depends on timing: every frame is produced by an explicit
    ``send``, and ``receive`` blocks until one exists or until the transport is
    really shut down.  That is the shape Collie has to survive — a peer whose
    RPC write succeeds is not a peer that stopped.

    ``killed`` (``terminate``) and ``closed`` (``close``) are recorded
    separately on purpose: the ordinary end-of-operation teardown closes every
    transport, so only ``killed`` is evidence that cancellation escalated to the
    owned process tree.
    """

    def __init__(self):
        self.sent = []
        self.returncode = None
        self.stderr = ""
        self.interrupts = 0
        self.prompted = threading.Event()
        self.killed = threading.Event()
        self.closed = threading.Event()
        self._frames = deque([{"jsonrpc": "2.0", "method": "event",
                               "params": {"type": "gateway.ready", "payload": {}}}])
        self._lock = threading.Lock()

    def send(self, message):
        message = dict(message)
        method = message.get("method")
        with self._lock:
            self.sent.append(message)
            if method == "session.create":
                result = {"session_id": "live1234", "stored_session_id": "stored-parent"}
            elif method == "session.interrupt":
                # Accepted on the wire, then ignored: no message.complete follows.
                self.interrupts += 1
                result = {"status": "queued"}
            elif method == "prompt.submit":
                result = {"status": "streaming"}
            else:
                result = {"status": "ok"}
            self._frames.append({"jsonrpc": "2.0", "id": message.get("id"),
                                 "result": result})
        if method == "prompt.submit":
            self.prompted.set()

    def receive(self, timeout_s):
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            with self._lock:
                if self._frames:
                    return self._frames.popleft()
            if self.killed.is_set() or self.closed.is_set():
                raise EOFError("Hermes gateway closed its output")
            if time.monotonic() >= deadline:
                raise TimeoutError("Hermes gateway produced no frame")
            time.sleep(.005)

    def terminate(self, timeout_s=5.0):
        self.killed.set()
        self.returncode = -9
        return True

    def close(self):
        self.closed.set()
        return True


def test_ignored_native_interrupt_escalates_to_container_tree_extinction(
        monkeypatch, tmp_path):
    """A written session.interrupt is a request, never proof the turn stopped.

    ``runner_slice._CancelWatcher`` stops asking the moment ``cancel_current()``
    returns True, so confirming a cancel that only reached the gateway's inbox
    leaves the container running against the user's workspace with nobody left
    to kill it.
    """
    _env(monkeypatch)
    gateway = WedgedGateway()
    runner = HermesGatewayRunner(transport_factory=lambda *_a: gateway,
                                 snapshotter=_snap, default_timeout_s=5.0)
    outcome = {}

    def turn():
        try:
            outcome["snapshot"] = runner.start("do it", str(tmp_path))
        except BaseException as exc:      # reported by the assertions below
            outcome["error"] = exc

    worker = threading.Thread(target=turn, name="collie-hermes-wedged", daemon=True)
    worker.start()
    seen = {}
    try:
        assert gateway.prompted.wait(timeout=10), "the turn never submitted a prompt"
        began = time.monotonic()
        seen["confirmed"] = runner.cancel_current()
        seen["elapsed"] = time.monotonic() - began
        seen["killed"] = gateway.killed.is_set()
    finally:
        gateway.terminate()               # never leave the worker thread wedged
        worker.join(timeout=15)

    assert gateway.interrupts == 1, "the native interrupt must still be tried first"
    assert seen["killed"], (
        "cancel_current() returned without terminating a gateway that ignored "
        "session.interrupt; the owned container tree was left running")
    assert seen["confirmed"] is True
    assert seen["elapsed"] <= 5.0, "cancel_current() must answer within five seconds"
    assert not worker.is_alive() and "error" not in outcome
    snapshot = outcome["snapshot"]
    assert snapshot.cancelled and not snapshot.settled
    assert snapshot.error == "Hermes operation was cancelled"


def test_interrupt_that_really_stops_the_turn_is_not_escalated(monkeypatch, tmp_path):
    """The native path stays native: a gateway that honours abort is not killed."""
    _env(monkeypatch)

    class ObedientGateway(WedgedGateway):
        def send(self, message):
            super().send(message)
            if message.get("method") == "session.interrupt":
                with self._lock:
                    self._frames.append({
                        "jsonrpc": "2.0", "method": "event",
                        "params": {"type": "message.complete",
                                   "session_id": "live1234",
                                   "payload": {"text": "stopped",
                                               "status": "complete"}}})

    gateway = ObedientGateway()
    runner = HermesGatewayRunner(transport_factory=lambda *_a: gateway,
                                 snapshotter=_snap, default_timeout_s=5.0)
    outcome = {}

    def turn():
        try:
            outcome["snapshot"] = runner.start("do it", str(tmp_path))
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=turn, name="collie-hermes-obedient", daemon=True)
    worker.start()
    seen = {}
    try:
        assert gateway.prompted.wait(timeout=10), "the turn never submitted a prompt"
        seen["confirmed"] = runner.cancel_current()
        seen["killed"] = gateway.killed.is_set()
    finally:
        gateway.terminate()
        worker.join(timeout=15)

    assert seen["confirmed"] is True
    assert not seen["killed"], "a gateway that stopped on request was killed anyway"
    assert gateway.closed.is_set(), "the honoured turn still closes its transport"
    snapshot = outcome["snapshot"]
    assert snapshot.cancelled and not snapshot.settled
