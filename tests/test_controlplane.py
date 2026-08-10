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
