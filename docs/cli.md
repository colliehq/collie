# CLI reference

`collie <command> [options]`. Bare `collie` opens the default chat surface and runs first-time
onboarding when nothing is configured. Full help for any command: `collie <command> --help`.

## Everyday

| Command | What it does |
|---|---|
| `collie` | Terminal chat (TUI). First run picks a provider. |
| `collie -p "<task>"` / `collie run "<task>"` | Run one task headlessly. |
| `collie web` | Serve the browser GUI — streams the verification gate live. First run offers a companion display name; rename later under **Settings → My Collie**. |
| `collie web --name Rowan` | Serve explicitly as kennel dog `Rowan`. This selection outranks the editable companion display setting and appears read-only for that server; it does not rename the Slack app or mail address. |
| `collie web --lan` | Same, but also listen on this machine's network address so the iOS app (CollieIOS) can pair. Network clients get **nothing** until they pair: the token is handed to loopback only, and `/pair` shows a one-shot code the phone trades for it at `/api/pair` (HMAC challenge/response — the secret never crosses the wire). Add `--qr` for a QR fallback of the same one-shot secret. |
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
| `collie library scaffold \| list \| show \| validate \| plan` | Create a safe starter, inspect installed extensions, or review a local package and its exact digest/scopes. |
| `collie library install \| enable \| disable \| rollback \| uninstall` | Operate the trusted extension lifecycle; activation and removal have explicit review boundaries. |
| `collie library revoke <id> --digest <sha256> --reason "…" --yes` | Revoke one exact installed digest; active matching code is disabled fail-closed. |
| `collie library connections \| audit` | List active data-only connection descriptors or inspect lifecycle audit records. |

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
| `collie mission start "<goal>"` | Persist a durable campaign and return its ID immediately. |
| `collie mission start "<goal>" --domains x.com,*.y.com --actions-per-hour 6` | Start with the saved Mission autonomy mode and named, paced boundaries. `--review` asks before irreversible actions; legacy `--auto` explicitly selects Hands-off. Also supports `--max-actions` and `--max-steps`. |
| `collie mission ls \| status \| run \| pause \| resume \| cancel \| confirm \| continue \| accept \| check \| reconcile` | Inspect, gate, and control durable campaigns. |
| `collie jobs daemon` | Foreground wake loop for Jobs/Missions; catches up after sleep. `collie supervisor install` keeps it available after sign-in/reboot. |
| `collie activity [--health]` | One durable view of foreground runs, Missions, specialists, automations, recovery, and worker health. |
| `collie recovery ls \| show \| reconcile` | Inspect crash-uncertain tool boundaries; reconciliation always requires an explicit resolution and `--yes`. |
| `collie hooks status \| check \| trust \| untrust` | Review deterministic hooks and trust only the exact configuration hash. |
| `collie supervisor install \| status \| uninstall` | Manage the per-user Windows 24×7 worker supervisor. |
| `collie automations upsert \| list \| status \| tick \| daemon` | Manage durable timer/file/page/webhook automation execution. |
| `collie acp` | Run as an ACP agent over stdio (Zed / JetBrains / neovim). |

## Configuration precedence

`COLLIE_<KEY>` environment variable → `~/.collie/settings.json` (the Settings panel /
`collie config`) → built-in default. A hard-set env var always wins. A token/cost budget
(`COLLIE_MAX_COST` / `COLLIE_MAX_TOTAL_TOKENS`) stops a run at a ceiling.

For first-party identity, explicit `collie web --name` → hard-set `COLLIE_COMPANION_NAME` → saved
`COMPANION_NAME` → a single kennel dog → generic `Collie`. The saved name is display-only across
Home, Mobile, Remote, and Ambient; Slack apps, `@` handles, and dog-mail addresses keep their own
external identities until renamed through those systems' workflows.
