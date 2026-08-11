import hashlib
import os
import sqlite3
import threading
import time

import pytest

from harness.mission import world_leash
from harness.missionweb import MissionService
from harness.tasktree import (BLOCKED, CANCEL_REQUESTED, CANCELLED, COMPLETED,
                              FAILED, NEEDS_YOU, QUEUED, RECOVERY_REQUIRED, RUNNING,
                              WORKSPACE_REQUIRED, TaskTreeStore, narrow_leash)
from harness.verifier import VERIFIED, Observation, Verdict


def _root(store, tmp_path, mission_id="", **leash_overrides):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    leash = world_leash(may=["research", "code"], **leash_overrides)
    root = store.create_root(
        "root task", leash,
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        mission_id=mission_id, workspace=str(repo), workspace_mode="current")
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


def test_workspace_and_mission_bindings_are_immutable_once_set(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(store, tmp_path)
    other = tmp_path / "other"
    other.mkdir()

    assert store.bind_mission(root["run_id"], "mission-a")
    assert store.bind_mission(root["run_id"], "mission-a")
    assert not store.bind_mission(root["run_id"], "mission-b")
    assert store.get(root["run_id"])["mission_id"] == "mission-a"

    # _root bound repo at creation time; a later caller cannot redirect the
    # run's execution authority to a different directory.
    assert store.bind_workspace(root["run_id"], str(repo))
    assert store.bind_workspace(root["run_id"], str(other)) is None
    assert store.get(root["run_id"])["workspace"] == os.path.realpath(str(repo))


def test_specialist_replay_uses_canonical_spawn_workspace_and_reports_collision(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(store, tmp_path)
    nested = repo / "nested"
    nested.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    run_id = "run_workspace_identity"
    first = store.spawn_specialist(
        root["run_id"], "reader", "inspect canonical workspace",
        resources=[], workspace=str(repo), run_id=run_id)
    replay = store.spawn_specialist(
        root["run_id"], "reader", "inspect canonical workspace",
        resources=[], workspace=os.path.join(str(nested), ".."), run_id=run_id)
    assert replay["run_id"] == first["run_id"]

    with pytest.raises(ValueError, match="run id collision"):
        store.spawn_specialist(
            root["run_id"], "reader", "inspect canonical workspace",
            resources=[], workspace=str(other), run_id=run_id)


def test_root_replay_keeps_its_spawn_workspace_identity_after_binding(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    repo = tmp_path / "repo"
    repo.mkdir()
    leash = world_leash(max_specialist_depth=2)
    run_id = "run_root_workspace_identity"
    root = store.create_root(
        "provision later", leash, [], run_id=run_id, workspace="")
    assert root["status"] == WORKSPACE_REQUIRED
    assert store.bind_workspace(run_id, str(repo), owns_workspace=True)

    replay = store.create_root(
        "provision later", leash, [], run_id=run_id, workspace="")
    assert replay["workspace"] == os.path.realpath(str(repo))
    assert replay["spawn_workspace"] == ""

    with pytest.raises(ValueError, match="root run id collision"):
        store.create_root(
            "provision later", leash, [], run_id=run_id, workspace=str(repo))


def test_tasktree_legacy_schema_migrates_spawn_workspace_and_cache_budget(tmp_path):
    path = str(tmp_path / "legacy-tree.db")
    store = TaskTreeStore(path)
    root, repo = _root(store, tmp_path, max_model_tokens=5)
    child = store.spawn_specialist(
        root["run_id"], "writer", "upgrade safely", resources=[],
        workspace="", run_id="run_legacy_writer")
    worktree = tmp_path / "legacy-worktree"
    worktree.mkdir()
    assert store.bind_workspace(child["run_id"], str(worktree), owns_workspace=True)
    assert store.bind_mission(child["run_id"], "spc_legacy_writer")
    token = store.claim(child["run_id"])
    assert store.account_usage(
        child["run_id"], token, cache_tokens=5, model_calls=2, turns=3)
    store.close()

    legacy = sqlite3.connect(path)
    legacy.execute("ALTER TABLE agent_runs DROP COLUMN spawn_workspace")
    legacy.execute("ALTER TABLE agent_runs DROP COLUMN cache_tokens")
    legacy.execute("ALTER TABLE agent_runs DROP COLUMN model_calls")
    legacy.execute("ALTER TABLE agent_runs DROP COLUMN turns")
    # Simulate a process dying after CREATE TABLE but before old rows and the
    # durable migration marker were committed.
    legacy.execute("DELETE FROM agent_mission_usage_projection")
    legacy.execute(
        "DELETE FROM tasktree_schema_migrations "
        "WHERE name='mission_usage_projection_v1'")
    legacy.commit()
    legacy.close()

    reopened = TaskTreeStore(path)
    migrated = reopened.get(child["run_id"])
    assert migrated["spawn_workspace"] == ""
    assert migrated["cache_tokens"] == 0
    assert migrated["model_calls"] == 0
    assert migrated["turns"] == 0
    projection = reopened.mission_usage_projection(child["run_id"])
    assert projection and projection["initialized"] is False
    replay = reopened.spawn_specialist(
        root["run_id"], "writer", "upgrade safely", resources=[],
        workspace="", run_id=child["run_id"])
    assert replay["workspace"] == os.path.realpath(str(worktree))

    exhausted = reopened.project_mission_usage(
        child["run_id"], "spc_legacy_writer",
        cache_tokens=5, model_calls=2, turns=3)
    assert (child["run_id"], "model-token budget exhausted") in exhausted
    assert (root["run_id"], "model-token budget exhausted") in exhausted
    assert reopened.get(child["run_id"])["cache_tokens"] == 5
    assert reopened.get(root["run_id"])["cache_tokens"] == 5
    assert reopened.get(child["run_id"])["model_calls"] == 2
    assert reopened.get(root["run_id"])["model_calls"] == 2
    # Reconciliation replays the same absolute receipt without charging twice.
    reopened.project_mission_usage(
        child["run_id"], "spc_legacy_writer",
        cache_tokens=5, model_calls=2, turns=3)
    assert reopened.get(root["run_id"])["cache_tokens"] == 5
    assert reopened.get(root["run_id"])["turns"] == 3


def test_spawn_workspace_backfill_resumes_when_column_already_exists(tmp_path):
    path = str(tmp_path / "partial-workspace-migration.db")
    store = TaskTreeStore(path)
    root, repo = _root(store, tmp_path)
    run_id = root["run_id"]
    store.close()

    partial = sqlite3.connect(path)
    partial.execute(
        "UPDATE agent_runs SET spawn_workspace='' WHERE run_id=?", (run_id,))
    partial.execute(
        "DELETE FROM tasktree_schema_migrations WHERE name='spawn_workspace_v1'")
    partial.commit()
    partial.close()

    reopened = TaskTreeStore(path)
    assert reopened.get(run_id)["spawn_workspace"] == os.path.realpath(str(repo))
    replay = reopened.create_root(
        "root task", root["leash"], root["resources"], run_id=run_id,
        workspace=str(repo), workspace_mode="current")
    assert replay["run_id"] == run_id


def test_long_mission_id_survives_usage_projection(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    mission_id = "mission-" + "x" * 140
    root, _repo = _root(store, tmp_path, mission_id=mission_id)

    store.project_mission_usage(
        root["run_id"], mission_id, input_tokens=7,
        model_calls=1, turns=1)

    assert store.get(root["run_id"])["mission_id"] == mission_id
    assert store.mission_usage_projection(root["run_id"])["mission_id"] == mission_id
    assert store.get(root["run_id"])["input_tokens"] == 7


def test_legacy_projection_recovers_root_own_usage_hidden_by_child_aggregate(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(store, tmp_path, mission_id="msn_root")
    child = store.spawn_specialist(
        root["run_id"], "reader", "inspect", resources=[],
        workspace=str(repo), run_id="run_legacy_child_usage")
    assert store.bind_mission(child["run_id"], "spc_child")

    # Old accounting charged the child's 100 tokens to both rows but never
    # projected the root Mission's own 50 tokens.
    store.db.execute(
        "UPDATE agent_runs SET input_tokens=100 WHERE run_id IN (?,?)",
        (root["run_id"], child["run_id"]))
    store.db.execute(
        "UPDATE agent_mission_usage_projection SET initialized=0,input_tokens=0 "
        "WHERE run_id IN (?,?)", (root["run_id"], child["run_id"]))
    store.db.commit()

    store.project_mission_usage(root["run_id"], "msn_root", input_tokens=50)
    store.project_mission_usage(child["run_id"], "spc_child", input_tokens=100)

    assert store.get(child["run_id"])["input_tokens"] == 100
    assert store.get(root["run_id"])["input_tokens"] == 150


def test_usage_reconciliation_poll_excludes_clean_terminal_history(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, _repo = _root(store, tmp_path, mission_id="msn_root")
    store.project_mission_usage(root["run_id"], "msn_root", input_tokens=1)
    store.db.execute(
        "UPDATE agent_runs SET status=? WHERE run_id=?", (COMPLETED, root["run_id"]))
    store.db.commit()
    assert store.usage_reconciliation_runs() == []

    store.db.execute(
        "UPDATE agent_mission_usage_projection SET initialized=0 WHERE run_id=?",
        (root["run_id"],))
    store.db.commit()
    assert [row["run_id"] for row in store.usage_reconciliation_runs()] == [root["run_id"]]


def test_absolute_mission_usage_projection_is_atomic_idempotent_and_public(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(store, tmp_path, mission_id="msn_root")
    child = store.spawn_specialist(
        root["run_id"], "reader", "inspect", resources=[],
        workspace=str(repo), run_id="run_usage_child")
    assert store.bind_mission(child["run_id"], "spc_child")

    store.project_mission_usage(
        root["run_id"], "msn_root", input_tokens=4, cache_tokens=1,
        model_calls=1, turns=1, model_cost_microusd=100_000,
        wall_ms=10)
    store.project_mission_usage(
        child["run_id"], "spc_child", input_tokens=2, output_tokens=3,
        cache_tokens=4, model_calls=2, turns=2,
        model_cost_microusd=200_000, wall_ms=20, retries=1)
    aggregate = store.get(root["run_id"])
    own_child = store.get(child["run_id"])
    assert (aggregate["input_tokens"], aggregate["output_tokens"],
            aggregate["cache_tokens"]) == (6, 3, 5)
    assert (aggregate["model_calls"], aggregate["turns"],
            aggregate["active_wall_ms"], aggregate["retry_count"]) == (3, 3, 30, 1)
    assert aggregate["model_cost_usd"] == pytest.approx(0.3)
    assert own_child["input_tokens"] == 2

    # Exact replay does nothing; a later absolute receipt charges only its
    # positive delta to the actor and every ancestor.
    store.project_mission_usage(
        child["run_id"], "spc_child", input_tokens=2, output_tokens=3,
        cache_tokens=4, model_calls=2, turns=2,
        model_cost_microusd=200_000, wall_ms=20, retries=1)
    store.project_mission_usage(
        child["run_id"], "spc_child", input_tokens=5, output_tokens=3,
        cache_tokens=4, model_calls=3, turns=3,
        model_cost_microusd=250_000, wall_ms=25, retries=1)
    aggregate = store.get(root["run_id"])
    assert aggregate["input_tokens"] == 9
    assert aggregate["model_calls"] == 4
    assert aggregate["turns"] == 4
    assert aggregate["model_cost_usd"] == pytest.approx(0.35)

    from harness.webapp import _public_task_run
    public = _public_task_run(aggregate)
    assert public["cache_tokens"] == 5
    assert public["model_calls"] == 4
    assert public["turns"] == 4


def test_concurrent_absolute_projection_charges_one_delta(tmp_path):
    path = str(tmp_path / "tree.db")
    setup = TaskTreeStore(path)
    root, repo = _root(setup, tmp_path, mission_id="msn_root")
    child = setup.spawn_specialist(
        root["run_id"], "reader", "inspect", resources=[],
        workspace=str(repo), run_id="run_concurrent_usage")
    assert setup.bind_mission(child["run_id"], "spc_child")
    setup.close()

    left, right = TaskTreeStore(path), TaskTreeStore(path)
    barrier = threading.Barrier(2)
    errors = []

    def project(store):
        try:
            barrier.wait()
            store.project_mission_usage(
                child["run_id"], "spc_child", input_tokens=5,
                model_calls=1, turns=1)
        except Exception as exc:  # surfaced below with both worker outcomes
            errors.append(exc)

    threads = [threading.Thread(target=project, args=(store,))
               for store in (left, right)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert left.get(child["run_id"])["input_tokens"] == 5
    assert left.get(root["run_id"])["input_tokens"] == 5
    assert left.get(root["run_id"])["model_calls"] == 1
    left.close()
    right.close()


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


def test_bound_child_result_delivery_is_structured_replayable_and_idempotently_acked(tmp_path):
    path = str(tmp_path / "tree.db")
    store = TaskTreeStore(path)
    root, repo = _root(store, tmp_path, mission_id="msn_parent")
    child = store.spawn_specialist(
        root["run_id"], "reader", "produce a report",
        resources=[{"kind": "file", "id": str(repo / "report.md"), "mode": "read"}],
        workspace=str(repo))
    token = store.claim(child["run_id"])
    assert store.complete(
        child["run_id"], token, "report ready",
        artifacts=[{"kind": "report", "uri": "collie://reports/one",
                    "digest": "sha256:abc"}],
        observation={"mission_state": "done_verified", "verified": True})
    store.close()
    store = TaskTreeStore(path)

    assert store.claim_child_results(root["run_id"], "another_mission") == []
    first = store.claim_child_results(root["run_id"], "msn_parent")
    replay = store.claim_child_results(root["run_id"], "msn_parent")
    assert [row["message_id"] for row in replay] == [first[0]["message_id"]]
    payload = first[0]["payload"]
    assert payload["role"] == "reader"
    assert payload["result"] == "report ready"
    assert payload["observation"]["verified"] is True
    assert payload["artifacts"] == [{
        "digest": "sha256:abc", "kind": "report", "uri": "collie://reports/one"}]

    message_id = first[0]["message_id"]
    assert store.ack_child_result(root["run_id"], "msn_parent", message_id)
    assert store.ack_child_result(root["run_id"], "msn_parent", message_id)
    assert store.claim_child_results(root["run_id"], "msn_parent") == []


def test_result_replay_acks_ids_trimmed_from_bounded_parent_case(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True, run_tree=tree)
    mission = service.start("aggregate many specialists", may=["research", "agent.*"])
    mid = mission["mission_id"]
    repo = tmp_path / "repo"
    repo.mkdir()
    root = service.create_run_tree(
        mid, [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))["root"]

    def complete_child(index):
        child = tree.spawn_specialist(
            root["run_id"], "reader-%d" % index, "result %d" % index,
            resources=[{"kind": "file", "id": str(repo / ("%d.txt" % index)),
                        "mode": "read"}], workspace=str(repo))
        token = tree.claim(child["run_id"])
        assert tree.complete(child["run_id"], token, "result-%d" % index)

    complete_child(0)
    replayed = tree.claim_child_results(root["run_id"], mid)
    old_id = replayed[0]["message_id"]
    case = dict(service.store.get(mid).case)
    case["specialist_results"] = [
        {"message_id": old_id, "run_id": "old", "result": "already folded"}
    ] + [{"message_id": -index, "run_id": "history-%d" % index}
         for index in range(1, 20)]
    service.store.set_case(mid, case)
    for index in range(1, 20):
        complete_child(index)

    mission_token = service.store.claim_run(mid, expected=(QUEUED,))
    assert mission_token
    assert service._fold_child_results(mid, root["run_id"], mission_token) == 19
    states = tree.db.execute(
        "SELECT state FROM agent_mailbox WHERE run_id=? AND kind='child_result' "
        "ORDER BY message_id", (root["run_id"],)).fetchall()
    assert len(states) == 20 and {row["state"] for row in states} == {"acked"}
    final_ids = {item["message_id"] for item in
                 service.store.get(mid).case["specialist_results"]}
    assert len(final_ids) == 20 and old_id not in final_ids
    assert tree.claim_child_results(root["run_id"], mid) == []


def test_descendant_control_never_crosses_agent_tree_or_terminal_boundary(tmp_path):
    store = TaskTreeStore(str(tmp_path / "tree.db"))
    left, repo = _root(store, tmp_path, mission_id="msn_left")
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    right = store.create_root(
        "other root", world_leash(may=["research", "agent.*"]),
        [{"kind": "file", "id": str(other_repo), "mode": "write"}],
        mission_id="msn_right", workspace=str(other_repo), workspace_mode="current")
    child = store.spawn_specialist(
        left["run_id"], "reader", "bounded child",
        resources=[{"kind": "file", "id": str(repo / "a.py"), "mode": "read"}],
        workspace=str(repo))

    with pytest.raises(ValueError, match="outside caller descendant scope"):
        store.send_to_descendant(right["run_id"], child["run_id"], "cross-tree steer")
    with pytest.raises(ValueError, match="outside caller descendant scope"):
        store.cancel_descendant(right["run_id"], child["run_id"])

    token = store.claim(child["run_id"])
    assert store.park_waiting(child["run_id"], token, "awaiting direction")
    steer_id = store.send_to_descendant(
        left["run_id"], child["run_id"], "continue with the bounded task")
    assert steer_id and store.get(child["run_id"])["status"] == QUEUED
    token = store.claim(child["run_id"])
    assert [row["message_id"] for row in
            store.claim_messages(child["run_id"], token)] == [steer_id]
    assert store.ack_message(child["run_id"], token, steer_id)
    assert store.complete(child["run_id"], token, "done")
    assert store.send_to_descendant(left["run_id"], child["run_id"], "too late") is None
    assert store.cancel_descendant(left["run_id"], child["run_id"]) is False


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
    assert store.account_usage(child["run_id"], token, input_tokens=1) == [
        "run ownership lost"]
    assert store.account_usage("run_missing", token, input_tokens=1) == []
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


def test_model_agent_controls_enforce_mission_lineage_and_resource_scope(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True, run_tree=tree)
    left_repo, right_repo = tmp_path / "left", tmp_path / "right"
    left_repo.mkdir(); right_repo.mkdir()
    left = service.start("left orchestrator", may=["research", "agent.*"])
    right = service.start("right orchestrator", may=["research", "agent.*"])
    service.create_run_tree(
        left["mission_id"],
        [{"kind": "file", "id": str(left_repo), "mode": "write"}],
        workspace=str(left_repo))
    service.create_run_tree(
        right["mission_id"],
        [{"kind": "file", "id": str(right_repo), "mode": "write"}],
        workspace=str(right_repo))

    escaped = service.agent_spawn(
        left["mission_id"], "escape", "read another Mission",
        leash={"may": ["research"]},
        resources=[{"kind": "file", "id": str(right_repo), "mode": "read"}])
    assert escaped["ok"] is False and "expands parent ownership" in escaped["error"]

    narrow_dir = left_repo / "narrow"
    narrow_dir.mkdir()
    unenforceable_writer = service.agent_spawn(
        left["mission_id"], "writer", "edit only the narrow directory",
        leash={"may": ["research"]},
        resources=[{"kind": "file", "id": str(narrow_dir), "mode": "write"}])
    assert unenforceable_writer["ok"] is False
    assert "narrower scopes are not yet enforceable" in unenforceable_writer["error"]

    spawned = service.agent_spawn(
        left["mission_id"], "reader", "inspect left only",
        leash={"may": ["research"]},
        resources=[{"kind": "file", "id": str(left_repo / "a.py"), "mode": "read"}])
    assert spawned["ok"] is True and spawned["authority"] == {
        "may": ["research"], "provider": "inherited"}
    run_id = spawned["run_id"]
    for operation in (
            lambda: service.agent_send(right["mission_id"], run_id, "peer steer"),
            lambda: service.agent_poll(right["mission_id"], run_id),
            lambda: service.agent_cancel(right["mission_id"], run_id)):
        refused = operation()
        assert refused["ok"] is False and "outside caller descendant scope" in refused["error"]

    assert service.agent_send(left["mission_id"], run_id, "focus on parser")["ok"] is True
    assert service.agent_poll(left["mission_id"], run_id)["runs"][0]["run_id"] == run_id
    cancelled = service.agent_cancel(left["mission_id"], run_id)
    assert cancelled["ok"] is True and cancelled["status"] == CANCELLED
    assert service.agent_cancel(left["mission_id"], run_id)["already_terminal"] is True


def test_model_agent_spawn_lazily_attaches_and_replays_same_delegation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True, run_tree=tree)
    mission = service.start("coordinate research", may=["research", "agent.*"])
    assert service.inspect_run_tree(mission["mission_id"])["attached"] is False

    first = service.agent_spawn(
        mission["mission_id"], "researcher", "inspect the protocol",
        leash={"may": ["research"]}, resources=[])
    second = service.agent_spawn(
        mission["mission_id"], "researcher", "inspect the protocol",
        leash={"may": ["research"]}, resources=[])

    assert first["ok"] is True and second["ok"] is True
    assert first["run_id"] == second["run_id"]
    attached = service.inspect_run_tree(mission["mission_id"])
    assert attached["attached"] is True
    assert len([row for row in attached["tree"]["flat"]
                if row["parent_run_id"]]) == 1


def test_lazy_root_inherits_bound_workspace_authority_and_unbound_stays_empty(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True, run_tree=tree)
    repo = tmp_path / "repo"
    repo.mkdir()

    reader = service.start("delegate a read", may=["research", "agent.*"])
    service.bind_workspace(reader["mission_id"], str(repo))
    read_child = service.agent_spawn(
        reader["mission_id"], "reader", "inspect one file",
        leash={"may": ["research"]},
        resources=[{"kind": "file", "id": str(repo / "parser.py"), "mode": "read"}],
        operation_id="read-once")
    assert read_child["ok"] is True
    read_root = tree.get(read_child["parent_run_id"])
    assert read_root["resources"] == [
        {"kind": "file", "id": os.path.normcase(os.path.realpath(str(repo))),
         "mode": "read"}]

    writer = service.start("delegate code", may=["code", "agent.*"])
    service.bind_workspace(writer["mission_id"], str(repo))
    write_child = service.agent_spawn(
        writer["mission_id"], "reviewer", "review before editing",
        resources=[{"kind": "file", "id": str(repo / "parser.py"), "mode": "read"}],
        operation_id="review-once")
    assert write_child["ok"] is True
    write_root = tree.get(write_child["parent_run_id"])
    assert write_root["resources"] == [
        {"kind": "file", "id": os.path.normcase(os.path.realpath(str(repo))),
         "mode": "write"}]

    unbound = service.start("no implicit filesystem grant", may=["research", "agent.*"])
    refused = service.agent_spawn(
        unbound["mission_id"], "reader", "try an undeclared path",
        resources=[{"kind": "file", "id": str(repo / "secret.py"), "mode": "read"}],
        operation_id="must-fail")
    assert refused["ok"] is False
    assert "no container-bound workspace" in refused["error"]
    unbound_root = tree.get(service.store.get(unbound["mission_id"]).case["_run_id"])
    assert unbound_root["resources"] == []


def test_model_spawn_nonce_distinguishes_intentional_duplicates_and_checks_collision(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True, run_tree=tree)
    repo = tmp_path / "repo"
    repo.mkdir()
    mission = service.start("parallel duplicate reads", may=["research", "agent.*"])
    service.bind_workspace(mission["mission_id"], str(repo))
    args = {
        "role": "reader",
        "task": "inspect the same protocol",
        "leash": {"may": ["research"]},
        "resources": [{"kind": "file", "id": str(repo / "protocol.py"),
                       "mode": "read"}],
    }

    first = service.agent_spawn(mission["mission_id"], operation_id="nonce-one", **args)
    replay = service.agent_spawn(mission["mission_id"], operation_id="nonce-one", **args)
    second = service.agent_spawn(mission["mission_id"], operation_id="nonce-two", **args)
    collision = service.agent_spawn(
        mission["mission_id"], operation_id="nonce-one", **{**args, "task": "changed task"})

    assert first["ok"] is True and replay["run_id"] == first["run_id"]
    assert second["ok"] is True and second["run_id"] != first["run_id"]
    assert collision["ok"] is False
    assert "different authority" in collision["error"]


def test_late_workspace_binding_initializes_empty_root_once_after_children_stop(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True, run_tree=tree)
    mission = service.start("research before a repo is bound", may=["research", "agent.*"])
    first = service.agent_spawn(
        mission["mission_id"], "researcher", "resource-free research",
        leash={"may": ["research"]}, resources=[], operation_id="before-bind")
    root = tree.get(first["parent_run_id"])
    assert root["workspace"] == "" and root["resources"] == []
    assert root["status"] == WORKSPACE_REQUIRED

    repo = tmp_path / "repo"
    repo.mkdir()
    active_refusal = service.bind_workspace(mission["mission_id"], str(repo))
    assert "specialists are active" in active_refusal["error"]
    assert service.agent_cancel(mission["mission_id"], first["run_id"])["ok"] is True

    bound = service.bind_workspace(mission["mission_id"], str(repo))
    assert "error" not in bound
    initialized = tree.get(root["run_id"])
    assert initialized["status"] == QUEUED
    assert initialized["workspace"] == os.path.realpath(str(repo))
    assert initialized["resources"] == [{
        "kind": "file", "id": os.path.normcase(os.path.realpath(str(repo))),
        "mode": "read"}]
    assert tree.initialize_root_workspace_authority(
        root["run_id"], str(repo), "read")["workspace"] == os.path.realpath(str(repo))
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="already initialized"):
        tree.initialize_root_workspace_authority(root["run_id"], str(other), "read")

    file_child = service.agent_spawn(
        mission["mission_id"], "reader", "inspect parser after binding",
        leash={"may": ["research"]},
        resources=[{"kind": "file", "id": str(repo / "parser.py"), "mode": "read"}],
        operation_id="after-bind")
    assert file_child["ok"] is True


def test_explicit_unbound_root_keeps_declared_resources_when_workspace_is_later_bound(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True, run_tree=tree)
    repo = tmp_path / "repo"
    repo.mkdir()
    mission = service.start("bind an explicitly scoped root", may=["code", "agent.*"])
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace="")["root"]
    assert root["status"] == WORKSPACE_REQUIRED and root["workspace"] == ""

    bound = service.bind_workspace(mission["mission_id"], str(repo))

    assert "error" not in bound
    current = tree.get(root["run_id"])
    assert current["status"] == QUEUED
    assert current["workspace"] == os.path.realpath(str(repo))
    assert current["resources"] == root["resources"]


def test_deterministic_orphan_root_reconciles_after_crash_before_case_attachment(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True, run_tree=tree)
    mission_view = service.start("recover root attachment", may=["research", "agent.*"])
    mid = mission_view["mission_id"]
    mission = service.store.get(mid)
    run_id = service._agent_root_id(mid)
    # Simulate process death after TaskTree.create_root() committed but before
    # Mission case['_run_id'] was written.
    tree.create_root(
        mission.goal, mission.leash, [], run_id=run_id, mission_id=mid,
        workspace="", workspace_mode="worktree")
    assert "_run_id" not in service.store.get(mid).case

    repo = tmp_path / "repo"
    repo.mkdir()
    rebound = service.bind_workspace(mid, str(repo))

    assert "error" not in rebound
    attached = service.store.get(mid)
    assert attached.case["_run_id"] == run_id
    recovered = tree.get(run_id)
    assert recovered["workspace"] == os.path.realpath(str(repo))
    assert recovered["resources"] == [{
        "kind": "file", "id": os.path.normcase(os.path.realpath(str(repo))),
        "mode": "read"}]
    child = service.agent_spawn(
        mid, "reader", "inspect after root recovery",
        leash={"may": ["research"]},
        resources=[{"kind": "file", "id": str(repo / "a.py"), "mode": "read"}],
        operation_id="after-root-recovery")
    assert child["ok"] is True and child["parent_run_id"] == run_id


def test_dispatcher_repairs_both_specialist_spawn_crash_windows(tmp_path, monkeypatch):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"),
        decider=lambda *_: {"action": "wait", "args": {"seconds": 60}},
        stub=True, run_tree=tree, specialist_workers=2)
    source = tmp_path / "source"
    source.mkdir()
    mission = service.start("recover interrupted spawns", may=["code", "research"])
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(source), "mode": "write"}],
        workspace=str(source))["root"]

    # Crash window 1: child INSERT committed, worktree provisioning did not.
    needs_workspace = tree.spawn_specialist(
        root["run_id"], "writer", "recover writer",
        leash={"may": ["code"]},
        resources=[{"kind": "file", "id": str(source), "mode": "write"}],
        workspace="")
    # Crash window 2: workspace binding committed, Mission creation/binding did not.
    already_bound = tree.spawn_specialist(
        root["run_id"], "reader", "recover reader",
        leash={"may": ["research"]}, resources=[], workspace=str(source))
    recovered_workspace = tmp_path / "recovered-worktree"
    provision_calls = []

    def recover_worktree(run_id, parent_cwd):
        provision_calls.append((run_id, os.path.realpath(parent_cwd)))
        recovered_workspace.mkdir(exist_ok=True)
        run = tree.bind_workspace(run_id, str(recovered_workspace), owns_workspace=True)
        return {"ok": True, "kind": "worktree", "dir": str(recovered_workspace),
                "run": run}

    monkeypatch.setattr(tree, "provision_worktree", recover_worktree)

    assert service._tick_specialists(int(time.time())) == 2
    repaired_writer = tree.get(needs_workspace["run_id"])
    repaired_reader = tree.get(already_bound["run_id"])
    assert provision_calls == [(needs_workspace["run_id"], os.path.realpath(str(source)))]
    assert repaired_writer["mission_id"] and repaired_reader["mission_id"]
    assert service.store.get(repaired_writer["mission_id"]).state == "waiting"
    assert service.store.get(repaired_reader["mission_id"]).state == "waiting"


def test_failed_orphan_recovery_surfaces_needs_you_instead_of_hanging(tmp_path, monkeypatch):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True,
        run_tree=tree, specialist_workers=1)
    source = tmp_path / "source"
    source.mkdir()
    mission = service.start("surface worktree recovery failure", may=["code"])
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(source), "mode": "write"}],
        workspace=str(source))["root"]
    orphan = tree.spawn_specialist(
        root["run_id"], "writer", "cannot provision",
        resources=[{"kind": "file", "id": str(source), "mode": "write"}],
        workspace="")
    monkeypatch.setattr(
        tree, "provision_worktree",
        lambda *_args, **_kwargs: {"ok": False, "error": "simulated disk failure"})

    assert service._tick_specialists(int(time.time())) == 0
    failed = tree.get(orphan["run_id"])
    assert failed["status"] == NEEDS_YOU
    assert "simulated disk failure" in failed["result"]
    assert any(item["run_id"] == orphan["run_id"] and item["kind"] == "needs_you"
               for item in tree.notifications())


