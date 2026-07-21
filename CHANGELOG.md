## v0.18.0 — round 17 · pi-study adoptions: reliability + honest measurement (all regression-locked)
Two multi-agent workflows studied a minimal reference harness ("pi") against collie, extracted
14 ideas, and adversarially reviewed each; 12 adopted (1 rejected, 1 deferred).
**60 new regression tests (44→104 core+GUI green).**
The SWE-bench n=16 / apitap $-measurements each entry names are the pre-registered plan — they need
a real provider + API budget and have NOT been run yet (flagged for a measurement pass).

- **Provider default → API key; OAuth opt-in** (`settings.py`, `acp_agent/delegate/pack`): the
  Claude-Code OAuth header impersonation (CHANGELOG history: "BANNED + server-side blocked") is no
  longer a silent default — `anthropic` (API key) is the default everywhere, `anthropic-oauth` is an
  explicit choice switchable in the web Settings panel. Entry points that never call
  `settings.apply()` (ACP) now route through `settings.get("PROVIDER")`, so a panel save is
  authoritative there too. Copies pi's framing honesty (it never claims the spoof is sanctioned).

- **Instrument-first (Batch A) — the flagship "lean" numbers become measured, not estimated:**
  - **Cache-waste ledger** (`costs.cache_miss`, loop): every turn, tokens that SHOULD have cache-hit
    but were re-billed are detected, priced, and attributed to a cause (schema change / history
    elision / TTL). A prefix-busting regression now shows as a red `cache waste $X` in the receipt
    instead of silent cost inflation — the runtime complement to the byte-stability test. Pre-reg
    honest-negative: if waste < $0.001/instance on DeepSeek, record "ledger found nothing, kept as a
    tripwire". (An existing-data SQL already shows ~52% DeepSeek cache-hit ratio as the baseline.)
  - **Usage-anchored prefix measurement** (`providers.measure_prefix`, `collie prefix --measure`):
    the headline `~70–99× leaner prefix` rested on `len//4`; now the real prefix is measured from
    provider usage (two-request differential), recorded to `~/.collie/prefix_probe.json` + runs.db
    `prefix_measured`. README's est basis is to be rewritten to the measured M as an editorial step.
    `#13` (input-token double-count) locked by `test_usage_no_double_count`; `#14` prefix-ceiling now
    WARNS (was never enforced). Zero prompt bytes added.

- **Cheap wins (Batch B):**
  - **#12 stage A** — the deferred-tool advert is now sorted + frozen, so activating a tool no longer
    shrinks the STABLE section and busts the cached prefix. Recorded as a **regression-locked
    invariant, NOT cache savings** (on the fallback path `body["tools"]` still grows on activation;
    provider-native `defer_loading` is stage B, sequenced into the Anthropic-cache package).
  - **#6 bash spill-to-file** — output over 8KB (and timed-out commands over 4KB) spill FULL output
    to `/tmp/collie-spill/`, with a pointer in the first line (survives the 240-char elision stub) so
    the model greps it instead of paying to re-run. Baseline caveat: today's 4-transcript corpus has
    0 truncations; if a fresh n=16 also shows ~0, this is timeout-branch insurance, not a re-run tax
    remover — still worth it (lean = prompt, not fewer features). Fallback if adoption=0: unconditional
    head+tail split (backlog #2's prescription).
  - **#14 unicode-tolerant edit rung + BOM fix** — exact→whitespace→**unicode (curly quotes / 7 dash
    variants / NBSP, NFKC)**→line-number ladder, splicing into ORIGINAL bytes (untouched lines never
    reformat). **Latent bug fixed:** a BOM'd `.py` file was UNEDITABLE (ast.parse chokes on U+FEFF →
    misleading "would break Python syntax"); BOM is now stripped for matching and restored on write.

