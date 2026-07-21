"""Definitive comparison run: collie vs Claude Code, both on DeepSeek-V3, 6 rich
tasks, with LLM-judge quality + $ cost + precision@k. Run in background (slow: CC
on DeepSeek is ~10-40s/task). Usage: DEEPSEEK_API_KEY=... .venv/bin/python run_compare.py
"""
import os, warnings
warnings.filterwarnings("ignore")
from harness import compare as c, adapters as A, dashboard as d, reval
from harness.cli import make_harness, _paths, DATA
from harness.providers import make_provider

mem_db, runs_db, out_html, sandbox = _paths()
for p in (runs_db, mem_db):
    if os.path.exists(p):
        os.remove(p)
os.makedirs(sandbox, exist_ok=True)
facts = c.build_sandbox(sandbox)
h = make_harness(sandbox, provider="deepseek", project="demo")
h.memory.remember("collie internalizes embeddings: jina-embeddings-v3 + sqlite-vec + FTS5 hybrid.",
                  keys="embedding memory design", project="demo")
cc = A.claude_on("https://api.deepseek.com/anthropic", "deepseek-chat",
                 os.environ["DEEPSEEK_API_KEY"], key="claude", label="Claude Code (deepseek)")
judge = make_provider("deepseek")

print("%-14s %-8s %6s %5s %4s %7s %s" % ("task", "harness", "prefix", "succ", "q", "$", "ms"), flush=True)
for task in c.task_suite(facts, full=True):
    c.reset_sandbox(sandbox)
    m = c.run_mh(h, task); c.grade_and_cost(m, task["prompt"], judge); h.recorder.finish_run(m)
    print("%-14s %-8s %6d %5s %4.0f %7.4f %d" % (
        task["id"], "collie", m.prefix_tokens, m.success, m.quality, m.cost_usd, m.wall_ms), flush=True)
    c.reset_sandbox(sandbox)
    r = cc.run(task, cwd=sandbox, recorder=h.recorder, model="deepseek-chat", timeout=150)
    c.grade_and_cost(r, task["prompt"], judge); h.recorder.finish_run(r)
    print("%-14s %-8s %6d %5s %4.0f %7.4f %d%s" % (
        task["id"], "CC", r.prefix_tokens, r.success, r.quality, r.cost_usd, r.wall_ms,
        ("  ERR:" + r.error[:24] if r.error else "")), flush=True)

reval.run_and_save(os.path.join(DATA, "retrieval_eval.json"), embed_name="local")
d.build(runs_db, out_html, True)
d.build(runs_db, "data/dashboard.body.html", False)
h.memory.close(); h.recorder.close()

import sqlite3
db = sqlite3.connect(runs_db)
avg = lambda hn, col: (lambda v: sum(v) / len(v) if v else 0)(
    [x[0] for x in db.execute("select %s from runs where harness=?" % col, (hn,))])
tot = lambda hn, col: sum(x[0] or 0 for x in db.execute("select %s from runs where harness=?" % col, (hn,)))
print("\n=== SUMMARY (both DeepSeek-V3) ===", flush=True)
for hn in ("collie", "claude"):
    print("  %-7s prefix=%.0f  succ=%.0f%%  quality=%.1f/10  cost=$%.4f  lat=%.0fms" % (
        hn, avg(hn, "prefix_tokens"), avg(hn, "success") * 100, avg(hn, "quality"),
        tot(hn, "cost_usd"), avg(hn, "wall_ms")), flush=True)
print("DONE", flush=True)
