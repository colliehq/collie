# Collie AI Delegate Architecture

Status: proposed

## 1. Product thesis

Collie is a language-driven AI delegate. A user gives it an outcome, authority, and
constraints; Collie plans, acts across digital systems, waits for the world, adapts, and
returns the completed outcome.

Executed verification remains important, but it is a trust primitive inside the product,
not the product itself.

The core product object is therefore a **delegation**, not a chat, session, or model run.

```text
"Find a good flight"                         conversation
"Book the best refundable flight under $500" delegation
```

A delegation survives process restarts, model switches, browser closes, external waiting,
and multiple conversations. It ends only when its outcome is reached, the user cancels it,
or Collie can explain why it cannot continue.

## 2. Product boundaries

Collie should optimize for:

- Natural-language delegation rather than manual workflow construction.
- Long-running work rather than one synchronous chat request.
- Broad digital action through APIs, apps, browsers, local tools, and human handoffs.
- Bounded autonomy: users grant authority once at the right level instead of approving every click.
- Personal continuity: preferences, relationships, prior decisions, and active commitments.
- Legible control: pause, redirect, inspect, approve, revoke, retry, and undo where possible.
- Outcome delivery: artifacts and real-world state changes, not merely an answer.

Collie should not treat the model as an authority system. A model may propose actions, but
deterministic host code decides whether the action is authorized and how credentials are used.

## 3. System shape

Start as a modular monolith with durable boundaries. Do not begin with microservices. The
same modules can later be split between a cloud coordinator and local device workers.

```text
 Web / Desktop / Mobile / Voice / API
                  |
                  v
        Conversation Gateway
                  |
                  v
       Delegation Control Plane
   +--------------+---------------+
   |              |               |
Mandate       Task Graph       Scheduler
Compiler      Orchestrator     + Event Inbox
   |              |               |
   +--------------+---------------+
                  |
                  v
          Agent Runtime Pool
    interpreter / planner / worker
                  |
                  v
            Action Gateway
   +-----------+--------+----------+
   |           |        |          |
 Policy     Secrets  Capability  Idempotency
 Engine      Vault    Registry      Log
   |                    |
   |           +--------+----------+
   |           |        |          |
   |          APIs     MCP     Computer/local
   |           |        |          |
   +-----------+--------+----------+
                  |
                  v
        Evidence + State Observer
                  |
                  v
          Outcome / User Inbox

 Shared data plane:
 event log · delegations · tasks · actions · artifacts · memory · connections
```

The architecture has two hard separations:

1. **Reasoning versus authority.** The model proposes; the Action Gateway authorizes and executes.
2. **Conversation versus work.** Messages can create or steer work, but durable work does not live
   inside a message transcript.

## 4. Core domain model

### Principal

The person or organization for whom Collie acts. A principal owns identities, preferences,
connections, policies, budgets, and delegations.

### Agent

The user's persistent Collie instance. It has a persona and memory namespace, but it does not
own authority. Authority always comes from a principal and a mandate.

### Mandate

A structured, revocable grant of authority compiled from the user's language.

```json
{
  "objective": "Book a refundable flight to Seattle next Friday",
  "success_criteria": [
    "reservation is confirmed",
    "total price is at most 500 USD",
    "ticket is refundable"
  ],
  "allowed_capabilities": ["travel.search", "travel.reserve", "payment.charge"],
  "resource_scope": {"traveler": "self", "payment_method": "visa-ending-2042"},
  "constraints": {"depart_after": "17:00", "stops_max": 1},
  "authority": {"spend_max_usd": 500, "may_submit": true},
  "approval_policy": {"payment.charge": "ask_once"},
  "expires_at": "2026-08-01T00:00:00Z"
}
```

The mandate is visible and editable. The user can narrow or revoke it at any time.

### Delegation

The durable lifecycle of one desired outcome. It references a mandate, conversations,
task graph, current state, result, and receipts.

Delegation states:

