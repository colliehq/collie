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


def in_app_bundle() -> bool:
    """Are we running from inside the shipped, code-signed Collie.app?

    The launcher exports COLLIE_BUNDLED; the path check is the belt for anyone who
    invokes the bundled interpreter directly. It matters because a signed bundle is
    *sealed*: writing a single file inside it invalidates the signature, and the
    app then stops opening at some later date with nothing to connect it to the
    write that did it.
    """
    import os as _os
    if _os.environ.get("COLLIE_BUNDLED"):
        return True
    return "/Collie.app/Contents/" in _os.path.abspath(__file__)


def translocated() -> bool:
    """Is macOS running us from a throwaway copy?

    Gatekeeper path-randomises a quarantined app that has never been moved by
    Finder: open Collie straight from the dmg or from Downloads and it runs out of
    /private/var/folders/…/AppTranslocation/…, a read-only copy that disappears
    when the app quits.

    Everything works — which is the problem. Any path we hand the user (the browser
    extension directory above all) points into that copy, so they load it, it works
    today, and it is gone tomorrow with nothing on screen having warned them.
    """
    import os as _os
    return "/AppTranslocation/" in _os.path.abspath(__file__)


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


def no_window_kwargs() -> dict:
    """Popen kwargs that keep a child from flashing a console window.

    CREATE_NO_WINDOW is a Windows-only flag, and passing `creationflags` anywhere else raises
    ValueError: creationflags is only supported on Windows platforms. Spread inline across the
    codebase and wrapped in the usual `except Exception`, that turned into six separate features
    that silently did nothing on macOS — app launching, icon extraction, music search. One helper,
    used everywhere, is the difference between "this platform is unsupported" and "this platform
    fails quietly".
    """
    return {"creationflags": 0x08000000} if is_windows() else {}


def kill_tree(proc) -> None:
    """Kill a Popen AND every descendant. A backgrounded grandchild that inherited
    the stdout pipe would otherwise hold its write end open and wedge a follow-up
    drain — the hazard the bash/grep tools guard against on a timeout."""
    try:
        if is_windows():
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           **no_window_kwargs())
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


