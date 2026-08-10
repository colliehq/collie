import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_run_decision_keeps_provider_and_pinned_model_but_scales_task_policy():
    from harness.router import resolve_run_decision

    decision = resolve_run_decision(
        "Fix the security race condition and its regression test",
        provider="codex-oauth", model="gpt-5.6-terra", route_kind="code")

    assert decision.provider == "codex-oauth"
    assert decision.model == "gpt-5.6-terra"
    assert decision.intent == "build"
    assert decision.quality == "thorough"
    assert decision.effort == "high"
    assert decision.verification == "required"
    assert "automatic routing never crosses providers" in " ".join(decision.reasons)


def test_route_kind_and_explicit_intent_have_clear_precedence():
    from harness.router import resolve_run_decision

    routed = resolve_run_decision("Explain this", "codex-oauth", route_kind="code")
    explicit = resolve_run_decision(
        "Fix it", "codex-oauth", route_kind="code", intent="review",
        explicit_axes=["intent"])

    assert routed.intent == "build" and routed.route_kind == "code"
    assert explicit.intent == "review" and explicit.sources["intent"] == "user"


def test_recent_failure_escalates_auto_model_without_crossing_provider():
    from harness.router import resolve_run_decision

    history = [{"role": "assistant", "content": "verification required but no test passed"}]
    decision = resolve_run_decision(
        "Fix the bug", "codex-oauth", route_kind="code", history=history)

    assert decision.model == "gpt-5.6-sol"
    assert decision.complexity == "hard"
    assert any("recent failure" in reason for reason in decision.reasons)


@pytest.mark.parametrize("task", [
    "Fix this tiny typo in the label",
    "把这个文案错别字修一下",
])
def test_auto_uses_luna_for_small_clear_repeatable_tasks(task):
    from harness.router import resolve_run_decision

    decision = resolve_run_decision(task, "codex-oauth", route_kind="code")
    assert decision.complexity == "simple"
    assert decision.model == "gpt-5.6-luna"
    assert decision.effort == "low"


@pytest.mark.parametrize("task", [
    "Fix the security race across this multi-file authentication flow",
    "系统性修复这个多文件并发竞态和权限回归",
])
def test_auto_uses_sol_for_high_risk_english_and_chinese_tasks(task):
    from harness.router import resolve_run_decision

    decision = resolve_run_decision(task, "codex-oauth", route_kind="code")
    assert decision.complexity == "hard"
    assert decision.model == "gpt-5.6-sol"
    assert decision.effort == "high"
    assert decision.verification == "required"


def test_quick_is_run_depth_while_fast_is_same_model_service_tier():
    from harness.router import resolve_run_decision

    quick = resolve_run_decision(
        "Make this tiny edit", "codex-oauth", route_kind="code",
        quality="quick", explicit_axes=["quality"])
    fast = resolve_run_decision(
        "Make this tiny edit", "codex-oauth", model="gpt-5.6-luna",
        route_kind="code", speed="fast", explicit_axes=["speed"])

    assert quick.quality == "quick" and quick.speed == "standard"
    assert quick.effort == "low"
    assert fast.model == "gpt-5.6-luna" and fast.speed == "fast"
    assert fast.billing_multiplier == 2.5


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
def test_codex_fast_capability_covers_the_56_family(model):
    from harness.providers import provider_capabilities

    caps = provider_capabilities("codex-oauth", model)
    assert "fast" in caps["speed_tiers"]
    assert caps["fast_billing_multiplier"] == 2.5
    assert "same model" in caps["fast_note"]


def test_unknown_codex_model_cannot_silently_fake_fast():
    from harness.providers import resolve_speed_tier

    with pytest.raises(ValueError, match="Fast is not supported"):
        resolve_speed_tier("codex-oauth", "gpt-4o", "fast")


def test_test_gate_runs_only_the_exact_proposed_check(tmp_path):
    from harness.gate import Gate, Mode

    gate = Gate(tmp_path, mode=Mode.TEST,
                allowed_commands=["python -m pytest -q"])
    assert gate.evaluate("bash", {"command": "python -m pytest -q"}).allowed
    assert not gate.evaluate("bash", {"command": "python -m pytest -q && echo unsafe"}).allowed
    assert not gate.evaluate("write_file", {"path": "x.py", "content": "x"}).allowed


def test_verification_detection_evidence_and_session_receipt_persist(monkeypatch, tmp_path):
    from harness import sessions
    from harness.verification import detect_verification_commands, run_verification_command

    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    candidates = detect_verification_commands(str(tmp_path))
    assert candidates and candidates[0]["command"] == "python -m pytest -q"

    evidence = run_verification_command(
        'python -c "print(123)"', str(tmp_path), source="test")
    assert evidence["passed"] is True and evidence["exit_code"] == 0
    for key in ("command", "timestamp", "duration_ms", "cwd", "working_tree",
                "ran_after_last_edit", "source"):
        assert key in evidence

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    sessions.save("receipt", [{"role": "user", "content": "one"}])
    sessions.append_run_receipt("receipt", {"decision": {"model": "m"},
                                                    "verification_evidence": evidence})
    sessions.save("receipt", [{"role": "user", "content": "one"},
                              {"role": "assistant", "content": "two"}])
    saved = sessions.load("receipt")
    assert saved["run_receipts"][-1]["verification_evidence"]["passed"] is True