def test_workspace_provisioning_has_a_durable_single_owner_claim(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(tree, tmp_path)
    child = tree.spawn_specialist(
        root["run_id"], "writer", "one provisioner only",
        resources=[{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace="")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    entered = threading.Event()
    release = threading.Event()
    first_result = {}
    prepares = []

    def slow_prepare(_cwd, _run_id, _label):
        prepares.append("winner")
        entered.set()
        assert release.wait(5)
        return {"ok": True, "kind": "worktree", "dir": str(worktree),
                "branch": "winner", "root": str(repo), "error": ""}

    def provision_first():
        first_result.update(tree.provision_worktree(
            child["run_id"], str(repo), prepare_fn=slow_prepare))

    worker = threading.Thread(target=provision_first)
    worker.start()
    assert entered.wait(5)
    loser = tree.provision_worktree(
        child["run_id"], str(repo),
        prepare_fn=lambda *_: pytest.fail("losing provisioner must not touch git"))
    assert loser["ok"] is False and loser["busy"] is True
    assert tree.mark_orphan_needs_you(
        child["run_id"], "stale loser", phase="workspace") is False
    release.set()
    worker.join(timeout=5)

    assert first_result["ok"] is True and prepares == ["winner"]
    assert tree.get(child["run_id"])["status"] == QUEUED
    assert tree.get(child["run_id"])["workspace"] == os.path.realpath(str(worktree))
    replay = tree.provision_worktree(
        child["run_id"], str(repo),
        prepare_fn=lambda *_: pytest.fail("bound workspace replay must not touch git"))
    assert replay["ok"] is True and replay["replayed"] is True
    assert tree.mark_orphan_needs_you(
        child["run_id"], "late stale loser", phase="workspace") is False


def test_worktree_recovery_label_hashes_full_run_identity(tmp_path, monkeypatch):
    from harness import worktree

    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(tree, tmp_path)
    run_ids = ("run_first_identity_deadbe", "run_other_identity_deadbe")
    children = [tree.spawn_specialist(
        root["run_id"], "reader", "isolated recovery %d" % index,
        run_id=run_id,
        resources=[{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace="") for index, run_id in enumerate(run_ids)]
    by_label = {}
    seen = []

    def fake_find(_cwd, session, label):
        seen.append(("find", session, label))
        return by_label.get(label)

    def fake_prepare(_cwd, session, label):
        seen.append(("prepare", session, label))
        directory = tmp_path / ("wt-" + session)
        directory.mkdir()
        result = {"ok": True, "kind": "worktree", "dir": str(directory),
                  "branch": "collie/" + label, "root": str(repo), "error": ""}
        by_label[label] = result
        return result

    monkeypatch.setattr(worktree, "find_prepared", fake_find)
    monkeypatch.setattr(worktree, "prepare", fake_prepare)
    results = [tree.provision_worktree(child["run_id"], str(repo)) for child in children]
    labels = [item[2] for item in seen if item[0] == "find"]

    assert len(labels) == 2 and labels[0] != labels[1]
    assert all(hashlib.sha256(run_id.encode()).hexdigest()[:16] in label
               for run_id, label in zip(run_ids, labels))
    assert all(result["ok"] for result in results)
    assert results[0]["dir"] != results[1]["dir"]
    assert all(item[0] == "find" for item in seen[::2])
    assert all(item[0] == "prepare" for item in seen[1::2])


def test_parent_code_waits_before_runner_when_descendant_owns_workspace(
        tmp_path, monkeypatch):
    from harness import primitives

    executed = []

    def code_runner(record):
        executed.append(record.args.get("workspace"))
        return {"case": {"coded": True, "code_verified": True},
                "result": "unexpected", "verified": True}

    monkeypatch.setattr(primitives, "_stub_code", code_runner)
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"),
        decider=lambda *_: {"action": "code", "args": {"goal": "edit parser"}},
        stub=True, run_tree=tree)
    mission = service.start("edit parser", may=["code", "agent.*"])
    repo = tmp_path / "repo"
    repo.mkdir()
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))["root"]
    child = tree.spawn_specialist(
        root["run_id"], "reader", "hold a stable parser view",
        resources=[{"kind": "file", "id": str(repo / "parser.py"), "mode": "read"}],
        workspace=str(repo))

    blocked = service.run(mission["mission_id"])

    assert blocked["state"] == "waiting"
    assert "shared external resource busy" in blocked["result"]
    assert executed == []
    assert child["run_id"] in tree.can_access(root["run_id"], str(repo), "write")[1]


def test_specialist_code_checks_source_authority_but_executes_in_isolated_worktree(
        tmp_path, monkeypatch):
    from harness import primitives

    executed = []

    def code_runner(record):
        executed.append(os.path.realpath(record.args.get("workspace")))
        return {"case": {"coded": True, "code_verified": True},
                "result": "fixed", "verified": True}

    monkeypatch.setattr(primitives, "_stub_code", code_runner)

    def decider(_goal, case, _primitives):
        if not case.get("coded"):
            return {"action": "code", "args": {"goal": "fix parser"}}
        return {"action": "done", "reason": "isolated edit verified"}

    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=decider, stub=True, run_tree=tree,
        goal_verifier=lambda *_: Verdict(
            VERIFIED, "isolated code evidence", (
                Observation("specialist-code", time.time(), True, asserted=True),)))
    source = tmp_path / "source"
    isolated = tmp_path / "isolated"
    source.mkdir(); isolated.mkdir()
    mission = service.start("coordinate writer", may=["code"])
    service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(source), "mode": "write"}],
        workspace=str(source))
    child = service.spawn_specialist(
        mission["mission_id"], "writer", "fix parser",
        leash={"may": ["code"]},
        resources=[{"kind": "file", "id": str(source), "mode": "write"}],
        workspace=str(isolated))

    assert service._tick_specialists(int(time.time())) == 1
    assert tree.get(child["run_id"])["status"] == COMPLETED
    assert executed == [os.path.realpath(str(isolated))]
    assert tree.can_access(child["run_id"], str(source), "write") == (True, "owned")


