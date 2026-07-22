# The front-door router — a literature-grounded intent-classifying head

**What it is.** Every message a user sends `collie` first hits a *router*: one
cheap model call classifies it into a small set of task kinds, and the UI routes
it to the right executor — an interactive **chat/code** stream, or the durable,
gated **mission** container. This doc records *why the taxonomy is what it is*, so
the category set isn't a guess. Implementation: `harness/router.py`,
`POST /api/route`, and the `send()` router in `webui/index.html`.

## The decision: three kinds, from two orthogonal axes

The taxonomy is **not** an ad-hoc list. It is two axes that recur across cognitive
science, human-automation engineering, speech-act theory, and AI safety —
discretized into three routable kinds.

| Kind | Axis-1 (know vs do) | Axis-2 (reversibility) | Routes to |
|---|---|---|---|
| **chat** | produce information (answer / explain / **research**) | — | interactive stream |
| **code** | take action | reversible workspace edit (sandbox + VCS) | interactive stream |
| **mission** | take action | **irreversible** real-world act (send/publish/buy/apply/book/pay), or waits on external events | mission container (gated) |

### Axis 1 — "know vs do" (separates chat from code+mission)
- **Parasuraman, Sheridan & Wickens (2000), "A Model for Types and Levels of Human
  Interaction with Automation," *IEEE T-SMC-A* 30(3):286–297.** Automation applies
  to four stages: (1) information acquisition, (2) information analysis, (3)
  decision/action selection, (4) action implementation. The model explicitly
  partitions the two *information* stages from the two *action* stages — the
  know/do line.
- **Kirsh & Maglio (1994), "On Distinguishing Epistemic from Pragmatic Action,"
  *Cognitive Science* 18(4):513–549.** The foundational epistemic (act to acquire
  information) vs pragmatic (act to advance a physical goal) distinction — the
  academic name for know/do.
- **Searle (1975/1969), taxonomy of illocutionary acts; Austin (1962).** Assertives
  (state true things) vs directives/commissives (get action done / commit to future
  action). A chat request wants assertives; code/mission requests are directives.

### Axis 2 — reversibility (separates code from mission, on the "do" side)
- **Amodei et al. (2016), "Concrete Problems in AI Safety," arXiv:1606.06565.** Two
  of its five problems — avoiding negative side effects, safe exploration — are
  fundamentally about *irreversible* environment actions. The safety-critical axis
  is consequence/reversibility, not information-gathering.
- **Krakovna et al. (2019), "Penalizing side effects using stepwise relative
  reachability," arXiv:1806.01186** (and NeurIPS 2020 follow-up). Formalizes
  reversibility as *reachability* — prefer actions that keep prior states reachable.
  Code edits are near-fully reachable (sandbox + version control); mission actions
  (money spent, message sent) reduce reachability. Reversibility is exactly what
  divides code from mission.
- **Sheridan & Verplank (1978), 10 levels of automation.** Autonomy is a continuum;
  mission is the class where the level dial (approve-before-send, wait-for-events)
  actually matters — hence its own gated path.

## Where "research" goes: into chat, never its own kind

Three independent frameworks converge: research is an **information-acquisition /
epistemic / read-only** activity.
- Parasuraman stage 1 (acquisition) — the first stage that *feeds* every task,
  including a mission; not a peer of action.
- Kirsh & Maglio — epistemic action, categorically the "know" side.
- **Yao et al. (2023), "ReAct," ICLR 2023, arXiv:2210.03629** — models web search as
  an information-acquisition *action inside a task loop*, not a task type.

So: research whose endpoint is an **answer** → **chat**; research that **feeds a
world action** → the `research` **step inside a mission** (that primitive already
exists). Never a fourth peer. If ever split out, it is a *sub-mode of chat*
("retrieval-backed answering").

## Mechanism: calibrated confidence, one gated threshold, explicit abstain

- **Ong et al. (2024), "RouteLLM," arXiv:2406.18665.** A small router emits a
  *calibrated score*; a threshold α turns it into a routing decision. We adopt this:
  the classifier returns `confidence`, and **only the irreversible `mission` route is
  gated** (`MISSION_THRESHOLD`, the highest bar). Below it we **abstain to chat**
  (reversible) and surface a one-click *"Run as mission."*
- **Larson et al. (2019), CLINC150, EMNLP-IJCNLP.** Production classifiers must have
  an explicit **out-of-scope / abstain** path + thresholding — do not force-fit every
  message. Our abstain is exactly this.
- **Switchboard-DAMSL (Stolcke et al. 2000); ISO 24617-2 (Bunt et al. 2012/2020).**
  Deployed dialogue-act schemes keep *task-bearing top-level classes small* and push
  detail into orthogonal dimensions. So the router stays coarse (3 kinds); fine
  intent ("which file / which API") is resolved *inside* a path, not at the door.

## The honesty rule: the model is a hard dependency

Chat, code, and mission **all** need the model. So if the model is genuinely
unavailable, the router **raises `ModelUnavailable`** and the UI says so — it does
**not** silently fall back to a heuristic and pretend to route (the downstream
executor would fail too). The *only* fallback is when the model **did** respond but
its label was unparseable (the model is up) → route **chat**, the cheapest working
path. Explicit `/chat` `/code` `/mission` (`/delegate`) prefixes override the
classifier with zero latency and no model call.

**Transient overload is not "unavailable."** Anthropic sheds load per-request with
HTTP 529 `overloaded_error`; collie's providers return errors-as-data and the host
owns retry (`providers.classify_error` already tags 529/429/timeouts as
*retryable*). The router is a host-level caller (it does not go through loop.py's
retry), so `classify()` runs its own **short bounded backoff on retryable errors**
(the front door must ride out a blip, not fail the user's first message) and raises
`ModelUnavailable` only on a **terminal** error (auth / bad request) or after the
transient retries are exhausted (persistent overload == effectively down). This is
why a 529 that a Claude Code session never notices — its harness retries it away —
must not surface as a dead router. `tests/test_router.py` pins both: a transient
529 recovers to the real label; a persistent 529 gives up after N tries.

## Tests
`tests/test_router.py` pins: the three kinds, the mission threshold + abstain, the
unparsed→chat fallback, the model-unavailable contract (no provider / crash / error
completion all raise), and prefix override skipping the model. All $0, deterministic
(a scripted provider stands in for the model).
