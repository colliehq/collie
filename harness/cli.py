"""collie CLI — the entrypoint you (or Claude) invoke to run and test the harness.

    python -m harness.cli selftest          # $0 deterministic end-to-end (mock)
    python -m harness.cli run "<task>" [--provider mock|anthropic] [--model M]
    python -m harness.cli compare [--cc off|baseline|real] [--provider ...]
    python -m harness.cli dashboard         # rebuild data/dashboard.html
    python -m harness.cli mem search "<q>"  |  mem add "<text>"
"""
from __future__ import annotations
import argparse
import os
import re
import sys

from .providers import make_provider
from .embeddings import make_embedding
from .memory import SqliteMemory
from .tools import default_registry
from .context import ContextComposer, TokenBudgeter
from .recorder import Recorder
from .loop import Harness
from . import compare as cmp
from . import dashboard as dash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def _paths():
    os.makedirs(DATA, exist_ok=True)
    return (os.path.join(DATA, "memory.db"),
            os.path.join(DATA, "runs.db"),
            os.path.join(DATA, "dashboard.html"),
            os.path.join(DATA, "sandbox"))


def _embedder(embed="auto"):
    """auto -> resident daemon (warm model, fast) if fastembed available, else hash.
    Overrides: COLLIE_EMBED=hash|local|daemon forces a backend; COLLIE_EMBED_DAEMON=0 keeps
    the model in-process (no daemon)."""
    embed = os.environ.get("COLLIE_EMBED", embed)
    if embed in ("auto", "daemon", "local"):
        use_daemon = embed != "local" and os.environ.get("COLLIE_EMBED_DAEMON") != "0"
        try:
            return make_embedding("daemon" if use_daemon else "local")
        except Exception as e:
            # stderr, NOT stdout — `run --json`/`--stream-json` promise machine-readable stdout,
            # and this fires on every fastembed-less install (the default pipx one). Say WHY and
            # the cure, or users sit on keyword-only retrieval thinking they have the real thing.
            if isinstance(e, ImportError):
                why, fix = ("fastembed not installed",
                            "pipx inject collie-harness fastembed  (or pip install collie-harness[local])")
            else:
                why = "%s: %s" % (type(e).__name__, str(e)[:100])
                fix = ("model download failed twice (huggingface.co + hf-mirror.com) — check the "
                       "network, or set an intranet mirror: COLLIE_HF_ENDPOINT=<url>")
            print("  [embed] local unavailable (%s) -> HashEmbedding (keyword-only, degraded)\n"
                  "  [embed] fix: %s" % (why, fix), file=sys.stderr)
            return make_embedding("hash")
    return make_embedding(embed)


def make_harness(cwd, provider="mock", model=None, project="demo",
                 embed="auto", prefix_ceiling=6000, browser=None, code_search=False,
                 rerank=None, distill=None, web_search=None, exec_code=False, delegate=False):
    from .embeddings import make_reranker
    from .distill import make_distiller
    mem_db, runs_db, _, _ = _paths()
    rr = make_reranker(rerank or os.environ.get("COLLIE_RERANK"))   # opt-in cross-encoder
    ds = make_distiller(distill or os.environ.get("COLLIE_DISTILL"))  # opt-in extraction
    memory = SqliteMemory(mem_db, embedder=_embedder(embed), reranker=rr, distiller=ds)
    if browser is None:                       # off | headless | headed | 1 | on  (Settings panel)
        browser = os.environ.get("COLLIE_BROWSER", "off") not in ("0", "off", "false", "")
    if web_search is None:
        web_search = os.environ.get("COLLIE_WEBSEARCH", "") in ("1", "on", "true")
    registry = default_registry(browser=browser, code_search=code_search, web_search=web_search,
                                exec_code=exec_code, delegate=delegate)
    composer = ContextComposer(memory, registry, TokenBudgeter(prefix_ceiling))
    recorder = Recorder(runs_db)
    prov = make_provider(provider, model)
    h = Harness(prov, memory, registry, composer, recorder, cwd=cwd, project=project)
    try:                                      # Settings-panel turn limit (env/JSON), else keep default
        mt = os.environ.get("COLLIE_MAX_TURNS")
        if mt:
            h.max_turns = max(1, min(120, int(mt)))
    except (TypeError, ValueError):
        pass
    return h


# --------------------------------------------------------------------------- #
def cmd_selftest(args):
    mem_db, runs_db, out_html, sandbox = _paths()
    os.makedirs(sandbox, exist_ok=True)
    facts = cmp.build_sandbox(sandbox)

    h = make_harness(sandbox, provider="mock", project="demo")
    # seed a durable design decision so the recall task has something to find
    h.memory.remember(
        "We decided to internalize embeddings: local bge-m3 via fastembed feeding "
        "sqlite-vec + FTS5 hybrid retrieval, so memory recall is $0 and fast.",
        keys="embedding memory design bge-m3 hybrid", project="demo")
    h.memory.set_block("project:demo", "goal",
                       "Build an evolvable coding harness; beat Claude Code on prefix tokens.",
                       char_limit=200)

    print("== collie selftest (mock provider, $0) ==")
    results = []
    for task in cmp.task_suite(facts, full=False):
        res = cmp.run_collie(h, task)
        results.append(res)
        print("  [%s] %-14s prefix=%-5d turns=%d tools=%d recall=%d %dms  ->  %s" % (
            "PASS" if res.success else "FAIL", task["id"], res.prefix_tokens,
            res.turns, res.tool_calls, res.mem_recalls, res.wall_ms,
            (res.answer or res.error)[:60].replace("\n", " ")))

    cmp.cc_baseline(h.recorder)   # reference prefix row for the dashboard
    dash.build(runs_db, out_html)
    npass = sum(r.success for r in results)
    print("\n  %d/%d tasks passed · memory facts=%d" % (
        npass, len(results), h.memory.count("demo")))
    print("  dashboard -> %s" % out_html)
    h.memory.close(); h.recorder.close()
    return 0 if npass == len(results) else 1