def test_nested_writer_keeps_logical_source_authority_across_physical_worktrees(
        tmp_path, monkeypatch):
    from harness import primitives

    executed = []

    def code_runner(record):
        executed.append(os.path.realpath(record.args.get("workspace")))
        return {"case": {"coded": True, "code_verified": True},
                "result": "nested fix", "verified": True}

    monkeypatch.setattr(primitives, "_stub_code", code_runner)

    def decider(_goal, case, _primitives):
        if not case.get("coded"):
            return {"action": "code", "args": {"goal": "nested parser fix"}}
        return {"action": "done", "reason": "nested fix verified"}

    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=decider, stub=True, run_tree=tree,
        goal_verifier=lambda *_: Verdict(
            VERIFIED, "nested evidence", (
                Observation("nested-code", time.time(), True, asserted=True),)))
    source = tmp_path / "source"
    first_worktree = tmp_path / "writer-one"
    second_worktree = tmp_path / "writer-two"
    source.mkdir(); first_worktree.mkdir()
    mission = service.start("coordinate nested writers", may=["code", "agent.*"])
    service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(source), "mode": "write"}],
        workspace=str(source))
    child = service.spawn_specialist(
        mission["mission_id"], "writer-one", "delegate nested fix",
        leash={"may": ["code", "agent.*"]},
        resources=[{"kind": "file", "id": str(source), "mode": "write"}],
        workspace=str(first_worktree))
    provision_from = []

    def provision_nested(run_id, parent_cwd):
        provision_from.append(os.path.realpath(parent_cwd))
        second_worktree.mkdir(exist_ok=True)
        run = tree.bind_workspace(run_id, str(second_worktree), owns_workspace=True)
        return {"ok": True, "kind": "worktree", "dir": str(second_worktree),
                "run": run}

    monkeypatch.setattr(tree, "provision_worktree", provision_nested)
    grandchild = service.agent_spawn(
        child["mission_id"], "writer-two", "apply nested parser fix",
        leash={"may": ["code"]},
        resources=[{"kind": "file", "id": str(source), "mode": "write"}],
        operation_id="nested-write")

    assert grandchild["ok"] is True
    assert provision_from == [os.path.realpath(str(first_worktree))]
    nested_run = tree.get(grandchild["run_id"])
    nested_mission = service.store.get(grandchild["mission_id"])
    assert nested_mission.case["_resource_source_workspace"] == os.path.realpath(str(source))
    assert nested_run["workspace"] == os.path.realpath(str(second_worktree))
    assert tree.can_access(nested_run["run_id"], str(source), "write") == (True, "owned")

    token = tree.claim(nested_run["run_id"])
    service._run_specialist(tree.get(nested_run["run_id"]), token)
    assert tree.get(nested_run["run_id"])["status"] == COMPLETED
    assert executed == [os.path.realpath(str(second_worktree))]