def test_web_refuses_uncertain_recovery_before_loading_history(monkeypatch, tmp_path):
    from harness import sessions, settings, webapp

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(webapp, "_provider", lambda: "mock")
    monkeypatch.setattr(settings, "apply", lambda: None)
    sessions.checkpoint(
        "uncertain", [{"role": "user", "content": "do it"}],
        run_id="r1", state="executing_tool", detail={"tool_name": "browser_click"})
    events = []
    handler = object.__new__(webapp.Handler)
    handler._sse_open = lambda: None
    handler._sse = lambda kind, data: events.append((kind, data))

    webapp.Handler._serve_stream(handler, {"q": ["continue"], "session": ["uncertain"]})

    assert events[-1][0] == "done"
    assert events[-1][1]["recovery_required"] is True
    assert not any(kind == "start" for kind, _ in events)


def _result(model="gpt-5.6-sol"):
    return SimpleNamespace(
        answer="done", error="", model=model, messages=[
            {"role": "user", "content": "fix"},
            {"role": "assistant", "content": "done"}],
        prefix_tokens=1, prefix_measured=False, input_tokens=2, output_tokens=3,
        cache_read=0, cache_creation=0, total_tokens=5, cache_miss_tokens=2,
        cache_waste_usd=0, turns=1, tool_calls=0, mem_recalls=0, wall_ms=1,
        cost_usd=0, verified=False)


def test_cli_run_executes_and_reports_the_resolved_decision(monkeypatch, tmp_path, capsys):
    from harness import cli, settings

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    seen = {}
    closer = SimpleNamespace(close=lambda: None, set_block=lambda *a, **k: None)

    class FakeHarness:
        def __init__(self):
            self.memory = self.recorder = closer
            self.provider = SimpleNamespace(actual_speed="standard")
            self.mode = "act"; self.force_edit = True; self.max_turns = 20
            self._max_turns_hard_cap = None; self.self_verify = False
            self.verify_max = 2; self.verify_gate = False; self.require_assert = False
        def run(self, *args, **kwargs):
            seen["mode"] = self.mode
            return _result()

    def fake_make(*args, **kwargs):
        seen.update(kwargs)
        return FakeHarness()

    monkeypatch.setattr(cli, "make_harness", fake_make)
    args = SimpleNamespace(
        task="fix a tiny typo", cwd=str(tmp_path), provider="codex-oauth",
        model="gpt-5.6-sol", project="p", mode=None, persona=None, goal=None,
        resume=None, cont=False, stream_json=False, json=True, print=False,
        web_search=False, intent="build", quality="quick", verification="auto",
        effort="low", speed="standard", verify_command=None)

    assert cli.cmd_run(args) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert seen["model"] == "gpt-5.6-sol" and seen["effort"] == "low"
    assert seen["mode"] == "act"
    assert payload["decision"]["model"] == "gpt-5.6-sol"
    assert payload["decision"]["quality"] == "quick"


@pytest.mark.parametrize("resume,cont", [("uncertain-cli", False), (None, True)])
def test_cli_resume_and_continue_refuse_uncertain_tool_replay(
        monkeypatch, tmp_path, capsys, resume, cont):
    from harness import cli, sessions

    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    sessions.checkpoint(
        "uncertain-cli", [{"role": "user", "content": "send it"}],
        run_id="r1", state="external_action", detail={"tool_name": "browser_click"})
    args = SimpleNamespace(
        cwd=str(tmp_path), provider="mock", resume=resume, cont=cont,
        json=True, stream_json=False)

    assert cli.cmd_run(args) == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["recovery_required"] is True
    assert payload["session"] == "uncertain-cli"


def test_cli_pack_passes_quality_effort_speed_and_decision(monkeypatch, tmp_path, capsys):
    from harness import cli, pack, settings

    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    seen = {}
    monkeypatch.setattr(pack, "run_pack", lambda *a, **kw: (
        seen.update(kw) or {"winner": 0, "applied": False, "reason": "passed",
                            "attempts": [], "answer": "ok", "n": 2,
                            "total_cost_usd": 0, "apply_error": ""}))
    args = SimpleNamespace(
        task="fix typo", cwd=str(tmp_path), provider="codex-oauth",
        model="gpt-5.6-luna", n=2, check="python -m pytest -q", apply=False,
        roster=None, parallel=1, json=True, quality="quick", verification="required",
        effort="low", speed="fast")

    assert cli.cmd_pack(args) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert (seen["model"], seen["effort"], seen["speed"]) == (
        "gpt-5.6-luna", "low", "fast")
    assert seen["quality"] == "quick" and seen["verification"] == "required"
    assert payload["decision"]["provider"] == "codex-oauth"
    assert payload["decision"]["strategy"] == "pack"
