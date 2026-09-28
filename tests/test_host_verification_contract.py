"""A Required run's own check must execute, even when the model's run has no test-runner trace.

The in-loop gate only counts commands it can recognize as asserting checks. A user's own
``python verify.py`` is opaque to it, so the gate used to fail a correct edit with
"verification required but no executed post-edit assertion passed" -- and that error is exactly
what stops the surface from starting the real check.
"""
import json
import sys

import pytest

from harness import cli, settings, webapp
from harness.providers import Completion, ToolCall
from _util import _RecordingMemory, _ScriptProvider


def _reminders(messages):
    return [m for m in messages if m.get("kind") == "verification_reminder"]


@pytest.mark.parametrize("expected,success", [("new", True), ("wrong", False)])
@pytest.mark.parametrize("surface", ["cli", "web"])
def test_required_custom_check_runs_after_real_edit(tmp_path, monkeypatch, capsys,
                                                    expected, success, surface):
    state = tmp_path / "state"
    state.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "value.txt").write_text("old", encoding="utf-8")
    (project / "verify.py").write_text(
        "from pathlib import Path\nassert Path('value.txt').read_text() == %r\n"
        "print('objective check executed')\n" % expected, encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setenv("COLLIE_NOTES_DIR", str(state / "notes"))
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(cli, "_paths", lambda: tuple(str(state / name) for name in
        ("memory.db", "runs.db", "dashboard.html", "sandbox")))
    command = '"%s" -I verify.py' % sys.executable
    requests = []

    def answer(messages):
        requests.append(list(messages))
        return Completion(text="Updated the requested file.")

    real_make = cli.make_harness
    created = []

    def make(*args, **kwargs):
        kwargs["embed"] = "hash"
        h = real_make(*args, **kwargs)
        h.provider = _ScriptProvider([
            Completion(tool_calls=[ToolCall("edit", "edit_file", {
                "path": "value.txt", "old_string": "old", "new_string": "new"})]),
            answer], name="mock", model="mock")
        h._max_turns_hard_cap = 8
        created.append(h)
        return h
    monkeypatch.setattr(cli, "make_harness", make)
    if surface == "cli":
        rc = cli.main(["run", "Update value.txt from old to new", "--provider", "mock",
            "--intent", "build", "--quality", "quick", "--verification", "required",
            "--verify-command", command, "--json"])
        result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert (rc == 0) is success
    else:
        monkeypatch.setattr(webapp, "_provider", lambda: "mock")
        monkeypatch.setattr(webapp.Handler, "_notify_done", lambda *a, **k: None)
        events = []
        handler = object.__new__(webapp.Handler)
        handler._sse_open = lambda: None
        handler._sse = lambda kind, data: events.append((kind, data))
        webapp.Handler._serve_stream(handler, {
            "q": ["Update value.txt from old to new"], "cwd": [str(project)],
            "session": ["host-check"], "intent": ["build"], "quality": ["quick"],
            "verification": ["required"], "verify_command": [command], "runner": ["collie"]})
        result = next(data for kind, data in events if kind == "done")
    assert (project / "value.txt").read_text() == "new"
    evidence = result["verification_evidence"]
    assert evidence["executed"] is True
    assert evidence["passed"] is success
    assert bool(result["error"]) is not success
    assert "verification required but no" not in (result["error"] or "")
    assert ("objective check executed" in evidence["output"]) is success
    # The model still gets one reminder before it finishes, and it names the check the
    # host will run -- not a test runner this project does not have -- and it does not
    # repeat: the gate cannot see that command satisfy it, so repeating would only ask
    # again for a check the model may already have run.
    assert created[0].provider.calls == 3
    reminders = _reminders(requests[-1])
    assert len(reminders) == 1
    assert command in reminders[0]["content"]


def _required_harness(monkeypatch, tmp_path, script):
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    h = cli.make_harness(str(tmp_path), provider="mock", project="required", embed="hash")
    h.memory = _RecordingMemory()
    h.provider = _ScriptProvider(script)
    cli.configure_run_options(h, verification="required")
    h.max_turns = 12
    return h


def _edit_then_answer():
    return [
        Completion(tool_calls=[ToolCall("e", "edit_file", {
            "path": "f.py", "old_string": "x = 1", "new_string": "x = 2"})],
                   stop_reason="tool_use"),
        Completion(text="done", stop_reason="end_turn"),
    ]


def test_a_check_the_gate_can_count_keeps_its_repair_rounds_but_not_the_verdict(
        monkeypatch, tmp_path):
    """A recognized runner keeps every bounded reminder; the host check still decides."""
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    command = "python -m pytest -q tests/test_f.py"
    h = _required_harness(monkeypatch, tmp_path, _edit_then_answer())
    cli.configure_host_verification(h, command)
    assert h.verify_gate is True and h.require_assert is True
    try:
        result = h.run("required-host", "fix it")
    finally:
        h.memory.close(); h.recorder.close()

    reminders = _reminders(result.messages)
    assert len(reminders) == h.verify_max
    # The reminders name the check that will actually be run on this tree.
    assert all(command in r["content"] for r in reminders)
    # Not having watched the model run it is no longer a run error: that error is what
    # made the surface skip the check it was about to run.
    assert not result.error and result.verified is False
    assert cli.stopped_before_verification(result) == ""


def test_without_a_host_check_the_required_gate_still_owns_the_verdict(monkeypatch, tmp_path):
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    h = _required_harness(monkeypatch, tmp_path, _edit_then_answer())
    cli.configure_host_verification(h, "   ")
    try:
        result = h.run("required-no-host", "fix it")
    finally:
        h.memory.close(); h.recorder.close()

    assert "verification required but no executed post-edit assertion passed" in result.error
    assert cli.stopped_before_verification(result)


def test_the_first_request_after_an_edit_names_the_host_check(monkeypatch, tmp_path):
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    command = '"%s" -I verify.py' % sys.executable
    seen = []

    def answer(messages):
        seen.append(list(messages))
        return Completion(text="done", stop_reason="end_turn")
    h = _required_harness(monkeypatch, tmp_path, [_edit_then_answer()[0], answer])
    cli.configure_host_verification(h, command)
    assert h.verify_gate is False and h.self_verify is True
    try:
        h.run("required-preflight", "fix it")
    finally:
        h.memory.close(); h.recorder.close()

    hints = [m for m in seen[0] if m.get("kind") == "verification_state"]
    assert len(hints) == 1 and command in hints[0]["content"]


@pytest.mark.parametrize("command,counted", [
    ("python -m pytest -q", True),
    ("npm test", True),
    ("python -c \"assert open('f.py').read()\"", True),
    ("python verify.py", False),
    ('"C:/Python/python.exe" -I verify.py', False),
    ("make check", False),
    ("python -m pytest -q | tee log", False),
])
def test_only_commands_the_gate_can_count_keep_the_gate(command, counted):
    from harness.loop import gate_counts_command

    assert gate_counts_command(command) is counted