def cmd_loop(args):
    """Autonomous goal-directed loop: pin a goal, iterate the agent (memory carried across
    iterations), stop when an executed check passes (--until) or after --max iterations.
    On brand with collie's executed-verification identity — the loop ends on real green, not
    the model's say-so."""
    import subprocess as _sp
    cwd = args.cwd or os.getcwd()
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")
    h = make_harness(cwd, provider=provider, model=args.model, project=args.project,
                     code_search=True, web_search=True, exec_code=True, delegate=True)
    goal = args.goal or args.task
    if goal:
        h.memory.set_block("project:" + args.project, "goal", goal[:390], char_limit=400)
    task = args.task or ("Make progress toward the goal above. Do one concrete step this turn.")
    stopped = False
    try:
        for i in range(args.max):
            print("\n── collie loop · iteration %d/%d ──" % (i + 1, args.max), flush=True)
            res = h.run("loop", task, consolidate=True)   # consolidate -> memory carries forward
            print(res.answer or res.error or "(no output)", flush=True)
            if args.until:
                rc = _sp.run(args.until, shell=True, cwd=cwd).returncode
                print("  [until] `%s` → exit %d" % (args.until, rc), flush=True)
                if rc == 0:
                    print("✓ goal condition met — stopping."); stopped = True; break
        if not stopped and args.until:
            print("✗ reached --max %d without the goal condition passing." % args.max)
    finally:
        h.memory.close(); h.recorder.close()
    return 0


def cmd_repl(args):
    """Interactive REPL — a lightweight readline chat that keeps the FULL conversation thread
    across turns (and persists it as a session, so you can --resume later). collie's answer to
    'no interactive mode' without a heavy TUI: one input() loop over the same harness."""
    from . import sessions as sess
    cwd = args.cwd or os.getcwd()
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")
    h = make_harness(cwd, provider=provider, model=args.model, project=args.project,
                     code_search=True, web_search=True, exec_code=True, delegate=True)
    sid = args.resume or (sess.latest() if getattr(args, "cont", False) else None) or sess.new_id()
    loaded = sess.load(sid) if (args.resume or getattr(args, "cont", False)) else None
    history = (loaded or {}).get("messages") or []
    if getattr(args, "goal", None):
        h.memory.set_block("project:" + args.project, "goal", args.goal[:390], char_limit=400)
    print("collie repl · session %s · %s · %d prior turns · /exit to quit, /new for a fresh thread"
          % (sid, provider, sum(1 for m in history if m.get("role") == "user")))
    try:
        while True:
            try:
                line = input("\n› ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in ("/exit", "/quit"):
                break
            if line == "/new":
                history, sid = [], sess.new_id()
                print("  [new session %s]" % sid)
                continue
            res = h.run("repl", line, consolidate=True, history=history)
            print("\n" + (res.answer or res.error or "(no output)"))
            history = res.messages
            sess.save(sid, history, project=args.project, cwd=cwd, answer=res.answer or "")
    finally:
        h.memory.close(); h.recorder.close()
        print("\nsession saved: %s  ·  resume: collie repl --resume %s" % (sid, sid))
    return 0


def cmd_tui(args):
    """Rich terminal TUI — friendly interactive chat with a live tool/gate/diff timeline."""
    from .tui import run_tui
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")
    return run_tui(args.cwd or os.getcwd(), provider, args.model, project=args.project,
                   resume=args.resume, cont=getattr(args, "cont", False), goal=args.goal)


def cmd_web(args):
    """Serve the local web GUI (streams the verification gate live over SSE)."""
    from .webapp import main as web_main
    argv = ["--port", str(args.port)]
    if not args.open:
        argv.append("--no-open")
    return web_main(argv)


def _desktop_window(url, kiosk=False):
    """From WSL, pop a borderless Edge window on the Windows desktop showing `url` — a *real* window,
    so clicks/typing are 100% reliable (unlike a behind-icons wallpaper, where the shell eats clicks).
    Uses the user's own Edge profile (logged-in). Returns (ok, detail)."""
    import shutil, subprocess
    ps = shutil.which("powershell.exe")
    if not ps:
        return False, ("no powershell.exe — `collie wallpaper` drives a Windows desktop from WSL; "
                       "on native Windows just open %s in a browser" % url)
    flags = ["'--kiosk'", "'--edge-kiosk-type=fullscreen'"] if kiosk else ["'--start-maximized'"]
    argl = ",".join(["'--app=%s'" % url] + flags) + \
        ",('--user-data-dir=' + $env:LOCALAPPDATA + '\\collie-desktop')"
    script = (
        "$e=@('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',"
        "'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe')|?{Test-Path $_}|Select-Object -First 1;"
        "if(-not $e){Write-Error 'edge-not-found';exit 3};"
        "Start-Process $e -ArgumentList " + argl
    )
    try:
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=25)
    except Exception as e:
        return False, "launch error: %s" % e
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or ("powershell exit %d" % r.returncode)).strip()
    return True, "opened"


def cmd_wallpaper(args):
    """Put collie's live desktop on screen and let collie OWN it — no third-party wallpaper engine.
    Starts the web server if it isn't already up, then pops a borderless full-screen window at
    /wallpaper. Real window ⇒ reliable clicks/typing; auto-reloads on restart ⇒ never hand-refresh."""
    import time, threading, urllib.request
    port = args.port
    url = "http://127.0.0.1:%d/wallpaper" % port

    def _up():
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/ver" % port, timeout=0.8).read()
            return True
        except Exception:
            return False

    if _up():                                   # a server is already running — just open the window
        ok, detail = _desktop_window(url, kiosk=args.kiosk)
        print("collie wallpaper · %s · %s" % (url, "window opened" if ok else "no window: " + detail))
        return 0 if ok else 1

    # no server yet: start one, and open the window the moment it starts accepting connections
    def _delayed():
        for _ in range(60):
            if _up():
                break
            time.sleep(0.2)
        ok, detail = _desktop_window(url, kiosk=args.kiosk)
        print(("collie wallpaper · window opened" if ok else
               "collie wallpaper · could not open window (%s) — open %s yourself" % (detail, url)),
              flush=True)
    threading.Thread(target=_delayed, daemon=True).start()
    from .webapp import main as web_main
    return web_main(["--port", str(port), "--no-open"])


def cmd_browser_bridge(args):
    """Run the browser-bridge server (the Chrome extension polls it; browser_* tools drive it)."""
    from .browserbridge import main as bb_main
    argv = ["--port", str(args.port)] if args.port else []   # [] not None: None re-reads argv
    if getattr(args, "browser", False):
        argv.append("--browser")
    return bb_main(argv)


def cmd_acp(args):
    """Serve collie as an ACP agent over stdio (the editor spawns this)."""
    # A human running `collie acp` in a terminal has a tty on stdio, not the pipes the ACP
    # transport needs — that used to crash with a raw asyncio traceback. Explain instead.
    if sys.stdin.isatty():
        print("collie acp speaks the Agent Client Protocol over stdio — it has no interactive UI "
              "and is meant to be SPAWNED BY AN EDITOR (Zed / VS Code ACP client).\n"
              "Configure it (Zed example, ~/.config/zed/settings.json):\n"
              '  {"agent_servers": {"collie": {"command": "%s", "args": ["acp"]}}}\n'
              "For a terminal chat use `collie tui`; for a browser UI use `collie web`."
              % (sys.argv[0] or "collie"))
        return 0
    from .acp_agent import main as acp_main
    acp_main()
    return 0


