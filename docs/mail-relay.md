# Collie Mail relay

`relay/mail_worker.js` is the one Worker behind `mail.collie.run`. It gives every dog an address,
seals every incoming message to that dog's X25519 key before storing it, and — new in `collie-mail/2`
— lets a dog write **one reply to its own owner**, with at most one provider attempt per request id.

> **Outbound is unproven.** Nothing in this repository has demonstrated a delivered message. The
> Email Routing `send_email` binding only delivers to addresses verified *on the Cloudflare account*,
> which is not the same as an owner who passed our own six-digit check, and the newer Email Sending
> API needs sending-domain provisioning this account has not been shown to have (a probe answered
> `401`). Read [Deployment prerequisites](#deployment-prerequisites) before treating `/send` as a
> feature rather than as code waiting for an account that may send.

What it deliberately is not: a mail relay. There is a single permitted destination per mailbox, the
email address that verified the handle. No recipient list, no CC, no BCC, no caller-chosen `From`. A
dog that can email anyone is a spam cannon with a key, and "tell me what happened" needs none of it.

Be precise about the privacy edge, because it is the product's whole claim. Incoming mail is sealed
to the dog's public key the moment it arrives and only the ciphertext is stored, so the operator
holds bytes it has no key for. But SMTP is cleartext: the message exists in plaintext in this
Worker's memory for the moment between delivery and sealing. *Never stored in the clear* is the
promise. End-to-end is not, and saying so would be a lie.

## Endpoints

Legacy clients are unaffected in request shape: `/pubkey`, `/handle/claim`, `/handle/verify`,
`/dog/claim`, `GET /mail`, `DELETE /mail` take exactly what they took before, and `/pubkey` answers
byte-for-byte what it did. The claim endpoints gained *additional* response fields and two new
refusal codes (409 conflict, 429 cooldown/lock) — see [Claiming a name](#claiming-a-name).

### Authentication

Unchanged. Every authenticated request carries four headers:

| header | value |
| --- | --- |
| `x-collie-addr` | the dog's full address |
| `x-collie-ts` | whole seconds since the epoch, within ±120s |
| `x-collie-nonce` | 8–128 chars of base64/base64url, single use for 240s |
| `x-collie-mac` | `HMAC-SHA256(auth_key, lp(method) ‖ lp(pathname+search) ‖ lp(ts) ‖ lp(nonce))` |

`auth_key = HKDF-SHA256(X25519(dog, relay), salt=address, info="collie-mail-auth")`, and `lp()` is a
4-byte big-endian length prefix. **The MAC covers `pathname + search`**, which is what makes the
`?sha256=` on a send, and the `?cursor=` on a page, authenticated rather than merely present.

### `GET /capabilities` — unauthenticated

```json
{ "ok": true, "version": "collie-mail/2",
  "receive": { "bounded": true, "max_bytes": 4194304,
               "oversize": "rejected at SMTP (552), never truncated", "retention_seconds": 604800 },
  "paging":  { "endpoint": "/mail-page", "max_items": 50, "max_bytes": 4194304 },
  "identity": { "ledger": true, "serialized": true, "code_max_sends": 5, "code_max_attempts": 5,
                "code_cooldown_seconds": 60, "claim_ttl_seconds": 1800 },
  "send":    { "endpoint": "/send", "binding": true, "ledger": true, "configured": true,
               "mode": "structured", "max_body_bytes": 98304, "max_text_bytes": 65536,
               "destination": "the verified owner of the handle, and no one else",
               "note": "configured is about bindings, not about permission — the account may still refuse" } }
```

`configured` means the `MAILER` and `MAIL_DELIVERY` bindings exist. **It is not proof the account is
allowed to send.** Cloudflare can still refuse every message because the sender domain is not
verified, and there is no call this Worker can make that proves otherwise without sending real mail.

### `GET /mail-page?cursor=…` — authenticated

The read a client can actually finish. Bounded by *both* a message count and a total sealed-byte
budget, whichever hits first.

```json
{ "ok": true,
  "messages": [ { "id": "9f2c…", "at": 1758600000, "env": { "epk": "…", "n": "…", "ct": "…" } } ],
  "next_cursor": "eyJtIjoi…",
  "more": true }
```

- `cursor` is omitted on the first call and is otherwise the previous `next_cursor` verbatim
  (base64url, ≤2048 chars). It is scoped to one mailbox and refused if replayed against another.
- `id` is a stable opaque message id. Page with ids, not with a timestamp high-water mark: KV is
  eventually consistent, so a message that lands a second late is behind a timestamp mark *forever*.
  Repeatedly scanning all retained messages and skipping known ids is safe and is the intended use.
- `more: true` means keep going; `next_cursor` is `null` only when `more` is `false`.
- A message larger than the byte budget is still returned alone, so no page can be un-gettable.
- Reading never deletes. Unread mail stays unread; only `DELETE /mail` removes anything.

### `POST /send?sha256=<hex of the exact request body>` — authenticated

Body, `application/json`, ≤96 KiB:

| field | required | bound |
| --- | --- | --- |
| `id` | yes | 8–128 chars of `A–Z a–z 0–9 . _ : -`; the idempotency key |
| `to` | yes | must equal the handle's verified owner address |
| `subject` | yes | 1–512 UTF-8 bytes, single line |
| `text` | yes | 1–65536 UTF-8 bytes, plain text |
| `in_reply_to` | no | one or more `<message-id>` tokens, ≤2048 UTF-8 bytes, printable ASCII |
| `references` | no | same |

`cc`, `bcc`, `from`, `reply_to`, `headers` and `html` are rejected outright (400) rather than
ignored — an ignored field looks like a supported one.

Every message goes out with `Auto-Submitted: auto-replied` and `X-Auto-Response-Suppress: All`, so
an owner's own vacation responder cannot start a loop.

Responses:

| status | body | meaning |
| --- | --- | --- |
| 200 | `{ok:true, id, status:"sent", receipt, duplicate, at, updated}` | accepted by the provider |
| 202 | `{ok:false, id, status:"unknown", …}` | the outcome is genuinely not known |
| 400 | `{ok:false, error}` | bad digest, bad field, or a destination that is not the owner |
| 401 | `{ok:false, error}` | stale, replayed or invalid stamp |
| 409 | `{ok:false, error}` | this id was already used with a *different* body |
| 429 | `{ok:false, retry_after}` | per-mailbox rate guard: 5/minute, 30/hour |
| 501 | `{ok:false, error}` | `MAILER` or `MAIL_DELIVERY` is not bound |
| 502 | `{ok:false, id, status:"failed", error:"E_…"}` | an explicit provider refusal |
| 503 | `{ok:false, error}` | the mailbox's receipt ledger is full |

`duplicate: true` means the receipt was replayed, and **no** second message was sent.

`recorded: false` appears only in the narrow case where the provider call finished but the ledger
could not be told the outcome. The body reports what was observed; the id's row is still `sending`,
so `/send-status` answers `unknown` for it from then on and nothing reissues it. A 500 there would
have hidden a message that had already gone out.

`receipt` is Cloudflare's `messageId` where one is returned. `status: "sent"` means the provider
accepted the submission. **It is not proof of delivery** — nothing available here is.

### `GET /send-status?id=…` — authenticated

Returns the same receipt shape (and the same status codes: 200 sent, 202 unknown, 502 failed, 404
for an id this mailbox has never used).

## At most one attempt per request id, and what happens when that is not enough

This is **not** exactly-once delivery, and it is worth being exact about what it is. The guarantee
is: *while a receipt for a request id is retained, at most one call to the mail binding is ever made
for that id.* Three gaps follow directly, and none of them is closed here:

- Receipts are pruned after **30 days** (`RECEIPT_TTL`). A client that replays an id older than that
  gets a fresh reservation and a second provider attempt. The window is a retention policy, not a
  proof, so "exactly once" would be a claim about client behaviour rather than about this code.
- "One provider attempt" is not "one delivery". `status: "sent"` means Cloudflare accepted the
  submission; what happens afterwards — a bounce, a retry inside the provider, a spam folder — is
  invisible here.
- If the attempt's outcome is unknown, it stays unknown forever. The message may or may not have
  gone. Nothing here can tell you which.

KV cannot even do the narrow guarantee: two concurrent `/send` calls with one id both read "nothing
sent yet" and the owner is mailed twice. So the ledger is a **SQLite Durable Object, one per
mailbox** (`MAIL_DELIVERY`, class `MailDelivery`). The sequence is:

1. **Reserve** inside `storage.transactionSync` — nothing awaited inside it, so no second request
   can observe the gap between "is there a row" and "there is now". The row is written with status
   `sending` *before* the provider is called.
2. **Send**, outside any transaction and outside any retry. At most one binding call per reservation.
3. **Complete** — record `sent`, `failed` or `unknown`.

Failure classification is the part that matters:

- An **explicit refusal** — `E_VALIDATION_ERROR`, `E_SENDER_NOT_VERIFIED`, `E_RECIPIENT_NOT_ALLOWED`,
  `E_SENDER_DOMAIN_NOT_AVAILABLE`, a rate limit — is a fact: it did not go. Status `failed`.
- `E_INTERNAL_SERVER_ERROR`, a timeout, a disconnect, or a Worker that dies mid-send is **unknown**,
  and stays unknown. Nothing reissues it. A retry needs a *new* request id and an explicit decision
  to accept the risk of a duplicate.

Only the matched `E_*` token is ever stored or returned. The provider's message body is dropped in
the Worker, because it may quote the mail, the account, or a credential.

The ledger rows hold the request id, the body digest, a status, a provider receipt and two
timestamps. **Never the subject, never the text** — a ledger that remembers what was said is a copy
of the mail, which is the one thing this relay promises not to keep. Rows are pruned only once they
are older than 30 days, and if a mailbox somehow reaches 2000 live rows the relay answers 503 rather
than dropping a row a client could still replay: discarding dedupe is how a retry becomes a second
delivery.

## Claiming a name

A handle and a dog address are identities: once one is bound to a public key, every later request
from that key is authorised by it. So binding must be a decision, not a race.

It used to be a race. `/handle/claim` and `/dog/claim` read KV, decided, and wrote KV, with an
`await` in the middle. Two claims for one name that arrive together both read "free", both write,
and the name ends up bound to **whichever write landed second**. That is a takeover whose only cost
is timing, and the old note that it was "self-correcting because the loser's code will not match"
was wrong in the direction that matters: it is the *winner's* claim that gets overwritten, and the
loser's code is the one that then works.

The decision now happens inside a **SQLite Durable Object named after the claim** (`CLAIMS`, class
`DirectoryClaims`) — one object per handle, one per dog address, one per recipient address for the
send budget, so the serialization is per name and there is no global bottleneck. Inside the object
the whole read/decide/write runs in `storage.transactionSync` with **nothing awaited inside it**,
which is the only construct here that actually serializes across concurrent calls. KV is demoted to
a mirror: the object writes it *after* committing, in exactly the shapes readers already parse
(`handle:<name>` `{pub,email,code,verified:false}` with a TTL while pending, `{pub,email,verified}`
once verified; `dog:<address>` `{pub,handle}`).

**Existing identities are adopted, not ignored.** The first operation for a name imports whatever KV
already holds as the ledger's baseline, so a handle verified before this change stays verified to
the same key and a re-claim with a different key is refused. No migration step, no export, and
nothing to run: the adoption is the first claim that touches the name.

| situation | answer |
| --- | --- |
| free name, or an expired pending claim | 200 `{ok:true, sent:true}` — a fresh six-digit code is mailed |
| same key **and** same address re-claiming a live pending claim | 200 `{ok:true, sent:false, retry_after}` inside the 60 s cooldown; past it, the **same code** is re-sent (`sent:true, resent:true`) |
| a different key or a different address on a live pending claim | **409**, and the pending claim is left exactly as it was |
| a verified handle, same key and address | 200 `{ok:true, sent:false, verified:true}` — idempotent, mails nothing |
| a verified handle, any other key | **409** `that handle is taken` |
| a 6th code for one pending claim | **429** — five per claim, ever |
| an 11th code to one email address in an hour | **429** — across every handle |
| `/dog/claim`, address already bound to another key | **409**; the same key is idempotent |
| `CLAIMS` not bound | **503**, naming the binding |

That last row is deliberate: when the ledger is missing, claiming is **refused**, not served by the
old read-then-write path. A fallback that silently restores the defect is worse than an outage,
because it is invisible. `/capabilities` reports `identity.ledger: false` in that state.

The code itself is six digits from `crypto.getRandomValues`, rejection-sampled so all 900 000 values
are equally likely. Six digits is only a secret if guesses are bounded, so `/handle/verify` allows
**five** wrong attempts against a pending claim and then answers 429 until the claim expires — the
counter lives in the same object as the code, so it cannot be raced past either. Both the code and
the public key are compared in constant time. Verifying the same claim twice with the same key and
code returns `ok` rather than an error, so a client that lost the first response does not conclude
its code was wrong.

Resends deliberately **preserve the code**: minting a new one each time an impatient client asked
would invalidate the message already sitting in the owner's inbox.

## Incoming mail

- **4 MiB** per message, and the reason it is not 8 or 25: this Worker inflates before it stores.
  Raw → base64 (×4/3) inside a JSON payload → AES-GCM ciphertext → base64 again. 4 MiB of RFC822
  lands in KV at roughly 7.2 MiB, inside KV's 25 MiB value limit, with a transient peak near 30 MiB
  against the Worker's 128 MiB.
- `message.rawSize` is checked first where the runtime offers it, so an oversize message is declined
  *before* it is buffered; the bounded stream read then enforces the same limit on a size that was
  under-reported.
- Oversize mail is **rejected** with `552`, not truncated. A half-message stored as if it were whole
  is the worse failure: the sender is told it arrived and the dog reads a body that stops mid-header.
- Within the cap, the MIME is preserved byte-exact.
- Base64 is chunked at 32 KiB. The old `btoa(String.fromCharCode(...bytes))` spread every byte into
  an argument list and threw `RangeError` on exactly the messages worth carrying.
- Each row stores a stable opaque `id` that matches the KV key's last segment; rows written before
  ids existed derive the same id from that key, so both readings agree.

## Deployment prerequisites

The public **Mail relay** Actions workflow tests the protocol and bundles the Worker
without account credentials. Its artifact includes `mail_worker.js`, deployment
configuration, source commit and SHA-256 checksums. Deploy the verified artifact
with `wrangler deploy --no-bundle --config <artifact>/mail.wrangler.toml` so the
deployed code is the code built on the public repository. Deployment credentials
stay with the operator; they are not required by the build.

1. **Secrets** (`npx wrangler secret put -c relay/mail.wrangler.toml <NAME>`): `RELAY_PRIVATE_B64`
   and `RELAY_PUBLIC_B64`. Unchanged, and neither can open a message.
2. **KV**: `MAIL` and `DIRECTORY`, already provisioned.
3. **Durable Objects**: two bindings and two migrations in `relay/mail.wrangler.toml` —
   `MAIL_DELIVERY` (class `MailDelivery`, `tag = "v1"`) and `CLAIMS` (class `DirectoryClaims`,
   `tag = "v2"`). The first deploy carrying each migration creates that class.
   SQLite-backed Durable Objects are **available on the Workers Free plan**, with the free-plan
   limits documented at
   <https://developers.cloudflare.com/durable-objects/platform/pricing/>. (An earlier draft of this
   page said a paid plan was required; that was wrong. Free-plan *limits* still apply — check that
   page for current storage and request allowances before assuming headroom.)
4. **Sending — read this before believing `/send` works.** Two different Cloudflare products are
   easy to conflate, and only one of them is configured here:
   - The `send_email` binding is **Email Routing**, and it delivers *only* to addresses that have
     been added and confirmed as destination addresses on the Cloudflare account
     (<https://developers.cloudflare.com/email-routing/email-workers/send-email-workers/>). A handle
     owner who passed our six-digit check is **not** thereby verified on the account. Those are two
     unrelated verifications, and treating one as the other is the reason this feature can pass every
     test here and still refuse every real user with `E_RECIPIENT_NOT_ALLOWED`.
   - Sending to arbitrary recipients is the separate **Email Sending** product and requires sending
     domain provisioning on the account (<https://developers.cloudflare.com/email-routing/>).
     Whether this account has it has **not** been established: the current Email Sending API probe
     answered `401`. No message has been proven to leave this Worker by either route, so `/send` is
     unproven end to end, not production ready.
5. **`MAIL_SEND_MODE`** (the checked-in configuration selects `legacy` to preserve
   the deployed Email Routing contract): `structured` uses the current object API
   (`{from,to,subject,text,headers}` → `{messageId}`), where Message-ID, Date and the MIME headers
   are platform-controlled — so no Message-ID is passed, because the builder rejects one. Set
   `legacy` for an account still on the raw `EmailMessage` binding; that path composes RFC-5322 by
   hand with a stable `Message-ID` derived from the address and request id, used as the submission
   receipt only after the send promise resolves (a legacy send may resolve `undefined`). There is
   **no automatic fallback between the two modes** — trying the other shape after an ambiguous
   failure is how one notification becomes two.
6. Email Routing still needs its catch-all rule pointed at this Worker for incoming mail.

The handle-verification code still goes out through the legacy `EmailMessage` path regardless of
`MAIL_SEND_MODE`, because that raw-message form remains supported and changing it is not this
change's business.

**The blocker, stated once and plainly.** Both outbound paths — the verification code and `/send` —
go through the same binding, so the restriction in (4) gates sign-up as well as replies: on Email
Routing alone, `/handle/claim` can only mail an address already verified on the Cloudflare account.
Everything in this Worker is written to be correct *once that account can send*; none of it makes
the account able to. Closing it means provisioning Email Sending (or another provider) and proving
one real delivery — neither of which has happened.

## Tests

```
node tests/mail_relay_test.mjs        # or: pytest tests/test_mail_relay.py
```

`tests/mail_relay_test.mjs` imports and executes the *real* Worker exports — `fetch`, `email`,
`MailDelivery` and `DirectoryClaims` — against in-memory KV, Durable Object namespaces and a send
binding, with genuine X25519/HKDF/HMAC stamps on every request. 111 checks. It covers the claims
above that cannot be reviewed into existence:

- **Identity.** Two genuinely concurrent claims for one handle (and for one dog address) leave the
  name bound to the winner's key, answer the loser 409, and mail exactly one code; the loser cannot
  verify even holding the code that was sent. A KV identity written before the ledger existed is
  adopted rather than re-claimable. The owner's own exact re-claim is idempotent and silent.
- **The code.** Five wrong guesses, then 429 — including for the code that was actually mailed. A
  correct code presented by a different key fails. Verifying twice succeeds twice. Eight simultaneous
  claims send one email; a resend past the cooldown re-sends the *same* code; a pending claim is
  worth five codes and one address ten an hour; an expired claim's code is refused.
- **Refusal over fallback.** With `CLAIMS` unbound, all three claim endpoints answer 503, name the
  binding, and write nothing.
- **Sending.** Two concurrent sends of one id produce exactly one binding call; an ambiguous failure
  is never reissued; a send whose outcome cannot be recorded is reported rather than 500'd; an id
  reused after the owner changed neither sends nor leaks the old destination; owner-only
  destinations; a body swapped under a valid stamp; non-numeric timestamps.
- **Intake and reads.** base64 past 64 KiB, the MIME bounds in both directions, paging 205 messages,
  and a byte-capped page split without losing one.

`tests/test_mail_relay.py` is the pytest wrapper that puts those checks in the collected suite CI
runs, and skips (rather than silently passes) when Node <20 is unavailable.

## Known limits

- **Outbound sending is unproven.** See [Deployment prerequisites](#deployment-prerequisites). The
  ledger, the bounds and the refusals are tested; the delivery is not, and cannot be from here.
- **The KV mirror can lag the ledger by one crash.** The identity object commits, then writes KV. A
  Worker that dies in between leaves the ledger right and KV stale — the name is still *bound*
  correctly (no one else can take it), but reads that go through KV may not see it until the next
  claim for that name, which rewrites the mirror. Re-claiming with the same key is idempotent and is
  the repair.
- **Nonce replay protection is KV-backed, so it is best-effort.** A nonce is written to KV after a
  stamp is accepted and checked against KV on the next request; KV is eventually consistent, so two
  copies of one stamp arriving within a second of each other in different colos can both pass. The
  ±120 s freshness window still bounds it, and every write path behind it is idempotent by request
  id or by claim identity. Making it exact would mean routing every authenticated request through a
  Durable Object, which is a latency decision not yet taken.
- **Claim refusals are an enumeration oracle.** 409 tells a caller that a name is taken or pending.
  That was already true of the previous code and is inherent to telling an honest user why they
  cannot have a name.
- **Per-IP is not a dimension here.** The claim budgets are per name and per recipient address;
  nothing bounds one client claiming many *different* free names to many *different* addresses
  beyond the account's own send quota.
- **KV eventual consistency on reads.** `/mail-page` is safe against it *because* it pages by durable
  id rather than by timestamp, but a message can still take a moment to appear in a list. Clients
  should rescan the retained window rather than assume a page was final.
- **Rate guards are per mailbox, not per handle or per account.** A user with many dogs has many
  budgets.
- `status: "sent"` is provider acceptance. Bounces, spam foldering and silent drops afterwards are
  invisible to this Worker.
- `/capabilities` reports configuration, never permission.
