"""The stock Claude Agent SDK route escalates its ONE corrective turn.

A real long run (27 turns, 26 tool calls) died on the free-text envelope: three
response-contract misses (``not_an_envelope``, ``extra_keys``,
``not_an_envelope``), and the third repair missed again with ``extra_keys``, so
the run ended ``HTTP 422 response_contract_error``.  The rejected replies were
never retained, so what the model actually wrote stays unknown; only the
structural categories above are evidence.

The provider-enforced schema transport (worker protocol 3/4) already existed and
was tested, but nothing constructed it: ``make_provider`` never passed
``structured_output``, so every stock tool call -- including the repair --
relied on free-text JSON.  Turning it on for every turn is NOT justified: the
recorded same-source comparison (bench/experiments/2026-09-08) ended cleanly
4/4 plain against 2/4 structured.  The corrective turn is the one call already
known to follow a refused envelope, and it is the only one escalated here.

Nothing in this file performs inference.  The single check that reads the real
optional dependency asserts a static option surface, not a model response.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import claude_agent_sdk as transport                      # noqa: E402
from harness import claude_agent_worker as sdk_worker                  # noqa: E402
from harness.claude_agent_sdk import ClaudeAgentSdkProvider            # noqa: E402
from harness.loop import FORMAT_REPAIR_NUDGE, format_repair_nudge      # noqa: E402
from harness.providers import Completion, Usage, make_provider         # noqa: E402

TOOLS = [
    {"name": "read_file", "description": "read", "input_schema": {"properties": {"path": {}}}},
    {"name": "write_file", "description": "write",
     "input_schema": {"properties": {"path": {}, "content": {}}}},
]
NAMES = ["read_file", "write_file"]
USAGE = {"input_tokens": 2, "output_tokens": 886,
         "cache_read_input_tokens": 1933, "cache_creation_input_tokens": 0}


def repair_turn(content=None):
    """The message loop.py itself appends for its one corrective request."""
    return {"role": "user",
            "content": content or format_repair_nudge("extra_keys", NAMES),
            "source": "harness", "kind": "format_repair"}


CONVERSATION = [{"role": "user", "content": "normalize the corpus"}]


class _Stock:
    """A stock-factory provider whose worker is replaced, and only its worker.

    Everything above ``_run_worker`` -- the factory's own arguments, the request
    gate, prompt construction, the attestation checks -- is the shipped path.
    """

    def __init__(self, result=None, **kwargs):
        self.provider = make_provider("claude-agent-sdk", model="claude-opus-5",
                                      **kwargs)
        self.requests = []
        self.reserved = []
        self.settled = []
        self.result = result if result is not None else answering('{"answer": "done"}')
        self.provider._run_worker = self._run_worker

    def _run_worker(self, request, cancel_scope="", registration=None):
        self.requests.append(request)
        data = self.result(request) if callable(self.result) else self.result
        if isinstance(data, dict) and data.get("structured_error"):
            # What the shipped transport does with a non-zero worker exit whose
            # payload is a formatter refusal, including its own validation.
            raise transport._formatter_refusal(data)
        return data

    def complete(self, messages, tools=TOOLS, system="COLLIE SYSTEM"):
        def reserve(kind):
            self.reserved.append(kind)
            return "req-%d" % len(self.reserved)

        with self.provider.request_authority(
                reserve, lambda request_id, status: self.settled.append((request_id, status))):
            return self.provider.complete(system, messages, tools)

    @property
    def request(self):
        return self.requests[-1]


def answering(text, **overrides):
    """A worker that answers and attests exactly the mode it was asked for."""
    def build(request):
        data = {"ok": True, "text": text, "usage": dict(USAGE), "api_key_source": "none"}
        if request.get("response_format"):
            data["response_format"] = request["response_format"]
            data["response_tools"] = list(request.get("response_tools") or [])
        data.update(overrides)
        return data
    return build


# --------------------------------------------------------------------------- #
#  The stock factory route: ordinary turns plain, the corrective turn schema-led.
# --------------------------------------------------------------------------- #
def test_the_stock_factory_still_sends_ordinary_tool_turns_as_plain_text():
    stock = _Stock()

    completion = stock.complete(CONVERSATION)

    assert completion.stop_reason == "end_turn" and completion.text == "done"
    assert stock.request["protocol"] == 1
    assert "response_format" not in stock.request
    assert "response_tools" not in stock.request
    assert "Reply with EXACTLY ONE JSON object" in stock.request["prompt"]


def test_the_stock_factorys_corrective_turn_is_provider_schema_enforced():
    stock = _Stock(answering('{"tool": "read_file", "args": {"path": "a.py"}}'))

    completion = stock.complete(CONVERSATION + [repair_turn()])

    assert completion.stop_reason == "tool_use"
    assert completion.tool_calls[0].name == "read_file"
    assert completion.tool_calls[0].args == {"path": "a.py"}
    # The request really is the tested structured transport, not a new one.
    assert stock.request["protocol"] == 3
    assert stock.request["response_format"] == "structured"
    assert stock.request["response_tools"] == NAMES
    assert "Call the StructuredOutput formatter exactly once" in stock.request["prompt"]
    assert "Reply with EXACTLY ONE JSON object" not in stock.request["prompt"]
    assert stock.request["system_prompt"].startswith("COLLIE SYSTEM\n\n")
    # The corrective turn itself still reaches the model.
    assert FORMAT_REPAIR_NUDGE.split(".")[0] in stock.request["prompt"]


def test_the_allowlist_is_the_calls_own_tools_and_nothing_else():
    stock = _Stock(answering('{"answer": "done"}'))

    stock.complete(CONVERSATION + [repair_turn()], tools=TOOLS[:1])

    assert stock.request["response_tools"] == ["read_file"]


def test_escalation_is_not_sticky_and_is_not_triggered_by_conversation_text():
    stock = _Stock()

    # An ordinary next turn, after a repair that succeeded.
    stock.complete(CONVERSATION + [repair_turn(),
                                   {"role": "assistant", "content": "ok"},
                                   {"role": "user", "content": "carry on"}])
    assert stock.request["protocol"] == 1

    # A user who pastes the nudge, or a message carrying only one marker, is
    # not the host's own corrective turn.
    for message in ({"role": "user", "content": FORMAT_REPAIR_NUDGE},
                    {"role": "user", "content": "x", "kind": "format_repair"},
                    {"role": "user", "content": "x", "source": "harness"},
                    {"role": "assistant", "content": "x", "source": "harness",
                     "kind": "format_repair"}):
        stock.complete(CONVERSATION + [message])
        assert stock.request["protocol"] == 1, message


def test_the_tool_less_planner_route_is_unchanged_even_on_a_repair_turn():
    """Mission's planner owns its own action contract; this envelope breaks it."""
    stock = _Stock(answering('{"action":"code","args":{},"reason":"go"}'))

    completion = stock.complete(CONVERSATION + [repair_turn()], tools=[], system="PLANNER")

    assert completion.text == '{"action":"code","args":{},"reason":"go"}'
    assert stock.request["protocol"] == 1
    assert "response_format" not in stock.request
    assert "response_tools" not in stock.request


