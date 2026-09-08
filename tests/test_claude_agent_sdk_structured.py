"""Provider-enforced {tool|answer} responses for the Claude Agent SDK lane.

Free-text envelopes sometimes came back with extra keys
(``{"answer": "...", "_note": "work complete"}``), which cost a whole
corrective turn after an otherwise correct patch.  Tool-bearing calls now ask
the provider to enforce Collie's envelope as a JSON schema.

The stream shapes pinned here are the ones actually observed for that mode: one
Assistant message whose only tool use is the synthetic ``StructuredOutput``
formatter, one matching successful tool receipt, one raw ``message_start``, and
a terminal success result whose ``structured_output`` equals the formatter
input exactly.  Everything else -- foreign tools, repeated or mismatched
formatter traffic, a second model response, forged results, an old worker that
ignores the mode -- must fail rather than be reported as a completed answer.
"""
import asyncio
import copy

import pytest

from harness import claude_agent_worker as sdk_worker
from harness.claude_agent_sdk import ClaudeAgentSdkProvider
from harness.claude_agent_worker import (_build_options, _query, _read_request,
                                         _response_schema)


TOOLS = [
    {"name": "read_file", "description": "read", "input_schema": {"type": "object"}},
    {"name": "write_file", "description": "write", "input_schema": {"type": "object"}},
    {"name": "grep", "description": "search", "input_schema": {"type": "object"}},
]
NAMES = ["read_file", "write_file", "grep"]
RECEIPT = "Structured output provided successfully"


class _Options:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Sdk:
    ClaudeAgentOptions = _Options


class _QuerySdk(_Sdk):
    def __init__(self, messages):
        self.messages = messages
        self.options = None
        self.prompt = None

    async def query(self, *, prompt, options):
        self.prompt, self.options = prompt, options
        for message in self.messages:
            yield message


def _init(**overrides):
    data = {"tools": ["StructuredOutput"], "skills": [], "plugins": [], "agents": {},
            "slash_commands": [], "mcp_servers": [], "model": "opus",
            "apiKeySource": "none"}
    data.update(overrides)
    return {"type": "system", "subtype": "init", "data": data}


def _stream(response, *, prose="I'll read the file first.", tool_id="toolu_1",
            message_id="msg_1", receipt_id=None, receipt=RECEIPT,
            is_error=None, structured_output=..., num_turns=2,
            result_error=False, starts=1, extra=()):
    """The observed structured stream, with one knob per adversarial variant.

    ``init`` attests ``tools=["StructuredOutput"]`` because that is the live
    shape: the formatter is the SDK's own synthetic tool, and every other agent
    surface is still required to be empty.
    """
    content = []
    if prose:
        content.append({"type": "text", "text": prose})
    content.append({"type": "tool_use", "id": tool_id,
                    "name": "StructuredOutput", "input": response})
    messages = [_init()]
    messages += [{"type": "stream_event", "event": {"type": "message_start",
                  "message": {"id": message_id, "model": "opus"}}}
                 for _ in range(starts)]
    messages.append({"type": "assistant", "id": message_id, "content": content})
    messages.append({"type": "user", "content": [
        {"type": "tool_result",
         "tool_use_id": tool_id if receipt_id is None else receipt_id,
         "content": receipt, "is_error": is_error}]})
    messages.append({
        "type": "result", "subtype": "success", "is_error": result_error,
        "num_turns": num_turns,
        "usage": {"input_tokens": 2, "output_tokens": 102},
        "structured_output": (copy.deepcopy(response) if structured_output is ...
                              else structured_output)})
    messages.extend(extra)
    return messages


def _run(messages, *, tools=NAMES, protocol=3):
    sdk = _QuerySdk(messages)
    request = {"protocol": protocol, "model": "opus", "system_prompt": "collie",
               "prompt": "next", "effort": "default",
               "response_format": "structured", "response_tools": list(tools)}
    return asyncio.run(_query(request, sdk)), sdk


# --------------------------------------------------------------------------- #
#  The observed happy paths, from the live read/write probes.
# --------------------------------------------------------------------------- #
def test_structured_tool_call_returns_only_the_canonical_envelope():
    result, sdk = _run(_stream({"response": {
        "tool": "read_file", "args": {"path": "cursor.py"}}}))

    assert result["ok"] is True
    assert result["text"] == '{"tool": "read_file", "args": {"path": "cursor.py"}}'
    assert result["response_format"] == "structured"
    assert result["response_tools"] == NAMES
    assert result["usage"]["output_tokens"] == 102
    assert result["api_key_source"] == "none"
    assert "I'll read the file" not in result["text"], "assistant prose must not travel"
    assert sdk.options.max_turns == 1, "structured mode keeps the one-turn limit"


