from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from harness import subscription_sidecar as sidecar


class FakeTransport:
    def __init__(self, text='{"answer":"done"}', usage=None):
        self.text = text
        self.usage = usage or {
            "input_tokens": 7,
            "output_tokens": 3,
            "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": 1,
        }
        self.calls = []
        self.cancelled = []

    def invoke(self, system, prompt, scope, cancel_event):
        self.calls.append((system, prompt, scope, cancel_event))
        return {"ok": True, "text": self.text, "usage": self.usage,
                "api_key_source": "none"}

    def cancel(self, scope):
        self.cancelled.append(scope)
        return True


def _body(*, stream=False, tools=True):
    value = {
        "model": sidecar.MODEL,
        "stream": stream,
        "messages": [
            {"role": "system", "content": "SYSTEM-SECRET-INSTRUCTION"},
            {"role": "user", "content": "inspect the workspace"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "old-call", "type": "function",
                "function": {"name": "read", "arguments": '{"path":"a.py"}'},
            }]},
            {"role": "tool", "tool_call_id": "old-call", "name": "read",
             "content": "historical tool result"},
            {"role": "user", "content": "continue"},
        ],
    }
    if tools:
        value["tools"] = [{
            "type": "function",
            "function": {
                "name": "read", "description": "Read a file",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string"}}, "required": ["path"]},
            },
        }]
    return value


def _receipts(directory: Path):
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))]


def _start_server(tmp_path, transport):
    server = sidecar.make_server("127.0.0.1", 0, tmp_path / "ledger",
                                 transport=transport)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _request(server, method, path, body=None, *, auth=True, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                            timeout=5)
    final_headers = dict(headers or {})
    if auth:
        final_headers["Authorization"] = "Bearer " + sidecar.BEARER_SENTINEL
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        final_headers["Content-Type"] = "application/json"
    connection.request(method, path, body=payload, headers=final_headers)
    response = connection.getresponse()
    data = response.read()
    headers_out = dict(response.getheaders())
    connection.close()
    return response.status, headers_out, data


def test_brand_neutral_serialization_preserves_caller_context_and_tools():
    system, prompt, tools = sidecar.serialize_turn(_body())

    assert system == "SYSTEM-SECRET-INSTRUCTION"
    assert tools == {"read"}
    assert "inspect the workspace" in prompt
    assert "historical tool result" in prompt
    assert '"tool_call_id":"old-call"' in prompt
    assert '"name":"read"' in prompt
    lowered = prompt.lower()
    assert "collie" not in lowered
    assert "claude code" not in lowered
    assert "pi" not in lowered


@pytest.mark.parametrize("text", [
    'prose {"answer":"x"}',
    '{"answer":"x","extra":true}',
    '{"tool":"missing","args":{}}',
    '{"tool":"read","args":[]}',
    '[{"answer":"x"}]',
])
def test_worker_output_contract_is_strict(text):
    with pytest.raises(RuntimeError, match="response contract"):
        sidecar._strict_result({"text": text, "usage": {},
                                "api_key_source": "none"}, {"read"})


def test_worker_output_requires_subscription_auth_attestation():
    with pytest.raises(RuntimeError, match="auth attestation"):
        sidecar._strict_result({"text": '{"answer":"x"}', "usage": {},
                                "api_key_source": "environment"}, set())


def test_worker_output_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(sidecar, "MAX_RESPONSE_BYTES", 16)
    with pytest.raises(RuntimeError, match="invalid assistant text"):
        sidecar._strict_result({"text": '{"answer":"too long"}', "usage": {},
                                "api_key_source": "none"}, set())


def test_service_returns_tool_call_and_writes_content_free_atomic_receipts(tmp_path):
    secret = "PROMPT-ONLY-SECRET-987"
    body = _body()
    body["messages"][-1]["content"] = secret
    raw = json.dumps(body).encode()
    transport = FakeTransport('{"tool":"read","args":{"path":"src/a.py"}}')
    ledger_dir = tmp_path / "receipts"
    service = sidecar.SidecarService(sidecar.ReceiptLedger(ledger_dir), transport)

    result = service.complete(body, raw, "req-one")

    assert result.kind == "tool"
    assert result.tool == "read"
    assert result.args == {"path": "src/a.py"}
    rows = _receipts(ledger_dir)
    assert [row["event"] for row in rows] == ["reserved", "settled"]
    assert rows[1]["outcome"] == "completed"
    assert rows[1]["usage"]["input_tokens"] == 7
    persisted = "\n".join(path.read_text(encoding="utf-8")
                           for path in ledger_dir.iterdir())
    assert secret not in persisted
    assert "SYSTEM-SECRET-INSTRUCTION" not in persisted
    assert "historical tool result" not in persisted
    assert not list(ledger_dir.glob("*.tmp"))
    assert len(rows[0]["request_sha256"]) == 64
    assert len(rows[0]["prompt_sha256"]) == 64