- **Error-path unification (Batch C, serialized — all four rewrite the same loop region):**
  - **#4 errors-as-data** — every provider's transport/HTTP/JSON failure now RETURNS a
    `stop_reason="error"` Completion (carrying `error_status`/`error_detail`), never raises; the loop
    has ONE failure path. **Two live bugs fixed en route:** (a) the answer-recovery fallback added in
    **v0.17.0 (5328c6a)** let an error completion become `res.answer` AND enter durable memory —
    reintroduced the exact leak the v0.15 sentinel closed; (b) `judge.py` read "HTTP **429**" as a
    quality **10** — now returns neutral 5.0 on an errored grader.
  - **#1 output-truncation guard** — a `stop_reason="length"` turn with tool calls executes NONE of
    them (args may be silently cut); each gets a "not executed — re-issue, or split into smaller
    edits" result. Truncated plain answers get a marker + are not consolidated. **Completes backlog
    #7's second half** — `finish_reason` was discarded on the DeepSeek path, so truncation was
    invisible; now normalized to `length` across OpenAI-compat/Anthropic/Ollama. Anthropic default
    `max_tokens` 1024→4096 (a big edit was systematically impossible before).
  - **#9 context-overflow recovery** — an input-too-long error shrinks the history once (window
    14→4, stub 240→120, recent-content cap) and retries the turn exactly once (`COLLIE_OVERFLOW_RECOVERY=0`
    to disable). A hard run-killer becomes a recoverable event; no LLM summarization.
  - **#5 retry classification** — one pure `classify_error()` (`retryable`/`terminal`/`overflow`,
    each pattern incident-annotated) + a single host-owned bounded backoff replaces the OpenAICompat
    internal 3× loop. Transient 529s no longer crash a benchmark arm as FAIL; quota errors still fail
    fast. Settings: `RETRIES`, `RETRY_BASE`.

- **Loop features (Batch D):**
  - **#7 model-quirk arg repair** — `repair_args()` fixes JSON-string-wrapped array/object args
    (Opus 4.6 / GLM-5.1) and `file_path`→`path` BEFORE dispatch, REBUILDING the ToolCall so the saved
    session keeps the model's raw args (replay fidelity). Malformed-JSON args now report "not valid
    JSON" instead of a misleading "missing required arg". Each repair is one saved round-trip on the
    weak/cheap models collie targets.
  - **#13 steering queue** — type while the agent works to redirect it (TUI), injected as a user
    message at safe points (turn start / voluntary finish), never canceling in-flight work. A single
    `_StdinFeed` owns stdin (kills the two-readers race); benchmark path byte-identical (steering
    unset). Course-correcting a drifting run beats abort-and-restart on tokens.

- **Skills lazy index (Batch E) — the only prompt-byte-adding change:** `harness/skills.py` discovers
  `SKILL.md` files (project `.collie/skills` + `~/.claude/skills` for free Claude-Code interop) and
  injects only `name: description (path)` (~20 tok/skill) into STABLE; the model `read_file`s the
  full skill on a match. The deferred-tool thesis generalized from schemas to prose — token-cheap
  domain knowledge vs the always-paid CLAUDE.md. Same trust level as the repo's own CLAUDE.md
  (deliberately no per-project trust gate). n=5 success reads are directional only (v0.14 lesson:
  ±1 at small n isn't attributable); hard conclusions ride mechanical counts (activation rate).

- **Rejected / deferred (with evidence):** **#11** per-tool prompt composition REJECTED — collie's
  tool list already generates from the registry, and `Tool.description` already ships in the API
  schema, so pi's `promptSnippet` would be pure ~120-150 tok duplication (+12-15% prefix). **#8**
  file-op ledger DEFERRED — pi's ledger survives DESTRUCTIVE compaction; collie has none (elision
  recomputes from full history every turn), so there is nothing to survive yet.

- Housekeeping: `pyproject.toml` version 0.12.0 → 0.18.0 (matched to `__version__`).

