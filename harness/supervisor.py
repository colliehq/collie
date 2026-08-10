"""Per-user Collie process supervisor and Windows Scheduled Task lifecycle.

The supervisor is intentionally a normal Python module (``python -m harness.supervisor``), so the
desktop app and ``harness.cli`` only need a thin command/API adapter.  Merely importing this module
does not register a task or start a process.

Windows uses Task Scheduler instead of a Startup-folder fire-and-forget script.  The task carries a
logon trigger, a boot trigger with ``StartWhenAvailable`` (it runs once the user's interactive token
exists), and Task Scheduler's restart-on-failure settings.  We never run Collie as SYSTEM: doing so
would put it in the wrong profile and separate it from the user's OAuth/Keychain-equivalent state.
If registration is refused, installation safely falls back to one Startup VBS which starts *this
supervisor*; the supervisor still supplies child crash recovery.

Microsoft's first-party contracts used here:
https://learn.microsoft.com/windows/win32/taskschd/starting-an-executable-when-a-user-logs-on
https://learn.microsoft.com/powershell/module/scheduledtasks/new-scheduledtasksettingsset
"""

from __future__ import annotations

import argparse
import ast
import csv
import html
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, field

from . import plat
from .ops import (OpsStore, RotatingLog, aggregate_health, enqueue_health_alerts,
                  credential_health)


TASK_NAME = r"\Collie\Supervisor"
SCHEMA = 1
_WORKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_SECRET_ENV_RE = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|APIKEY|CREDENTIAL|AUTH|COOKIE|SESSION|"
    r"PRIVATE_?KEY|ACCESS_?KEY|SIGNING_?KEY|ENCRYPTION_?KEY|BEARER|"
    r"(?:^|_)PAT(?:_|$)|(?:^|_)KEY(?:_|$)|DSN|CONNECTION_?STRING",
    re.I,
)


def state_dir(path: str | None = None) -> str:
    return os.path.abspath(path or os.environ.get("COLLIE_STATE_DIR")
                           or os.path.expanduser("~/.collie"))


def config_path(root: str | None = None) -> str:
    return os.path.join(state_dir(root), "supervisor.json")


