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


def _quiet_credentials(monkeypatch):
    # Health reads the configured provider and the logins on disk; neither belongs in this test.
    monkeypatch.setattr("harness.ops._needed_credentials", lambda provider=None: set())


def test_without_a_supervisor_background_workers_are_not_reported_missing(tmp_path, monkeypatch):
    """The macOS app and a pip `collie web` run no supervisor: that is not a degraded runtime."""
    from harness.controlplane import health
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    _quiet_credentials(monkeypatch)
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    report = health(str(tmp_path), probe_services=False)
    assert report["supervised"] is False
    assert report["workers"] == {}
    assert report["ok"] is True and report["status"] == "ok"


def test_an_installed_supervisor_still_reports_its_missing_workers(tmp_path, monkeypatch):
    from harness.controlplane import health
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    _quiet_credentials(monkeypatch)
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": True, "mode": "scheduled_task"})
    report = health(str(tmp_path), probe_services=False)
    assert report["supervised"] is True
    assert report["workers"]["web"]["state"] == "missing"
    assert report["status"] == "degraded"


def test_a_supervisor_that_ran_here_counts_even_when_not_installed_as_a_task(tmp_path, monkeypatch):
    from harness.controlplane import health
    from harness.ops import OpsStore
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    _quiet_credentials(monkeypatch)
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.beat("supervisor", "running", {}, ttl=30)
    report = health(str(tmp_path), probe_services=False)
    assert report["supervised"] is True
    assert report["workers"]["jobd"]["state"] == "missing"
    assert report["status"] == "degraded"


def test_recovery_work_is_the_first_reason_given(tmp_path, monkeypatch):
    from harness import sessions
    from harness.controlplane import health
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    _quiet_credentials(monkeypatch)
    sessions.checkpoint("uncertain", [{"role": "user", "content": "send it"}], run_id="r1",
                        state="external_action", detail={"tool_name": "publish", "tool_call_id": "c1"})
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    report = health(str(tmp_path), probe_services=False)
    assert report["reasons"][0] == {"code": "recovery_required", "subject": "work", "count": 1}


def test_a_stopped_supervisor_is_one_reason_not_one_per_worker(tmp_path, monkeypatch):
    from harness.controlplane import health
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    _quiet_credentials(monkeypatch)
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": True, "mode": "scheduled_task"})
    report = health(str(tmp_path), probe_services=False)
    codes = [row["code"] for row in report["reasons"]]
    assert codes == ["supervisor_not_running"]
    assert report["status"] == "degraded"


def test_a_running_supervisor_names_the_worker_that_is_silent(tmp_path, monkeypatch):
    import time
    from harness.controlplane import health
    from harness.ops import OpsStore
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    _quiet_credentials(monkeypatch)
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": True, "mode": "scheduled_task"})
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.beat("supervisor", "running", {}, ttl=60, now=time.time())
        for name in ("web", "automations", "ambient", "bridge"):
            store.beat("worker:" + name, "running", {}, ttl=60, now=time.time())
    report = health(str(tmp_path), probe_services=False)
    assert [(r["code"], r["subject"]) for r in report["reasons"]] == [("worker_not_reporting", "jobd")]
