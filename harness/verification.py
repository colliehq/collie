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


# The repository command must not receive even one instruction byte until its
# process tree has a kernel owner and the caller has registered its cancellation
# handle.  This trusted, isolated Python gate blocks on stdin; only the parent
# can release it, after assigning the gate to a POSIX process group or Windows
# Job Object.  The real command and every descendant inherit that owner.
_VERIFICATION_START_GATE_SCRIPT = r"""
import json
import subprocess
import sys

try:
    request = json.loads(sys.stdin.read())
    argv = request.get("argv")
    use_shell = request.get("shell")
    valid_argv = (isinstance(argv, str) and bool(argv)) or (
        isinstance(argv, list) and bool(argv) and
        all(isinstance(item, str) and item for item in argv))
    if not valid_argv or not isinstance(use_shell, bool):
        raise ValueError("invalid gated verification request")
    child = subprocess.Popen(
        argv, shell=use_shell, stdin=subprocess.DEVNULL,
        stdout=sys.stdout, stderr=subprocess.STDOUT)
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
            for name in sorted(name for name in dirs if name != ".git"):
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
    except OSError:
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
        entries = [entry for entry in raw_status.split(b"\0") if entry]
        dirty = []
        untracked = []
        for entry in entries:
            if len(entry) < 3 or entry[2:3] != b" ":
                continue
            path = entry[3:].decode("utf-8", "replace")
            dirty.append(path)
            if entry[:2] == b"??":
                untracked.append(path)
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
        digest.update(raw_status)
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


def cancel_verification_process(proc, timeout_s: float = 5.0) -> bool:
    """Cancel an owned verifier and return only after the complete tree is gone.

    ``proc`` is the trusted start-gate process passed to ``on_process``.  The
    per-process lock serializes an external cancellation with timeout/finally
    cleanup.  Signal/TerminateJobObject delivery is never treated as extinction
    evidence: Windows polls Job accounting and POSIX polls the dedicated group.
    """
    if proc is None:
        return True
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


def run_verification_command(command: str, cwd: str, timeout: int = 300,
                             source: str = "user", after_last_edit: bool = True,
                             on_process=None) -> dict:
    """Execute a proposed check and return receipt-ready evidence.

    ``on_process`` receives the still-blocked trusted gate after process-tree
    ownership is installed.  The caller may retain it for
    :func:`cancel_verification_process`; returning ``False`` cancels without
    ever launching the repository command.
    """
    from . import plat
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
    cancelled = False
    proc = None
    windows_job = None
    owned_posix_pgid = None
    tree_cleanup_ok = False
    tree_cleanup_error = ""
    try:
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
            [sys.executable, "-I", "-c", _VERIFICATION_START_GATE_SCRIPT],
            shell=False, cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
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
            confirmed = cancel_verification_process(proc)
            if not confirmed:
                raise RuntimeError(
                    "verification ownership failed and trusted-gate extinction "
                    "could not be confirmed") from owner_error
            raise RuntimeError(
                "could not establish verification process-tree ownership") from owner_error
        # Registration is the launch latch.  A concurrent cancellation can keep
        # the target at zero executions by returning False here.
        registered = on_process(proc) if callable(on_process) else True
        if registered is False:
            cancelled = True
            if not cancel_verification_process(proc):
                raise RuntimeError(
                    "verification was cancelled before start but process-tree "
                    "extinction could not be confirmed")
            evidence["output"] = "verification cancelled before command start"
        else:
            request = json.dumps(
                {"argv": args, "shell": bool(use_shell)},
                ensure_ascii=True, separators=(",", ":"))
            proc._collie_verification_gate_closed = False
            executed = True
            output, _ = proc.communicate(input=request, timeout=timeout)
            evidence["exit_code"] = int(proc.returncode)
            evidence["command_passed"] = proc.returncode == 0
            evidence["output"] = (output or "")[-4000:]
    except subprocess.TimeoutExpired as e:
        executed = True
        # ``Popen.communicate`` does not kill its child on timeout.  More importantly, killing
        # only the shell leaves backgrounded test runners holding the output pipe and editing the
        # workspace after their receipt was issued.  The process was started in its own group so
        # the platform layer can reap the shell and every descendant before we snapshot again.
        tree_cleanup_ok = cancel_verification_process(proc)
        if not tree_cleanup_ok:
            tree_cleanup_error = str(
                getattr(proc, "_collie_verification_tree_error", "") or
                "process-tree extinction could not be confirmed")
        partial = _captured_text(e.output)
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
        if proc is not None:
            confirmed = cancel_verification_process(proc)
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
        "ran_after_last_edit": bool(
            executed and tree_cleanup_ok and after_last_edit and unchanged),
        "freshness": ("not_run" if not executed else
                      "process_tree_cleanup_failed" if not tree_cleanup_ok else
                      "caller_marked_stale" if not after_last_edit else "fresh" if unchanged else
                      "changed_during_check" if comparable else "unknown"),
        "snapshot_kind": before.get("snapshot_kind", "unknown"),
        "post_tree_digest": after.get("tree_digest", ""),
        "post_snapshot_complete": bool(after.get("snapshot_complete")),
        "executed": executed,
        "cancelled": cancelled,
        "process_tree_terminated": bool(proc is not None and tree_cleanup_ok),
    })
    # ``passed`` is the completion-grade verdict consumed by CLI/Web/Pack. Exit zero remains
    # separately visible as ``command_passed``, but it cannot certify bytes that changed during
    # the check or whose freshness snapshot was incomplete.
    evidence["passed"] = bool(
        evidence["command_passed"] and evidence["ran_after_last_edit"])
    return evidence