def test_structured_write_call_preserves_exact_argument_bytes():
    content = 'def example():\n    return {"quote": "a\\b"}\n'
    result, _sdk = _run(_stream({"response": {
        "tool": "write_file", "args": {"path": "example.py", "content": content}}},
        prose=""))

    import json

    assert json.loads(result["text"]) == {
        "tool": "write_file", "args": {"path": "example.py", "content": content}}


def test_structured_answer_drops_the_extra_key_failure_mode():
    result, _sdk = _run(_stream({"response": {"answer": "the patch is applied"}}))

    assert result["text"] == '{"answer": "the patch is applied"}'


def test_structured_options_carry_the_nested_wrapper_schema_and_one_message_start():
    options = _build_options(_Sdk, {
        "model": "opus", "system_prompt": "s", "effort": "default",
        "response_format": "structured", "response_tools": NAMES})

    schema = options.output_format["schema"]
    assert options.output_format["type"] == "json_schema"
    assert schema == _response_schema(NAMES)
    assert "anyOf" not in schema, "a top-level anyOf is rejected with HTTP 400"
    assert schema["required"] == ["response"] and schema["additionalProperties"] is False
    variants = schema["properties"]["response"]["anyOf"]
    assert variants[0]["required"] == ["answer"]
    assert variants[1]["properties"]["tool"]["enum"] == NAMES
    assert variants[1]["properties"]["args"] == {"type": "object"}, (
        "tool argument validation stays owned by the host executor")
    assert all(variant["additionalProperties"] is False for variant in variants)
    assert options.include_partial_messages is True
    assert options.max_turns == 1
    assert options.env["MAX_STRUCTURED_OUTPUT_RETRIES"] == "0"


def test_planner_plain_mode_options_stay_byte_identical():
    plain = {"model": "opus", "system_prompt": "s", "effort": "default"}
    options = _build_options(_Sdk, plain)

    assert not hasattr(options, "output_format")
    assert not hasattr(options, "include_partial_messages")
    assert vars(options) == vars(_build_options(_Sdk, dict(plain)))
    assert options.tools == [] and options.max_turns == 1
    assert "MAX_STRUCTURED_OUTPUT_RETRIES" not in options.env


def test_plain_stream_is_unaffected_by_structured_validation():
    messages = [_init(tools=[]),
                {"type": "assistant", "id": "msg_1",
                 "content": [{"type": "text", "text": '{"answer":"ok"}'}]},
                {"type": "user", "content": [{"type": "text", "text": "echo"}]},
                {"type": "result", "is_error": False, "num_turns": 1, "usage": {}}]
    sdk = _QuerySdk(messages)

    result = asyncio.run(_query({"protocol": 1, "model": "opus",
                                 "system_prompt": "collie", "prompt": "p",
                                 "effort": "default"}, sdk))

    assert result == {"ok": True, "text": '{"answer":"ok"}', "api_key_source": "none",
                      "usage": {"input_tokens": 0, "output_tokens": 0,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0}}
    assert "response_format" not in result


# --------------------------------------------------------------------------- #
#  Adversarial stream shapes.  None of these may return ok.
# --------------------------------------------------------------------------- #
def _foreign_tool():
    messages = _stream({"response": {"answer": "ok"}})
    messages[2]["content"][-1]["name"] = "Bash"
    return messages


def _second_formatter_call():
    messages = _stream({"response": {"answer": "ok"}})
    messages[2]["content"].append({"type": "tool_use", "id": "toolu_2",
                                   "name": "StructuredOutput",
                                   "input": {"response": {"answer": "other"}}})
    return messages


def _second_assistant_turn():
    messages = _stream({"response": {"answer": "ok"}})
    messages.insert(4, {"type": "assistant", "id": "msg_2", "content": [
        {"type": "text", "text": "actually, one more thing"}]})
    return messages


def _no_formatter_call():
    """The old failure mode: prose only, with no schema-enforced formatter."""
    messages = _stream({"response": {"answer": "ok"}})
    messages[2]["content"] = [{"type": "text",
                               "text": '{"answer":"ok","_note":"work complete"}'}]
    del messages[3]
    return messages