def test_cancel_race_after_control_check_refuses_specialist_code_before_runner(
        tmp_path, monkeypatch):
    from harness import primitives

    executed = []
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    child_id = {"value": ""}

    def code_runner(record):
        executed.append(record.args.get("workspace"))
        return {"case": {"coded": True}, "result": "must not run", "verified": True}

    def decider(_goal, _case, _primitives):
        tree.request_cancel(child_id["value"])
        return {"action": "code", "args": {"goal": "must be fenced"}}

    monkeypatch.setattr(primitives, "_stub_code", code_runner)
    service = MissionService(
        base=str(tmp_path / "svc"), decider=decider, stub=True, run_tree=tree)
    source = tmp_path / "source"
    isolated = tmp_path / "isolated"
    source.mkdir(); isolated.mkdir()
    mission = service.start("cancel racing writer", may=["code"])
    service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(source), "mode": "write"}],
        workspace=str(source))
    child = service.spawn_specialist(
        mission["mission_id"], "writer", "do not start after cancel",
        leash={"may": ["code"]},
        resources=[{"kind": "file", "id": str(source), "mode": "write"}],
        workspace=str(isolated))
    child_id["value"] = child["run_id"]

    assert service._tick_specialists(int(time.time())) == 1
    assert executed == []
    assert tree.get(child["run_id"])["status"] == CANCELLED
    assert service.store.get(child["mission_id"]).state == "cancelled"


