# Communications runtime — adversarial audit and repair, 2026-09-23

Independent audit of the integrated email/phone runtime against the current
goal: a robust email + phone + daily-brief loop that needs minimal intervention.
Audited at `57e77c4` plus the working tree; the four findings it proved are
repaired here, together with one the parent found in the retry path.

Scope read: `harness/communications.py`, `harness/channel_service.py`,
`harness/channel_policy.py`, `harness/channel_web.py`, `harness/channel_secrets.py`,
`harness/mail_messages.py`, `harness/mail_transport.py`, `harness/phone_transport.py`,
`harness/dogmail.py`, the `/api/channels` and `/api/brief` routes in
`harness/webapp.py`, and `harness/webui/communications.html`.

Everything below is covered by `tests/test_communication_audit.py` (64 tests),
which runs against an isolated `COLLIE_STATE_DIR`/`COLLIE_SESSIONS_DIR` under a
pytest `tmp_path` with fake transports: no network, no live provider, no real
user state. Every address, body and identifier is fixture data (`example.test`,
`evil.test`).

```
python -m pytest tests/test_communication_audit.py tests/test_communications.py \
                 tests/test_channel_service.py tests/test_channel_drafts.py \
                 tests/test_channel_web.py tests/test_collie_mail_channel.py -q
```

## Status

| # | Severity | Area | Status |
|---|----------|------|--------|
| 1 | **High** | receiving | **Repaired.** An unstorable message is refused explicitly and the cursor passes it |
| 2 | Medium | reply recovery | **Repaired.** The lane filters before it spends its budget, and reaches new *and* old work |
| 3 | Medium | settings race | **Repaired.** A poll in flight cannot republish `connected` over a pause or disconnect |
| 4 | Low | auto-drafting | **Repaired.** The throttle counts the newest hour and reports what it defers |
| 5 | **High** | retry | **Repaired in the service layer**; one caller change is left for the parent (below) |
| 6 | **High** | retention | **Repaired.** Accepted work is kept until its reply is durable, and a full store refuses rather than discards |

---

### 1 — one unstorable message no longer stalls the mailbox

`mail_messages.parse` accepts content the durable store refuses: a `text/plain`
body containing a NUL byte, and a `Subject` whose RFC 2047 encoded-word decodes
to text with a line break (the header-injection guard, refusing correctly).
`ChannelService.ingest` had no branch for `comms.InvalidRequest`, so the refusal
escaped past the line that advances the cursor — every message that arrived
after the bad one was never received, with only a red "Connection check failed"
to show for it.

`ingest` now records "something arrived under this id and could not be kept" as
an explicit rejection, with a readable reason in `metadata.input_error`, and the
poll counts it as unreadable and moves on. The content is never repaired into a
task: a message silently altered on the way in is not the message the person
received. A delivery whose *id* the store cannot hold has no event to show, so
the poll counts it as skipped instead.

`StoreFull`, `IdConflict` and `StoreCorrupt` are deliberately **not** caught
there. Those mean the next poll should see the same messages again, so they
reach the caller with the cursor untouched — regression-tested, because
swallowing them would turn a storage problem into mail recorded as unreadable.

### 2 — reply recovery is no longer pinned to the oldest events

`reconcile` sliced `events(limit=500)[:25]` from a list that is sorted oldest
first, so a connection past its first 25 events examined the same settled rows
forever: the task ran, the answer was in the journal, and no draft ever
appeared. `comms.list_events`/`list_results` gained an opt-in `newest=True`
window (the default front slice is unchanged, so no existing caller silently
moves), and the lane now filters to accepted work that is still missing a reply
*before* it spends its budget. Of that budget, five slots are reserved for the
oldest waiting work so a handful of events that will never reconcile cannot
starve everything behind them.

`ChannelService.events`/`results` default to the newest window, which is what
the desktop inbox and thread matching both want; a reply that quotes a message
from a busy mailbox now still continues its thread.

### 3 — a poll in flight no longer republishes `connected`

`disconnect` and `poll` hold different locks on purpose, and neither
`set_enabled` nor `disconnect` bumped `revision`, so a slow poll could finish
and write `status="connected"` over a disconnection — showing a connected badge
for an account with no credentials. `set_enabled` now bumps the revision, and
`_status` refuses to publish anything for a connection that is not enabled. The
op lock is deliberately not taken in `set_enabled`: pausing must stay instant
while a send is in flight, and those two guards already order the outcome.

The cursor still advances in that window (the messages were durably recorded
before it moved, and re-reading them would only produce duplicates), but only
when the row still names the same account.

### 4 — the auto-draft throttle counts, and says what it defers

The hourly window was read from the *oldest* 200 events, so past 200 events it
collapsed to zero and stopped throttling exactly when a connection was busy;
and when it did engage it skipped new mail with `drafted: 0` and no `issues`
entry, which is indistinguishable from "nothing to do". The window is now taken
from the newest accepted events, and anything the cap defers is reported through
the tick's issues — how many are waiting, that the limit is ten an hour, roughly
when it resumes, and that nothing was discarded. Deferred mail stays `pending`
and is offered again on the next pass.

