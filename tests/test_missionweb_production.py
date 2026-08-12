import os
import sqlite3

import pytest

from harness.missionweb import MissionService
from harness.tasktree import CANCEL_REQUESTED, CANCELLED, TaskTreeStore
from harness.verification import workspace_snapshot


_CODEX_ZERO_CREDIT_EVIDENCE = {
    "credits_remaining": 0,
    "auto_reload": False,
    "observed_at_utc": "2026-08-12T00:00:00Z",
}


def _allow_test_subscription(provider, *, account_evidence=None, environ=None,
                             model="", require_direct_probe=True,
                             minimum_token_validity_seconds=0):
    """Allow only the two reviewed first-party test routes, never unknown ones."""
    if provider in ("claude-code", "claude-direct"):
        assert account_evidence is None
    elif provider == "codex-cli":
        assert account_evidence == _CODEX_ZERO_CREDIT_EVIDENCE
    else:
        raise RuntimeError("unreviewed subscription route")
    assert isinstance(environ, dict)
    assert not any(name.upper().startswith(
        ("ANTHROPIC_", "OPENAI_", "CLAUDE_", "CODEX_")) for name in environ)
    return {
        "format": "collie-subscription-guard-v1",
        "schema_version": 1,
        "provider": provider,
        "verdict": "allow",
    }


