"""Contracts for the orthogonal web run options (intent, quality, verification, workspace/Pack)."""

from types import SimpleNamespace

import pytest

from harness.cli import configure_run_options, normalize_run_options


def harness_stub():
    return SimpleNamespace(
        mode="act", force_edit=True, max_turns=20, verify_max=2,
        self_verify=False, verify_gate=False, require_assert=False,
        _max_turns_hard_cap=None,
    )


def test_plan_is_a_prompt_role_not_a_quality_or_verification_alias():
    h = harness_stub()
    got = configure_run_options(h, intent="plan", quality="balanced", verification="auto")
    assert got == {"intent": "plan", "quality": "balanced", "verification": "auto"}
    assert h.mode == "plan" and h.force_edit is False
    assert h.max_turns == 40 and h.verify_gate is False


def test_thorough_and_required_are_independent_axes():
    thorough = harness_stub()
    configure_run_options(thorough, quality="thorough", verification="auto")
    assert thorough.max_turns == 50 and thorough.verify_max == 4
    assert thorough.verify_gate is False and thorough.require_assert is False

    required = harness_stub()
    configure_run_options(required, quality="balanced", verification="required")
    assert required.max_turns == 40
    assert required.self_verify is True
    assert required.verify_gate is True and required.require_assert is True
    assert required.verify_max == 4


def test_quality_targets_differ_but_never_widen_a_user_hard_cap():
    balanced = harness_stub()
    configure_run_options(balanced, quality="balanced")
    thorough = harness_stub()
    configure_run_options(thorough, quality="thorough")
    capped = harness_stub()
    capped._max_turns_hard_cap = 5
    configure_run_options(capped, quality="thorough")

    assert (balanced.max_turns, thorough.max_turns) == (40, 50)
    assert capped.max_turns == 5


def test_thorough_respects_the_real_max_turns_environment_cap(monkeypatch, tmp_path):
    from harness import cli

    monkeypatch.setenv("COLLIE_MAX_TURNS", "5")
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    h = cli.make_harness(str(tmp_path), provider="mock", project="turn-cap", embed="hash")
    try:
        configure_run_options(h, quality="thorough")
        assert h._max_turns_hard_cap == 5 and h.max_turns == 5
    finally:
        h.memory.close(); h.recorder.close()


@pytest.mark.parametrize("axis,value", [
    ("intent", "review-ish"),
    ("quality", "fast"),
    ("verification", "maybe"),
])
def test_unknown_options_fail_closed(axis, value):
    kwargs = {"intent": "build", "quality": "balanced", "verification": "auto"}
    kwargs[axis] = value
    with pytest.raises(ValueError):
        configure_run_options(harness_stub(), **kwargs)


def test_web_ui_sends_each_axis_and_does_not_fake_fast():
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "harness" / "webui" / "index.html").read_text(
        encoding="utf-8")
    for field in ("runIntent", "runQuality", "runVerification", "runWorkspace", "runStrategy",
                  "runEffort", "runSpeed", "verifyCommand"):
        assert f'id="{field}"' in page
    for query in ("&intent=", "&quality=", "&verification=", "&workspace=", "&strategy=",
                  "&effort=", "&speed=", "&explicit_axes="):
        assert query in page
    assert "Fast is not lower effort" in page
    assert 'data-val="quick"' in page
    assert 'data-val="test"' in page and 'data-val="review"' in page
    assert "Pack needs an executed check command" in page
    assert 'id="mode"' not in page


def test_normalization_is_case_and_whitespace_safe():
    assert normalize_run_options(" PLAN ", "THOROUGH", "Required") == {
        "intent": "plan", "quality": "thorough", "verification": "required"}


