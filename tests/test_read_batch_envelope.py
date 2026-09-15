"""Bounded READ-ONLY read batching for the Claude Agent SDK transport.

Observed coding runs spend one whole logical model request per ``read_file``
even when the next few paths are already known.  A request that opts in may
answer with one ``{"reads":[...]}`` envelope naming up to eight of them; the
adapter expands it into that many ORDINARY ``read_file`` tool calls with
distinct ids, so the existing loop keeps doing its own per-call validation,
permission, receipt and cancellation work.  Nothing here executes a read, and
nothing here claims parallel disk I/O -- the saving is model round trips only.

Everything below is the refusal surface that makes that safe: the envelope is
refused outright unless this exact request advertised ``read_file`` and enabled
the capability, one bad member rejects the whole response before anything runs,
and no batch may carry a write, a tool name, an answer, or an extra key.
"""
import json

import pytest

from harness.claude_agent_sdk import ClaudeAgentSdkProvider
from harness.claude_agent_worker import (_canonical_structured, _read_request,
                                         _response_schema)
from harness.providers import (Completion, _parse_response_envelope,
                               contract_miss_reason)


READ_TOOLS = [
    {"name": "read_file", "description": "read", "input_schema": {"type": "object"}},
    {"name": "write_file", "description": "write", "input_schema": {"type": "object"}},
]
NO_READ_TOOLS = [
    {"name": "bash", "description": "run", "input_schema": {"type": "object"}},
]
BATCH = '{"reads":[{"path":"a.py"},{"path":"b.py","offset":10,"limit":20}]}'


def _parse(text, tools=("read_file", "write_file"), read_batch=True):
    return _parse_response_envelope(text, allowed_tools=list(tools),
                                    read_batch=read_batch)


# --------------------------------------------------------------------------- #
# The envelope itself
def test_batch_becomes_distinct_ordinary_read_file_calls():
    kind, calls = _parse(BATCH)
    assert kind == "reads"
    assert [call.name for call in calls] == ["read_file", "read_file"]
    assert [call.args for call in calls] == [
        {"path": "a.py"}, {"path": "b.py", "offset": 10, "limit": 20}]
    # Distinct identities: a shared id would let one read's result be mistaken
    # for another's receipt.
    assert len({call.id for call in calls}) == 2
    assert all(call.id for call in calls)


def test_single_tool_and_answer_contracts_are_unchanged():
    assert _parse('{"answer":"done"}')[0] == "answer"
    kind, call = _parse('{"tool":"read_file","args":{"path":"a.py"}}')
    assert (kind, call.name, call.args) == ("tool", "read_file", {"path": "a.py"})
    # ...and they stay identical when the capability is off.
    assert _parse('{"answer":"done"}', read_batch=False)[0] == "answer"


def test_batch_is_refused_unless_this_request_enabled_it():
    """Legacy CLI providers must not accept a batch they never advertised."""
    assert _parse(BATCH, read_batch=False) is None
    assert contract_miss_reason(BATCH, allowed_tools=["read_file"]) == \
        "read_batch_not_allowed"
    # The default argument is the one every existing caller uses.
    assert _parse_response_envelope(BATCH, allowed_tools=["read_file"]) is None


def test_batch_is_refused_when_read_file_is_not_advertised():
    assert _parse(BATCH, tools=("bash",)) is None
    assert _parse(BATCH, tools=()) is None
    # ``allowed_tools=None`` is the tool-less planner shape: no allowlist exists,
    # so no batch may be admitted against it.
    assert _parse_response_envelope(BATCH, allowed_tools=None, read_batch=True) is None


@pytest.mark.parametrize("text", [
    '{"reads":[]}',                                   # empty
    '{"reads":%s}' % json.dumps([{"path": "f%d.py" % i} for i in range(9)]),
    '{"reads":{"path":"a.py"}}',                      # not a list
    '{"reads":["a.py"]}',                             # member is not an object
    '{"reads":[{}]}',                                 # member has no path
    '{"reads":[{"path":""}]}',                        # empty path
    '{"reads":[{"path":"   "}]}',                     # whitespace path
    '{"reads":[{"path":123}]}',                       # path is not a string
    '{"reads":[{"path":"a.py","offset":0}]}',         # non-positive bound
    '{"reads":[{"path":"a.py","limit":true}]}',       # bool is not a line count
    '{"reads":[{"path":"a.py","limit":1.5}]}',        # float is not a line count
    '{"reads":[{"path":"a.py","content":"x"}]}',      # a write argument
    '{"reads":[{"tool":"bash","args":{}}]}',          # a nested tool name
    '{"reads":[{"path":"a.py"},{"path":"b.py","offset":-1}]}',  # one bad member
])
def test_malformed_or_oversized_batches_are_refused_entirely(text):
    assert _parse(text) is None
    assert contract_miss_reason(text, allowed_tools=["read_file"],
                                read_batch=True) == "read_batch_invalid"


