"""Killable process boundary for durable Mission code slices.

The Mission driver deliberately runs one bounded slice at a time.  The transcript
and verification receipts live in ``sessions``; this module only supplies the OS
process boundary which Python threads cannot provide.  If the outer watchdog or a
user cancellation fires, ``cancel_current`` kills the whole child tree before a
new Mission owner can touch the workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

# Script-mode workers use ``python -I <this trusted file>`` so an untrusted
# workspace cannot run sitecustomize.py before the start gate.  Restore only the
# package context needed by our later relative imports.
if __name__ == "__main__" and not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "harness"


def _private_json(path: str, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())


def _write_result(path: str, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())


def _is_windows() -> bool:
    return os.name == "nt"


def _wait_start_gate(path: str, token: str, timeout_s: float = 30.0) -> None:
    """Do not let a new worker touch its workspace before ownership is durable.

    A file gate is intentionally simpler than inheritable Windows handles.  The
    directory is owner-private, the payload is atomically published, and deleting
    the temporary directory revokes a child whose parent failed during setup.
    """
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    parent = os.path.dirname(path)
    while True:
        try:
            with open(path, encoding="utf-8") as fh:
                value = json.load(fh)
            candidate = str(value.get("token") or "") if isinstance(value, dict) else ""
            if not candidate or not secrets.compare_digest(candidate, str(token or "")):
                raise RuntimeError("Mission code worker start gate was not authentic")
            return
        except FileNotFoundError:
            if not os.path.isdir(parent):
                raise RuntimeError("Mission code worker start authorization was revoked")
            if time.monotonic() >= deadline:
                raise TimeoutError("Mission code worker start authorization timed out")
            time.sleep(.01)
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("Mission code worker start gate was unreadable") from exc


def _publish_start_gate(path: str, token: str) -> None:
    temp = path + "." + secrets.token_hex(8) + ".tmp"
    _private_json(temp, {"token": token})
    os.replace(temp, path)


def _execute_request(request: dict) -> dict:
    """Child body.  Authority is reduced to the exact parent-bound workspace."""
    workspace = os.path.realpath(os.path.abspath(str(request.get("workspace") or "")))
    if not workspace or not os.path.isdir(workspace):
        raise ValueError("Mission code workspace does not exist")
    # _live_code retains its positive-root check as defense in depth.  The
    # request file is owner-private and was produced only after TaskTree checked
    # this same canonical workspace.
    os.environ["COLLIE_MISSION_CODE_ROOTS"] = workspace
    session_dir = str(request.get("session_dir") or "")
    if session_dir:
        os.environ["COLLIE_SESSIONS_DIR"] = session_dir
    from .primitives import _live_code
    return _live_code(
        str(request.get("goal") or ""), workspace,
        mission_id=str(request.get("mission_id") or ""),
        execution_profile=request.get("execution_profile") or {},
        verify_command=str(request.get("verify_command") or ""),
        session_id=str(request.get("session_id") or ""),
        baseline_tree_digest=str(request.get("baseline_tree_digest") or ""),
        expected_tree_digest=str(request.get("expected_tree_digest") or ""),
        slice_turns=request.get("slice_turns"),
        verify_timeout_seconds=request.get("verify_timeout_seconds"),
        max_session_storage_bytes=request.get("max_session_storage_bytes"),
        max_model_calls=request.get("max_model_calls"),
        mission_store_path=str(request.get("mission_store_path") or ""),
        mission_run_token=str(request.get("mission_run_token") or ""),
    )


class CodeSliceProcessRunner:
    """Run one native Collie code slice in a process that can be terminated."""

    def __init__(self, popen=None, session_dir=None, worker_dir=None):
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._starting = {}
        self._procs = {}
        self._jobs = {}
        self._owners = {}
        self._popen = popen or subprocess.Popen
        self._production_popen = popen is None
        self.session_dir = os.path.realpath(os.path.abspath(
            session_dir or os.environ.get("COLLIE_SESSIONS_DIR") or
            os.path.join(os.path.expanduser("~/.collie"), "mission-code-sessions")))
        self.worker_dir = os.path.realpath(os.path.abspath(
            worker_dir or os.path.join(os.path.dirname(self.session_dir),
                                       "mission-code-workers")))

    def _receipt_path(self, mission_id):
        digest = hashlib.sha256(str(mission_id or "").encode(
            "utf-8", "replace")).hexdigest()[:32]
        return os.path.join(self.worker_dir, digest + ".json")

    def job_name_for(self, mission_id, generation=""):
        # A per-run generation prevents a stale receipt from opening and killing
        # a later Job that happens to reuse the same Mission ID.
        material = (self.worker_dir + "\0" + str(mission_id or "") +
                    "\0" + str(generation or ""))
        digest = hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:32]
        return "Local\\CollieMissionCode-" + digest

    def _write_worker_receipt(self, mission_id, value):
        os.makedirs(self.worker_dir, exist_ok=True)
        path = self._receipt_path(mission_id)
        temp = path + "." + secrets.token_hex(8) + ".tmp"
        _private_json(temp, value)
        os.replace(temp, path)
        return path

    @staticmethod
    def _process_identity(pid):
        """Best available process-birth identity, never merely a reusable PID."""
        if _is_windows():
            return ""
        try:
            stat_path = "/proc/%d/stat" % int(pid)
            if os.path.exists(stat_path):
                with open(stat_path, encoding="utf-8") as fh:
                    stat = fh.read()
                tail = stat[stat.rfind(")") + 2:].split()
                start_ticks = tail[19]  # proc(5) field 22; tail begins at field 3.
                boot_id = ""
                try:
                    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
                        boot_id = fh.read().strip()
                except OSError:
                    pass
                return "linux:%s:%s" % (boot_id, start_ticks)
            value = subprocess.run(
                ["ps", "-p", str(int(pid)), "-o", "lstart="],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=3, check=False).stdout.strip()
            return "ps:" + value if value else ""
        except Exception:
            return ""

    @classmethod
    def _process_matches(cls, pid, marker, identity=""):
        """Reject PID reuse before a cross-process POSIX cancellation."""
        if _is_windows():
            return True
        try:
            if identity and cls._process_identity(pid) != str(identity):
                return False
            proc_path = "/proc/%d/cmdline" % int(pid)
            if os.path.exists(proc_path):
                with open(proc_path, "rb") as fh:
                    command = fh.read().decode("utf-8", "replace")
            else:
                command = subprocess.run(
                    ["ps", "-p", str(int(pid)), "-o", "command="],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, timeout=3, check=False).stdout
            normalized = command.replace("\\", "/")
            owned_command = ("harness.codeworker" in command or
                             "/harness/codeworker.py" in normalized)
            return owned_command and str(marker or "") in command
        except Exception:
            return False

    @staticmethod
    def _posix_group_extinct(pgid):
        """Return True only when the kernel says this process group is absent."""
        try:
            os.killpg(int(pgid), 0)
            return False
        except ProcessLookupError:
            return True
        except (OSError, ValueError, TypeError):
            return False

    @classmethod
    def _wait_posix_group_extinct(cls, pgid, timeout_s=5.0):
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            if cls._posix_group_extinct(pgid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(.01)

    @staticmethod
    def _remove_matching_receipt(path, token):
        """Remove only the receipt generation this caller actually owns."""
        try:
            with open(path, encoding="utf-8") as fh:
                current = json.load(fh)
            if not isinstance(current, dict) or not secrets.compare_digest(
                    str(current.get("token") or ""), str(token or "")):
                return False
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except (OSError, ValueError, TypeError):
            return False

    def cancel_persisted(self, mission_id=None):
        """Cancel trees owned by another Collie process from durable receipts."""
        try:
            names = ([os.path.basename(self._receipt_path(mission_id))] if mission_id else
                     [name for name in os.listdir(self.worker_dir) if name.endswith(".json")])
        except OSError:
            return False
        cancelled = False
        from . import plat
        for name in names:
            path = os.path.join(self.worker_dir, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    row = json.load(fh)
            except Exception:
                continue
            if mission_id is not None and str(row.get("mission_id") or "") != str(mission_id):
                continue
            token = str(row.get("token") or "")
            if _is_windows():
                confirmed = getattr(plat, "terminate_named_job_and_wait", None)
                if callable(confirmed) and confirmed(str(row.get("job_name") or "")):
                    if self._remove_matching_receipt(path, token):
                        cancelled = True
                elif not callable(confirmed) and plat.terminate_named_job(
                        str(row.get("job_name") or "")):
                    # Older platform layers can prove delivery but not Job
                    # extinction.  Let the owning process retire its exact-token
                    # receipt after its Job accounting reaches zero.
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline:
                        try:
                            with open(path, encoding="utf-8") as fh:
                                current = json.load(fh)
                            if str(current.get("token") or "") != token:
                                cancelled = True
                                break
                        except FileNotFoundError:
                            cancelled = True
                            break
                        except (OSError, ValueError, TypeError):
                            break
                        time.sleep(.01)
                continue
            pid = int(row.get("pid") or 0)
            pgid = int(row.get("pgid") or 0)
            marker = str(row.get("request_path") or "")
            identity = str(row.get("process_identity") or "")
            if int(row.get("version") or 0) >= 2 and not identity:
                continue
            if pid <= 1 or not self._process_matches(pid, marker, identity):
                # A dead/reused leader is not authority to signal its old PGID.
                # Only absence of the recorded group proves this receipt stale.
                if pgid > 1 and self._posix_group_extinct(pgid):
                    if self._remove_matching_receipt(path, token):
                        cancelled = True
                continue
            try:
                group = os.getpgid(pid)
                if group != pid or (pgid > 1 and group != pgid):
                    continue
                # Recheck the birth identity immediately before signalling.  If
                # the leader changed, retain the fence rather than risk PID reuse.
                if not self._process_matches(pid, marker, identity):
                    continue
                os.killpg(group, signal.SIGKILL)
                if self._wait_posix_group_extinct(group) and \
                        self._remove_matching_receipt(path, token):
                    cancelled = True
            except (OSError, ProcessLookupError):
                pass
        return cancelled

    def has_owned_worker(self, mission_id=None):
        """Whether a local process or durable receipt still claims this Mission."""
        key = str(mission_id or "")
        with self._lock:
            if mission_id is None:
                if self._starting:
                    return True
            elif key in self._starting:
                return True
            if mission_id is None:
                if any(proc is not None and proc.poll() is None
                       for proc in self._procs.values()):
                    return True
            else:
                proc = self._procs.get(key)
                if proc is not None and proc.poll() is None:
                    return True
        try:
            if mission_id is not None:
                names = [os.path.basename(self._receipt_path(mission_id))]
            else:
                names = [name for name in os.listdir(self.worker_dir)
                         if name.endswith(".json")]
            for name in names:
                path = os.path.join(self.worker_dir, name)
                if not os.path.isfile(path):
                    continue
                with open(path, encoding="utf-8") as fh:
                    row = json.load(fh)
                if mission_id is None or str(row.get("mission_id") or "") == key:
                    return True
        except (OSError, ValueError, TypeError):
            # An unreadable receipt is not proof of absence.
            return True
        return False

    def _terminate_local(self, proc, job, owner):
        """Terminate one locally-owned tree and prove extinction."""
        from . import plat
        confirmed = False
        if job is not None:
            try:
                terminate = getattr(job, "terminate_and_wait", None)
                confirmed = bool(terminate(timeout_s=5)) if callable(terminate) else False
            except Exception:
                confirmed = False
        elif not self._production_popen:
            if proc.poll() is None:
                plat.kill_tree(proc)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            confirmed = proc.poll() is not None
        elif not _is_windows():
            pgid = int((owner or {}).get("pgid") or 0)
            if proc.poll() is None:
                # An unreaped child PID cannot be reused.  Signal the dedicated
                # session before wait() gives that guarantee away.
                plat.kill_tree(proc)
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            # Once the leader is gone, absence is proof but a live PGID is not
            # authority to kill: it may be an orphan or a later reused group.
            confirmed = pgid > 1 and self._wait_posix_group_extinct(pgid)
        # A production Windows worker without a Job is never confirmable.
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return confirmed

    def cancel_current(self, mission_id=None, include_persisted=True):
        key = str(mission_id or "")
        # Registering startup intent precedes Popen.  A cancellation that lands
        # during Popen -> Job attach -> receipt publication marks that intent and
        # waits for the owner thread to kill the still-gated child.  It must never
        # report safe absence merely because the PID has not entered _procs yet.
        deadline = time.monotonic() + 5.0
        with self._condition:
            starts = (list(self._starting.values()) if mission_id is None else
                      ([self._starting[key]] if key in self._starting else []))
            for state in starts:
                state["cancel_requested"] = True
            while any(any(current is state for current in self._starting.values())
                      for state in starts):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            if mission_id is None:
                items = list(self._procs.items())
            else:
                proc = self._procs.get(key)
                items = [(key, proc)] if proc is not None else []
            jobs = {item_key: self._jobs.get(item_key) for item_key, _proc in items}
            owners = {item_key: self._owners.get(item_key) for item_key, _proc in items}
        confirmed_any = False
        for key, proc in items:
            job = jobs.get(key)
            owner = owners.get(key) or {}
            confirmed = self._terminate_local(proc, job, owner)
            if confirmed:
                path = str(owner.get("path") or self._receipt_path(key))
                token = str(owner.get("token") or "")
                if token and self._remove_matching_receipt(path, token):
                    confirmed_any = True
        # Web/API requests commonly live in a different process from jobd.
        # The named Job/process-group receipt reaches that owner too.
        persisted = self.cancel_persisted(mission_id) if include_persisted else False
        if persisted or confirmed_any:
            return not self.has_owned_worker(mission_id)
        # No startup/process/receipt is a confirmed-safe absence, not a failed kill.
        return not self.has_owned_worker(mission_id)

    cancel_pending = cancel_current

    def cancel_for(self, mission_id):
        """Return a Capability-compatible zero-argument scoped canceller."""
        return lambda: self.cancel_current(mission_id)

    def __call__(self, goal, *, workspace=None, mission_id=None,
                 execution_profile=None, verify_command="", max_wall_seconds=0,
                 session_id="", baseline_tree_digest="", slice_turns=None,
                  expected_tree_digest="",
                  verify_timeout_seconds=None, max_session_storage_bytes=None,
                  max_model_calls=None, mission_store_path="", mission_run_token=""):
        workspace = os.path.realpath(os.path.abspath(str(workspace or "")))
        if not os.path.isdir(workspace):
            raise ValueError("Mission code workspace does not exist")
        temp_root = tempfile.mkdtemp(prefix="collie-mission-code-")
        request_path = os.path.join(temp_root, "request.json")
        result_path = os.path.join(temp_root, "result.json")
        start_gate_path = os.path.join(temp_root, "start.json")
        start_token = secrets.token_hex(16)
        process_key = str(mission_id or "")
        _private_json(request_path, {
            "goal": str(goal or ""), "workspace": workspace,
            "mission_id": str(mission_id or ""),
            "execution_profile": dict(execution_profile or {}),
            "verify_command": str(verify_command or ""),
            "session_dir": self.session_dir,
            "session_id": str(session_id or ""),
            "baseline_tree_digest": str(baseline_tree_digest or ""),
            "expected_tree_digest": str(expected_tree_digest or ""),
            "slice_turns": slice_turns,
            "verify_timeout_seconds": verify_timeout_seconds,
            "max_session_storage_bytes": max_session_storage_bytes,
            "max_model_calls": max_model_calls,
            "mission_store_path": str(mission_store_path or ""),
            "mission_run_token": str(mission_run_token or ""),
        })
        from . import plat
        worker_receipt = ""
        receipt_token = secrets.token_hex(16)
        windows_job = None
        proc = None
        owner = {}
        start_state = None
        tree_confirmed = False
        try:
            allowed = {
                "APPDATA", "COMSPEC", "HOME", "HOMEDRIVE", "HOMEPATH", "LANG",
                "LC_ALL", "LC_CTYPE", "LOCALAPPDATA", "LOGNAME", "PATH", "PATHEXT",
                "SHELL", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "USER", "USERPROFILE",
                "WINDIR",
            }
            env = {name: value for name, value in os.environ.items()
                   if name.upper() in allowed}
            # In a source checkout the parent can import ``harness`` because the
            # repository root is on sys.path, but that path is not necessarily
            # exported in PYTHONPATH.  The child deliberately runs with cwd set
            # to the isolated workspace, so make the trusted Collie package root
            # explicit instead of relying on the user's project being installed.
            package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            inherited = str(env.get("PYTHONPATH") or "")
            env["PYTHONPATH"] = package_root + (os.pathsep + inherited if inherited else "")
            group_kwargs = plat.new_group_kwargs()
            if self._production_popen and not _is_windows() and not group_kwargs.get(
                    "start_new_session"):
                raise RuntimeError(
                    "Mission code worker requires an independent POSIX process group")
            with self._condition:
                if process_key in self._starting or process_key in self._procs:
                    raise RuntimeError("Mission already has an active code slice")
                start_state = {"cancel_requested": False}
                self._starting[process_key] = start_state
            worker_script = os.path.abspath(__file__)
            proc = self._popen(
                [sys.executable, "-I", worker_script, "_execute",
                 "--request", request_path, "--result", result_path,
                 "--start-gate", start_gate_path, "--start-token", start_token],
                cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **group_kwargs, **plat.no_window_kwargs())
            job_name = self.job_name_for(process_key, receipt_token)
            owned_pgid = 0
            process_identity = ""
            if self._production_popen:
                try:
                    windows_job = plat.attach_kill_on_close_job(proc, name=job_name)
                    if _is_windows() and windows_job is None:
                        raise RuntimeError("Windows Job Object was not created")
                    if not _is_windows():
                        owned_pgid = int(proc.pid)
                        if os.getpgid(int(proc.pid)) != owned_pgid:
                            raise RuntimeError("POSIX worker is not its process-group leader")
                        process_identity = self._process_identity(proc.pid)
                        if not process_identity:
                            raise RuntimeError("POSIX worker birth identity was unavailable")
                except Exception:
                    plat.kill_tree(proc)
                    raise RuntimeError(
                        "Mission code worker process-tree ownership could not be established")
            worker_receipt = self._write_worker_receipt(process_key, {
                "version": 2,
                "mission_id": process_key,
                "pid": int(proc.pid),
                "pgid": owned_pgid,
                "process_identity": process_identity,
                "job_name": job_name if _is_windows() else "",
                "request_path": request_path,
                "workspace_digest": hashlib.sha256(
                    workspace.encode("utf-8", "replace")).hexdigest(),
                "token": receipt_token,
            })
            owner = {"path": worker_receipt, "token": receipt_token,
                     "pgid": owned_pgid, "process_identity": process_identity}
            with self._condition:
                if start_state.get("cancel_requested"):
                    raise RuntimeError("Mission code worker cancelled during startup")
                self._procs[process_key] = proc
                self._jobs[process_key] = windows_job
                self._owners[process_key] = owner
                # Publish only after Job/PGID ownership and its durable receipt
                # both exist.  Holding the registry lock closes the final window
                # in which cancel_current could observe neither state.
                _publish_start_gate(start_gate_path, start_token)
                self._starting.pop(process_key, None)
                self._condition.notify_all()
            try:
                timeout = float(max_wall_seconds or 0)
                proc.wait(timeout=timeout if timeout > 0 else None)
            except subprocess.TimeoutExpired:
                raise TimeoutError("Mission code slice exceeded its process wall limit")
            try:
                with open(result_path, encoding="utf-8") as fh:
                    result = json.load(fh)
            except Exception:
                raise RuntimeError(
                    "Mission code worker exited %s without a durable result" % proc.returncode)
            if result.get("exception"):
                raise RuntimeError(str(result["exception"])[:1000])
            return result
        finally:
            unwinding = sys.exc_info()[0] is not None
            cleanup_error = None
            if proc is not None:
                try:
                    tree_confirmed = self._terminate_local(proc, windows_job, owner)
                except Exception as exc:
                    cleanup_error = exc
                    tree_confirmed = False
            if windows_job is not None:
                try:
                    windows_job.close()
                except Exception as exc:
                    cleanup_error = cleanup_error or exc
                    tree_confirmed = False
            if worker_receipt and tree_confirmed:
                if not self._remove_matching_receipt(worker_receipt, receipt_token):
                    cleanup_error = cleanup_error or RuntimeError(
                        "Mission code worker receipt ownership changed during cleanup")
            with self._condition:
                if self._procs.get(process_key) is proc:
                    self._procs.pop(process_key, None)
                    self._jobs.pop(process_key, None)
                    self._owners.pop(process_key, None)
                if self._starting.get(process_key) is start_state:
                    self._starting.pop(process_key, None)
                self._condition.notify_all()
            shutil.rmtree(temp_root, ignore_errors=True)
            if worker_receipt and not tree_confirmed and not unwinding:
                raise RuntimeError(
                    "Mission code worker process-tree extinction could not be confirmed")
            if cleanup_error is not None and not unwinding:
                raise RuntimeError(
                    "Mission code worker cleanup could not be confirmed") from cleanup_error


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.codeworker")
    parser.add_argument("action", choices=["_execute"])
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--start-gate", required=True)
    parser.add_argument("--start-token", required=True)
    args = parser.parse_args(argv)
    try:
        _wait_start_gate(args.start_gate, args.start_token)
        with open(args.request, encoding="utf-8") as fh:
            value = _execute_request(json.load(fh))
    except Exception as exc:
        value = {"exception_type": type(exc).__name__,
                 "exception": "%s: %s" % (type(exc).__name__, exc)}
    _write_result(args.result, value)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the parent runner
    raise SystemExit(main())
