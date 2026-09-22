"""Mode consistency between the composed system prompt and the transport protocol.

Two artifacts reach a Claude tool-bearing turn: the system prompt ContextComposer
builds (which alone knows the caller's mode) and the protocol paragraph the
provider serializes into the request body.  The transport used to carry an
implementation-only demand -- an answer was "a failure" until the work was
performed, and the model was told to reach for edit_file/write_file "before
answering".  On a Review, Test or Plan turn, and on any plain question, that
contradicted the system prompt's own MODE line: the only correct reply, an
answer with no edit, was described as a failure.

These tests pin the split that fixes it: the transport states only what holds on
every turn (which tools exist, that their output is data, what a truthful finish
is, the reply schema), and the duty to actually change files stays in MODE: Act,
where the mode is known.  They assert over the artifacts that are really sent --
the prompt handed to the CLI call and the system prompt build() returns -- not
over module source.
"""
import os

import pytest

from harness.claude_agent_sdk import ClaudeAgentSdkProvider
from harness.cli import make_harness
from harness.providers import ClaudeCliProvider, Usage

_SCHEMAS = [
    {"name": "read_file", "description": "read a file",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                      "required": ["path"]}},
    {"name": "edit_file", "description": "edit a file",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_string": {"type": "string"}}}},
]

# Phrasings that assert an edit/action happened or must happen on EVERY turn.
# Each one is false on a Review/Test/Plan turn and on a question answered from
# inspection alone, so none of them may appear in the mode-blind transport.
_IMPLEMENTATION_ONLY = (
    "before answering",
    "answer json is a failure",
    "an answer is a failure",
    "until the requested work",
    "use edit_file",
    "use write_file",
    "on the first turn of a coding task",
)


def _provider(replies):
    """A ClaudeCliProvider whose physical CLI call is scripted, never spawned."""
    provider = ClaudeCliProvider("opus")
    sent = []

    def _call(prompt, system):
        sent.append({"prompt": prompt, "system": system})
        reply = replies[min(len(sent) - 1, len(replies) - 1)]
        return reply, Usage(input_tokens=3, output_tokens=1)

    provider._call = _call
    provider.sent = sent
    return provider


def _system_for(mode):
    harness = make_harness(os.getcwd(), provider="mock",
                           project="modeprompt", embed="hash")
    system, _msgs, _meta = harness.composer.build(
        {"messages": []}, "look at the parser", os.getcwd(), "modeprompt", mode=mode)
    return system


def _mode_line(system, name):
    lines = [l for l in system.split("\n") if l.startswith("MODE: %s" % name)]
    assert lines, "no MODE: %s line in the composed system prompt" % name
    return lines[0]


# ---------------------------------------------------------------- transport
@pytest.mark.parametrize("mode", ["act", "plan", "review", "test"])
def test_the_transport_body_carries_no_implementation_only_demand(mode):
    """What is actually sent must be true on the turn it is sent on."""
    provider = _provider(['{"answer":"done"}'])

    provider.complete(_system_for(mode),
                      [{"role": "user", "content": "look at the parser"}], _SCHEMAS)

    body = provider.sent[0]["prompt"].lower()
    for phrase in _IMPLEMENTATION_ONLY:
        assert phrase not in body, (
            "the mode-blind transport claims %r, which is false in MODE %s" % (phrase, mode))


def test_the_transport_body_is_identical_across_modes():
    """The mode belongs to the system prompt; the protocol is one fixed contract.

    A per-mode transport body would also be a second, competing mode authority
    for any caller whose system prompt was replaced (the desktop persona).
    """
    bodies = set()
    for mode in ("act", "plan", "review", "test"):
        provider = _provider(['{"answer":"done"}'])
        provider.complete(_system_for(mode),
                          [{"role": "user", "content": "look at the parser"}], _SCHEMAS)
        bodies.add(provider.sent[0]["prompt"])
        # ...and the mode the caller chose does reach the model, unaltered.
        assert provider.sent[0]["system"] == _system_for(mode)
    assert len(bodies) == 1


def test_the_transport_body_still_states_the_executor_contract():
    provider = _provider(['{"answer":"done"}'])

    provider.complete("system", [{"role": "user", "content": "fix it"}], _SCHEMAS)
    body = provider.sent[0]["prompt"]

    # Only the listed executor tools are callable, and they are named.
    assert "# Tools the executor can run:" in body
    assert "- read_file(path): read a file" in body
    # Tool output is data that already arrived, not something to invent.
    assert "returns its output above as data" in body
    # Truthful completion, with no assumption that the work was an edit.
    assert "never report an inspection, a change, or a check you did not perform" in body
    # The strict single-object reply schema is unchanged.
    assert "Reply with EXACTLY ONE JSON object and nothing else" in body
    assert '{"tool":"<name>","args":{...}}' in body
    assert '{"answer":"<final answer>"}' in body


