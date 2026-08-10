import asyncio
from types import SimpleNamespace

import pytest


def _settings_auto(monkeypatch):
    from harness import settings

    monkeypatch.setattr(
        settings, "get",
        lambda key, default=None: {
            "PROVIDER": "codex-oauth", "MODEL": "", "REASONING_EFFORT": "auto",
        }.get(key, default),
    )


def _result(model, history=None, error=""):
    messages = list(history or []) + [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "done"},
    ]
    return SimpleNamespace(
        answer="done" if not error else "", error=error, model=model, messages=messages,
        verified=False, total_tokens=5, turns=1, tool_calls=0, wall_ms=1, cost_usd=0,
    )


def test_shared_turn_policy_scales_and_structured_failure_escalates(monkeypatch):
    from harness.cli import resolve_turn_decision

    _settings_auto(monkeypatch)
    tiny = resolve_turn_decision(
        "Fix a typo in README.md", "codex-oauth", route_kind="code")
    hard = resolve_turn_decision(
        "Fix the security race condition and its regression test",
        "codex-oauth", route_kind="code")
    retry = resolve_turn_decision(
        "Fix the bug", "codex-oauth", route_kind="code",
        receipts=[{"error": "provider timeout"}])

    assert (tiny.provider, tiny.model, tiny.effort) == (
        "codex-oauth", "gpt-5.6-luna", "low")
    assert (hard.model, hard.effort, hard.verification) == (
        "gpt-5.6-sol", "high", "required")
    assert retry.model == "gpt-5.6-sol" and retry.complexity == "hard"


def test_explicit_model_pin_wins_without_crossing_provider(monkeypatch):
    from harness.cli import configured_model_for, resolve_turn_decision

    _settings_auto(monkeypatch)
    pinned = resolve_turn_decision(
        "Fix the security race condition", "codex-oauth",
        configured_model="gpt-5.6-luna", route_kind="code")

    assert pinned.provider == "codex-oauth"
    assert pinned.model == "gpt-5.6-luna"
    assert configured_model_for(
        "anthropic-oauth", None, provider_was_explicit=True) is None


def test_reused_harness_keeps_stores_and_resets_gate_between_turns(monkeypatch):
    from harness import cli
    from harness.gate import Gate, Mode

    _settings_auto(monkeypatch)

    class Provider:
        def __init__(self, name, model, effort="default", speed="standard"):
            self.name, self.model = name, model
            self.effort, self.speed = effort, speed
            self.actual_speed = speed

    monkeypatch.setattr(
        cli, "make_provider",
        lambda name, model, effort=None, speed="standard":
            Provider(name, model, effort or "default", speed),
    )
    memory, recorder = object(), object()
    gate = Gate(".", mode=Mode.PROJECT)
    h = SimpleNamespace(
        provider=Provider("codex-oauth", "gpt-5.6-terra"),
        memory=memory, recorder=recorder, gate=gate,
        mode="act", force_edit=False, self_verify=True, max_turns=50,
        verify_max=2, verify_gate=False, require_assert=False,
        _max_turns_hard_cap=None,
    )

    plan = cli.resolve_turn_decision("Explain this function", "codex-oauth")
    cli.apply_turn_decision(h, plan, gate)
    assert gate.mode is Mode.PLAN and h.mode == "plan"

    build = cli.resolve_turn_decision(
        "Fix the security race condition", "codex-oauth", route_kind="code")
    cli.apply_turn_decision(h, build, gate)
    assert gate.mode is Mode.PROJECT and h.mode == "act"
    assert h.provider.model == "gpt-5.6-sol" and h.provider.effort == "high"
    assert h.memory is memory and h.recorder is recorder


def test_repl_routes_each_turn_and_persists_decision_receipts(monkeypatch, tmp_path):
    import builtins
    from harness import cli, sessions

    _settings_auto(monkeypatch)
    seen, persisted = [], []

    class Provider:
        def __init__(self, model="gpt-5.6-terra", effort="default", speed="standard"):
            self.name, self.model = "codex-oauth", model
            self.effort, self.speed, self.actual_speed = effort, speed, speed

    class Harness:
        def __init__(self):
            self.provider = Provider()
            self.memory = self.recorder = SimpleNamespace(
                close=lambda: None, set_block=lambda *a, **k: None)
            self.gate = None
            self.mode = "act"; self.force_edit = False; self.self_verify = True
            self.max_turns = 50; self.verify_max = 2
            self.verify_gate = False; self.require_assert = False
            self._max_turns_hard_cap = None; self.checkpoint_scope = ""

        def run(self, _task_id, _text, consolidate=True, history=None):
            seen.append((self.provider.name, self.provider.model, self.provider.effort,
                         id(self.memory)))
            return _result(self.provider.model, history)

    monkeypatch.setattr(cli, "make_harness", lambda *a, **k: Harness())
    monkeypatch.setattr(
        cli, "make_provider",
        lambda _name, model, effort=None, speed="standard":
            Provider(model, effort or "default", speed),
    )
    monkeypatch.setattr(sessions, "new_id", lambda: "routed-repl")
    monkeypatch.setattr(sessions, "save", lambda *a, **k: "routed-repl")
    monkeypatch.setattr(sessions, "append_run_receipt", lambda _sid, row: persisted.append(row))
    inputs = iter((
        "Fix a typo in README.md",
        "Fix the security race condition and its regression test",
        "/exit",
    ))
    monkeypatch.setattr(builtins, "input", lambda *a, **k: next(inputs))
    args = SimpleNamespace(
        cwd=str(tmp_path), provider="codex-oauth", model=None, project="p",
        mode=None, resume=None, cont=False, goal=None,
    )

    assert cli.cmd_repl(args) == 0
    assert [row[1] for row in seen] == ["gpt-5.6-luna", "gpt-5.6-sol"]
    assert len({row[3] for row in seen}) == 1
    assert [row["decision"]["model"] for row in persisted] == [
        "gpt-5.6-luna", "gpt-5.6-sol"]