def _missing_receipt():
    messages = _stream({"response": {"answer": "ok"}})
    del messages[3]
    return messages


def _tool_result_before_call():
    messages = _stream({"response": {"answer": "ok"}})
    messages[2], messages[3] = messages[3], messages[2]
    return messages


@pytest.mark.parametrize("messages,match", [
    (_foreign_tool(), "foreign tool use"),
    (_second_formatter_call(), "formatter more than once"),
    (_second_assistant_turn(), "more than one Assistant message id"),
    (_stream({"response": {"answer": "ok"}}, starts=2), "more than one model response"),
    (_stream({"response": {"answer": "ok"}}, starts=0),
     "exactly one model response"),
    (_no_formatter_call(), "did not invoke the structured formatter"),
    (_missing_receipt(), "exactly one structured formatter receipt"),
    (_tool_result_before_call(), "before the structured formatter call"),
    (_stream({"response": {"answer": "ok"}}, receipt_id="toolu_other"),
     "receipt did not match its call id"),
    (_stream({"response": {"answer": "ok"}}, receipt="Tool ran"),
     "receipt was not recognised"),
    (_stream({"response": {"answer": "ok"}}, num_turns=3), "one-turn limit"),
    (_stream({"response": {"answer": "ok"}}, result_error=True), "reported an error"),
    (_stream({"response": {"answer": "ok"}}, structured_output=None),
     "not a single response wrapper"),
    (_stream({"response": {"answer": "ok"}},
             structured_output={"response": {"answer": "something else"}}),
     "did not match the formatter input"),
    (_stream({"response": {"answer": "ok"}},
             extra=[{"type": "assistant", "id": "msg_1", "content": []}]),
     "content after the terminal result"),
    (_stream({"response": {"answer": "ok"}}, tool_id=""),
     "formatter call is missing its id"),
])
def test_structured_stream_fails_closed(messages, match):
    with pytest.raises(RuntimeError, match=match):
        _run(messages)


def test_duplicate_formatter_receipts_fail_closed():
    messages = _stream({"response": {"answer": "ok"}})
    messages.insert(4, copy.deepcopy(messages[3]))
    with pytest.raises(RuntimeError, match="more than one structured formatter receipt"):
        _run(messages)


def test_structured_mode_still_requires_empty_foreign_surfaces_and_valid_auth():
    surfaced = _stream({"response": {"answer": "ok"}})
    surfaced[0] = _init(skills=["deploy"])
    with pytest.raises(RuntimeError, match="non-empty skills"):
        _run(surfaced)

    forged_auth = _stream({"response": {"answer": "ok"}})
    forged_auth[0] = _init(apiKeySource="ANTHROPIC_API_KEY")
    with pytest.raises(RuntimeError, match="disallowed API key source"):
        _run(forged_auth)

    no_init = _stream({"response": {"answer": "ok"}})[1:]
    with pytest.raises(RuntimeError, match="before validated init"):
        _run(no_init)


@pytest.mark.parametrize("response,match", [
    ({"response": {"answer": "done", "_note": "work complete"}},
     "not exactly one tool call or answer"),
    ({"response": {"answer": "done"}, "_note": "x"}, "single response wrapper"),
    ({"answer": "done"}, "single response wrapper"),
    ({"response": {"tool": "rm_rf", "args": {}}}, "outside the allowlist"),
    ({"response": {"tool": "read_file", "args": "path"}}, "arguments are not an object"),
    ({"response": {"tool": "read_file"}}, "not exactly one tool call or answer"),
    ({"response": {"answer": {"text": "done"}}}, "answer is not a string"),
    ({"response": {"answer": "a", "tool": "grep", "args": {}}},
     "not exactly one tool call or answer"),
    ({"response": "done"}, "response is not an object"),
    ({"response": {}}, "not exactly one tool call or answer"),
    ({"response": {"tool": "read_file", "args": {"n": float("nan")}}},
     "not finite JSON"),
])
def test_malformed_or_extra_keyed_structured_output_is_refused(response, match):
    with pytest.raises(RuntimeError, match=match):
        _run(_stream(response))


# --------------------------------------------------------------------------- #
#  Protocol/capability attestation.
# --------------------------------------------------------------------------- #
def _read(request, monkeypatch):
    import io
    import json as _json

    raw = _json.dumps(request).encode("utf-8")
    monkeypatch.setattr(sdk_worker.sys, "stdin",
                        type("S", (), {"buffer": io.BytesIO(raw)})())
    return _read_request()


