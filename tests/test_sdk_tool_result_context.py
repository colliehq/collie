"""The model must receive the tool-result context the composer retained."""
from harness.claude_agent_sdk import ClaudeAgentSdkProvider
from harness.providers import ClaudeCliProvider

TOOLS = [{"name": "read_file", "description": "Read source",
          "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]


def test_sdk_keeps_recent_file_tail_and_deliberate_omission_marker():
    # Reading a moderately sized module used to hide its last public function
    # even though the host had retained the full tool result for this turn.
    source = "def helper():\n    pass\n" * 140 + "def public_tail():\n    return 42\n"
    old_result = "earlier output\n" * 170 + "[older output omitted by context budget]"
    messages = [
        {"role": "user", "content": "Check every public function in module.py"},
        {"role": "tool", "name": "read_file", "content": old_result},
        {"role": "tool", "name": "read_file", "content": source},
    ]
    prompt = ClaudeAgentSdkProvider()._payload(messages, TOOLS)
    assert source in prompt
    assert old_result in prompt
    assert messages[-1]["content"] == source


def test_legacy_cli_keeps_its_existing_tool_result_limit():
    source = "x" * 2000 + "TAIL_AFTER_LEGACY_LIMIT"
    prompt = ClaudeCliProvider._prompt(None, [
        {"role": "tool", "name": "read_file", "content": source}], TOOLS)
    assert "x" * 2000 in prompt
    assert "TAIL_AFTER_LEGACY_LIMIT" not in prompt
