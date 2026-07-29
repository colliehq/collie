"""The native app window must be pointed at the port the server ACTUALLY bound.

`webapp.main` scans forward when the asked-for port is busy — and used to keep the port it settled on
to itself. `collie app` asked for 8787, the server bound 8791, and the window was pointed at 8787: a
dead port. What that looks like is an app that opens, bounces in the Dock and shows nothing, and
relaunching makes it worse, because the abandoned server keeps its port and the next one moves
further along. Every symptom points at the window; none of them is the window.

    python3 tests/test_app_port.py
"""
import os
import socket
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def main():
    os.environ.setdefault("COLLIE_PROVIDER", "mock")
    from harness.webapp import main as web_main                      # noqa: E402

    # Pick a base port nothing else is using. Hardcoding 8787 made this fail whenever a real collie
    # happened to be running — the test would then be measuring the machine, not the code.
    base = 8787
    for cand in range(8830, 8990, 3):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", cand))
            probe.close()
            base = cand
            break
        except OSError:
            probe.close()

    # Take the preferred port and the next one, so the scan has to move twice.
    blockers = []
    for p in (base, base + 1):
        s = socket.socket()
        # HOLD the port EXCLUSIVELY so the server (which sets SO_REUSEADDR) is forced to scan past it.
        # Do NOT give the blocker SO_REUSEADDR: on Windows it means "share the port", and on macOS a
        # SO_REUSEADDR server can then bind the same port too — either way the server never scans and
        # the test can't exercise the fix. Windows needs SO_EXCLUSIVEADDRUSE; POSIX a plain bind holds
        # it (SO_REUSEADDR only reuses TIME_WAIT, not an active listener).
        if sys.platform == "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", p))
            s.listen(1)
            blockers.append(s)
        except OSError:
            s.close()                       # already taken by something else; the test still holds

    bound = {}
    threading.Thread(target=web_main, args=(["--port", str(base), "--no-open"],),
                     kwargs={"on_bound": lambda p: bound.setdefault("port", p)},
                     daemon=True).start()
    for _ in range(75):
        if "port" in bound:
            break
        time.sleep(0.2)

    check("port" in bound, "the server reports the port it bound")
    port = bound.get("port")
    check(port is not None and port != base,
          "and it is NOT the one that was asked for, because that one was busy (got %s)" % port)

    def reachable(p, timeout=3):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/ver" % p, timeout=timeout).read()
            return True
        except Exception:
            return False

    check(port is not None and reachable(port),
          "the reported port answers — a window pointed there shows the UI")
    check(not reachable(base, timeout=1.5),
          "the requested port does not answer, which is exactly where the window used to be sent")

    for s in blockers:
        try:
            s.close()
        except Exception:
            pass

    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "app port: all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
