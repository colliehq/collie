# -*- coding: utf-8 -*-
"""Checkpoints: snapshot the working tree before the agent edits it, and rewind on demand.

Every competing agent UI ships this (Cursor's Restore Checkpoint, Cline's checkpoints) and
Collie did not. It is the one missing feature that can actually hurt someone: without it, a bad
run is undone by hand.

MECHANISM (modelled on Cline's, whose source was read rather than guessed at):

  * `git stash create` builds a snapshot commit WITHOUT touching the working tree, the index,
    or any ref. That is the whole trick — an agent that had to `git stash push` would be
    mutating the user's tree just to record it.
  * Untracked files are captured separately: list them with `ls-files --others
    --exclude-standard -z`, stage them into a THROWAWAY index file (GIT_INDEX_FILE in a temp
    dir, so the user's index is never touched), `write-tree`, `commit-tree`. That commit is
    attached as a THIRD PARENT of the snapshot, which keeps the object `git stash apply`-
    compatible while carrying files a plain stash would lose.
  * Paths are fed via `--pathspec-from-file` with NUL separators. A repo with thousands of
    untracked files would otherwise overflow the command-line limit — and it would do so
    exactly when the snapshot matters most.
  * The snapshot is stored under `refs/collie/checkpoints/<session>/<n>`. A private namespace
    keeps the object alive against GC without putting anything in the user's `git stash list`.

HONESTY: a checkpoint that silently did not save is worse than none, because the user relies on
it before letting the agent run. `capture()` returns a Checkpoint or raises — it never returns a
handle that will not restore. Outside a git work tree, `available()` says so and the caller must
surface that, not pretend the run is protected.

RESTORE IS DESTRUCTIVE. It resets the tree and deletes files created since the checkpoint. It is
only ever called on explicit user action, and reports exactly what it did.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

_MARKER = "collie-checkpoint:"          # identifies OUR snapshot commits, never a real merge
_REF_NS = "refs/collie/checkpoints"


class CheckpointError(RuntimeError):
    """Raised when a checkpoint cannot be taken or restored. Never swallowed by the caller —
    the point of a checkpoint is that its absence is known before the agent starts editing."""


@dataclass
class Checkpoint:
    ref: str                              # sha of the snapshot commit
    session: str
    n: int
    created_at: float = field(default_factory=time.time)
    kind: str = "stash"                   # "stash" (snapshot) or "commit" (clean-tree fallback)
    label: str = ""

    def as_dict(self) -> dict:
        return {"ref": self.ref, "session": self.session, "n": self.n, "kind": self.kind,
                "created_at": self.created_at, "label": self.label}


def _git(cwd: str, args, env=None, check=True, timeout=120) -> str:
    from . import plat
    p = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True,
                       env=env, timeout=timeout, **plat.no_window_kwargs())
    if check and p.returncode != 0:
        raise CheckpointError("git %s failed (%d): %s"
                              % (" ".join(args[:2]), p.returncode, (p.stderr or "").strip()[:300]))
    return (p.stdout or "").strip()


def available(cwd: str) -> tuple:
    """(ok, reason). Checkpoints need a git work tree; say plainly when there isn't one."""
    try:
        inside = _git(cwd, ["rev-parse", "--is-inside-work-tree"], check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return False, "git is not usable here: %s" % e
    if inside != "true":
        return False, "%s is not inside a git repository, so there is nothing to rewind to" % cwd
    try:
        _git(cwd, ["rev-parse", "HEAD"])
    except CheckpointError:
        return False, "this repository has no commits yet — make one commit and checkpoints work"
    return True, ""


def _untracked_parent(cwd: str) -> str:
    """Commit holding the untracked files, or "" when there are none.

    Built in a temp index so the user's staged changes are untouched — staging into the real
    index to take a snapshot would corrupt whatever they had staged.
    """
    from . import plat as _plat
    listing = subprocess.run(["git", "-C", cwd, "ls-files", "--others", "--exclude-standard", "-z"],
                             **_plat.no_window_kwargs(),
                             capture_output=True, text=True, timeout=300)
    files = [f for f in (listing.stdout or "").split("\0") if f]
    # NOTE the empty case still produces a commit, holding an EMPTY tree. "There were no untracked
    # files" is complete knowledge, not missing knowledge: it means every untracked file present at
    # restore time appeared afterwards and is safe to remove. Cline stops at a HEAD-only fallback
    # here and consequently leaves agent-created files behind on a clean tree — found by restoring
    # for real and watching new.txt survive, which is precisely the "undo" a user expects to work.
    tmp = tempfile.mkdtemp(prefix="collie-ckpt-")
    try:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, "index"))
        if files:
            spec = os.path.join(tmp, "pathspec")
            with open(spec, "wb") as f:                   # NUL-delimited: no argv length limit
                f.write(("\0".join(files) + "\0").encode("utf-8"))
            _git(cwd, ["add", "--force", "--pathspec-from-file", spec, "--pathspec-file-nul"],
                 env=env)
        tree = _git(cwd, ["write-tree"], env=env)         # empty tree when there were none
        if not tree:
            return ""
        return _git(cwd, ["commit-tree", tree, "-m", _MARKER + "untracked"], env=env)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def capture(cwd: str, session: str, n: int, label: str = "") -> Checkpoint:
    """Snapshot the tree. Raises CheckpointError rather than returning a handle that won't work."""
    ok, why = available(cwd)
    if not ok:
        raise CheckpointError(why)
    # The commit message carries NO user text. These refs live under refs/, so `git log --all`
    # and `git for-each-ref` list them — putting the prompt in the subject wrote what the user
    # asked Collie straight into their repository, where a later `git log --all` shows it to
    # whoever is looking. Real runs had already committed subjects like "What did we decide about
    # the embedding memory design?" and a base64 image payload. `label` is kept in memory for this
    # process and shown in the UI, but it is never written to git.
    msg = "%s%s run=%d" % (_MARKER, session, n)
    stash = _git(cwd, ["stash", "create", msg], check=False)
    untracked = _untracked_parent(cwd)

    if stash:
        if not untracked:
            ref, kind = stash, "stash"
        else:
            tree = _git(cwd, ["rev-parse", stash + "^{tree}"])
            base = _git(cwd, ["rev-parse", stash + "^1"])
            index_parent = _git(cwd, ["rev-parse", stash + "^2"])
            ref = _git(cwd, ["commit-tree", tree, "-p", base, "-p", index_parent,
                             "-p", untracked, "-m", msg]) or stash
            kind = "stash"
    elif untracked:
        # Tracked tree clean. Still synthesize a snapshot: the untracked parent records the exact
        # set present now (possibly none), which is what lets restore delete whatever appears later.
        head = _git(cwd, ["rev-parse", "HEAD"])
        head_tree = _git(cwd, ["rev-parse", "HEAD^{tree}"])
        index_parent = _git(cwd, ["commit-tree", head_tree, "-p", head, "-m", _MARKER + "index"])
        ref = _git(cwd, ["commit-tree", head_tree, "-p", head, "-p", index_parent,
                         "-p", untracked, "-m", msg])
        kind = "stash"
    else:
        ref, kind = _git(cwd, ["rev-parse", "HEAD"]), "commit"   # nothing to snapshot

    _git(cwd, ["update-ref", "%s/%s/%d" % (_REF_NS, session, n), ref])
    prune(cwd, keep=int(os.environ.get("COLLIE_CHECKPOINT_KEEP", "50") or 50))
    return Checkpoint(ref=ref, session=session, n=n, kind=kind, label=label)


