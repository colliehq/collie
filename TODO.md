# Collie product TODO

Last reviewed: 2026-08-10. This is the product backlog, not a release checklist. Items are ordered
by user value and by whether Collie can implement them honestly across providers.

## P0 — make run controls truthful and composable

- [x] Replace the single mixed `Normal / Extreme Herding / Isolated / Pack` picker with separate
  controls for task intent, verification, workspace, and candidate strategy.
- [x] Expose `Plan` in the web composer as a tool-enforced read-only intent; it must never silently
  turn the planning run writable.
- [x] Expose verification as `Auto / Required`. `Required` must only report success after an
  executed check; a model saying “tests pass” is not evidence.
- [x] Move `Isolated` to `Workspace: Current / Isolated worktree`.
- [x] Move `Pack` to `Strategy: Single / Pack best-of-N`. Require an explicit check command in the
  composer, and show every candidate's check result and the winner rationale.
- [x] Rename the user-facing quality presets to `Balanced / Thorough`; retain “Extreme Herding” only
  as the branded description of Thorough, not as a separate kind of workspace.

## P1 — real speed and verification evidence

- [x] Add a provider capability contract for same-model Fast, reasoning effort, supported models,
  availability, and billing multiplier. Unsupported providers must not show or silently accept Fast.
- [x] Add true `Standard / Fast` only after receipts and budget limits account for its premium. Fast
  must never mean fewer tests, a smaller model, or silently lower reasoning effort.
- [x] Detect repository verification commands (tests, lint, typecheck, build), show the proposed
  command before the run, and let the user edit it.
- [x] Persist structured verification evidence: command, exit code, timestamp, relevant output,
  working tree/commit, and whether it ran after the last edit.
- [x] Distinguish `Quick / Balanced / Thorough` routing from provider Fast. Quick may trade quality
  for latency, so its model/effort/turn trade-off must be explicit.
- [x] Add a `Test` intent that may inspect files and execute detected checks but cannot edit them;
  offer a failing check as evidence for a separate Build run instead of silently changing scope.

## P2 — plan, review, and long-running work

- [x] Make plans editable artifacts with scope, files, risks, checks, and an explicit `Approve & build`
  action.
- [x] Add a read-only Review intent that produces findings tied to paths/lines and can hand selected
  findings to a new Build run.
- [x] Persist and expose a durable task TODO/progress backend for long runs, including blocked items,
  background state, notifications, resume, steer, and cancellation acknowledgement.
- [x] Render durable task/progress state in the CLI Activity view and authenticated Web Activity panel.
- [x] Carry the same Activity/recovery controls into the dedicated mobile, remote, and ambient pages.
- [x] Add hooks for deterministic pre/post actions such as formatting, tests, and policy checks.

## P3 — orchestration without UI clutter

- [x] Add executable scoped specialist agents with explicit prompts, narrowed tool/resource authority,
  worktree isolation, and a durable run-tree API.
- [x] Render the specialist lifecycle in one compact run tree on the authenticated Web Activity surface.
- [x] Add the compact run tree to the dedicated mobile/remote surfaces when they support long runs.
- [x] Add scheduled/event-triggered automations with isolated workspaces, budgets, notification rules,
  and auditable permissions.
- [x] Add evaluated model routing behind `Auto`; keep model/provider details in an advanced drawer.

## Product rules from the 2026-08-09 review

1. Speed, reasoning effort, verification, permission, isolation, and multi-candidate search are
   independent dimensions. Do not present them as mutually exclusive modes.
2. Fast means the same model at a faster service tier and a higher provider cost. OpenAI documents
   it as `service_tier: "fast"`; Anthropic documents `speed: "fast"` for eligible Opus models.
3. A green verification state requires executed evidence after the last edit.
4. Plan is enforced by tool permissions, not by prompting the model to avoid edits.
5. Advanced controls belong in a disclosure; the default composer should remain calm.

## Official references used for this review

- [OpenAI Codex speed](https://learn.chatgpt.com/docs/agent-configuration/speed)
- [OpenAI Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [OpenAI Codex environments](https://learn.chatgpt.com/docs/environments/modes)
- [OpenAI API Fast mode](https://developers.openai.com/api/docs/guides/fast-mode)
- [Claude Code Fast mode](https://code.claude.com/docs/en/fast-mode)
- [Claude Code agents](https://code.claude.com/docs/en/agents)
- [Claude Code worktrees](https://code.claude.com/docs/en/worktrees)
- [GitHub Copilot CLI reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference)
- [GitHub Copilot autopilot](https://docs.github.com/en/copilot/concepts/agents/copilot-cli/autopilot)
- [Cursor Plan Mode](https://cursor.com/blog/plan-mode)
- [Cursor background agents](https://docs.cursor.com/background-agent)
- [Gemini CLI Plan Mode](https://geminicli.com/docs/cli/plan-mode/)
- [Gemini CLI sandboxing](https://geminicli.com/docs/cli/sandbox/)
- [Gemini CLI checkpointing](https://geminicli.com/docs/cli/checkpointing/)
