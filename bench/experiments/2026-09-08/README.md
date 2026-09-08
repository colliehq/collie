# Collie 0.26 subscription experiments

This directory contains reviewed, local experiment evidence. It is not a leaderboard
for the entire agent market. Read `validation.json` for how many subscription windows
were completed when this particular snapshot was exported.

Run the independent checks without a model, account, network connection or Python
package installation:

```sh
python verify.py
python verify.py --regrade-all
python verify.py --source-repo /path/to/collie
```

The second command needs Git. It reconstructs every candidate from the frozen initial
files and applies the stored patch before running the external grader in a fresh
directory. All frozen task definitions also have baseline-fails and reference-solution-passes
checks. Correctness here means passing these specified local tasks; it does not prove
general coding ability or repair a failed run's missing execution evidence.

`SHA256SUMS.json` covers the reviewed bundle. `experiments.json` contains source pins,
task sets, response/context/session overrides and script hashes. Second-window native
manifests attest their Windows runtime hashes; Linux normalized image inventories are
separate post-run checks under `runtime-audit/`, not per-call historical attestations.
Candidate patches remain separate from the hidden grading code during model
execution. The exported tasks include graders so the offline results can be checked.

The new experiment controller snapshots under `snapshot/` retain their original local
paths. They document the exact runs; they are not a portable model-rerun entry point.
The baseline transport/container setup and its pinned OSS adapters are preserved in
the adjacent `2026-09-07` experiment bundle. Re-running with models requires configuring
that environment, explicitly authorizing subscription spend and registering fresh
output paths/deadlines. The offline checker above is portable and spends nothing.

Interpret the tracks separately:

- Native product comparisons use Collie's own loop versus Claude Code with a restricted
  common file-tool set. A native 48-turn cap is not an attestation of 48 physical model
  requests. They do not share an OS filesystem sandbox.
- The normalized track adapts Collie, Pi, Hermes and Prime to one official SDK sidecar
  and its physical request ledger. This compares adapted behavior, not the products'
  complete native experience. Failed and incomplete ledger evidence remains visible.
- Persistent-session and tool-history treatments are experimental checkouts. Their
  cleanup receipts are part of the outcome. Host context rewrites require session
  resets, even if that reduces cache reuse.
  The two transports encode the selected host history differently: a flattened
  prompt versus native conversation turns. This is not a cache-only intervention.
- The 36-call public-code dialogue measured cache reuse without host tools. Its cache
  counters and API-equivalent token values are not subscription billing receipts and
  cannot stand in for coding quality or end-to-end latency.

Source repairs retain their original failed attempts and get new cohorts. Do not pool
all versions into one product success rate. First-window native CLI and SDK runtime
versions differed; see `runtime-audit/first-window.json`. That inventory was made after
the runs and is not per-call attestation. The second-window design unifies the runtimes.

`second-window-plan.json` is the pre-run design; version 1 is preserved separately.
The revision adds a context-history ablation prompted by a zero-model reproduction of
frequent session resets. `product-validation/` distinguishes full source regression,
actual container checks with scripted model responses, and installed-wheel UI checks.
Initial failing validations are retained beside the later fixes.

`source-patches.json` and `source-patches/` reconstruct every Collie source tree used
in the coding experiment manifests from the recorded product base commit, including experimental branches that
are not ancestors of the final product. The optional `--source-repo` check uses a
temporary Git index and object database, applies each full-tree patch and checks its
resulting tree ID. The source object database is an alternate for read-only access;
the checkout, original index and original object database stay untouched. It needs
that base commit locally.

The recovery and response-reminder comparisons were registered after observing the
initial second-window results. They are additional exploratory cohorts, with their
own plans and source pins; they do not replace the original failures.

`planned-received.json` reconciles every represented coding suite. `evidence-review.md`
records the independent audit and its corrections. The 45-read dependent chain,
native start/resume effort checks, UI screenshots and storage measurements are product
validation, separately labeled and excluded from coding benchmark counts. PNG images
are included in the same byte-level hash manifest as the text evidence.