```text
draft -> clarifying -> ready -> active
                         |        |
                         |        +-> waiting_external
                         |        +-> waiting_user
                         |        +-> paused
                         |        +-> blocked
                         |
                         +------------> completed
                                      failed
                                      cancelled
```

### Task

A node in a durable DAG. Tasks are small enough to retry or assign independently and explicit
enough that the UI can explain what remains.

Task states:

```text
pending -> ready -> running -> succeeded
                    |   |
                    |   +-> waiting_external
                    |   +-> waiting_user
                    +-----> retryable -> ready
                    +-----> failed / cancelled
```

### Action

One proposed side effect or observation through a capability. Every action records its inputs,
policy decision, execution attempt, outputs, and evidence.

Action states:

```text
proposed -> policy_checked -> authorized -> executing -> observed -> verified
                |                |             |
                v                v             +-> retryable
          awaiting_approval    denied          +-> failed
                                                 +-> compensated
```

### Capability

A typed operation Collie can request. Existing tools become capabilities by adding metadata:

```json
{
  "name": "marketplace.publish_listing",
  "input_schema": {},
  "output_schema": {},
  "effect": "external_write",
  "risk": "high",
  "required_scopes": ["marketplace.listings.write"],
  "idempotent": false,
  "reversible": true,
  "compensation": "marketplace.unpublish_listing",
  "verification": "marketplace.listing_is_live"
}
```

Capabilities may be implemented by REST/OpenAPI, MCP, browser/desktop control, local code,
or a human service. The runtime should not care which transport is behind the contract.

## 5. Application modules

### 5.1 Conversation Gateway

Responsibilities:

- Accept text, image, voice, API, and notification inputs.
- Attach incoming messages to a conversation and optionally to a delegation.
- Route language to one of four intents: answer, create delegation, steer delegation, or decide approval.
- Stream human-readable progress without making the stream the source of truth.

The current Web UI and SSE protocol can remain as the first surface, but should consume events
from the control plane instead of running `Harness.run()` directly inside the HTTP request.

### 5.2 Mandate Compiler

Converts the user's request into a structured mandate and identifies missing decisions.

It should ask only questions that materially change authority or outcome. It may infer harmless
preferences from memory, but never infer permission to spend, publish, message, delete, disclose,
or impersonate.

Output:

- Objective and measurable success criteria.
- Constraints, budget, deadline, and preferred tradeoffs.
- Required capabilities and accounts.
- Proposed approval policy.
- Ambiguities and blockers.

### 5.3 Task Graph Orchestrator

Owns durable work state. It is not an LLM loop.

Responsibilities:

- Create and revise a task DAG.
- Mark dependencies and runnable tasks.
- Lease ready tasks to workers.
- Persist checkpoints before and after every worker step.
- Wake tasks on time, webhook, message, approval, or connection change.
- Enforce delegation-level cost, time, attempt, and concurrency budgets.
- Detect stuck work and request help with a specific question.

The planner may use an LLM to propose graph changes, but host code validates every transition.

### 5.4 Agent Runtime

The current `Harness` becomes a stateless-ish task executor beneath the orchestrator.

A worker receives:

- One task and its acceptance criteria.
- A scoped context packet.
- A bounded capability set.
- A token/time/cost budget.
- The current real-world observations.

It returns proposed actions, new observations, artifacts, task notes, or a request for user input.
It does not directly mark the delegation complete.

Use one coordinator and bounded specialist workers rather than an unconstrained swarm. Parallelism
is useful for independent research or monitoring; authority and final state remain centralized.

### 5.5 Action Gateway

This is the most important execution boundary.

For every proposed action it:

1. Resolves the capability contract.
2. Canonicalizes and validates arguments.
3. Evaluates policy against principal, mandate, current state, and risk.
4. Requests approval if required.
5. Adds an idempotency key and records an immutable proposal event.
6. Resolves credentials without exposing secrets to the model.
7. Executes through the selected adapter.
8. Records result, external identifiers, and artifacts.
9. Runs the capability's postcondition verifier.
10. Schedules retry, compensation, or escalation when necessary.

