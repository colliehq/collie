# Cross-harness benchmark protocol

Collie keeps two leaderboards because they answer different questions.

- **Controlled track:** every harness uses one frozen model endpoint, task list, grader,
  container, prompt, tool contract, dependency lock, sandbox, context window, retry policy,
  concurrency policy, network policy, and aggregate root-plus-descendant budget. This is the
  only track allowed to estimate a harness effect.
- **Product track:** each product uses a separately frozen native model configuration. This
  measures the product system a user can buy, not an isolated harness effect.

The protocol is fail-closed. A result is not publishable if the plan differs from the canonical
manifest expansion, a run is missing, a pin is mutable, a budget is exceeded, aggregate usage is
not independently receipted, or any trace, patch, grader, or usage artifact fails validation.
Inferential statistics are withheld whenever any evidence error exists.

## Freeze a controlled manifest

Revision fields accept only a full 40/64-character commit or object ID, or a
`sha256:<64 lowercase hex>` digest. Tags, branches, semver ranges, `HEAD`, and names such as
`stable` are rejected. Dated provider model snapshots are accepted where providers do not expose
weights digests. Every placeholder below must be replaced before validation.

```json
{
  "schema_version": 1,
  "name": "collie-vs-peer-controlled",
  "track": "controlled",
  "dataset": {
    "name": "SWE-bench_Pro",
    "revision": "<full dataset commit>",
    "grader_revision": "<full grader commit>",
    "container_digest": "sha256:<64 lowercase hex>",
    "tasks_file": "task-ids.txt",
    "tasks_sha256": "<64 lowercase hex>"
  },
  "model": {
    "provider": "<provider>",
    "id": "<exact model id>",
    "snapshot": "model-build-2026-08-11",
    "endpoint": "<exact endpoint or route>",
    "reasoning_effort": "high",
    "temperature": 0,
    "top_p": 1
  },
  "controls": {
    "prompt_file": "controls/prompt.json",
    "prompt_sha256": "<64 lowercase hex>",
    "tool_contract_file": "controls/tools.json",
    "tool_contract_sha256": "<64 lowercase hex>",
    "dependency_lock_file": "controls/dependencies.lock",
    "dependency_lock_sha256": "<64 lowercase hex>",
    "sandbox_policy_file": "controls/sandbox.json",
    "sandbox_policy_sha256": "<64 lowercase hex>",
    "retry_policy_file": "controls/retries.json",
    "retry_policy_sha256": "<64 lowercase hex>",
    "concurrency_policy_file": "controls/concurrency.json",
    "concurrency_policy_sha256": "<64 lowercase hex>",
    "environment_digest": "sha256:<64 lowercase hex>",
    "context_window_tokens": 131072
  },
  "budget": {
    "scope": "root_plus_descendants",
    "wall_seconds": 1800,
    "model_calls": 150,
    "turns": 150,
    "input_tokens": 1000000,
    "output_tokens": 100000,
    "cache_tokens": 1000000,
    "cost_usd": 10
  },
  "execution": {
    "repetitions": 3,
    "seeds": [101, 202, 303],
    "pass_at": 1,
    "attempts_per_task": 1,
    "network": "disabled",
    "memory": "fresh_per_run",
    "refine": false,
    "native_prompt_extensions": false,
    "schedule": "counterbalanced_latin_square",
    "schedule_seed": 17,
    "max_parallel_runs": 1
  },
  "harnesses": [
    {
      "name": "collie",
      "revision": "<full Collie commit>",
      "command": ["collie", "run"],
      "trace_format": "jsonl",
      "model_source": "manifest",
      "seed_source": "manifest",
      "budget_source": "manifest",
      "usage_source": "independent-meter",
      "usage_meter_revision": "<full meter commit>",
      "usage_receipt_format": "collie-benchmark-usage-v1",
      "includes_subagents": true
    },
    {
      "name": "peer",
      "revision": "<full peer commit>",
      "command": ["peer", "run"],
      "trace_format": "jsonl",
      "model_source": "manifest",
      "seed_source": "manifest",
      "budget_source": "manifest",
      "usage_source": "independent-meter",
      "usage_meter_revision": "<same full meter commit>",
      "usage_receipt_format": "collie-benchmark-usage-v1",
      "includes_subagents": true
    }
  ]
}
```

All control files and the task list must be relative files inside the manifest directory and must
match their hashes. The task file contains one exact task ID per line and at least two tasks. Never
call a sample “Verified-mini” without publishing that file and digest; several incompatible mini
subsets exist.

In a product manifest, omit the global `model`. Each harness instead supplies a complete frozen
`model` object and sets `model_source` to `native_manifest`. Product harnesses may use different
models, but their exact provider, ID, snapshot, endpoint, reasoning effort, temperature, and top-p
are copied into their usage receipts. Both tracks still require measurable root-plus-descendant
usage and the shared manifest budget.

