"""Repository check discovery and durable, structured execution evidence."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time


_KIND_ORDER = {"test": 0, "typecheck": 1, "lint": 2, "build": 3}
_SNAPSHOT_FILE_CAP = 20_000
_SNAPSHOT_BYTE_CAP = 64 * 1024 * 1024
_GENERATED_CACHE_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
})
_VERIFICATION_OUTPUT_CHARS = 4_000
# The stop predicate belongs to one request, so polling it is cheap and short.
# Anything longer would let a Stop press sit behind a check that runs for minutes.
_CANCEL_POLL_SECONDS = 0.05
# The durable boundary a host check arms before it can touch the workspace.
CHECK_BOUNDARY_TOOL = "verification"


class _VerificationCancelled(Exception):
    """Internal signal: this exact request was stopped before its command ran."""


class _TailCapture:
    """Thread-safe bounded tail for a verifier's arbitrarily large stdout."""

    def __init__(self, limit: int = _VERIFICATION_OUTPUT_CHARS):
        self.limit = max(1, int(limit))
        self._value = ""
        self._lock = threading.Lock()

    def append(self, value) -> None:
        if not value:
            return
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        with self._lock:
            self._value = (self._value + str(value))[-self.limit:]

    def text(self) -> str:
        with self._lock:
            return self._value


def _bounded_stdout_reader(stream, tail: _TailCapture) -> None:
    """Drain a verifier pipe without ever allocating one unbounded line."""
    try:
        while True:
            chunk = stream.readline(65_536)
            if chunk == "":
                return
            tail.append(chunk)
    except Exception:
        # Cancellation closes the pipe underneath this daemon thread.  Process
        # status and the ownership proof remain the completion facts.
        return


def _is_untracked_generated_cache(rel: str) -> bool:
    """Transient Python verifier caches are not project/source ownership.

    These paths are ignored only as untracked/filesystem artifacts. A tracked
    cache file remains represented by Git's diff, so a repository that
    intentionally versions one still gets exact freshness semantics.
    """
    normalized = str(rel or "").replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part]
    if any(part in _GENERATED_CACHE_DIRS for part in parts):
        return True
    name = parts[-1] if parts else ""
    return (name == ".coverage" or name.startswith(".coverage.") or
            name.endswith((".pyc", ".pyo")))


# The repository command must not receive even one instruction byte until its
# process tree has a kernel owner and the caller has registered its cancellation
# handle.  This trusted, isolated Python gate blocks on stdin; only the parent
# can release it, after assigning the gate to a POSIX process group or Windows
# Job Object.  The real command and every descendant inherit that owner.
_VERIFICATION_START_GATE_SCRIPT = r"""
import json
import os
import subprocess
import sys

def reject_constant(value):
    raise ValueError("non-finite JSON number is forbidden: %s" % value)

try:
    request = json.loads(sys.stdin.read(), parse_constant=reject_constant)
    argv = request.get("argv")
    use_shell = request.get("shell")
    valid_argv = (isinstance(argv, str) and bool(argv)) or (
        isinstance(argv, list) and bool(argv) and
        all(isinstance(item, str) and item for item in argv))
    if not valid_argv or not isinstance(use_shell, bool):
        raise ValueError("invalid gated verification request")
    child = subprocess.Popen(
        argv, shell=use_shell, stdin=subprocess.DEVNULL,
        stdout=sys.stdout, stderr=subprocess.STDOUT,
        **({"creationflags": 0x08000000} if os.name == "nt" else {}))
    raise SystemExit(child.wait())
except SystemExit:
    raise
except BaseException as exc:
    sys.stderr.write("gated verification launch failed: %s: %s\n" %
                     (type(exc).__name__, exc))
    raise SystemExit(125)
"""


def _candidate(kind: str, command: str, source: str, confidence: str = "high") -> dict:
    return {"kind": kind, "command": command, "source": source, "confidence": confidence}


def detect_verification_commands(cwd: str) -> list[dict]:
    """Detect likely repo-owned checks without executing project code.

    Results are proposals, not permission.  The UI shows the first one and lets
    the user edit it; Test mode allowlists only that exact command.
    """
    cwd = os.path.abspath(cwd)
    found = []

    package = os.path.join(cwd, "package.json")
    if os.path.isfile(package) and os.path.getsize(package) <= 2_000_000:
        try:
            with open(package, encoding="utf-8") as f:
                scripts = (json.load(f) or {}).get("scripts") or {}
            pm = ("pnpm" if os.path.exists(os.path.join(cwd, "pnpm-lock.yaml")) else
                  "yarn" if os.path.exists(os.path.join(cwd, "yarn.lock")) else "npm")
            aliases = (
                ("test", ("test", "test:unit", "test:ci")),
                ("typecheck", ("typecheck", "type-check", "check:types")),
                ("lint", ("lint",)),
                ("build", ("build",)),
            )
            for kind, names in aliases:
                name = next((n for n in names if n in scripts), None)
                if name:
                    cmd = "%s %s%s" % (pm, "run " if pm == "npm" or name != "test" else "", name)
                    found.append(_candidate(kind, cmd, "package.json#scripts.%s" % name))
        except (OSError, ValueError, TypeError):
            pass

    python_markers = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
    if os.path.isdir(os.path.join(cwd, "tests")) or any(
            os.path.isfile(os.path.join(cwd, p)) for p in python_markers):
        found.append(_candidate("test", "python -m pytest -q", "Python test layout"))

    if os.path.isfile(os.path.join(cwd, "Cargo.toml")):
        found.append(_candidate("test", "cargo test", "Cargo.toml"))
    if os.path.isfile(os.path.join(cwd, "go.mod")):
        found.append(_candidate("test", "go test ./...", "go.mod"))

    makefile = next((os.path.join(cwd, n) for n in ("Makefile", "makefile")
                     if os.path.isfile(os.path.join(cwd, n))), None)
    if makefile:
        try:
            with open(makefile, encoding="utf-8", errors="replace") as f:
                text = f.read(512_000)
            for kind, target in (("test", "test"), ("typecheck", "typecheck"),
                                 ("lint", "lint"), ("build", "build")):
                if re.search(r"(?m)^%s\s*:" % re.escape(target), text):
                    found.append(_candidate(kind, "make " + target, os.path.basename(makefile)))
        except OSError:
            pass

    # De-duplicate while keeping the strongest/useful ordering stable.
    unique = {}
    for item in found:
        unique.setdefault(item["command"], item)
    return sorted(unique.values(), key=lambda x: (_KIND_ORDER.get(x["kind"], 99), x["command"]))


