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
| `collie run "<task>" --runner codex-exec` | Hand the task to an external coding harness as the worker. Collie still owns budget, approval, verification, the receipt, and cancellation. |
| `collie runners` | What this machine can use as a worker: install state, version, login route, billing class, capabilities. |
| `collie selftest` | $0 deterministic end-to-end (mock model, real tools). |

## Choosing the worker (`--runner`)

`--provider`/`--model` choose the **brain** — who thinks. `--runner` chooses the **worker** — who
actually carries the task out: whose tool loop, whose sandbox, whose login. The default is
`collie`, Collie's own harness, and it is also the only worker with the browser, desktop, MCP, and
per-action approval tools.

```bash
collie run "add a regression test for the parser" --runner codex-exec
collie run "tidy the docstrings in harness/ops.py" --runner claude-code
collie run "…" --runner auto        # choose per task, but only inside RUNNER_POOL
```

| Value | Worker |
|---|---|
| `collie` | Collie's own harness. Default; also the fallback for every other value. |
| `auto` | Choose per task from the members listed in `RUNNER_POOL`, and never from outside it. |
| `codex-exec` | OpenAI Codex CLI (`codex exec --json`), under your existing `codex login`. |
| `codex-sdk` | Official OpenAI Codex Python SDK in a sanitized background sidecar; optional `collie-harness[codex]`. |
| `codex-app-server` | OpenAI Codex App Server (experimental local stdio JSON-RPC), with approval round-trips, steer, and interrupt. |
| `claude-code` | Claude Code (`claude -p`), under your existing `claude login`. |
| `pi-rpc` | Pi RPC with file tools only, native steer/follow-up/fork/compact, and no shell. |

What does **not** change when you pick an external worker: the budget ceiling, the approval policy,
the verification gate, the receipt, session persistence, and cancellation are all still Collie's.
The worker saying it is done is not a completion signal — `verified` is written only by a host
check that actually ran. What does change: the worker runs its own tools inside its own sandbox and
its work is billed to *its* login. Only App Server currently has a tool-approval round-trip; other
external routes deny approvals or omit shell.

Persist a choice with `collie config RUNNER codex-exec`, and set the pool `auto` may draw from with
`collie config RUNNER_POOL "collie,codex-exec"`. Listing an external worker in `RUNNER_POOL` is the
consent to use its login and billing route; a worker that is not in the pool is never picked
automatically, even when it is installed and logged in. An explicit `--runner` overrides both — and
if the worker you named cannot run, the command exits with the reason instead of quietly using a
different one. `--persona` and `--goal` are not supported with an external worker.

## Inspecting workers (`collie runners`)

| Command | What it does |
|---|---|
| `collie runners` | Table of every known worker: key, phase, installed, version, login, billing class, last compatibility result, caveats. |
| `collie runners probe <key>` | One worker in detail. Read-only metadata: `shutil.which`, `--version`, whether a login file exists and when it expires. No network, no credential is read. |
| `collie runners probe <key> --live` | Additionally ask the vendor CLI's own status command (`claude auth status --json`, `codex login status`) so the billing class is attested rather than guessed. |
| `collie runners compat [--runners a,b] [--live] [--report PATH]` | Run the conformance matrix and write a dated JSON + Markdown report. |
| Any of the above `--json` | Machine-readable output. |

`compat` checks each worker on the same terms: probe shape, child-environment hygiene, CLI
handshake and minimum version, frame parsing under malformed output, that the worker's own
goal/scheduler control plane is disabled, and that its billing class is one Collie recognises.
`--live` adds the columns that cost tokens — a real one-turn edit, a resume, a cancel, and a usage
readback. Capabilities the latest report could not verify on this host are downgraded to unavailable
rather than assumed, which is why the table describes your machine and not the design intent.

Later-phase Prime and Hermes rows run only a read-only admission fingerprint against their
documented programmatic CLI surface. A PASS there means “candidate binary/protocol recognized,” not
“adapter enabled”; normal isolation, billing, framing, cancel, and live-turn columns remain gated.
Pi is phase 2 and runs the normal offline conformance columns.

See [Workers](runners.md) for the per-worker capability and boundary tables.

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
| `collie library publisher-payload \| publishers \| publisher-trust \| publisher-untrust` | Produce externally signable package bytes and manage exact local Ed25519 publisher-key trust. Publisher trust never approves authority scopes. |
| `collie doctor [--no-probe]` | Diagnose version drift, durable recovery, credentials, services, and notification delivery without changing state. |
| `collie resilience matrix [--report PATH]` | Run isolated network/restart/corruption/disk/tamper/process-kill fault injections without a model or network. |
| `collie resilience soak --duration 12h --interval 5m --report PATH` | Repeat the fault matrix with an atomic, restartable checkpoint. |

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
| `collie mission start "<goal>" --code --workspace PATH --overnight --provider claude-agent-sdk --model claude-opus-4-8 --no-paid-overage --verify-command "python -m pytest -q"` | Start Collie's bounded native Opus route through the official Claude Agent SDK. Startup first runs an isolated SDK inference probe and fails closed if the subscription route is unavailable. |
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
`--model` freeze the SDK route without changing global Settings. Native
overnight currently requires `claude-agent-sdk` and an explicit model such as
`claude-opus-4-8`; Codex OAuth is not an overnight route.
`--no-paid-overage` records the
user's provider-side attestation. Collie invokes Anthropic's official Claude Agent
SDK directly—not `claude -p` and not a raw OAuth Messages call—with Collie's custom
replacement system prompt. `setting_sources=[]`; SDK built-in tools, skills,
plugins, agents, slash commands, MCP servers, and fallback model are disabled. The
worker environment excludes API keys and routing/proxy overrides, and there is no
API-key, paid-credit, provider, or model fallback. Hitting a plan limit waits or
asks for the user; it never buys, reloads, or switches to metered billing
automatically.

The route accepts an eligible signed-in Pro/Max plan (live-tested on Max) and remains subject to its
limits. Current validation is a short end-to-end test, not a 12-hour soak. The
12-active-hour Mission leash is a maximum authority envelope, not a promise of
unlimited use, a completed overnight endurance result, or a guarantee that future
provider policy will remain unchanged.

## Configuration precedence

`COLLIE_<KEY>` environment variable → `~/.collie/settings.json` (the Settings panel /
`collie config`) → built-in default. A hard-set env var always wins. A token/cost budget
(`COLLIE_MAX_COST` / `COLLIE_MAX_TOTAL_TOKENS`) stops a run at a ceiling.

For first-party identity, explicit `collie web --name` → hard-set `COLLIE_COMPANION_NAME` → saved
`COMPANION_NAME` → a single kennel dog → generic `Collie`. The saved name is display-only across
Home, Mobile, Remote, and Ambient; Slack apps, `@` handles, and dog-mail addresses keep their own
external identities until renamed through those systems' workflows.
