import json


def test_recovery_cli_honours_explicit_state_dir(tmp_path, monkeypatch, capsys):
    from harness import cli, sessions

    sessions_dir = tmp_path / "sessions"
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(sessions_dir))
    sessions.checkpoint(
        "crashed", [{"role": "user", "content": "private prompt"}], run_id="r1",
        state="external_action", detail={"tool_name": "publish", "tool_call_id": "c1"})

    assert cli.main(["recovery", "show", "crashed", "--state-dir", str(tmp_path)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["recovery"]["recovery_required"] is True

    assert cli.main(["recovery", "reconcile", "crashed", "--state-dir", str(tmp_path),
                     "--resolution", "not_fired", "--yes"]) == 0
    assert sessions.recovery_state("crashed", directory=str(sessions_dir))["state"] == "turn_boundary"


def test_recovery_cli_requires_session_and_confirmation(capsys):
    from harness import cli

    assert cli.main(["recovery", "show"]) == 2
    assert "requires a session id" in capsys.readouterr().err
    assert cli.main(["recovery", "reconcile", "missing"]) == 2
    assert "repeat with --yes" in capsys.readouterr().err


def test_supervisor_and_automation_wrappers_forward_options(monkeypatch):
    from harness import automations, cli, supervisor

    calls = []
    monkeypatch.setattr(supervisor, "main", lambda argv: calls.append(("supervisor", argv)) or 0)
    monkeypatch.setattr(automations, "main", lambda argv: calls.append(("automations", argv)) or 0)

    assert cli.main(["supervisor", "install", "--state-dir", "state", "--no-boot",
                     "--disable-worker", "bridge"]) == 0
    assert calls[-1] == ("supervisor", ["install", "--state-dir", "state", "--no-boot",
                                        "--disable-worker", "bridge"])
    assert cli.main(["supervisor", "run", "--state-dir", "ignored", "--config", "c.json"]) == 0
    assert calls[-1] == ("supervisor", ["run", "--config", "c.json"])
    assert cli.main(["automations", "tick", "--state-dir", "state", "--execute"]) == 0
    assert calls[-1] == ("automations", ["tick", "--state-dir", "state", "--execute"])


def test_hooks_cli_trusts_relative_file_by_exact_hash(tmp_path, monkeypatch, capsys):
    from harness import cli

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    config = tmp_path / "hooks.json"
    config.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
        "type": "command", "command": "python -c \"print('{}')\""
    }]}]}}), encoding="utf-8")

    assert cli.main(["hooks", "check", "hooks.json", "--cwd", str(tmp_path)]) == 0
    capsys.readouterr()
    assert cli.main(["hooks", "trust", "hooks.json", "--cwd", str(tmp_path)]) == 0
    trusted = json.loads(capsys.readouterr().out)
    assert trusted["trusted"] is True
    # Status is the source of truth; editing one byte would return this file to pending.
    assert cli.main(["hooks", "status", "hooks.json", "--cwd", str(tmp_path)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["configs"][0]["trusted"] is True
