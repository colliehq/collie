# Daily Brief implementation

A thoughtful morning read that is true. One factual snapshot, rendered by every
surface that shows it: the **`/brief` page** linked from the desktop's Today
dashboard, and an opt-in **email digest**. The page also fits a narrow browser;
there is no separate mobile app. These brief renderers share their counts and
content. The existing Today dashboard remains a separate overview.

`harness/daily_brief.py` is that snapshot builder. It is deterministic, evidence
backed, and model-free: a brief that says *"3 things need your attention"* must be
able to point at the three rows, and a sentence a model wrote cannot be pointed at.

## What it is not

* Not a second agent loop. It does not fetch, schedule, send, crawl, or call a model.
* Not an authority. Titles, agendas and mail-derived text are **data**. They are
  collapsed to one bounded line, stripped of control characters, and HTML-escaped
  again in the renderers. Nothing in a payload can become an instruction or a link.
* Not a task store. It never writes to the sources it reads. The only file it owns is
  its own small preferences/seen store.

## Integration API

```python
from harness import daily_brief

brief = daily_brief.build(
    payloads,                     # {source name: the JSON body that source returned}
    now=None,                     # epoch seconds; default wall clock
    timezone="Europe/Helsinki",   # IANA key, or a datetime.tzinfo instance
    utc_offset_minutes=None,      # preferred fallback: what a browser/phone knows
    fallback_timezone="utc",      # or "system"
    language="en",                # "en" and "zh" have labels; others fall back to en
    state_dir=None,               # enables dismiss/snooze + staleness; omit for pure
    profile="default",
    remember=True,                # record seen-bookkeeping when state_dir is set
    news=None,                    # None = off. A list of already-sourced items.
    top_limit=3,
)

text  = daily_brief.render_text(brief, bilingual=False)   # -> str
email = daily_brief.render_email(brief, bilingual=False)  # -> subject/text/html/headers
```

`payloads` keys are `daily_brief.SOURCE_NAMES`, and
`daily_brief.PAYLOAD_ENDPOINTS` maps each to the local endpoint Today already fetches:

| name | endpoint | used for |
| --- | --- | --- |
| `personal` | `/api/personal` | sourced reminders, tracked events, connected sources |
| `missions` | `/api/missions` | attention / progress / done, scheduler health |
| `approvals` | `/api/approvals` | blocking decisions (always rank first) |
| `procedures` | `/api/procedures?limit=20` | proposed routines |
| `runs` | `/api/runs` | in-flight and finished-today runs |
| `meetings` | `/api/meetings/schedule` | the real calendar agenda |
| `task_inbox` | `/api/task-inbox/pending` | queued work, and what is stranded on a person |
| `communications` | `/api/channels` | messages already received, replies already written |

Each value is read three ways, and the difference is the whole point:

| value | state | rendered as |
| --- | --- | --- |
| a payload dict | `ok` | its actual entries |
| `{"__unavailable": true}`, a dict with `error`, or any non-dict | `unavailable` | *"Could not read: … Nothing missing has been assumed clear."* |
| missing / `None` | `absent` | never configured; contributes to the setup state |

`all_clear` is `true` only when every supplied source was read **and** at least one
answered **and** nothing needs attention. A failed integration can never present as a
clear day.

Two distinctions inside those payloads decide whether the brief is honest, and both are
the source stores' own vocabulary rather than this module's guess:

* **Terminal is not finished.** `webapp._run_end` publishes `stop_reason` beside
  `state` precisely because a run stopped by an error or a turn/budget cap has ended
  without answering. Those rows are **attention**, carrying the run's own `error`;
  only a completed run is listed under *Done today*; canceled work is omitted.
  Counting an aborted run as done is how the same work gets asked
  for twice.
* **A scheduled wait is not always progress.** `quota_resume.status` reports
  `scheduled` / `queued` / `starting` (the server will start this) but also `stalled`,
  `stopped` and `unknown` (nothing will, without a person). Only the first group files
  a queued session under *In progress*; the rest are attention, with the wait's own
  reason as the detail.

A fourth state sits between `ok` and `unavailable`: **partly read**. A mailbox is
unbounded and a brief is not, so the collector reads at most
`COMMS_CONNECTION_LIMIT` (16) connections and `COMMS_ROW_LIMIT` (60) received messages
and replies per connection. Whatever is past that — or a connection whose store would
not open at all — puts the source in `coverage["partial"]`, which forces `all_clear`
to `false` and adds a notice saying what was *not* covered. Reporting it `unavailable`
would throw away the connections that did open; reporting nothing would let an unread
message be rendered as a clear day.

