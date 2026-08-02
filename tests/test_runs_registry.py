"""A run belongs to the machine, not to the window that started it.

Two things had to be true before more than one conversation at a time could be honest, and neither
was.

The run died with the socket. `h.emit` wrote to the starting client FIRST and unguarded, so the next
event after a browser went away raised BrokenPipeError, which travelled out of h.run() and ended the
run. The web UI carried a comment asserting the opposite and a reconnect path built on it; what it
reconnected to was a corpse. Closing a tab, switching threads or locking a phone all silently
cancelled the work.

And nothing knew what was running. State lived in one tab's `running` variable, so a second window
could not tell whether the machine was busy, and a sidebar could not show it.

    python3 tests/test_runs_registry.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


class _Res:
    def __init__(self, turns=3, verified=True, error=""):
        self.turns, self.verified, self.error = turns, verified, error


def unit():
    from harness.webapp import Handler
    with Handler._runs_lock:
        Handler._runs.clear()

    Handler._run_begin("s1", "fix the parser", "/tmp/a")
    snap = Handler._runs_snapshot()
    check(len(snap) == 1 and snap[0]["state"] == "running", "a started run is listed as running")
    check(snap[0]["ask"] == "fix the parser", "with what was asked")

    Handler._run_end("s1", _Res(turns=4, verified=True))
    snap = Handler._runs_snapshot()
    check(snap[0]["state"] == "done" and snap[0]["verified"] is True,
          "and finishes carrying the gate's verdict")

    # The `finally` guard runs immediately after the success path. Before _run_end became
    # idempotent it overwrote every good result with its own catch-all, so every finished run
    # read as failed.
    Handler._run_end("s1", error="ended without a verdict")
    check(Handler._runs_snapshot()[0]["state"] == "done",
          "a later catch-all cannot overwrite a verdict already recorded")

    Handler._run_begin("s2", "second thing", "/tmp/b")
    Handler._run_end("s2", error="RuntimeError: boom")
    got = [r for r in Handler._runs_snapshot() if r["session"] == "s2"][0]
    check(got["state"] == "failed" and "boom" in got["error"], "a crash is listed as failed, with why")

    Handler._run_begin("s3", "still going", "/tmp/c")
    states = [r["state"] for r in Handler._runs_snapshot()]
    check(states[0] == "running", "anything still running sorts above anything finished")

    for i in range(Handler._RUNS_KEEP + 5):
        Handler._run_begin("old%d" % i, "x", "/tmp")
        Handler._run_end("old%d" % i, _Res())
    finished = [r for r in Handler._runs_snapshot() if r["ended"]]
    check(len(finished) <= Handler._RUNS_KEEP,
          "finished runs are capped (%d <= %d)" % (len(finished), Handler._RUNS_KEEP))
    check(any(r["state"] == "running" for r in Handler._runs_snapshot()),
          "and the cap never evicts a run that is still going")
    with Handler._runs_lock:
        Handler._runs.clear()


def integration():
    """Start a real run, walk away mid-stream, and see it finish anyway.

    What this half does and does not prove, stated so nobody has to re-derive it: it CAUGHT the
    verdict being lost — the run finished, saved its answer, and was filed as `failed / turns=0`
    because the final `done` frame was written to the dead socket before the line that recorded the
    result. It does NOT discriminate on the emit guard: a mock run is over in milliseconds, fast
    enough that every emit lands in the send buffer before the RST is felt, so it passes with or
    without `_tx`. That one is held by reading the path, not by this.
    """
    port = 8994
    tmp = tempfile.mkdtemp(prefix="collie_runs_")
    env = dict(os.environ, COLLIE_SETTINGS_PATH=os.path.join(tmp, "settings.json"),
               COLLIE_SESSIONS_DIR=os.path.join(tmp, "sessions"), PYTHONUNBUFFERED="1")
    env.pop("COLLIE_PROVIDER", None)
    with open(env["COLLIE_SETTINGS_PATH"], "w") as fh:
        json.dump({"PROVIDER": "mock", "MODEL": "mock"}, fh)
    srv = subprocess.Popen([sys.executable, "-m", "harness.webapp", "--port", str(port), "--no-open"],
                           cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = "http://127.0.0.1:%d" % port
        tok = ""
        for _ in range(40):
            try:
                page = urllib.request.urlopen(base + "/", timeout=2).read().decode("utf-8", "ignore")
                m = re.findall(r"[0-9a-f]{32}", page)
                if m:
                    tok = m[0]
                    break
            except Exception:
                time.sleep(0.25)
        if not tok:
            check(False, "server came up")
            return

        def runs():
            u = base + "/api/runs?token=" + tok
            return json.load(urllib.request.urlopen(u, timeout=5))["runs"]

        sid = "walkaway-1"
        url = (base + "/api/stream?token=" + tok + "&session=" + sid +
               "&q=" + urllib.parse.quote("say something short"))

        # A raw socket closed with SO_LINGER 0, not urlopen().close().
        #
        # A polite close leaves the server's next few writes succeeding into a kernel buffer, so a
        # mock run — over in less time than it takes to ask twice — can finish without ever touching
        # a dead socket, and the test passes against the very code it exists to catch. RST makes the
        # next write fail immediately, which is what a closed laptop lid really looks like.
        def start_and_leave():
            import socket
            import struct
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=5)
                path = url[len(base):]
                s.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n"
                           "Connection: close\r\n\r\n" % path).encode())
                # Wait for the run to actually START before disappearing. Sending RST straight after
                # the headers kills the handler before it registers anything, which makes the whole
                # test measure nothing — the run is equally absent whether or not the fix is in.
                buf = b""
                deadline = time.time() + 10
                while b"event: start" not in buf and time.time() < deadline:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                s.close()                        # RST, not FIN
            except Exception:
                pass

        th = threading.Thread(target=start_and_leave, daemon=True)
        th.start()

        # Not asserting "caught it mid-flight": a mock run is over in well under the time it takes to
        # ask twice, so that check would be a coin toss. `running` while running is covered above,
        # deterministically. What matters here is that the run appears at all — registration happens
        # on the server, for a client that is already gone.
        listed = False
        for _ in range(60):
            time.sleep(0.25)
            if any(r["session"] == sid for r in runs()):
                listed = True
                break
        check(listed, "the run is listed by the server, not by the window that left")

        finished = None
        for _ in range(120):
            time.sleep(0.25)
            hit = [r for r in runs() if r["session"] == sid]
            if hit and hit[0]["ended"]:
                finished = hit[0]
                break
        check(finished is not None, "it finishes after the client walked away")
        if finished:
            check(finished["state"] == "done",
                  "and finishes normally, not as a casualty of the dropped socket (%s %r)"
                  % (finished["state"], finished["error"][:60]))
            check(finished["turns"] > 0, "having actually done turns (%s)" % finished["turns"])
    finally:
        srv.kill()


def main():
    unit()
    integration()
    print("\n  " + ("%d FAILED" % len(fails) if fails else "runs registry: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
