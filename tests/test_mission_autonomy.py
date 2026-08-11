import json
import time

import pytest

from harness.actions import ActionStore
from harness.jobs import (CANCELLED, Capability, DONE_VERIFIED, FAILED_S, NEEDS_YOU, PAUSED,
                          QUEUED, RECOVERY_REQUIRED, RUNNING, WAITING)
from harness.mission import (MissionDriver, MissionStore, ModelDecider, create_mission,
                             world_leash)
from harness.missionweb import MissionService
from harness.providers import Completion, Usage
from harness.verifier import FAILED, VERIFIED, Observation, Verdict


def _stores(tmp_path):
    return (MissionStore(str(tmp_path / "missions.db")),
            ActionStore(str(tmp_path / "actions.db")))


def test_specialist_creation_cannot_race_past_parent_cancellation(tmp_path):
    store, actions = _stores(tmp_path)
    create_mission(store, "parent", "parent", leash=world_leash())
    assert store.cancel("parent")
    with pytest.raises(ValueError, match="parent Mission is stopping or terminal"):
        create_mission(
            store, "late-child", "must never run",
            case={"_parent_mission_id": "parent"}, leash=world_leash(),
            lane="specialist", external_run_id="run_late")
    assert store.get("parent").state == CANCELLED
    assert store.get("late-child") is None
    store.close()
    actions.close()


def test_hung_decider_is_bounded_and_heartbeat_cannot_fake_progress(tmp_path):
    store, actions = _stores(tmp_path)

    def hung(*_):
        time.sleep(.3)
        return {"action": "needs_human"}

    leash = world_leash()
    leash["max_step_seconds"] = .05
    create_mission(store, "hung", "wait safely", leash=leash)
    started = time.monotonic()
    state = MissionDriver(store, actions, hung, capabilities=[]).advance("hung")
    assert time.monotonic() - started < .2
    assert state == WAITING
    assert store.runtime("hung")["retry_count"] == 1
    assert store.events("hung")[-1]["name"] == "decider_timeout"


def test_hung_action_is_fenced_and_late_worker_cannot_fold(tmp_path):
    store, actions = _stores(tmp_path)

    def execute(_rec):
        time.sleep(.2)
        return {"case": {"late_write": True}}

    cap = Capability("slow", execute, lambda _r, _v: Verdict(VERIFIED, "ok"),
                     reversible=True, risk="read")
    leash = world_leash(may=["slow"], autonomous=True)
    leash["max_step_seconds"] = .05
    create_mission(store, "slow", "do it", leash=leash)
    decider = lambda *_: {"action": "slow", "args": {}}
    state = MissionDriver(store, actions, decider, [cap]).advance("slow")
    assert state == RECOVERY_REQUIRED
    time.sleep(.3)
    assert store.get("slow").state == RECOVERY_REQUIRED
    assert "late_write" not in store.get("slow").case
    assert actions.receipts() and actions.receipts()[0]["fired"] == 1


def test_reversible_failure_returns_to_planner_with_diagnostic(tmp_path):
    store, actions = _stores(tmp_path)
    attempts = []

    def decide(_goal, case, _caps):
        attempts.append(case)
        return ({"action": "inspect", "args": {}} if len(attempts) == 1 else
                {"action": "needs_human", "args": {"summary": "repaired next step"}})

    cap = Capability("inspect", lambda _rec: {"result": "button disabled"},
                     lambda _rec, _result: Verdict(FAILED, "final action disabled"),
                     reversible=True, risk="read")
    create_mission(store, "repair", "recover autonomously",
                   leash=world_leash(may=["inspect"], autonomous=True))
    state = MissionDriver(store, actions, decide, [cap]).advance("repair")
    assert state == NEEDS_YOU
    assert attempts[1]["_recent_failures"][-1]["reason"] == "final action disabled"
    assert store.runtime("repair")["retry_count"] == 1


def test_phase_aware_crash_recovery(tmp_path):
    store, _actions = _stores(tmp_path)
    create_mission(store, "safe", "model only", leash=world_leash())
    token = store.claim_run("safe", lease_s=0)
    store.record_checkpoint("safe", token, "deciding")
    assert store.recover_stale_runs(int(time.time()) + 1) == 1
    assert store.get("safe").state == QUEUED

    create_mission(store, "uncertain", "external", leash=world_leash())
    token = store.claim_run("uncertain", lease_s=0)
    store.record_checkpoint("uncertain", token, "executing")
    assert store.recover_stale_runs(int(time.time()) + 1) == 1
    assert store.get("uncertain").state == RECOVERY_REQUIRED


