<p align="center">
  <img src="assets/collie-logo.svg" width="120" height="120" alt="Collie logo">
</p>

<h1 align="center">Collie</h1>

<p align="center">
  <b>A lean coding-agent harness that proves its work.</b><br>
  <sub>Writes a reproduction, runs it, and finishes only when an executed assertion passes.
  Terminal-first, model-agnostic, token-lean.</sub>
</p>

<p align="center">
  <a href="https://collie.run">collie.run</a> ·
  <code>collie -p "fix the bug"</code> ·
  <code>collie acp</code>
</p>

---

`collie` is a from-scratch coding-agent harness built around one idea most agents bury in a
tool log: **executed verification.** When Collie fixes something it writes a reproduction
that *must* fail on the broken code, makes the smallest edit that flips it, and re-runs the
assertion — a run isn't "done," it's **verified ✓**. Everything else follows from being
lean: quiet by default, semantic code navigation built in, and built to measure itself
against other harnesses on the same task, so progress is a number, not a vibe.

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

Other agents check "did the test not error." Collie's gate is stronger: the reproduction
carries an `assert actual == expected` derived from the issue, so a plausible-but-wrong edit
fails *loudly* and drives another repair round. This **`assert-verify`** loop is the core of
the harness — a wrong edit never silently ships as "done."

## Where it runs

Collie is **terminal-first** and reaches editors through an open protocol, not a bespoke
extension:

| Surface | Command | Reaches |
|---|---|---|
| **Terminal** | `collie` (TUI) · `collie -p "task"` | anywhere — SSH, CI, tmux |
| **Browser GUI** | `collie web` | chat + the live verification gate, diffs, the star-map, settings |
| **VS Code** | the bundled `vscode-collie` extension | Collie docked in a sidebar panel (manages its own server) |
| **Streaming / CI** | `collie run "task" --stream-json` | NDJSON events (tool · edit · repro-gate · receipt) for scripts & editors |
| **Editors (ACP)** | `collie acp` | Zed · JetBrains · neovim · VS Code (via the ACP client) — one adapter, every editor |

The verification gate, native diffs, and the token/time/$ receipt render in every editor for
free, because Collie's streaming events map straight onto the [Agent Client Protocol](https://agentclientprotocol.com).

## Platforms

One cross-platform Python codebase — **not** a per-OS fork. The handful of operations that
genuinely differ (kill a process tree on a timeout, secure a token file, convert a path,
choose a shell) are isolated in `harness/plat.py`, so the same wheel runs everywhere.

| OS | Status | Notes |
|---|---|---|
| **Linux** | ✅ native | the primary target |
| **macOS** | ✅ native | POSIX; the browser bridge is *simplest* here (Chrome + Collie on one OS, plain localhost) |
| **Windows** (native) | ⚠️ core runs | no POSIX shell — the agent prefers the file/search tools over `bash`; process-tree kill uses `taskkill /T` |
| **WSL2** | ✅ | a Windows-Chrome ↔ WSL bridge crosses OSes, so it uses the LAN IP + `wslpath` (handled for you) |

Per-OS setup — especially the real-browser bridge (`collie browser-bridge` + the
`harness/browser_ext/` extension) on each platform — is in **[docs/PLATFORMS.md](docs/PLATFORMS.md)**.

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
| `EmbeddingProvider` | **OnnxEmbedding** granite-107m (Apache, 55MB, multilingual) · bge-m3 / e5 · jina-v3 opt-in (fastembed) · **BM25-only** when no model |
| `SqliteMemory` | CORE + facts + FTS5 + cosine, hybrid RRF + optional rerank + consolidation |
| `ContextComposer` | STABLE/CONTEXT/VOLATILE + auto-prefetch · a ~1K-token fixed prefix (kept deliberately lean) |

**`code_search`** (built in): extracts the identifiers from a natural-language query and greps the
repo (ripgrep, else grep) — ranking files by how many of your terms each contains and returning the
top `path:line` snippets, so the agent reasons about *where* to edit instead of grepping blind.
Grep localization is cheap and robust — no model, no index build, and never a stale line number.
**`edit_file`** is exact-match + whitespace-tolerant and
**rejects any edit that would break Python syntax** (a wrong edit never silently ships).
**`read_file`** is line-numbered and pageable. A **self-verification loop** (`assert-verify`)
runs the reproduction and repairs until the assertion holds.

## Install