## v0.17.0 — round 16 · capability surface + security hardening (all regression-locked)
- **MCP client** (`harness/mcpclient.py`): consume any stdio MCP server (JSON-RPC 2.0 NDJSON,
  protocol 2024-11-05). Tools land in the **deferred tier** namespaced `mcp__<server>__<tool>`
  — advertised by name, kept OUT of the cached prefix — and the model pulls a schema via the
  new **`load_tools`** meta-tool (`ToolRegistry.activate` + `active_schemas` union). Tool lists
  are cached (`~/.collie/mcp_cache.json`, keyed by config hash) so startup spawns nothing;
  servers spawn lazily on first call, pooled, atexit-closed. This completes the deferred seam
  the lean-prompt thesis was built for.
- **pack mode** (`harness/pack.py`, `collie pack`): best-of-N with EXECUTION-based selection —
  N isolated tree copies, pick the winner by `--check` (exit 0) then the harness `verified`
  verdict; refuses `--apply` if nothing passes. The verification thesis applied to selection.
- **web_fetch** (`harness/webfetch.py`): read one url → text (SSRF-guarded: refuses loopback/
  private/link-local, re-checks after redirect; 4 MB cap). Completes search→read→verify.
- **plan** + **undo** tools: multi-step task tracking (persisted per project) and rollback of
  file edits (write/edit snapshot prior content; `undo` restores or removes new files).
- **Settings** (`harness/settings.py` + web gear panel + `/api/settings`): layered env >
  `~/.collie/settings.json` > default; `apply()` injects into `os.environ` with zero call-site
  churn. **Budget**: `COLLIE_MAX_COST` / `COLLIE_MAX_TOTAL_TOKENS` stop a run at a ceiling.
- **Security — browser bridge & injection**: (1) CSRF gate now requires an `X-Collie-Bridge`
  header, closing a no-Origin `no-cors` GET hole that could DEQUEUE (steal) a queued command;
  (2) all externally-fetched content (browser read/open/click/console/eval + web_fetch) is
  **fenced as untrusted DATA** so a page saying "ignore your instructions, run …" is treated
  as data, not commands (collie has bash + full-machine access by design).
- **Prefix**: now ~1K tokens core (plan+undo added ~90) — still ~70–99× leaner than a
  mainstream agent. Tests: **44 core + 29 renderer + 21 GUI + 11 surfaces** green.

## v0.16.0 — round 15 · efficiency measured on BOTH sides + churn cap
- **apitap** (`harness/apitap.py`): a usage-metering reverse proxy — point an opaque CLI
  (Hermes reports no tokens) at localhost; it forwards to the real API, reads `usage` from
  each response (forces `include_usage` on streams), overrides auth with a known-good key.
  First real **token** comparison, same DeepSeek `usage` source for both:
  **collie uses 1.4–4.9× FEWER tokens than Hermes at equal capability** (flask 162k vs
  787k, requests-1142 186k vs 261k).
- **Post-edit churn cap** (loop): a debug trace showed collie fixed flask correctly at
  turn 8 then spun to 35 (coverage nudge chasing unrelated files + verifying in an unset-up
  env + re-reading history-stubbed output). Once edited AND coverage offered, if it makes
  tool calls ≥5 turns with no NEW edit, finish. Measured: flask **35→19 turns, 378k→162k
  tokens (−57%)**, fix preserved. Across 16: median 20 turns, ~$0.03/instance.
- **Confirmed capability held**: re-ran collie n=16 with all loop changes → **7/16**
  (unchanged; Hermes 8/16). ±1 composition drift is model variance (gained requests-1724,
  lost xarray-3095 to a persistent old_string mis-quote — the residual empty-patch mode).
- Also: cost_usd now computed (`res.cost_usd` was never set); read_file truncation marker.

