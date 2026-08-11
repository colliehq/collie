# Changelog

## v0.21.22 — Keep Missions moving across authorization waits

- **Missing authorization is branch-scoped.** Collie records a structured Needs You request and
  continues independent Mission work; it pauses the whole Mission only when every remaining path
  depends on that request.
- **Confirmed profile facts are reusable without becoming blanket consent.** A local age threshold
  can satisfy an exact low/medium-risk form claim, while CAPTCHA, person-required MFA, KYC,
  biometrics, legal signatures, security keys, and spending stay person-required. Verification
  codes from an authorized Google Voice/mail connection remain ordinary connected work and are
  never persisted in Mission history.
- **Google Voice can be Collie's assigned work line.** The Connection grants operational use of
  messages, calls, voicemail, verification codes, and routine Voice settings while storing only a
  masked number. Number transfer/release, purchases, and Google-account security remain separate
  authority. Codes still move through a dedicated transient read-and-fill path without exposure
  to the model, Mission state, audit log, or Receipt.
- **Mission cards now say what is happening.** Current work, verified steps, next action, blockers,
  and waiting authorizations are summarized before bounded details and receipts.
- **Takeover is no longer ambiguous.** The terminal button is labeled **End mission & take over**
  and requires confirmation. **Return to Collie** creates an audited successor that inherits
  completed semantic action keys, preventing already-fired work from replaying.

## v0.21.21 — Keep OAuth consent active and verify delayed redirects

- **A final browser action activates its already-bound Mission tab before snapshotting.** Consent
  pages that intentionally disable approval in background tabs can now become actionable without
  weakening exact-button or TOCTOU binding.
- **Composite no-write inspection instructions remain read-only.** Goals such as “without
  navigating, reloading, opening, clicking, typing, or submitting” no longer misclassify semantic
  page expectations as form fields when a planner omits the optional `read_only` flag.
- **A fired click is re-observed for a bounded window, never repeated.** OAuth redirects and SPA
  success states that land shortly after the trusted click can verify normally, while unresolved
  actions remain inconclusive. Persisted targets now strip query strings and OAuth state, using an
  opaque digest for exact URL binding instead.

## v0.21.20 — Lock read-only OAuth pages and bind delayed consent controls

- **A read-only CURRENT-page task cannot silently guess another URL on the same host.** When the
  goal explicitly forbids navigation, reload, or opening another page, Collie binds the step to the
  exact starting URL and fails closed on any drift without persisting OAuth query credentials.
- **Short consent-button safety delays no longer cause needless replanning.** Final-action snapshot
  preparation briefly rereads one exact unique disabled target and binds it only after it becomes
  enabled; missing or ambiguous controls still fail immediately and no click is attempted.

## v0.21.19 — Recover stale reversible action latches

- **A timed-out reversible child cannot permanently prevent Mission retry.** A failed Mission with
  no live run or resource lease may retire an old research/compose/browse/observe/code execution
  latch after its watchdog window and create a durable inconclusive receipt.
- **Consequential uncertainty is never auto-cleared.** Publish, send, commerce, destructive, and
  other irreversible execution latches still block retry until explicitly inspected and reconciled.

## v0.21.18 — Trusted exact-ref final clicks

- **Sites that reject synthetic events can now accept gated final actions.** `browse.submit` uses a
  genuine CDP click while retaining the exact accessibility ref captured by the outer Gate.
- **A real click cannot drift onto a different control.** After the visible cursor delay, the
  extension re-resolves the approved node, recomputes its center, and refuses the action if that
  node disappeared, moved off-screen, or became covered.

## v0.21.17 — Localized final-action binding

- **A verified form can bind its final button across UI languages.** Common Post, Publish, Save,
  Send, and Submit labels are matched through a deliberately small localization table, including
  bilingual planner descriptions such as `保存 / Save`.
- **Localization does not weaken the irreversible-action boundary.** Collie still requires one
  unique enabled live button; multiple localized candidates, duplicate controls, and disabled
  controls remain refused before any click can fire.

## v0.21.16 — Self-contained browser payloads and submit sequencing

- **A failed form preparation can no longer be followed by `browse.submit`.** The Mission container
  deterministically requires the newest browse result to be independently verified before it will
  materialize a final browser click.
- **Browser children may not invent content hidden in the outer case.** Mutating browse payloads
  must embed every complete expected value directly; references such as “use the case draft” fail
  before the browser is touched.
- **Rich-editor verification is exact instead of prefix-based.** A correct opening sentence can no
  longer hide an invented tail or wrong link in the actual post body.

## v0.21.15 — Bounded browser-agent latency

- **Routine browser execution no longer inherits open-ended deep reasoning.** Mission browse
  children use medium reasoning effort by default while preserving the configured model and
  allowing explicit browser-specific overrides.
- **A reversible browser step has an 18-turn default ceiling instead of 35.** The outer Mission can
  repair from a bounded diagnostic rather than letting a two-field form monopolize the full
  ten-minute action watchdog.
- **The browser workflow now has an explicit no-spin condition.** One- or two-field tasks use one
  read/fill/verify pass and stop with a precise diagnostic after two failures on the same field.

## v0.21.14 — Trusted rich-editor input and active-field verification

- **Label-addressed typing now uses genuine browser input when high-fidelity mode is enabled.**
  React/contenteditable editors such as X receive real click, select-all, and text-insertion events,
  so a DOM-filled draft can no longer leave the live Post button disabled merely because the app
  never accepted the synthetic event.
- **Duplicate mounted composers no longer steal writes.** Label targeting ranks rendered,
  in-viewport, modal-local fields ahead of stale off-screen copies, and browser field/form snapshots
  exclude hidden controls from actionable verification.
- **Per-site input authorization is inspectable.** Browser mode responses report the requested
  origin's effective high-fidelity setting as well as the currently active tab setting.

## v0.21.13 — OAuth boundary verification and flow-secret redaction

- **A cross-domain OAuth redirect cannot pass form verification by accident.** The restricted
  browser child now reports the domain it actually ended on; leaving a single-action boundary is a
  failed step even when the provider login page contains many prefilled or hidden fields.
- **OAuth transitions can be resumed as explicit gated steps.** `browse.submit` now documents final
  account creation and app-authorization buttons, with success URL/text postconditions, while still
  refusing commerce.
- **OAuth flow state is treated as secret.** CSRF/authenticity values, OAuth tokens, redirect state,
  page/session identifiers, and referers are redacted in both extension and Python form snapshots.

## v0.21.12 — Reversible browser advancement and privacy hardening

- **Mission browsing can now cross ordinary multi-step UI.** A new exact-ref `browser_advance`
  action opens menus, follows sign-in navigation, chooses non-final options, and focuses rich-text
  editors. The extension itself refuses final publishes/account creation, CAPTCHA, consent grants,
  commerce, destructive actions, and consequential links, which remain behind the outer Gate.
- **Explicit write intent can no longer be reclassified as a read.** `read_only: false` now wins over
  language heuristics, so failed social-editor fills cannot receive a false verified receipt.
- **CAPTCHA response fields are redacted before durable storage.** Both the CSP-safe extension
  snapshot and Python defense-in-depth sanitizer recognize CAPTCHA/recaptcha tokens as secrets.
- **The newest recovery note keeps its complete tail.** Context compaction preserves an ordinary
  operator instruction, including its final URL or constraint, before spending budget on history.

## v0.21.11 — CSP-proof browser verification and self-repair

- **Rich form verification no longer depends on page `eval`.** A structured extension snapshot
  rereads full input and contenteditable values even on strict-CSP sites such as X, while retaining
  sensitive-field redaction. It also records whether final Post/Publish/Next actions are enabled.
- **Final-action targeting prefers an enabled button over same-named navigation links.** Disabled
  submit controls are rejected before the irreversible boundary instead of accidentally targeting a
  global “Post” link.
