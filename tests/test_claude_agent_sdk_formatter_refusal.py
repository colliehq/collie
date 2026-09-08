"""A structured response the provider's own formatter refused.

Observed live (2026-09-07, claude-opus-5, effort=high) on four recorded Collie
requests: the model calls ``StructuredOutput`` exactly once, but its input
misses the schema -- an extra top-level key beside ``response``, or a
``response`` wrapped in a second ``response`` -- and the SDK receipts a failed
tool result reading ``Output does not match required schema: ...``.

That is a response-contract miss, the same event plain mode reports when an
envelope cannot be bridged, and the SDK's next act is a hidden repair turn.  So
the worker stops the stream there and reports one stable, content-free
category; the transport turns it into the same 422 the free-text envelope miss
produces, and Harness may spend its one corrective turn.  The refused response,
the formatter's report, and any second model response must never survive.
"""
import asyncio
import json

import pytest

from harness import claude_agent_sdk as transport
from harness import claude_agent_worker as sdk_worker
from harness.claude_agent_sdk import (ClaudeAgentSdkProvider, _FORMATTER_REFUSED,
                                      _StructuredContractRejected, _formatter_refusal)
from harness.claude_agent_worker import _query
from harness.providers import Usage, classify_error


TOOLS = [{"name": "grep", "description": "search", "input_schema": {"type": "object"}},
         {"name": "glob", "description": "list", "input_schema": {"type": "object"}}]
NAMES = ["grep", "glob"]
# The observed refusals: a duplicated top-level key, and a doubled wrapper.
REFUSED_INPUT = {"args": {"pattern": "**/*"},
                 "response": {"tool": "glob", "args": {"pattern": "**/*"}}}
REFUSED_REPORT = "Output does not match required schema: root: must NOT have additional properties"
START_USAGE = {"input_tokens": 2, "output_tokens": 3, "cache_read_input_tokens": 20502,
               "cache_creation_input_tokens": 0}


class _Options:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _QuerySdk:
    """Yields a scripted stream and counts what the worker actually consumed."""

    ClaudeAgentOptions = _Options

    def __init__(self, messages):
        self.messages = messages
        self.consumed = 0

    async def query(self, *, prompt, options):
        for message in self.messages:
            self.consumed += 1
            yield message


def _init():
    return {"type": "system", "subtype": "init", "data": {
        "tools": ["StructuredOutput"], "skills": [], "plugins": [], "agents": {},
        "slash_commands": [], "mcp_servers": [], "model": "opus",
        "apiKeySource": "none"}}


def _stream(*, refused=True, tool_id="toolu_1", receipt_id=None, starts=1,
            usage=None, report=REFUSED_REPORT, tail=()):
    messages = [_init()]
    messages += [{"type": "stream_event", "event": {"type": "message_start", "message": {
        "id": "msg_1", "model": "opus",
        "usage": dict(START_USAGE if usage is None else usage)}}}
        for _ in range(starts)]
    messages.append({"type": "assistant", "id": "msg_1", "content": [
        {"type": "thinking", "thinking": "..."},
        {"type": "tool_use", "id": tool_id, "name": "StructuredOutput",
         "input": REFUSED_INPUT}]})
    messages.append({"type": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id if receipt_id is None else receipt_id,
         "content": report, "is_error": True if refused else None}]})
    messages.extend(tail)
    return messages


def _repair_tail():
    """What the SDK does next if the stream is left open: another model turn."""
    return [{"type": "stream_event", "event": {"type": "message_start", "message": {
                "id": "msg_2", "model": "opus", "usage": dict(START_USAGE)}}},
            {"type": "assistant", "id": "msg_2", "content": [
                {"type": "tool_use", "id": "toolu_2", "name": "StructuredOutput",
                 "input": {"response": {"answer": "second try"}}}]},
            {"type": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_2",
                                          "content": "Structured output provided successfully"}]},
            {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
             "usage": {"input_tokens": 2, "output_tokens": 257},
             "structured_output": {"response": {"answer": "second try"}}}]


def _run(messages, tools=NAMES):
    sdk = _QuerySdk(messages)
    request = {"protocol": 3, "model": "opus", "system_prompt": "collie",
               "prompt": "next", "effort": "high",
               "response_format": "structured", "response_tools": list(tools)}
    return asyncio.run(_query(request, sdk)), sdk


# --------------------------------------------------------------------------- #
#  The worker: classify the refusal, keep the measured usage, stop the stream.
# --------------------------------------------------------------------------- #
def test_refused_formatter_result_is_classified_not_a_generic_worker_failure():
    with pytest.raises(sdk_worker._StructuredContractRejected) as excinfo:
        _run(_stream())

    failure = excinfo.value
    assert failure.api_key_source == "none"
    # The request side is measured and preserved; the response side is only
    # revised into the stream after the repair turn has begun, so it stays 0.
    assert failure.usage == {"input_tokens": 2, "output_tokens": 0,
                             "cache_read_input_tokens": 20502,
                             "cache_creation_input_tokens": 0}
    text = json.dumps([str(failure), failure.usage])
    assert "**/*" not in text and "must NOT have" not in text


