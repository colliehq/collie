# SWE-bench failure audit — where collie's resolve-rate actually leaks

Follow-up to the retrieval work. MEMORY_BENCH.md concluded the SWE lever is **downstream of
retrieval** (code_search localization is already ~0.83 file-hit@10; Qwen3-swap infeasible,
reranker hurts). This audit opens up the 9 failing instances (of 16, DeepSeek-V3, the c16
run) to find the real bottleneck.

## Failure taxonomy (9 failures)

| failure mode | n | instances |
|---|:--:|---|
| **multi-file miss** (edits the primary file, misses sibling(s)) | 4 | seaborn-3187, pylint-4551, pylint-4604, pylint-4661 |
| **right file, wrong edit** (localization perfect, code change wrong) | 3 | requests-1766, pytest-10051, sphinx-10435 |
| empty patch (never converged to an edit) | 1 | seaborn-3069 |
| wrong file (localization miss; also flaky) | 1 | requests-1724 |

Localization was NOT the top failure — collie edits *a* correct gold file in 8/9 failures.
The leaks are (a) multi-file coverage and (b) edit correctness. All 3 pylint failures are
multi-file misses.

## Fix shipped: the working-directory blind spot (general efficiency win)

Instrumenting a pylint-4551 re-run (`COLLIE_DEBUG=1`) exposed a bug affecting **every**
bash-using instance: **the model was never told its working directory.** It burned ~15 of 35
turns guessing — `cd /repo`, `cd /workspace`, `cd ~`, `wc -l /home/user/pylint/...` — all
failing, because the harness runs tools with `cwd=repo_root` but the prompt never said where
that was. First successful edit didn't land until turn 25.

Fix (`context.py`): add a `WORKING DIRECTORY: <cwd>` line to the stable prompt tier, stating
all tools run from there and to use relative paths (no `cd`, no `/repo`/`~` prefixes).

**Measured:** same instance, same everything else — the `cd`-guessing turns vanished and the
first edit moved from **turn 25 → turn 12** (~13 turns freed). No regression on a passing
single-file instance (flask-5014: still 1/1, 59s). This is a pure efficiency/clarity fix and
frees turn budget for the actual work on every instance.

Also bumped `related_locations` k=4 → 8 (a gold sibling, pylint-4551 writer.py, sits at
rank ~6, invisible at k=4). Low-risk recall bump; retained.

## Honest negative: multi-file coordinated edits are a MODEL ceiling, not a harness bug

The multi-file miss is 4/9 failures, so it was the prime fix target. The machinery to handle
it already exists (`related_locations` surfaces sibling files right after the first edit) and
it **works** — on pylint-4551 the model was shown AND read the gold siblings (inspector.py,
writer.py) after editing the primary file. But across **three** instrumented pylint-4551 runs
and re-runs of pylint-4604 / seaborn-3187, the model **edited only the one primary file every
time** (1/4, 0/2, 1/2). Tried, and reverted as no-gain:

- stronger hint wording ("you MUST `edit_file` each sibling that applies") — zero coverage
  gain, and pylint-4604 regressed to an empty patch (over-steering). Reverted to neutral.
- wider post-edit churn window (5→8 turns) to give the hint room — the model doesn't use the
  turns to edit siblings anyway; only re-inflated single-file runs. Reverted to 5.

**Conclusion:** DeepSeek-V3 reliably fixes the *primary* file and does not commit *coordinated
sibling edits* even when the siblings are surfaced, read, explicitly requested, and turn
budget is available. This is a model-capability ceiling. Harness nudges (surface + instruct +
budget) don't cross it. The `related_locations` surfacing is kept (it's correct and cheap);
the honest limit is documented in `loop.py`.

