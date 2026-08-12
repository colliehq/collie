# -*- coding: utf-8 -*-
"""Paired, repeated, memory-isolated harness comparison.

Why not "run SWE-bench Verified and report a number":

  * Verified is contaminated. OpenAI stopped reporting it: frontier models reproduce gold patches
    verbatim from the task id alone, >60% of its 138 problematic tasks are unsolvable through test
    defects, and independent work measures ~33% of successful patches involving solution leakage
    with file paths recalled up to 76% of the time. A score there measures model memory.
  * A single number is not falsifiable anyway. Harness choice alone moves SWE-bench results by
    10-20 points on identical weights, and the five SWE-bench variants are not comparable to each
    other — so "we scored X" invites "on which variant, which harness, which model", and all three
    swing it more than any difference we could claim.
  * Our own history is the argument against it: earlier runs are remembered as "well behind CC,
    Hermes and Pi", and there is no record left in this repo to check. Numbers with no trace are
    worse than no numbers.

So this measures the thing that survives contamination: the SAME instance through every selected
product, repeated, reported per instance rather than as a total.  The subscription-native run is
explicitly a PRODUCT comparison: Collie and Claude Code use Opus, while Codex uses its native Sol
route.  It says which purchasable system solved the task; it cannot isolate a pure harness effect.

MEMORY IS THE TRAP THIS FILE EXISTS TO AVOID. Collie remembers across runs and Claude Code does
not, so a repeated instance is exactly where Collie starts answering from its own notes rather
than from the repo. That already happened once here: an earlier experiment's runs shared a project
and a store, and the agent reported "result unchanged from the previous runs" about a repository it
had never looked at. Every run below gets its own COLLIE_STATE_DIR and its own project name, and
`verify_isolation()` proves it against a real fork before any quota is spent.

Only the cold condition is implemented here: every attempt gets fresh state.  A previously exposed
``collie-warm`` arm did not consolidate or retain a memory-writing tool, so its shared directory was
empty and the label was false; it is rejected until a real cross-task memory contract exists.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
DATASET = "ScaleAI/SWE-bench_Pro"          # not Verified — see the module docstring


# ---------------------------------------------------------------- dataset
def load_instances(n: int, seed: int = 0, repos: str = "") -> list:
    """A deterministic sample of SWE-bench Pro. Deterministic so a rerun is a rerun, not a redraw."""
    import pandas as pd
    url = ("https://huggingface.co/datasets/%s/resolve/main/data/test-00000-of-00001.parquet"
           % DATASET)
    df = pd.read_parquet(url)
    if repos:
        keep = {r.strip() for r in repos.split(",") if r.strip()}
        df = df[df["repo"].isin(keep)]
    df = df.sort_values("instance_id")                    # stable order before sampling
    if n and n < len(df):
        df = df.sample(n=n, random_state=seed)
    cols = ["instance_id", "repo", "base_commit", "problem_statement", "fail_to_pass",
            "pass_to_pass", "repo_language"]
    return df[[c for c in cols if c in df.columns]].to_dict("records")


# ---------------------------------------------------------------- isolation
def verify_isolation() -> tuple:
    """Prove, before spending anything, that a run cannot read another run's memory.

    Asserted against a real child process rather than by inspecting a variable: the settings bug
    found the same day showed that an inherited environment can silently defeat what the code
    looks like it does.
    """
    real = os.path.expanduser("~/.collie/data")
    before = {}
    for root, _d, files in os.walk(real):
        for f in files:
            p = os.path.join(root, f)
            try:
                before[p] = os.path.getmtime(p)
            except OSError:
                pass

    state = tempfile.mkdtemp(prefix="isocheck-")
    env = {**os.environ, "COLLIE_STATE_DIR": state, "COLLIE_DATA_DIR": os.path.join(state, "data")}
    code = ("import os, sys\n"
            "sys.path.insert(0, %r)\n"
            "from harness.cli import make_harness\n"
            "h = make_harness(os.getcwd(), provider='mock', project='isocheck')\n"
            "h.memory.remember('ISOLATION CANARY', keys='canary', project='isocheck')\n"
            "h.memory.close()\n"
            "print('wrote canary')\n" % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    f = os.path.join(state, "w.py")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(code)
    r = subprocess.run([sys.executable, f], capture_output=True, text=True, env=env, timeout=300)

    touched = []
    for p, m in before.items():
        try:
            if os.path.getmtime(p) != m:
                touched.append(os.path.basename(p))
        except OSError:
            pass
    canary_landed = any("memory" in fn for fn in os.listdir(os.path.join(state, "data"))
                        ) if os.path.isdir(os.path.join(state, "data")) else False
    shutil.rmtree(state, ignore_errors=True)
    ok = (not touched) and canary_landed and r.returncode == 0
    return ok, {"real_store_touched": touched, "canary_in_isolated_store": canary_landed,
                "child_stdout": (r.stdout or "").strip(), "child_err": (r.stderr or "")[-200:]}


# ---------------------------------------------------------------- one run
def run_collie(inst: dict, workdir: str, model: str, rep: int, max_turns: int = 24) -> dict:
    """One memory-isolated, tool-restricted Collie product attempt."""
    from harness import swe
    state = tempfile.mkdtemp(prefix="pe-cold-")
    prev = os.environ.get("COLLIE_DATA_DIR")
    # COLLIE_DATA_DIR, not COLLIE_STATE_DIR: from a source checkout the latter is
    # silently ignored (see _paths), so it would isolate nothing.
    os.environ["COLLIE_DATA_DIR"] = os.path.join(state, "data")
    t0 = time.time()
    patch, err, usage = "", "", {}
    try:
        # Both predictors EDIT THE WORKDIR IN PLACE; neither returns the patch. predict_collie
        # returns a RunResult (tokens/cost), predict_claude_code returns the CLI result. Reading
        # the return value as a patch is how you get a benchmark that reports plausible-looking
        # byte counts for work that never happened — take the diff from the repo, always.
        # The official Claude CLI is the only subscription-safe Opus backend in this benchmark.
        # The old anthropic-oauth route reused a bearer token directly and could not prove whether
        # usage landed in the flat plan or metered extra usage.
        rr = swe.predict_collie(workdir, inst["problem_statement"],
                                provider="claude-cli", model=model, max_turns=max_turns,
                                benchmark_safe=True)
        # Record EVERY usage field RunResult carries, not just input/output. Cache reads are
        # billed at a tenth of input and are the bulk of an agent loop's tokens, so a cost figure
        # without them overstates spend several-fold — and the cold runs delete their store, so a
        # field not captured here is gone for good rather than recoverable from runs.db.
        usage = {k: getattr(rr, k) for k in
                 ("turns", "input_tokens", "output_tokens", "total_tokens",
                  "cache_read", "cache_creation", "cache_miss_tokens", "cost_usd")
                 if getattr(rr, k, None) is not None}
        if "cost_usd" in usage:
            # RunResult prices tokens at the public API rate even when Claude Max paid the real
            # bill.  Preserve that useful efficiency estimate without calling it a charge.
            usage["api_equivalent_cost_usd"] = usage.pop("cost_usd")
        # Collie reports a provider failure (quota exhausted, HTTP error) in RunResult.error and
        # returns NORMALLY — it does not raise. Reading only exceptions therefore turned a
        # subscription outage into "collie produced no patch": two 16-second, one-turn, zero-byte
        # runs were scored as losses on NodeBB-97c8 while the Claude arm's identical outage was
        # correctly reported, because that arm exits non-zero. Same outage, opposite bookkeeping.
        err = (getattr(rr, "error", "") or "").strip()
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
    try:
        patch = swe.make_patch(workdir)
    except Exception as e:
        err = err or "make_patch: %s: %s" % (type(e).__name__, e)
    finally:
        if prev is None:
            os.environ.pop("COLLIE_DATA_DIR", None)
        else:
            os.environ["COLLIE_DATA_DIR"] = prev
        shutil.rmtree(state, ignore_errors=True)
    return {"harness": "collie", "rep": rep, "secs": round(time.time() - t0, 1),
            "patch_bytes": len(patch or ""), "patch": patch, "error": err, "usage": usage}


def run_claude(inst: dict, workdir: str, model: str, rep: int, max_turns: int = 24) -> dict:
    from harness import swe
    t0 = time.time()
    patch, err, cli = "", "", None
    try:
        cli = swe.predict_claude_code(workdir, inst["problem_statement"], model=model,
                                      max_turns=max_turns)
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
    data = {}
    if cli is not None and (cli.stdout or "").strip().startswith("{"):
        try:
            parsed = json.loads(cli.stdout)
            data = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            data = {}
    # An empty patch with no explanation is the failure mode this whole file exists to prevent:
    # the arm "ran", scored 0, and looked like a legitimate loss. Surface why it produced nothing.
    if cli is not None and not err:
        tail = ((cli.stderr or "").strip() or (cli.stdout or "").strip())[-400:]
        if cli.returncode != 0:
            err = "claude exited %d: %s" % (cli.returncode, tail)
        elif data.get("is_error") is True:
            err = "claude reported an adapter error"
    try:
        patch = swe.make_patch(workdir)     # same contract as collie — diff the repo, not the return
    except Exception as e:
        err = err or "make_patch: %s: %s" % (type(e).__name__, e)
    # The CLI reports its own usage/cost on stdout under --output-format json. Parse failures are
    # recorded as an empty usage dict, never as a zero — a missing measurement is not free work.
    usage = {}
    u = data.get("usage") or {}
    if isinstance(u, dict):
        usage = {"api_equivalent_cost_usd": data.get("total_cost_usd"),
                 "duration_ms": data.get("duration_ms"),
                 "input_tokens": u.get("input_tokens"),
                 "output_tokens": u.get("output_tokens"),
                 "cache_read": u.get("cache_read_input_tokens"),
                 "cache_creation": u.get("cache_creation_input_tokens")}
        usage = {k: v for k, v in usage.items() if v is not None}
    if not patch and not err:
        # Headless Claude can exit zero after declining an edit because the cwd failed its
        # permission check.  That is an adapter-invalid attempt, not a solved/unsolved sample.
        result_text = str(data.get("result") or "").lower()
        permission_markers = (
            "approve the write", "permission mode", "filesystem access",
            "access is sorted", "cannot edit", "can't edit", "unable to edit",
        )
        if any(marker in result_text for marker in permission_markers):
            err = "claude adapter denied workspace editing"
    row = {"harness": "claude", "rep": rep, "secs": round(time.time() - t0, 1),
           "patch_bytes": len(patch or ""), "patch": patch, "error": err, "usage": usage}
    if not patch and cli is not None:
        # rc==0 and an empty diff still needs an explanation — keep what the CLI actually said.
        row["cli_rc"] = cli.returncode
        row["cli_tail"] = ((cli.stdout or "") + "\n" + (cli.stderr or "")).strip()[-800:]
    return row


def _codex_usage(stdout: str) -> dict:
    """Parse the last aggregate usage receipt from `codex exec --json` JSONL.

    Codex has emitted both direct `usage` objects and nested token-count objects across CLI
    revisions.  Keep the parser conservative: missing fields stay missing rather than becoming
    fabricated zeroes, and the last aggregate event wins instead of summing cumulative events.
    """
    found = {}
    aliases = {
        "input_tokens": "input_tokens", "cached_input_tokens": "cache_read",
        "cache_read_input_tokens": "cache_read", "output_tokens": "output_tokens",
        "reasoning_output_tokens": "reasoning_output_tokens", "total_tokens": "total_tokens",
    }
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        candidates = [event.get("usage"), event.get("token_usage")]
        result = event.get("result")
        if isinstance(result, dict):
            candidates += [result.get("usage"), result.get("token_usage")]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            parsed = {dest: candidate[src] for src, dest in aliases.items()
                      if isinstance(candidate.get(src), (int, float))
                      and not isinstance(candidate.get(src), bool)}
            if parsed:
                found = parsed
    return found


def _codex_adapter_error(stdout: str, stderr: str) -> str:
    """Return a stable error code for JSONL/diagnostic failures even when Codex exits zero."""
    messages = []
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        event_type = str(event.get("type") or "").lower()
        if event_type in {"error", "turn.failed", "item.failed"} or event_type.endswith(".error"):
            return "codex emitted an error event"
        # Search structured text only for known permission failures; never echo arbitrary model or
        # server output into the public result.
        messages.append(json.dumps(event, ensure_ascii=False).lower())
    messages.append((stderr or "").lower())
    combined = "\n".join(messages)
    if "writing is blocked by read-only sandbox" in combined or "read-only sandbox" in combined:
        return "codex workspace was read-only"
    if ("write" in combined or "patch" in combined) and any(marker in combined for marker in (
            "rejected by user approval", "permission denied", "operation not permitted")):
        return "codex workspace edit was denied"
    return ""


def run_codex(inst: dict, workdir: str, model: str, rep: int) -> dict:
    """One native Codex product attempt using the existing ChatGPT subscription login."""
    from harness import swe
    t0 = time.time()
    patch, err, cli = "", "", None
    try:
        cli = swe.predict_codex(workdir, inst["problem_statement"], model=model)
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
    if cli is not None and not err and cli.returncode != 0:
        tail = ((cli.stderr or "").strip() or (cli.stdout or "").strip())[-400:]
        err = "codex exited %d: %s" % (cli.returncode, tail)
    if cli is not None and not err:
        err = _codex_adapter_error(cli.stdout or "", cli.stderr or "")
    try:
        patch = swe.make_patch(workdir)
    except Exception as e:
        err = err or "make_patch: %s: %s" % (type(e).__name__, e)
    usage = _codex_usage(cli.stdout if cli is not None else "")
    row = {"harness": "codex", "rep": rep, "secs": round(time.time() - t0, 1),
           "patch_bytes": len(patch or ""), "patch": patch, "error": err, "usage": usage}
    if not patch and cli is not None:
        row["cli_rc"] = cli.returncode
        row["cli_tail"] = ((cli.stdout or "") + "\n" + (cli.stderr or "")).strip()[-800:]
    return row


# ---------------------------------------------------------------- reporting
def summarize(rows: list) -> dict:
    """Per-instance, per-harness. A total is deliberately NOT the headline — with a handful of
    instances a total difference is noise, and the paired per-instance record is what carries."""
    by = {}
    for r in rows:
        by.setdefault((r["instance_id"], r["harness"]), []).append(r)
    out = {"per_instance": {}, "variance": {}}
    for (iid, h), rs in sorted(by.items()):
        produced = [1 if (x["patch_bytes"] > 0 and not x["error"]) else 0 for x in rs]
        out["per_instance"].setdefault(iid, {})[h] = {
            "reps": len(rs), "produced_patch": sum(produced),
            "secs": [x["secs"] for x in rs],
            "errors": [x["error"] for x in rs if x["error"]],
        }
        if len(produced) > 1:
            out["variance"].setdefault(h, []).append(statistics.pstdev(produced))
    for h, v in out["variance"].items():
        out["variance"][h] = {"mean_within_instance_stdev": round(sum(v) / len(v), 3), "n": len(v)}
    return out


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="paired_eval")
    ap.add_argument("--n", type=int, default=3, help="instances to sample")
    ap.add_argument("--reps", type=int, default=2, help="repeats per instance (variance)")
    ap.add_argument("--model", default="opus",
                    help="Claude model for both Collie and native Claude Code")
    ap.add_argument("--codex-model", default="gpt-5.6-sol",
                    help="native Codex product model (not a same-model control)")
    ap.add_argument("--max-turns", type=int, default=24,
                    help="per-run turn ceiling for Collie and Claude Code")
    ap.add_argument("--arms", default="collie,claude,codex",
                    help="comma-separated product arms: collie,claude,codex")
    ap.add_argument("--codex-credits-remaining", type=float, default=None,
                    help="freshly observed Codex credit balance; must be exactly 0")
    ap.add_argument("--codex-auto-reload-off", action="store_true",
                    help="assert the freshly observed Codex auto-reload setting is off")
    ap.add_argument("--codex-evidence-observed-at", default="",
                    help="UTC timestamp for the Codex account UI observation")
    ap.add_argument("--repos", default="", help="restrict to these repos")
    ap.add_argument("--warm", action="store_true",
                    help="reserved; currently rejected because no valid warm-memory contract exists")
    ap.add_argument("--external-sandboxed", action="store_true",
                    help="assert this process already runs in a disposable external sandbox")
    ap.add_argument("--dry", action="store_true", help="load + isolate + plan only; spend nothing")
    a = ap.parse_args(argv)

    arms = [value.strip() for value in a.arms.split(",") if value.strip()]
    allowed = {"collie", "claude", "codex"}
    if len(arms) < 2 or len(arms) != len(set(arms)) or not set(arms) <= allowed:
        print("refusing to run: --arms needs two or more unique values from collie,claude,codex")
        return 2
    if a.warm:
        print("refusing to run: collie-warm is not implemented truthfully yet")
        return 2
    if not 1 <= a.max_turns <= 120:
        print("refusing to run: --max-turns must be between 1 and 120")
        return 2
    if not a.dry and not a.external_sandboxed:
        print("refusing to run: real benchmark repositories require a disposable external sandbox")
        print("use python -m bench.subscription_smoke for the trusted one-file adapter gate")
        return 2

    # Fail closed before loading a repository or sending a model request.  The guard checks the
    # untouched parent environment, official CLI login modes, and (for Codex) a fresh account-UI
    # observation proving there is no credit balance or auto-reload fallback.  A denial is missing
    # authority, never a benchmark loss.
    from bench.subscription_guard import check_subscription_guard, SubscriptionGuardError
    guard_receipts = []
    try:
        if set(arms) & {"collie", "claude"}:
            guard_receipts.append(check_subscription_guard("claude-code"))
        if "codex" in arms:
            guard_receipts.append(check_subscription_guard("codex-cli", account_evidence={
                "credits_remaining": a.codex_credits_remaining,
                "auto_reload": False if a.codex_auto_reload_off else None,
                "observed_at_utc": a.codex_evidence_observed_at,
            }))
    except SubscriptionGuardError as e:
        print("subscription guard: DENY")
        print(json.dumps(e.receipt, ensure_ascii=False, indent=1))
        print("refusing to run: %s" % e.reason)
        return 2
    print("subscription guard: ALLOW (%s)" %
          ", ".join(receipt["provider"] for receipt in guard_receipts))

    ok, detail = verify_isolation()
    print("isolation: %s  %s" % ("OK" if ok else "FAILED", json.dumps(detail, ensure_ascii=False)))
    if not ok:
        print("refusing to run: a run could read another run's memory, which is the one thing "
              "this comparison cannot survive.")
        return 2

    instances = load_instances(a.n, repos=a.repos)
    print("\n%d instances from %s:" % (len(instances), DATASET))
    for i in instances:
        print("   %-44s %-28s %s" % (i["instance_id"][:44], i["repo"], i.get("repo_language", "")))

    run_arms = arms
    plan = len(instances) * a.reps * len(run_arms)
    print("\nplan: %d instances x %d reps x %d arms = %d runs" %
          (len(instances), a.reps, len(run_arms), plan))
    print("track: subscription-native product comparison (system result, NOT harness effect)")
    print("scope: exploratory smoke; output is deliberately non-publishable")
    print("arms: " + ", ".join(
        "%s=%s" % (arm, a.codex_model if arm == "codex" else a.model)
        for arm in run_arms))
    if a.dry:
        print("dry run — nothing spent.")
        return 0

    # PREDICTION half only. Grading needs swe-bench-pro's own Docker evaluator, which is not
    # wired yet — so nothing here claims a resolve rate. What it does measure is already the
    # harness question: given the same repository at the same commit and the same problem
    # statement, does each harness produce a patch at all, how long does it take, and how much
    # does that vary run to run. A patch is necessary but not sufficient for a resolve, so treat
    # these as an upper bound per arm, never as a score.
    from harness import swe
    os.makedirs(RESULTS, exist_ok=True)
    rows = []
    for inst in instances:
        for rep in range(1, a.reps + 1):
            for arm in run_arms:
                wd = tempfile.mkdtemp(prefix="pe-repo-")
                try:
                    swe.prepare_repo(inst["repo"], inst["base_commit"], wd)
                except Exception as e:
                    rows.append({"instance_id": inst["instance_id"], "harness": arm, "rep": rep,
                                 "secs": 0, "patch_bytes": 0, "patch": "",
                                 "error": "prepare_repo: %s" % e})
                    shutil.rmtree(wd, ignore_errors=True)
                    continue
                if arm == "collie":
                    r = run_collie(inst, wd, a.model, rep, max_turns=a.max_turns)
                elif arm == "claude":
                    r = run_claude(inst, wd, a.model, rep, max_turns=a.max_turns)
                else:
                    r = run_codex(inst, wd, a.codex_model, rep)
                r["instance_id"] = inst["instance_id"]
                # The preflight proves the metered fallback is disabled. It predicts zero marginal
                # charge; without a post-run account observation it does not prove an actual bill.
                r["expected_marginal_charge_usd"] = 0
                r["metered_fallback_disabled"] = True
                rows.append(r)
                print("  %-46s %-7s rep%d  patch=%-6s %5.0fs %s" %
                      (inst["instance_id"][:46], arm, rep, r["patch_bytes"], r["secs"],
                       (r["error"] or "")[:40]), flush=True)
                shutil.rmtree(wd, ignore_errors=True)
                out = os.path.join(RESULTS, "paired-subscription-product.json")
                with open(out, "w", encoding="utf-8") as f:
                    json.dump({"dataset": DATASET, "track": "product",
                               "claim": "system_comparison",
                               "scope": "exploratory_smoke", "publishable": False,
                               "safety_profile": {
                                   "collie": "repo rules/skills disabled; no shell or network",
                                   "claude": "safe-mode; no persistence; no shell or network",
                                   "codex": "workspace-write sandbox; ephemeral; user config ignored",
                               },
                               "subscription_guard_receipts": guard_receipts,
                               "models": {"collie": a.model, "claude": a.model,
                                          "codex": a.codex_model},
                               "billing": {"mode": "subscription_only",
                                           "expected_marginal_charge_usd": 0,
                                           "metered_fallback_disabled": True,
                                           "api_equivalent_cost_is_not_a_charge": True},
                               "rows": [{k: v for k, v in x.items() if k != "patch"} for x in rows],
                               "summary": summarize(rows)}, f, ensure_ascii=False, indent=1)
                # Patches kept alongside, because grading is the only half that discriminates and
                # a stripped result file means re-spending the whole run to get them back.
                with open(out.replace(".json", "-patches.json"), "w", encoding="utf-8") as f:
                    json.dump({"dataset": DATASET, "track": "product",
                               "claim": "system_comparison",
                               "scope": "exploratory_smoke", "publishable": False,
                               "safety_profile": {
                                   "collie": "repo rules/skills disabled; no shell or network",
                                   "claude": "safe-mode; no persistence; no shell or network",
                                   "codex": "workspace-write sandbox; ephemeral; user config ignored",
                               },
                               "subscription_guard_receipts": guard_receipts,
                               "models": {"collie": a.model, "claude": a.model,
                                          "codex": a.codex_model},
                               "billing": {"mode": "subscription_only",
                                           "expected_marginal_charge_usd": 0,
                                           "metered_fallback_disabled": True,
                                           "api_equivalent_cost_is_not_a_charge": True},
                               "rows": rows},
                              f, ensure_ascii=False, indent=1)
    print()
    print(json.dumps(summarize(rows), ensure_ascii=False, indent=1)[:1400])
    print()
    print("written to", os.path.join(RESULTS, "paired-subscription-product.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
