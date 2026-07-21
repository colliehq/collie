"""ULTRA best-of-k, measured WITHOUT new compute: reuse the 3 collie_verify multi-run patch
sets (mr_collie_verify_r1/2/3) as the k candidates, apply oracle-free selection (consensus
-> LLM judge), and write an ultra prediction file. Then eval it and compare to pass@1 / pass@3.

If ultra's realized resolve approaches pass@3 (50%), selection works and it's a REAL gain over
single-run (44%); if it sticks at pass@1, selection can't tell good patches from bad.

    python -m bench.ultra_select        # writes preds/mr_collie_ultra.jsonl
    (then eval it with run_id mr_collie_ultra and compare)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness import swe

RUNS = ["mr_collie_verify_r1", "mr_collie_verify_r2", "mr_collie_verify_r3"]
IDS = [l.strip() for l in open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ids16.txt")) if l.strip()]


def _load(run):
    d = {}
    p = "preds/%s.jsonl" % run
    if os.path.exists(p):
        for line in open(p):
            r = json.loads(line)
            d[r["instance_id"]] = r.get("model_patch", "") or ""
    return d


def main():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("set DEEPSEEK_API_KEY"); return
    from datasets import load_dataset
    ds = {r["instance_id"]: r for r in load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
          if r["instance_id"] in IDS}
    cand = {run: _load(run) for run in RUNS}
    out = open("preds/mr_collie_ultra.jsonl", "w")
    n_consensus = n_judge = n_empty = 0
    for iid in IDS:
        patches = [cand[run].get(iid, "") for run in RUNS]
        nonempty = [p for p in patches if p.strip()]
        if not nonempty:
            winner = ""; n_empty += 1
        else:
            from collections import Counter
            norm = [swe._norm_patch(p) for p in nonempty]
            common, cnt = Counter(norm).most_common(1)[0]
            if cnt >= 2:                       # self-consistency
                winner = nonempty[norm.index(common)]; n_consensus += 1
            else:                              # all distinct -> judge
                winner = nonempty[swe._judge_patch(ds[iid]["problem_statement"], nonempty)]
                n_judge += 1
        out.write(json.dumps({"instance_id": iid, "model_name_or_path": "collie_ultra",
                              "model_patch": winner}, ensure_ascii=False) + "\n")
    out.close()
    print("ultra selection: consensus=%d judge=%d empty=%d (of %d)" % (n_consensus, n_judge, n_empty, len(IDS)))
    print("wrote preds/mr_collie_ultra.jsonl")


if __name__ == "__main__":
    main()
