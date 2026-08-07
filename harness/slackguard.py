"""OS-backed owner for one Slack task's complete process tree.

The listener durably records this guard before releasing it.  The guard starts a
second, still-gated executor, records that PID, then accepts ``go``.  Windows puts
the executor in a kill-on-close Job Object; POSIX gives it a parent pipe and its
own process group.  Thus listener death, guard death, and ordinary ``stop`` all
terminate descendants before a replacement can retry.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading

from . import plat

INTERRUPTED_EXIT = 76


def _read_release() -> bool:
    data = b""
    while len(data) < 4 and not data.endswith(b"\n"):
        chunk = os.read(sys.stdin.fileno(), 1)
        if not chunk:
            break
        data += chunk
    return data in (b"go\n", b"go\r\n")


def _process_identity(pid: int) -> str:
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k = ctypes.windll.kernel32
            k.OpenProcess.restype = wintypes.HANDLE
            handle = k.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return ""
            try:
                created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
                if not k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                         ctypes.byref(kernel), ctypes.byref(user)):
                    return ""
                return "%08x%08x" % (created.dwHighDateTime, created.dwLowDateTime)
            finally:
                k.CloseHandle(handle)
        try:
            with open("/proc/%d/stat" % int(pid), encoding="ascii") as f:
                return f.read().split()[21]
        except FileNotFoundError:
            return subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(int(pid))],
                text=True, timeout=2).strip()
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return ""


class _WindowsJob:
    """A kernel-owned process tree that dies even if this guard is killed."""

    def __init__(self, child: subprocess.Popen):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC_LIMITS(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED_LIMITS(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMITS),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k = ctypes.windll.kernel32
        k.CreateJobObjectW.restype = wintypes.HANDLE
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError()
        info = EXTENDED_LIMITS()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k.CloseHandle(handle)
            raise ctypes.WinError()
        if not k.AssignProcessToJobObject(handle, wintypes.HANDLE(int(child._handle))):
            k.CloseHandle(handle)
            raise ctypes.WinError()
        self.handle = handle

    def terminate(self):
        if self.handle is not None:
            import ctypes
            ctypes.windll.kernel32.TerminateJobObject(self.handle, 137)

    def close(self):
        if self.handle is not None:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def _write_state(path: str, child: subprocess.Popen):
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"exec_pid": child.pid,
                   "exec_started": _process_identity(child.pid)}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _kill_tree(child: subprocess.Popen, job: _WindowsJob | None):
    if os.name == "nt":
        if job is not None:
            job.terminate()
        else:
            try:
                subprocess.run(
                    [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                  "System32", "taskkill.exe"),
                    "/PID", str(child.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10, **plat.no_window_kwargs())
            except (OSError, subprocess.SubprocessError):
                pass
        return
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except OSError:
        pass


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4 or args[:1] != ["--state"] or "--" not in args[2:]:
        print("slackguard: expected --state PATH -- COMMAND", file=sys.stderr)
        return 2
    split = args.index("--", 2)
    state_path, command = args[1], args[split + 1:]
    if not command:
        return 2

    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    executor = subprocess.Popen(
        [sys.executable, "-m", "harness.slackexec", "--"] + command,
        stdin=subprocess.PIPE, **kwargs)
    job = None
    try:
        job = _WindowsJob(executor)
        _write_state(state_path, executor)

        # Accept exactly one line token, allowing Windows TextIOWrapper's CRLF.
        if not _read_release():
            return 75
        executor.stdin.write(b"go\n")
        executor.stdin.flush()

        def watch_listener():
            os.read(sys.stdin.fileno(), 1)
            _kill_tree(executor, job)

        threading.Thread(target=watch_listener, daemon=True).start()
        rc = executor.wait()
        # A signal-killed executor has an outcome-unknown task: a shell timeout
        # may already have changed files before ending the shared owned group.
        # Give the listener a portable code instead of letting POSIX turn -9
        # into the ordinary-looking exit 247.
        return INTERRUPTED_EXIT if rc < 0 or rc == 137 else rc
    finally:
        try:
            executor.stdin.close()
        except OSError:
            pass
        # Also clears descendants left behind if the direct executor exited or
        # was killed independently. On Windows close is the kernel guarantee.
        _kill_tree(executor, job)
        if job is not None:
            job.close()


if __name__ == "__main__":
    raise SystemExit(main())