Validate and expand the run matrix without spending model quota:

```bash
python -m harness.benchmark_protocol validate manifest.json
python -m harness.benchmark_protocol plan manifest.json --out evidence/plan.jsonl
```

The canonical plan rotates harness order across task/seed cells with a deterministic Latin-square
schedule. Runs are launched serially, and each result supplies `started_at_unix_ms` and
`finished_at_unix_ms`; summarization rejects both out-of-order and overlapping intervals in
`schedule_index` order. `harness_position` makes position
balance auditable. The frozen concurrency policy separately governs parallelism inside each run.
The CLI prints the maximum authorized spend as
`tasks × repetitions × harnesses × budget.cost_usd`.

## Result evidence

The executor writes one result per planned `run_id`. Identity fields, including schedule fields,
must exactly match the plan. Each row also contains:

- the exact harness revision and complete frozen model object;
- `attempt: 1`, `started_at_unix_ms`, `finished_at_unix_ms`, and a boolean `resolved`
  from the frozen grader;
- aggregate `usage` with `scope: root_plus_descendants` and every budget field;
- the pinned usage source and meter revision;
- relative paths and SHA-256 hashes for trace, patch, grader receipt, and usage receipt.

The standard usage receipt is a JSON object shaped as follows:

```json
{
  "format": "collie-benchmark-usage-v1",
  "schema_version": 1,
  "manifest_sha256": "<manifest digest>",
  "run_id": "<planned run id>",
  "task_id": "<task id>",
  "schedule_index": 1,
  "started_at_unix_ms": 1786406400000,
  "finished_at_unix_ms": 1786406410000,
  "harness": "collie",
  "harness_revision": "<frozen revision>",
  "model": {"provider": "...", "id": "...", "snapshot": "..."},
  "meter": {"source": "independent-meter", "revision": "<frozen revision>"},
  "trace_sha256": "<trace digest>",
  "patch_sha256": "<patch digest>",
  "scope": "root_plus_descendants",
  "includes_subagents": true,
  "usage": {"wall_seconds": 1, "model_calls": 1, "turns": 1,
            "input_tokens": 1, "output_tokens": 1, "cache_tokens": 0,
            "cost_usd": 0.01, "scope": "root_plus_descendants"}
}
```

`usage.wall_seconds` is aggregate active time across the root and descendants; it may exceed the
outer execution interval when subagents overlap. The timestamp interval is reported separately as
elapsed latency and is used to prove serial scheduling. The full `model` and `usage` objects must
exactly equal the result and manifest values; abbreviated
objects in the example are illustrative only. The `collie-benchmark-grader-v1` receipt separately
binds the manifest, run, dataset revision, task, patch digest, grader revision, container digest,
and verdict.

Artifacts must use unique relative files inside the evidence directory. Hard-linked or reused
files are rejected. Trace files are non-empty JSONL objects and are limited to 64 MiB. Patches are
UTF-8 `.patch`/`.diff` files, limited to 16 MiB; a non-empty patch must contain a git diff and a
change record, including valid mode-only changes. Grader and usage receipts are JSON objects
limited to 2 MiB. All JSON forbids duplicate object keys,
`NaN` and infinities.

```bash
python -m harness.benchmark_protocol summarize manifest.json \
  --plan evidence/plan.jsonl --results evidence/results.jsonl \
  --out evidence/report.json
```

## Statistical contract

Repeated seeds from the same task are correlated. The report therefore uses task—not task/seed—as
the inferential unit:

- pass@1 intervals use a deterministic 10,000-replicate whole-task percentile bootstrap;
- paired harness differences use task-level seed-averaged effects and a two-sided sign-flip test
  (exact through 20 nonzero task clusters, then 65,536 deterministic Monte Carlo draws);
- all predeclared harness-pair p-values receive Holm family-wise adjustment;
- raw task/seed contingency counts remain descriptive only.

The interval generalizes only to the empirical task population represented by the frozen list.
The paired sign-flip test assumes task-level differences are exchangeable with symmetric signs
under the null. Repetitions improve each task-rate estimate; they do not turn seeds into independent
task samples. These assumptions and the exact/Monte Carlo method are recorded in the report.

Best@k, warm cross-task memory, and continual `/refine` experiments belong in separate manifests
and must never be merged into the controlled pass@1 table.

## Launch and trust gate

Do not launch if any harness cannot enforce the same aggregate call/token/cost limits, meter its
root and every descendant, honor the schedule, or emit the standard usage receipt. Never substitute
zeros for unavailable usage.

Hashes prove bundle integrity, not that an untrusted executor told the truth. For a public claim,
run the grader and usage meter outside the compared harnesses, preserve provider request IDs or
equivalent audit records, sign or timestamp the evidence bundle, and publish the manifest, control
files, plan, results, artifacts, and report together.