def _filesystem_snapshot(cwd: str) -> dict:
    """Best-effort freshness fingerprint for workspaces without usable Git metadata."""
    digest = hashlib.sha256()
    count = 0
    remaining = _SNAPSHOT_BYTE_CAP
    complete = True
    try:
        for root, dirs, files in os.walk(cwd):
            # Do not follow directory symlinks, but do bind their link target into
            # the digest.  Otherwise swapping an unversioned source tree symlink
            # could leave a verification receipt looking fresh.
            descend = []
            for name in sorted(name for name in dirs
                               if name != ".git" and
                               not _is_untracked_generated_cache(name)):
                path = os.path.join(root, name)
                rel = os.path.relpath(path, cwd).replace(os.sep, "/")
                info = os.lstat(path)
                count += 1
                if count > _SNAPSHOT_FILE_CAP:
                    complete = False
                    break
                digest.update(("dir\0%s\0%d\0" % (rel, info.st_mode)).encode(
                    "utf-8", "surrogatepass"))
                if stat.S_ISLNK(info.st_mode):
                    digest.update(os.readlink(path).encode("utf-8", "surrogatepass"))
                elif stat.S_ISDIR(info.st_mode):
                    descend.append(name)
                else:
                    complete = False
            dirs[:] = descend if complete else []
            for name in sorted(files):
                rel = os.path.relpath(os.path.join(root, name), cwd).replace(os.sep, "/")
                if _is_untracked_generated_cache(rel):
                    continue
                count += 1
                if count > _SNAPSHOT_FILE_CAP:
                    complete = False
                    break
                path = os.path.join(root, name)
                rel = os.path.relpath(path, cwd).replace(os.sep, "/")
                info = os.lstat(path)
                digest.update(("file\0%s\0%d\0%d\0" % (
                    rel, info.st_mode, info.st_size)).encode(
                        "utf-8", "surrogatepass"))
                if stat.S_ISLNK(info.st_mode):
                    digest.update(os.readlink(path).encode("utf-8", "surrogatepass"))
                elif stat.S_ISREG(info.st_mode):
                    if info.st_size > remaining:
                        complete = False
                        digest.update(("content-over-cap:%d" % info.st_size).encode("ascii"))
                        continue
                    with open(path, "rb") as fh:
                        while True:
                            chunk = fh.read(min(1024 * 1024, remaining + 1))
                            if not chunk:
                                break
                            if len(chunk) > remaining:
                                complete = False
                                break
                            digest.update(chunk)
                            remaining -= len(chunk)
                else:
                    # Sockets/devices/FIFOs are neither safely readable nor a
                    # complete source snapshot.  Their metadata remains bound,
                    # but they cannot support a completion-grade receipt.
                    complete = False
            if not complete:
                break
    # Windows reserved device basenames (for example a literal ``nul`` file produced by a POSIX
    # shell) can make ntpath.relpath raise ValueError because the path resolves onto ``\\.\nul``
    # instead of the workspace drive.  A snapshot of such a tree is incomplete, but the verifier
    # must fail closed rather than crash before it can return evidence.
    except (OSError, ValueError):
        complete = False
    return {"tree_digest": digest.hexdigest(), "snapshot_complete": complete,
            "snapshot_kind": "filesystem"}