- **Failed reversible steps feed a bounded diagnostic back to the Mission planner.** Campaigns can
  shorten an overlong post, correct a form, or choose another read path autonomously; cumulative
  retry and model-turn budgets still stop loops.

## v0.21.10 — Safe failed-Mission retry

- **A normally failed Mission can now be retried without erasing its audit trail.** `mission retry`
  creates a fenced successor with the same goal and authority, carries bounded predecessor context
  and receipts forward for duplicate avoidance, and refuses to start while an earlier external
  action or resource is still outcome-uncertain. Failed Mission status now advertises `retry` as
  its explicit recovery control.

## v0.21.9 — Rich social-media editor support

- **Mission browsing can now see and fill contenteditable post composers.** The browser field
  inventory includes rich-text editors used by X, LinkedIn, and Reddit; label- and snapshot-ref
  typing update those editors and emit the input/change events expected by modern web apps. The
  restricted browser child is also explicitly guided to fall back from labelled fields to an exact
  accessibility-snapshot textbox ref, without granting it generic or final-submit clicks.

## v0.21.8 — Read-only browser verification precedence

- **Semantic inspection hints no longer become imaginary form fields.** An explicit no-write
  browser inspection now verifies against the independently reread live page even when a planner
  also supplies semantic expectations such as account identity or Company Page availability.
  Those hints can no longer turn a successful authenticated-account read into a terminal
  “form fields not filled” failure.

## v0.21.7 — Deliverable-aware Mission composition

- **Writing requests can no longer masquerade as finished copy.** If a Mission planner mistakenly
  puts an unmistakable “write/create/draft a post” request in the final-text field, the compose
  primitive repairs it into an actual model-generation request. The Verification Gate also rejects
  both an exact prompt echo and any returned text that is still another writing instruction, while
  preserving legitimate imperative slogans as literal copy.

## v0.21.6 — Exact settings preservation during upgrades

- **Upgrades snapshot and restore the complete settings file.** In addition to skipping the
  first-install language command, Setup now preserves an exact pre-upgrade `settings.json`, restores
  it before supervisor children start, restores it again at successful completion, and restores it
  on rollback. This protects provider, model, language, identity, and every other preference from
  any stale UI request or future post-install helper that writes during the upgrade window.

## v0.21.5 — Durable multi-site Mission context

- **Recent Mission evidence now keeps the newest entries.** When the model context is bounded, the
  driver retains the tail of result/event timelines instead of the oldest prefix. A newly verified
  account or page state therefore informs the very next decision instead of being silently cut off.

- **Browser discoveries accumulate by domain.** Read-only results for VocalCode, X, Reddit,
  LinkedIn, Product Hunt, and other sites remain in a compact per-site map (including the two latest
  observations), so inspecting one platform no longer erases the others or causes discovery loops.

- **CLI human-assist notes are durable.** `mission continue --note ...` now passes the operator's
  actual recovery guidance into the Mission case instead of replacing it with a generic message.

## v0.21.4 — Multi-platform Mission discovery

- **Different sites no longer trigger polling backoff.** The Mission anti-spin guard now tracks the
  resource being observed: refreshing the same inbox still pauses after three consecutive reads,
  while first-time checks of X, Reddit, LinkedIn, Product Hunt, and other distinct sites can proceed
  in one run.

- **The planner distinguishes literal polling from semantic inspection.** `observe.expect` is now
  documented to the planner as an exact page substring. Account identity and page-state discovery
  use one read-only browser action per site, preventing false “not logged in” conclusions and
  preventing a reversible browser child from attempting to cross unrelated site boundaries.

## v0.21.3 — Verified read-only browser work

- **Browser inspection is no longer mistaken for a failed form fill.** A Mission can explicitly
  mark navigation/inspection as `read_only`; the Gate then verifies the independently reread live
  page identity instead of demanding form fields. If a planner omits the flag, an unmistakable
  “inspect/check” plus “do not change/submit” goal is recognized conservatively. Empty-form fill
  operations remain inconclusive, so the new path cannot weaken draft or submission verification.

- **Silent upgrades no longer rewrite user settings.** The installer seeds the selected language on
  first install only; upgrades retain the existing language, provider, model and every other saved
  preference without running a settings write.

## v0.21.2 — Mission composition that produces the deliverable

- **Compose requests and final copy now have distinct fields.** Mission planning puts writing
  instructions in `instruction`, while `text` is reserved for already-final literal copy. The
  composer follows the requested format and returns the ready-to-use deliverable; an echoed
  instruction is rejected by verification instead of being recorded as a successful draft.

- **Polling backoff applies only to actual observation loops.** Research and local multi-channel
  composition can proceed in one startup burst; repeated inbox/page observation still receives the
  durable one-hour anti-spin delay.

## v0.21.1 — Hands-off Missions without command-line ceremony

- **Plain `/mission` now means sustained execution inside your rules.** The saved Mission autonomy
  mode defaults to Hands-off, so publish/send steps already inside the Leash do not ask again merely
  because they are irreversible. `--review` is the clear per-Mission override; legacy `--auto`
  remains compatible but is no longer the main UI or documentation path.

- **Slash commands are discoverable.** Typing `/` opens a keyboard-accessible palette for Mission,
  reviewed Mission, Code and Chat, and the Home starter now inserts the simple `/mission` form.
  Receipts say `execution attempted` instead of the ambiguous `action fired` when verification fails.

- **A connected work identity is usable, not decorative.** User-authorized mailboxes, phone/Google
  Voice numbers, signed-in sessions and verification-code inboxes may support routine signup and OTP
  completion without persisting secrets or codes in Mission history. Person-required CAPTCHA/MFA,
  unavailable identity, new consent/spending, scope expansion and duplicate uncertainty remain
  resumable Needs You boundaries; Collie does not bypass platform security checks.

- **Browser verification understands platforms and rich editors.** A live page origin now proves the
  platform/site expectation, while `content`, `body`, `post_text` and similar semantic expectations
  are checked against independently reread editor values. Abstract `platform` and `tweet_text`
  fields no longer make a valid X draft unverifiable, and the same Gate still rejects the right text
  on the wrong site.

- **Duration is part of completion.** The Mission driver is explicitly required to wait and continue
  for goals that name a cadence or time window instead of declaring a 24-hour campaign complete
  after its first action.

- **Windows upgrades keep their rollback guarantee when directory rename is unavailable.** The
  installer still prefers an atomic runtime rename, but can make a complete known-good backup copy
  when Windows retains a non-delete-sharing directory handle. A later install failure removes the
  partial runtime and restores that backup exactly as before.

## v0.21.0 — A personal AI operations system, from one calm entrance

- **The companion is named, renameable, and no longer trapped on a coloured tile.** First run now
  offers an adoption-style name step with a calm default and Skip; **My Collie** keeps the durable
  rename control. The validated Unicode display name updates Home, Mobile, Remote, and Ambient live,
  with a name-versioned, non-cacheable transparent avatar. Explicit `web --name` kennel selection
  and pinned environment names remain visibly authoritative. Slack app names, `@` handles, and mail
  addresses are deliberately not relabelled. Plated avatar generation remains the compatible
  default for tiny external/Slack icons where the background still carries recognition.

- **The desktop is now an operations home, not a harness dashboard.** Home, Missions, Pack,
  Library, Activity, Needs You, Settings, Mobile, Remote and Ambient share one identity and one
  truthful state model. The default composer stays calm while intent, depth, effort, speed,
  verification, workspace and Pack remain independently controllable. Responsive and reconnect
  paths preserve the user's latest navigation and never turn a failed request into a green state.

