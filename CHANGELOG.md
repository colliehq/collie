# Changelog

## v0.20.38 — the verification gate stops announcing that nothing is happening

- **Idle no longer occupies the corner.** The gate has six states and only one of them —
  `idle` — has nothing to report, yet that is the state the sidebar sat in all day: a ring, a
  heading and two lines of copy, permanently, to say "no task yet". It now appears with a run and
  stays afterwards as that run's evidence, including the "nothing needed verifying" outcome. What
  it no longer does is stand there when there is nothing to stand for.

  Deliberately not deleted. "Finishes only when the check goes green" is the claim the whole
  harness is built on, and this panel is the one visible proof of it — a gate that is never seen is
  a promise with nothing behind it. The change is to when it speaks, not whether.

- **The welcome copy pointed at a corner that would now be empty.** That screen shows exactly when
  the gate is idle, so "the verification gate keeps watch from the bottom-left corner" was about to
  become the only untrue thing on screen. Reworded in all nine languages to say when it appears
  rather than where it waits.

## v0.20.37 — a busy model costs a rung, not the answer

- **An overloaded model steps down instead of ending the turn.** Spending the whole retry budget on
  a frontier model that is overloaded and then handing back an error throws away an answer that was
  available one rung down the entire time — and `overloaded_error` on the popular model is the
  commonest way a correctly configured setup stops working. When retries are spent (or the plan is),
  the loop now steps once down a family ladder — opus → sonnet → haiku — and carries on.

- **Down the same provider only.** Inside one provider the plan is already paid for and the only
  open question is which model has capacity. Crossing providers can move the bill from a flat plan
  onto a metered key — the difference between "wait a minute" and "a charge nobody chose" — so it
  is never done automatically. `subscription_fallbacks()` from v0.20.33 still lays out the
  cross-provider option for a person to choose deliberately.

- **Once per run.** A cascade would slide down the whole ladder on one bad minute with nobody
  deciding to, and by the third rung the answer is not the one anyone asked for.

- **And it says so in the ANSWER, not only in an event.** A reply that came from a smaller model
  than the one someone picked has to say so where they will actually read it; an event reaches only
  a panel they may not have open, and silently answering from a lesser model is the one outcome
  worse than saying the frontier one was busy. `res.model` keeps the model that was CHOSEN — the
  record of someone's choice should not be quietly rewritten by what the day's capacity allowed.

## v0.20.36 — the classifier stops running on a frontier model, and says why when it cannot run

- **The router picked its model from a hardcoded pair of provider names.** `DEFAULT_ROUTER_MODEL if
  _name in ("anthropic-oauth", "anthropic")` meant any other Anthropic-family provider — one
  arriving as a plugin, say — fell through to its own default, which is a frontier model, and then
  ran the classifying head on every single message's critical path. Expensive on a good day; on a
  bad one it is an `overloaded_error` where the cheap classifier would have answered fine, and that
  is exactly how it was found. Which providers take a claude model id is a question about what the
  provider IS, so it is asked that way now: build it, check `isinstance(prov, AnthropicProvider)`,
  and re-build with the router's model if it wants one. Both constructions are local.

- **"model unavailable — set a working provider in Settings" was frequently a lie.** The server
  already knows why the route failed and puts it in `detail`; the browser threw that away and
  printed one sentence blaming the configuration. An upstream overload — nothing to do with the
  settings, and over in a minute — therefore read as a setup error, and sent at least one person
  auditing a configuration that had been correct the whole time. The real reason is shown when
  there is one.

## v0.20.35 — the same providers on every screen, and a shorter list

- **The Settings panel and the model picker never asked plugins anything.** v0.20.32 taught
  `collie init` about provider plugins and stopped there — but three surfaces enumerate providers,
  and a plugin reached one. A provider could therefore be installed, selectable and working from
  the command line while being simply absent from the two screens people actually configure collie
  on, which reads as "it was never built". Both now offer what the wizard offers: the panel merges
  plugin providers into the PROVIDER knob, and the catalog reads `catalog` / `via` / `kind` /
  `auth` / `auth_hint` / `discover` out of `COLLIE_PROVIDER_INFO`. The panel copies SCHEMA rather
  than appending to it — SCHEMA is module state shared by every request, and appending would grow
  the options once per request until the list was mostly duplicates.

- **A plugin can be pinned below the everyday list with `rank`, which is compared BEFORE auth.**
  That ordering is the point: something marked advanced belongs at the bottom whether or not it
  currently works, and ranking auth first meant it sank only while it was broken and sprang back up
  among the ordinary choices the moment it started working. Live-discovered rows inherit their
  provider's rank — the first version missed that, and a pinned provider scattered back through the
  middle of the list as soon as discovery succeeded. The ordering invariant in `tests/test_catalog.py`
  is now per-rank rather than global: inside one rank nothing usable is ever buried under something
  that is not, which was the original rule's real intent.

