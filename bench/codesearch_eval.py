"""code_search localization eval — does collie's semantic code-nav surface the file(s) a
fix must touch? This is the harness lever for SWE resolve-rate (locate-before-edit), and
the one retrieval spot with NO reranker, so the base embedder matters most here.

For N SWE-bench Verified instances: clone @ base_commit, build the code index with a given
embedder, run code_search(problem_statement), and check whether the GOLD-patch file(s) land
in the top-k. Reports file-hit@k (>=1 gold file retrieved) and gold-file recall@k.

    OMP_NUM_THREADS=6 COLLIE_EMBED_THREADS=6 python -m bench.codesearch_eval --n 8 --k 10
"""
import argparse
import os
import re
import sys
import tempfile
import subprocess
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness import swe
from harness.codeindex import CodeIndex
from harness.embeddings import make_embedding, make_reranker

REPOS = ["pallets/flask", "psf/requests", "pylint-dev/pylint", "sphinx-doc/sphinx",
         "pytest-dev/pytest", "pydata/xarray", "mwaskom/seaborn"]


def _pick(ds, n):
    by = {}
    for r in ds:
        if r["repo"] in REPOS:
            by.setdefault(r["repo"], []).append(r)
    out, i = [], 0
    while len(out) < n and any(by.values()):
        rp = REPOS[i % len(REPOS)]; i += 1
        if by.get(rp):
            out.append(by[rp].pop(0))
    return out


def _gold_files(patch):
    return set(re.findall(r'^\+\+\+ b/(\S+)', patch, re.M)) or \
        set(re.findall(r'diff --git a/(\S+)', patch))


def run(instances, k, embed_name, rerankers):
    """Clone+build ONCE per instance, then search under each reranker variant on the SAME
    built index (fair same-index A/B). rerankers = [None] or [None, cross_encoder]."""
    emb = make_embedding(embed_name)
    agg = {id(rr): {"hit": 0, "recall": 0, "rr": rr} for rr in rerankers}
    n = 0
    t0 = time.time()
    for inst in instances:
        wd = tempfile.mkdtemp(prefix="cs_")
        try:
            swe.prepare_repo(inst["repo"], inst["base_commit"], wd)
            idx = CodeIndex(wd, embedder=emb)
            idx.build()
            gold = _gold_files(inst["patch"])
            for rr in rerankers:
                idx.reranker = rr
                hits = idx.search(inst["problem_statement"][:2000], k=k)
                got = {h.split(":")[0] for h in hits}      # "path:line-line\n..." -> path
                found = sum(1 for g in gold if g in got)
                agg[id(rr)]["hit"] += 1 if found else 0
                agg[id(rr)]["recall"] += found / len(gold) if gold else 0
            n += 1
        finally:
            subprocess.run(["rm", "-rf", wd], check=False)
    sec = round(time.time() - t0, 1)
    out = []
    for rr in rerankers:
        a = agg[id(rr)]
        tag = emb.name + ("+" + rr.name if rr else "")
        out.append({"embed": tag, "file_hit@k": round(a["hit"] / n, 3),
                    "gold_recall@k": round(a["recall"] / n, 3), "n": n, "sec": sec})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--embeds", default="local:BAAI/bge-small-en-v1.5")
    ap.add_argument("--rerank", type=int, default=0, help="1=also run each embedder WITH the cross-encoder")
    args = ap.parse_args()
    from datasets import load_dataset
    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    inst = _pick(ds, args.n)
    print("code_search localization · %d instances · k=%d\n" % (len(inst), args.k))
    print("%-42s %-11s %-13s %-5s %s" % ("embedder", "file_hit@k", "gold_recall@k", "n", "sec"))
    rr = make_reranker("local") if args.rerank else None
    rerankers = [None, rr] if rr else [None]
    for name in args.embeds.split(","):
        for r in run(inst, args.k, name.strip(), rerankers):
            print("%-42s %-11s %-13s %-5d %s" % (
                r["embed"], r["file_hit@k"], r["gold_recall@k"], r["n"], r["sec"]), flush=True)


if __name__ == "__main__":
    main()