def test_worker_accepts_structured_protocols_and_rejects_unknown_ones(monkeypatch):
    base = {"model": "opus", "system_prompt": "s", "effort": "default",
            "response_format": "structured", "response_tools": NAMES}

    request = _read(dict(base, protocol=3, prompt="hi"), monkeypatch)
    assert request["protocol"] == 3 and request["response_tools"] == NAMES

    with pytest.raises(RuntimeError, match="invalid worker protocol"):
        _read(dict(base, protocol=9, prompt="hi"), monkeypatch)
    with pytest.raises(RuntimeError, match="invalid worker response format"):
        _read({"protocol": 3, "model": "opus", "system_prompt": "s", "prompt": "hi",
               "response_tools": NAMES}, monkeypatch)
    with pytest.raises(RuntimeError, match="must not carry a response format"):
        _read({"protocol": 1, "model": "opus", "system_prompt": "s", "prompt": "hi",
               "response_format": "structured", "response_tools": NAMES}, monkeypatch)


@pytest.mark.parametrize("tools,match", [
    ([], "missing its response tools"),
    ("read_file", "missing its response tools"),
    (["read_file", "read_file"], "repeats a response tool"),
    (["   "], "invalid response tool"),
    ([""], "invalid response tool"),
    ([None], "invalid response tool"),
])
def test_worker_refuses_an_invalid_response_tool_allowlist(tools, match, monkeypatch):
    with pytest.raises(RuntimeError, match=match):
        _read({"protocol": 3, "model": "opus", "system_prompt": "s", "prompt": "hi",
               "response_format": "structured", "response_tools": tools}, monkeypatch)


class _Provider(ClaudeAgentSdkProvider):
    def __init__(self, response, **kwargs):
        kwargs.setdefault("structured_output", True)
        super().__init__(**kwargs)
        self.response = response
        self.request = None
        self.spawned = 0

    def _run_worker(self, request, cancel_scope="", registration=None):
        self.spawned += 1
        self.request = request
        if callable(self.response):
            return self.response(request)
        return self.response


def _worker(text, **overrides):
    def build(request):
        data = {"ok": True, "text": text, "usage": {}, "api_key_source": "none",
                "response_format": request.get("response_format"),
                "response_tools": list(request.get("response_tools") or [])}
        if data["response_format"] is None:
            data.pop("response_format")
            data.pop("response_tools")
        data.update(overrides)
        return data
    return build


def test_opt_in_tool_bearing_calls_are_structured_end_to_end():
    provider = _Provider(_worker('{"tool": "grep", "args": {"pattern": "x"}}'))

    completion = provider.complete("SYS", [{"role": "user", "content": "fix"}], TOOLS)

    assert completion.stop_reason == "tool_use"
    assert completion.tool_calls[0].name == "grep"
    assert completion.tool_calls[0].args == {"pattern": "x"}
    assert completion.request_count == 1
    assert provider.request["protocol"] == 3
    assert provider.request["response_tools"] == NAMES
    assert 'Call the StructuredOutput formatter exactly once' in provider.request['prompt']
    assert 'Reply with EXACTLY ONE JSON object and nothing else' not in provider.request['prompt']
    assert provider.request['system_prompt'].startswith('SYS\n\n')


def test_structured_answer_reaches_the_caller_and_is_streamed_once():
    provider = _Provider(_worker('{"answer": "patched"}'))
    streamed = []

    completion = provider.complete("SYS", [{"role": "user", "content": "fix"}],
                                   TOOLS, on_text=streamed.append)

    assert completion.text == "patched" and completion.stop_reason == "end_turn"
    assert streamed == ["patched"]


def test_planner_call_without_tools_stays_plain_and_never_attests_structured():
    provider = _Provider(_worker('{"action":"code","args":{},"reason":"go"}'))

    completion = provider.complete("PLANNER", [{"role": "user", "content": "plan"}], [])

    assert completion.text == '{"action":"code","args":{},"reason":"go"}'
    assert provider.request["protocol"] == 1
    assert "response_format" not in provider.request
    assert "response_tools" not in provider.request


def test_explicit_opt_out_restores_the_legacy_plain_request():
    provider = _Provider(_worker('{"answer": "done"}'), structured_output=False)

    completion = provider.complete("SYS", [{"role": "user", "content": "fix"}], TOOLS)

    assert completion.text == "done"
    assert provider.request["protocol"] == 1
    assert "response_format" not in provider.request