def test_explicit_opt_out_keeps_every_turn_including_the_repair_in_plain_mode():
    stock = _Stock(structured_repair=False)

    stock.complete(CONVERSATION + [repair_turn()])

    assert stock.request["protocol"] == 1
    assert "response_format" not in stock.request


# --------------------------------------------------------------------------- #
#  Request accounting: the escalation is the already-budgeted corrective call.
# --------------------------------------------------------------------------- #
def test_the_escalated_turn_spends_exactly_one_request_and_no_extra_worker():
    stock = _Stock(answering('{"answer": "patched"}'))

    completion = stock.complete(CONVERSATION + [repair_turn()])

    assert completion.request_count == 1
    assert len(stock.requests) == 1
    assert stock.reserved == ["claude_agent_sdk"]
    assert stock.settled == [("req-1", "completed")]
    assert completion.usage.output_tokens == 886
    assert completion.usage.cache_read == 1933


def test_a_refused_repair_is_reported_as_the_same_422_and_is_never_hidden():
    """The provider's formatter can still refuse; that must stay visible."""
    stock = _Stock({"ok": False, "structured_error": "formatter_refused_response",
                    "error": "the structured formatter refused the model response",
                    "usage": dict(USAGE), "api_key_source": "none"})

    completion = stock.complete(CONVERSATION + [repair_turn()])

    assert completion.stop_reason == "error"
    assert completion.error_status == 422
    assert completion.error_code == "response_contract_error"
    assert completion.contract_reason == "provider_schema_refusal"
    assert completion.request_count == 1
    assert len(stock.requests) == 1, "a refusal must not silently re-ask the model"
    assert stock.settled == [("req-1", "completed")]


def test_a_worker_that_ignores_or_misreports_the_mode_fails_the_repair_turn():
    """No free-text answer may pass as schema-enforced on the escalated turn."""
    for overrides, match in (
            ({"response_format": None, "response_tools": None},
             "did not attest structured response mode"),
            ({"response_format": "plain"}, "did not attest structured response mode"),
            ({"response_tools": ["read_file"]}, "different response tool allowlist"),
            ({"api_key_source": "ANTHROPIC_API_KEY"}, "reviewed auth attestation")):
        result = answering('{"answer":"done","_note":"complete"}')

        def worker(request, _result=result, _overrides=overrides):
            data = _result(request)
            for key, value in _overrides.items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            return data

        stock = _Stock(worker)
        completion = stock.complete(CONVERSATION + [repair_turn()])

        assert completion.stop_reason == "error", overrides
        assert match in completion.error_detail, overrides
        # A hard attestation failure is not a contract miss: it must not consume
        # the host's contract-repair budget a second time.
        assert completion.error_code != "response_contract_error"
        assert "_note" not in (completion.text or "")