def cmd_run(args):
    import json as _json
    _, runs_db, out_html, _ = _paths()
    cwd = args.cwd or os.getcwd()
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")
    h = make_harness(cwd, provider=provider, model=args.model, project=args.project,
                     browser=getattr(args, "browser", None),
                     web_search=True if getattr(args, "web_search", False) else None,
                     exec_code=True, delegate=True)
    if getattr(args, "goal", None):              # pin a standing goal into CORE memory (every turn)
        h.memory.set_block("project:" + args.project, "goal", args.goal[:390], char_limit=400)
    # --continue / --resume: load a prior conversation THREAD so this run keeps full context
    # (not just memory). Fresh runs mint a new session; continued runs append to the same one.
    from . import sessions as sess
    history, sid = None, None
    if getattr(args, "resume", None):
        s = sess.load(args.resume)
        if s:
            history, sid = (s.get("messages") or []), args.resume
        else:
            print("  [session] no such session %r — starting fresh" % args.resume)
    elif getattr(args, "cont", False):
        sid = sess.latest()
        if sid:
            history = (sess.load(sid) or {}).get("messages")
    sid = sid or sess.new_id()
    # --stream-json: emit one NDJSON event per action (tool/edit/repro/receipt) as it happens,
    # so a terminal, an editor extension, or the ACP adapter can render the run LIVE (the
    # verification gate flipping fail->pass) instead of waiting for one final blob. Progress to
    # stderr keeps stdout clean for --json consumers piping the final object.
    if getattr(args, "stream_json", False):
        import sys as _sys
        h.emit = lambda kind, d: print(_json.dumps({"type": kind, **d}, ensure_ascii=False),
                                       file=_sys.stderr, flush=True)
    res = h.run("adhoc", args.task, history=history)
    sess.save(sid, res.messages, project=args.project, cwd=cwd, answer=res.answer or "")
    if getattr(args, "json", False) or getattr(args, "stream_json", False):
        print(_json.dumps({
            "answer": res.answer, "error": res.error, "model": res.model, "session": sid,
            "prefix_tokens": res.prefix_tokens, "prefix_measured": res.prefix_measured,
            "input_tokens": res.input_tokens,
            "output_tokens": res.output_tokens, "cache_read": res.cache_read,
            "cache_creation": res.cache_creation, "total_tokens": res.total_tokens,
            "cache_miss_tokens": res.cache_miss_tokens, "cache_waste_usd": res.cache_waste_usd,
            "turns": res.turns, "tool_calls": res.tool_calls, "mem_recalls": res.mem_recalls,
            "wall_ms": res.wall_ms, "cost_usd": res.cost_usd}, ensure_ascii=False))
    elif getattr(args, "print", False):
        print(res.answer or res.error)          # headless: answer only (like claude -p)
    else:
        print("prefix=%d in=%d out=%d turns=%d tools=%d recall=%d %dms" % (
            res.prefix_tokens, res.input_tokens, res.output_tokens, res.turns,
            res.tool_calls, res.mem_recalls, res.wall_ms))
        print("\n%s" % (res.answer or res.error))
        print("\n  session %s · continue: collie run \"…\" --continue  (or --resume %s)" % (sid, sid))
        dash.build(runs_db, out_html)
    h.memory.close(); h.recorder.close()
    return 0


def cmd_prefix(args):
    """Measure the real prefix cost on a provider via a two-request usage differential — the honest
    counterpart to the est_tokens (~len/4) headline number. Appends the result to
    ~/.collie/prefix_probe.json as the raw evidence the CHANGELOG/README leanness claim cites."""
    import json as _json
    from .providers import measure_prefix
    cwd = args.cwd or os.getcwd()
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")
    h = make_harness(cwd, provider=provider, model=args.model, project=args.project or "demo")
    system, _msgs, meta = h.composer.build({"messages": []}, ".", cwd, h.project, h.mode)
    schemas = h.registry.active_schemas()
    measured = measure_prefix(h.provider, system, schemas)
    est = meta.prefix_tokens
    drift = (100.0 * (est - measured) / measured) if measured else 0.0
    print("provider=%s model=%s  est=%d  measured=%d  drift=%+.0f%% (est vs measured)" % (
        h.provider.name, h.provider.model, est, measured, drift)
        + ("" if measured else "   [measured=0: provider gave no usable usage — probe unsupported]"))
    if measured:
        rec = {"provider": h.provider.name, "model": h.provider.model,
               "est": est, "measured": measured, "note": "prefetch-off composition"}
        pj = os.path.expanduser("~/.collie/prefix_probe.json")
        try:
            os.makedirs(os.path.dirname(pj), exist_ok=True)
            hist = []
            if os.path.exists(pj):
                try: hist = _json.load(open(pj))
                except Exception: hist = []
            hist.append(rec)
            _json.dump(hist[-100:], open(pj, "w"), indent=1)
            print("  recorded -> %s" % pj)
        except OSError:
            pass
    h.memory.close(); h.recorder.close()
    return 0


def cmd_pack(args):
    import json as _json
    from . import pack as _pack
    cwd = args.cwd or os.getcwd()
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")

    def _emit(i, rec):
        tag = ("check=%s " % ("pass" if rec.get("check_pass") else "fail")) if args.check else ""
        print("  attempt %d: %sverified=%s turns=%s%s" % (
            i, tag, rec.get("verified"), rec.get("turns"),
            (" ERROR " + rec["error"]) if rec.get("error") else ""), flush=True)

    res = _pack.run_pack(args.task, cwd, n=args.n, check=args.check, provider=provider,
                         model=args.model, apply=args.apply, emit=_emit)
    if getattr(args, "json", False):
        print(_json.dumps(res, ensure_ascii=False))
        return 0
    if res["winner"] is None:
        print("\nno winner: %s (nothing applied)" % res["reason"])
        return 1
    print("\nwinner: attempt %d (%s) · total $%.4f across %d attempts%s" % (
        res["winner"], res["reason"], res["total_cost_usd"], res["n"],
        " · APPLIED to cwd" if res["applied"] else " · not applied (use --apply)"))
    print("\n%s" % res.get("answer", ""))
    return 0