- **Library adds a reviewable extension lifecycle.** Data-only packages can contribute Skills,
  exact-hash hooks, connection descriptors, templates and assets, but install inert and cannot add
  arbitrary tools or workers. Exact inventory, digest, publisher identity, component mapping,
  declared authority, compatibility and data policy are validated before approval; enable,
  disable, rollback, revocation, integrity failure and uninstall all fail closed and are audited.

- **24x7 work now has fencing all the way down.** Automation leases carry owner tokens and recover
  orphaned executions without replaying unsafe effects. Task trees enforce ancestor budgets,
  mailbox delivery and resource locks; cancelling a parent propagates through specialist children.
  Mission completion requires structured independent evidence, and verification turns stale if the
  workspace changes while a check runs. Audit/checkpoint failures stop consequential tool calls.

- **Every entrance shares the same supervised runtime.** The VS Code panel starts only a verified
  Collie process on a free port and uses a per-process authenticated embed. Mobile, Ambient and
  Remote restore cross-session approvals; Pack reports real worker freshness and active assignments.
  The relay persists device delivery before acknowledgement, bounds replay/in-flight state and uses
  encrypted WSS outside exact loopback.

- **The release chain is recoverable and bootstrap-pinned.** Windows upgrades back up and restore the
  complete owned runtime, pin bootstrap downloads, verify signed bootstrap publishers and preserve
  user state. The landing build generates its exact CSP hashes, rate limits before model use without
  storing raw client addresses, and the full Python/Node/GUI suites are part of the release runner.

## v0.20.32 (unreleased; folded into v0.21.0) — Auto that really routes, and durable work that really comes back

- **Auto now means a per-task decision, not a disguised model pin.** Within the configured provider,
  Codex routes small, clear work to GPT-5.6 Luna at low effort, everyday engineering to Terra at
  medium effort, and risky/architectural work or recent failures to Sol at high effort. English and
  Chinese task cues are covered; an explicit model/effort still wins. The same resolver and compact
  receipt now serve headless Run, Web, REPL, TUI and ACP. Changing an unrelated Setting can no longer
  persist a synthetic Claude default, and the model picker has an explicit Auto/unpin action.

- **Speed, depth and correctness are separate controls.** Quick/Balanced/Thorough changes loop room;
  Standard/Fast changes the same model's provider service tier and records the credit/price multiplier;
  reasoning effort, Build/Plan/Test/Review, verification, worktree isolation and Pack remain independent.
  Required completion needs executed post-edit evidence, while Plan/Review are gate-enforced read-only.

- **Long work has a crash-safe spine.** Continuous session checkpoints fence uncertain tool calls from
  replay; explicit recovery reconciliation is available in CLI and Web. Mission adds hard per-step
  watchdogs, cumulative token/cost/time/retry/storage budgets, compact context checkpoints, independent
  goal verification, resource locks, escalation deadlines and an executable durable specialist run tree
  with progress, steer, cancellation, mailbox acknowledgement and ancestor accounting.

- **Windows gets an actual per-user supervisor and durable automations.** Task Scheduler (with Startup
  fallback) owns Web, Jobs/Missions, automation execution, the browser bridge and opted-in Slack workers;
  health probes, crash backoff/circuit breaking, sleep catch-up, rotating logs, OAuth refresh ownership and
  retry/DLQ notifications are observable through `collie activity --health` and the authenticated Web
  Activity panel. Timer/file/page/webhook automations separate trigger ingestion from bounded execution,
  use isolated Git worktrees and fail closed on ambient shell/browser/MCP authority.
  Existing Slack listeners are adopted by their fresh per-dog heartbeat, so upgrades have one recovery
  owner instead of racing the legacy launcher into a false circuit-open alarm.

- **Hooks and handoffs are first-class artifacts.** Exact-hash-reviewed lifecycle hooks can gate tool and
  completion boundaries. Editable versioned Plans require explicit user approval before Build; Review
  findings are structured, selectable and handed to a real follow-up Build. The Web control plane exposes
  allowlisted Health, Activity, Recovery, Hooks and specialist controls without task, prompt, result or
  tool-argument content.

- **Windows upgrades stop accumulating mixed runtimes.** The payload build now fails on native/pip errors,
  asserts code/metadata versions and required assets, excludes the live browser-bridge token, includes the
  OAuth adapter assets, and the installer removes only stale Collie/pip package directories before overlay.
  User state under `~/.collie` remains untouched.

## v0.20.31 — @-able, an address of its own, and a gate on the part that leaves your machine

- **A dog has a face, derived from its name.** A name, an address and an @handle wanted a picture to
  go with them, so `collie slack setup` now writes `~/.collie/avatars/<name>.{png,svg}` — the Collie
  logo recoloured, never redrawn. The only entropy is `sha256(name)`, because the same dog has to
  look the same on every machine and after every reinstall; `random` would give a different dog each
  run, which is the one thing an avatar must not do. Two axes: the coat of the coloured regions, and
  the plate behind them, with one natural dark eye on every dog. Palette decided by rendering at the
  sizes Slack actually draws a bot rather than by taste — 20px in the member list, 36-48px beside a
  message — and the plate's lightness is *solved in luminance* against the coat, since a fixed value
  put half the coats at identical luminance to the head and those dogs dissolved into their own
  background. Stdlib only, like the rest of the core: the PNG encoder is `zlib` + `struct` and the
  rasteriser is a scanline fill, which suffices because no path in the logo has a curve or a stroke.
  Slack exposes no API for an app icon — `display_information` carries name, description and
  background colour and nothing else — so setup prints the file and the page rather than pretending.

- **`collie slack` had never started.** `Worker` subclasses `threading.Thread` and its constructor
  assigned `self.ident`, which is a read-only property holding the thread id — AttributeError,
  raised before the socket was opened and before anything reached Slack. Shipped in v0.20.30 and
  dead on arrival, invisible from the outside because a channel with no collie in it looks exactly
  like a collie with nothing to say. Verified afterwards by a real dog reporting into a real
  channel.

- **`collie slack setup`: one Slack app per dog, created from a manifest.** Slack binds an @handle
  to an app, so a pack whose members can be addressed separately needs an app each — and then
  routing is Slack's problem rather than a name-matching heuristic. `apps.manifest.create` makes
  that one command; run it again for the next dog. The kennel is keyed by name, not by machine, so
  one laptop can run several dogs on different repositories. The manifest is bot-only because that
  is the only shape that installs without a public https endpoint: user scopes switch on token
  rotation, a rotating app cannot use the Install button, and Slack then refuses bot scopes on a
  loopback redirect. Two clicks are left, and they are the two Slack exposes no API for.

- **A dog can have an address, and wait for a letter.** `collie mail claim <handle> <email>` once,
  then `collie mail add <dog>` gives `rowan.daming@collie.run`. The point is `collie mail wait
  --subject verify`: a signup that ends in "check your email" stops being a handover to a human.
  Mail is sealed to the dog's public key the moment it arrives, so the hosted relay stores
  ciphertext it has no key for — and the limits are stated rather than glossed: SMTP is cleartext,
  so the message is in Worker memory for an instant (never STORED in the clear); the relay sees
  metadata; a compromised machine means that dog's mail is readable. Primitives are the existing
  `e2e.py` ones — X25519 · HKDF-SHA256 · AES-256-GCM · HMAC-SHA256 — and no request carries a
  bearer token, because a token on disk is a token that can be copied.

- **The Python client and the Worker are checked against each other**, byte for byte: derived keys,
  MAC framing, and an envelope sealed by one and opened by the other. It earned itself immediately
  — WebCrypto imports an X25519 public key as `raw` and refuses a private one, which must arrive as
  PKCS8. Both halves read as correct in isolation.

