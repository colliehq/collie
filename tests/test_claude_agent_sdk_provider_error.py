"""Provider-rejection accounting for the Claude Agent SDK transport.

A request the provider refuses is a real, billed request.  These tests pin the
one narrow path that keeps its stable category and measured usage: everything
else about a rejection (auth attestation, one-turn contract, terminal result,
absence of rejected text) still fails closed.
"""
import asyncio
import json

import pytest

from harness import claude_agent_sdk as transport
from harness.claude_agent_sdk import (ClaudeAgentSdkProvider, _PROVIDER_ERROR_CATEGORIES,
                                      _ProviderRejected, _provider_rejection)
from harness.claude_agent_worker import _query
from harness.providers import Usage, classify_error, is_known_terminal


_REJECTED_PROSE = "Your credit balance is too low to run this request."


def _init(**overrides):
    data = {
        "tools": [], "skills": [], "plugins": [], "agents": {}, "slash_commands": [],
        "mcp_servers": [], "model": "opus", "apiKeySource": "none",
    }
    data.update(overrides)
    return {"type": "system", "subtype": "init", "data": data}


def _rejected_assistant(error="invalid_request", **extra):
    """The observed shape: an error category, provider prose, and no message id."""
    message = {"type": "assistant", "error": error,
               "content": [{"type": "text", "text": _REJECTED_PROSE}]}
    message.update(extra)
    return message


def _result(is_error=True, usage=None, **extra):
    message = {"type": "result", "subtype": "success", "is_error": is_error,
               "num_turns": 1,
               "usage": {"input_tokens": 1274, "output_tokens": 8} if usage is None
               else usage}
    message.update(extra)
    return message


class _QuerySdk:
    """Yields a scripted stream, then optionally raises like the real SDK does."""

    ClaudeAgentOptions = type("_Options", (), {
        "__init__": lambda self, **kwargs: self.__dict__.update(kwargs)})

    def __init__(self, messages, raises=None):
        self.messages = messages
        self.raises = raises

    async def query(self, *, prompt, options):
        for message in self.messages:
            yield message
        if self.raises is not None:
            raise self.raises


def _run(messages, raises=None):
    return asyncio.run(_query({
        "model": "opus", "system_prompt": "collie", "prompt": "next",
        "effort": "default",
    }, _QuerySdk(messages, raises)))


