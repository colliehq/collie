"""A dedicated code Mission runs the user's own goal, and says what it did.

The failure these pin down is a real one.  A Mission created with `code=True`, a
bound workspace and an explicit verify command asked a planner model for a "next
step"; the planner replied "read the files and report their contents, do not
modify anything", the native coding loop obeyed exactly, and the Mission stopped
with "code edited but not executed-verified" after editing nothing at all.

Three separate things had to be true for that outcome, and each has a test here:
the goal was replaced by a paraphrase, the mutation facts were dropped between
the code worker and its done-check, and the final sentence was written without
looking at either.
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import pytest

from harness import sessions
from harness.actions import ActionStore
from harness.jobs import Capability, DONE_VERIFIED, NEEDS_YOU, WAITING
from harness.mission import (MissionDriver, MissionStore, code_mission_goal,
                             code_mission_profile, code_stop_report,
                             create_mission, world_leash)
from harness.missionweb import MissionService
from harness.primitives import _code_verify, _live_code, _real_code
from harness.recorder import RunResult
from harness.verifier import (CodeWorkspaceGoalVerifier, MissionGoalVerifier,
                              INCONCLUSIVE, VERIFIED)


# The exact bytes the real failing run was given, kept verbatim: this test is
# also the regression for "the complete UTF-8 goal survives dispatch".
GOAL = (
    "只在当前项目内修改 ledger.py、test_ledger.py 和 README.md："
    "添加可重复指定的 --category NAME 参数，精确区分大小写地筛选这些类别，"
    "多个参数取并集，重复指定同一类别不得重复累加。"
    "与 --since、--total、--json 正确组合；没有匹配结果时沿用现有的空结果约定。"
    "先阅读现有代码，再实现并运行现有及新增测试。"
)


class _Closer:
    def close(self):
        pass


class _Recorder(_Closer):
    def finish_run(self, _result):
        pass


class _FakeHarness:
    """Just enough of loop.Harness for the code slice's own bookkeeping."""

    def __init__(self, result, seen):
        self._result = result
        self._seen = seen
        self.registry = SimpleNamespace(_tools={})
        self.provider = SimpleNamespace(subscription_only=False)
        self.memory = _Closer()
        self.recorder = _Recorder()
        self.max_turns = 0
        self.self_verify = True
        self.durable_session_id = ""
        self.checkpoint_scope = ""

    def run(self, task_id, prompt, history=None):
        self._seen.append({"task_id": task_id, "prompt": prompt,
                           "history": history,
                           "run_owner": getattr(self, "run_owner", None),
                           "max_turns": self.max_turns,
                           "max_model_calls": getattr(self, "max_model_calls", None),
                           "tools": sorted(self.registry._tools)})
        return self._result

    def settle_run_memory(self, *_args, **_kwargs):
        pass


def _run_result(**kwargs):
    fields = {"task_id": "code", "answer": "", "messages": [], "turns": 1,
              "input_tokens": 3, "output_tokens": 2, "cache_read": 0,
              "cache_creation": 0, "cost_usd": 0.0}
    fields.update(kwargs)
    return RunResult(**fields)


def _dedicated_case(workspace, verify_command="python -m unittest -q"):
    return {
        "_isolated_workspace": str(workspace),
        "code_profile": {
            "durable": True, "direct_dispatch": True, "overnight": False,
            "verify_command": verify_command, "slice_turns": 0,
            "verify_timeout_seconds": 300, "max_session_storage_bytes": 0,
            "session_id": "mission-code-dispatch-test",
        },
    }


