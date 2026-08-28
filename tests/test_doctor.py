import json


def test_doctor_reports_version_drift_without_exposing_notification_content(tmp_path, monkeypatch):
    from harness import doctor
    from harness.ops import OpsStore

    monkeypatch.setattr(doctor, "_command_version", lambda path: {
        "path": "old/collie", "version": "0.1.0", "ok": True, "error": ""})
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "old/collie")
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    monkeypatch.setattr("harness.supervisor.remote_notifications_enabled", lambda: True)
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.enqueue("notice", "private title", "private body", now=10)
        store.beat("notification-pump", "running", ttl=20, now=10)
    report = doctor.report(str(tmp_path), probe_services=False, now=400)
    assert any(row["id"] == "version-drift" for row in report["checks"])
    assert any(row["id"] == "notification-backlog" for row in report["checks"])
    wire = json.dumps(report)
    assert "private title" not in wire and "private body" not in wire


def test_doctor_does_not_warn_about_disabled_remote_transport(tmp_path, monkeypatch):
    from harness import doctor
    from harness.ops import OpsStore

    monkeypatch.setattr(doctor, "_command_version", lambda path: {
        "path": "collie", "version": "0.22.0", "ok": True, "error": ""})
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "collie")
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    monkeypatch.setattr("harness.supervisor.remote_notifications_enabled", lambda: False)
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.enqueue("notice", "private", "private", now=10)
        store.beat("notification-pump", "running", ttl=20, now=10)
        store.beat("remote", "running", ttl=20, now=10)
    result = doctor.report(str(tmp_path), probe_services=False, now=400)
    ids = {row["id"] for row in result["checks"]}
    assert "notification-backlog" not in ids
    assert not any(value.startswith("stale-heartbeat:") for value in ids)


def test_doctor_reports_cli_data_databases_from_the_data_directory(tmp_path, monkeypatch):
    from harness import doctor

    monkeypatch.setattr(doctor, "_command_version", lambda path: {
        "path": "collie", "version": "0.22.0", "ok": True, "error": ""})
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "collie")
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    monkeypatch.setattr("harness.supervisor.remote_notifications_enabled", lambda: False)
    data = tmp_path / "data"
    data.mkdir()
    (data / "memory.db").write_bytes(b"memory")
    (data / "runs.db").write_bytes(b"runs")

    result = doctor.report(str(tmp_path), probe_services=False, now=400)
    databases = {row["name"]: row for row in result["databases"]}
    assert databases["memory.db"] == {"name": "memory.db", "present": True, "bytes": 6}
    assert databases["runs.db"] == {"name": "runs.db", "present": True, "bytes": 4}


def test_doctor_repairs_are_bounded_and_confirmation_is_exact(tmp_path):
    from harness.doctor import repair
    from harness.ops import OpsStore

    with OpsStore(str(tmp_path / "ops.db")) as store:
        nid = store.enqueue("notice", "dead", "body", now=10)
        store.claim(now=10)
        store.failed(nid, "offline", max_attempts=1, now=10)
    try:
        repair("retry_dead_notifications", str(tmp_path), confirmed=False)
    except ValueError as exc:
        assert "confirmed=true" in str(exc)
    else:
        raise AssertionError("dead-letter retry needs an explicit confirmation")
    assert repair("retry_dead_notifications", str(tmp_path), confirmed=True)["retried"] == 1
    assert repair("test_notifications", str(tmp_path))["notification_id"].startswith("ntf_")
