"""The CLI management surface shares the Web Mission state."""

import json

from harness import cli
from harness.mission import MissionStore


def _run(capsys, argv):
    rc = cli.main(argv)
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return rc, json.loads(lines[-1])


def test_cli_can_create_pause_resume_and_cancel(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    rc, created = _run(capsys, ["mission", "start", "watch replies", "--json"])
    assert rc == 0 and created["state"] == "queued"
    mid = created["mission_id"]

    rc, paused = _run(capsys, ["mission", "pause", mid, "--json"])
    assert rc == 0 and paused["state"] == "paused"
    rc, resumed = _run(capsys, ["mission", "resume", mid, "--json"])
    assert rc == 0 and resumed["state"] == "queued"
    rc, cancelled = _run(capsys, ["mission", "cancel", mid, "--json"])
    assert rc == 0 and cancelled["state"] == "cancelled"

    rc, listed = _run(capsys, ["mission", "ls", "--json"])
    assert rc == 0 and listed["missions"][0]["mission_id"] == mid


def test_mission_is_a_real_cli_command():
    assert "mission" in cli.CMDS
    assert callable(cli.cmd_mission)


def test_cli_reconciles_only_the_explicit_recovery_state(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    rc, created = _run(capsys, ["mission", "start", "inspect after crash", "--json"])
    mid = created["mission_id"]
    store = MissionStore(str(tmp_path / "jobs.db"))
    assert store.claim_run(mid, lease_s=-1)
    assert store.recover_stale_runs() == 1
    store.close()
    rc, out = _run(capsys, ["mission", "reconcile", mid, "--note",
                            "site and receipts inspected", "--json"])
    assert rc == 0 and out["state"] == "queued"
    assert out["case"]["human_updates"][-1]["recovery"] is True
