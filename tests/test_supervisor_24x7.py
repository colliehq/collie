import json
import os
import subprocess
import xml.etree.ElementTree as ET

import pytest

from harness import supervisor
from harness.ops import OpsStore


class FakeProcess:
    def __init__(self, pid=123, code=None):
        self.pid = pid
        self.code = code
        self.stdout = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 0

    def wait(self, timeout=None):
        return self.code


def test_task_xml_has_logon_boot_restart_and_no_system_identity(tmp_path):
    text = supervisor.task_xml(
        r"C:\Python\pythonw.exe", str(tmp_path / "supervisor.json"),
        "S-1-5-21-123", include_boot=True)
    root = ET.fromstring(text)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:LogonTrigger", ns) is not None
    assert root.find(".//t:BootTrigger", ns) is not None
    assert root.findtext(".//t:StartWhenAvailable", namespaces=ns) == "true"
    assert root.findtext(".//t:WakeToRun", namespaces=ns) == "true"
    assert root.findtext(".//t:RestartOnFailure/t:Count", namespaces=ns) == "999"
    assert root.findtext(".//t:LogonType", namespaces=ns) == "InteractiveToken"
    assert "SYSTEM" not in text


def test_install_uses_task_scheduler_then_safe_startup_fallback(tmp_path, monkeypatch):
    root = tmp_path / "state"
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(supervisor.plat, "is_windows", lambda: True)

    calls = []
    def success(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "whoami.exe":
            return subprocess.CompletedProcess(argv, 0, '"user","S-1-5-21-1"\n', "")
        return subprocess.CompletedProcess(argv, 0, "created", "")

    result = supervisor.install_windows(
        root=str(root), pythonw=os.path.abspath(__file__), runner=success)
    assert result["mode"] == "scheduled_task"
    assert any(call[0] == "schtasks.exe" and "/Create" in call for call in calls)
    assert (root / "supervisor-task.xml").exists()

    def refused(argv, **kwargs):
        if argv[0] == "whoami.exe":
            return subprocess.CompletedProcess(argv, 0, '"user","S-1-5-21-1"\n', "")
        return subprocess.CompletedProcess(argv, 1, "", "access denied")

    fallback = supervisor.install_windows(
        root=str(tmp_path / "fallback"), pythonw=os.path.abspath(__file__), runner=refused)
    assert fallback["mode"] == "startup_fallback" and fallback["degraded"] is True
    assert os.path.isfile(fallback["launcher"]) and os.path.isfile(fallback["boot_script"])


def test_install_retries_logon_only_when_boot_trigger_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor.plat, "is_windows", lambda: True)
    creates = []

    def runner(argv, **kwargs):
        if argv[0] == "whoami.exe":
            return subprocess.CompletedProcess(argv, 0, '"user","S-1-5-21-1"\n', "")
        creates.append(argv)
        code = 1 if len(creates) == 1 else 0
        return subprocess.CompletedProcess(argv, code, "", "boot trigger denied" if code else "")

    result = supervisor.install_windows(
        root=str(tmp_path / "state"), pythonw=os.path.abspath(__file__), runner=runner)
    assert result["mode"] == "scheduled_task"
    assert result["boot"] is False and result["degraded"] is True
    assert len(creates) == 2
    xml = (tmp_path / "state" / "supervisor-task.xml").read_text(encoding="utf-16")
    assert "LogonTrigger" in xml and "BootTrigger" not in xml


def test_worker_crash_backoff_restart_and_unresponsive_recovery(tmp_path):
    processes = [FakeProcess(1), FakeProcess(2)]
    def popen(*args, **kwargs):
        return processes.pop(0)

    healthy = [False, True, False, False, True, False, False]
    def probe(spec):
        return healthy.pop(0) if healthy else False

    spec = supervisor.WorkerSpec(
        "worker", ["python", "worker.py"], probe_url="http://health",
        startup_grace_s=2, stable_s=10, max_backoff_s=10)
    with OpsStore(str(tmp_path / "ops.db")) as store:
        runtime = supervisor.WorkerRuntime(
            spec, store, str(tmp_path), popen=popen, probe=probe, clock=lambda: 0)
        assert runtime.step(0) == "starting"
        assert runtime.step(1) == "running"
        runtime.process.code = 7
        assert runtime.step(2) == "backoff"
        assert runtime.step(3) == "backoff"
        assert runtime.step(4) == "starting"
        # Live but repeatedly unresponsive is terminated after its grace window.
        assert runtime.step(5) == "running"
        assert runtime.step(6) == "unhealthy"
        assert runtime.step(9) == "backoff"
        assert runtime.process is None
        beats = store.heartbeats(now=9)
        assert beats["worker:worker"]["state"] == "backoff"
        runtime.close()


