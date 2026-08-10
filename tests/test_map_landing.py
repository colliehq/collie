"""What the star-map shows when you just open it.

Every fault behind the "opens black" report was a default that assumed the server sits in the user's
project. It does not: wallpaper.start_server_windowless spawns it with no cwd of its own, so from a
shortcut launch it inherits Explorer's — and the map drew C:\\Windows\\System32.

    python3 tests/test_map_landing.py
"""
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def main():
    from harness import webapp, sessions

    # --- the default project ------------------------------------------------------------------
    neutral = tempfile.mkdtemp(prefix="not_a_repo_")     # stands in for System32
    repo = tempfile.mkdtemp(prefix="worked_in_")
    os.makedirs(os.path.join(repo, ".git"), exist_ok=True)

    here = os.getcwd()
    real_recent = sessions.recent
    cache = dict(webapp.Handler._REPOS_CACHE)
    try:
        os.chdir(neutral)
        sessions.recent = lambda n=10: [{"id": "s1", "cwd": repo}]
        got = webapp.Handler._default_repo()
        check(os.path.realpath(got) == os.path.realpath(repo),
              "a cwd that is not a project falls back to the last project worked in")

        sessions.recent = lambda n=10: []
        webapp.Handler._REPOS_CACHE["repos"] = [{"root": repo, "name": "worked_in"}]
        check(os.path.realpath(webapp.Handler._default_repo()) == os.path.realpath(repo),
              "...or, with no history, to a discovered project")

        webapp.Handler._REPOS_CACHE.pop("repos", None)
        check(os.path.realpath(webapp.Handler._default_repo()) == os.path.realpath(neutral),
              "...and with nothing at all it returns the cwd rather than inventing one")

        os.chdir(repo)
        sessions.recent = lambda n=10: [{"id": "s1", "cwd": neutral}]
        check(os.path.realpath(webapp.Handler._default_repo()) == os.path.realpath(repo),
              "a cwd that IS a project always wins — the user launched collie there")
    finally:
        os.chdir(here)
        sessions.recent = real_recent
        webapp.Handler._REPOS_CACHE.clear()
        webapp.Handler._REPOS_CACHE.update(cache)

    # ...and that /api/tree actually USES it. Testing the helper alone would pass while the endpoint
    # still called os.getcwd() — which is exactly the state that drew System32.
    proj = tempfile.mkdtemp(prefix="served_")
    os.makedirs(os.path.join(proj, ".git"), exist_ok=True)
    open(os.path.join(proj, "m.py"), "w").write(      # >=3 lines; build_tree skips trivial files
        "import os\n\n\ndef f():\n    return os.sep\n\n\ndef g():\n    return f()\n")
    sent = {}

    class FakeHandler(webapp.Handler):
        def __init__(self):
            pass

        def _send_json(self, obj, status=200):
            sent["obj"] = obj

    try:
        os.chdir(neutral)
        sessions.recent = lambda n=10: [{"id": "s1", "cwd": proj}]
        webapp.Handler._TREE_CACHE.clear()
        FakeHandler()._serve_tree({})
        check(os.path.realpath(sent.get("obj", {}).get("cwd", "")) == os.path.realpath(proj),
              "/api/tree with no ?repo serves that project, not the directory collie was launched in")
        check(len(sent.get("obj", {}).get("files") or []) > 0, "...and it has something to draw")
    finally:
        os.chdir(here)
        sessions.recent = real_recent
        webapp.Handler._TREE_CACHE.clear()

    # --- the landing view ---------------------------------------------------------------------
    page = open(os.path.join(ROOT, "harness", "webui", "map.html"), encoding="utf-8").read()
    check('location.replace("?session=' not in page,
          "a bare /map does NOT redirect to a run — a run's footprint is a handful of stars")
    check("/api/tree" in page, "it draws the project instead")
    check(re.search(r'fetch\("/api/session_map', page) is not None,
          "and ?session= deep links still work")
    # A session that maps to nothing must fall back rather than leave the canvas empty.
    m = re.search(r"if\(!\(m\.files&&m\.files\.length\)\)\{(.{0,600}?)\n\s*\}", page, re.S)
    check(bool(m) and "/api/tree" in m.group(1),
          "a hand-picked run with no file work falls back to the project, not to black")

    # --- the counts the picker shows ----------------------------------------------------------
    src = open(os.path.join(ROOT, "harness", "sessions.py"), encoding="utf-8").read()
    check("touched, edited = set(), set()" in src,
          "edits/touches count DISTINCT FILES — counting tool calls made one file look like eleven")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "map landing: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
