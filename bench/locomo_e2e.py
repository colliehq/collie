"""LOCOMO END-TO-END QA — the protocol Mem0/Zep report (LLM-as-judge accuracy), so the
number is comparable (directionally) to their headline LOCOMO figures.

Pipeline per question: ingest the conversation into collie memory -> recall top-k (with the
cross-encoder reranker, collie's best config) -> DeepSeek answers from the retrieved
snippets -> DeepSeek judges the answer against the gold. Reports accuracy.

    DEEPSEEK_API_KEY=... python -m bench.locomo_e2e --samples 3 --k 10

CAVEATS (why single figures are directional, per the memory research):
- Judge model here is DeepSeek, not GPT-4o-mini (what Mem0 used) -> not identical scoring.
- Excludes category 5 (the disputed adversarial/unanswerable set at the heart of the
  Zep 84% -> Mem0 58% -> Zep 75% -> Mem0 67% LOCOMO dispute).
- collie retrieves top-k snippets; Mem0/Zep use their own memory representations.
"""
import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness.memory import SqliteMemory
from harness.embeddings import make_embedding, make_reranker
from bench.locomo_eval import _turns, DATA

_KEY = os.environ.get("DEEPSEEK_API_KEY", "")


def _ds(messages, max_tokens=256, temperature=0.0):
    body = json.dumps({"model": "deepseek-chat", "messages": messages,
                       "max_tokens": max_tokens, "temperature": temperature}).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions", data=body,
        headers={"content-type": "application/json", "authorization": "Bearer " + _KEY})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())["choices"][0]["message"]["content"].strip()
        except Exception:
            if attempt == 2:
                return ""
            time.sleep(2 ** attempt)


def _answer(question, snippets):
    ctx = "\n".join("- " + s for s in snippets)
    msgs = [{"role": "system", "content":
             "Answer the question using ONLY these memory snippets from a long conversation. "
             "Be concise (a few words). If the snippets don't contain the answer, say "
             "\"No information available.\""},
            {"role": "user", "content": "Memory snippets:\n%s\n\nQuestion: %s" % (ctx, question)}]
    return _ds(msgs, max_tokens=64)


def _judge(question, gold, pred):
    msgs = [{"role": "system", "content":
             "You grade a predicted answer against the gold answer. Reply with exactly "
             "CORRECT or WRONG. CORRECT if the prediction conveys the same key fact as the "
             "gold (allow paraphrase, extra words, different formatting of dates/numbers)."},
            {"role": "user", "content": "Question: %s\nGold: %s\nPredicted: %s" % (
                question, gold, pred)}]
    return "CORRECT" in (_ds(msgs, max_tokens=8).upper())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--rerank", type=int, default=1, help="1=with reranker (best), 0=hybrid only")
    ap.add_argument("--distill", default=None, help="e.g. deepseek — per-turn extract on ingest")
    ap.add_argument("--chunk", default=None, help="e.g. deepseek — CHUNK-level (per-session) extract")
    args = ap.parse_args()
    if not _KEY:
        print("set DEEPSEEK_API_KEY"); return
    data = json.load(open(DATA, encoding="utf-8"))[: args.samples]
    emb = make_embedding("local")
    rr = make_reranker("local") if args.rerank else None
    dist = None
    if args.distill:
        from harness.distill import make_distiller
        dist = make_distiller(args.distill)
    chunk = None
    if args.chunk:
        from harness.distill import make_chunk_extractor
        chunk = make_chunk_extractor(args.chunk)
    mode = ("chunk-extract:" + args.chunk) if chunk else ("distill:" + args.distill if dist else "raw")
    print("LOCOMO end-to-end QA (LLM-as-judge) · %d conv · k=%d · rerank=%s · ingest=%s\n"
          % (len(data), args.k, bool(rr), mode))
    correct = n = 0
    t0 = time.time()
    for s in data:
        db = tempfile.mktemp(suffix=".db")
        m = SqliteMemory(db, embedder=emb, reranker=rr, distiller=dist)
        if chunk:                                        # CHUNK-level: extract facts per session
            conv = s["conversation"]
            for sk in sorted((k for k in conv if k.startswith("session_")
                              and not k.endswith("date_time")),
                             key=lambda x: int(x.split("_")[1])):
                ct = "\n".join("%s: %s" % (t.get("speaker", ""), t.get("text", ""))
                               for t in conv[sk])
                for fact in chunk(ct):
                    m.remember(fact, keys=sk, project="loco")
        else:
            for dia, text in _turns(s["conversation"]):
                m.remember(text, keys=dia, project="loco")   # -1 (dropped) is fine, ignored
        for qa in s["qa"]:
            if qa.get("category") == 5 or not (qa.get("evidence") or []):
                continue
            hits = m.recall(qa["question"], project="loco", k=args.k)
            snaps = [h["text"] for h in hits]
            pred = _answer(qa["question"], snaps)
            if _judge(qa["question"], str(qa.get("answer", "")), pred):
                correct += 1
            n += 1
            if n % 50 == 0:
                print("  ...%d judged, running acc %.1f%%  (%.0fs)"
                      % (n, 100 * correct / n, time.time() - t0), flush=True)
        m.close()
        try:
            os.remove(db)
        except OSError:
            pass
    print("\nLOCOMO end-to-end accuracy: %.1f%%  (%d/%d, %.0fs)"
          % (100 * correct / max(n, 1), correct, n, time.time() - t0))
    print("Reference (vendor-reported LOCOMO LLM-judge, DIFFERENT judge/config): "
          "Mem0 ~66.9%%, Zep ~75%% (disputed). Treat as directional.")


if __name__ == "__main__":
    main()
