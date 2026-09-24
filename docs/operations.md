# Operations, recovery, and resilience

The Control Center makes Collie's durable background state inspectable in one place. Open
`collie web`, then choose **Control Center** to move between Connected Mode, recovery, Automation Studio, memory,
budgets, and permissions. These views summarize metadata and counts; notification bodies, prompts,
credentials, and private model output are not copied into health reports.

## Diagnose before repairing

```bash
collie doctor
collie doctor --no-probe
collie activity --health
```

`doctor` checks packaged/backend version drift, runner compatibility evidence, stale services,
notification backlog age and dead letters, credential expiry metadata, and durable work waiting for
recovery. It is read-only unless `--repair` names one bounded action. Retrying a dead-letter queue
requires `--yes`; Collie does not infer that an uncertain external action is safe to replay.

```bash
collie doctor --repair test_notifications
collie doctor --repair retry_dead_notifications --yes
collie doctor --repair reprobe_workers
collie recovery ls
collie recovery show <session>
collie recovery reconcile <session> --resolution not_fired --note "checked target" --yes
```

Recovery reconciliation is an evidence statement made by the operator. `completed`, `not_fired`,
and `cancel` do not mean “try again”; they record what inspection established at the uncertain
boundary so the session can move forward without a blind duplicate effect.

## Automation Studio

Automation Studio provides reviewable recipes, JSON preview, enable/disable, and manual-run actions
over the same durable automation store used by `collie automations`. Every recipe freezes its
trigger, context policy, workspace mode, budgets, notification policy, and exact permissions before
it is enabled. A crashed read-only execution can be reclaimed within its retry budget; an expired
lease that may have written externally moves to **Needs You**. Stale lease tokens cannot publish a
late result.

A finished execution in **Needs You** keeps asking until somebody says they read it. **Mark
reviewed** — offered next to *Open automation* in the recovery lane and next to the run in
Execution history — records that acknowledgement durably for that one execution
(`POST /api/automations/review` with its `execution_id`; authenticated, no confirmation dialog,
because it changes nothing outside Collie). It runs, retries and resumes nothing, and the run keeps
its state, error, receipt, saved conversation and history row; only the attention projection stops
counting it. Acknowledgement is per execution on purpose: two daily runs can strand two different
pieces of work, so a later success never speaks for an earlier incident and each new **Needs You**
arrives unreviewed. A run that is still pending, claimed or running cannot be marked reviewed.

### Automation budgets

An automation is bounded by its budget, and every key is frozen into the spec when it is saved:

| Key | Default | Meaning |
| --- | --- | --- |
| `max_wall_s` | 1800 | Hard wall-clock ceiling; the child process tree is killed at it. |
| `max_model_tokens` | 200000 | Run token budget, checked against reported usage. |
| `max_cost_usd` | 25 | Run cost budget, checked against reported spend. |
| `max_actions` | 100 | Hard ceiling on tool calls. |
| `max_runs_per_day` | 24 | How often the trigger may start a run. |
| `max_retries` | 1 | Reclaim attempts after a crashed read-only execution. 0 disables retries. |
| `max_turns` | 0 | Optional hard ceiling on model turns. **0 = no turn ceiling.** |

The first five must be positive: they are what actually bounds an unattended run, so "unlimited
turns" is only safe while they stay mandatory. Four of them — `max_wall_s`, `max_model_tokens`,
`max_cost_usd` and `max_actions` — are metered against observed usage during the run. Metered is
not the same as rate-limited: `max_runs_per_day` bounds how often the trigger may start a run and
is never consumed inside one. Only `max_turns` and `max_retries` accept 0. A model response can
cross a token or cost budget before its usage is known. That run stops as **Needs You**, keeping
its result and usage receipt, including any usage above the configured value.

A separate turn ceiling can stop a task while wall, token and cost budgets remain available:
`stop_reason: turn_limit`, status **Needs You**. New automations therefore default to `max_turns: 0`, and the Automation
Studio editor carries a stored cap through untouched instead of rewriting it. An automation that
already has an explicit positive cap keeps it exactly as written; nothing migrates existing values.
An accepted cap is applied as written, including values above the Settings panel's interactive
turn-cap range, which bounds keyboard surfaces rather than accepted automation budgets.

The Automation Studio editor shows `max_runs_per_day`, `max_model_tokens`, `max_cost_usd`,
`max_wall_s` and `max_actions`, and its help names every ceiling that can end a run — including a
turn cap set outside the panel, with the number it carries. `max_actions` is a tool-call ceiling,
not a turn ceiling: a run that exhausts it stops the same way, so it is editable rather than
implied.

