<p align="center">
  <img src="assets/collie-logo.svg" width="120" height="120" alt="Collie logo">
</p>

<h1 align="center">Collie</h1>

<p align="center">
  <b>A coding agent that lives on your computer — and can actually run it.</b><br>
  <sub>Local and private. It reaches your real environment — your logged-in browser, your desktop,
  your screen, your files — and proves its work by running it.</sub>
</p>

<p align="center">
  <a href="https://collie.run">collie.run</a> ·
  <code>collie -p "fix the bug"</code> ·
  <code>collie web</code>
</p>

---

Most coding agents live in a cloud tab or an editor pane and can only touch the files you hand
them. **Collie runs on your machine** — so it works the way you already do: it drives your *real*
logged-in browser, arranges your desktop, records your screen, takes tasks from your phone, and
edits your code. Nothing leaves your computer unless you send it there; there's no account and no
telemetry.

And it doesn't just *claim* to be done. When Collie fixes something it writes a reproduction that
must fail on the broken code, makes the smallest edit that flips it, and re-runs the assertion — a
run isn't "done," it's **verified ✓**.

## Why it's different

**It's local, and it reaches your real world.** A cloud agent can read a repo. Collie can open the
site in the browser you're already logged into, click through the actual flow, watch what happens on
your screen, and change the code — all on one machine, all under your control. That's a different
class of task: not "edit these files," but "get this working, end to end."

**The range is the proof.** Collie isn't a coding agent with a pile of unrelated features bolted on.
The breadth below — the desktop console, the browser control, the screen recorder, the phone remote —
is there because **Collie's coding agent built all of it.** The features *are* the benchmark: a
harness strong enough to ship its own desktop app and iOS companion is strong enough for your bug.

## The range

| | Capability | What it means |
|---|---|---|
| 🧠 | **Coding agent** | Semantic code navigation, syntax-gated edits, and a self-verifying repair loop — the core, covered below. |
| 🌐 | **Your real browser** | A Chrome extension lets Collie act *in your logged-in browser* — the real session, real cookies — so it can operate sites, not just scrape them. Every action is a fenced, CSRF-checked localhost call. |
| 🖥️ | **Living desktop** | `collie web` powers an interactive ambient wallpaper: clock, weather, an app dock, projects, a music player (real audio + synced karaoke lyrics), and a command bar — all agent-manageable via one JSON config. When Collie is working, the wallpaper becomes a live star-map of your code. |
| 🎬 | **Screen recorder** | `collie record` captures screen + camera + mic (Windows and macOS) — a built-in way to demo or document a run. |
| 📱 | **Phone remote** | Pair once by scanning a code; then tail runs and start new ones from your phone — on the same Wi-Fi (`--lan`) or anywhere through a relay (`--remote`), with the companion iOS app. |
| 🔌 | **Everywhere else** | Terminal, browser GUI, VS Code, and any ACP editor (Zed/JetBrains/neovim) — one harness, every surface. |

## Where it runs

Collie is **terminal-first** and reaches editors through an open protocol, not a bespoke extension:

