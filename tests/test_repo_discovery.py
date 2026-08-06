"""Which directories count as a user's projects (harness/codemap.discover_repos).

The star-map opened onto a list of `collie_wt_test_*` temp directories with the real repository
nowhere in it, and both halves of that are tested here: a throwaway repo under TEMP is not a
project, and a project outside the home directory still has to be findable — which is the ordinary
Windows layout (code on C:\\workspace, a home holding little but AppData).

    python3 tests/test_repo_discovery.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def mkrepo(parent, name):
    d = os.path.join(parent, name)
    os.makedirs(os.path.join(d, ".git"), exist_ok=True)
    open(os.path.join(d, "a.py"), "w").write("x = 1\n")
    return d


def main():
    from harness import codemap

    home = tempfile.mkdtemp(prefix="fake_home_")
    real = mkrepo(home, "myproject")

    # A temp dir INSIDE the home, which is exactly where %TEMP% sits on Windows.
    temp = os.path.join(home, "AppData", "Local", "Temp")
    os.makedirs(temp, exist_ok=True)
    junk = mkrepo(temp, "collie_wt_test_abc123")

    found = {r["name"] for r in codemap.discover_repos(home)}
    check("myproject" in found, "a real repo under the home is found")
    check("collie_wt_test_abc123" not in found,
          "a repo under AppData/Temp is NOT a project — collie's own worktrees live there")
    check("AppData" not in found, "and AppData itself is never walked")

    # The system temp dir, wherever it actually is.
    sys_temp = tempfile.mkdtemp(prefix="collie_wt_test_")
    open(os.path.join(sys_temp, "b.py"), "w").write("y = 2\n")
    os.makedirs(os.path.join(sys_temp, ".git"), exist_ok=True)
    seeded = codemap.discover_repos(home, extra=[sys_temp])
    check(any(r["root"] == sys_temp for r in seeded),
          "a seed is taken at its word — the caller knows something the walk cannot")

    # Outside the home entirely: the usual Windows layout.
    elsewhere = tempfile.mkdtemp(prefix="not_home_")
    outside = mkrepo(elsewhere, "workspace_project")
    names = {r["name"] for r in codemap.discover_repos(home)}
    check("workspace_project" not in names, "a repo outside the home is missed by the walk alone")
    names2 = {r["name"] for r in codemap.discover_repos(home, extra=[outside])}
    check("workspace_project" in names2,
          "...and IS found when seeded from where work actually happened")
    check("myproject" in names2, "seeding does not displace what the walk found")

    # Siblings of a seed: one project on C:\workspace means the rest of it is projects too.
    sibling = mkrepo(elsewhere, "sibling_project")
    os.makedirs(os.path.join(elsewhere, "just_a_folder"), exist_ok=True)
    sib = {r["name"] for r in codemap.discover_repos(home, extra=[outside])}
    check("sibling_project" in sib, "a seed's siblings are picked up — code is kept together")
    check("just_a_folder" not in sib, "...but a plain directory beside it is not a project")

    # A file inside a repo seeds the repo, not the file's directory.
    deep = os.path.join(outside, "sub", "dir")
    os.makedirs(deep, exist_ok=True)
    got = codemap.discover_repos(home, extra=[deep])
    check(any(r["root"] == outside for r in got),
          "a seed anywhere inside a repo resolves to the repo root")

    # The seeds have to SURVIVE a restart. The web server is spawned with no cwd of its own, so a
    # shortcut launch lands it somewhere arbitrary and the in-memory run list is empty — leaving the
    # saved sessions as the only record of where this user keeps code. If recent() drops the cwd
    # field there is nothing to seed with and the map goes back to being empty.
    from harness import sessions
    rec = sessions.recent(5)
    check(all(isinstance(s, dict) and "cwd" in s for s in rec) if rec else True,
          "sessions.recent() carries cwd — the only seed that outlives a restart")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "repo discovery: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
