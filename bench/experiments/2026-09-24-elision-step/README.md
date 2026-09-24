# Elision boundary in steps (2026-09-24)

The composer stubs tool outputs older than its recent window. The boundary used to move with every
new message, so each turn rewrote a message the provider had already cached and everything after it
was read again. `context.ELIDE_STEP` makes the boundary move six messages at a time. **Result: ship
it.**

## Why it was looked at

The developer's run log (`~/.collie/data/runs.db`, runs up to 2026-09-08) attributes 1651 of 4467
model turns to a cache miss caused by elision: 11.3M tokens that were expected to be cached were
processed again, about 6.9k per turn. 993 of those turns were on codex-oauth and 533 on
anthropic-oauth.

## Offline check (`tests/test_elision_cache_stability.py`)

A 40-turn read-file session built through the real composer. Turns whose request rewrote part of
what the previous request had already sent: **33** with the per-message boundary, **11** with steps
of six. The last 14 messages are always full, and everything older than 14 + 5 is stubbed.

## Live check (`elide_ab.py`, `results.jsonl`)

- One growing session (a user task, then one `read_file` call and a ~1.6 kB result per turn) is
  composed by Collie's `ContextComposer` for turns 6 to 24 and each request is sent to the ChatGPT
  Codex backend through `CodexOAuthProvider` (`gpt-6-astra`, effort low). Replies are ignored; the
  usage the backend reports is recorded.
- Arm `sliding` sets `ELIDE_STEP = 1` (the old behaviour); arm `stepped` uses 6. Each arm has its
  own provider session and a nonce at the top of its system prompt, so neither warms the other.
- The script refuses to run when the access token has under two hours left, so it never refreshes
  or rewrites `~/.codex/auth.json`.

Turns 10–24 (past the window, where elision is in play):

| arm | tokens sent | cached | not cached | cached share | median request |
| --- | --- | --- | --- | --- | --- |
| sliding | 187,073 | 46,080 | 140,993 | 24.6% | 3.1 s |
| stepped | 216,180 | 139,136 | 77,044 | 64.4% | 2.9 s |

With the per-message boundary only the ~3k-token system prompt was ever reused. In steps, the two
turns after each move reuse 13–15k tokens; the move itself costs about what every turn used to.
Stepped requests are larger (up to five more full outputs are kept) but 45% less of them has to be
processed afresh. One run per arm; request latency at this size is dominated by the backend and
did not separate clearly.

## Not measured

The Anthropic route now also marks the end of the history as a cache breakpoint (system, elision
boundary, end: three of the four allowed), which only pays because the boundary no longer moves
every turn. It was not measured live here.
