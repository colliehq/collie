import json


def _timer_spec(enabled=True):
    return {
        "automation_id": "daily-review",
        "task": "Review the repository",
        "trigger": {"provider": "timer", "every_s": 60, "fire_immediately": True},
        "workspace": {"mode": "isolated"},
        "permissions": {},
        "enabled": enabled,
    }


def test_automation_studio_upsert_preview_toggle_and_manual_run(tmp_path):
    from harness.controlcenter import (automation_preview, automation_run_now,
                                       automation_set_enabled, automation_snapshot,
                                       automation_upsert)

    saved = automation_upsert(_timer_spec(), str(tmp_path))
    assert saved["spec"]["automation_id"] == "daily-review"
    preview = automation_preview(None, str(tmp_path), automation_id="daily-review", now=100)
    assert preview["fired"] is True and preview["persisted"] is False
    assert automation_set_enabled("daily-review", False, str(tmp_path))["spec"]["enabled"] is False
    try:
        automation_run_now("daily-review", str(tmp_path), confirmed=False, now=101)
    except ValueError as exc:
        assert "confirmed=true" in str(exc)
    else:
        raise AssertionError("manual automation run needs explicit confirmation")
    queued = automation_run_now("daily-review", str(tmp_path), confirmed=True, now=101)
    assert queued["queued"] is True
    snap = automation_snapshot(str(tmp_path), "daily-review")
    assert snap["specs"][0]["enabled"] is False
    assert snap["executions"][0]["execution_id"] == queued["execution_id"]
    assert all("request_json" not in row and "result_json" not in row
               for row in snap["executions"])
    assert {row["id"] for row in snap["recipes"]} >= {
        "daily-repo-review", "release-readiness", "page-monitor", "webhook-triage"}


def test_memory_review_surface_preserves_lifecycle_and_requires_confirmation(tmp_path):
    from harness.controlcenter import memory_review, memory_snapshot
    from harness.memory import SqliteMemory

    data = tmp_path / "data"
    data.mkdir()
    memory = SqliteMemory(str(data / "memory.db"), embedder=None)
    proposal = memory.propose("The private release window is Friday", project="demo",
                              evidence="source receipt", provenance="test")
    memory.close()
    pending = memory_snapshot(str(tmp_path), status="proposed")
    assert pending["claims"][0]["id"] == proposal
    try:
        memory_review(proposal, "approve", str(tmp_path), confirmed=False)
    except ValueError as exc:
        assert "confirmed=true" in str(exc)
    else:
        raise AssertionError("memory review needs exact confirmation")
    reviewed = memory_review(proposal, "attest", str(tmp_path), note="I checked the receipt",
                             confirmed=True)
    assert reviewed["claim"]["status"] == "attested"
    assert reviewed["claim"]["review_source"] == "local_user"


def test_recovery_center_combines_work_and_stalled_delivery_without_private_payload(tmp_path,
                                                                                   monkeypatch):
    from harness import sessions
    from harness.controlcenter import recovery_snapshot
    from harness.ops import OpsStore

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr("harness.doctor._command_version", lambda path: {
        "path": "collie", "version": "0.21.32", "ok": True, "error": ""})
    monkeypatch.setattr("harness.doctor.shutil.which", lambda _: "collie")
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    monkeypatch.setattr("harness.supervisor.remote_notifications_enabled", lambda: True)
    sessions.checkpoint("uncertain", [{"role": "user", "content": "private publish text"}],
                        run_id="r1", state="external_action",
                        detail={"tool_name": "publish", "tool_call_id": "c1"})
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.enqueue("notice", "private title", "private body", now=10)
        store.beat("notification-pump", "running", ttl=20, now=10)
    snap = recovery_snapshot(str(tmp_path))
    assert {row["kind"] for row in snap["items"]} >= {"interactive", "notification"}
    wire = json.dumps(snap)
    assert "private publish text" not in wire
    assert "private title" not in wire and "private body" not in wire