- **`mcpctl_add` promised something the code did not do**, and three more in the same family: the
  MCP panel could not draw a single server row (a variable read across a function boundary, with a
  catch-all swallowing the ReferenceError), a refused POST was dropped instead of shown, the
  callback port was picked fresh every sign-in so no provider requiring an exact redirect could
  ever match, and no `scope` was requested — which produces a sign-in that succeeds and a token
  every call then refuses.

- **The main loop had no permission gate at all.** The authority machinery — `leash.py`,
  `actions.py` with its HMAC-bound proposals, single-use nonces, TOCTOU snapshot check and durable
  receipts — was only ever wired to `collie jobs`. Everything a user actually runs (`collie -p`, the
  TUI, `collie web`, ACP) went straight from the model's tool call to `tool.run`. That path drives
  the user's REAL logged-in browser and their real desktop, so `browser_click` could send, post, buy
  or delete under their cookies with nothing to stop it. The Origin/token check in
  `browserbridge.py` is transport authentication — it stops a web page driving the bridge; it never
  answered "should this step happen".

- **Risk is now a declared property, in one table.** `harness/risk.py` classifies every tool as
  `read` / `write_local` / `exec` / `external`, and a test walks the live registry so a new tool
  cannot ship without someone deciding what it can reach. It found `mcpctl_connect` — which opens a
  browser OAuth flow and registers a whole new tool set under the user's credentials — sitting
  unclassified. The fallback is `external`, not `read`: collie's tool set is open (MCP,
  `enable_capability`, provider plugins), so an unclassified tool has to fail closed.

- **`project` mode: running collie in your repo is the consent.** Reads, writes and commands inside
  that directory go ahead; anything reaching off the machine asks. An agent that interrupts every
  `pytest` would not be usable, and asking about the work you just asked for is theatre — so the
  line is drawn at the machine's edge instead. `--mode plan|project|interactive|auto`, `COLLIE_MODE`.

- **"Always allow" pins to a target, never to a tool.** `browser_click` on a nav link and on "Send"
  are the same tool with the same schema, so a per-tool rule is either useless or dangerous. Rules
  are `(tool, origin)` — the origin re-read live from the bridge on every call, never cached,
  because a cached origin is exactly how this gets walked past. `browser_eval` / `browser_script` /
  shell can never carry a rule at all: arbitrary code in a logged-in page is that whole account.

- **Nobody there means no.** Piped, in CI, or with no terminal, an off-machine call is refused with
  a reason the model can route around, instead of running because no one objected.

- **ACP was auto-approving everything.** The protocol has `session/request_permission` and the types
  were already in collie's dependency; the adapter never called it. It does now, so Zed / JetBrains
  / neovim render their own native approval UI and collie ships no dialog of its own.

- Authorization for a turn happens **before** any of its calls execute — five proposed calls are all
  decided on before the first one happens, not discovered after the first two already landed.

- The approval path sees the **pre-redaction** arguments. `_redact.restore` swaps `{{SECRET:…}}`
  back one line before `tool.run`; a prompt built after that would print the user's key on screen in
  the name of asking permission. Pinned by a test that fails if the ordering is flipped.

- `collie risk` prints the table. Benchmarks, `pack` and the delegate child build harnesses through
  the same constructor and stay ungated, so what they measure is unchanged.

- **The Inbox: unattended does not raise the ceiling, it changes who can answer.** When nobody is
  at the machine the question parks and the run *suspends* — a phone gets a nudge, and the answer
  can come from there, from the browser card, or from `collie inbox allow <id>`. It is ONE record
  with two visibilities (inline when someone is attending, inbox when not), resolved exactly once,
  first responder wins — so there is no second, laxer code path for "nobody is watching", which is
  precisely where a second path would be wrong. The phone needed no new code: `remote.py` already
  proxies any method and path to the local server, so `POST /api/approve` works over the relay.
  The notification is deliberately not rate-limited the way run-finished notices are — the run is
  stopped until it is answered, so a silent parked approval is a run that looks hung.

- **`collie trust`: a repo cannot grant itself anything.** `.collie/allow.toml` command prefixes are
  inert until the user trusts that exact canonical directory, and trust follows the path rather than
  a content snapshot. This replaces `COLLIE_TRUST_REPO_SKILLS`, which was one global switch — turning
  it on for a project you wrote turned it on for every repo you would ever clone. Entries still face
  the argv-prefix allowlist, so a repo cannot hand itself an operator chain either.

- **Risk overrides, and the rule that nothing but the user writes them.** Every MCP tool defaults to
  `external` because a server's `create_page` and `delete_database` are indistinguishable by name;
  `collie risk --set 'mcp__fs__read_*' --risk read` relaxes what you have actually read, most
  specific glob winning. There is no tool for this and no config hook, on purpose: if something
  collie loaded could reclassify itself as harmless, the gate would be decorative. A test asserts
  no registered tool can reach the store.

- **`collie audit --unexplained`.** Every call that runs WITHOUT a prompt records the rule that let
  it through, so "why was I not asked about that?" has an answer. Reads are not recorded — they have
  no side effect to account for, and burying the log in them is how an audit trail stops being read.
  Typed text and message bodies are stored as a length, never a value.

- **Personas.** A role as one file: identity, tool allowlist, and permission mode together, because
  "fix this bug in my repo" and "go and do this on these websites" want genuinely different defaults
  and the mode belongs with the job description. A persona can only NARROW — its mode is clamped
  against the user's, its tools are filtered against what is registered, and it has no field for
  trust or risk overrides. These are files people copy from the internet; one that could relax the
  gate would be permission smuggled in as configuration. Ships with `webwork`.

- Fixed while building this: `RiskClass(str(value))` is the obvious spelling and the wrong one —
  RiskClass subclasses str+Enum, so `str(RiskClass.READ)` is `"RiskClass.READ"`, and passing the
  enum raised while passing the string worked.

## v0.20.30 — the two releases that were never tagged, and a browser that can finish a form

- **v0.20.28 and v0.20.29 were written and never released.** Both have notes below and neither has
  a git tag, so the newest thing anyone could install is v0.20.27, from 31 July: checkpoints, the
  verify gate that stopped being Python-only, pack's independent attempts and one-attempt-per-model
  have all been sitting in the repository, shipped to nobody. This release carries them.

- **The bridge grew a hand.** A round trip to the browser is a MODEL TURN, so a six-field form cost
  six turns and six copies of the page — `browser_script` runs a list of steps in one call and stops
  at the first one that did not land. Snapshots became an accessibility TREE whose survivors, when
  the cap bites, are chosen by importance (an open dialog first) rather than document order, which
  used to drop precisely the modal that had just opened. Each space gets its OWN tab: `browser_open`
  no longer adopts whatever the user had in front of them, after a run walked into a half-filled
  form in someone's own window. Cross-origin iframes — embedded checkouts, payment fields, captchas
  — can be read and driven, and a page that has them says so instead of quietly returning the top
  document. Plus keys, hover, drag and a bare point: a menu that only opens on hover and a dialog
  that only closes on Escape were unreachable with click and type alone.

- **A token on the bridge, and a record of what it did.** The bridge drives the user's real
  logged-in browser with trusted input, and until now anything running on the machine could drive
  it: the `X-Collie-Bridge` header is not a secret. Chrome closed the equivalent hole in 136; an
  extension that hands the capability back should not be looser than the browser it lives in.

- **A native `<select>` was invisible to every tool that handles dropdowns.** `[role=combobox]`
  matches an explicit role attribute, which a plain HTML dropdown does not carry — so the commonest
  dropdown on the web was missing from `browser_fields`, unreachable by `browser_pick`, and answered
  `browser_type` with `typed: true, landed: false` while the old option stayed selected. A form that
  will not submit until a dropdown is set is a wall, and the agent could not see what it had hit.

- **An upload that attached nothing reported success.** `attached: landed === transfer.files.length`
  is true when BOTH are zero, and a DataTransfer can refuse `items.add` without throwing — so the
  caller's only check passed on an empty file list. It is compared against what was asked for now,
  and says which half refused: the browser before the input was touched, or the page after.

