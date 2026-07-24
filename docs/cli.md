# CLI reference

`collie <command> [options]`. Bare `collie` opens the default chat surface and runs first-time
onboarding when nothing is configured. Full help for any command: `collie <command> --help`.

## Everyday

| Command | What it does |
|---|---|
| `collie` | Terminal chat (TUI). First run picks a provider. |
| `collie -p "<task>"` / `collie run "<task>"` | Run one task headlessly. |
| `collie web` | Serve the browser GUI — streams the verification gate live. |
| `collie app` | Open the native desktop window (Windows). |
| `collie tui` | Rich terminal chat with a live tool/gate/diff timeline. |
| `collie repl` | Interactive REPL that keeps the conversation thread. |

## Running work

| Command | What it does |
|---|---|
| `collie run "<task>" --json` | Final result object (tokens, cost, verified). |
| `collie run "<task>" --stream-json` | Live NDJSON: tool · edit · repro-gate · receipt. |
| `collie loop --goal "<g>" --until "<shell>"` | Iterate toward a goal; stop when the check exits 0. |
| `collie pack "<task>" -n 3 --check "<shell>" --apply` | Best-of-N; keep only what passes. |
| `collie selftest` | $0 deterministic end-to-end (mock model, real tools). |

## Setup & configuration

| Command | What it does |
|---|---|
| `collie setup` | Install optional deps, pick a provider, pre-download the memory model. |
| `collie setup --check` | Diagnose only; install nothing. |
| `collie init` | Warm the memory model + validate the codemap for this repo. |
| `collie init --rules` | Additionally have the model write an `AGENTS.md`. |
| `collie config` | List every setting and its effective value. |
| `collie config KEY` | Print one setting. |
| `collie config KEY VALUE` | Set one setting (e.g. `collie config LANG zh-tw`). |
| `collie mcp list \| login \| logout \| tools` | Manage MCP servers. |

## Desktop (Windows)

| Command | What it does |
|---|---|
| `collie wallpaper --install` | Live desktop star-map behind your icons; starts at logon. |
| `collie wallpaper --stop` / `--uninstall` | Stop it / remove the autostart. |
| `collie browser-bridge` | Run the bridge the browser extension polls (the `browser_*` tools). |
| `collie browser-bridge --install` | Start the bridge at logon. |

See [The desktop app](desktop.md) for what these do and how they fit together.

## Benchmark lab & delegation

| Command | What it does |
|---|---|
| `collie compare` / `collie harnesses` | Run and compare harnesses on the same task. |
| `collie dashboard` | Open the results dashboard. |
| `collie prefix` | Measure the real prefix token cost on a provider. |
| `collie mem` | Inspect / manage the memory store. |
| `collie jobs ls \| inbox \| run \| confirm \| receipts` | Delegated work. |
| `collie acp` | Run as an ACP agent over stdio (Zed / JetBrains / neovim). |

## Configuration precedence

`COLLIE_<KEY>` environment variable → `~/.collie/settings.json` (the Settings panel /
`collie config`) → built-in default. A hard-set env var always wins. A token/cost budget
(`COLLIE_MAX_COST` / `COLLIE_MAX_TOTAL_TOKENS`) stops a run at a ceiling.
