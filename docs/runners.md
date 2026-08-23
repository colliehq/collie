# Workers

A **worker** is whoever actually carries a task out: whose agent loop runs, whose tools get called,
whose sandbox contains them, and whose login pays for the tokens. By default the worker is Collie's
own harness. From 0.21.27 you can also hire an external coding harness — OpenAI's Codex CLI, or
Claude Code — for a single run, without giving up anything Collie was already guaranteeing you.

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

And what genuinely changes: an external worker runs its own tools inside its own sandbox, so Collie
cannot approve or deny its individual actions, and its work is billed to *its* login rather than to
your configured provider.

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
token counts (never dollars). Events are replayed after the process exits rather than streamed, so a
`codex-exec` run shows its tool activity at the end rather than live.

### `claude-code` — Claude Code

Runs `claude -p --output-format json` under your existing `claude login`, minimum version 2.1.221.
The prompt goes in over stdin. The tool list is pinned to exactly five file tools — `Read`, `Edit`,
`Write`, `Grep`, `Glob` — and **there is no shell**, because with no approval channel back into
Collie's gate an unreviewed `Bash` would be an unbounded action on your machine. Sessions are
resumable through a Collie-generated UUID. It is the only worker that reports a dollar cost of its
own. Reach for `codex-exec` or `collie` when the task needs to run commands rather than only edit
files.

### Declared capabilities

Declared, not promised: `collie runners` shows these intersected with what the last compatibility
report actually verified here.

| | `collie` | `codex-exec` | `claude-code` |
|---|---|---|---|
| Tools | code, bash, browser, desktop, MCP, web search, email, Slack | code, bash | code (five file tools) |
| Shell | yes, gated per action | yes, inside the workspace sandbox | no |
| Resume a session | yes | yes (`exec resume`) | yes (`--resume`) |
| Steer mid-turn | yes | no — a new instruction becomes the next resume | no |
| Streaming events | yes | no — replayed after exit | no |
| Cancel | native + process tree | process tree | process tree |
| Approvals reach Collie's gate | yes — the gate *is* the channel | no (rejects, fail-closed) | no (no shell instead) |
| Usage: tokens / cost | yes / yes | yes / no | yes / yes |
| Plan quota signals | no | no | no |
| Per-request metering (Mission leash) | yes | no | no |
| Confinement | Collie's gate + leash, per action | `workspace-write` sandbox | tool allowlist |
| Needs a Git workspace | no | yes | yes |

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

## Not yet wired

As of 0.21.27 the worker selection is available on `collie run` only. Missions, Pack, and the web GUI
still run Collie's own harness; the selection layer and the Mission code-slice entry point exist and
are tested, but those call sites do not offer the choice yet. `auto` ranks candidates on pool order,
declared capability fit, past verified-run history, and billing preference; it does not yet see live
rate-limit cooldowns or remaining plan allowance. Streaming, mid-turn steering, and routing an
external worker's approval requests into Collie's gate are the next steps after that.
