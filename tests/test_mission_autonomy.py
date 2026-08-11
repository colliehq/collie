import json
import time

import pytest

from harness.actions import ActionStore
from harness.jobs import (CANCELLED, Capability, DONE_VERIFIED, FAILED_S, NEEDS_YOU, PAUSED,
                          QUEUED, RECOVERY_REQUIRED, RUNNING, WAITING)
from harness.mission import (_model_case_json, MissionDriver, MissionStore,
                             ModelDecider, create_mission, world_leash)
from harness.missionweb import MissionService
from harness.providers import Completion, Usage
from harness.verifier import FAILED, INCONCLUSIVE, VERIFIED, Observation, Verdict


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


def test_reversible_uncertainty_does_not_stop_independent_branches(tmp_path):
    store, actions = _stores(tmp_path)
    seen = []

    def decide(_goal, case, _caps):
        seen.append(case)
        return ({"action": "inspect", "args": {}} if len(seen) == 1 else
                {"action": "needs_human", "args": {"summary": "other work finished"}})

    cap = Capability(
        "inspect", lambda _rec: {"result": "content without a bound page"},
        lambda _rec, _result: Verdict(INCONCLUSIVE, "page host was unavailable"),
        reversible=True, risk="read")
    create_mission(store, "uncertain-read", "continue other branches",
                   leash=world_leash(may=["inspect"], autonomous=True))
    state = MissionDriver(store, actions, decide, [cap]).advance("uncertain-read")

    assert state == NEEDS_YOU
    assert len(seen) == 2
    assert seen[1]["_recent_failures"][-1]["verdict"] == INCONCLUSIVE
    assert store.runtime("uncertain-read")["retry_count"] == 1
    store.close()
    actions.close()


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


def test_branch_wait_keeps_independent_work_running_then_wakes_due_check(tmp_path):
    store, actions = _stores(tmp_path)
    decisions = iter([
        {"action": "wait", "args": {"seconds": 3600, "branch": "linkedin outcome"},
         "reason": "re-check the submitted post later"},
        {"action": "research", "args": {}, "reason": "prepare another channel"},
        {"action": "done", "reason": "independent work is complete for now"},
        {"action": "inspect", "args": {"followup_branch": "linkedin outcome"},
         "reason": "check the due LinkedIn outcome"},
        {"action": "needs_human", "args": {"summary": "campaign review"}},
    ])
    caps = [
        Capability("research", lambda _r: {"case": {"researched": True}},
                   lambda _r, _v: Verdict(VERIFIED, "channel research saved"),
                   reversible=True, risk="read"),
        Capability("inspect", lambda _r: {"case": {"linkedin_checked": True}},
                   lambda _r, _v: Verdict(VERIFIED, "LinkedIn outcome checked"),
                   reversible=True, risk="read"),
    ]
    create_mission(store, "branches", "run a multi-channel campaign",
                   leash=world_leash(may=["research", "inspect"], autonomous=True))
    driver = MissionDriver(store, actions, lambda *_: next(decisions), caps)

    assert driver.advance("branches") == WAITING
    first = store.get("branches")
    assert first.case["researched"] is True
    assert first.case["pending_followups"][0]["branch"] == "linkedin outcome"
    assert any(x["status"] == "scheduled" for x in store.activity_ledger("branches"))

    assert driver.tick_missions(10**11) == 1
    resumed = store.get("branches")
    assert resumed.state == NEEDS_YOU
    assert resumed.case["linkedin_checked"] is True
    assert not resumed.case.get("_due_followups")
    assert resumed.case["resolved_followups"][-1]["verdict"] == VERIFIED
    ledger = store.activity_ledger("branches")
    assert any(x["status"] == "completed" and
               "LinkedIn outcome checked" in x["summary"] for x in ledger)
    store.close()
    actions.close()


def test_repeated_branch_wait_sleeps_instead_of_spinning(tmp_path):
    store, actions = _stores(tmp_path)
    wait = {"action": "wait", "args": {"seconds": 600, "branch": "reply inbox"},
            "reason": "no reply yet"}
    decisions = iter([wait, wait])
    create_mission(store, "wait-spin", "wait for one reply", leash=world_leash())
    driver = MissionDriver(store, actions, lambda *_: next(decisions), [])

    assert driver.advance("wait-spin") == WAITING
    assert store.next_wait("wait-spin") is not None
    scheduled = [e for e in store.events("wait-spin") if e["kind"] == "followup"]
    assert len(scheduled) == 2
    store.close()
    actions.close()


def test_activity_ledger_and_do_not_repeat_survive_context_compaction(tmp_path):
    store, actions = _stores(tmp_path)
    create_mission(store, "ledger", "remember completed work", leash=world_leash())
    store.record_event("ledger", "result", "browse.submit", "nonce-1",
                       {"verdict": VERIFIED, "reason": "post action fired"})
    with store._lock:
        store.db.execute(
            "INSERT INTO mission_action_keys(mission_id,action_key,nonce,state,at) "
            "VALUES(?,?,?,?,?)", ("ledger", "semantic-1", "nonce-1", VERIFIED, 7))
        store.db.execute(
            "INSERT INTO mission_steps(mission_id,name,nonce,verdict,at) VALUES(?,?,?,?,?)",
            ("ledger", "browse.submit", "nonce-1", VERIFIED, 7))
        store.db.commit()
    ledger = store.activity_ledger("ledger")
    protected = store.do_not_repeat("ledger")
    context = _model_case_json({"old": "x" * 30000, "_activity_ledger": ledger,
                                "_do_not_repeat": protected}, 2200)
    assert ledger[-1]["do_not_repeat"] is True
    assert protected[-1]["instruction"] == "Do not repeat this external action."
    assert "post action fired" in context and "Do not repeat" in context
    store.close()
    actions.close()


def test_full_priority_context_is_valid_json_after_total_budget_compaction():
    case = {
        "_authority": {"text": "a" * 3000},
        "_standing_authority": {"text": "b" * 3000},
        "_mission_summary": "c" * 3000,
        "human_updates": [{"note": "d" * 1000}] * 5,
        "pending_authorizations": [{"summary": "e" * 1000}] * 5,
        "resolved_authorizations": [{"summary": "f" * 1000}] * 5,
        "pending_followups": [{"summary": "g" * 1000}] * 5,
        "_due_followups": [{"summary": "h" * 1000}] * 5,
        "_activity_ledger": [{"summary": "i" * 1000}] * 20,
        "_do_not_repeat": [{"summary": "j" * 1000}] * 20,
        "browse_sites": {str(i): "k" * 1000 for i in range(20)},
        "_recent_results": [{"summary": "l" * 1000}] * 20,
        "_recent_events": [{"summary": "m" * 1000}] * 20,
        "_checkpoint": {"text": "n" * 1000},
    }
    encoded = _model_case_json(case, 12000)
    decoded = json.loads(encoded)
    assert len(encoded) <= 12000
    assert decoded["_context_truncated"] is True
    assert "_activity_ledger" in decoded and "_do_not_repeat" in decoded


def test_service_defaults_to_isolated_workspace_and_exposes_binding(tmp_path):
    service = MissionService(base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True)
    status = service.start("edit safely", may=["code"])
    mid = status["mission_id"]
    assert service.store.get(mid).leash["workspace_mode"] == "isolated"
    assert status["workspace_request"] is True
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    assert service.bind_workspace(mid, str(workspace))["workspace_request"] is False
