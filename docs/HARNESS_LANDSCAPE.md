# Harness landscape — what collie should (and should not) add

> **Methodology.** 12 parallel research agents (Claude Opus 4.8, web + official docs/changelogs, 2025-2026) surveyed the coding-agent field — **Codex, Cursor, Claude Code, Hermes, Aider, Cline, Windsurf, OpenCode, Amp, Devin** — plus one agent that read collie's own repo for an accurate baseline. A synthesis pass built the matrix below and judged every gap against collie's identity. Generated 2026-07-12. This is a decision doc, not a spec — recommendations are opinionated on purpose.

**collie's moat is real and narrow:** executed **assert-verify** + **most-capability-per-token** + **honestly benchmarked**, all **keyless/local**. The rule this doc applies to every candidate feature: *does it deepen verification or per-token efficiency, or is it heavyweight/hosted sprawl that pulls against lean?* Deepen the moat; don't chase the GUI/cloud players.

---

## Feature matrix

Legend: ✅ = has it · ⚠️ = partial/opt-in/immature · ❌ = absent. collie is first column.

| Capability | collie | Codex | Cursor | Claude Code | Hermes | Aider | Cline | Windsurf | OpenCode | Amp | Devin |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Executed verification gate (assert/repro must run) | ✅ **signature** | ⚠️ review | ⚠️ browser | ⚠️ /verify skill | ❌ loop-robustness only | ⚠️ auto-test | ❌ undo-centric | ⚠️ test iter | ⚠️ LSP only | ⚠️ test iter | ⚠️ self-review |
| Auto test/lint loop | ⚠️ gate+pytest | ⚠️ | ⚠️ | ✅ hooks | ✅ script fb | ✅ test/lint | ⚠️ | ✅ lint-fix | ✅ LSP+fmt | ✅ | ✅ |
| LSP/compiler diagnostics fed back (cross-lang safety) | ⚠️ Python ast only | ❌ | ⚠️ | ⚠️ | ❌ | ⚠️ tree-sitter lint | ❌ | ❌ | ✅ | ❌ | ⚠️ |
| Dedicated code-review pass | ❌ | ✅ | ✅ Bugbot | ✅ /code-review | ✅ review agent | ❌ | ❌ | ❌ | ⚠️ diff view | ✅ composable | ✅ Review |
| Semantic codebase index | ✅ code_search | ⚠️ | ✅ | ⚠️ grep | ⚠️ FTS | ⚠️ repo-map | ⚠️ | ✅ fast ctx | ⚠️ | ✅ Librarian | ✅ Wiki/Search |
| Programmatic tool calling (exploration out of context) | ❌ | ❌ | ❌ | ⚠️ subagents | ✅ **signature** | ❌ | ❌ | ❌ | ❌ | ⚠️ | ❌ |
| Plan mode / todo tracking | ⚠️ --goal only | ⚠️ | ✅ Plan | ✅ Plan/Todo | ⚠️ | ⚠️ architect | ✅ Plan/Act+FocusChain | ✅ Plan | ✅ Plan/Build | ⚠️ | ✅ Interactive |
| Subagents / delegation | ❌ | ✅ | ✅ 8-parallel | ✅ teams | ✅ isolated budget | ❌ | ❌ | ✅ | ✅ Explore/Scout | ✅ Oracle/etc | ✅ managed |
| Lifecycle hooks | ❌ | ✅ | ✅ ~20 pts | ✅ | ⚠️ post-turn | ❌ | ⚠️ | ❌ | ✅ JS plugins | ⚠️ plugins | ⚠️ |
| MCP client | ⚠️ seam only | ✅ | ✅ 1-click | ✅ | ✅ | ✅ | ✅ marketplace | ✅ 100-cap | ✅ auto-OAuth | ✅ | ✅ +server |
| Skills / reusable learned procedures | ❌ | ⚠️ | ✅ marketplace | ✅ SKILL.md | ✅ self-minting | ⚠️ conventions | ✅ | ✅ Flows | ✅ | ✅ | ✅ |
| Cross-session memory | ✅ SqliteMemory | ⚠️ AGENTS.md | ✅ Memories | ✅ auto+CLAUDE.md | ✅ FTS+plugins | ⚠️ git | ✅ MemBank | ✅ Memories | ⚠️ AGENTS.md | ✅ threads | ✅ KB |
| Auto-prefetch recall (no search decision) | ✅ **rare** | ❌ | ❌ | ⚠️ | ⚠️ | ❌ | ❌ | ⚠️ | ❌ | ❌ | ⚠️ |
| Checkpoints / undo / rewind | ❌ | ⚠️ git | ✅ | ✅ /rewind | ✅ fork | ✅ git-commit | ✅ shadow-git | ✅ | ✅ revert | ⚠️ | ✅ snapshots |
| Multi-model architect/editor split (cheap-model routing) | ❌ | ⚠️ effort dial | ✅ per-phase | ⚠️ per-agent | ✅ per-agent | ✅ **architect/editor** | ⚠️ | ✅ router | ✅ per-agent | ✅ dial | ⚠️ |
| Browser use | ✅ opt-in | ✅ | ✅ native | ✅ /chrome | ⚠️ | ❌ | ✅ | ✅ preview | ⚠️ webfetch | ✅ localhost | ✅ full |
| Web search | ✅ keyless | ⚠️ | ✅ | ✅ | ✅ | ⚠️ scrape | ⚠️ | ✅ | ✅ | ⚠️ | ✅ |
| IDE surface | ✅ ACP (all) | ✅ VS Code | ✅ own IDE | ✅ 4 IDEs | ⚠️ gateways | ⚠️ plugins | ✅ 6 IDEs | ✅ own IDE | ✅ | ✅ 4 IDEs | ✅ Desktop |
| Cloud / async execution | ❌ | ✅ | ✅ bg agents | ✅ &/bg | ⚠️ serverless | ❌ | ❌ | ✅ Devin | ⚠️ | ✅ Orbs | ✅ core |
| OS-native sandbox isolation | ❌ | ✅ Seatbelt/bwrap | ✅ mac | ✅ | ⚠️ containers | ⚠️ dry-run | ⚠️ | ⚠️ | ❌ perms only | ⚠️ | ✅ VM |
| Granular permissions (allow/ask/deny) | ❌ | ✅ 2-axis | ✅ | ✅ | ⚠️ approval | ⚠️ confirm | ✅ per-type | ✅ deny-list | ✅ 3-state glob | ✅ allowlist | ✅ enterprise |
| Session sharing / collab threads | ❌ | ⚠️ cloud PR | ✅ canvases | ✅ Artifacts | ⚠️ | ⚠️ git | ❌ | ⚠️ | ✅ /share | ✅ **signature** | ✅ |
| Model-agnostic | ✅ | ❌ OpenAI | ⚠️ +own | ❌ Anthropic | ✅ | ✅ LiteLLM | ✅ | ⚠️ +own | ✅ 75+ | ⚠️ auto | ⚠️ internal |
| Per-token efficiency as design goal | ✅ **~1/6 signature** | ⚠️ | ⚠️ | ⚠️ | ✅ prog-TC | ⚠️ 2-model | ⚠️ manual | ⚠️ | ⚠️ | ❌ unconstrained | ❌ ACU |
| Honest in-repo benchmark lab | ✅ **rare** | ❌ | ❌ | ❌ | ❌ | ✅ Polyglot | ❌ | ⚠️ | ❌ | ❌ | ⚠️ |
| Structured/streaming output (JSON/NDJSON) | ✅ | ✅ | ⚠️ | ✅ schema | ✅ | ❌ | ⚠️ | ❌ | ✅ OpenAPI | ✅ stream | ✅ API |
| Interactive TUI/REPL | ❌ one-shot | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Image input | ❌ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ⚠️ | ⚠️ | ✅ | ✅ Painter | ✅ |