def test_supervisor_detects_sleep_resume_and_wakes_all_workers(tmp_path):
    class Runtime:
        def __init__(self, spec, store, root):
            self.spec = spec
            self.wakes = []
        def step(self, now): return "running"
        def wake(self, now): self.wakes.append(now)
        def close(self): pass

    config = {
        "schema": 1, "state_dir": str(tmp_path), "poll_interval_s": 5,
        "alert_interval_s": 999,
        "workers": [supervisor.WorkerSpec("web", ["python"]).as_dict()],
    }
    with OpsStore(str(tmp_path / "ops.db")) as store:
        sup = supervisor.Supervisor(
            config, store=store, runtime_factory=Runtime,
            clock=lambda: 100, monotonic=lambda: 10)
        sup.step(now=100, mono=10)
        sup.step(now=200, mono=100)
        assert sup.workers[0].wakes == [200]
        assert store.heartbeats(now=200)["power"]["state"] == "resumed"


def test_default_config_never_persists_secret_environment(tmp_path):
    spec = supervisor.WorkerSpec.from_dict({
        "name": "x", "argv": ["python"],
        "env": {"NORMAL": "yes", "API_KEY": "secret", "ACCESS_TOKEN": "secret",
                "OPENAI_APIKEY": "secret", "GITHUB_PAT": "secret",
                "AWS_ACCESS_KEY_ID": "secret", "AUTHORIZATION": "secret"},
    })
    assert spec.env == {"NORMAL": "yes"}
    config = supervisor.default_config(str(tmp_path), python="python")
    supervisor.save_config(config, str(tmp_path / "supervisor.json"))
    assert "secret" not in (tmp_path / "supervisor.json").read_text(encoding="utf-8")


def test_worker_identity_cannot_escape_log_root_or_persist_probe_credentials():
    with pytest.raises(ValueError, match="path-safe"):
        supervisor.WorkerSpec.from_dict({
            "name": "../outside", "argv": ["python"],
        })
    with pytest.raises(ValueError, match="embed credentials"):
        supervisor.WorkerSpec.from_dict({
            "name": "safe", "argv": ["python"],
            "probe_url": "https://user:password@example.com/health",
        })


def test_slack_worker_adopts_fresh_legacy_heartbeat_then_takes_over(tmp_path):
    spawned = []

    def popen(*args, **kwargs):
        spawned.append(args)
        return FakeProcess(pid=456)

    spec = supervisor.WorkerSpec.from_dict({
        "name": "slack-rowan",
        "argv": ["python", "-m", "harness.cli", "slack", "--name", "Rowan"],
    })
    assert spec.adopt_heartbeat == "slack:rowan"  # migration for existing schema-1 config
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.beat("slack:rowan", "connected", {}, pid=321, ttl=10, now=100)
        runtime = supervisor.WorkerRuntime(
            spec, store, str(tmp_path), popen=popen, probe=lambda _: False, clock=lambda: 0)
        assert runtime.step(105) == "external"
        row = store.heartbeats(now=105)["worker:slack-rowan"]
        assert row["pid"] == 0
        assert row["detail"]["external_pid"] == 321
        assert not spawned
        assert runtime.step(111) == "starting"
        assert len(spawned) == 1
        runtime.close()


def test_load_config_discovers_slack_added_after_initial_install(tmp_path):
    cfg = supervisor.default_config(str(tmp_path), python="old-python")
    supervisor.save_config(cfg, str(tmp_path / "supervisor.json"))
    launcher = tmp_path / "slack-Rowan.pyw"
    launcher.write_text(
        "sys.argv = ['collie'] + ['slack', '--name', 'Rowan', '--listen']\n",
        encoding="utf-8")

    loaded = supervisor.load_config(str(tmp_path / "supervisor.json"), python="new-python")
    rowan = next(item for item in loaded["workers"] if item["name"] == "slack-rowan")
    assert rowan["argv"][0] == "new-python"
    assert rowan["adopt_heartbeat"] == "slack:rowan"
