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
    """Resolve the memory embedder. auto -> granite (in-process, no daemon) if its ONNX deps are
    present, else None = BM25-only (NEVER HashEmbedding — measured worse than BM25). A named backend
    (COLLIE_EMBED=granite|bge-m3|e5|jina|hash|onnx:<repo>|...) is honored as-is.

    Returns an EmbeddingProvider or None (None => the memory pipeline runs sparse-only)."""
    embed = os.environ.get("COLLIE_EMBED", embed)
    if embed in ("bm25", "none", "off", "sparse"):
        return None
    if embed in ("auto", "granite", "local", "default", "daemon"):   # "daemon" = legacy alias
        try:
            return make_embedding("granite")
        except Exception as e:
            # stderr, NOT stdout — `run --json`/`--stream-json` promise machine-readable stdout.
            # This fires when onnxruntime/tokenizers aren't installed or the model can't download.
            if isinstance(e, ImportError):
                why, fix = ("onnxruntime/tokenizers not installed",
                            "pip install collie-harness[local]   (or run: collie setup)")
            else:
                why = "%s: %s" % (type(e).__name__, str(e)[:100])
                fix = ("model download failed (huggingface.co + hf-mirror.com) — check the network, "
                       "or set an intranet mirror: COLLIE_HF_ENDPOINT=<url>")
            print("  [embed] semantic memory unavailable (%s) -> BM25-only (keyword retrieval)\n"
                  "  [embed] enable it: %s" % (why, fix), file=sys.stderr)
            return None
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
                from . import plat
                _uargs, _ush = plat.shell_argv(args.until)   # POSIX --until predicate on every OS
                rc = _sp.run(_uargs, shell=_ush, cwd=cwd).returncode
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
    if getattr(args, "remote", False):
        return _cmd_web_remote(args)
    from .webapp import main as web_main
    argv = ["--port", str(args.port)]
    if not args.open:
        argv.append("--no-open")
    if getattr(args, "lan", False):
        argv.append("--lan")
    if getattr(args, "qr", False):
        argv.append("--qr")
    return web_main(argv)


def _print_qr(data: str):
    """Print a scannable ASCII QR of the pairing link to the terminal, so a phone can just scan the
    screen.

    Uses collie's own stdlib encoder (harness/qr.py) rather than `segno`: the core ships no
    dependencies, and an optional one meant this printed "pip install …" instead of a code on a
    plain install — precisely when someone is trying to pair a phone for the first time."""
    from . import qr
    try:
        print(qr.ansi(data), flush=True)
    except ValueError:
        # the encoder tops out at 106 bytes (v6-M); a longer link is still printed above as text
        print("  (link too long for a terminal QR — open it on the phone directly)", flush=True)


def _cmd_web_remote(args):
    """collie web --remote — run the local GUI server AND dial the public relay so a phone can
    drive this desktop from anywhere. The local server still binds 127.0.0.1 only; the relay client
    replays the phone's requests to it with the CSRF token injected (see harness/remote.py)."""
    import threading, time
    from . import webapp
    from .remote import RemoteState

    relay = os.environ.get("COLLIE_RELAY", "wss://collie.run").rstrip("/")

    try:
        httpd, port = webapp.bind_server(args.port)
    except OSError as e:
        print("collie web --remote: %s (pass --port <free port>)" % e)
        return 1
    threading.Thread(target=httpd.serve_forever, name="collie-web", daemon=True).start()

    state = RemoteState(relay, port, webapp.TOKEN, logf=lambda *a: print(*a, flush=True))
    webapp.REMOTE = state                           # expose to the web server's /api/remote/* + panel
    state.start()
    n_dev = len(state.identity.devices())

    print("collie web · local http://127.0.0.1:%d/ · provider=%s" % (port, webapp._provider()), flush=True)
    print("collie remote · relay=%s · %d paired device(s)" % (relay, n_dev), flush=True)
    print("  Control panel (on this computer):  http://127.0.0.1:%d/remote" % port, flush=True)
    print("─" * 60, flush=True)
    print("  Open on your phone:  %s" % state.link(), flush=True)
    _print_qr(state.link())
    print("  Pairing code: %s   (only needed to add a NEW device)" % state.paircode, flush=True)
    if n_dev:
        print("  Already-paired devices reconnect automatically — no code needed.", flush=True)
    print("─" * 60, flush=True)
    print("  Ctrl-C to stop (this instantly cuts off all remote access).", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        state.stop()
        httpd.shutdown()
    return 0


# ── the desktop window ───────────────────────────────────────────────────────────────────────────
# One contract on every OS: a Chromium-family browser in --app mode under collie's own profile dir,
# which is what makes it a real borderless window instead of a tab (and stops Chrome from handing the
# request to an already-running instance that would ignore --app).
#
# Only two things genuinely vary, and neither is "which OS":
#   where the binary lives  — a .app bundle on macOS, PATH everywhere else
#   whose desktop it opens on — on WSL the user is looking at the WINDOWS desktop, so the browser has
#                               to be launched over there; everywhere else "local" is the right screen
# So supporting one more OS should be an entry in a tuple, not another branch.
_BROWSERS_BUNDLE = ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium", "Vivaldi")
_BROWSERS_PATH = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                  "microsoft-edge", "microsoft-edge-stable", "brave-browser", "vivaldi-stable")