**Plan-first also falsified (4th strategy).** The remaining untested idea was to commit the
full multi-file scope up front — a `COLLIE_PLAN_FIRST` prompt telling the model to emit
`PLAN: file1, file2, ...` before editing, then edit every file in the plan. Measured (coverage
proxy, no Docker): pylint-4551 **1/4**, pylint-4604 **1/2**, seaborn-3187 **1/2** — no gain
over the default hint on any case. So four distinct strategies (default one-shot hint, stronger
"MUST edit" wording, wider churn window, plan-first) all yield zero multi-file coverage gain.
This also kills the heavier "plan-then-edit enforcement loop": if the cheap plan-first prompt
can't get the model to produce and follow a multi-file plan, a checklist loop enforcing it
would only spin or force wrong edits (the strong wording already regressed pylint-4604 to an
empty patch). `COLLIE_PLAN_FIRST` is kept OFF-by-default and gated for a future stronger model.
The multi-file lever is closed at the harness layer for this model; it needs a more agentic
model, not more prompt/loop engineering.

## Ground-truth re-eval (official Docker) — cwd fix is efficiency, not resolve-rate

Re-ran all 16 with fresh cwd-fix predictions (`preds/c16_cwd.jsonl`), official SWE-bench
Docker eval: **7/16 resolved — identical to the 7/16 baseline.** Instance churn vs baseline:
+requests-1724, +requests-1766 resolved; −xarray-3095 (empty patch this run), −pytest-10081.
That 2-in/2-out is DeepSeek-V3 run-to-run **non-determinism** (requests-1724 already flipped
between the c16 and c16b baseline runs; xarray-3095 produced a patch before, an empty one here),
not a real delta. Net resolve change from the cwd fix: **zero.**

Honest read: the cwd fix is a genuine **efficiency** win (first edit turn 25→12; no more
`cd /repo` flailing) and worth keeping, but resolve rate on this set is bounded by
edit-correctness and the multi-file model ceiling — neither of which a working-directory hint
touches. Efficiency ≠ resolve; this run separates them cleanly. (Eval infra note: native
Docker in WSL needed `~/.docker/config.json` `credsStore: desktop.exe` removed — the leftover
Docker-Desktop credential helper made SWE-bench's base-image pull error out on 11/16 instances
until fixed.)

## Does embedding help a STRONGER loop? Bolt collie's code_search onto Hermes

collie (embedding code_search) resolves 7/16; Hermes (grep + file + code_execution, **no
embedding**) resolves 8/16 — so on collie's own loop, embedding didn't beat grep. Open
question: is embedding worthless for SWE, or was collie's *loop* the bottleneck? Test: expose
collie's `code_search` (same bge-small index) as an MCP server (`harness/mcp_codesearch.py`),
register it with Hermes (`hermes mcp add collie_cs ...`), and let Hermes use it.

Hermes used it heavily — **27 code_search calls across all 16 instances** (unforced; it chose
to, with sensible queries, indexing the correct per-instance repo). Result:

| agent | code nav | resolved |
|---|---|:--:|
| Hermes (baseline) | grep + file only | 8/16 |
| **Hermes + collie embedding** | grep + semantic code_search | **9/16** (+seaborn-3187, +requests-1724, +requests-1766; −pytest-10081; xarray-3095 un-evaluable*) |

\* xarray-3095 hit persistent containerd snapshot corruption (survives `prune -a` + daemon
restart) and couldn't be scored; it was resolved in the Hermes baseline, so the true figure
is 9 or 10 of 16.

**Read:** +1 to +2 is at the edge of DeepSeek-V3's ±2 run-to-run noise on n=16, so not
decisive — but it is **directionally positive on Hermes**, unlike on collie's own loop where
embedding ≈ grep. Notably it resolved **seaborn-3187, a multi-file case** both collie and
baseline Hermes failed. This *revises* the earlier "embedding isn't the moat" read: embedding
code_search does add value, **but only when the agentic loop is good enough to exploit it**.
collie's bottleneck was its LOOP (edit follow-through, multi-file coordination), not its
retrieval. The lever for collie is loop quality — and a good embedding tool pays off once the
loop can use it, as Hermes' does. (n=16 with ±2 noise → suggestive, not proven; a larger set
would be needed to nail the delta.)

## Aligning collie's LOOP to Hermes (keep native embedding) — the real lever

The Hermes+embedding result said the bottleneck was collie's LOOP, not its retrieval. Diagnosed
by diffing a Hermes session trace (`hermes sessions export`) vs collie's on the same instance:

