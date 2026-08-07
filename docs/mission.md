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
/mission --auto <goal>
/mission --auto --domains=x.com,*.y.com --rate=6 <goal>
/mission list
/mission status|run|pause|resume|cancel|check|continue|accept|reconcile <id>
```

The slash parser currently belongs to the Web/Desktop chat surface; scripts use
`collie mission ...`. `/delegate` is a compatibility alias. `--auto`
pre-authorizes irreversible capabilities inside the leash; otherwise every exact
send/publish payload parks for confirmation. Commerce is not exposed through the
generic publish primitive: payment needs a dedicated capability with an explicit,
payload-bound amount.

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

`continue` is for a temporary human assist such as CAPTCHA/MFA and returns control
to Collie. `accept` means the human is taking over or accepting reported completion.
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
provides the standalone loop and catch-up after sleep. This change does not install
an OS startup service: after reboot the daemon must be started again, and no
software runs while the computer is powered off.

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