def _driver(tmp_path, runner, decider=None, goal_verifier=None):
    store = MissionStore(str(tmp_path / "missions.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))

    def refuse(*_args, **_kwargs):
        raise AssertionError("a dedicated code Mission spent a planner call")

    cap = Capability("code", execute=_real_code(runner), verify=_code_verify,
                     reversible=True, risk="code")
    driver = MissionDriver(
        store, actions, decider or refuse, [cap],
        goal_verifier=(goal_verifier if goal_verifier is not None
                       else MissionGoalVerifier(store, actions)))
    return store, actions, driver


# ── the goal itself ─────────────────────────────────────────────────────────
def test_first_dispatch_carries_the_complete_goal_and_spends_no_planner_call(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []

    def runner(goal, *, workspace=None, **_context):
        seen.append((goal, workspace))
        return {"answer": "read the files", "verified": False,
                "slice_mutated": False, "patch_attributed": False,
                "session_id": "mission-code-dispatch-test", "turns": 20,
                "model_calls": 20, "_model_calls_reserved": True,
                "verification": {"verified": False, "detail":
                                 "check passed but the Mission produced no attributed patch",
                                 "evidence": {"command": "python -m unittest -q",
                                              "passed": True, "command_passed": True,
                                              "executed": True, "exit_code": 0,
                                              "patch_attributed": False,
                                              "output": "Ran 49 tests\n\nOK\n"}}}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "code-goal", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code", "compose", "observe"],
                                         autonomous=True, workspace_mode="isolated"))
        driver.advance("code-goal")

        assert len(seen) == 1
        dispatched_goal, dispatched_workspace = seen[0]
        # Verbatim: not summarized, not translated, not narrowed to a survey.
        assert dispatched_goal == GOAL
        assert os.path.realpath(dispatched_workspace) == os.path.realpath(str(workspace))
        # No model transport was used to choose this step, so no model call is
        # charged for one.  The slice's own 20 calls were reserved by the worker.
        runtime = store.runtime("code-goal")
        assert runtime["model_calls"] == 0
        # One deterministic step, plus the coding loop's own twenty turns.
        assert runtime["turns"] == 21
        kinds = [(e["kind"], e["name"]) for e in store.events("code-goal", 40)]
        assert ("planning_turn", "code_dispatch") in kinds
        assert not [k for k in kinds if k[0] == "decision"]
    finally:
        store.close()
        actions.close()


def test_mixed_work_mission_still_asks_the_planner(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    asked = []

    def decider(goal, case, _capabilities):
        asked.append(goal)
        return {"action": "needs_human", "args": {"summary": "planner ran"}}

    store, actions, driver = _driver(
        tmp_path, lambda *_a, **_k: {"answer": "unused"}, decider=decider)
    try:
        # `code` is allowed, but this Mission was not created as one workspace's
        # coding job, so choosing the next primitive is still a real decision.
        create_mission(store, "mixed", "sell the car and fix the script",
                       case={"_isolated_workspace": str(workspace)},
                       leash=world_leash(may=["code", "research", "web.send"],
                                         autonomous=True, workspace_mode="isolated"))
        assert driver.advance("mixed") == NEEDS_YOU
        assert asked == ["sell the car and fix the script"]
    finally:
        store.close()
        actions.close()


def test_human_updates_reach_the_coding_loop_in_order_and_are_never_dropped(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []

    def runner(goal, **_context):
        seen.append(goal)
        # A patch exists but nothing verified it, so the Mission hands off.
        return {"answer": "pass %d done" % len(seen), "verified": False,
                "session_id": "mission-code-dispatch-test",
                "slice_mutated": True, "patch_attributed": True, "turns": 4}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "steered", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        assert driver.advance("steered") == NEEDS_YOU
        assert store.get("steered").case["code_delivery"]["answer"] == "pass 1 done"

        # The user answers the durable hand-off with a correction.
        assert store.continue_handoff("steered", "另外 README 也要写清楚示例") is True
        driver.advance("steered")

        assert len(seen) == 2
        assert seen[0] == GOAL
        # The original request is intact and the update follows it, in order.
        assert seen[1].startswith(GOAL)
        assert "另外 README 也要写清楚示例" in seen[1]
        assert seen[1].index(GOAL) < seen[1].index("另外 README")
        # A second update appends; it never replaces the first.
        assert store.continue_handoff("steered", "并且保持向后兼容") is True
        mission = store.get("steered")
        composed = code_mission_goal(mission)
        assert composed.count("另外 README 也要写清楚示例") == 1
        assert composed.index("另外 README") < composed.index("并且保持向后兼容")
        assert len(mission.case["human_updates"]) == 2
    finally:
        store.close()
        actions.close()


def test_resumed_dispatch_keeps_one_durable_session_identity(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    dispatched = []

    def runner(goal, *, session_id=None, **_context):
        dispatched.append((goal, session_id))
        return {"answer": "slice", "verified": False,
                "continue_needed": len(dispatched) < 2,
                "session_id": "mission-code-dispatch-test",
                "slice_mutated": True, "patch_attributed": True,
                "turns": 3, "turns_exhausted": len(dispatched) < 2}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "resumed", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        assert driver.advance("resumed") == WAITING     # bounded slice checkpointed
        driver.wake("resumed", force=True)              # the daemon's next tick
        assert dispatched == [(GOAL, "mission-code-dispatch-test")] * 2
        assert store.get("resumed").case["code_dispatch"]["attempts"] == 2
    finally:
        store.close()
        actions.close()


# ── telling the truth about what happened ───────────────────────────────────
def test_read_only_slice_is_reported_as_a_report_not_as_an_unverified_patch(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()

    def runner(_goal, **_context):
        return {
            "answer": "现有 ledger.py 提供 --since/--total/--json 三个参数。",
            "verified": False, "slice_mutated": False, "patch_attributed": False,
            "session_id": "mission-code-dispatch-test", "turns": 20,
            "verification": {
                "verified": False,
                "detail": "check passed but the Mission produced no attributed patch",
                "evidence": {"command": "python -m unittest -q", "executed": True,
                             "exit_code": 0, "passed": True, "command_passed": True,
                             "cancelled": False, "patch_attributed": False,
                             "output": "Ran 49 tests in 0.027s\n\nOK\n"}}}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "readonly", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        assert driver.advance("readonly") == NEEDS_YOU
        mission = store.get("readonly")

        # The false claim that started all this.
        assert "code edited" not in mission.result
        assert "no file in the workspace was changed" in mission.result
        # The model's actual answer is the deliverable and is preserved verbatim.
        assert "现有 ledger.py 提供 --since/--total/--json 三个参数。" in mission.result
        assert mission.case["code_delivery"]["answer"].startswith("现有 ledger.py")
        assert mission.case["code_delivery"]["mutation_reported"] is True
        assert mission.case["code_delivery"]["slice_mutated"] is False
        # The host check's own evidence survives, exit code and all.
        assert "exit=0" in mission.result
        assert mission.case["code_verification"]["evidence"]["output"].startswith(
            "Ran 49 tests")
    finally:
        store.close()
        actions.close()


def test_real_code_keeps_the_mutation_facts_its_done_check_needs():
    execute = _real_code(lambda *_a, **_k: {
        "answer": "surveyed the repository", "verified": False,
        "slice_mutated": False, "patch_attributed": False,
        "session_id": "s", "post_tree_digest": "abc",
        "agent_post_tree_digest": "abc", "baseline_tree_digest": "abc"})
    record = SimpleNamespace(args={"goal": "look around", "workspace": "."},
                             job_id="m")

    result = execute(record)

    assert result["answer"] == "surveyed the repository"
    assert result["slice_mutated"] is False and result["patch_attributed"] is False
    verdict = _code_verify(record, result)
    assert verdict.status == INCONCLUSIVE
    assert "changed no file" in verdict.reason


def test_legacy_runner_without_mutation_facts_never_claims_an_edit():
    execute = _real_code(lambda *_a, **_k: {"answer": "did something",
                                            "verified": False})
    record = SimpleNamespace(args={"goal": "x"}, job_id="m")

    verdict = _code_verify(record, execute(record))

    assert verdict.status == INCONCLUSIVE
    assert "edited" not in verdict.reason
    assert "did something" in verdict.reason


def test_stop_report_states_patch_verification_and_stop_separately():
    text = code_stop_report("code stopped without completion-grade evidence", {
        "answer": "here is what I found", "slice_mutated": False,
        "patch_attributed": False, "turns_exhausted": False,
        "verification": {"detail": "check passed but the Mission produced no "
                                   "attributed patch",
                         "evidence": {"command": "python -m unittest -q",
                                      "executed": True, "exit_code": 0,
                                      "cancelled": False}}})

    assert "no file in the workspace was changed" in text
    assert "python -m unittest -q" in text
    assert "here is what I found" in text


# ── no premature green ──────────────────────────────────────────────────────
def test_passing_suite_without_an_attributed_patch_never_closes_the_goal(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "ledger.py").write_text("x = 1\n", encoding="utf-8")
    from harness.verification import workspace_snapshot
    digest = workspace_snapshot(str(workspace))["tree_digest"]
    mission = SimpleNamespace(case={
        "code_profile": {"verify_command": "python -m unittest -q"},
        "code_verified": True, "_isolated_workspace": str(workspace),
        "code_baseline_tree_digest": "an-older-baseline",
        "code_verification": {"verified": True, "evidence": {
            "timestamp": time.time(), "passed": True, "command_passed": True,
            "ran_after_last_edit": True, "post_tree_digest": digest,
            "post_snapshot_complete": True, "command": "python -m unittest -q",
            "source": "mission_code_profile", "executed": True,
            "patch_attributed": False}}})

    verdict = CodeWorkspaceGoalVerifier().verify_mission(mission)

    assert verdict.status == INCONCLUSIVE
    assert "no Mission-attributed patch" in verdict.reason

    # The same receipt with real provenance does close it.
    mission.case["code_verification"]["evidence"]["patch_attributed"] = True
    assert CodeWorkspaceGoalVerifier().verify_mission(mission).status == VERIFIED

    # ... unless the coding run itself never reached a settled delivery.
    mission.case["code_delivery"] = {"cancelled": True, "answer": "stopped"}
    assert CodeWorkspaceGoalVerifier().verify_mission(
        mission).status == INCONCLUSIVE


def test_a_turn_limited_slice_with_a_green_suite_is_not_delivery(tmp_path):
    """Exit zero answers "do the tests pass", not "is the task done"."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "ledger.py"
    target.write_text("x = 1\n", encoding="utf-8")
    from harness.verification import workspace_snapshot
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    calls = []

    def runner(_goal, *, workspace=None, **_context):
        calls.append(1)
        target.write_text("x = %d\n" % len(calls), encoding="utf-8")
        post = workspace_snapshot(workspace)
        return {
            "answer": "cut off mid-sentence" if len(calls) < 3 else "done, here is the summary",
            "verified": True, "session_id": "mission-code-dispatch-test",
            "slice_mutated": True, "patch_attributed": True, "turns": 24,
            # The run was cut off by its turn limit every time but the last.
            "turns_exhausted": len(calls) < 3,
            "stop_reason": "turn_limit" if len(calls) < 3 else "completed",
            "baseline_tree_digest": baseline,
            "post_tree_digest": post["tree_digest"],
            "verification": {"verified": True, "evidence": {
                "timestamp": time.time(), "passed": True, "command_passed": True,
                "ran_after_last_edit": True, "executed": True,
                "patch_attributed": True,
                "post_tree_digest": post["tree_digest"],
                "post_snapshot_complete": post["snapshot_complete"],
                "command": "python -m unittest -q",
                "source": "mission_code_profile"}}}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "cutoff", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        # The first slice's suite is already green, but it never got to finish.
        driver.advance("cutoff")
        assert len(calls) >= 2
        assert store.get("cutoff").case["code_dispatch"]["finishing"] >= 1
        # Once the run finishes on its own, the same evidence does close the goal.
        for _ in range(4):
            if store.get("cutoff").state in (DONE_VERIFIED, NEEDS_YOU):
                break
            driver.wake("cutoff", force=True)
        assert store.get("cutoff").state == DONE_VERIFIED
        assert store.get("cutoff").case["code_delivery"]["answer"] == \
            "done, here is the summary"
    finally:
        store.close()
        actions.close()


def test_a_run_that_never_finishes_stops_honestly_instead_of_claiming_delivery(
        tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "ledger.py"
    target.write_text("x = 1\n", encoding="utf-8")
    from harness.verification import workspace_snapshot
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    calls = []

    def runner(_goal, *, workspace=None, **_context):
        calls.append(1)
        target.write_text("x = %d\n" % len(calls), encoding="utf-8")
        post = workspace_snapshot(workspace)
        return {
            "answer": "still going", "verified": True, "turns": 24,
            "session_id": "mission-code-dispatch-test", "slice_mutated": True,
            "patch_attributed": True, "turns_exhausted": True,
            "stop_reason": "turn_limit", "baseline_tree_digest": baseline,
            "post_tree_digest": post["tree_digest"],
            "verification": {"verified": True, "evidence": {
                "timestamp": time.time(), "passed": True, "command_passed": True,
                "ran_after_last_edit": True, "executed": True,
                "patch_attributed": True,
                "post_tree_digest": post["tree_digest"],
                "post_snapshot_complete": post["snapshot_complete"],
                "command": "python -m unittest -q",
                "source": "mission_code_profile"}}}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "never", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        for _ in range(6):
            if store.get("never").state in (DONE_VERIFIED, NEEDS_YOU):
                break
            driver.advance("never")
            driver.wake("never", force=True)
        mission = store.get("never")

        assert mission.state == NEEDS_YOU
        assert "cut off before it finished" in mission.result
        # Bounded: it did not keep buying slices forever.
        assert len(calls) <= 1 + 2
    finally:
        store.close()
        actions.close()


def test_a_cancelled_slice_is_never_re_dispatched_without_a_human_step(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def runner(_goal, **_context):
        calls.append(1)
        return {"answer": "stopped part way", "verified": False,
                "session_id": "mission-code-dispatch-test", "cancelled": True,
                "slice_mutated": True, "patch_attributed": True, "turns": 2,
                "stop_reason": "canceled",
                "verification": {"verified": False, "skipped": True,
                                 "detail": "host verification skipped: the code "
                                           "worker was cancelled"}}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "stopped", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        assert driver.advance("stopped") == NEEDS_YOU
        assert len(calls) == 1

        # Re-entering with nothing new to say must not buy another slice.
        store.set_state("stopped", "queued")
        assert driver.advance("stopped") == NEEDS_YOU
        assert len(calls) == 1
        assert "will not be repeated automatically" in store.get("stopped").result

        # An explicit human step is what moves it.
        assert store.continue_handoff("stopped", "请继续，从上次的地方接着做") is True
        driver.advance("stopped")
        assert len(calls) == 2
    finally:
        store.close()
        actions.close()


def test_a_transient_error_that_checkpointed_itself_still_resumes_directly(tmp_path):
    """A retryable transport error is a yield, not a stop, and keeps the goal."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []

    def runner(goal, **_context):
        seen.append(goal)
        if len(seen) == 1:
            return {"answer": "", "verified": False, "continue_needed": True,
                    "session_id": "mission-code-dispatch-test",
                    "error": "overloaded_error: please retry", "transient": True,
                    "retry_after_seconds": 1, "slice_mutated": True,
                    "patch_attributed": True, "turns": 2}
        return {"answer": "finished after the retry", "verified": False,
                "session_id": "mission-code-dispatch-test",
                "slice_mutated": True, "patch_attributed": True, "turns": 5}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "transient", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        assert driver.advance("transient") == WAITING
        driver.wake("transient", force=True)

        # Resumed by the same deterministic dispatch, with the same full goal,
        # rather than handed to a planner that would rewrite it.
        assert seen == [GOAL, GOAL]
        assert store.get("transient").case["code_dispatch"]["attempts"] == 2
    finally:
        store.close()
        actions.close()


def test_unproductive_slices_stop_instead_of_rerunning_forever(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def runner(_goal, **_context):
        calls.append(1)
        return {"answer": "still reading", "verified": False,
                "continue_needed": True, "session_id": "mission-code-dispatch-test",
                "slice_mutated": False, "patch_attributed": False,
                "turns": 3, "turns_exhausted": True}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "stuck", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        driver.advance("stuck")
        for _ in range(6):
            if store.get("stuck").state == NEEDS_YOU:
                break
            driver.wake("stuck", force=True)
        mission = store.get("stuck")
        assert mission.state == NEEDS_YOU
        assert "no file change across" in mission.result
        # It stopped on evidence, not after an unbounded number of tries.
        assert len(calls) <= 4
    finally:
        store.close()
        actions.close()


def test_profile_helpers_only_claim_dedicated_code_missions():
    bound = SimpleNamespace(
        goal="g", leash={"may": ["code"]},
        case={"_isolated_workspace": "/tmp/x",
              "code_profile": {"durable": True, "direct_dispatch": True}})
    assert code_mission_profile(bound) is not None
    assert code_mission_profile(SimpleNamespace(
        goal="g", leash={"may": ["code"]},
        case={"code_profile": {"durable": True, "direct_dispatch": True}})) is None
    assert code_mission_profile(SimpleNamespace(
        goal="g", leash={"may": ["research"]}, case=bound.case)) is None
    assert code_mission_profile(SimpleNamespace(
        goal="g", leash={"may": ["code"]},
        case={"_isolated_workspace": "/tmp/x",
              "code_profile": {"durable": True}})) is None


# ── the host check's lifecycle ──────────────────────────────────────────────
def _fake_harness(monkeypatch, result):
    seen = []
    monkeypatch.setattr("harness.cli.make_harness",
                        lambda *_a, **_k: _FakeHarness(result, seen))
    return seen


def test_cancelled_host_check_kills_its_child_before_a_delayed_write(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "slow_writer.py").write_text(
        "import time, pathlib\n"
        "time.sleep(6)\n"
        "pathlib.Path('late.txt').write_text('written after cancellation')\n",
        encoding="utf-8")
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    seen = _fake_harness(monkeypatch, _run_result(
        answer="edited", messages=[{"role": "assistant", "content": "edited"}]))
    started = time.monotonic()

    out = _live_code(
        "do the work", str(workspace), mission_id="cancel-check",
        session_id="mission-code-cancel", verify_timeout_seconds=60,
        verify_command="python slow_writer.py",
        cancelled=lambda: time.monotonic() - started > 1.0)

    evidence = out["verification"]["evidence"]
    assert evidence["executed"] is True
    assert evidence["cancelled"] is True
    assert evidence["passed"] is False
    assert evidence["process_tree_terminated"] is True
    assert out["verified"] is False
    # A stop is settled, not a scheduling yield: nothing may replay it.
    assert out["continue_needed"] is False
    assert out["cancelled"] is True
    assert _code_verify(None, out).status == INCONCLUSIVE
    # The fence was armed before the command and retired only on the proof that
    # the tree is gone.
    assert out["verification"]["check_boundary"]["retired"] is True
    assert out["recovery_required"] is False
    assert seen and seen[0]["run_owner"] is not None

    # The child had six seconds of work left; it must never land.
    time.sleep(7)
    assert not (workspace / "late.txt").exists()


def _marker_check(workspace, name="check_ran.txt"):
    """A verify command that proves, by its own side effect, whether it ran."""
    (workspace / "marker_check.py").write_text(
        "import pathlib\npathlib.Path(%r).write_text('1')\n" % name,
        encoding="utf-8")
    return "python marker_check.py", workspace / name


def test_errored_worker_never_starts_a_fresh_host_check(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    _fake_harness(monkeypatch, _run_result(
        answer="", messages=[], error="provider refused the request"))
    command, marker = _marker_check(workspace)

    out = _live_code("do the work", str(workspace), mission_id="errored",
                     session_id="mission-code-errored", verify_command=command)

    assert not marker.exists()
    assert out["verification"]["skipped"] is True
    assert "stopped with an error" in out["verification"]["detail"]
    assert out["verified"] is False


def test_a_boundary_opened_during_the_run_is_not_retired_by_the_host_check(
        tmp_path, monkeypatch):
    """A tool that was still executing when the slice ended keeps its fence.

    ``save`` already refuses to clear an uncertain boundary; this pins the rest
    of the path: the transcript is written, the host check is NOT started on a
    workspace whose state nobody has inspected, and the fence is still there
    afterwards for a human to reconcile.
    """
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    sid = "mission-code-fenced"
    command, marker = _marker_check(workspace)
    result = _run_result(
        answer="new work", messages=[{"role": "assistant", "content": "new work"}])

    class _InterruptedHarness(_FakeHarness):
        def run(self, task_id, prompt, history=None):
            # The loop checkpointed an in-flight tool and then stopped.
            sessions.checkpoint(sid, [], project="mission-code", cwd=str(workspace),
                                state="executing_tool",
                                detail={"tool_name": "write_file"})
            return super().run(task_id, prompt, history)

    monkeypatch.setattr("harness.cli.make_harness",
                        lambda *_a, **_k: _InterruptedHarness(result, []))

    out = _live_code("continue", str(workspace), mission_id="fenced",
                     session_id=sid, verify_command=command)

    assert not marker.exists()
    assert out["verification"]["skipped"] is True
    assert "earlier unreconciled boundary" in out["verification"]["detail"]
    assert out["recovery_required"] is True
    # The transcript was saved, and saving it did not retire the fence.
    assert sessions.load(sid)["last_answer"] == "new work"
    assert sessions.recovery_state(sid)["recovery_required"] is True


def test_second_executor_is_refused_without_touching_the_workspace(
        tmp_path, monkeypatch):
    from harness import session_owner
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(
        "harness.cli.make_harness",
        lambda *_a, **_k: pytest.fail("a second executor started a model run"))

    with session_owner.own("mission-code-busy", label="test"):
        out = _live_code("continue", str(workspace), mission_id="busy",
                         session_id="mission-code-busy")

    assert out["needs_human"] is True
    assert out["recovery_required"] is not True
    assert "another executor" in out["answer"]


# ── surface projections ─────────────────────────────────────────────────────
def test_service_marks_only_workspace_bound_code_missions_for_direct_dispatch(
        tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(base=str(tmp_path / "svc"),
                             provider="claude-agent-sdk", model="claude-opus-4-8",
                             decider=lambda *_a, **_k: {"action": "done"},
                             stub=True)
    try:
        bound = service.start("fix the parser", code=True, workspace=str(repo),
                              verify_command="python -m unittest -q")
        loose = service.start("fix something later", code=True)

        assert bound["case"]["code_profile"]["direct_dispatch"] is True
        assert loose["case"]["code_profile"]["direct_dispatch"] is False
        assert code_mission_profile(
            service.store.get(bound["mission_id"])) is not None
        assert code_mission_profile(
            service.store.get(loose["mission_id"])) is None
    finally:
        service.close()


# ── the loop can check its own work inside the run ──────────────────────────
class _CheckingHarness(_FakeHarness):
    """A loop that edits and then calls the host check, like the real one does."""

    def __init__(self, result, seen, workspace, edits=(), check_after_edit=True):
        super().__init__(result, seen)
        self.workspace = str(workspace)
        self.edits = list(edits)
        self.check_after_edit = check_after_edit
        self.tool_output = []

    def run(self, task_id, prompt, history=None):
        from harness.tools import ToolCtx
        ctx = ToolCtx(cwd=self.workspace, project="mission-code", memory=None,
                      cancelled=getattr(self, "cancelled", None))
        tool = self.registry._tools.get("run_verification")
        for name, body in self.edits[:1]:
            (os.path.join(self.workspace, name))
            with open(os.path.join(self.workspace, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        if tool is not None and self.check_after_edit:
            self.tool_output.append(tool.run({}, ctx))
        for name, body in self.edits[1:]:
            with open(os.path.join(self.workspace, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        return super().run(task_id, prompt, history)


def _counting_check(workspace, counter, exit_code=0, note="OK"):
    """A verify command that counts its own executions OUTSIDE the workspace."""
    (workspace / "check.py").write_text(
        "import pathlib, sys\n"
        "p = pathlib.Path(%r)\n"
        "p.write_text(str(int(p.read_text() or 0) + 1) if p.exists() else '1')\n"
        "print(%r)\n"
        "sys.exit(%d)\n" % (str(counter), note, exit_code), encoding="utf-8")
    return "python check.py"


def _code_env(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_MISSION_CODE_ROOTS", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))


def test_the_coding_loop_can_run_the_pre_authorized_check_and_see_it_fail(
        tmp_path, monkeypatch):
    """The failure that cost a whole slice to discover is now visible in-run."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "ledger.py").write_text("x = 1\n", encoding="utf-8")
    counter = tmp_path / "runs.txt"
    command = _counting_check(workspace, counter, exit_code=1,
                              note="FAIL: --category is case-insensitive")
    from harness.verification import workspace_snapshot
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    _code_env(monkeypatch, tmp_path)
    seen = []
    harness = _CheckingHarness(
        _run_result(answer="tried it", messages=[{"role": "assistant", "content": "x"}]),
        seen, workspace, edits=[("ledger.py", "x = 2\n")])
    monkeypatch.setattr("harness.cli.make_harness", lambda *_a, **_k: harness)

    out = _live_code("add --category", str(workspace), mission_id="inslice",
                     session_id="mission-code-inslice", verify_command=command,
                     baseline_tree_digest=baseline, max_model_calls=40)

    # The tool existed, ran the exact command, and handed back the real failure.
    assert "run_verification" in seen[0]["tools"]
    assert "VERIFICATION FAILED" in harness.tool_output[0]
    assert "--category is case-insensitive" in harness.tool_output[0]
    # The prompt told it the tool is there and what it runs.
    assert "run_verification" in seen[0]["prompt"] and command in seen[0]["prompt"]
    assert out["in_slice_checks"] == 1
    assert out["verified"] is False
    # A red in-slice check is never reused as the Mission's receipt, so the host
    # ran its own: the failure is confirmed, not inherited.
    assert counter.read_text() == "2"
    assert out["reused_in_slice_check"] is False


def test_the_check_is_the_only_hand_added_and_nothing_general_survives(
        tmp_path, monkeypatch):
    """One narrow tool, not a shell: the positive authority list still holds."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _code_env(monkeypatch, tmp_path)
    seen = []
    harness = _FakeHarness(_run_result(answer="ok", messages=[]), seen)
    for name in ("read_file", "write_file", "edit_file", "grep", "code_search",
                 "plan", "undo", "bash", "execute_code", "browser_open",
                 "http_get", "mcp__server__anything", "screenshot", "delegate"):
        harness.registry._tools[name] = SimpleNamespace(
            name=name, tier="always", description=name, schema={})
    monkeypatch.setattr("harness.cli.make_harness", lambda *_a, **_k: harness)

    _live_code("do the work", str(workspace), mission_id="narrow",
               session_id="mission-code-narrow", verify_command="python -c pass",
               max_model_calls=40)

    assert seen[0]["tools"] == ["code_search", "edit_file", "grep", "plan",
                                "read_file", "run_verification", "undo",
                                "write_file"]


def test_no_check_tool_exists_when_no_command_was_authorized(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _code_env(monkeypatch, tmp_path)
    seen = _fake_harness(monkeypatch, _run_result(answer="ok", messages=[]))

    _live_code("do the work", str(workspace), mission_id="nocmd",
               session_id="mission-code-nocmd", max_model_calls=40)

    assert seen[0]["tools"] == []
    assert "run_verification" not in seen[0]["prompt"]


def test_a_green_in_slice_check_on_unchanged_bytes_is_not_run_a_second_time(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "ledger.py").write_text("x = 1\n", encoding="utf-8")
    counter = tmp_path / "runs.txt"
    command = _counting_check(workspace, counter)
    from harness.verification import workspace_snapshot
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    _code_env(monkeypatch, tmp_path)
    harness = _CheckingHarness(
        _run_result(answer="done", messages=[{"role": "assistant", "content": "x"}]),
        [], workspace, edits=[("ledger.py", "x = 2\n")])
    monkeypatch.setattr("harness.cli.make_harness", lambda *_a, **_k: harness)

    out = _live_code("add --category", str(workspace), mission_id="reuse",
                     session_id="mission-code-reuse", verify_command=command,
                     baseline_tree_digest=baseline, max_model_calls=40)

    # Identical command, identical bytes: the answer is already on disk, and the
    # user does not pay the suite's wall-clock twice for it.
    assert counter.read_text() == "1"
    assert out["reused_in_slice_check"] is True
    assert out["verified"] is True
    assert out["patch_attributed"] is True
    assert out["verification"]["evidence"]["source"] == "mission_code_profile"
    assert out["verification"]["evidence"]["invoked_by"] == "code_agent_tool"
    # And the independent goal verifier, which re-snapshots the workspace for
    # itself, accepts the reused receipt on the same terms as a fresh one.
    mission = SimpleNamespace(case={
        "code_profile": {"verify_command": command},
        "code_verified": True, "_isolated_workspace": str(workspace),
        "code_baseline_tree_digest": baseline,
        "code_verification": out["verification"]})
    assert CodeWorkspaceGoalVerifier().verify_mission(mission).status == VERIFIED


def test_an_edit_after_the_in_slice_check_forces_the_host_to_check_again(
        tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "ledger.py").write_text("x = 1\n", encoding="utf-8")
    counter = tmp_path / "runs.txt"
    command = _counting_check(workspace, counter)
    from harness.verification import workspace_snapshot
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    _code_env(monkeypatch, tmp_path)
    harness = _CheckingHarness(
        _run_result(answer="done", messages=[{"role": "assistant", "content": "x"}]),
        [], workspace,
        # Edit, check, then edit again: the receipt no longer describes the tree.
        edits=[("ledger.py", "x = 2\n"), ("ledger.py", "x = 3\n")])
    monkeypatch.setattr("harness.cli.make_harness", lambda *_a, **_k: harness)

    out = _live_code("add --category", str(workspace), mission_id="restale",
                     session_id="mission-code-restale", verify_command=command,
                     baseline_tree_digest=baseline, max_model_calls=40)

    assert counter.read_text() == "2"
    assert out["reused_in_slice_check"] is False
    assert out["verified"] is True


def test_the_loop_saying_it_passes_never_overrides_what_the_check_reported(
        tmp_path, monkeypatch):
    """Prose is not evidence, whichever direction it points."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "ledger.py").write_text("x = 1\n", encoding="utf-8")
    counter = tmp_path / "runs.txt"
    command = _counting_check(workspace, counter, exit_code=1, note="FAIL")
    from harness.verification import workspace_snapshot
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    _code_env(monkeypatch, tmp_path)
    harness = _CheckingHarness(
        _run_result(answer="All 49 tests pass and the feature is complete.",
                    verified=True,
                    messages=[{"role": "assistant", "content": "x"}]),
        [], workspace, edits=[("ledger.py", "x = 2\n")])
    monkeypatch.setattr("harness.cli.make_harness", lambda *_a, **_k: harness)

    out = _live_code("add --category", str(workspace), mission_id="claims",
                     session_id="mission-code-claims", verify_command=command,
                     baseline_tree_digest=baseline, max_model_calls=40)

    assert out["verified"] is False
    assert out["verification"]["evidence"]["exit_code"] == 1
    assert _code_verify(None, out).status == INCONCLUSIVE


def test_a_check_that_writes_its_own_files_is_not_reported_as_a_patch(
        tmp_path, monkeypatch):
    """The agent surveyed and ran the suite; the suite's output is not its work."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "ledger.py").write_text("x = 1\n", encoding="utf-8")
    (workspace / "check.py").write_text(
        "import pathlib\npathlib.Path('build.log').write_text('artifact')\n"
        "print('OK')\n", encoding="utf-8")
    from harness.verification import workspace_snapshot
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    _code_env(monkeypatch, tmp_path)
    harness = _CheckingHarness(
        _run_result(answer="I only read the code",
                    messages=[{"role": "assistant", "content": "x"}]),
        [], workspace, edits=[])
    monkeypatch.setattr("harness.cli.make_harness", lambda *_a, **_k: harness)

    out = _live_code("survey the repository", str(workspace), mission_id="artifact",
                     session_id="mission-code-artifact",
                     verify_command="python check.py",
                     baseline_tree_digest=baseline, max_model_calls=40)

    # The tree really did change — build.log exists — but not because the agent
    # wrote a patch, and a green suite over it must not close anything.
    assert (workspace / "build.log").exists()
    assert out["slice_mutated"] is False
    assert out["patch_attributed"] is False
    assert out["verified"] is False
    assert _code_verify(None, out).status == INCONCLUSIVE


# ── how many turns one slice may take ───────────────────────────────────────
def _turns_seen(tmp_path, monkeypatch, name, **kwargs):
    workspace = tmp_path / name
    workspace.mkdir()
    _code_env(monkeypatch, tmp_path)
    seen = _fake_harness(monkeypatch, _run_result(
        answer="ok", messages=[{"role": "assistant", "content": "ok"}]))
    _live_code("do the work", str(workspace), mission_id=name,
               session_id="mission-code-" + name, **kwargs)
    return seen[0]


def test_explicit_zero_slice_turns_means_no_arbitrary_turn_ceiling(
        tmp_path, monkeypatch):
    """The ordinary dedicated code profile asks for zero, and gets what it asked.

    Slicing one small feature into 24-turn fragments is what turned a real run
    into five resumed prompts, five repository re-reads and five host checks.
    """
    seen = _turns_seen(tmp_path, monkeypatch, "zero", slice_turns=0,
                       max_model_calls=40)

    assert seen["max_turns"] == 0            # the loop's own "unlimited"
    assert seen["max_model_calls"] == 40     # ...and what actually bounds it


def test_an_absent_slice_turns_still_uses_the_configured_scheduling_slice(
        tmp_path, monkeypatch):
    seen = _turns_seen(tmp_path, monkeypatch, "absent", max_model_calls=40)
    assert seen["max_turns"] == 24


def test_an_explicitly_positive_slice_turns_stays_bounded(tmp_path, monkeypatch):
    seen = _turns_seen(tmp_path, monkeypatch, "three", slice_turns=3,
                       max_model_calls=40)
    assert seen["max_turns"] == 3
    seen = _turns_seen(tmp_path, monkeypatch, "huge", slice_turns=9000,
                       max_model_calls=9000)
    assert seen["max_turns"] == 50


def test_unlimited_turns_are_clamped_by_a_smaller_model_call_budget(
        tmp_path, monkeypatch):
    """Every turn costs a call, so the ledger is the real ceiling."""
    seen = _turns_seen(tmp_path, monkeypatch, "small", slice_turns=0,
                       max_model_calls=5)
    assert seen["max_turns"] == 0 and seen["max_model_calls"] == 5
    # A bounded slice is still clamped down to the remaining budget.
    seen = _turns_seen(tmp_path, monkeypatch, "clamped", slice_turns=30,
                       max_model_calls=4)
    assert seen["max_turns"] == 4


def test_without_a_call_budget_zero_is_not_read_as_unbounded(tmp_path, monkeypatch):
    """Nothing would count it down, so "unlimited" would mean literally forever."""
    seen = _turns_seen(tmp_path, monkeypatch, "nobudget", slice_turns=0)
    assert seen["max_turns"] == 24


def test_an_exhausted_model_budget_is_refused_before_anything_is_touched(
        tmp_path, monkeypatch):
    workspace = tmp_path / "spent"
    workspace.mkdir()
    _code_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "harness.cli.make_harness",
        lambda *_a, **_k: pytest.fail("a spent Mission started a model run"))

    out = _live_code("do the work", str(workspace), mission_id="spent",
                     session_id="mission-code-spent", slice_turns=0,
                     max_model_calls=0)

    assert "budget is exhausted" in out["answer"]
    assert out["continue_needed"] is False


# ── continuation state ──────────────────────────────────────────────────────
def test_one_historical_edit_cannot_keep_a_stuck_loop_running_for_ever(tmp_path):
    """``patch_attributed`` is cumulative; progress is what the LAST slice did."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def runner(_goal, **_context):
        calls.append(1)
        return {
            "answer": "still thinking about it", "verified": False,
            "continue_needed": True, "session_id": "mission-code-dispatch-test",
            # An edit from an earlier slice still differs from the baseline, so
            # this stays true for the rest of the Mission...
            "patch_attributed": True,
            # ...while this slice, and the next, and the next, changed nothing.
            "slice_mutated": False,
            "agent_post_tree_digest": "unchanged-since-the-first-edit",
            "turns": 3, "turns_exhausted": True}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "sticky", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        driver.advance("sticky")
        for _ in range(6):
            if store.get("sticky").state == NEEDS_YOU:
                break
            driver.wake("sticky", force=True)
        mission = store.get("sticky")

        assert mission.state == NEEDS_YOU
        assert "no file change across" in mission.result
        assert len(calls) <= 4
    finally:
        store.close()
        actions.close()


def test_a_moving_workspace_digest_still_counts_as_progress(tmp_path):
    """A runner that reports only digests is not accused of standing still."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def runner(_goal, **_context):
        calls.append(1)
        return {"answer": "slice %d" % len(calls), "verified": False,
                "continue_needed": True, "session_id": "mission-code-dispatch-test",
                "patch_attributed": True, "slice_mutated": False,
                "agent_post_tree_digest": "digest-%d" % len(calls),
                "turns": 3, "turns_exhausted": True}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "moving", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        driver.advance("moving")
        for _ in range(4):
            driver.wake("moving", force=True)

        assert len(calls) >= 4
        assert store.get("moving").case["code_dispatch"]["unproductive"] == 0
    finally:
        store.close()
        actions.close()


# ── the user's own words, whole ─────────────────────────────────────────────
def test_a_long_human_update_reaches_the_coding_loop_complete(tmp_path):
    """No silent shortening at the point where the text becomes the agent's scope."""
    long_note = "并且" + ("务必保持向后兼容，" * 400)
    assert len(long_note) > 2000
    mission = SimpleNamespace(
        goal=GOAL, leash={"may": ["code"]},
        case={"_isolated_workspace": str(tmp_path),
              "code_profile": {"durable": True, "direct_dispatch": True},
              "human_updates": [{"at": 1, "note": long_note}]})

    composed = code_mission_goal(mission)

    assert long_note in composed
    assert composed.startswith(GOAL)


def test_a_recovery_note_the_host_wrote_is_not_presented_as_the_users_scope(tmp_path):
    mission = SimpleNamespace(
        goal=GOAL, leash={"may": ["code"]},
        case={"_isolated_workspace": str(tmp_path),
              "code_profile": {"durable": True, "direct_dispatch": True},
              "human_updates": [
                  {"at": 1, "note": "Retry the failed predecessor.", "recovery": True},
                  {"at": 2, "note": "顺便把 README 也更新一下"}]})

    composed = code_mission_goal(mission)

    assert "[Mission host recovery note] Retry the failed predecessor." in composed
    assert "2. 顺便把 README 也更新一下" in composed


def test_a_long_answer_is_shortened_visibly_and_kept_whole_in_the_record():
    answer = "详细说明。" * 900
    text = code_stop_report(
        "the coding run reached its turn limit",
        {"answer": answer, "slice_mutated": True, "patch_attributed": True,
         "turns_exhausted": True,
         "verification": {"detail": "configured check failed (exit 1)",
                          "evidence": {"command": "python -m unittest -q",
                                       "executed": True, "exit_code": 1,
                                       "cancelled": False}}})

    # The structured facts are never what gets dropped...
    assert "files changed and the change is attributed to this Mission" in text
    assert "python -m unittest -q" in text
    assert "Stop: the coding run reached its turn limit." in text
    # ...and the cut is announced, with where the whole of it lives.
    assert "report shortened here" in text
    assert "code_delivery" in text
    assert len(text) <= 1800
    # A short answer is never touched.
    short = code_stop_report("stopped", {"answer": "all done", "slice_mutated": False,
                                         "patch_attributed": False})
    assert short.endswith("all done") and "shortened" not in short


def test_the_full_worker_answer_survives_in_the_durable_case(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    answer = "第 %d 段说明。" * 1
    long_answer = "".join(answer % index for index in range(300))

    def runner(_goal, **_context):
        return {"answer": long_answer, "verified": False, "turns": 9,
                "session_id": "mission-code-dispatch-test",
                "slice_mutated": True, "patch_attributed": True}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        create_mission(store, "verbose", GOAL, case=_dedicated_case(workspace),
                       leash=world_leash(may=["code"], autonomous=True,
                                         workspace_mode="isolated"))
        assert driver.advance("verbose") == NEEDS_YOU
        mission = store.get("verbose")

        assert len(mission.result) <= 1800
        # The one-screen result is bounded; the deliverable itself is not lost.
        assert mission.case["code_delivery"]["answer"] == long_answer
        assert mission.case["code"]["answer"] == long_answer
    finally:
        store.close()
        actions.close()


def test_run_tree_root_reports_the_missions_own_lifecycle(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(base=str(tmp_path / "svc"),
                             provider="claude-agent-sdk", model="claude-opus-4-8",
                             decider=lambda *_a, **_k: {"action": "done"},
                             stub=True)
    try:
        created = service.start("fix the parser", code=True, workspace=str(repo),
                                verify_command="python -m unittest -q")
        mid = created["mission_id"]
        # The durable row is deliberately ownerless (MissionStore leases the root
        # run, TaskTree does not), so it stays "queued" forever.
        run_id = service.store.get(mid).case["_run_id"]
        assert service._run_tree.get(run_id)["status"] == "queued"

        service.store.set_state(mid, NEEDS_YOU, "waiting on you")
        row = service.status(mid)["run_tree"]["root"]

        assert row["status"] == "needs_you"
        assert row["durable_status"] == "queued"
        assert row["status_source"] == "mission"
        assert row["mission_state"] == NEEDS_YOU
        assert row["progress_at"] >= row["created_at"]
        assert service._run_tree.get(run_id)["status"] == "queued"
    finally:
        service.close()
