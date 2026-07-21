# Delegate spine — the verification gate, extended from code to the world

Status: implemented on branch `verifier-protocol`, CORE 111/111 + all new suites
green throughout. A coherent, runnable build of DELEGATE_PLAN_CLAUDE.md §5.1/§5.2:
authority (leash) + evidence (verifier) + an irreversible seam (confirm token +
model-free executor + receipts) + durable waiting (colliejobd) + a real,
executable capability. The only thing deliberately withheld is a live *external
irreversible* capability (send/publish/pay), which needs real authority.

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

## The modules

```
verifier.py     the done-check protocol (arms / freshness / ground-truth /
                assert-strength / repairable). CodeReproVerifier re-expresses the
                SWE gate; ListingVerifier is the world case. loop.py's three
                inline gate copies now all call one _repro_verified() -> this.
observe.py      the independent observation channel: a cookieless, SSRF-guarded
                GET (via webfetch._open_pinned). Ground truth for a listing is a
                LOGGED-OUT re-fetch — never the acting page's own success toast.
actions.py      confirm-token + model-free executor + receipts. propose -> confirm
                -> execute -> receipt. HMAC integrity (key outside the DB), atomic
                single-use latch, crash-evidenced. SQLite ~/.collie/actions.db.
leash.py        the authority model: evaluate(leash, cap) -> allow / ask / deny
                over an allowlist, spend cap, expiry, irreversible mode. Enforced
                in the executor — a DENY blocks even a confirmed action.
jobs.py         the Job object + state machine + Capability registry + Executor.
                drive() = autonomous entry (reversible in-scope auto-runs;
                irreversible parks for confirm); run_confirmed() = post-confirm.
capabilities.py the shipped, executable capabilities. note.append (a safe
                reversible file write) verified by an independent re-read — the
                full chain runs live, not just in tests.
scheduler.py    durable waiting + catch-up-on-wake (colliejobd). A wait is a row;
                tick(now) fires every overdue wait by driving its action. serve()
                is the thin daemon loop.
cli.py          `collie jobs ls | inbox | run <cap> | confirm <nonce> | receipts
                | wake | daemon` — the human/daemon surface.
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

## Done since the first cut

- **colliejobd** — durable waiting + catch-up-on-wake (scheduler.py); `collie
  jobs daemon` / `wake`.
- **Leash authority model** (leash.py) enforced in the executor.
- **A real, executable capability** (note.append) verified by an independent
  re-read — the full chain runs live.
- **HMAC-with-external-key** for action integrity (replaced the plain digest).
- **Loop-level regression** for the same-turn freshness fix (test_gate_freshness;
  proven to fail without the fix).

## Deliberately NOT done (needs real authority or is a separate subsystem)

- **A live EXTERNAL irreversible capability** (real FB publish / email send).
  Everything around it is real — a done-check, the confirm token, the executor,
  receipts, and a working reversible capability — but wiring a real external
  side effect needs real authority and is a deliberate stop.
- **The leash gate at the tool-registry dispatch layer** — the *coding agent's*
  own tools (gate `Tool.run` not `loop.py:545`, cover the progtool RPC,
  argument-level bash gating, browser-bridge secret). Hardens the existing
  harness rather than the delegate spine; separate, riskier piece.
- **Email / page-change waits** — scheduler.py ships timer waits; content-poll
  waits schedule the same way but need live credentials (auto-apply's IMAP loop
  is the port target).

## Try it

```
collie jobs run note.append '{"file":"todo.txt","text":"buy milk"}' --leash '{"may":["note.*"]}'
                                  # create + drive a job live -> done_verified + receipt
collie jobs inbox                 # pending confirmations + jobs needing you
collie jobs confirm <nonce>       # approve a concrete payload; executor runs+verifies
collie jobs receipts              # what fired, under which leash, how it verified
collie jobs wake                  # fire due durable waits now (catch-up)
collie jobs daemon                # colliejobd: catch-up + tick on an interval
```
