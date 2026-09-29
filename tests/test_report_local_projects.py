"""Projects on this computer: throwaway git repositories, scanned for what is worth a line.

Only three things about a local repository are ever said: changes that have sat uncommitted for
more than a week, commits that have sat unpushed for more than a day, and a branch far behind its
upstream.  A project someone is touching every day produces nothing.  Every repository here is
made under ``tmp_path``; none of the developer's own repositories is read.
"""
import os
import shutil
import subprocess
import time

import pytest

from harness import report_signals as rs
from harness import report_sources as src

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")

DAY = 86400.0


def git(cwd, *args, when=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "@%d +0000" % int(when)
    out = subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com",
                          "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]
                         + list(args), cwd=str(cwd), env=env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def make_repo(path, *, remote=None, when=None):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "first", when=when)
    if remote:
        git(path, "remote", "add", "origin", remote)
    return path


def age(path, seconds_ago):
    stamp = time.time() - seconds_ago
    os.utime(str(path), (stamp, stamp))


def context(roots, **options):
    return rs.Context(now=time.time(), state_dir="", options=dict(options, roots=[str(r) for r in roots]))


def titles(out):
    return {s["title"]: s for s in out["signals"]}


def test_old_uncommitted_changes_are_a_tidy_up_named_by_the_github_project(tmp_path):
    repo = make_repo(tmp_path / "root" / "alpha", remote="git@github.com:Owner/Alpha.git")
    for name in ("a.txt", "b.txt"):
        (repo / name).write_text("x", encoding="utf-8")
        age(repo / name, 10 * DAY)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    age(repo / "README.md", 9 * DAY)
    out = src.local_projects(context([tmp_path / "root"]))
    stale = [s for s in out["signals"] if s["kind"] == "stale"]
    assert len(stale) == 1
    assert stale[0]["project"] == "Owner/Alpha" and stale[0]["title"].startswith("3 uncommitted")
    assert "9 days" in stale[0]["detail"] and stale[0]["untrusted"] is False
    assert stale[0]["link"] == "https://github.com/Owner/Alpha"
    assert out["stats"]["repos"] == 1


def test_changes_made_this_week_say_nothing(tmp_path):
    repo = make_repo(tmp_path / "root" / "busy")
    (repo / "new.txt").write_text("x", encoding="utf-8")
    out = src.local_projects(context([tmp_path / "root"]))
    assert out["signals"] == [] and out["stats"]["repos"] == 1


def test_commits_that_sat_unpushed_for_a_day_need_the_person(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    repo = make_repo(tmp_path / "root" / "beta", when=time.time() - 5 * DAY)
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "main")
    for n in range(3):
        (repo / ("f%d.txt" % n)).write_text("x", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "c%d" % n, when=time.time() - 3 * DAY)
    out = src.local_projects(context([tmp_path / "root"]))
    sig = titles(out)["3 commits not pushed yet on main"]
    assert sig["kind"] == "needs_you" and sig["project"] == "beta"     # no GitHub remote: folder


def test_commits_made_today_are_not_nagged_about(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    repo = make_repo(tmp_path / "root" / "gamma")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "main")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "today")
    assert src.local_projects(context([tmp_path / "root"]))["signals"] == []


def test_a_branch_without_upstream_counts_commits_no_remote_has(tmp_path):
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    repo = make_repo(tmp_path / "root" / "delta", when=time.time() - 4 * DAY)
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "origin", "main")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "g.txt").write_text("x", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "local only", when=time.time() - 2 * DAY)
    out = src.local_projects(context([tmp_path / "root"]))
    assert "1 commit not pushed yet on feature" in titles(out)


def test_a_branch_far_behind_its_upstream_is_mentioned(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "BEHIND_THRESHOLD", 2)
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    repo = make_repo(tmp_path / "root" / "eps")
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "push", "-q", "-u", "origin", "main")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(bare), str(other))
    for n in range(3):
        (other / ("o%d.txt" % n)).write_text("x", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "o%d" % n)
    git(other, "push", "-q", "origin", "HEAD:main")
    git(repo, "fetch", "-q")
    out = src.local_projects(context([tmp_path / "root"]))
    behind = [s for s in out["signals"] if "behind" in s["title"]]
    assert behind and behind[0]["kind"] == "fyi" and "3" in behind[0]["title"]


