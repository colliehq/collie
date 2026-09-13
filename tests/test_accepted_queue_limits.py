"""A queued request keeps the budget it was accepted under, wherever it is started.

The Web queue freezes MAX_COST / MAX_TOTAL_TOKENS / MAX_TURNS / MAX_TOKENS /
TEMPERATURE at acceptance and replays that snapshot when the request's turn comes.
The terminal is the other door onto the same durable queue: ``/next`` in ``collie
repl`` / ``collie tui`` claims the very same entry.  It must be held to the same
numbers, in both directions — a cap raised while the request waited must not let it
spend past what was authorized, and a cap lowered while it waited must not abort the
answer the person is waiting for.

Offline throughout: the mock provider, a temp sessions root and a temp settings file.
"""
import json
import os

import pytest

from harness import (cli, run_ownership, sessions, settings, task_inbox,
                     terminal_queue, tui, web_tasks)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated sessions root, data dir and settings file, like one installation."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(directory))
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "_cache", {"mtime": -1.0, "data": {}})
    monkeypatch.setattr(settings, "_HARD_ENV", set())
    for key in settings.LIMIT_KEYS:
        monkeypatch.delenv("COLLIE_" + key, raising=False)
    return str(directory)


def _panel(**values):
    """Write the Settings panel file, as a save from another tab would."""
    with open(settings._PATH, "w", encoding="utf-8") as f:
        json.dump(values, f)
    settings._cache["mtime"] = -1.0


def _accept(sid, entry_id, text, *, cwd, config=None):
    """Accept one request exactly as the web surface does: frozen settings and all."""
    frozen = web_tasks.freeze_config(
        dict(config or {}), provider="mock", model="",
        limits=settings.freeze_limits())
    sessions.save(sid, [{"role": "user", "content": "earlier turn"}], cwd=cwd)
    return task_inbox.enqueue(sid, entry_id, text, mode="follow_up", config=frozen)


def _args(sid, cwd):
    from types import SimpleNamespace
    return SimpleNamespace(cwd=cwd, provider="mock", model=None, project="limits",
                           mode=None, persona=None, goal=None, resume=sid, cont=False,
                           task=None, json=False, print=False, stream_json=False)


def _drive(monkeypatch, tmp_path, surface, lines, *, resume, capture, before_run=None):
    """Run one interactive surface over scripted input, keeping every RunResult."""
    typed = iter(lines)
    real_make = cli.make_harness

    def make(cwd, **kw):
        h = real_make(cwd, **dict(kw, embed="bm25"))
        original = h.run

        def run(*a, **kwargs):
            if before_run is not None:
                before_run(h)
            res = original(*a, **kwargs)
            capture.append(res)
            return res

        h.run = run
        return h

    monkeypatch.setattr(cli, "make_harness", make)
    monkeypatch.setattr(cli, "default_gate", lambda *a, **k: None)
    if surface == "tui":
        monkeypatch.setattr(tui, "_HAVE_RICH", False)
        monkeypatch.setattr(tui, "make_harness", make, raising=False)
        monkeypatch.setattr(tui, "_read_line", lambda *a, **k: next(typed))
        return tui.run_tui(str(tmp_path), "mock", None, project="limits", resume=resume)
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(typed))
    return cli.cmd_repl(_args(resume, str(tmp_path)))


@pytest.mark.parametrize("surface", ["repl", "tui"])
def test_next_runs_a_queued_request_under_the_budget_it_was_accepted_with(
        store, tmp_path, monkeypatch, surface):
    """A cap lowered while the request waited must not abort it."""
    sid = "queued-budget-" + surface
    _panel()                                     # accepted with no ceiling at all
    _accept(sid, "wide", "summarize the plan", cwd=str(tmp_path))
    _panel(MAX_TOTAL_TOKENS="1", MAX_TURNS="1")  # ... then another tab tightens both
    results = []
    assert _drive(monkeypatch, tmp_path, surface, ["/next", "/exit"],
                  resume=sid, capture=results) == 0
    assert len(results) == 1, "the queued request ran exactly once"
    res = results[0]
    assert res.budget_limits["MAX_TOTAL_TOKENS"] == "0", res.budget_limits
    assert res.budget_limits["MAX_TURNS"] == "0", res.budget_limits
    assert res.budget_limits["source"] == "frozen", res.budget_limits
    assert not res.budget_exhausted, res.answer
    assert not res.turns_exhausted, res.answer
    assert "budget ceiling reached" not in (res.answer or "")
    assert task_inbox.get(sid, "wide")["state"] == "consumed"


@pytest.mark.parametrize("surface", ["repl", "tui"])
def test_next_cannot_spend_past_the_ceiling_a_queued_request_was_accepted_with(
        store, tmp_path, monkeypatch, surface):
    """And a cap raised while it waited must not loosen what was authorized."""
    sid = "queued-ceiling-" + surface
    _panel(MAX_TOTAL_TOKENS="1")                 # accepted under a hard ceiling
    _accept(sid, "tight", "summarize the plan", cwd=str(tmp_path))
    _panel()                                     # ... then the ceiling is removed
    results = []
    assert _drive(monkeypatch, tmp_path, surface, ["/next", "/exit"],
                  resume=sid, capture=results) == 0
    res = results[0]
    assert res.budget_limits["MAX_TOTAL_TOKENS"] == "1", res.budget_limits
    assert res.budget_limits["source"] == "frozen", res.budget_limits
    assert res.budget_exhausted, res.answer