def test_failed_orchestrator_atomically_fences_queued_and_running_descendants(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(tree, tmp_path, mission_id="msn_root")
    orchestrator = tree.spawn_specialist(
        root["run_id"], "orchestrator", "fail with descendants",
        resources=[{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))
    owner = tree.claim(orchestrator["run_id"])
    queued = tree.spawn_specialist(
        orchestrator["run_id"], "queued", "must never run",
        resources=[{"kind": "file", "id": str(repo / "queued.py"), "mode": "read"}],
        workspace=str(repo))
    running = tree.spawn_specialist(
        orchestrator["run_id"], "running", "must stop at boundary",
        resources=[{"kind": "file", "id": str(repo / "running.py"), "mode": "read"}],
        workspace=str(repo))
    running_owner = tree.claim(running["run_id"])

    assert tree.fail(orchestrator["run_id"], owner, "orchestrator failed")
    assert tree.get(orchestrator["run_id"])["status"] == FAILED
    assert tree.get(queued["run_id"])["status"] == CANCELLED
    assert tree.get(running["run_id"])["status"] == CANCEL_REQUESTED
    assert tree.claim(queued["run_id"]) is None
    cancel_messages = tree.claim_messages(running["run_id"], running_owner)
    assert [item["kind"] for item in cancel_messages] == ["cancel"]
    outcome = tree.claim_child_results(root["run_id"], "msn_root")
    assert outcome[0]["payload"]["run_id"] == orchestrator["run_id"]
    assert outcome[0]["payload"]["state"] == FAILED


def test_root_mission_failure_cancels_tree_and_linked_child_missions(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"),
        decider=lambda *_: {"action": "nonexistent.capability"},
        stub=True, run_tree=tree)
    repo = tmp_path / "repo"
    repo.mkdir()
    mission = service.start("fail the orchestrator", may=["research"])
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))["root"]
    child = service.spawn_specialist(
        mission["mission_id"], "child", "queued child",
        leash={"may": ["research"]},
        resources=[{"kind": "file", "id": str(repo / "a.py"), "mode": "read"}],
        workspace=str(repo))
    grandchild = service.spawn_specialist(
        child["mission_id"], "grandchild", "queued grandchild",
        leash={"may": ["research"]},
        resources=[{"kind": "file", "id": str(repo / "a.py"), "mode": "read"}],
        workspace=str(repo))

    failed = service.run(mission["mission_id"])

    assert failed["state"] == "failed"
    assert tree.get(root["run_id"])["status"] == FAILED
    assert tree.get(child["run_id"])["status"] == CANCELLED
    assert tree.get(grandchild["run_id"])["status"] == CANCELLED
    assert service.store.get(child["mission_id"]).state == "cancelled"
    assert service.store.get(grandchild["mission_id"]).state == "cancelled"
    assert tree.claim(child["run_id"]) is None
    assert tree.claim(grandchild["run_id"]) is None