def test_the_scan_is_two_levels_deep_and_skips_worktrees_and_vendored_code(tmp_path):
    root = tmp_path / "root"
    main = make_repo(root / "main")
    git(main, "worktree", "add", "-q", str(root / "main-wt"), "-b", "wt")
    make_repo(root / "group" / "inner")
    make_repo(root / "group" / "node_modules" / "dep")
    make_repo(root / "a" / "b" / "too-deep")
    make_repo(root / ".hidden" / "x")
    out = src.local_projects(context([root, root]))              # a root named twice counts once
    assert out["stats"]["repos"] == 2                             # main and group/inner


def test_repositories_past_the_cap_make_the_scan_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(src, "MAX_REPOS", 2)
    for name in ("r1", "r2", "r3"):
        make_repo(tmp_path / "root" / name)
    out = src.local_projects(context([tmp_path / "root"]))
    assert out["state"] == "partial" and out["stats"]["repos"] == 2 and "2" in out["reason"]


def test_a_repository_whose_git_call_hangs_costs_only_itself(tmp_path):
    make_repo(tmp_path / "root" / "fine")
    make_repo(tmp_path / "root" / "stuck")
    real = src._git_runner(rs.Context(now=time.time()))

    def runner(args, timeout):
        if any(str(arg).endswith("stuck") for arg in args):
            raise subprocess.TimeoutExpired(["git"], timeout)
        return real(args, timeout)

    out = src.local_projects(context([tmp_path / "root"], git=runner))
    assert out["state"] == "partial" and "1 repositor" in out["reason"]
    assert out["stats"]["repos"] == 2


def test_without_git_the_source_is_unavailable(tmp_path, monkeypatch):
    make_repo(tmp_path / "root" / "x")
    monkeypatch.setattr(src.shutil, "which", lambda name: None)
    _, sources, _ = rs.collect(context([tmp_path / "root"]), [src.adapter("local")])
    assert sources[0]["state"] == "unavailable" and "git" in sources[0]["reason"]


def test_no_project_folder_is_unavailable_not_an_empty_desk(tmp_path):
    _, sources, _ = rs.collect(context([tmp_path / "missing"]), [src.adapter("local")])
    assert sources[0]["state"] == "unavailable" and "folder" in sources[0]["reason"]


def test_the_scan_changes_nothing_in_the_repository(tmp_path):
    repo = make_repo(tmp_path / "root" / "quiet")
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    index = repo / ".git" / "index"
    before = (index.stat().st_mtime_ns, index.read_bytes())
    src.local_projects(context([tmp_path / "root"]))
    assert (index.stat().st_mtime_ns, index.read_bytes()) == before


def test_default_roots_are_the_folders_holding_recent_workspaces(tmp_path):
    home = tmp_path / "home"
    (home / "code").mkdir(parents=True)
    main = make_repo(tmp_path / "projects" / "main")
    git(main, "worktree", "add", "-q", str(tmp_path / "elsewhere" / "wt"), "-b", "wt")
    plain = tmp_path / "scratch" / "notes"
    plain.mkdir(parents=True)
    ctx = rs.Context(now=time.time(), options={
        "workspaces": [str(tmp_path / "elsewhere" / "wt"), str(plain), str(tmp_path / "gone")],
        "home": str(home)})
    roots = [os.path.normcase(os.path.realpath(p)) for p in src.project_roots(ctx)]
    expect = [os.path.normcase(os.path.realpath(str(p))) for p in (
        tmp_path / "projects", tmp_path / "scratch", home / "code")]
    assert roots == expect      # a worktree's main repository decides; missing folders are skipped


def test_a_local_clone_and_its_github_repository_are_one_project(tmp_path):
    repo = make_repo(tmp_path / "root" / "collie", remote="https://github.com/CollieHQ/Collie.git")
    (repo / "old.txt").write_text("x", encoding="utf-8")
    age(repo / "old.txt", 30 * DAY)
    local = src.local_projects(context([tmp_path / "root"]))["signals"]
    remote = [rs.make("github", "release", kind="done", title="colliehq/collie v1 is out",
                      project="colliehq/collie")]
    groups = rs.group_by_project(local + remote)
    assert len(groups) == 1 and len(next(iter(groups.values()))) == 2
