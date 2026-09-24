# Read batches on the Claude Agent SDK route (2026-09-24)

`ClaudeAgentSdkProvider(read_batch=...)` lets one model response name up to eight `read_file`
calls. It shipped off by default "until a recorded comparison decides whether fewer turns actually
beat the extra refusal surface". This is that comparison. **Result: keep it off.**

## Setup

- Real Collie loop: `make_harness(provider="claude-agent-sdk")` and `Harness.run`, product default
  model `claude-opus-4-8`, default effort, plain (not structured) response mode, 40-turn cap.
- The only difference between arms is `provider.read_batch` (`off` / `on`).
- Three small repair tasks written for this run, each with two bugs spread over several modules
  and an unmodifiable unittest file: `config` (env overrides + deep merge), `pricing` (discount
  order + unknown tax state), `report` (team normalisation + sort order). `oracle.py` checks that
  every baseline fails and a reference fix passes.
- Two repetitions per task and arm, order alternated per task and repetition. Grading re-runs
  `python -m unittest -q` outside the agent and checks the test file is byte-identical.
- Isolation: Collie's settings, MCP config, memory, sessions and state are redirected to the
  experiment directory; HOME stays real only so the SDK's CLI finds the existing Claude login.
  Desktop, screenshot, MCP-management and MCP tools are removed in both arms.
- Windows 11, Python 3.14, `claude-agent-sdk` 0.2.157 with Claude Code CLI 2.1.280 in its
  `_bundled` folder (the SDK names 2.1.277 as its bundled version). Claude Max subscription.

## Result (`results.jsonl`)

| arm | runs | passed | model calls, passed runs (mean) | wall time, passed runs (mean) |
| --- | --- | --- | --- | --- |
| off | 6 | 6 | 10.5 | 65.2 s |
| on  | 6 | 4 | 8.0 | 62.8 s |

Batching was used in 5 responses across the `on` runs. Where it was used it removed 2-4 model
calls per run, but the batched response itself was slower and longer, so wall time did not
improve (the 4% difference is within the spread between repetitions). Both `on` failures ended
the whole run on the first failing call with the same worker refusal, `SDK emitted more than one
Assistant message id` (3 and 1 responses in); no `off` run produced it. The worker keeps no
session transcript, so what the second assistant message contained is not known.

## Limits

Twelve runs, three task shapes, one model, one machine: enough to see a failure mode that the
`off` arm did not show, not enough to estimate its rate. The `report` task was often read with
`grep`/`bash` rather than `read_file`, which batching does not cover. This does not measure the
structured response mode, other models, or long tasks.

## Reproduce

```
python bench/experiments/2026-09-24-read-batch/oracle.py            # no model, no account
READ_BATCH_EXPERIMENT_DIR=/tmp/rb python bench/experiments/2026-09-24-read-batch/read_batch_probe.py config,pricing,report 2
```

The second command spends Claude subscription usage and needs a logged-in Claude Code CLI
reachable by the SDK.