def test_the_refused_stream_is_never_read_into_a_second_model_response():
    messages = _stream(tail=_repair_tail())
    sdk = _QuerySdk(messages)
    request = {"protocol": 3, "model": "opus", "system_prompt": "collie",
               "prompt": "next", "effort": "high", "response_format": "structured",
               "response_tools": NAMES}
    with pytest.raises(sdk_worker._StructuredContractRejected):
        asyncio.run(_query(request, sdk))
    assert sdk.consumed == len(messages) - len(_repair_tail()), (
        "the hidden repair turn must never be consumed as this call's response")


def test_a_refusal_without_exactly_one_accounted_model_response_fails_closed():
    with pytest.raises(RuntimeError, match="more than one model response"):
        _run(_stream(starts=2))
    with pytest.raises(RuntimeError, match="exactly one model response"):
        _run(_stream(starts=0))


def test_an_unmatched_or_uncountable_refusal_stays_a_hard_failure():
    with pytest.raises(RuntimeError, match="receipt did not match its call id"):
        _run(_stream(receipt_id="toolu_other"))
    with pytest.raises(RuntimeError, match="receipt is missing its tool id"):
        _run(_stream(tool_id="toolu_1", receipt_id="   "))


@pytest.mark.parametrize("flag", [0, 1, "true", "false", [], None])
def test_only_a_boolean_error_flag_may_classify_a_refusal(flag):
    """A coerced flag is not a verdict: it must not become a repairable miss."""
    messages = _stream()
    messages[3]["content"][0]["is_error"] = flag
    with pytest.raises(RuntimeError) as excinfo:
        _run(messages)
    assert not isinstance(excinfo.value, sdk_worker._StructuredContractRejected)


def test_worker_reports_the_refusal_as_a_classified_nonzero_exit(monkeypatch, capsys):
    """main() turns the classification into the worker's stdout contract.

    The failure is injected at the request boundary so this stays a unit test of
    the payload and exit code, with no optional SDK import.
    """
    def refuse():
        raise sdk_worker._StructuredContractRejected(
            {"input_tokens": 2, "output_tokens": 0, "cache_read_input_tokens": 20502,
             "cache_creation_input_tokens": 0}, "none")

    monkeypatch.setattr(sdk_worker, "_arm_parent_death_from_argv", lambda: None)
    monkeypatch.setattr(sdk_worker, "_read_request", refuse)

    assert sdk_worker.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": False, "structured_error": _FORMATTER_REFUSED,
                       "error": "the structured formatter refused the model response",
                       "usage": {"input_tokens": 2, "output_tokens": 0,
                                 "cache_read_input_tokens": 20502,
                                 "cache_creation_input_tokens": 0},
                       "api_key_source": "none"}
    assert "text" not in payload


# --------------------------------------------------------------------------- #
#  The transport: recover the classification across the process boundary.
# --------------------------------------------------------------------------- #
def _payload(**overrides):
    data = {"ok": False, "structured_error": _FORMATTER_REFUSED,
            "error": "the structured formatter refused the model response",
            "usage": {"input_tokens": 2, "output_tokens": 0,
                      "cache_read_input_tokens": 20502,
                      "cache_creation_input_tokens": 0},
            "api_key_source": "none"}
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def test_formatter_refusal_recovers_the_category_and_usage():
    failure = _formatter_refusal(_payload())

    assert isinstance(failure, _StructuredContractRejected)
    assert failure.api_key_source == "none"
    assert failure.usage == Usage(input_tokens=2, output_tokens=0, cache_read=20502)


@pytest.mark.parametrize("payload,match", [
    (_payload(ok=True), "both success and a refused response"),
    (_payload(ok="no"), "invalid success flag"),
    (_payload(api_key_source="ANTHROPIC_API_KEY"), "reviewed auth attestation"),
    (_payload(api_key_source=None), "reviewed auth attestation"),
    (_payload(text='{"answer":"done"}'), "carried assistant content"),
    (_payload(tool_calls=[]), "carried assistant content"),
    (_payload(provider_error="rate_limit"), "carried assistant content"),
    (_payload(usage="none"), "invalid usage"),
    (_payload(usage={"input_tokens": -1}), "invalid input_tokens usage"),
])
def test_a_forged_refusal_payload_fails_closed(payload, match):
    with pytest.raises(RuntimeError, match=match):
        _formatter_refusal(payload)


@pytest.mark.parametrize("payload", [
    _payload(structured_error="something_else"),
    _payload(structured_error=None),
    {"ok": False, "error": "RuntimeError: SDK emitted more than one init message"},
])
def test_other_worker_failures_stay_on_the_generic_path(payload):
    assert _formatter_refusal(payload) is None