def test_no_tool_outside_the_advertised_set_is_offered():
    """A tool the executor cannot run must never be described as callable."""
    provider = _provider(['{"answer":"done"}'])

    provider.complete("system", [{"role": "user", "content": "fix it"}],
                      [_SCHEMAS[0]])
    body = provider.sent[0]["prompt"]

    assert "edit_file" not in body, (
        "the protocol named a tool this turn's schema list does not contain")


# ---------------------------------------------------------------- finishing
def test_a_review_turn_finishes_on_an_answer_with_no_edit():
    """The deliverable is the findings: one request, no re-prompt, no contract miss."""
    provider = _provider(['{"answer":"parser.py:31 swallows the decode error"}'])

    completion = provider.complete(
        _system_for("review"),
        [{"role": "user", "content": "review the parser"}], _SCHEMAS)

    assert completion.stop_reason == "end_turn"
    assert completion.text == "parser.py:31 swallows the decode error"
    assert completion.error_code == ""
    assert completion.request_count == 1 and len(provider.sent) == 1


def test_a_prose_reply_is_still_a_repairable_contract_miss():
    """Relaxing the implementation demand must not relax the reply schema."""
    provider = _provider(["I would start by reading parser.py.", "Still prose."])

    completion = provider.complete(
        _system_for("review"),
        [{"role": "user", "content": "review the parser"}], _SCHEMAS)

    assert completion.stop_reason == "error" and completion.error_status == 422
    assert completion.error_code == "response_contract_error"
    assert completion.request_count == 2


# ---------------------------------------------------------------- MODE lines
def test_act_owns_the_duty_to_make_and_verify_the_change():
    act = _mode_line(_system_for("act"), "Act")

    assert "edit_file" in act and "write_file" in act
    assert "before answering" in act, "Act must demand the change, not a description of it"
    # The verify contract survives, and stays project-shaped.
    assert "python -m pytest -q" in act
    assert "did and did not establish" in act
    # ...and an Act turn that is only a question may still answer without editing.
    assert "no edit is required" in act


@pytest.mark.parametrize("mode, name", [("plan", "Plan"), ("review", "Review"),
                                        ("test", "Test")])
def test_read_only_modes_say_what_finishing_looks_like(mode, name):
    line = _mode_line(_system_for(mode), name)

    assert "Do not edit" in line
    assert "edit_file" not in line and "write_file" not in line, (
        "a read-only mode must not carry Act's edit mandate")
    assert any(word in line for word in ("finish", "completes this task")), (
        "MODE %s never tells the model it may finish without an edit" % name)


def test_unknown_mode_still_falls_back_to_act():
    assert "MODE: Act" in _system_for("bogusmode")


# ---------------------------------------------------------------- neighbours
def test_the_sdk_shares_the_one_serializer():
    """ClaudeAgentSdkProvider must keep rendering the same protocol, untruncated."""
    sdk = ClaudeAgentSdkProvider.__new__(ClaudeAgentSdkProvider)
    sdk.structured_output = False
    messages = [{"role": "user", "content": "fix it"},
                {"role": "tool", "name": "read_file", "content": "x" * 2400}]

    rendered = sdk._prompt(messages, _SCHEMAS)
    cli = ClaudeCliProvider("opus")._prompt(messages, _SCHEMAS, tool_result_limit=None)

    assert rendered == cli
    assert "x" * 2400 in rendered, "the composer, not the serializer, decides what is elided"


def test_the_structured_formatter_mode_keeps_its_own_envelope():
    sdk = ClaudeAgentSdkProvider.__new__(ClaudeAgentSdkProvider)
    sdk.structured_output = True

    rendered = sdk._prompt([{"role": "user", "content": "fix it"}], _SCHEMAS)

    assert "Call the StructuredOutput formatter exactly once" in rendered
    assert '{"response":{"tool":"<name>","args":{...}}}' in rendered
    assert "Your only SDK tool is StructuredOutput" in rendered
    assert "Reply with EXACTLY ONE JSON object" not in rendered
    for phrase in _IMPLEMENTATION_ONLY:
        assert phrase not in rendered.lower()


def test_a_tool_less_caller_gets_no_harness_envelope():
    """Mission's planner defines its own action schema."""
    rendered = ClaudeAgentSdkProvider._plain_prompt(
        [{"role": "user", "content": "plan the migration"}])

    assert "Tools the executor can run" not in rendered
    assert '{"tool"' not in rendered
    assert rendered.endswith("Respond to the latest user message according to the system prompt.")
