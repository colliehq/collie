# Local communication store (`harness/communications.py`)

Status: first bounded implementation batch. This module is storage only — the
transports, surfaces and the run loop are integrated separately.

A durable, stdlib-only record of what arrived over email/SMS and what we sent
back, plus a crash-safe hand-off of one received message to the existing
`task_inbox`. Nothing here opens a socket, fetches mail, sends mail, calls a
model, or starts a thread or a scheduler. Every function is a bounded
read/modify/write of one JSON file under `sessions._locked` +
`sessions._atomic_dump`.

## Authority boundary

A received message is **data awaiting local authorization**, permanently.

- Acceptance is a separate call (`accept_event`) made by local trusted code that
  names itself in `actor=`. Nothing a message says or claims to be from causes
  execution.
- The text handed to `task_inbox` is wrapped in an explicit untrusted-data frame
  naming the connection it arrived on; see `compose_task_text` and
  `UNTRUSTED_HEADER`. The task metadata carries `trust: "untrusted_external"`.
- `policy["allowed_senders"]` can only *refuse*. Passing it grants nothing;
  failing it refuses acceptance until a local caller passes
  `override_sender=True`, which is recorded in the acceptance receipt with the
  actor who did it. Sender addresses are unauthenticated transport metadata and
  this module never treats them as cryptographic authority.
- Outbound destinations fail closed: a message may only go to the connection's
  `owner_reply_target` or an entry in `allowed_destinations`
  (`COLLIE_ONLINE_V1.md` invariant 8).
- No plaintext provider secret is stored. `secret_ref` must be
  `<scheme>:<name>` with scheme in `keychain|env|file|vault|none`, and nothing in
  this module resolves or reads it.

## Storage

One file per connection: `<sessions>/comms/<connection_id>.json`, a sidecar
directory so `sessions.recent()` never mistakes a connection for a conversation.
The connection id goes through `sessions._path`'s validator via
`session_owner.sidecar_path`, so a name that cannot address a journal cannot
address a connection.

`state_dir` is explicit: every entry point takes `directory=` and resolves it
once through `session_owner.sessions_root` (a test enforces this). Loads are
bounded by file size *and* by the read, then fully validated — a store that
this module could not have written raises `StoreCorrupt` and is never repaired
automatically.

## API

### Connections

| function | notes |
| --- | --- |
| `create_connection(id, *, channel, address, display_name, policy, secret_ref, directory)` | idempotent by id; a different definition is `IdConflict`. `channel` is `email` or `sms`. |
| `update_policy(id, policy, *, actor, directory)` | whole-object replacement, never a merge; unknown keys refused. |
| `get_connection(id, *, directory, include_private)` / `list_connections(...)` | public projection by default. |
| `connection_status(id, *, directory)` | counts, caps, thread count — no content. |

Policy keys: `allowed_senders`, `allowed_destinations`, `owner_reply_target`,
`require_allowed_sender` (default `True`), `notes`. Email allow-list entries may
be a full address or `@domain`.

### Received events

| function | notes |
| --- | --- |
| `record_received(connection_id, event_id, *, sender, text, recipient, subject, thread_key, attachments, metadata, raw_ref, received_at, directory)` | `event_id` is the provider's id and the per-connection dedupe key. Same payload → `duplicate=True`; different payload under the same id → `IdConflict`. |
| `get_event` / `list_events` / `reject_event(*, actor, reason)` | states are `pending`, `accepted`, `rejected`. |

`thread_key` defaults to the event id, so an unthreaded message starts its own
task. `attachments` are **metadata references only** (`id`, `name`,
`media_type`, `bytes`, `digest`) — nothing is fetched and no path is stored.

Malformed or oversize input is refused with its measured size; nothing is ever
truncated. Caps (`MAX_TEXT_BYTES` 64 KiB, `MAX_PENDING_EVENTS` 256, …) refuse the
*new* request rather than evicting a stored one.

### Threads and sessions

`derive_session(connection_id, thread_key)` →
`comm-<connection-fp8>-<thread-fp16>`, and `derive_entry_id(connection_id,
event_id)` → `comm-<40 hex>`. Both are deterministic, which is what makes crash
recovery work without allocated state.

Thread mappings are connection-scoped. The same thread id on two connections
maps to two different sessions and two different tasks. `bind_thread` supports
explicit continuation into an existing conversation, and refuses a session this
module derived for a *different* connection (`PolicyRefusal`) or a thread
already bound elsewhere (`StateConflict`).

