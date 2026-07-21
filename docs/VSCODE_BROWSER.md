# collie: VS Code integration & browser control — plan

Distilled from a survey of Cline, Continue, Roo Code, the Claude Code VS Code
extension, ACP, and Playwright-MCP / browser-use / chrome-devtools-mcp.

## Verdict up front

- **VS Code**: build a **thin TS extension** (~few hundred lines) that spawns the
  `collie` CLI and streams JSON. The Python agent loop is unchanged. This is the
  Claude Code model, and it's collie's natural fit (core already lives in a CLI).
- **Browser control = TWO backends behind one interface** (corrected after reading
  the user's own extensions — see below). **Playwright/CDP** for general/public/CI
  web tasks; a **Chrome extension + localhost bridge** for the user's real
  authenticated, bot-protected sessions (Manheim/Akamai, job portals, Reddit) where
  Playwright gets fingerprinted and 403'd. This is NOT "extension vs Playwright" —
  they solve different problems, and collie should ship both.

## (A) VS Code — thin extension over `collie --json-stream`

How the ecosystem does it: thin-client (Claude Code = extension wraps the CLI) vs
TS-rewrite (Cline/Roo reimplement the agent in TS); Continue is a thin extension +
a reusable compiled Core. Cline's 2026 move — extracting the agent into a
host-agnostic SDK behind a `HostProvider` — confirms the agent shouldn't be welded
into the extension. collie already satisfies that (Python CLI).

**Architecture**: `extension.ts` `spawn("collie", ["--json-stream"])` → event router
→ three VS Code adapters:
- **DiffAdapter** — `TextDocumentContentProvider` (collie's proposed content as a
  virtual doc) + `vscode.diff(left,right)` for a reviewable side-by-side; on accept,
  `WorkspaceEdit` + `applyEdit()` (enters undo/redo). This is where collie's
  `write_file`/`edit_file` become **reviewable diffs** — no new collie tools needed.
- **TerminalAdapter** — `window.createTerminal()` + shell-integration (exit codes,
  output); collie's `bash` executes here, output streamed back.
- **DiagnosticsAdapter** — `languages.getDiagnostics(uri)` before/after an edit
  (Roo's pattern); new errors fed back to collie to self-correct. Add one read-only
  collie tool `get_diagnostics(path)`.

**Optional bonus (multi-editor for near-free): ACP agent-side in Python.** Implement
`agent-client-protocol` (official Python SDK) — map collie's read/write/edit/bash to
`fs/read_text_file` / `fs/write_text_file` / `session/request_permission` / terminal.
Free support in **Zed, Neovim, Emacs, JetBrains** (client renders diffs/permissions).
Caveat: **VS Code is not an ACP client**, so ACP is *additional* editors, not a
replacement for the thin extension.

Effort: read-only chat panel **S**; full (diff accept/reject + terminal + diagnostics)
**M**; ACP agent **S–M** (doesn't cover VS Code).

## (B) Browser control — TWO backends behind one `browser_*` interface

Correcting an earlier over-generalization: after reading the user's THREE working
extensions (`auto-apply-ext`, `cartek-manheim-probe`, `forum-autopost`), the reason
they chose extensions is real and Playwright can't replace it. collie needs both.

### Backend 1 — Extension bridge (real authenticated, bot-protected sites) ⭐
The user's proven pattern (all three extensions, identical shape):
- **MV3 extension, content script injected into the real site** (Manheim /
  Workday·Greenhouse·Lever·Ashby / Reddit·1point3acres).
- **Bridge = plain `fetch("http://localhost:<port>")` to a local Python brain**
  (7777 / 7799 / 7788), with the localhost URL in `host_permissions`. **No native
  messaging** — so none of the 1 MB/msg or host-process friction I worried about.
- Permissions: `tabs, scripting, storage, alarms` (+ `activeTab` for auto-apply).

Why it's irreplaceable here:
1. **Real login/session** — runs in the user's already-authenticated Chrome (Manheim
   account, job portals, Reddit); no re-login, MFA already passed.
2. **Anti-bot** — Manheim is behind **Akamai (bare requests 403)**; portals/Reddit
   fingerprint automation. A real extension in the real browser is indistinguishable
   from the human. Playwright/headless/CDP-launched browsers get detected/blocked.
3. **Trivial bridge** — `fetch localhost`. **collie's CLI can BE that localhost brain
   server**, exactly like the cartek/auto-apply brains. So this backend = a thin
   content-script (reuse the user's skeleton) + a collie HTTP endpoint that returns
   the next action. MCP/Playwright-MCP does NOT help (it launches its own browser →
   same anti-bot wall).

Effort: **M** (reuse the user's extension skeleton + a collie `serve` HTTP endpoint).

### Backend 2 — Playwright Python (general/public/CI web)
`BrowserSession` (Playwright Python): `chromium.launch()`, or `connect_over_cdp(
"http://localhost:9222")` to a `--remote-debugging-port` Chrome for a real profile.
Full control (DOM, accessibility, screenshot, network, JS, tabs), native library,
no extra process. Best for "research X on the web", scraping public data, CI.
Effort: **S** (6–8 tools). Or via **`@playwright/mcp`** if collie is an MCP client
(accessibility-snapshot, token-cheap) — near-zero code, and the MCP client also
unlocks the rest of the ecosystem (see [TOOLBOX.md](TOOLBOX.md)).

### Shared tool surface (same names, backend chosen per-site/policy)
`browser_navigate(url)` · `browser_back` · `browser_click(ref)` · `browser_type` ·
`browser_fill_form(fields)` · `browser_read()` (accessibility snapshot / DOM text —
token-cheap) · `browser_screenshot()` · `browser_tabs` / `browser_switch_tab` ·
`browser_eval_js` · `browser_wait_for`.

**Routing rule**: authenticated / bot-protected / must-use-my-session (Manheim, jobs,
LinkedIn, banking, Reddit) → **extension bridge**; everything else → **Playwright**.

## Recommended build order (ties into TOOLBOX.md + SWE-bench)

1. **Tier-1 native tools**: `web_search` / `web_fetch`, `todo`, `apply_patch`. (S)
2. **MCP client** — the unlock (browser + ecosystem without hand-writing tools). (M)
3. **Browser** — Playwright backend (public web) *and* the extension-bridge backend
   (real authenticated/bot-protected sites; reuse the user's extension skeleton +
   a collie localhost `serve` endpoint). → full browser control. (S + M)
4. **VS Code thin extension** (diff/terminal/diagnostics). (M)  [+ optional ACP]
5. **SWE-bench** as the credible eval — resolve-rate head-to-head vs Claude Code +
   collie's efficiency edge. **Docker is available** (Docker Desktop on Windows,
   v29.5.3, reachable from WSL), so this is unblocked. Toy tasks stay the fast dev
   loop; start with a small SWE-bench Verified sample (~10-25).
