# Collie cross-harness benchmark protocol

## Scope and claim boundary

Prime Agent is a **methodological reference, not a benchmark arm**. Its useful contribution here
is the separation of taskset, harness, and runtime, plus its emphasis on long-running agents and
external sandboxes. The first comparison roster is Collie, Claude Code, Codex CLI, and Pi. If an
arm cannot satisfy the authentication, isolation, metering, or reproducibility gate, report it as
`not admitted`; do not score it as a loss and do not silently replace it with Prime Agent.

Collie keeps two completely separate leaderboards because they answer different questions:

| Track | What must be frozen | Authentication and billing | Claim allowed |
| --- | --- | --- | --- |
| **Same-model controlled** (`track: controlled`) | One immutable model snapshot and endpoint, reasoning/sampling settings, task list, grader, prompt, tool contract, dependency lock, runtime image, sandbox, context window, retry/concurrency/network policy, and aggregate root-plus-descendant budget | A metered API or evaluation gateway is normally required. Obtain an explicit spend budget before launch. | An estimate of the **harness effect** under the frozen controls. |
| **Subscription-native product** (`track: product`) | Each product's pinned official CLI, documented native subscription route, native model configuration, and the shared task/runtime/grading controls | No copied OAuth tokens, spoofed client identity, or hidden API-key fallback. Record subscription-plan evidence, billing overrides, and actual versus API-equivalent cost separately. | A comparison of the **products as available to a subscriber**, not an isolated harness effect. |

Never merge these tracks into one table, headline, or statistical test. A same-model claim is
invalid if one arm uses a nearby model, a different snapshot, a different reasoning setting, or a
provider-specific endpoint that changes model behavior. A subscription-native result is invalid
if it is described as a harness-only result merely because two products happen to select the same
model family.

For subscription-native artifacts, `actual_marginal_charge_usd` is a post-run billing observation,
`expected_marginal_charge_usd` is a preflight prediction, and `api_equivalent_cost_usd` is an
efficiency estimate. They are not interchangeable. The current local smoke records the latter two:
its guard proves that known metered fallbacks are disabled, but without a post-run account check it
does not claim an observed charge of zero. A flat subscription also does not remove plan limits or
authorize an unbounded run. In the formal manifest, `budget.cost_usd` remains a normalized
fail-closed cap; the evidence bundle must state which accounting basis it uses.

The protocol is fail-closed. A result is not publishable if the plan differs from the canonical
manifest expansion, a run is missing, a pin is mutable, a budget is exceeded, aggregate usage is
not independently receipted, or any trace, patch, grader, or usage artifact fails validation.
Inferential statistics are withheld whenever any evidence error exists.

## Current evidence is plumbing-only

The current one-file `bench/results/subscription-smoke.json` artifact is deliberately marked
`scope: adapter_smoke`, `claim: plumbing_only`, and `publishable: false`. It checks only that an
official subscription route launches, an adapter can leave a patch, the patch can be collected,
and an external deterministic check can grade it. One trivial file, one attempt, and one hidden
assertion set provide no meaningful coverage of planning, repository search, test execution,
long-context behavior, recovery, or autonomy. Its rows must never be converted into a capability
score or ranking. The runner currently supports Collie, Claude Code, and Codex, but the saved
artifact contains rows for Collie and Claude Code only; neither Codex nor Pi is validated by it.

The exploratory artifact `bench/results/paired-subscription-product.json` is also invalid for
ranking. It contains one SWE-bench Pro instance and no arm produced a patch: Collie hit a Windows
path-length error, Claude Code stopped at an edit/permission boundary, and Codex encountered a
read-only sandbox/tool failure. These are runner and adapter failures, not evidence of relative
coding ability. The artifact stays `publishable: false` and must not be cited as a win, loss,
leaderboard position, or SWE-bench score. A replacement run starts only after every arm passes
the same isolated repository-write conformance test.

## Native execution and authentication admission

The product track invokes each product through its documented automation surface and its own
credential store:

- Claude Code uses `claude -p` with JSON or stream-JSON output. The preflight must prove that the
  selected credential is the Claude.ai subscription login; environment API keys and routing
  overrides take precedence in non-interactive mode and therefore fail the subscription guard.