### Messages

`communications` is the only source whose rows were written by strangers, so it is the
most tightly bounded one. It is a read of the local store behind `/api/channels` —
`ChannelService.overview()`, `.events()` and `.results()`, and nothing else. Opening
the brief does not poll a mailbox, probe, send, retry, accept, reject or read a
credential, and it is **not** a live check with a provider: it reports what the
connection last stored, with each source's own `collected_at` beside it.

| what the store holds | how the brief reads it |
| --- | --- |
| pending message, sender the owner allowed | **needs you** — *ready for review* |
| pending message, sender not allowed / automatic / bulk | counted in a notice, **no row** |
| rejected message | settled; it keeps its record in Communications and makes no claim |
| accepted message | the task it became, folded into that session's existing row |
| pending reply | **needs you** — *ready to send*, and says so if the connection is paused |
| `failed` / `unknown` reply | **needs you**; `unknown` is never retried by itself |
| `sending` | in progress |
| `submitted` today | done today — *"The service accepted it. Delivery is not confirmed."* |

Four rules hold that table up:

* **Only a subject.** No body, no sender or recipient address, no attachment name, no
  raw-message reference and nothing about a credential leaves the store. A message with
  no subject gets a fixed, module-written line instead. The collector projects those
  fields away before the builder ever sees them, and the builder reads no others.
* **Severity is the stored state, never the text.** "URGENT" in a subject buys exactly
  as much rank as any other subject. If it did not, ranking a person's morning would
  belong to whoever sends the most mail.
* **Accepted work is shown once.** An accepted message carries the session its task
  runs under, so it shares that session's dedupe key at the lowest rank and collapses
  into the Missions/Needs You/session row that already exists (`evidence.also_in`). A
  reply still waiting to go out wins that same key: the inbound message has been dealt
  with, the send has not.
* **Accepted by a provider is not delivered.** `submitted` means the transport took the
  message. The brief says that in those words, and `delivery_known` stays false
  everywhere; a bounce arriving later is not something this snapshot can know.

A **paused** connection is read exactly like any other. Pausing stops new mail
arriving; it does not settle the messages and drafts already sitting in the store, and
hiding them would be the actual dishonesty. A root with **no connections** is an empty
local store — `ok`, zero rows, no notice — and never a claim that a mailbox is
connected.

Likewise, *nothing is connected* and *everything we asked failed* are opposite facts
and get opposite words: `setup["reason"]` is `"no sources are configured"` only when
every source was `absent`, and `"no source responded"` when any of them errored. A
fresh profile whose endpoints all answered with empty bodies is `ok` and reaches
neither — an empty store is an answer.

### The snapshot

`build()` returns a JSON-safe dict: `id`, `date`, `generated_at`, `language`,
`timezone`, `greeting`, `headline`, `all_clear`, `sources`, `coverage`, `top` (≤3
actionable/attention), `attention`, `agenda` (compact, today only), `progress`,
`completed`, `news`, `suggestions` (1–3), `suppressed`, `setup`, `counts`, `notices`,
`reply`.

Every item carries a stable `id` (`kind:16-hex`, derived from source + native id), a
content `fingerprint`, a `source`, a `source_ref` with a validated `href`, bounded
`evidence`, `first_seen_date`, `days_seen`, `changed_since_last_brief`, `stale`, and
`generated` (below). A message or reply id is namespaced by *connection* as well as by
the provider's row id, because two accounts can hold the same `Message-ID` and they are
not the same thing.

`coverage` is `{ok, unavailable, absent, partial, complete, configured}`; `counts` adds
`messages_quiet` (messages deliberately not shown) and `connections` (how many message
connections were read) to the section lengths.

#### Language, and whose words get translated

`item["generated"]` lists the fields whose words **this module wrote** — the fallback
title of a row that had none, and the fixed detail lines like *"Coming up soon."* or
*"Ready to send from Communications."* Those are translated when `language="zh"`; every
other string is the source's own — a subject line, a mission title, a store's error
message — and is rendered exactly as it was stored, in whatever language it was
written. Translating those would be a rewrite of somebody's text, not a translation of
ours.

Translation happens after ids, fingerprints and ordering are fixed, so switching
language does not move a row, split a history entry, or forget that a row was hidden.

