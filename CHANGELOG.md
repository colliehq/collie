# Changelog

## v0.20.28 — the work that had been sitting unreleased

Nineteen commits had accumulated since v0.20.27 without a release, so the machine doing the
developing was running the version before all of them. This release is those commits, plus the two
test fixes that were needed to make the suite honest about them.

- **Checkpoints: one-click rollback of what the agent changed.** The tree is snapshotted before the
  agent edits it, wired through the run, the API and the UI, and the prompts stay out of the user's
  repository.
- **The verify gate stopped being Python-only.** "Prove it runs" was measured to be spinning on
  445 of 731 tasks, because the gate only recognised `python3 -c`. It now detects the language and
  requires a build before finish.
- **Benchmark honesty.** A local SWE-bench Pro grader and the first resolve numbers that follow the
  official protocol; the comparison harness stopped inventing wins; a provider outage is recorded as
  a missing observation rather than a loss; cost is measured on both arms including cache reads.
- **Settings apply on change, and say when one is being ignored** — a saved setting could not reach
  the process that reads it.
- **Running out of turns is no longer reported as "done".**
- **Two tests that could never have caught anything.** One asserted a directive that has never
  existed on this branch, and had been red since it landed. The other could not pass on Windows at
  all: it interpolated a path into generated source through two levels of string literal, so
  `C:\Users\…` became a truncated `\U` escape and the child died of SyntaxError before printing —
  and the assertion blamed the settings code it was meant to be testing.

## v0.20.27 — a tool name that made Collie unable to say anything

- **`mcp_status`, `mcp_add`, `mcp_set_enabled` and `mcp_remove` are now `mcpctl_*`, and that is the
  whole release.** The Anthropic API reserves the `mcp_<name>` shape for its own MCP connector and
  refuses any request that declares a tool with it — the entire request, HTTP 400, before a single
  token. Those four tools shipped in v0.20.21 and are registered unconditionally, so from that
  release on, **every message on the subscription path failed**. Nothing could be sent at all.
- **The error said something else entirely.** The refusal comes back as
  `invalid_request_error: "You're out of extra usage. Add more at claude.ai/settings/usage"` — a
  quota message for a naming problem, on an account whose 5-hour window was 8% used. It cost three
  wrong diagnoses before the request was bisected a variable at a time; the tool name was the only
  thing that mattered, and renaming it with the description and schema untouched fixes it.
  `mcp__server__tool` — the double-underscore form MCP servers' own tools use — is unaffected.
- **A test now refuses any tool named `mcp_<name>`,** because nothing about the failure points at
  the cause and the next person will not get there by reading the message.
- **Failures record the HTTP status and the rate-limit headers.** Only the body was kept, so a 400,
  a 429 and a 529 were indistinguishable afterwards — which is exactly why "is this us or them?"
  could not be answered from the record.
## v0.20.26 — releases that do not depend on one laptop being awake

- **The macOS build is signed from repository secrets, on a GitHub-hosted runner.** It had moved to
  a self-hosted Mac so signing could read the Developer ID out of that machine's login keychain.
  That bought one thing and cost two: every release waited on one laptop being awake, and a public
  repository — the one place GitHub advises against it — had a runner attached to it. v0.20.21 and
  v0.20.25 are what the first cost looks like: both were tagged, both queued against a runner that
  was not attached to the repository releases are cut from, and neither produced a single file.
  Everything in their notes below ships here.
- The dmg is still signed and notarised, and the build still refuses to cut a tag whose dmg is
  neither — that check is what makes reading the certificate from secrets safe rather than hopeful.

## v0.20.25 — the desktop stops fidgeting, and a chat that keeps up

- **The chat follows new output again, and can recover from a dropped connection.** Auto-scroll
  decided whether to follow *after* appending, so a block that landed whole — a finished answer, a
  tool card — was already past the 130px threshold by the time the check ran, and the page read its
  own new content as "the user scrolled away". Following stopped for the rest of the run and never
  came back. Separately, a dropped stream does not stop the run: recovery polled the saved session
  five times 1.5s apart, and accepted "the last message is an assistant" as proof of finishing —
  which mid-run matches the PREVIOUS turn, so the window replaced a live run with an older thread.
  It now re-attaches to `/api/mirror`, the run's own event bus, and judges completion against the
  turn count the run started with.
