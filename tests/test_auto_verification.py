"""Auto verification should not re-run a check that already passed on the latest edit."""
import pytest

from harness import cli
from harness.providers import Completion, ToolCall
from _util import _ScriptProvider


@pytest.mark.parametrize("check_result,command,extra_call", [
    ("3 passed", "python -m pytest -q", False),
    ("[exit 1]\n1 failed", "python -m pytest -q", True),
    ("3 passed", "python -m pytest -q | tail -5", True),
])
def test_auto_uses_fresh_evidence_before_adding_a_reminder(
        tmp_path, monkeypatch, check_result, command, extra_call):
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    (tmp_path / "value.txt").write_text("old", encoding="utf-8")
    h = cli.make_harness(str(tmp_path), provider="mock", project="auto-check", embed="hash")
    h.registry.get("bash").run = lambda args, ctx: check_result
    h.provider = _ScriptProvider([
        Completion(tool_calls=[ToolCall("e", "edit_file", {
            "path": "value.txt", "old_string": "old", "new_string": "new"})]),
        Completion(tool_calls=[ToolCall("v", "bash", {"command": command})]),
        Completion(text="Updated the value. Verification result recorded."),
    ])
    try:
        result = h.run("test", "Update the value and report briefly.", consolidate=False)
        assert result.model_calls == 3 + int(extra_call)
        reminders = [m for m in result.messages if m.get("kind") == "verification_reminder"]
        assert bool(reminders) is extra_call
        assert result.verified is (not extra_call)
        if reminders:
            assert reminders[0]["source"] == "harness"
    finally:
        h.memory.close()
        h.recorder.close()