def _fake_worker(monkeypatch, stdout: bytes, returncode: int = 1):
    from harness import plat

    class Proc:
        pid = 4242

        def __init__(self, *args, **kwargs):
            self.returncode = None
            self._stdout_file = kwargs["stdout"]

        def communicate(self, input=None, timeout=None):
            self._stdout_file.write(stdout)
            self.returncode = returncode
            return None, None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(transport.subprocess, "Popen", lambda *a, **k: Proc(*a, **k))
    monkeypatch.setattr(plat, "new_group_kwargs", lambda: {})
    monkeypatch.setattr(plat, "no_window_kwargs", lambda: {})
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat, "attach_kill_on_close_job",
                        lambda child: type("Owner", (), {"close": lambda self: None})())
    monkeypatch.setattr(transport, "_terminate_worker_tree",
                        lambda proc, timeout_s=5.0: True)


def test_nonzero_worker_exit_carrying_a_refusal_is_not_a_generic_failure(monkeypatch):
    _fake_worker(monkeypatch, json.dumps(_payload()).encode("utf-8"))

    with pytest.raises(_StructuredContractRejected) as excinfo:
        ClaudeAgentSdkProvider()._run_worker({"protocol": 3, "prompt": "x"})

    assert excinfo.value.usage.cache_read == 20502


def test_a_zero_exit_success_payload_may_not_also_claim_a_refusal(monkeypatch):
    _fake_worker(monkeypatch, json.dumps(
        {"ok": True, "text": '{"answer":"done"}', "usage": {}, "api_key_source": "none",
         "response_format": "structured", "response_tools": NAMES,
         "structured_error": _FORMATTER_REFUSED}).encode("utf-8"), returncode=0)

    with pytest.raises(RuntimeError, match="both success and a refused response"):
        ClaudeAgentSdkProvider()._run_worker({"protocol": 3, "prompt": "x"})


# --------------------------------------------------------------------------- #
#  The completion: one accounted request, one repairable protocol error.
# --------------------------------------------------------------------------- #
class _RefusingProvider(ClaudeAgentSdkProvider):
    def __init__(self, usage=None, **kwargs):
        kwargs.setdefault("structured_output", True)
        super().__init__(**kwargs)
        self.usage = usage or Usage(input_tokens=2, output_tokens=0, cache_read=20502)
        self.spawned = 0

    def _run_worker(self, request, cancel_scope="", registration=None):
        self.spawned += 1
        raise _StructuredContractRejected(self.usage, "none")


def test_refusal_is_one_accounted_repairable_response_contract_error():
    provider = _RefusingProvider(subscription_only=True)
    events = []
    streamed = []
    provider.request_gate = lambda kind: events.append(("reserve", kind)) or "req-1"
    provider.request_complete = lambda request_id, status: events.append(
        ("complete", request_id, status))

    completion = provider.complete("SYS", [{"role": "user", "content": "fix"}],
                                   TOOLS, on_text=streamed.append)

    assert completion.stop_reason == "error"
    assert completion.tool_calls == []
    assert completion.error_code == "response_contract_error"
    assert completion.error_status == 422
    assert completion.request_count == 1, "exactly one model response was issued"
    assert completion.usage.input_tokens == 2 and completion.usage.cache_read == 20502
    assert completion.api_key_source == "none"
    assert classify_error(completion.error_detail, completion.error_status,
                          completion.error_code) == "protocol"
    assert streamed == [], "a refused response is never streamed"
    assert events == [("reserve", "claude_agent_sdk"), ("complete", "req-1", "completed")]
    assert provider._active_runs == {}


def test_a_refused_response_never_reaches_the_caller_as_text():
    provider = _RefusingProvider()

    completion = provider.complete("SYS", [{"role": "user", "content": "fix"}], TOOLS)

    assert completion.text == "ERROR(claude-agent-sdk): response contract error"
    for quoted in ("**/*", "must NOT have", "StructuredOutput"):
        assert quoted not in completion.text + completion.error_detail


def test_a_plain_call_can_never_be_answered_with_a_structured_refusal():
    """No schema was requested, so this classification cannot be trusted."""
    provider = _RefusingProvider()

    completion = provider.complete("PLANNER", [{"role": "user", "content": "plan"}], [])

    assert completion.stop_reason == "error"
    assert completion.error_code != "response_contract_error"
    assert classify_error(completion.error_detail, completion.error_status,
                          getattr(completion, "error_code", "")) != "protocol"
    assert completion.request_count == 1


def test_end_to_end_worker_exit_becomes_a_repairable_completion(monkeypatch):
    _fake_worker(monkeypatch, json.dumps(_payload()).encode("utf-8"))
    provider = ClaudeAgentSdkProvider(structured_output=True)

    completion = provider.complete("SYS", [{"role": "user", "content": "fix"}], TOOLS)

    assert completion.error_code == "response_contract_error"
    assert completion.request_count == 1
    assert completion.usage.cache_read == 20502
    assert classify_error(completion.error_detail, completion.error_status,
                          completion.error_code) == "protocol"
