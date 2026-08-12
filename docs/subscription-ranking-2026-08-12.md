# Collie Opus subscription product ranking — 2026-08-12

Status: **completed, exploratory, not publishable**

- Branch: `feat/collie-harness-benchmark`
- Frozen suite: `1498273f355f24345d19516a307d8c426fb559065846eb3aaba09bc9d2c3b00f`
- Execution revision: `739cc0ed4cd3c87e11bf00c50d8b9c2d3535aa55`
- Corrected summary revision: `02f78c82c4b65ffa4b98ecdb673fe993b71ad574`
- Result directory: `bench/results/subscription-rank-v1-1498273f355f/`

## Result

The declared primary metric is the task-balanced pass rate from the evaluator-owned hidden
grader. On that metric, **Collie and native Claude Code tie for rank 1**.

| Metric | Collie | Native Claude Code | Reading |
| --- | ---: | ---: | --- |
| Hidden-grader patch passes | 6/6 (100%) | 6/6 (100%) | Rank 1 tie |
| Multi-file audit task | 3/3 | 3/3 | Tie |
| Circuit-breaker task | 3/3 | 3/3 | Tie |
| Normal product completion | 3/6 (50%) | 6/6 (100%) | Claude leads operationally |
| Turn-budget exhaustion | 3/6 | 0/6 | Collie termination defect |
| Median agent wall time | 54.714 s | 29.644 s | Collie was 1.85x slower |
| Descriptive processed tokens | 236,062 | 219,852 | Collie reported 7.4% more |

Latency and token fields are secondary and never break a capability tie. Token accounting is not
strictly comparable across the two products.

## Why the first summary was corrected

The first derived `summary.json` ranked the top-level `result.resolved` field and produced Claude
1.0 versus Collie 0.5. That contradicted the frozen manifest's
`task_balanced_hidden_test_solve_rate` rule. All twelve external graders actually passed.

Collie's three multi-file audit runs produced correct patches, then exhausted the Collie outer
loop's 12-turn budget before emitting a final no-tool completion message. They were therefore
stored as `valid_unresolved` even though `grader.resolved` was true. The patches touch all four
required files; two are byte-identical and the third differs only in formatting style.

The corrected summary uses the external grader for capability ranking and retains completion as a
separate product metric. No model was rerun and none of the twelve reservations, patches, usage
receipts, grader receipts, or terminal result receipts was changed. The old summary is preserved
as `summary.rule-v1-original.json`; `summary-correction.json` binds both summaries to the unchanged
result-receipt digest.

This is best classified as a **deterministic termination-efficiency and budget-accounting
failure**, not a comprehension or implementation failure. Collie's reasoner contract permits one
tool action per outer turn. The four-file task required enough inspection, editing, and coverage
checks to use all twelve turns. The one-file task consistently completed in eight turns.

Claude reported 13–14 native turns on the same multi-file task despite the nominal 12-turn input,
so native turn counts are not equivalent across products. Wall time is the usable cross-product
resource comparison here.

## Frozen protocol

The run used two synthetic coding task clusters, three repetitions per task and arm, and one
attempt per cell: `2 × 3 × 2 = 12` total launches. The AB/BA schedule gave each arm the first
position three times. Both arms requested the rolling `opus` alias through Claude Code 2.1.221;
Collie used the official CLI as its reasoner and the comparison arm used native Claude Code.

Each attempt ran in a fresh non-root Docker workspace. Agent containers could not see the hidden
grader or gold implementation. Grading ran afterward with no network and a read-only candidate
workspace. Evaluator retries and Collie loop retries were both zero. An independent audit matched
the suite digest, pinned execution blobs, all 12 result rows, and all 60 expected run artifacts;
it found no residual benchmark containers.

This is a subscription-native **whole-product** comparison, not an isolated harness-effect test.
Prime Agent is a methodology reference rather than an arm. Pi was not admitted to this Opus/no-
extra-charge track because its documented Claude Pro/Max route uses metered extra usage; Codex
does not provide the same Opus model route. The broader admission rules are in
[`harness-benchmark.md`](harness-benchmark.md).

## Billing evidence

Before launch, the account UI showed a Max (20x) first-party Claude.ai login, usage credits off,
auto-reload off, and `$0.00` usage-credit spend. The container guard rejected API keys, proxy/base
URL overrides, and other metered fallback routes. This supports an expected marginal charge of
zero: with usage credits disabled, the run could consume subscription allowance or stop, but not
fall through to metered credits.

There was no separately attested post-run billing observation. The evidence therefore does **not**
claim an independently proven `actual_marginal_charge_usd = 0`.

## What Collie should change next

1. Reserve a terminalization turn, or auto-finalize a valid candidate when the last allowed turn
   successfully edits the workspace.
2. Support batched reads and edits so a multi-file change does not require one outer model call per
   file operation.
3. Keep patch correctness, normal agent termination, wall timeout, and infrastructure validity as
   separate result fields throughout the product—not only in the benchmark summary.
4. Compare products primarily under a common external wall budget; retain native turn counts only
   as diagnostics unless their semantics can be normalized.
5. Expand to substantially more independent task clusters before making a public capability or
   statistical claim.

## Claim boundary

The defensible conclusion is narrow: **on two frozen local synthetic task clusters and twelve
Opus subscription attempts, both products produced hidden-test-correct patches in every attempt;
native Claude Code completed normally and ran faster more consistently.** Repetitions of the same
two tasks are not independent evidence, the model alias is rolling, and no significance claim is
warranted.
