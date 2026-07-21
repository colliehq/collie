# Delegate spine — the verification gate, extended from code to the world

Status: implemented on branch `verifier-protocol` (6 commits, CORE 111/111 + new
suites green throughout). This is the first working slice of DELEGATE_PLAN_CLAUDE.md
§5.1/§5.2 — the safety spine, not yet a live irreversible capability.

## What it is

collie's signature is executed verification: write a repro that must fail on the
broken code, make the smallest edit that flips it, re-run the assertion. This
branch lifts that gate off the code path so a code fix and a real-world errand
(publish a listing, send an email) clear the **same** gate, and adds the
irreversible-action machinery around it.

Two different gates, often conflated — keep them apart:

- **Leash gate** — *may this action happen?* (authority, pre-execution)
- **Verification gate / done-check** — *did the intended outcome actually occur?*
  (evidence, post-execution)

They meet at the executor: leash authorizes → execute → verify → receipt.

## The five modules

```
verifier.py   the done-check protocol (arms / freshness / ground-truth /
              assert-strength / repairable). CodeReproVerifier re-expresses the
              SWE gate; ListingVerifier is the world case. loop.py's three inline
              gate copies now all call one _repro_verified() -> this.
observe.py    the independent observation channel: a cookieless, SSRF-guarded
              GET (via webfetch._open_pinned). Ground truth for a listing is a
              LOGGED-OUT re-fetch — never the acting page's own success toast.
actions.py    confirm-token + model-free executor + receipts. propose (materialize
              exact payload) -> confirm (human approves the payload) -> execute
              (deterministic, no model) -> receipt. SQLite, ~/.collie/actions.db.
jobs.py       the Job object + state machine + Capability registry + Executor.
              Maps a done-check verdict onto the job's terminal state.
cli.py        `collie jobs ls | inbox | confirm <nonce> | receipts` — the human
              surface that closes the loop from the terminal.
```

## The six load-bearing pieces (why it generalizes)

Each is the same idea in code and in the world; only the substrate changes.

| Piece | Code substrate | World substrate |
|---|---|---|
| 1. arms only after a change | `did_edit` | an irreversible Action fired |
| 2. ground truth, not self-report | process exit code | logged-out re-fetch / IMAP, not the app's toast |
| 3. freshness | repro turn ≥ edit turn | observation timestamp ≥ action timestamp |
| 4. assertion strength | `assert expected==actual` | predicate asserts title+price present |
| 5. bounded repair | `verify_max=2` | `repairable()` — but see piece 6 |
| 6. reversibility split | (n/a — edits are reversible) | irreversible + failed post-check ⇒ compensate, **never a silent retry** |

Piece 6 is the one thing code never needed: the code gate is purely post-hoc
because edits are reversible; a sent email is not. So the world gate is **pre-hoc
for irreversibility** (precheck: form values == mandate, target not already
live) **+ post-hoc for confirmation** (re-observe it landed). The "repro must
fail on broken code first" idea maps onto the precheck, not the postcheck.

## Job state machine

```
queued -> running -> waiting -> needs_you -> done_verified | done_accepted
                                           | failed | cancelled
```

- **done_verified** — fresh independent evidence satisfied the check. The ledger
  counts only this.
- **done_accepted** — a reversible no-op or explicit human accept; NO machine
  evidence. A fired *irreversible* action can never land here.
- **needs_you** — a gated action awaits confirm, OR a fired action's done-check
  came back INCONCLUSIVE (could not observe). INCONCLUSIVE is never laundered
  into success — this is the anti-Manus property.

## Invariants (each pinned by a test)

- an irreversible action fires **at most once** (atomic APPROVED→EXECUTING latch)
- propose/confirm **survive process restart** (on-disk SQLite; the confirm
  boundary works precisely because it is a step boundary)
- **payload + authority fields are digest-bound**; tampering args/leash/job/risk
  after approval is refused (plain-SHA256 caveat: an HMAC keyed outside the DB is
  the noted follow-up for a DB-write attacker)
- **TOCTOU-safe**: if the world diverged from the approved snapshot, execute
  refuses without firing
- **fail-closed**: an unconfirmed action cannot execute
- **evidenced**: every fire writes a receipt (terminal state + receipt in one
  commit); a durable attempt marker makes a crash distinguishable
- an INCONCLUSIVE / print-only / acting-channel observation can **never** become
  done_verified

## Adversarial audit

A 16-agent audit (4 attackers × dimension, then independent verify per finding)
attacked these invariants; 11 confirmed/partial findings were all fixed and
regression-locked (commit `437794c`). Notably it caught a **pre-existing** bug in
the signature code gate: same-turn repro-then-edit could stamp a broken edit
VERIFIED (turn-granular freshness key) — now a landed edit invalidates prior
repro evidence.

## Deliberately NOT done yet

- **The live irreversible action itself** (real FB publish / email send). It
  belongs behind the confirm-token / daemon executor and needs real authority —
  wiring it live now would violate the plan's own rules. Everything *around* it
  (precheck + independent post-check + receipt) is real and tested.
- **colliejobd** (the daemon owning scheduling / IMAP wake / catch-up-on-wake) —
  DELEGATE_PLAN_CLAUDE.md §5.2, stage 1.
- **The leash gate at the tool-registry dispatch layer** — the other safety
  blocker from the earlier plan review (gate `Tool.run` not `loop.py:545`, cover
  the progtool RPC, argument-level bash gating, browser-bridge secret). Separate,
  riskier piece; not started here.
- **HMAC-with-external-key** for the action digest (see invariants).
- A full mock-provider-driven loop test for the same-turn freshness fix (the fix
  is in place; CORE 111 stays green; a dedicated Harness-level regression is a
  follow-up).

## Try it

```
collie jobs inbox                 # pending confirmations + jobs needing you
collie jobs confirm <nonce>       # approve a concrete payload; a runner executes+verifies
collie jobs receipts              # what fired, under which leash, how it verified
```