No side-effecting adapter should be callable outside this gateway.

### 5.6 Policy and Approval Engine

Policy decisions are deterministic and explainable: `allow`, `ask`, or `deny`.

Suggested risk levels:

| Level | Examples | Default |
|---|---|---|
| R0 observe | read page, search, inspect file | allow |
| R1 reversible private | draft, organize, save locally | allow |
| R2 external communication | send message, publish, invite | ask unless mandate pre-authorizes |
| R3 financial/legal/security | pay, sign, change access, disclose sensitive data | explicit scoped authority + step-up |
| R4 prohibited | unauthorized access, deception, bypassing controls | deny |

Approval should be attached to a semantic action, not a click. For example, approve “publish this
listing with this price and audience,” not every browser interaction needed to accomplish it.

A user can pre-authorize repeated actions with bounds:

```text
May answer messages from existing buyers for seven days.
May not lower price below $420.
Must ask before accepting an offer or sharing an address.
```

### 5.7 Connection and Secret Service

A connection represents one external identity and its granted scopes. It stores health, expiry,
last verification, and a reference to credentials in a vault.

The model receives a capability summary such as “Gmail: connected, can draft and send,” never the
OAuth token. Refresh, revocation, reauthentication, and scope upgrades are host responsibilities.

### 5.8 Memory and Personal Context

Replace one undifferentiated fact store with typed memory namespaces:

| Type | Example | Lifetime |
|---|---|---|
| identity | legal name, own accounts | durable, user controlled |
| preference | aisle seat, concise emails | durable, revisable |
| policy | never publish without preview | durable, authoritative |
| relationship | Alex is the landlord | durable, provenance required |
| episodic | what happened during a delegation | timeline / searchable |
| semantic | learned factual information | confidence + source + expiry |
| procedural | how to complete a recurring workflow | versioned skill |
| working | current task observations | delegation-scoped, disposable |

Every memory record needs provenance, confidence, sensitivity, validity interval, and revocation.
Memory may inform decisions; it may not silently broaden authority.

### 5.9 Evidence, Artifacts, and Receipts

Evidence is the observed state used to judge an outcome. Artifacts are deliverables such as files,
drafts, confirmations, screenshots, reservations, or external record IDs.

A receipt should answer:

- What did Collie do?
- Under which mandate and approval?
- Which accounts and capabilities were used?
- What changed externally?
- How was success checked?
- What remains uncertain or reversible?

Verification lives here as one component of trustworthy delegation.

## 6. Durable data model

Use SQLite for the first local single-user version, with migrations and foreign keys enabled.
Keep the schema compatible with a later move to Postgres.

Core tables:

```text
principals
  id, kind, display_name, created_at

agents
  id, principal_id, name, persona, status

conversations
  id, principal_id, delegation_id?, surface, created_at, updated_at

messages
  id, conversation_id, role, content_json, created_at

mandates
  id, principal_id, version, objective, success_json, scope_json,
  constraints_json, authority_json, approval_policy_json, expires_at, revoked_at

delegations
  id, principal_id, agent_id, mandate_id, state, summary, result_json,
  created_at, started_at, finished_at, version

tasks
  id, delegation_id, parent_id?, kind, title, state, input_json,
  acceptance_json, priority, attempts, not_before, lease_owner?, lease_expires_at?

task_dependencies
  task_id, depends_on_task_id

actions
  id, delegation_id, task_id, capability_id, state, args_redacted_json,
  risk, idempotency_key, external_id?, result_json, error_json, created_at, finished_at

policy_decisions
  id, action_id, decision, reason_code, explanation, policy_version, created_at

approvals
  id, action_id, requested_from, state, summary, scope_json, expires_at,
  decided_at, decision_proof

capabilities
  id, name, version, schemas_json, effect, risk, required_scopes_json,
  idempotent, reversible, verifier_name?, compensation_name?

connections
  id, principal_id, provider, external_account_id, scopes_json,
  secret_ref, health, expires_at, last_checked_at

artifacts
  id, delegation_id, task_id?, action_id?, kind, uri, sha256,
  metadata_json, created_at

memories
  id, principal_id, namespace, type, content, source_event_id?, confidence,
  sensitivity, valid_from, valid_until?, superseded_by?, created_at

events
  seq, event_id, aggregate_type, aggregate_id, type, payload_json,
  actor_type, actor_id?, causation_id?, correlation_id?, created_at

triggers
  id, delegation_id?, kind, spec_json, next_fire_at?, enabled
```

