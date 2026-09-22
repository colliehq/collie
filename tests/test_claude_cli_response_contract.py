"""The claude CLI transport's response-contract contract.

A tool-bearing turn's reply is an instruction for Collie's executor.  When the
CLI's own bounded re-prompt still comes back outside the ``{tool|answer}``
envelope, that is a response-contract miss -- the same event the SDK transport
reports -- and the host owns the one corrective turn.  Returning the unparsed
reply as an ordinary ``end_turn`` answer instead ended the run as a SUCCESS
whose answer was the model's prose, with nothing executed and nothing verified.

A tool-less caller (the loop's closing synthesis, Mission's planner) validates
its own schema, so its plain text keeps crossing unchanged.
"""
import pytest

from harness.cli import make_harness
from harness.providers import ClaudeCliProvider, Usage, classify_error

_PROSE = "I will start by reading the file to understand the bug."
_SCHEMAS = [{"name": "read_file", "description": "read a file",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}},
                              "required": ["path"]}}]


def _provider(replies):
    """A ClaudeCliProvider whose physical CLI call is scripted, never spawned."""
    provider = ClaudeCliProvider("opus")
    sent = []

    def _call(prompt, _system):
        sent.append(prompt)
        reply = replies[min(len(sent) - 1, len(replies) - 1)]
        return reply, Usage(input_tokens=3, output_tokens=1)

    provider._call = _call
    provider.sent = sent
    return provider


class _ProposingMemory:
    """Records durable-memory proposals so a false finish cannot hide."""

    def __init__(self):
        self.proposed = []

    def propose(self, text="", **_kw):
        self.proposed.append(text)
        return len(self.proposed)

    def promote(self, *_a, **_kw):
        pass

    def remember(self, text, keys=None, project=None):
        self.proposed.append(text)

    def set_block(self, *_a, **_kw):
        pass

    def close(self):
        pass


def _harness(tmp_path, provider, memory=None):
    harness = make_harness(str(tmp_path), provider="mock",
                           project="claude-cli-contract", embed="hash")
    harness.max_turns = 4
    harness.max_retries = 0
    harness.memory = memory if memory is not None else _ProposingMemory()
    harness.provider = provider
    return harness


def test_an_unparsed_tool_bearing_reply_is_a_repairable_contract_miss():
    provider = _provider([_PROSE, "Let me look at the file first."])

    completion = provider.complete(
        "system", [{"role": "user", "content": "fix the bug"}], _SCHEMAS)

    assert completion.stop_reason == "error" and completion.error_status == 422
    assert completion.error_code == "response_contract_error"
    assert completion.contract_reason == "no_json_object"
    assert completion.request_count == 2 and len(provider.sent) == 2
    # The host's retry policy must read this as one corrective turn, not a
    # transport outage and not a terminal fault.
    assert classify_error(completion.error_detail, completion.error_status,
                          completion.error_code) == "protocol"
    # Measured usage from both physically issued requests still travels.
    assert completion.usage.input_tokens == 6
    # Content-free: no fragment of either refused reply crosses the boundary.
    for field in (completion.text, completion.error_detail):
        assert "reading the file" not in field and "look at the file" not in field


@pytest.mark.parametrize("reply, reason", [
    ('{"tool":"read_file","args":{"path":"a.txt"},"why":"because"}', "extra_keys"),
    ('[{"tool":"read_file","args":{"path":"a.txt"}}]', "not_a_json_object"),
    ('{"answer": 17}', "answer_not_string"),
    ("", "empty_response"),
])
def test_the_miss_reports_the_structural_category_the_repair_needs(reply, reason):
    provider = _provider([reply])

    completion = provider.complete(
        "system", [{"role": "user", "content": "fix the bug"}], _SCHEMAS)

    assert completion.error_code == "response_contract_error"
    assert completion.contract_reason == reason


def test_a_tool_less_caller_still_receives_its_own_plain_text():
    """Mission's planner and the loop's closing synthesis own their own schema."""
    provider = _provider([_PROSE, "Nothing JSON here either."])

    completion = provider.complete(
        "system", [{"role": "user", "content": "plan the work"}], [])

    assert completion.stop_reason == "end_turn"
    assert completion.text == "Nothing JSON here either."
    assert completion.error_code == "" and completion.request_count == 2


def test_a_valid_envelope_is_untouched_by_the_refusal_path():
    provider = _provider([_PROSE, '{"tool":"read_file","args":{"path":"a.txt"}}'])

    completion = provider.complete(
        "system", [{"role": "user", "content": "fix the bug"}], _SCHEMAS)

    assert completion.stop_reason == "tool_use" and completion.request_count == 2
    assert [(call.name, call.args) for call in completion.tool_calls] == [
        ("read_file", {"path": "a.txt"})]


def test_prose_never_finishes_a_tool_bearing_run_as_its_answer(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    provider = _provider([_PROSE, "Let me look at the file first.",
                          "Still thinking about it.", "Almost there."])
    memory = _ProposingMemory()
    harness = _harness(tmp_path, provider, memory)
    events = []
    harness.emit = lambda kind, data: events.append((kind, data))

    result = harness.run("cli-contract", "Fix the bug in a.txt", consolidate=True)

    assert not result.answer and not result.success
    assert result.error.startswith("protocol:")
    assert "no_json_object" in result.error and "response-contract repair" in result.error
    # One host-owned corrective turn was spent, and it is visible as one.
    assert result.contract_repairs == 1
    assert [data["reason"] for kind, data in events if kind == "format_repair"] == [
        "no_json_object"]
    # Nothing ran, so nothing may be remembered or claimed as done.
    assert result.tool_calls == 0 and memory.proposed == []
    transcript = "".join(str(message) for message in result.messages)
    for fragment in ("reading the file", "look at the file", "Still thinking",
                     "Almost there", "previous response could not be parsed"):
        assert fragment not in transcript


def test_the_corrective_turn_lets_a_repaired_reply_carry_the_run(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    provider = _provider([_PROSE, "Let me look at the file first.",
                          '{"tool":"read_file","args":{"path":"a.txt"}}',
                          '{"answer":"a.txt contains hello."}'])
    harness = _harness(tmp_path, provider)

    result = harness.run("cli-repair", "Read a.txt", consolidate=False)

    assert result.answer == "a.txt contains hello." and not result.error
    assert result.contract_repairs == 1 and result.tool_calls == 1
    assert sum(message["role"] == "tool" for message in result.messages) == 1
    # The corrective request is a real, budget-visible provider call.
    assert len(provider.sent) == 4 and result.model_calls == 4
    assert "previous response could not be parsed" in provider.sent[2]
