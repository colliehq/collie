"""Text-protocol tool calls need distinct execution identities, even on retries."""
import copy
import pytest

from harness.providers import (_parse_response_envelope, unique_tool_history,
                               AnthropicProvider, OpenAICompatProvider)


def test_repeated_and_equal_length_calls_do_not_reuse_receipt_identity():
    texts = ['{"tool":"read_file","args":{"path":"a.py"}}',
             '{"tool":"read_file","args":{"path":"b.py"}}']
    calls = [_parse_response_envelope(texts[index % 2])[1] for index in range(100)]
    assert len({call.id for call in calls}) == len(calls)
    assert all(call.name == "read_file" for call in calls)
    assert calls[0].args == {"path": "a.py"}
    assert calls[1].args == {"path": "b.py"}


@pytest.mark.parametrize("adapter", ["anthropic", "openai", "codex"])
def test_saved_length_based_ids_can_switch_provider_without_mutating_journal(adapter):
    from harness.codex_oauth import CodexOAuthProvider

    messages = []
    for name in ("a.py", "b.py"):
        messages.extend([
            {"role": "assistant", "tool_calls": [{"id": "cli_43", "name": "read_file", "args": {"path": name}}]},
            {"role": "tool", "tool_call_id": "cli_43", "content": name},
        ])
    original = copy.deepcopy(messages)
    if adapter == "anthropic":
        convert = object.__new__(AnthropicProvider)._to_anthropic
        rows = convert(messages)
        calls = [block["id"] for row in rows for block in row["content"] if block["type"] == "tool_use"]
        results = [block["tool_use_id"] for row in rows for block in row["content"] if block["type"] == "tool_result"]
    elif adapter == "openai":
        provider = object.__new__(OpenAICompatProvider)
        convert = lambda value: provider._to_openai("system", value)
        rows = convert(messages)
        calls = [call["id"] for row in rows for call in row.get("tool_calls", [])]
        results = [row["tool_call_id"] for row in rows if row["role"] == "tool"]
    else:
        convert = object.__new__(CodexOAuthProvider)._to_input
        rows = convert(messages)
        calls = [row["call_id"] for row in rows if row["type"] == "function_call"]
        results = [row["call_id"] for row in rows if row["type"] == "function_call_output"]
    assert len(calls) == 2 and len(set(calls)) == 2 and calls == results
    assert convert(messages) == rows
    assert messages == original


def test_overlapping_duplicate_ids_are_not_guessed_into_valid_history():
    messages = [{"role": "assistant", "tool_calls": [
        {"id": "same", "name": "read_file", "args": {"path": "a.py"}},
        {"id": "same", "name": "read_file", "args": {"path": "b.py"}},
    ]}]
    with pytest.raises(ValueError, match="overlapping tool calls"):
        unique_tool_history(messages)
