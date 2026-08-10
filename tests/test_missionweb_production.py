import os
import sqlite3

import pytest

from harness.missionweb import MissionService
from harness.tasktree import CANCELLED, TaskTreeStore


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
    service.close()

    reopened = TaskTreeStore(str(state / "tasktree.db"))
    try:
        assert reopened.get(child["run_id"])["status"] == CANCELLED
    finally:
        reopened.close()


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