- **A refusal that arrives as a dropped connection is not a refusal.** The bridge answered a blocked
  POST without reading its body, and closing a socket with data still queued makes Windows send an
  RST: the caller got `ConnectionAbortedError` instead of the 403 it was sent, about 2 times in 10.
  The gate was always right — what was wrong is that "the connection dropped" reads as "the bridge
  is down", so the thing that gets restarted is the thing that was working.

- **Collie could make an isolated worktree on Windows and could not remove one.** The removal ran
  git from inside the directory it was deleting — repo_root answers a worktree with its own path —
  and Windows refuses to delete a directory a process is standing in. Git deregistered the worktree
  and then failed to delete the files, so the caller was told nothing had been removed while an
  orphan directory git no longer knew about stayed on disk.

- **The welcome overlay could reappear after you dismissed it.** It opens when the provider probe
  answers, and the skip flag was read before that request went out — so a dismissal inside the
  window was undone by the answer. A dialog that comes back reads as broken software, not a slow
  probe.

- **Connect a service in one press — from the panel, the chat, or the terminal.** Asked to connect
  Slack, Collie used to reach for an npm package wanting a bot token you mint by hand, while Slack
  runs a remote endpoint that does OAuth in a browser and Collie has had the whole handshake (2.1,
  PKCE, dynamic registration) the entire time. The missing piece was the address. A catalog of ten
  endpoints — each probed and answered `401 WWW-Authenticate: Bearer` — now backs a chip in Settings,
  a new `mcpctl_connect` tool, and `collie mcp connect <name>`. `mcpctl_add` told the model in its
  own description that a bare name was enough and then refused one; a description and an
  implementation that disagree are worse than neither, because only one of them is visible from the
  chat.

- **Collie sits in a Slack channel and answers to a name.** `@Rowan` in a channel queues the ask, on
  Socket Mode rather than a webhook, because these are laptops behind NAT. Two gates before the ask
  is read — which channel, and whose word — since a colleague inviting the bot elsewhere would
  otherwise hand that room the ability to drive the machine.

- **A provider can live outside this repository.** `make_provider` consults plugins LAST, so a third
  party can add a backend without being able to take a built-in name, and a plugin that fails to
  import says so rather than surfacing as "unknown provider".

- **Three ways the Mac build was quietly wrong**, all found by opening our own shipped dmg: the app
  inside it was never stapled (it fails only on the machine that is offline the first time it is
  opened), the browser token was being written INSIDE the signed bundle, and the local build aborted
  on every machine without a GITHUB_TOKEN while blaming a rate limit that was not there.

- **Installing the extension is a two-gesture job.** Chrome's "Load unpacked" is a file picker, and a
  picker cannot be typed into — so the absolute path goes on the clipboard and the folder is revealed
  in the file manager, and `--headed` reaches the managed browser at last, which is what makes its
  persistent profile worth signing into.

## v0.20.29 — pack's attempts were reading each other

- **Best-of-N was not N independent attempts.** The trees were isolated; the memory was not. Every
  attempt ran under one project, the loop consolidates its answer by default, and the composer
  auto-prefetches — so attempt 2's prompt arrived carrying attempt 1's conclusion. Measured, not
  inferred: the second prompt contained `RELEVANT MEMORY (auto-recalled): - Task 'pack0' -> the bug
  is in widget_factory.py line 42`. Selection by what passes only means something if the candidates
  are independent, and they had quietly become serial refinement.

  The fix is not "give each attempt its own memory" — every attempt should start from the same
  knowledge about the repo, or they differ for reasons that have nothing to do with the attempt.
  Reads fall through to the shared store; writes land in an overlay that dies with the attempt.
  Nothing to clean up, and a losing attempt's answer no longer becomes a durable fact.

- **One attempt per model.** `--roster anthropic-oauth,codex-oauth,deepseek:deepseek-reasoner`
  spreads the attempts over different backends, round-robin, and `--parallel` runs them at once.
  Selection is still by what PASSES, never by opinion, so a weak member costs tokens and nothing
  else — which is what makes model diversity safe here rather than somewhere a model would judge.
  Every attempt records which backend produced it, and the result names the one that won.

- **A dead backend says so before the attempts, not after.** An expired Claude subscription token
  used to surface as N identical failures and "no attempt passed the check" — indistinguishable
  from a hard task. `expiresAt` existed in the credential blob and nothing read it. Now it is
  checked, the message says to run `claude`, and pack refuses in 0.05s naming the fix.

- **The critic can be a different model.** Its justification is that a separate read does not share
  the author's blind spot, but it was the same model reading twice. `COLLIE_CRITIC_PROVIDER` /
  `COLLIE_CRITIC_MODEL` make the reviewer independent; a backend that cannot be built raises rather
  than silently falling back, because a silent fallback is indistinguishable from a working
  cross-model critic.

- **The tool/loop seam has tests, and test_core.py has been split** along the section boundaries it
  already carried. Its `if __name__ == "__main__"` sat mid-file with 14 tests defined after it —
  every checkpoint test among them — so the standalone runner had never run those.

## v0.20.28 — the work that had been sitting unreleased

Nineteen commits had accumulated since v0.20.27 without a release, so the machine doing the
developing was running the version before all of them. This release is those commits, plus the two
test fixes that were needed to make the suite honest about them.

- **Checkpoints: one-click rollback of what the agent changed.** The tree is snapshotted before the
  agent edits it, wired through the run, the API and the UI, and the prompts stay out of the user's
  repository.
- **The verify gate stopped being Python-only.** "Prove it runs" was measured to be spinning on
  445 of 731 tasks, because the gate only recognised `python3 -c`. It now detects the language and
  requires a build before finish.
- **Benchmark honesty.** A local SWE-bench Pro grader and the first resolve numbers that follow the
  official protocol; the comparison harness stopped inventing wins; a provider outage is recorded as
  a missing observation rather than a loss; cost is measured on both arms including cache reads.
- **Settings apply on change, and say when one is being ignored** — a saved setting could not reach
  the process that reads it.
- **Running out of turns is no longer reported as "done".**
- **Two tests that could never have caught anything.** One asserted a directive that has never
  existed on this branch, and had been red since it landed. The other could not pass on Windows at
  all: it interpolated a path into generated source through two levels of string literal, so
  `C:\Users\…` became a truncated `\U` escape and the child died of SyntaxError before printing —
  and the assertion blamed the settings code it was meant to be testing.

## v0.20.27 — a tool name that made Collie unable to say anything

- **`mcp_status`, `mcp_add`, `mcp_set_enabled` and `mcp_remove` are now `mcpctl_*`, and that is the
  whole release.** The Anthropic API reserves the `mcp_<name>` shape for its own MCP connector and
  refuses any request that declares a tool with it — the entire request, HTTP 400, before a single
  token. Those four tools shipped in v0.20.21 and are registered unconditionally, so from that
  release on, **every message on the subscription path failed**. Nothing could be sent at all.
- **The error said something else entirely.** The refusal comes back as
  `invalid_request_error: "You're out of extra usage. Add more at claude.ai/settings/usage"` — a
  quota message for a naming problem, on an account whose 5-hour window was 8% used. It cost three
  wrong diagnoses before the request was bisected a variable at a time; the tool name was the only
  thing that mattered, and renaming it with the description and schema untouched fixes it.
  `mcp__server__tool` — the double-underscore form MCP servers' own tools use — is unaffected.
- **A test now refuses any tool named `mcp_<name>`,** because nothing about the failure points at
  the cause and the next person will not get there by reading the message.
- **Failures record the HTTP status and the rate-limit headers.** Only the body was kept, so a 400,
  a 429 and a 529 were indistinguishable afterwards — which is exactly why "is this us or them?"
  could not be answered from the record.
