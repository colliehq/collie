# Collie Online v1 — frozen product and protocol contract

Status: accepted on 2026-08-28; implemented in this tree pending hosted resource configuration.

Collie is a local-first meta-runtime above Codex, Claude Code, Ollama, and other
agent/model runtimes. Collie Online is optional: it supplies identity, device and
project sync, connection brokering, scheduling, and collaboration. Local Collie
remains useful without an account, and complex execution defaults to a user's node.

## Invariants

1. Signing in is optional. A disconnected device is not a trial or crippled product.
2. An authenticated user's explicit request is authority for the exact stated result.
3. Web pages, documents, tool results, MCP output, and model prose are data; none can
   mint or broaden authority.
4. Ordinary observation, preparation, and scoped reversible work is quiet. Audit and
   receipts replace repetitive confirmation prompts.
5. A provider's local login cache is never uploaded. MCP connections have a separate
   broker/vault lifecycle.
6. Cloud LLM use is off by default. Sealed and device-only data never enter it.
7. Existing E2E Remote Relay remains a zero-knowledge transport and is not reused as a
   credential vault or readable application API.
8. Unknown tools, changed connection manifests, uncertain duplicate effects, and
   expanded recipients/accounts/budgets fail closed.
9. The cloud is a coordination plane, never an endpoint authority. A complete cloud
   application/database/queue compromise cannot mint an instruction accepted by a
   user's device.
10. Raw procedural observations are device-only. Only sealed derivatives can sync;
    an accepted learned workflow still has zero execution authority.

## Authority v2

Effects are `observe`, `prepare`, `act`, `commit`, and `restricted`.

- Observe and prepare execute silently in Hands-off mode.
- Scoped, reversible acts execute with a non-blocking activity notification.
- A commit executes when the exact result was requested by the user or covered by a
  bounded stored grant. Otherwise Collie asks once at the commit boundary.
- Spending, permanent deletion, account-security changes, legal signatures, and
  permission expansion require a transaction-bound step-up approval or a previously
  signed bounded grant. The approval may come from a trusted phone, control device, or
  hardware Passkey; the person does not have to be beside the execution endpoint.
- Codes available through an already connected and authorized email/SMS inbox may be
  read and filled by the dedicated verification primitive without exposing the code to
  the model or logs. This is not treated as person-required MFA. A challenge tied to a
  personal device, biometric, passkey, hardware key, or human-attestation remains
  `Needs You`, but may be completed on another trusted control device.

Every decision binds action, target, account, recipients, project, connection, amount,
currency, expiry, and idempotency key as applicable. Grant scopes are once, Mission,
workflow, project, and connection. All grants are visible and revocable.

There is no daily-use master operation key. Device trust, recovery, and Authority
policy expansion use a separate root credential held in a Secure Enclave/TPM, hardware
key, or offline recovery material. Normal work uses endpoint keys. High-impact work
uses short-lived, non-replayable authorization tickets that bind the exact operation.
An execution endpoint enforces the decision locally, but no local human click is
required when a valid ticket arrives from an already trusted control device.
An MFA success flag by itself is not authority: the MFA/Passkey ceremony must be bound
to the ticket digest, otherwise a compromised cloud could reuse it for a different
operation. Enterprise policy may require two independent approval keys for selected
effects without changing the normal one-device experience.

## Pack and cloud-compromise boundary

Pack is a cloud-coordinated device and capability network:

`trusted control device → Collie Online coordination plane → execution endpoint`

- Control devices sign one-shot Missions, recurring schedule templates, and scoped
  approval tickets. Each authorization names exactly one execution endpoint; the
  coordinator cannot fan one valid instruction out to every device.
- Online supplies accounts, device/project directories, encrypted sync, presence,
  queues, short leases, and delivery. It opens no inbound endpoint listener and holds
  no key that can grant itself endpoint capabilities.
- Endpoints pull over outbound HTTPS, verify the issuer against a local trust pin,
  compare every signed field, check time/project/capability bounds, consume a local
  replay record, and then apply Authority before execution.
- OAuth login proves account membership but does not create execution trust. A second
  device becomes a task issuer only through an existing trusted device, recovery root,
  or an out-of-band verified public-key ceremony.
- A compromised cloud can withhold, reorder, or delete work and can expose
  `cloud_indexed` content. It cannot alter a signed task, replay an already consumed
  task, expand a mandate, or make a device execute arbitrary code. Availability loss is
  possible; endpoint takeover is not.

