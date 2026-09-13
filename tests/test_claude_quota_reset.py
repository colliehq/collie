"""Quota metadata must schedule a durable wait, without repeated model requests."""
from dataclasses import dataclass
import time
from types import SimpleNamespace

import pytest

from harness.claude_agent_sdk import ClaudeAgentSdkProvider, _provider_rejection
from harness.providers import Completion, provider_retry_at
from test_claude_agent_sdk_provider_error import _init, _rejected_assistant, _result, _run
from test_request_accounting import _harness


def _event(reset, status="rejected", window="five_hour"):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "rateLimitType": window, "resetsAt": reset}}


def test_rejected_quota_window_crosses_worker_and_provider_boundaries():
    reset = int(time.time()) + 18000
    payload = _run([_init(), _event(reset), _rejected_assistant("rate_limit"), _result()])
    assert payload["retry_at"] == reset

    class Provider(ClaudeAgentSdkProvider):
        def _run_worker(self, *args, **kwargs):
            raise _provider_rejection(payload)

    provider = Provider(subscription_only=True)
    settled = []
    with provider.request_authority(lambda _: "one", lambda *args: settled.append(args)):
        completion = provider.complete("S", [{"role": "user", "content": "u"}], [])
    assert completion.error_status == 429 and completion.retry_at == reset
    assert completion.request_count == 1
    assert completion.usage.input_tokens == 1274
    assert len(settled) == 1


def test_sdk_dataclass_rate_limit_events_are_recognized():
    @dataclass
    class RateLimitEvent:
        rate_limit_info: object
    reset = int(time.time()) + 18000
    event = RateLimitEvent(SimpleNamespace(status="rejected", rate_limit_type="five_hour",
                                          resets_at=reset))
    payload = _run([_init(), event, _rejected_assistant("rate_limit"), _result()])
    assert payload["retry_at"] == reset


@pytest.mark.parametrize("late", [True, False])
def test_quota_receipt_outside_the_authenticated_turn_is_ignored(late):
    events = [_init(), _rejected_assistant("rate_limit"), _result()]
    event = _event(int(time.time()) + 18000)
    events.insert(len(events) if late else 0, event)
    assert "retry_at" not in _run(events)


@pytest.mark.parametrize("status,window,category", [
    ("allowed", "five_hour", "rate_limit"),
    ("allowed_warning", "five_hour", "rate_limit"),
    ("rejected", "overage", "rate_limit"),
    ("rejected", "unknown", "rate_limit"),
    ("rejected", "five_hour", "authentication_failed"),
    ("rejected", "five_hour", "billing_error"),
])
def test_warnings_unknown_windows_and_terminal_errors_do_not_schedule(status, window, category):
    payload = _run([_init(), _event(int(time.time()) + 18000, status, window),
                    _rejected_assistant(category), _result()])
    assert "retry_at" not in payload


def test_each_rejected_window_is_tracked_and_later_allowed_state_clears_it():
    now = int(time.time())
    events = [_init(), _event(now + 3600), _event(now + 86400, window="seven_day")]
    payload = _run(events + [_rejected_assistant("rate_limit"), _result()])
    assert payload["retry_at"] == now + 86400
    payload = _run(events + [_event(now + 86400, "allowed", "seven_day"),
                            _rejected_assistant("rate_limit"), _result()])
    assert payload["retry_at"] == now + 3600


@pytest.mark.parametrize("value", [None, True, False, "2000000300", 2000000300.0,
                                    -1, 0, 2000000000, 2000000000 + 9 * 86400])
def test_unusable_reset_metadata_is_not_a_timer(value):
    assert provider_retry_at(value, now=2000000000) == 0


def test_known_future_reset_does_not_spend_the_loop_retry_budget(tmp_path):
    reset = int(time.time()) + 18000
    class Provider:
        name, model, reports_cache = "mock", "mock", False
        calls = 0
        def complete(self, *args, **kwargs):
            self.calls += 1
            return Completion(stop_reason="error", error_status=429,
                              error_detail="rate limit", retry_at=reset)
    provider = Provider()
    harness = _harness(str(tmp_path), "quota", provider, turns=8)
    harness.max_retries = 4
    harness.retry_base = 0
    result = harness.run("quota", "finish the task", consolidate=False)
    assert provider.calls == result.model_calls == 1
    assert result.retry_at == reset
    assert "provider quota reset" in result.error


def test_planner_uses_the_reset_instead_of_exponential_polling():
    from harness.mission import ModelDecider
    reset = int(time.time()) + 18000
    provider = SimpleNamespace(complete=lambda *a: Completion(
        stop_reason="error", error_status=429, error_detail="rate limit", retry_at=reset))
    decision = ModelDecider(provider)("goal", {}, [])
    assert decision["action"] == "wait"
    assert 18000 <= decision["args"]["seconds"] <= 18003
    assert decision["args"]["transient"] is True


