# Mission architecture

Mission is Collie's durable mode for work that spans multiple actions or waits for
the outside world. Collie still chooses every next action; the container owns
persistence, authority, scheduling, concurrency, evidence, and lifecycle control.

## Explicit entry only

Ordinary messages are classified as `chat` or `code`. A model-produced `mission`
label is always collapsed to `chat`, regardless of confidence. Durable work starts
only from a user-authored command:

```text
/mission <goal>
/mission --review <goal>
/mission --domains=x.com,*.y.com --rate=6 <goal>
/mission list
/mission status|run|pause|resume|cancel|check|continue|accept|reconcile <id>
```

The slash parser currently belongs to the Web/Desktop chat surface; scripts use
`collie mission ...`. `/delegate` is a compatibility alias. Plain `/mission` uses
the saved **Mission autonomy** setting, whose default is Hands-off: available
actions inside the leash run without a confirmation at every send/publish step.
`--review` is the one-Mission override that parks each irreversible external action.
`--auto` remains a backward-compatible explicit Hands-off override, but is no
longer the primary UI. Commerce is not exposed through the generic publish
primitive: payment needs a dedicated capability with an explicit, payload-bound
amount.

Hands-off does not mean pretending missing capabilities exist. A connected work
identity—authorized email, phone/Google Voice number, signed-in browser session,
or verification-code inbox—may be used directly, including retrieving and filling
an OTP without persisting it in Mission history. A CAPTCHA or MFA challenge that
explicitly requires a person, an unavailable credential, a new identity/consent
choice, new spending authority, or uncertain duplicate risk becomes a temporary
Needs You handoff. The user handles that one step and Continue resumes the same
Mission. Collie does not bypass or outsource platform security checks.

Reusable profile facts are explicit, local eligibility claims rather than inferred
identity. For example, a user can save an age threshold and allow Collie to reuse
that exact or lower threshold on low/medium-risk forms. The claim never authorizes
CAPTCHA, person-required MFA, biometric/KYC, legal signatures, security keys, or
spending. Codes readable from an authorized Google Voice/mail connection are
ordinary connected work; the code itself stays outside Mission case/history.

The experimental native unattended coding profile is invoked with:

```text
collie mission start "finish the refactor and make the suite green" \
  --code --workspace C:\path\to\repo --overnight \
  --provider claude-agent-sdk --model claude-opus-4-8 \
  --no-paid-overage \
  --verify-command "python -m pytest -q"
```

`--overnight` is a bounded profile, not an infinite turn. When admitted, it permits at most 12
hours of active execution inside a seven-day elapsed window, so laptop sleep or a
reboot does not consume the active-work budget, while an abandoned Mission still
expires. It freezes the provider/model/billing route, runs resumable three-turn
code slices under one durable Mission session, persists each slice before yielding,
and lets the job daemon re-enter it.

Active model/worker boundaries are serialized across a root Mission and all of its
descendants. Collie durably charges elapsed active time before releasing that slot;
after a crash, any provisional recovery charge is bounded by the last durable
heartbeat plus one heartbeat interval, so lease, sleep, and reboot gaps are not
mistaken for active work.

Overnight code requires an existing `--workspace`. Collie snapshots a complete
baseline before the first edit and refuses to start if it cannot obtain a stable
tree digest. `--verify-command` may be supplied explicitly; if omitted, Collie may
use a detected project check, but it fails closed when none is available. Only the
exact fresh host-side check against the current, changed workspace can complete the
Mission.

The native overnight profile accepts only `claude-agent-sdk` with an explicit Opus
model such as `claude-opus-4-8`. It uses Anthropic's official Claude Agent SDK as a
one-message reasoner inside Collie's own harness and agent loop. Collie supplies a
replacement custom system prompt; it does not invoke `claude -p`, inherit Claude
Code's default system prompt, or make a raw OAuth Messages request.

The SDK worker is intentionally empty of foreign harness surfaces:
`setting_sources=[]`; tools and allowed tools are empty; MCP servers are empty and
strictly configured; skills, plugins, and agents are empty; slash commands and
built-in agents are disabled; no fallback model is configured; and a call is
limited to one SDK turn. Collie accepts assistant output only after the SDK init
event attests that tools, skills, plugins, agents, and slash commands are empty.
Collie's own system prompt, JSON tool protocol, durable loop, permissions, budgets,
workspace ownership, and host verification remain authoritative.

Per-Mission `--provider` and `--model` freeze the exact route without changing
global Settings. `--no-paid-overage` is an explicit user attestation that paid
usage credits/overage and auto-reload are disabled in the provider account. At
creation Collie performs a real inference probe through the same isolated Agent SDK
route. At every later runnable boundary it revalidates the signed-in Claude plan;
the next actual SDK call still fails closed if authority has changed. The worker
receives a minimal environment without API keys, proxy/base-URL overrides, or
provider routing overrides, and the frozen profile permits no API-key, paid-credit,
provider, model, raw-OAuth, or `claude -p` fallback. A saved receipt is audit
evidence, not permanent authorization; any mismatch fails closed before the next
model call.

