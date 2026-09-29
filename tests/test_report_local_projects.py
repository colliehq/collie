"""Projects on this computer: throwaway git repositories, scanned for what is worth a line.

A disk full of clones is not a list of projects.  Checkouts that share a remote are one project,
and only the most recently active one speaks; a project counts only when the person worked on it
lately (their own commits, or their edits); and what it says is status, never a to-do.  Only
three facts are ever said: changes uncommitted for more than a week, commits unpushed for more
than a day, and a branch far behind its upstream.  Every repository here is made under
``tmp_path``, and each one's git identity is "T <t@example.com>", the person.
"""
import os
import shutil
import subprocess
import sys
import time

import pytest

from harness import report_signals as rs
from harness import report_sources as src

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git is not installed")

DAY = 86400.0


PERSON = ("T", "t@example.com")
BOT = ("Collie Checkpoint", "checkpoint@collie.local")


def git(cwd, *args, when=None, who=PERSON):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "@%d +0000" % int(when)
    out = subprocess.run(["git", "-c", "user.name=" + who[0], "-c", "user.email=" + who[1],
                          "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]
                         + list(args), cwd=str(cwd), env=env, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def make_repo(path, *, remote=None, when=None, who=PERSON):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", PERSON[0])
    git(path, "config", "user.email", PERSON[1])
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "first", when=when, who=who)
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


def test_old_uncommitted_changes_are_a_status_fact_named_by_the_github_project(tmp_path):
    repo = make_repo(tmp_path / "root" / "alpha", remote="git@github.com:Owner/Alpha.git")
    for name in ("a.txt", "b.txt"):
        (repo / name).write_text("x", encoding="utf-8")
        age(repo / name, 10 * DAY)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    age(repo / "README.md", 9 * DAY)
    out = src.local_projects(context([tmp_path / "root"]))
    stale = [s for s in out["signals"] if s["title"].endswith("on main")]
    assert len(stale) == 1 and stale[0]["kind"] == "fyi"      # status, not a to-do
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
    bare = tmp_path / "beta.git"
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
    assert sig["kind"] == "fyi" and sig["project"] == "beta"     # a bare repo on disk: its name


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


# ---------------------------------------------------------------- which projects are active


def commit(repo, name, *, when, who=PERSON):
    (repo / name).write_text(name, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", name, when=when, who=who)


def old_change(repo, name, days):
    (repo / name).write_text("draft", encoding="utf-8")
    age(repo / name, days * DAY)


def test_clones_of_one_remote_are_one_project_told_by_the_freshest_checkout(tmp_path):
    url = "https://github.com/Owner/Proj.git"
    older = make_repo(tmp_path / "root" / "proj", remote=url, when=time.time() - 20 * DAY)
    old_change(older, "a.txt", 10)
    newer = make_repo(tmp_path / "root" / "proj-v2", remote=url, when=time.time() - 2 * DAY)
    old_change(newer, "b.txt", 8)
    out = src.local_projects(context([tmp_path / "root"]))
    assert out["signals"] and all(str(newer) in s["detail"] for s in out["signals"])
    assert {s["project"] for s in out["signals"]} == {"Owner/Proj"}
    assert list(out["activity"]) == ["Owner/Proj"]
    assert abs(out["activity"]["Owner/Proj"] - (time.time() - 2 * DAY)) < 120
    assert out["stats"] == dict(out["stats"], repos=2, projects=1)


def test_a_repository_without_a_remote_needs_the_persons_commit_in_the_last_two_weeks(tmp_path):
    root = tmp_path / "root"
    quiet = make_repo(root / "quiet", when=time.time() - 20 * DAY)
    old_change(quiet, "x.txt", 10)
    machine = make_repo(root / "machine", when=time.time() - 2 * DAY, who=BOT)
    old_change(machine, "x.txt", 9)
    mine = make_repo(root / "mine", when=time.time() - 5 * DAY)
    old_change(mine, "x.txt", 8)
    out = src.local_projects(context([root]))
    assert {s["project"] for s in out["signals"]} == {"mine"}
    assert list(out["activity"]) == ["mine"]


def test_a_project_with_a_remote_is_active_by_recent_commits_or_recent_edits(tmp_path):
    root = tmp_path / "root"
    edited = make_repo(root / "edited", remote="https://github.com/o/edited.git",
                       when=time.time() - 60 * DAY, who=BOT)
    old_change(edited, "x.txt", 10)                          # the person's edit, 10 days ago
    idle = make_repo(root / "idle", remote="https://github.com/o/idle.git",
                     when=time.time() - 60 * DAY, who=BOT)
    old_change(idle, "x.txt", 20)
    committed = make_repo(root / "committed", remote="https://github.com/o/committed.git",
                          when=time.time() - 25 * DAY)
    old_change(committed, "x.txt", 20)
    out = src.local_projects(context([root]))
    assert sorted(out["activity"]) == ["o/committed", "o/edited"]
    assert {s["project"] for s in out["signals"]} == {"o/committed", "o/edited"}


def test_a_machine_made_copy_is_filed_under_the_project_it_was_copied_from(tmp_path):
    root = tmp_path / "root"
    main = make_repo(root / "main", remote="https://github.com/Owner/Main.git")
    copy = tmp_path / "root" / "experiment-2026-09-13"
    git(tmp_path, "clone", "-q", str(main), str(copy))
    git(copy, "config", "user.name", PERSON[0])
    git(copy, "config", "user.email", PERSON[1])
    git(copy, "checkout", "-q", "-b", "feat/overnight")
    commit(copy, "agent.txt", when=time.time() - 2 * DAY, who=BOT)
    old_change(copy, "left-over.txt", 9)
    second = tmp_path / "root" / "experiment-2026-09-14"
    git(tmp_path, "clone", "-q", str(copy), str(second))    # a copy of the copy
    out = src.local_projects(context([root]))
    assert list(out["activity"]) == ["Owner/Main"]
    assert out["signals"] == []              # the main checkout speaks, and it has nothing to say


def test_a_checkout_with_two_remotes_reports_both_names_as_one_project(tmp_path):
    repo = make_repo(tmp_path / "root" / "collie", remote="https://github.com/me/collie.git")
    git(repo, "remote", "add", "upstream", "git@github.com:CollieHQ/collie.git")
    out = src.local_projects(context([tmp_path / "root"]))
    assert out["aliases"] == [["CollieHQ/collie", "me/collie"]]
    assert list(out["activity"]) == ["me/collie"]


def test_active_projects_come_back_most_recent_first(tmp_path):
    root = tmp_path / "root"
    for name, days in (("b", 3), ("a", 1), ("c", 9)):
        repo = make_repo(root / name, remote="https://github.com/o/%s.git" % name,
                         when=time.time() - days * DAY)
        old_change(repo, "x.txt", 8)
    out = src.local_projects(context([root]))
    assert list(out["activity"]) == ["o/a", "o/b", "o/c"]
    assert list(dict.fromkeys(s["project"] for s in out["signals"])) == ["o/a", "o/b", "o/c"]
    assert all(s["kind"] == "fyi" for s in out["signals"])


# ---------------------------------------------------------------- security: a scanned repo runs nothing


def test_the_scan_runs_git_only_through_the_hardened_runner(tmp_path, monkeypatch):
    """Every git the scan runs carries the neutralizing -c flags, the hardened env, and blanks
    the filter drivers the repository defines.  No git is really run: subprocess is a spy."""
    repo = make_repo(tmp_path / "root" / "repo")
    git(repo, "config", "filter.lfs.clean", "git-lfs clean -- %f")
    git(repo, "config", "filter.evil.process", "run-me")
    seen = []
    real = src.subprocess.run

    def spy(argv, **kwargs):
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        if "--get-regexp" in argv:                 # the read that lists filter drivers
            return real(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_SSH_COMMAND", "run-me")
    monkeypatch.setattr(src.subprocess, "run", spy)
    src.local_projects(context([tmp_path / "root"]))
    scan = [(argv, env) for argv, env in seen if "-C" in argv and str(repo) in argv]
    assert scan
    for argv, env in scan:
        joined = " ".join(argv)
        for flag in ("core.fsmonitor=false", "core.hooksPath=", "core.pager=cat",
                     "diff.external=", "protocol.allow=never", "log.showSignature=false",
                     "gpg.program=", "safe.directory="):
            assert "-c " + flag in joined, (flag, argv)
        assert env.get("GIT_CONFIG_NOSYSTEM") == "1" and env.get("GIT_TERMINAL_PROMPT") == "0"
        assert "GIT_DIR" not in env and "GIT_SSH_COMMAND" not in env
    status = " ".join(next(argv for argv, _ in scan if "status" in argv))
    assert "-c filter.lfs.clean=" in status and "-c filter.evil.process=" in status


def test_a_config_command_in_a_scanned_repo_is_never_executed(tmp_path):
    """A downloaded repo with core.fsmonitor and a clean filter set to a sentinel command: a
    real scan must not run either.  The fixture commits everything BEFORE the config is set, so
    building it never triggers the very hooks under test."""
    root = tmp_path / "root"
    repo = make_repo(root / "downloaded")          # init + add + commit, no malicious config yet
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (repo / "work.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", ".gitattributes", "work.txt")
    git(repo, "commit", "-q", "-m", "tracked files")
    ran = tmp_path / "ran"
    cmd = '%s -c "open(r\'%s\',\'w\').close()"' % (sys.executable.replace("\\", "/"), str(ran))
    git(repo, "config", "core.fsmonitor", cmd)     # only now, so no fixture step runs it
    git(repo, "config", "filter.evil.clean", cmd)
    time.sleep(1.1)
    (repo / "work.txt").write_text("two\n", encoding="utf-8")   # stat-dirty: status rehashes it
    out = src.local_projects(context([root]))
    assert out["stats"]["repos"] == 1
    assert not ran.exists()


def test_a_repository_git_reports_as_not_owned_is_skipped_not_failed(tmp_path):
    root = tmp_path / "root"
    make_repo(root / "odd")
    real = src._git_runner(rs.Context(now=time.time()))

    def runner(args, timeout):
        if "status" in args:
            return 128, "", "fatal: detected dubious ownership in repository at '%s'" % (root / "odd")
        return real(args, timeout)

    out = src.local_projects(context([root], git=runner))
    assert out["state"] == "ok" and out["stats"]["not_owned"] == 1 and out["signals"] == []
