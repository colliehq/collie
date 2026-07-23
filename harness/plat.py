"""plat — the thin OS-abstraction layer.

collie is ONE cross-platform codebase, not a per-OS fork. The handful of operations
that genuinely differ across systems — killing a process tree on a timeout, creating
a secret file that can't be symlink-hijacked, converting a path for a Windows program,
choosing a shell — live here so the rest of the harness stays portable.

  Linux / macOS  POSIX primitives (process groups, O_NOFOLLOW, chmod)
  Windows        the Windows equivalents (taskkill /T, no chmod, PowerShell)
  WSL            Linux, but a Windows-side browser needs Windows paths (wslpath)

Nothing here assumes a platform at import time; each function branches on the live
OS, so the same wheel runs everywhere and degrades gracefully where a primitive is
absent (e.g. chmod is a no-op on Windows rather than a crash).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


# ── detection ────────────────────────────────────────────────────────────────
def is_windows() -> bool:
    return os.name == "nt"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_wsl() -> bool:
    """WSL = a Linux kernel with 'microsoft' in its release string. Distinct from
    native Linux because a Windows-side browser/tool needs Windows paths."""
    if os.name != "posix":
        return False
    try:
        with open("/proc/version", encoding="utf-8", errors="ignore") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def os_label() -> str:
    if is_wsl():
        return "WSL (Linux under Windows)"
    if is_windows():
        return "Windows"
    if is_macos():
        return "macOS"
    return "Linux"


# ── process management ───────────────────────────────────────────────────────
def new_group_kwargs() -> dict:
    """Popen kwargs to isolate the child so a timeout can reap the WHOLE tree.
    POSIX: start_new_session (setsid) → its own process group. Windows: taskkill /T
    walks the PID tree directly, so no special flag is needed (and
    CREATE_NEW_PROCESS_GROUP would change Ctrl-C semantics), so return nothing."""
    return {} if is_windows() else {"start_new_session": True}


def kill_tree(proc) -> None:
    """Kill a Popen AND every descendant. A backgrounded grandchild that inherited
    the stdout pipe would otherwise hold its write end open and wedge a follow-up
    drain — the hazard the bash/grep tools guard against on a timeout."""
    try:
        if is_windows():
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        import signal
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)   # session leader + all grandchildren
    except Exception:
        try:
            proc.kill()                                   # last resort: the direct child only
        except Exception:
            pass


# ── filesystem ───────────────────────────────────────────────────────────────
def rmtree(path: str) -> None:
    """Recursively remove a directory tree, cross-platform (replaces `rm -rf`)."""
    shutil.rmtree(path, ignore_errors=True)


def open_excl(path: str, mode: int = 0o600) -> int:
    """os.open with O_CREAT|O_EXCL|O_WRONLY, plus O_NOFOLLOW where the platform has
    it (a symlink-planting guard on POSIX; simply absent on Windows). Returns an fd.
    Fails if the target already exists — the atomic-create contract."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, mode)


def chmod_private(path: str) -> None:
    """Restrict a file to its owner (for secrets/tokens). On Windows this is a no-op
    — the POSIX permission bits don't map to Windows ACLs — rather than an error."""
    if is_windows():
        return
    try:
        import stat
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def to_host_path(p: str) -> str:
    """Convert a path to the HOST OS's form for a program that runs outside this
    environment. Only meaningful under WSL (a Linux path → a Windows path, e.g. to
    hand a file to Windows Chrome); elsewhere it is the identity."""
    if not is_wsl():
        return p
    try:
        out = subprocess.run(["wslpath", "-w", p], capture_output=True, text=True).stdout.strip()
        return out or p
    except Exception:
        return p


# ── shell ────────────────────────────────────────────────────────────────────
def shell_hint() -> str:
    """A one-line note for the agent's system prompt so it emits commands the host
    can actually run. On POSIX it's empty (the model's default Unix habits are
    correct); on Windows it steers away from ls/grep/cat/rm toward the native tools."""
    if not is_windows():
        return ""
    return ("PLATFORM: you are on Windows — there is no POSIX shell. Prefer the file and "
            "search tools (read_file / edit_file / code_search / glob / execute_code) over "
            "`bash`; the shell here is PowerShell/cmd, so do NOT use ls, grep, cat, rm, find, "
            "or other Unix commands.")
