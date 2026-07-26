"""Surface smoke suite ($0 — mock provider). Spawns each collie CLI surface and checks it starts,
responds, and exits cleanly (no crash). Strict per-surface timeouts + hard kills (a stuck server
must never hang the suite).
    .venv/bin/python tests/surfaces_test.py     (exit 0 = all pass)"""
import json, os, subprocess, sys, threading, time, urllib.error, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = dict(os.environ, COLLIE_PROVIDER="mock", PYTHONUNBUFFERED="1",
           # isolate session writes so the mock suite never pollutes the user's real Map run list
           COLLIE_SESSIONS_DIR=os.path.join(__import__("tempfile").gettempdir(), "collie_surftest_sessions"))
# Invoke collie as a module (sys.executable -m harness.cli), NOT the installed `collie` console
# script — so the suite runs from a bare checkout on any OS with no PATH / install assumption.
COLLIE = [sys.executable, "-m", "harness.cli"]
results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name + (("  :: " + detail) if detail and not cond else ""))

def run(args, timeout=45, stdin=None):
    p = subprocess.run(COLLIE + args, cwd=ROOT, env=ENV, capture_output=True,
                       text=True, timeout=timeout, input=stdin)
    return p.stdout, p.stderr, p.returncode

def test_run_json():
    out, _, rc = run(["run", "hi there", "--json"])
    d = json.loads(out)
    check("run --json valid + keyed", rc == 0 and "answer" in d and "total_tokens" in d)

def test_run_stream_json():
    out, _, rc = run(["run", "hi there", "--stream-json"])
    lines = [json.loads(l) for l in out.splitlines() if l.strip()]
    check("run --stream-json valid NDJSON", rc == 0 and len(lines) >= 1 and all(isinstance(x, dict) for x in lines))

def test_dashboard():
    out, _, rc = run(["dashboard"])
    p = os.path.join(ROOT, "data", "dashboard.html")
    check("dashboard builds valid html", rc == 0 and os.path.exists(p) and "<html" in open(p).read().lower())

def test_repl():
    out, _, rc = run(["repl"], stdin="hi\n/exit\n", timeout=45)
    check("repl greets + saves + exits", "collie repl" in out and "session saved" in out and rc == 0)

def test_tui():
    out, _, rc = run(["tui"], stdin="/exit\n", timeout=45)
    check("tui starts + exits clean", "session" in out and rc == 0 and "Traceback" not in out)

