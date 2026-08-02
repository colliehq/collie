# -*- coding: utf-8 -*-
"""Grade SWE-bench Pro patches locally with Docker, following the official protocol.

This is the half that can actually discriminate. Patch PRODUCTION saturated immediately (both
harnesses produce a patch on every instance), so the only question left is whether the patch is
right — and that is decided by the benchmark's own tests, not by us.

The protocol is copied from scaleapi/SWE-bench_Pro-os `swe_bench_pro_eval.py::create_entryscript`,
deliberately without improvement:

    cd /app
    git reset --hard <base_commit>
    git checkout <base_commit>
    git apply -v /workspace/patch.diff
    <LAST line of before_repo_set_cmd>       # git checkout <fix> -- <gold test files>
    bash /workspace/run_script.sh <selected_test_files_to_run, comma-joined>
    python /workspace/parser.py stdout.log stderr.log output.json

Two details in that order matter and are easy to get wrong:
  * only the LAST line of before_repo_set_cmd runs, and it runs AFTER the model patch. It checks
    out the gold test files, so an agent that edited the tests has those edits overwritten. A
    harness that applied it before the patch, or skipped it, would be grading agents on their own
    tests.
  * `git reset --hard` precedes the patch, so the patch must apply to the pristine base commit.

Resolved is the official criterion: (fail_to_pass | pass_to_pass) <= {tests reported PASSED}.
Every listed test must pass — no partial credit.

We report "patch did not apply" and "container/parse error" as their own outcomes rather than
folding them into unresolved. They are unresolved for scoring purposes, but a run where one arm's
patches mostly fail to apply is a finding about the arm, not a score, and burying it would hide
exactly the kind of broken-arm result this benchmark line has already produced four times.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
IMAGE_REPO = "jefzda/sweap-images"
# The official run_script.sh / parser.py are PER INSTANCE and live in the harness repo. They are
# not reimplemented here: a hand-rolled Go/jest output parser that disagrees with the official one
# would silently change every score.
HARNESS_REPO_ENV = "SWEBENCH_PRO_REPO"


def _harness_repo() -> str:
    p = os.environ.get(HARNESS_REPO_ENV) or ""
    if p and os.path.isdir(os.path.join(p, ".git")):
        return p
    raise RuntimeError(
        "set %s to a clone of https://github.com/scaleapi/SWE-bench_Pro-os — its per-instance "
        "run_script.sh and parser.py are the official graders and must not be reimplemented"
        % HARNESS_REPO_ENV)


def _as_list(v) -> list:
    """These columns are Python reprs (single-quoted), not JSON — upstream parses them with eval().
    They also arrive as numpy arrays from pandas. json.loads fails on both, so do it properly."""
    import ast
    if v is None:
        return []
    if isinstance(v, str):
        return list(ast.literal_eval(v))
    return list(v)


def _script(repo: str, instance_id: str, name: str) -> str:
    """Read a harness file out of the git object store.

    Not off the working tree: these paths exceed Windows' 260-char limit, so `git clone` leaves
    them missing on disk while the objects are perfectly intact. Reading the tree directly means
    the grader does not quietly fall back to something else on a checkout that looks fine.
    """
    r = subprocess.run(["git", "-C", repo, "show", "HEAD:run_scripts/%s/%s" % (instance_id, name)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError("official %s missing for %s: %s" % (name, instance_id, r.stderr[:200]))
    return r.stdout


def _entryscript(inst: dict) -> str:
    last = [l for l in inst["before_repo_set_cmd"].strip().splitlines() if l.strip()][-1]
    tests = ",".join(_as_list(inst["selected_test_files_to_run"]))
    # Image ENV is applied by `docker run` itself, so the upstream env-export block (needed only
    # because Modal sandboxes drop it) is intentionally omitted.
    return (
        "cd /app\n"
        "git reset --hard %s\n"
        "git checkout %s\n"
        "git apply -v /workspace/patch.diff && echo PATCH_APPLIED > /workspace/applied\n"
        "%s\n"
        "bash /workspace/run_script.sh %s > /workspace/stdout.log 2> /workspace/stderr.log\n"
        "python /workspace/parser.py /workspace/stdout.log /workspace/stderr.log "
        "/workspace/output.json\n" % (inst["base_commit"], inst["base_commit"], last, tests))


def grade(inst: dict, patch: str, timeout: int = 3600) -> dict:
    """Run one patch through the official grader. Returns an outcome dict, never raises for a
    normal failure — but never reports `resolved` unless the tests actually said so."""
    iid = inst["instance_id"]
    image = "%s:%s" % (IMAGE_REPO, inst["dockerhub_tag"])
    ws = tempfile.mkdtemp(prefix="proeval-")
    cid = ""
    try:
        repo = _harness_repo()
        files = {"patch.diff": patch,
                 "run_script.sh": _script(repo, iid, "run_script.sh"),
                 "parser.py": _script(repo, iid, "parser.py"),
                 "entry.sh": _entryscript(inst)}
        for name, body in files.items():
            with open(os.path.join(ws, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(body)

        r = subprocess.run(["docker", "create", "--entrypoint", "bash", image,
                            "/workspace/entry.sh"], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return {"outcome": "container_error", "resolved": False, "detail": r.stderr[-300:]}
        cid = r.stdout.strip()
        subprocess.run(["docker", "cp", ws + "/.", cid + ":/workspace"], check=True,
                       capture_output=True, timeout=300)
        run = subprocess.run(["docker", "start", "-a", cid], capture_output=True, text=True,
                             timeout=timeout)

        out = os.path.join(ws, "out")
        os.makedirs(out, exist_ok=True)
        for f in ("output.json", "stdout.log", "stderr.log", "applied"):
            subprocess.run(["docker", "cp", "%s:/workspace/%s" % (cid, f), out],
                           capture_output=True, timeout=120)
        applied = os.path.exists(os.path.join(out, "applied"))
        opath = os.path.join(out, "output.json")
        if not os.path.exists(opath):
            # No parse output at all: the container died, the tests never ran, or the parser blew
            # up. Scoring that as "the agent's patch was wrong" is exactly the lie to avoid.
            tail = ""
            for f in ("stderr.log", "stdout.log"):
                p = os.path.join(out, f)
                if os.path.exists(p):
                    tail = open(p, encoding="utf-8", errors="replace").read()[-500:]
                    if tail.strip():
                        break
            return {"outcome": "patch_did_not_apply" if not applied else "no_test_output",
                    "resolved": False, "container_rc": run.returncode,
                    "detail": (tail or run.stderr or "")[-500:]}

        parsed = json.load(open(opath, encoding="utf-8", errors="replace"))
        passed = {t["name"] for t in parsed.get("tests", []) if t.get("status") == "PASSED"}
        f2p = set(_as_list(inst["fail_to_pass"]))
        p2p = set(_as_list(inst["pass_to_pass"]))
        resolved = (f2p | p2p) <= passed        # official criterion; no partial credit
        return {"outcome": "graded", "resolved": resolved, "patch_applied": applied,
                "f2p_passed": len(f2p & passed), "f2p_total": len(f2p),
                "p2p_passed": len(p2p & passed), "p2p_total": len(p2p),
                "tests_reported": len(parsed.get("tests", []))}
    except subprocess.TimeoutExpired:
        return {"outcome": "timeout", "resolved": False}
    except Exception as e:
        return {"outcome": "harness_error", "resolved": False,
                "detail": "%s: %s" % (type(e).__name__, e)}
    finally:
        if cid:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        shutil.rmtree(ws, ignore_errors=True)


def main(argv) -> int:
    import argparse
    import pandas as pd
    ap = argparse.ArgumentParser(prog="pro_eval")
    ap.add_argument("--patches", required=True, help="paired_eval JSON that still carries patches")
    ap.add_argument("--gold", action="store_true",
                    help="ALSO grade the dataset's own gold patch — a control that proves the "
                         "grader itself works before any agent score is believed")
    a = ap.parse_args(argv)

    with open(a.patches, encoding="utf-8") as f:
        data = json.load(f)
    df = pd.read_parquet("https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro/resolve/main/"
                         "data/test-00000-of-00001.parquet")
    by_id = {r["instance_id"]: r for _, r in df.iterrows()}

    out = []
    for row in data["rows"]:
        inst = by_id.get(row["instance_id"])
        if inst is None:
            print("  ! %s not in dataset" % row["instance_id"][:44])
            continue
        if a.gold and not any(o["harness"] == "gold" and o["instance_id"] == row["instance_id"]
                              for o in out):
            g = grade(inst, inst["patch"])
            g.update({"instance_id": row["instance_id"], "harness": "gold"})
            out.append(g)
            print("  %-44s gold    %s resolved=%s" %
                  (row["instance_id"][:44], g["outcome"], g["resolved"]), flush=True)
        res = grade(inst, row.get("patch") or "")
        res.update({"instance_id": row["instance_id"], "harness": row["harness"],
                    "rep": row.get("rep", 1)})
        out.append(res)
        print("  %-44s %-7s %s resolved=%s %s" %
              (row["instance_id"][:44], row["harness"], res["outcome"], res["resolved"],
               "" if res["outcome"] == "graded" else (res.get("detail") or "")[:80]), flush=True)
        os.makedirs(RESULTS, exist_ok=True)
        with open(os.path.join(RESULTS, "graded.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)

    print()
    tally = {}
    for r in out:
        t = tally.setdefault(r["harness"], {"n": 0, "resolved": 0, "not_graded": 0})
        t["n"] += 1
        t["resolved"] += 1 if r["resolved"] else 0
        t["not_graded"] += 0 if r["outcome"] == "graded" else 1
    for h, t in sorted(tally.items()):
        note = "" if not t["not_graded"] else ("  (%d of these never graded — NOT losses)"
                                               % t["not_graded"])
        print("  %-7s resolved %d/%d%s" % (h, t["resolved"], t["n"], note))
    if any(r["harness"] == "gold" and not r["resolved"] for r in out):
        print("\n  WARNING: a GOLD patch did not resolve. The grader is wrong, not the agents — "
              "no agent number from this run means anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