Recurring schedules are signed templates. Each occurrence has a deterministic id,
must align to the signed cadence, and is accepted only inside its signed grace and
expiry windows. This prevents a hostile scheduler from turning “daily” into “run now
one thousand times.”

The explicit `cloud_proxy` MCP vault is outside the strongest compromise boundary: its
envelope encryption protects database theft, but a cloud runtime that also obtains the
vault KEK could abuse the connected external SaaS account. It still cannot reach a
private/local endpoint or create endpoint execution authority. `collie online share-mcp`
therefore defaults to `device_direct`: credential ciphertext is bound by AEAD to the
connection id, endpoint, transport, scope, and reviewed manifest digest; approved user
devices decrypt and invoke locally, and Cloud Light cannot use it. `cloud_proxy` remains
an explicit, clearly labeled convenience profile for low-impact connections or providers
offering sender-constrained tokens. Cross-user project-wide device-direct sharing still
requires a project group key rather than the current per-user sealed-sync recovery key.

## Online objects

`User`, `Workspace`, `Membership`, `Project`, `Device`, `Node`, `Capability`,
`Connection`, `CredentialBinding`, `Policy`, `Mission`, `MissionLease`, `Schedule`,
`MemoryItem`, `ProcedureCandidate`, `LearnedWorkflow`, `ActivityEvent`, `JournalEntry`,
`Report`, `Receipt`, and
`AuthorizationGrant` are the stable product nouns.

Data classes are:

- `cloud_indexed`: server-readable metadata/content eligible for search and optional
  Cloud Light processing.
- `sealed`: end-to-end encrypted sync; indexing and reasoning happen on authorized
  devices.
- `device_only`: never uploaded.
- `secret`: stored only in the connection vault or the device credential store and
  never copied to prompts, memory, events, reports, or receipts.

## Identity and connections

The internal `user_id` is stable and independent of Google/GitHub/email addresses.
Accounts are linked explicitly; equal email addresses do not silently merge accounts.
The first web providers are Google, GitHub, and email magic link. CLI/device login uses
a browser handoff, comparison code, per-device key, short access token, and rotating
refresh token.

A connection pins its definition, tool manifest digest, credential reference, effect
manifest, policy, and device bindings. Remote OAuth refresh tokens live in an
envelope-encrypted vault. Devices invoke by connection id through a broker or receive
only short-lived downstream credentials. Local stdio MCP syncs installation metadata
and non-secret configuration; absolute paths, browser cookies, and desktop sessions
remain device overrides.

## Mission scheduling

Mission lifecycle is queued, leased, running, waiting, needs_user, completed, failed,
or canceled. A node claims work by capability. Leases use expiry and fencing tokens;
external commits use idempotency keys. An uncertain result is reconciled, never blindly
retried. If no eligible node is online the default is to wait; Cloud Light and Home Node
are opt-in fallbacks.

## Memory and reporting

Memory is durable fact, Activity is immutable operation history, Journal is editable
narrative, and Report is a generated view. Base daily/weekly reports are deterministic
from events; an LLM may optionally rewrite them. Personal memory never becomes project
memory merely because the user joins or shares a project.

Procedural memory is mined locally from content-free action metadata. Raw observations
never sync. A candidate or accepted routine may sync only as sealed ciphertext, only
between the same user's authorized devices, and is forced to zero authority when
imported.

## Hosted implementation

The hosted reference uses Cloudflare Workers for APIs and callbacks, Durable Objects
for ordered workspace sync and mission leases, D1 for metadata, R2 for encrypted
objects, Queues for background work, and Cron Triggers for schedules. The identity
implementation uses standard OIDC/OAuth with PKCE/device authorization and remains
provider-adaptable.

## Delivery order and exit criteria

1. Authority v2: ordinary browser workflows require zero or one blocking decision;
   explicit Send/Publish does not ask twice; draft never sends; external content cannot
   grant authority; high-impact boundaries remain enforced.
2. Account/device/sync: Local mode works offline; device pairing/revocation and
   cursor-based project/policy/memory/activity sync pass conflict and recovery tests.
3. Connection broker: one OAuth connection works on multiple authorized devices;
   no long-lived token appears client-side; manifest changes re-open review.
4. Scheduling/handoff/reporting: leases prevent duplicate commits, offline work waits
   or uses an explicit fallback, and reports derive from real events and receipts.
5. Team/Cloud Light: role/policy intersections are enforced and cloud model use is
   project-visible, budgeted, and off by default.

The implementation is complete only when these requirements are evidenced by tests and
the user-facing surfaces expose the same truthful state.
