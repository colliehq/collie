"""LOCOMO retrieval eval — the standard agent-memory benchmark, measuring the part
collie's memory subsystem actually owns: RETRIEVAL. For each question we ingest the whole
multi-session conversation into collie memory, recall top-k, and check whether the
annotated `evidence` turns are retrieved. Reports recall@k and hit@k (≥1 evidence turn),
comparing hybrid retrieval with and without the cross-encoder reranker.

    python -m bench.locomo_eval --samples 3 --k 10

Data: snap-research/locomo `locomo10.json` (10 conversations). Category 5 is the disputed
adversarial/unanswerable set — excluded (no evidence to retrieve).
"""
import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness.memory import SqliteMemory
from harness.embeddings import make_embedding, make_reranker

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "bench", "locomo10.json")


def _turns(conv):
    for key in sorted((k for k in conv if k.startswith("session_")
                       and not k.endswith("date_time")),
                      key=lambda s: int(s.split("_")[1])):
        for t in conv[key]:
            yield t["dia_id"], "%s: %s" % (t.get("speaker", ""), t.get("text", ""))


def _score(m, qas, k, rid2dia):
    tr = th = nq = 0
    for qa in qas:
        ev = qa.get("evidence") or []
        if qa.get("category") == 5 or not ev:          # skip adversarial / no-evidence
            continue
        got = {rid2dia.get(h["id"]) for h in m.recall(qa["question"], project="locomo", k=k)}
        found = sum(1 for e in ev if e in got)
        tr += found / len(ev)
        th += 1 if found else 0
        nq += 1
    return tr, th, nq


def eval_both(samples, k, embedder, reranker, distiller=None):
    """Ingest each conversation ONCE, then score with reranker off then on."""
    agg = {"base": [0, 0, 0, 0.0], "rerank": [0, 0, 0, 0.0]}   # tr, th, nq, sec
    for s in samples:
        db = tempfile.mktemp(suffix=".db")
        m = SqliteMemory(db, embedder=embedder, distiller=distiller)
        rid2dia = {}
        for dia, text in _turns(s["conversation"]):
            rid = m.remember(text, keys=dia, project="locomo")
            if rid != -1:                                  # -1 = distiller dropped chit-chat
                rid2dia[rid] = dia
        for cfg, rr in (("base", None), ("rerank", reranker)):
            if rr is False:
                continue
            m.reranker = rr
            t0 = time.time()
            tr, th, nq = _score(m, s["qa"], k, rid2dia)
            a = agg[cfg]
            a[0] += tr; a[1] += th; a[2] += nq; a[3] += time.time() - t0
        m.close()
        try:
            os.remove(db)
        except OSError:
            pass

    def fin(a):
        return {"recall@k": round(a[0] / a[2], 3), "hit@k": round(a[1] / a[2], 3),
                "n_q": a[2], "sec": round(a[3], 1)}
    return fin(agg["base"]), fin(agg["rerank"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--embed", default="local")
    ap.add_argument("--distill", default=None, help="e.g. deepseek — extract facts on ingest")
    args = ap.parse_args()
    data = json.load(open(DATA, encoding="utf-8"))[: args.samples]
    emb = make_embedding(args.embed)
    dist = None
    if args.distill:
        from harness.distill import make_distiller
        dist = make_distiller(args.distill)
    print("LOCOMO retrieval eval · %d conversations · k=%d · embed=%s · distill=%s\n"
          % (len(data), args.k, emb.name, args.distill or "off"))
    print("%-26s %-9s %-8s %-6s %s" % ("config", "recall@k", "hit@k", "n_q", "sec"))
    try:
        rr = make_reranker("local")
    except Exception as e:
        rr, _ = None, print("  (reranker unavailable: %s)" % e)
    base, rk = eval_both(data, args.k, emb, rr, distiller=dist)
    print("%-26s %-9s %-8s %-6d %s" % ("hybrid (BM25+dense+RRF)",
          base["recall@k"], base["hit@k"], base["n_q"], base["sec"]))
    if rr is not None:
        print("%-26s %-9s %-8s %-6d %s" % ("  + cross-encoder rerank",
              rk["recall@k"], rk["hit@k"], rk["n_q"], rk["sec"]))


if __name__ == "__main__":
    main()