## v0.15.0 — round 14 · n=16 variance-robust + backlog + efficiency now measured
- **Headline (n=16, same model):** collie **7/16** vs Hermes **8/16** — agree on 15/16,
  differ only on `seaborn-3069`. Investigated it: both edit the right file/method; the
  gap is DeepSeek *reasoning variance* on one tricky fix (Hermes' simpler grid-off+invert
  passed, collie's didn't) — **not a harness deficiency**. collie is a competitive harness;
  0 empty patches across 16.
- **Worked the audit backlog to 16/32** (all high+med + several cleanup): bash exit code,
  API-error sentinel + retry/backoff, max_tokens, git add -A+exclude, timeout-not-cached,
  memory dense-abstain + LIKE multi-token, code-index per-file budget, no-op-edit reject,
  nudge once-guard, history bound, read_file truncation marker.
- **Efficiency now measurable:** `res.cost_usd` was never computed (recorder logged $0)
  despite the price table — now computed at finish (honors cache). A collie SWE instance =
  **~$0.018** on DeepSeek, prefix **725 tok**. The history bound (old tool outputs stubbed)
  roughly halved multi-turn context (~323k → ~167k total on a 20-turn run).
- **Ceiling insight:** with collie ≈ Hermes and the lone gap being model variance, the
  SWE resolve-rate ceiling is now the *model* (DeepSeek), not the harness.

## v0.14.0 — round 13 · multi-agent audit + a false conclusion, corrected
- **Multi-agent harness audit** (7 subsystems → adversarial verify → ROI rank): 32
  confirmed problems, backlog in `docs/AUDIT_BACKLOG.md`.
- **Correction:** round 12's "Hermes 7/8, big harness gap" was an artifact — `swe_predict_one`
  dispatched every non-`collie` agent to `predict_claude_code`, so "Hermes" was actually
  `claude -p` (subscription). Fixed dispatch (`AGENTS[agent]`). **True same-model result:
  collie 4/8 ≈ Hermes 5/8** (±1 = n=8 variance); the Claude 7/8 gap is a model gap;
  pylint/seaborn are model-bound (both DeepSeek harnesses fail them). All the round-12
  agonising over multi-file coverage was chasing a phantom — real Hermes doesn't solve
  pylint either.
- **Core bug fixed** (audit Rank 1): a failed `edit_file` (DeepSeek mis-quoting
  `old_string` → `ERROR:` return, nothing written) was counted as a successful edit,
  silently disabling every empty-patch guard. Now `did_edit` is gated on edit success.
- **Robustness** (Rank 3): `tool.run` wrapped so a malformed tool call is a recoverable
  error, not a whole-run abort.
- **Method lesson:** n=8 with ±1 variance can't attribute a single harness fix. Next:
  larger sample / best-of-k, plus the queued audit backlog (bash exit codes, API-error-as-
  answer, git add -A+exclude, memory RRF).

## v0.13.0 — round 12 · SWE-bench n=8, add Hermes, close the harness gap
- **Native Docker Engine in WSL** (`docker-ce` systemd service) replaces flaky Docker
  Desktop — stable `/var/run/docker.sock`, root-cause fix for the recurring "Docker down".
- **OOM + junk hardening** (real bugs from the first n=8 dry run): predict each instance
  in a fresh subprocess (ONNX arenas accumulated ~30GB → near-OOM); systemd cgroup
  `MemoryMax` per instance; `git add -u` not `-A` (a stray `pip install` made a
  12MB/1164-file venv "patch"); flush+fsync each prediction (resumable for real).
- **Added Hermes** as a 3rd SWE-bench agent (same DeepSeek as collie) — the same-model
  control that revealed the truth: **Hermes 7/8 vs collie's initial 4/8** (toy tasks had
  said "collie = Claude Code"). OpenClaw dropped (Node/config friction).
- **Diagnosed + fixed the gap** (COLLIE_DEBUG): collie burned all turns exploring, never
  edited (empty patch); `code_search` surfaced a docstring chunk for every query.
  → **hard tool-restriction** (past a deadline with no edit, only read/edit/write is
  offered — text nudges alone failed) + **code-density penalty** in `code_search`.
  Result: **collie 4/8 → 5/8** (empty patches 1 → 0; requests-1142 resolves). Remaining
  gap to Hermes: pylint (4-file fix, collie does 1) + seaborn (right place, wrong fix).