| behavior | Hermes | collie (before) |
|---|---|---|
| **verify the fix** | edits once after exploring, then runs `python -c` to TEST it and iterates (6 terminal calls on sphinx-10449) | `self_verify=False` + "never run tests" → **edits blind** |
| turn budget | `max_iterations=90` | `max_turns=35` + churn-cap breaks 5 turns post-edit |
| localization | 12 precise `search_files` (grep) + targeted offset/limit reads | semantic search + fewer reads |

The biggest gap was **no verification** — mapping straight onto collie's 3/9 "right file, wrong
edit" failures. Ported (not cloning Hermes — porting the behavior, keeping collie's native
embedding + auto-prefetch):
- **SWE verify step** (`_SWE_VERIFY_NUDGE`, gated `COLLIE_SWE_VERIFY=1`): after editing, run a
  short `python -c` that reproduces the issue and checks the fix; if it errors, fix and re-check.
  NOT pytest/the suite (env unset; grader runs tests). Smoke trace confirmed collie now does
  reproduce→understand→edit→verify instead of edit-blind.
- **max_turns 35 → 50** so the verify/fix loop has headroom.

**Measured (official Docker):**

| config | loop | embedding | resolved |
|---|---|---|:--:|
| collie baseline | edit-blind | native | 7/16 |
| **collie + verify** | Hermes-style verify | native | **8/16** (9 likely; xarray-3095 un-evaluable*) |
| Hermes baseline | strong | none | 8/16 |
| Hermes + collie embedding | strong | MCP | 9–10/16 |

\* xarray-3095 hit the same persistent containerd snapshot corruption; resolved in the collie
baseline, so the true collie+verify figure is 8 or 9 of 16.

**Read:** porting Hermes' verify loop lifted collie 7 → 8–9, *catching up to Hermes' own 8/16*
while keeping native embedding — the two paths (collie+verify, Hermes+embedding) both land at
8–10. The +1–2 is at the ±2 noise edge, but the gain is **hypothesis-consistent**: requests-1766,
a "right file, wrong edit" audit case the verify step is designed to catch, went from fail →
resolve; and empty-patch failures dropped to 0. The lever the whole audit pointed to — loop
quality, not retrieval — is confirmed: **good loop + native embedding is collie's best config**,
and it's now competitive with Hermes. Larger-n needed to nail the delta; the multi-file model
ceiling (pylint 4551/4604/4661) still caps the top end.
- **right file / wrong edit** (3/9): the code change itself is wrong — model-bound, hardest
  to move at the harness layer.
- **structural multi-file gaps**: pylint-4661 needs `setup.cfg` and pylint-4604 needs
  `pylint/constants.py`; `codeindex` only indexes source extensions, so config/const files a
  fix depends on are invisible to `code_search`/`related_locations` by construction.

## Rigorous re-evaluation (N=3 multi-run) — the verify "gain" was noise

Built `bench/multirun_eval.py` (pass@k / consistency / Wilson CI / McNemar) after the 2026
"Lucky Pass" finding that pass@1 is unreliable. Ran collie_base vs collie_verify 3× on the 16:

| config | pass@1 | pass@3 | consistency (majority) | 95% CI |
|---|:--:|:--:|:--:|:--:|
| collie_base | 40.0% | 43.8% | 43.8% | [0.27, 0.55] |
| collie_verify | 44.4% | 50.0% | 37.5% | [0.31, 0.59] |

**McNemar (paired, majority-solve): 1 discordant instance, p = 1.000 — NOT significant.** The
verify port does not reliably beat baseline; the earlier "7 → 8" was a single-run lucky pass.
verify even has *lower* consistency (more variance, not more capability). Honest: the whole
cwd/verify/embedding thread moved SWE resolve by ~noise on this 16-instance DeepSeek setup.

**ULTRA (best-of-k) also fails to bank the pass@3 ceiling.** collie_verify's pass@3 is 50% vs
pass@1 44% — the right patch is often among 3 samples. `predict_collie_ultra` / `bench/ultra_select`
generate k candidates and select oracle-free (consensus → LLM judge). Measured on the 3 verify
runs: **ultra = 40%**, *below* single-run 44% and far under the 50% ceiling. Consensus fired on
only 6/16; the judge (10/16, all-distinct) can't tell a correct patch from a plausible one
without test access. Best-of-k's ceiling is real but **unrealizable without an oracle** here.

