"""A fresh code worker assembles its application only past its start gate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from harness import codeworker, primitives


def _isolated_environment(directory):
    prefixes = ("CODEX_", "CLAUDE_", "COLLIE_", "OPENAI_", "ANTHROPIC_", "AZURE_",
                "GOOGLE_", "AWS_", "GEMINI_", "OLLAMA_", "COMPONENT_")
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(prefixes)
           and key.upper() not in ("PYTHONPATH", "PYTHONHOME")
           and not any(word in key.upper() for word in
                       ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH", "KEY", "PROXY"))}
    for name in ("home", "state", "temp", "appdata", "localappdata", "notes", "hf"):
        (directory / name).mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(directory / "home"), USERPROFILE=str(directory / "home"),
               APPDATA=str(directory / "appdata"), LOCALAPPDATA=str(directory / "localappdata"),
               TEMP=str(directory / "temp"), TMP=str(directory / "temp"),
               TMPDIR=str(directory / "temp"), COLLIE_STATE_DIR=str(directory / "state"),
               COLLIE_DATA_DIR=str(directory / "state" / "data"),
               COLLIE_SETTINGS_PATH=str(directory / "state" / "settings.json"),
               COLLIE_SESSIONS_DIR=str(directory / "state" / "sessions"),
               COLLIE_NOTES_DIR=str(directory / "notes"), COLLIE_PROVIDER="mock",
               COLLIE_EMBED="bm25", COLLIE_BROWSER_BRIDGE="0", COLLIE_BROWSER_LIVE="0",
               COLLIE_BROWSER_BRIDGE_NOSPAWN="1", COLLIE_DESKTOP_CONTROL="0",
               COLLIE_NO_APPLE_EVENTS="1", HF_HUB_OFFLINE="1", HF_HOME=str(directory / "hf"),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               PYTHONDONTWRITEBYTECODE="1")
    return env


@pytest.fixture
def worker_environment(monkeypatch):
    # _execute_request owns these child-only settings; restore them in this
    # in-process fixture so it cannot influence another test's authority.
    for name in ("COLLIE_MISSION_CODE_ROOTS", "COLLIE_PROCESS_OWNER", "COLLIE_SESSIONS_DIR"):
        monkeypatch.setenv(name, "fixture-before-worker")


def _request(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {"workspace": str(workspace), "session_dir": str(tmp_path / "sessions"),
            "goal": "synthetic offline request", "mission_id": "fixture-worker",
            "execution_profile": {"provider": "mock", "allow_provider_fallback": False}}


def test_child_assembly_observes_bound_environment_before_execution(
        tmp_path, monkeypatch, worker_environment):
    request = _request(tmp_path)
    stages = []

    def assemble():
        assert os.environ["COLLIE_MISSION_CODE_ROOTS"] == os.path.realpath(request["workspace"])
        assert os.environ["COLLIE_PROCESS_OWNER"] == "mission-code-worker"
        assert os.environ["COLLIE_SESSIONS_DIR"] == request["session_dir"]
        stages.append("assemble")

    def execute(goal, workspace, **kwargs):
        assert stages == ["assemble"]
        assert kwargs["execution_profile"]["provider"] == "mock"
        stages.append("execute")
        return {"answer": "synthetic result", "verified": False}

    monkeypatch.setattr(codeworker, "bootstrap_application", assemble)
    monkeypatch.setattr(primitives, "_live_code", execute)
    assert codeworker._execute_request(request)["answer"] == "synthetic result"
    assert stages == ["assemble", "execute"]


def test_child_entry_waits_for_start_gate_before_assembly(
        tmp_path, monkeypatch, worker_environment):
    request = _request(tmp_path)
    incoming, result = tmp_path / "request.json", tmp_path / "result.json"
    incoming.write_text(json.dumps(request), encoding="utf-8")
    stages = []
    monkeypatch.setattr(codeworker, "_wait_start_gate", lambda *args: stages.append("gate"))

    def assemble():
        assert stages == ["gate"]
        stages.append("assemble")

    def execute(*args, **kwargs):
        assert stages == ["gate", "assemble"]
        stages.append("execute")
        return {"answer": "synthetic result", "verified": False}

    monkeypatch.setattr(codeworker, "bootstrap_application", assemble)
    monkeypatch.setattr(primitives, "_live_code", execute)
    assert codeworker.main(["_execute", "--request", str(incoming), "--result", str(result),
                            "--start-gate", str(tmp_path / "gate"), "--start-token", "synthetic"]) == 0
    assert stages == ["gate", "assemble", "execute"]
    assert json.loads(result.read_text(encoding="utf-8"))["answer"] == "synthetic result"


def test_rejected_start_gate_cannot_assemble_or_execute(tmp_path, monkeypatch):
    result = tmp_path / "result.json"

    def rejected(*args):
        raise PermissionError("synthetic rejected start gate")

    monkeypatch.setattr(codeworker, "_wait_start_gate", rejected)
    monkeypatch.setattr(codeworker, "bootstrap_application",
                        lambda: pytest.fail("a rejected gate cannot assemble the application"))
    assert codeworker.main(["_execute", "--request", str(tmp_path / "missing.json"),
                            "--result", str(result), "--start-gate", str(tmp_path / "gate"),
                            "--start-token", "synthetic"]) == 0
    assert json.loads(result.read_text(encoding="utf-8"))["exception_type"] == "PermissionError"


def test_invalid_workspace_does_not_assemble(tmp_path, monkeypatch):
    monkeypatch.setattr(codeworker, "bootstrap_application",
                        lambda: pytest.fail("an invalid workspace cannot assemble the application"))
    with pytest.raises(ValueError, match="workspace does not exist"):
        codeworker._execute_request({"workspace": str(tmp_path / "missing")})


def test_worker_rejects_an_import_root_outside_its_package(tmp_path):
    worker = tmp_path / "request-workspace" / "codeworker.py"
    worker.parent.mkdir()
    worker.touch()
    with pytest.raises(RuntimeError, match="trusted harness package"):
        codeworker._source_import_roots(str(worker))


def test_flat_worker_restores_only_its_own_package_parent(tmp_path):
    worker = tmp_path / "flat-source" / "harness" / "codeworker.py"
    worker.parent.mkdir(parents=True)
    worker.touch()
    assert codeworker._source_import_roots(str(worker)) == [str(worker.parent.parent)]
    assert not (worker.parent.parent / "components").exists()


@pytest.mark.parametrize("released", [False, True])
def test_fresh_canonical_child_assembles_only_after_authentic_gate(tmp_path, released):
    root = Path(__file__).resolve().parents[1]
    assert not (root / "components").exists()
    isolated = tmp_path / "fresh-worker"
    isolated.mkdir()
    workspace = isolated / "workspace"
    workspace.mkdir()
    (isolated / "request.json").write_text(json.dumps({
        "workspace": str(workspace), "session_dir": str(isolated / "sessions"),
        "goal": "synthetic offline request", "execution_profile": {"provider": "mock"}}), encoding="utf-8")
    (isolated / "gate.json").write_text(json.dumps({
        "token": "synthetic" if released else "unreleased"}), encoding="utf-8")
    env = _isolated_environment(isolated)
    code = """
