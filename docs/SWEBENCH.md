# SWE-bench eval (collie vs Claude Code)

The credible benchmark: real GitHub issues → the agent produces a patch → the
repo's own tests decide (FAIL_TO_PASS + PASS_TO_PASS). Toy tasks stay the fast dev
loop; SWE-bench is the official number.

## Status

- `swebench` 4.1.0 + `datasets` installed; SWE-bench **Verified (500 instances)**
  loads; schema confirmed.
- Runner built: `harness/swe.py` + `swe_run.py`. **Prediction phase tested** (clone
  `pallets/flask` @ base_commit → edit → `git diff --cached` = valid unified diff).
- `build_predictions` is **resumable** — re-running skips already-predicted instances,
  so a killed run continues instead of restarting.

## Docker: use the native WSL engine (not Docker Desktop)

Docker Desktop + WSL integration was flaky on this box (socket disappears, daemon
"down", permission-denied). **Fix once, for good:** install the native Docker Engine
*inside* the WSL distro so `dockerd` runs as a systemd service with a stable,
docker-group-owned socket — no GUI dependency.

```bash
# Docker's official apt repo + engine
sudo apt-get install -y ca-certificates curl && sudo install -m0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER && sudo systemctl enable --now docker
```

Then turn **off** Docker Desktop's WSL integration for this distro (else it re-injects
`/usr/bin/docker` and fights the native binary). Verify:

```bash
sg docker -c "docker run --rm hello-world"          # new shell picks up the group
.venv/bin/python -c "import docker; print(docker.from_env().version()['Version'])"
```

Installed here: **Docker 29.6.1**, `/var/run/docker.sock` = `root:docker` (0660),
`systemctl is-active docker` = active. Since the current login shell may pre-date the
group add, wrap eval commands in `sg docker -c "..."`.

**Gotcha — pin `requests==2.32.3`.** docker-py 7.1.0 breaks with `requests>=2.33`
(`docker.from_env()` → `Invalid URL 'None'`, even with a valid base_url); a stray upgrade
to requests 2.34.2 silently made every eval score 0/N (patches never applied). `datasets`
5.0 needs `requests>=2.32.2`, so 2.32.3 is the one version that satisfies both. Symptom to
watch for: an eval where a harness with obviously-correct patches resolves 0/N.

## Run (once Docker is on)

```bash
DEEPSEEK_API_KEY=... .venv/bin/python swe_run.py --n 5
# or explicit instances:
.venv/bin/python swe_run.py --ids pallets__flask-5014 psf__requests-... 
# prediction only (works now, no Docker):
.venv/bin/python swe_run.py --n 3 --predict-only
```

It runs the PREDICTION phase (clone @ base_commit → agent edits → patch) for both
**collie** (DeepSeek) and **Claude Code** (`claude -p --permission-mode
bypassPermissions`), writes `preds/{agent}.jsonl`, then the OFFICIAL eval
(`python -m swebench.harness.run_evaluation`) for each, and prints resolve-rate.

## How it works (per the official harness)

- Predictions JSONL: `{instance_id, model_name_or_path, model_patch}` (model_patch =
  a `git apply`-able unified diff).
- Eval builds a per-instance Docker image, applies model_patch + gold test_patch,
  runs the tests, writes a report `{model}.{run_id}.json` whose `resolved_instances /
  total_instances` = resolve-rate. Per-instance logs under
  `logs/run_evaluation/<run_id>/<model>/<instance>/`.
- The agent sees ONLY `problem_statement` + the clean repo — never the gold `patch`
  or `test_patch` (harness applies test_patch itself at eval time).
- `--cache_level env` keeps env images, drops per-instance images to save disk.
  Default `--namespace swebench` pulls prebuilt images from Docker Hub (x86_64) —
  much faster than local builds. Budget ~10-30GB for a 5-instance sample.

## Honest expectation

SWE-bench Verified is hard. A lean harness on DeepSeek-V3 will resolve a modest
fraction (single/low-double-digit % on a small sample is normal); the point is a
credible, same-eval **collie vs Claude Code** number — and collie's efficiency edge
(lean prefix / cost) holds regardless of resolve-rate. Start small, scale the sample
once the pipeline is confirmed green with the `--predictions_path gold` smoke test.

## Headline (n=16, SWE-bench Verified, 7 repos) — four harnesses, one model (DeepSeek)

| harness | resolved | note |
|---|:--:|---|
| Hermes | **8/16** | strong same-model agent |
| **collie** | **7/16** | this harness |
| Aider | 5/16 | most popular open-source coding agent |
| opencode | 1/16 | headless-fragile (empty patches at scale) |

All four: DeepSeek-V3, same 16 instances, same official eval → the delta is the harness.
**collie (7/16) beats Aider (5/16) and is on par with Hermes (8/16).** opencode's 1/16 is
integration fragility, not a fair read. collie produced 0 empty patches (the did_edit fix).
Reference: subscription Claude (stronger model) = 7/8 on the first 8 — a model gap.

*Gotcha that nearly hid this: `requests>=2.33` breaks docker-py 7.1.0, silently scoring
every harness 0/N. Pinned `requests==2.32.3`. Aider timed out on requests-1142 (its miss).*

## Graded result (n=8, SWE-bench Verified, 7 repos) — corrected

| instance | collie | Hermes | Claude |
|---|:--:|:--:|:--:|
| flask-5014 | ✓ | ✓ | ✓ |
| requests-1142 | ✓ | ✓ | ✓ |
| pylint-4551 | ✗ | ✗ | ✗ |
| sphinx-10323 | ✓ | ✓ | ✓ |
| pytest-10051 | ✗ | ✗ | ✓ |
| xarray-2905 | ✓ | ✓ | ✓ |
| seaborn-3069 | ✗ | ✗ | ✓ |
| requests-1724 | ✗ | ✓ | ✓ |
| **resolved** | **4/8** | **5/8** | **7/8** |
| model | DeepSeek | DeepSeek | subscription |

**On the same model, collie (4/8) ≈ Hermes (5/8)** — a one-instance gap inside n=8
variance (DeepSeek is stochastic; collie's own resolved set shifts ±1 between runs, e.g.
requests-1724 ↔ requests-1142). The Claude gap (7/8) is a **model** gap. `pylint-4551` and
`seaborn-3069` fail on **both** DeepSeek harnesses → model-bound, not a collie gap.

### Correction (why an earlier version said "Hermes 7/8")
The first table here reported Hermes 7/8 and a large collie-vs-Hermes harness gap. **It was
an artifact of a bug**: `swe_predict_one` dispatched every non-`collie` agent to
`predict_claude_code`, so "Hermes" actually ran `claude -p` (subscription). A multi-agent
harness audit (`AUDIT_BACKLOG.md`) flagged it; the dispatch now uses `AGENTS[agent]` and
the numbers above are real Hermes/DeepSeek (patch bytes differ from Claude on all 8).

### Real bugs the audit fixed (correctness, effect needs larger n to see)
- **did_edit on failed edits** — `edit_file` returns `ERROR: old_string not found` *without
  writing*; the loop counted it as an edit, disabling every empty-patch guard. Now gated on
  success. This is the genuine core empty-patch bug (the earlier "hard tool-restriction /
  code-density" changes helped but this was the root cause).
- **tool.run unguarded** — a malformed tool call aborted the whole run; now a recoverable error.

**Method note:** n=8 with ±1 run-to-run variance cannot attribute a single harness fix.
Next: a larger sample (n≈20–30) and/or best-of-k per instance for a variance-robust
collie-vs-Hermes delta.
