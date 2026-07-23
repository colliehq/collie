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
| `kill_tree(proc)` | `killpg(getpgid(pid), SIGKILL)` — reaps the session + all grandchildren | `taskkill /F /T /PID` — walks the PID tree |
| `new_group_kwargs()` | `{"start_new_session": True}` (own process group) | `{}` (taskkill /T handles the tree) |
| `rmtree(path)` | `shutil.rmtree` (was `rm -rf`) | `shutil.rmtree` |
| `open_excl(path)` | `O_CREAT\|O_EXCL\|O_WRONLY \| O_NOFOLLOW` (symlink-planting guard) | same, minus `O_NOFOLLOW` (absent on Windows) |
| `chmod_private(path)` | `chmod 0600` (owner-only) | no-op (Windows ACLs differ) |
| `to_host_path(p)` | identity | — (only WSL differs; see below) |
| `shell_hint()` | "" (the model's Unix habits are correct) | steers the agent to the file/search tools, away from `ls/grep/rm` |

Detection: `is_windows()`, `is_macos()`, `is_wsl()`, `os_label()`. Nothing branches at
import time — each call checks the live OS, so a single build degrades gracefully where a
primitive is absent (e.g. `chmod` becomes a no-op rather than a crash).

Pinned by `tests/test_plat.py` (runs on the current OS; the cross-OS branches are asserted
structurally, so the same test is meaningful on Linux, macOS, and Windows).

## Support matrix

| OS | Core agent | Browser bridge | Notes |
|---|---|---|---|
| **Linux (native)** | ✅ | ✅ same-OS localhost | the primary development target |
| **macOS (native)** | ✅ | ✅ same-OS localhost | POSIX; the *simplest* bridge setup |
| **Windows (native)** | ⚠️ runs | ✅ same-OS localhost | no POSIX shell — see "Windows" below |
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

1. **No POSIX shell.** The agent's habit is to emit Unix commands (`ls`, `grep`, `cat`,
   `rm`, `find`). On Windows `bash` maps to PowerShell/cmd, where those fail. Collie mitigates
   this by (a) leaning on the **native, cross-platform tools** — `read_file`, `edit_file`,
   `code_search` (ripgrep), `glob`, `execute_code` — which cover most work without a shell,
   and (b) injecting `plat.shell_hint()` so the model prefers those tools and avoids Unix
   commands. `bash` remains a rarely-needed escape hatch. For a heavy shell workflow, install
   **Git Bash** (or run Collie under **WSL2**) to get a POSIX shell.
2. **Process/file primitives** are handled by `plat` (`taskkill /T` for timeouts, no-op
   `chmod`, `O_NOFOLLOW` omitted) — no action needed.

## Install (every OS)

```bash
pipx install collie-harness          # one package, all platforms
# optional extras:
pipx install "collie-harness[tui,local,search,browser]"
```

Optional native conveniences (not required — the wheel is the source of truth) can be layered
on later without forking the code: a Homebrew formula (macOS), an installer + Task-Scheduler
autostart for `colliejobd` (Windows), a `.deb` + systemd unit (Linux).