def test_retry_repairs_failed_root_fence_and_waits_for_running_cancel_ack(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(base=str(tmp_path / "svc"), stub=True, run_tree=tree)
    repo = tmp_path / "repo"
    repo.mkdir()
    mission = service.start("failed before tree projection", may=["research"])
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))["root"]
    queued = tree.spawn_specialist(
        root["run_id"], "queued", "must be fenced",
        resources=[{"kind": "file", "id": str(repo / "queued.py"), "mode": "read"}],
        workspace=str(repo))
    running = tree.spawn_specialist(
        root["run_id"], "running", "must acknowledge cancel",
        resources=[{"kind": "file", "id": str(repo / "running.py"), "mode": "read"}],
        workspace=str(repo))
    running_token = tree.claim(running["run_id"])
    mission_token = service.store.claim_run(mission["mission_id"])
    assert service.store.finish_run(
        mission["mission_id"], mission_token, FAILED, "simulated crash failure")
    before_ids = {item.mission_id for item in service.store.list()}

    refused = service.retry(mission["mission_id"])

    assert "until predecessor specialists settle" in refused["error"]
    assert {item.mission_id for item in service.store.list()} == before_ids
    assert tree.get(root["run_id"])["status"] == FAILED
    assert tree.get(queued["run_id"])["status"] == CANCELLED
    assert tree.get(running["run_id"])["status"] == CANCEL_REQUESTED
    assert [message["kind"] for message in
            tree.claim_messages(running["run_id"], running_token)] == ["cancel"]
    assert tree.ack_cancel(running["run_id"], running_token, "cancel acknowledged")

    successor = service.retry(mission["mission_id"])
    assert successor["state"] == "queued"
    assert successor["mission_id"] not in before_ids
    assert successor["case"]["_retry_of"] == mission["mission_id"]