def _atomic_json(path: str, value: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _safe_env(env: dict | None) -> dict:
    """Only persist non-secret worker tuning, never credentials."""
    out = {}
    for key, value in (env or {}).items():
        key = str(key)
        if _SECRET_ENV_RE.search(key):
            continue
        out[key] = str(value)
    return out


@dataclass
class WorkerSpec:
    name: str
    argv: list[str]
    enabled: bool = True
    critical: bool = True
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    probe_url: str = ""
    probe_json_key: str = ""
    adopt_heartbeat: str = ""
    startup_grace_s: float = 30.0
    stable_s: float = 120.0
    max_rapid_failures: int = 8
    max_backoff_s: float = 300.0

    @classmethod
    def from_dict(cls, value: dict):
        if not isinstance(value, dict) or not value.get("name"):
            raise ValueError("worker needs a name")
        argv = value.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            raise ValueError("worker %s needs a string argv" % value.get("name"))
        name = str(value["name"])
        if not _WORKER_NAME_RE.fullmatch(name):
            raise ValueError("worker name must be 1-80 path-safe characters")
        probe_url = str(value.get("probe_url") or "")
        parsed_probe = urllib.parse.urlsplit(probe_url)
        if probe_url and (parsed_probe.username is not None or parsed_probe.password is not None):
            raise ValueError("worker probe_url must not embed credentials")
        adopt_heartbeat = str(value.get("adopt_heartbeat") or "")
        # Schema-1 supervisor files created before heartbeat adoption did not carry this field.
        # Infer it for Collie's generated Slack workers so upgrades can immediately adopt an
        # already-connected legacy listener instead of racing its per-dog OS lock.
        if not adopt_heartbeat and name.startswith("slack-"):
            adopt_heartbeat = "slack:" + name[len("slack-"):]
        return cls(
            name=name, argv=list(argv), enabled=bool(value.get("enabled", True)),
            critical=bool(value.get("critical", True)), cwd=str(value.get("cwd") or ""),
            env=_safe_env(value.get("env")), probe_url=probe_url,
            probe_json_key=str(value.get("probe_json_key") or ""),
            adopt_heartbeat=adopt_heartbeat,
            startup_grace_s=float(value.get("startup_grace_s", 30)),
            stable_s=float(value.get("stable_s", 120)),
            max_rapid_failures=int(value.get("max_rapid_failures", 8)),
            max_backoff_s=float(value.get("max_backoff_s", 300)),
        )

    def as_dict(self) -> dict:
        return {
            "name": self.name, "argv": self.argv, "enabled": self.enabled,
            "critical": self.critical, "cwd": self.cwd, "env": _safe_env(self.env),
            "probe_url": self.probe_url, "probe_json_key": self.probe_json_key,
            "adopt_heartbeat": self.adopt_heartbeat,
            "startup_grace_s": self.startup_grace_s, "stable_s": self.stable_s,
            "max_rapid_failures": self.max_rapid_failures,
            "max_backoff_s": self.max_backoff_s,
        }


def _legacy_slack_argv(root: str) -> list[list[str]]:
    """Safely recover argv literals from Collie's generated launchers (never execute them)."""
    found = []
    try:
        names = sorted(n for n in os.listdir(root)
                       if n.startswith("slack-") and n.endswith(".pyw"))
    except OSError:
        names = []
    for name in names:
        try:
            text = open(os.path.join(root, name), encoding="utf-8").read()
            marker = "sys.argv = ['collie'] + "
            line = next(line for line in text.splitlines() if line.startswith(marker))
            argv = ast.literal_eval(line[len(marker):])
            if (isinstance(argv, list) and argv and argv[0] == "slack"
                    and all(isinstance(x, str) for x in argv)):
                found.append(argv)
        except (OSError, ValueError, SyntaxError, StopIteration):
            continue
    return found


def default_config(root: str | None = None, python: str | None = None) -> dict:
    """Generate a secret-free desired-state file; it is not written until :func:`save_config`."""
    root = state_dir(root)
    python = python or sys.executable
    workers = [
        WorkerSpec("web", [python, "-m", "harness.webapp", "--port", "8787", "--no-open"],
                   probe_url="http://127.0.0.1:8787/api/ver").as_dict(),
        WorkerSpec("jobd", [python, "-m", "harness.cli", "jobs", "daemon", "--interval", "60"],
                   startup_grace_s=15).as_dict(),
        WorkerSpec("automations", [python, "-m", "harness.automations", "daemon",
                                    "--interval", "5", "--state-dir", root],
                   critical=False, startup_grace_s=15).as_dict(),
        WorkerSpec("bridge", [python, "-m", "harness.cli", "browser-bridge", "--port", "8677"],
                   critical=False, probe_url="http://127.0.0.1:8677/health").as_dict(),
    ]
    for n, argv in enumerate(_legacy_slack_argv(root)):
        dog = "dog%d" % (n + 1)
        try:
            dog = argv[argv.index("--name") + 1]
        except (ValueError, IndexError):
            pass
        dog = dog.lower()
        workers.append(WorkerSpec(
            "slack-" + dog, [python, "-m", "harness.cli"] + argv,
            adopt_heartbeat="slack:" + dog, startup_grace_s=20).as_dict())
    return {
        "schema": SCHEMA, "state_dir": root, "poll_interval_s": 5,
        "heartbeat_ttl_s": 20, "alert_interval_s": 30,
        "workers": workers,
    }


def save_config(value: dict, path: str | None = None):
    specs = [WorkerSpec.from_dict(item).as_dict() for item in value.get("workers", [])]
    clean = dict(value)
    clean["schema"] = SCHEMA
    clean["workers"] = specs
    _atomic_json(path or config_path(value.get("state_dir")), clean)


def load_config(path: str | None = None, *, python: str | None = None) -> dict:
    path = path or config_path()
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
    except FileNotFoundError:
        value = default_config(os.path.dirname(path))
    if not isinstance(value, dict) or int(value.get("schema", 0)) != SCHEMA:
        raise ValueError("unsupported supervisor config schema")
    value["workers"] = [WorkerSpec.from_dict(item).as_dict()
                        for item in value.get("workers", [])]
    # A dog can opt into Slack after supervisor.json was first created. Discover only those
    # generated launchers on every supervisor start, preserving every existing worker setting.
    # This also lets the supervisor adopt pre-supervisor listeners during an upgrade.
    root = state_dir(value.get("state_dir") or os.path.dirname(path))
    known = {item["name"] for item in value["workers"]}
    generated = default_config(root, python or sys.executable)
    for item in generated["workers"]:
        if item["name"].startswith("slack-") and item["name"] not in known:
            value["workers"].append(item)
            known.add(item["name"])
    return value


class InstanceLock:
    """OS-released single-supervisor guard; a stale file is harmless."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.file = open(path, "a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"\0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            self.file.close()
            self.file = None
            raise RuntimeError("Collie supervisor is already running") from exc

    def close(self):
        f, self.file = self.file, None
        if f is None:
            return
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


def _probe(spec: WorkerSpec) -> bool:
    if not spec.probe_url:
        return True
    try:
        with urllib.request.urlopen(spec.probe_url, timeout=1.5) as response:
            raw = response.read(65536)
        if response.status != 200:
            return False
        if not spec.probe_json_key:
            return True
        value = json.loads(raw or b"{}")
        return bool(isinstance(value, dict) and value.get(spec.probe_json_key))
    except Exception:
        return False


class WorkerRuntime:
    """One desired child with exponential backoff and a rapid-failure circuit breaker."""

    def __init__(self, spec: WorkerSpec, store: OpsStore, root: str, *,
                 popen=subprocess.Popen, probe=_probe, clock=time.time):
        self.spec, self.store, self.root = spec, store, root
        self._popen, self._probe, self._clock = popen, probe, clock
        self.process = None
        self.external = False
        self.started_at = 0.0
        self.next_start_at = 0.0
        self.restart_count = 0
        self.consecutive_failures = 0
        self.last_exit = None
        self.last_error = ""
        self.unhealthy_since = 0.0
        self.circuit_until = 0.0
        self.heartbeat_ttl = 20.0
        self.log = RotatingLog(os.path.join(root, "logs", spec.name + ".log"))
        self.reader = None

    def _argv(self) -> list[str]:
        return [part.replace("{python}", sys.executable).replace("{state}", self.root)
                for part in self.spec.argv]

    def _read_output(self, stream):
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                self.log.write(line.rstrip("\r\n"))
        except Exception as exc:
            self.log.write("[supervisor] log reader stopped: %s" % exc)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _beat(self, state: str, now: float, **detail):
        detail.update({"restarts": self.restart_count, "last_exit": self.last_exit,
                       "next_start_at": self.next_start_at})
        self.store.beat("worker:" + self.spec.name, state, detail,
                        pid=getattr(self.process, "pid", 0), ttl=self.heartbeat_ttl, now=now)

    def _external_status(self, now: float) -> dict | None:
        if self.spec.probe_url and self._probe(self.spec):
            return {"adopted_via": "health_probe"}
        if not self.spec.adopt_heartbeat:
            return None
        row = self.store.heartbeats(now=now).get(self.spec.adopt_heartbeat) or {}
        if not row.get("fresh") or row.get("state") in (
                "dead", "failed", "stopped", "shutdown_timeout"):
            return None
        return {
            "adopted_via": "heartbeat",
            "adopted_heartbeat": self.spec.adopt_heartbeat,
            "external_state": row.get("state", "unknown"),
            "external_pid": row.get("pid", 0),
        }

    def spawn(self, now: float | None = None):
        now = float(self._clock() if now is None else now)
        env = os.environ.copy()
        env.update(self.spec.env)
        env["COLLIE_SUPERVISED"] = "1"
        env["COLLIE_STATE_DIR"] = self.root
        env["COLLIE_OPS_DB"] = self.store.path
        env["PYTHONUNBUFFERED"] = "1"
        kwargs = dict(cwd=self.spec.cwd or None, env=env, stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                      encoding="utf-8", errors="replace", bufsize=1)
        kwargs.update(plat.no_window_kwargs())
        try:
            self.process = self._popen(self._argv(), **kwargs)
            self.started_at = now
            self.unhealthy_since = 0.0
            stream = getattr(self.process, "stdout", None)
            if stream is not None and hasattr(stream, "readline"):
                self.reader = threading.Thread(target=self._read_output, args=(stream,),
                                               name="log-" + self.spec.name, daemon=True)
                self.reader.start()
            self.log.write("[supervisor] started pid %s" % getattr(self.process, "pid", "?"))
            self._beat("starting", now)
            return True
        except Exception as exc:
            self.process = None
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self._schedule_restart(now, rapid=True)
            self._beat("failed", now, error=self.last_error)
            return False

    def _schedule_restart(self, now: float, *, rapid: bool):
        self.restart_count += 1
        self.consecutive_failures = self.consecutive_failures + 1 if rapid else 0
        if self.consecutive_failures >= max(1, self.spec.max_rapid_failures):
            self.circuit_until = now + min(900.0, self.spec.max_backoff_s * 2)
            self.next_start_at = self.circuit_until
            return
        self.next_start_at = now + min(self.spec.max_backoff_s,
                                       2 ** min(self.consecutive_failures, 8))

    def step(self, now: float | None = None) -> str:
        now = float(self._clock() if now is None else now)
        if not self.spec.enabled:
            self._beat("disabled", now)
            return "disabled"
        if self.process is None:
            # Adopt an already-running legacy instance by its health probe or durable heartbeat. If
            # it later disappears, this supervisor becomes the owner and starts the worker.
            external = self._external_status(now)
            if external:
                self.external = True
                self._beat("external", now, **external)
                return "external"
            self.external = False
            if now < self.next_start_at:
                state = "circuit_open" if now < self.circuit_until else "backoff"
                self._beat(state, now, error=self.last_error)
                return state
            return "starting" if self.spawn(now) else "failed"

        code = self.process.poll()
        if code is not None:
            uptime = max(0.0, now - self.started_at)
            self.last_exit = int(code)
            self.last_error = "exited %s after %.1fs" % (code, uptime)
            self.log.write("[supervisor] %s" % self.last_error)
            self.process = None
            self._schedule_restart(now, rapid=uptime < self.spec.stable_s)
            state = "circuit_open" if now < self.circuit_until else "backoff"
            self._beat(state, now, error=self.last_error)
            return state

        uptime = max(0.0, now - self.started_at)
        if uptime >= self.spec.stable_s:
            self.consecutive_failures = 0
            self.circuit_until = 0.0
        healthy = self._probe(self.spec)
        if healthy:
            self.unhealthy_since = 0.0
            self._beat("running", now, uptime_s=uptime)
            return "running"
        if not self.unhealthy_since:
            self.unhealthy_since = now
        # A live but unresponsive process is as unavailable as a dead one. Give startup a generous
        # grace, then terminate the tree and let normal backoff/restart policy take over.
        if now - self.unhealthy_since >= self.spec.startup_grace_s:
            self.last_error = "health probe failed for %.1fs" % (now - self.unhealthy_since)
            self.stop(grace_s=2)
            self._schedule_restart(now, rapid=True)
            self._beat("backoff", now, error=self.last_error)
            return "backoff"
        self._beat("starting" if uptime < self.spec.startup_grace_s else "unhealthy", now,
                   probe_failed_s=now - self.unhealthy_since)
        return "unhealthy"

    def wake(self, now: float | None = None):
        """A sleep/resume boundary invalidates sockets; probe immediately instead of waiting."""
        now = float(self._clock() if now is None else now)
        self.unhealthy_since = now if self.process is not None and self.spec.probe_url else 0.0
        if self.process is None:
            self.next_start_at = min(self.next_start_at, now)

    def stop(self, grace_s: float = 8):
        proc, self.process = self.process, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=max(0.1, float(grace_s)))
        except Exception:
            plat.kill_tree(proc)
        self.log.write("[supervisor] stopped")

    def close(self):
        self.stop()
        self.log.close()


def startup_self_check(config: dict, store: OpsStore) -> dict:
    errors, warnings = [], []
    if not os.path.exists(sys.executable):
        errors.append("python executable is missing")
    try:
        specs = [WorkerSpec.from_dict(item) for item in config.get("workers", [])]
    except ValueError as exc:
        specs = []
        errors.append(str(exc))
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        errors.append("worker names are not unique")
    credentials = credential_health()
    for row in credentials:
        if row["state"] in ("expired", "expiring"):
            warnings.append("%s is %s" % (row["name"], row["state"]))
    try:
        store.beat("startup-self-check", "failed" if errors else "ok",
                   {"errors": errors, "warnings": warnings}, ttl=300)
    except Exception as exc:
        errors.append("ops database unavailable: %s" % exc)
    try:
        from . import update
        hook = getattr(update, "record_startup_health", None)
        if hook:
            hook(not errors, "; ".join(errors or warnings))
    except Exception as exc:
        warnings.append("update health hook failed: %s" % exc)
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "workers": names, "credentials": credentials}


class Supervisor:
    def __init__(self, config: dict, *, store: OpsStore | None = None,
                 runtime_factory=WorkerRuntime, clock=time.time, monotonic=time.monotonic):
        self.config = config
        self.root = state_dir(config.get("state_dir"))
        self.store = store or OpsStore(os.path.join(self.root, "ops.db"))
        self._own_store = store is None
        self.clock, self.monotonic = clock, monotonic
        self.poll_s = max(0.2, float(config.get("poll_interval_s", 5)))
        self.alert_s = max(5.0, float(config.get("alert_interval_s", 30)))
        self.workers = [runtime_factory(WorkerSpec.from_dict(item), self.store, self.root)
                        for item in config.get("workers", []) if item.get("enabled", True)]
        for worker in self.workers:
            if hasattr(worker, "heartbeat_ttl"):
                worker.heartbeat_ttl = max(5.0, float(config.get("heartbeat_ttl_s", 20)))
        self.stop_event = threading.Event()
        self._last_mono = self.monotonic()
        self._last_alert = 0.0
        self.lock = None

    def request_stop(self, *_):
        self.stop_event.set()

    def step(self, now: float | None = None, mono: float | None = None) -> dict:
        now = float(self.clock() if now is None else now)
        mono = float(self.monotonic() if mono is None else mono)
        gap = max(0.0, mono - self._last_mono)
        self._last_mono = mono
        if gap > max(30.0, self.poll_s * 4):
            for worker in self.workers:
                worker.wake(now)
            self.store.beat("power", "resumed", {"sleep_gap_s": gap}, ttl=120, now=now)
        states = {worker.spec.name: worker.step(now) for worker in self.workers}
        self.store.beat("supervisor", "running", {"workers": states, "sleep_gap_s": gap},
                        ttl=max(10.0, self.poll_s * 3), now=now)
        if now - self._last_alert >= self.alert_s:
            report = aggregate_health(
                self.store, desired_workers=list(states), state_dir=self.root,
                now=now, probe_services=False)
            enqueue_health_alerts(self.store, report, now=now)
            self._last_alert = now
        return states

    def run(self) -> int:
        stop_path = os.path.join(self.root, "supervisor.stop")
        pid_path = os.path.join(self.root, "supervisor.pid.json")
        try:
            os.remove(stop_path)  # a stale uninstall request must not poison a later reinstall
        except OSError:
            pass
        self.lock = InstanceLock(os.path.join(self.root, "supervisor.lock"))
        _atomic_json(pid_path, {"pid": os.getpid(), "started_at": time.time()})
        check = startup_self_check(self.config, self.store)
        if not check["ok"]:
            self.store.beat("supervisor", "failed", {"errors": check["errors"]}, ttl=120)
            self.lock.close()
            self.lock = None
            try:
                os.remove(pid_path)
            except OSError:
                pass
            return 2
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    signal.signal(sig, self.request_stop)
                except (ValueError, OSError):
                    pass
        try:
            while not self.stop_event.is_set() and not os.path.exists(stop_path):
                self.step()
                self.stop_event.wait(self.poll_s)
            return 0
        finally:
            for worker in reversed(self.workers):
                worker.close()
            self.store.beat("supervisor", "stopped", {}, ttl=5)
            if self.lock:
                self.lock.close()
                self.lock = None
            try:
                os.remove(pid_path)
            except OSError:
                pass
            if self._own_store:
                self.store.close()


def _current_sid(runner=subprocess.run) -> str:
    try:
        result = runner(["whoami.exe", "/user", "/fo", "csv", "/nh"],
                        capture_output=True, text=True, timeout=10)
        row = next(csv.reader(io.StringIO(result.stdout or "")))
        if len(row) >= 2 and row[1].startswith("S-"):
            return row[1]
    except Exception:
        pass
    return os.environ.get("USERDOMAIN", ".") + "\\" + os.environ.get("USERNAME", "")


def task_xml(pythonw: str, config: str, user_id: str, *, include_boot: bool = True) -> str:
    esc = lambda value: html.escape(str(value), quote=False)
    boot = ("\n    <BootTrigger><Enabled>true</Enabled></BootTrigger>"
            if include_boot else "")
    # InteractiveToken is deliberate. OAuth files, DPAPI and desktop/browser control belong to the
    # user, so an elevated SYSTEM boot service would be a different and mostly broken Collie.
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Supervises Collie web, jobs, browser bridge and Slack workers.</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger>{boot}
  </Triggers>
  <Principals><Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowHardTerminate>true</AllowHardTerminate>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{python}</Command><Arguments>-m harness.supervisor run --config &quot;{config}&quot;</Arguments></Exec></Actions>
</Task>
""".format(user=esc(user_id), boot=boot, python=esc(pythonw), config=esc(config))


def _fallback_paths(root: str) -> tuple[str, str]:
    startup = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                           "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    return os.path.join(root, "supervisor-boot.pyw"), os.path.join(startup, "collie-supervisor.vbs")


def _write_fallback(root: str, pythonw: str, cfg: str) -> tuple[str, str]:
    boot, vbs = _fallback_paths(root)
    os.makedirs(os.path.dirname(vbs), exist_ok=True)
    with open(boot, "w", encoding="utf-8") as f:
        f.write("# generated by harness.supervisor; starts the crash-restarting supervisor.\n"
                "from harness.supervisor import main\n"
                "raise SystemExit(main(%r))\n" % ["run", "--config", cfg])
    with open(vbs, "w", encoding="utf-8") as f:
        f.write("' Collie supervisor fallback (Task Scheduler registration was unavailable).\n"
                "q = Chr(34)\n"
                'CreateObject("WScript.Shell").Run q & "%s" & q & " " & q & "%s" & q, 0, False\n'
                % (pythonw, boot))
    return boot, vbs


def install_windows(*, root: str | None = None, config: str | None = None,
                    pythonw: str | None = None, include_boot: bool = True,
                    disabled_workers: list[str] | None = None,
                    runner=subprocess.run) -> dict:
    if not plat.is_windows():
        return {"ok": False, "mode": "unsupported", "error": "Windows Task Scheduler only"}
    root = state_dir(root)
    cfg = os.path.abspath(config or config_path(root))
    pythonw = os.path.abspath(pythonw or sys.executable)
    if not os.path.exists(cfg):
        generated = default_config(root, pythonw)
        disabled = {str(name) for name in (disabled_workers or ())}
        for worker in generated["workers"]:
            if worker["name"] in disabled:
                worker["enabled"] = False
        save_config(generated, cfg)
    else:
        # Persist schema-compatible inferred fields and any newly discovered per-dog launchers.
        # Explicit user settings on existing workers remain untouched.
        save_config(load_config(cfg, python=pythonw), cfg)
    sid = _current_sid(runner)
    xml_path = os.path.join(root, "supervisor-task.xml")
    # Task Scheduler's XML reader accepts UTF-16 with the matching declaration.
    def write_xml(with_boot):
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(task_xml(pythonw, cfg, sid, include_boot=with_boot))
    write_xml(include_boot)
    try:
        result = runner(["schtasks.exe", "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"],
                        capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return {"ok": True, "mode": "scheduled_task", "task": TASK_NAME,
                    "config": cfg, "boot": bool(include_boot),
                    "note": "boot runs when the user's interactive token is available"}
        error = (result.stderr or result.stdout or "Task Scheduler refused registration").strip()
        # Per-user installers often cannot register a machine boot trigger. A logon-only task still
        # gives correct access to the user's OAuth/desktop session and needs no elevation, so retry
        # that exact least-privilege form before degrading to the Startup folder.
        if include_boot:
            write_xml(False)
            retry = runner(
                ["schtasks.exe", "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"],
                capture_output=True, text=True, timeout=30)
            if retry.returncode == 0:
                return {"ok": True, "mode": "scheduled_task", "task": TASK_NAME,
                        "config": cfg, "boot": False, "degraded": True,
                        "note": "per-user logon trigger installed; boot trigger required elevation"}
            error += "; logon-only retry: " + (
                retry.stderr or retry.stdout or "registration refused").strip()
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    boot, vbs = _write_fallback(root, pythonw, cfg)
    return {"ok": True, "mode": "startup_fallback", "task": TASK_NAME,
            "config": cfg, "launcher": vbs, "boot_script": boot,
            "degraded": True, "error": error[:500]}


def _request_supervisor_stop(root: str, *, timeout_s: float = 12.0):
    stop_path = os.path.join(root, "supervisor.stop")
    pid_path = os.path.join(root, "supervisor.pid.json")
    os.makedirs(root, exist_ok=True)
    with open(stop_path, "w", encoding="ascii") as f:
        f.write("stop\n")
    deadline = time.time() + max(0.0, float(timeout_s))
    while os.path.exists(pid_path) and time.time() < deadline:
        time.sleep(0.2)
    return not os.path.exists(pid_path)


def uninstall_windows(*, root: str | None = None, runner=subprocess.run,
                      stop_timeout_s: float = 12.0) -> dict:
    root = state_dir(root)
    removed, errors, warnings = [], [], []
    if plat.is_windows():
        graceful = _request_supervisor_stop(root, timeout_s=stop_timeout_s)
        try:
            # /End is a bounded fallback after the cooperative stop window. If the process already
            # exited, Task Scheduler simply reports that the task is not running.
            runner(["schtasks.exe", "/End", "/TN", TASK_NAME],
                   capture_output=True, text=True, timeout=20)
            result = runner(["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"],
                            capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                removed.append(TASK_NAME)
            elif "cannot find" not in (result.stderr or result.stdout or "").lower():
                errors.append((result.stderr or result.stdout or "delete failed").strip()[:500])
        except Exception as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))
        if not graceful:
            warnings.append(
                "supervisor did not acknowledge stop before the forced Task Scheduler end")
    for path in _fallback_paths(root):
        try:
            if os.path.exists(path):
                os.remove(path)
                removed.append(path)
        except OSError as exc:
            errors.append("%s: %s" % (path, exc))
    return {"ok": not errors, "removed": removed, "errors": errors, "warnings": warnings}


def query_windows(*, root: str | None = None, runner=subprocess.run) -> dict:
    root = state_dir(root)
    boot, vbs = _fallback_paths(root)
    scheduled = False
    detail = ""
    if plat.is_windows():
        try:
            result = runner(["schtasks.exe", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
                            capture_output=True, text=True, timeout=20)
            scheduled = result.returncode == 0
            detail = (result.stdout if scheduled else result.stderr or result.stdout or "")[:4000]
        except Exception as exc:
            detail = "%s: %s" % (type(exc).__name__, exc)
    fallback = os.path.exists(boot) and os.path.exists(vbs)
    cfg = config_path(root)
    return {"installed": scheduled or fallback,
            "mode": "scheduled_task" if scheduled else ("startup_fallback" if fallback else "none"),
            "scheduled": scheduled, "fallback": fallback, "config": cfg,
            "config_exists": os.path.exists(cfg), "detail": detail}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.supervisor")
    sub = parser.add_subparsers(dest="action", required=True)
    runp = sub.add_parser("run")
    runp.add_argument("--config", default=config_path())
    ins = sub.add_parser("install")
    ins.add_argument("--state-dir", default=None)
    ins.add_argument("--no-boot", action="store_true")
    ins.add_argument("--disable-worker", action="append", default=[],
                     choices=["web", "jobd", "automations", "bridge"],
                     help="disable a generated default worker (only affects first install)")
    un = sub.add_parser("uninstall")
    un.add_argument("--state-dir", default=None)
    st = sub.add_parser("status")
    st.add_argument("--state-dir", default=None)
    args = parser.parse_args(argv)
    if args.action == "run":
        return Supervisor(load_config(args.config)).run()
    if args.action == "install":
        result = install_windows(root=args.state_dir, include_boot=not args.no_boot,
                                 disabled_workers=args.disable_worker)
    elif args.action == "uninstall":
        result = uninstall_windows(root=args.state_dir)
    else:
        result = query_windows(root=args.state_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", result.get("installed", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