## v0.20.26 — releases that do not depend on one laptop being awake

- **The macOS build is signed from repository secrets, on a GitHub-hosted runner.** It had moved to
  a self-hosted Mac so signing could read the Developer ID out of that machine's login keychain.
  That bought one thing and cost two: every release waited on one laptop being awake, and a public
  repository — the one place GitHub advises against it — had a runner attached to it. v0.20.21 and
  v0.20.25 are what the first cost looks like: both were tagged, both queued against a runner that
  was not attached to the repository releases are cut from, and neither produced a single file.
  Everything in their notes below ships here.
- The dmg is still signed and notarised, and the build still refuses to cut a tag whose dmg is
  neither — that check is what makes reading the certificate from secrets safe rather than hopeful.

## v0.20.25 — the desktop stops fidgeting, and a chat that keeps up

- **The chat follows new output again, and can recover from a dropped connection.** Auto-scroll
  decided whether to follow *after* appending, so a block that landed whole — a finished answer, a
  tool card — was already past the 130px threshold by the time the check ran, and the page read its
  own new content as "the user scrolled away". Following stopped for the rest of the run and never
  came back. Separately, a dropped stream does not stop the run: recovery polled the saved session
  five times 1.5s apart, and accepted "the last message is an assistant" as proof of finishing —
  which mid-run matches the PREVIOUS turn, so the window replaced a live run with an older thread.
  It now re-attaches to `/api/mirror`, the run's own event bus, and judges completion against the
  turn count the run started with.
- **Settings holds still.** The panel was sized by its content, so switching category resized the
  dialog under the cursor (701px, then 403px, then 489px) and every keystroke in the search box
  resized it again.
- **The title bar follows the theme.** It is drawn by Windows, not by the page, so a dark UI sat
  under a white caption until the window was told — and the page now reports every theme change.
- **Opening Collie no longer flashes a console, every time.** The C# host was recompiled on every
  single launch: the canonical exe cannot be overwritten while an engine is running, so the swap
  failed, the exe stayed older than its source, and the next launch compiled it again — leaving an
  orphan `cw-build-*.exe` behind each time, one of which the app window was itself running as.
- **"No installed app matching 'Google Chrome'" on a machine with Chrome installed.** The Windows
  app list was six hardcoded paths labelled with the exe basename. It now comes from the Start Menu,
  which is Windows' own list of installed applications and names them the way people say them —
  4 apps became 153 on the machine this was found on.

## v0.20.24 — the model you picked, and a release that arrives

- **`mock` is no longer offered as a model.** It answers from canned text, which is
  indistinguishable from a model that has gone wrong, and it sat in the picker between real
  models where one tap silently replaces every future answer with a fixture. A machine already
  running on it still sees the row it is on, named as canned replies rather than as a model.
- **A model switch that cannot take effect says so.** `COLLIE_PROVIDER` set before Collie starts
  outranks the panel — deliberately, so `COLLIE_PROVIDER=x collie web` still means something.
  It used to do that in silence: the picker accepted the choice, wrote it, reported it back, and
  every run kept using the pinned provider. Now the desktop names the variable, and the phone
  shows that sentence above the list rather than under it.
- **v0.20.21 shipped nothing.** Its tag built on `[self-hosted, macOS, collie-mac]`, and no such
  runner was attached to the repository releases are cut from, so the run queued against a machine
  that did not exist. On the Mac that does exist, `actions/setup-python` then failed at
  `mkdir: /Users/runner: Permission denied` — its macOS package carries an install script with the
  hosted runner's path compiled in. The Mac jobs now use the machine's own Python — and only the
  Mac jobs: applying that same redirection to the hosted Windows runner broke its Python install
  and cost v0.20.22 its release in turn. Everything in the v0.20.21 notes below is in this one.

## v0.20.21 — MCP you can see, an extension that updates itself, settings you can navigate

- **MCP servers have a place in Settings.** Which servers exist, which are switched on, which are
  signed in, how many tools each one advertises — and a switch, a sign-in, and a remove. Previously
  the only way to manage MCP was to hand-write `~/.collie/mcp.json`; the panel did not mention it.
  You can add a server here too: one field that takes an `https://` URL or a command line.
  - Servers can now be switched **off** without deleting how they were set up, which is what you
    want when you are working out whether one of them is the thing causing a problem.
  - Collie can set servers up itself when a task needs one, but only after asking: adding a server
    means Collie choosing its own tools, and for a remote one, using your credentials. Reading the
    list and switching a server **off** never need permission — being able to disable something
    that is misbehaving should not require a permission dance.
  - A server added this way is usable immediately, without restarting Collie.
- **Collie can finish its own update.** Updating Collie updates the browser extension's files, and
  Chrome would go on running the old one — it never re-reads an unpacked extension by itself, and
  its extensions page cannot be automated. New browser tools appeared to be missing for no visible
  reason. Collie now reloads the extension and confirms which version came back, so "I updated" and
  "the browser changed" cannot come apart silently. One manual reload is still needed to adopt this,
  once.
- **Settings is a two-pane panel.** Twenty-six settings in a single scroll, cut into ten groups —
  four of which held a single row — meant scrolling past everything to reach anything. There is now
  a category rail and a search that spans all of it, following what desktop tools do here, because
  matching the habit matters more than being original.
- **It can see inside closed shadow roots.** Component-based sites put real controls in shadow roots
  created in "closed" mode, where the standard way of looking inside returns nothing — so those
  controls did not appear at all, and "not in the snapshot" reads exactly like "not on the page".
  Your pages are unaffected: nothing is forced open and `shadowRoot` still reads as the site built
  it.

## v0.20.20 — the browser tools stop reporting success they never had

This release comes out of watching collie try to work a real, unfamiliar web flow end to end and
fail — not for lack of intelligence, but because four of its browser tools could not fail. Each one
returned the same cheerful result whether it had worked or done nothing at all, so collie believed
them, built theories on top of them, and gave up on things it was actually able to do.

- **It can upload files.** New `browser_upload`: give it a path on your machine and it attaches the
  file to the page — profile picture, banner, video, any attachment, any format. This was previously
  impossible in a way that was nobody's fault and everybody's problem: the obvious move is to click
  the page's "choose file" button and drive the picker that appears, but **Chrome opens the OS file
  picker only for a genuine human gesture**, so an automated click opens no window at all. There was
  nothing to drive, and no error to explain why. Uploading now writes the file to the page's file
  input directly, which is how browser automation has always had to do it.
- **Typing is checked.** `browser_type` now reads the field back afterwards, and a write that landed
  nowhere is an error naming the routes that work, instead of a confident "typed". A silent no-op
  here is worse than a failure: it lets an empty form be submitted and believed.
- **An ambiguous click says it was ambiguous.** Clicking by visible text or a CSS selector takes the
  first match, and pages routinely hold several elements answering to the same name. When more than
  one matches, collie is told the count and the candidates, and pointed at snapshot refs, which are
  exact.
- **A truncated snapshot says it was truncated.** `browser_snapshot` caps how many elements it
  returns, and it walks the page in document order — so what falls off the end is whatever came
  last, which is exactly where a dialog that just opened lives. A cut-off list used to be
  indistinguishable from a complete one, which made a required control look like it did not exist.
- **It can see inside closed shadow roots.** Component-based sites put real controls inside shadow
  roots created in "closed" mode, where the standard way of looking inside returns nothing — so
  those controls did not appear in the snapshot at all, and "not in the snapshot" reads exactly like
  "not on the page". Collie can now see them. Your pages are unaffected: nothing is forced open,
  `shadowRoot` still reads as the site built it, and the visibility is one-way and collie's own.
