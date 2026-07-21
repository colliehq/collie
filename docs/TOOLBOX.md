# collie toolbox — gap analysis & plan

What to add to collie's tool registry, from surveying Claude Code and Hermes
(both installed locally; inventories read from source).

## Inventory (measured 2026-07-06)

| harness | # tools | notable |
|---|---|---|
| **collie** | 8 | read_file, write_file, edit_file, bash, grep, glob, memory_search, remember |
| **Claude Code** | 19 | + WebFetch, WebSearch, TodoWrite, **Agent** (subagents), **Mcp**/ReadMcpResource, NotebookEdit, AskUserQuestion, EnterWorktree, ExitPlanMode |
| **Hermes** | ~90 | + full **browser** (browser_navigate/click/type/press/scroll/snapshot/console/dialog/cdp + computer_use), web_search/web_extract/x_search, **delegate_task** (subagents), skills (skill_manage/view/list), vision (vision_analyze/video_analyze/image_generate), todo, kanban/project mgmt, terminal/process, feishu/discord/homeassistant integrations |

collie is deliberately lean, but is missing four capability classes that both
mainstream harnesses have: **web, browser, subagents, and an MCP client.**

## Shipped (2026-07-13)

- **`web_search`** — keyless (DDG/SearXNG) + optional Brave/Tavily/bridge. On w/ `web`.
- **`web_fetch`** — read ONE url as text (markup stripped). SSRF-guarded: refuses
  loopback/private/link-local by default (`COLLIE_WEBFETCH_ALLOW_LOCAL=1` to opt out).
- **MCP client** ⭐ — consume any stdio MCP server. Config `~/.collie/mcp.json`
  (`{"servers":{"fs":{"command":"npx","args":[...]}}}`). Tools land in the **deferred
  tier** (advertised by name, kept OUT of the cached prefix); the model pulls a schema
  with the new **`load_tools`** meta-tool, then calls `mcp__<server>__<tool>`. Tool lists
  are cached (`~/.collie/mcp_cache.json`, keyed by config hash) so startup spawns nothing;
  servers spawn lazily on first call and are pooled. This is the deferred-tier seam that
  the lean-prompt thesis was built for.
- **Settings** — `~/.collie/settings.json` (env `COLLIE_*` still wins) + web GUI gear
  panel (provider/model/embed/reranker/max_turns/budget). `COLLIE_SETTINGS_PATH` redirects.
- **Budget** — `COLLIE_MAX_COST` / `COLLIE_MAX_TOTAL_TOKENS` (0 = off) stop the loop past
  a $/token ceiling and annotate the answer; no extra synthesis turn is spent.
- **`plan`** — multi-step task list, persisted per project (`~/.collie/plans/`); rides back
  in the tool result each turn. **`undo`** — write_file/edit_file snapshot the prior content;
  `undo` restores (or removes a newly-created file). Both always-on (CC/Hermes parity).
- **`pack`** (`collie pack -n N --check "…" [--apply]`) — best-of-N with EXECUTION-based
  selection: N isolated tree copies, winner = passes `--check` then harness `verified`;
  refuses to apply if nothing passes. The verification thesis applied to candidate selection.
- **Security** — browser bridge requires an `X-Collie-Bridge` CSRF header (closes a no-Origin
  `no-cors` GET that could steal a queued command); all externally-fetched content (browser +
  web_fetch) is fenced as untrusted DATA (prompt-injection defense; `COLLIE_NO_CONTENT_FENCE=1`
  opts out).

Regression-locked: 44 core + 21 GUI + 29 renderer + 11 surfaces green (`tests/run_all.sh`).

## Plan (prioritized by leverage)

### Tier 1 — core coding parity (cheap, native, high value)
- **`web_search` + `web_fetch`** — research-first coding (look up docs/APIs, cut
  hallucination). Both CC and Hermes have them; collie has none. Effort: **S**
  (HTTP + a search API/provider; or wrap an MCP search server).
- **`todo`** — a task-list tool for multi-step plans (CC TodoWrite / Hermes todo).
  Effort: **S**.
- **`apply_patch`** — multi-hunk edit (Hermes `patch`, CC FileEdit replace_all) for
  bigger changes than a single `edit_file`. Effort: **S**.

### Tier 2 — MCP client (HIGHEST LEVERAGE) ⭐
- Add an **MCP client** so collie consumes *any* MCP server's tools, deferred-loaded
  like CC's two-tier system. This is the strategic move: instead of hand-writing
  Hermes's ~90 tools, collie speaks MCP and plugs into the whole ecosystem —
  **including browser control (Playwright-MCP), filesystem, github, and more.**
  Effort: **M**. Unlocks Tier 3 for near-free.

### Tier 3 — browser control (full web/agent control) 🌐
- **Preferred: Playwright-MCP via the Tier-2 MCP client** → `browser_navigate`,
  `browser_click`, `browser_type`, `browser_snapshot`, `browser_screenshot`,
  multi-tab, network — Microsoft's official server, full real-Chromium control.
  Effort once MCP client exists: **S** (just register the server).
- Alternative (native): a `browser` toolset over Playwright/CDP (like Hermes's
  `browser_cdp_tool`). Full control but reimplements what Playwright-MCP gives free.
- (A Chrome *extension* is a weaker option — sandboxed to the page, needs native
  messaging; see the VS Code / browser architecture doc for the verdict.)

### Tier 4 — subagents / delegation
- **`delegate_task`** — spawn an isolated sub-collie for a subtask, return a short
  summary (CC Agent / Hermes delegate_task). collie already has the Workflow shape
  internally; expose it as a tool. Effort: **M**.

### Tier 5 — vision (once browser screenshots are in play)
- **`vision_analyze`** — send a screenshot to a vision-capable model to read/verify
  a page. Needed to close the browser loop (act → screenshot → understand). Depends
  on a vision model (DeepSeek-VL / Qwen-VL / a local VLM). Effort: **M**.

## Recommended sequence
1. Tier 1 native tools (web_search/web_fetch, todo, apply_patch) — a day.
2. **MCP client** (Tier 2) — the unlock.
3. Register **Playwright-MCP** (Tier 3) → full browser control for near-free.
4. delegate_task + vision as the agent takes on web+multi-step work.

Net: rather than clone Hermes's 90 tools, collie gets an **MCP client** + a thin
native core, and inherits the ecosystem (browser included). See the companion doc
for VS Code integration and the Chrome-extension-vs-CDP verdict.