def cmd_compare(args):
    from . import adapters
    mem_db, runs_db, out_html, sandbox = _paths()
    os.makedirs(sandbox, exist_ok=True)
    facts = cmp.build_sandbox(sandbox)
    h = make_harness(sandbox, provider=args.provider, model=args.model, project="demo")
    h.memory.remember(
        "We decided to internalize embeddings: local bge-m3 via fastembed feeding "
        "sqlite-vec + FTS5 hybrid retrieval.", keys="embedding memory design",
        project="demo")

    targets = adapters.resolve([k.strip() for k in args.vs.split(",")])
    inst = ", ".join("%s%s" % (a.label, "" if a.available() else "(off)")
                     for a in targets) or "(none)"
    try:                                              # cheap LLM-judge for quality
        judge = make_provider(args.judge) if args.judge else None
    except Exception:
        judge = None
    print("== compare: collie(%s) vs %s  [%s]  judge=%s ==" % (
        args.provider, inst, "REAL" if args.real else "baseline", judge.name if judge else "heuristic"))

    for task in cmp.task_suite(facts, full=True):
        cmp.reset_sandbox(sandbox)                    # pristine copy per run (fair edits)
        m = cmp.run_collie(h, task)
        cmp.grade_and_cost(m, task["prompt"], judge); h.recorder.finish_run(m)
        line = "  %-14s | collie q=%2.0f $%.4f %s" % (
            task["id"], m.quality, m.cost_usd, "PASS" if m.success else "FAIL")
        for a in targets:
            if args.real and a.available():
                cmp.reset_sandbox(sandbox)
                c = a.run(task, cwd=sandbox, recorder=h.recorder, model=args.vs_model)
                cmp.grade_and_cost(c, task["prompt"], judge); h.recorder.finish_run(c)
                line += " | %s q=%2.0f %s" % (
                    a.key, c.quality, "PASS" if c.success else ("ERR" if c.error else "FAIL"))
            elif cmp.baseline(h.recorder, a.key, task["id"]):
                line += " | %s baseline" % a.key
            else:
                line += " | %s(%s)" % (a.key, "off" if not a.available() else "no-baseline")
        print(line)

    dash.build(runs_db, out_html)
    print("  dashboard -> %s" % out_html)
    h.memory.close(); h.recorder.close()
    return 0


def cmd_harnesses(args):
    from . import adapters
    print("== mainstream harness adapters ==")
    for a in adapters.ADAPTERS.values():
        base = adapters.MEASURED_PREFIX.get(a.key)
        print("  %-9s %-18s cli=%-13s %s  usage=%s  baseline=%s" % (
            a.key, a.label, a.cli,
            "INSTALLED" if a.available() else "—",
            "yes" if a.usage_supported else "no",
            base if base else "—"))
    print("\n  run:  python -m harness.cli compare --vs all --real")
    return 0


def cmd_dashboard(args):
    _, runs_db, out_html, _ = _paths()
    if not os.path.exists(runs_db):
        print("no runs.db yet — run `selftest` or `compare` first"); return 1
    dash.build(runs_db, out_html)
    print("dashboard -> %s" % out_html)
    return 0


def cmd_mem(args):
    mem_db, runs_db, _, _ = _paths()
    if args.action == "eval":
        from . import reval
        out = os.path.join(DATA, "retrieval_eval.json")
        r = reval.run_and_save(out, embed_name="local")
        for k in ("real", "hash"):
            e = r[k] or {}
            print("  %-6s %-24s P@1=%.2f P@5=%.2f MRR=%.2f (n=%d)" % (
                k, e.get("embedder", "?"), e.get("p_at_1", 0), e.get("p_at_5", 0),
                e.get("mrr", 0), e.get("n", 0)))
        print("  -> %s" % out)
        return 0
    m = SqliteMemory(mem_db, embedder=_embedder(args.embed))
    print("  [embed] %s (dim=%d)" % (m.embedder.name, m.embedder.dim))
    if args.action == "import":
        from .mem_import import run_import
        run_import(m, source=args.source, limit=args.limit, dry_run=args.dry_run,
                   no_llm=args.no_llm, force=args.force,
                   provider_name=args.provider, model=args.model,
                   max_chunks=args.max_chunks, workers=args.workers)
    elif args.action == "purge-imported":
        from .mem_import import purge
        print("purged %d imported facts" % purge(m))
    elif args.action == "add":
        print("remembered #%d" % m.remember(args.text, project=args.project))
    elif args.action == "reembed":
        print("re-embedded %d facts with %s" % (m.reembed_all(), m.embedder.name))
    else:
        hits = m.recall(args.text, project=args.project, k=8)
        for h in (hits or []):
            print("[%.3f] %s" % (h["score"], h["text"][:120]))
        if not hits:
            print("(no memories)")
    m.close()
    return 0


def _state_dir():
    """Where the delegate's durable state lives (~/.collie, overridable for tests
    via COLLIE_STATE_DIR). Matches the actions.py/jobs.py defaults."""
    d = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
    os.makedirs(d, exist_ok=True)
    return d