The append-only `events` table is the recovery and audit backbone. Other tables are query-friendly
projections and current state. Every state transition and external side effect writes an event in
the same transaction as its projection update.

## 7. End-to-end delegation flow

```text
User: "Handle renewing my car registration this month. Ask before paying."

1. Conversation Gateway stores the message.
2. Mandate Compiler proposes objective, deadline, accounts, and payment approval rule.
3. User accepts or edits the mandate.
4. Orchestrator creates tasks: gather documents -> check eligibility -> prepare renewal -> pay.
5. Worker gathers data with read-only capabilities.
6. Action Gateway prepares the renewal submission under the mandate.
7. Payment action evaluates to ASK and appears in the user's Inbox with amount and summary.
8. User approves once with step-up authentication.
9. Gateway submits payment using vault-resolved credentials and an idempotency key.
10. Observer confirms the external renewal record and captures a receipt artifact.
11. Orchestrator marks success criteria complete and closes the delegation.
12. Collie reports the outcome, receipt, and any remaining follow-up.
```

If the process waits three days for an external response, no model process remains alive. A durable
trigger wakes the delegation when a webhook, email, timer, or user message arrives.

## 8. Deployment architecture

### Phase A: local-first

```text
Collie UI <-> collied local daemon <-> local SQLite / encrypted vault
                                |-> model providers
                                |-> API/MCP connectors
                                |-> local/browser/desktop adapters
```

The daemon is long-lived and owns the scheduler, worker leases, event log, connections, and local
capabilities. The UI can close without stopping delegations.

### Phase B: hybrid always-on

```text
Collie Cloud Coordinator <-> encrypted sync / command channel <-> Device Runners
          |                                               |
          |-> cloud-safe connectors                       |-> local apps and private data
          |-> scheduler and notifications                 |-> device-held credentials
```

The cloud coordinator keeps tasks alive and reaches the user. Device runners perform actions that
need local state. Credentials should stay on the execution side whenever possible. The capability
contract lets the orchestrator choose an eligible runner without changing agent reasoning.

## 9. Mapping from the current codebase

Preserve the parts that already work; move them behind the new domain model.

| Current component | New role |
|---|---|
| `loop.Harness` | bounded worker runtime for one task |
| `providers.py` / `codex_oauth.py` | model gateway |
| `tools.ToolRegistry` | seed of the capability registry |
| direct `tool.run()` dispatch | replaced by Action Gateway dispatch |
| `sessions.py` JSON files | conversation/message projection; migrate to app DB |
| `plantool.py` | replaced by durable task DAG |
| `recorder.py` runs/turns | telemetry projection fed from the event log |
| `memory.py` | retrieval engine beneath typed personal memory |
| `delegate.py` | worker-spawn mechanism controlled by orchestrator budgets |
| `checkpoint.py` | compensation/undo support for reversible local actions |
| `pack.py` | optional execution strategy for high-value tasks |
| `webapp.py` | API/BFF and event stream; no synchronous ownership of a run |
| Web UI thread list | delegation list + conversation views |

Compatibility rule: existing `collie run`, `collie web`, and benchmark paths should continue to
work while the application layer is introduced. The coding harness can become one capability pack
of the broader delegate rather than being deleted.

## 10. Recommended implementation sequence

### Milestone 0: establish durable seams