def _app_window_flags(url, kiosk):
    """The window contract — byte-identical on every OS."""
    profile = os.path.join(os.path.expanduser("~"), ".collie", "desktop")
    return (["--app=%s" % url, "--user-data-dir=%s" % profile]
            + (["--kiosk", "--start-fullscreen"] if kiosk else ["--start-maximized"]))


def _find_browser():
    """(path, label) of a local Chromium-family browser, or (None, why-not)."""
    from . import plat
    if plat.is_macos():
        for name in _BROWSERS_BUNDLE:
            for root in ("/Applications", os.path.expanduser("~/Applications")):
                exe = os.path.join(root, name + ".app", "Contents", "MacOS", name)
                if os.path.exists(exe):
                    return exe, name
        return None, ("no Chromium-family browser in /Applications (looked for %s), and Safari has "
                      "no --app mode" % ", ".join(_BROWSERS_BUNDLE))
    import shutil
    for name in _BROWSERS_PATH:
        exe = shutil.which(name)
        if exe:
            return exe, name
    return None, "no Chromium-family browser on PATH (looked for %s)" % ", ".join(_BROWSERS_PATH)


def _open_window_local(url, kiosk):
    """The window on THIS machine's desktop — macOS and Linux."""
    import subprocess
    exe, label = _find_browser()
    if not exe:
        return False, "%s — open %s in a browser instead" % (label, url)
    try:
        subprocess.Popen([exe] + _app_window_flags(url, kiosk),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return False, "launch error (%s): %s" % (label, e)
    return True, "opened in %s" % label


def _open_window_wsl(url, kiosk):
    """WSL only, and the one case that can't be done locally: the desktop the user is looking at
    belongs to Windows, so the browser is launched over there through powershell.exe, in their own
    logged-in Edge profile. A Linux-side window would open in WSLg, not on that desktop."""
    import shutil, subprocess
    ps = shutil.which("powershell.exe")
    if not ps:
        return False, "no powershell.exe"
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


def _desktop_window(url, kiosk=False):
    """Pop a borderless browser window showing `url` — a *real* window, so clicks and typing are 100%
    reliable (unlike a behind-icons wallpaper, where the shell eats clicks). Returns (ok, detail).

    Only ever reached off native Windows: there `collie app` / `collie wallpaper` drive the WebView2
    engine instead, so the cases here are WSL, macOS and Linux."""
    from . import plat
    if plat.is_wsl():
        ok, detail = _open_window_wsl(url, kiosk)
        if ok:
            return ok, detail
        # fall through rather than give up: WSLg puts a Linux window on the Windows desktop too, so
        # a WSL box with no powershell.exe still has a way to show this.
    return _open_window_local(url, kiosk)


def cmd_app(args):
    """collie app — open collie in a native desktop window (WebView2) with the server behind it."""
    from . import plat
    if plat.is_windows():
        from . import wallpaper as wp
        return wp.run_app(port_pref=args.port)
    print("collie app: native window is Windows-only — falling back to the browser GUI.")
    return cmd_web(args)


def cmd_wallpaper(args):
    """collie's live desktop as the wallpaper (behind the icons), owned by collie. On Windows this
    drives the WebView2 engine (built on demand from source, autostart-able, port picked at runtime);
    elsewhere it degrades to a borderless full-screen browser window. See harness/wallpaper.py."""
    from . import plat
    # sub-actions (Windows engine): install/uninstall autostart, boot entry, clean stop
    if plat.is_windows():
        from . import wallpaper as wp
        if getattr(args, "install", False):
            return wp.install()
        if getattr(args, "uninstall", False):
            return wp.uninstall()
        if getattr(args, "stop", False):
            return wp.stop()
        return wp.run(port_pref=args.port, boot=getattr(args, "boot", False))

    # non-Windows: no Progman/WebView2 — fall back to a borderless browser window. Same page the
    # Windows engine loads (wallpaper.py sets COLLIE_WALLPAPER_URL to /ambient): the calm
    # theme-adaptive desktop, not the older /wallpaper galaxy, which only stayed the default here
    # because this branch hardcoded its own URL and the switch never reached it.
    import time, threading, urllib.request
    port = args.port
    url = "http://127.0.0.1:%d/ambient" % port

    # macOS has a real behind-the-icons path (window levels, via PyObjC) — the counterpart to the
    # Windows Progman engine. Use it when it's installed; a browser window sits *over* the desktop
    # and is only the fallback. --front asks for the plain interactive window on purpose.
    if plat.is_macos() and not getattr(args, "front", False):
        from . import desktop_mac
        if getattr(args, "stop", False):
            print(desktop_mac.stop())
            return 0
        ok, why = desktop_mac.available()
        if ok:
            if desktop_mac.running_pid():
                print("collie wallpaper: already running (pid %s) · stop it with:  collie wallpaper --stop"
                      % desktop_mac.running_pid())
                return 0
            # AppKit must own the main thread, so the server goes to a daemon thread — the same
            # split the Windows engine gets by launching the server as its own pythonw process.
            from .webapp import main as web_main
            threading.Thread(target=web_main, args=(["--port", str(port), "--no-open"],),
                             daemon=True).start()
            for _ in range(60):
                try:
                    urllib.request.urlopen("http://127.0.0.1:%d/api/ver" % port, timeout=0.8).read()
                    break
                except Exception:
                    time.sleep(0.2)
            return desktop_mac.run(url, behind=True)
        print("collie wallpaper: %s" % why, file=sys.stderr)
        print("  falling back to a borderless browser window over the desktop.\n", file=sys.stderr)

    def _up():
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/ver" % port, timeout=0.8).read()
            return True
        except Exception:
            return False

    if _up():
        ok, detail = _desktop_window(url, kiosk=args.kiosk)
        print("collie wallpaper · %s · %s" % (url, "window opened" if ok else "no window: " + detail))
        return 0 if ok else 1

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
    """Run the browser-bridge server (the Chrome extension polls it; browser_* tools drive it).
    --install registers it to start hidden at logon, so collie keeps its real-browser powers."""
    from . import browserbridge as bb
    if getattr(args, "install", False):
        return bb.install_autostart()
    if getattr(args, "uninstall", False):
        return bb.uninstall_autostart()
    argv = ["--port", str(args.port)] if args.port else []   # [] not None: None re-reads argv
    if getattr(args, "browser", False):
        argv.append("--browser")
    return bb.main(argv)


def cmd_record(args):
    """Screen recording with a circular webcam bubble + mic (Loom / Reframe style), via ffmpeg.
    Sub-actions: start (default) / stop / status / devices. See harness/record.py."""
    from . import record as rec
    action = getattr(args, "record_action", None) or "start"
    try:
        if action == "stop":
            print(rec.stop())
        elif action == "status":
            print(rec.status())
        elif action == "devices":
            cams, mics = rec.list_capture_devices()
            print("cameras:\n  " + ("\n  ".join(cams) or "(none found)"))
            print("microphones:\n  " + ("\n  ".join(mics) or "(none found)"))
            screens = rec.list_screens()
            if screens:
                print("monitors:\n  " + "\n  ".join(screens))
        elif action == "windows":
            print("windows:\n  " + ("\n  ".join(rec.list_windows()) or "(none)"))
        elif action == "list":
            recs = rec.list_recordings()
            print("recordings in %s:\n  " % rec._default_outdir() + ("\n  ".join(
                "%s  (%.1f MB)" % (r["name"], r["mb"]) for r in recs) or "(none)"))
        else:  # start
            print(rec.start(webcam=args.webcam, mic=args.mic, sysaudio=args.sys_audio,
                            fps=args.fps, cam_size=args.cam_size, margin=args.margin,
                            position=args.position, mirror=not args.no_mirror,
                            monitor=args.monitor, region=args.region, window=args.window,
                            out=args.out, no_cam=args.no_cam, no_mic=args.no_mic, countdown=args.countdown))
        return 0
    except Exception as e:
        print("record: %s" % e)
        return 1


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
    """collie init — one-time PROJECT prep for the current repo. code_search is ripgrep-backed now
    (no index to build), so this front-loads the memory embedder's first-use model download and
    validates the codemap. --rules additionally has the MODEL explore the repo and write an AGENTS.md
    (the opencode `/init` convention); collie reads AGENTS.md / CLAUDE.md as project rules every run.
    For machine-level setup (install deps, pick a provider), use `collie setup`."""
    import time as _t
    cwd = os.path.abspath(args.cwd or os.getcwd())
    print("collie init · %s" % cwd)
    if not args.no_config:
        _setup_wizard(force=True)     # provider/model first — init is also "set me up" (tty only)
    t0 = _t.time()
    emb = _embedder(args.embed)                       # warm the memory embedder (first use downloads)
    if emb is not None:
        emb.embed("warm-up", kind="query")
        print("  ✓ semantic memory ready: %s (dim=%d)  [%.1fs]" % (emb.name, emb.dim, _t.time() - t0))
    else:
        print("  · semantic memory unavailable — BM25 keyword recall (run `collie setup` to enable)")
    from . import codemap                             # codemap (cheap; validates the map view)
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


def cmd_setup(args):
    """collie setup — machine-level onboarding ("collie doctor" + one-click install). Checks the
    environment (POSIX shell, ripgrep, the ONNX deps for semantic memory), installs the missing
    Python pieces with ONE confirmation, prints OS-specific hints for the non-pip tools, and picks
    a provider. `--check` diagnoses only (installs nothing); `--yes` installs without prompting."""
    import importlib.util as _il
    import shutil as _sh
    import subprocess as _sp
    from . import plat
    check_only = getattr(args, "check", False)
    assume_yes = getattr(args, "yes", False)
    print("collie setup · %s\n" % plat.os_label())

    def have(mod):
        return _il.find_spec(mod) is not None

    # 1) POSIX shell (the cross-platform shell contract) ----------------------------------------
    sh = plat.posix_shell()
    if sh:
        print("  ✓ POSIX shell: %s" % sh)
    else:
        hint = ("winget install Git.Git  (Git Bash)" if plat.is_windows()
                else "your package manager (bash ships with the OS)")
        print("  ✗ POSIX shell: none — the `bash` tool degrades to cmd.exe.\n    install: %s" % hint)

    # 2) ripgrep (code_search backend; grep is the fallback) ------------------------------------
    if _sh.which("rg"):
        print("  ✓ ripgrep: %s" % _sh.which("rg"))
    elif _sh.which("grep"):
        print("  · ripgrep not found — using grep (fine; rg is faster on big repos)")
    else:
        rg_hint = ("winget install BurntSushi.ripgrep.MSVC" if plat.is_windows()
                   else "brew install ripgrep" if plat.is_macos() else "apt install ripgrep")
        print("  ✗ no ripgrep or grep — code_search needs one.  install: %s" % rg_hint)

    # 3) semantic-memory deps (granite via onnxruntime) ----------------------------------------
    need = [m for m in ("onnxruntime", "tokenizers", "huggingface_hub", "numpy") if not have(m)]
    if not need:
        print("  ✓ semantic memory deps present (onnxruntime, tokenizers, huggingface_hub, numpy)")
    else:
        print("  ✗ semantic memory needs: %s" % ", ".join(need))
        if check_only:
            print("    install: pip install collie-harness[local]")
        else:
            ok = assume_yes or _confirm("  install semantic-memory deps now (pip install "
                                        "collie-harness[local])?")
            if ok:
                rc = _sp.run([sys.executable, "-m", "pip", "install",
                              "onnxruntime", "tokenizers", "huggingface_hub", "numpy"]).returncode
                print("  %s deps install" % ("✓" if rc == 0 else "✗"))
            else:
                print("  · skipped — memory runs on BM25 keyword recall until installed")

    # 4) pre-download the default model so the first run is instant -----------------------------
    if not check_only and not need and have("onnxruntime"):
        want = assume_yes or _confirm("  pre-download the granite semantic model (~55MB) now?")
        if want:
            try:
                from .embeddings import make_embedding
                e = make_embedding("granite")
                print("  ✓ model ready: %s (dim=%d)" % (e.name, e.dim))
            except Exception as e:
                print("  ✗ model download failed (%s) — will retry on first use; for a mirror set "
                      "COLLIE_HF_ENDPOINT=https://hf-mirror.com" % (type(e).__name__))

    # 5) real-browser bridge -------------------------------------------------------------------
    # Without this, collie's browser_* tools fall back to a logged-out scratch browser and every
    # "check my account" task fails confusingly. The classic failure: the Chrome extension IS
    # loaded, but nobody ever started the local server it polls.
    from . import browserbridge as _bb
    _ext = os.path.join(os.path.dirname(os.path.abspath(_bb.__file__)), "browser_ext")
    if _bb._bridge_live():
        # Compare the LOADED extension's version with the one we ship. A mismatch means Chrome is
        # running a copy from some other path (a second checkout, a \wsl$ share) — every fix you
        # make here is invisible to it, which is maddening to debug without this line.
        import json as _json
        import urllib.request as _urlreq
        want = ""
        try:
            with open(os.path.join(_ext, "manifest.json"), encoding="utf-8") as _f:
                want = (_json.load(_f) or {}).get("version", "")
        except Exception:
            pass
        got = ""
        try:
            with _urlreq.urlopen("http://127.0.0.1:%d/health" % _bb._port(), timeout=2) as _r:
                got = (_json.loads(_r.read() or b"{}") or {}).get("extension_version", "")
        except Exception:
            pass
        if want and got and want != got:
            print("  ! real browser: bridge live, but the loaded extension is v%s while this collie "
                  "ships v%s\n    it is loaded from ANOTHER copy — remove it and Load unpacked: %s"
                  % (got, want, _ext))
        else:
            print("  ✓ real browser: bridge live, extension connected%s" % (" (v%s)" % got if got else ""))
    elif _bb._server_up(_bb._port()):
        print("  · real browser: bridge running, but no extension connected.\n"
              "    load it: chrome://extensions → Developer mode → Load unpacked → %s" % _ext)
    else:
        print("  ✗ real browser: bridge not running — browser tools would use a LOGGED-OUT browser")
        if check_only:
            print("    fix: collie browser-bridge   (and load %s in chrome://extensions)" % _ext)
        elif assume_yes or _confirm("  start the browser bridge now and run it at every logon?"):
            ok = _bb.start_background()
            print("  %s bridge started" % ("✓" if ok else "✗"))
            _bb.install_autostart()
            if ok and not _bb._bridge_live():
                print("    now load the extension: chrome://extensions → Developer mode → "
                      "Load unpacked → %s" % _ext)

    # 6) provider (interactive) ------------------------------------------------------------------
    if not check_only:
        print("")
        _setup_wizard(force=True)
    print("\nsetup %s." % ("check complete" if check_only else "complete — try: collie -p \"explain this repo\""))
    return 0


def _confirm(prompt):
    """Yes/no on a tty; default NO off a tty (non-interactive/CI never auto-installs)."""
    try:
        if not sys.stdin.isatty():
            return False
        return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_config(args):
    """collie config — read/write ~/.collie/settings.json from the command line.

        collie config            list every knob and its effective value
        collie config LANG       print one
        collie config LANG zh    set one (merges; never clobbers the other keys)

    Scriptable counterpart to the GUI Settings panel — it's how the Windows installer hands the
    language you picked in the wizard to the app, so the first launch is already in your language.
    """
    from . import settings
    keys = [s["key"] for s in settings.SCHEMA]
    if not args.key:
        vals = settings.all_values()
        for k in keys:
            print("%-18s %s" % (k, vals.get(k, "")))
        return 0
    key = args.key.upper()
    if key not in keys:
        print("collie config: unknown key %r (try `collie config` to list them)" % args.key,
              file=sys.stderr)
        return 2
    spec = next(s for s in settings.SCHEMA if s["key"] == key)
    if args.value is None:
        print(settings.get(key, spec.get("default", "")) or "")
        return 0
    # validate against the schema's own options — a typo'd language should fail loudly, not
    # silently persist a value nothing reads
    opts = [o["value"] for o in spec.get("options", [])]
    if opts and args.value not in opts:
        print("collie config: %s must be one of: %s" % (key, ", ".join(opts)), file=sys.stderr)
        return 2
    settings.update({key: args.value})
    print("%s = %s" % (key, args.value))
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
        "loop", "repl", "tui", "web", "app", "wallpaper", "browser-bridge", "record", "mcp", "init",
        "setup", "jobs", "config"}


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
    pw.add_argument("--remote", action="store_true",
                    help="also dial the public relay so a phone can drive this desktop from anywhere "
                         "(relay via $COLLIE_RELAY, default wss://collie.run)")
    pw.add_argument("--lan", action="store_true",
                    help="also listen on this machine's network address, so a phone on the same Wi-Fi "
                         "can reach it directly, no relay (CollieIOS); pairing is still required")
    pw.add_argument("--qr", action="store_true",
                    help="with --lan, also print a QR fallback of the one-shot pairing secret "
                         "(for when a camera can't read the ring code)")
    pw.set_defaults(open=True, fn=cmd_web)

    # wallpaper: collie owns its own live desktop window (no third-party wallpaper engine)
    pwp = sub.add_parser("wallpaper", help="live desktop behind your icons (Windows engine); "
                                           "--install autostarts it at logon")
    pwp.add_argument("--port", type=int, default=8787, help="preferred port (a free one is picked if busy)")
    pwp.add_argument("--kiosk", action="store_true", help="non-Windows: immersive full-screen window")
    pwp.add_argument("--front", action="store_true",
                     help="macOS: an ordinary interactive window instead of the behind-the-icons desktop")
    pwp.add_argument("--install", action="store_true", help="autostart the wallpaper at every logon")
    pwp.add_argument("--uninstall", action="store_true", help="remove the logon autostart")
    pwp.add_argument("--stop", action="store_true", help="cleanly stop the running wallpaper engine")
    pwp.add_argument("--boot", action="store_true", help=argparse.SUPPRESS)  # internal autostart entry
    pwp.set_defaults(fn=cmd_wallpaper)

    # browser-bridge: LLM-driven real browser via a Chrome extension (authenticated / full-page)
    pb = sub.add_parser("browser-bridge", help="run the bridge the browser extension polls (browser_* tools)")
    pb.add_argument("--port", type=int, default=0)
    pb.add_argument("--browser", action="store_true",
                    help="also auto-launch a managed Chromium with the extension (no manual install)")
    pb.add_argument("--install", action="store_true",
                    help="start the bridge hidden at every logon (keeps real-browser powers)")
    pb.add_argument("--uninstall", action="store_true", help="remove the logon autostart")
    pb.set_defaults(fn=cmd_browser_bridge)

    # record: Loom/Reframe-style screen capture with a circular webcam bubble + mic, via ffmpeg
    prc = sub.add_parser("record", help="screen recording with a circular webcam bubble + mic "
                                        "(start / stop / status / devices)")
    prc.add_argument("record_action", nargs="?", default="start",
                     choices=["start", "stop", "status", "devices", "windows", "list"],
                     help="start (default), stop, status, devices, windows, or list recordings")
    prc.add_argument("--window", default=None,
                     help="record just this window (by title; see `record windows`) — small + smooth 30fps")
    prc.add_argument("--webcam", default=None,
                     help="camera device name (default: first found; see `record devices`)")
    prc.add_argument("--mic", default=None, help="microphone device name (default: first found)")
    prc.add_argument("--no-cam", dest="no_cam", action="store_true", help="screen only, no webcam bubble")
    prc.add_argument("--no-mic", dest="no_mic", action="store_true", help="no microphone audio")
    prc.add_argument("--sys-audio", dest="sys_audio", default=None,
                     help="also record system audio from this loopback device, mixed with the mic "
                          "(see `record devices`; needs Stereo Mix or a virtual audio cable)")
    prc.add_argument("--monitor", type=int, default=None,
                     help="record only display N (1-based, left-to-right; see `record devices`)")
    prc.add_argument("--region", default=None, help="record only a region, 'X,Y,W,H'")
    prc.add_argument("--position", default="bl", choices=["bl", "br", "tl", "tr"],
                     help="webcam bubble corner: bl/br/tl/tr (default bl)")
    prc.add_argument("--no-mirror", dest="no_mirror", action="store_true",
                     help="don't mirror the webcam (default: mirrored, like a selfie)")
    prc.add_argument("--countdown", type=int, default=0, help="3-2-1 countdown seconds before start")
    prc.add_argument("--fps", type=int, default=30, help="frame rate (default 30)")
    prc.add_argument("--cam-size", dest="cam_size", type=int, default=240,
                     help="webcam bubble diameter in px (default 240)")
    prc.add_argument("--margin", type=int, default=40, help="bubble margin from the corner in px (default 40)")
    prc.add_argument("--out", default=None, help="output file (default: the Collie folder under your videos dir)")
    prc.set_defaults(fn=cmd_record)

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
    pi = sub.add_parser("init", help="project prep for this repo: warm the memory model + codemap; "
                                     "--rules writes AGENTS.md")
    pi.add_argument("--cwd", default=None)
    pi.add_argument("--no-config", action="store_true",
                    help="skip the provider/model prompt (CI / scripted runs)")
    pi.add_argument("--embed", default="auto", help="embedder (auto|granite|bge-m3|e5|bm25)")
    pi.add_argument("--rules", action="store_true",
                    help="also have the model explore the repo and write an AGENTS.md")
    pi.add_argument("--provider", default=None, help="provider for --rules (default: configured one)")
    pi.set_defaults(fn=cmd_init)

    # app: collie in a real desktop window (WebView2) — what the installer's shortcut launches
    pa = sub.add_parser("app", help="open collie in a native desktop window (not a browser tab)")
    pa.add_argument("--port", type=int, default=8787)
    pa.add_argument("--open", action="store_true", help=argparse.SUPPRESS)
    pa.set_defaults(fn=cmd_app)

    # setup: machine-level onboarding — deps + model + provider ("collie doctor" + one-click install)
    ps = sub.add_parser("setup", help="install deps, pick a provider, pre-download the model "
                                      "(--check = diagnose only)")
    ps.add_argument("--check", action="store_true", help="diagnose only; install nothing")
    ps.add_argument("--yes", action="store_true", help="install without prompting")
    ps.set_defaults(fn=cmd_setup)

    # config: scriptable settings.json access (the installer uses it to seed the UI language)
    pc = sub.add_parser("config", help="read/write settings (config | config KEY | config KEY VALUE)")
    pc.add_argument("key", nargs="?", default="")
    pc.add_argument("value", nargs="?", default=None)
    pc.set_defaults(fn=cmd_config)

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