def test_checkpoint_and_budget_survive_restart(tmp_path):
    path = str(tmp_path / "missions.db")
    store = MissionStore(path)
    leash = world_leash(max_model_tokens=5)
    create_mission(store, "budget", "bounded", leash=leash)
    token = store.claim_run("budget")
    store.record_checkpoint("budget", token, "deciding", {"turn": 1})
    store.account_runtime("budget", token, input_tokens=5, cost_usd=.25, wall_ms=7)
    store.close()

    reopened = MissionStore(path)
    assert reopened.latest_checkpoint("budget")["phase"] == "deciding"
    assert reopened.runtime("budget")["input_tokens"] == 5
    assert reopened.budget_reason("budget") == "mission model-token budget exhausted"


def test_goal_verifier_is_only_path_to_done_verified(tmp_path):
    store, actions = _stores(tmp_path)
    create_mission(store, "verified", "prove it", leash=world_leash())
    driver = MissionDriver(store, actions, lambda *_: {"action": "done"}, [],
                           goal_verifier=lambda *_: Verdict(
                               VERIFIED, "targeted contract passed", (
                                   Observation("independent-check", time.time(), True,
                                               asserted=True, detail="expected state observed"),)))
    assert driver.advance("verified") == DONE_VERIFIED
    event = store.events("verified")[-1]
    assert event["payload"]["evidence"][0]["channel"] == "independent-check"

    create_mission(store, "failed", "prove it", leash=world_leash())
    driver = MissionDriver(store, actions, lambda *_: {"action": "done"}, [],
                           goal_verifier=lambda *_: Verdict(FAILED, "refuted"))
    assert driver.advance("failed") == FAILED_S

    create_mission(store, "unverified", "prove it", leash=world_leash())
    assert MissionDriver(store, actions, lambda *_: {"action": "done"}, []).advance(
        "unverified") == NEEDS_YOU

    create_mission(store, "unscoped", "prove it", leash=world_leash())
    unscoped = MissionDriver(
        store, actions, lambda *_: {"action": "done"}, [],
        goal_verifier=lambda *_: Verdict(VERIFIED, "trust me"))
    assert unscoped.advance("unscoped") == NEEDS_YOU
    assert "without scoped independent evidence" in store.get("unscoped").result


def test_human_wait_escalates_then_pauses_and_resumes_exact_state(tmp_path):
    store, actions = _stores(tmp_path)
    leash = world_leash(human_escalate_seconds=1, human_timeout_seconds=2)
    create_mission(store, "human", "ask", leash=leash)
    driver = MissionDriver(
        store, actions,
        lambda *_: {"action": "needs_human", "args": {"summary": "choose"}}, [])
    assert driver.advance("human") == NEEDS_YOU
    runtime = store.runtime("human")
    one = store.escalate_human_waits(runtime["human_escalate_at"])
    assert one == [{"mission_id": "human", "level": 1, "state": NEEDS_YOU,
                    "reason": "human response overdue"}]
    two = store.escalate_human_waits(runtime["human_deadline_at"])
    assert two[0]["level"] == 2 and store.get("human").state == PAUSED
    assert store.resume_paused("human") == NEEDS_YOU


def test_parallel_tick_does_not_let_hung_mission_starve_fast_one(tmp_path):
    store, actions = _stores(tmp_path)
    leash = world_leash()
    leash["max_step_seconds"] = .05
    create_mission(store, "hung", "hang", leash=leash)
    create_mission(store, "fast", "fast", leash=leash)

    def decide(goal, *_):
        if goal == "hang":
            time.sleep(.8)
        return {"action": "needs_human", "args": {"summary": goal}}

    driver = MissionDriver(store, actions, decide, [])
    started = time.monotonic()
    assert driver.tick_missions(max_workers=2, max_batch=2) == 2
    assert time.monotonic() - started < .5
    assert store.get("hung").state == WAITING
    assert store.get("fast").state == NEEDS_YOU


def test_recent_context_survives_large_old_case():
    class Provider:
        model = "mock"

        def __init__(self):
            self.prompt = ""

        def complete(self, _system, messages, _tools):
            self.prompt = messages[0]["content"]
            return Completion(text=json.dumps({"action": "needs_human"}),
                              usage=Usage(input_tokens=3, output_tokens=2))

    provider = Provider()
    decision = ModelDecider(provider)(
        "goal", {"old": "x" * 30000,
                 "_recent_events": [{"kind": "result", "name": "newest-marker"}],
                 "_mission_summary": "durable summary"}, [])
    assert decision["action"] == "needs_human"
    assert "newest-marker" in provider.prompt and "durable summary" in provider.prompt
    assert decision["_usage"]["input_tokens"] == 3


def test_service_defaults_to_isolated_workspace_and_exposes_binding(tmp_path):
    service = MissionService(base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True)
    status = service.start("edit safely", may=["code"])
    mid = status["mission_id"]
    assert service.store.get(mid).leash["workspace_mode"] == "isolated"
    assert status["workspace_request"] is True
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    assert service.bind_workspace(mid, str(workspace))["workspace_request"] is False
