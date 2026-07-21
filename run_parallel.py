"""Parallel comparison runner — 4 harnesses per task run CONCURRENTLY, each in its
own isolated sandbox + its own recorder connection (WAL). Tasks are sequential so
OpenClaw never runs against itself concurrently. ~3-4x faster than the serial run.

Env: node24 + ~/.local/bin on PATH, DEEPSEEK_API_KEY set.
Args: --no-openclaw  --no-judge  --tasks basic|full
"""
import os, sys, warnings, threading
from concurrent.futures import ThreadPoolExecutor
warnings.filterwarnings("ignore")
from harness import compare as c, adapters as A, dashboard as d, reval
from harness.cli import _paths, DATA
from harness.memory import SqliteMemory
from harness.tools import default_registry
from harness.context import ContextComposer, TokenBudgeter
from harness.recorder import Recorder
from harness.loop import Harness
from harness.providers import make_provider
from harness.embeddings import make_embedding

_, runs_db, out_html, _ = _paths()
for p in (runs_db, os.path.join(DATA, "memory.db")):
    if os.path.exists(p):
        os.remove(p)
for ext in ("-wal", "-shm"):
    if os.path.exists(runs_db + ext):
        os.remove(runs_db + ext)

NO_OC = "--no-openclaw" in sys.argv
NO_JUDGE = "--no-judge" in sys.argv
FULL = "--tasks" not in sys.argv or "basic" not in sys.argv

EMBEDDER = make_embedding("local")            # load jina-v3 ONCE, shared by collie runs
KEY = os.environ["DEEPSEEK_API_KEY"]
JUDGE = None if NO_JUDGE else make_provider("deepseek")
_print_lock = threading.Lock()


def sbx_for(name, task):
    p = os.path.join(DATA, "sbx_%s_%s" % (name, task["id"]))
    os.makedirs(p, exist_ok=True)
    c.build_sandbox(p)
    return p


def emit(row):
    with _print_lock:
        print(row, flush=True)


def do_collie(task):
    sbx = sbx_for("collie", task)
    mem = SqliteMemory(os.path.join(DATA, "mem_%s.db" % task["id"]), embedder=EMBEDDER)
    mem.remember("collie internalizes embeddings: jina-embeddings-v3 + sqlite-vec + FTS5 hybrid.",
                 keys="embedding memory design", project="demo")
    rec = Recorder(runs_db)
    h = Harness(make_provider("deepseek"), mem, default_registry(),
                ContextComposer(mem, default_registry(), TokenBudgeter()), rec,
                cwd=sbx, project="demo")
    r = c.run_mh(h, task)
    c.grade_and_cost(r, task["prompt"], JUDGE); rec.finish_run(r)
    mem.close(); rec.close()
    emit("%-15s %-8s succ=%-5s q=%-4.0f %dms" % (task["id"], "collie", r.success, r.quality, r.wall_ms))


def do_adapter(name, ad, task):
    sbx = sbx_for(name, task)
    rec = Recorder(runs_db)
    r = ad.run(task, cwd=sbx, recorder=rec, model="deepseek-chat", timeout=120)
    c.grade_and_cost(r, task["prompt"], JUDGE); rec.finish_run(r)
    rec.close()
    emit("%-15s %-8s succ=%-5s q=%-4.0f %dms %s" % (
        task["id"], name, r.success, r.quality, r.wall_ms,
        (r.error[:24] if r.error else "")))


facts = c.build_sandbox(os.path.join(DATA, "sandbox"))
cc = A.claude_on("https://api.deepseek.com/anthropic", "deepseek-chat", KEY,
                 key="claude", label="Claude Code (deepseek)")
jobs = [("claude", cc), ("hermes", A.HermesAdapter())]
if not NO_OC:
    jobs.append(("openclaw", A.OpenClawAdapter()))

import time
t0 = time.time()
for task in c.task_suite(facts, full=FULL):
    with ThreadPoolExecutor(max_workers=1 + len(jobs)) as ex:
        futs = [ex.submit(do_collie, task)]
        futs += [ex.submit(do_adapter, n, a, task) for n, a in jobs]
        for f in futs:
            try:
                f.result()
            except Exception as e:
                emit("  worker error: %s" % e)

reval.run_and_save(os.path.join(DATA, "retrieval_eval.json"), embed_name="local")
d.build(runs_db, out_html, True)
d.build(runs_db, "data/dashboard.body.html", False)

import sqlite3
db = sqlite3.connect(runs_db)
avg = lambda hn, col: (lambda v: sum(v) / len(v) if v else 0)(
    [x[0] for x in db.execute("select %s from runs where harness=?" % col, (hn,))])
print("\n=== SUMMARY (parallel, %.0fs wall) ===" % (time.time() - t0), flush=True)
for hn in ("collie", "claude", "hermes", "openclaw"):
    n = db.execute("select count(*) from runs where harness=?", (hn,)).fetchone()[0]
    if n:
        print("  %-9s succ=%.0f%%  q=%.1f  prefix=%.0f  lat=%.0fms  (n=%d)" % (
            hn, avg(hn, "success") * 100, avg(hn, "quality"), avg(hn, "prefix_tokens"),
            avg(hn, "wall_ms"), n), flush=True)
print("DONE", flush=True)