def prune(cwd: str, keep: int = 50) -> int:
    """Delete all but the `keep` newest snapshots. Returns how many were removed.

    Snapshots are refs, so they never expire on their own: one per run means an everyday user
    accumulates thousands, each pinning a tree against gc. Old ones are also the least useful —
    nobody rewinds to run 12 of last month. Deleting the ref only unpins the objects; a normal
    `git gc` reclaims them later, and nothing the user created is touched.
    """
    if keep <= 0:
        return 0
    items = history(cwd)
    doomed = items[keep:]
    for c in doomed:
        _git(cwd, ["update-ref", "-d", "%s/%s/%d" % (_REF_NS, c.session, c.n)], check=False)
    return len(doomed)


def _kind_of(cwd: str, ref: str) -> str:
    """A snapshot must have BOTH the merge shape and our marker — otherwise an ordinary merge
    commit could be handed to `git stash apply`, which would corrupt the tree."""
    parents = _git(cwd, ["rev-list", "--parents", "-n", "1", ref], check=False).split()[1:]
    subject = _git(cwd, ["log", "-1", "--format=%s", ref], check=False)
    return "stash" if len(parents) >= 2 and subject.startswith(_MARKER) else "commit"


def restore(cwd: str, cp: Checkpoint) -> dict:
    """Rewind the tree to `cp`. DESTRUCTIVE — only on explicit user action.

    Returns what it actually did, so the UI can tell the user whether untracked files were
    rewound (only snapshots carrying the third parent can do that; a clean-tree fallback cannot,
    and claiming otherwise would be the lie this module exists to avoid).
    """
    ok, why = available(cwd)
    if not ok:
        raise CheckpointError(why)
    if _git(cwd, ["cat-file", "-t", cp.ref], check=False) != "commit":
        raise CheckpointError("checkpoint %s is gone from this repository" % cp.ref[:12])
    kind = _kind_of(cwd, cp.ref)
    base = cp.ref if kind == "commit" else cp.ref + "^1"
    _git(cwd, ["cat-file", "-e", base + "^{commit}"])
    had_untracked = _git(cwd, ["cat-file", "-t", cp.ref + "^3"], check=False) == "commit"

    _git(cwd, ["reset", "--hard", base])
    if had_untracked:
        # Only clear untracked when the snapshot can put them back. `clean -fd` (no -x) leaves
        # .gitignored paths — node_modules, build output, .env — alone on purpose.
        _git(cwd, ["clean", "-fd"], check=False)
    if kind == "stash":
        _git(cwd, ["stash", "apply", "--index", cp.ref], check=False) or \
            _git(cwd, ["stash", "apply", cp.ref], check=False)
    return {"restored_to": cp.ref[:12], "kind": kind, "untracked_rewound": had_untracked}


def history(cwd: str, session: str = "") -> list:
    """Checkpoints still present in this repo, newest first."""
    ns = "%s/%s" % (_REF_NS, session) if session else _REF_NS
    out = _git(cwd, ["for-each-ref", "--format=%(refname) %(objectname) %(subject)", ns],
               check=False)
    items = []
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 2:
            continue
        name, sha = parts[0], parts[1]
        tail = name[len(_REF_NS) + 1:].split("/")
        if len(tail) != 2 or not tail[1].isdigit():
            continue
        # label deliberately NOT read back from the commit subject: subjects carry no user text
        # any more, and older snapshots may still hold some — surfacing it would undo the fix.
        items.append(Checkpoint(ref=sha, session=tail[0], n=int(tail[1])))
    return sorted(items, key=lambda c: c.n, reverse=True)