def test_production_defaults_bind_durable_tasktree_and_pending_hooks(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "hooks.json").write_text(
        '{"hooks":{"TaskCompleted":[{"hooks":[{"type":"command",'
        '"command":"never-run"}]}]}}', encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    inherited_state = str(tmp_path / "different-process-state")
    monkeypatch.setenv("COLLIE_STATE_DIR", inherited_state)

    service = MissionService(
        state_dir=str(state), decider=lambda *_: {"action": "done"}, stub=True)
    tree = service._run_tree
    mission = service.start("coordinate production specialists", may=["research"])

    assert os.path.normcase(tree.path) == os.path.normcase(str(state / "tasktree.db"))
    assert service._hooks.cwd == os.path.realpath(str(repo))
    assert os.environ["COLLIE_STATE_DIR"] == inherited_state
    assert mission["tasktree"] == {
        "available": True, "attached": False, "path": str(state / "tasktree.db")}
    assert mission["run_tree"] is None
    assert mission["hooks"]["active"] is False
    assert mission["hooks"]["pending"][0]["path"] == str(state / "hooks.json")
    assert service.inspect_run_tree(mission["mission_id"])["tree"] == {
        "root": None, "flat": []}

    service.close()
    with pytest.raises(sqlite3.ProgrammingError):
        tree.list_runs()


def test_overnight_execution_profile_survives_restart_and_forbids_cli_fallback(
        tmp_path, monkeypatch):
    base = str(tmp_path / "svc")
    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(
        base=base, provider="anthropic-oauth", model="claude-opus-4-8",
        decider=lambda *_: {}, stub=True,
        subscription_guard=_allow_test_subscription)
    created = service.start(
        "finish overnight", code=True, workspace=str(repo), overnight=True,
        verify_command="python -m pytest -q", no_paid_overage=True)
    mid = created["mission_id"]
    service.close()

    # Current settings now point at a metered provider.  Reopening the durable
    # Mission must restore its frozen subscription route, not inherit that change.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only-key")
    reopened = MissionService(
        base=base, provider="anthropic", model="metered-model", stub=True,
        subscription_guard=_allow_test_subscription)
    reopened._ensure_runtime()
    assert reopened._provider == "anthropic"
    mission = reopened.store.get(mid)
    reopened._activate_execution_profile(mission)
    reopened._ensure_runtime()
    assert reopened._provider == "anthropic-oauth"
    assert reopened._model == "claude-opus-4-8"
    assert reopened._prov.subscription_only is True
    assert mission.case["execution_profile"]["allow_provider_fallback"] is False
    assert mission.case["code_profile"]["verify_command"] == "python -m pytest -q"
    reopened.close()


def test_daemon_tick_activates_the_frozen_overnight_route(tmp_path):
    base = str(tmp_path / "svc")
    repo = tmp_path / "repo"
    repo.mkdir()
    created_by = MissionService(
        base=base, provider="anthropic-oauth", model="claude-opus-4-8",
        decider=lambda *_: {}, stub=True,
        subscription_guard=_allow_test_subscription)
    mid = created_by.start(
        "continue while I sleep", code=True, workspace=str(repo), overnight=True,
        verify_command="python -m pytest -q", no_paid_overage=True)["mission_id"]
    created_by.close()

    daemon = MissionService(
        base=base, provider="codex-oauth", model="gpt-5.6-terra",
        decider=lambda *_: {"action": "needs_human", "args": {"summary": "done"}},
        stub=True, subscription_guard=_allow_test_subscription)
    daemon._ensure_runtime()  # simulate an already-warm, long-lived job daemon
    ticked = daemon.tick()

    assert ticked["advanced"] == 1
    assert daemon._provider == "anthropic-oauth" and daemon._model == "claude-opus-4-8"
    assert daemon._subscription_only is True
    assert daemon.store.get(mid).state == "needs_you"
    daemon.close()


def test_overnight_rejects_claude_cli_system_prompt_route(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(
        base=str(tmp_path / "svc"), provider="claude-cli",
        model="opus", decider=lambda *_: {}, stub=True,
        subscription_guard=_allow_test_subscription)
    with pytest.raises(ValueError, match="direct Claude subscription route"):
        service.start(
            "do not risk extra usage", code=True, workspace=str(repo), overnight=True,
            no_paid_overage=True)
    assert service.missions() == []
    service.close()


def test_overnight_rejects_non_opus_model_substitution(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(
        base=str(tmp_path / "svc"), provider="anthropic-oauth",
        model="claude-sonnet-5", decider=lambda *_: {}, stub=True,
        subscription_guard=_allow_test_subscription)
    with pytest.raises(ValueError, match="explicit Claude Opus model"):
        service.start(
            "run Opus overnight", code=True, workspace=str(repo), overnight=True,
            no_paid_overage=True)
    assert service.missions() == []
    service.close()


def test_overnight_caller_cannot_raise_hard_preset_bounds(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(
        base=str(tmp_path / "svc"), provider="anthropic-oauth",
        model="claude-opus-4-8", decider=lambda *_: {}, stub=True,
        subscription_guard=_allow_test_subscription)
    created = service.start(
        "bounded Opus overnight", code=True, workspace=str(repo), overnight=True,
        verify_command="python -m pytest -q", no_paid_overage=True,
        max_total_steps=99_999, max_model_calls=99_999,
        max_model_tokens=99_999_999, max_model_cost_usd=500,
        max_active_wall_seconds=999_999, max_elapsed_seconds=9_999_999,
        max_step_seconds=99_999)
    leash = service.store.get(created["mission_id"]).leash

    assert leash["max_total_steps"] == 4_000
    assert leash["max_model_calls"] == 4_000
    assert leash["max_model_tokens"] == 8_000_000
    assert leash["max_model_cost_usd"] == 0.01
    assert leash["max_active_wall_seconds"] == 43_200
    assert leash["max_elapsed_seconds"] == 604_800
    assert leash["max_step_seconds"] == 1_800
    service.close()


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf")])
def test_overnight_rejects_nonfinite_cost_bound(tmp_path, invalid):
    repo = tmp_path / ("repo-" + str(invalid).replace("-", "neg"))
    repo.mkdir()
    service = MissionService(
        base=str(tmp_path / ("svc-" + str(invalid).replace("-", "neg"))),
        provider="anthropic-oauth", model="claude-opus-4-8",
        decider=lambda *_: {}, stub=True,
        subscription_guard=_allow_test_subscription)

    with pytest.raises(ValueError, match="must be finite"):
        service.start(
            "reject nonfinite money", code=True, workspace=str(repo), overnight=True,
            verify_command="python -m pytest -q", no_paid_overage=True,
            max_model_cost_usd=invalid)
    assert service.missions() == []
    service.close()


@pytest.mark.parametrize("mutation", ["model", "profile"])
def test_overnight_route_pin_rejects_durable_case_tampering(tmp_path, mutation):
    repo = tmp_path / mutation
    repo.mkdir()
    service = MissionService(
        base=str(tmp_path / ("svc-" + mutation)), provider="anthropic-oauth",
        model="claude-opus-4-8", decider=lambda *_: {}, stub=True,
        subscription_guard=_allow_test_subscription)
    created = service.start(
        "keep the frozen route", code=True, workspace=str(repo), overnight=True,
        verify_command="python -m pytest -q", no_paid_overage=True)
    mission = service.store.get(created["mission_id"])
    tampered = dict(mission.case)
    tampered["execution_profile"] = dict(tampered["execution_profile"])
    if mutation == "model":
        tampered["execution_profile"]["model"] = "claude-sonnet-5"
    else:
        tampered["execution_profile"]["profile"] = "durable-code"
        tampered["code_profile"] = dict(tampered["code_profile"], overnight=False)
    service.store.set_case(mission.mission_id, tampered)

    with pytest.raises(RuntimeError, match="invalid|canonical|route pin"):
        service._activate_execution_profile(service.store.get(mission.mission_id))
    service.close()


def test_global_tick_advances_multiple_frozen_overnight_profiles(tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    service = MissionService(
        base=str(tmp_path / "svc"), provider="anthropic-oauth",
        model="claude-opus-4-8",
        decider=lambda *_: {"action": "needs_human", "args": {"summary": "stop"}},
        stub=True, subscription_guard=_allow_test_subscription)
    first = service.start(
        "first route", code=True, workspace=str(one), overnight=True,
        verify_command="python -m pytest -q", no_paid_overage=True)["mission_id"]
    second = service.start(
        "second route", code=True, workspace=str(two), overnight=True,
        verify_command="python -m pytest -q", no_paid_overage=True)["mission_id"]

    first_tick = service.tick()
    second_tick = service.tick()

    assert first_tick["advanced"] == 2
    assert second_tick["advanced"] == 0
    assert {service.store.get(first).state, service.store.get(second).state} == {"needs_you"}
    service.close()


def test_same_provider_warm_runtime_is_rebuilt_for_subscription_only_profile(
        tmp_path, monkeypatch):
    class FakeProvider:
        model = "claude-opus-4-8"
        subscription_only = False

    made = []

    def make_provider(*_args, **_kwargs):
        provider = FakeProvider()
        made.append(provider)
        return provider

    monkeypatch.setattr("harness.providers.make_provider", make_provider)
    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(
        base=str(tmp_path / "svc"), provider="anthropic-oauth",
        model="claude-opus-4-8",
        decider=lambda *_: {}, stub=False,
        subscription_guard=_allow_test_subscription)
    service._ensure_runtime()
    assert made[-1].subscription_only is False
    mid = service.start(
        "freeze the billing route", code=True, workspace=str(repo),
        overnight=True, verify_command="python -m pytest -q",
        no_paid_overage=True)["mission_id"]

    service._activate_execution_profile(service.store.get(mid))
    assert service._runtime_ready is False
    service._ensure_runtime()

    assert len(made) == 2
    assert made[-1].subscription_only is True
    service.close()


def test_overnight_code_fails_closed_without_workspace_or_verification(tmp_path):
    service = MissionService(
        base=str(tmp_path / "svc"), provider="anthropic-oauth",
        model="claude-opus-4-8",
        decider=lambda *_: {}, stub=True,
        subscription_guard=_allow_test_subscription)

    with pytest.raises(ValueError, match="existing workspace"):
        service.start(
            "missing workspace", code=True, overnight=True,
            no_paid_overage=True)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="verification command"):
        service.start(
            "missing verifier", code=True, workspace=str(empty), overnight=True,
            no_paid_overage=True)

    detectable = tmp_path / "detectable"
    (detectable / "tests").mkdir(parents=True)
    created = service.start(
        "detected verifier", code=True, workspace=str(detectable), overnight=True,
        no_paid_overage=True)
    case = service.store.get(created["mission_id"]).case
    assert case["code_profile"]["verify_command"] == "python -m pytest -q"
    service.close()


def test_subscription_guard_is_rechecked_at_the_runnable_boundary(tmp_path):
    calls = []

    def guard(provider, *, account_evidence=None, environ=None, model="",
              require_direct_probe=True, minimum_token_validity_seconds=0):
        calls.append((provider, account_evidence, dict(environ or {})))
        return {
            "format": "collie-subscription-guard-v1",
            "schema_version": 1,
            "provider": provider,
            "verdict": "allow",
            "serial": len(calls),
        }

    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(
        base=str(tmp_path / "svc"), provider="anthropic", model="metered-default",
        decider=lambda *_: {
            "action": "needs_human", "args": {"summary": "test boundary"}},
        stub=True, subscription_guard=guard)
    created = service.start(
        "prove route twice", code=True, workspace=str(repo), overnight=True,
        verify_command="python verify.py", no_paid_overage=True,
        provider="anthropic-oauth", model="claude-opus-4-8")
    assert len(calls) == 1

    result = service.run(created["mission_id"])

    assert result["state"] == "needs_you"
    assert len(calls) == 2
    refreshed = service.store.get(created["mission_id"]).case["billing_safety"]
    assert refreshed["guard_receipt"]["serial"] == 2
    assert all(provider == "claude-direct" for provider, _evidence, _env in calls)
    service.close()


def test_failed_overnight_retry_preserves_frozen_contract_and_workspace_binding(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(
        base=str(tmp_path / "svc"), provider="anthropic-oauth",
        model="claude-opus-4-8",
        decider=lambda *_: {}, stub=True,
        subscription_guard=_allow_test_subscription)
    original = service.start(
        "retry without changing route", code=True, workspace=str(repo), overnight=True,
        verify_command="python verify.py", no_paid_overage=True)
    original_id = original["mission_id"]
    original_case = service.store.get(original_id).case
    service.store.set_state(original_id, "failed", "simulated ordinary failure")

    successor = service.retry(original_id, "try the remaining work")
    successor_case = service.store.get(successor["mission_id"]).case

    assert successor["state"] == "queued"
    assert successor_case["execution_profile"] == original_case["execution_profile"]
    assert successor_case["billing_safety"] == original_case["billing_safety"]
    assert successor_case["code_profile"] == original_case["code_profile"]
    assert successor_case["_isolated_workspace"] == os.path.realpath(str(repo))
    assert successor_case["code_profile"]["session_id"] == \
        original_case["code_profile"]["session_id"]
    assert successor["tasktree"]["attached"] is True
    assert successor["run_tree"]["root"]["mission_id"] == successor["mission_id"]
    service.close()


def test_code_session_recovery_requires_and_applies_an_explicit_outcome(
        tmp_path, monkeypatch):
    from harness import sessions

    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(
        state_dir=str(state), provider="anthropic-oauth",
        model="claude-opus-4-8", decider=lambda *_: {}, stub=True,
        subscription_guard=_allow_test_subscription)
    created = service.start(
        "recover an interrupted edit", code=True, workspace=str(repo), overnight=True,
        verify_command="python verify.py", no_paid_overage=True)
    mid = created["mission_id"]
    sid = service.store.get(mid).case["code_profile"]["session_id"]
    session_dir = str(state / "mission-code-sessions")
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", session_dir)
    sessions.checkpoint(
        sid, [{"role": "assistant", "content": "", "tool_calls": [{
            "id": "edit-1", "name": "edit_file", "args": {"path": "fix.py"}}]}],
        run_id="code-run", turn=1, state="executing_tool",
        detail={"tool_name": "edit_file", "tool_call_id": "edit-1"})
    service.store.set_state(mid, "recovery_required", "worker disappeared")

    status = service.status(mid)
    assert status["code_session_recovery"]["allowed_resolutions"] == [
        "completed", "not_fired", "cancel"]
    refused = service.reconcile(mid, "inspected files")
    assert refused["state"] == "recovery_required"
    assert "code_resolution" in refused["error"]

    reconciled = service.reconcile(
        mid, "the write did not land", code_resolution="not_fired")

    assert reconciled["state"] == "queued"
    assert sessions.recovery_state(sid, directory=session_dir)["recovery_required"] is False

    sessions.checkpoint(
        sid, [{"role": "assistant", "content": "", "tool_calls": [{
            "id": "edit-2", "name": "edit_file", "args": {"path": "fix.py"}}]}],
        run_id="code-run-2", turn=2, state="executing_tool",
        detail={"tool_name": "edit_file", "tool_call_id": "edit-2"})
    service.store.set_state(mid, "recovery_required", "second worker disappeared")

    cancelled = service.reconcile(
        mid, "stop instead of replaying the edit", code_resolution="cancel")

    assert cancelled["state"] == CANCELLED
    assert service.store.claim_run(mid) is None
    assert sessions.recovery_state(sid, directory=session_dir) is None
    service.close()


def test_tick_cancels_stale_code_worker_before_recovery_transition(tmp_path):
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True)
    mid = service.start("recover a dead code owner")["mission_id"]
    token = service.store.claim_run(mid, lease_s=1)
    stale_at = service.store.get(mid).lease_until
    observed = []

    class CodeProcess:
        def cancel_current(self, mission_id=None, include_persisted=True):
            if mission_id:
                mission = service.store.get(mission_id)
                observed.append((mission_id, mission.state, mission.run_token))
            return True

    service._code_process = CodeProcess()
    ticked = service.tick(now=stale_at)

    assert ticked["recovered"] == 1
    assert observed == [(mid, "running", token)]
    assert service.store.get(mid).state == "recovery_required"
    service.close()


def test_tick_keeps_stale_owner_fenced_when_worker_extinction_is_unconfirmed(
        tmp_path):
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True)
    mid = service.start("do not overlap an unkillable worker")["mission_id"]
    token = service.store.claim_run(mid, lease_s=1)
    stale_at = service.store.get(mid).lease_until

    class CodeProcess:
        def cancel_current(self, mission_id=None, include_persisted=True):
            return False

    service._code_process = CodeProcess()
    ticked = service.tick(now=stale_at)

    assert ticked["recovered"] == 0
    current = service.store.get(mid)
    assert current.state == "running"
    assert current.run_token == token
    assert any(event["name"] == "stale_worker_termination_unconfirmed"
               for event in service.store.events(mid, 20))
    service.close()


def test_overnight_specialist_gets_own_session_baseline_and_fresh_billing_receipt(
        tmp_path):
    calls = []

    def guard(provider, *, account_evidence=None, environ=None, model="",
              require_direct_probe=True, minimum_token_validity_seconds=0):
        calls.append(provider)
        return {"format": "collie-subscription-guard-v1", "schema_version": 1,
                "provider": provider, "verdict": "allow", "serial": len(calls)}

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "base.py").write_text("value = 1\n", encoding="utf-8")
    service = MissionService(
        base=str(tmp_path / "svc"), provider="anthropic-oauth",
        model="claude-opus-4-8", decider=lambda *_: {}, stub=True,
        subscription_guard=guard)
    parent = service.start(
        "split the refactor", code=True, workspace=str(repo), overnight=True,
        verify_command="python verify.py", no_paid_overage=True)
    child_workspace = tmp_path / "child-worktree"
    child_workspace.mkdir()
    (child_workspace / "base.py").write_text("value = 1\n", encoding="utf-8")
    child = service.spawn_specialist(
        parent["mission_id"], "writer", "change the isolated copy",
        resources=[{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(child_workspace))
    child_case = service.store.get(child["mission_id"]).case
    parent_case = service.store.get(parent["mission_id"]).case

    assert calls == ["claude-direct", "claude-direct"]
    assert child_case["billing_safety"]["guard_receipt"]["serial"] == 2
    assert child_case["code_profile"]["session_id"] != \
        parent_case["code_profile"]["session_id"]
    assert child_case["code_baseline_tree_digest"] == \
        workspace_snapshot(str(child_workspace))["tree_digest"]
    assert child_case["code_baseline_tree_digest"] != ""
    assert child_case["code_expected_tree_digest"] == \
        child_case["code_baseline_tree_digest"]
    service.close()


def test_default_tasktree_create_spawn_steer_cancel_and_inspect(tmp_path):
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(
        state_dir=str(state), decider=lambda *_: {"action": "done"}, stub=True)
    mission = service.start("coordinate specialists", may=["research"])
    mid = mission["mission_id"]

    root = service.create_run_tree(
        mid, [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))["root"]
    child = service.spawn_specialist(
        mid, "reader", "inspect parser",
        resources=[{"kind": "file", "id": str(repo / "parser.py"), "mode": "read"}],
        workspace=str(repo))
    sibling = service.spawn_specialist(
        mid, "reviewer", "inspect tests",
        resources=[{"kind": "file", "id": str(repo / "tests.py"), "mode": "read"}],
        workspace=str(repo))
    cancelled_processes = []

    class CodeProcess:
        def cancel_current(self, mission_id=None, include_persisted=True):
            if mission_id:
                cancelled_processes.append(mission_id)
            return True

    service._code_process = CodeProcess()

    status = service.status(mid)
    assert status["tasktree"]["attached"] is True
    assert status["run_tree"]["root"]["run_id"] == root["run_id"]
    steer = service.steer_specialist(child["run_id"], "also inspect tests")
    assert steer["queued"] is True
    inspected = service.inspect_specialist(child["run_id"])
    assert inspected["run"]["mission_id"].startswith("spc_")
    assert any(event["kind"] == "steer_queued" for event in inspected["events"])

    cancelled = service.cancel_specialist(child["run_id"])
    assert cancelled["run"]["status"] == CANCELLED
    assert cancelled["bound_missions_cancelled"] == 1
    assert service.store.get(child["mission_id"]).state == CANCELLED

    model_cancelled = service.agent_cancel(mid, sibling["run_id"])
    assert model_cancelled["ok"] is True
    assert model_cancelled["status"] == CANCELLED
    assert model_cancelled["bound_missions_cancelled"] == 1
    assert service.store.get(sibling["mission_id"]).state == CANCELLED
    assert cancelled_processes == [child["mission_id"], sibling["mission_id"]]
    service.close()

    reopened = TaskTreeStore(str(state / "tasktree.db"))
    try:
        assert reopened.get(child["run_id"])["status"] == CANCELLED
        assert reopened.get(sibling["run_id"])["status"] == CANCELLED
    finally:
        reopened.close()


@pytest.mark.parametrize("cancel_api", ["agent", "operator"])
def test_specialist_cancel_sweeps_mission_committed_after_initial_tree_snapshot(
        tmp_path, monkeypatch, cancel_api):
    """A child created in the cross-database fence window cannot remain runnable."""
    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {"action": "done"},
        stub=True)
    parent = service.start("coordinate a cancellation race", may=["research"])
    mid = parent["mission_id"]
    service.create_run_tree(mid, [], workspace=str(repo))
    target = service.spawn_specialist(
        mid, "target", "coordinate nested work", resources=[],
        workspace=str(repo))

    original_tree = service._run_tree.tree
    inserted = {}

    def snapshot_then_commit_late_child(run_id):
        # Return the pre-spawn snapshot to the first cancellation sweep, while
        # committing the concurrent TaskTree+Mission pair before it starts
        # cancelling the rows in that snapshot.
        snapshot = original_tree(run_id)
        if run_id == target["run_id"] and not inserted:
            late = service._run_tree.spawn_specialist(
                run_id, "late", "work created during cancellation",
                resources=[], workspace=str(repo))
            late = service._create_specialist_mission(target["mission_id"], late)
            inserted.update(late)
        return snapshot

    monkeypatch.setattr(
        service._run_tree, "tree", snapshot_then_commit_late_child)
    cancelled = (service.agent_cancel(mid, target["run_id"])
                 if cancel_api == "agent" else
                 service.cancel_specialist(target["run_id"]))

    late_mid = inserted["mission_id"]
    if cancel_api == "agent":
        assert cancelled["ok"] is True
    else:
        assert cancelled["run"]["status"] == CANCELLED
    assert cancelled["bound_missions_cancelled"] == 2
    assert service._run_tree.get(inserted["run_id"])["status"] == CANCELLED
    assert service.store.get(late_mid).state == CANCELLED
    service.close()


def test_specialist_creation_cancels_mission_when_run_is_fenced_after_insert(
        tmp_path, monkeypatch):
    """The creator-side handshake catches cancellation after its Mission commit."""
    import harness.missionweb as missionweb

    repo = tmp_path / "repo"
    repo.mkdir()
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {"action": "done"},
        stub=True)
    parent = service.start("coordinate a creator-side race", may=["research"])
    mid = parent["mission_id"]
    service.create_run_tree(mid, [], workspace=str(repo))

    original_create = missionweb.create_mission

    def create_then_fence(*args, **kwargs):
        created = original_create(*args, **kwargs)
        service._run_tree.request_cancel(kwargs["external_run_id"])
        return created

    monkeypatch.setattr(missionweb, "create_mission", create_then_fence)
    child = service.spawn_specialist(
        mid, "late", "commit while cancellation fences the run",
        resources=[], workspace=str(repo))

    assert child["status"] == CANCELLED
    assert service.store.get(child["mission_id"]).state == CANCELLED
    assert service.store.claim_run(child["mission_id"]) is None
    service.close()


def test_parent_mission_cancel_propagates_to_bound_specialists(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tasktree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {"action": "done"},
        stub=True, run_tree=tree)
    repo = tmp_path / "repo"
    repo.mkdir()
    mission = service.start("coordinate cancellable specialists", may=["research"])
    mid = mission["mission_id"]
    root = service.create_run_tree(
        mid, [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))["root"]
    running = service.spawn_specialist(
        mid, "running", "stop at its next boundary",
        resources=[{"kind": "file", "id": str(repo / "running.py"), "mode": "read"}],
        workspace=str(repo))
    queued = service.spawn_specialist(
        mid, "queued", "never start",
        resources=[{"kind": "file", "id": str(repo / "queued.py"), "mode": "read"}],
        workspace=str(repo))
    token = tree.claim(running["run_id"])
    assert token

    cancelled = service.cancel(mid)
    assert cancelled["state"] == CANCELLED and "error" not in cancelled
    assert "in-flight action may still finish" in cancelled["result"]
    assert tree.get(root["run_id"])["status"] == CANCELLED
    assert tree.get(queued["run_id"])["status"] == CANCELLED
    assert tree.get(running["run_id"])["status"] == CANCEL_REQUESTED
    assert service.store.get(queued["mission_id"]).state == CANCELLED
    assert service.store.get(running["mission_id"]).state == CANCELLED
    assert service.tick()["specialists_advanced"] == 0
    assert tree.claim(queued["run_id"]) is None
    assert [row["kind"] for row in tree.claim_messages(running["run_id"], token)] == [
        "cancel"]

    service.close()
    tree.close()


def test_close_preserves_injected_tasktree_and_hooks(tmp_path):
    class Hooks:
        active = False
        pending = []

        def __init__(self):
            self.closed = 0

        def dispatch(self, *_args, **_kwargs):
            return None

        def close(self):
            self.closed += 1

    hooks = Hooks()
    tree = TaskTreeStore(str(tmp_path / "shared-tasktree.db"), hooks=hooks)
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True,
        run_tree=tree, hooks=hooks)
    service.close()
    service.close()  # close is idempotent

    assert tree.list_runs() == []
    assert hooks.closed == 0
    tree.close()