---

## The 5 highest-leverage additions

Each one **compounds verification or per-token efficiency** — the two axes collie already wins on.

| # | Add | Why it's top-tier | Effort |
|---|---|---|:--:|
| 1 | **Programmatic tool calling** (agent writes a script that drives tools over RPC; only printed output re-enters context) | Hermes' signature and the one mechanism collie's token-parity rival has that collie lacks. Collapses 10 exploration calls into 1 summarized turn → directly multiplies the ~1/6-token claim. **Highest ROI on the board.** | M |
| 2 | **LSP/compiler diagnostics fed back post-edit** | Extends executed verification from Python-only (`ast.parse`) to *every* language — closes collie's own flagged weakness. The assert-gate philosophy one level down, keyless. | M |
| 3 | **Lifecycle hooks** (deterministic user-pinned checks the model can't skip) | Executed verification made configurable. Small surface, big trust payoff, stays terminal/CI-native. | M |
| 4 | **Architect/editor multi-model split** (strong model plans, cheap model emits diffs) | A second independent lever on cost besides prog-tool-calling. Aider set polyglot SOTA with it; collie's `ModelProvider` seam + in-repo lab already exist to prove the delta. | M |
| 5 | **Dedicated diff code-review pass** | A cheap second adversarial read of the working diff — now table stakes, and a natural extension of "prove it works." Reuses the loop + `--json`. | S |

---

## Full gap analysis (ranked by value to collie)

Legend: 🟢 ADD · 🟡 ADAPT (reuse existing infra, stay lean) · ⚪ SKIP (off-strategy).

### 1. Programmatic tool calling (agent writes a script that drives tools over RPC; only printed output returns to context)  🟢 **ADD**

- **Present in:** Hermes
- **Fit to collie:** high — it IS the per-token identity mechanized; a lean harness benefits more than a heavyweight one because context is the scarce resource collie already optimizes.
- **Effort:** M
- **Why it matters:** Heavy exploration (10 greps/reads/code_search calls) collapses into ONE summarized turn instead of 10 tool messages in the window. This is the single largest structural lever on tokens-per-instance and is exactly the axis collie competes on.
- **How / rationale:** collie already has a bash tool and a tool registry; wrapping tools as callable functions in a child process (Unix-socket or in-process RPC) and adding an execute_code tool that returns only stdout is the highest-leverage single feature. It compounds directly with the ~1/6-token claim and is the one thing the token-parity rival (Hermes) does that collie does not. Guardrail it with a timeout/output cap like Hermes.

### 2. LSP / compiler diagnostics fed back after every edit (language-agnostic post-edit safety)  🟢 **ADD**

- **Present in:** OpenCode, Aider (tree-sitter lint), Claude Code (via hooks)
- **Fit to collie:** high — directly extends executed verification to all languages and closes a named weakness; it is verification, not bloat.
- **Effort:** M
- **Why it matters:** collie's edit safety net is Python-only (ast.parse). Every non-Python edit ships with zero structural verification. Feeding real type/compile errors back turns a whole class of silent breakage into a self-correct signal — the same philosophy as the assert gate, one level down.
- **How / rationale:** Fixes the explicitly-flagged 'Python-only syntax gate' weakness. Shelling to an installed LSP (or even language-native `tsc --noEmit`/`go build`/`cargo check` on edited files) and appending diagnostics to the edit result is cheap, keyless, and philosophically identical to the assert-verify loop. Reuse the existing edit_file post-hook path.

### 3. Lifecycle hooks (deterministic scripts fired on pre/post-edit, pre-commit, stop, etc.)  🟢 **ADD**

- **Present in:** Codex, Cursor, Claude Code, OpenCode, Amp (plugins)
- **Fit to collie:** high — hooks are executed-verification made user-configurable and deterministic; small surface, big trust payoff, no hosted infra.
- **Effort:** M
- **Why it matters:** Lets the USER pin verification the model cannot skip — run the suite after every edit, block commit on lint fail. Deterministic gates are more trustworthy than a model deciding to verify, which is collie's whole thesis.
- **How / rationale:** A minimal PreToolUse/PostToolUse/Stop hook mechanism (shell commands from config, exit-code gates the action) is a few hundred lines, stays terminal/CI-native, and lets teams enforce their own executed checks around collie's gate. Highest-value extensibility investment vs. a full plugin SDK.

### 4. Multi-model architect/editor split (strong model plans, cheap model emits edits)  🟢 **ADD**

- **Present in:** Aider, OpenCode (per-agent), Cursor, Amp, Hermes
- **Fit to collie:** high — collie already has the ModelProvider seam and benchmarks the harness delta; a plan-model/edit-model split is pure cost-efficiency, its core positioning.
- **Effort:** M
- **Why it matters:** Aider set SOTA on its polyglot benchmark with this and it cuts cost: reasoning tokens on the expensive model, mechanical diff tokens on a cheap one. It's a second independent lever on per-token cost besides prog-tool-calling.
- **How / rationale:** The provider factory already exists (make_provider). Add --edit-model / --plan-model so the reproduction+edit steps can run on a cheaper model while planning uses the strong one, and measure it in the existing lab. Low risk, on-thesis, and empirically validated by Aider.

### 5. Dedicated code-review pass over the working diff (correctness/simplification, severity-ranked)  🟢 **ADD**

- **Present in:** Codex, Cursor (Bugbot), Claude Code, Amp, Devin
- **Fit to collie:** high — verification-flavored, runs locally on a diff, no hosted infra; complements (does not replace) the assert gate.
- **Effort:** S
- **Why it matters:** A second adversarial read of the diff catches bugs the fix-then-gate loop rationalizes away. It's a natural extension of collie's 'prove it actually works' stance and a common table-stakes feature now.
- **How / rationale:** A `collie review` subcommand that feeds the git diff to the model with a correctness-focused rubric and emits findings (it already has ReportFindings-style structured output patterns via --json) is small and reuses the loop. Keep it a separate reviewer role, not an auto-blocking bot, to stay lean.

### 6. Real MCP client (connect stdio/HTTP MCP servers as tools)  🟡 **ADAPT**

- **Present in:** Codex, Cursor, Claude Code, Hermes, Aider, Cline, Windsurf, OpenCode, Amp, Devin
- **Fit to collie:** medium — extensibility is orthogonal to lean/verification, but the deferred-tool tier already exists and MCP fills it without bloating the always-on core.
- **Effort:** M
- **Why it matters:** MCP is now table stakes — it's the universal integration surface (DBs, APIs, internal tools). collie has only a lightly-populated deferred-tool seam, so it can't reach the ecosystem every rival plugs into.
- **How / rationale:** Implement a minimal stdio MCP client that registers server tools into the EXISTING deferred tier (advertised by name, schema fetched on demand) so it stays token-cheap — this preserves the two-tier design rather than dumping 100 tools into every prompt. Skip hosted/OAuth-heavy remote MCP initially.

### 7. Isolated subagents / delegation with independent budgets and clean context  🟡 **ADAPT**

- **Present in:** Codex, Cursor, Claude Code, Hermes, Cline, OpenCode, Amp, Devin
- **Fit to collie:** medium — Hermes-style single-child delegation (parent sees only final text) serves per-token, but full multi-agent fan-out/teams is off-strategy sprawl for a lean harness.
- **Effort:** M
- **Why it matters:** Delegating a noisy sub-investigation to a child with its own context keeps the parent window clean and token-cheap — Hermes shows this is a token discipline mechanism, not just a parallelism feature.
- **How / rationale:** Add a single-depth `delegate` tool that spawns a fresh Harness with a restricted toolset and returns only its final summary — the token-efficiency use, not Claude Code's hundreds-of-agents orchestration. Bound total tree cost explicitly (Hermes flags independent budgets as a cost footgun). Skip nested teams.

### 8. Plan/read-only mode separating investigation from editing  🟡 **ADAPT**

- **Present in:** Cursor, Claude Code, Cline (Plan/Act), Windsurf, OpenCode (Plan/Build), Aider (architect)
- **Fit to collie:** medium — helps correctness and token spend on big tasks, but collie's one-shot batch identity (SWE-bench) values fewer turns; risk of adding an interactive ceremony it doesn't need.
- **Effort:** S
- **Why it matters:** A read-only exploration pass that proposes a plan before any write reduces wasted edits and gives the user a checkpoint. Nearly every rival has converged on this.
- **How / rationale:** Rather than a full interactive Plan/Act toggle, add a --plan flag that runs the loop with edit/write tools denied and emits a structured plan, reusing the permission gating from the hooks work. Cheap, optional, and leans on collie's existing tool-restriction machinery (force_edit already restricts tools mid-loop).

### 9. Reusable skills + lightweight self-improvement (mint/patch a procedure after a complex task)  🟡 **ADAPT**

- **Present in:** Hermes (self-minting), Claude Code, Cline, Windsurf, OpenCode, Amp, Devin
- **Fit to collie:** medium — compounds with existing memory and auto-prefetch, but autonomous self-rewriting skills add a review/safety burden that cuts against 'lean and auditable'.
- **Effort:** M
- **Why it matters:** The harness gets better at a repo/task family over time instead of starting cold — Hermes' closed loop is a real differentiator, and collie already has the SqliteMemory substrate to store procedures.
- **How / rationale:** Store distilled task procedures as ARCHIVAL memory entries (collie's memory already does write-time distillation + dedup) and surface them via auto-prefetch — reuse infrastructure rather than build a separate skills marketplace. Keep minting user-confirmable, not fully autonomous, to preserve auditability.

### 10. Checkpoints / undo / rewind of agent edits  🟡 **ADAPT**

- **Present in:** Claude Code, Cline (shadow-git), Windsurf, OpenCode, Aider (git-commit), Devin
- **Fit to collie:** medium — safety net is nice, but collie's assert-gate already prevents shipping broken edits, so the marginal value is lower than for undo-centric rivals; the git-commit form is cheap enough to justify.
- **Effort:** S
- **Why it matters:** A guaranteed undo lets the agent attempt ambitious edits safely. Aider's model (auto-commit per change) is the cheapest, most terminal-native version and doubles as reviewable history.
- **How / rationale:** Adopt Aider's git-native approach (optional auto-commit each landed edit with a generated message) rather than Cline's shadow-git overhead — it gives revert-via-plain-git for free and reviewable diffs, fitting terminal/CI without a snapshot subsystem. Note Claude Code's caveat: checkpoints don't cover bash side effects, so don't oversell it.

### 11. Granular permissions (allow/ask/deny per tool, path, command)  🟡 **ADAPT**

- **Present in:** Codex, Claude Code, OpenCode (3-state glob), Cline, Amp, Windsurf
- **Fit to collie:** medium — matters mainly for the autonomous loop; a config-driven allow/deny list is lean, but full interactive approval UX is off collie's headless identity.
- **Effort:** M
- **Why it matters:** For the autonomous `loop` running unattended, a policy layer bounding what bash/edit can touch is a real safety gap — collie currently has bash timeout only.
- **How / rationale:** Add a config-based deny/allow list for bash commands and edit paths (OpenCode's glob last-match-wins is a clean model) that the loop enforces silently — headless-appropriate. Skip per-command interactive prompts; that's a TUI feature collie doesn't have.

### 12. OS-native sandbox isolation for shell execution  🟡 **ADAPT**

- **Present in:** Codex (Seatbelt/bubblewrap), Claude Code, Cursor, Windsurf, Devin (VM)
- **Fit to collie:** medium — protects the autonomy story, but a robust cross-OS sandbox is heavy engineering that competes with lean; Codex itself notes bubblewrap fails on some hosts.
- **Effort:** L
- **Why it matters:** Autonomous loops running arbitrary bash on the host are a real risk; Codex's network-off-by-default OS sandbox is isolation without Docker.
- **How / rationale:** Offer an OPTIONAL bubblewrap/sandbox-exec wrapper for the bash tool (best-effort, off by default) rather than owning full isolation — pair it with the cheaper permission-list layer above, which delivers most of the safety at a fraction of the effort. Don't build a VM/container substrate; that's Devin's game.

### 13. Structured-output schema validation loop (fail cleanly after N tries)  🟢 **ADD**

- **Present in:** Codex (--output-schema), Claude Code (--json-schema)
- **Fit to collie:** medium — small, verification-flavored, strengthens the CI/streaming surface collie already markets.
- **Effort:** S
- **Why it matters:** For scripted/CI use, validating the final result against a JSON schema and retrying instead of emitting malformed data makes collie safer to embed in pipelines — a natural fit for the existing --json surface.
- **How / rationale:** collie already emits --json / --stream-json; adding an optional --output-schema that validates and re-prompts on failure is small and reinforces the honest-machine-readable-output positioning without new surface area.

### 14. Interactive TUI / REPL with conversational continuation  ⚪ **SKIP**

- **Present in:** Codex, Cursor, Claude Code, Hermes, Aider, Cline, Windsurf, OpenCode, Amp, Devin
- **Fit to collie:** low — a full TUI is a large surface that pulls against lean/batch identity and duplicates what the ACP editor path already covers for humans.
- **Effort:** L
- **Why it matters:** Every rival offers an interactive loop; collie is one-shot run or iterated loop, with modest max_turns. Interactive use is the dominant daily-driver mode.
- **How / rationale:** collie's edge is headless executed-verification and per-token efficiency for batch/CI/agent use, and it already reaches interactive humans through the ACP adapter (any editor). Building a competing TUI spends scarce effort on a crowded, non-differentiating surface. Raise default max_turns if interactive-via-ACP feels shallow, but don't build a REPL.

### 15. Cloud / async remote execution (overnight jobs, background agents, PR handoff)  ⚪ **SKIP**

- **Present in:** Codex, Cursor, Claude Code, Amp (Orbs), Devin
- **Fit to collie:** low — requires hosted infrastructure, directly contradicts the $0/keyless/local, self-hostable positioning.
- **Effort:** L
- **Why it matters:** Delegating long jobs to hosted infra that returns diffs/PRs is a headline feature for the heavyweight players.
- **How / rationale:** This is the opposite of collie's identity (lean, local, keyless, honestly benchmarked). It ties value to paid cloud, which the competitor analysis itself flags as lock-in for Codex/Amp/Devin. collie's `loop` already gives local autonomy that ends on a real executed check — that's the on-brand version.

### 16. Session sharing / persistent shareable collaboration threads  ⚪ **SKIP**

- **Present in:** Amp, OpenCode (/share), Devin, Claude Code (Artifacts)
- **Fit to collie:** low — needs a hosted share backend and routes code off-machine; a data-exposure surface at odds with local-first.
- **Effort:** L
- **Why it matters:** Team knowledge-sharing and thread discovery are real collaboration wins for Amp/OpenCode.
- **How / rationale:** Requires hosted infra and leaks proprietary code by default (OpenCode/Amp both flag this as a weakness). collie's honest lab + --stream-json receipts already give shareable, reproducible artifacts locally without a backend.

### 17. Native IDE GUI / editor sidebar (own editor or rich extension)  ⚪ **SKIP**

- **Present in:** Cursor, Cline, Windsurf, Devin Desktop
- **Fit to collie:** low — collie is terminal-first and already reaches all major editors through one ACP adapter; building GUI is redundant.
- **Effort:** L
- **Why it matters:** A GUI is how most developers experience these tools day-to-day.
- **How / rationale:** The ACP adapter is the lean answer — one adapter renders collie's verification gate, diffs, and token/$ receipt in Zed/JetBrains/neovim/VS Code for free. Building a bespoke IDE or heavy extension duplicates that at huge cost and abandons the terminal-first thesis.

### 18. Auto-indexed repo wiki + cited codebase Q&A; machine snapshots; multimodal (image/voice) input  ⚪ **SKIP**

- **Present in:** Devin (Wiki/Search/Snapshots), Cursor, Amp (Painter), Aider (voice/image), Windsurf (voice)
- **Fit to collie:** low — heavyweight hosted infra (wiki/snapshots) or peripheral input modalities that don't serve verification or per-token efficiency.
- **Effort:** L
- **Why it matters:** Nice ergonomic/onboarding features (browsable architecture docs, reproducible VM state, image error input, voice).
- **How / rationale:** Auto-wikis and machine snapshots depend on continuous indexing/VM infra a lean harness cannot cheaply replicate and that collie's code_search already partially covers for grounding. Image/voice input are peripheral to a headless verification harness. Revisit image input (S effort) only if users hit screenshot-driven debugging; everything else is off-strategy.

---

## Summary & roadmap

collie's moat is real and narrow: executed assert-verify + most-capability-per-token + honestly benchmarked, all keyless/local. The roadmap should deepen that moat, not chase the heavyweight players' hosted/GUI sprawl.

HIGHEST-LEVERAGE TO ADD (each compounds verification or per-token, the two things collie already wins on):

1. Programmatic tool calling (Hermes' signature; M effort). The agent writes a script that drives tools over RPC and only printed output re-enters context. This is the single most copyable idea for a lean harness and the one mechanism collie's token-parity rival (Hermes) has that collie lacks — it directly multiplies the ~1/6-token claim. Highest ROI on the board.

2. LSP/compiler diagnostics fed back post-edit (OpenCode; M effort). Extends executed verification from Python-only (ast.parse) to every language, closing collie's own flagged weakness. It's the assert-gate philosophy one level down and keyless.

3. Lifecycle hooks (Codex/Claude Code/OpenCode; M effort). Deterministic user-pinned checks the model cannot skip — executed verification made configurable. Small surface, big trust payoff, stays terminal/CI-native.

4. Architect/editor multi-model split (Aider, SOTA on polyglot; M effort). A second independent lever on cost besides prog-tool-calling: reasoning on the strong model, mechanical diffs on a cheap one. The ModelProvider seam already exists and the in-repo lab can prove the delta — maximally on-thesis.

5. Dedicated diff code-review pass (Codex/Amp/Claude Code; S effort). A cheap second adversarial read that's now table stakes and naturally extends 'prove it works.' Reuses the loop + --json.

Secondary ADAPTs (reuse existing infra, stay lean): real MCP client wired into the EXISTING deferred-tool tier (schema-on-demand so it stays token-cheap); single-depth isolated subagent for token discipline (Hermes-style, not Claude Code fan-out); git-native auto-commit for cheap undo (Aider, not Cline shadow-git); config-based allow/deny permissions for the autonomous loop; a --plan read-only pass reusing the existing tool-restriction machinery; and skills-as-distilled-memory riding the SqliteMemory substrate rather than a marketplace.

DELIBERATELY NOT BUILDING (off-strategy — grounded in the research):
- Cloud/async remote execution (Codex/Amp/Devin) — needs hosted infra, breaks $0/keyless/local; the competitor notes flag it as lock-in. collie's `loop --until` is the on-brand local autonomy.
- Own IDE / GUI sidebar (Cursor/Cline/Windsurf/Devin) — the one ACP adapter already reaches every major editor with the gate + receipts rendered for free.
- Session-sharing / hosted collaboration threads (Amp/OpenCode/Devin) — hosted backend + routes code off-machine (both vendors flag the leak).
- Auto-indexed wiki / machine snapshots (Devin) — heavyweight indexing/VM infra a lean harness can't cheaply match; code_search already covers grounding.
- Full interactive TUI/REPL — large non-differentiating surface; ACP covers interactive humans. (Bump default max_turns instead.)
- Voice input, and image input only if screenshot-debugging demand appears.

UNCERTAINTY FLAGS: Hermes' SWE-bench resolve-rate and the ~588k-tokens/instance figure are third-party, not first-party (Nous publishes neither), so the '1/6 the tokens, ties Hermes at 78%' framing rests on collie's own small-n pooled run (~27 instances, ±1-2 noise per the README) — treat the parity claim as directional, not proven. Per-token efficiency cells in the matrix are qualitative (design-intent, not measured head-to-head). The subagent ADAPT carries Hermes' documented footgun: independent child budgets make total tree cost exceed the parent cap, so bound it explicitly.