def _git_snapshot(cwd: str) -> dict:
    from . import plat
    out = {"commit": "", "working_tree": "unversioned", "dirty_files": [],
           "tree_digest": "", "snapshot_complete": False,
           "snapshot_kind": "filesystem"}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True,
            timeout=10, **plat.no_window_kwargs())
        if commit.returncode != 0:
            out.update(_filesystem_snapshot(cwd))
            return out
        out["commit"] = (commit.stdout or "").strip()
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=cwd, capture_output=True,
            timeout=10, **plat.no_window_kwargs())
        if status.returncode != 0:
            out["working_tree"] = "unknown"
            out.update(_filesystem_snapshot(cwd))
            return out
        raw_status = status.stdout or b""
        raw_entries = [entry for entry in raw_status.split(b"\0") if entry]
        entries = []
        dirty = []
        untracked = []
        for entry in raw_entries:
            if len(entry) < 3 or entry[2:3] != b" ":
                entries.append(entry)
                continue
            path = entry[3:].decode("utf-8", "replace")
            if entry[:2] == b"??" and _is_untracked_generated_cache(path):
                continue
            entries.append(entry)
            dirty.append(path)
            if entry[:2] == b"??":
                untracked.append(path)
        filtered_status = b"\0".join(entries) + (b"\0" if entries else b"")
        out["dirty_files"] = dirty[:200]
        out["working_tree"] = "dirty" if entries else "clean"

        diff = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--binary", "HEAD", "--"], cwd=cwd,
            capture_output=True, timeout=30, **plat.no_window_kwargs())
        if diff.returncode != 0:
            out["working_tree"] = "unknown"
            out.update(_filesystem_snapshot(cwd))
            return out
        digest = hashlib.sha256()
        digest.update(out["commit"].encode("ascii", "replace"))
        digest.update(filtered_status)
        digest.update(diff.stdout or b"")
        remaining = _SNAPSHOT_BYTE_CAP
        complete = True
        root = os.path.realpath(os.path.abspath(cwd))
        for rel in untracked:
            path = os.path.realpath(os.path.abspath(os.path.join(root, rel)))
            try:
                if os.path.commonpath((path, root)) != root or not os.path.isfile(path):
                    continue
                size = os.path.getsize(path)
                digest.update(rel.encode("utf-8", "surrogatepass"))
                if size > remaining:
                    complete = False
                    digest.update(("oversize:%d" % size).encode("ascii"))
                    continue
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(min(1024 * 1024, remaining + 1))
                        if not chunk:
                            break
                        if len(chunk) > remaining:
                            complete = False
                            break
                        digest.update(chunk)
                        remaining -= len(chunk)
            except (OSError, ValueError):
                complete = False
        out.update({"tree_digest": digest.hexdigest(), "snapshot_complete": complete,
                    "snapshot_kind": "git"})
    except Exception:
        out.update(_filesystem_snapshot(cwd))
    return out


def workspace_snapshot(cwd: str) -> dict:
    """Return a receipt-safe fingerprint for binding checks to exact workspace bytes.

    The digest is intentionally content-derived and contains no file contents.  A
    durable code worker uses it to distinguish "the existing suite was already
    green" from "this Mission produced a patch and the suite is green now".
    Untracked Python cache artifacts are excluded because a verifier owns those
    bytes; tracked files remain bound through Git's diff.
    """
    snap = _git_snapshot(os.path.realpath(os.path.abspath(cwd)))
    return {key: snap.get(key) for key in (
        "commit", "working_tree", "dirty_files", "tree_digest",
        "snapshot_complete", "snapshot_kind")}


def _terminate_owned_posix_group(pgid: int) -> tuple[bool, str]:
    """End every process left in a verifier's dedicated POSIX process group.

    The direct shell may already have exited successfully, so ``plat.kill_tree``
    cannot rediscover its group with ``getpgid(proc.pid)``.  The group id is
    therefore captured while the child is alive and used directly here.  A
    SIGKILL delivery alone is not proof that every member has exited.  Poll the
    group until ESRCH before the post-verification workspace snapshot so an
    in-flight background write cannot race the freshness receipt.
    """
    import signal
    try:
        # SIGKILL is required on POSIX.  The numeric fallback keeps the helper
        # unit-testable from a Windows host where ``signal.SIGKILL`` is absent.
        os.killpg(int(pgid), getattr(signal, "SIGKILL", 9))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.killpg(int(pgid), 0)
            except ProcessLookupError:
                return True, ""
            except PermissionError as e:
                return False, "%s: %s" % (type(e).__name__, e)
            time.sleep(.01)
        return False, "process group did not become extinct after SIGKILL"
    except ProcessLookupError:
        return True, ""
    except OSError as e:
        return False, "%s: %s" % (type(e).__name__, e)


def _wait_verification_process(proc, timeout_s: float = 5.0) -> bool:
    """Prove the trusted gate itself exited; reject unknown production state."""
    wait = getattr(proc, "wait", None)
    if not callable(wait):
        # Injectable process doubles have no OS process.  Every production
        # subprocess.Popen object exposes wait().
        return True
    try:
        wait(timeout=max(0.0, float(timeout_s)))
        return True
    except Exception:
        return False