def test_old_worker_that_ignores_structured_mode_fails_instead_of_answering():
    provider = _Provider({"ok": True, "text": '{"answer":"done","_note":"complete"}',
                          "usage": {}, "api_key_source": "none"})

    completion = provider.complete("SYS", [{"role": "user", "content": "fix"}], TOOLS)

    assert completion.stop_reason == "error"
    assert completion.error_code != "response_contract_error"
    assert "did not attest structured response mode" in completion.error_detail
    assert completion.request_count == 1


@pytest.mark.parametrize("overrides,match", [
    ({"response_format": "plain"}, "did not attest structured response mode"),
    ({"response_tools": ["grep"]}, "different response tool allowlist"),
    ({"response_tools": []}, "different response tool allowlist"),
    ({"api_key_source": "ANTHROPIC_API_KEY"}, "reviewed auth attestation"),
])
def test_parent_verifies_the_attestation_before_parsing(overrides, match):
    provider = _Provider(_worker('{"answer": "done"}', **overrides))

    completion = provider.complete("SYS", [{"role": "user", "content": "fix"}], TOOLS)

    assert completion.stop_reason == "error"
    assert match in completion.error_detail


def test_plain_call_refuses_a_worker_that_forges_a_structured_attestation():
    provider = _Provider({"ok": True, "text": '{"answer":"done"}', "usage": {},
                          "api_key_source": "none", "response_format": "structured",
                          "response_tools": []})

    completion = provider.complete("PLANNER", [{"role": "user", "content": "p"}], [])

    assert completion.stop_reason == "error"
    assert "unexpected response format" in completion.error_detail


def test_structured_request_rejects_a_tool_name_it_cannot_put_in_a_schema():
    provider = _Provider(_worker('{"answer": "done"}'))

    completion = provider.complete("SYS", [{"role": "user", "content": "fix"}],
                                   [{"name": None, "input_schema": {}}])

    assert completion.stop_reason == "error"
    assert provider.spawned == 0, "an unschemable allowlist must fail before inference"
    assert completion.request_count == 0, "nothing was physically requested"
    assert "response schema" in completion.error_detail


def test_formatter_enum_preserves_large_and_namespaced_host_tool_sets():
    # These names are JSON string values, not native SDK tools. The formatter
    # must not impose the native tool name/count limits on Collie's registry.
    names = ["extension_%d" % n for n in range(90)] + ["mcp__" + "source_" * 15 + "read", "read file"]
    provider = _Provider(_worker('{"answer": "done"}'))
    completion = provider.complete("s", [{"role": "user", "content": "u"}],
        [{"name": name, "description": "tool"} for name in names])
    assert completion.stop_reason == "end_turn"
    assert provider.request["response_tools"] == names
    result, _ = _run(_stream({"response": {"tool": names[-2], "args": {}}}), tools=names)
    assert result["response_tools"] == names


@pytest.mark.parametrize("tools", [[], {}, {"StructuredOutput": True}, "StructuredOutput"])
def test_structured_init_requires_exact_formatter_attestation(tools):
    messages = _stream({"response": {"answer": "ok"}})
    messages[0] = _init(tools=tools)
    with pytest.raises(RuntimeError, match="formatter surface"):
        _run(messages)


@pytest.mark.parametrize("flag", [0, 1, "false"])
def test_formatter_receipt_error_flag_is_not_coerced(flag):
    """Only the literal ``True`` flag is the formatter's refusal verdict.

    A refused response is a repairable response-contract miss
    (test_claude_agent_sdk_formatter_refusal.py).  A flag that is neither
    boolean nor absent classifies nothing, so it still fails closed here.
    """
    with pytest.raises(RuntimeError, match="failed tool result"):
        _run(_stream({"response": {"answer": "ok"}}, is_error=flag))


@pytest.mark.parametrize("args,altered", [({"value": True}, {"value": 1}),
                                       ({"value": 1}, {"value": 1.0})])
def test_equal_python_values_cannot_mask_changed_json_tool_arguments(args, altered):
    with pytest.raises(RuntimeError, match="did not match the formatter input"):
        _run(_stream({"response": {"tool": "grep", "args": args}},
                    structured_output={"response": {"tool": "grep", "args": altered}}))


def test_model_stream_identity_must_match_assistant_fragments():
    messages = _stream({"response": {"answer": "ok"}})
    messages[1]["event"]["message"]["id"] = "unrelated"
    with pytest.raises(RuntimeError, match="response id did not match"):
        _run(messages)
