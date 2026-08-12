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
| `collie mission start "<goal>" --code --workspace PATH --overnight --provider anthropic-oauth --model claude-opus-4-8 --no-paid-overage --verify-command "python -m pytest -q"` | Attempt to start Collie's experimental native direct-OAuth loop. Startup first runs a real Collie-owned inference probe and fails closed if the subscription route is unavailable. |
| `collie mission ls \| status \| run \| pause \| resume \| cancel \| confirm \| continue \| accept \| check \| reconcile` | Inspect, gate, and control durable campaigns. |
| `collie jobs daemon` | Foreground wake loop for Jobs/Missions; catches up after sleep. `collie supervisor install` keeps it available after sign-in/reboot. |
| `collie activity [--health]` | One durable view of foreground runs, Missions, specialists, automations, recovery, and worker health. |
| `collie recovery ls \| show \| reconcile` | Inspect crash-uncertain tool boundaries; reconciliation always requires an explicit resolution and `--yes`. |
| `collie hooks status \| check \| trust \| untrust` | Review deterministic hooks and trust only the exact configuration hash. |
| `collie supervisor install \| status \| uninstall` | Manage the per-user Windows 24×7 worker supervisor. |
| `collie automations upsert \| list \| status \| tick \| daemon` | Manage durable timer/file/page/webhook automation execution. |
| `collie acp` | Run as an ACP agent over stdio (Zed / JetBrains / neovim). |

Overnight code always requires an existing workspace. `--verify-command` can be
omitted only when Collie detects a project check; startup fails if no check is
available or the baseline snapshot is incomplete. Per-Mission `--provider` and
`--model` freeze the direct route without changing global Settings. Native
overnight currently requires `anthropic-oauth` and an explicit model such as
`claude-opus-4-8`; Codex OAuth is not an overnight route.
`--no-paid-overage` records the
user's provider-side attestation. Collie then locks requests to Anthropic's official
Messages endpoint, disables ambient proxy and API/provider/CLI fallback, and reruns
the subscription guard at creation and every later runnable boundary. It uses
Collie's own system/tool contract; `claude -p` is benchmark/compatibility-only and
is never the native Mission runtime. Hitting a plan limit waits or asks for the
user; it never buys, reloads, or switches to metered billing automatically.
On the account tested on 2026-08-12, the direct probe returns HTTP 429 while the
official Claude Code client works, so this command is currently denied. Collie
does not replace it with `claude -p`, copy Claude Code's system prompt, or imply
that a paid Claude plan includes raw Messages API access.
Collie also does not implement Claude Code's private token-refresh protocol: the
current login-store token must already span the entire 12-hour active window, or
startup fails closed. A short-lived token plus a refresh token is not treated as
proof that a Collie-owned unattended route can last overnight.

## Configuration precedence

`COLLIE_<KEY>` environment variable → `~/.collie/settings.json` (the Settings panel /
`collie config`) → built-in default. A hard-set env var always wins. A token/cost budget
(`COLLIE_MAX_COST` / `COLLIE_MAX_TOTAL_TOKENS`) stops a run at a ceiling.

For first-party identity, explicit `collie web --name` → hard-set `COLLIE_COMPANION_NAME` → saved
`COMPANION_NAME` → a single kennel dog → generic `Collie`. The saved name is display-only across
Home, Mobile, Remote, and Ambient; Slack apps, `@` handles, and dog-mail addresses keep their own
external identities until renamed through those systems' workflows.
