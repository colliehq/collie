"""What bounds an unattended automation: the budget it was accepted with, and nothing else.

An automation is supposed to work until the task is done or one of the budgets a person can
see runs out.  A turn ceiling is neither: nothing meters it, nothing reports it in advance,
and a run that hits it stops mid-task with wall, tokens, cost and actions largely unspent.
So `max_turns` is now allowed to be 0 ("no hard turn ceiling") and defaults there for new
specs, while every budget that actually measures consumption stays mandatory and positive.

These cases drive the real child-process body (`_run_collie_request`) in-process against the
real `Harness`, exactly like `test_automation_outcomes`, with the same environment ceilings
`DefaultCollieRunner` exports.  The mock model calls one tool per turn and each turn depends
on the previous one, so only a host ceiling can stop it early; nothing writes to the
workspace and no provider is contacted.
"""
import pytest

from harness import automations, providers
from harness.automations import AutomationSpec, NEEDS_YOU, SUCCEEDED

from test_automation_outcomes import _isolate, _loop_forever, _request


def _spec(**budget):
    return {"automation_id": "limits", "task": "keep working",
            "trigger": {"provider": "timer", "every_s": 3600}, "budget": budget}


def _finishes_after(monkeypatch, tool_turns, answer="every TODO is listed"):
    """A model that needs `tool_turns` dependent tool turns before it can answer."""
    from harness import cli

    real = cli.make_harness
    state = {"turns": 0}

    def patched(*args, **kwargs):
        harness = real(*args, **kwargs)

        def complete(system, messages, tool_schemas, on_text=None):
            usage = providers.Usage(input_tokens=10, output_tokens=5)
            if state["turns"] >= tool_turns:
                return providers.Completion(text=answer, usage=usage, stop_reason="end_turn")
            state["turns"] += 1
            return providers.Completion(
                tool_calls=[providers.ToolCall("call-%d" % state["turns"], "grep",
                                               {"pattern": "TODO", "path": "."})],
                usage=usage, stop_reason="tool_use")

        harness.provider.complete = complete
        return harness

    monkeypatch.setattr(cli, "make_harness", patched)
    return state


def _long_budget(max_turns, tool_turns):
    """The budget exactly as the store accepted it, so validation and runtime cannot disagree."""
    return AutomationSpec.from_dict(_spec(
        max_turns=max_turns, max_actions=tool_turns + 10, max_wall_s=600,
        max_model_tokens=200000)).budget


def test_a_zero_turn_budget_lets_the_work_run_past_the_old_fifty_turn_ceiling(
        tmp_path, monkeypatch):
    """The visible budgets bound the run; 55 turns of real work is not a stopping condition."""
    _isolate(tmp_path, monkeypatch, COLLIE_MAX_TURNS=0)
    state = _finishes_after(monkeypatch, 55)
    value = automations._run_collie_request(
        _request(tmp_path, budget=_long_budget(0, 55)))

    assert state["turns"] == 55, "the model must have been allowed all of its tool turns"
    assert value["stop_reason"] == "completed", value
    assert value["status"] == SUCCEEDED, value
    assert "every TODO is listed" in value["summary"], value


def test_an_explicit_turn_budget_still_stops_the_run_when_the_ambient_cap_is_unlimited(
        tmp_path, monkeypatch):
    """0 means unlimited on BOTH sides, so the old min() discarded the automation's own cap."""
    _isolate(tmp_path, monkeypatch)             # no COLLIE_MAX_TURNS at all: ambient unlimited
    _loop_forever(monkeypatch)
    value = automations._run_collie_request(
        _request(tmp_path, budget={"max_turns": 3, "max_actions": 50}))

    assert value["stop_reason"] == "turn_limit", value
    assert value["status"] == NEEDS_YOU, value
    assert value["tool_calls"] == 3, value


def test_an_explicit_turn_budget_above_the_settings_range_is_honored_not_clamped(
        tmp_path, monkeypatch):
    """The Settings panel's 120 is an interactive UI range, not a ceiling on an accepted budget."""
    _isolate(tmp_path, monkeypatch, COLLIE_MAX_TURNS=400)
    state = _finishes_after(monkeypatch, 125)
    value = automations._run_collie_request(
        _request(tmp_path, budget=_long_budget(400, 125)))

    assert state["turns"] == 125, "a 400-turn budget must not be reduced to 120"
    assert value["stop_reason"] == "completed", value
    assert value["status"] == SUCCEEDED, value


def test_only_turns_and_retries_may_be_zero():
    """Unbounded turns are safe only because everything that meters consumption stays positive."""
    assert AutomationSpec.from_dict(_spec(max_turns=0)).budget["max_turns"] == 0
    assert AutomationSpec.from_dict(_spec(max_retries=0)).budget["max_retries"] == 0
    for key in ("max_wall_s", "max_model_tokens", "max_cost_usd", "max_actions",
                "max_runs_per_day"):
        with pytest.raises(ValueError, match=key):
            AutomationSpec.from_dict(_spec(**{key: 0}))
        with pytest.raises(ValueError, match=key):
            AutomationSpec.from_dict(_spec(**{key: -1}))
    with pytest.raises(ValueError, match="max_turns"):
        AutomationSpec.from_dict(_spec(max_turns=-1))


def test_a_new_spec_defaults_to_no_hard_turn_ceiling_and_positive_everything_else():
    budget = AutomationSpec.from_dict(_spec()).budget

    assert budget["max_turns"] == 0, "a new automation is bounded by its visible budgets"
    for key in ("max_wall_s", "max_model_tokens", "max_cost_usd", "max_actions",
                "max_runs_per_day"):
        assert budget[key] > 0, key


def test_an_existing_explicit_turn_cap_is_kept_exactly_as_written():
    """Stored 50s are indistinguishable from deliberate choices, so nothing migrates them."""
    assert AutomationSpec.from_dict(_spec(max_turns=50)).budget["max_turns"] == 50
    assert AutomationSpec.from_dict(_spec(max_turns=400)).budget["max_turns"] == 400
