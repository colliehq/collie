# Agent-harness surfaces and user profiles

Research date: 2026-08-29. This is a product-research note, not market-share marketing copy.

## Executive conclusion

There is no defensible public number for “what percentage of Codex or Claude Code users primarily
use Desktop vs IDE vs CLI.” Neither vendor publishes that split, and users commonly combine a
terminal agent with an IDE for review. Downloads, extension installs, GitHub stars, search volume,
and total active users cannot be converted into mutually exclusive surface shares.

The evidence does support three decisions:

1. **Desktop is the growth and supervision surface.** It lowers setup cost for domain experts and
   becomes more valuable as a person runs several long-lived agents in parallel.
2. **CLI and IDE remain the high-frequency execution surfaces for developers.** Collie must not
   force those users through a graphical shell or duplicate their editor.
3. **The product is one runtime with several views, not several separate clients.** A Mission,
   permission decision, connection, memory, and receipt must survive movement between surfaces.

The right Collie shape is therefore a desktop control plane, a full-fidelity CLI, a context-and-review
IDE companion, and a mobile supervision surface. Desktop should not become a general-purpose IDE;
CLI should not become the only place where powerful features exist.

## What is actually measured

### Adoption, not surface share

- JetBrains' May–July 2026 Developer Ecosystem Survey reports more than 15,000 professional
  developers: 90% used an AI coding agent at work at least weekly and 68% daily. Claude Code was
  used by 39% and was the primary tool for 31%; Codex was used by 16%. These are overlapping tool
  adoption rates, not CLI/Desktop/IDE shares.
- Sonar's 2026 survey of 1,149 developers reports past-year use of Claude/Claude Code at 48% and
  Codex at 17%. The average team used four AI tools; 35% of access across the leading tools was via
  personal rather than sanctioned accounts. Smaller companies were more likely to use command-line
  tools, while Codex skewed toward developers with ten or fewer years of experience. Again, this
  does not separate each product's surfaces.
- OpenAI reports more than five million weekly Codex users by June 2026, more than six times the
  level around the February desktop launch. Roughly 20% were knowledge workers rather than
  developers. This strongly suggests that the desktop launch and broader artifact workflows opened
  a new audience, but it does not prove what share of existing developers switched surfaces.
- Anthropic analyzed about 400,000 interactive sessions from about 235,000 people. Its included
  data combines CLI, Claude.ai, and Desktop while explicitly excluding third-party IDE and headless
  use, so it cannot answer the surface split. Anthropic says users average 20 hours per week while
  Claude Code is actively running; that is agent runtime, not hands-on screen time.

### Work and user composition

Anthropic's session analysis gives the best public shape of harness work:

| Work mode | Share of interactive sessions |
| --- | ---: |
| Write, fix, test, or orchestrate code | 56% |
| Operate software: deploy, configure, run, monitor | 17% |
| Plan or understand systems | 14% |
| Analyze data or produce prose/artifacts | 13% |

From October 2025 to April 2026, fixing broken code fell from 33% to 19%, operating software rose
from 14% to 21%, and writing/data analysis roughly doubled from about 10% to 20%. Software and data
occupations were still the largest inferred group, followed by business/finance, design/media,
management, and sciences; management, sales, and legal were the fastest-growing non-software groups.

The same study found that people made about 70% of planning decisions but only 20% of execution
decisions. Expert users triggered more than twice as many actions per prompt as novices. This makes
“show every tool call as the main UI” the wrong default: users need control over goals and evidence,
with execution detail available on demand.

OpenAI reports a parallel shift: by June 2026 about 20% of Codex users were knowledge workers, and
more than 70% of sampled individual users had requested at least one task estimated to take a person
over an hour. At OpenAI itself, Codex became the primary AI surface across engineering and also legal,
finance, and recruiting. Long-horizon parallel work makes attention management—not chat rendering—the
core desktop problem.

### Supervision behavior

A smaller, biased but directly relevant 2026 survey of 412 AI-tool users found that for tasks longer
than five minutes, 34% watched the terminal continuously, 41% checked periodically, 15% waited for
the result, and 10% used remote monitoring. It also found 53% comfortable with autonomy when approval
gates exist, versus only 12% comfortable without that condition. Because almost half the sample came
from a remote-monitoring product's users, these numbers are directional, not population estimates.

The important UX signal is robust: most people want **awareness without reading a transcript** and
conditional autonomy rather than either constant interruption or blind trust.

## Working surface-share hypotheses

These ranges are product-planning priors to validate, **not observed market statistics**. “Primary”
means where a user starts and supervises most interactive work; an IDE used to inspect changes beside
a terminal does not become a separate user.

| Product | CLI / terminal primary | Desktop primary | IDE-extension primary | Web / mobile primary | Confidence |
| --- | ---: | ---: | ---: | ---: | --- |
| Claude Code interactive users | 40–55% | 25–40% | 10–20% | 5–15% | Low |
| Codex active users after desktop launch | 20–35% | 40–55% | 10–20% | 10–20% | Low |