import importlib.abc, json, pathlib, socket, subprocess, sys
root, isolated = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
sys.path.insert(0, str(root))
class NoFeatures(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('anthropic', 'openai', 'openai_codex', 'claude_agent_sdk'):
            raise AssertionError('vendor SDK forbidden in canned worker fixture')
        return None
sys.meta_path.insert(0, NoFeatures())
def deny(*args, **kwargs):
    raise AssertionError('network/process forbidden in canned worker fixture')
socket.socket.connect = deny
socket.create_connection = deny
class NoProcesses(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        deny()
subprocess.Popen = NoProcesses
from harness import application_ports, codeworker, primitives
assert pathlib.Path(codeworker.__file__).resolve() == root / 'harness' / 'codeworker.py'
assert 'harness.cli' not in sys.modules
def canned(*args, **kwargs):
    assert callable(application_ports.cli_factory('make_harness'))
    return {'answer': 'canned fixed result', 'verified': False}
primitives._live_code = canned
codeworker.main(['_execute', '--request', str(isolated / 'request.json'),
                 '--result', str(isolated / 'result.json'), '--start-gate', str(isolated / 'gate.json'),
                 '--start-token', 'synthetic'])
value = json.loads((isolated / 'result.json').read_text(encoding='utf-8'))
released = json.loads((isolated / 'gate.json').read_text(encoding='utf-8'))['token'] == 'synthetic'
if released:
    assert value.get('answer') == 'canned fixed result', value
    assert 'harness.cli' in sys.modules
else:
    assert value.get('exception_type') == 'RuntimeError', value
    assert 'harness.cli' not in sys.modules
print('fresh canonical worker gate/assembly checked')
"""
    result = subprocess.run([sys.executable, "-I", "-c", code, str(root), str(isolated)],
                            cwd=str(isolated), env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "fresh canonical worker gate/assembly checked" in result.stdout


def test_fresh_flat_script_reads_mock_fixture_after_real_start_release(tmp_path):
    """Exercise the actual -I file entrypoint, without a source PYTHONPATH."""
    root = Path(__file__).resolve().parents[1]
    assert Path(codeworker.__file__).resolve() == root / "harness" / "codeworker.py"
    assert not (root / "components").exists()
    isolated = tmp_path / "actual-flat-worker"
    env = _isolated_environment(isolated)
    workspace = isolated / "workspace"
    workspace.mkdir()
    fixture = workspace / "fixture.txt"
    fixture.write_text("synthetic_flat_worker_marker\n", encoding="utf-8")
    # An untrusted cwd must not execute Python startup code, even before release.
    (workspace / "sitecustomize.py").write_text(
        "raise AssertionError('request workspace is not a Python startup root')\n", encoding="utf-8")
    incoming, result, gate = [isolated / name for name in ("request.json", "result.json", "gate.json")]
    sid = "flat-script-read"
    incoming.write_text(json.dumps({
        "workspace": str(workspace), "session_dir": env["COLLIE_SESSIONS_DIR"],
        "goal": "Read fixture.txt and report it without editing.", "session_id": sid,
        "mission_id": sid, "execution_profile": {
            "provider": "mock", "model": "mock-planner-v1", "billing_mode": "local",
            "subscription_only": False, "allow_provider_fallback": False},
        "slice_turns": 3, "max_model_calls": 3, "verify_command": "",
        "max_session_storage_bytes": 1000000}), encoding="utf-8")
    command = [sys.executable, "-I", str(root / "harness" / "codeworker.py"), "_execute",
               "--request", str(incoming), "--result", str(result),
               "--start-gate", str(gate), "--start-token", "synthetic-release"]
    process = subprocess.Popen(command, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=os.name != "nt",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        time.sleep(.1)
        assert process.poll() is None
        assert not result.exists()
        codeworker._publish_start_gate(str(gate), "synthetic-release")
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            from harness import plat
            plat.kill_tree(process)
            process.communicate(timeout=10)
    assert process.returncode == 0, stderr.decode("utf-8", "replace")
    assert not stdout
    outcome = json.loads(result.read_text(encoding="utf-8"))
    assert not outcome.get("exception_type")
    assert outcome.get("recovery_required") is False, outcome
    assert not outcome["error"] and not outcome["receipt_error"]
    assert outcome["recovery_required"] is False and outcome["needs_human"] is False
    assert outcome["cancelled"] is False
    assert outcome["model_calls"] == 2
    assert "synthetic_flat_worker_marker" in outcome["answer"]
    assert fixture.read_text(encoding="utf-8") == "synthetic_flat_worker_marker\n"
    saved = json.loads((Path(env["COLLIE_SESSIONS_DIR"]) / (sid + ".json")).read_text(encoding="utf-8"))
    assert any(receipt["kind"] == "mission_code_baseline" for receipt in saved["run_receipts"])
    slices = [receipt for receipt in saved["run_receipts"] if receipt["kind"] == "mission_code_slice"]
    assert len(slices) == 1 and slices[0]["transcript_persisted"] is True
    calls = [call for message in saved["messages"] for call in message.get("tool_calls", [])]
    assert [call["name"] for call in calls] == ["read_file"]