- **"More models" folds the long tail behind a disclosure.** The split is CURATED vs DISCOVERED,
  deliberately not a hand-written "these are the latest" set — a hand-written one is precisely what
  goes stale, and this catalog had already offered three Claude models for a machine that could
  serve ten. What the maintained list names stays outside; what only live discovery turns up —
  older generations, dated snapshots, internal ids — folds away. Local models never fold: they are
  on the machine because somebody deliberately pulled them. A search reaches everything, folded or
  not, because hiding a model whose name was just typed would be a bug rather than tidiness.

## v0.20.34 — one checkout, one memory

- **Memory was scoped by the surface you spoke through, not by the project.** The web app wrote its
  facts under `project="web"`; every argparse default wrote under `"demo"`. On one machine, in one
  checkout, that is two memories divided by nothing but which window the person happened to type
  into — and the dog answering in Slack could not recall a word of what the same dog had worked out
  in the desktop panel an hour earlier. Nothing about a project changes when you move from a chat
  panel to Slack, so nothing about its memory should. `memory.project_scope()` now derives the scope
  from the working directory: a git checkout by its ROOT, so a subdirectory is the same project as
  the repo above it, and the directory itself outside a checkout. `--project` still overrides.

- Deliberate limitation, stated here rather than discovered later: the key is the directory
  basename, so a fork checked out beside its upstream shares one memory. That is usually what is
  wanted — they are the same project — and `--project` exists for when it is not.

- The fallback is `"default"`, never `"global"`. `recall()` reads `project=? OR project='global'`,
  so a scope that fell into `global` would quietly publish one repo's facts to every other project
  on the machine.

**Upgrading.** Facts already stored under `"demo"` or `"web"` stay exactly where they are and are no
longer in scope by default. Nothing is deleted and `--project demo` still reaches them, but a
machine with memory worth keeping should move it into the new scope.

## v0.20.33 — a spent plan is not a rate limit

- **`usage_limit_reached` was being classified as retryable**, because it arrives as a 429 and 429 is
  in the retryable set. A dog whose flat plan had run out therefore spent three backoffs discovering
  that a refusal resetting in two days had not stopped resetting in two days, and then posted the
  vendor's raw JSON at whoever asked — once per ask, for as long as the window lasted.
  `classify_error` now returns a fourth class, `exhausted`, ranked above both terminal and retryable:
  the same text can match either of those and neither answer is useful — one gives up without saying
  the plan comes back, the other retries what cannot succeed until it does. Callers that only know
  `retryable` are unaffected; every one of them already routes everything else to its terminal path,
  which is the correct handling for a spent plan.

- **The refusal now says which plan, when it returns, and what else would work.** The vendor envelope
  carries a `plan_type` and no provider name at all, so a reader holding two subscriptions could not
  tell which had run out, and `resets_in_seconds: 173470` is not a time anyone reads. All three
  answers were already knowable locally; `explain_exhausted` gives them.

- **`provider_kind()` and `subscription_fallbacks()` write down the rule any automatic switch has to
  obey: a spent flat plan may hand work to another flat plan, never to a metered key.** In the moment
  those two are indistinguishable — both are "it stopped working" — but one resolves by waiting and
  the other resolves as a bill nobody chose. An unrecognised provider classifies as metered on
  purpose, so a name added tomorrow cannot be spent by accident. Acting on this automatically is
  deliberately NOT wired in yet: the message tells a human what to switch to, and a human switches.

- Behaviour change worth stating plainly: `classify_error("insufficient_quota", 429)` used to return
  `"terminal"` and now returns `"exhausted"`. Both are non-retryable, so nothing downstream changes
  its mind about whether to retry — only about what it can say.

## v0.20.32 — a provider plugin can introduce itself

- **Installed provider plugins now appear in `collie init`.** The menu was built from the settings
  schema alone, so a provider that arrived as a plugin was reachable only by someone who already
  knew its name to type — discoverable precisely to the people who did not need it discovered. A
  plugin that declares `COLLIE_PROVIDER_INFO = {"name": {"label": ..., "setup": ...}}` is now listed
  beside the built-ins, in the short first-run list as well as the full menu, because a fresh
  machine is where a provider actually gets chosen. `COLLIE_PROVIDERS` on its own still means
  "usable when asked for by name, not advertised"; nothing about the existing hook changed.

- **...and can ask for what it needs at the moment it is picked.** Some providers need more than an
  exported env var — a pairing code, a device enrolment. With no hook for that, choosing one
  "succeeds" and then the first completion fails over a step nobody mentioned, long after the person
  was holding the answer. The optional `setup` callable runs on selection and returns False for "not
  configured", and the wizard then saves nothing: a saved PROVIDER that cannot complete is worse
  than no choice at all.

- **`test_no_plugins_configured_is_silent` was measuring the machine, not the code.** It asserted
  that discovery finds nothing, which stops being true on any box with a provider plugin pip-
  installed — entry-point discovery finding an installed plugin is the feature working. Entry points
  are stubbed in that test now, so it tests what its name claims, and the new menu tests borrow the
  same fixture.

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