Why the difference: Claude Code grew from a terminal-native power-user base and only later added a
rich desktop shell; Codex's six-fold growth coincided with a heavily promoted desktop app and a
knowledge-worker expansion. Both vendors now let users move between surfaces, so these ranges should
converge rather than be treated as durable identities.

Do not quote these ranges outside product planning. Replace them with real data only if a survey
asks a mutually exclusive “primary surface” question and separately records secondary surfaces.

## Collie target profiles

The following is a proposed next-12-month product mix, not a claim about the whole market:

| Target profile | Planning weight | Primary surface | What they are really hiring Collie for |
| --- | ---: | --- | --- |
| Agent-native builder | 35% | CLI plus IDE review | Dense control, scripting, repo work, remote machines, exact evidence |
| Visual supervising developer | 30% | Desktop plus IDE | Several Missions, visual diff/test review, previews, attention routing |
| Domain expert automator | 20% | Desktop | Outcome language, artifacts, app connections, no terminal prerequisite |
| Team lead / governance operator | 10% | Desktop/Web | Status, policy, budgets, provenance, receipts, shared reviewed connections |
| Local-first integrator | 5% | CLI/Desktop | Private memory, own models, custom remote MCP, inspectable local state |

Mobile is a secondary surface across all five profiles: start, monitor, approve, steer, and receive a
finished artifact. It should not be designed as a cramped desktop editor.

## UX changes this implies

### One runtime, resumable everywhere

- CLI, desktop, IDE, browser, and phone open the same Mission and Receipt.
- “Open in Desktop,” “Open in IDE,” and “Continue in terminal” are transitions, not exports.
- Connection and permission decisions are global, payload-bound, and visible on every surface.

### Desktop: control plane, not another IDE

- Default Home hierarchy: Needs You → active/waiting Missions → outcome composer → recent receipts.
- A Mission card shows state, next step, elapsed time, execution location, budget, and evidence grade.
- Multiple agents collapse to a quiet attention queue. Raw transcripts are an advanced diagnostic.
- Code work opens visual diff, check results, app preview, and “Open in IDE”; it does not recreate
  project search, refactoring, or language navigation already supplied by the user's editor.
- Non-code work opens the actual artifact preview and source/data provenance, not a code-centric diff.
- Library starts with “What outcome needs a service?” and remote MCP recommendations. Local Skills
  and executable packages are secondary/advanced paths.

### CLI: full fidelity, no GUI tax

- Every core action remains scriptable and composable; JSON/stream output stays stable.
- The TUI shows compact Mission/Needs You state without reducing available authority or evidence.
- A CLI user can hand a running session to Desktop for parallel supervision or visual review without
  re-prompting or losing context.

### IDE: context and review, not a second control plane

- Send selection, diagnostics, open files, and repository identity into an existing/new Mission.
- Show diffs, checks, evidence, and the exact approval that needs attention.
- Delegate long work to the durable runtime, then let the developer keep editing another branch.
- Keep global connection discovery, devices, budgets, and automation setup in Desktop.

### Progressive disclosure by behavior, not persona questionnaires

- Start everyone with outcome, status, evidence, and one safe next action.
- Reveal terminal/tool details when a user opens them repeatedly; remember that preference locally.
- Keep model, effort, MCP manifests, raw events, and policy internals under inspectable Advanced
  controls rather than asking users to configure them before first success.
- Use the current entry surface and task artifact locally to choose layout; never upload a “persona.”

## Validation plan without covert telemetry

Collie's no-product-telemetry promise should remain. Validate these hypotheses with:

1. An optional, clearly disclosed two-question survey: primary surface and primary task family.
2. Content-free local counters for entry surface, surface transitions, time-to-first-success,
   Mission abandonment, approval frequency, and whether evidence was opened.
3. A user-triggered “Share anonymous UX diagnostics” export that shows the exact aggregate before
   upload; no prompts, paths, filenames, app names, URLs, connection names, or transcripts.
4. Ten interviews per target profile focused on the last completed task, not feature preferences.
5. Usability tests for three journeys: code fix with IDE handoff, multi-Mission supervision, and a
   domain expert connecting calendar/email to produce an artifact.

Primary success metrics: first verified outcome, uninterrupted time while a Mission works, time from
Needs You to resolution, successful cross-surface resume, and percentage of completed work whose
evidence the user can correctly explain. “Messages sent” and “minutes with the app open” are not
success metrics.

## Sources

- [JetBrains Developer Ecosystem Survey 2026: agent adoption](https://blog.jetbrains.com/research/2026/08/ai-coding-agent-adoption-2026/)
- [Anthropic: Agentic coding and persistent returns to expertise](https://www.anthropic.com/research/claude-code-expertise)
- [Anthropic Claude Code platform guidance](https://code.claude.com/docs/en/platforms)
- [OpenAI: Codex for knowledge work](https://openai.com/index/codex-for-knowledge-work/)
- [OpenAI: How agents are transforming work](https://openai.com/index/how-agents-are-transforming-work/)
- [Sonar State of Code 2026](https://www.sonarsource.com/state-of-code-developer-survey-report.pdf)
- [Tactic Remote developer survey](https://www.clauderc.com/blog/2026-02-27-developer-survey-results-2026/)
