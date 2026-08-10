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
and returns control to Collie. `accept` means the human is taking over or accepting reported completion.
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

`code` is not in the default world leash. When explicitly granted, its child has
no shell or arbitrary executor; path-bearing tools are canonicalized beneath an
approved `COLLIE_MISSION_CODE_ROOTS` workspace and same-workspace Missions share a
cross-process resource lease. Without separately sandboxed execution evidence, an
edit remains inconclusive for human review.

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

The leash also enforces cumulative model tokens, estimated model dollars, active wall time,
elapsed time, retries, and durable storage. These totals survive waits and restarts. Long cases
retain a compact rolling summary, recent results, recent events, human updates, and recovery
metadata; old bulk results remain auditable in the event/receipt/checkpoint ledgers rather than
crowding out the newest facts in the model prompt.

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
and resource declarations are checked as deterministic subsets of the parent. Child usage is
charged to every ancestor, so fan-out cannot escape the root budget. Write scopes cannot overlap
between live siblings, and `can_access()` tells a parent when a file has been delegated. Worktree
provisioning is the default; missing provisioning is `workspace_required`, not a silently shared
checkout.

This is an executable path, not only a task record. A production `MissionService()` now creates a
`TaskTreeStore` at `<state_dir>/tasktree.db` automatically and loads a `HookManager` for the current
working directory. Unreviewed or changed hook definitions remain visible as `hooks.pending` in
status and are not executed. Injected stores/hooks are still supported for embeddings and tests;
the service closes only resources it created itself.

Root creation remains explicit: choosing the resources and isolated worktree is an authority
decision that should not be guessed by `start()`. Before a root is attached, `status.tasktree`
reports the available durable backend and `inspect_run_tree()` returns an empty tree. After
`create_run_tree()`, `MissionService.spawn_specialist()` creates a child Mission in the
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
events. A denying `TaskCompleted`/`Stop` hook prevents an automated success transition and routes
the work to human review. Explicit user cancellation remains authoritative and is still dispatched
for audit.

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
python -m pytest -q tests/test_mission.py tests/test_missionweb.py tests/test_scheduler.py tests/test_actions.py tests/test_verifier.py
```
