"""Output-limit truncation is an episode to recover from, not a run-global death sentence.

A response that stops at the output-token limit may carry half-written tool arguments, so the
loop refuses every call in it and asks for the action again at a bigger ceiling.  The bound on
that recovery used to be counted once per RUN: a long session that hit the limit three times,
recovered fully each time, and was about to answer, was killed on the third one and reported an
"output-limit truncation loop" that had never happened.  A completion that did NOT stop at the
limit is real forward progress, so it restores the allowance -- exactly as a valid response
restores the structured-response repair in test_protocol_recovery_progress.py.

Truncated tool calls must still never execute, and three truncations in a row must still stop.
"""
import os

from _util import _ScriptProvider
from harness.cli import make_harness
from harness.providers import Completion, ToolCall, Usage


SENTINEL = "must_not_exist.txt"


def setup(tmp_path, replies, max_turns=0):
    for name in ("a", "b", "c"):
        (tmp_path / (name + ".txt")).write_text("contents of " + name)
    harness = make_harness(str(tmp_path), provider="mock", project="trunc-progress",
                           embed="hash")
    harness.max_turns = max_turns
    harness.max_retries = 0
    harness.provider = _ScriptProvider(replies)
    return harness


def truncated_write(text=""):
    """A length-stopped response whose tool arguments are, by definition, untrustworthy."""
    return Completion(
        text=text,
        tool_calls=[ToolCall("cut", "write_file",
                             {"path": SENTINEL, "content": "half-written"})],
        stop_reason="length")


def read(name):
    return Completion(tool_calls=[ToolCall("read-" + name, "read_file",
                                           {"path": name + ".txt"})],
                      stop_reason="tool_use")


def test_separated_truncations_do_not_end_a_progressing_run(tmp_path):
    harness = setup(tmp_path, [
        truncated_write(), read("a"),
        truncated_write(), read("b"),
        truncated_write(), read("c"),
        Completion(text="All three files were read.", stop_reason="end_turn",
                   usage=Usage(input_tokens=3)),
    ])
    result = harness.run("progress", "Read every file.", consolidate=False)

    assert result.answer == "All three files were read." and not result.error
    assert result.success and result.stop_reason == "completed"
    assert harness.provider.calls == result.model_calls == 7
    # Each truncated response was refused, and each recovery turn really ran.
    assert not os.path.exists(str(tmp_path / SENTINEL))
    tool_msgs = [m for m in result.messages if m.get("role") == "tool"]
    refused = [m for m in tool_msgs if "not executed" in m["content"]]
    assert len(refused) == 3 and all(m["name"] == "write_file" for m in refused)
    assert len([m for m in tool_msgs if m["name"] == "read_file"]) == 3
    # The run finished cleanly, so no answer may be labelled truncated.
    assert "truncated at output-token limit" not in result.answer


def test_three_consecutive_truncations_still_stop_the_run(tmp_path):
    harness = setup(tmp_path, [truncated_write(), truncated_write(), truncated_write(),
                               Completion(text="must not be reached", stop_reason="end_turn")])
    result = harness.run("consecutive", "Write the file.", consolidate=False)

    assert harness.provider.calls == result.model_calls == 3
    assert not os.path.exists(str(tmp_path / SENTINEL))
    assert not result.answer and not result.success
    # The receipt names the condition and the ceiling that was already tried.
    assert result.error.startswith("output-limit truncation: 3 consecutive responses hit "
                                   "the output-token limit"), result.error
    assert "output ceiling now 32768 tokens" in result.error   # 4096 doubled three times
    assert "not executed" in result.error


def test_a_recovered_truncation_does_not_count_against_a_later_episode(tmp_path):
    """One truncation, a real turn, then two more in a row: the pair is under the bound."""
    harness = setup(tmp_path, [
        truncated_write(), read("a"),
        truncated_write(), truncated_write(), read("b"),
        Completion(text="Recovered twice.", stop_reason="end_turn", usage=Usage(input_tokens=3)),
    ])
    result = harness.run("mixed", "Read the files.", consolidate=False)

    assert result.answer == "Recovered twice." and not result.error
    assert harness.provider.calls == result.model_calls == 6


def test_truncation_on_the_last_available_turn_is_not_reported_as_a_loop(tmp_path):
    harness = setup(tmp_path, [truncated_write(),
                               Completion(text="must not be reached", stop_reason="end_turn")],
                    max_turns=1)
    result = harness.run("last-turn", "Write the file.", consolidate=False)

    assert harness.provider.calls == result.model_calls == 1
    assert not os.path.exists(str(tmp_path / SENTINEL))
    assert result.error == ("output-limit truncation: the response hit the output-token limit "
                            "on the last available turn; its tool calls were not executed "
                            "because truncated arguments are unsafe to run "
                            "(output ceiling now 8192 tokens)"), result.error


def test_a_truncated_plain_answer_still_keeps_its_marker_after_recovery(tmp_path):
    """The per-episode reset must not disarm the visible truncation marker."""
    harness = setup(tmp_path, [
        Completion(text="partial one", stop_reason="length", usage=Usage(input_tokens=3)),
        read("a"),
        Completion(text="partial two", stop_reason="length", usage=Usage(input_tokens=3)),
        Completion(text="ignored", stop_reason="end_turn"),
    ], max_turns=3)
    result = harness.run("marker", "Explain.", consolidate=False)

    assert harness.provider.calls == 3
    assert result.answer.startswith("partial two")
    assert "_[answer truncated at output-token limit]_" in result.answer
