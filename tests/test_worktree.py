"""Isolation you can review, and a sweep that will not eat your work.

`pack` isolates by copying the tree minus `.git`, which is right for disposable best-of-N attempts
and wrong for anything you mean to keep: no `.git` means no diff, so the result arrives as a pile of
changed files with nothing to read; and no branch means two results cannot be told apart or merged.

A git worktree gives both. The two things worth testing are the two that are easy to get wrong: a
directory that is not a repository must be REPORTED, not silently handed back as if it had been
isolated, and a worktree holding uncommitted work must never be swept away — that work is the entire
reason the run happened.

    python3 tests/test_worktree.py
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def _sh(args, cwd):
    return subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _repo():
    d = tempfile.mkdtemp(prefix="collie_wt_test_")
    _sh(["git", "init", "-q", "-b", "main"], d)
    _sh(["git", "config", "user.email", "t@example.com"], d)
    _sh(["git", "config", "user.name", "T"], d)
    with open(os.path.join(d, "a.txt"), "w") as fh:
        fh.write("one\n")
    _sh(["git", "add", "-A"], d)
    _sh(["git", "commit", "-qm", "first"], d)
    return d


def main():
    from harness import worktree as wt

    plain = tempfile.mkdtemp(prefix="collie_plain_")
    got = wt.prepare(plain, "s-1")
    check(not got["ok"] and got["kind"] == "none",
          "a directory that is not a repo is reported, not silently 'isolated'")
    check(got["dir"] == plain, "and the caller is handed back exactly what it passed in")
    check("not a git repository" in got["error"], "with a reason (%r)" % got["error"][:40])

    repo = _repo()
    a = wt.prepare(repo, "sess-alpha", label="fix the parser")
    check(a["ok"] and a["kind"] == "worktree", "a repo gets a real worktree (%s)" % a["error"][:60])
    check(os.path.isdir(a["dir"]) and a["dir"] != repo, "on its own directory")
    check(a["branch"].startswith("collie/"), "on its own namespaced branch (%s)" % a["branch"])
    check("fix-the-parser" in a["branch"], "named after the work, not a random id")
    check(os.path.exists(os.path.join(a["dir"], "a.txt")), "with the repo's files in it")

    b = wt.prepare(repo, "sess-alpha", label="fix the parser")
    check(b["ok"] and b["branch"] != a["branch"],
          "the same session twice does not collide with its own leftover branch (%s)" % b["branch"])

    # Two runs, same repo, no collision — the point of the exercise.
    with open(os.path.join(a["dir"], "a.txt"), "w") as fh:
        fh.write("changed by A\n")
    with open(os.path.join(b["dir"], "a.txt"), "w") as fh:
        fh.write("changed by B\n")
    check(open(os.path.join(a["dir"], "a.txt")).read() == "changed by A\n",
          "one run's edit does not reach the other's tree")
    check(open(os.path.join(repo, "a.txt")).read() == "one\n",
          "and neither reaches the tree you are working in")

    st = wt.status(a["dir"])
    check(st["dirty"] and "a.txt" in st["files"], "status reports what the run changed")
    patch = wt.diff(a["dir"])
    check("changed by A" in patch and "a.txt" in patch,
          "and the work comes back as a readable diff, which the copy-based isolation could not give")

    r = wt.release(a["dir"])
    check(not r["ok"] and not r["removed"],
          "a worktree holding uncommitted work is NOT swept away")
    check("still holds work" in r["error"], "and says why (%r)" % r["error"][:60])

    trees = wt.listing(repo)
    check(len(trees) >= 2, "collie's worktrees are listable (%d)" % len(trees))
    check(any(t["dirty"] for t in trees), "with which of them are holding changes")

    _sh(["git", "checkout", "--", "."], b["dir"])
    r2 = wt.release(b["dir"])
    check(r2["ok"] and r2["removed"], "a clean one is removed (%s)" % r2["error"][:60])
    check(not os.path.isdir(b["dir"]), "and is really gone")

    r3 = wt.release(a["dir"], force=True)
    check(r3["removed"], "force removes one that still has work, when explicitly asked")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "worktree: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