**Verdict:** SWE resolve on this model is noise/capability-bound, not harness-bound. collie's
real, significant wins over Hermes are **cost (~2× cheaper, leaner context) and speed (~1.6×)**,
not resolve (tied within noise). Crossing the resolve ceiling needs a stronger agentic model,
not more scaffolding. The multi-run harness is the durable deliverable — it stops future
single-run lucky passes from being reported as gains.

## Opus loop-upgrade attempt (2026-07) — nudge tweaks net-neutral; the gap is deeper

On claude-opus-4-8 (3-way, subscription): collie 9/15, Hermes 11/15, Claude Code 14/15. Opus
lifted everyone vs DeepSeek and cracked the multi-file "ceiling" cases — confirming those were
model-bound. On the SAME model the ranking is cc > hermes > collie, so the remaining gap is
LOOP quality (collie is cheapest at ~105k tok/inst vs Hermes' ~588k, but least capable).

Diagnosed collie's 4-instance gap to Hermes: 2 multi-file-coverage (pylint-4551 2/4, seaborn-3187
1/2) + 2 edit-correctness (seaborn-3069, sphinx-10435, right file wrong edit). A research pass
(web SWE-agent techniques + Hermes' `verification_stop`/`verification_evidence` source + collie
code) pinned the root cause: collie's coverage/verify drivers are all one-shot, boolean-latched,
evidence-free advisory nudges — fine on DeepSeek's ceiling, but they actively truncate a capable
model. Two changes were implemented (env-gated) and measured on Opus:

**① Verify evidence-gate** (reproduce→verify→repair invariant, bounded): targeted A/B looked like
+1 (flipped seaborn-3069), but the full-15 re-eval was **net-neutral** — it fixed seaborn-3069
AND broke pytest-10081 (the repair loop rewrote an already-correct edit into a wrong one when the
model misjudged its own reproduction). (requests-1724's apparent loss was eval flakiness —
byte-identical patch, non-deterministic test.) Forcing repair is double-edged. **Default OFF.**

