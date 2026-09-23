# Collie 0.28.0: longer work and clearer recovery

This release improves the path from accepting a task to finding its result, including work
that stops before completion. It also fixes operations-panel races that could discard an
automation draft or silently rewrite settings the editor did not display.

## Automated work

New automations have no arbitrary model-turn ceiling. Wall-clock, token, cost, tool-action
and daily-run budgets still bound them. Existing explicit turn limits remain unchanged and
are applied exactly, including values above the interactive Settings range. The interactive
defaults and the agent's soft convergence target are unchanged.

A run that exhausts a budget is recorded as needing attention, with its partial result and
saved conversation when saving succeeded. It is not reported as completed or automatically
replayed. Execution history links directly to that conversation. A finished outcome can be
marked reviewed to clear its reminder while keeping its original state, error and history.
That acknowledgement applies only to the selected execution, never to a later run.

When the loop needs a final no-tools response, it explicitly asks for observed work and
remaining steps. Tool requests returned on that turn are not executed or used as the final
report; they receive a stopped-run fallback. This adds no request or retry beyond the existing
budgeted summary attempt.

Automation Studio exposes the tool-action budget and preserves accepted settings that its
form does not show, including plan mode, continued context and tool permissions. Changing
the trigger type intentionally replaces the trigger configuration. Background refreshes
leave an open draft alone; late responses cannot overwrite a replacement editor. Save
temporarily locks its own fields, submits once and leaves them editable after a refusal.
Closing and reopening the same panel preserves its draft and retires older refreshes.
Recovery and automation controls have additional Simplified and Traditional Chinese labels;
checkbox labels also keep their available width on desktop and phone layouts.

## Task continuity

- Folder discovery has bounded retries and a timeout. Send waits for a confirmed folder;
  a failed lookup leaves the draft and attachments available with an explanation.
- Ordinary automatic follow-ups appear as progress. Stopped or unconfirmed starts remain
  visible. Withdrawing a queued request retires only its own waiting quota-reset plan.
- A failed reset-triggered launch returns the accepted task to the queue and preserves the
  error. A new request's schedule and an admission already under way are protected.
- Recovery checkpoints must be saved before further tool actions. Final transcript-save
  failures are reported accurately. Refused checks cannot certify an edit as verified.
- Pack review detects parent-path conflicts before writing. A fresh review is required
  after conflicts or uncertain application, and completed applications release their lease
  before an immediate follow-up arrives.

## Runtime compatibility

Windows file writes preserve the supplied bytes, including line endings and byte counts.
The macOS cancellation path continues probing an owned process group after reaping its
finished child, retaining recovery when cleanup cannot be confirmed.

Codex CLI and App Server launch arguments accommodate the removed Windows setting in
0.156, based on the executable actually resolved. The optional Codex SDK remains pinned to
0.155.1 and its matching runtime. Initialization, model listing and thread creation were
tested against both CLI versions; newer live editing and resumption were not established
by those probes.

An explicit subscription-only request now rejects unsupported providers before construction
or plugin discovery. Codex's OAuth route additionally requires its first-party endpoint
under that constraint. Ordinary provider selection is unchanged. A proposed automation
subscription-only option is deferred pending complete unattended request accounting.

## Validation scope

Focused regression checks exercise actual stores, HTTP handlers, browser interactions and
owned process cleanup with mocked providers. The complete test gate and release builds run
in the public [colliehq/collie Actions](https://github.com/colliehq/collie/actions).
Release publication requires the separate quality gate and signed installer checks.

Private synthetic Claude experiments informed the fixes. They separately record output
correctness, completion, scope compliance and reporting accuracy. A fresh 18-run comparison
produced correct final artifacts in all runs, but one candidate run timed out before its
final receipt; transient-write and reporting qualifications remain. This is defect-finding
evidence, not a claim that Collie outperforms another harness. Read-only HTTP recovery
experiments do not certify replay of external side effects.

The [fixed-version source review](agent-harness-source-update-2026-09-22.md) records the
Codex, OpenCode and Goose paths inspected and the limits of those comparisons.
