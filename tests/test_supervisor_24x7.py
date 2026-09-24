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


def test_supervisor_only_queues_health_alerts_when_remote_delivery_is_on(tmp_path, monkeypatch):
    class Runtime:
        def __init__(self, spec, store, root): self.spec = spec
        def step(self, now): return "failed"
        def wake(self, now): pass
        def close(self): pass

    config = {
        "schema": 1, "state_dir": str(tmp_path), "poll_interval_s": 5,
        "alert_interval_s": 5,
        "workers": [supervisor.WorkerSpec("web", ["python"]).as_dict()],
    }
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.enqueue(
            "worker_dead", "legacy", "legacy", dedupe_key="worker-dead:web", now=1)
        monkeypatch.setattr(supervisor, "remote_notifications_enabled", lambda: False)
        off = supervisor.Supervisor(config, store=store, runtime_factory=Runtime,
                                    clock=lambda: 100, monotonic=lambda: 10)
        off.step(now=100, mono=10)
        assert store.notification_stats().get("pending", 0) == 0

        monkeypatch.setattr(supervisor, "remote_notifications_enabled", lambda: True)
        on = supervisor.Supervisor(config, store=store, runtime_factory=Runtime,
                                   clock=lambda: 200, monotonic=lambda: 20)
        on.step(now=200, mono=20)
        assert store.db.execute(
            "SELECT count(*) FROM notifications WHERE kind='worker_dead' AND state='pending'"
        ).fetchone()[0] == 1


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


def test_supervisor_authority_types_and_timing_bounds_are_strict(tmp_path):
    with pytest.raises(ValueError, match="enabled must be boolean"):
        supervisor.WorkerSpec.from_dict({
            "name": "must-not-start", "argv": ["python"], "enabled": "false"})
    with pytest.raises(ValueError, match="finite number"):
        supervisor.WorkerSpec.from_dict({
            "name": "never-stable", "argv": ["python"], "stable_s": float("nan")})
    with pytest.raises(ValueError, match="finite integer"):
        supervisor.WorkerSpec.from_dict({
            "name": "fractional", "argv": ["python"], "max_rapid_failures": 1.5})
    with pytest.raises(ValueError, match="env must be a JSON object"):
        supervisor.WorkerSpec.from_dict({
            "name": "bad-env", "argv": ["python"], "env": []})

    path = tmp_path / "supervisor.json"
    path.write_text(
        '{"schema":1,"poll_interval_s":NaN,"workers":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite JSON"):
        supervisor.load_config(str(path))

    config = supervisor.default_config(str(tmp_path), python="python")
    config["workers"].append(dict(config["workers"][0]))
    with pytest.raises(ValueError, match="names must be unique"):
        supervisor.save_config(config, str(path))


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
    cfg["workers"] = [row for row in cfg["workers"] if row["name"] != "ambient"]
    supervisor.save_config(cfg, str(tmp_path / "supervisor.json"))
    launcher = tmp_path / "slack-Rowan.pyw"
    launcher.write_text(
        "sys.argv = ['collie'] + ['slack', '--name', 'Rowan', '--listen']\n",
        encoding="utf-8")

    loaded = supervisor.load_config(str(tmp_path / "supervisor.json"), python="new-python")
    ambient = next(item for item in loaded["workers"] if item["name"] == "ambient")
    assert ambient["argv"][0] == "new-python"
    rowan = next(item for item in loaded["workers"] if item["name"] == "slack-rowan")
    assert rowan["argv"][0] == "new-python"
    assert rowan["adopt_heartbeat"] == "slack:rowan"


def test_supervisor_lines_carry_a_timestamp_and_the_restart_reason(tmp_path):
    """Exits, starts and self-initiated restarts can be matched to what happened on the machine."""
    import datetime as dt
    processes = [FakeProcess(41), FakeProcess(42)]
    clock = [1_790_000_000.0]
    spec = supervisor.WorkerSpec("web", ["python", "web.py"], probe_url="http://health",
                                 startup_grace_s=2, stable_s=10, max_backoff_s=10)
    with OpsStore(str(tmp_path / "ops.db")) as store:
        runtime = supervisor.WorkerRuntime(
            spec, store, str(tmp_path), popen=lambda *a, **k: processes.pop(0),
            probe=lambda spec: False, clock=lambda: clock[0])
        runtime.step(clock[0])                  # starts pid 41
        clock[0] += 5
        runtime.step(clock[0] - 5)              # unhealthy since now
        runtime.step(clock[0])                  # past the grace: restart
        runtime.close()
    text = (tmp_path / "logs" / "web.log").read_text(encoding="utf-8")
    stamp = dt.datetime.fromtimestamp(1_790_000_000.0).astimezone().isoformat(timespec="seconds")
    assert "[supervisor %s] started pid 41" % stamp in text
    assert "health probe failed for 5.0s; restarting pid 41" in text
    assert "] stopped" in text
    assert "[supervisor] " not in text          # no unstamped supervisor line remains


def test_a_held_instance_lock_names_its_owner(tmp_path):
    path = str(tmp_path / "automations.lock")
    first = supervisor.InstanceLock(path, what="The Collie automations daemon")
    try:
        with pytest.raises(supervisor.AlreadyRunning) as refused:
            supervisor.InstanceLock(path, what="The Collie automations daemon")
        assert "automations daemon is already running" in str(refused.value)
        assert "supervisor" not in str(refused.value)
        assert isinstance(refused.value, RuntimeError)       # existing callers catch RuntimeError
    finally:
        first.close()


@pytest.mark.parametrize("module,lock_name,argv", [
    ("automations", "automations.lock", ["daemon", "--interval", "1"]),
    ("ambient", "ambient.lock", ["--interval", "1"]),
])
def test_a_second_daemon_exits_with_one_line_instead_of_a_traceback(tmp_path, capsys, monkeypatch,
                                                                     module, lock_name, argv):
    import importlib
    mod = importlib.import_module("harness." + module)
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    if module == "ambient":
        monkeypatch.setattr(mod, "state_path", lambda _d=None: str(tmp_path / "ambient.json"))
    else:
        argv = argv + ["--state-dir", str(tmp_path)]
    held = supervisor.InstanceLock(str(tmp_path / lock_name))
    try:
        assert mod.main(argv) == 3
    finally:
        held.close()
    err = capsys.readouterr().err
    assert "already running" in err and "this copy is exiting" in err
    assert "Traceback" not in err
