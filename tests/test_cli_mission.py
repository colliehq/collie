"""The CLI management surface shares the Web Mission state."""

import json

from harness import cli, settings
from harness.mission import MissionStore
from harness.jobs import FAILED_S


def _allow_claude_subscription(provider, *, account_evidence=None, environ=None,
                               model="", require_direct_probe=True):
    if provider != "claude-agent-sdk" or account_evidence is not None:
        raise RuntimeError("unreviewed subscription route")
    assert isinstance(environ, dict)
    return {
        "format": "collie-subscription-guard-v1",
        "schema_version": 1,
        "provider": provider,
        "verdict": "allow",
    }


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


def test_cli_starts_atomic_subscription_only_overnight_code_mission(
        monkeypatch, tmp_path, capsys):
    state = tmp_path / "state"
    repo = tmp_path / "existing-repo"
    repo.mkdir()
    marker = repo / "owned-by-user.txt"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_PROVIDER", "claude-agent-sdk")
    monkeypatch.setenv("COLLIE_MODEL", "claude-opus-4-8")
    monkeypatch.setattr(
        settings, "_HARD_ENV", settings._HARD_ENV | {"COLLIE_PROVIDER", "COLLIE_MODEL"})
    monkeypatch.setattr(
        "harness.subscription_guard.check_subscription_guard",
        _allow_claude_subscription)

    rc, created = _run(capsys, [
        "mission", "start", "finish the refactor and prove it green",
        "--code", "--workspace", str(repo), "--overnight",
        "--provider", "claude-agent-sdk", "--model", "claude-opus-4-8",
        "--verify-command", "python -m pytest -q", "--no-paid-overage", "--json",
    ])

    assert rc == 0 and created["state"] == "queued"
    assert created["workspace_request"] is False
    assert created["tasktree"]["attached"] is True
    assert created["run_tree"]["root"]["owns_workspace"] is False
    assert created["run_tree"]["root"]["workspace"] == str(repo.resolve())
    assert created["case"]["_isolated_workspace"] == str(repo.resolve())
    assert created["case"]["execution_profile"] == {
        "version": 1,
        "profile": "overnight",
        "provider": "claude-agent-sdk",
        "model": "claude-opus-4-8",
        "billing_mode": "subscription",
        "subscription_only": True,
        "allow_provider_fallback": False,
    }
    assert created["case"]["code_profile"]["verify_command"] == "python -m pytest -q"
    store = MissionStore(str(state / "jobs.db"))
    try:
        mission = store.get(created["mission_id"])
        assert "code" in mission.leash["may"]
        assert mission.leash["max_active_wall_seconds"] == 43_200
        assert mission.leash["max_elapsed_seconds"] == 604_800
        assert mission.leash["max_model_calls"] == 4_000
        assert mission.leash["max_model_tokens"] == 8_000_000
        assert mission.leash["max_model_cost_usd"] == 0.01
        assert len(mission.leash["execution_profile_sha256"]) == 64
    finally:
        store.close()
    rc, cancelled = _run(
        capsys, ["mission", "cancel", created["mission_id"], "--json"])
    assert rc == 0 and cancelled["state"] == "cancelled"
    assert repo.is_dir(), "cancelling must not delete a user-owned workspace"
    assert marker.read_text(encoding="utf-8") == "keep"


def test_cli_reports_sdk_subscription_preflight_denial_without_traceback(
        monkeypatch, tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLLIE_PROVIDER", "claude-agent-sdk")
    monkeypatch.setenv("COLLIE_MODEL", "claude-opus-4-8")
    monkeypatch.setattr(
        settings, "_HARD_ENV", settings._HARD_ENV | {"COLLIE_PROVIDER", "COLLIE_MODEL"})

    def deny(*_args, **_kwargs):
        raise RuntimeError("Agent SDK inference unavailable")

    monkeypatch.setattr("harness.subscription_guard.check_subscription_guard", deny)
    rc = cli.main([
        "mission", "start", "work overnight", "--code", "--workspace", str(repo),
        "--overnight", "--no-paid-overage", "--provider", "claude-agent-sdk",
        "--model", "claude-opus-4-8", "--json",
    ])
    output = capsys.readouterr().out

    assert rc == 1
    assert "subscription preflight denied: RuntimeError" in output
    assert "Traceback" not in output


def test_cli_exports_redacted_progress_report(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    rc, created = _run(capsys, ["mission", "start", "report progress", "--json"])
    mid = created["mission_id"]
    rc, report = _run(capsys, ["mission", "report", mid, "--json"])
    assert rc == 0 and report["format_version"] == 1
    assert report["mission_id"] == mid and "case" not in report
    assert report["markdown"].startswith("# Mission progress:")
    assert report["runtime"]["model_cost_usd"] == 0.0
    assert report["runtime"]["equivalent_model_cost_usd"] == 0.0
    assert "API-equivalent" in report["markdown"]


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


def test_cli_retries_failed_mission_as_a_fenced_successor(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    rc, created = _run(capsys, ["mission", "start", "finish campaign", "--auto", "--json"])
    old = created["mission_id"]
    store = MissionStore(str(tmp_path / "jobs.db"))
    store.set_state(old, FAILED_S, "rich editor was not filled")
    store.close()

    rc, retried = _run(capsys, ["mission", "retry", old, "--note",
                                "No external write occurred; use the editor fix.", "--json"])
    assert rc == 0 and retried["state"] == "queued"
    assert retried["mission_id"] != old
    assert retried["goal"] == "finish campaign"
    assert retried["case"]["predecessor"]["mission_id"] == old
    assert retried["case"]["human_updates"][-1]["recovery"] is True
    assert retried["controls"] == ["run", "pause", "cancel"]

    rc, old_status = _run(capsys, ["mission", "status", old, "--json"])
    assert rc == 0 and old_status["state"] == "failed"
    assert old_status["controls"] == ["retry"]
