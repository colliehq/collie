# Changelog

## v0.20.3 — the app opens, and collie can update itself

- **Double-clicking Collie.app now opens a window.** It never did on macOS: `collie app`
  gave up on a native window off Windows and fell through to the browser path, which has
  no terminal and nothing to attach a browser to when launched from a bundle — so the
  server started, zero windows were created, and the Dock icon bounced until macOS gave up.
- **`collie update`** checks for a newer release and installs it with `--yes`, into whichever
  install this is (app / Windows setup / brew / pip). Nothing installs unverified: on macOS
  the dmg must satisfy Gatekeeper *and* carry our Developer ID, and the app inside is checked
  again after mounting; on Windows, where the installer is not code-signed, GitHub's published
  sha256 is required. An unsigned image, a dmg with 64 bytes changed, and an exe with 8 bytes
  changed are all refused, each saying what was wrong.
- **`collie uninstall`** — macOS had no uninstaller, so dragging the app to the Trash left
  ~/.collie behind (179 MB on the machine this was written on) plus the Screen Recording,
  Camera and Microphone grants, listed under an app that no longer existed. It lists
  everything first and deletes nothing without `--yes`.

## v0.20.2 — the macOS desktop actually works

The macOS bundle shipped with its whole desktop backend inert: six features were written
against Windows-only APIs and wrapped in `except Exception`, so they returned False without
a word. Opening an app, opening a project, the launcher's contents, every icon, and the
yt-dlp download were all affected, plus fourteen call sites passing a Windows-only
`creationflags`.

- **Apps open.** `/usr/bin/open` and `xdg-open`; `/Applications` is scanned (103 apps on the
  machine this was found on) instead of a list of `C:\` paths; icons come from the bundle's
  `.icns` via `sips` rather than PowerShell.
- **Music is 10x faster and finds playable tracks.** The platform yt-dlp binaries unpack 38MB
  on every run — `--version` alone took 20 seconds — so the pure-Python zipapp is used
  instead: 40s+ down to 4.3s. 24/7 livestreams are dropped rather than down-ranked, since
  they offer no audio-only format and "lofi" matches nothing else.
- **The composer routes desktop intents** — open an app, ask about this machine, open a
  project, stop the music — instead of handing everything to the coding agent.
- **Chinese lyrics match the song playing.** The guard compared word tokens, which Chinese
  does not have, so 太阳之子 and 太陽之子 looked unrelated and another song's lyrics came back.
- **The desktop is a desktop.** It sits one level below every app window, so it can never
  cover your work; double-clicking empty space reveals the desktop, which the window would
  otherwise have swallowed.
- **`tests/test_platform_purity.py`** refuses unguarded Windows-only APIs outside `plat.py`.
  It caught new code on its first day.

## v0.20.1 — the macOS download

A signed, notarised **`Collie-arm64.dmg`** now ships alongside `Collie-Setup.exe`: double-click,
drag to Applications, done. No Python, no terminal, no Gatekeeper warning.

The bundle already existed; it did not work, and every check it had said it did.

- **Compiled extensions could not load.** The bundled interpreter is the process, so it is what
  library validation judges, and it was signed with the hardened runtime and no entitlements —
  `onnxruntime`, `tokenizers` and all of `pyobjc` failed to import. Semantic memory silently
  degraded to keyword search. Now 10/10 import.
- **The signature broke on first launch.** `.pyc` was stripped as build detritus; a signed `.app`
  is sealed, so the first run wrote 242 of them back and Gatekeeper began refusing the app *on the
  user's machine*. Bytecode is precompiled into the bundle, and the launcher cannot write to it.
- **The disk image was unsigned.** It notarised, stapled, and `stapler validate` reported success
  on a file nobody could open — only `spctl` tells you. The dmg is signed before notarisation now.
- **The build asks Gatekeeper for a verdict** and exits non-zero if it is refused. `codesign
  --verify` passes happily on all three failures above.
- **Releases are arm64 only**, and cross-building is refused rather than silently producing a
  payload that cannot be smoke-tested on the machine that built it.

## v0.18.0 — first public release

- **One-click Windows installer** (`Collie-Setup.exe`) — bundles a self-contained runtime
  (Python + Collie + semantic memory), the native desktop window, and the browser bridge.
  No Python, no terminal, no configuration.
- **Verification gate (`assert-verify`)** — Collie writes a reproduction that must fail on
  the broken code, makes the smallest edit that flips it, and re-runs the assertion before a
  task is called done.
- **Terminal-first, editor-anywhere** — `collie` (TUI), `collie web` (browser GUI with the
  live gate, diffs, and settings), and `collie acp` for Zed / JetBrains / neovim / VS Code
  over the Agent Client Protocol.
- **Local-first & model-agnostic** — bring your own subscription or API key (Anthropic,
  OpenAI-compatible presets, Ollama), or run fully local. No account, no telemetry.
- **Built in** — hybrid semantic memory, `code_search`, keyless web search, MCP support, a
  best-of-N `pack` mode, an autonomous `loop` that stops on a real green check, and a
  real-browser bridge that drives your logged-in Chrome/Edge.

MIT-licensed · runs locally · <https://collie.run>