def test_plain_tui_routes_each_turn_and_keeps_one_harness(monkeypatch, tmp_path):
    from harness import cli, sessions, tui

    _settings_auto(monkeypatch)
    seen, persisted = [], []

    class Provider:
        def __init__(self, model="gpt-5.6-terra", effort="default", speed="standard"):
            self.name, self.model = "codex-oauth", model
            self.effort, self.speed, self.actual_speed = effort, speed, speed

    class Harness:
        def __init__(self):
            self.provider = Provider()
            self.memory = self.recorder = SimpleNamespace(
                close=lambda: None, set_block=lambda *a, **k: None)
            self.gate = None; self.emit = lambda *a: None
            self.mode = "act"; self.force_edit = False; self.self_verify = True
            self.max_turns = 50; self.verify_max = 2
            self.verify_gate = False; self.require_assert = False
            self._max_turns_hard_cap = None; self.checkpoint_scope = ""

        def run(self, _task_id, _text, consolidate=True, history=None):
            seen.append((self.provider.model, self.provider.effort, id(self.memory)))
            return _result(self.provider.model, history)

    harness = Harness()
    monkeypatch.setattr(cli, "make_harness", lambda *a, **k: harness)
    monkeypatch.setattr(
        cli, "make_provider",
        lambda _name, model, effort=None, speed="standard":
            Provider(model, effort or "default", speed),
    )
    monkeypatch.setattr(tui, "_HAVE_RICH", False)
    inputs = iter((
        "Fix a typo in README.md",
        "Fix the security race condition and its regression test",
        "/exit",
    ))
    monkeypatch.setattr(tui, "_read_line", lambda *_a, **_k: next(inputs))
    monkeypatch.setattr(sessions, "new_id", lambda: "routed-tui")
    monkeypatch.setattr(sessions, "save", lambda *a, **k: "routed-tui")
    monkeypatch.setattr(
        sessions, "append_run_receipt", lambda _sid, row: persisted.append(row))

    assert tui.run_tui(
        str(tmp_path), "codex-oauth", None, project="p") == 0
    assert [(row[0], row[1]) for row in seen] == [
        ("gpt-5.6-luna", "low"), ("gpt-5.6-sol", "high")]
    assert len({row[2] for row in seen}) == 1
    assert harness.checkpoint_scope == "session:routed-tui"
    assert [row["decision"]["model"] for row in persisted] == [
        "gpt-5.6-luna", "gpt-5.6-sol"]


def test_acp_resolves_each_prompt_inside_configured_provider(monkeypatch, tmp_path):
    pytest.importorskip("acp")
    from harness import acp_agent, cli

    _settings_auto(monkeypatch)
    made = []

    class Provider:
        def __init__(self, model, effort="default", speed="standard"):
            self.name, self.model = "codex-oauth", model
            self.effort, self.speed, self.actual_speed = effort, speed, speed

    class Harness:
        def __init__(self, provider):
            self.provider = provider
            self.memory = self.recorder = SimpleNamespace(close=lambda: None)
            self.gate = None; self.emit = lambda *a: None
            self.mode = "act"; self.force_edit = False; self.self_verify = True
            self.max_turns = 50; self.verify_max = 2
            self.verify_gate = False; self.require_assert = False
            self._max_turns_hard_cap = None

        def run(self, _task_id, _text, consolidate=False, history=None):
            self.emit("receipt", {
                "verified": False, "total_tokens": 5, "turns": 1,
                "tool_calls": 0, "wall_ms": 1, "cost_usd": 0, "error": "",
            })
            return _result(self.provider.model, history)

    def fake_make(*_a, **kw):
        made.append((kw["provider"], kw["model"], kw["effort"]))
        return Harness(Provider(kw["model"], kw["effort"], kw["speed"]))

    monkeypatch.setattr(cli, "make_harness", fake_make)
    monkeypatch.setattr(
        cli, "make_provider",
        lambda _name, model, effort=None, speed="standard":
            Provider(model, effort or "default", speed),
    )

    class Conn:
        def __init__(self): self.updates = []
        async def session_update(self, sid, update): self.updates.append((sid, update))

    async def exercise():
        agent = acp_agent.CollieAgent()
        agent.conn = Conn()
        sid = "acp-routed"
        agent.sessions[sid] = {"cwd": str(tmp_path)}
        await agent.prompt(sid, [SimpleNamespace(text="Fix a typo in README.md")])
        await agent.prompt(sid, [SimpleNamespace(
            text="Fix the security race condition and its regression test")])
        return agent

    agent = asyncio.run(exercise())
    assert made == [
        ("codex-oauth", "gpt-5.6-luna", "low"),
        ("codex-oauth", "gpt-5.6-sol", "high"),
    ]
    assert [row["decision"]["model"] for row in agent.sessions["acp-routed"]["run_receipts"]] == [
        "gpt-5.6-luna", "gpt-5.6-sol"]
