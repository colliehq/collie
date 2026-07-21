<p align="center">
  <img src="assets/collie-logo.svg" width="120" height="120" alt="Collie logo">
</p>

<h1 align="center">Collie</h1>

<p align="center">
  <b>A lean coding-agent harness that proves its work.</b><br>
  <sub>Writes a reproduction, runs it, and finishes only when an executed assertion passes —
  at ~1/6 the tokens of comparable agents. Terminal-first, model-agnostic, honestly measured.</sub>
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
lean: quiet by default, semantic code navigation built in, and every claim measured against
other harnesses on the same task, so progress is a number, not a vibe.

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
fails *loudly* and drives another repair round. On SWE-bench Verified this **`assert-verify`**
loop is the one change that measurably moved Collie's resolve rate — and it generalizes to
unseen repos (see below).

## Results — same model, isolated harness

SWE-bench Verified, **official Docker eval**, every harness on the **same model**
(Claude Opus 4.8) so the delta is the *harness*, not the model. Pooled over 27 instances
(a diagnosis set + a fresh, unseen holdout):

| harness | resolved | tokens / instance |
|---|:--:|:--:|
| Claude Code | **85%** | not reported |
| **Collie + assert-verify** | **78%** | **~110k** |
| Hermes | 78% | ~588k |
| Collie (baseline) | 63% | ~105k |

**Collie + assert-verify matches Hermes and trails Claude Code by 7 points — at roughly
1/6 the tokens**, and it's the fastest of the four on multi-language (Exercism py/cpp/js).
The gain **generalizes**: on a fresh holdout from repos Collie had never touched
(django / sympy / matplotlib / scikit-learn), assert-verify held its edge with no net
regression.

> **Honest caveats.** n is small (tens of instances) and single-model, so treat ±1–2 as
> noise — Collie ships a multi-run harness (`bench/multirun_eval.py`: pass@k, Wilson CI,
> McNemar) and a flakiness guard exactly because a single lucky run lies. SWE-bench Verified
> itself is now widely considered contamination-prone; Collie's pitch is not a leaderboard
> number but **most-capable-per-token, with the evidence kept on disk.** Reproduce it:
> `docs/SWE_AUDIT.md` records every experiment, including the ones that failed.

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
| `EmbeddingProvider` | **LocalEmbedding** bge-small / jina-v3 (fastembed, ONNX/CPU, $0) · Hash fallback |
| `SqliteMemory` | CORE + facts + FTS5 + cosine, hybrid RRF + optional rerank + consolidation |
| `ContextComposer` | STABLE/CONTEXT/VOLATILE + auto-prefetch · a ~1K-token fixed prefix (still ~70–99× leaner than a mainstream agent's) |

**`code_search`** (built in): batch-embeds the repo with a fast local model (~8s to index,
~16ms/query) and returns ranked `path:line` snippets, so the agent reasons about *where* to
edit instead of grepping blind — a localization lever most harnesses lack, and it runs
entirely inside Collie's own loop. **`edit_file`** is exact-match + whitespace-tolerant and
**rejects any edit that would break Python syntax** (a wrong edit never silently ships).
**`read_file`** is line-numbered and pageable. A **self-verification loop** (`assert-verify`)
runs the reproduction and repairs until the assertion holds.

## Install

```bash
pipx install collie-harness        # or: uv tool install collie-harness
collie                             # first run asks where completions come from, then the TUI opens
```

No account, no telemetry, and the core has **zero third-party dependencies** — `mock` and
`ollama` run without any key. Try it without installing anything:

```bash
uvx --from collie-harness collie -p "explain this repo"
```

Optional extras: `pipx install "collie-harness[tui,local,search]"` — `tui` (rich terminal chat),
`local` (real local embeddings), `search` (keyless web search), `acp` (editor protocol),
`browser` (Playwright). From source: `git clone … && pip install -e ".[dev]"`.

## Quickstart

```bash
# zero-config: bare `collie` opens the default surface; first run picks a provider
collie                     # terminal chat (TUI)
collie web                 # browser GUI — chat, live verification gate, diffs, the star-map
collie init                # optional: pre-warm the embedder + code index (--rules writes AGENTS.md)

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
collie run "make timeparse handle compound units" --goal "beat CC on prefix tokens" --web-search

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
the SWE-bench path** so benchmarks stay deterministic; on in interactive runs.

Providers: `mock`, `ollama`, `anthropic`, `anthropic-oauth`, and OpenAI-compatible presets
`deepseek` · `qwen`/`dashscope` · `openrouter` · `moonshot` · `groq` · `zhipu` · `openai`.

## Benchmark lab (built in)

Collie measures itself against other harnesses on the **same** task and model:

```bash
# SWE-bench Verified head-to-head (needs Docker) — official eval
DEEPSEEK_API_KEY=... python swe_run.py --n 5                    # docs/SWEBENCH.md

# rigorous multi-run: pass@1 / pass@k / consistency / Wilson CI / McNemar
python -m bench.multirun_eval

# multi-language (Aider-Polyglot: python / cpp / js)
python -m bench.polyglot_eval --langs python,cpp,javascript --n 6 --agent collie --model claude-opus-4-8

# discover + compare installed CLIs (collie vs Claude Code / Aider / …)
.venv/bin/python -m harness.cli compare --vs all
```

## Honesty & policy

- Every result is version-tagged and reproducible; `docs/SWE_AUDIT.md` records the failures
  too. "Progress is a number" cuts both ways — Collie documents the levers that turned out
  **net-neutral**, not just the wins.
- Token counts are real usage (the model's own `usage`, or `harness/apitap.py` metering for
  CLIs that report none) — apples-to-apples, same source both sides.
- Collie draws a personal Max/Pro subscription only through the first-party OAuth path
  (`anthropic-oauth`), the same mechanism the official CLI uses; it never scrapes or resells
  subscription tokens. Cheap API keys and local models are the default.

## License

MIT © 2026 — see [LICENSE](LICENSE). See [CHANGELOG.md](CHANGELOG.md) for the iteration log.