- **Settings holds still.** The panel was sized by its content, so switching category resized the
  dialog under the cursor (701px, then 403px, then 489px) and every keystroke in the search box
  resized it again.
- **The title bar follows the theme.** It is drawn by Windows, not by the page, so a dark UI sat
  under a white caption until the window was told — and the page now reports every theme change.
- **Opening Collie no longer flashes a console, every time.** The C# host was recompiled on every
  single launch: the canonical exe cannot be overwritten while an engine is running, so the swap
  failed, the exe stayed older than its source, and the next launch compiled it again — leaving an
  orphan `cw-build-*.exe` behind each time, one of which the app window was itself running as.
- **"No installed app matching 'Google Chrome'" on a machine with Chrome installed.** The Windows
  app list was six hardcoded paths labelled with the exe basename. It now comes from the Start Menu,
  which is Windows' own list of installed applications and names them the way people say them —
  4 apps became 153 on the machine this was found on.

## v0.20.24 — the model you picked, and a release that arrives

- **`mock` is no longer offered as a model.** It answers from canned text, which is
  indistinguishable from a model that has gone wrong, and it sat in the picker between real
  models where one tap silently replaces every future answer with a fixture. A machine already
  running on it still sees the row it is on, named as canned replies rather than as a model.
- **A model switch that cannot take effect says so.** `COLLIE_PROVIDER` set before Collie starts
  outranks the panel — deliberately, so `COLLIE_PROVIDER=x collie web` still means something.
  It used to do that in silence: the picker accepted the choice, wrote it, reported it back, and
  every run kept using the pinned provider. Now the desktop names the variable, and the phone
  shows that sentence above the list rather than under it.
- **v0.20.21 shipped nothing.** Its tag built on `[self-hosted, macOS, collie-mac]`, and no such
  runner was attached to the repository releases are cut from, so the run queued against a machine
  that did not exist. On the Mac that does exist, `actions/setup-python` then failed at
  `mkdir: /Users/runner: Permission denied` — its macOS package carries an install script with the
  hosted runner's path compiled in. The Mac jobs now use the machine's own Python — and only the
  Mac jobs: applying that same redirection to the hosted Windows runner broke its Python install
  and cost v0.20.22 its release in turn. Everything in the v0.20.21 notes below is in this one.

## v0.20.21 — MCP you can see, an extension that updates itself, settings you can navigate

- **MCP servers have a place in Settings.** Which servers exist, which are switched on, which are
  signed in, how many tools each one advertises — and a switch, a sign-in, and a remove. Previously
  the only way to manage MCP was to hand-write `~/.collie/mcp.json`; the panel did not mention it.
  You can add a server here too: one field that takes an `https://` URL or a command line.
  - Servers can now be switched **off** without deleting how they were set up, which is what you
    want when you are working out whether one of them is the thing causing a problem.
  - Collie can set servers up itself when a task needs one, but only after asking: adding a server
    means Collie choosing its own tools, and for a remote one, using your credentials. Reading the
    list and switching a server **off** never need permission — being able to disable something
    that is misbehaving should not require a permission dance.
  - A server added this way is usable immediately, without restarting Collie.
- **Collie can finish its own update.** Updating Collie updates the browser extension's files, and
  Chrome would go on running the old one — it never re-reads an unpacked extension by itself, and
  its extensions page cannot be automated. New browser tools appeared to be missing for no visible
  reason. Collie now reloads the extension and confirms which version came back, so "I updated" and
  "the browser changed" cannot come apart silently. One manual reload is still needed to adopt this,
  once.
