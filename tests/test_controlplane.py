import json


def test_activity_is_lane_isolated_and_health_surfaces_recovery(tmp_path, monkeypatch):
    from harness import sessions
    from harness.controlplane import activity, health

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    sessions.checkpoint(
        "uncertain", [{"role": "user", "content": "send it"}], run_id="r1",
        state="external_action", detail={"tool_name": "publish", "tool_call_id": "c1"})

    got = activity(str(tmp_path))
    assert got["sessions"][0]["recovery_required"] is True
    # Optional DBs do not have to exist for Activity to be useful.
    assert got["missions"] == got["task_runs"] == got["automations"] == []

    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    report = health(str(tmp_path), probe_services=False)
    assert report["status"] == "needs_you"
    assert report["work"]["recovery_required"]
    assert "send it" not in json.dumps(report), "health must not expose conversation text"


def test_health_never_omits_mission_or_specialist_needs_you(tmp_path, monkeypatch):
    from harness.controlplane import health
    from harness.mission import MissionStore
    from harness.tasktree import TaskTreeStore

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    missions = MissionStore(str(tmp_path / "jobs.db"))
    missions.create("mission-recovery", "private mission prompt")
    missions.set_state("mission-recovery", "recovery_required", "private failure detail")
    missions.close()

    tree = TaskTreeStore(str(tmp_path / "tasktree.db"))
    repo = tmp_path / "repo"
    repo.mkdir()
    run = tree.create_root(
        "private specialist prompt", {},
        [{"kind": "file", "id": str(repo), "mode": "read"}],
        workspace=str(repo), workspace_mode="current")
    token = tree.claim(run["run_id"])
    tree.block(run["run_id"], token, "private question", needs_you=True)
    tree.close()

    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    report = health(str(tmp_path), probe_services=False)
    recovery = report["work"]["recovery_required"]
    assert {row["kind"] for row in recovery} >= {"mission", "specialist"}
    assert report["status"] == "needs_you" and report["ok"] is False
    serialized = json.dumps(report)
    assert "private mission prompt" not in serialized
    assert "private specialist prompt" not in serialized
    assert "private failure detail" not in serialized
    assert "private question" not in serialized