# --------------------------------------------------------------------------- #
#  The escalated request against the real worker and the real SDK option surface.
# --------------------------------------------------------------------------- #
def test_the_escalated_request_is_accepted_by_the_worker_and_configures_the_schema(monkeypatch):
    stock = _Stock()
    stock.complete(CONVERSATION + [repair_turn()])
    wire = stock.request

    monkeypatch.setattr(sdk_worker.sys, "stdin", type("_In", (), {
        "buffer": type("_B", (), {"read": staticmethod(
            lambda _n: json.dumps(wire).encode("utf-8"))})()})())
    request = sdk_worker._read_request()
    assert request["protocol"] == 3 and request["response_tools"] == NAMES

    class _Options:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    options = sdk_worker._build_options(type("_Sdk", (), {"ClaudeAgentOptions": _Options}),
                                        request)
    assert options.output_format["type"] == "json_schema"
    schema = options.output_format["schema"]["properties"]["response"]["anyOf"]
    assert [branch for branch in schema if "tool" in branch["properties"]
            ][0]["properties"]["tool"]["enum"] == NAMES
    # The host owns the one corrective turn: the CLI's own structured retry
    # stays off, so no unbudgeted repair request can be issued underneath it.
    assert options.env["MAX_STRUCTURED_OUTPUT_RETRIES"] == "0"
    assert options.include_partial_messages is True
    assert options.max_turns == 1 and options.allowed_tools == [] and options.tools == []


def test_the_installed_sdk_really_supports_this_mode():
    """What the escalation depends on, read from the pinned dependency itself."""
    import dataclasses

    sdk = pytest.importorskip("claude_agent_sdk")
    fields = {field.name for field in dataclasses.fields(sdk.ClaudeAgentOptions)}
    assert {"output_format", "include_partial_messages", "max_turns", "env"} <= fields


# --------------------------------------------------------------------------- #
#  The loop actually produces the turn this provider escalates.
# --------------------------------------------------------------------------- #
def _contract_error(reason):
    completion = Completion(
        text="ERROR(claude-agent-sdk): response contract error",
        stop_reason="error", error_status=422, error_code="response_contract_error",
        error_detail="response_contract_error: assistant response did not match "
                     "Collie's tool/answer contract (%s)" % reason,
        usage=Usage(input_tokens=2, output_tokens=886))
    completion.contract_reason = reason
    return completion


def _harness(tmp_path, script):
    from _util import _RecordingMemory, _ScriptProvider
    from harness.cli import make_harness
    harness = make_harness(str(tmp_path), provider="mock", project="structured-repair",
                           embed="hash")
    harness.max_turns = 3
    harness.max_retries = 0
    harness.max_contract_repairs = 1
    harness.memory = _RecordingMemory()
    harness.provider = _ScriptProvider(script)
    return harness


def test_the_loops_corrective_call_is_the_one_the_provider_escalates(tmp_path):
    seen = {}

    def corrected(messages):
        seen["messages"] = [dict(message) for message in messages]
        return Completion(text="done", stop_reason="end_turn")

    harness = _harness(tmp_path, [_contract_error("extra_keys"), corrected])
    result = harness.run("repair", "normalize the corpus", consolidate=False)

    assert not result.error and result.contract_repairs == 1
    provider = ClaudeAgentSdkProvider(model="claude-opus-5")
    # The loop's real corrective turn, handed to the real predicate.
    assert provider._repair_escalation(seen["messages"]) is True
    assert provider._structured_tools(TOOLS, seen["messages"]) == NAMES
    # Every earlier prefix of the same call stays plain.
    assert provider._repair_escalation(seen["messages"][:-1]) is False
    # The corrective turn is temporary: it never becomes durable history, so no
    # later turn can be escalated by a leftover marker.
    assert all(message.get("kind") != "format_repair" for message in result.messages)


def test_a_repair_that_misses_again_still_ends_the_run_honestly(tmp_path):
    harness = _harness(tmp_path, [_contract_error("not_an_envelope"),
                                  _contract_error("extra_keys")])

    result = harness.run("terminal", "normalize the corpus", consolidate=False)

    # Unchanged budget: one corrective request, then the honest 422 terminal.
    assert result.model_calls == 2 and result.contract_repairs == 1
    assert "gave up after 1 response-contract repair" in result.error
    assert "HTTP 422 response_contract_error" in result.error
    assert "[extra_keys]" in result.error
    assert not result.answer