def _drifted_command(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr("harness.doctor._command_version", lambda path: {
        "path": "old/collie", "version": "0.24.0", "ok": True, "error": ""})
    monkeypatch.setattr("harness.doctor.shutil.which", lambda _: "old/collie")
    # No browser bridge on this host: keep the snapshot about versions and recovery only.
    monkeypatch.setattr("harness.doctor._get_json", lambda *_a, **_k: {})
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    monkeypatch.setattr("harness.supervisor.remote_notifications_enabled", lambda: False)


def test_recovery_center_lists_version_drift_without_asking_for_a_decision(tmp_path, monkeypatch):
    from harness import __version__
    from harness.controlcenter import recovery_snapshot

    _drifted_command(monkeypatch, tmp_path)
    snap = recovery_snapshot(str(tmp_path))
    drift = [row for row in snap["items"] if row["id"] == "doctor:version-drift"]
    assert len(drift) == 1 and drift[0]["severity"] == "warning"
    assert "0.24.0" in drift[0]["detail"] and __version__ in drift[0]["detail"]
    assert drift[0]["command"] == "collie update --yes"
    assert snap["summary"] == {"total": 1, "needs_you": 0, "warning": 1}
    assert snap["status"] == "warning"


def test_recovery_center_counts_real_recovery_beside_advisory_version_drift(tmp_path,
                                                                            monkeypatch):
    from harness import sessions
    from harness.controlcenter import recovery_snapshot

    _drifted_command(monkeypatch, tmp_path)
    monkeypatch.setattr("harness.update.rollback_status", lambda *_a, **_k: {
        "required": True, "available": True, "state": "rollback_required",
        "previous_version": "0.25.0", "plan": {}, "startup_failures": 3,
        "last_error": "startup checks failed"})
    sessions.checkpoint("uncertain", [{"role": "user", "content": "private publish text"}],
                        run_id="r1", state="external_action",
                        detail={"tool_name": "publish", "tool_call_id": "c1"})
    snap = recovery_snapshot(str(tmp_path))
    severities = {row["id"]: row["severity"] for row in snap["items"]}
    assert severities["doctor:version-drift"] == "warning"
    assert severities["doctor:rollback-required"] == "needs_you"
    assert severities["interactive:uncertain"] == "needs_you"
    assert snap["summary"] == {"total": 3, "needs_you": 2, "warning": 1}
    assert snap["status"] == "needs_you"
    assert "private publish text" not in json.dumps(snap)


def test_security_center_lists_and_revokes_exact_standing_override(tmp_path):
    from harness.controlcenter import security_revoke_risk, security_snapshot
    from harness.overrides import RiskOverrideStore

    store = RiskOverrideStore(str(tmp_path / "risk_overrides.json"))
    store.set("mcp__files__read_*", "read")
    assert security_snapshot(str(tmp_path))["risk_overrides"] == [
        {"pattern": "mcp__files__read_*", "risk": "read"}]
    try:
        security_revoke_risk("mcp__files__read_*", str(tmp_path), confirmed=False)
    except ValueError as exc:
        assert "confirmed=true" in str(exc)
    else:
        raise AssertionError("standing-authority revoke needs confirmation")
    assert security_revoke_risk(
        "mcp__files__read_*", str(tmp_path), confirmed=True)["removed"] is True


def test_security_center_lists_and_revokes_authority_grant(tmp_path):
    from harness.authority import AuthorityStore, GrantScope
    from harness.controlcenter import security_revoke_grant, security_snapshot

    store = AuthorityStore(str(tmp_path / "authority.db"))
    grant = store.add(scope=GrantScope.PROJECT, action="publish", project="collie",
                      target="https://example.test")
    store.close()
    listed = security_snapshot(str(tmp_path))["authority_grants"]
    assert listed == [{
        "id": grant.id, "scope": "project", "action": "publish", "project": "collie",
        "mission_id": "", "workflow_id": "", "connection_id": "",
        "target": "https://example.test", "account": "", "recipients": [],
        "max_amount": None, "currency": "", "expires_at": 0,
        "created_at": grant.created_at, "source": "user",
    }]
    assert security_revoke_grant(grant.id, str(tmp_path), confirmed=True)["removed"] is True
    assert security_snapshot(str(tmp_path))["authority_grants"] == []
