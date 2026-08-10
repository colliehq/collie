import os
import time

import pytest

from harness.mission import world_leash
from harness.missionweb import MissionService
from harness.tasktree import (BLOCKED, CANCEL_REQUESTED, CANCELLED, COMPLETED,
                              QUEUED, RECOVERY_REQUIRED, RUNNING,
                              WORKSPACE_REQUIRED, TaskTreeStore, narrow_leash)
from harness.verifier import VERIFIED, Observation, Verdict


def _root(store, tmp_path, **leash_overrides):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    leash = world_leash(may=["research", "code"], **leash_overrides)
    root = store.create_root(
        "root task", leash,
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo), workspace_mode="current")
    return root, repo


def test_worktree_default_is_explicit_and_bindable(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    repo = tmp_path / "repo"
    repo.mkdir()
    run = store.create_root(
        "isolated", world_leash(),
        [{"kind": "file", "id": str(repo), "mode": "write"}])
    assert run["status"] == WORKSPACE_REQUIRED
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    bound = store.bind_workspace(run["run_id"], str(worktree), owns_workspace=True)
    assert bound["status"] == QUEUED
    assert bound["workspace"] == os.path.realpath(str(worktree))
    assert bound["owns_workspace"] is True


def test_specialist_leash_and_resources_can_only_narrow(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(store, tmp_path, max_model_tokens=100)
    child_file = repo / "parser.py"
    child = store.spawn_specialist(
        root["run_id"], "parser", "inspect parser",
        leash={**root["leash"], "may": ["research"], "max_model_tokens": 50,
               "workspace_mode": "isolated"},
        resources=[{"kind": "file", "id": str(child_file), "mode": "write"}],
        workspace=str(repo), workspace_mode="worktree")
    assert child["leash"]["may"] == ["research"]
    assert child["depth"] == 1

    expanded = dict(root["leash"], max_model_tokens=101)
    with pytest.raises(ValueError, match="cannot exceed parent"):
        store.spawn_specialist(root["run_id"], "bad", "expand", leash=expanded,
                               resources=[], workspace=str(repo))
    with pytest.raises(ValueError, match="expands parent ownership"):
        store.spawn_specialist(
            root["run_id"], "bad", "escape", resources=[
                {"kind": "file", "id": str(tmp_path / "elsewhere"), "mode": "write"}],
            workspace=str(repo))


def test_write_ownership_is_visible_and_sibling_conflicts_fail(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(store, tmp_path)
    owned = repo / "owned.py"
    child = store.spawn_specialist(
        root["run_id"], "owner", "edit one file",
        resources=[{"kind": "file", "id": str(owned), "mode": "write"}],
        workspace=str(repo))
    ok, reason = store.can_access(root["run_id"], str(owned), "write")
    assert not ok and child["run_id"] in reason
    assert store.can_access(root["run_id"], str(repo / "other.py"), "write")[0]
    with pytest.raises(ValueError, match="already owned"):
        store.spawn_specialist(
            root["run_id"], "other", "same file",
            resources=[{"kind": "file", "id": str(owned), "mode": "write"}],
            workspace=str(repo))

    observed = repo / "observed.py"
    reader = store.spawn_specialist(
        root["run_id"], "reader", "inspect without mutation",
        resources=[{"kind": "file", "id": str(observed), "mode": "read"}],
        workspace=str(repo))
    ok, reason = store.can_access(root["run_id"], str(observed), "write")
    assert not ok and reader["run_id"] in reason, (
        "a parent write must not race a delegated reader's stable view")


def test_background_progress_steer_and_cancel_ack_are_durable(tmp_path):
    path = str(tmp_path / "tree.db")
    store = TaskTreeStore(path)
    root, _repo = _root(store, tmp_path)
    token = store.claim(root["run_id"], lease_s=30)
    assert token and store.get(root["run_id"])["status"] == RUNNING
    assert store.set_background(root["run_id"], True, token)
    assert store.progress(root["run_id"], token, "indexed files", percent=25)

    message_id = store.steer(root["run_id"], "focus on parser")
    messages = store.claim_messages(root["run_id"], token)
    assert messages[0]["message_id"] == message_id
    assert messages[0]["payload"]["text"] == "focus on parser"
    assert store.ack_message(root["run_id"], token, message_id)

    assert store.request_cancel(root["run_id"])
    assert store.get(root["run_id"])["status"] == CANCEL_REQUESTED
    cancel = store.claim_messages(root["run_id"], token)
    assert cancel and cancel[0]["kind"] == "cancel"
    assert store.ack_cancel(root["run_id"], token)
    assert store.get(root["run_id"])["status"] == CANCELLED
    assert store.notifications()[0]["kind"] == "cancelled"
    store.close()
    assert TaskTreeStore(path).get(root["run_id"])["cancel_ack_at"] > 0


def test_parent_cancel_atomically_stops_queued_and_running_descendants(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(store, tmp_path)
    running = store.spawn_specialist(
        root["run_id"], "reader", "currently running",
        resources=[{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace=str(repo))
    queued = store.spawn_specialist(
        root["run_id"], "queued", "must never start",
        resources=[{"kind": "file", "id": str(repo / "queued.py"), "mode": "read"}],
        workspace=str(repo))
    grandchild = store.spawn_specialist(
        running["run_id"], "nested", "must also never start",
        resources=[{"kind": "file", "id": str(repo / "nested.py"), "mode": "read"}],
        workspace=str(repo))
    token = store.claim(running["run_id"])
    assert token

    assert store.request_cancel(root["run_id"])
    assert store.get(root["run_id"])["status"] == CANCELLED
    assert store.get(queued["run_id"])["status"] == CANCELLED
    assert store.get(grandchild["run_id"])["status"] == CANCELLED
    assert store.get(running["run_id"])["status"] == CANCEL_REQUESTED
    assert store.claim(queued["run_id"]) is None
    assert store.claim(grandchild["run_id"]) is None
    with pytest.raises(ValueError, match="stopping or terminal"):
        store.spawn_specialist(
            running["run_id"], "late", "must not gain authority after cancel",
            resources=[], workspace=str(repo))

    # Repeating the parent decision is safe while its live descendant drains and does not enqueue
    # duplicate cancellation messages.
    assert store.request_cancel(root["run_id"])
    messages = store.claim_messages(running["run_id"], token)
    assert [row["kind"] for row in messages] == ["cancel"]
    assert store.ack_cancel(running["run_id"], token)
    assert not store.request_cancel(root["run_id"])


def test_block_resume_completion_and_child_mailbox(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(store, tmp_path)
    root_token = store.claim(root["run_id"])
    child = store.spawn_specialist(
        root["run_id"], "reader", "read file",
        resources=[{"kind": "file", "id": str(repo / "a.py"), "mode": "read"}],
        workspace=str(repo))
    child_token = store.claim(child["run_id"])
    assert store.block(child["run_id"], child_token, "provider unavailable")
    assert store.get(child["run_id"])["status"] == BLOCKED
    assert store.resume(child["run_id"])
    child_token = store.claim(child["run_id"])
    assert store.complete(child["run_id"], child_token, "summary")
    assert store.get(child["run_id"])["status"] == COMPLETED
    results = store.claim_messages(root["run_id"], root_token)
    assert results[0]["kind"] == "child_result"
    assert results[0]["payload"]["result"] == "summary"


def test_child_usage_charges_every_ancestor_and_stale_claim_fails_closed(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(store, tmp_path, max_model_tokens=10)
    child = store.spawn_specialist(
        root["run_id"], "reader", "consume budget",
        leash={**root["leash"], "max_model_tokens": 6, "workspace_mode": "isolated"},
        resources=[{"kind": "file", "id": str(repo / "a.py"), "mode": "read"}],
        workspace=str(repo))
    token = store.claim(child["run_id"], lease_s=0)
    exhausted = store.account_usage(child["run_id"], token, input_tokens=6)
    assert (child["run_id"], "model-token budget exhausted") in exhausted
    assert store.get(root["run_id"])["input_tokens"] == 6
    assert store.recover_stale(int(time.time()) + 1) == 1
    assert store.get(child["run_id"])["status"] == RECOVERY_REQUIRED
    assert store.reconcile(child["run_id"], "worktree inspected")
    assert store.get(child["run_id"])["status"] == QUEUED


def test_stale_worker_requeues_unacked_steering_and_reconcile_supersedes_cancel(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, _repo = _root(store, tmp_path)
    token = store.claim(root["run_id"], lease_s=1)
    steer_id = store.steer(root["run_id"], "preserve this direction")
    assert store.claim_messages(root["run_id"], token)[0]["message_id"] == steer_id
    assert store.recover_stale(int(time.time()) + 2) == 1
    assert store.reconcile(root["run_id"], "worker inspected")
    fresh = store.claim(root["run_id"])
    replayed = store.claim_messages(root["run_id"], fresh)
    assert [row["message_id"] for row in replayed] == [steer_id]
    assert store.ack_message(root["run_id"], fresh, steer_id)

    assert store.request_cancel(root["run_id"])
    cancel = store.claim_messages(root["run_id"], fresh)
    assert cancel and cancel[0]["kind"] == "cancel"
    assert store.recover_stale(int(time.time()) + 400) == 1
    assert store.reconcile(root["run_id"], "explicitly resume instead of cancel")
    resumed = store.claim(root["run_id"])
    assert store.claim_messages(root["run_id"], resumed) == []


def test_specialist_scheduler_poll_recovers_crashed_worker(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(store, tmp_path)
    child = store.spawn_specialist(
        root["run_id"], "reader", "survive a dispatcher restart",
        resources=[{"kind": "file", "id": str(repo / "a.py"), "mode": "read"}],
        workspace=str(repo))
    assert store.claim(child["run_id"], lease_s=0)

    # MissionService's specialist dispatcher polls through this query.  A
    # durable worker must therefore become recoverable without a separate
    # operator/API call after the process restarts.
    recoverable = store.list_runs(RECOVERY_REQUIRED, specialists_only=True)
    assert [run["run_id"] for run in recoverable] == [child["run_id"]]
    assert store.get(child["run_id"])["owner_token"] == ""


def test_exhausted_ancestor_budget_blocks_fresh_specialist_claim(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(store, tmp_path, max_model_tokens=5)
    first = store.spawn_specialist(
        root["run_id"], "first", "consume shared budget",
        resources=[{"kind": "file", "id": str(repo / "a.py"), "mode": "read"}],
        workspace=str(repo))
    second = store.spawn_specialist(
        root["run_id"], "second", "must not escape shared budget",
        resources=[{"kind": "file", "id": str(repo / "b.py"), "mode": "read"}],
        workspace=str(repo))
    token = store.claim(first["run_id"])
    store.account_usage(first["run_id"], token, input_tokens=5)
    assert store.complete(first["run_id"], token, "spent")

    assert store.claim(second["run_id"]) is None
    blocked = store.get(second["run_id"])
    assert blocked["status"] == "needs_you"
    assert root["run_id"] in blocked["result"]


def test_standalone_narrow_leash_rejects_capability_and_autonomy_expansion():
    parent = world_leash(may=["web.*"], autonomous=False)
    assert narrow_leash(parent, {**parent, "may": ["web.send"]})["may"] == ["web.send"]
    with pytest.raises(ValueError, match="capabilities"):
        narrow_leash(parent, {**parent, "may": ["code"]})
    with pytest.raises(ValueError, match="irreversible"):
        narrow_leash(parent, {**parent, "irreversible": "allow"})


def test_mission_service_exposes_run_tree_wiring_without_ui_changes(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(base=str(tmp_path / "svc"), decider=lambda *_: {},
                             stub=True, run_tree=tree)
    mission = service.start("coordinate specialists", may=["research"])
    repo = tmp_path / "repo"
    repo.mkdir()
    attached = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))
    assert attached["root"]["mission_id"] == mission["mission_id"]
    child = service.spawn_specialist(
        mission["mission_id"], "reader", "inspect",
        resources=[{"kind": "file", "id": str(repo / "a.py"), "mode": "read"}],
        workspace=str(repo))
    assert child["parent_run_id"] == attached["root"]["run_id"]
    assert service.status(mission["mission_id"])["run_tree"]["flat"][-1]["role"] == "reader"


def test_specialist_dispatcher_runs_real_child_model_and_tool_to_completion(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))

    def decider(_goal, case, _primitives):
        if not case.get("researched"):
            return {"action": "research", "args": {"query": "parser ownership"}}
        return {"action": "done", "reason": "research captured"}

    service = MissionService(
        base=str(tmp_path / "svc"), decider=decider, stub=True, run_tree=tree,
        goal_verifier=lambda *_: Verdict(
            VERIFIED, "independent child evidence", (
                Observation("specialist-contract", time.time(), True,
                            asserted=True, detail="child outcome observed"),)),
        specialist_workers=2)
    mission = service.start("orchestrate", may=["research"])
    repo = tmp_path / "repo"
    repo.mkdir()
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))["root"]
    child = service.spawn_specialist(
        mission["mission_id"], "researcher", "inspect parser ownership",
        resources=[{"kind": "file", "id": str(repo / "parser.py"), "mode": "read"}],
        workspace=str(repo))
    assert child["mission_id"].startswith("spc_")

    tick = service.tick()
    finished = tree.get(child["run_id"])
    child_mission = service.store.get(child["mission_id"])
    assert tick["specialists_advanced"] == 1
    assert finished["status"] == COMPLETED
    assert child_mission.state == "done_verified"
    assert child_mission.case["researched"] is True
    assert any(event["kind"] == "completed" for event in tree.events(child["run_id"]))


def test_task_and_mission_lifecycle_hooks_have_backend_dispatch_points(tmp_path):
    class HookResult:
        allowed = True
        reason = ""

    class Hooks:
        def __init__(self):
            self.calls = []

        def dispatch(self, event, payload, subject=""):
            self.calls.append((event, payload, subject))
            return HookResult()

    hooks = Hooks()
    store = TaskTreeStore(str(tmp_path / "tree.db"), hooks=hooks)
    root, _repo = _root(store, tmp_path)
    token = store.claim(root["run_id"])
    assert store.complete(root["run_id"], token, "done")
    assert [call[0] for call in hooks.calls][:2] == ["TaskCreated", "TaskCompleted"]

    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {"action": "done"},
        stub=True, hooks=hooks,
        goal_verifier=lambda *_: Verdict(
            VERIFIED, "verified", (
                Observation("hook-contract", time.time(), True, asserted=True),)))
    mission = service.start("finish")
    service.run(mission["mission_id"])
    assert any(event == "Stop" and payload["mission_id"] == mission["mission_id"]
               for event, payload, _subject in hooks.calls)