- **Settings is a two-pane panel.** Twenty-six settings in a single scroll, cut into ten groups —
  four of which held a single row — meant scrolling past everything to reach anything. There is now
  a category rail and a search that spans all of it, following what desktop tools do here, because
  matching the habit matters more than being original.
- **It can see inside closed shadow roots.** Component-based sites put real controls in shadow roots
  created in "closed" mode, where the standard way of looking inside returns nothing — so those
  controls did not appear at all, and "not in the snapshot" reads exactly like "not on the page".
  Your pages are unaffected: nothing is forced open and `shadowRoot` still reads as the site built
  it.

## v0.20.20 — the browser tools stop reporting success they never had

This release comes out of watching collie try to work a real, unfamiliar web flow end to end and
fail — not for lack of intelligence, but because four of its browser tools could not fail. Each one
returned the same cheerful result whether it had worked or done nothing at all, so collie believed
them, built theories on top of them, and gave up on things it was actually able to do.

- **It can upload files.** New `browser_upload`: give it a path on your machine and it attaches the
  file to the page — profile picture, banner, video, any attachment, any format. This was previously
  impossible in a way that was nobody's fault and everybody's problem: the obvious move is to click
  the page's "choose file" button and drive the picker that appears, but **Chrome opens the OS file
  picker only for a genuine human gesture**, so an automated click opens no window at all. There was
  nothing to drive, and no error to explain why. Uploading now writes the file to the page's file
  input directly, which is how browser automation has always had to do it.
- **Typing is checked.** `browser_type` now reads the field back afterwards, and a write that landed
  nowhere is an error naming the routes that work, instead of a confident "typed". A silent no-op
  here is worse than a failure: it lets an empty form be submitted and believed.
- **An ambiguous click says it was ambiguous.** Clicking by visible text or a CSS selector takes the
  first match, and pages routinely hold several elements answering to the same name. When more than
  one matches, collie is told the count and the candidates, and pointed at snapshot refs, which are
  exact.
- **A truncated snapshot says it was truncated.** `browser_snapshot` caps how many elements it
  returns, and it walks the page in document order — so what falls off the end is whatever came
  last, which is exactly where a dialog that just opened lives. A cut-off list used to be
  indistinguishable from a complete one, which made a required control look like it did not exist.
- **It can see inside closed shadow roots.** Component-based sites put real controls inside shadow
  roots created in "closed" mode, where the standard way of looking inside returns nothing — so
  those controls did not appear in the snapshot at all, and "not in the snapshot" reads exactly like
  "not on the page". Collie can now see them. Your pages are unaffected: nothing is forced open,
  `shadowRoot` still reads as the site built it, and the visibility is one-way and collie's own.
- **macOS parity.** The Apple Events transport (the no-extension path on macOS) checks typing the
  same way the extension does, so the two never disagree about whether text landed.

## v0.20.19 — collie can look at things

- **It can see the screen.** Every perception collie had was a TREE — `browser_snapshot` returns the
  accessibility tree, `desktop_inspect` returns the UI Automation tree. That is the right primitive
  for acting, since you click a stable element rather than a pixel that moves with DPI and scroll,
  and it is why driving apps works at all. But it meant collie could never see what anything LOOKED
  like: whether a rendering is correct, whether a layout broke, what an app with no accessibility
  tree is showing. Two new tools close that, and the image genuinely reaches the model rather than
  being described to it — on a vision-capable model it is looked at; on a text-only one it degrades
  to a note instead of failing.
  - `screenshot` captures a native window — even one behind others or off-screen, without stealing
    focus — or the whole display. Zero new dependencies.
  - `browser_screenshot` captures the page as rendered. This is the right tool for anything web:
    the OS-level capture cannot see Chromium page content at all (it renders the window frame and an
    empty page, because the page is composited by the GPU process), and it needs the window
    unobscured, while this reads the page directly.
- **It will not hand you a picture of the wrong thing.** The fallback capture path reads screen
  pixels, so with another window in front it would return that window's contents labelled as the
  target — verified: capturing a covered browser returned the editor sitting on top of it. It now
  detects the occlusion and refuses, naming what to do instead. A wrong image presented as right is
  worse than no image.
