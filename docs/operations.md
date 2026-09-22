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