def test_exactly_eight_reads_is_the_boundary():
    eight = json.dumps({"reads": [{"path": "f%d.py" % i} for i in range(8)]})
    kind, calls = _parse(eight)
    assert kind == "reads" and len(calls) == 8


def test_no_partial_acceptance_of_a_mixed_batch():
    """One refused member rejects the response before anything is executed."""
    mixed = '{"reads":[{"path":"a.py"},{"path":"b.py","max_bytes":0}]}'
    assert _parse(mixed) is None


@pytest.mark.parametrize("text", [
    '{"reads":[{"path":"a.py"}],"answer":"done"}',
    '{"reads":[{"path":"a.py"}],"tool":"bash","args":{}}',
    '{"reads":[{"path":"a.py"}],"note":"extra"}',
])
def test_a_batch_may_not_be_mixed_with_an_answer_or_another_tool(text):
    assert _parse(text) is None
    assert contract_miss_reason(text, allowed_tools=["read_file", "bash"],
                                read_batch=True) == "extra_keys"


@pytest.mark.parametrize("text", [
    '{"reads":[{"path":"a.py","path":"b.py"}]}',   # duplicate key
    '{"reads":[{"path":"a.py","limit":NaN}]}',     # non-finite number
])
def test_batches_get_no_duplicate_key_or_nan_leniency(text):
    assert _parse(text) is None


def test_an_extra_envelope_beside_a_batch_is_refused():
    assert _parse(BATCH + "\n" + '{"answer":"done"}') is None
    assert _parse(BATCH + "\n" + BATCH) is None


def test_tool_name_arrays_are_not_a_batch():
    assert _parse('{"reads":["read_file","read_file"]}') is None
    assert _parse('[{"tool":"read_file","args":{"path":"a.py"}}]') is None


# --------------------------------------------------------------------------- #
# Per-request capability
def test_provider_read_batch_defaults_to_off_and_is_opt_in():
    assert ClaudeAgentSdkProvider(model="opus").read_batch is False
    provider = ClaudeAgentSdkProvider(model="opus", read_batch=True)
    assert provider.read_batch is True
    assert provider._read_batch_allowed(READ_TOOLS) is True


def test_capability_requires_read_file_on_this_actual_request():
    provider = ClaudeAgentSdkProvider(model="opus", read_batch=True)
    assert provider._read_batch_allowed(NO_READ_TOOLS) is False
    assert provider._read_batch_allowed([]) is False
    # Mission's tool-less planner never gains the envelope.
    assert provider._read_batch_allowed(None) is False


def test_disabled_provider_never_permits_the_envelope():
    provider = ClaudeAgentSdkProvider(model="opus")
    assert provider._read_batch_allowed(READ_TOOLS) is False


def test_prompt_describes_the_envelope_only_when_it_is_permitted():
    enabled = ClaudeAgentSdkProvider(model="opus", read_batch=True)
    disabled = ClaudeAgentSdkProvider(model="opus")
    messages = [{"role": "user", "content": "read a.py and b.py"}]
    described = enabled._payload(messages, READ_TOOLS, False,
                                 enabled._read_batch_allowed(READ_TOOLS))
    assert '"reads"' in described
    assert disabled._payload(messages, READ_TOOLS, False,
                             disabled._read_batch_allowed(READ_TOOLS)).count('"reads"') == 0
    # A request without read_file is byte-identical to the disabled build.
    assert enabled._payload(messages, NO_READ_TOOLS, False,
                            enabled._read_batch_allowed(NO_READ_TOOLS)) == \
        disabled._payload(messages, NO_READ_TOOLS, False, False)