def test_failed_tree_repair_skips_synced_history_beyond_scan_limit(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(base=str(tmp_path / "svc"), stub=True, run_tree=tree)
    repo = tmp_path / "repo"
    repo.mkdir()
    orphan = None
    for index in range(65):
        mission = service.start("failure history %d" % index, may=["research"])
        root = service.create_run_tree(
            mission["mission_id"],
            [{"kind": "file", "id": str(repo), "mode": "read"}],
            workspace=str(repo))["root"]
        token = service.store.claim_run(mission["mission_id"])
        assert service.store.finish_run(
            mission["mission_id"], token, FAILED, "historical failure")
        if index < 64:
            assert tree.fail_mission_root(
                root["run_id"], mission["mission_id"], "already projected")
        else:
            orphan = (mission["mission_id"], root["run_id"])

    assert tree.get(orphan[1])["status"] == QUEUED
    assert service._fence_failed_mission_trees(limit=64) == 1
    assert tree.get(orphan[1])["status"] == FAILED


def test_root_mission_success_projection_requires_terminal_descendants(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    root, repo = _root(tree, tmp_path, mission_id="msn_root")
    child = tree.spawn_specialist(
        root["run_id"], "reader", "finish before root",
        resources=[{"kind": "file", "id": str(repo / "parser.py"), "mode": "read"}],
        workspace=str(repo))

    assert not tree.complete_mission_root(root["run_id"], "msn_root", "too early")
    assert tree.get(root["run_id"])["status"] == QUEUED
    token = tree.claim(child["run_id"])
    assert tree.complete(child["run_id"], token, "child done")

    assert tree.complete_mission_root(root["run_id"], "msn_root", "all done")
    assert tree.get(root["run_id"])["status"] == COMPLETED
    assert tree.get(root["run_id"])["result"] == "all done"
    assert tree.complete_mission_root(root["run_id"], "msn_root", "replay")
    assert [event["kind"] for event in tree.events(root["run_id"])].count(COMPLETED) == 1


def test_tick_repairs_root_success_projection_crash_window(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {"action": "done"},
        stub=True, run_tree=tree,
        goal_verifier=lambda *_: Verdict(
            VERIFIED, "root verified", (
                Observation("root-contract", time.time(), True, asserted=True),)))
    repo = tmp_path / "repo"
    repo.mkdir()
    mission = service.start("finish root", may=["research"])
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace=str(repo))["root"]

    # Simulate a process dying after MissionStore committed success but before
    # MissionService projected that authoritative result into TaskTree.
    assert service._driver().advance(mission["mission_id"]) == "done_verified"
    assert tree.get(root["run_id"])["status"] == QUEUED

    service.tick()
    assert tree.get(root["run_id"])["status"] == COMPLETED
    assert tree.get(root["run_id"])["result"] == service.store.get(
        mission["mission_id"]).result


def test_parent_done_waits_when_child_finishes_after_control_poll(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    child_owner = {"run_id": "", "token": "", "finished": False}

    def decider(_goal, _case, _primitives):
        return {"action": "done", "reason": "all delegated work observed"}

    service = MissionService(
        base=str(tmp_path / "svc"), decider=decider, stub=True, run_tree=tree,
        goal_verifier=lambda *_: Verdict(
            VERIFIED, "root verified", (
                Observation("root-race", time.time(), True, asserted=True),)))
    repo = tmp_path / "repo"
    repo.mkdir()
    mission = service.start("finish without dropping child output", may=["research"])
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace=str(repo))["root"]
    child = tree.spawn_specialist(
        root["run_id"], "reader", "finish during parent decision",
        resources=[{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace=str(repo))
    child_owner.update(run_id=child["run_id"], token=tree.claim(child["run_id"]))
    normal_guard = service._agent_completion_guard

    def racing_guard(mid, current):
        # This is the narrow window after MissionDriver's post-model control
        # boundary but before its DONE completion guard.
        if not child_owner["finished"]:
            child_owner["finished"] = True
            assert tree.complete(
                child_owner["run_id"], child_owner["token"], "late child result")
        return normal_guard(mid, current)

    service._agent_completion_guard = racing_guard

    waiting = service.run(mission["mission_id"])
    assert waiting["state"] == "waiting"
    assert "await durable folding" in waiting["result"]
    assert tree.has_child_results(root["run_id"], mission["mission_id"])

    service.tick()
    parent = service.store.get(mission["mission_id"])
    assert parent.state == "done_verified"
    assert parent.case["specialist_results"][0]["result"] == "late child result"
    assert not tree.has_child_results(root["run_id"], mission["mission_id"])
    assert tree.get(root["run_id"])["status"] == COMPLETED


def test_accept_refuses_root_with_unfolded_child_result(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(base=str(tmp_path / "svc"), stub=True, run_tree=tree)
    repo = tmp_path / "repo"
    repo.mkdir()
    mission = service.start("human handoff with child output", may=["research"])
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace=str(repo))["root"]
    child = tree.spawn_specialist(
        root["run_id"], "reader", "return before handoff",
        resources=[{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace=str(repo))
    child_token = tree.claim(child["run_id"])
    assert tree.complete(child["run_id"], child_token, "unfolded evidence")
    mission_token = service.store.claim_run(mission["mission_id"])
    assert service.store.finish_run(
        mission["mission_id"], mission_token, NEEDS_YOU, "handoff requested")

    refused = service.accept(mission["mission_id"])

    assert refused["state"] == "needs_you"
    assert "await durable folding" in refused["error"]
    assert service.store.get(mission["mission_id"]).state == "needs_you"
    assert tree.get(root["run_id"])["status"] == QUEUED
    assert tree.has_child_results(root["run_id"], mission["mission_id"])


def test_accept_refuses_nested_specialist_with_active_grandchild(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(base=str(tmp_path / "svc"), stub=True, run_tree=tree)
    repo = tmp_path / "repo"
    repo.mkdir()
    parent = service.start("nested human handoff", may=["research", "agent.*"])
    service.create_run_tree(
        parent["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))
    child = service.spawn_specialist(
        parent["mission_id"], "orchestrator", "delegate then hand off",
        resources=[{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace=str(repo))
    grandchild = service.spawn_specialist(
        child["mission_id"], "reader", "still working",
        resources=[{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace=str(repo))
    child_tree_token = tree.claim(child["run_id"])
    assert tree.block(
        child["run_id"], child_tree_token, "human handoff", needs_you=True)
    child_mission_token = service.store.claim_run(child["mission_id"])
    assert service.store.finish_run(
        child["mission_id"], child_mission_token, NEEDS_YOU, "human handoff")

    refused = service.accept(child["mission_id"])

    assert refused["state"] == "needs_you"
    assert "still active" in refused["error"]
    assert service.store.get(child["mission_id"]).state == "needs_you"
    assert tree.get(child["run_id"])["status"] == NEEDS_YOU
    assert tree.get(grandchild["run_id"])["status"] == QUEUED


def test_non_code_resources_remain_scheduling_authority_not_a_generic_tool_sandbox(
        tmp_path, monkeypatch):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    access_checks = []
    original_can_access = tree.can_access

    def watched_can_access(*args, **kwargs):
        access_checks.append((args, kwargs))
        return original_can_access(*args, **kwargs)

    monkeypatch.setattr(tree, "can_access", watched_can_access)

    def decider(_goal, case, _primitives):
        if not case.get("researched"):
            return {"action": "research", "args": {"query": "parser contract"}}
        return {"action": "wait", "args": {"seconds": 60},
                "reason": "await specialist"}

    service = MissionService(
        base=str(tmp_path / "svc"), decider=decider, stub=True, run_tree=tree)
    mission = service.start("research while delegated", may=["research"])
    repo = tmp_path / "repo"
    repo.mkdir()
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))["root"]
    tree.spawn_specialist(
        root["run_id"], "reader", "inspect parser",
        resources=[{"kind": "file", "id": str(repo / "parser.py"), "mode": "read"}],
        workspace=str(repo))

    status = service.run(mission["mission_id"])

    assert status["state"] == "waiting"
    assert service.store.get(mission["mission_id"]).case["researched"] is True
    assert access_checks == [], (
        "TaskTree resources do not claim to sandbox capabilities other than code")


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


def test_specialist_dispatcher_finally_projects_usage_after_driver_exception(
        tmp_path, monkeypatch):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True, run_tree=tree)
    repo = tmp_path / "repo"
    repo.mkdir()
    mission = service.start("orchestrate", may=["research"])
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))["root"]
    child = service.spawn_specialist(
        mission["mission_id"], "researcher", "pause after one slice",
        resources=[{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace=str(repo))
    token = tree.claim(child["run_id"])

    class FailingDriver:
        def advance(self, mid):
            assert service.store.reserve_decision(
                mid, service.store.get(mid).leash)
            assert service.store.account_runtime(
                mid, input_tokens=11, cache_tokens=7, cost_usd=0.25,
                wall_ms=19, retries=1)
            raise RuntimeError("crash after Mission accounting")

    monkeypatch.setattr(service, "_driver", lambda **_kwargs: FailingDriver())

    service._run_specialist(tree.get(child["run_id"]), token)

    projected = tree.get(child["run_id"])
    aggregate = tree.get(root["run_id"])
    assert projected["status"] == RECOVERY_REQUIRED
    assert (projected["input_tokens"], projected["cache_tokens"],
            projected["model_calls"], projected["turns"]) == (11, 7, 1, 1)
    assert aggregate["input_tokens"] == 11
    assert aggregate["cache_tokens"] == 7
    assert aggregate["model_calls"] == 1


def test_specialist_reconciles_root_usage_before_ancestor_budget_check(
        tmp_path, monkeypatch):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=lambda *_: {}, stub=True, run_tree=tree)
    repo = tmp_path / "repo"
    repo.mkdir()
    mission = service.start(
        "orchestrate", may=["research"], max_model_tokens=5)
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))["root"]
    child = service.spawn_specialist(
        mission["mission_id"], "researcher", "must not start over budget",
        resources=[{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace=str(repo))
    assert service.store.account_runtime(mission["mission_id"], input_tokens=5)
    token = tree.claim(child["run_id"])
    called = []
    monkeypatch.setattr(
        service, "_driver", lambda **_kwargs: called.append(True))

    service._run_specialist(tree.get(child["run_id"]), token)

    assert called == []
    assert tree.get(root["run_id"])["input_tokens"] == 5
    assert tree.get(child["run_id"])["input_tokens"] == 0
    assert tree.get(child["run_id"])["status"] == NEEDS_YOU


def test_status_catches_up_crash_gap_once_for_root_and_descendant_usage(tmp_path):
    tree_path = str(tmp_path / "tree.db")
    base = str(tmp_path / "svc")
    tree = TaskTreeStore(tree_path)
    service = MissionService(base=base, decider=lambda *_: {}, stub=True, run_tree=tree)
    repo = tmp_path / "repo"
    repo.mkdir()
    mission = service.start("orchestrate", may=["research"])
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))["root"]
    child = service.spawn_specialist(
        mission["mission_id"], "researcher", "inspect one branch",
        resources=[{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace=str(repo))

    # These authoritative commits represent a process dying before either
    # TaskTree projection.  Each receipt is own-Mission usage, not an aggregate.
    assert service.store.reserve_decision(
        mission["mission_id"], service.store.get(mission["mission_id"]).leash)
    assert service.store.reserve_decision(
        child["mission_id"], service.store.get(child["mission_id"]).leash)
    assert service.store.account_runtime(
        mission["mission_id"], input_tokens=4, cache_tokens=1,
        cost_usd=0.1, wall_ms=10)
    assert service.store.account_runtime(
        child["mission_id"], input_tokens=2, output_tokens=3, cache_tokens=5,
        cost_usd=0.2, wall_ms=20, retries=1)
    assert tree.get(root["run_id"])["input_tokens"] == 0
    service.close()
    tree.close()

    reopened_tree = TaskTreeStore(tree_path)
    reopened = MissionService(
        base=base, decider=lambda *_: {}, stub=True, run_tree=reopened_tree)
    status = reopened.status(mission["mission_id"])
    aggregate = reopened_tree.get(root["run_id"])
    own_child = reopened_tree.get(child["run_id"])
    assert status["usage_projection_errors"] == []
    assert status["aggregate_runtime"]["model_calls"] == 2
    assert (aggregate["input_tokens"], aggregate["output_tokens"],
            aggregate["cache_tokens"]) == (6, 3, 6)
    assert (aggregate["model_calls"], aggregate["turns"],
            aggregate["active_wall_ms"], aggregate["retry_count"]) == (2, 2, 30, 1)
    assert own_child["input_tokens"] == 2
    assert own_child["cache_tokens"] == 5

    # Status/terminal reconciliation is freely retryable.
    reopened.status(mission["mission_id"])
    assert reopened_tree.get(root["run_id"])["input_tokens"] == 6
    assert reopened_tree.get(root["run_id"])["model_calls"] == 2


def test_model_agent_spawn_child_completion_folds_result_and_wakes_parent(tmp_path):
    tree = TaskTreeStore(str(tmp_path / "tree.db"))
    catalogs = []

    def decider(goal, case, primitives):
        catalogs.append({item["name"] for item in primitives})
        if goal == "coordinate one specialist":
            if not case.get("agent.spawn"):
                return {
                    "action": "agent.spawn",
                    "args": {
                        "role": "researcher",
                        "task": "inspect the parser contract",
                        "resources": [{"kind": "file", "id": str(repo / "parser.py"),
                                       "mode": "read"}],
                        "leash": {"may": ["research"]},
                    },
                }
            if not case.get("specialist_results"):
                # A mistaken/self-optimistic orchestrator cannot finish while its
                # delegated authority is live; the container turns this into an
                # event-driven wait and wakes it when child_result arrives.
                return {"action": "done", "reason": "premature parent completion"}
            return {"action": "done", "reason": "specialist evidence returned"}
        if not case.get("researched"):
            return {"action": "research", "args": {"query": "parser contract"}}
        return {"action": "done", "reason": "child research captured"}

    verifier = lambda *_: Verdict(
        VERIFIED, "independent contract evidence", (
            Observation("agent-graph-contract", time.time(), True,
                        asserted=True, detail="scoped result observed"),))
    service = MissionService(
        base=str(tmp_path / "svc"), decider=decider, stub=True, run_tree=tree,
        goal_verifier=verifier, specialist_workers=1)
    mission = service.start(
        "coordinate one specialist", may=["research", "agent.*"])
    repo = tmp_path / "repo"
    repo.mkdir()
    root = service.create_run_tree(
        mission["mission_id"],
        [{"kind": "file", "id": str(repo), "mode": "write"}],
        workspace=str(repo))["root"]

    waiting = service.run(mission["mission_id"])
    assert waiting["state"] == "waiting"
    child = [row for row in tree.tree(root["run_id"])["flat"]
             if row["parent_run_id"] == root["run_id"]][0]
    assert child["leash"]["may"] == ["research"]
    assert {"agent.spawn", "agent.send", "agent.poll", "agent.cancel"} <= catalogs[0]

    tick = service.tick()
    parent = service.store.get(mission["mission_id"])
    finished = tree.get(child["run_id"])
    assert tick["specialists_advanced"] == 1
    assert tick["parents_resumed"]["normal"] == 1
    assert finished["status"] == COMPLETED
    assert parent.state == "done_verified"
    assert tree.get(root["run_id"])["status"] == COMPLETED
    result = parent.case["specialist_results"][0]
    assert result["run_id"] == child["run_id"]
    assert result["observation"] == {
        "accepted": False, "mission_state": "done_verified", "verified": True}
    assert result["artifacts"][0]["uri"] == "collie://runs/%s" % child["run_id"]
    assert "path" not in result["artifacts"][0], (
        "a shared read checkout is not promoted into parent resource authority")
    assert tree.ack_child_result(
        root["run_id"], mission["mission_id"], result["message_id"]), (
        "ack replay remains idempotent after the parent committed")

    refused = service.spawn_specialist(
        mission["mission_id"], "late", "must not run", resources=[], workspace=str(repo))
    assert "terminal Mission" in refused["error"]
    assert service.agent_spawn(
        mission["mission_id"], "late", "must not run", resources=[])["ok"] is False


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
