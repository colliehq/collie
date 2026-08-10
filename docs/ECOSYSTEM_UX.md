# Collie Ecosystem Product and UX Contract

Status: product direction, not a claim that every described capability ships today.

This document defines how Collie should evolve from a coding-agent interface into a coherent,
desktop-first AI system. It is a product and UX contract: implementation may arrive in stages, but
the nouns, navigation, trust language, and user mental model should remain stable.

## Positioning

### Product promise

> **Collie is the local AI operations system for your devices. Give it an outcome; Collie chooses
> the brain, works across your code, browser, and apps, keeps the mission moving, asks when
> authority matters, and returns evidence.**

Chinese:

> **Collie 是运行在你设备上的个人 AI 执行系统。你只说结果，它自动选择模型、工具和工作方式，
> 持续把事情做完，在需要授权时找你，并交回可核验的证据。**

Brand shorthand:

> **One Collie at the front door. A Pack underneath.**

The user should experience one durable relationship. Collie may route work to multiple models,
workers, devices, and services behind that relationship.

### Product, platform, and surface are different things

- **Product:** a personal AI operations system for delegating real outcomes.
- **Platform strategy:** an open ecosystem of brains, skills, connections, devices, and workers.
- **Primary surface:** the desktop is Collie's home and control plane.
- **Runtime:** a supervised local service owns durable work, policy, evidence, and recovery.
- **Other entrances:** CLI, IDE, browser, phone, and messaging clients enter the same runtime.

“AI ecosystem” describes the platform strategy. It is not a sufficient customer promise by itself.
People adopt Collie to finish work with less supervision and more accountability, not to acquire an
ecosystem.

### Positioning guardrails

Do not lead with claims that are broad, generic, or already category-standard:

- “Do anything with AI.” This is a north star, not a falsifiable product promise.
- “Your AI command center.” It describes a layout, not a differentiated outcome.
- “Local-first,” “desktop-first,” “multi-model,” “memory,” “skills,” “subagents,” or “automation” as
  standalone differentiation.
- “Always on” or “24/7” without naming the execution node and its availability. A powered-off local
  device cannot run work.
- “Verified,” “proved,” or “correct” without naming exactly what evidence passed and what remained
  outside the verification scope.
- A cute companion as the primary value. The Collie identity must carry operational meaning:
  device, capabilities, memory, authority, and status.

The defensible combination is **accountable delegation**: model-independent routing, access to the
user's real environment, durable missions, explicit authority boundaries, and scoped evidence.

## The eight product objects

These are the durable nouns shown across every surface. Internal implementation names must not leak
into the primary UX when one of these objects can explain the same concept.

| Object | User meaning | Product rule |
| --- | --- | --- |
| **Collie** | The persistent delegate the user talks to | One stable identity and relationship; not a model or chat session |
| **Mission** | An outcome Collie owns until it reaches a terminal state | Survives chat turns, waits, restarts, and surface changes |
| **Pack** | The workers and devices available to help | Collie routes work automatically; users may inspect or override |
| **Brain** | A model or provider used for reasoning | Replaceable execution resource, not the product identity |
| **Skill** | A reusable method for doing a kind of work | Defines procedure and supporting resources, not authority |
| **Connection** | An app, MCP server, browser, account, or data source | Declares capabilities and authentication separately |
| **Leash** | The authority, budget, scope, and approval boundary | Enforced outside model reasoning and narrowable at every level |
| **Receipt** | The durable record of actions, evidence, cost, and residual risk | States facts and scope; never upgrades uncertainty into success |

### Relationships

```text
User -> Collie -> Mission -> Pack -> Brain / Skill / Connection
                         \-> Leash applies to every action
                         \-> Receipt records actions and evidence
```

A chat is an interaction channel. It is not a ninth domain object. A conversation may create,
inspect, steer, or resume a Mission, but closing a chat must not erase Mission ownership.

## Information architecture

The primary navigation has five destinations. **Needs You** is a global priority queue that can be
opened from any destination; it is not buried inside a Mission or Settings.

### Home

Home answers: **What can I hand off, what is happening, and what needs me?**

Required elements:

- A single outcome-first composer: “What should Collie handle?”
- Visible context chips for the inferred project, device, and relevant connections.
- A compact, editable **Run Plan** generated after submission, not a configuration form before it.
- Needs You at the top whenever it is non-empty.
- Active Missions with status, next step, execution location, elapsed time, and budget.
- Recent outcomes with their evidence grade and unread state.
- Suggested recurring work only after a workflow has succeeded manually.