def test_service_timeout_is_safe_and_settled(tmp_path):
    class TimeoutTransport(FakeTransport):
        def invoke(self, *_args):
            raise TimeoutError("raw secret timeout detail")

    ledger_dir = tmp_path / "ledger"
    service = sidecar.SidecarService(sidecar.ReceiptLedger(ledger_dir),
                                     TimeoutTransport())
    body = _body()
    with pytest.raises(sidecar.SidecarError) as caught:
        service.complete(body, json.dumps(body).encode(), "timeout-one")

    assert caught.value.status == 504
    assert caught.value.code == "request_timeout"
    rows = _receipts(ledger_dir)
    assert rows[-1]["outcome"] == "timeout"
    assert rows[-1]["error_code"] == "request_timeout"
    assert "secret" not in json.dumps(rows)


def test_service_enforces_physical_request_budget_before_transport(tmp_path):
    ledger_dir = tmp_path / "ledger"
    transport = FakeTransport()
    service = sidecar.SidecarService(
        sidecar.ReceiptLedger(ledger_dir), transport, max_requests=2)
    body = _body()
    raw = json.dumps(body).encode()

    service.complete(body, raw, "budget-one")
    service.complete(body, raw, "budget-two")
    with pytest.raises(sidecar.SidecarError) as caught:
        service.complete(body, raw, "budget-three")
    with pytest.raises(sidecar.SidecarError):
        service.complete(body, raw, "budget-four")

    assert caught.value.status == 429
    assert caught.value.code == "request_budget_exhausted"
    assert len(transport.calls) == 2
    rows = _receipts(ledger_dir)
    assert [row["event"] for row in rows] == [
        "reserved", "settled", "reserved", "settled", "budget_exhausted"]
    assert rows[-1]["max_requests"] == 2
    assert rows[-1]["request_id"] == "budget-three"


def test_sdk_transport_uses_existing_owned_worker_with_frozen_route(monkeypatch):
    from harness import claude_agent_sdk

    observed = {}

    class FakeProvider:
        def __init__(self, **kwargs):
            observed["init"] = kwargs

        def _register_pending(self, scope):
            observed["registered"] = scope
            return ("invocation", {})

        def _worker_request(self, system, prompt):
            return {"model": sidecar.MODEL, "system_prompt": system,
                    "prompt": prompt}

        def _run_worker(self, request, cancel_scope="", registration=None):
            observed["run"] = (request, cancel_scope, registration)
            return {"ok": True, "text": '{"answer":"ok"}',
                    "usage": {}, "api_key_source": "none"}

        def _retire_pending(self, registration):
            observed["retired"] = registration

        def cancel_for(self, scope):
            return lambda: observed.setdefault("cancel", scope) is not None

    monkeypatch.setattr(claude_agent_sdk, "ClaudeAgentSdkProvider", FakeProvider)
    transport = sidecar.SdkTransport(timeout=17)
    result = transport.invoke("literal system", "serialized turn", "scope-1",
                              threading.Event())

    assert observed["init"] == {
        "model": sidecar.MODEL, "timeout": 17, "effort": "high",
        "subscription_only": True,
    }
    assert observed["run"][0] == {
        "model": sidecar.MODEL, "system_prompt": "literal system",
        "prompt": "serialized turn",
    }
    assert observed["run"][1] == "scope-1"
    assert observed["retired"] == ("invocation", {})
    assert result["api_key_source"] == "none"


