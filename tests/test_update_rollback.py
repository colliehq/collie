from harness import update


def test_update_journal_requires_repeated_failures_and_never_executes_plan(tmp_path):
    path = str(tmp_path / "journal.json")
    value = update.begin_update_journal(
        artifact="Collie-Setup.exe", mode="windows-handoff", parts=["web", "bridge"],
        target_version="9.9.9", artifact_sha256="abc",
        rollback={"kind": "verified_installer", "path": "previous.exe", "sha256": "def"},
        path=path, now=10)
    assert value["state"] == "installing" and value["previous_version"]
    assert update.record_update_handoff(path=path, now=11)["state"] == "pending_startup"
    assert update.record_startup_health(False, "bad import", path=path,
                                        now=12)["state"] == "startup_failed"
    update.record_startup_health(False, "bad import", path=path, now=13)
    third = update.record_startup_health(False, "bad import", path=path, now=14)
    assert third["state"] == "rollback_required"
    status = update.rollback_status(path)
    assert status["required"] and status["available"]
    assert status["plan"]["path"] == "previous.exe"  # data only; no subprocess hook exists here


def test_successful_startup_blesses_update_and_clears_failure_counter(tmp_path):
    path = str(tmp_path / "journal.json")
    update.begin_update_journal(artifact="setup.exe", mode="test", path=path, now=1)
    update.record_update_handoff(path=path, now=2)
    update.record_startup_health(False, "temporary", path=path, now=3)
    healthy = update.record_startup_health(True, "", path=path, now=4)
    assert healthy["state"] == "healthy"
    assert healthy["startup_failures"] == 0 and healthy["healthy_at"] == 4
    assert not update.rollback_status(path)["required"]