Home must not open with provider, temperature, reasoning effort, verification mode, workspace mode,
or worker-count selectors. Those belong in the generated Run Plan and advanced overrides.

### Missions

Missions unifies work that would otherwise be split into chats, goals, jobs, loops, and automations.
A Mission may be one-shot, long-running, event-driven, or scheduled without becoming a different
top-level product object.

Recommended filters:

- Active
- Waiting
- Needs You
- Review required
- Completed
- All

Mission detail is an outcome timeline, not a wall of chat:

```text
Outcome -> Plan -> Assignment -> Actions -> Waits / Decisions -> Evidence -> Receipt
```

Chat, files, diffs, worker transcripts, and raw logs are inspectable secondary panels. The main
view always shows the current outcome, next state transition, authority boundary, and evidence.

Use one lifecycle vocabulary across all surfaces:

- Draft
- Queued
- Running
- Waiting
- Needs You
- Paused
- Review required
- Verified against contract
- Completed without independent verification
- Blocked
- Recovery required
- Cancelled

Do not collapse `Review required`, `Completed without independent verification`, or `Recovery
required` into a green completed state.

### Pack

Pack answers: **Who and what can work for me right now?**

Show:

- The primary Collie identity.
- Local and remote devices, online state, capabilities, and last heartbeat.
- Specialist workers and their intended roles.
- Current assignments, queue depth, and resource conflicts.
- Available Brains and the active routing policy.
- Per-device permission and execution boundaries.

Pack is operational, not decorative. A device-specific Collie may have distinct browser sessions,
files, apps, memory scope, and Leash. The interface must not imply that an offline device can accept
immediate work.

### Library

Library answers: **What can Collie learn or connect to?**

Use four sections:

- **Skills:** reusable procedures.
- **Connections:** apps, accounts, MCP servers, browsers, and data sources.
- **Templates:** reviewed Mission recipes with explicit inputs, outputs, and verification contracts.
- **Discover:** installable ecosystem packages only when provenance and permissions can be shown.

Installation must show capability requests before activation. “Installed” does not mean “trusted,”
and a Skill must never silently widen a Leash or grant itself a Connection.

### Activity

Activity answers: **What happened, what did it cost, and what evidence exists?**

It contains:

- Receipts and evidence.
- Action and approval history.
- Mission and worker events.
- Cost, token, duration, and device usage.
- Security and policy decisions.
- Search, export, and filters by Mission, Collie, device, Connection, and time.

Activity is the canonical audit surface. Decorative “thinking” animation and model prose are not
evidence unless they point to a recorded event or artifact.

### Needs You

Needs You is a cross-Mission inbox for decisions only the user can make:

- Exact action approval.
- Missing information.
- CAPTCHA, MFA, or foreground handoff.
- Budget or authority expansion.
- Ambiguous completion review.
- Recovery reconciliation after uncertain side effects.

Every item must show the requesting Mission, exact payload or question, consequence, expiration,
and available safe alternatives. Approvals are payload-bound; broad “always allow this tool” UX is
not acceptable for consequential actions.

## Run Plan and intelligent defaults

Collie should choose the execution recipe from the outcome, risk, available evidence, connected
capabilities, cost policy, and prior results. The default interaction is:

1. The user describes an outcome.
2. Collie presents a compact Run Plan.
3. Work starts immediately if the plan fits the existing Leash.
4. The user can expand and override details without learning Harness internals.

The Run Plan may reveal:

- Intent and deliverable.
- Selected device and workspace isolation.
- Brain and reasoning class.
- Single worker or Pack delegation.
- Connections and Skills expected to be used.
- Verification contract and evidence target.
- Budget, deadline, and approval boundaries.

User-facing presets should express preferences, not mechanisms: **Fast**, **Balanced**, **Deep**,
**Local only**, or **Budget capped**. Raw model parameters, embedder configuration, retry timing,
and environment variables belong under Advanced.

## Desktop control plane and runtime data plane

The desktop application is Collie's home, but it must not own durable execution state in its view
process.

### Desktop control plane

Responsible for:

- Creating, steering, pausing, resuming, and cancelling Missions.
- Showing Needs You and collecting exact approvals.
- Inspecting Pack state, evidence, Receipts, memory, and Connections.
- Configuring user intent: autonomy, privacy, routing, budgets, and notifications.
- Providing ambient status and a global summon affordance.