def cmd_jobs(args):
    """The human surface for delegated work: see jobs, confirm gated (irreversible)
    actions, read receipts. The confirm step approves a CONCRETE materialized
    payload; execution runs only in a process where the capability is registered
    (a runner/daemon), so a bare `collie jobs confirm` approves and reports —
    the model never executes here."""
    from .actions import ActionStore, RefusedError
    from .jobs import JobStore, Executor, NEEDS_YOU
    from . import capabilities as _caps
    _caps.register_builtins()          # make shipped capabilities executable here
    d = _state_dir()
    acts = ActionStore(os.path.join(d, "actions.db"))
    jobs = JobStore(os.path.join(d, "jobs.db"))
    rc = 0
    try:
        if args.action == "ls":
            js = jobs.list()
            if not js:
                print("(no jobs)")
            for j in js:
                print("  %-12s %-14s %s" % (j.job_id, j.state, (j.goal or "")[:60]))
        elif args.action == "inbox":
            pend = acts.pending()
            print("── pending confirmations (%d) ──" % len(pend))
            for p in pend:
                print("  %s  %s  %s" % (p["nonce"], p["capability"],
                                        (p["args_json"] or "")[:80]))
            ny = jobs.list(state=NEEDS_YOU)
            print("── jobs needing you (%d) ──" % len(ny))
            for j in ny:
                print("  %-12s %s" % (j.job_id, (j.goal or "")[:60]))
            if not pend and not ny:
                print("  (nothing waiting)")
        elif args.action == "confirm":
            nonce = args.text
            rec = acts.get(nonce)
            if not rec:
                print("unknown nonce"); return 1
            # confirm() raises on a non-pending nonce (already approved/executed);
            # don't let that abort the command — report state and, if it already
            # fired, reconcile the job from its receipt rather than crashing.
            try:
                acts.confirm(nonce)
                print("approved %s  cap=%s  digest=%s…" % (nonce, rec.capability, rec.digest[:12]))
            except RefusedError as e:
                print("not confirming %s: %s" % (nonce, e))
            try:
                v = Executor(acts, jobs).run_confirmed(nonce, job_id=rec.job_id)
                print("executed → %s: %s" % (v.status, v.reason))
            except RefusedError as e:
                print("not executed here (%s)." % e)
                print("a runner with the capability loaded will execute it.")
        elif args.action == "run":
            # collie jobs run <capability> '<json-args>' — create a job, propose the
            # action, and DRIVE it: a reversible in-scope capability runs live and
            # verifies; an irreversible one parks in needs_you awaiting confirm.
            import json as _json
            cap = args.text
            if not cap:
                print("usage: collie jobs run <capability> '<json-args>' [--goal ...]"); return 1
            try:
                cap_args = _json.loads(args.jargs) if args.jargs else {}
            except _json.JSONDecodeError as e:
                print("bad json args: %s" % e); return 1
            import secrets as _s
            jid = "job-" + _s.token_hex(4)
            jobs.create(jid, args.goal or cap, leash=_json.loads(args.leash) if args.leash else {})
            nonce = acts.propose(cap, cap_args, job_id=jid)
            print("job %s  proposed %s (%s)" % (jid, cap, nonce[:12]))
            try:
                v = Executor(acts, jobs).drive(nonce)
                print("→ %s: %s   [job %s]" % (v.status, v.reason, jobs.get(jid).state))
            except RefusedError as e:
                print("refused: %s" % e)
        elif args.action == "wake":
            # catch-up-on-wake: fire every overdue wait now (what the daemon does
            # on start / each tick). Durable waits survive restart; this drains them.
            from .scheduler import Scheduler
            import time as _t
            sched = Scheduler(acts, jobs, db_path=os.path.join(d, "jobs.db"))
            fired = sched.tick(int(_t.time()))
            print("catch-up: fired %d due wait(s); %d still pending"
                  % (fired, len(sched.pending_waits())))
            sched.close()
        elif args.action == "ask":
            # natural language -> compile to a job -> drive it
            from . import mandate
            from .jobs import Executor
            import secrets as _s2
            text = (args.text + (" " + args.jargs if args.jargs else "")).strip()
            if not text:
                print('usage: collie jobs ask "记一下 今晚买菜"'); return 1
            prov = None
            try:
                from . import settings as _st
                from .providers import make_provider
                _st.apply()
                prov = make_provider(_st.get("PROVIDER"), _st.get("MODEL"))
            except Exception:
                pass
            plan = mandate.compile(text, prov)
            if not plan.get("capability"):
                print("🤔 " + (plan.get("clarify") or "not sure what to do")); return 0
            print("understood → %s %s" % (plan["capability"], plan.get("args")))
            jid = "job-" + _s2.token_hex(4)
            jobs.create(jid, plan.get("goal") or text, leash=plan.get("leash") or {})
            nonce = acts.propose(plan["capability"], plan.get("args") or {}, job_id=jid)
            try:
                v = Executor(acts, jobs).drive(nonce)
                print("→ %s: %s   [job %s]" % (v.status, v.reason, jobs.get(jid).state))
            except RefusedError as e:
                print("refused: %s" % e)
        elif args.action == "web":
            # the delegation-first dashboard (Today / Inbox / Receipts).
            from .jobsweb import serve
            serve(port=int(args.port) if args.port else 8794, state_dir=d)
        elif args.action == "daemon":
            # colliejobd: catch up on start, then tick on an interval. Owns no
            # model process — it only drives due, already-materialized actions.
            from .scheduler import Scheduler
            sched = Scheduler(acts, jobs, db_path=os.path.join(d, "jobs.db"))
            print("colliejobd: catch-up + tick every %ss (Ctrl-C to stop)" % args.interval)
            try:
                sched.serve(interval=float(args.interval))
            except KeyboardInterrupt:
                print("\ncolliejobd stopped")
            finally:
                sched.close()
        elif args.action == "receipts":
            rows = acts.receipts(args.text or None)
            if not rows:
                print("(no receipts)")
            for r in rows:
                print("  %s  %s  fired=%s  %s: %s" % (
                    r["capability"], r["nonce"][:12], r["fired"],
                    r["verdict"], (r["verdict_reason"] or "")[:60]))
                if r["evidence"]:
                    print("      evidence: %s" % r["evidence"][:100])
    finally:
        acts.close()
        jobs.close()
    return rc


def cmd_init(args):
    """collie init — one-time repo prep. Everything collie does is lazy, so this only front-loads
    the two costs a user would otherwise pay mid-conversation: the embedder's first-use model
    download, and the code_search batch index (~seconds). --rules additionally has the MODEL explore
    the repo and write an AGENTS.md (the opencode `/init` convention); collie reads AGENTS.md /
    CLAUDE.md as project rules on every run."""
    import time as _t
    cwd = os.path.abspath(args.cwd or os.getcwd())
    print("collie init · %s" % cwd)
    if not args.no_config:
        _setup_wizard(force=True)     # provider/model first — init is also "set me up" (tty only)
    t0 = _t.time()
    emb = _embedder(args.embed)                       # 1) warm the embedder (first use downloads)
    emb.embed("warm-up", kind="query")
    print("  ✓ embedder ready: %s (dim=%d)  [%.1fs]" % (emb.name, emb.dim, _t.time() - t0))
    t1 = _t.time()
    from . import codeindex                           # 2) code_search index (batch-embed the repo)
    n = codeindex.get_index(cwd, emb).build()
    print("  ✓ code index: %d chunks  [%.1fs]" % (n, _t.time() - t1))
    from . import codemap                             # 3) codemap (cheap; validates the map view)
    tree = codemap.build_tree(cwd)
    print("  ✓ codemap: %d files · %d defs" % (len(tree), sum(f.get("defs", 0) for f in tree)))
    if args.rules:                                    # 4) optional: model-written AGENTS.md
        existing = [f for f in ("AGENTS.md", "CLAUDE.md", ".collie.md") if os.path.exists(os.path.join(cwd, f))]
        if existing:
            print("  · rules file already present (%s) — skipping generation" % ", ".join(existing))
        else:
            print("  … generating AGENTS.md with the model (one short run)")
            provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")
            h = make_harness(cwd, provider=provider, project="init")
            res = h.run("init", (
                "Explore this repository briefly (README, entry points, key modules, how tests run) "
                "and CREATE a concise AGENTS.md at the repo root with: what the project is (2-3 "
                "sentences), the layout (key dirs/files), how to build/run/test, and any conventions "
                "an agent must follow. Write the file with write_file. Keep it under 80 lines."))
            ok = os.path.exists(os.path.join(cwd, "AGENTS.md"))
            print(("  ✓ AGENTS.md written" if ok else "  ✗ AGENTS.md not written (%s)" %
                   (res.error or "model finished without writing")))
    print("done in %.1fs — collie is warm; first question won't pay the indexing cost." % (_t.time() - t0))
    return 0