def test_browser_bridge_health():
    p = subprocess.Popen(COLLIE + ["browser-bridge", "--port", "8689"], cwd=ROOT,
                         env=dict(ENV, COLLIE_BROWSER_BRIDGE_NOSPAWN="1"),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ok = False
        for _ in range(20):
            try:
                d = json.load(urllib.request.urlopen("http://127.0.0.1:8689/health", timeout=1))
                ok = d.get("ok") is True and "extension_connected" in d; break
            except Exception:
                time.sleep(0.25)
        check("browser-bridge /health responds", ok)
        # SECURITY: a drive-by WEB PAGE (http Origin) must NOT be able to drive the bridge
        def status(path, headers, data=None, method=None):
            # returns the HTTP code, or -1 on timeout (a request that PASSED the CSRF gate and is
            # long-polling /poll — i.e. NOT 403).
            try:
                req = urllib.request.Request("http://127.0.0.1:8689" + path, data=data,
                                             headers=headers, method=method)
                urllib.request.urlopen(req, timeout=2)
                return 200
            except urllib.error.HTTPError as e:
                return e.code
            except Exception:
                return -1
        s = status("/enqueue", {"Origin": "https://evil.example", "content-type": "application/json",
                                "X-Collie-Bridge": "1"}, b'{"action":"eval"}')
        check("browser-bridge blocks web-origin /enqueue (403)", s == 403)
        # SECURITY: no-Origin CSRF (fetch mode:'no-cors' / <img>) carries no Origin but WITHOUT the
        # X-Collie-Bridge header must still be refused — else it could DEQUEUE (steal) a command
        s = status("/poll", {})                       # no header, no origin -> the drive-by GET hole
        check("browser-bridge /poll refuses no-header GET (403)", s == 403)
        s = status("/enqueue", {"content-type": "application/json"}, b'{"action":"eval"}')
        check("browser-bridge /enqueue refuses no-header POST (403)", s == 403)
        # a legit collie request (header present, no web Origin) is NOT blocked by the CSRF gate
        s = status("/poll", {"X-Collie-Bridge": "1"})
        check("browser-bridge /poll allows header'd request (not 403)", s != 403)
    finally:
        p.kill()
        try: p.wait(timeout=5)
        except Exception: pass

def test_acp_initialize():
    try:
        import acp  # noqa: F401 — optional extra (agent-client-protocol); the ACP surface needs it
    except ImportError:
        print("  SKIP acp initialize -> JSON-RPC result :: agent-client-protocol not installed")
        return
    p = subprocess.Popen(COLLIE + ["acp"], cwd=ROOT, env=ENV, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        # ACP wire format = newline-delimited JSON-RPC
        p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": 1, "clientCapabilities": {}}}) + "\n").encode())
        p.stdin.flush()
        box = {}
        th = threading.Thread(target=lambda: box.__setitem__("l", p.stdout.readline()), daemon=True)
        th.start(); th.join(timeout=15)
        line = box.get("l")
        ok = False
        if line:
            j = json.loads(line); ok = j.get("id") == 1 and "result" in j
        check("acp initialize -> JSON-RPC result", ok)
    finally:
        p.kill()
        try: p.wait(timeout=5)
        except Exception: pass

def test_map_codemap():
    """The Map's data layer: build_tree yields real facts and read_source is path-traversal safe."""
    sys.path.insert(0, ROOT)
    from harness import codemap
    tree = codemap.build_tree(ROOT)
    check("codemap.build_tree returns files", len(tree) > 10,
          "got %d files" % len(tree))
    check("codemap facts (defs+imports present)",
          any(f.get("defs") for f in tree) and any(f.get("imports") for f in tree))
    check("codemap.read_source reads a real file",
          bool(codemap.read_source(ROOT, "harness/pack.py")))
    check("codemap.read_source blocks path traversal",
          codemap.read_source(ROOT, "../../etc/passwd") is None)
    # per-repo + per-session (the two Map axes)
    check("codemap.git_root finds this repo", codemap.git_root(ROOT) == os.path.realpath(ROOT)
          or codemap.git_root(ROOT) == ROOT)
    check("codemap.git_root None outside a repo", codemap.git_root("/etc") is None)
    # a synthetic run that reads one file, greps another, and edits a third -> constellation + diff
    fake = {"id": "t", "cwd": ROOT, "messages": [
        {"role": "assistant", "tool_calls": [{"name": "read_file", "args": {"path": "harness/pack.py"}}]},
        {"role": "assistant", "tool_calls": [{"name": "bash", "args": {"command": "sed -n 1,5p harness/codemap.py"}}]},
        {"role": "assistant", "tool_calls": [{"name": "edit_file", "args": {
            "path": "harness/settings.py", "old_string": "import json", "new_string": "import json  # noqa"}}]}]}
    sm = codemap.session_map(fake, ROOT)
    check("codemap.session_map maps touched files (incl bash)", len(sm["files"]) >= 2 and bool(sm["agents"]),
          "files=%d agents=%d" % (len(sm["files"]), len(sm["agents"])))
    check("codemap.session_map carries abs paths for cross-repo reads",
          all(x.get("abs") for x in sm["files"]))
    edited = [x for x in sm["files"] if x.get("edited")]
    check("codemap.session_map flags edited files with diff hunks",
          len(edited) == 1 and edited[0]["edits"][0]["kind"] == "edit"
          and edited[0]["edits"][0]["new"] == "import json  # noqa",
          "edited=%d" % len(edited))
    # read_abs guards reads to under the user's home — so test with a file actually under home
    # (the repo itself may live anywhere: c:\workspace here, D:\a on Windows CI, /home on Linux CI).
    import tempfile as _tf, shutil as _sh
    _hd = _tf.mkdtemp(dir=os.path.expanduser("~"))
    try:
        _fp = os.path.join(_hd, "cm_read_abs_probe.py")
        open(_fp, "w", encoding="utf-8").write("x = 1\n")
        check("codemap.read_abs reads under home", bool(codemap.read_abs(_fp)))
    finally:
        _sh.rmtree(_hd, ignore_errors=True)
    check("codemap.read_abs blocks outside home", codemap.read_abs("/etc/passwd") is None)

def test_map_web():
    """The Map's web surface: /map serves the galaxy, /api/tree + /api/file feed it, /api/file is
    guarded, and /api/session emits structured tool_calls the replay can parse."""
    port = 8791
    p = subprocess.Popen(COLLIE + ["web", "--port", str(port)], cwd=ROOT, env=ENV,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = "http://127.0.0.1:%d" % port
        up = False
        for _ in range(40):
            try:
                urllib.request.urlopen(base + "/api/tree", timeout=1); up = True; break
            except Exception:
                time.sleep(0.25)
        check("map web: server came up", up)
        if not up:
            return
        html = urllib.request.urlopen(base + "/map", timeout=3).read().decode("utf-8", "ignore")
        check("map web: /map serves the galaxy page",
              "/map/three.min.js" in html and "/api/tree" in html)
        try:                                          # the sheepdog meadow scene was removed
            urllib.request.urlopen(base + "/meadow", timeout=3)
            check("map web: /meadow is removed (404)", False)
        except Exception as e:
            check("map web: /meadow is removed (404)", getattr(e, "code", None) == 404)
        tree = json.load(urllib.request.urlopen(base + "/api/tree", timeout=3))
        check("map web: /api/tree builds the map", len(tree.get("files", [])) > 10)
        f = json.load(urllib.request.urlopen(base + "/api/file?path=harness/pack.py", timeout=3))
        check("map web: /api/file returns source", bool(f.get("source")))
        try:
            g = json.load(urllib.request.urlopen(base + "/api/file?path=../../etc/passwd", timeout=3))
            guarded = not g.get("source")             # 200 with an error body, no source leaked
        except urllib.error.HTTPError as he:
            guarded = he.code in (400, 403, 404)      # or a hard 4xx — either way, nothing leaked
        check("map web: /api/file guards traversal", guarded)
        sess = json.load(urllib.request.urlopen(base + "/api/sessions", timeout=3)).get("sessions", [])
        if sess:
            s = json.load(urllib.request.urlopen(
                base + "/api/session/" + urllib.request.quote(sess[0]["id"]), timeout=3))
            tcs = [tc for m in s.get("messages", []) for tc in (m.get("tool_calls") or [])]
            check("map web: /api/session tool_calls are structured (not repr strings)",
                  all(isinstance(tc, dict) for tc in tcs))
            sm = json.load(urllib.request.urlopen(
                base + "/api/session_map?id=" + urllib.request.quote(sess[0]["id"]), timeout=3))
            check("map web: /api/session_map returns a constellation shape",
                  "files" in sm and "agents" in sm and "repos" in sm)
        repos = json.load(urllib.request.urlopen(base + "/api/repos", timeout=5)).get("repos", [])
        check("map web: /api/repos discovers projects", len(repos) >= 1)
        tr = json.load(urllib.request.urlopen(base + "/api/tree?repo=" + urllib.request.quote(ROOT), timeout=3))
        check("map web: /api/tree?repo builds a chosen project", len(tr.get("files", [])) > 10)
        try:
            a = json.load(urllib.request.urlopen(base + "/api/file?abs=/etc/passwd", timeout=3))
            aguard = not a.get("source")
        except urllib.error.HTTPError as he:
            aguard = he.code in (400, 403, 404)
        check("map web: /api/file?abs guards outside home", aguard)
        # live event bus: /api/live is the SSE feed the Map + mini-map subscribe to for real-time
        # render. Raw socket + recv so a long-lived stream doesn't hang the reader.
        import socket
        buf = b""
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=4)
            # Host must be a loopback name or _host_ok() rejects it (403) as a DNS-rebinding guard.
            s.sendall(("GET /api/live HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nConnection: close\r\n\r\n"
                       % port).encode())
            s.settimeout(4)
            while b"live_hello" not in buf and len(buf) < 2000:   # frame may arrive after headers
                d = s.recv(400)
                if not d:
                    break
                buf += d
            s.close()
        except Exception:
            pass
        check("map web: /api/live streams the event bus", b"live_hello" in buf)
    finally:
        p.kill()
        try: p.wait(timeout=5)
        except Exception: pass


def test_image_upload():
    """Multimodal: POST /api/upload stashes an image (CSRF-gated); /api/stream?imgs=<id> runs with
    it as a multimodal message without crashing the (mock) run."""
    import re
    port = 8793
    p = subprocess.Popen(COLLIE + ["web", "--port", str(port)], cwd=ROOT, env=ENV,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = "http://127.0.0.1:%d" % port
        html = ""
        for _ in range(40):
            try:
                html = urllib.request.urlopen(base + "/", timeout=1).read().decode("utf-8", "ignore"); break
            except Exception:
                time.sleep(0.25)
        m = re.search(r'collie-token" content="([^"]+)"', html)
        if not m:
            check("image upload: server came up", False); return
        tok = m.group(1)
        PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
               "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
        def post(path, obj):
            req = urllib.request.Request(base + path, data=json.dumps(obj).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            return json.load(urllib.request.urlopen(req, timeout=5))
        iid = post("/api/upload?token=" + tok, {"media_type": "image/png", "data": PNG}).get("id")
        check("image upload returns an id", bool(iid))
        try:
            post("/api/upload", {"media_type": "image/png", "data": PNG}); code = 200   # no token
        except urllib.error.HTTPError as e:
            code = e.code
        check("image upload CSRF: unauth -> 403", code == 403)
        # a run that references the image must complete (mock), not crash on the multimodal message
        done = False
        try:
            r = urllib.request.urlopen(
                base + "/api/stream?token=%s&q=describe&imgs=%s" % (tok, iid), timeout=15)
            for _ in range(200):
                line = r.readline()
                if not line:
                    break
                if b"event: done" in line:
                    done = True; break
            r.close()
        except Exception:
            pass
        check("image upload: run with a multimodal message completes", done)
    finally:
        p.kill()
        try: p.wait(timeout=5)
        except Exception: pass

def test_cli_init():
    """collie init reports its memory status + validates the codemap, and exits 0 (code_search is
    ripgrep now — no index to build). Accepts either readiness signal: a real embedder warmed
    ("semantic memory ready") OR the honest BM25-only fallback when [local] isn't installed — CI
    runs the latter (COLLIE_EMBED=bm25), a dev box with granite the former."""
    out, err, rc = run(["init"], timeout=90)
    check("init exits 0", rc == 0, err[-200:])
    reported = ("semantic memory ready" in out) or ("BM25 keyword recall" in out)
    check("init reports memory status", reported, out[-200:])
    check("init scans codemap", "codemap:" in out, out[-200:])
    assert rc == 0 and "codemap:" in out

def test_sse_write_lock_serializes_frames():
    """The run-stream keep-alive: a heartbeat thread pings the SSE socket while the run's token/tool
    events write to the SAME socket. With _wlock set, _sse must emit WHOLE frames — a ping can never
    interleave mid-frame and corrupt the stream. (Without the lock, concurrent write+flush pairs would
    splice.)"""
    import io
    from harness.webapp import Handler
    h = Handler.__new__(Handler)
    h.wfile = io.BytesIO()
    h._wlock = threading.Lock()
    N_THREADS, PER = 8, 40
    barrier = threading.Barrier(N_THREADS)

    def writer(ev, n):
        barrier.wait()                       # maximize contention: all fire at once
        for i in range(PER):
            h._sse(ev, {"n": n, "i": i})
    ts = [threading.Thread(target=writer, args=("ping" if k % 2 else "token", k)) for k in range(N_THREADS)]
    for t in ts: t.start()
    for t in ts: t.join()
    frames = [f for f in h.wfile.getvalue().decode().split("\n\n") if f]
    for f in frames:
        assert f.startswith("event: ") and "\ndata: " in f and f.count("event: ") == 1, \
            "interleaved/corrupt SSE frame: %r" % f[:100]
    check("sse write-lock: no interleaved frames", len(frames) == N_THREADS * PER,
          "got %d, want %d" % (len(frames), N_THREADS * PER))
    assert len(frames) == N_THREADS * PER, "every frame intact, none merged/lost"

def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
        except subprocess.TimeoutExpired:
            check(name + " (TIMEOUT)", False)
        except Exception as e:
            check(name + " (ERROR: %s)" % e, False)
    npass = sum(1 for _, c in results if c)
    print("\n== SURFACES: %d/%d passed ==%s" % (npass, len(results),
          "" if npass == len(results) else " FAILS: " + ", ".join(n for n, c in results if not c)))
    return 0 if npass == len(results) else 1

if __name__ == "__main__":
    sys.exit(main())
