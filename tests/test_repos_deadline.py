"""/api/repos must answer even when the filesystem will not.

Walking a home directory is not merely slow in the bad case — it BLOCKS. On macOS, ~/Music and
~/Movies are the Apple Music and TV libraries, and os.walk over one full of cloud placeholders never
returns. Measured on a real machine: discover_repos had not finished after five minutes, twice, and
an instrumented walk could not even reach its own timeout check because control never came back from
the OS.

What that looks like from outside: the phone's Code screen spins forever, and a server thread is
gone for good.

Two defences, and this pins both — the names that are known to do it are skipped, and the endpoint
has a deadline regardless, because the next one will have a different name.

    python3 tests/test_repos_deadline.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import codemap                                          # noqa: E402
from harness import webapp                                           # noqa: E402

_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def main():
    # The names that hang, pruned at the top level of $HOME only.
    for name in ("Music", "Movies", "Library", "Pictures"):
        check(name in codemap._HOME_SKIP, "%s is skipped at the top of $HOME" % name)
    check("projects" not in codemap._HOME_SKIP and "src" not in codemap._HOME_SKIP,
          "and ordinary project directories are not")

    # A real scan of this machine's home has to be quick now.
    box = {}

    def scan():
        t0 = time.time()
        box["repos"] = codemap.discover_repos(os.path.expanduser("~"))
        box["t"] = time.time() - t0

    t = threading.Thread(target=scan, daemon=True)
    t.start()
    t.join(30)
    check("t" in box, "discover_repos finishes on this machine's home at all")
    if "t" in box:
        check(box["t"] < 20, "and quickly (%.2fs, %d repos)" % (box["t"], len(box["repos"])))

    # The deadline itself: with discovery replaced by something that never returns, the handler must
    # still answer. This is the guarantee that survives the next directory nobody thought of.
    sent = {}

    class FakeHandler(webapp.Handler):
        def __init__(self):                      # no socket, no request — only the method under test
            pass

        def _send_json(self, obj, status=200):
            sent["obj"] = obj
            sent["status"] = status

    forever = threading.Event()

    def never_returns(_home):
        forever.wait()                            # exactly as unresponsive as a stalled mount
        return []

    real_discover = codemap.discover_repos
    real_cache = dict(webapp.Handler._REPOS_CACHE)
    codemap.discover_repos = never_returns
    webapp.Handler._REPOS_CACHE.clear()
    webapp.Handler.REPOS_BUDGET_S = 1.0
    try:
        t0 = time.time()
        FakeHandler()._serve_repos()
        elapsed = time.time() - t0
    finally:
        forever.set()
        codemap.discover_repos = real_discover
        webapp.Handler._REPOS_CACHE.clear()
        webapp.Handler._REPOS_CACHE.update(real_cache)
        webapp.Handler.REPOS_BUDGET_S = 8.0

    check(elapsed < 5, "a scan that never returns still gets an answer out (%.2fs)" % elapsed)
    check(sent.get("status") == 200, "and it is a normal 200, not an error the client must decode")
    check(sent.get("obj", {}).get("partial") is True, "flagged partial, so the client can say so")
    check("repos" not in webapp.Handler._REPOS_CACHE,
          "the empty answer is NOT cached — a truthful nothing now must not become a permanent one")

    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "repos deadline: all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
