# Evidence audit disposition

A separate native Claude Code review recomputed the reported statistics, checked all
candidate hashes and reconstructed eight source trees, then independently regraded a
55-candidate subset. Its checks used no additional model calls. The root controller
had separately regraded all 166 candidates. Neither source-review job is counted as a
coding benchmark attempt.

Corrections made after the review:

- Exact regression counts are accompanied by original logs; final integration gets its
  own source-pinned full regression and installed-wheel verification.
- The report discloses the second-window HTTP retry override and separates runtime
  inventories: per-native-experiment Windows hashes versus post-run Linux image checks.
- Persistent-session correctness and clean completion are reported separately. Full
  history yielded five correct artifacts but only two clean completions before the
  repair change. A successful patch does not erase a delivery failure.
- Native call counters carry units. Collie's physical requests are not Claude Code's
  native turn counter. Native host-contract and cleanup failures remain product outcomes;
  normalized eligibility additionally requires the shared request ledger.
- Error-cause labels are explicitly diagnostic, partly derived from error text. They
  are not used to authorize recovery. Unknown categories remain counted as failures.
- Reminder-cell ranges and small sample size accompany the cache medians. The single
  twenty-turn reuse example is not a stable ranking or subscription-cost estimate.
- The bundle checker detects extra or missing files as well as hash mismatches, while
  ignoring generated Python bytecode. Source reconstruction uses a temporary Git index
  and object database, reading existing source objects through an alternate.
- Planned-versus-received reconciliation now covers every represented coding suite,
  including the first window.

The frozen evaluator helper's self_check() validates its own original two built-in
tasks. The exported verifier intentionally uses materialize_task() with all six frozen
task definitions (four base tasks plus two coverage variants), testing each baseline
and reference solution. The immutable helper was not altered to imply that its separate
self_check() was run on the new tasks.

The benchmark still has only two base tasks per window and two or three repetitions
per cell. Concurrent source reviews affect timing, and adapted harnesses do not model
their full native experience. These results support the specific product fixes and
continued persistent-transport experiments; they do not establish a market leaderboard.

A later Linux check mounted the source repository read-only and found that git apply
needs to write reconstructed objects even with a temporary index. The initial run failed
before candidate regrading. The verifier now directs those writes to its temporary
object database, preserving read-only source access; original failure and final Linux
results are separate artifacts. This tooling change does not change any candidate or grade.
The exact Linux-executed checker is archived as `snapshot/verified-linux-checker.py`
and matches its recorded SHA-256. The final checker also narrows the manifest-file
exception to the root manifest, so a nested file with the same name is not ignored.