- **macOS parity.** The Apple Events transport (the no-extension path on macOS) checks typing the
  same way the extension does, so the two never disagree about whether text landed.

## v0.20.19 — collie can look at things

- **It can see the screen.** Every perception collie had was a TREE — `browser_snapshot` returns the
  accessibility tree, `desktop_inspect` returns the UI Automation tree. That is the right primitive
  for acting, since you click a stable element rather than a pixel that moves with DPI and scroll,
  and it is why driving apps works at all. But it meant collie could never see what anything LOOKED
  like: whether a rendering is correct, whether a layout broke, what an app with no accessibility
  tree is showing. Two new tools close that, and the image genuinely reaches the model rather than
  being described to it — on a vision-capable model it is looked at; on a text-only one it degrades
  to a note instead of failing.
  - `screenshot` captures a native window — even one behind others or off-screen, without stealing
    focus — or the whole display. Zero new dependencies.
  - `browser_screenshot` captures the page as rendered. This is the right tool for anything web:
    the OS-level capture cannot see Chromium page content at all (it renders the window frame and an
    empty page, because the page is composited by the GPU process), and it needs the window
    unobscured, while this reads the page directly.
- **It will not hand you a picture of the wrong thing.** The fallback capture path reads screen
  pixels, so with another window in front it would return that window's contents labelled as the
  target — verified: capturing a covered browser returned the editor sitting on top of it. It now
  detects the occlusion and refuses, naming what to do instead. A wrong image presented as right is
  worse than no image.
- **Seeing is gated separately from acting**, and off by default. Desktop control can act, but a
  capture can read whatever happens to be on screen — a password manager, a bank tab, a private
  message — and the image then travels to whatever model is configured. Consent is asked for that
  specifically, in those words, rather than folded into the existing desktop permission.
- **Capabilities ask to be turned on when they are needed.** Gated tools are always registered now,
  so collie can see it HAS a hand or eyes and reach for them: it explains what the capability grants,
  and enables it only after you agree. Previously an off capability was simply invisible to it.
- **Clicks and uploads admit when they were a guess.** A page often has many elements matching the
  same text, and clicking the first was indistinguishable from clicking the right one — the tools now
  report how many matched. File uploads find the input themselves (including inside shadow roots),
  refuse when several exist rather than picking one, and read the result back, because assigning a
  file list is silently refused in some contexts and a refused upload looked exactly like a
  successful one. When a click opens a native OS dialog, collie is pointed at the desktop tools,
  which are the only thing that can drive one.

## v0.20.18 — the download Windows used to refuse

- **The Windows installer is signed.** Unsigned, Chrome and Microsoft Defender did not merely warn
  about `Collie-Setup.exe` — they called it a virus and blocked the download outright, which is the
  whole reason this project ships a plain Inno installer instead of the WebView2 shell it used to
  have. It is signed now, by Azure Artifact Signing, chaining to the Microsoft Identity Verification
  Root, so Windows names a publisher instead of refusing the file. SmartScreen still builds its
  reputation from real installs; what signing changes is that the reputation accumulates against one
  identity instead of starting from nothing with every release.
- **Nothing long-lived had to be stored to do it.** The release job authenticates to Azure over
  OIDC: GitHub mints a short-lived token, Azure exchanges it, and the identity behind it can do
  exactly one thing — sign with one certificate profile. The client and tenant ids in the workflow
  are identifiers, not secrets, which is what makes them safe to keep in a public workflow file. A
  stored client secret would not be: leaking one would let anybody sign code as the certificate
  holder.
- **The build will not publish an unsigned installer.** Signing can report success and leave a file
  untouched — that is how the macOS chain fooled us once — so a verify step runs
  `signtool verify /pa` afterwards and fails the build rather than letting the release through.
- **An empty search is no longer mistaken for proof.** Asked about a project living elsewhere on the
  machine, collie grepped the working directory alone, found nothing, and reported that the thing did
  not exist — while it sat two directories away, edited minutes earlier. Three rules now ride in the
  system prompt: widen the search and say what was actually searched before claiming absence, treat
  auto-recalled memory as a lead rather than a fact, and answer what you can determine yourself
  instead of opening with a list of questions.

## v0.20.17 — music you can stop without asking the agent

- **Three ways to stop the music, none of them a conversation.** Anything the agent starts that
  outlives the request has to leave behind a control that is NOT the agent, and music had none: the
  only ways out were to ask again or to kill a process in a terminal. Now there is a menu-bar item on
  macOS that appears only while something is playing and stops it in one click; a pill in collie's own
  UI, which is the control that exists on every platform; and the reply that starts music says where
  the off switch is, while you are still looking at it.
- **The player no longer outlives collie.** Kill collie mid-song and the music kept going with nothing
  anywhere that could stop it — it is started in its own session so a timeout can reap the whole tree,
  which also meant it did not die with us. Reaped on exit and on SIGTERM/SIGHUP now, installed from
  the main thread because signal handlers cannot be set from the HTTP worker that starts playback.
- **"Stop the music" stops the music.** The intent router used to answer `action=stop` and leave it to
  the caller's own player, which was right while the caller had one.
- **/api/desktop/nowplaying tells the two apart.** What the SYSTEM plays (Spotify, Music) is read-only;
  what collie plays can actually be stopped. Only the second gets a stop button — offering one for the
  first would be a lie.

## v0.20.15 — /api/repos could hang forever

- **A directory walk that never returns no longer takes the endpoint with it.** `~/Music` and
  `~/Movies` are the Apple Music and TV libraries; full of cloud placeholders, `os.walk` over one
  does not come back. `/api/repos` had not finished after five minutes on a real machine — which
  from the phone is a Code screen spinning with nothing to time it out, and a server thread gone
  for good. Those names are pruned at the top of $HOME, and the endpoint answers within a deadline
  whatever the filesystem does, because the next one will have a different name.

## v0.20.14 — the desktop could not tell it had gone offline

- **A dead relay socket is noticed now.** The desktop reported `connected: true` while the relay
  answered "desktop offline" to the phone. Nothing had raised and nothing was wrong with the
  network: the socket was still writable, so every keepalive ping succeeded and the client went on
  believing it was connected, indefinitely, until someone restarted it by hand. Pinging only proves
  the local socket accepts writes — the far end's PONG is the evidence, and the transport was
  discarding it. It is timestamped now, and two missed replies close the socket so the existing
  reconnect can do its job.

## v0.20.13 — your chats were being written inside the application

- **User data moved out of the install.** `data/` — sessions, memory.db, runs.db, the sandbox —
  resolved to wherever `harness` happened to be installed. From the .app that is inside the signed
  bundle, which is read-only: nothing could be saved at all, so the app showed "no chats yet" no
  matter how much you had said, and every run was forgotten the moment it ended. A writable bundle
  would have been worse — each update replaces it and would take the history with it. From pip it
  landed in site-packages, which the next upgrade deletes. A checkout keeps its own `data/`;
  everything else now writes beside `settings.json` and `remote.json`. `COLLIE_DATA_DIR` overrides
  both.
- **The Dock says Collie.** The bundle execs its private interpreter, and macOS names a process
  after the file it executed — so the Dock, Force Quit and Activity Monitor all said "python3".

## v0.20.12 — the Dock said "python3"

- **The app is called Collie everywhere now.** The bundle hands off to its private interpreter
  directly, and macOS names a process after the file it executed — so the Dock, the Force-Quit
  list and Activity Monitor all said "python3", undoing the reason this is a bundle at all.
  The interpreter gets a second name beside itself and the launcher execs that. Setting the name
  from inside (NSProcessInfo, CFBundleName) changes nothing System Events reports, and a hard
  link takes the name but kills the interpreter — CPython finds its stdlib by walking up from the
  path it was executed as. A symlink resolves back to the real file first, so the prefix comes out
  right and the name still sticks.