## v0.10.0 — round 11 · browser control (backend 1: Playwright) + SWE-bench unblocked
- `harness/browser.py`: Playwright backend with `browser_navigate/read/click/type/
  screenshot` (opt-in via `--browser` / COLLIE_BROWSER=1 so coding runs stay lean).
  Validated: collie(DeepSeek) navigated example.com and read the h1 (3 turns, 2 tools).
- Two-backend browser plan (docs/VSCODE_BROWSER.md): Playwright for public web;
  extension-bridge (reuse the user's auto-apply/manheim/forum skeletons + a collie
  localhost endpoint) for real authenticated + Akamai/bot-protected sessions.
- Docker confirmed available (Docker Desktop on Windows, reachable from WSL) → SWE-bench
  eval unblocked (recipe in progress).

# collie iteration log

Standing goal (`/goal`): continuously iterate collie — each round, benchmark against
existing harnesses across dimensions, then upgrade & adjust. Every collie run is tagged
with the version below (stored in `runs.note`) so the dashboard trends progress
across rounds.

Iteration protocol (one round = one pass):
1. **Bench** — `compare --vs <harnesses>` (mock = free plumbing/prefix; `--real` =
   live head-to-head, costs quota). Record to runs.db.
2. **Diagnose** — read the dashboard: which dimension is weakest (prefix, success,
   retrieval precision, latency, generality)?
3. **Upgrade** — make one targeted change behind an existing seam.
4. **Verify** — re-bench; confirm the metric moved the right way; log it here.

---

## v0.7.0 — round 8 · self-verification loop + better tools (close the gap)
- **`edit_file` tool**: targeted, unique-substring replace (safer than full rewrite);
  8 always-on tools now.
- **Self-verification loop**: after collie edits code and moves to answer, the
  harness nudges once to run `pytest` and fix failures before finalizing
  (`Harness.self_verify`, default on). max_turns 6 → 10 for room to verify+fix.
- **Act-mode prompt**: instructs "after editing, run the tests to verify".
- Goal: close the ~10% capability gap to Claude Code on hard tasks while keeping
  collie's ~18x efficiency edge. Result in dashboard.

## v0.6.0 — round 7 · realistic repo-level tasks (the trade-off surfaces)
- 10-task suite on a real mini-package (pkg/{money,stats,text} + 3 test modules,
  3 seeded bugs): fix failing tests, implement functions, run the suite.
- Fixed a silent bug: pytest checks ran under a python without pytest → always
  false. Now use the venv interpreter.
- **10-task 4-harness result (all DeepSeek-V3)** — on HARDER tasks the trade-off
  shows honestly:
  - collie: prefix **616**, success **80%**, quality 7.3, $0.0085, 7.2s
  - Claude Code: prefix 206k, success **90%**, quality 9.0, $0.1565, 20.8s
  - Hermes: N/A tokens, success 80%, quality 9.0, 23.6s
  - OpenClaw: N/A, success 10% (not a coding agent)
  - Read: CC's heavier harness (5.4 turns) buys +10% success on hard tasks; collie
    trades that for **~18x leaner prefix, ~18x cheaper, ~3x faster**. Honest, not
    a clean sweep. (count_py is noisy — CC roams to .venv counting 2450 .py; will
    drop/replace next round.)

## v0.5.0 — round 6 · OpenClaw + Hermes actually in the comparison
- **Hermes Agent** installed (v0.18) + configured for DeepSeek (custom provider in its
  own config file); runs headless via `hermes -z "<task>"`. Adapter unsets a
  shadowing `OPENAI_API_KEY` so it reads its own DeepSeek `.env`.