**② Coverage-gated finish** (advisory, recompute related_siblings at finish): improved file
coverage (pylint-4551 2/4→3/4) with no flask over-edit, but **no resolve flip** — 3/4 still fails
(needs all 4), and seaborn-3187's missing file is cross-directory so the same-package score signal
can't reach it. Calibration confirmed the score threshold can't separate a needed sibling from an
incidental same-package file (pylint gold siblings 2.0–2.2 ≈ flask's unrelated siblings 2.1–2.4),
so it can only ever be advisory, not a hard gate. **Default OFF.**

**Verdict:** the top-ROI lean-nudge tweaks are **net-neutral** on Opus (±1 = noise on n=15, per
our own multirun methodology). collie's 9→11 gap to Hermes is not a nudge-tuning problem — it is
a deeper loop-architecture + exploration-depth difference, and that depth correlates with token
spend (collie 105k vs Hermes 588k/inst). **collie's leanness is simultaneously its cost moat and
its capability ceiling on a strong model.** Genuinely closing the gap means a richer loop that
spends more (deeper exploration/verification), trading away the efficiency lead — a real rework,
not a gated nudge. Kept both changes opt-in for study; SRC_EXT gained .cfg/.ini/.toml so config
files a fix needs (pylint-4661 setup.cfg) are visible to code_search.

---

## Executed-oracle study — best-of-N is DEAD, the gap is edit-correctness (2026-07-10, Opus)

Goal raised to **match cc (14/15)**. Multi-agent abstraction of cc + Hermes + SWE-agent literature
produced an **executed-oracle** design whose flagship lever was **generate-N + executed-oracle selection** (Aide
26→62% with N≤5). Before building the N× tier, tested its load-bearing necessary condition:
**does Opus have pass@1 < pass@N headroom on the instances collie fails but cc solves?**

**Gap to cc** (cc solves, collie's 9 misses): pylint-4551, pylint-4661, seaborn-3069,
seaborn-3187, sphinx-10435. Generated **3 diverse Opus samples** (flat temp=1.0, baseline config)
of each + flask-5014 control; evaluated all in Docker.

| instance | resolved | mode |
|---|---|---|
| flask-5014 (control) | **3/3** | OAuth-subscription generation pipeline verified sound |
| pylint-4551 | 2/3 | **variance** miss — collie solves it most runs; baseline 9 got unlucky |
| pylint-4661 | 0/3 | diverse-but-wrong |
| seaborn-3069 | 0/3 | diverse-but-wrong |
| seaborn-3187 | 0/3 | diverse-but-wrong |
| sphinx-10435 | 0/3 | diverse-but-wrong |

**pass@1 = pass@3 = 1/5 (only pylint-4551). Ensemble headroom ≈ 0.** This REPLICATES the DeepSeek
finding (mr_collie_verify r2≡r3 resolves, pass@3=ultra=6, recoverable=0) on Opus: for the hard gap
instances collie's diverse samples are **different WRONG answers — the correct patch is not in the
trajectory distribution**, so best-of-N + any oracle selects nothing. **best-of-N (executed-oracle Stage 3)
is capability-bound and should NOT ship as the gap-closer.** Its only real payoff is banking the
one variance instance (pylint-4551) via a cheap exec-selector → 9→10.

**Failure-mode diagnosis (the redirect).** collie is NOT mis-localizing — it hits the RIGHT files
almost every sample (pylint-4661: both config/__init__.py AND setup.cfg; sphinx-10435: latex.py
3/3). The failure is **right-file, WRONG-EDIT**: a plausible edit that doesn't produce the exact
correct behavior, finished without verifying. Example sphinx-10435: gold is a 3-line surgical
change (add `%`, trailer `[:-14]`→`[:-15]`, `'%'+CR+'}}'`); collie rewrites the block with the
right *concept* (%, CR, strip) but wrong *bytes* → test fails. Plus one true multi-file miss
(seaborn-3187 always edits scales.py, never utils.py).

**Why the old verify-gate (①) couldn't catch this:** collie's wrong edits **don't raise** — they
emit WRONG OUTPUT. A "reproduction ran without a traceback" gate passes them. The needed gate is
**output == expected** (assert-based), not no-error. The issue text itself often carries the
expected result (sphinx-10435 shows the target `\sphinxupquote{%`…). This is exactly the cc/Hermes
mechanism collie lacks: reproduce → compare to expected → iterate the EDIT until the assert holds.

**Redirected executed-oracle plan:** drop the N× best-of-N tier. Two live levers, both single-loop:
- (cheap, +1) exec-selector variance-reducer to reliably bank pylint-4551.
- (**the real lever**) **assert-against-expected in-loop verification** — gate finish on an
  executed reproduction that checks the CORRECT output (not no-error), driving edit-iteration.
  Distinct from failed gate ①: ① force-repaired on a weak no-traceback signal and wrecked correct
  edits; this iterates the edit on a strong output-mismatch signal and leaves a passing edit alone.
  Next experiment: implement it and test whether it flips any of the 3 right-file/wrong-edit
  instances (pylint-4661, seaborn-3069, sphinx-10435) on Opus. seaborn-3187 additionally needs
  multi-file completion.

### assert-verify RESULT — the first NON-net-neutral lever (2026-07-10, Opus)

Implemented `COLLIE_ASSERT_VERIFY=1` (harness/loop.py + swe.py): gate finish on an executed
`assert actual==expected`; `require_assert` closes the print-only hole; repair nudge says
"reconsider the FIX" not "re-run". Validated on the gap + regression.

**Gap flips (2 samples), baseline was 0/3 for each:**
- **seaborn-3069: RESOLVED in BOTH samples** (patch 934→~2340, near gold 2235) — a genuine
  capability-gap instance best-of-N could never reach, solved by verification depth.
- **seaborn-3187: RESOLVED (s2)** — the multi-file miss; assert-driven iteration grew the patch
  1017→1341 and it finally also edits utils.py.
- pylint-4661, sphinx-10435 still fail (sphinx even went EMPTY once — assert loop non-convergence).
- No regression on the control/variance (flask 2/2, pylint-4551 recovered s2).

**Adopt-gate regression test** — assert-verify on the 8 instances opus_collie already solves:
kept 6/8, "regressed" requests-1724 + requests-1766. **BUT both regressions are BYTE-IDENTICAL
to the baseline patch (532==532, 453==453)** — assert-verify did not change them; the resolved→
failed flip is SWE-bench eval flakiness on requests' non-deterministic tests (requests-1724 was
already flagged flaky earlier in this audit), NOT an assert-caused break. So the true regression
count is **0**.

**True net: 9 baseline + 2 real flips − 0 real regressions = ~11 (matches Hermes).** This is the
FIRST lever in the whole study that is not net-neutral — the difference from the net-neutral
COLLIE_VERIFY_GATE is real and comes from (a) assert-against-expected (a wrong edit fails loudly)
vs no-traceback (a wrong edit that prints passes), and (b) it flips genuine capability-gap
instances, not just variance ones. Caveat: single-sample-per-instance; needs a clean multirun
full-15 (pass@1/consistency/Wilson/McNemar) to fix the headline and confirm ≥11 with CI. The 2
still-failing gap instances (pylint-4661, sphinx-10435) are the next target toward cc (14).

**Full-15 confirmation (definitive, same-env).** Assembled `preds/opus_collie_assert.jsonl`
(one assert-verify sample per instance) and evaluated:
- **collie_assert = 10/15 raw.** gained {pylint-4551, seaborn-3069, seaborn-3187}; "lost"
  {requests-1724, requests-1766}.
- The 2 "losses" are BYTE-IDENTICAL to the baseline patch AND — the clincher — re-evaluating the
  **baseline** opus_collie for those two in the CURRENT env ALSO fails them (✓=0/2, identical
  patch). So requests-1724/1766 are **environment/time-dependent tests that fail for BOTH
  harnesses equally right now** — a wash, not an assert-caused regression.
- **Apples-to-apples same-env: collie_assert 10 vs baseline 7 → +3, 0 real regressions.**
  Historical-credit framing: 12 vs 9. Either way **collie ≈ 12 > Hermes 11**, ~half the remaining
  gap to cc (14) closed. seaborn-3069 is a robust flip (2/2 samples); seaborn-3187 1/2; pylint-4551
  is the variance instance now reliably banked.

**Bottom line of the executed-oracle study:** best-of-N is capability-bound (dead as a gap-closer); the gap
was edit-correctness, not localization; and **assert-against-expected in-loop verification is the
first lever that actually moves collie (9→12) without net regression** — because it converts the
model's correctness judgment into an executed, gate-checkable signal, catching exactly the
right-file/wrong-edit failures that a no-traceback check waved through. `COLLIE_ASSERT_VERIFY=1`
(opt-in; recommend promoting to default after a multirun full-15 with CI). Remaining toward cc:
pylint-4661 + sphinx-10435 (both localize right; assert loop not yet converging — sphinx even
went empty once, so the repair bound / assert-authoring needs work there).

---

## best-of-N campaign — assert-verify GENERALIZES (validated); exec-oracle selection does NOT realize the headroom (2026-07-11, Opus)

Built two tool upgrades (A1 lint-in-edit: `ast.parse`-reject syntax-breaking edits; A2 line-numbered `read_file` + offset/limit paging — fixed a dead-end where the truncation msg promised `offset` the schema lacked) + `bench/exec_select.py` (executed-repro oracle). Then ran a clean multirun on a SECOND fresh holdout (holdout-2 = 12 unseen Verified instances, distinct from the diagnosis-16 and fresh-12), single-stream (3-concurrent collie rate-limits the flat subscription -> empty patches; keep concurrency <=2).

**assert-verify multirun (holdout-2, improved tools, N=3):**
- pass@1 per-sample = [10, 10, 10] -> ROCK-STABLE 10/12 (83%).
- consistency (all-3 solve) = 9/12; pass@3 (union) = 11/12; only sympy-21596 never solved.
- variance instances: sympy-14248 (2/3), matplotlib-22865 (1/3) -> real pass@k > pass@1 headroom (+1), UNLIKE DeepSeek where pass@k==pass@1.
- VERDICT: assert-verify generalizes to a second unseen holdout, stable, no regression. This is the validated win.

**exec-oracle best-of-N (the hoped-for +1) — FALSIFIED as a reliable selector:**
- Ran `exec_select.select_n` on the 2 variance instances (author repro -> validate fails on base -> run each of the 3 samples -> pick the passer, prefer-first on tie).
- sympy-14248 (truth s1✓ s2✗ s3✓): repro passed on NONE (too weak to validate even correct patches) -> tie -> picked s1 -> RESOLVED by luck, not discrimination.
- matplotlib-22865 (truth s1✗ s2✓ s3✗): repro was ACTIVELY WRONG — passed on non-resolving s3, failed on resolving s2 -> picked s3 -> WRONG. Matplotlib (rendering/API bug) is exactly where a standalone self-authored repro misjudges expected behavior.
- Realized best-of-N = 9 common + 1 lucky + 1 wrong = 10/12 = single-run. Did NOT realize the 11/12 pass@3 ceiling.

**Bottom line of the whole arc:** the ONE solid, generalizing, non-net-neutral lever is **assert-verify** (in-loop executed assertion): 9->12 on diagnosis-15, +1 on fresh-12, stable 10/12 on holdout-2, ties Hermes at ~78% pooled. Selection over candidates — whether oracle-free (LLM judge, 40%<44%) OR executed-repro-oracle (this test) — does NOT reliably add on top, because self-authored repros are too weak/wrong on subtle bugs (the sphinx/matplotlib class). exec_select stays structurally safe (prefer-base-on-tie never worse than the default) but its upside doesn't materialize for best-of-N among plausible variance samples. A1/A2 shipped (holdout ran with them, no regression). Recommendation: keep assert-verify as the flagship (opt-in; promote to default only after a clean baseline-vs-assert multirun on the same holdout, which this campaign did NOT run — the honest gap). Drop best-of-N-via-self-repro as a reliability tier.

### honest-gap CLOSED — baseline-vs-assert on the SAME holdout-2 (2026-07-12, Opus)
Ran the missing control: **collie baseline (`COLLIE_ASSERT_VERIFY=0`), holdout-2, same 12 ids, same A1/A2 tools, N=3.**
- baseline pass@1 per-sample = **[9, 9, 10]** (mean 9.33, mode 9), vs assert-verify's rock-stable **[10, 10, 10]** on the identical set. assert is +0.67 mean pass@1 AND variance-free where baseline oscillates 9–10.
- baseline **pass@3 (union) = 10/12**; assert **pass@3 (union) = 11/12** — assert's union STRICTLY DOMINATES. The one extra instance assert resolves and baseline never does (0/3 samples) is **sympy-14248** — exactly the pass@k variance instance (assert 2/3). Both unions include matplotlib-22865; consistency (all-3) is 9 for both.
- So on the same fresh, never-touched holdout, assert-verify is **+1 pass@3 and +≥1 pass@1 over its own baseline** — SAME direction as diagnosis-15 (9->12) and fresh-12 (+1). Three independent unseen sets, delta always ≥0, never a regression.
- Honest sizing: +1 at n=12 is inside binomial noise for any *single* set; the promotable signal is the **consistent direction across three disjoint holdouts** + the strict union-dominance here, not any one delta. exec best-of-N, by contrast, was net-zero AND mis-selected (matplotlib) on this same set.
- **DECISION (executed): promote assert-verify to default-on** — `swe.py` now reads `COLLIE_ASSERT_VERIFY` defaulting to `1`; `=0/false/off` disables (escape hatch). Same-model, same-tools, directionally-consistent-across-three-holdouts positive with strict pass@3 union-dominance and zero regression. best-of-N-via-self-repro stays **shelved** (exec_select kept only for its structural prefer-base-on-tie safety, not as a gap-closer).

---

## verify-loop v2→v3 saga — over-verification causes SCOPE CREEP (2026-07-17, Opus, rebench 2026_03)

**Context.** rebench 2026_03 (contamination-free) baseline: collie 49/110 = 44.5% (vs SWE-bench
Verified's inflated ~75%; official SWE-rebench leaderboard has Opus 4.6 at 65.3% best-of-5). A
60/61 adversarially-verified failure forensics found **95% genuine capability gaps** (only 3
not-our-fault vs Verified's 5/6) — the top clusters: near_miss(18), wrong_edit_right_file(15),
multi_file_miss(10), p2p_regression(7). Highest-ROI lever proposed: a bidirectional verify gate.

**v2 (the over-correction).** Implemented three things: (a) bidirectional verify nudge (assert
fix + edge cases + neighbor-still-works), (b) hardened finish-gate (never release on "never
asserted" — block to max_turns, bash-only tool restriction), (c) exact-API rule (implement the
issue's DECLARED name verbatim — hats-648 renamed skymap_coverage→compute_skymap_coverage → 0/15;
with the rule → 14/15). Plus two real bug fixes: API-blip→empty-patch now retries (output_tokens==0
⇒ not-completed), and the rebench eval loop no longer spins forever on empty-patch instances.

**v2 measured NET-NEGATIVE.** Partial full run (n=40 paired vs v1): rescued 2/18 v1-failures (11%)
but regressed 8/22 v1-solved (36%). Net -6. A **controlled experiment** (old-harness resample of
the 8 regressions) split them exactly: **4/8 genuine v2 damage, 4/8 resampling variance.**

**Root cause (found by studying trajectories, not counting).** copier-2646: old harness solved it
with a focused **2-file** fix (_template.py, _vcs.py); v2 produced a **3-file** patch — it added
copier/__main__.py (a SIGTERM handler that referenced a nonexistent helper) and broke the fix. The
mechanism: in the bare checkout the module often isn't importable (deps absent), so the model can't
run a clean passing assertion; the hardened "never-release" gate + the "assert edges / neighbors"
push then drove it to keep EDITING (bigger diffs, extra files) to satisfy the broader verification
— **scope creep that wrecks an already-correct fix.** This is exactly the earlier audit's warning
("forcing repair is double-edged: it rescues a wrong edit but can wreck a right one"), rediscovered.

**v3 (keep the wins, drop the harm).** Reverted loop.py (gate hardening) and the bidirectional
verify nudge to the pre-v2 single fix-assert. KEPT the proven wins: exact-API rule, env-mismatch
guard, both bug fixes, plus a new tight anti-scope-creep line in the SWE prompt ("keep the change
focused — do not expand it to unrelated files or cases the issue didn't ask for"). Validation on
the 4 damage instances: copier back to the **exact 2-file** set of the resolved version (scope
creep eliminated), 2/4 re-resolved (the other 2 fail on resample-variance implementation, same
focused files). **Lesson: a verify LOOP must confirm the fix WITHOUT prompting broader editing —
the leverage is in checking, not in doing more. Lean is partly a correctness property, not only a
cost one.** Net measurement of small harness deltas is dominated by temp=1.0 resampling variance
(~28-36% of solved instances flip on any resample); isolating a small effect needs a controlled
old-vs-new A/B, not a single-arm run.

## Hermes vs collie on rebench 2026_03 (2026-07-17, Opus 4.8, both on flat subscription)

Filled the gap the Verified-only 3-way left. Same 30 rebench 2026_03 instances, both agents on
claude-opus-4-8 via the same subscription-backed provider for both agents — verified live on the
same model. Result:

  collie 16/30 (53%)   Hermes 19/30 (63%)   net Hermes +3 (+10pp), McNemar p=0.45 (n=30, NS)
  discordant: Hermes-only 5 (tox-3904, pygeoapi-2338, mempalace-1004, astropy-19438, pdm-3759),
              collie-only 2 (copier-2646, opensandbox-816)

Same direction as Verified (collie 9 / Hermes 11 / 15): Hermes' deeper exploration buys a modest,
consistent edge on fresh tasks too — but at ~588k tok/instance vs collie's ~105k, i.e. collie
reaches ~84% of Hermes' resolve at ~1/6 the tokens. collie's position is confirmed: not the
strongest, the leanest. Closing the gap without abandoning leanness is the real target — precise
fixes (exact-API) add points; over-verification (v2's scope creep) subtracts them.