- **Windows: a command could no longer be killed by one character.** `print` raises on a console
  that cannot encode what it is given — cp1252 has no U+2713 — so a single tick mark in
  "✓ codemap:" ended `collie init` with exit 1 and half a line written. Output is reconfigured
  before anything prints. This was never about init: any command with a glyph was one console
  away from dying.

## v0.20.11 — the app window was pointed at a dead port

- **`collie app` opened, bounced, and showed nothing.** The server scans forward when its
  preferred port is busy, and kept the port it settled on to itself — so the window was sent to
  the one that was *asked for*, which by then belonged to nobody. It probed that port for twelve
  seconds before giving up, which is the bouncing. Relaunching made it worse, not better: the
  abandoned server held its port, so the next launch landed one further along and missed by one
  more. `main()` now reports the port it bound.
- **Music plays on this computer.** collie could already find a track in about a second, but only
  ever handed the URL to whichever screen asked — so a phone saying "play Cruel Summer" got the
  right answer and silence. `/api/desktop/play` plays it here, and stops it.
- **Commands go into the conversation.** A request the intent router carried out itself left no
  trace anywhere. It is a fast path, not a separate place for things to happen, so what it does is
  now written into the chat it was typed in — starting one if the command came first.
- **An encrypted phone survives a desktop restart.** `K_dev` lived only in memory while the
  keypair was regenerated per process, so restarting `collie web` left every paired phone unable
  to open a single frame — reported as an opaque 5xx. It persists in the device store now, as
  E2E_DESIGN.md §7 always said it should, and a device whose key is genuinely gone is told to pair
  again instead of being shown a number.
- **Pairing asks on this screen.** The approval card lived on a page nobody has open at the moment
  a phone scans. A device asking for the run of your computer now interrupts, once.
- **`desktop_*` tools on macOS.** The driver was already there; only Windows was wired to it.

## v0.20.4 — the app is an app, and collie can drive your other ones

- **`collie app` opens an ordinary window.** v0.20.3 fixed it opening nothing by reusing the
  desktop's window, which over-corrected: borderless, no close button, no Dock tile. It now
  has its own — titled, closable, in the Dock and in Cmd-Tab, closing it quits. The live
  desktop stays opt-in, under `collie wallpaper`.
- **App control on macOS.** The Windows build has driven apps through UI Automation for a
  while; macOS now does the same through System Events, with no new dependency. Listing your
  apps and windows, switching to one, quitting one and hiding the rest need **no permission
  at all**; only reading or clicking a window's controls asks for Accessibility, and a denial
  says so by name instead of returning an empty result.
- **The desktop composer understands more.** "switch to Xcode", "quit Safari", "what do I
  have open" now do the thing rather than starting a coding session about it.

## v0.20.3 — the app opens, and collie can update itself

- **Double-clicking Collie.app now opens a window.** It never did on macOS: `collie app`
  gave up on a native window off Windows and fell through to the browser path, which has
  no terminal and nothing to attach a browser to when launched from a bundle — so the
  server started, zero windows were created, and the Dock icon bounced until macOS gave up.
- **`collie update`** checks for a newer release and installs it with `--yes`, into whichever
  install this is (app / Windows setup / brew / pip). Nothing installs unverified: on macOS
  the dmg must satisfy Gatekeeper *and* carry our Developer ID, and the app inside is checked
  again after mounting; on Windows, where the installer is not code-signed, GitHub's published
  sha256 is required. An unsigned image, a dmg with 64 bytes changed, and an exe with 8 bytes
  changed are all refused, each saying what was wrong.
- **`collie uninstall`** — macOS had no uninstaller, so dragging the app to the Trash left
  ~/.collie behind (179 MB on the machine this was written on) plus the Screen Recording,
  Camera and Microphone grants, listed under an app that no longer existed. It lists
  everything first and deletes nothing without `--yes`.

## v0.20.2 — the macOS desktop actually works

The macOS bundle shipped with its whole desktop backend inert: six features were written
against Windows-only APIs and wrapped in `except Exception`, so they returned False without
a word. Opening an app, opening a project, the launcher's contents, every icon, and the
yt-dlp download were all affected, plus fourteen call sites passing a Windows-only
`creationflags`.

- **Apps open.** `/usr/bin/open` and `xdg-open`; `/Applications` is scanned (103 apps on the
  machine this was found on) instead of a list of `C:\` paths; icons come from the bundle's
  `.icns` via `sips` rather than PowerShell.
- **Music is 10x faster and finds playable tracks.** The platform yt-dlp binaries unpack 38MB
  on every run — `--version` alone took 20 seconds — so the pure-Python zipapp is used
  instead: 40s+ down to 4.3s. 24/7 livestreams are dropped rather than down-ranked, since
  they offer no audio-only format and "lofi" matches nothing else.
- **The composer routes desktop intents** — open an app, ask about this machine, open a
  project, stop the music — instead of handing everything to the coding agent.
- **Chinese lyrics match the song playing.** The guard compared word tokens, which Chinese
  does not have, so 太阳之子 and 太陽之子 looked unrelated and another song's lyrics came back.
- **The desktop is a desktop.** It sits one level below every app window, so it can never
  cover your work; double-clicking empty space reveals the desktop, which the window would
  otherwise have swallowed.
- **`tests/test_platform_purity.py`** refuses unguarded Windows-only APIs outside `plat.py`.
  It caught new code on its first day.

## v0.20.1 — the macOS download

A signed, notarised **`Collie-arm64.dmg`** now ships alongside `Collie-Setup.exe`: double-click,
drag to Applications, done. No Python, no terminal, no Gatekeeper warning.

The bundle already existed; it did not work, and every check it had said it did.

- **Compiled extensions could not load.** The bundled interpreter is the process, so it is what
  library validation judges, and it was signed with the hardened runtime and no entitlements —
  `onnxruntime`, `tokenizers` and all of `pyobjc` failed to import. Semantic memory silently
  degraded to keyword search. Now 10/10 import.
- **The signature broke on first launch.** `.pyc` was stripped as build detritus; a signed `.app`
  is sealed, so the first run wrote 242 of them back and Gatekeeper began refusing the app *on the
  user's machine*. Bytecode is precompiled into the bundle, and the launcher cannot write to it.
- **The disk image was unsigned.** It notarised, stapled, and `stapler validate` reported success
  on a file nobody could open — only `spctl` tells you. The dmg is signed before notarisation now.
- **The build asks Gatekeeper for a verdict** and exits non-zero if it is refused. `codesign
  --verify` passes happily on all three failures above.
- **Releases are arm64 only**, and cross-building is refused rather than silently producing a
  payload that cannot be smoke-tested on the machine that built it.

## v0.18.0 — first public release

- **One-click Windows installer** (`Collie-Setup.exe`) — bundles a self-contained runtime
  (Python + Collie + semantic memory), the native desktop window, and the browser bridge.
  No Python, no terminal, no configuration.
- **Verification gate (`assert-verify`)** — Collie writes a reproduction that must fail on
  the broken code, makes the smallest edit that flips it, and re-runs the assertion before a
  task is called done.
- **Terminal-first, editor-anywhere** — `collie` (TUI), `collie web` (browser GUI with the
  live gate, diffs, and settings), and `collie acp` for Zed / JetBrains / neovim / VS Code
  over the Agent Client Protocol.
- **Local-first & model-agnostic** — bring your own subscription or API key (Anthropic,
  OpenAI-compatible presets, Ollama), or run fully local. No account, no telemetry.
- **Built in** — hybrid semantic memory, `code_search`, keyless web search, MCP support, a
  best-of-N `pack` mode, an autonomous `loop` that stops on a real green check, and a
  real-browser bridge that drives your logged-in Chrome/Edge.

MIT-licensed · runs locally · <https://collie.run>
