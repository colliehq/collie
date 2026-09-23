# Workers

A **worker** is whoever actually carries a task out: whose agent loop runs, whose tools get called,
whose sandbox contains them, and whose login pays for the tokens. By default the worker is Collie's
own harness. From 0.21.27 you can also hire an external coding harness — OpenAI's Codex CLI, or
Claude Code — for a single run, without giving up anything Collie was already guaranteeing you.
Version 0.22.0 adds three reviewed routes: Codex App Server for interactive approval/steer, the
official Codex Python SDK in a sanitized background sidecar, and Pi RPC with shell disabled. Collie
remains the control plane in all three.

The 0.28.0 release retains Codex CLI/Python SDK 0.155.1 and Claude Agent SDK
0.2.157. Windows installers include Claude Code 2.1.278. The Codex SDK route
uses the native CLI distributed with its pinned Python dependency, so installing
the `codex` extra does not also require a separate CLI on `PATH`. The `PATH`
routes additionally tolerate Codex CLI 0.156's removed Windows sandbox setting —
see [the version split](#windows-sandbox-and-the-codex-cli-version-split), which
also records what that has and has not been verified against. See the
[release review](release-0.28.0.md) for validation scope.

## Worker is not the same thing as brain

| | Setting | Per-run flag | Question |
|---|---|---|---|
| **Brain** | `PROVIDER`, `MODEL` | `--provider`, `--model` | Which model thinks? See [Providers](providers.md). |
| **Worker** | `RUNNER`, `RUNNER_POOL` | `--runner` | Which harness does the work? |

With the default worker (`collie`) the two collapse into one: Collie runs its own loop and calls the
provider you configured. Pick an external worker and they come apart — that CLI arrives with its own
model, its own login, and its own tools, so `PROVIDER`/`MODEL` stop describing who thinks for that
run. This is also why `claude-cli` (a *provider*: one `claude -p` inference inside Collie's loop) and
`claude-code` (a *worker*: Claude Code runs the whole loop itself) are different things with
confusingly similar names.

## What stays Collie's, whichever worker you pick

Hiring an external worker does not move the control plane. It stays here:

- **Budget.** `MAX_COST` / `MAX_TOTAL_TOKENS` and the Mission leash still bound the run.
- **Verification.** `verified` is written only by a host check that Collie executed. A worker
  announcing "done" in prose is a claim, not evidence; a run where the worker declared success and
  the check failed is a failed run.
- **The receipt.** Every run records which worker ran it, its credential family, its billing class,
  and its usage — or records explicitly that usage is unknown, rather than reporting a zero.
- **Sessions and resume.** The session ID you resume with is Collie's. The worker's own thread or
  session identifier is stored as a locator inside the receipt and is never the thing you type.
- **Cancellation.** External workers launch through the same start gate and Job/process-group
  ownership Collie uses everywhere else, so a cancel kills the whole process tree and can prove it.

And what genuinely changes: an external worker runs its own tools inside its own sandbox and its
work is billed to *its* login rather than to your configured provider. Only adapters with an
explicit interaction channel can bring a native approval back to Collie's Gate; `codex-exec`, the
Codex SDK background route, Claude Code, and Pi therefore stay fail-closed or omit shell entirely.

## Choosing a worker

```bash
# one run
collie run "add a regression test for the parser" --runner codex-exec

# persist a default
collie config RUNNER claude-code

# let Collie choose, but only from workers you have consented to
collie config RUNNER auto
collie config RUNNER_POOL "collie,codex-exec"
```

**`RUNNER`** — which worker to use: `collie` (the default), `auto`, or a specific key such as
`codex-exec` or `claude-code`. Leaving it at `collie` is a genuine no-op: nothing probes an external
CLI, no extra subprocess starts, and the run takes the same code path it always did.

**`RUNNER_POOL`** — a comma-separated list, best first, of the workers `auto` may choose from. Order
is also the tie-break preference. **Writing an external worker here is the consent to use its login
and billing route for your tasks.** A worker that is not in the pool is never chosen automatically,
even when it is installed, logged in, and would have been the better fit.

`--runner` on a single run overrides both. If the worker you named cannot run — not installed, not
logged in, too old, or unable to do something the task needs — the command exits with the reason.
It never silently substitutes a different worker, because a substitution would move your billing to
a different account without asking. Automatic fallback happens in exactly one situation: `auto`
picked a worker, the process failed to start at all, and a same-login, same-billing alternative
exists. Once a worker has been handed the prompt there is no fallback, because a second worker would
be re-doing work whose side effects already exist on disk.

Some things force the worker back to `collie` regardless of the setting: plan and review modes
(read-only work Collie's own gate performs), Missions running overnight, chat/REPL/TUI/ACP surfaces,
and any task that needs a tool no external worker has. `--persona` and `--goal` are not supported
with an external worker.

## Inspecting what this machine can use

```bash
collie runners                          # the table
collie runners probe codex-exec         # one worker, metadata only
collie runners probe codex-exec --live  # additionally ask the CLI's own status command
collie runners compat --runners claude-code,codex-exec --live --report compat.json
collie runners compat --runners codex-app-server --report app-server-compat.json
collie runners compat --runners codex-sdk,pi-rpc --report phase2-compat.json
```

A plain probe is deliberately cheap and read-only: it resolves the binary on `PATH`, reads
`--version`, and checks whether a login file exists and when its token expires. It reads no
credential, makes no network request, and its results are cached for a minute. `--live` additionally
runs the vendor's own status command (`claude auth status --json`, `codex login status`) so that the
billing class below is attested by the vendor rather than inferred by us.

`collie runners compat` runs the conformance matrix and writes a dated JSON and Markdown report.
Offline columns — probe shape, child-environment hygiene, CLI handshake and minimum version, frame
parsing under malformed output, proof that the worker's own goal/scheduler control plane is
disabled, and billing-class validity — cost nothing and run anywhere. `--live` adds the columns that
spend tokens: a real one-turn edit against a fixture repository, a resume of that same session, a
cancel, and a usage readback. Capabilities the most recent report could not verify **on this host**
are downgraded to unavailable rather than assumed, so the table describes your machine, not our
intentions.

## The workers

### `collie` — Collie's own harness

The default and the fallback. It is the only worker with the browser, desktop, MCP, web-search,
email and Slack tools, the only one that can ask you to approve an individual action, and the only
one Missions can meter per request. It works in any directory; a Git workspace only improves the
change evidence. Billing follows your configured provider.

### `codex-exec` — OpenAI Codex CLI

Runs `codex exec --json` under your existing `codex login`, minimum CLI version 0.149.0. The prompt
goes in over stdin, never as a command-line argument. The sandbox is `--sandbox workspace-write`, so
it can edit files and run shell commands inside the workspace, and approvals are set to `never` —
`codex exec` answers every approval request by rejecting it, which is fail-closed but also means
there is no channel back into Collie's gate. Its own goals surface is not used. Usage comes back as
token counts (never dollars). Each complete native JSONL event is forwarded while the process is
running and then parsed again from the full captured stream before the turn can settle. The CLI does
not expose partial token deltas, so streaming is event-level rather than character-level.

### `claude-code` — Claude Code

Runs `claude -p --output-format stream-json --verbose` under your existing `claude login`, minimum
version 2.1.221. Complete system, assistant, tool and terminal result records are shown live and are
also retained in the runner snapshot and receipt digest.
The prompt goes in over stdin. The tool list is pinned to exactly five file tools — `Read`, `Edit`,
`Write`, `Grep`, `Glob` — and **there is no shell**, because with no approval channel back into
Collie's gate an unreviewed `Bash` would be an unbounded action on your machine. Sessions are
resumable through a Collie-generated UUID. Safe mode, Chrome integration off, slash commands off,
prompt suggestions off, and strict empty MCP configuration are pinned on both start and resume, so
the user's plugins/skills/browser bridge cannot silently enlarge that five-tool surface. It is the
only worker that reports a dollar cost of its own. Reach for `codex-exec` or `collie` when the task
needs to run commands rather than only edit files.

An explicit reasoning effort travels to this worker as `--effort <level>` (`low`, `medium`, `high`,
`xhigh`, `max`), on the first turn and again on every resumed one, because the flag is scoped to the
session and each turn is a new process. Auto passes no flag and leaves the CLI's own default alone.
A level the CLI does not document is refused before anything starts rather than run at the default:
`claude` itself only warns on stderr and continues, which would silently spend a run at a level
nobody chose.

### `codex-app-server` — OpenAI Codex App Server

Runs Codex's documented **experimental** App Server JSON-RPC protocol over local stdio, minimum
Codex CLI 0.149.0. Stability is earned by the pinned CLI version and Collie's conformance matrix,
not inferred from the command name. It starts or resumes one thread, starts one turn, streams native
events, supports `turn/steer`, and requests
`turn/interrupt` before escalating to Collie's owned process-tree kill. Command and file-change
approval requests round-trip to Collie's callback; no callback means `decline`. Unknown server
requests receive a JSON-RPC method-not-found error, and broad permission requests receive an empty
grant.

On the CLI that callback uses the same Gate and attended TTY approver as Collie's native loop. On
Web it parks the same idempotent Inbox item used by desktop and phone approval cards. Project-mode
commands and workspace file changes may be auto-approved by the Gate; Interactive mode waits for an
explicit answer. Mid-turn Web messages are relayed to `turn/steer`, including messages queued during
the small process-start race.

App Server has no `--ignore-user-config` switch, so the launch uses strict config and pins empty MCP
and plugin tables, disables web search, project instructions, hooks, memories, multi-agent tools and
apps, and re-pins `workspace-write` plus `on-request` when a thread is resumed. It uses neither the
experimental WebSocket transport nor `thread/goal/*`; a native goal surface existing in Codex is not
permission for a second planner to continue Collie's Mission.

### `codex-sdk` — official OpenAI Codex Python SDK

This optional route (`pip install "collie-harness[codex]"`) is for background slices. The official
SDK runs in a separate sanitized Python process because SDK environment settings extend the current
environment; putting it in-process would let ambient API keys and endpoint overrides cross the
billing boundary. The worker pins `workspace-write`, denies every approval, disables MCP, plugins,
web, hooks, memory, apps, multi-agent tools, and project instructions, and reports token usage.

It accepts text, data-image URLs, and local image files, and supports native thread resume, fork,
and compaction. It deliberately does not pretend a one-request sidecar can steer a live turn; use
`codex-app-server` when approval or mid-turn interaction matters. Malformed LFJSONL, duplicate or
non-terminal result frames, and literal records after completion make the slice unsettled.

### Windows sandbox, and the Codex CLI version split

On Windows the Codex routes pin `windows.sandbox="unelevated"` on every launch. Without it, Codex
rewrites an explicitly requested `workspace-write` sandbox to read-only whenever no Windows sandbox
level is configured, and `--ignore-user-config` removes the one your `config.toml` would have
supplied — so the turn refuses every write *and still exits 0*. `elevated` is never chosen for you
(it needs a one-time administrator install) and 0.156's new `mxc` mode is never chosen either
(it maps to a disabled sandbox). Collie never falls back to an unrestricted or elevated run.

A second override, `windows.sandbox_private_desktop=false`, is **version-scoped**. Codex 0.156.0
removed that field from its config schema, and Collie launches with `--strict-config`, under which
an unknown `-c` field is a hard startup error — so passing it to a 0.156 CLI kills the launch before
any model call. On older CLIs it is still required: measured on Windows 11 / Codex 0.149.0, a
private-desktop sandbox under Collie's no-window start gate silently refused every write, and
turning the desktop off was what made the same prompt write its file.

The `codex-exec` and `codex-app-server` routes therefore ask the executable they actually resolved
(not whatever `codex` is first on `PATH`, and not the Python SDK pin) for its version, once per
binary, and send the override only below 0.156.0. The answer is cached and re-taken as soon as that
file changes or is replaced in place, and in any case within fifteen minutes — an installer can
repoint a stable `codex.cmd` shim at a new package binary without touching the shim itself, so a
cached answer is never held for the life of the process. Only Codex's own `codex-cli <version>`
banner (or a line that is nothing but a version) is read; anything else counts as unknown rather
than being mined for the first version-shaped number in it. If the version cannot be read or parsed,
Collie keeps the measured override: against a 0.156 host that fails loudly at startup and changes
nothing, whereas dropping it against the host that was actually measured would reinstate a sandbox
that quietly accomplishes nothing.

What this change is verified to do is let a 0.156 CLI start: the launch that previously died with
`unknown configuration field` now completes `initialize`, model list and thread start. What it does
not settle is whether a 0.156 sandbox then writes. 0.156 makes the private desktop unconditional,
which is the configuration the 0.149 measurement above found fatal under Collie's start gate — the
implementation changed underneath (the desktop is now shared and cached, and a failure to get one is
now a hard error rather than a silent degrade), but that is a reason to retest rather than evidence.
Collie still treats a turn that changed nothing while its sandbox refused every write as a failure,
so this fails visibly rather than banking empty progress.

`codex-sdk` is on the other side of that split and is **not** version-gated. It runs the native
binary bundled with its pinned `openai-codex` dependency (0.155.1 in this release), never one from
`PATH`, so upgrading the CLI on your machine does not change what that route executes. Two
consequences: a 0.156 host runs 0.156 for `codex-exec`/`codex-app-server` and 0.155.1 for
`codex-sdk`, and bumping the `openai-codex` pin to 0.156 or later is a prerequisite for — and must
be done in the same change as — removing the override from `harness/codex_sdk_worker.py`. That
sidecar launches without `--strict-config`, so a stale key there would be ignored with a warning
rather than refused, which is exactly the shape that produced the original silent write failure.

### `pi-rpc` — Pi RPC with an explicit file-tool boundary

Pi runs in `--mode rpc` with exactly `read,edit,write,grep,find,ls`. Bash is absent because the RPC
protocol has no tool-level approval round-trip. Extensions, skills, prompt templates, context files,
and project trust prompts are disabled on every launch. Native RPC provides distinct steer and
follow-up queues, abort, resume, fork, compaction, final assistant text, token usage, and reported
cost. A written abort is not treated as cancellation proof: if the peer does not settle promptly,
Collie escalates to its owned process tree.

Pi authentication is probed read-only with `pi auth check --no-refresh`. Its billing class remains
unknown unless the route can be evidenced, so `--no-paid-overage` still refuses rather than guessing.

### Declared capabilities

Declared, not promised: `collie runners` shows these intersected with what the last compatibility
report actually verified here.

| | `collie` | `codex-exec` | `codex-sdk` | `codex-app-server` | `claude-code` | `pi-rpc` |
|---|---|---|---|---|---|---|
| Tools | code, shell, browser/apps | code, shell | code, shell | code, shell | five file tools | six file tools |
| Shell | gated per action | workspace sandbox | workspace sandbox | workspace sandbox | no | no |
| Resume / fork | yes / yes | yes / no | yes / yes | yes / no | yes / no | yes / yes |
| Steer / follow-up | yes / yes | no / no | no / no | yes / no | no / no | yes / yes |
| Input | text + native tools | text | text + URL/file images | text | text | text |
| Streaming | yes | complete JSONL | terminal sidecar events | JSON-RPC events | stream-json | RPC events |
| Cancel | native + tree | tree | tree | native + tree | tree | native + tree |
| Gate approvals | yes | no; rejects | no; rejects | yes; default decline | no shell | no shell |
| Usage tokens / cost | yes / yes | yes / no | yes / no | unverified / no | yes / yes | yes / yes |
| Confinement | gate + leash | `workspace-write` | `workspace-write` | `workspace-write` | tool allowlist | tool allowlist |

### Phase-3 declarations: Prime and Hermes

`prime-rpc`, `hermes-gateway`, and `hermes-acp` are visible so their intended billing, protocol, and
authority boundaries can be reviewed, but they are not selectable. The offline matrix
runs one `admission` fingerprint: it may read only the candidate's version/help output and verify
that the documented RPC/ACP entry point exists. That PASS is deliberately insufficient for a
compatibility badge. Each adapter remains disabled until its separate framing, isolation,
double-control, cancellation, billing, real-turn, resume, and usage evidence passes in the phase
that implements it. Hermes Gateway's JSON-RPC wire adapter is implemented and fixture-tested,
including stored-vs-runtime session identity, approval denial, resume, fork, compaction, and cancel.
Real launch still requires an explicit Docker/Podman/nerdctl command and remains phase-gated: a bare
gateway inherits Hermes profiles, plugins, skills, MCP servers, schedulers, secrets, and shell. Sudo
and secret requests are always answered empty. Prime and Hermes were not installed on this host, so
no live runtime claim is made about either.

## Billing classes

Every worker's route is labelled with one of five classes, and that label goes into the receipt:

| Class | Meaning |
|---|---|
| `subscription_allowance` | A signed-in plan's included allowance (a ChatGPT plan through `codex login`, a Claude Pro/Max plan through `claude login`). |
| `paid_overage` | A plan route that will bill beyond its included allowance. |
| `api_metered` | An API key: metered per token. A `codex`/`claude` login that turns out to be key-backed lands here, not in `subscription_allowance`. |
| `local` | A local model. Nothing is billed and nothing leaves the machine. |
| `unknown` | Not attested. Treated as the worst case. |

A login is not by itself proof of zero marginal charge, so the classes are evidence-based: a
`subscription_allowance` claim needs the vendor's own status output behind it (and for Codex, recent
enough account evidence), and `--live` is what fetches it. Under `--no-paid-overage`, or any run
with a cost ceiling, an `unknown` class is refused rather than gambled on.

Two guards run before any external worker starts. The child environment is rebuilt from an allowlist
rather than inherited, so no proxy setting, provider override or credential of Collie's reaches the
worker; the worker reads its own login file itself, and Collie records only metadata about it. And
if the *parent* environment holds an API key or OAuth token that would redirect the worker onto
metered billing — `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` and relatives —
the run is refused before launch rather than quietly charged to it.

## Latest compatibility check

*No report has been recorded in this file yet.*

Run `collie runners compat --live --report <path>` and paste the Markdown report it writes here,
with the date and the machine it ran on. Until then, every "verified" claim in the capability table
above is the design's declaration rather than an observation of your host — which is exactly what
`collie runners` will tell you if you ask it.

## Current surfaces and boundaries

Worker selection is wired through `collie run`, Web, Pack, and durable non-overnight Mission code
slices. Each surface records the selected worker in its receipt; Web also emits the resolved Run Plan
before work begins, and complete Codex/Claude/App Server/Pi native events are forwarded live. Pack creates a private
Git baseline for every candidate and lets Collie's host verifier choose the winner. Mission freezes
the worker and billing route into its leash, re-probes login/billing/quota evidence at every runnable
boundary, and keeps the worker's native session locator for the next slice.

`auto` ranks only the consented pool using pool order, capability fit, verified-run history, billing
preference, recent 429 cooldowns, and known plan headroom. Codex headroom is obtained with a read-only
`account/rateLimits/read` app-server handshake; the probe creates no thread and sends no model prompt.
If every route, including Collie, fails a hard rule, Auto refuses; it never overrides H5
`no_paid_overage`/`subscription_only` just to produce a fallback.
Unknown quota remains visibly unknown and receives no ranking credit. Desktop and mobile show the
worst observed quota window and its reset time; the snapshot cache is scoped to the resolved Codex
binary and `CODEX_HOME`, so two local logins cannot borrow one another's signal.

External output is treated as a bounded protocol, not as an arbitrary terminal transcript. JSONL is
split only on LF (an optional CR is removed), so literal `U+2028`/`U+2029` inside a JSON string remain
data. Non-standard `NaN`/`Infinity`, cyclic values, and structures deeper than 64 levels become
explicit protocol-error evidence rather than entering a receipt or overflowing the redaction stack.
Stdout and stderr are drained concurrently and retained up to 16 MiB each; exceeding the bound
makes the turn unsettled instead of accepting a partial terminal frame. A live surface forwards at
most 200 native events plus an explicit omission marker, while the receipt still records the bounded
snapshot digest and event count. A single native event is capped at 128,000 characters before JSON
materialization, a persisted snapshot restores at most 10,000 tail events, and every numeric counter
must be finite and non-negative. Exactly one terminal event is required and it must be the final
record; plausible JSON after `turn.completed`/Claude's `result` makes the invocation unsettled.

Runner prompts, history recaps, events, final text, subprocess errors and verifier output are scrubbed
for credential-shaped values before they enter a stream, transcript or receipt. The host verifier
keeps only a redacted 4 KiB output tail. This is a last-resort containment boundary, not permission to
paste credentials into a task: the external worker itself receives only the permanently masked prompt
because its process cannot use Collie's reversible in-memory secret vault.

Pack and Mission also fail closed when an external worker omits a budgeted usage field. The value is
stored as `unknown`/`null`, never zero: Pack stops launching more paid candidates because it can no
longer enforce the remaining budget, and Mission moves to `needs_human` before another slice. A
subscription/local worker records zero *marginal* charge while retaining any reported or computable
API-equivalent value; a metered worker with unknown cost is not relabelled free.

Receipts are publication fences, not optional telemetry. Each external receipt now carries the
capability handshake used for that slice and any unresolved typed interactions; a paused
interaction cannot be serialized as a settled terminal result. CLI, Web and Pack arm a durable
`external_action` replay fence before the worker or copy-back sees the task. It is cleared only after
the receipt and visible transcript are durable and the worker reported no recovery requirement; an
unexpected exception, a partial Pack apply, an orphaned attempt directory, or a missing receipt leaves
the session in explicit recovery instead of allowing a blind retry. Pack receipts retain compact
evidence for every candidate and report cleanup failures rather than hiding them behind best-effort
deletion. Web, CLI, Pack and Mission keep a useful worker answer when a receipt write fails, but the
run becomes a visible failure and cannot be called verified. Mission persists a pre-edit baseline
before starting a worker and a post-slice ownership WAL
before another slice can run; a transcript or ownership-write exception becomes
`recovery_required`, while local stores are still closed independently. Existing session journals are
validated before every update. An unreadable or semantically torn journal is never overwritten as a
fresh conversation and appears in Activity as a recovery item requiring inspection.

Mission applies the same rule to its SQLite authority boundary. Leash, case, checkpoint and event
objects use standard JSON only; a non-object payload or `NaN`/`Infinity` is never interpreted with
default limits. A corrupt leash or case moves to `recovery_required` before the Mission can claim a
runner. Boolean autonomy, integer call/token/wall/storage bounds, money, expiry timestamps and both
frozen profile digests are shape-checked before persistence. Runtime measurements are finite and
non-negative or rejected, so malformed usage cannot be clamped to zero and undercharge the campaign.
The killable Mission code subprocess uses the same strict-object protocol for its request, start gate,
result and process-ownership receipt; an unreadable ownership receipt remains fenced.

The Run Setup UI distinguishes Pack's *base* workspace from its execution boundary: every candidate
runs in its own isolated worktree, the current files change only when “apply winner” is selected, and
both desktop and phone surfaces state that explicitly. The wire value remains `workspace=current`
because it names the baseline project; Pack itself owns candidate isolation and cleanup.

The same standard-JSON rule now covers the surrounding authority seams, not only runner events.
Web, pairing, the delegate dashboard, the logged-in browser bridge and `execute_code`'s privileged
tool RPC reject non-object bodies and `NaN`/`Infinity` before dispatch. Responses are also encoded
with non-finite numbers forbidden. Action proposals require object-shaped finite payloads, an exact
boolean daemon flag and a valid TTL; approval rechecks the HMAC-bound durable payload, and corrupt
approved JSON is terminally refused before the side effect. An incomplete action-integrity key no
longer falls back to a process-private replacement key.

Unattended automations apply the same policy to their specs, budgets, permission booleans, durable
requests, runtime usage and parent/child files. Truthy strings cannot grant filesystem, webhook,
current-workspace or external-write authority. A corrupt queued request is parked in `needs_you`
before claim, malformed usage cannot be clamped to zero, child results are atomically replaced, and
failure to remove the private execution directory is surfaced. The Claude Agent SDK worker and host
adapter likewise reject non-finite/fractional/negative token counters before accounting.

TaskTree specialist lanes no longer recover malformed durable JSON as an empty leash. New leash,
resource, progress and usage records use finite standard JSON; child numeric ceilings can only
narrow finite parent ceilings. A corrupt queued run is moved to `recovery_required` before claim,
and a corrupt ancestor blocks its descendant rather than substituting fresh default budgets.

The legacy Job executor now observes the same authority rule. `may`, irreversible mode and spend
cap have exact types; a non-finite cap cannot become an accidental unlimited budget. Corrupt durable
Job leashes are locked down and parked in `recovery_required`, paused/recovery/terminal Jobs cannot
fire a late confirmed action, and an already executed nonce can still replay its one durable receipt.
The CLI and delegate dashboard reject malformed shapes before creating a Job.

Long-running infrastructure is strict at startup too. Supervisor enable/critical switches are real
booleans, worker identities are unique, and every grace/backoff/poll interval is finite and bounded,
so a string `"false"` or `NaN` cannot silently start a worker or poison recovery scheduling. The
subscription sidecar rejects duplicate keys and non-standard JSON before transport and refuses to
coerce fractional, negative, string or boolean usage into plausible token counts.

Capability, probe, request and receipt JSON use strict booleans: the string `"false"` is not truthy.
That matters for `approval_round_trip`, steering, paid-overage attestation, `usage_known`, settlement
and recovery. Cache entries are scoped to the account/runtime identity, injected transports never
borrow one another's result, and a wall-clock rollback forces a refresh rather than extending stale
billing or quota evidence indefinitely.

The non-interactive phase-one CLIs still cannot steer a turn already in flight or round-trip an
approval into Collie's gate. Web exposes those capabilities explicitly and keeps the steering input
disabled for such runs. Codex is bounded by its workspace-write sandbox with approvals rejected;
Claude Code is bounded to its five file tools and receives no shell. Plan/review, chat, overnight
Mission, REPL/TUI/ACP, Slack, delegate, and automation work therefore stay on Collie's native harness.