- **Seeing is gated separately from acting**, and off by default. Desktop control can act, but a
  capture can read whatever happens to be on screen — a password manager, a bank tab, a private
  message — and the image then travels to whatever model is configured. Consent is asked for that
  specifically, in those words, rather than folded into the existing desktop permission.
- **Capabilities ask to be turned on when they are needed.** Gated tools are always registered now,
  so collie can see it HAS a hand or eyes and reach for them: it explains what the capability grants,
  and enables it only after you agree. Previously an off capability was simply invisible to it.
- **Clicks and uploads admit when they were a guess.** A page often has many elements matching the
  same text, and clicking the first was indistinguishable from clicking the right one — the tools now
  report how many matched. File uploads find the input themselves (including inside shadow roots),
  refuse when several exist rather than picking one, and read the result back, because assigning a
  file list is silently refused in some contexts and a refused upload looked exactly like a
  successful one. When a click opens a native OS dialog, collie is pointed at the desktop tools,
  which are the only thing that can drive one.

## v0.20.18 — the download Windows used to refuse

- **The Windows installer is signed.** Unsigned, Chrome and Microsoft Defender did not merely warn
  about `Collie-Setup.exe` — they called it a virus and blocked the download outright, which is the
  whole reason this project ships a plain Inno installer instead of the WebView2 shell it used to
  have. It is signed now, by Azure Artifact Signing, chaining to the Microsoft Identity Verification
  Root, so Windows names a publisher instead of refusing the file. SmartScreen still builds its
  reputation from real installs; what signing changes is that the reputation accumulates against one
  identity instead of starting from nothing with every release.
- **Nothing long-lived had to be stored to do it.** The release job authenticates to Azure over
  OIDC: GitHub mints a short-lived token, Azure exchanges it, and the identity behind it can do
  exactly one thing — sign with one certificate profile. The client and tenant ids in the workflow
  are identifiers, not secrets, which is what makes them safe to keep in a public workflow file. A
  stored client secret would not be: leaking one would let anybody sign code as the certificate
  holder.
- **The build will not publish an unsigned installer.** Signing can report success and leave a file
  untouched — that is how the macOS chain fooled us once — so a verify step runs
  `signtool verify /pa` afterwards and fails the build rather than letting the release through.
- **An empty search is no longer mistaken for proof.** Asked about a project living elsewhere on the
  machine, collie grepped the working directory alone, found nothing, and reported that the thing did
  not exist — while it sat two directories away, edited minutes earlier. Three rules now ride in the
  system prompt: widen the search and say what was actually searched before claiming absence, treat
  auto-recalled memory as a lead rather than a fact, and answer what you can determine yourself
  instead of opening with a list of questions.

## v0.20.17 — music you can stop without asking the agent

- **Three ways to stop the music, none of them a conversation.** Anything the agent starts that
  outlives the request has to leave behind a control that is NOT the agent, and music had none: the
  only ways out were to ask again or to kill a process in a terminal. Now there is a menu-bar item on
  macOS that appears only while something is playing and stops it in one click; a pill in collie's own
  UI, which is the control that exists on every platform; and the reply that starts music says where
  the off switch is, while you are still looking at it.
- **The player no longer outlives collie.** Kill collie mid-song and the music kept going with nothing
  anywhere that could stop it — it is started in its own session so a timeout can reap the whole tree,
  which also meant it did not die with us. Reaped on exit and on SIGTERM/SIGHUP now, installed from
  the main thread because signal handlers cannot be set from the HTTP worker that starts playback.
- **"Stop the music" stops the music.** The intent router used to answer `action=stop` and leave it to
  the caller's own player, which was right while the caller had one.
- **/api/desktop/nowplaying tells the two apart.** What the SYSTEM plays (Spotify, Music) is read-only;
  what collie plays can actually be stopped. Only the second gets a stop button — offering one for the
  first would be a lie.

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
