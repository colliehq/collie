"""LongMemEval end-to-end — a harder long-term-memory benchmark than LOCOMO (longer
histories ~100k tokens, 6 question types incl. temporal-reasoning & knowledge-update).

Per question: parse the long multi-session history, ingest into collie memory, recall
top-k for the question, DeepSeek answers, DeepSeek judges vs gold. Reports accuracy overall
and by question_type. Ingest raw messages, or --chunk to extract facts per session.

    DEEPSEEK_API_KEY=... OMP_NUM_THREADS=8 python -m bench.longmemeval_eval --n 20 --k 10

Dataset: xiaoyuanliu/longmemeval-s-50 (a 50-question subset of LongMemEval_S).
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness.memory import SqliteMemory
from harness.embeddings import make_embedding, make_reranker
from bench.locomo_e2e import _answer, _judge

def _sessions(conv_str):
    """-> list of (timestamp, [(role, content), ...]). The per-session block is an
    almost-JSON array of {role,content} objects separated by newlines, not commas."""
    out = []
    for block in re.split(r'Session Timestamp:\s*', conv_str):
        block = block.strip()
        if "[" not in block:
            continue
        ts = block.splitlines()[0].strip()
        arr = re.sub(r'\}\s*\{', '},{', block[block.find("["):])   # add missing commas
        try:
            msgs = json.loads(arr)
            out.append((ts, [(m.get("role", ""), m.get("content", "")) for m in msgs]))
        except Exception:
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--rerank", type=int, default=1)
    ap.add_argument("--chunk", default=None, help="deepseek — chunk-extract per session")
    args = ap.parse_args()
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("set DEEPSEEK_API_KEY"); return
    from datasets import load_dataset
    ds = load_dataset("xiaoyuanliu/longmemeval-s-50", split="train")
    items = list(ds)[: args.n]
    emb = make_embedding("local")
    rr = make_reranker("local") if args.rerank else None
    chunk = None
    if args.chunk:
        from harness.distill import make_chunk_extractor
        chunk = make_chunk_extractor(args.chunk)
    print("LongMemEval-s · %d questions · k=%d · rerank=%s · ingest=%s\n"
          % (len(items), args.k, bool(rr), ("chunk:" + args.chunk) if chunk else "raw"))
    by_type = {}
    correct = n = 0
    t0 = time.time()
    for it in items:
        db = tempfile.mktemp(suffix=".db")
        m = SqliteMemory(db, embedder=emb, reranker=rr)
        for ts, msgs in _sessions(it["conversation_str"]):
            if chunk:
                ct = "\n".join("%s: %s" % (r, c) for r, c in msgs)
                for fact in chunk("[%s]\n%s" % (ts, ct)):
                    m.remember(fact, keys=ts, project="lme")
            else:
                for r, c in msgs:
                    m.remember("[%s] %s: %s" % (ts, r, c[:600]), keys=ts, project="lme")
        hits = m.recall(it["question"], project="lme", k=args.k)
        pred = _answer(it["question"], [h["text"] for h in hits])
        ok = _judge(it["question"], str(it.get("answer", "")), pred)
        qt = it.get("question_type", "?")
        by_type.setdefault(qt, [0, 0])
        by_type[qt][0] += 1 if ok else 0
        by_type[qt][1] += 1
        correct += 1 if ok else 0
        n += 1
        m.close()
        try:
            os.remove(db)
        except OSError:
            pass
        if n % 5 == 0:
            print("  ...%d done, acc %.1f%%  (%.0fs)" % (n, 100 * correct / n, time.time() - t0),
                  flush=True)
    print("\nLongMemEval-s accuracy: %.1f%%  (%d/%d, %.0fs)"
          % (100 * correct / max(n, 1), correct, n, time.time() - t0))
    print("by type:", {k: "%d/%d" % (v[0], v[1]) for k, v in by_type.items()})
    print("Reference (LongMemEval_S, DIFFERENT setup): commercial memory systems ~55-75%; "
          "harder than LOCOMO. Directional.")


if __name__ == "__main__":
    main()