def test_a_line_typed_at_the_prompt_still_uses_the_current_settings(
        store, tmp_path, monkeypatch):
    """Only a queued request replays a snapshot; typed input is measured now."""
    sid = "typed-now"
    _panel(MAX_TURNS="1")
    sessions.save(sid, [{"role": "user", "content": "earlier turn"}], cwd=str(tmp_path))
    results = []
    assert _drive(monkeypatch, tmp_path, "repl", ["do the thing", "/exit"],
                  resume=sid, capture=results) == 0
    limits = results[0].budget_limits
    assert limits["source"] == "current" and limits["MAX_TURNS"] == "1", limits
    assert results[0].turns_exhausted, "an explicit turn cap still binds a typed turn"


def test_an_accepted_ceiling_does_not_leak_into_the_next_typed_turn(
        store, tmp_path, monkeypatch):
    """One terminal Harness serves many turns, so a replayed snapshot must end with its run."""
    sid = "no-leak"
    _panel(MAX_TURNS="1")                        # the queued request is accepted capped
    _accept(sid, "capped", "summarize the plan", cwd=str(tmp_path))
    _panel()                                     # the person then removes the cap
    results = []
    assert _drive(monkeypatch, tmp_path, "repl", ["/next", "and now this", "/exit"],
                  resume=sid, capture=results) == 0
    queued, typed = results
    assert queued.budget_limits["source"] == "frozen" and queued.turns_exhausted
    assert typed.budget_limits["source"] == "current", typed.budget_limits
    assert typed.budget_limits["MAX_TURNS"] == "0", typed.budget_limits
    assert not typed.turns_exhausted, typed.answer


def test_a_frozen_budget_this_build_cannot_read_keeps_the_request_pending(store, tmp_path):
    """A snapshot that will not replay is a refusal, never a guessed ceiling."""
    sid = "unreplayable"
    _panel(MAX_TOTAL_TOKENS="2000")
    entry = _accept(sid, "broken", "do it", cwd=str(tmp_path))
    config = dict(entry["config"])
    config["frozen"] = dict(config["frozen"])
    config["frozen"]["limits"] = dict(config["frozen"]["limits"], digest="not-the-digest")
    task_inbox.enqueue(sid, "broken2", "do it", mode="follow_up", config=config)
    task_inbox.cancel(sid, "broken", reason="replaced by the tampered copy")
    with run_ownership.hold(sid, label="test") as lease:
        with pytest.raises(terminal_queue.QueueError) as raised:
            with terminal_queue.claimed_next(sid, lease, requested=True):
                pass
    assert "cannot replay" in str(raised.value)
    assert task_inbox.get(sid, "broken2")["state"] == "pending"
    assert task_inbox.get(sid, "broken2")["attempts"] == 0


@pytest.mark.parametrize("surface", ["repl", "tui"])
def test_next_turn_releases_the_previous_requests_generation_and_turn_limits(
        store, tmp_path, monkeypatch, surface):
    """Exercise queued -> typed transitions on the same live terminal harness."""
    from harness.providers import MockProvider

    class GenerationMock(MockProvider):
        default_max_tokens = max_tokens = 4096
        default_temperature = temperature = 0.2

    monkeypatch.setattr(cli, "make_provider", lambda *a, **kw: GenerationMock())
    sid = "generation-transition-" + surface
    _panel(MAX_TOKENS="64", TEMPERATURE="0", MAX_TURNS="2")
    _accept(sid, "first", "summarize the plan", cwd=str(tmp_path))
    _panel(MAX_TOKENS="128", TEMPERATURE="0.9", MAX_TURNS="4")
    environment = {key: os.environ.get("COLLIE_" + key) for key in settings.LIMIT_KEYS}
    results, observed = [], []

    def inspect(h):
        observed.append((h.provider.max_tokens, h.provider.temperature, h.max_turns,
                         h.limits.source if h.limits is not None else None))

    assert _drive(monkeypatch, tmp_path, surface, ["/next", "summarize again", "/exit"],
                  resume=sid, capture=results, before_run=inspect) == 0
    assert observed == [(64, 0.0, 2, "frozen"), (128, 0.9, 4, None)]
    assert [r.budget_limits["source"] for r in results] == ["frozen", "current"]
    assert environment == {key: os.environ.get("COLLIE_" + key) for key in settings.LIMIT_KEYS}


@pytest.mark.parametrize("surface", ["repl", "tui"])
def test_explicit_environment_ceiling_still_binds_a_queued_request(
        store, tmp_path, monkeypatch, surface):
    sid = "pinned-ceiling-" + surface
    _panel(MAX_TOTAL_TOKENS="100000")
    _accept(sid, "first", "summarize the plan", cwd=str(tmp_path))
    monkeypatch.setenv("COLLIE_MAX_TOTAL_TOKENS", "1")
    monkeypatch.setattr(settings, "_HARD_ENV", {"COLLIE_MAX_TOTAL_TOKENS"})
    results = []
    assert _drive(monkeypatch, tmp_path, surface, ["/next", "/exit"],
                  resume=sid, capture=results) == 0
    assert results[0].budget_limits["MAX_TOTAL_TOKENS"] == "1"
    assert results[0].budget_exhausted