@pytest.mark.parametrize("deadline_seconds", [None, 3600])
def test_code_wait_survives_store_reopen_and_respects_the_task_deadline(tmp_path, deadline_seconds):
    from harness.actions import ActionStore
    from harness.jobs import Capability, WAITING
    from harness.mission import MissionDriver, MissionStore, create_mission, world_leash
    from harness.primitives import _code_verify, _real_code
    now = int(time.time())
    reset = now + 18000
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []
    def runner(*args, **kwargs):
        calls.append(True)
        return {"answer": "rate limit 429", "verified": False, "continue_needed": True,
                "transient": True, "retry_after_seconds": 60, "retry_at": reset,
                "session_id": "quota-session", "turns": 1, "model_calls": 1}
    store_path = str(tmp_path / "missions.db")
    store = MissionStore(store_path)
    actions = ActionStore(str(tmp_path / "actions.db"))
    leash = world_leash(may=["code"], autonomous=True, workspace_mode="isolated",
                        expires=now + deadline_seconds if deadline_seconds else None)
    create_mission(store, "quota", "wait and continue",
                   case={"_isolated_workspace": str(workspace), "code_profile": {}}, leash=leash)
    cap = Capability("code", execute=_real_code(runner), verify=_code_verify,
                     reversible=True, risk="read")
    driver = MissionDriver(store, actions,
                           lambda *args: {"action": "code", "args": {"goal": "fix"}}, [cap])
    assert driver.advance("quota") == WAITING
    assert calls == [True]
    expected = now + deadline_seconds if deadline_seconds else reset + 3
    assert store.next_wait("quota")["fire_at"] == expected
    store.close()
    actions.close()
    restored = MissionStore(store_path)
    assert restored.next_wait("quota")["fire_at"] == expected
    assert not restored.due_waits(now + 120)
    restored.close()


def test_automatic_wake_resumes_the_original_goal_only_after_reset(tmp_path):
    from harness.jobs import WAITING
    from harness.mission import create_mission, world_leash
    from test_mission_code_dispatch import GOAL, _dedicated_case, _driver
    now = int(time.time())
    reset = now + 18000
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []

    def runner(goal, **_context):
        seen.append(goal)
        return {"answer": "quota exhausted", "verified": False,
                "continue_needed": True, "transient": True,
                "retry_at": reset, "retry_after_seconds": 60,
                "session_id": "mission-code-dispatch-test", "turns": 1}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "resume-quota", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        assert driver.advance("resume-quota") == WAITING
        assert driver.wake("resume-quota", now=now + 60, force=False) == WAITING
        assert seen == [GOAL]
        driver.wake("resume-quota", now=reset + 3, force=False)
        assert seen == [GOAL, GOAL]
    finally:
        store.close()
        actions.close()


@pytest.mark.parametrize("mode", ["published", "absent", "stale"])
def test_refused_slices_wait_out_every_window_without_spending_no_progress(tmp_path, mode):
    """A slice the provider refused never ran, so it is not evidence of no progress.

    Three consecutive quota windows are three correctly scheduled waits, not
    three unproductive coding slices: consuming that budget ended the Mission
    with "no file change across 3 consecutive slices" after fifteen hours of
    waiting.  A transient stop with no published reset keeps consuming it,
    because that retry has no natural boundary to wait for.
    """
    from harness.jobs import NEEDS_YOU, WAITING
    from harness.mission import CODE_UNPRODUCTIVE_SLICES, create_mission, world_leash
    from test_mission_code_dispatch import GOAL, _dedicated_case, _driver
    now = int(time.time())
    reset = now + 18000
    retry_at = {"published": reset, "absent": 0, "stale": now - 1}[mode]
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []

    def runner(goal, **_context):
        seen.append(goal)
        return {"answer": "HTTP 429: Claude Code rate limit; subscription resets",
                "error": "HTTP 429: Claude Code rate limit", "verified": False,
                "continue_needed": True, "transient": True,
                "retry_at": retry_at, "retry_after_seconds": 60,
                "session_id": "mission-code-dispatch-test", "turns": 1,
                "slice_mutated": False, "patch_attributed": False}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "windows", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        states = [driver.advance("windows")]
        for _ in range(2 * CODE_UNPRODUCTIVE_SLICES):
            if states[-1] != WAITING:
                break
            states.append(driver.wake("windows", now=reset + 3, force=False))
        case = store.get("windows").case
        delivery = case["code_delivery"]
        assert delivery["transient"] is True
        # Validated at the slice boundary: a stale epoch is not a timer to wait on.
        assert delivery["quota_reset_at"] == (reset if mode == "published" else 0)
        if mode == "published":
            # Still waiting on the provider's own reset, still resuming the user's
            # own goal, and never asking a person to do something about a quota.
            assert states == [WAITING] * (2 * CODE_UNPRODUCTIVE_SLICES + 1)
            assert seen == [GOAL] * len(states)
            assert store.next_wait("windows")["fire_at"] == reset + 3
            assert case["code_dispatch"]["unproductive"] == 0
            assert case["code_dispatch"]["attempts"] == len(states)
        else:
            assert states[-1] == NEEDS_YOU
            assert "no file change across" in store.get("windows").result
            assert len(seen) <= CODE_UNPRODUCTIVE_SLICES + 1
    finally:
        store.close()
        actions.close()