- Introduce an app database and migration runner.
- Add domain IDs and an append-only event writer.
- Define dataclasses/enums for mandate, delegation, task, action, approval, and capability.
- Wrap existing tools with capability metadata without changing behavior.
- Add a compatibility adapter that runs a legacy `Harness` task through the new interfaces.

Exit criterion: existing tests stay green and one current chat run produces a delegation/action
timeline in the new store.

### Milestone 1: delegation MVP

- Create, clarify, accept, pause, steer, cancel, and resume delegations.
- Implement durable task DAG, worker leases, retries, and restart recovery.
- Put all existing tool calls through the Action Gateway.
- Implement R0-R4 policy decisions and an approval inbox.
- Replace the thread-first home screen with active delegations and their timelines.

Exit criterion: a multi-step delegation survives a daemon restart, pauses for one approval, resumes,
and returns an artifact and receipt.

### Milestone 2: personal context and connections

- Add typed memories with provenance, expiry, sensitivity, and user editing.
- Add account connections, scope health, reauthentication, and encrypted secret references.
- Add a capability catalog and connection-aware planning.
- Add timers, webhooks, email/event ingestion, and user notifications.

Exit criterion: Collie completes a delegation that waits on an external event and resumes without
the user repeating context.

### Milestone 3: dependable autonomy

- Add bounded parallel workers and per-delegation budgets.
- Add idempotency and compensation for every side-effecting first-party capability.
- Add capability-specific observers and postcondition verifiers.
- Add recurring delegations and policy templates.
- Add failure recovery, stuck-work detection, and precise escalations.

Exit criterion: repeated workflows run with low intervention and no duplicated side effects.

### Milestone 4: hybrid and ecosystem

- Add a cloud coordinator and device-runner protocol.
- Add multi-device routing and end-to-end encrypted synchronization where practical.
- Publish a capability SDK for API, MCP, local, and human-backed adapters.
- Add reviewed procedural skills and organization/team principals.

Exit criterion: an always-on delegation can coordinate cloud and device capabilities while the user
retains one policy, timeline, and revocation surface.

## 11. First product slice

Do not validate the architecture with a toy chat or a single browser click. Choose three workflows
that exercise different forms of delegation:

1. **Research and decision:** gather options, apply personal preferences, produce a recommendation.
2. **External coordination:** draft/send bounded messages, wait for replies, negotiate within limits.
3. **Transactional workflow:** prepare a real form/order/listing, request one semantic approval,
   submit it, and capture the result.

These test memory, long-running state, authority, external effects, and recovery without requiring
hundreds of integrations.

## 12. Product metrics

Primary:

- Completed delegated outcomes per active user per week.
- User time saved and time under active delegation.
- Percentage completed without manual takeover.
- Repeat use of the same delegation pattern.

Trust and reliability:

- Unauthorized side effects: target zero.
- Duplicate side effects: target zero.
- Approval precision: approvals should be meaningful, not click fatigue.
- Recovery rate after transient failure or restart.
- Percentage of completed delegations with sufficient evidence.
- Rate of user correction to memory, mandate, and action summaries.

Model benchmarks remain useful for the worker runtime, but they are no longer the product's north
star. The product wins when users safely hand Collie more of their real work over time.

## 13. Architectural rules

1. A conversation is never the source of truth for work state.
2. The model never grants itself permission.
3. The model never receives raw long-lived credentials.
4. Every side effect passes through one Action Gateway.
5. Every external write has an idempotency strategy or an explicit duplicate-risk warning.
6. Authority is explicit, bounded, revocable, and time-limited.
7. Memory can guide action but cannot expand authority.
8. Waiting is durable state, not a sleeping process or open SSE connection.
9. Completion belongs to the orchestrator's success criteria, not the model's prose.
10. The user can always inspect, pause, redirect, and revoke active work.

## 14. Proposed code layout and API

Keep the current coding harness intact while adding an application package around it:

```text
harness/
  loop.py                    existing bounded model/tool loop
  providers.py               existing model gateway
  tools.py                   existing tool implementations
  app/
    domain.py                enums and immutable domain records
    db.py                    migrations, transactions, repositories
    events.py                append-only event writer and event subscriptions
    mandates.py              mandate compiler and versioning
    orchestrator.py          task DAG transitions, leases, wakeups, budgets
    workers.py               worker runtime and legacy Harness adapter
    capabilities.py          contracts, registry, adapter selection
    actions.py               Action Gateway and execution lifecycle
    policy.py                allow / ask / deny evaluation
    approvals.py             inbox and approval resolution
    connections.py           account health, scopes, secret references
    memory_service.py        typed memory and context packet assembly
    artifacts.py             evidence, blobs, hashes, receipts
    scheduler.py             timers, webhooks, wakeup queue
    api.py                   HTTP application API and event streaming
```

When introducing the subpackage, change packaging from the fixed `packages = ["harness"]` list to
package discovery so `harness.app` ships in wheels.

Minimum application API:

```text
POST   /api/delegations                 create from language
GET    /api/delegations                 list active/recent
GET    /api/delegations/{id}            current projection
GET    /api/delegations/{id}/events     timeline, cursor-based
POST   /api/delegations/{id}/steer      add instruction or constraint
POST   /api/delegations/{id}/pause
POST   /api/delegations/{id}/resume
POST   /api/delegations/{id}/cancel

GET    /api/inbox                       approvals, questions, connection failures
POST   /api/approvals/{id}/decide       approve or deny

GET    /api/capabilities                available actions and health
GET    /api/connections
POST   /api/connections/{provider}/start
POST   /api/connections/{id}/revoke

GET    /api/memory
PATCH  /api/memory/{id}
DELETE /api/memory/{id}

GET    /api/events?after={seq}          resumable SSE stream for all projections
```

Commands should write state and return quickly. Workers advance the state asynchronously. The UI
subscribes with an event cursor and can reconnect without inventing a synthetic “stream interrupted”
failure when the underlying delegation is still running.

## 15. Application UX

The initial application can keep natural-language chat as its command surface, but the information
architecture should become work-first:

```text
Left navigation
  Today             active and recently completed delegations
  Inbox             approvals, questions, errors, expired connections
  Delegations       all work with filters and saved recurring patterns
  Connections       accounts, scopes, health, reauthentication
  Memory            what Collie knows and which policies it follows

Main delegation view
  Objective + current status + pause/redirect/cancel
  Mandate summary   authority, constraints, budget, deadline
  Live timeline     decisions, actions, waiting, retries, external events
  Work area         current question, action preview, or generated artifact
  Outcome           result, receipt, evidence, follow-up
```

The composer remains available on every page. A new message either creates a delegation, steers the
current one, answers an outstanding question, or remains a normal conversation. Collie should show
which interpretation it chose and make that reversible.

The UI should avoid exposing raw tool traces as the primary story. Users need semantic progress:
“Compared 14 options,” “Waiting for Alex,” “Ready to publish for $450,” and “Payment needs approval.”
Raw calls, model messages, screenshots, and request IDs belong in expandable technical details.

## 16. First implementation PR sequence

1. Add `harness.app.domain`, SQLite migrations, and repository tests.
2. Add the transactional event writer and delegation/task projections.
3. Wrap one existing read tool and one write tool as typed capabilities.
4. Add the Action Gateway with idempotency, policy decisions, and redacted event payloads.
5. Add the legacy Harness worker adapter and persist its action timeline.
6. Add worker leases, restart recovery, and a daemon-owned scheduler loop.
7. Add delegation and resumable-event APIs while preserving existing `/api/stream`.
8. Add the Inbox and one full approval/resume path.
9. Change the Web UI home from threads to delegation cards and a semantic timeline.
10. Migrate existing sessions into conversations and keep a compatibility read path.

The first vertical capability should be selected only after PR 4. Before that boundary exists,
adding more integrations merely creates more places where the model can perform ungoverned side
effects.
