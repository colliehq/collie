"""Run one browser suite against a `collie web` started just for it.

steer_ui_check and parallel_ui_check both need a live server AND a browser, and both take the server
from the environment so they can be pointed at a running one by hand. That makes them awkward to
call from run_all.sh, which has neither — and a suite that is awkward to call from the suite runner
is a suite nobody runs.

The server it starts is its own: a temp settings file on the mock provider and a temp sessions dir,
so it can never talk to the user's real Collie or leave anything in their history.

    python3 tests/browser_suite.py steer_ui_check
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port():
    """A port nobody is on, chosen now rather than hoped for.

    This was hardcoded to 8993, and a leftover `python -m http.server` from someone's afternoon was
    sitting on it: the page fetch SUCCEEDED, carried a directory listing instead of collie, and the
    only thing the suite could think to say was "server never came up on port 8993" — about a server
    that had come up perfectly well, next to a port that was answering. Two suites failed for a
    reason nothing in the output pointed at. Ask the OS for a free one instead; the env var still
    pins it for anyone who needs a known port.
    """
    fixed = os.environ.get("COLLIE_BROWSER_SUITE_PORT")
    if fixed:
        return int(fixed)
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


PORT = _free_port()


def main():
    if len(sys.argv) < 2:
        print("usage: browser_suite.py <suite-name>")
        return 2
    name = sys.argv[1]
    script = os.path.join(ROOT, "tests", name + ".py")
    if not os.path.exists(script):
        print("no such suite: " + script)
        return 2

    tmp = tempfile.mkdtemp(prefix="collie_%s_" % name)
    env = dict(os.environ,
               COLLIE_SETTINGS_PATH=os.path.join(tmp, "settings.json"),
               COLLIE_SESSIONS_DIR=os.path.join(tmp, "sessions"),
               PYTHONUNBUFFERED="1")
    env.pop("COLLIE_PROVIDER", None)          # the file decides, so the picker is not pinned
    with open(env["COLLIE_SETTINGS_PATH"], "w") as fh:
        # The suites exercise a working desktop, not the first-launch name
        # dialog.  A fixed adopted name keeps that modal from intercepting the
        # composer differently on clean CI hosts and developer machines.
        json.dump({"PROVIDER": "mock", "MODEL": "mock", "COMPANION_NAME": "Rowan"}, fh)

    srv = subprocess.Popen([sys.executable, "-m", "harness.webapp", "--port", str(PORT), "--no-open"],
                           cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
    try:
        token, answered = "", False
        for _ in range(60):
            try:
                page = urllib.request.urlopen("http://127.0.0.1:%d/" % PORT, timeout=2)
                answered = True
                hit = re.findall(r"[0-9a-f]{32}", page.read().decode("utf-8", "ignore"))
                if hit:
                    token = hit[0]
                    break
            except Exception:
                time.sleep(0.25)
        if not token:
            # Say which of the two happened. They call for opposite fixes, and reporting both as
            # "never came up" is how a busy port gets debugged as a broken server.
            if answered:
                print("  port %d is answering, but not with collie — something else is already "
                      "serving it. Set COLLIE_BROWSER_SUITE_PORT to a free port." % PORT)
            else:
                print("  server never came up on port %d" % PORT)
                if srv.poll() is not None:
                    out = (srv.communicate(timeout=5)[0] or "").strip().splitlines()
                    print("  it exited %s: %s" % (srv.returncode, "; ".join(out[-4:]) or "(silent)"))
            return 1
        r = subprocess.run([sys.executable, script], cwd=ROOT,
                           env=dict(env, COLLIE_WEB="http://127.0.0.1:%d" % PORT,
                                    COLLIE_TOKEN=token))
        return r.returncode
    finally:
        srv.kill()


if __name__ == "__main__":
    sys.exit(main())
