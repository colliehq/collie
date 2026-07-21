"""Multi-run SWE eval — fixes the Lucky-Pass / pass@1 noise (AgentLens 2026): run each
config N times, then report the metrics single-run comparisons can't support:
  - pass@1  : mean resolve over all instance-runs (the noisy number everyone quotes)
  - pass@k  : resolved in >=1 of N runs (upper bound; also the ULTRA best-of-k ceiling)
  - pass^k / consistency : resolved in a MAJORITY of runs (genuine capability, not luck)
  - Wilson 95% CI on pass@1
  - McNemar exact paired test between two configs (is the +1 real or noise?)

A "config" is (name, agent, env-overrides) so the SAME agent can be run under different
settings — e.g. collie with COLLIE_SWE_VERIFY=0 vs =1 — as two configs, holding model+tasks
fixed (the correct way to isolate a harness change per the 2026 benchmark-hygiene guidance).

Phases (each resumable; run predict/eval in the background, stats is instant):
  predict : build preds/mr_<config>_r<i>.jsonl for every config x run           (DeepSeek)
  eval    : official Docker eval each run -> logs/run_evaluation/mr_<config>_r<i> (Docker)
  stats   : aggregate report.json's -> the metrics above

    python -m bench.multirun_eval predict --runs 3
    python -m bench.multirun_eval eval    --runs 3
    python -m bench.multirun_eval stats   --runs 3
"""
import argparse
import glob
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness import swe

IDS_FILE = (os.environ.get("COLLIE_MR_IDS")     # instance list override (e.g. data/ids50.txt)
            or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "ids16.txt"))

# (config-name, swe.AGENTS key, env overrides). Same model+tasks; only the harness setting varies.
# NOTE both sides of an A/B pair set their key EXPLICITLY — env persists across configs in-process,
# so an implicit default would inherit the other arm's value.
CONFIGS = {
    "collie_base":      ("collie", {"COLLIE_SWE_VERIFY": "0"}),
    "collie_verify":    ("collie", {"COLLIE_SWE_VERIFY": "1"}),
    "collie_default":   ("collie", {"COLLIE_PLAN_FIRST": "0"}),   # 2026-07-16 A/B: does PLAN_FIRST
    "collie_planfirst": ("collie", {"COLLIE_PLAN_FIRST": "1"}),   # cure the sibling-file misses?
}
# Seed run #1 from an existing single-run eval so we don't recompute it (name -> existing run_id).
SEED_R1 = {"collie_base": "c16", "collie_verify": "collie_verify16"}


def _ids():
    return [l.strip() for l in open(IDS_FILE) if l.strip()]


def _run_id(cfg, i):
    return "mr_%s_r%d" % (cfg, i)


def _resolved_set(run_id, agent):
    """Instances resolved in this run (empty if the run dir is missing)."""
    s = set()
    for r in glob.glob("logs/run_evaluation/%s/%s/*/report.json" % (run_id, agent)):
        try:
            d = json.load(open(r)); iid = list(d)[0]
            if d[iid].get("resolved"):
                s.add(iid)
        except Exception:
            pass
    return s


def _evaluated_set(run_id, agent):
    """Instances that produced ANY report (resolved or not) — i.e. actually evaluated."""
    return {os.path.basename(os.path.dirname(r))
            for r in glob.glob("logs/run_evaluation/%s/%s/*/report.json" % (run_id, agent))}


# ---- statistics (pure python; no scipy) ----------------------------------------------------
def wilson(x, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = x / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z / d * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, c - h), min(1.0, c + h))