def open_with_default(path: str) -> bool:
    """Hand a file to whatever the OS opens it with (a video in the default player, say).
    os.startfile does NOT exist outside Windows, so calling it directly — as `collie record play`
    used to — is an AttributeError everywhere else, not a fallback."""
    try:
        if is_windows():
            os.startfile(path)                                  # noqa: B606 (Windows-only API)
        else:
            subprocess.Popen(["open" if is_macos() else "xdg-open", path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def reveal_in_file_manager(path: str) -> bool:
    """Show a file/folder in Explorer / Finder / the desktop's file manager. Given a FILE, macOS
    and Windows select it in its folder; Linux has no portable 'select', so its folder is opened."""
    try:
        if is_windows():
            # /select, needs the file; for a directory plain startfile opens it
            if os.path.isdir(path):
                os.startfile(path)                              # noqa: B606 (Windows-only API)
            else:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif is_macos():
            subprocess.Popen(["open"] + ([] if os.path.isdir(path) else ["-R"]) + [path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def make_output_safe() -> None:
    """Stop a character from killing a command.

    Windows consoles hand Python whatever the active code page is — cp1252 on the GitHub runners —
    and printing a character it cannot encode raises UnicodeEncodeError from inside `print`. That is
    not a degraded line: it is an unhandled exception that ends the command. `collie init` died on a
    single U+2713 in "✓ codemap:", exiting 1 with its last line half-written, and every check that
    read its output failed for what looked like an unrelated reason.

    Prefer UTF-8, which modern Windows terminals render; fall back to replacing the characters that
    do not fit. Either way the command survives its own output.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if enc in ("utf8", "utf8mb4"):
                # Already fine, but a lone unencodable byte should still never be fatal.
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass                       # a stream that cannot be reconfigured (a pipe, a test double)


def play_stream(url: str):
    """Play an audio URL ON THIS MACHINE, in the background, and return the process (or None).

    collie could already FIND music — yt-dlp resolves a stream in a second — but only ever handed the
    URL to whichever screen asked, so a phone saying "play Cruel Summer" got a correct answer and
    silence. This is the missing half: the computer plays it.

    Headless players first (ffplay, mpv) because they make no window and can be stopped by killing
    them. Failing that, the platform's own opener, which always exists but takes over a window.
    """
    for argv in (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", url],
                 ["mpv", "--no-video", "--really-quiet", url]):
        if shutil.which(argv[0]):
            try:
                return subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                        **new_group_kwargs(), **no_window_kwargs())
            except Exception:
                continue
    try:
        if is_windows():
            os.startfile(url)                                 # noqa: B606 (Windows-only API)
            return None
        if is_macos():
            # QuickTime takes a URL directly and is always installed; `open <url>` would hand an
            # http link to the browser instead.
            subprocess.Popen(["osascript", "-e",
                              'tell application "QuickTime Player" to (open location %s)'
                              % _as_str(url), "-e",
                              'tell application "QuickTime Player" to play document 1'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return None
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None
    except Exception:
        return None


def stop_stream(proc) -> bool:
    """Stop what play_stream started. Only the headless players are ours to kill; a URL handed to
    QuickTime or a browser belongs to that app, and killing those would close a window someone may
    be using for something else."""
    if proc is None:
        return False
    try:
        kill_tree(proc)
        return True
    except Exception:
        return False


def ask_allow_deny(title: str, message: str, allow: str = "Allow", deny: str = "Not me",
                   timeout: int = 150) -> "bool | None":
    """Put a yes/no question in front of the person AT THE MACHINE, and wait for an answer.

    For decisions a page cannot carry: a pairing request is answered by whoever is sitting at this
    computer, and a card on a web page they may not have open is not asking them anything. This
    takes the front of the screen, which for a once-per-device security question is the point.

    Returns True/False, or **None** when there is no one to ask — a headless box, a machine with no
    GUI, or a timeout. None means "undecided", never "denied": the caller still has the in-app card,
    and turning an unanswerable prompt into a refusal would break pairing on servers entirely.
    """
    try:
        if is_macos():
            # osascript rather than a native alert, so this works the same from `collie web` in a
            # terminal as from the menu bar app; a bare Python process has no NSApplication to put
            # an NSAlert on.
            script = (
                'display dialog %s with title %s buttons {%s, %s} default button %s '
                'giving up after %d with icon caution'
                % (_as_str(message), _as_str(title), _as_str(deny), _as_str(allow),
                   _as_str(allow), timeout))
            out = subprocess.run(["osascript", "-e", script], capture_output=True,
                                 text=True, timeout=timeout + 15)
            if out.returncode != 0:
                return None                       # cancelled, no window server, or no one there
            # osascript answers `button returned:Allow, gave up:false`. Compare with the spaces
            # stripped from BOTH sides: stripping only the reply and then matching a needle that
            # still has one silently turns "nobody was there" into "denied" — the one reading this
            # must never produce, because it would refuse every pairing on an unattended machine.
            reply = (out.stdout or "").replace(" ", "")
            if "gaveup:true" in reply:
                return None
            return ("buttonreturned:" + allow.replace(" ", "")) in reply
        if is_windows():
            import ctypes
            MB_YESNO, MB_ICONWARNING, MB_SYSTEMMODAL, IDYES = 0x4, 0x30, 0x1000, 6
            # No timeout in the plain API; MessageBoxTimeoutW is undocumented, so this one waits.
            r = ctypes.windll.user32.MessageBoxW(   # type: ignore[attr-defined]
                None, "%s\n\n%s?" % (message, allow), title,
                MB_YESNO | MB_ICONWARNING | MB_SYSTEMMODAL)
            return True if r == IDYES else False
        # Linux: whichever of these the desktop actually ships, and nothing if it is headless.
        for argv in (["zenity", "--question", "--title", title, "--text", message,
                      "--ok-label", allow, "--cancel-label", deny, "--timeout", str(timeout)],
                     ["kdialog", "--title", title, "--yesno", message]):
            if not shutil.which(argv[0]):
                continue
            # zenity/kdialog are Linux-only, so this is a no-op there — but the scan reads the CALL,
            # and the call says only `argv`. Carrying the helper is cheaper than an exemption that
            # depends on someone remembering what the loop above it iterates over.
            out = subprocess.run(argv, capture_output=True, timeout=timeout + 15,
                                 **no_window_kwargs())
            return out.returncode == 0
        return None
    except Exception:
        return None


def _as_str(s) -> str:
    """An AppleScript string literal. Quotes and backslashes are the only things that can break out,
    and a device name comes off the network — so it is escaped, not interpolated."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


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
    """A one-line PLATFORM line for the agent's system prompt.

    ALWAYS states the OS and which shell `bash` actually runs, because a model that doesn't know
    guesses — and guesses wrong expensively. (Observed: on native Windows + Git Bash it assumed WSL,
    ran `ip route`/`/proc/version`, and burned several turns before discovering MINGW64.) Naming the
    environment up front costs ~20 tokens and removes a whole class of wrong-environment flailing."""
    if not is_windows():
        return "PLATFORM: %s — commands run in a POSIX shell." % os_label()
    sh = posix_shell()
    if sh:
        flavour = "Git Bash / MSYS2 (MINGW)" if ("git" in sh.lower() or "msys" in sh.lower()) else sh
        return ("PLATFORM: native Windows (NOT WSL, NOT Linux). `bash` runs through %s, so POSIX "
                "commands work, but paths are Windows paths (C:\\...) and Linux-only things "
                "(/proc, ip route, apt, systemd) do NOT exist. Use PowerShell via `powershell -Command` "
                "for Windows-native queries (services, registry, processes)." % flavour)
    return ("PLATFORM: native Windows with NO POSIX shell (Git Bash / WSL not found). Prefer the file "
            "and search tools (read_file / edit_file / code_search / glob / execute_code) over `bash`; "
            "the fallback shell is cmd.exe, so do NOT use ls, grep, cat, rm, find, or other Unix "
            "commands. Installing Git Bash restores full shell support.")