### 5 — a failed reply can now actually be retried

The Collie Mail relay keys its delivery ledger on the result id. `comms.retry`
re-queued the *same* id, so once the ledger held `failed` for it every later
submission was answered from the ledger — the reply could never leave the
machine, however the connection was fixed.

`comms.retry_as` creates a fresh attempt (`<id>-try2`, `-try3`, …) carrying the
approved text, subject, destination, thread and the **same `Message-ID`**, so
the person sends the reply they approved and a recipient sees one message rather
than two. The failed record keeps its outcome and history and names its
replacement, so a second Retry answers with the attempt that already exists;
`comms.retry` refuses once a replacement exists, so neither route can queue the
same reply twice. `unknown` is still never retried by any path — only a person's
`resolve_unknown` can move it on. `ChannelService.retry` is the entry point and
serializes with `send`/`disconnect`.

**Patch note for the parent (one line, in a file this worker does not own).**
`harness/channel_web.py:65` still calls `comms.retry`, which keeps the old
same-id behaviour. It should become:

```python
    if action == "retry":
        return host.retry(connection, body.get("id"))
```

The desktop UI needs no change: `communications.html` calls `action("retry", …)`
and then reloads the list, so the new attempt appears as a pending draft beside
the failed original.

### 6 — accepted work is no longer tombstoned before its reply exists

The limitation the previous round *pinned as correct* was a durability bug. Only
`pending` events were exempt from compaction, so an accepted message became an
id+digest tombstone as soon as `MAX_RETAINED_EVENTS` (500) settled events sat
behind it — **while its task was still running, and before any reply had been
captured**. `ChannelService.capture_result` composes the answer from the
original event (subject, thread, `Message-ID`, references, the body it is
replying to), so that tombstone is a reply that can never afterwards exist. On a
busy connection the message a person cared about most is the one most likely to
be collapsed, and nothing in any surface says so: the event simply reports
`accepted` forever and no draft ever appears.

The rule is now stated once, in `comms._retained_event`: compaction may collapse
a record only when nobody is owed anything for it. `pending` is owed a decision;
`accepted` is owed a **reply**, and stays in full until it carries a durable
settlement marker.

**The marker.** `event["settlement"]` is `{disposition, result, digest, at,
actor, reason}` with two dispositions:

* `replied` — an outbox message answers this event. `create_result(for_event=…)`
  stamps it **inside the same transaction** that stores the reply, so no crash
  can land between "the answer exists" and "the message knows it does".
  `ChannelService.prepare_reply` passes it, which covers both the automatic
  capture path and the desktop one.
* `closed` — a local person recording that no reply is coming. It requires an
  actor and a reason, because it is the only route by which someone's message
  stops being work.

The marker lives on the *event*, not in the outbox. That is what lets an
answered message compact normally long after the reply itself has been pruned:
one retained class can never pin another. It is additive — not part of
`_event_digest`, not required by `_validate_event` — so a store written before
it existed loads unchanged, and a provider re-poll still de-duplicates against
the same immutable digest.

**Repair.** A reply stored by an older build (or by a process that died before
the marker existed) is repaired from the only durable evidence there is: the
outbox. `comms.mark_event_settled(disposition="replied", result_id=…)` refuses
unless that result is actually stored, or has a tombstone; the reconcile lane
calls it when it finds an answer the event does not acknowledge, which is also
what stops such an event from being re-examined on every pass forever.

**Bounded, without dropping work.** Exempting a class from the retention window
needs a ceiling of its own, and the honest answer at a ceiling is backpressure:

* `_compact_to_budget` never offers retained work as a candidate at any
  pressure. When what is left does not fit, `_save` raises `StoreFull` and
  writes *nothing* — the message already says to settle waiting messages, save
  or close the replies accepted messages are owed, or finish open sends.
* `accept_event` refuses past `MAX_UNSETTLED_ACCEPTED` (1000) with what to do
  about it. Receiving is unaffected: arrivals stay `pending` under their own cap.
* `connection_status` reports `accepted_awaiting_reply` and the limit, so the
  class that is exempt from the window is one a surface can watch fill up.

### 6a — the lanes that read that retained work

Retaining accepted work makes the event list longer, which would have made three
existing windows starve. All three now filter in the store, *before* the window:

* `comms.list_events` gained `needs=` (`"reply"` — accepted and not yet
  answered; `"reservation"` — an acceptance a crash left mid-enqueue) and
  `reserve_oldest=`, which moves the oldest/newest fairness rule into the window
  itself. Both are a bounded scan of the document the call already read — no
  secondary index to keep in step with the store.
* `_reconcile_candidates` asks for `needs="reply"` with the budget as the
  window, so it can no longer spend its slots on mail that was already answered.
* `recover_acceptances` asks for `needs="reservation"` first and gives the rest
  of the budget to resumable work. A stranded reservation names a task nothing
  else knows exists; it was previously reachable only if fewer than 200 events
  had arrived since — regression-tested with 210.
