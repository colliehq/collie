# Harness audit backlog (round 12, multi-agent)

A read-only multi-agent audit (7 subsystems → adversarial verify → ROI rank) surfaced
**32 confirmed problems**. `[x]` = fixed; `[ ]` = queued. Do-now 🟢 · high 🟡 · cleanup ⚪.

## Fixed this round (critical)
- [x] **#1 🟢 did_edit set on FAILED edits** (loop.py) — `edit_file` returns `ERROR:
  old_string not found` *without writing*; DeepSeek mis-quotes constantly, and flipping
  `did_edit=True` disabled every convergence guard → the core empty-patch bug. Now gated
  on `not out.startswith("ERROR")`.
- [x] **#3 🟢 malformed tool call aborts the whole run** (loop.py) — `tool.run` now wrapped
  in try/except → a missing required arg becomes a recoverable `ERROR:` turn.
- [x] **#11 🟡 comparison-agent dispatch bug** (swe_predict_one.py) — the `else` branch called
  `predict_claude_code` for *any* non-collie agent, so a comparison agent's predictions were
  actually produced by the reference agent, not the intended one. This invalidated the
  same-model comparison. Now dispatches via `AGENTS[agent]`. **Re-measuring the true
  same-model numbers; docs to be corrected.**

## Queued — next batch (prediction-path correctness)
- [ ] #2 🟢 bash hides exit code + head-truncates (drops the error tail) — prepend `[exit N]`,
  keep head+tail. *Biggest turn-waster: the model can't tell pass from fail.*
- [ ] #4 🟡 transient API error (429/401) returned as the final answer AND written to durable
  memory as a "fact" — add a `stop_reason="error"` sentinel; set `res.error`, skip remember.
- [ ] #5 🟡 killed/timed-out child cached as an empty patch, never retried — add a `status`
  field; exclude non-ok from the resumable `done` set. *Transient OOM → permanent 0-score.*
- [ ] #6 🟡 `git add -u` drops NEW source files an agent creates — `git add -A` + pathspec
  exclude `venv/__pycache__/*.egg-info` + size cap. *(Refines this round's `-u` fix.)*
- [ ] #7 🟡 no `max_tokens` + `finish_reason` unchecked → truncated tool args become empty
  edits — send max_tokens; on `finish_reason=="length"` feed a "cut off" message back.

## Queued — retrieval & robustness
- [ ] #8 🟡 memory dense arm never abstains (`s>0` before RRF), ignores `embed_model`, LIKE
  uses only the first query token — three one-liners restore retrieval sanity.
- [ ] #9 🟡 code index drops whole files at cap 3000 (os.walk order), missing exts
  (.mjs/.vue/.pyi/.lua), over-broad test/doc SKIP_DIR — per-file chunk budget; narrow skip.
- [ ] #10 🟡 silent truncation bundle: read_file@40k, glob unsorted@200, grep leading-dash,
  edit strict-utf8 — add markers; sort glob; `--` before pattern; `errors='replace'`.

## Queued — cleanup ⚪
- [ ] #12 only first of CLAUDE.md/AGENTS.md/.mh.md used · #13 cached-token double count ·
  #14 prefix_ceiling never enforced + unbounded history · #15 no HTTP retry/backoff ·
  #16 `tools=[]`+tool_choice 400s some endpoints · #17 edit no-op reports success ·
  #18 prefetch ungated every turn + full cosine scan · #19 EDIT_FORCE_NUDGE no once-guard ·
  #20 density heuristic demotes Rust/C lines as prose.

---

## Status: 14 / 32 fixed (all high + medium severity)

**Fixed this session** (commits in CHANGELOG v0.14.0): #1 did_edit-on-failed-edit · #2 bash
exit-code+tail · #3 tool.run guard · #4 API-error sentinel · #5 timeout-not-cached · #6 git
add -A+exclude · #7 max_tokens · #8 memory dense-abstain+LIKE · #9 index per-file budget ·
#11 comparison-agent dispatch · #14 history bound · #15 retry/backoff · #16 empty-tools · #17 no-op
edit reject · #19 nudge once-guard.

**Still open (low severity):** #10 remaining truncation markers · #12 CLAUDE.md merge ·
#13 cached-token count · #18 prefetch gate · #20 density word-boundary.

*The `[ ]` boxes above predate this session; this footer is authoritative.*

*Method: each finding was proposed by a subsystem auditor and had to survive an adversarial
verifier (default-refute) to be listed. Full run: workflow `collie-harness-audit`.*