def cmd_mcp(args):
    from . import mcpclient as mc
    servers = mc._load_config()
    if args.action == "list":
        if not servers:
            print("(no MCP servers configured — create ~/.collie/mcp.json)")
            return 0
        toks = mc._load_tokens()
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            if mc._is_remote(cfg):
                if any(k.lower() == "authorization" for k in (cfg.get("headers") or {})):
                    auth = "static-header"
                elif name in toks:
                    auth = "oauth ✓"
                else:
                    auth = "oauth (run: collie mcp login %s)" % name
                print("  %-16s remote  %s  [%s]" % (name, cfg.get("url"), auth))
            else:
                print("  %-16s stdio   %s" % (name, cfg.get("command")))
        return 0
    cfg = servers.get(args.name) if args.name else None
    if args.action in ("login", "tools") and not cfg:
        print("no such server: %r (see `collie mcp list`)" % args.name)
        return 1
    if args.action == "login":
        try:
            mc.login(args.name, cfg)
        except Exception as e:
            print("login failed: %s" % e)
            return 1
        print("✓ authorized %s — refreshing tool cache…" % args.name)
        try:                                    # re-list now that we're authorized, so tools cache warms
            cache = mc._read_cache()
            conn = mc._get_conn(args.name, cfg)
            tools = [{"name": t.get("name"), "description": t.get("description", ""),
                      "inputSchema": t.get("inputSchema") or t.get("input_schema")}
                     for t in conn.list_tools() if t.get("name")]
            cache[args.name] = {"hash": mc._cfg_hash(cfg), "tools": tools}
            mc._write_cache(cache)
            print("  %d tools available" % len(tools))
        except Exception as e:
            print("  (authorized, but tool list failed: %s)" % e)
        return 0
    if args.action == "logout":
        toks = mc._load_tokens()
        existed = toks.pop(args.name, None) is not None
        mc._save_tokens(toks)
        print("logged out %s" % args.name if existed else "no stored token for %s" % args.name)
        return 0
    if args.action == "tools":
        try:
            conn = mc._get_conn(args.name, cfg)
            tools = conn.list_tools()
        except Exception as e:
            print("list failed: %s" % e)
            return 1
        for t in tools:
            print("  mcp__%s__%s — %s" % (args.name, t.get("name"), (t.get("description") or "")[:70]))
        if not tools:
            print("  (no tools)")
        return 0
    return 0


CMDS = {"selftest", "run", "prefix", "pack", "compare", "harnesses", "dashboard", "mem", "acp",
        "loop", "repl", "tui", "web", "browser-bridge", "mcp", "init", "jobs"}


