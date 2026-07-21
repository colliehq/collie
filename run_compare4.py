"""4-harness comparison: collie vs Claude Code vs Hermes vs OpenClaw, all on
DeepSeek-V3, 7 tasks, with quality-judge + cost. Hermes/OpenClaw don't emit tokens
headless (prefix/cost shown N/A); success + quality + latency are measured.
Run in background with node24 + ~/.local/bin on PATH and DEEPSEEK_API_KEY set.
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
key = os.environ["DEEPSEEK_API_KEY"]
cc = A.claude_on("https://api.deepseek.com/anthropic", "deepseek-chat", key,
                 key="claude", label="Claude Code (deepseek)")
hermes = A.HermesAdapter()
openclaw = A.OpenClawAdapter()
judge = make_provider("deepseek")

others = [("claude", cc), ("hermes", hermes), ("openclaw", openclaw)]
print("task           harness   succ q/10   ms   note", flush=True)
for task in c.task_suite(facts, full=True):
    c.reset_sandbox(sandbox)
    m = c.run_mh(h, task); c.grade_and_cost(m, task["prompt"], judge); h.recorder.finish_run(m)
    print("%-14s %-8s %5s %4.0f %5d" % (task["id"], "collie", m.success, m.quality, m.wall_ms), flush=True)
    for name, ad in others:
        c.reset_sandbox(sandbox)
        try:
            r = ad.run(task, cwd=sandbox, recorder=h.recorder, model="deepseek-chat", timeout=120)
        except Exception as e:
            print("%-14s %-8s  ERR %s" % (task["id"], name, str(e)[:40]), flush=True); continue
        c.grade_and_cost(r, task["prompt"], judge); h.recorder.finish_run(r)
        print("%-14s %-8s %5s %4.0f %5d  %s" % (
            task["id"], name, r.success, r.quality, r.wall_ms,
            (r.error[:30] if r.error else (r.answer or "")[:30].replace(chr(10), " "))), flush=True)

reval.run_and_save(os.path.join(DATA, "retrieval_eval.json"), embed_name="local")
d.build(runs_db, out_html, True)
d.build(runs_db, "data/dashboard.body.html", False)
h.memory.close(); h.recorder.close()

import sqlite3
db = sqlite3.connect(runs_db)
avg = lambda hn, col: (lambda v: sum(v) / len(v) if v else 0)(
    [x[0] for x in db.execute("select %s from runs where harness=?" % col, (hn,))])
print("\n=== SUMMARY (all DeepSeek-V3) ===", flush=True)
for hn in ("collie", "claude", "hermes", "openclaw"):
    n = db.execute("select count(*) from runs where harness=?", (hn,)).fetchone()[0]
    if not n:
        continue
    print("  %-9s succ=%.0f%%  quality=%.1f/10  prefix=%.0f  lat=%.0fms  (n=%d)" % (
        hn, avg(hn, "success") * 100, avg(hn, "quality"), avg(hn, "prefix_tokens"),
        avg(hn, "wall_ms"), n), flush=True)
print("DONE", flush=True)
