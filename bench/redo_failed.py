# -*- coding: utf-8 -*-
"""Re-run only the runs that FAILED for harness reasons, and merge them back.

A run that died because the CLI hit its session limit is not a result — it is a missing
observation. Dropping the instance would shrink the sample; keeping it would score a quota
outage as a loss. Redoing just those runs is the only option that neither lies nor wastes the
other 37 runs' worth of quota.

Only rows with a non-empty `error` are redone. Rows that produced a patch are never touched, so
this cannot be used to quietly re-roll a bad-looking result.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench.paired_eval import run_collie, run_claude, load_instances   # noqa: E402


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="redo_failed")
    ap.add_argument("--patches", required=True)
    ap.add_argument("--model", default="claude-sonnet-5")
    a = ap.parse_args(argv)

    with open(a.patches, encoding="utf-8") as f:
        data = json.load(f)
    rows = data["rows"]
    todo = [i for i, r in enumerate(rows) if r.get("error")]
    if not todo:
        print("nothing to redo — no row carries an error")
        return 0
    print("redoing %d failed runs:" % len(todo))
    for i in todo:
        print("   %-38s %-7s rep%s  %s" % (rows[i]["instance_id"][9:47], rows[i]["harness"],
                                           rows[i]["rep"], rows[i]["error"][:60]))

    by_id = {i["instance_id"]: i for i in load_instances(0)}   # 0 = whole set, no sampling
    from harness import swe
    for i in todo:
        old = rows[i]
        inst = by_id.get(old["instance_id"])
        if inst is None:
            print("  ! %s vanished from the dataset" % old["instance_id"][:44])
            continue
        wd = tempfile.mkdtemp(prefix="redo-repo-")
        try:
            swe.prepare_repo(inst["repo"], inst["base_commit"], wd)
            new = (run_collie(inst, wd, a.model, old["rep"]) if old["harness"] == "collie"
                   else run_claude(inst, wd, a.model, old["rep"]))
        except Exception as e:
            print("  ! prepare/run failed: %s: %s" % (type(e).__name__, e))
            shutil.rmtree(wd, ignore_errors=True)
            continue
        shutil.rmtree(wd, ignore_errors=True)
        new["instance_id"] = old["instance_id"]
        new["redone_after"] = old["error"][:120]      # keep the trace of why this row was redone
        rows[i] = new
        print("  %-38s %-7s rep%s  patch=%-6d %5.0fs %s" %
              (new["instance_id"][9:47], new["harness"], new["rep"], new["patch_bytes"],
               new["secs"], (new.get("error") or "")[:50]), flush=True)
        with open(a.patches, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        stripped = a.patches.replace("-patches.json", ".json")
        if os.path.exists(stripped):
            with open(stripped, encoding="utf-8") as f:
                sd = json.load(f)
            sd["rows"] = [{k: v for k, v in x.items() if k != "patch"} for x in rows]
            with open(stripped, "w", encoding="utf-8") as f:
                json.dump(sd, f, ensure_ascii=False, indent=1)

    left = sum(1 for r in rows if r.get("error"))
    print("\nremaining failed rows: %d" % left)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