def test_web_plan_wires_both_prompt_role_and_tool_gate(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from harness import cli, settings, webapp

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(webapp, "_provider", lambda: "mock")
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get", lambda *args, **kwargs: "")
    seen = {}
    closer = SimpleNamespace(close=lambda: None)

    class FakeHarness:
        def __init__(self, gate):
            self.gate = gate
            self.composer = SimpleNamespace(identity="")
            self.memory = self.recorder = closer
            self.mode = "act"; self.force_edit = True; self.max_turns = 20
            self._max_turns_hard_cap = None
            self.self_verify = False; self.verify_max = 2
            self.verify_gate = False; self.require_assert = False

        def run(self, task_id, message, history=None, **kwargs):
            seen.update(mode=self.mode, gate=self.gate.mode.value,
                        verification=self.verify_gate, assert_required=self.require_assert)
            return SimpleNamespace(
                answer="read-only plan", error="", model="mock", prefix_tokens=0,
                input_tokens=0, output_tokens=0, total_tokens=0, turns=1, tool_calls=0,
                wall_ms=1, cost_usd=0, verified=False, canceled=False,
                messages=[{"role": "user", "content": message},
                          {"role": "assistant", "content": "read-only plan"}],
            )

    monkeypatch.setattr(cli, "make_harness",
                        lambda *args, **kwargs: FakeHarness(kwargs["gate"]))
    events = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear(); webapp.Handler._cancel_events.clear()

    webapp.Handler._serve_stream(fake, {
        "q": ["propose the change"], "session": ["plan-contract"],
        "intent": ["plan"], "quality": ["thorough"], "verification": ["auto"],
        "workspace": ["current"], "strategy": ["single"],
    })

    assert seen == {"mode": "plan", "gate": "plan",
                    "verification": False, "assert_required": False}, events
    start = next(data for kind, data in events if kind == "start")
    assert start["intent"] == "plan" and start["quality"] == "thorough"
    assert events[-1][0] == "done" and not events[-1][1]["error"]


@pytest.mark.parametrize("extra,needle", [
    ({"strategy": ["pack"]}, "Pack is only available for Build"),
    ({"workspace": ["isolated"]}, "isolation is only available for Build"),
    ({"verification": ["required"]}, "Required verification is only available for Build"),
])
def test_web_plan_rejects_build_only_combinations(monkeypatch, tmp_path, extra, needle):
    from harness import settings, webapp

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(webapp, "_provider", lambda: "mock")
    monkeypatch.setattr(settings, "apply", lambda: None)
    events = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    qs = {"q": ["plan it"], "intent": ["plan"], "quality": ["balanced"],
          "verification": ["auto"], "workspace": ["current"], "strategy": ["single"]}
    qs.update(extra)

    webapp.Handler._serve_stream(fake, qs)

    assert events[-1][0] == "done" and needle in events[-1][1]["error"]
    assert not any(kind == "start" for kind, _ in events)


def _required_harness(monkeypatch, tmp_path, script):
    from harness import cli
    from tests._util import _RecordingMemory, _ScriptProvider

    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    h = cli.make_harness(str(tmp_path), provider="mock", project="required", embed="hash")
    h.memory = _RecordingMemory()
    h.provider = _ScriptProvider(script)
    h.self_verify = False
    cli.configure_run_options(h, verification="required")
    h.max_turns = 8
    return h


def test_required_hard_fails_after_repair_rounds_and_persists_failed_verdict(monkeypatch, tmp_path):
    from harness.providers import Completion, ToolCall

    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    h = _required_harness(monkeypatch, tmp_path, [
        Completion(tool_calls=[ToolCall("e", "edit_file", {
            "path": "f.py", "old_string": "x = 1", "new_string": "x = 2"})],
                   stop_reason="tool_use"),
        Completion(tool_calls=[ToolCall("p", "bash", {
            "command": "python -c \"print('assert')\""})], stop_reason="tool_use"),
        Completion(text="done", stop_reason="end_turn"),
    ])
    seen = {}
    finish = h.recorder.finish_run
    h.recorder.finish_run = lambda result: (seen.update(
        verified=result.verified, error=result.error, success=result.success), finish(result))[1]
    try:
        result = h.run("required-fail", "fix it")
    finally:
        h.memory.close(); h.recorder.close()

    assert h.self_verify is True
    assert result.verified is False and result.error
    assert "verification required" in result.error
    assert "run failed" in result.answer
    assert seen["verified"] is False and seen["error"] and seen["success"] is False
    assert not h.memory.remembered


def test_required_accepts_an_actual_passing_test_runner(monkeypatch, tmp_path):
    from harness.providers import Completion, ToolCall

    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "test_f.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    h = _required_harness(monkeypatch, tmp_path, [
        Completion(tool_calls=[ToolCall("e", "edit_file", {
            "path": "f.py", "old_string": "x = 1", "new_string": "x = 2"})],
                   stop_reason="tool_use"),
        Completion(tool_calls=[ToolCall("t", "bash", {
            "command": "python -m pytest -q"})], stop_reason="tool_use"),
        Completion(text="done", stop_reason="end_turn"),
    ])
    try:
        result = h.run("required-pass", "fix it")
    finally:
        h.memory.close(); h.recorder.close()

    assert result.verified is True and not result.error


@pytest.mark.parametrize("command", [
    "python -m pytest -q || true",
    "python -m pytest -q | tee test.log",
    "python -m pytest -q; exit 0",
    "python -m pytest -q &",
    "echo $(python -m pytest -q)",
    "python -c \"assert False\" || true",
    "python -c \"assert False\" | tee test.log",
    "python <<'PY'\nassert False\nPY\ntrue",
])
def test_required_rejects_checks_whose_exit_status_can_be_masked(command):
    from harness.loop import _is_asserting_cmd, _is_repro_cmd, _is_test_runner_cmd

    assert _is_asserting_cmd(command) is False
    assert _is_repro_cmd("bash", {"command": command}) is False
    if "pytest" in command:
        assert _is_test_runner_cmd(command) is False


@pytest.mark.parametrize("command", [
    "cd src && python -m pytest -q",
    "python -m pytest -q 2>&1",
    "python -m pytest -q &>test.log",
])
def test_required_accepts_safe_test_chains_and_redirections(command):
    from harness.loop import _is_test_runner_cmd

    assert _is_test_runner_cmd(command) is True


def test_required_parses_python_heredoc_assertions_without_treating_payload_as_shell():
    from harness.loop import _is_asserting_cmd, _is_repro_cmd

    asserted = "python 2>&1 <<'PY'\nvalue = 2\nassert value == 2\nPY"
    print_only = "python <<'PY'\nprint('assert value == 2')\nPY"
    assert _is_repro_cmd("bash", {"command": asserted}) is True
    assert _is_asserting_cmd(asserted) is True
    assert _is_repro_cmd("bash", {"command": print_only}) is True
    assert _is_asserting_cmd(print_only) is False
