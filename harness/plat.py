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
def posix_shell() -> "str | None":
    """Absolute path to a POSIX shell, or None if the host has none.

    POSIX contract, one shell dialect on every OS: the model emits POSIX commands
    (`ls`, `grep`, `;`, `&&`, pipes, heredocs) and they run the same everywhere.
      POSIX     /bin/sh (always present)
      Windows   Git Bash → MSYS2 → Cygwin, in that order — EXPLICITLY skipping
                C:\\Windows\\System32\\bash.exe (the WSL launcher: it runs commands
                inside the Linux filesystem with different cwd/path semantics, so a
                Windows-native cwd would land in the wrong place). WSL proper (where
                the interpreter itself is Linux) takes the POSIX branch above.
    Discovery is live (no import-time branch, no cache) so the same wheel degrades
    gracefully wherever a shell is absent."""
    if not is_windows():
        return shutil.which("sh") or "/bin/sh"
    cands = []
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            cands.append(os.path.join(base, "Git", "bin", "bash.exe"))
            cands.append(os.path.join(base, "Git", "usr", "bin", "bash.exe"))
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(os.path.join(la, "Programs", "Git", "bin", "bash.exe"))
    drive = os.environ.get("SystemDrive", "C:")
    cands += [drive + r"\msys64\usr\bin\bash.exe", drive + r"\cygwin64\bin\bash.exe"]
    for c in cands:
        if os.path.exists(c):
            return c
    # last resort: PATH lookup, but reject the System32 WSL stub (see docstring)
    p = shutil.which("bash")
    if p and "system32" not in p.lower() and "windir" not in p.lower():
        return p
    return None


def has_posix_shell() -> bool:
    return posix_shell() is not None


def shell_argv(command: str):
    """Return (args, use_shell) for running `command` in a POSIX shell on any OS.

    POSIX: the system /bin/sh via shell=True. Windows: a real bash (Git Bash /
    MSYS2 / Cygwin) if one is present, so `;`, `&&`, pipes, `seq`/`sleep` and
    process-group kills all behave; otherwise cmd.exe as a degraded fallback (with
    shell_hint() steering the model toward the native file/search tools)."""
    if not is_windows():
        return command, True
    sh = posix_shell()
    if sh:
        return [sh, "-c", command], False
    return command, True                                   # cmd.exe (degraded)


def shell_hint() -> str:
    """A one-line note for the agent's system prompt so it emits commands the host
    can actually run. Empty when a POSIX shell is available (the model's Unix habits
    are correct) — including native Windows with Git Bash. Only when Windows has NO
    POSIX shell does it steer toward the native tools and away from ls/grep/cat/rm."""
    if not is_windows() or has_posix_shell():
        return ""
    return ("PLATFORM: you are on Windows with no POSIX shell (Git Bash / WSL not found). "
            "Prefer the file and search tools (read_file / edit_file / code_search / glob / "
            "execute_code) over `bash`; the fallback shell is cmd.exe, so do NOT use ls, grep, "
            "cat, rm, find, or other Unix commands. Installing Git Bash restores full shell "
            "support.")
