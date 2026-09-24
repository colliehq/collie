"""One jobs daemon at a time, and a supervisor adopts the one already running.

A supervisor restart leaves its children running (the scheduled task's job object does not kill
them), and the next supervisor started another jobd: two daemons ticking one jobs.db, which is
where the developer machine's "mission tick paused: database is locked" lines came from. The same
restart left automations with a second copy that could not take its lock, exited, was restarted and
ended "circuit open", reported stopped while one was running.
"""
import json
import os
import subprocess
import sys
import time

from harness import supervisor
from harness.ops import OpsStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(state):
    return dict(os.environ, COLLIE_STATE_DIR=str(state), COLLIE_OPS_DB=str(state / "ops.db"),
                PYTHONPATH=ROOT)


def test_a_second_jobs_daemon_says_so_and_exits(tmp_path):
    lock = supervisor.InstanceLock(str(tmp_path / "jobd.lock"), what="The Collie jobs daemon")
    try:
        proc = subprocess.run([sys.executable, "-m", "harness.cli", "jobs", "daemon",
                               "--interval", "1"], cwd=ROOT, env=_env(tmp_path),
                              capture_output=True, text=True, timeout=60)
    finally:
        lock.close()
    assert proc.returncode == 3, (proc.returncode, proc.stderr[-500:])
    assert "The Collie jobs daemon is already running" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_a_running_jobs_daemon_reports_a_heartbeat(tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "harness.cli", "jobs", "daemon",
                             "--interval", "1"], cwd=ROOT, env=_env(tmp_path),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 45
        beat = {}
        while time.time() < deadline:
            if (tmp_path / "ops.db").exists():
                with OpsStore(str(tmp_path / "ops.db")) as store:
                    beat = store.heartbeats().get("jobs-daemon") or {}
                if beat.get("fresh"):
                    break
            time.sleep(0.5)
        assert beat.get("fresh") and beat.get("state") == "running", beat
        from harness import plat
        assert plat.pid_alive(beat.get("pid"))      # the daemon itself (a venv adds a launcher)
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_an_older_supervisor_config_learns_the_heartbeats_to_adopt_by(tmp_path):
    config = supervisor.default_config(str(tmp_path), sys.executable)
    for worker in config["workers"]:
        if worker["name"] in ("jobd", "automations"):
            worker["adopt_heartbeat"] = ""          # as written by an earlier release
    path = tmp_path / "supervisor.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    loaded = {w["name"]: w for w in supervisor.load_config(str(path))["workers"]}
    assert loaded["jobd"]["adopt_heartbeat"] == "jobs-daemon"
    assert loaded["automations"]["adopt_heartbeat"] == "automation-daemon"


class _Proc:
    def __init__(self, pid, code=None):
        self.pid, self.code, self.stdout = pid, code, None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 0

    def wait(self, timeout=None):
        return self.code


def test_a_daemon_left_running_by_an_earlier_supervisor_is_adopted_not_doubled(tmp_path):
    spawned = []
    spec = supervisor.WorkerSpec("jobd", ["python", "jobd"], adopt_heartbeat="jobs-daemon",
                                 startup_grace_s=2, stable_s=10, max_backoff_s=10)
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.beat("jobs-daemon", "running", {}, pid=os.getpid(), ttl=180, now=1000.0)
        runtime = supervisor.WorkerRuntime(
            spec, store, str(tmp_path), popen=lambda *a, **k: spawned.append(1) or _Proc(51),
            probe=lambda spec: True, clock=lambda: 1001.0)
        assert runtime.step(1001.0) == "external"
        assert spawned == []
        runtime.close()


def test_a_fresh_beat_from_a_process_that_is_gone_is_not_adopted(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=30)
    spawned = []
    spec = supervisor.WorkerSpec("jobd", ["python", "jobd"], adopt_heartbeat="jobs-daemon",
                                 startup_grace_s=2, stable_s=10, max_backoff_s=10)
    with OpsStore(str(tmp_path / "ops.db")) as store:
        store.beat("jobs-daemon", "running", {}, pid=dead.pid, ttl=180, now=1000.0)
        runtime = supervisor.WorkerRuntime(
            spec, store, str(tmp_path), popen=lambda *a, **k: spawned.append(1) or _Proc(52),
            probe=lambda spec: True, clock=lambda: 1001.0)
        assert runtime.step(1001.0) == "starting" and spawned == [1]
        runtime.close()


def test_its_own_child_that_just_died_is_restarted_not_adopted(tmp_path):
    child = _Proc(77)
    spawned = []

    def popen(*a, **k):
        spawned.append(1)
        return child if len(spawned) == 1 else _Proc(78)

    spec = supervisor.WorkerSpec("jobd", ["python", "jobd"], adopt_heartbeat="jobs-daemon",
                                 startup_grace_s=2, stable_s=10, max_backoff_s=1)
    clock = [1000.0]
    with OpsStore(str(tmp_path / "ops.db")) as store:
        runtime = supervisor.WorkerRuntime(spec, store, str(tmp_path), popen=popen,
                                           probe=lambda spec: True, clock=lambda: clock[0])
        assert runtime.step(clock[0]) == "starting"
        store.beat("jobs-daemon", "running", {}, pid=77, ttl=180, now=clock[0])   # the child beats
        child.code = 1                                                           # ...then dies
        clock[0] += 30
        runtime.step(clock[0])                                                  # sees the exit
        clock[0] += 30
        assert runtime.step(clock[0]) == "starting", "its own beat must not look like a stranger"
        assert len(spawned) == 2
        runtime.close()