- Codex uses `codex exec --json --ephemeral` with an explicit sandbox. The preflight must prove
  ChatGPT login and must rule out API-key/base-URL overrides and paid-credit fallback.
- Pi uses print/JSON mode with `--no-session`, explicit project-trust behavior, and a fresh config
  root. Pi's documented Claude Pro/Max login draws from **extra usage billed per token**, so that
  route is not eligible for a “no additional charge” Opus product arm. Pi may enter through a
  documented subscription route with separately verified billing behavior (for example, its
  ChatGPT Plus/Pro Codex route), or enter the paid same-model controlled track under an approved
  budget.
- The historical Collie **product-track** arm delegates subscription Opus calls through the
  official Claude Code CLI and its verified Claude.ai login. That is valid for comparing the
  shipped products, but it is not evidence for Collie's native harness: Claude Code contributes
  its own system prompt. Native Collie overnight therefore forbids this fallback. Its experimental
  direct route is admitted only if a Collie-owned live inference probe succeeds; current HTTP 429
  results are an admission failure, not a score. Direct bearer-token reuse or an OAuth proxy is not
  admissible evidence for a subscription-native benchmark claim.

Pin the CLI package/version and full source revision for every arm. If a CLI cannot be forced onto
the exact model endpoint and settings required by a controlled manifest, exclude it from that
controlled run instead of substituting its native model. Missing trustworthy descendant usage is
also an admission failure; never fill missing token or cost fields with zero.

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

In a subscription-native product manifest (`track: product`), omit the global `model`. Each
harness instead supplies a complete frozen `model` object and sets `model_source` to
`native_manifest`. Product harnesses may use different models, but their exact provider, ID,
snapshot, endpoint, reasoning effort, temperature, and top-p are copied into their usage receipts.
Both tracks still require measurable root-plus-descendant usage and the shared manifest budget.

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

## Repository benchmarks require external isolation

Built-in permission modes are defense in depth, not the benchmark security boundary. Real
repository tasks must run in a disposable environment created and supervised by the evaluator,
outside every compared harness. This is mandatory even when a product advertises a sandbox: Pi
deliberately has no built-in permission prompts, Codex documents broader write access as safe only
inside a controlled environment, and Prime Agent itself warns that its worker/kernel separation is
not a security sandbox.

For every task, harness, and repetition, the evaluator must:

- materialize a fresh repository from the same pinned base commit inside a per-run container, VM,
  or equivalent OS-enforced sandbox and destroy it after collecting the patch; a clone or worktree
  may provide the repository state but is not an isolation boundary by itself. No arm may inherit
  another arm's filesystem, session, memory, or caches unless a separate warm-memory experiment
  explicitly declares that condition;
- mount only the task repository and scoped read-only credentials needed for the admitted native
  route; do not expose the host home directory, personal configuration, unrelated credentials,
  grader implementation, hidden tests, gold patch, another arm's trace, or prior-run artifacts;
- enforce the network allowlist, wall clock, process-tree kill, CPU/memory/disk limits, and
  root-plus-descendant accounting from outside the agent process;
- run grading after the agent exits in an evaluator-owned environment, bind the verdict to the
  harvested patch digest, and keep the compared harness unable to edit the grader or its receipt;
- start with fresh product config/session directories, explicitly disable native resume/refine
  behavior, and record any unavoidable product-owned cache as part of the product condition.

A local temporary directory under the same host account is sufficient for the one-file plumbing
smoke only. It is not sufficient isolation for a publishable SWE-bench or real-repository result.

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

## Official primary references

- Prime methodology and trust boundary: [Verifiers v1: taskset × harness × runtime](https://www.primeintellect.ai/blog/verifiers-v1),
  [Prime Agent repository and external-sandbox warning](https://github.com/PrimeIntellect-ai/prime-agent)
- Claude Code: [non-interactive/headless execution](https://code.claude.com/docs/en/headless),
  [authentication and credential precedence](https://code.claude.com/docs/en/authentication)
- Codex CLI: [non-interactive mode, ephemeral sessions, and sandbox flags](https://learn.chatgpt.com/docs/non-interactive-mode),
  [authentication](https://learn.chatgpt.com/docs/auth)
- Pi: [coding-agent modes, project trust, and security model](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md),
  [subscription providers and billing behavior](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md)
