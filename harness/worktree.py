"""Isolated working trees, so two runs can touch the same repo without touching each other.

`pack` already isolates: it copies the tree to a temp dir, minus `.git` and the heavy directories.
That is right for best-of-N, where the attempts are disposable and only the winner is copied back.
It is wrong for work you intend to keep, and for the same reason twice: a tree with no `.git` cannot
be diffed, so the result arrives as a pile of changed files with no way to see what changed; and
there is no branch, so two results cannot be told apart, reviewed in order, or merged.

A git worktree gives both for free. Same history, separate checkout, its own branch — which is what
every other agent runner converged on this year, and what makes a finished run reviewable as a
branch instead of as an assertion.

What this is careful about:

- **Not every directory is a repo.** Falling back to the copy-based isolation would silently produce
  something unreviewable; falling back to the shared tree would silently let two runs collide. So
  `prepare` says which it got, and the caller decides.
- **A worktree with work in it is never removed.** `release` refuses while anything is uncommitted or
  the branch holds commits the main tree does not — the whole point was to keep the result.
- **The branch name comes from the session**, so an abandoned worktree can be traced back to the
  conversation that made it rather than being one of six `collie-tmp-*`.
"""
import os
import re
import shutil
import subprocess
import tempfile

PREFIX = "collie/"                       # branch namespace, so `git branch --list 'collie/*'` finds them


def _git(args, cwd, timeout=60):
    """Run one git command. Returns (ok, output) — never raises for a non-zero exit."""
    try:
        p = subprocess.run(["git"] + list(args), cwd=cwd, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return p.returncode == 0, (p.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, "%s: %s" % (type(e).__name__, e)


def repo_root(cwd):
    """The repository `cwd` belongs to, or None. A worktree's own root counts."""
    ok, out = _git(["rev-parse", "--show-toplevel"], cwd)
    return out if ok and out else None


def _slug(text, fallback="run"):
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", (text or "").strip().lower()).strip("-.")
    return (s or fallback)[:48]


def prepare(cwd, session, label=""):
    """Make an isolated checkout for one run.

    Returns a dict: {"ok", "dir", "branch", "root", "kind", "error"}.

    `kind` is "worktree" when it is a real git worktree on its own branch, and "none" when the
    directory is not a repository — in which case `dir` is the ORIGINAL cwd and nothing has been
    isolated. Callers must look: quietly handing back the shared tree is how two runs end up editing
    the same file, and quietly handing back a copy is how a result arrives with no diff.
    """
    root = repo_root(cwd)
    if not root:
        return {"ok": False, "dir": cwd, "branch": "", "root": "", "kind": "none",
                "error": "not a git repository — nothing to isolate against"}

    branch = PREFIX + _slug(label or session, fallback=_slug(session))
    # A session that runs twice must not collide with its own leftover branch.
    ok, _ = _git(["rev-parse", "--verify", "--quiet", branch], root)
    if ok:
        n = 2
        while True:
            cand = "%s-%d" % (branch, n)
            hit, _ = _git(["rev-parse", "--verify", "--quiet", cand], root)
            if not hit:
                branch = cand
                break
            n += 1

    dst = os.path.join(tempfile.mkdtemp(prefix="collie_wt_"), _slug(session))
    ok, out = _git(["worktree", "add", "-b", branch, dst, "HEAD"], root, timeout=180)
    if not ok:
        shutil.rmtree(os.path.dirname(dst), ignore_errors=True)
        return {"ok": False, "dir": cwd, "branch": "", "root": root, "kind": "none",
                "error": ("git worktree add failed: " + out)[:400]}
    return {"ok": True, "dir": dst, "branch": branch, "root": root, "kind": "worktree", "error": ""}


def status(wt_dir):
    """What a run left behind: {"dirty", "files", "commits", "branch"}."""
    ok, out = _git(["status", "--porcelain"], wt_dir)
    # NOT line[3:]. _git strips its output, which eats porcelain's leading status column, and the
    # slice then took a character off every filename — " M a.txt" was reported as ".txt". Splitting
    # on whitespace survives that and the rename form ("R  old -> new") alike.
    files = []
    if ok:
        for line in out.splitlines():
            part = line.strip().split(None, 1)
            if len(part) == 2:
                files.append(part[1].split(" -> ")[-1])

    _, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], wt_dir)

    # Commits that exist ONLY here. Counting "ahead of main" called a freshly created worktree one
    # commit ahead of itself, so release() refused to clean up a tree in which nothing had happened.
    others = []
    ok2, refs = _git(["for-each-ref", "--format=%(refname)", "refs/heads/"], wt_dir)
    if ok2:
        here = "refs/heads/" + branch
        others = [r for r in refs.splitlines() if r and r != here and PREFIX not in r]
    commits = 0
    if others:
        ok3, ahead = _git(["rev-list", "--count", "HEAD", "--not"] + others, wt_dir)
        if ok3 and ahead.strip().isdigit():
            commits = int(ahead.strip())
    return {"dirty": bool(files), "files": files[:200], "commits": commits, "branch": branch}


def diff(wt_dir, max_bytes=200_000):
    """The run's work as a patch, staged and unstaged, including new files."""
    _git(["add", "-A", "--intent-to-add"], wt_dir)     # so new files appear in the diff
    ok, out = _git(["diff", "HEAD"], wt_dir, timeout=120)
    if not ok:
        return ""
    return out[:max_bytes]


def release(wt_dir, force=False):
    """Remove a worktree — refusing, unless forced, while it still holds work.

    The reason to isolate was to keep the result. A sweep that removes a tree with uncommitted edits
    in it destroys exactly the thing the run was for, and does it quietly.
    """
    if not wt_dir or not os.path.isdir(wt_dir):
        return {"ok": True, "removed": False, "error": ""}
    st = status(wt_dir)
    if not force and (st["dirty"] or st["commits"]):
        return {"ok": False, "removed": False,
                "error": "worktree still holds work (%d changed file%s, %d commit%s)"
                         % (len(st["files"]), "" if len(st["files"]) == 1 else "s",
                            st["commits"], "" if st["commits"] == 1 else "s")}
    root = repo_root(wt_dir) or wt_dir
    args = ["worktree", "remove", wt_dir] + (["--force"] if force else [])
    ok, out = _git(args, root, timeout=120)
    if ok:
        shutil.rmtree(os.path.dirname(wt_dir), ignore_errors=True)
    return {"ok": ok, "removed": ok, "error": "" if ok else out[:300]}


def listing(cwd):
    """Every collie worktree of this repo, with what each one is holding."""
    root = repo_root(cwd)
    if not root:
        return []
    ok, out = _git(["worktree", "list", "--porcelain"], root)
    if not ok:
        return []
    trees, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur.get("dir") and cur.get("branch", "").startswith("refs/heads/" + PREFIX):
                st = status(cur["dir"])
                trees.append({"dir": cur["dir"], "branch": st["branch"],
                              "dirty": st["dirty"], "files": len(st["files"]),
                              "commits": st["commits"]})
            cur = {}
            continue
        if line.startswith("worktree "):
            cur["dir"] = line[len("worktree "):]
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):]
    return trees