`counts` are `len()` of real entries — there is no placeholder row anywhere. An empty
agenda is `[]` plus a notice; the *"No events today"* row that gets counted as an
appointment by the next person to write a loop does not exist here. The notice fires
whenever there is no calendar evidence at all — including the commonest case, a profile
where the calendar endpoint was never wired up — and stays quiet when the meetings
source is `unavailable`, because *"could not be read"* is already said and *"not
connected"* would be a second, wrong explanation for the same empty list.

Rows with a `when` also carry `when_offset_minutes` — the UTC offset in force at *that
instant*, not at local midnight. On a spring-forward day those differ by an hour, and a
single day-level offset printed every afternoon meeting one hour early.

#### Two identities, on purpose

`id` names **this content**. It digests the date, profile, resolved zone, headline,
counts, coverage, setup state and the `fingerprint` of every rendered row. A mission
going `needs_you → failed`, an approval arriving, a source falling over or a row being
hidden all produce a *different* id, because they are a different brief. An id built
only from the date and the row ids (which never move when a row's status changes) would
collapse two different briefs into one history entry and put the same
`X-Collie-Brief-Id` on two different emails.

`reply["day_key"]` (`daily-brief:<profile>:<YYYY-MM-DD>`) names **the day**. It is
stable across every rebuild of that local day, and it — not `id` — is the
at-most-one-email-per-day job key. Deduping sends on `id` would mail the user again
every time anything moved.

`reply` is `{brief_id, date, day_key, subject_tag, sources}`; with the email
`X-Collie-Brief-Id` / `X-Collie-Brief-Date` headers it is what lets a later reply be
tied back to the brief that prompted it. Storing and matching that is the caller's job.

### Feedback

```python
daily_brief.dismiss(item_id, state_dir=..., fingerprint=item["fingerprint"])
daily_brief.snooze(item_id, until_epoch, state_dir=...)
daily_brief.restore(item_id, state_dir=...)
daily_brief.feedback_state(state_dir=...)
```

Written to `<state_dir>/daily-brief/<profile>.json` under `statelock.transaction`
(process lock + `os.replace`), so concurrent web/CLI/native writers cannot lose each
other's updates. Retention is bounded: 500 preferences / 45 days, 1000 seen entries /
30 days, 60 logged briefs. The store keeps ids and fingerprints — no titles.

**Hiding an item from the brief is not cancelling, completing, or acknowledging it.**
The task keeps its row in Missions, Needs You and the thread list, and the brief's own
`counts["attention_total"]` still counts it. A dismissal applies only while the
content fingerprint is unchanged: a changed status or changed evidence resurfaces the
item. **Pending approvals can never be hidden** (`UNSUPPRESSIBLE_KINDS`) — `dismiss()`
returns `{"ok": false, "refused": ...}`.

Pass `fingerprint=item["fingerprint"]`. A caller that omits it has recorded nothing
that can be re-checked when the item changes, so the "it comes back when it changes"
guarantee cannot be honoured for that preference; rather than let it run to the 45-day
TTL and bury a mission that has since failed, an **unanchored dismissal lasts only for
its own local day**. A snooze is already bounded by its own deadline and is unaffected.

Repeating yesterday's obligations verbatim forever is the defect that makes a daily
brief worthless. Beyond dismissal, an unchanged non-decision item older than
`STALE_DAYS` (3) is marked `stale` and loses its claim on the three top slots. It is
still listed, and renderers print *"unchanged since &lt;date&gt;"* — demoted, never
dropped, because silently dropping an obligation is worse than repeating it.

### Timezones and day boundaries

The day is local midnight to the next local midnight, so a DST day is 23 or 25 hours
(`timezone["day_hours"]`). An invalid zone name, or a valid IANA key on a machine with
no `tzdata` — which is the stock Windows CPython this product ships against — does not
raise and does not silently pretend to be UTC. It degrades in this order:

1. `utc_offset_minutes`, if the caller passed one (a browser and a phone both know it);
2. the OS local zone, if `fallback_timezone="system"`;
3. UTC.

`timezone["degraded"]`, `["kind"]`, `["reason"]` and `["resolved"]` record what
happened, and both renderers print the reason. A midnight that does not exist in a
spring-forward zone resolves to the pre-transition offset.

`greeting` follows the **reader's current local hour**, not the start of the day
window — whose hour is 0 on every day there has ever been, which is how a 07:00 morning
summary came to open with *"Good evening."*

### Links

`source_ref["href"]` is either a local whitelisted UI route (first segment in
`_LOCAL_ROUTES`) or, for caller-supplied news only, a validated `https` URL with a
hostname and no embedded credentials. `javascript:`, `data:`, `//host`, `http`,
traversal, backslashes, interior whitespace and control characters are **dropped**,
not escaped — a link is an action target, not text. Nothing in a brief executes; each
suggestion is a `prompt` plus `requires_user_intent: true`.

The allowlist is the set of paths `webapp.Handler` actually serves. Missions, Needs
You, Today, sessions, the Library and Activity are **panes of the single-page app at
`/`**, not routes: rows that linked to `/missions` or `/needs-you` 404ed. They are now
reached the way the page reaches them itself — `/?mission=<id>` and `/?session=<id>`,
the query keys `index.html` reads on load. The identifier is percent-encoded data and
the finished link still has to satisfy `safe_href`, so no id can change a link's
target; a row whose id will not survive that simply renders as unlinked text.

## Architecture, and what is actually available

```
stores under <state root>        harness/daily_brief_web.py      harness/daily_brief.py
 personal / missions / runs  ->  collect(root)  -> payloads ->  build() -> one snapshot
 approvals / procedures          (read-only, one                  |
 meetings / task_inbox            guard per source)               +-> render_text()
 channels + communications                                        +-> render_email()
                                                                        |
 webapp.Handler  --  GET /brief (daily_brief.html)   <- fetch --  GET/POST /api/brief
                     GET/POST /api/brief/preferences  ------------> daily_brief_schedule
```

| surface | state |
| --- | --- |
| `GET /brief` — the page, in-app and in a browser | mounted, authenticated |
| `GET /api/brief` — snapshot + freshness + email preview | mounted, authenticated |
| `POST /api/brief` — `dismiss` / `snooze` / `restore` / `preview` | mounted, authenticated |
| `GET`/`POST /api/brief/preferences` — the opt-in email settings | mounted, authenticated |
| the opt-in daily email (`daily_brief_schedule`) | implemented and **off** by default; the settings routes are mounted, and the periodic `tick(root)` that actually sends is called from the app's pump (parent-owned wiring). A profile that never opts in never sends, and nothing here starts a send by itself. |
| mobile | not started; it would be the same endpoint and the same snapshot |

Every route is an authenticated read or write of local state. Opening the brief runs no
model and no tool, and `_preview` renders the mail text without sending it
(`sendable: false`).

**A brief is a snapshot, not a live check.** Each source is read from disk (or, for
runs and approvals, from this process's memory) when the brief is built, and every
source carries its own `collected_at` that the page shows. In particular, the messages
source reports what the connection last stored — it does not contact a mail server, and
a message that arrived one second ago appears once the connection's own polling has
stored it. "Fresh" here means *freshly read from the store*, and the brief never claims
more than that.

**Dismiss and snooze are about this brief only.** They hide a row while its content
fingerprint is unchanged; they do not cancel, complete or acknowledge the work, and
approvals cannot be hidden at all. See *Feedback* above.

**Provider accepted ≠ delivered.** A reply the transport took is reported as accepted
by the service, with delivery explicitly unconfirmed — in the brief, in the email, and
in the schedule's own outbox row.

### Not built, and not claimed

* **No news fetching.** `news=` still takes items a caller already has. Nothing in this
  work added a crawler, a feed reader or an outbound request of any kind.
* **No semantic reading of your mail.** The messages source summarises *connected local
  work* — what is waiting, what is drafted, what failed to send. It does not extract
  commitments, deadlines, prices or intent from message bodies; it never reads a body
  at all. A "what did my inbox actually ask of me today" summary is a different feature
  and is not here.
* **No account provisioning.** Connecting a mailbox, creating an address and storing
  credentials live in the communications settings surface, not here. The brief reads
  connections that already exist; if there are none, it says the store is empty rather
  than offering to make one.
* **The email digest's rules are enforced by `daily_brief_schedule`, not by this
  module.** At most one message per local day keyed on `reply["day_key"]` (never on
  `id`, which moves whenever content moves), a frozen recipient taken from the
  connection's owner address at opt-in, a bounded send window with a visible skip, and
  no send at all without an explicit opt-in.

## Limitations, honestly

* **Offline / partial.** Only local sources are read. A source that is down is
  `unavailable`, which visibly shrinks the brief's claims; it never widens them.
* **News is off by default** and this module contains no crawler. `news=` accepts
  items the caller already obtained, and each one is dropped unless it carries both a
  publisher name and a safe `https` link. There is no speculative discovery.
* **Replies and actions need explicit intent.** A reply to a digest is a *description*
  of a request, routed through the existing untrusted-data framing in
  `harness/communications.py`. It cannot approve, send, buy or cancel anything.
* **Lock screens.** Notifications derived from a brief default to counts and section
  names only — no titles, no senders, no amounts. Calendar rows the meeting store
  marks `sensitive` are already reduced to *"Private event"* with no location inside
  the snapshot itself, so no renderer can leak them.
* **`language`** ships labels for `en` and `zh` and falls back to English elsewhere.
  Only the strings this module wrote are translated (`item["generated"]`); text that
  came out of a source is always the source's own words.
* **Messages are summarised, not understood.** A waiting message is reported as
  waiting. What it asks for is not read, inferred or ranked, and a subject line cannot
  move a row up the page.
* **Not a planner.** `suggestions` are 1–3 deterministic drafts derived from rows in
  the same brief. They are prompts a person may choose, not a plan being executed.

## Tests

`tests/test_daily_brief.py` builds its own payloads; `tests/test_daily_brief_web.py`
drives real stores under `tmp_path`, including a configured message connection whose
transport raises on every call. No network, no mail server, no credentials, no model
and no real user state: addresses are `example.test` fixtures and message bodies are
literals written in the test file.

The builder suite covers item identity and cross-source dedupe, counts equal to real
entries, no phantom agenda rows, empty vs unavailable vs absent vs partly-read sources,
local-day and DST boundaries, invalid zone and missing tzdata, hostile HTML and unsafe
links, dismiss/snooze/restore with content-change resurfacing, unhideable approvals,
stale demotion, a corrupt store preserved and not applied, bounded retention, and
text/HTML renderer agreement.

Each of these is a counterexample to a defect the module actually had:

| test | what it would otherwise do |
| --- | --- |
| `test_the_greeting_follows_the_reader_not_local_midnight` | greet every reader "Good evening.", at any hour |
| `test_times_use_the_offset_in_force_at_the_event_not_at_midnight` | print afternoon rows an hour early on a spring-forward day |
| `test_a_run_that_stopped_without_finishing_is_not_done_today` | list an error/turn-capped run under *Done today* |
| `test_a_stranded_request_is_not_hidden_behind_a_dead_scheduled_wait` | file a `stalled` request as progress and call the day clear |
| `test_every_source_failing_is_not_reported_as_nothing_connected` | tell a user whose endpoints all 500ed that nothing is connected yet |
| `test_an_empty_day_without_a_calendar_says_so_even_when_unconfigured` | show an empty agenda with no caveat when no calendar exists |
| `test_the_brief_id_names_the_content_and_the_day_key_names_the_day` | give two materially different briefs one id |
| `test_a_hide_with_no_fingerprint_cannot_bury_work_for_forty_five_days` | keep hiding a mission that has since failed |
| `test_every_row_links_somewhere_the_server_actually_serves` | link every row to a 404 |
| `test_a_stranger_cannot_rank_himself_first_by_typing_urgent` | let anyone who can reach the mailbox head somebody's morning |
| `test_a_send_that_failed_or_was_never_confirmed_is_never_quiet` | leave an `unknown` send — which is never retried — unmentioned |
| `test_provider_acceptance_is_not_delivery` | report a reply the transport merely accepted as delivered |
| `test_a_reply_waiting_to_be_sent_outranks_the_message_it_answers` | show the same exchange twice, inbound row and outbound row |
| `test_an_unreadable_connection_is_partial_coverage_not_a_clear_day` | read one broken connection as an empty inbox |
| `test_a_mailbox_bigger_than_one_brief_says_so_instead_of_reading_clear` | call a mailbox clear after looking at the newest 60 rows |
| `test_nothing_from_inside_a_message_reaches_the_brief` | copy a body, an address or an attachment name into a mailed digest |
| `test_chinese_translates_our_words_and_leaves_the_readers_own_alone` | print a Chinese brief with "Coming up soon." in it |
| `test_the_email_names_the_clock_and_keeps_our_ids_out_of_sight` | mail bare times with no zone, and an internal id in the subject |
| `test_messages_are_read_from_the_store_and_the_transport_is_never_touched` | open a mailbox because somebody opened the brief |
