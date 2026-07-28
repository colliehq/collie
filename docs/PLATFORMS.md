# Platforms — cross-platform support & per-OS setup

Collie is **one cross-platform Python codebase**, not a per-OS fork. Forking into a
"Windows version" and a "macOS version" would triple the maintenance of what is
essentially one program (Python + a web UI + a Chrome extension — all inherently
portable). Instead, the small number of genuinely OS-specific operations live behind a
thin abstraction, `harness/plat.py`, and the same wheel runs everywhere.

## The abstraction: `harness/plat.py`

Everything platform-specific is one module, so the rest of the harness stays portable.

| Function | POSIX (Linux/macOS/WSL) | Windows |
|---|---|---|
| `posix_shell()` | `/bin/sh` | Git Bash → MSYS2 → Cygwin (skips the `System32\bash.exe` WSL stub); `None` if none found |
| `shell_argv(cmd)` | `(cmd, shell=True)` via `/bin/sh` | `([bash, "-c", cmd], shell=False)` if a POSIX shell exists, else `(cmd, shell=True)` → cmd.exe |
| `kill_tree(proc)` | `killpg(getpgid(pid), SIGKILL)` — reaps the session + all grandchildren | `taskkill /F /T /PID` — walks the PID tree |
| `new_group_kwargs()` | `{"start_new_session": True}` (own process group) | `{}` (taskkill /T handles the tree) |
| `rmtree(path)` | `shutil.rmtree` (was `rm -rf`) | `shutil.rmtree` |
| `open_excl(path)` | `O_CREAT\|O_EXCL\|O_WRONLY \| O_NOFOLLOW` (symlink-planting guard) | same, minus `O_NOFOLLOW` (absent on Windows) |
| `chmod_private(path)` | `chmod 0600` (owner-only) | no-op (Windows ACLs differ) |
| `to_host_path(p)` | identity | — (only WSL differs; see below) |
| `shell_hint()` | "" (the model's Unix habits are correct) | "" when a POSIX shell is present; only when none is found does it steer to the file/search tools |

**The shell contract.** collie routes every shell call — the `bash` tool, `grep`, `pack --check`,
`loop --until` — through `shell_argv()`, so one POSIX dialect works identically on every OS. On
Windows that means a real bash (Git Bash / MSYS2 / Cygwin); the WSL `System32\bash.exe` launcher is
deliberately skipped because it runs commands inside the Linux filesystem with different cwd/path
semantics. Where no POSIX shell is found, `bash` degrades to cmd.exe and `shell_hint()` steers the
model toward the native file/search tools — but installing Git Bash restores full parity.

Detection: `is_windows()`, `is_macos()`, `is_wsl()`, `os_label()`. Nothing branches at
import time — each call checks the live OS, so a single build degrades gracefully where a
primitive is absent (e.g. `chmod` becomes a no-op rather than a crash).

Pinned by `tests/test_plat.py` (runs on the current OS; the cross-OS branches are asserted
structurally, so the same test is meaningful on Linux, macOS, and Windows).

## Support matrix

| OS | Core agent | Browser bridge | Notes |
|---|---|---|---|
| **Linux (native)** | ✅ | ✅ same-OS localhost | the primary development target |
| **macOS (native)** | ✅ | ✅ same-OS localhost, or **no extension at all** via Apple Events | POSIX; the *simplest* bridge setup |
| **Windows (native)** | ✅ (Git Bash) | ✅ same-OS localhost | full parity with Git Bash; degrades without it — see "Windows" below |
| **WSL2** | ✅ | ⚠️ cross-OS | Windows Chrome ↔ WSL server; see "WSL" below |

## The browser bridge, per OS (the one real platform nuance)

Collie drives your **real, logged-in** browser through a bridge: `collie browser-bridge`
runs a localhost server; the extension in `harness/browser_ext/` (loaded into your Chrome)
long-polls it and runs `browser_*` actions in your actual session. Where Collie and Chrome
sit relative to each other is the only thing that changes:

- **Native (Linux / macOS / Windows)** — Chrome, the extension, and the bridge all run on
  the **same OS**. Plain `127.0.0.1` works. This is the simplest case:
  1. `collie browser-bridge`
  2. Chrome → `chrome://extensions` → *Developer mode* → *Load unpacked* → `harness/browser_ext/`
  3. run Collie with `COLLIE_BROWSER_BRIDGE=1`
  (or `collie browser-bridge --browser` to launch a managed Chromium with the extension
  pre-loaded — a fresh profile, not your login, for dev/CI.)

- **macOS, without installing anything** — Apple Events can drive your already-open,
  already-logged-in tab, so the extension is optional here. If no extension answers, collie
  falls back to it automatically. One-time setup, in place of loading an extension:
  **Chrome → View → Developer → Allow JavaScript from Apple Events** (Safari: *Develop →
  Allow JavaScript from Apple Events*), plus the macOS Automation prompt on first use.
  That toggle is a security setting — it lets local processes run JavaScript in your
  logged-in browser — so it is deliberately left for you to enable.

  The fallback is a *transport*, not a second implementation: the page-side functions are
  read out of `browser_ext/background.js` and injected as-is, so the two paths cannot drift.
  `browser_console`, `browser_eval`, `browser_pick` and upload still need the extension —
  console requires Chrome's `debugger` permission (CDP), and the others are async page
  functions that Apple Events cannot await. They say so rather than returning something wrong.
  Disable the fallback with `COLLIE_NO_APPLE_EVENTS=1`.

- **WSL2** — the *hardest* case, because Collie runs in WSL (Linux) while Chrome is Windows.
  WSL2's `localhost` forwarding is one-directional and flaky, so:
  - bind the bridge to the LAN IP: `COLLIE_BROWSER_BRIDGE_HOST=0.0.0.0 collie browser-bridge`,
    and point the extension at the WSL IP (`hostname -I`);
  - paths handed to Windows Chrome are converted with `wslpath` automatically
    (`plat.to_host_path`).
  This is why the same setup that feels fiddly under WSL is trivial on a native OS — the
  cross-OS boundary is a WSL artifact, not a Collie limitation.

## Windows (native) specifics

The core agent runs on native Windows, with two things to know:

1. **The shell is a POSIX shell.** collie discovers a real bash — **Git Bash**, MSYS2 or Cygwin —
   and routes the `bash` tool, `grep`, `pack --check` and `loop --until` through it (`plat.shell_argv`),
   so `ls`, `grep`, `;`, `&&`, pipes and heredocs behave exactly as on Linux/macOS. Git Bash ships
   with **Git for Windows** (which most devs already have) and is preinstalled on GitHub's
   `windows-latest` runner, so CI exercises the same commands there. If no POSIX shell is found,
   `bash` falls back to cmd.exe and `plat.shell_hint()` steers the model toward the native,
   cross-platform tools (`read_file`, `edit_file`, `code_search`, `glob`, `execute_code`) — install
   Git Bash to restore full parity. (Prefer to avoid the WSL `System32\bash.exe` launcher: collie
   skips it on purpose, since it runs commands in the Linux filesystem with different path semantics.)
2. **Process/file primitives** are handled by `plat` (`taskkill /T` for timeouts, no-op
   `chmod`, `O_NOFOLLOW` omitted) — no action needed.

## Install (every OS)

Windows has a one-click `Collie-Setup.exe` (see the releases page). Everywhere else — and for
developers on Windows — it's one `pip` install (PyPI publish is planned; today, from a clone):

```bash
pip install -e .                     # one package, all platforms
# optional extras:
pip install -e ".[tui,local,search,browser]"
```

Optional native conveniences (not required — the wheel is the source of truth) can be layered
on later without forking the code: a Homebrew formula (macOS), an installer + Task-Scheduler
autostart for `colliejobd` (Windows), a `.deb` + systemd unit (Linux).