def test_tool_less_planner_prompt_is_untouched():
    provider = ClaudeAgentSdkProvider(model="opus", read_batch=True)
    messages = [{"role": "user", "content": "plan"}]
    assert provider._payload(messages, [], False, False) == \
        ClaudeAgentSdkProvider(model="opus")._payload(messages, [], False, False)


# --------------------------------------------------------------------------- #
# Structured path: schema, worker request validation, attestation
def test_structured_schema_offers_reads_only_when_enabled():
    plain = _response_schema(["read_file", "write_file"])
    keys = [set(alt["properties"]) for alt in plain["properties"]["response"]["anyOf"]]
    assert {"reads"} not in keys

    batched = _response_schema(["read_file", "write_file"], True)
    alternatives = batched["properties"]["response"]["anyOf"]
    reads = [alt for alt in alternatives if set(alt["properties"]) == {"reads"}]
    assert len(reads) == 1
    array = reads[0]["properties"]["reads"]
    assert (array["minItems"], array["maxItems"]) == (1, 8)
    assert array["items"]["additionalProperties"] is False
    assert array["items"]["required"] == ["path"]
    assert set(array["items"]["properties"]) == {"path", "offset", "limit", "max_bytes"}


def test_worker_request_carries_the_flag_only_when_permitted():
    provider = ClaudeAgentSdkProvider(model="opus", read_batch=True)
    request = provider._worker_request("sys", "prompt", ["read_file"], True)
    assert request["response_read_batch"] is True
    assert request["protocol"] == 3
    assert "response_read_batch" not in provider._worker_request(
        "sys", "prompt", ["read_file"], False)
    # Plain mode never carries the structured flag at all.
    assert "response_read_batch" not in provider._worker_request("sys", "prompt")


def _worker_stdin(monkeypatch, request):
    import io
    import sys
    raw = json.dumps(request).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(raw)})())


def test_worker_rejects_an_unbacked_or_invalid_read_batch_flag(monkeypatch):
    base = {"protocol": 3, "model": "opus", "system_prompt": "s", "prompt": "p",
            "response_format": "structured", "response_tools": ["read_file"]}
    _worker_stdin(monkeypatch, dict(base, response_read_batch=True))
    assert _read_request()["response_read_batch"] is True

    # read_file withheld from the allowlist: the schema may not offer reads.
    _worker_stdin(monkeypatch, dict(base, response_tools=["bash"],
                                    response_read_batch=True))
    with pytest.raises(RuntimeError):
        _read_request()

    _worker_stdin(monkeypatch, dict(base, response_read_batch="yes"))
    with pytest.raises(RuntimeError):
        _read_request()

    # Plain protocol must not carry it.
    _worker_stdin(monkeypatch, {"protocol": 1, "model": "opus", "system_prompt": "s",
                                "prompt": "p", "response_read_batch": True})
    with pytest.raises(RuntimeError):
        _read_request()


def test_worker_canonicalizes_a_structured_batch():
    value = {"response": {"reads": [{"path": "a.py"},
                                    {"path": "b.py", "offset": 10, "limit": 20}]}}
    text = _canonical_structured(value, ["read_file"], True)
    assert json.loads(text) == value["response"]
    # And the host's own parser accepts exactly what the worker emitted.
    kind, calls = _parse(text)
    assert kind == "reads" and len(calls) == 2


@pytest.mark.parametrize("response,allowed,enabled", [
    ({"reads": [{"path": "a.py"}]}, ["read_file"], False),   # not enabled
    ({"reads": [{"path": "a.py"}]}, ["bash"], True),         # tool withheld
    ({"reads": []}, ["read_file"], True),                    # empty
    ({"reads": [{"path": "a.py", "content": "x"}]}, ["read_file"], True),
    ({"reads": [{"path": "a.py"}], "answer": "done"}, ["read_file"], True),
])
def test_worker_refuses_batches_it_was_not_given_permission_for(response, allowed, enabled):
    with pytest.raises(RuntimeError):
        _canonical_structured({"response": response}, allowed, enabled)


def test_worker_answer_and_tool_canonicalization_still_work():
    assert json.loads(_canonical_structured(
        {"response": {"answer": "done"}}, ["read_file"], True)) == {"answer": "done"}
    assert json.loads(_canonical_structured(
        {"response": {"tool": "read_file", "args": {"path": "a.py"}}},
        ["read_file"], True)) == {"tool": "read_file", "args": {"path": "a.py"}}


