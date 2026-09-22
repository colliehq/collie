"""The post-edit reminder must name a check this workspace actually has.

Observed on a live JSON-only task (conversation-42, collie-own-loop arm, turn 1): the model
wrote the one requested file correctly, the loop injected the advisory reminder, and the
reminder's first named command was ``python -m pytest -q``.  The model then searched for a
test project, found none, ran pytest anyway, got "no tests ran", and left four
``.pytest_cache`` files in a workspace whose task authorized exactly two output files.

These tests pin the host side of that: which command the reminder names, given what the
workspace contains.  They do not claim anything about what a model does next — that needs a
live run, which is not part of this suite.
"""
import json

import pytest

from harness import cli, loop
from harness.providers import Completion, ToolCall
from _util import _ScriptProvider


def _workspace(tmp_path, name="ws"):
    ws = tmp_path / name
    ws.mkdir()
    return ws


def _json_only(tmp_path):
    """The shape of the real fixture: prose + one JSON deliverable, no code, no suite."""
    ws = _workspace(tmp_path)
    (ws / "README.md").write_text("# ledger workspace\n", encoding="utf-8")
    (ws / "ledger.json").write_text('{"schema": 1, "entries": []}\n', encoding="utf-8")
    return ws


def _pytest_project(tmp_path):
    ws = _workspace(tmp_path, "code-ws")
    (ws / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (ws / "tests").mkdir()
    (ws / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return ws


# ── selection ────────────────────────────────────────────────────────────────

def test_json_only_workspace_reminder_names_no_test_runner(tmp_path):
    nudge = loop.verify_nudge_for(str(_json_only(tmp_path)), ["ledger.json"])

    for runner in ("pytest", "npm test", "go test", "cargo test", "tox"):
        assert runner not in nudge, "no suite exists here; naming %r invites a blind run" % runner
    assert "ledger.json" in nudge, "the reminder should point at what was actually produced"
    assert "install" in nudge and "test project" in nudge


def test_json_only_reminder_does_not_let_parsing_stand_in_for_the_goal(tmp_path):
    """A format check is evidence about bytes, not about the request being satisfied."""
    nudge = loop.verify_nudge_for(str(_json_only(tmp_path)), ["ledger.json"])

    assert "does not prove the request was satisfied" in nudge
    assert "remain unverified" in nudge


def test_json_only_reminder_asks_for_a_check_that_writes_nothing(tmp_path):
    """Caches violated the final file contract; the early stop was a separate driver defect."""
    nudge = loop.verify_nudge_for(str(_json_only(tmp_path)), ["ledger.json"])

    assert "writes nothing into this workspace" in nudge


def test_python_project_reminder_names_the_detected_suite(tmp_path):
    nudge = loop.verify_nudge_for(str(_pytest_project(tmp_path)), ["app.py"])

    assert "`python -m pytest -q`" in nudge
    assert "Python test layout" in nudge, "say where the command came from, don't guess in prose"
    assert "If anything fails, read the error, fix it, and re-run." in nudge


def test_node_project_reminder_names_the_package_script(tmp_path):
    ws = _workspace(tmp_path)
    (ws / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8")

    nudge = loop.verify_nudge_for(str(ws), ["index.js"])

    assert "`npm run test`" in nudge and "package.json#scripts.test" in nudge
    assert "pytest" not in nudge


def test_every_variant_keeps_the_run_hygiene_and_delivery_rules(tmp_path):
    """Hygiene ("don't hide the exit status") and "answer the user" apply to all wordings."""
    variants = [loop.VERIFY_NUDGE,
                loop.verify_nudge_for(str(_json_only(tmp_path)), ["ledger.json"]),
                loop.verify_nudge_for(str(_pytest_project(tmp_path)), ["app.py"])]

    for nudge in variants:
        assert "hiding its exit status" in nudge
        assert "not a new user request" in nudge
        assert "in the user's requested format" in nudge


def test_detection_failure_keeps_the_generic_reminder(tmp_path, monkeypatch):
    """Detection is an optimization: losing it must not lose the reminder itself."""
    from harness import verification

    def _boom(cwd):
        raise OSError("workspace not readable")

    monkeypatch.setattr(verification, "detect_verification_commands", _boom)

    assert loop.verify_nudge_for(str(_json_only(tmp_path)), ["ledger.json"]) == loop.VERIFY_NUDGE


# ── through the loop ─────────────────────────────────────────────────────────

def _harness(tmp_path, monkeypatch, cwd, script):
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    h = cli.make_harness(str(cwd), provider="mock", project="verify-select", embed="hash")
    h.provider = _ScriptProvider(script)
    h.max_turns = 8
    return h


def _reminders(result):
    return [m["content"] for m in result.messages
            if m.get("kind") == "verification_reminder"]


def test_loop_reminder_on_a_json_workspace_does_not_ask_for_pytest(tmp_path, monkeypatch):
    """The regression for the observed turn: same edit, same finish, no pytest instruction."""
    ws = _json_only(tmp_path)
    h = _harness(tmp_path, monkeypatch, ws, [
        Completion(tool_calls=[ToolCall("w", "write_file", {
            "path": "ledger.json",
            "content": '{"schema": 1, "rule_version": 1, "entries": []}\n'})],
            stop_reason="tool_use"),
        Completion(text="Created ledger.json.", stop_reason="end_turn"),
        Completion(text="Created ledger.json; re-read it and it parses.", stop_reason="end_turn"),
    ])
    try:
        result = h.run("json-task", "Create ledger.json with an empty entries list.")
    finally:
        h.memory.close()
        h.recorder.close()

    reminders = _reminders(result)
    assert len(reminders) == 1, "still exactly one advisory nudge, not a new loop"
    assert "pytest" not in reminders[0]
    assert "ledger.json" in reminders[0]
    assert not result.error


def test_loop_reminder_on_a_real_pytest_project_still_asks_for_the_suite(tmp_path, monkeypatch):
    """The fix must not quietly stop asking for tests where tests exist."""
    ws = _pytest_project(tmp_path)
    h = _harness(tmp_path, monkeypatch, ws, [
        Completion(tool_calls=[ToolCall("e", "edit_file", {
            "path": "app.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"})],
            stop_reason="tool_use"),
        Completion(text="Changed the value.", stop_reason="end_turn"),
        Completion(text="Changed the value; suite is green.", stop_reason="end_turn"),
    ])
    try:
        result = h.run("code-task", "Bump VALUE to 2.")
    finally:
        h.memory.close()
        h.recorder.close()

    reminders = _reminders(result)
    assert len(reminders) == 1
    assert "`python -m pytest -q`" in reminders[0]


def test_caller_supplied_verify_nudge_still_overrides_selection(tmp_path, monkeypatch):
    """SWE pins its own wording (a quick `python -c` repro, not a suite). That still wins."""
    ws = _json_only(tmp_path)
    h = _harness(tmp_path, monkeypatch, ws, [
        Completion(tool_calls=[ToolCall("w", "write_file", {
            "path": "ledger.json", "content": "{}\n"})], stop_reason="tool_use"),
        Completion(text="done", stop_reason="end_turn"),
        Completion(text="done, checked", stop_reason="end_turn"),
    ])
    h.verify_nudge = "RUN THE PINNED REPRO."
    try:
        result = h.run("pinned", "write it")
    finally:
        h.memory.close()
        h.recorder.close()

    assert _reminders(result) == ["RUN THE PINNED REPRO."]


def test_required_verification_still_refuses_an_unverified_finish(tmp_path, monkeypatch):
    """Required is a contract, not a hint: a workspace with no suite does not relax it.

    The reminder's wording changed; what makes a finish acceptable did not.  Nothing was
    executed here, so Required must still end unverified and in error.
    """
    ws = _json_only(tmp_path)
    h = _harness(tmp_path, monkeypatch, ws, [
        Completion(tool_calls=[ToolCall("w", "write_file", {
            "path": "ledger.json", "content": "{}\n"})], stop_reason="tool_use"),
        Completion(text="done", stop_reason="end_turn"),
    ])
    cli.configure_run_options(h, verification="required")
    h.max_turns = 8
    try:
        result = h.run("required-json", "write it")
    finally:
        h.memory.close()
        h.recorder.close()

    assert h.verify_gate is True and h.self_verify is True
    assert result.verified is False
    assert "verification required" in result.error
    assert _reminders(result), "Required still pushes back before giving up"


if __name__ == "__main__":  # pragma: no cover - convenience for the pytest-less path
    raise SystemExit(pytest.main([__file__, "-q"]))