### Runtime data plane

A supervised runtime service is responsible for:

- Durable Mission state and task graphs.
- Scheduling, leases, retries, waits, and crash recovery.
- Worker and device assignment.
- Capability execution and credential isolation.
- Leash enforcement and cumulative budgets.
- Evidence collection, verification state, Receipts, and event ledgers.
- Notifications and Needs You delivery.

```text
Desktop / CLI / IDE / Phone / Browser / Messaging
                         |
                  Control API
                         |
              Supervised Collie runtime
          / orchestration / trust / evidence \
         Devices       Connections        Stores
```

Closing a window must not silently cancel a Mission. Conversely, a local Mission must clearly say
that it cannot advance while its execution device is asleep, powered off, disconnected, or missing
required foreground access.

The current living wallpaper should become an optional **Ambient mode**. Its product duties are
global summon, Running / Needs You / Ready / Blocked status, notifications, and quick handoff. It
should not compete with the operating system as a general music, weather, or app-launcher shell.

## Initial user wedge

The first customer is not “everyone who uses AI.”

Start with builders, independent developers, technical operators, and small teams whose work crosses
code, a logged-in browser, desktop apps, and external services. Their recurring problem is not text
generation; it is supervising a fragile end-to-end workflow and determining whether it actually
finished.

Initial promise:

> **Hand Collie an end-to-end technical outcome. It can work across the real environment, keep the
> Mission alive, stop at genuine authority boundaries, and return a scoped Receipt.**

Good wedge Missions include:

- Reproduce a UI bug, change the code, rerun the flow, and present evidence.
- Monitor a pull request, address review feedback, and stop when trusted checks pass.
- Investigate an incident across logs, code, dashboards, and a browser session.
- Prepare a release across repository, build, browser, and distribution steps with explicit approval
  before irreversible publication.

Breadth should grow from reliable adjacent workflows. It must not precede reliability.

## Ecosystem extension contract

Collie may call itself an open platform before it has a marketplace. It should call itself a mature
ecosystem only after third parties can extend it safely and predictably.

Every installable package must have a stable manifest covering:

- Identity, version, publisher, provenance, and content digest.
- Compatible Collie/runtime and platform versions.
- Included Skills, Connections, tools, hooks, templates, assets, and workers.
- Capability and network scopes.
- Filesystem, desktop, browser, credential, and external-action requirements.
- Secret references without embedded secret values.
- Lifecycle hooks and whether each can block a transition.
- Verification contracts and evidence types the package can produce.
- Data retention, export, and uninstall behavior.

The ecosystem runtime must provide:

- Reviewable install and update diffs.
- Signatures or equivalent provenance verification.
- Permission review before activation and after scope changes.
- Isolation appropriate to capability risk.
- Install, enable, disable, update, rollback, and uninstall.
- Compatibility checks and deterministic package digests.
- A trust state separate from install state.
- Audit events for package actions and policy decisions.
- Publisher review and a vulnerability/revocation path for public distribution.

Extensions may add capability but may not:

- Widen their own Leash.
- Reclassify their own risk.
- Edit or replace a trusted verifier without invalidating its evidence.
- Read unrelated credentials or memory by default.
- Claim global success from a package-specific check.

## UI copy contract

### Lead with outcomes

Prefer:

- “What should Collie handle?”
- “Waiting for the deployment to finish.”
- “Needs your approval to publish this exact release.”
- “Completed; browser confirmation still needs review.”

Avoid:

- “Start a completion.”
- “Configure an agent loop.”
- “Max turns reached” without explaining the user-visible consequence.
- “The model thinks it is done.”

### Verification language is scoped

`Verified` is never a synonym for “correct in every way.” The UI must name the contract, evidence,
freshness, and residual uncertainty.

Preferred forms:

- “Targeted test passed after the last edit.”
- “Verified against `pytest tests/auth -q` at 14:32.”
- “Browser flow reached the expected success state in the signed-in test account.”
- “Build and regression checks passed. Production load was not tested.”
- “Check passed” when the check is not strong enough to support a broader claim.
- “Inconclusive” when evidence is absent, stale, mutable, or outside the trusted channel.

Forbidden forms unless the named claim is actually established:

- “Proved correct.”
- “Fully verified.”
- “No bugs.”
- “Safe to ship.”
- “Done ✓” based only on model self-report, a successful command exit, or an irrelevant assertion.