- **OpenClaw** installed (needs Node 24) + DeepSeek provider plugin; runs headless
  via `openclaw agent --agent main --message "<task>" --json --local`.
- Both are ambient/gateway assistants, not coding agents — included honestly:
  success + quality(judge) + latency measured; tokens N/A in headless output
  (dashboard shows "N/A · headless" for their prefix, not a misleading 0).
- Adapter `run()` supports `extra_env` values of `None` (unset a var) for the
  key-shadowing fix.
- 4-harness run: collie vs Claude Code vs Hermes vs OpenClaw, all DeepSeek-V3.

## v0.4.0 — round 5 · richer tasks + quality + cost + precision@k + more harnesses
- **Richer task suite**: real coding tasks incl. **edit-and-verify** (fix a bug so
  a pytest passes; implement `slugify` and run it) with per-run sandbox reset so
  edits are fair. `check(answer, cwd)` can execute code.
- **Task-completion QUALITY**: LLM-judge (0-10) via a cheap grader (DeepSeek),
  beyond binary pass/fail. New `quality` column + dashboard.
- **Cost dimension**: `costs.py` price table → per-run $ (turns the prefix gap into
  money). New `cost_usd` column + dashboard.
- **Retrieval precision@k**: `collie mem eval` on a labeled multilingual set →
  **jina-v3 P@1 0.80 / P@5 1.00 / MRR 0.90 vs hash 0.50 / 0.80 / 0.56** (quantifies
  pain #1). Dashboard "Memory retrieval quality" section.
- **More harnesses**: Codex CLI installed + adapter (blocked: current Codex only
  speaks the OpenAI Responses API, so it can't run on DeepSeek/Ollama without an
  OpenAI key or a Responses-proxy). OpenClaw + Hermes adapters wired with their
  **real headless commands** (`openclaw agent --message … --json --local` /
  `hermes -z "…"`) + DeepSeek config — running them needs install (Node 24 / shell
  installer) + a token-logging proxy (neither emits tokens headless).
- **Definitive run**: collie vs Claude Code, both DeepSeek-V3, 7 tasks, quality +
  cost — see dashboard.

## v0.3.0 — round 4 · cheap-API backend + same STRONG model + renamed to collie
- **Provider**: added `OpenAICompatProvider` — any OpenAI-compatible endpoint
  (DeepSeek/Qwen/GLM/Kimi/OpenRouter/Groq/OpenAI) as collie's backend. This is the
  right shape (a completion endpoint), which `claude -p` is not. mh drives its own
  loop on a real strong model for pennies. `claude_on()` generalizes the CC adapter
  to any Anthropic-compatible endpoint.
- **Definitive same-STRONG-model result** — both collie and Claude Code on
  **DeepSeek-V3** (via DeepSeek's `/anthropic` endpoint for CC): fixed prefix
  **collie ~527 vs CC 48k–249k (~180× leaner)**, **success 100% vs 100%** (model
  strong ⇒ tie ⇒ the gap is purely harness), latency **4.4s vs 17.2s**, turns 2 vs 3.7.
- **Renamed** the project/harness id `mh` → **collie** (repo + branding + dashboard).
- Dashboard note-box + same-model detection now generic.

## v0.2.2 — round 3 · fair SAME-MODEL comparison (harness isolated)
- **Methodology fix** (user's point): comparing mh(qwen) vs CC(Sonnet) confounds
  harness with model. Solution: run BOTH on the **same** model.
- **How**: Ollama now speaks the Anthropic Messages API (2026-01-16), so
  `ANTHROPIC_BASE_URL=localhost:11434 claude -p --model qwen2.5-coder:7b` runs
  **Claude Code on the same local model mh uses** — proven working. Added
  `adapters.claude_on_local()` + `extra_env` on adapters (reproducible).
- **Result (both on qwen2.5-coder:7b — pure harness delta)**: fixed prefix
  **mh 518 vs Claude Code 16,386 (32× less)**; latency mh 3.5s vs 4.1s; success
  mh 67% vs 33% (small N). Model held constant ⇒ the gap is the harness.
- **Policy research** (reconciled with the user's Anthropic emails): extracting the
  subscription OAuth token to hit api.anthropic.com from a non-official client is
  BANNED + server-side blocked. BUT official `claude -p` / Agent SDK on the
  subscription is ALLOWED — the June-15 "move to separate credit" change was
  PAUSED and still hasn't taken effect. So a subscription-backed Sonnet run is
  possible via `claude -p`; the same-local-model route above is the cleaner,
  $0, policy-trivial way to isolate the harness.

## v0.2.1 — round 2 · real model backend via local CLI (no API key)
- **Finding**: `claude -p` **cannot** serve as mh's raw model backend — asked to
  emit a tool-call for mh's loop, Sonnet refuses it as "a prompt injection attempt".
  The `claude` CLI is a full harness, not a model endpoint; it also force-carries
  Claude Code's own ~18K system prefix, erasing mh's lean-prefix advantage.
- **Fix**: added `OllamaProvider` — mh runs on a **local model** (qwen2.5-coder:7b
  on the 5090 via Ollama). Free, no API key, real tool-calling, and mh keeps its
  **lean prefix** (~526 tok) because it owns the whole prompt. Added a content-
  fallback tool parser (local models often emit tool calls as text, not the
  structured `tool_calls` field). `AnthropicProvider` and `ClaudeCliProvider`
  (kept as a documented dead-end) also present.
- **First real same-tooling comparison** (both real models, $0 key): mh(local
  qwen-7b) vs Claude Code(Sonnet) — prefix **526 vs 32,393** (62×↓), latency
  **3.1s vs 8.2s**, success **67% vs 100%** (7b < Sonnet; find_todo missed). This
  is the honest multi-dimensional picture: mh wins context-efficiency + cost +
  latency; Sonnet wins raw task success. Dashboard note is now model-aware.
- **Next**: stronger local model (qwen2.5-coder:14b/32b on the 5090) to close the
  success gap; async prefetch; install a 2nd harness CLI to widen the field.

## v0.2.0 — round 1 · real local embedding
- **Change**: deployed `LocalEmbedding` (fastembed, ONNX/CPU) as the default
  embedder, replacing `HashEmbedding`. Model = **jinaai/jina-embeddings-v3**
  (1024-d, multilingual+code), picked by an on-machine acid test:
  jina-v3 **5/5** vs multilingual-e5-large 3/5 vs mpnet 4/5 (paraphrase + zh↔en
  cross-lingual). mpnet/e5 exposed as `local:<model>` fast/alt profiles.
- **Also**: `kind='query'|'passage'` asymmetric encoding; `mem reembed`;
  per-user-message prefetch cache (embed once/msg, not once/turn).
- **Metric delta**: retrieval quality hash→jina on acid test 1/5→5/5; semantic +
  cross-lingual recall now real. Cost: ~950ms/embed CPU (vs echomem 6500ms cloud;
  <50ms on GPU later). $0, offline, private.
- **Next candidates**: (a) real-model mh side (needs ANTHROPIC_API_KEY) for a true
  same-model head-to-head; (b) cross-encoder rerank on the fused pool; (c) async
  prefetch so the 950ms is off the critical path; (d) install a 2nd harness CLI
  (codex/gemini) to widen the comparison.

## v0.1.0 — round 0 · walking skeleton
- Agentic loop + Mock/Anthropic providers + two-tier tools + tiered context
  composer w/ auto-prefetch + SQLite memory (CORE/facts/FTS5/hash-embed hybrid) +
  recorder + multi-harness adapters + dashboard.
- Real head-to-head (mock planner, real prefix/tools) vs Claude Code (Sonnet):
  fixed prefix **534 vs 32,393 tok (98.4% ↓)**, 3/3 tasks both sides.