def cancel_verification_process(proc, timeout_s: float = 5.0, *,
                                cancel_request: bool = True) -> bool:
    """Cancel an owned verifier and return only after the complete tree is gone.

    ``proc`` is the trusted start-gate process passed to ``on_process``.  The
    per-process lock serializes an external cancellation with timeout/finally
    cleanup.  Signal/TerminateJobObject delivery is never treated as extinction
    evidence: Windows polls Job accounting and POSIX polls the dedicated group.

    ``cancel_request`` records that somebody deliberately stopped this verifier,
    which the evidence then reports.  Routine post-run reaping passes ``False``:
    every check ends here, and a completed run is not a cancelled one.
    """
    if proc is None:
        return True
    if cancel_request:
        # An `on_process` holder that stops the tree has stopped the task.  Its
        # exit code cannot come back later as "the required check passed".
        try:
            setattr(proc, "_collie_verification_cancel_requested", True)
        except Exception:
            pass
    from . import plat
    lock = getattr(proc, "_collie_verification_tree_lock", None)
    if lock is None:
        lock = threading.RLock()
        setattr(proc, "_collie_verification_tree_lock", lock)
    with lock:
        if bool(getattr(proc, "_collie_verification_tree_extinct", False)):
            return True
        owner = getattr(proc, "_collie_verification_job", None)
        if owner is not None:
            terminate_and_wait = getattr(owner, "terminate_and_wait", None)
            if not callable(terminate_and_wait):
                # Production Windows owners always expose this proof-bearing
                # operation.  A close()/terminate() return value alone is not
                # evidence that descendants stopped.
                return False
            try:
                confirmed = bool(terminate_and_wait(timeout_s=timeout_s))
            except Exception:
                confirmed = False
            setattr(proc, "_collie_verification_tree_extinct", confirmed)
            return confirmed
        pgid = int(getattr(proc, "_collie_verification_pgid", 0) or 0)
        if pgid > 1:
            confirmed, error = _terminate_owned_posix_group(pgid)
            if error:
                setattr(proc, "_collie_verification_tree_error", error)
            setattr(proc, "_collie_verification_tree_extinct", confirmed)
            return confirmed
        # This path is only valid before the trusted gate was released (for
        # example Job assignment failed).  Callers mark that fact explicitly;
        # killing the direct gate then proves no target could have existed.
        if bool(getattr(proc, "_collie_verification_gate_closed", False)):
            plat.kill_tree(proc)
            confirmed = _wait_verification_process(proc, timeout_s)
            setattr(proc, "_collie_verification_tree_extinct", confirmed)
            return confirmed
        return False


def _stop_requested(predicate) -> tuple[bool, str]:
    """Read one request's own stop predicate without ever trusting it to behave.

    A predicate that raises is a broken caller, not evidence that the user
    pressed Stop.  Report the fault and keep running: terminating on a bug here
    would kill work nobody asked to stop.
    """
    if not callable(predicate):
        return False, ""
    try:
        return bool(predicate()), ""
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def _emit_event(on_event, kind: str, data: dict) -> None:
    """Publish progress for the UI.  Observability never changes the verdict."""
    if not callable(on_event):
        return
    try:
        on_event(str(kind), dict(data))
    except Exception:
        return