**Windows — one click.** Download **`Collie-Setup.exe`** from the
[latest release](https://github.com/colliehq/collie/releases/latest) and double-click it. A small
app-style installer (borderless, animated) walks you through language + install location, lays down a
self-contained runtime (Python + Collie + semantic memory, nothing to preinstall), and opens Collie
in a native desktop window. On first launch you **pick a brain** — an existing Claude, Codex, or Grok
login is detected and connects in one click; or paste an API key. See [docs/install](docs/install.md).

**Developers — pip.** The core is stdlib-only, so the base install is tiny:

```bash
pip install -e ".[local,dev]"      # from a clone (PyPI publish is planned)
collie setup                       # install optional deps, pre-download the model, pick a provider
collie                             # the terminal chat (TUI) opens
```

No account, no telemetry, and the core has **zero third-party dependencies** — `mock` and `ollama`
run without any key, and memory works out of the box on **BM25 keyword recall**.

Optional extras: `pip install ".[local,tui,search]"` — `local` (semantic memory: granite-107m via
onnxruntime, ~55MB, multilingual — what `collie setup` installs), `tui` (rich terminal chat),
`search` (keyless web search), `acp` (editor protocol), `browser` (Playwright), `fastembed`
(jina-v3 opt-in). macOS/Linux use the same `pip` path; the one-click installer is Windows-only today
(a `.dmg` is on the roadmap).

## Quickstart

```bash
# zero-config: bare `collie` opens the default surface; first run picks a provider
collie                     # terminal chat (TUI)
collie web                 # browser GUI — chat, live verification gate, diffs, the star-map
collie init                # optional: warm the memory model for this repo (--rules writes AGENTS.md)

# $0 deterministic end-to-end (mock model, real tools + memory + dashboard)
collie selftest

# a real cheap model (provider key in env)
DEEPSEEK_API_KEY=... collie -p "fix the off-by-one in utils/timeparse.py"

# machine-readable / streaming
collie run "fix the bug" --json          # final result object (tokens, cost, verified)
collie run "fix the bug" --stream-json   # live NDJSON: tool · edit · repro-gate · receipt

# fully local, no key
collie run "summarize app.py" --provider ollama --model qwen2.5-coder:7b

# a standing goal (pinned into CORE memory, loaded every turn) + web lookup
collie run "make timeparse handle compound units" --goal "keep the prefix lean" --web-search

# autonomous loop: iterate toward the goal, STOP the first turn an executed check goes green
collie loop --goal "get the suite passing" --until "pytest -q" --max 8

# best-of-N with EXECUTION-based selection: run N isolated attempts, keep only what passes
collie pack "fix the failing test" -n 3 --check "pytest -q" --apply

# serve as an ACP agent (an editor spawns this over stdio)
collie acp
```

**Pack mode** applies the verification thesis to *selection*: `collie pack` runs the task N times
in isolated copies of the tree and picks the winner by what actually passes — the `--check` command
first, then the harness's own verified verdict — refusing to `--apply` anything if nothing passes.
**MCP**: point `~/.collie/mcp.json` at any stdio MCP server and its tools join Collie's *deferred*
tier (advertised by name, schema pulled on demand via `load_tools`) so they never bloat the cached
prefix. **Settings**: a web gear-panel / `~/.collie/settings.json` (env `COLLIE_*` still wins) plus a
token/cost **budget** (`COLLIE_MAX_COST` / `COLLIE_MAX_TOTAL_TOKENS`) that stops a run at a ceiling.
Untrusted web/page content the agent reads is **fenced as data** (prompt-injection defense), and the
browser bridge refuses any request missing its CSRF header.

**Autonomy that ends on green.** `collie loop` re-runs the agent toward a `--goal` (carried
across iterations in memory) and stops when `--until "<shell>"` exits 0 — the loop terminates
on a *real executed check*, not the model announcing it's done. Same idea as the verification
gate, one level up. `web_search` is keyless (a DuckDuckGo HTML fetch Collie does itself, $0),
or set `COLLIE_WEBSEARCH_BRIDGE=host:port` to route through a Chrome extension in your real
logged-in browser — the same fetch-localhost pattern as Collie's browser tool. It's **off in
the benchmark path** so runs stay deterministic; on in interactive runs.

Providers: `mock`, `ollama`, `anthropic`, `anthropic-oauth`, and OpenAI-compatible presets
`deepseek` · `qwen`/`dashscope` · `openrouter` · `moonshot` · `groq` · `zhipu` · `openai`.

## Benchmark lab (built in)

Collie measures itself against other harnesses on the **same** task and model — you run it
yourself; no numbers are asserted here:

```bash
# SWE-bench Verified head-to-head (needs Docker)
DEEPSEEK_API_KEY=... python swe_run.py --n 5

# rigorous multi-run: pass@1 / pass@k / consistency / Wilson CI / McNemar
python -m bench.multirun_eval

# multi-language (Aider-Polyglot: python / cpp / js)
python -m bench.polyglot_eval --langs python,cpp,javascript --n 6 --agent collie --model claude-opus-4-8

# discover + compare installed CLIs (collie vs Claude Code / Aider / …)
.venv/bin/python -m harness.cli compare --vs all
```

## Honesty & policy

- The benchmark harness is version-tagged and reproducible. "Progress is a number" cuts both
  ways — Collie is built to surface the levers that turn out **net-neutral**, not just the wins.
- Token counts are real usage (the model's own `usage`, or `harness/apitap.py` metering for
  CLIs that report none) — apples-to-apples, same source both sides.
- Collie draws a personal Max/Pro subscription only through the first-party OAuth path
  (`anthropic-oauth`), the same mechanism the official CLI uses; it never scrapes or resells
  subscription tokens. Cheap API keys and local models are the default.

## License

MIT © 2026 — see [LICENSE](LICENSE).