# --------------------------------------------------------------------------- #
#  Worker: the rejected stream itself
# --------------------------------------------------------------------------- #
def test_rejection_keeps_category_and_measured_usage_without_any_rejected_text():
    result = _run([_init(), _rejected_assistant(), _result()])

    assert result == {
        "ok": False, "provider_error": "invalid_request",
        "error": "provider rejected the request: invalid_request",
        "usage": {"input_tokens": 1274, "output_tokens": 8,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        "api_key_source": "none",
    }
    assert "text" not in result
    assert _REJECTED_PROSE not in json.dumps(result)


@pytest.mark.parametrize("category", sorted(_PROVIDER_ERROR_CATEGORIES))
def test_every_allowlisted_category_survives_the_known_post_result_raise(category):
    """The SDK re-raises the CLI's deliberate non-zero exit after the result."""
    result = _run([_init(), _rejected_assistant(category), _result()],
                  raises=Exception("Claude Code returned an error result: success"))

    assert result["provider_error"] == category
    assert result["usage"]["input_tokens"] == 1274


def test_unreviewed_error_categories_stay_fail_closed():
    # ``unknown`` is a real SDK literal but carries no classification, so it is
    # deliberately not admitted; nor is any value the SDK has not documented.
    for category in ("unknown", "brand_new_category", 7, {"code": "x"}):
        with pytest.raises(RuntimeError, match="assistant reported an error"):
            _run([_init(), _rejected_assistant(category), _result()])


def test_post_result_raise_is_never_absorbed_after_a_successful_result():
    messages = [_init(),
                {"type": "assistant", "id": "msg-1",
                 "content": [{"type": "text", "text": '{"answer":"ok"}'}]},
                _result(is_error=False)]

    with pytest.raises(Exception, match="returned an error result"):
        _run(messages, raises=Exception(
            "Claude Code returned an error result: success"))


def test_only_the_known_post_result_exception_is_absorbed():
    for raised in (RuntimeError("bundled runtime crashed"),
                   Exception("transport closed unexpectedly")):
        with pytest.raises(Exception, match="crashed|closed"):
            _run([_init(), _rejected_assistant(), _result()], raises=raised)


def test_rejection_still_requires_a_validated_native_subscription_init():
    with pytest.raises(RuntimeError, match="before validated init"):
        _run([_rejected_assistant(), _result()])

    with pytest.raises(RuntimeError, match="disallowed API key source"):
        _run([_init(apiKeySource="ANTHROPIC_API_KEY"), _rejected_assistant(),
              _result()])

    with pytest.raises(RuntimeError, match="non-empty tools"):
        _run([_init(tools=["Read"]), _rejected_assistant(), _result()])


def test_rejection_requires_a_terminal_result_message():
    with pytest.raises(RuntimeError, match="did not emit a result message"):
        _run([_init(), _rejected_assistant()])

    # Without a terminal result there is nothing to absorb: the SDK's own
    # exception propagates rather than becoming a classified rejection.
    with pytest.raises(Exception, match="returned an error result"):
        _run([_init(), _rejected_assistant()],
             raises=Exception("Claude Code returned an error result: success"))


def test_a_success_result_over_a_rejected_turn_is_forged_and_fails_closed():
    with pytest.raises(RuntimeError, match="success after a rejected turn"):
        _run([_init(), _rejected_assistant(), _result(is_error=False)])


def test_duplicate_result_and_extra_assistant_turns_fail_closed():
    with pytest.raises(RuntimeError, match="more than one result message"):
        _run([_init(), _rejected_assistant(), _result(), _result()])

    with pytest.raises(RuntimeError, match="assistant content after a rejected turn"):
        _run([_init(), _rejected_assistant(),
              {"type": "assistant", "id": "msg-2",
               "content": [{"type": "text", "text": '{"answer":"ok"}'}]},
              _result()])

    with pytest.raises(RuntimeError, match="assistant content after result"):
        _run([_init(), _rejected_assistant(), _result(),
              {"type": "assistant", "id": "msg-2", "content": []}])


def test_a_second_assistant_id_before_a_rejection_still_fails_closed():
    messages = [_init(),
                {"type": "assistant", "id": "msg-1",
                 "content": [{"type": "text", "text": "partial"}]},
                _rejected_assistant(id="msg-2"),
                _result()]

    with pytest.raises(RuntimeError, match="more than one Assistant message id"):
        _run(messages)


def test_answer_text_before_a_same_turn_rejection_is_never_returned():
    result = _run([_init(),
                   {"type": "assistant", "id": "msg-1",
                    "content": [{"type": "text", "text": '{"answer":"leaked"}'}]},
                   _rejected_assistant(id="msg-1"),
                   _result()])

    assert result["provider_error"] == "invalid_request"
    assert "leaked" not in json.dumps(result)


@pytest.mark.parametrize("usage", [
    {"input_tokens": -1}, {"input_tokens": 1.5}, {"output_tokens": True},
    {"input_tokens": float("inf")},
])
def test_malformed_rejection_usage_fails_closed(usage):
    with pytest.raises(RuntimeError, match="invalid .* usage"):
        _run([_init(), _rejected_assistant(), _result(usage=usage)])


def test_rejection_reports_zero_usage_when_the_provider_measured_none():
    result = _run([_init(), _rejected_assistant("rate_limit"), _result(usage={})])

    assert result["usage"] == {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


# --------------------------------------------------------------------------- #
#  Transport: re-validating the worker payload at the process boundary
# --------------------------------------------------------------------------- #
def _payload(**overrides):
    payload = {"ok": False, "provider_error": "rate_limit",
               "error": "provider rejected the request: rate_limit",
               "usage": {"input_tokens": 11, "output_tokens": 2,
                         "cache_read_input_tokens": 3,
                         "cache_creation_input_tokens": 4},
               "api_key_source": "none"}
    payload.update(overrides)
    return payload


def test_transport_accepts_a_validated_rejection_payload():
    rejection = _provider_rejection(_payload())

    assert isinstance(rejection, _ProviderRejected)
    assert rejection.category == "rate_limit"
    assert rejection.usage == Usage(input_tokens=11, output_tokens=2,
                                    cache_read=3, cache_creation=4)


@pytest.mark.parametrize("payload,match", [
    (_payload(ok=True), "both success and a provider error"),
    (_payload(api_key_source="ANTHROPIC_API_KEY"), "reviewed auth attestation"),
    (_payload(api_key_source=None), "reviewed auth attestation"),
    (_payload(text="answer text"), "carried assistant content"),
    (_payload(tool_calls=[{"name": "grep"}]), "carried assistant content"),
    (_payload(usage="lots"), "invalid usage"),
    (_payload(usage={"input_tokens": -3}), "invalid input_tokens usage"),
])
def test_transport_refuses_a_malformed_rejection_payload(payload, match):
    with pytest.raises(RuntimeError, match=match):
        _provider_rejection(payload)


@pytest.mark.parametrize("payload", [
    _payload(provider_error="unknown"), _payload(provider_error=""),
    _payload(provider_error=None), {"ok": False, "error": "worker crashed"},
])
def test_unclassified_worker_failures_stay_on_the_generic_path(payload):
    assert _provider_rejection(payload) is None


class _ExitingProvider(ClaudeAgentSdkProvider):
    """Drives the real _run_worker over a fake, non-zero-exit worker process."""

    def __init__(self, stdout: bytes, returncode: int = 1, **kwargs):
        super().__init__(**kwargs)
        self.stdout = stdout
        self.returncode = returncode


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

    monkeypatch.setattr(transport.subprocess, "Popen",
                        lambda *a, **k: Proc(*a, **k))
    monkeypatch.setattr(plat, "new_group_kwargs", lambda: {})
    monkeypatch.setattr(plat, "no_window_kwargs", lambda: {})
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat, "attach_kill_on_close_job",
                        lambda child: type("Owner", (), {"close": lambda self: None})())
    monkeypatch.setattr(transport, "_terminate_worker_tree",
                        lambda proc, timeout_s=5.0: True)


def test_nonzero_worker_exit_carrying_a_rejection_is_not_a_generic_failure(monkeypatch):
    _fake_worker(monkeypatch, json.dumps(_payload()).encode("utf-8"))
    provider = ClaudeAgentSdkProvider()

    with pytest.raises(_ProviderRejected) as excinfo:
        provider._run_worker({"protocol": 1, "prompt": "x"})

    assert excinfo.value.category == "rate_limit"
    assert excinfo.value.usage.input_tokens == 11


def test_nonzero_worker_exit_without_a_rejection_stays_generic(monkeypatch):
    _fake_worker(monkeypatch, json.dumps(
        {"ok": False, "error": "RuntimeError: SDK emitted more than one init "
                               "message"}).encode("utf-8"))
    provider = ClaudeAgentSdkProvider()

    with pytest.raises(RuntimeError, match="worker exited 1"):
        provider._run_worker({"protocol": 1, "prompt": "x"})


def test_a_zero_exit_success_payload_may_not_also_claim_a_provider_error(monkeypatch):
    _fake_worker(monkeypatch, json.dumps(
        {"ok": True, "text": '{"answer":"done"}', "usage": {},
         "api_key_source": "none", "provider_error": "rate_limit"}).encode("utf-8"),
        returncode=0)
    provider = ClaudeAgentSdkProvider()

    with pytest.raises(RuntimeError, match="both success and a provider error"):
        provider._run_worker({"protocol": 1, "prompt": "x"})


# --------------------------------------------------------------------------- #
#  Provider: the completion, its accounting, and its classification
# --------------------------------------------------------------------------- #
class _RejectingProvider(ClaudeAgentSdkProvider):
    def __init__(self, category, usage=None, **kwargs):
        super().__init__(**kwargs)
        self.category = category
        self.usage = usage or Usage(input_tokens=1274, output_tokens=8)
        self.spawned = 0

    def _run_worker(self, request, cancel_scope="", registration=None):
        self.spawned += 1
        raise _ProviderRejected(self.category, self.usage, "none")


def test_rejection_is_an_error_completion_with_usage_and_one_spent_request():
    provider = _RejectingProvider("invalid_request", subscription_only=True)
    events = []
    streamed = []
    provider.request_gate = lambda kind: events.append(("reserve", kind)) or "req-1"
    provider.request_complete = lambda request_id, status: events.append(
        ("complete", request_id, status))

    completion = provider.complete("COLLIE SYSTEM", [
        {"role": "user", "content": "fix it"}], [{
            "name": "edit_file", "description": "edit", "input_schema": {
                "type": "object", "properties": {"path": {"type": "string"}}}}],
        on_text=streamed.append)

    assert completion.stop_reason == "error"
    assert completion.tool_calls == []
    assert completion.error_code == "provider_invalid_request"
    assert completion.error_status == 400
    assert completion.request_count == 1
    assert completion.usage.input_tokens == 1274 and completion.usage.output_tokens == 8
    assert completion.api_key_source == "none"
    assert streamed == [], "a rejected request produced no assistant text to stream"
    assert events == [("reserve", "claude_agent_sdk"),
                      ("complete", "req-1", "error")]
    assert provider._active_runs == {}


def test_a_rejection_never_reads_as_a_response_contract_miss():
    provider = _RejectingProvider("invalid_request")

    completion = provider.complete("s", [{"role": "user", "content": "u"}], [
        {"name": "grep", "description": "search", "input_schema": {"type": "object"}}])

    assert completion.error_code != "response_contract_error"
    assert classify_error(completion.error_detail, completion.error_status,
                          completion.error_code) != "protocol"


@pytest.mark.parametrize("category,expected,terminal", [
    ("rate_limit", "retryable", False),
    ("server_error", "retryable", False),
    ("authentication_failed", "terminal", True),
    ("billing_error", "terminal", True),
    ("invalid_request", "terminal", False),
])
def test_host_retry_policy_can_still_distinguish_each_category(category, expected,
                                                               terminal):
    provider = _RejectingProvider(category)

    completion = provider.complete("s", [{"role": "user", "content": "u"}], [])

    assert classify_error(completion.error_detail, completion.error_status,
                          completion.error_code) == expected
    assert is_known_terminal(completion.error_detail) is terminal


def test_a_rejection_carries_no_provider_prose_into_the_completion():
    provider = _RejectingProvider("billing_error")

    completion = provider.complete("s", [{"role": "user", "content": "u"}], [])

    assert _REJECTED_PROSE not in completion.text
    assert completion.text == ("ERROR(claude-agent-sdk): provider rejected the "
                               "request: billing_error (billing)")


def test_a_rejection_before_reservation_authority_never_spawns_a_worker():
    provider = _RejectingProvider("rate_limit", subscription_only=True)

    completion = provider.complete("s", [{"role": "user", "content": "u"}], [])

    assert completion.request_count == 0
    assert completion.stop_reason == "error"
    assert provider.spawned == 0
