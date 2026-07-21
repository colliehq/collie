# How collie manages context

collie's context strategy is the inverse of a long-running agent loop that keeps stuffing a
growing transcript into every call. collie keeps a **small, fixed prefix** and pulls in exactly
the facts a turn needs via **memory recall**. That is where "most-capability-per-token" comes
from — the prefix is measured in hundreds of tokens, not tens of thousands.

## The system prompt: three tiers, ordered for caching

Every turn, `context.ContextComposer` assembles the system prompt from three tiers. The order is
deliberate — the stable part is first so a provider's prefix cache is never invalidated by
per-turn churn below it.

| Tier | Contents | Changes |
|---|---|---|
| **STABLE** | identity (`You are collie…`), mode role, tool **names**, working-dir note | rarely |
| **CONTEXT** | merged project rules — `CLAUDE.md` / `AGENTS.md` / `.collie.md`, char-capped (~4000) | per repo |
| **VOLATILE** (last) | CORE memory blocks (e.g. the goal) + **auto-prefetched** memory + timestamp | every turn |

VOLATILE goes **last** on purpose: per-turn recall changes only the tail, so everything above it
stays byte-identical and cacheable.

## Fixed prefix + budgeter

`TokenBudgeter` enforces a fixed-prefix ceiling (default 6000) and reports per-section cost. In
practice the real prefix is **~725–1200 tokens/turn** including the tool schemas — a deliberate,
measured lever, not an accident. (Contrast: harnesses that inline a growing transcript pay for it
on every turn.)

## Auto-prefetch: recall without the model asking

Each turn collie embeds the user message and runs a **hybrid recall** over memory, injecting the
top-k facts into VOLATILE — so the model never has to *decide* to search (that decision, and the
round-trips it costs, is exactly what collie removes):

```
user message ──embed──▶ ┌─ BM25 (FTS5, sparse) ─┐
                        │                        ├─ RRF fusion ─▶ [+ optional cross-encoder rerank] ─▶ top-k facts
                        └─ dense cosine ─────────┘
```

The embed is served by a **resident daemon** (`embed_server`) that keeps the local model warm
across invocations, so this step is ~0.4s instead of a ~1.3s cold load every call. It is cached
per user-message (embed once per message, not once per loop turn).

## Memory: the durable store (persists across runs)

`memory.SqliteMemory` (one SQLite file, per project) is where cross-run context lives:

- **CORE blocks** — pinned, char-capped, loaded **every turn**. The `--goal` lives here.
- **ARCHIVAL facts** — text + keys + embedding; hybrid-retrieved; near-duplicates are
  consolidated / superseded so it doesn't bloat.

At the end of a run collie self-writes a durable breadcrumb — `Task '<id>' -> <answer summary>` —
so a later run can recall what a past run concluded.

## Within a run: history elision of old tool output

Inside a run the conversation `messages` list grows, but `ContextComposer.build` **elides old
tool output** before sending it to the model: any `tool` message older than the **last 14
messages** has its content truncated to ~240 chars + `…[older tool output elided]`. Recent turns
stay full-fidelity; stale tool dumps (a big `grep`/`read` from ten turns ago) shrink to a stub.
This is what keeps a long thread — or a `--continue`d session — from bloating the prefix, so
long-horizon state can live in the thread + memory without an ever-growing token bill.

## Across runs / the CLI: what carries and what doesn't

Each `collie run "…"` is a **fresh process with a fresh message thread**. Two kinds of "context"
behave differently:

- **Durable memory — carries.** `--goal`, `remember`-ed facts, and the per-run task→answer
  breadcrumbs all persist in `memory.db` and are auto-recalled next time. The CLI is **not
  amnesiac**.
- **Conversation thread — does not carry (by default).** The previous run's back-and-forth is
  *not* replayed into the next `collie run`. There is no `--continue` / session-resume yet. A
  follow-up call sees only (a) the code changes on disk and (b) whatever landed in memory.

The one place full context carries automatically is **`collie loop`**: within a single invocation
it reuses the same harness + memory across iterations (`consolidate=True`), so iterations are
continuous — but that is within one call, not across separate CLI calls.

**Known gap:** no local session resume (`collie run --continue` / `--resume <id>`) that replays a
prior run's thread. It would be local-only and on-brand; it just isn't built yet.

## One-line summary

Small fixed prefix (STABLE → CONTEXT → VOLATILE) + per-turn hybrid recall that pulls the right
facts in — instead of an unbounded transcript. Durable context lives in memory and survives across
CLI runs; the conversation *thread* does not (yet).
