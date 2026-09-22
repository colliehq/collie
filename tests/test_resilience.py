import json

from harness import resilience


def test_fault_matrix_exercises_every_declared_scenario(tmp_path):
    report = resilience.run_fault_matrix(scratch_root=str(tmp_path))
    assert report["status"] == "PASS", report
    assert report["failed"] == 0
    assert {row["name"] for row in report["scenarios"]} == set(
        resilience.scenario_names())
    assert all(row["status"] == "PASS" for row in report["scenarios"])
    # Report is intentionally content-free enough to attach to an issue.
    wire = json.dumps(report)
    assert "private body" not in wire and "private title" not in wire


def test_matrix_rejects_unknown_scenario(tmp_path):
    try:
        resilience.run_fault_matrix(scenarios=["not-real"], scratch_root=str(tmp_path))
    except ValueError as exc:
        assert "unknown resilience scenario" in str(exc)
    else:
        raise AssertionError("unknown scenario was accepted")


def test_soak_checkpoints_each_cycle_and_finishes(tmp_path):
    report_path = tmp_path / "soak.json"
    times = iter([100.0, 100.1, 100.21])

    report = resilience.run_soak(
        duration_s=.2, interval_s=.1, report_path=str(report_path),
        scenarios=["notification_disconnect"], scratch_root=str(tmp_path),
        clock=lambda: next(times), sleeper=lambda _seconds: None)

    assert report["status"] == "PASS"
    assert report["cycles"] == 2
    stored = json.loads(report_path.read_text(encoding="utf-8"))
    assert stored["cycles"] == 2 and stored["last_matrix"]["status"] == "PASS"


def test_soak_resumes_unfinished_checkpoint_and_records_long_gap(tmp_path):
    report_path = tmp_path / "soak.json"
    report_path.write_text(json.dumps({
        "schema": resilience.SCHEMA, "kind": "soak", "collie_version": "old",
        "status": "RUNNING", "started_at": 0.0, "updated_at": 1.0,
        "target_duration_s": 1000.0, "interval_s": 10.0,
        "scenarios_selected": ["notification_disconnect"], "cycles": 1,
        "passed_cycles": 1, "failed_cycles": 0, "sleep_or_restart_gaps": 0,
        "last_cycle_at": 1.0, "last_matrix": {}, "failures": [],
    }), encoding="utf-8")
    times = iter([1000.0, 1000.0])
    report = resilience.run_soak(
        duration_s=1.0, interval_s=10.0, report_path=str(report_path),
        scenarios=["notification_disconnect"], scratch_root=str(tmp_path),
        clock=lambda: next(times), sleeper=lambda _seconds: None)
    assert report["status"] == "PASS"
    assert report["cycles"] == 2
    assert report["sleep_or_restart_gaps"] == 1


def test_resilience_cli_matrix_is_machine_readable(tmp_path, capsys):
    from harness import cli

    assert cli.main([
        "resilience", "matrix", "--scenarios", "notification_disconnect",
        "--scratch-root", str(tmp_path), "--json",
    ]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "PASS"
    assert [row["name"] for row in report["scenarios"]] == [
        "notification_disconnect"]
