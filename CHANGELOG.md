# Changelog

## v0.20.15 — /api/repos could hang forever

- **A directory walk that never returns no longer takes the endpoint with it.** `~/Music` and
  `~/Movies` are the Apple Music and TV libraries; full of cloud placeholders, `os.walk` over one
  does not come back. `/api/repos` had not finished after five minutes on a real machine — which
  from the phone is a Code screen spinning with nothing to time it out, and a server thread gone
  for good. Those names are pruned at the top of $HOME, and the endpoint answers within a deadline
  whatever the filesystem does, because the next one will have a different name.

## v0.20.14 — the desktop could not tell it had gone offline

- **A dead relay socket is noticed now.** The desktop reported `connected: true` while the relay
  answered "desktop offline" to the phone. Nothing had raised and nothing was wrong with the
  network: the socket was still writable, so every keepalive ping succeeded and the client went on
  believing it was connected, indefinitely, until someone restarted it by hand. Pinging only proves
  the local socket accepts writes — the far end's PONG is the evidence, and the transport was
  discarding it. It is timestamped now, and two missed replies close the socket so the existing
  reconnect can do its job.

## v0.20.13 — your chats were being written inside the application

- **User data moved out of the install.** `data/` — sessions, memory.db, runs.db, the sandbox —
  resolved to wherever `harness` happened to be installed. From the .app that is inside the signed
  bundle, which is read-only: nothing could be saved at all, so the app showed "no chats yet" no
  matter how much you had said, and every run was forgotten the moment it ended. A writable bundle
  would have been worse — each update replaces it and would take the history with it. From pip it
  landed in site-packages, which the next upgrade deletes. A checkout keeps its own `data/`;
  everything else now writes beside `settings.json` and `remote.json`. `COLLIE_DATA_DIR` overrides
  both.
- **The Dock says Collie.** The bundle execs its private interpreter, and macOS names a process
  after the file it executed — so the Dock, Force Quit and Activity Monitor all said "python3".

## v0.20.12 — the Dock said "python3"

- **The app is called Collie everywhere now.** The bundle hands off to its private interpreter
  directly, and macOS names a process after the file it executed — so the Dock, the Force-Quit
  list and Activity Monitor all said "python3", undoing the reason this is a bundle at all.
  The interpreter gets a second name beside itself and the launcher execs that. Setting the name
  from inside (NSProcessInfo, CFBundleName) changes nothing System Events reports, and a hard
  link takes the name but kills the interpreter — CPython finds its stdlib by walking up from the
  path it was executed as. A symlink resolves back to the real file first, so the prefix comes out
  right and the name still sticks.
- **Windows: a command could no longer be killed by one character.** `print` raises on a console
  that cannot encode what it is given — cp1252 has no U+2713 — so a single tick mark in
  "✓ codemap:" ended `collie init` with exit 1 and half a line written. Output is reconfigured
  before anything prints. This was never about init: any command with a glyph was one console
  away from dying.

## v0.20.11 — the app window was pointed at a dead port

- **`collie app` opened, bounced, and showed nothing.** The server scans forward when its
  preferred port is busy, and kept the port it settled on to itself — so the window was sent to
  the one that was *asked for*, which by then belonged to nobody. It probed that port for twelve
  seconds before giving up, which is the bouncing. Relaunching made it worse, not better: the
  abandoned server held its port, so the next launch landed one further along and missed by one
  more. `main()` now reports the port it bound.
- **Music plays on this computer.** collie could already find a track in about a second, but only
  ever handed the URL to whichever screen asked — so a phone saying "play Cruel Summer" got the
  right answer and silence. `/api/desktop/play` plays it here, and stops it.
- **Commands go into the conversation.** A request the intent router carried out itself left no
  trace anywhere. It is a fast path, not a separate place for things to happen, so what it does is
  now written into the chat it was typed in — starting one if the command came first.
- **An encrypted phone survives a desktop restart.** `K_dev` lived only in memory while the
  keypair was regenerated per process, so restarting `collie web` left every paired phone unable
  to open a single frame — reported as an opaque 5xx. It persists in the device store now, as
  E2E_DESIGN.md §7 always said it should, and a device whose key is genuinely gone is told to pair
  again instead of being shown a number.
- **Pairing asks on this screen.** The approval card lived on a page nobody has open at the moment
  a phone scans. A device asking for the run of your computer now interrupts, once.
- **`desktop_*` tools on macOS.** The driver was already there; only Windows was wired to it.

## v0.20.4 — the app is an app, and collie can drive your other ones

- **`collie app` opens an ordinary window.** v0.20.3 fixed it opening nothing by reusing the
  desktop's window, which over-corrected: borderless, no close button, no Dock tile. It now
  has its own — titled, closable, in the Dock and in Cmd-Tab, closing it quits. The live
  desktop stays opt-in, under `collie wallpaper`.
- **App control on macOS.** The Windows build has driven apps through UI Automation for a
  while; macOS now does the same through System Events, with no new dependency. Listing your
  apps and windows, switching to one, quitting one and hiding the rest need **no permission
  at all**; only reading or clicking a window's controls asks for Accessibility, and a denial
  says so by name instead of returning an empty result.
- **The desktop composer understands more.** "switch to Xcode", "quit Safari", "what do I
  have open" now do the thing rather than starting a coding session about it.

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