### Acceptance

`accept_event(connection_id, event_id, *, actor, mode="follow_up", config=None,
session="", override_sender=False, reason="", directory=None)`

Two-phase commit against `task_inbox`:

1. **reserve** — under our lock, freeze `entry_id`, `session`, `mode`, `config`
   and a digest of the exact payload; write `acceptance.state = "enqueuing"`;
2. **enqueue** — `task_inbox.enqueue` with the frozen entry id, outside our lock;
3. **settle** — separate transaction marking `acceptance.state = "accepted"`.

A crash between 2 and 3 leaves a reservation. Re-running `accept_event`
re-derives the same ids, `enqueue` answers `duplicate=True`, and the *same* task
is settled — no new session, no duplicate input. The result carries
`recovered=True`. Concurrent acceptors converge on one reservation, one entry
and one thread binding.

A retry asking for different terms is an `AcceptanceConflict`; the first
acceptance decides. `abandon_acceptance` can release a reservation only when
`task_inbox` holds neither an entry nor a tombstone for it.

### Result outbox

| function | transition |
| --- | --- |
| `create_result(connection_id, result_id, *, destination, text, subject, thread_key, in_reply_to, session, metadata, directory)` | → `pending`. Immutable; idempotent by `result_id`; different payload → `IdConflict`. |
| `claim_send(connection_id, result_id, *, transport, directory)` | `pending` → `sending`, **on disk before any transport**. Returns the payload and a `token`. |
| `mark_submitted(*, token, provider_message_id, detail)` | → `submitted` (terminal; a delivery cannot be un-recorded). |
| `mark_failed(*, token, error)` | → `failed`. The caller asserts no delivery occurred. |
| `mark_unknown(*, token, error)` | → `unknown`. The honest answer to an ambiguous timeout. |
| `sweep_sending(connection_id, *, older_than, reason)` | abandoned `sending` claims → `unknown`. Never resends. |
| `resolve_unknown(*, actor, outcome, reason)` | `unknown` → `submitted`/`failed`, recorded as a local operator decision. |
| `retry(*, actor, reason)` | `failed` → `pending`. **Only** from `failed`. |
| `next_sendable` / `get_result` / `list_results` | listings; they start nothing. |

`unknown` is never auto-retried and never auto-resent. A stale claim token
cannot report an outcome for a newer attempt, but the original holder may still
report the truth after a sweep.

### Public projection

`public_connection`, `public_event`, `public_result` and every listing withhold
private content by default: message bodies, subjects, full sender/recipient
addresses, attachment file names, raw-message references and `secret_ref`.
Addresses are masked (`o***@example.com`, `***4321`). `include_private=True` is
for the local trusted surface.

## Known limitations and design concerns

- **Attachment content is out of scope.** Only metadata references are stored;
  resolving them is the transport's job and deliberately not modelled here.
- **`compose_task_text` is version-pinned** (`COMPOSE_VERSION`). If it changes
  while a reservation is open, recovery reconciles against an existing task if
  one is there and otherwise raises `AcceptanceConflict` rather than enqueuing
  different words under an id that promises identical ones.
- **A compacted event cannot be accepted.** Settled events beyond the retention
  window collapse to an id+digest tombstone (dedupe survives; the full record
  does not), so `accept_event` on one raises `UnknownRecord`.
- **`sweep_sending` is age-based, not liveness-based.** A pid is not proof of
  life, so the sweep takes an explicit age from the caller and lands on
  `unknown` — a false positive is safe because nothing auto-resends.
- **Exactly-once *insertion*, not exactly-once *execution*.** As with
  `task_inbox`, nothing here can undo a tool call; the guarantee is that one
  received message becomes at most one accepted task, and that every accepted
  message has a truthful record of what happened to it.
- **Not yet integrated.** No webapp/webui route, no dogmail or relay transport,
  no scheduler wake-up. `next_sendable()` and `list_events(states=["pending"])`
  are what a caller polls; this module starts nothing.

## Tests

`tests/test_communications.py` (54 tests, temporary state, no transport).
Cross-process coverage uses real subprocesses for concurrent acceptance,
concurrent polling, concurrent send claims, and the two crash windows (before
enqueue, and between enqueue and bookkeeping).