def _setup_wizard(force=False):
    """Interactive provider/model setup, saved to ~/.collie/settings.json. Two entries:

    bare `collie` (force=False) — one-time onboarding, only when NOTHING is configured (no
    COLLIE_PROVIDER env, no saved PROVIDER); a short curated list, the opencode/hermes convention.
    `collie init` (force=True) — ALWAYS offered (init is the canonical "set me up" command):
    the full provider menu straight from the settings SCHEMA (single source of truth — a provider
    added there appears here with zero wizard edits), current values prefilled, model asked too.

    Non-tty always skips (CI/scripts stay non-interactive); the full knob set lives in the web
    Settings panel. anthropic-oauth stays a deliberate pick, never a silent default."""
    from . import settings as st
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    if not force and (os.environ.get("COLLIE_PROVIDER") or st._load().get("PROVIDER")):
        return
    cur = st.get("PROVIDER") or ""
    if force:
        sch = {s["key"]: s for s in st.SCHEMA}
        opts = [(o["value"], o["label"]) for o in sch["PROVIDER"]["options"]]
        print("Where should completions come from? (Enter keeps the current choice)\n")
    else:
        opts = [
            ("anthropic-oauth", "Claude subscription (Pro/Max — $0/token, reuses your Claude Code login)"),
            ("anthropic",       "Anthropic API key (metered — needs ANTHROPIC_API_KEY exported)"),
            ("ollama",          "Ollama (local models — nothing leaves this machine)"),
            ("mock",            "Mock (offline demo — try the harness before connecting anything)"),
        ]
        print("Welcome to collie — one-time setup. Where should completions come from?\n")
    for i, (val, label) in enumerate(opts, 1):
        print("  %d) %s%s" % (i, label, "   ← current" if val == cur else ""))
    if not force:
        print("\n(more providers + models: `collie init`, or the Settings panel in `collie web`)")
    # Enter = keep the current provider; with nothing configured yet it means the schema default
    # ("anthropic") on init, or the recommended option 1 on first run — NEVER the runtime's mock.
    default = cur or (sch["PROVIDER"]["default"] if force else opts[0][0])
    try:
        c = input("Choice [1-%d, Enter = %s]: " % (len(opts), default)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    pick = opts[int(c) - 1][0] if c.isdigit() and 1 <= int(c) <= len(opts) else default
    data = dict(st._load())            # save() replaces the whole file — merge, don't clobber
    data["PROVIDER"] = pick
    if force:                          # model id, prefilled; `-` clears back to the provider default
        curm = st.get("MODEL") or ""
        # suggest models of the picked provider's family; the generic head of the list otherwise
        fam = {"anthropic": "claude", "anthropic-oauth": "claude", "claude-cli": "claude"}.get(
            pick, pick.split("-")[0])
        sug = [m for m in sch["MODEL"]["list"] if fam in m] or sch["MODEL"]["list"][:4]
        print("\nModel id for %s (e.g. %s)" % (pick, ", ".join(sug[:4])))
        hint = "Enter = %s" % (curm or "provider default")
        if curm:
            hint += ", `-` = provider default"
        try:
            m = input("Model [%s]: " % hint).strip()
        except (EOFError, KeyboardInterrupt):
            m = ""
        if m == "-":
            data.pop("MODEL", None)
        elif m:
            data["MODEL"] = m
    st.save(data)
    st.apply()                         # same-process pickup (e.g. init --rules runs right after)
    print("→ provider = %s%s (saved to %s)"
          % (pick, ", model = " + data["MODEL"] if data.get("MODEL") else "", st._PATH))
    hard = os.environ.get("COLLIE_PROVIDER") if "COLLIE_PROVIDER" in st._HARD_ENV else None
    if hard and hard != pick:
        print("  note: COLLIE_PROVIDER=%s is exported in this shell and overrides the saved value." % hard)
    if pick == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("  next: export ANTHROPIC_API_KEY=sk-ant-…   (add it to your shell profile)")
    elif pick == "anthropic-oauth":
        print("  note: reuses your Claude Code OAuth token — run `claude` once if you never logged in.")
    else:
        # schema labels carry the key env var, e.g. "DeepSeek (DEEPSEEK_API_KEY) ☁" — hint if unset
        mkey = re.search(r"\(([A-Z][A-Z0-9_]*_API_KEY)\)", dict(opts)[pick])
        if mkey and not os.environ.get(mkey.group(1)):
            print("  next: export %s=…   (add it to your shell profile)" % mkey.group(1))
    print()


def _first_run_wizard():
    _setup_wizard(force=False)


def main(argv=None):
    from . import settings as _settings
    _settings.apply()   # inject saved Settings-panel values into os.environ (real env vars still win)
    argv = list(sys.argv[1:] if argv is None else argv)
    # headless one-liner:  collie "task"  |  collie -p "task"
    if argv and argv[0] in ("-p", "--print"):
        argv = ["run", "-p"] + argv[1:]
    elif argv and argv[0] not in CMDS and not argv[0].startswith("-"):
        argv = ["run"] + argv
    elif not argv and sys.stdin.isatty():
        # bare `collie` = the default interactive surface (the opencode/hermes convention).
        # (the first-run wizard fires at dispatch below — it covers every chat surface, not just this)
        try:
            import rich  # noqa: F401
            argv = ["tui"]
        except ImportError:
            argv = ["repl"]           # stdlib-only fallback — and SAY so, or nobody learns the TUI exists
            print("(rich not installed — plain repl. `pipx inject collie-harness rich` unlocks `collie tui`)")

    p = argparse.ArgumentParser(prog="collie", description="collie — evolvable coding-agent harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)

    pr = sub.add_parser("run", help="run one task headlessly (collie -p \"task\")")
    pr.add_argument("task")
    pr.add_argument("--provider", default=None,
                    help="mock|ollama|anthropic|deepseek|qwen|... (env COLLIE_PROVIDER)")
    pr.add_argument("--model", default=None)
    pr.add_argument("--cwd", default=None); pr.add_argument("--project", default="demo")
    pr.add_argument("-p", "--print", action="store_true", help="print only the answer")
    pr.add_argument("--json", action="store_true", help="print a JSON result")
    pr.add_argument("--stream-json", action="store_true", dest="stream_json",
                    help="stream NDJSON events (tool/edit/repro/receipt) to stderr as they "
                         "happen — for live UX / editor extension / ACP adapter")
    pr.add_argument("--browser", action="store_true", default=None,
                    help="enable Playwright browser tools (else COLLIE_BROWSER=1)")
    pr.add_argument("--web-search", action="store_true", dest="web_search",
                    help="enable the web_search tool (keyless DuckDuckGo, or a browser-extension "
                         "bridge via COLLIE_WEBSEARCH_BRIDGE)")
    pr.add_argument("--goal", default=None,
                    help="pin a standing goal into CORE memory (loaded every turn)")
    pr.add_argument("--continue", dest="cont", action="store_true",
                    help="continue the most recent session's conversation thread")
    pr.add_argument("--resume", default=None, metavar="ID",
                    help="resume a specific session by id (see the id printed after each run)")
    pr.set_defaults(fn=cmd_run)

    # prefix: measure the real cached-prefix cost on a provider (honest counterpart to est ~len/4)
    pp = sub.add_parser("prefix", help="measure the real prefix token cost on a provider "
                                       "(two-request usage differential; --measure implied)")
    pp.add_argument("--measure", action="store_true", help="(default action; accepted for clarity)")
    pp.add_argument("--provider", default=None, help="mock|anthropic|deepseek|... (env COLLIE_PROVIDER)")
    pp.add_argument("--model", default=None)
    pp.add_argument("--cwd", default=None); pp.add_argument("--project", default="demo")
    pp.set_defaults(fn=cmd_prefix)

    # pack: best-of-N with execution-based selection (run N isolated attempts, pick what passes)
    pk = sub.add_parser("pack", help="best-of-N: run the task N times in isolation, keep what passes")
    pk.add_argument("task")
    pk.add_argument("-n", type=int, default=3, help="number of attempts (1-8, default 3)")
    pk.add_argument("--check", default=None,
                    help="shell command run in each attempt's copy; exit 0 = pass (selection gate)")
    pk.add_argument("--apply", action="store_true",
                    help="copy the winning attempt's files back over the working dir")
    pk.add_argument("--provider", default=None); pk.add_argument("--model", default=None)
    pk.add_argument("--cwd", default=None)
    pk.add_argument("--json", action="store_true", help="print a JSON result")
    pk.set_defaults(fn=cmd_pack)

    # repl: lightweight interactive chat that keeps the full thread (and persists it as a session)
    prp = sub.add_parser("repl", help="interactive REPL that keeps the conversation thread")
    prp.add_argument("--provider", default=None); prp.add_argument("--model", default=None)
    prp.add_argument("--cwd", default=None); prp.add_argument("--project", default="demo")
    prp.add_argument("--goal", default=None)
    prp.add_argument("--continue", dest="cont", action="store_true", help="continue the latest session")
    prp.add_argument("--resume", default=None, metavar="ID", help="resume session by id")
    prp.set_defaults(fn=cmd_repl)

    # tui: rich full-experience terminal chat (live gate/diff/receipt timeline)
    pt = sub.add_parser("tui", help="rich terminal chat with a live tool/gate/diff timeline")
    pt.add_argument("--provider", default=None); pt.add_argument("--model", default=None)
    pt.add_argument("--cwd", default=None); pt.add_argument("--project", default="demo")
    pt.add_argument("--goal", default=None)
    pt.add_argument("--continue", dest="cont", action="store_true", help="continue the latest session")
    pt.add_argument("--resume", default=None, metavar="ID", help="resume session by id")
    pt.set_defaults(fn=cmd_tui)

    # web: local browser GUI, streams the run over SSE
    pw = sub.add_parser("web", help="serve the local web GUI (streams the verification gate live)")
    pw.add_argument("--port", type=int, default=8787)
    pw.add_argument("--no-open", dest="open", action="store_false", help="don't auto-open a browser")
    pw.set_defaults(open=True, fn=cmd_web)

    # wallpaper: collie owns its own live desktop window (no third-party wallpaper engine)
    pwp = sub.add_parser("wallpaper", help="put collie's live desktop on screen, owned by collie (no Lively/WE)")
    pwp.add_argument("--port", type=int, default=8787)
    pwp.add_argument("--kiosk", action="store_true", help="immersive full-screen (no frame; Alt-F4 exits)")
    pwp.set_defaults(fn=cmd_wallpaper)

    # browser-bridge: LLM-driven real browser via a Chrome extension (authenticated / full-page)
    pb = sub.add_parser("browser-bridge", help="run the bridge the browser extension polls (browser_* tools)")
    pb.add_argument("--port", type=int, default=0)
    pb.add_argument("--browser", action="store_true",
                    help="also auto-launch a managed Chromium with the extension (no manual install)")
    pb.set_defaults(fn=cmd_browser_bridge)

    # loop: autonomous goal-directed iteration — run the agent repeatedly toward a goal, stopping
    # when an EXECUTED check passes (on brand: the loop ends on real green, not the model's word).
    pl = sub.add_parser("loop", help="run the agent repeatedly toward a --goal until an executed "
                                     "--until check passes or --max iterations")
    pl.add_argument("task", nargs="?", default=None,
                    help="per-iteration instruction (default: 'make progress toward the goal')")
    pl.add_argument("--goal", default=None, help="the standing goal (pinned into CORE memory)")
    pl.add_argument("--until", default=None,
                    help="shell command; the loop stops the first iteration it exits 0 "
                         "(e.g. --until \"pytest -q\")")
    pl.add_argument("--max", type=int, default=5, help="max iterations (default 5)")
    pl.add_argument("--provider", default=None); pl.add_argument("--model", default=None)
    pl.add_argument("--cwd", default=None); pl.add_argument("--project", default="demo")
    pl.set_defaults(fn=cmd_loop)

    pa = sub.add_parser("acp", help="run as an ACP agent over stdio (Zed/JetBrains/neovim/"
                                    "VS Code plug in and drive collie's loop)")
    pa.set_defaults(fn=cmd_acp)

    pc = sub.add_parser("compare")
    pc.add_argument("--provider", default="mock"); pc.add_argument("--model", default=None)
    pc.add_argument("--vs", default="claude",
                    help="harness keys (claude,codex,gemini,cursor,opencode,aider) "
                         "or 'all' / 'discovered'")
    pc.add_argument("--real", action="store_true",
                    help="actually execute installed harness CLIs (spends quota)")
    pc.add_argument("--vs-model", default="")
    pc.add_argument("--judge", default="", help="provider for LLM quality judge (e.g. deepseek); '' = heuristic")
    pc.set_defaults(fn=cmd_compare)

    ph = sub.add_parser("harnesses"); ph.set_defaults(fn=cmd_harnesses)

    sub.add_parser("dashboard").set_defaults(fn=cmd_dashboard)

    pm = sub.add_parser("mem")
    pm.add_argument("action", choices=["search", "add", "reembed", "eval", "import", "purge-imported"])
    pm.add_argument("text", nargs="?", default="")
    pm.add_argument("--project", default="demo"); pm.add_argument("--embed", default="auto")
    # mem import: distill past Claude Code / Codex sessions into memory (see mem_import.py)
    pm.add_argument("--source", choices=["cc", "codex", "all"], default="all",
                    help="which local agent history to import")
    pm.add_argument("--limit", type=int, default=100, help="max sessions this run (newest first)")
    pm.add_argument("--dry-run", action="store_true", help="show extracted facts, store nothing")
    pm.add_argument("--no-llm", action="store_true", help="heuristic extraction only (no distiller calls)")
    pm.add_argument("--force", action="store_true", help="re-import sessions already in the state file")
    pm.add_argument("--provider", default=None, help="distiller provider override (default: Settings PROVIDER)")
    pm.add_argument("--model", default=None, help="distiller model override (default: sonnet on Claude providers)")
    pm.add_argument("--max-chunks", type=int, default=16,
                    help="rolling-distill call budget per session; giants get evenly sampled; 0 = no cap")
    pm.add_argument("--workers", type=int, default=1,
                    help="parallel distillation workers (db writes stay single-threaded)")
    pm.set_defaults(fn=cmd_mem)

    # jobs: the delegate surface — list jobs, confirm gated actions, read receipts.
    pj = sub.add_parser("jobs", help="delegated work: ls | inbox | run <cap> | confirm <nonce> | receipts")
    pj.add_argument("action",
                    choices=["ls", "inbox", "ask", "run", "confirm", "receipts",
                             "wake", "daemon", "web"])
    pj.add_argument("text", nargs="?", default="",
                    help="nonce (confirm/receipts) or capability name (run)")
    pj.add_argument("jargs", nargs="?", default="", help="JSON args for `run`")
    pj.add_argument("--goal", default="", help="job goal text (run)")
    pj.add_argument("--leash", default="", help="job leash as JSON (run)")
    pj.add_argument("--interval", default=60, type=float, help="daemon tick seconds")
    pj.add_argument("--port", default=0, type=int, help="dashboard port (web; default 8794)")
    pj.set_defaults(fn=cmd_jobs)

    # init: front-load the lazy first-use costs (embedder download + code index) and optionally
    # have the model write AGENTS.md — the friendly "collie, meet my repo" moment.
    pi = sub.add_parser("init", help="configure provider/model, warm the embedder + build the code "
                                     "index; --rules writes AGENTS.md")
    pi.add_argument("--cwd", default=None)
    pi.add_argument("--no-config", action="store_true",
                    help="skip the provider/model prompt (CI / scripted runs)")
    pi.add_argument("--embed", default="auto", help="embedder (auto|local|daemon|hash)")
    pi.add_argument("--rules", action="store_true",
                    help="also have the model explore the repo and write an AGENTS.md")
    pi.add_argument("--provider", default=None, help="provider for --rules (default: configured one)")
    pi.set_defaults(fn=cmd_init)

    # mcp: manage MCP servers — list configured ones, OAuth-login to a remote, logout, or list tools
    pmcp = sub.add_parser("mcp", help="manage MCP servers (list | login <name> | logout <name> | tools <name>)")
    pmcp.add_argument("action", choices=["list", "login", "logout", "tools"])
    pmcp.add_argument("name", nargs="?", default="")
    pmcp.set_defaults(fn=cmd_mcp)

    args = p.parse_args(argv)
    # any chat surface started interactively with nothing configured gets the one-time wizard —
    # without this, a fresh install's `collie web`/`collie tui`/`collie run` silently lands on the
    # mock provider and answers with canned "Based on the tool output" nonsense. Never prompts when:
    # the user already chose (--provider), the output is machine-read (--json/--stream-json may run
    # on an editor's pty — input() there would hang the protocol), or stdin/stdout isn't a tty.
    if (args.cmd in ("run", "repl", "tui", "web") and not getattr(args, "provider", None)
            and not (getattr(args, "json", False) or getattr(args, "stream_json", False))):
        _first_run_wizard()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