| Surface | Command | Reaches |
|---|---|---|
| **Terminal** | `collie` (TUI) · `collie -p "task"` | anywhere — SSH, CI, tmux |
| **Browser GUI** | `collie web` | chat, the live verification gate, diffs, the star-map, the ambient desktop, settings |
| **iPhone** | `collie web --lan` (same Wi-Fi) or `--remote` (anywhere, via the relay) + the companion app | scan the pair code once, then run from the phone |
| **VS Code** | the bundled `vscode-collie` extension | Collie docked in a sidebar panel (manages its own server) |
| **Editors (ACP)** | `collie acp` | Zed · JetBrains · neovim · VS Code — one adapter, every [ACP](https://agentclientprotocol.com) editor |
| **Streaming / CI** | `collie run "task" --stream-json` | NDJSON events (tool · edit · repro-gate · receipt) |

## Install

**Windows — one click.** Download **`Collie-Setup.exe`** from the
[latest release](https://github.com/colliehq/collie/releases/latest) and double-click it. A small
app-style installer lays down a self-contained runtime (Python + Collie + semantic memory, nothing to
preinstall) and opens Collie in a native desktop window. On first launch you **pick a brain** — an
existing Claude, Codex, or Grok login is detected and connects in one click; or paste an API key.

**macOS / Linux — pip.** The core is stdlib-only, so the base install is tiny:

```bash
pip install -e ".[local,dev]"      # from a clone (PyPI publish is planned)
collie setup                       # optional deps, pre-download the memory model, pick a provider
collie                             # the terminal chat (TUI) opens
```

No account, no telemetry, and the core has **zero third-party dependencies** — `mock` and `ollama`
run without any key, and memory works out of the box on BM25 keyword recall.

Optional extras: `pip install ".[local,tui,search]"` — `local` (semantic memory: granite-107m via
onnxruntime, ~55MB, multilingual), `tui` (rich terminal chat), `search` (keyless web search), `acp`
(editor protocol), `browser` (Playwright — only for `collie browser-bridge --browser`, a managed
Chromium with the extension preloaded, for CI or when you'd rather not use your own Chrome). Per-OS
setup — especially the real-browser bridge (`collie browser-bridge` + `harness/browser_ext/`) — is in
**[docs/PLATFORMS.md](docs/PLATFORMS.md)**.

## Quickstart

```bash
collie                     # terminal chat (TUI); first run picks a provider
collie web                 # browser GUI — chat, live gate, diffs, star-map, ambient desktop
collie selftest            # $0 deterministic end-to-end (mock model, real tools + memory)

# a real cheap model (provider key in env)
DEEPSEEK_API_KEY=... collie -p "fix the off-by-one in utils/timeparse.py"

# machine-readable / streaming
collie run "fix the bug" --json          # final result object (tokens, cost, verified)
collie run "fix the bug" --stream-json   # live NDJSON: tool · edit · repro-gate · receipt

# fully local, no key
collie run "summarize app.py" --provider ollama --model qwen2.5-coder:7b

# autonomous loop: iterate toward the goal, STOP the first turn an executed check goes green
collie loop --goal "get the suite passing" --until "pytest -q" --max 8

# best-of-N with EXECUTION-based selection: run N isolated attempts, keep only what passes
collie pack "fix the failing test" -n 3 --check "pytest -q" --apply

collie acp                 # serve as an ACP agent (an editor spawns this over stdio)
```

Providers: `mock`, `ollama`, `anthropic`, `anthropic-oauth`, and OpenAI-compatible presets
`deepseek` · `qwen`/`dashscope` · `openrouter` · `moonshot` · `groq` · `zhipu` · `openai`.

---

# For developers

Everything above rests on a small, honest harness. This is what's under it.

## The signature: the verification gate

```
  locate   code_search "parse_duration compound units"   · 4 hits
           › utils/timeparse.py:42  _parse  ············· 0.91

  repro    wrote repro.py · assert parse_duration("1h30m") == 5400
           ✗ FAILING  › got 1800, want 5400              ← gate armed

  edit     utils/timeparse.py  ································· +1 −1
           43 │- total = SECONDS[unit] * int(val)
           43 │+ total += SECONDS[unit] * int(val)

  verify   python repro.py
           ✓ PASSING  › parse_duration("1h30m") == 5400   ← gate green

  ✓ verified in 12.8s · Δ +1 −1 · 3,410 tok · $0.006
```

Other agents check "did the test not error." Collie's gate is stronger: the reproduction carries an
`assert actual == expected` derived from the issue, so a plausible-but-wrong edit fails *loudly* and
drives another repair round. This **`assert-verify`** loop is the core of the harness — a wrong edit
never silently ships as "done." The same idea scales up: `collie loop` stops when a real shell check
exits 0, and `collie pack` picks the best of N attempts by what actually passes.

## What it asks before doing

Collie reaches further than a cloud agent — your logged-in browser, your desktop, your files — so
it draws a line and asks before crossing it. Every tool declares how far it reaches
([`harness/risk.py`](harness/risk.py)), and `collie risk` prints the whole table:

| | | |
|---|---|---|
| **read** | no side effects | never asks |
| **write_local** | changes files here | inside your directory: goes ahead |
| **exec** | runs commands here | inside your directory: goes ahead |
| **external** | **leaves this machine** — your logged-in browser, your desktop, an MCP server | **asks, every time** |

**Running `collie` in your repo is the consent** for the middle two. That is the whole point of the
default `project` mode: an agent that interrupts every `pytest` is not usable, and asking about work
you already asked for is theatre. What you did *not* consent to by launching it is `browser_click`
sending mail under your cookies — so that asks.

```bash
collie -p "fix the bug"                  # project (default)
collie -p "..." --mode plan              # read-only: explore and propose, change nothing
collie -p "..." --mode interactive       # ask before every write and command too
collie -p "..." --mode auto              # ask nothing (sandboxes, CI)
collie risk                              # what collie can reach, grouped by how far
```

Two things worth knowing:

- **"Always allow" is pinned to a target, never to a tool.** Approving clicks on
  `http://localhost:5173` does not approve clicks on your bank — the rule is
  `browser_click → http://localhost:5173`, the origin is re-read live on every call, and it lasts
  one run. There is deliberately no way to express "always allow browser_click".
- **Nobody there means no.** Piped, in CI, or with no terminal, off-machine calls are refused with
  a reason the model can work around, rather than run because no one objected. Unattended does not
  raise the ceiling; it only changes who can answer.

In an editor, this is the editor's own prompt: collie speaks ACP's `session/request_permission`, so
Zed / JetBrains / neovim render their native approval UI.

## Architecture (abstractions & seams)

```
                      ┌──────────────── loop.Harness ────────────────┐
   task ─────────────▶│  compose → complete → run tools → verify ✓   │
                      └──┬──────────────┬──────────────┬─────────────┘
     ┌───────────────────┘              │              └───────────────────┐
     ▼                                  ▼                                  ▼
 ContextComposer                  ModelProvider                     ToolRegistry
 STABLE/CONTEXT/VOLATILE          OpenAI-compat · Anthropic ·       read/write/edit/bash/
 + token budgeter                 Ollama · subscription-OAuth       grep/glob + code_search
     ▼                                  │                                  │
 memory.SqliteMemory                    ▼                            recorder.Recorder
 hybrid recall (BM25+dense+RRF)   emit → stream-json / ACP          runs.db (+ dashboard)
```

| Seam (abstract base) | shipped impl |
|---|---|
| `ModelProvider` | **OpenAICompat** (DeepSeek/Qwen/GLM/OpenRouter…) · Anthropic · Ollama · subscription-OAuth |
| `ToolRegistry` | read/write/**edit** (syntax-gated) · bash · grep · glob · **`code_search`** · **`web_search`** + **`web_fetch`** (keyless) · **`plan`** · **`undo`** · browser · **MCP** (deferred tier + `load_tools`) |
| `EmbeddingProvider` | **OnnxEmbedding** granite-107m (Apache, 55MB, multilingual) · bge-m3 / e5 · jina-v3 opt-in · **BM25-only** when no model |
| `SqliteMemory` | CORE + facts + FTS5 + cosine, hybrid RRF + optional rerank + consolidation |
| `ContextComposer` | STABLE/CONTEXT/VOLATILE + auto-prefetch · a ~1K-token fixed prefix (kept deliberately lean) |

**`code_search`** extracts the identifiers from a natural-language query and greps the repo (ripgrep,
else grep), ranking files by how many of your terms each contains — so the agent reasons about *where*
to edit instead of grepping blind, with no model and no index to go stale. **`edit_file`** is
exact-match, whitespace-tolerant, and **rejects any edit that would break Python syntax**.
Untrusted web/page content is **fenced as data** (prompt-injection defense), and the browser bridge
refuses any request missing its CSRF header. A token/cost **budget**
(`COLLIE_MAX_COST` / `COLLIE_MAX_TOTAL_TOKENS`) stops a run at a ceiling.

## Platforms

One cross-platform Python codebase — **not** a per-OS fork. The handful of operations that genuinely
differ (kill a process tree on a timeout, secure a token file, convert a path, choose a shell) are
isolated in `harness/plat.py`, so the same wheel runs everywhere.

| OS | Status | Notes |
|---|---|---|
| **Linux** | ✅ native | the primary target |
| **macOS** | ✅ native | POSIX; the browser bridge is *simplest* here (Chrome + Collie on one OS) |
| **Windows** | ✅ one-click | the packaged installer; the agent prefers the file/search tools over `bash` |
| **WSL2** | ✅ | a Windows-Chrome ↔ WSL bridge uses the LAN IP + `wslpath` (handled for you) |

## Benchmark lab (built in)

Collie measures itself against other harnesses on the **same** task and model — you run it yourself;
no numbers are asserted here:

```bash
DEEPSEEK_API_KEY=... python swe_run.py --n 5                 # SWE-bench Verified (needs Docker)
python -m bench.multirun_eval                                # pass@1 / pass@k / Wilson CI / McNemar
python -m bench.polyglot_eval --langs python,cpp,javascript  # Aider-Polyglot, multi-language
python -m harness.cli compare --vs all                       # vs Claude Code / Aider / …
```

## Honesty & policy

- The benchmark harness is version-tagged and reproducible. "Progress is a number" cuts both ways —
  Collie surfaces the levers that turn out **net-neutral**, not just the wins.
- Token counts are real usage (the model's own `usage`, or `harness/apitap.py` metering for CLIs that
  report none) — apples-to-apples, same source both sides.
- Collie draws a personal Max/Pro subscription only through the first-party OAuth path
  (`anthropic-oauth`), the same mechanism the official CLI uses; it never scrapes or resells
  subscription tokens. Cheap API keys and local models are the default.

## License

MIT © 2026 — see [LICENSE](LICENSE).