def mcnemar_exact(b, c):
    """Two-sided exact binomial McNemar p-value for discordant counts b, c."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) * (0.5 ** n)
    return min(1.0, 2 * tail)


# ---- phases --------------------------------------------------------------------------------
def phase_predict(configs, runs):
    ids = _ids()
    for cfg in configs:
        agent, env = CONFIGS[cfg]
        for i in range(1, runs + 1):
            out = "preds/%s.jsonl" % _run_id(cfg, i)
            if i == 1 and cfg in SEED_R1 and not os.path.exists(out):
                src = "preds/%s.jsonl" % {"c16": "c16", "collie_verify16": "collie_verify16"}[SEED_R1[cfg]]
                if os.path.exists(src):
                    import shutil; shutil.copy(src, out)
                    print("[seed] %s <- %s" % (out, src), flush=True)
            for k, v in env.items():
                os.environ[k] = v
            print("[predict] %s run %d/%d (env %s)" % (cfg, i, runs, env), flush=True)
            swe.build_predictions(ids, agent=agent, out_path=out)
    print("PREDICT DONE", flush=True)


def phase_eval(configs, runs):
    ids = _ids()
    for cfg in configs:
        agent, _ = CONFIGS[cfg]
        for i in range(1, runs + 1):
            rid = _run_id(cfg, i)
            if i == 1 and cfg in SEED_R1:
                # reuse the existing eval dir by symlink/copy of reports if present
                seed = SEED_R1[cfg]
                if _evaluated_set(seed, agent) and not _evaluated_set(rid, agent):
                    import shutil
                    src = "logs/run_evaluation/%s/%s" % (seed, agent)
                    dst = "logs/run_evaluation/%s/%s" % (rid, agent)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    if not os.path.exists(dst):
                        shutil.copytree(src, dst)
                    print("[seed-eval] %s <- %s" % (rid, seed), flush=True)
            preds = "preds/%s.jsonl" % rid
            swe.assemble_jsonl(preds)                 # rebuild jsonl view from shards before eval
            done = _evaluated_set(rid, agent)
            todo = [x for x in ids if x not in done]
            if not todo:
                print("[eval] %s complete (%d)" % (rid, len(done)), flush=True); continue
            print("[eval] %s: %d/%d remaining" % (rid, len(todo), len(ids)), flush=True)
            swe.evaluate(preds, rid, todo, max_workers=2)
    print("EVAL DONE", flush=True)


def phase_stats(configs, runs):
    ids = _ids()
    agg = {}  # cfg -> {iid: [resolved bools per run]}
    for cfg in configs:
        agent, _ = CONFIGS[cfg]
        per = {iid: [] for iid in ids}
        for i in range(1, runs + 1):
            rid = _run_id(cfg, i)
            res = _resolved_set(rid, agent)
            ev = _evaluated_set(rid, agent)
            for iid in ids:
                if iid in ev:                    # only count runs that actually evaluated it
                    per[iid].append(iid in res)
        agg[cfg] = per

    print("\n=== multi-run SWE (N=%d) — %d instances ===\n" % (runs, len(ids)))
    print("%-16s %-9s %-9s %-13s %s" % ("config", "pass@1", "pass@%d" % runs, "consistency", "pass@1 95%CI"))
    solved_majority = {}
    for cfg in configs:
        per = agg[cfg]
        trials = sum(len(v) for v in per.values())
        succ = sum(sum(v) for v in per.values())
        p1 = succ / trials if trials else 0
        atk = sum(1 for v in per.values() if any(v)) / len(ids)
        maj = {iid: (sum(v) > len(v) / 2 if v else False) for iid, v in per.items()}
        solved_majority[cfg] = maj
        cons = sum(maj.values()) / len(ids)
        lo, hi = wilson(succ, trials)
        print("%-16s %-9s %-9s %-13s [%.2f, %.2f]" % (
            cfg, "%.1f%%" % (100 * p1), "%.1f%%" % (100 * atk),
            "%.1f%%" % (100 * cons), lo, hi))

    if len(configs) == 2:
        a, b = configs
        ma, mb = solved_majority[a], solved_majority[b]
        bd = sum(1 for iid in ids if ma[iid] and not mb[iid])   # a solves, b doesn't
        cd = sum(1 for iid in ids if mb[iid] and not ma[iid])
        p = mcnemar_exact(bd, cd)
        print("\nMcNemar (majority-solve, paired): %s solves %d that %s doesn't; %s solves %d that %s doesn't."
              % (a, bd, b, b, cd, a))
        print("Discordant %d/%d — exact two-sided p = %.3f -> %s"
              % (bd + cd, len(ids), p,
                 "SIGNIFICANT at .05" if p < 0.05 else "NOT significant (within noise)"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["predict", "eval", "stats"])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--configs", default=",".join(CONFIGS))
    args = ap.parse_args()
    configs = [c.strip() for c in args.configs.split(",") if c.strip() in CONFIGS]
    {"predict": phase_predict, "eval": phase_eval, "stats": phase_stats}[args.phase](configs, args.runs)


if __name__ == "__main__":
    main()
