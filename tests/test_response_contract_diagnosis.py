"""Diagnose response refusals without retaining rejected assistant content.

A real plain-envelope run failed after three tools and its one format repair.
Rejected replies were not saved: neither their exact cause nor equivalence is
known. Test large valid arguments remain executable and refused shapes produce
content-free categories through the provider, repair nudge and terminal record.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import claude_agent_sdk as transport                    # noqa: E402
from harness.claude_agent_sdk import ClaudeAgentSdkProvider          # noqa: E402
from harness import loop, providers                               # noqa: E402
from harness.loop import FORMAT_REPAIR_NUDGE                        # noqa: E402
from harness.providers import Completion, Usage, _parse_response_envelope  # noqa: E402

# Keep collection possible on the pre-fix product: interface checks may fail there,
# while the provider/real-loop tests still exercise its existing error behavior.
CONTRACT_REPAIR_HINTS = getattr(loop, "CONTRACT_REPAIR_HINTS", {})
CONTRACT_MISS_REASONS = getattr(providers, "CONTRACT_MISS_REASONS", ())

def contract_miss_reason(*args, **kwargs):
    return providers.contract_miss_reason(*args, **kwargs)

def format_repair_nudge(*args, **kwargs):
    return loop.format_repair_nudge(*args, **kwargs)

TOOLS = [
    {"name": "read_file", "description": "read", "input_schema": {"properties": {"path": {}}}},
    {"name": "write_file", "description": "write",
     "input_schema": {"properties": {"path": {}, "content": {}}}},
    {"name": "execute_code", "description": "run", "input_schema": {"properties": {"code": {}}}},
]
NAMES = {tool["name"] for tool in TOOLS}

# The shape the live corpus actually had: one ~8000-character payload string
# that the model must copy verbatim into a tool argument.
PAYLOAD = ("BJ2WQPV47WU4HH-0000 " * 400)[:8000]
SECRET = "MODEL-PROSE-THAT-MUST-NEVER-BE-QUOTED"


class _PlainProvider(ClaudeAgentSdkProvider):
    """The shipped default (host-validated text envelope), with a mocked worker."""

    def __init__(self, text, **kwargs):
        super().__init__(**kwargs)
        self.text = text
        self.request = None

    def _run_worker(self, request, cancel_scope="", registration=None):
        self.request = request
        return {"ok": True, "text": self.text, "api_key_source": "none",
                "usage": {"input_tokens": 2, "output_tokens": 886,
                          "cache_read_input_tokens": 1933,
                          "cache_creation_input_tokens": 0}}


def _envelope():
    return {"tool": "write_file",
            "args": {"path": "out/rec_00.json",
                     "content": json.dumps({"id": "rec_00", "payload": PAYLOAD},
                                           ensure_ascii=False) + "\n"}}


# --------------------------------------------------------------- not a size fault
@pytest.mark.parametrize("wrap", [
    lambda body: body,
    lambda body: body + "\n",
    lambda body: "```json\n" + body + "\n```",
    lambda body: "Writing the first record now.\n" + body,
])
def test_a_valid_large_envelope_still_reaches_the_executor(wrap):
    """8000 characters of copied payload are not what the contract refuses."""
    provider = _PlainProvider(wrap(json.dumps(_envelope(), ensure_ascii=False)))

    completion = provider.complete("SYS", [{"role": "user", "content": "normalize"}], TOOLS)

    assert completion.stop_reason == "tool_use" and completion.error_code == ""
    assert completion.contract_reason == ""
    call = completion.tool_calls[0]
    assert call.name == "write_file"
    # Byte-identical arguments: nothing was truncated, re-encoded or repaired.
    assert json.loads(call.args["content"])["payload"] == PAYLOAD
    assert call.args == _envelope()["args"]


def test_a_long_escaped_script_argument_still_reaches_the_executor():
    code = "\n".join("d = json.load(open('source/rec_%02d.json'))  # \\d \"q\" {b}" % i
                     for i in range(40))
    provider = _PlainProvider(json.dumps({"tool": "execute_code", "args": {"code": code}}))

    completion = provider.complete("SYS", [{"role": "user", "content": "go"}], TOOLS)

    assert completion.tool_calls[0].args["code"] == code


# --------------------------------------------------------------- the categories
@pytest.mark.parametrize("reply,expected", [
    ("", "empty_response"),
    ("   \n ", "empty_response"),
    ("I will now copy each record into out/ one at a time.", "no_json_object"),
    # The classic hand-written-JSON failures for a long argument.
    ('{"tool": "execute_code", "args": {"code": "import json\nprint(1)"}}', "malformed_json"),
    ('{"tool": "execute_code", "args": {"code": "re.findall(\'\\d+\', s)"}}', "malformed_json"),
    ('{"tool": "write_file", "args": {"path": "out/rec_00.json", "content": "{\\"id\\"',
     "malformed_json"),
    ('{"tool": "read_file", "tool": "write_file", "args": {}}', "ambiguous_json"),
    ('[{"tool": "read_file", "args": {"path": "a"}},'
     ' {"tool": "read_file", "args": {"path": "b"}}]', "not_a_json_object"),
    ('{"tool": "read_file", "args": {"path": "a"}}\n{"answer": "done"}', "multiple_envelopes"),
    ('{"tool": "read_file", "args": {"path": "a"}, "why": "inspect it"}', "extra_keys"),
    ('{"answer": "done", "confidence": 0.9}', "extra_keys"),
    ('{"tool": "read_file", "args": "path=a"}', "args_not_object"),
    ('{"answer": {"text": "done"}}', "answer_not_string"),
    ('{"tool": "apply_patch", "args": {"patch": "x"}}', "unknown_tool"),
    ('{"action": "read", "path": "a"}', "not_an_envelope"),
])
def test_every_refusal_gets_its_own_stable_category(reply, expected):
    assert _parse_response_envelope(reply, allowed_tools=NAMES) is None, "must stay refused"
    reason = contract_miss_reason(reply, allowed_tools=NAMES)
    assert reason == expected
    assert reason in CONTRACT_MISS_REASONS


def test_a_category_never_carries_the_refused_text():
    reply = ('%s\n{"tool": "apply_patch", "args": {"secret": "%s"}}' % (SECRET, SECRET))
    reason = contract_miss_reason(reply, allowed_tools=NAMES)
    assert reason == "unknown_tool"
    assert SECRET not in reason and reason in CONTRACT_MISS_REASONS


def test_an_accepted_reply_is_never_given_a_miss_category():
    accepted = json.dumps(_envelope(), ensure_ascii=False)
    assert _parse_response_envelope(accepted, allowed_tools=NAMES) is not None
    assert contract_miss_reason(accepted, allowed_tools=NAMES) == ""


# --------------------------------------------------------------- provider boundary
def test_the_provider_reports_the_category_and_still_withholds_the_reply():
    reply = SECRET + '\n{"tool": "execute_code", "args": {"code": "print(1)\n"}}'
    provider = _PlainProvider(reply)

    completion = provider.complete("SYS", [{"role": "user", "content": "go"}], TOOLS)

    assert completion.stop_reason == "error" and completion.error_status == 422
    assert completion.error_code == "response_contract_error"
    assert completion.contract_reason == "malformed_json"
    assert "malformed_json" in completion.error_detail
    # The refused reply stays on the far side of the boundary, as before.
    assert SECRET not in completion.text and SECRET not in completion.error_detail
    assert SECRET not in json.dumps([completion.text, completion.error_detail])
    # One physically issued request, with its measured usage intact.
    assert completion.request_count == 1 and completion.usage.output_tokens == 886
    assert provider.request["protocol"] == 1 and "response_format" not in provider.request


def test_the_provider_tells_an_unknown_tool_apart_from_unparsable_json():
    provider = _PlainProvider('{"tool": "apply_patch", "args": {"path": "out/rec_00.json"}}')

    completion = provider.complete("SYS", [{"role": "user", "content": "go"}], TOOLS)

    assert completion.error_code == "response_contract_error"
    assert completion.contract_reason == "unknown_tool"


def test_a_provider_schema_refusal_keeps_its_own_category():
    class _Refusing(ClaudeAgentSdkProvider):
        def _run_worker(self, request, cancel_scope="", registration=None):
            raise transport._StructuredContractRejected(
                Usage(input_tokens=2), "none")

    completion = _Refusing(structured_output=True).complete(
        "SYS", [{"role": "user", "content": "go"}], TOOLS)

    assert completion.error_code == "response_contract_error"
    assert completion.contract_reason == "provider_schema_refusal"


# --------------------------------------------------------------- the repair turn
def test_the_nudge_is_sharpened_but_never_weakened():
    generic = format_repair_nudge("", [])
    assert generic == FORMAT_REPAIR_NUDGE
    # An unclassified miss must not invent a hint.
    assert format_repair_nudge("something_new", ["read_file"]) == FORMAT_REPAIR_NUDGE
    for reason in CONTRACT_MISS_REASONS:
        nudge = format_repair_nudge(reason, sorted(NAMES))
        assert nudge.startswith(FORMAT_REPAIR_NUDGE) and len(nudge) > len(FORMAT_REPAIR_NUDGE)
        assert CONTRACT_REPAIR_HINTS[reason] in nudge
    named = format_repair_nudge("unknown_tool", sorted(NAMES))
    # The host's own allowlist is host data, so naming it leaks nothing back.
    for name in NAMES:
        assert name in named


def _contract_error(reason, **usage):
    completion = Completion(
        text="ERROR(claude-agent-sdk): response contract error",
        stop_reason="error", error_status=422,
        error_code="response_contract_error",
        error_detail="response_contract_error: assistant response did not match "
                     "Collie's tool/answer contract (%s)" % reason,
        usage=Usage(input_tokens=2, output_tokens=886, **usage))
    completion.contract_reason = reason
    return completion


def _harness(tmp_path, script):
    from _util import _RecordingMemory, _ScriptProvider
    from harness.cli import make_harness
    harness = make_harness(str(tmp_path), provider="mock", project="contract-diagnosis",
                           embed="hash")
    harness.max_turns = 3
    harness.max_retries = 0
    harness.max_contract_repairs = 1
    harness.memory = _RecordingMemory()
    harness.provider = _ScriptProvider(script)
    return harness


def test_the_one_repair_turn_names_the_category(tmp_path):
    seen = {}

    def corrected(messages):
        seen["nudge"] = messages[-1]["content"]
        return Completion(text="done", stop_reason="end_turn")

    harness = _harness(tmp_path, [_contract_error("unknown_tool"), corrected])
    events = []
    harness.emit = lambda kind, data: events.append((kind, data))

    result = harness.run("repair", "normalize the corpus", consolidate=False)

    assert not result.error and result.contract_repairs == 1
    # Before: one generic sentence for every possible miss.  Now the model is
    # told which rule it broke and which tools actually exist.
    assert seen["nudge"].startswith(FORMAT_REPAIR_NUDGE)
    assert CONTRACT_REPAIR_HINTS["unknown_tool"] in seen["nudge"]
    assert "read_file" in seen["nudge"] and "write_file" in seen["nudge"]
    repair = [data for kind, data in events if kind == "format_repair"]
    assert [data["reason"] for data in repair] == ["unknown_tool"]
    # The corrective turn is still temporary and still content-free.
    assert all(CONTRACT_REPAIR_HINTS["unknown_tool"] not in str(message)
               for message in result.messages)
    row = harness.recorder.db.execute(
        "SELECT detail FROM turns WHERE run_id=? AND kind='format_repair'",
        (result.run_id,)).fetchone()
    assert "[unknown_tool]" in row["detail"]


def test_a_run_that_dies_on_the_contract_records_which_rule_it_broke(tmp_path):
    harness = _harness(tmp_path, [_contract_error("malformed_json"),
                                  _contract_error("malformed_json")])

    result = harness.run("terminal", "normalize the corpus", consolidate=False)

    assert result.model_calls == 2 and result.contract_repairs == 1
    assert result.error.startswith("protocol:")
    # The fact this investigation needed and did not have.
    assert "[malformed_json]" in result.error
    assert "HTTP 422 response_contract_error" in result.error
    assert "gave up after 1 response-contract repair" in result.error
    # The run was in the shipped plain-envelope mode; it never used structured mode.
    assert "structured-response" not in result.error
    assert not any("response_contract_error" in item for item in harness.memory.remembered)


def test_an_unclassified_contract_miss_still_fails_closed(tmp_path):
    harness = _harness(tmp_path, [_contract_error(""), _contract_error("")])

    result = harness.run("unclassified", "normalize the corpus", consolidate=False)

    assert result.model_calls == 2 and result.contract_repairs == 1
    assert result.error.startswith("protocol:") and result.error.endswith(
        "response_contract_error")
    assert not result.answer


@pytest.mark.parametrize("reason", ["PRIVATE-REJECTED-ANSWER", {"raw": "PRIVATE-REJECTED-ANSWER"}])
def test_unknown_provider_category_never_enters_durable_outputs(tmp_path, reason):
    h = _harness(tmp_path, [_contract_error(reason), _contract_error(reason)])
    events = []
    h.emit = lambda kind, data: events.append((kind, data))
    result = h.run("boundary", "diagnose", consolidate=False)
    assert result.error.startswith("protocol:")
    assert result.model_calls == 2 and result.contract_repairs == 1
    rows = h.recorder.db.execute("SELECT detail FROM turns WHERE run_id=?", (result.run_id,)).fetchall()
    assert "PRIVATE-REJECTED-ANSWER" not in str(events) + result.error + str([r["detail"] for r in rows])


def test_single_accepted_envelope_has_no_miss_category():
    text = '{"not":"envelope"}\n{"answer":"accepted"}\n{"tool":"missing","args":{}}'
    assert _parse_response_envelope(text, allowed_tools={"read_file"}) is not None
    assert contract_miss_reason(text, allowed_tools={"read_file"}) == ""


def test_completion_preserves_positional_request_accounting():
    completion = Completion("error", [], Usage(), "error", 422, "", "response_contract_error", 0, [], 123)
    assert completion.request_count == 0
    assert completion.thinking_blocks == [] and completion.retry_at == 123
    assert getattr(completion, "contract_reason", "") == ""