As of 2026-08-12, Anthropic says Claude Agent SDK usage can draw from a paid Claude
plan. Collie's native route accepts an eligible signed-in Pro/Max plan and was live-tested on Max; it is
still subject to plan limits. This is a current policy snapshot, not a promise of
unlimited use, future routing, or an invoice guarantee. Collie refuses a route it
cannot prove and never enables paid fallback, but the user remains responsible for
the provider-side overage setting. The current validation is a short end-to-end
test of this bounded route, not a 12-hour soak; the 12-active-hour leash is a maximum
authority envelope, not evidence that a full-night run has already completed.
[Anthropic: use the Claude Agent SDK with your Claude plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan); [paid Claude plans and API billing](https://support.claude.com/en/articles/9876003-i-have-a-paid-claude-subscription-pro-max-team-or-enterprise-plans-why-do-i-have-to-pay-separately-to-use-the-claude-api-and-console)

Missing authorization is branch-scoped by default. It is recorded in
`case.pending_authorizations`, surfaced as Needs You, and the decider receives
another turn to pursue independent work. The whole Mission enters `needs_you` only
when the request is explicitly blocking, the same unresolved request is immediately
repeated, or completion is reported while requests remain.

## State machine

```text
queued -> running -> waiting -> running ... -> needs_you -> done_accepted
             |              |               |
             +-> pausing -> paused          +-> queued (temporary human assist)
             |
             +-> recovery_required -> reconciling -> queued (explicit reconcile only)

any non-terminal state -> cancelled
```

`cancelled`, `failed`, `done_accepted`, and `done_verified` are terminal. A
model's `done` self-report goes to `needs_you`; only an independent verifier may
produce `done_verified`, and only the user-facing Accept control produces
`done_accepted`.

Pause is cooperative at action boundaries. A running owner moves to `pausing` and
keeps its token; Resume is unavailable until that owner acknowledges `paused`.
An external action already in flight cannot honestly be recalled, but a second
worker cannot overlap it or overwrite its lifecycle state.

`continue` is for a temporary human assist such as a person-required CAPTCHA/MFA
and returns control to Collie. The UI labels `accept` as **End mission & take over**:
it is terminal and records no independent verification. A later **Return to Collie**
creates a successor Mission instead of rewriting that audit history; completed
semantic action keys and bounded receipts are inherited so fired work cannot replay.
Creation is persistence-first: the caller sees a queued ID before model/browser
work starts, so it can be managed or cancelled immediately.

## Single-driver, browser, and wake guarantees

Each run receives a random token in SQLite. Every state/case write is conditional
on it, so Web, the app ticker, `collie jobs daemon`, and manual Check may race:
only one driver wins.

Browser work has a second, cross-process SQLite resource lease. Each Mission also
gets its own browser `space` (one owned tab lane), so campaigns cannot navigate the
same tab. Final approval contains the tab, URL/origin, title, exact button ref, and
form digest; execution re-snapshots these and refuses if any target changed. The
final exact-ref click uses the bound DOM node rather than a delayed screen
coordinate. A click is not completion: a fresh permalink, success state, or other
postcondition is required for `verified`; otherwise the Mission stops as uncertain.

Mission waits are durable. Claiming a due wait and its run slot is one transaction;
a paused Mission retains its pending wake, and cancellation retires it. The
Web/Desktop process ticks while open. A manually running `collie jobs daemon`
provides the standalone loop and catch-up after sleep. For automatic restart after
sign-in/reboot, explicitly install the per-user worker supervisor with
`collie supervisor install`; no software runs while the computer is powered off.

## Authority, history, and pacing

Every primitive is evaluated against the Mission leash. A gated action is an exact
payload-bound nonce. Confirmation checks that the Mission is `needs_you`, the nonce
is its newest parked action, its job/leash IDs match, and it remains pending or was
approved but temporarily blocked by a shared resource.

Cancellation terminally changes Mission state, then idempotently revokes pending
or approved-but-unclaimed actions in the separate action store. `collie jobs
confirm` recognizes Mission-owned nonces and routes them back through the campaign.

The event ledger is append-only and recent events are fed back to the decider.
Irreversible actions have a semantic key derived from campaign, capability, payload,
and the stable part of the snapshotted target, so an exact duplicate is blocked
after waits/restarts. Each irreversible capability declares the executor inputs
that define that identity; model-invented idempotency labels, verification hints,
browser tab IDs, and DOM refs cannot turn the same external action into a new one.
Reservations, binding, and safe release are fenced by the exact Mission run token.
Proven no-fire releases append a compensating ledger event, returning their pacing
quota without rewriting history.
Defaults are 1,000 model decisions, 100 irreversible actions total, and 12
irreversible actions per rolling hour. SQLite enforces these across every wake.
`allowed_domains`, expiry, and spend caps are deterministic checks; unknown bound
names are rejected rather than stored as decorative policy.

The adaptive browser child has a positive allowlist only: Mission-scoped
open/read/snapshot/fields/links, type-without-submit, and dropdown pick. It has no
click, Enter-submit, desktop, filesystem, shell, MCP, upload, script, or capability
loading escape hatch. It checks both requested and live post-redirect origins, and
consequential GET routes are refused. Like/Follow/Repost/Publish must return to the
outer gate. Browser snapshots mask password, token, payment, email, phone, and
signup identity fields. Credential-bearing action args stop for a human browser
handoff instead of being written to Mission/Action SQLite.

`code` is not in the default world leash. When explicitly granted, each bounded
slice runs in a killable child process, reuses one Mission-scoped transcript, and
persists its checkpoint before the daemon schedules the next slice. Its editing
tools have no shell or arbitrary executor; path-bearing tools are canonicalized
beneath the TaskTree-bound workspace and same-workspace Missions share a
cross-process resource lease. The exact pre-authorized verification command runs
host-side after each slice. Completion requires a passing, fresh command receipt
whose content digest still matches the workspace and differs from the Mission's
baseline; a model answer or a green pre-existing tree is not success.

This is lifecycle/process isolation, not an OS security sandbox. The verification
command is user-authorized project code and runs with the Collie daemon's host
permissions. Use a disposable worktree plus a container/VM for untrusted repos.

## Recovery boundary

Drivers heartbeat their lease. A hard crash or legacy ownerless `running` row goes
to distinct `recovery_required`, never an ordinary human-assist state. Ordinary
Continue is forbidden. The UI shows relevant pending/approved/executing actions;
after inspecting the target system and receipts, the user must explicitly Reconcile
or Cancel. Reconcile first CASes into persistent, non-runnable `reconciling`, then
revokes only the exact pre-fence pending/approved nonces in the separate ActionStore,
and only then publishes `queued`. The publication transaction also retires the old
confirmation row and reservations proven never to have reached ActionStore, while
preserving executing/executed keys and receipts. A cleanup owner has its own leased
token; an expired owner cannot touch a new run after takeover. If the process crashes
halfway, re-running Reconcile resumes cleanup; another daemon cannot enter the gap.
This prevents a possibly-fired external action from being blindly repeated.

## Watchdog, checkpoints, and cumulative budgets

Mission progress has its own durable clock; lease heartbeats cannot advance it. Every model,
preparation, action, fold, and goal-verification boundary writes a bounded SQLite checkpoint.
`max_step_seconds` puts model and tool calls behind a daemon-thread boundary. A timed-out model
read is safe to retry after backoff. A timed-out action is different: its run token is fenced and
the Mission enters `recovery_required`, because the late worker may still finish externally. A
late worker may write its Action receipt, but it cannot fold stale case state or start another
action.

The leash also enforces cumulative model tokens, marginal model charges, active wall time,
elapsed time, retries, durable storage, model turns, irreversible actions, and action rate. A
specialist's immutable `parent_mission_id` makes these budgets cumulative over the complete
descendant tree, so parallel fan-out cannot mint a fresh budget. Sibling turn/action reservations
are serialized in SQLite; token, cost, and wall usage are charged when each in-flight boundary
returns. These totals survive waits and restarts. Long cases retain a compact rolling summary,
recent results, recent events, human updates, and recovery metadata; old bulk results remain
auditable in the event/receipt/checkpoint ledgers rather than crowding out the newest facts in the
model prompt. Subscription routes retain equivalent API list-price in a separate
observability counter. For the Agent SDK route, that split is a conservative
control-plane classification rather than proof of the provider's actual bill;
the overnight marginal-charge leash remains at $0.01 as a routing-regression
tripwire.

`needs_you` has two durable deadlines. The first emits an escalation record for notification
wiring. The hard deadline fail-closes to `paused` while preserving the exact confirmation inbox;
Resume restores `needs_you` and starts a fresh response window.

## Isolated durable work and scoped specialists

`MissionService.start()` defaults durable jobs to `workspace_mode="isolated"`. Code cannot run
until a provisioner binds an existing isolated directory with `bind_workspace()`. Current-workspace
code remains available only when explicitly requested and is serialized across processes by the
canonical workspace resource lease.

`harness.tasktree.TaskTreeStore` is the durable orchestration backend. It stores a parent/child run
tree, explicit resource ownership, progress/history, background state, a steer/cancel mailbox with
delivery acknowledgement, notification outbox, crash leases, and cumulative budgets. Child leash
and resource declarations are checked as deterministic subsets of the parent. Write scopes cannot
overlap between live siblings, and `can_access()` prevents a parent from writing a file range
currently delegated to an active descendant. Worktree provisioning is the default; missing
provisioning is `workspace_required`, not a silently shared checkout. Today this is a hard execution
boundary for Mission `code` and file resources. Other resource kinds remain durable
scheduling/ownership declarations until their capability adapter explicitly enforces them; they
must not be treated as a general operating-system sandbox.

This is an executable path, not only a task record. A production `MissionService()` now creates a
`TaskTreeStore` at `<state_dir>/tasktree.db` automatically and loads a `HookManager` for the current
working directory. Unreviewed or changed hook definitions remain visible as `hooks.pending` in
status and are not executed. Injected stores/hooks are still supported for embeddings and tests;
the service closes only resources it created itself.

Explicit `create_run_tree()` remains available when a host needs to provide a custom resource set.
For ordinary production Missions, the first model-facing delegation lazily creates a deterministic
root. A Mission with a bound isolated workspace receives file authority for that workspace (write
when its leash permits `code`, otherwise read); without a bound workspace, file delegation fails
closed while resource-free research can still be delegated.

The planner-facing primitives are `agent.spawn`, `agent.send`, `agent.poll`, and `agent.cancel`.
Spawn does not permit a per-call provider/model override: the child uses the current owning
`MissionService` configuration and can only narrow leash and resources. Replaying the same already
persisted Action nonce finds the same child; semantic equality or a newly proposed Action after an
uncertain crash does not reuse it. Thus two intentional, identical delegations remain distinct.
Terminal children publish bounded structured results, artifact references, and verification
observations to a durable mailbox. The parent folds those results into case state before
acknowledging delivery; an interruption between those operations replays safely. Waiting parents
wake on arrival, and the completion guard refuses success while a descendant remains active or a
child result remains unconsumed.

After a root is attached, `MissionService.spawn_specialist()` creates a child Mission in the
`specialist` scheduler lane.
`MissionService.tick()` claims and runs those child Missions through the normal model, leash,
ActionStore, watchdog, and verifier gates, then durably completes/blocks/fails the run-tree node.
Ordinary Mission scans exclude the specialist lane, so a child cannot bypass its run-tree owner.
Steers are consumed between model/action boundaries; cancellation is acknowledged at a safe
boundary. Missing provider, worktree, goal evidence, or enforceable code-resource scope becomes
`needs_you` instead of leaving a child queued forever.

The explicit backend control methods are `create_run_tree()`, `spawn_specialist()`,
`inspect_run_tree()` / `inspect_specialist()`, `steer_specialist()`, and `cancel_specialist()`.
Steer and cancel requests use the durable mailbox rather than reaching into a running thread.

Production wiring example:

```python
from harness.missionweb import MissionService
service = MissionService(goal_verifier=my_goal_verifier)  # owns state_dir/tasktree.db
root = service.create_run_tree(mission_id, resources, workspace=worktree_path)
child = service.spawn_specialist(mission_id, "test-specialist", prompt,
                                 leash=narrower_leash,
                                 resources=narrower_resources,
                                 workspace=child_worktree)
service.steer_specialist(child["run_id"], "also inspect the retry path")
snapshot = service.inspect_specialist(child["run_id"])
service.tick()  # daemon catch-up drives both ordinary Missions and specialists
```

Trusted lifecycle hooks receive `TaskCreated`, `TaskCompleted`, `Notification`, and Mission `Stop`
events. A denying `TaskCompleted` hook can stop specialist-owned completion before commit, while a
denying Mission `Stop` hook prevents an automated Mission success transition and routes the work to
human review. Projecting an already-terminal root Mission into its TaskTree row emits
`TaskCompleted` as post-commit audit only; it cannot reopen the Mission. Explicit user cancellation
remains authoritative and is still dispatched for audit.

## What “24×7” means here

- Process crash: claims/checkpoints survive; safe model-only boundaries requeue, while uncertain
  action boundaries require reconciliation.
- Sleep: durable timers catch up when the daemon resumes.
- Reboot: the supervisor configuration includes the job daemon, but it runs after reboot only when
  the user has explicitly installed/enabled that supervisor startup integration.
- Hung provider/tool: the watchdog releases the dispatch lane; an uncertain action never silently
  retries.
- Powered-off computer or unavailable third-party service: Collie cannot execute. It resumes or
  escalates from durable state when compute/service returns.

Focused verification commands:

```text
python -m pytest -q tests/test_mission_autonomy.py tests/test_tasktree.py
python -m pytest -q tests/test_mission.py tests/test_missionweb.py tests/test_mission_aggregate_budget.py tests/test_scheduler.py tests/test_actions.py tests/test_verifier.py
```