Every Receipt should state:

- What outcome was requested.
- What changed or occurred.
- Which evidence was collected and when.
- Which verifier or observer produced it.
- Whether the verifier was independent and protected from mutation.
- What was not checked.
- Cost, duration, approvals, and remaining risk.

### Authority language is exact

Say what will happen, where, and under which identity:

- “Allow Collie to click **Publish v1.4.0** on `example.com` as `sining@example.com`?”

Do not say:

- “Allow browser access?” when the decision is actually an irreversible publication.

### Personality must not inflate trust

Collie may be warm, recognizable, and alive. Consequential actions, evidence, failures, budgets,
and recovery states use direct language and stable visual semantics. Cute language must never soften
or obscure risk.

## Settings architecture

Settings should be organized around user intent:

1. **My Collie:** identity, voice, personality, and appearance.
2. **Autonomy & approvals:** default Leash, unattended behavior, escalation, and budgets.
3. **Brains & routing:** connected providers and Fast / Balanced / Deep / Local-only policy.
4. **Capabilities & connections:** files, browser, desktop, apps, MCP, and accounts.
5. **Memory:** sources, write policy, review, correct, forget, import, and export.
6. **Devices & remote:** Pack membership, availability, pairing, and execution location.
7. **Privacy & security:** data boundaries, secret handling, audit, and retention.
8. **Notifications & appearance:** Needs You routes, ambient mode, language, and accessibility.
9. **Advanced / developer:** raw models, effort, sampling, retrieval, retries, environment, and logs.

Changes that widen authority require explicit confirmation. Independent settings should apply
independently; a monolithic Save action must not imply an all-or-nothing security transaction.

## Phased roadmap

### Phase 0 — Truthful foundation

- Adopt the eight objects and shared lifecycle vocabulary in docs and UI copy.
- Replace blanket verification claims with scoped evidence language.
- Make current versus planned capability explicit.
- Establish one status and Receipt schema across existing surfaces.

Exit condition: two surfaces cannot describe the same run with conflicting state or evidence.

### Phase 1 — Mission-first desktop

- Ship Home, Missions, Activity, and global Needs You as the primary desktop shell.
- Replace pre-run option matrices with an inspectable Run Plan and intelligent defaults.
- Reorganize Settings around user intent; move engine knobs to Advanced.
- Make the desktop a client of durable runtime state rather than the owner of that state.
- Reduce the living desktop to optional Ambient mode.

Exit condition: a user can create, leave, return to, steer, approve, and audit one Mission without
understanding provider or Harness terminology.

### Phase 2 — Operational Pack

- Expose devices and specialists as a real Pack with health and capability state.
- Route by task, risk, device availability, cost, and evidence requirements.
- Add isolated workspaces, resource ownership, steering, cancellation, and recovery UX.
- Unify one-shot, long-running, and scheduled work under Missions.

Exit condition: parallel work is faster or more reliable on measured workloads without hiding cost,
conflicts, or incomplete results.

### Phase 3 — Trusted extension platform

- Stabilize and document the extension manifest and capability contract.
- Implement provenance, permission diffs, trust state, lifecycle management, and compatibility tests.
- Turn Skills, Connections, and Mission Templates into a coherent Library.
- Provide an SDK, examples, validation tooling, and private/team distribution.

Exit condition: a third party can build, validate, install, update, and remove an extension without
editing Collie core or receiving ambient authority.

### Phase 4 — Ecosystem and optional always-on execution

- Add reviewed public discovery and distribution.
- Support user-owned always-on nodes and, if strategically justified, an optional cloud executor.
- Add secure cross-device handoff and Pack-wide policy.
- Introduce team governance only after the single-user trust model is measured and stable.

Exit condition: “ecosystem” and “always on” describe observable supply, usage, and availability—not
aspirational marketing.

## Success criteria

The redesign succeeds when:

- Users begin with an outcome instead of configuring a run.
- Needs You items are answered faster and never lose their originating context.
- Users can explain Collie, Mission, Pack, Leash, and Receipt without internal terminology.
- More Missions finish without supervision while false-completion rates fall.
- Evidence scope and residual risk are visible at the decision point.
- Provider, worker, and device routing improves completion time or cost without reducing quality.
- The desktop can close and reopen without losing durable work.
- New ecosystem capability does not silently widen authority.