class _CancelWatcher:
    """Stop exactly one verifier tree when this request's own predicate fires.

    The watcher holds nothing global: one trusted gate, one predicate, one stop
    Event that the owning call always sets in its ``finally``.  That is why a
    Stop pressed on another session cannot reach this tree, and why no thread
    can outlive the call that created it.  A predicate that raises ends the poll
    and is reported; it never terminates anybody's process.
    """

    def __init__(self, proc, predicate, on_fire=None,
                 interval: float = _CANCEL_POLL_SECONDS):
        self._proc = proc
        self._predicate = predicate
        self._on_fire = on_fire
        self._interval = max(0.005, float(interval))
        self._done = threading.Event()
        self._thread = None
        self.fired = False
        self.probe_error = ""

    def _poll(self) -> None:
        while not self._done.wait(self._interval):
            wants_stop, probe_error = _stop_requested(self._predicate)
            if probe_error:
                self.probe_error = probe_error
                return
            if not wants_stop:
                continue
            self.fired = True
            if callable(self._on_fire):
                try:
                    self._on_fire()
                except Exception:
                    pass
            try:
                cancel_verification_process(self._proc)
            except Exception as exc:
                self.probe_error = self.probe_error or "%s: %s" % (
                    type(exc).__name__, exc)
            return

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._poll, name="collie-verifier-cancel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """End the poll and join it, so no watcher survives its own request."""
        self._done.set()
        thread, self._thread = self._thread, None
        if thread is None:
            return
        # cancel_verification_process proves extinction before returning, which
        # is bounded by its own timeout; this join only has to outlast that.
        thread.join(timeout=15)
        if thread.is_alive():
            self.probe_error = (self.probe_error or
                                "verification cancel watcher did not stop")


def run_verification_command(command: str, cwd: str, timeout: int = 300,
                             source: str = "user", after_last_edit: bool = True,
                             on_process=None, cancelled=None, on_event=None) -> dict:
    """Execute a proposed check and return receipt-ready evidence.

    ``on_process`` receives the still-blocked trusted gate after process-tree
    ownership is installed.  The caller may retain it for
    :func:`cancel_verification_process`; returning ``False`` cancels without
    ever launching the repository command.

    ``cancelled`` is an optional predicate scoped to THIS request (the Web
    surface passes its per-run cancel Event).  It is read before the gate is
    created, again after registration but before one command byte is released,
    and then polled by a watcher bound to this call alone.  A stop at any of
    those points yields ``cancelled`` evidence, never a pass.

    ``on_event(kind, data)`` mirrors check progress onto an existing event
    stream so a UI can say "verifying" instead of leaving a heartbeat to imply
    the agent vanished.
    """
    from . import plat
    from .runner_specs import redact_text
    command = (command or "").strip()
    started = datetime.now(timezone.utc).isoformat()
    before = _git_snapshot(cwd)
    t0 = time.monotonic()
    evidence = {
        "command": command,
        "exit_code": None,
        "command_passed": False,
        "passed": False,
        "timestamp": started,
        "duration_ms": 0,
        "output": "",
        "cwd": os.path.abspath(cwd),
        "commit": before["commit"],
        "working_tree": before["working_tree"],
        "dirty_files": before["dirty_files"],
        "ran_after_last_edit": False,
        "freshness": "not_run",
        "source": source,
        "tree_digest": before.get("tree_digest", ""),
        "snapshot_complete": bool(before.get("snapshot_complete")),
        "executed": False, "cancelled": False, "process_tree_terminated": False,
    }
    if not command:
        evidence["output"] = "no verification command"
        return evidence
    args, use_shell = plat.shell_argv(command)
    group_kwargs = plat.new_group_kwargs()
    windows = plat.is_windows()

    def _captured_text(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return ""

    executed = False
    cancelled_flag = False
    cancel_reason = ""
    cancel_probe_error = ""
    watcher = None
    proc = None
    windows_job = None
    owned_posix_pgid = None
    tree_cleanup_ok = False
    tree_cleanup_error = ""
    bounded_tail = None
    bounded_reader = None
    try:
        # A Stop pressed while the run was still finishing must not be answered
        # by creating a gate at all.
        stop_now, cancel_probe_error = _stop_requested(cancelled)
        if stop_now:
            cancelled_flag = True
            cancel_reason = "before_start"
            evidence["output"] = "verification cancelled before command start"
            raise _VerificationCancelled()
        # On POSIX a verifier must have a group Collie can safely kill without
        # signalling itself.  Continuing in a shared group would allow an
        # exit-zero command to leave a background writer behind and would make
        # cleanup unsafe, so refuse that execution mode rather than issue a
        # completion-grade receipt.
        if not windows and not group_kwargs.get("start_new_session"):
            raise RuntimeError(
                "could not establish independent verification process-tree ownership")
        if on_process is not None and not callable(on_process):
            raise ValueError("on_process must be callable")
        proc = subprocess.Popen(
            [(getattr(sys, "_base_executable", None) or sys.executable)
             if windows else sys.executable, "-I", "-c", _VERIFICATION_START_GATE_SCRIPT],
            shell=False, cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=plat.shell_environment(),
            **group_kwargs, **plat.no_window_kwargs())
        proc._collie_verification_tree_lock = threading.RLock()
        # Until the JSON request is written and stdin is closed, the isolated
        # trusted gate cannot launch the repository command.
        proc._collie_verification_gate_closed = True
        if not windows:
            # start_new_session makes the child's PID its process-group ID.  Save
            # it now: once the shell exits, getpgid(proc.pid) can no longer find
            # the still-running background members of that group.
            owned_posix_pgid = int(proc.pid)
            proc._collie_verification_pgid = owned_posix_pgid
        # taskkill follows the ordinary Windows parent-PID tree, which MSYS/Git Bash can
        # re-parent while launching a native executable.  A Job Object is the kernel-backed
        # ownership boundary that still contains those descendants.  Binding failure is fail
        # closed: running an unowned verifier would let it keep editing after its receipt.
        try:
            windows_job = plat.attach_kill_on_close_job(proc)
            if windows and windows_job is None:
                raise RuntimeError("Windows Job Object was not created")
            if windows_job is not None:
                proc._collie_verification_job = windows_job
        except Exception as owner_error:
            # No target request has crossed the gate, so direct-gate extinction
            # proves that no repository command or descendant ever existed.
            # A failed launch is not a user cancellation, so it is not recorded
            # as one.
            confirmed = cancel_verification_process(proc, cancel_request=False)
            if not confirmed:
                raise RuntimeError(
                    "verification ownership failed and trusted-gate extinction "
                    "could not be confirmed") from owner_error
            raise RuntimeError(
                "could not establish verification process-tree ownership") from owner_error
        # Registration is the launch latch.  A concurrent cancellation can keep
        # the target at zero executions by returning False here.
        registered = on_process(proc) if callable(on_process) else True
        # A Stop can land between registration and release: the caller now holds
        # a handle, but no command byte has moved.  Read the predicate again on
        # this side of the latch so that window cannot execute anything.
        stop_now, probe_error = _stop_requested(cancelled)
        if probe_error:
            cancel_probe_error = probe_error
        if registered is False or stop_now:
            cancelled_flag = True
            cancel_reason = "before_start"
            if not cancel_verification_process(proc, cancel_request=False):
                raise RuntimeError(
                    "verification was cancelled before start but process-tree "
                    "extinction could not be confirmed")
            evidence["output"] = "verification cancelled before command start"
        else:
            request = json.dumps(
                {"argv": args, "shell": bool(use_shell)},
                ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            proc._collie_verification_gate_closed = False
            executed = True
            _emit_event(on_event, "verification_started", {
                "command": redact_text(command, 4_000), "cwd": evidence["cwd"],
                "source": source, "timeout_s": int(timeout)})
            # The watcher exists only for the span in which a repository command
            # can actually be running, and only for this one tree.
            if callable(cancelled):
                watcher = _CancelWatcher(
                    proc, cancelled,
                    on_fire=lambda: _emit_event(on_event, "verification_canceling", {
                        "command": redact_text(command, 4_000),
                        "cwd": evidence["cwd"]}))
                watcher.start()
            # Production Popen pipes take the bounded path.  Several embedders
            # and process doubles expose only ``communicate``; keep that narrow
            # compatibility path, while a real child can never accumulate an
            # unbounded stdout string in this process.
            if (getattr(proc, "stdout", None) is not None and
                    getattr(proc, "stdin", None) is not None and
                    callable(getattr(proc.stdout, "readline", None)) and
                    callable(getattr(proc.stdin, "write", None)) and
                    callable(getattr(proc, "wait", None))):
                bounded_tail = _TailCapture()
                bounded_reader = threading.Thread(
                    target=_bounded_stdout_reader,
                    args=(proc.stdout, bounded_tail),
                    name="collie-verifier-stdout", daemon=True)
                bounded_reader.start()
                try:
                    proc.stdin.write(request)
                    proc.stdin.flush()
                finally:
                    proc.stdin.close()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    raise subprocess.TimeoutExpired(
                        "verification command", timeout,
                        output=bounded_tail.text())
                bounded_reader.join(timeout=5)
                if bounded_reader.is_alive():
                    raise RuntimeError(
                        "verification output pipe did not close after command exit")
                output = bounded_tail.text()
            else:
                output, _ = proc.communicate(input=request, timeout=timeout)
            evidence["exit_code"] = int(proc.returncode)
            evidence["command_passed"] = proc.returncode == 0
            evidence["output"] = (output or "")[-_VERIFICATION_OUTPUT_CHARS:]
    except _VerificationCancelled:
        # The stop is already written into the evidence above; the finally below
        # is still what proves nothing was left running.
        pass
    except KeyboardInterrupt:
        # Ctrl-C during a host check is a STOP, not a crash.  Unwinding out of
        # here would take the surface's receipt, session save and the agent's
        # already-produced answer with it.  Convert it into honest evidence and
        # let the caller honour `cancelled` instead.
        cancelled_flag = True
        cancel_reason = "keyboard_interrupt"
        evidence["output"] = (evidence.get("output") or "")[-3500:]
    except subprocess.TimeoutExpired as e:
        executed = True
        # ``Popen.communicate`` does not kill its child on timeout.  More importantly, killing
        # only the shell leaves backgrounded test runners holding the output pipe and editing the
        # workspace after their receipt was issued.  The process was started in its own group so
        # the platform layer can reap the shell and every descendant before we snapshot again.
        tree_cleanup_ok = cancel_verification_process(proc, cancel_request=False)
        if not tree_cleanup_ok:
            tree_cleanup_error = str(
                getattr(proc, "_collie_verification_tree_error", "") or
                "process-tree extinction could not be confirmed")
        partial = (bounded_tail.text() if bounded_tail is not None else
                   _captured_text(e.output))
        if bounded_reader is not None:
            bounded_reader.join(timeout=5)
            partial = bounded_tail.text()
        else:
            try:
                drained, _ = proc.communicate(timeout=5)
                if isinstance(drained, (str, bytes)):
                    partial = _captured_text(drained)
            except subprocess.TimeoutExpired as drain_error:
            # A broken platform/process double must not turn verification cleanup into an
            # unbounded wait.  Keep any bytes communicate managed to collect, close our pipe, and
            # make a final best-effort reap of the direct child.
                if isinstance(drain_error.output, (str, bytes)):
                    partial = _captured_text(drain_error.output)
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                except Exception:
                    pass
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass
        evidence["output"] = partial[-3500:] + "\n(check timed out after %ds)" % timeout
    except Exception as e:
        evidence["output"] = "check failed to run: %s: %s" % (type(e).__name__, e)
    finally:
        # The watcher is joined before anything reads its result, so this call
        # can never leave a thread polling a predicate it no longer owns.
        if watcher is not None:
            watcher.stop()
        if proc is not None:
            # Routine reaping, not a cancellation: every run ends here.
            confirmed = cancel_verification_process(proc, cancel_request=False)
            tree_cleanup_ok = bool(tree_cleanup_ok or confirmed)
            if not tree_cleanup_ok and not tree_cleanup_error:
                tree_cleanup_error = str(
                    getattr(proc, "_collie_verification_tree_error", "") or
                    "process-tree extinction could not be confirmed")
        if windows_job is not None:
            try:
                windows_job.close()
            except Exception as cleanup_error:
                close_error = "%s: %s" % (
                    type(cleanup_error).__name__, cleanup_error)
                tree_cleanup_error = (tree_cleanup_error + "; " + close_error
                                      if tree_cleanup_error else close_error)
    if bounded_reader is not None:
        bounded_reader.join(timeout=.25)
        # Ctrl-C can interrupt wait() before the normal path copies stdout. The
        # pipe reader already owns that evidence; preserve it after tree cleanup.
        if bounded_tail is not None and not evidence.get("output"):
            evidence["output"] = bounded_tail.text()
    # Resolve cancellation from what actually happened, never from a snapshot
    # taken before the command ran.
    if watcher is not None:
        if watcher.probe_error and not cancel_probe_error:
            cancel_probe_error = watcher.probe_error
        if watcher.fired and not cancelled_flag:
            cancelled_flag = True
            cancel_reason = "during_execution"
    if not cancelled_flag and bool(
            getattr(proc, "_collie_verification_cancel_requested", False)):
        # An `on_process` holder stopped this tree itself.  Whatever exit code
        # the race produced, the check was not allowed to finish.
        cancelled_flag = True
        cancel_reason = "caller_cancelled_process"
    _CANCEL_NOTES = {
        "during_execution": "\n(verification stopped by user request)",
        "keyboard_interrupt":
            "\n(verification interrupted by user; the check did not finish)",
        "caller_cancelled_process":
            "\n(verification process was cancelled by its caller)",
    }
    if cancel_reason in _CANCEL_NOTES:
        evidence["output"] = ((evidence.get("output") or "")[-3500:] +
                              _CANCEL_NOTES[cancel_reason])[-4000:]
    if cancel_probe_error:
        evidence["output"] = ((evidence.get("output") or "")[-3500:] +
                              "\n(cancellation check failed: %s)" %
                              cancel_probe_error)[-4000:]
    if executed and not tree_cleanup_ok:
        suffix = "\n(could not terminate verification process tree"
        if tree_cleanup_error:
            suffix += ": " + tree_cleanup_error
        suffix += ")"
        evidence["output"] = ((evidence.get("output") or "")[-3500:] + suffix)[-4000:]
    evidence["duration_ms"] = int((time.monotonic() - t0) * 1000)
    after = _git_snapshot(cwd)
    comparable = bool(before.get("tree_digest") and after.get("tree_digest") and
                      before.get("snapshot_complete") and after.get("snapshot_complete"))
    unchanged = bool(comparable and before["tree_digest"] == after["tree_digest"] and
                     before.get("commit") == after.get("commit"))
    evidence.update({
        "post_commit": after.get("commit", ""),
        "post_working_tree": after.get("working_tree", "unknown"),
        "post_dirty_files": after.get("dirty_files", []),
        "working_tree_changed_during_check": (not unchanged) if comparable else None,
        # A stopped check never ran to completion, so it cannot certify the tree
        # it was pointed at, however clean that tree happens to look afterwards.
        "ran_after_last_edit": bool(
            executed and tree_cleanup_ok and after_last_edit and unchanged and
            not cancelled_flag),
        "freshness": ("not_run" if not executed else
                      "process_tree_cleanup_failed" if not tree_cleanup_ok else
                      "cancelled" if cancelled_flag else
                      "caller_marked_stale" if not after_last_edit else "fresh" if unchanged else
                      "changed_during_check" if comparable else "unknown"),
        "snapshot_kind": before.get("snapshot_kind", "unknown"),
        "post_tree_digest": after.get("tree_digest", ""),
        "post_snapshot_complete": bool(after.get("snapshot_complete")),
        "executed": executed,
        "cancelled": cancelled_flag,
        "cancel_reason": cancel_reason,
        "cancel_probe_error": cancel_probe_error,
        "process_tree_terminated": bool(proc is not None and tree_cleanup_ok),
    })
    # ``passed`` is the completion-grade verdict consumed by CLI/Web/Pack. Exit zero remains
    # separately visible as ``command_passed``, but it cannot certify bytes that changed during
    # the check or whose freshness snapshot was incomplete.  A cancellation racing an exit-zero
    # callback is excluded here too: a stop the user asked for cannot be laundered into a pass.
    evidence["passed"] = bool(
        evidence["command_passed"] and evidence["ran_after_last_edit"] and
        not cancelled_flag)
    # Command lines and test output are durable and are streamed to remote UI
    # clients.  Execute the original command above, but persist only a bounded,
    # redacted projection; verification status/digests are untouched.
    evidence["command"] = redact_text(evidence.get("command", ""), 4_000)
    evidence["output"] = redact_text(
        evidence.get("output", ""), _VERIFICATION_OUTPUT_CHARS)
    _emit_event(on_event, "verification_finished", {
        "command": evidence["command"], "executed": evidence["executed"],
        "passed": evidence["passed"], "cancelled": evidence["cancelled"],
        "exit_code": evidence["exit_code"], "freshness": evidence["freshness"],
        "duration_ms": evidence["duration_ms"]})
    return evidence


# ── what one receipt actually establishes, in one sentence ───────────────────
# ``passed`` is the completion-grade verdict and it is correct.  What no
# user-facing surface ever said out loud is WHY a receipt can be not-passed
# while the command itself exited zero: the freshness binding failed, so the
# exit code does not cover the bytes the check was asked about.
#
# That is not a check failure, and calling it one sends a person hunting for a
# broken test that does not exist.  It is also not rare: any check that writes
# its own build output into the workspace it grades — ``cargo test`` into
# ``target/``, ``npm run build`` into ``dist/``, ``make build`` — changes the
# tree under itself and lands here on every green run.
_UNCERTIFIED_FRESHNESS = {
    "changed_during_check":
        "the workspace changed while it was running, so its result does not "
        "cover the files that exist now",
    "process_tree_cleanup_failed":
        "its process tree could not be proved extinct, so something it started "
        "may still be writing files",
    "caller_marked_stale":
        "the result was already recorded as stale before the check started",
    "unknown":
        "the workspace could not be fingerprinted completely, so the result "
        "cannot be bound to exact bytes",
}
_UNCERTIFIED_DEFAULT = ("its result could not be bound to the files that exist "
                        "now")


def check_result_state(evidence) -> str:
    """Classify one check receipt: what did it establish, in one word?

    ``passed``, ``failed``, ``inconclusive``, ``cancelled`` or ``not_run``.
    ``inconclusive`` is the exit-zero-but-unbindable case above; every caller
    that only knew "passed or not" reported it as a failure.
    """
    ev = evidence if isinstance(evidence, dict) else {}
    if ev.get("executed") is False:
        return "not_run"
    if ev.get("cancelled"):
        return "cancelled"
    if ev.get("passed") is True:
        return "passed"
    if ev.get("command_passed") is True:
        return "inconclusive"
    return "failed"


def check_result_reason(evidence, command="") -> str:
    """The run-error sentence a required check owes when it did not certify.

    Empty for a check that passed.  The wording is the receipt's own, so the
    conversation, the CLI and the durable receipt cannot disagree with the
    evidence they were all derived from.
    """
    ev = evidence if isinstance(evidence, dict) else {}
    command = str(command or ev.get("command") or "")
    state = check_result_state(ev)
    if state == "passed":
        return ""
    if state == "not_run":
        return "required check did not run: %s" % command
    if state == "cancelled":
        return "required check was stopped before it finished: %s" % command
    if state == "inconclusive":
        return ("required check did not certify this result: %s exited 0, but %s"
                % (command, _UNCERTIFIED_FRESHNESS.get(
                    str(ev.get("freshness") or ""), _UNCERTIFIED_DEFAULT)))
    return "required check failed: %s (exit %s)" % (command, ev.get("exit_code"))


# ── the durable effect fence around a host check ─────────────────────────────
# A repository check is a real external action: it runs project code that can
# write files, and the surrounding turn cannot be replayed afterwards without
# knowing whether it did.  The native harness has already closed its own journal
# boundary by the time the host check starts, so the check needs its own — armed
# BEFORE the first command byte, retired only on evidence.


def open_check_boundary(sid, messages, project="", cwd="", run_id="",
                        command="", surface="") -> dict:
    """Fence a host check's possible file effects before it can run.

    Returns a boundary the caller passes back to :func:`close_check_boundary`.
    ``error`` is non-empty when no durable fence could be established, and the
    caller must then NOT start the command: an unfenced check that dies with the
    process leaves a workspace nobody knows the state of.

    An older uncertain boundary is never overwritten.  Its detail describes an
    effect a human still has to inspect, and hiding that behind this check's own
    detail would quietly retire someone else's unreconciled action.
    """
    from . import sessions
    from .runner_specs import redact_text
    state = {"armed": False, "preexisting": False, "error": "", "sid": sid,
             "project": project, "cwd": cwd, "run_id": str(run_id or "")}
    if not sid or not isinstance(sid, str):
        state["error"] = ("verification boundary needs a durable session id "
                          "before a host check may run")
        return state
    try:
        existing = sessions.recovery_state(sid)
    except Exception as exc:
        state["error"] = redact_text(
            "verification boundary could not read the session journal: %s: %s"
            % (type(exc).__name__, exc), 500)
        return state
    if isinstance(existing, dict) and existing.get("recovery_required"):
        state["preexisting"] = True
        return state
    try:
        sessions.checkpoint(
            sid, list(messages or []), project=project, cwd=cwd,
            run_id=str(run_id or ""), state="external_action",
            detail={"tool_name": CHECK_BOUNDARY_TOOL,
                    "surface": surface or "host",
                    "command": redact_text(str(command or ""), 500)})
        armed = sessions.recovery_state(sid)
    except Exception as exc:
        state["error"] = redact_text(
            "verification recovery boundary could not be persisted: %s: %s"
            % (type(exc).__name__, exc), 500)
        return state
    # checkpoint() is a no-op for an id that maps to no session file, so the
    # write is only believed once the journal reads it back as a live fence.
    if not (isinstance(armed, dict) and armed.get("recovery_required") and
            (armed.get("detail") or {}).get("tool_name") == CHECK_BOUNDARY_TOOL):
        state["error"] = ("verification recovery boundary did not become durable "
                          "for this session")
        return state
    state["armed"] = True
    return state


def check_boundary_verdict(evidence) -> tuple[bool, str]:
    """May a host check's fence be retired, and what does the receipt owe?

    Termination is not the same claim as "nothing happened".  A verifier that
    ran and was proved extinct may still have written files; the boundary can be
    retired because nothing is *still* running, and the receipt says so.
    """
    ev = evidence if isinstance(evidence, dict) else {}
    if ev.get("executed") is False:
        return True, "the check did not execute, so it changed nothing"
    if ev.get("executed") is True and ev.get("process_tree_terminated") is True:
        return True, ("the check executed and may have changed files in the "
                      "workspace; its process tree is confirmed gone")
    return False, ("the check's execution or process-tree termination could not be "
                   "confirmed; files may still be changing")


def close_check_boundary(boundary, evidence) -> dict:
    """Retire only a fence this check armed, and only on real evidence.

    A boundary that belongs to somebody else — an external worker's pre-launch
    fence, an interrupted tool — is left exactly where it was.  That is the rule
    which stops "the check returned" from being read as "that other effect
    resolved".
    """
    from . import sessions
    from .runner_specs import redact_text
    retire, detail = check_boundary_verdict(evidence)
    out = {"retired": False, "fenced": False, "detail": detail, "error": ""}
    state = boundary if isinstance(boundary, dict) else {}
    if state.get("preexisting"):
        # Somebody else's fence is already open.  Report only what THIS check
        # left unaccounted for; retiring the other boundary is not ours to do,
        # and neither is holding it open on this check's behalf.
        out["fenced"] = not retire
        out["detail"] = (detail + "; an earlier unreconciled boundary still "
                                  "fences this thread")
        return out
    if not state.get("armed"):
        out["fenced"] = not retire
        return out
    if not retire:
        out["fenced"] = True
        return out
    try:
        sessions.checkpoint(state.get("sid"), [], project=state.get("project", ""),
                            cwd=state.get("cwd", ""),
                            run_id=state.get("run_id", ""), terminal=True)
    except Exception as exc:
        # A fence that could not be retired stays a fence.  Saying so is the
        # point: silently dropping it is the failure mode this guards against.
        out["fenced"] = True
        out["error"] = redact_text(
            "verification recovery boundary could not be cleared: %s: %s"
            % (type(exc).__name__, exc), 500)
        return out
    out["retired"] = True
    return out
