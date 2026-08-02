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

So this measures the thing that survives contamination: the SAME instance through BOTH harnesses,
repeated, reported per instance rather than as a total. Contamination inflates both arms, so a
paired result stays meaningful where an absolute score does not (it compresses the gap, which
makes a difference we do observe a conservative one).

MEMORY IS THE TRAP THIS FILE EXISTS TO AVOID. Collie remembers across runs and Claude Code does
not, so a repeated instance is exactly where Collie starts answering from its own notes rather
than from the repo. That already happened once here: an earlier experiment's runs shared a project
and a store, and the agent reported "result unchanged from the previous runs" about a repository it
had never looked at. Every run below gets its own COLLIE_STATE_DIR and its own project name, and
`verify_isolation()` proves it against a real fork before any quota is spent.

Two conditions are worth measuring and must never be mixed:
  cold  — fresh memory per run. Comparable to Claude Code; measures the harness.
  warm  — memory carried across instances. Measures what the memory is worth.
Reporting one while running the other is how a harness flatters itself.
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
def run_collie(inst: dict, workdir: str, model: str, rep: int, warm_state: str = "") -> dict:
    """One Collie attempt. `warm_state` shares a store across instances (the warm condition);
    empty means a fresh store — the only condition comparable to Claude Code."""
    from harness import swe
    state = warm_state or tempfile.mkdtemp(prefix="pe-cold-")
    prev = os.environ.get("COLLIE_DATA_DIR")
    # COLLIE_DATA_DIR, not COLLIE_STATE_DIR: from a source checkout the latter is
    # silently ignored (see _paths), so it would isolate nothing.
    os.environ["COLLIE_DATA_DIR"] = os.path.join(state, "data")
    t0 = time.time()
    try:
        patch = swe.predict_collie(workdir, inst["problem_statement"],
                                   provider="anthropic-oauth", model=model)
        err = ""
    except Exception as e:
        patch, err = "", "%s: %s" % (type(e).__name__, e)
    finally:
        if prev is None:
            os.environ.pop("COLLIE_DATA_DIR", None)
        else:
            os.environ["COLLIE_DATA_DIR"] = prev
        if not warm_state:
            shutil.rmtree(state, ignore_errors=True)
    return {"harness": "collie", "rep": rep, "secs": round(time.time() - t0, 1),
            "patch_bytes": len(patch or ""), "patch": patch, "error": err}


def run_claude(inst: dict, workdir: str, model: str, rep: int) -> dict:
    from harness import swe
    t0 = time.time()
    try:
        patch = swe.predict_claude_code(workdir, inst["problem_statement"], model=model)
        err = ""
    except Exception as e:
        patch, err = "", "%s: %s" % (type(e).__name__, e)
    return {"harness": "claude", "rep": rep, "secs": round(time.time() - t0, 1),
            "patch_bytes": len(patch or ""), "patch": patch, "error": err}


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
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--repos", default="", help="restrict to these repos")
    ap.add_argument("--warm", action="store_true", help="ALSO run collie with memory carried over")
    ap.add_argument("--dry", action="store_true", help="load + isolate + plan only; spend nothing")
    a = ap.parse_args(argv)

    ok, detail = verify_isolation()
    print("isolation: %s  %s" % ("OK" if ok else "FAILED", json.dumps(detail, ensure_ascii=False)))
    if not ok:
        print("refusing to run: a run could read another run's memory, which is the one thing "
              "this comparison cannot survive.")
        return 2

    inst = load_instances(a.n, repos=a.repos)
    print("\n%d instances from %s:" % (len(inst), DATASET))
    for i in inst:
        print("   %-44s %-28s %s" % (i["instance_id"][:44], i["repo"], i.get("repo_language", "")))

    plan = len(inst) * a.reps * (3 if a.warm else 2)
    print("\nplan: %d instances x %d reps x %d arms = %d runs" %
          (len(inst), a.reps, 3 if a.warm else 2, plan))
    if a.dry:
        print("dry run — nothing spent.")
        return 0

    print("NOT IMPLEMENTED past this point on purpose: the Docker evaluation half is unverified, "
          "and a half-run benchmark is worse than none. Wire swe-bench-pro's evaluator, then "
          "remove this guard.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