# --------------------------------------------------------------------------- #
# The actual request path: what provider.complete() sends, demands back, and
# does with the answer.  The checks above are on the pieces; these are on the
# one call that has to make all of them agree.
class _Provider(ClaudeAgentSdkProvider):
    """A provider whose worker is a function, so no process is spawned."""

    def __init__(self, worker, **kwargs):
        super().__init__(model="opus", **kwargs)
        self._worker = worker
        self.request = None

    def _run_worker(self, request, cancel_scope="", registration=None):
        self.request = request
        return self._worker(request)


def _reply(text, **overrides):
    """A well-formed worker reply that attests exactly what it was asked for."""
    def build(request):
        data = {"ok": True, "text": text, "usage": {}, "api_key_source": "none"}
        if request.get("response_format") is not None:
            data["response_format"] = request["response_format"]
            data["response_tools"] = list(request.get("response_tools") or [])
            if request.get("response_read_batch"):
                data["response_read_batch"] = True
        data.update(overrides)
        return data
    return build


def _complete(provider, tools=READ_TOOLS):
    return provider.complete("SYS", [{"role": "user", "content": "read a.py and b.py"}],
                             tools)


def test_structured_request_round_trips_a_batch_into_read_file_calls():
    provider = _Provider(_reply(BATCH), structured_output=True, read_batch=True)

    completion = _complete(provider)

    assert provider.request["response_read_batch"] is True
    assert completion.stop_reason == "tool_use"
    assert [c.name for c in completion.tool_calls] == ["read_file", "read_file"]
    assert [c.args for c in completion.tool_calls] == [
        {"path": "a.py"}, {"path": "b.py", "offset": 10, "limit": 20}]
    # One logical request produced them, and the loop still sees several calls.
    assert completion.request_count == 1


def test_plain_request_round_trips_a_batch_without_any_structured_flag():
    """The non-structured worker path is text-only; the flag is structured-only."""
    provider = _Provider(_reply(BATCH), read_batch=True)

    completion = _complete(provider)

    assert "response_read_batch" not in provider.request
    assert "response_format" not in provider.request
    assert '"reads"' in provider.request["prompt"]
    assert [c.name for c in completion.tool_calls] == ["read_file", "read_file"]


def test_a_worker_that_did_not_attest_the_batch_fails_closed():
    """An older worker enforced a schema without ``reads``; do not parse one."""
    provider = _Provider(lambda request: {
        "ok": True, "text": BATCH, "usage": {}, "api_key_source": "none",
        "response_format": request["response_format"],
        "response_tools": list(request["response_tools"])},
        structured_output=True, read_batch=True)

    completion = _complete(provider)

    # An attestation failure is NOT a contract miss: it is not the model's
    # mistake and must not be spent on the one corrective turn.
    assert completion.stop_reason == "error"
    assert completion.error_code != "response_contract_error"
    assert "did not attest read-batch response mode" in completion.error_detail
    assert completion.tool_calls == []


@pytest.mark.parametrize("kwargs,detail", [
    # Structured, batch not requested: the worker offered a mode this request
    # withheld.
    ({"structured_output": True}, "attested an unrequested read batch"),
    # Plain path, which never requests it at all.
    ({}, "attested an unexpected response format"),
])
def test_an_unrequested_batch_attestation_is_refused(kwargs, detail):
    provider = _Provider(_reply('{"answer":"done"}', response_read_batch=True),
                         **kwargs)

    completion = _complete(provider)

    assert completion.stop_reason == "error"
    assert detail in completion.error_detail
    assert not completion.text.startswith("done")


def test_feature_off_reports_the_batch_as_a_contract_miss():
    """Every existing caller: a batch is refused, and diagnosed as such."""
    provider = _Provider(_reply(BATCH), structured_output=True)

    completion = _complete(provider)

    assert "response_read_batch" not in provider.request
    assert completion.tool_calls == []
    assert completion.error_status == 422
    assert completion.contract_reason == "read_batch_not_allowed"


def test_enabled_provider_without_read_file_still_refuses_a_batch():
    provider = _Provider(_reply(BATCH), structured_output=True, read_batch=True)

    completion = _complete(provider, tools=NO_READ_TOOLS)

    assert "response_read_batch" not in provider.request
    assert completion.error_status == 422
    assert completion.contract_reason == "read_batch_not_allowed"