* `_draft_lane`'s hourly throttle now counts through
  `comms.count_acceptances(since=…)`, by **when each acceptance was made**
  rather than by arrival position. A mailbox synced with `history="all"` accepts
  mail that arrived long ago; counting by arrival order reported a connection
  that had just started ten tasks as idle. Its pending window is
  `comms.MAX_PENDING_EVENTS`, which covers the whole capped class, so an
  eligible message cannot hide behind ineligible ones in front of it.

Public retry shapes are unchanged, and `unknown` is still never auto-retried by
any path.

## What held under attack

Locked in as regression tests: an untrusted `From` and body cannot approve their
own unlisted sender (and the default projection masks both address and body); a
frozen draft scope cannot be widened by the stream query, nor forged onto
another connection or event; `restrict_draft` strips tools, hooks, gate,
compaction and memory, and `DraftComposer` drops every journal entry before the
accepted message; a reply threads on the provider's own `Message-ID` when it
differs from the one proposed; revising a reply cancels the original atomically
and clears `auto_eligible`; a refusal settles as `failed` and an abandoned claim
as `unknown`, with no automatic resend from either; attachment ids outside
`[0-9a-f]{64}` are refused and a tampered blob fails its digest check;
auto-replies, bulk mail and list mail are recorded as evidence and never become
tasks.

## Limitations

* **Accepted work with no answer needs a person.** An accepted message whose
  input was cancelled, whose task will never finish, or whose journal cannot be
  read is never settled by anything automatic — by design, since every automatic
  rule for "this one will never be answered" is a rule for silently discarding
  someone's request. It stays in full, and stays a reconcile candidate (the
  reserved oldest slots keep a handful of them from starving the lane). The way
  out is explicit: `comms.mark_event_settled(disposition="closed", actor=…,
  reason=…)`. Nothing in the desktop UI calls it yet, so today that resolution is
  an API call; a "no reply is coming" action in the inbox is the obvious follow-up.
* **Backpressure is reachable.** A connection that accumulates accepted messages
  and never answers them will hit `MAX_UNSETTLED_ACCEPTED`, or `MAX_STORE_BYTES`
  first if the bodies are large, and further acceptances (and eventually further
  writes) are refused until some are settled. That is the intended trade — a
  visible refusal rather than a quietly discarded reply — but it is a state a
  person has to act on, and `accepted_awaiting_reply` is currently only in the
  `counts` payload, not surfaced in the UI.
* **A withdrawn draft still counts as an answer.** `create_result(for_event=…)`
  marks the event when the reply is *stored*; cancelling or revising that draft
  afterwards does not un-mark it (the revision carries its own id). The event can
  therefore compact even though nothing was ultimately sent. That is a local
  person's decision rather than silent loss, but it means "settled" means "an
  answer was produced", not "an answer was delivered".
* **Parser vs store.** `mail_messages.parse` was left as it is: refusing NUL and
  line breaks at the store covers every adapter at once, including ones that do
  not go through the mail parser at all.
* **Unit retention tests use a shrunken scale.** The retention tests monkeypatch
  `_COMPACTABLE` to keep five settled events rather than writing 500, and the
  store-full test shrinks `MAX_STORE_BYTES`. The rule under test
  (`_retained_event`) is the real one. A separate production-limit benchmark now
  crosses the actual 500-record threshold with 620 settled messages and preserves
  the earlier acceptance; see [evaluation](communications-evaluation.md).
  The byte limit and 1,000-acceptance ceiling were not saturated.
  `_reconcile_candidates`' pre-window filter is
  likewise pinned by calling it with a small explicit budget rather than by
  writing 500 accepted events.
* **Not exercised:** live IMAP/SMTP/Twilio/relay transports, the browser UI
  itself, and the brief↔channel seam (a brief item referencing a connection
  whose events were compacted).
* **Concurrency:** finding 3 is reproduced by a deterministic re-entrant call,
  not by real threads. The interleaving it stands for is real — the two locks
  are genuinely disjoint — but the test does not measure how often it is hit.
* **Case comparison, repaired in final integration.** Thread matching now uses
  the same address normalization as the inbox allow-list. Parameterized tests cover
  received-message references and replies to an emailed Daily Brief with mixed-case
  owner addresses, while a different sender still cannot join the thread.

## Recipient changes, repaired before release

A separate acceptance-to-delivery experiment found that changing the configured
owner while a task was running could route its later answer to the new owner.
Acceptance now freezes its intended recipient. Capturing an automatic result
checks that pin under the same operation lock used by configuration changes;
a missing pin or changed recipient preserves the task answer and asks for a
reviewed reply. Existing prepared replies are also checked again when sent.
Address casing alone does not change the owner.

Tests exercise capture, reconciliation, legacy acceptances without a pin, manual
review and a real thread racing configuration against result capture. An older
reply for a previous owner stays pending without consuming the automatic send
budget or blocking replies addressed to the current owner. The task notification
explains the required review instead of promising automatic recovery; unexpected
transport errors still cannot expose credentials in that notification.