def test_http_health_models_completion_and_sse_are_openai_compatible(tmp_path):
    transport = FakeTransport()
    server, thread = _start_server(tmp_path, transport)
    try:
        status, _headers, data = _request(server, "GET", "/health", auth=False)
        assert status == 200
        assert json.loads(data)["model"] == sidecar.MODEL

        status, _headers, data = _request(server, "GET", "/v1/models")
        assert status == 200
        assert json.loads(data)["data"][0]["id"] == sidecar.MODEL

        status, _headers, data = _request(
            server, "POST", "/v1/chat/completions", _body(),
            headers={"X-Request-ID": "http-answer"})
        assert status == 200
        completion = json.loads(data)
        assert completion["id"] == "http-answer"
        assert completion["choices"][0]["message"]["content"] == "done"
        assert completion["usage"] == {
            "prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
            "prompt_tokens_details": {"cached_tokens": 2},
        }

        stream_body = _body(stream=True)
        stream_body["stream_options"] = {"include_usage": True}
        status, headers, data = _request(
            server, "POST", "/v1/chat/completions", stream_body,
            headers={"X-Request-ID": "http-stream"})
        assert status == 200
        assert headers["Content-Type"].startswith("text/event-stream")
        assert data.endswith(b"data: [DONE]\n\n")
        events = [line[6:] for line in data.splitlines()
                  if line.startswith(b"data: {")]
        decoded = [json.loads(event) for event in events]
        assert decoded[0]["choices"][0]["delta"] == {"role": "assistant"}
        assert any(event.get("usage", {}).get("total_tokens") == 13
                   for event in decoded)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_http_tool_result_uses_openai_tool_calls_shape(tmp_path):
    transport = FakeTransport('{"tool":"read","args":{"path":"a.py"}}')
    server, thread = _start_server(tmp_path, transport)
    try:
        status, _headers, data = _request(
            server, "POST", "/v1/chat/completions", _body(),
            headers={"X-Request-ID": "http-tool"})
        assert status == 200
        choice = json.loads(data)["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["content"] is None
        function = choice["message"]["tool_calls"][0]["function"]
        assert function == {"name": "read", "arguments": '{"path":"a.py"}'}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_http_rejects_missing_sentinel_and_wrong_model_before_transport(tmp_path):
    transport = FakeTransport()
    server, thread = _start_server(tmp_path, transport)
    try:
        status, _headers, data = _request(
            server, "POST", "/v1/chat/completions", _body(), auth=False)
        assert status == 401
        assert json.loads(data)["error"]["code"] == "unauthorized"

        body = _body()
        body["model"] = "some-other-model"
        status, _headers, data = _request(
            server, "POST", "/v1/chat/completions", body)
        assert status == 400
        assert json.loads(data)["error"]["code"] == "model_mismatch"
        assert transport.calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_http_cancel_endpoint_fences_an_active_request(tmp_path):
    class BlockingTransport(FakeTransport):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()

        def invoke(self, system, prompt, scope, cancel_event):
            self.entered.set()
            assert cancel_event.wait(3)
            raise sidecar.RequestCancelled()

    transport = BlockingTransport()
    server, thread = _start_server(tmp_path, transport)
    result = {}

    def post():
        result["response"] = _request(
            server, "POST", "/v1/chat/completions", _body(),
            headers={"X-Request-ID": "cancel-me"})

    request_thread = threading.Thread(target=post)
    request_thread.start()
    try:
        assert transport.entered.wait(2)
        status, _headers, data = _request(server, "DELETE",
                                          "/v1/requests/cancel-me")
        assert status == 200
        assert json.loads(data) == {"id": "cancel-me", "cancelled": True}
        request_thread.join(3)
        assert not request_thread.is_alive()
        assert result["response"][0] == 409
        rows = _receipts(tmp_path / "ledger")
        assert rows[-1]["outcome"] == "cancelled"
        assert transport.cancelled == ["cancel-me"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_limits_and_loopback_binding_fail_closed(tmp_path):
    body = _body()
    body["messages"][1]["content"] = "x" * (sidecar.MAX_TEXT_BYTES + 1)
    with pytest.raises(sidecar.SidecarError) as caught:
        sidecar.serialize_turn(body)
    assert caught.value.status == 413

    ledger = sidecar.ReceiptLedger(tmp_path / "ledger")
    service = sidecar.SidecarService(ledger, FakeTransport())
    with pytest.raises(ValueError, match="loopback"):
        sidecar.SidecarServer(("0.0.0.0", 0), service)

    server = sidecar.SidecarServer(
        ("0.0.0.0", 0), service, allow_private_peers=True)
    server.server_close()

    with pytest.raises(ValueError, match="max_requests"):
        sidecar.SidecarService(ledger, FakeTransport(), max_requests=-1)