Saving replaces the whole stored record, so the editor starts from the automation exactly as it was
accepted and overlays only the fields the person changed. Everything the form does not show —
`context`, `execution` (a plan automation stays a plan automation), `notifications`, workspace
options, `permissions.write_roots`/`tools`/`desktop_targets`, the remaining budget keys, and the
trigger's predicate and scheduling fields — is carried through unchanged. Choosing a different
trigger type replaces the entire trigger with that type's form values; old predicates and scheduling fields are discarded. Authority follows
the choice that grants it: picking the current workspace or a webhook trigger authorizes it, and
leaving either alone keeps exactly the authority that was accepted, neither widened nor dropped.

Separately from all of this, the loop uses a **soft convergence target** (50 turns when no cap is
set) to decide when to stop exploring, nudge toward a commit, and withdraw exploration tools. That
target only changes how the run is steered; it never terminates the work.

Memory review shows proposed, attested, verified, and rejected claims separately. Budget views show
limits alongside observed usage rather than treating unknown usage as zero. The permissions view
shows authority by surface and target; package publisher trust and package scope approval remain
separate decisions.

## Failure matrix

The deterministic matrix uses only private temporary stores and Collie-owned child processes. It
does not contact a model, open the network, modify settings, or kill an unrelated process.

```bash
collie resilience scenarios
collie resilience matrix
collie resilience matrix --json --report resilience-matrix.json
```

The current scenarios exercise:

- notification disconnect, bounded retry, dead letter, explicit retry, and reconnect drain;
- daemon restart, expired automation leases, replay-safe recovery, unsafe-work parking, and stale
  owner fencing;
- corrupt durable JSON being parked before claim;
- simulated disk-full failure preserving the prior atomic update journal, followed by startup
  failure escalation to a declared rollback plan;
- extension bytes changing after review and being refused by the exact digest pin;
- an App Server peer ignoring native interrupt, followed by owned process-tree extinction.

## Restartable soak

```bash
# short local qualification
collie resilience soak --duration 10m --interval 30s --report ~/.collie/soak.json

# release qualification
collie resilience soak --duration 12h --interval 5m --report ~/.collie/soak-12h.json

# after a reboot or sleep, repeat the same command/path to resume
collie resilience status --report ~/.collie/soak-12h.json
```

Every cycle is atomically checkpointed. Reopening an unfinished report resumes it, and a long gap is
counted as a sleep/restart observation. The final status is `PASS` only when every completed cycle
passed. Implementing this harness is not itself evidence of a 12-hour run; attach the finished JSON
report to a release qualification when that run has actually completed.

## Completion and integration contracts

Every Mission status response contains `summary.completion` with exactly five fields:
`status`, `summary`, `evidence`, `artifacts`, and `next_action`. A durable `done_verified` state is
projected as `verified` only when its stored goal-verification event contains positive independent
evidence; older or incomplete records are shown as `accepted`. Artifact references come from the
Mission's artifact ledger, not from activity labels or model prose.

The integration library includes two fail-closed boundaries for hosts that embed Collie:

- `SignedWebhookClient` sends bounded JSON only to one exact allowlisted HTTPS host, rejects
  credential-bearing URLs, redirects, and private/link-local DNS answers, and signs timestamp,
  nonce, body digest, and idempotency key with HMAC-SHA256.
- `DelegationEnvelope` signs an audience-bound, expiring child scope and refuses any tool, path,
  network, token, or cost authority broader than its parent. `LiveSessionSupervisor` adds a bounded
  pause/resume/cancel/steer/follow-up queue and heartbeat lease without retaining audio,
  transcripts, credentials, or native process handles.

These are library contracts, not an automatically opened webhook, voice, or A2A network service.
An embedding surface must supply the secret, destination allowlist, replay store, and transport
explicitly; Collie does not infer or advertise new network authority from their presence.

## Update channels

`collie update --channel stable` excludes prereleases. `--channel beta` considers both prerelease
and stable tags using semantic-version precedence, so a later stable release still supersedes an
older beta. Downloads retain the existing digest and platform-signature checks. A stable release tag
also publishes the wheel and source distribution through PyPI Trusted Publishing once the repository
and workflow are registered as that project's Trusted Publisher; no PyPI API token is stored in the
repository.

The desktop reads the last check from `~/.collie/update-status.json` (`GET /api/update`, which never
waits on the network). A check happens on `POST /api/update/check` or, with `UPDATE_CHECK=on`, at
most about once a day in the background; a failed check keeps the last answer and records the error
instead of reporting "up to date". `POST /api/update/install` is accepted only from a loopback,
non-relayed request on a Windows installer copy. It runs `collie update --yes --expect <version>`
as a child process, logging to `~/.collie/logs/update-install.log`; `--expect` makes the CLI refuse
(exit 3) unless that exact newer release is still the latest. The Windows handoff then proceeds as
for `collie update --yes`, and the update journal records the target version so the restarted
server can tell an installed update from one that did not take effect.
