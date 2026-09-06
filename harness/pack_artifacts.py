"""Reviewable, conflict-checked change bundles for Pack winners.

Pack used to have exactly two endings.  ``apply=False`` (the default) deleted every candidate
tree, including the winning edits somebody had just paid N model runs for — the CLI said "use
--apply", which means *run it all again*.  ``apply=True`` mirrored the whole winning tree over the
live workspace, and that tree is a snapshot of the workspace as it looked BEFORE the model started:
anything the user edited, added, or deleted while the candidates ran was silently overwritten or
removed.

This module stores what the winner actually CHANGED, measured against the exact tree that attempt
started from, and replays those changes later against the live workspace — only if every touched
path still looks the way that attempt found it.  Three properties do the work:

* **Bounded.**  A bundle holds the added/replaced file contents and the deletion list, not a second
  copy of the repository.  A pack that changed three files stores three blobs.
* **Baseline-anchored.**  Every change carries the baseline type/digest as well as the target
  type/digest.  Apply compares the live file to the BASELINE, so an unrelated edit elsewhere is
  untouched, a concurrent edit to a touched file is a refusal rather than a silent overwrite, and
  re-applying an already-applied bundle is a no-op instead of a rollback of newer work.
* **Refuse before mutating.**  Every path is validated (no traversal, no excluded tree, no symlink
  or Windows junction in the chain), every blob is verified against its digest, and every conflict
  is collected BEFORE the first byte is written.  Writes are staged, backed up, and rolled back on
  a partial filesystem failure; what could not be rolled back is reported, never swallowed.
* **Never evicts unapplied work.**  Storage is bounded per workspace (count and bytes), but the
  only bundles auto-pruning may delete are the ones whose changes are verifiably present in their
  workspace RIGHT NOW, plus the ones a user deleted on purpose.  When the quota cannot be freed
  that way the new save is refused with the ids that are in the way — Pack then keeps the winning
  attempt directory instead of deleting it.  Pruning a delivered bundle also discards the pre-apply
  backups stored inside it; that is the documented cost of the space it frees.

What this module does NOT promise: atomicity against an arbitrary concurrent editor.  Collie's own
saves, prunes and applies are serialized against each other with cross-process locks, but another
process can still edit a file between our last check and our write.  Every mutation therefore
re-resolves its path and re-checks the live state immediately before touching it, and stops at the
first surprise; rollback is best effort and what it could not restore is named in the result.

Not a general artifact platform: one Pack winner, one workspace, local review and apply.  There is
no model call anywhere in this file — review and apply are pure filesystem work.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid

from . import statelock

SCHEMA = 1

# Trees Pack never isolates, and therefore never owns.  A bundle that names a path inside one of
# them is refused rather than applied: .git in particular is how the user recovers from us.
SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
                       ".pytest_cache", ".collie", "dist", "build", ".tox"})
# Matched case-insensitively everywhere.  On Windows and macOS ".GiT" IS ".git", so an exact-case
# test is a bypass; refusing it on Linux too keeps one bundle from meaning two different things on
# two machines, and costs only a directory nobody names on purpose.
_SKIP_LOWER = frozenset(name.lower() for name in SKIP_DIRS)

_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1024 * 1024
_MAX_JOURNAL = 20            # remembered apply attempts per artifact
_MAX_DIR_SCAN = 2000         # live descendants inspected when a directory becomes a file
# Ceilings for READING a stored manifest.  Deliberately not the configurable Limits: those bound
# what Pack will create, and lowering an env var must not turn already-saved bundles into corrupt
# ones.  These only separate "a manifest" from "a file that cannot be one".
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_CHANGES = 200_000
_MAX_BLOB_BYTES = 1 << 40
_MAX_PATH_CHARS = 1024
_MAX_NAME_CHARS = 255
_MAX_DEPTH = 64
_RESERVED_NT = frozenset(
    {"CON", "PRN", "AUX", "NUL"} |
    {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)})


class PackArtifactError(Exception):
    """Base class for every failure this module reports by exception."""


class ArtifactNotFound(PackArtifactError):
    """No readable artifact with that id under the state root."""


class ArtifactStorageError(PackArtifactError):
    """The bundle could not be written durably (disk full, permissions, corrupt store)."""


class ArtifactLimitError(ArtifactStorageError):
    """The winner is larger than the configured artifact quota."""


class ArtifactQuotaError(ArtifactStorageError):
    """The store is full of bundles that are NOT safe to delete, so the save was refused.

    Distinct from :class:`ArtifactLimitError`: nothing is wrong with this winner.  Older winners
    are still waiting to be reviewed, and deleting one of them to make room would throw away the
    only copy of somebody's edits.  The message names the ids that are in the way.
    """


class UnsafePath(PackArtifactError):
    """A bundle path escapes the workspace or names an excluded tree."""


class Limits:
    """Explicit, overridable ceilings.  Silent truncation would make a bundle a lie."""

    __slots__ = ("max_files", "max_file_bytes", "max_total_bytes", "max_scan_files", "keep",
                 "max_store_bytes")

    def __init__(self, max_files=5000, max_file_mb=64, max_total_mb=512,
                 max_scan_files=200_000, keep=20, max_store_mb=2048):
        self.max_files = max(1, int(max_files))
        self.max_file_bytes = max(1, int(max_file_mb)) * 1024 * 1024
        self.max_total_bytes = max(1, int(max_total_mb)) * 1024 * 1024
        self.max_scan_files = max(1, int(max_scan_files))
        # keep >= 1 is a hard floor: pruning must never be able to evict the only copy.
        self.keep = max(1, int(keep))
        # Bytes one workspace may occupy in the store, counting blobs AND the backups an apply
        # left behind.  Admission is checked against this before a single blob is written.
        self.max_store_bytes = max(1, int(max_store_mb)) * 1024 * 1024

    @classmethod
    def from_env(cls):
        def _int(name, default):
            try:
                value = int(os.environ.get(name, "") or default)
            except (TypeError, ValueError):
                raise ValueError("%s must be an integer" % name)
            return value
        return cls(max_files=_int("COLLIE_PACK_ARTIFACT_MAX_FILES", 5000),
                   max_file_mb=_int("COLLIE_PACK_ARTIFACT_MAX_FILE_MB", 64),
                   max_total_mb=_int("COLLIE_PACK_ARTIFACT_MAX_TOTAL_MB", 512),
                   max_scan_files=_int("COLLIE_PACK_ARTIFACT_MAX_SCAN_FILES", 200_000),
                   keep=_int("COLLIE_PACK_ARTIFACT_KEEP", 20),
                   max_store_mb=_int("COLLIE_PACK_ARTIFACT_STORE_MB", 2048))

    def to_dict(self):
        return {"max_files": self.max_files, "max_file_bytes": self.max_file_bytes,
                "max_total_bytes": self.max_total_bytes, "keep": self.keep,
                "max_store_bytes": self.max_store_bytes}


def enabled():
    """Artifact persistence is on unless explicitly disabled (``COLLIE_PACK_ARTIFACTS=0``)."""
    return (os.environ.get("COLLIE_PACK_ARTIFACTS", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off")


def artifacts_root(root=None):
    """Durable store: ``<COLLIE_STATE_DIR>/pack_artifacts`` unless overridden.

    ``root`` is a STATE root (the same thing COLLIE_STATE_DIR names), so a caller that already
    resolved Collie's state directory does not have to know this module's layout.
    """
    if root:
        return os.path.abspath(os.path.join(str(root), "pack_artifacts"))
    direct = os.environ.get("COLLIE_PACK_ARTIFACT_DIR")
    if direct:
        return os.path.abspath(direct)
    state = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
    return os.path.abspath(os.path.join(state, "pack_artifacts"))


def _os_error(action, path, exc):
    """Actionable, content-free error text.  Windows needs the path; nobody needs the bytes."""
    detail = getattr(exc, "strerror", "") or str(exc)
    winerr = getattr(exc, "winerror", None)
    if winerr:
        detail = "[WinError %s] %s" % (winerr, detail)
    return "%s %s: %s" % (action, path, detail)


def _now():
    return time.time()


def _stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _text(value, limit=400):
    return str("" if value is None else value).replace("\x00", "")[:limit]


# --------------------------------------------------------------------------- paths

def check_relpath(rel):
    """Validate one bundle path.  Returns the normalized POSIX path or raises ``UnsafePath``.

    This is the only place a stored path becomes a filesystem path, so it is where traversal
    ("../../.ssh/config"), absolute paths, drive-relative paths ("C:evil"), excluded trees and
    Windows device names are refused — for hand-edited or corrupted bundles as much as for ours.
    """
    if rel is not None and not isinstance(rel, str):
        raise UnsafePath("bundle path is not a string: %s" % _text(type(rel).__name__, 40))
    text = str(rel or "")
    if not text or "\x00" in text:
        raise UnsafePath("bundle path is empty or contains a null byte")
    if len(text) > _MAX_PATH_CHARS:
        raise UnsafePath("bundle path is longer than %d characters" % _MAX_PATH_CHARS)
    unified = text.replace("\\", "/")
    if unified.startswith("/") or os.path.isabs(text):
        raise UnsafePath("bundle path is absolute: %s" % text)
    parts = unified.split("/")
    if len(parts) > _MAX_DEPTH:
        raise UnsafePath("bundle path is deeper than %d components: %s" % (_MAX_DEPTH, text))
    for part in parts:
        if part in ("", ".", ".."):
            raise UnsafePath("bundle path is not a simple relative path: %s" % text)
        if len(part) > _MAX_NAME_CHARS:
            raise UnsafePath("bundle path component is longer than %d characters: %s" % (
                _MAX_NAME_CHARS, text))
        if ":" in part:
            raise UnsafePath("bundle path contains a drive or stream separator: %s" % text)
        if part.lower() in _SKIP_LOWER:
            raise UnsafePath("bundle path is inside an excluded tree: %s" % text)
        if os.name == "nt":
            if part.rstrip(" .") != part:
                raise UnsafePath("bundle path component ends with a space or dot: %s" % text)
            if part.split(".")[0].upper() in _RESERVED_NT:
                raise UnsafePath("bundle path uses a reserved Windows device name: %s" % text)
    return "/".join(parts)


def _link_like(path, st=None):
    """True for POSIX symlinks AND Windows junctions/reparse points.

    ``os.path.islink`` alone has been version- and reparse-tag-dependent on Windows; a junction we
    failed to recognise is a way out of the workspace, so check the attribute bit too.
    """
    try:
        st = st if st is not None else os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) &
                getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _refuse_redirect(path, what):
    """Refuse to read or write through a link inside the store.

    The store's own layout (``<id>/artifact.json``, ``<id>/blobs/...``, ``<id>/backups/...``) is
    written by this module.  If one of those names has become a symlink or junction, something
    else is steering our reads and writes, and the honest answer is "this artifact is corrupt"
    rather than following it.
    """
    if _link_like(path):
        raise PackArtifactError("%s is a symlink or junction; refusing to use it: %s" % (
            what, path))


def resolve_within(workspace, rel):
    """Join ``rel`` under ``workspace`` refusing any link-like component on the way.

    Following a symlink or junction here would let a bundle write outside the workspace even
    though every stored path is relative.  Existing components are checked; missing ones cannot
    redirect anything yet and are created (still checked) at apply time.
    """
    rel = check_relpath(rel)
    base = os.path.abspath(workspace)
    current = base
    for part in rel.split("/"):
        current = os.path.join(current, part)
        if _link_like(current):
            raise UnsafePath("refusing to follow a symlink or junction: %s" % current)
    # Belt and braces: even with every component checked, the joined path must stay under the root.
    if not os.path.normcase(current).startswith(os.path.normcase(base.rstrip(os.sep)) + os.sep):
        raise UnsafePath("bundle path escapes the workspace: %s" % current)
    return current


def workspace_key(path):
    """Stable identity for a workspace root (case-folded real path)."""
    try:
        real = os.path.realpath(os.path.abspath(str(path)))
    except OSError:
        real = os.path.abspath(str(path))
    return os.path.normcase(real.rstrip("\\/") or real)


# --------------------------------------------------------------------------- scanning

def _hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(_CHUNK)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def _file_entry(path, st):
    digest, size = _hash_file(path)
    return {"type": "file", "sha256": digest, "size": size,
            "exec": bool(st.st_mode & 0o111)}


def capture_baseline(root, *, limits=None):
    """Hash every file under ``root`` (excluded trees skipped, links never followed).

    Pack calls this on the attempt's OWN isolated directory, before the model runs.  Taking it
    from the live workspace afterwards is what made the old apply destructive: by then the user's
    concurrent edits are indistinguishable from the candidate's.
    """
    limits = limits or Limits.from_env()
    root = os.path.abspath(str(root))
    if not os.path.isdir(root):
        raise PackArtifactError("baseline root is not a directory: %s" % root)
    files, dirs, unreadable = {}, [], []
    stack, seen = [""], 0
    while stack:
        rel_dir = stack.pop()
        abs_dir = os.path.join(root, rel_dir) if rel_dir else root
        try:
            with os.scandir(abs_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise PackArtifactError(_os_error("cannot read", abs_dir, exc)) from exc
        for entry in entries:
            name = entry.name
            if name.lower() in _SKIP_LOWER:
                continue
            rel = ("%s/%s" % (rel_dir, name)) if rel_dir else name
            seen += 1
            if seen > limits.max_scan_files:
                raise ArtifactLimitError(
                    "workspace has more than %d files; raise COLLIE_PACK_ARTIFACT_MAX_SCAN_FILES "
                    "or set COLLIE_PACK_ARTIFACTS=0" % limits.max_scan_files)
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                unreadable.append({"path": rel, "error": _os_error("cannot stat", rel, exc)})
                continue
            if _link_like(entry.path, st):
                try:
                    target = os.readlink(entry.path)
                except OSError:
                    target = ""
                files[rel] = {"type": "link",
                              "sha256": hashlib.sha256(
                                  target.encode("utf-8", "surrogatepass")).hexdigest()}
            elif stat.S_ISDIR(st.st_mode):
                dirs.append(rel)
                stack.append(rel)
            elif stat.S_ISREG(st.st_mode):
                try:
                    files[rel] = _file_entry(entry.path, st)
                except OSError as exc:
                    unreadable.append({"path": rel, "error": _os_error("cannot read", rel, exc)})
            else:
                files[rel] = {"type": "other"}
    return {"root": root, "at": _now(), "files": files, "dirs": sorted(dirs),
            "unreadable": unreadable}


# --------------------------------------------------------------------------- diff

def _state_of(rel, manifest_files, manifest_dirs):
    entry = manifest_files.get(rel)
    if entry is not None:
        return dict(entry)
    return {"type": "dir"} if rel in manifest_dirs else {"type": "absent"}


class ChangeBundle:
    """The winner's diff against its own baseline — computed, not yet persisted.

    Kept separate from ``save_artifact`` so a pack that changed nothing costs one scan and no
    storage at all, and so a storage failure is reported as exactly that.
    """

    __slots__ = ("workspace", "source_dir", "changes", "unsupported", "metadata",
                 "baseline_at", "unreadable")

    def __init__(self, workspace, source_dir, changes, unsupported, metadata,
                 baseline_at, unreadable):
        self.workspace = workspace
        self.source_dir = source_dir
        self.changes = changes
        self.unsupported = unsupported
        self.metadata = metadata
        self.baseline_at = baseline_at
        self.unreadable = unreadable

    @property
    def empty(self):
        return not self.changes and not self.unsupported

    @property
    def summary(self):
        counts = {"added": 0, "modified": 0, "deleted": 0, "files": len(self.changes),
                  "bytes": 0, "unsupported": len(self.unsupported)}
        for change in self.changes:
            counts[{"add": "added", "modify": "modified",
                    "delete": "deleted"}[change["action"]]] += 1
            counts["bytes"] += int(change.get("target", {}).get("size") or 0)
        return counts

    def to_dict(self):
        return {"workspace": self.workspace, "changes": list(self.changes),
                "unsupported": list(self.unsupported), "summary": self.summary,
                "metadata": dict(self.metadata or {})}


def create_artifact(attempt_dir, baseline, *, workspace, metadata=None, limits=None):
    """Diff a finished attempt against ITS baseline manifest.  Returns a :class:`ChangeBundle`.

    Symlink/junction changes are collected into ``unsupported`` instead of being encoded: a bundle
    Collie cannot replay safely must be refused up front, not half-applied later.
    """
    limits = limits or Limits.from_env()
    if not isinstance(baseline, dict) or "files" not in baseline:
        raise PackArtifactError("baseline manifest is missing or malformed")
    current = capture_baseline(attempt_dir, limits=limits)
    base_files, cur_files = baseline.get("files") or {}, current["files"]
    base_dirs, cur_dirs = set(baseline.get("dirs") or ()), set(current["dirs"])
    changes, unsupported = [], []
    total_bytes = 0
    for rel in sorted(set(base_files) | set(cur_files)):
        before = _state_of(rel, base_files, base_dirs)
        after = _state_of(rel, cur_files, cur_dirs)
        if before == after:
            continue
        try:
            check_relpath(rel)
        except UnsafePath as exc:
            unsupported.append({"path": rel, "reason": str(exc)})
            continue
        if "link" in (before.get("type"), after.get("type")):
            unsupported.append({"path": rel, "reason": "symlink or junction changed"})
            continue
        if "other" in (before.get("type"), after.get("type")):
            unsupported.append({"path": rel, "reason": "special file changed"})
            continue
        if after.get("type") == "file":
            size = int(after.get("size") or 0)
            if size > limits.max_file_bytes:
                raise ArtifactLimitError(
                    "winner file %s is %d bytes; the per-file artifact limit is %d "
                    "(COLLIE_PACK_ARTIFACT_MAX_FILE_MB)" % (rel, size, limits.max_file_bytes))
            total_bytes += size
            action = "modify" if before.get("type") == "file" else "add"
        else:
            action = "delete"
        changes.append({"path": rel, "action": action, "baseline": before, "target": after,
                        "blob": after.get("sha256") if after.get("type") == "file" else None})
        if len(changes) > limits.max_files:
            raise ArtifactLimitError(
                "winner changed more than %d files; raise COLLIE_PACK_ARTIFACT_MAX_FILES or set "
                "COLLIE_PACK_ARTIFACTS=0" % limits.max_files)
        if total_bytes > limits.max_total_bytes:
            raise ArtifactLimitError(
                "winner changes exceed the %d byte artifact limit "
                "(COLLIE_PACK_ARTIFACT_MAX_TOTAL_MB)" % limits.max_total_bytes)
    # Bound the strings, keep scalars typed: a UI should not have to parse "5" back into a turn
    # count, and anything else is stringified rather than smuggled into the manifest.
    meta = {}
    for key, value in dict(metadata or {}).items():
        meta[_text(key, 60)] = (value if isinstance(value, (int, float, bool)) or value is None
                                else _text(value, 2000))
    return ChangeBundle(workspace=os.path.abspath(str(workspace)),
                        source_dir=os.path.abspath(str(attempt_dir)),
                        changes=changes, unsupported=unsupported, metadata=meta,
                        baseline_at=baseline.get("at"),
                        unreadable=list(current.get("unreadable") or ())[:50])


# --------------------------------------------------------------------------- storage

def _write_json(path, data):
    tmp = path + ".tmp-%s" % uuid.uuid4().hex[:8]
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _copy_blob(source, destination, expected_sha):
    """Copy one file into the store, verifying it still hashes to what the diff measured."""
    digest = hashlib.sha256()
    with open(source, "rb") as src, open(destination, "wb") as dst:
        while True:
            block = src.read(_CHUNK)
            if not block:
                break
            digest.update(block)
            dst.write(block)
        dst.flush()
        os.fsync(dst.fileno())
    if digest.hexdigest() != expected_sha:
        raise ArtifactStorageError(
            "candidate file changed while the artifact was being saved: %s" % source)


def _workspace_digest(key):
    return hashlib.sha256(str(key).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _lock_path(store, name):
    """Locks live beside the bundles, never inside one: pruning a bundle must not free a lock."""
    return os.path.join(store, "locks", name)


def _store_lock(store, workspace):
    """Serializes admission + prune + install for one workspace, across processes."""
    return _lock_path(store, "%s.store" % _workspace_digest(workspace_key(workspace)))


def _apply_lock(store, target):
    return _lock_path(store, "%s.apply" % _workspace_digest(workspace_key(target)))


def _artifact_lock(store, artifact_id):
    """Held by apply for as long as it needs its bundle, and by anything that would delete it."""
    return _lock_path(store, "%s.artifact" % artifact_id)


def _dir_bytes(path, cap=100_000):
    """Bytes one artifact occupies on disk: blobs and retained backups.  Links not followed."""
    total, seen, stack = 0, 0, [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > cap:
                return total
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode) and not _link_like(entry.path, st):
                stack.append(entry.path)
            else:
                total += max(0, int(getattr(st, "st_size", 0) or 0))
    return total


def _records_for(store, workspace):
    """Every readable bundle belonging to ``workspace``, newest first.

    Directories we cannot read as a manifest are skipped: they are neither counted against the
    quota nor deleted.  Auto-pruning something we cannot even identify would be exactly the
    guess this module exists to avoid; ``delete_artifact`` remains the explicit remedy.
    """
    key = workspace_key(workspace)
    rows = []
    try:
        names = sorted(os.listdir(store), reverse=True)
    except OSError:
        return rows
    for name in names:
        if not _ID.match(name):
            continue
        try:
            record = inspect_artifact(name, store=store)
        except PackArtifactError:
            continue
        if record.get("workspace_key") == key:
            rows.append(record)
    return rows


def delivery_state(record):
    """Has this bundle already been delivered to its workspace?  Decides what pruning may touch.

    * ``unapplied``   — never applied anywhere.  It may be the only copy of these edits.
    * ``diverged``    — applied once, but the workspace no longer matches it (reverted, edited,
      or applied then rolled back).  An old journal line is NOT proof the work is still there.
    * ``unverifiable``— the workspace is gone or unreadable, so nothing can be checked.
    * ``delivered``   — every change is present in the workspace right now, verified by content.

    Only ``delivered`` is safe to auto-delete, and even then deleting it discards the pre-apply
    backups kept inside the bundle.
    """
    applies = [row for row in (record.get("applies") or ()) if isinstance(row, dict)]
    delivered_to = [row.get("cwd") for row in applies if row.get("status") == "applied"]
    if not delivered_to:
        return "unapplied"
    # Check the tree it was last applied to (usually its own workspace, but an explicit
    # allow_foreign_root apply delivers it somewhere else and that copy counts).
    last = delivered_to[-1]
    workspace = last if isinstance(last, str) and os.path.isdir(last) else ""
    workspace = workspace or str(record.get("workspace") or "")
    if not workspace or not os.path.isdir(workspace):
        return "unverifiable"
    try:
        plan = _plan(record, workspace)
    except (OSError, PackArtifactError):
        return "unverifiable"
    if plan["conflicts"] or plan["steps"]:
        return "diverged"
    return "delivered"


_PRUNE_REASON = {
    "unapplied": "never applied; still the only copy of its edits",
    "diverged": "applied once, but the workspace no longer matches it",
    "unverifiable": "its workspace cannot be read, so delivery cannot be verified",
    "in use": "being applied right now",
    "corrupt": "unreadable manifest; delete it explicitly if you no longer want it",
    "undeletable": "could not be removed from the store",
}


def _prune_delivered(store, artifact_id, *, timeout=5.0):
    """Delete one bundle only while it is verifiably delivered.  Returns ``(deleted, reason)``.

    The delivery check is repeated INSIDE the artifact lock, so a bundle cannot be verified,
    then applied/edited/rolled back by somebody else, and then deleted on the strength of the
    stale verdict.  A bundle an apply is holding is never deleted out from under it.
    """
    try:
        with statelock.transaction(_artifact_lock(store, artifact_id), timeout=timeout):
            try:
                record = inspect_artifact(artifact_id, store=store)
            except ArtifactNotFound:
                return True, ""
            except PackArtifactError:
                return False, "corrupt"
            state = delivery_state(record)
            if state != "delivered":
                return False, state
            try:
                shutil.rmtree(record["path"])
            except FileNotFoundError:
                return True, ""
            except OSError:
                return False, "undeletable"
            return True, ""
    except statelock.StateLockTimeout:
        return False, "in use"


def _over_quota(rows, usage, incoming_bytes, limits):
    used = sum(usage.get(row["id"], 0) for row in rows)
    return (len(rows) + 1 > limits.keep or
            used + incoming_bytes > limits.max_store_bytes)


def _quota_error(workspace, rows, usage, blocked, incoming_bytes, limits):
    used = sum(usage.get(row["id"], 0) for row in rows)
    listed = "; ".join("%s (%s)" % (ident, _PRUNE_REASON.get(reason, reason))
                       for ident, reason in blocked[:5]) or "none could be examined"
    return ArtifactQuotaError(
        "the pack artifact store for %s is full: %d bundle(s), %.1f MB stored, and this winner "
        "needs %.1f MB more (limits: keep=%d bundles, %d MB). No unapplied winner was deleted. The "
        "oldest bundles are not safe to delete: %s%s. Review and apply them, delete the ones you "
        "do not want (delete_artifact), or raise COLLIE_PACK_ARTIFACT_KEEP / "
        "COLLIE_PACK_ARTIFACT_STORE_MB. This winner was NOT saved." % (
            workspace, len(rows), used / 1048576.0, incoming_bytes / 1048576.0,
            limits.keep, limits.max_store_bytes // 1048576, listed,
            "" if len(blocked) <= 5 else " (+%d more)" % (len(blocked) - 5)))


def _admit(store, workspace, incoming_bytes, limits):
    """Make room for one more bundle under this workspace's quota, or refuse the save.

    Eviction is oldest-first and delivered-only.  There is no "just drop the oldest" fallback:
    when every candidate is still the only copy of something, the honest outcome is a refusal
    the caller can act on (Pack keeps the winning attempt directory instead).
    """
    if incoming_bytes > limits.max_store_bytes:
        # Nothing older is in the way: this one winner does not fit on its own.
        raise ArtifactLimitError(
            "this winner needs %.1f MB but one workspace may store %d MB of pack artifacts; "
            "raise COLLIE_PACK_ARTIFACT_STORE_MB or set COLLIE_PACK_ARTIFACTS=0" % (
                incoming_bytes / 1048576.0, limits.max_store_bytes // 1048576))
    rows = _records_for(store, workspace)
    usage = {row["id"]: _dir_bytes(row["path"]) for row in rows}
    order = list(reversed(rows))              # oldest first: the eviction order
    pruned, blocked, index = [], [], 0
    while _over_quota(rows, usage, incoming_bytes, limits):
        victim = None
        while index < len(order):
            row = order[index]
            index += 1
            deleted, reason = _prune_delivered(store, row["id"])
            if deleted:
                victim = row
                break
            blocked.append((row["id"], reason))
        if victim is None:
            raise _quota_error(workspace, rows, usage, blocked, incoming_bytes, limits)
        rows.remove(victim)
        usage.pop(victim["id"], None)
        pruned.append(victim["id"])
    return pruned


def save_artifact(bundle, *, root=None, limits=None, timeout=60.0):
    """Persist a :class:`ChangeBundle` durably and return its record.

    Staged into a private directory and renamed into place, so a crash mid-save leaves a staging
    directory rather than an artifact that claims changes whose contents are missing.

    Admission (quota) and installation happen under one cross-process lock per workspace: two
    packs finishing at the same moment cannot both decide there is room for one more.  Raises
    :class:`ArtifactQuotaError` when the store is full of bundles that are not safe to delete —
    the caller is expected to keep the winning attempt directory rather than pretend it was saved.
    """
    if not isinstance(bundle, ChangeBundle):
        raise PackArtifactError("save_artifact expects a ChangeBundle")
    if bundle.empty:
        raise PackArtifactError("winner changed no files; there is nothing to save")
    limits = limits or Limits.from_env()
    store = artifacts_root(root)
    try:
        os.makedirs(store, exist_ok=True)
        if os.name != "nt":
            try:
                os.chmod(store, 0o700)
            except OSError:
                pass
    except OSError as exc:
        raise ArtifactStorageError(_os_error("cannot create artifact store", store, exc)) from exc
    try:
        with statelock.transaction(_store_lock(store, bundle.workspace), timeout=timeout):
            return _install(bundle, store, limits)
    except statelock.StateLockTimeout as exc:
        raise ArtifactStorageError(
            "another pack is saving a winner for %s and did not finish within %.0fs; this "
            "winner was NOT saved" % (bundle.workspace, timeout)) from exc


def _install(bundle, store, limits):
    """Admit, stage, verify and rename one bundle into the store.  Caller holds the store lock."""
    pruned = _admit(store, bundle.workspace, int(bundle.summary.get("bytes") or 0), limits)
    try:
        staging = tempfile.mkdtemp(prefix=".staging-", dir=store)
    except OSError as exc:
        raise ArtifactStorageError(_os_error("cannot create artifact store", store, exc)) from exc

    artifact_id = "%s-%s" % (_stamp(), uuid.uuid4().hex[:8])
    final = os.path.join(store, artifact_id)
    try:
        blobs_dir = os.path.join(staging, "blobs")
        os.makedirs(blobs_dir, exist_ok=True)
        stored_bytes = 0
        for change in bundle.changes:
            sha = change.get("blob")
            if not sha:
                continue
            destination = os.path.join(blobs_dir, sha)
            if os.path.exists(destination):
                continue                      # identical content added under two paths
            source = os.path.join(bundle.source_dir, *change["path"].split("/"))
            try:
                _copy_blob(source, destination, sha)
            except OSError as exc:
                raise ArtifactStorageError(
                    _os_error("cannot store winner file", change["path"], exc)) from exc
            stored_bytes += int(change.get("target", {}).get("size") or 0)
        record = {
            "schema": SCHEMA, "id": artifact_id, "kind": "pack-winner",
            "created_at": _now(),
            "created_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "workspace": bundle.workspace, "workspace_key": workspace_key(bundle.workspace),
            "baseline_at": bundle.baseline_at,
            "metadata": dict(bundle.metadata or {}),
            "summary": dict(bundle.summary, stored_bytes=stored_bytes),
            "unsupported": list(bundle.unsupported),
            "unreadable": list(bundle.unreadable),
            "limits": limits.to_dict(),
            "changes": list(bundle.changes),
            "applies": [],
        }
        _write_json(os.path.join(staging, "artifact.json"), record)
        os.replace(staging, final)
    except BaseException as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, OSError):
            raise ArtifactStorageError(
                _os_error("cannot save pack artifact", final, exc)) from exc
        raise
    record["path"] = final
    record["pruned"] = pruned
    return record


def _artifact_dir(artifact_id, root=None, store=None):
    ident = str(artifact_id or "")
    if not _ID.match(ident):
        raise ArtifactNotFound("not a pack artifact id: %s" % _text(ident, 80))
    return os.path.join(store or artifacts_root(root), ident)


def _corrupt(path, detail):
    return PackArtifactError("pack artifact %s is corrupt: %s" % (path, detail))


def _notes(value, limit=50):
    """Bound an advisory list (``unsupported``/``unreadable``) into predictable UI-safe rows."""
    rows = []
    for item in (value if isinstance(value, list) else ())[:limit]:
        if isinstance(item, dict):
            rows.append({"path": _text(item.get("path"), 200),
                         "reason": _text(item.get("reason") or item.get("error"), 200)})
        else:
            rows.append({"path": "", "reason": _text(item, 200)})
    return rows


def _int_field(value, name, path, *, maximum, minimum=0):
    # ``bool`` is an ``int`` in Python; a manifest saying ``"size": true`` is corrupt, not 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _corrupt(path, "%s is not an integer" % name)
    if value < minimum or value > maximum:
        raise _corrupt(path, "%s is out of range (%d)" % (name, value))
    return value


def _clean_state(value, name, path):
    """Normalize one baseline/target state.

    Everything downstream — the conflict check, the executor, the rollback — reads these dicts,
    so this is where an untrusted shape stops.  ``link``/``other``/anything unknown is refused:
    the writer never produces them as a change (they go to ``unsupported``), so seeing one here
    means the manifest was edited or damaged.
    """
    if not isinstance(value, dict):
        raise _corrupt(path, "%s state is not an object" % name)
    kind = value.get("type")
    if kind not in ("file", "dir", "absent"):
        raise _corrupt(path, "%s state has an unusable type: %s" % (name, _text(kind, 40)))
    if kind != "file":
        return {"type": kind}
    sha = value.get("sha256")
    if not isinstance(sha, str) or not _SHA.match(sha):
        raise _corrupt(path, "%s state has no valid sha256" % name)
    size = _int_field(value.get("size"), "%s size" % name, path, maximum=_MAX_BLOB_BYTES)
    return {"type": "file", "sha256": sha, "size": size, "exec": bool(value.get("exec"))}


def _clean_change(raw, path, index):
    """One change entry, fully typed, or ``PackArtifactError``.  Unsafe paths are reported."""
    if not isinstance(raw, dict):
        raise _corrupt(path, "change %d is not an object" % index)
    action = raw.get("action")
    if action not in ("add", "modify", "delete"):
        raise _corrupt(path, "change %d has an unknown action: %s" % (index, _text(action, 40)))
    baseline = _clean_state(raw.get("baseline"), "change %d baseline" % index, path)
    target = _clean_state(raw.get("target"), "change %d target" % index, path)
    # The action must agree with the states, or the executor would be told to do one thing while
    # the conflict check reasoned about another.
    if action == "modify" and (baseline["type"] != "file" or target["type"] != "file"):
        raise _corrupt(path, "change %d claims a modification of a non-file" % index)
    if action == "add" and (target["type"] != "file" or baseline["type"] == "file"):
        raise _corrupt(path, "change %d claims an addition that replaces a file" % index)
    if action == "delete" and (baseline["type"] != "file" or target["type"] == "file"):
        raise _corrupt(path, "change %d claims a deletion of a non-file" % index)
    blob = raw.get("blob")
    if action == "delete":
        if blob is not None:
            raise _corrupt(path, "change %d is a deletion but carries content" % index)
    elif not isinstance(blob, str) or blob != target["sha256"]:
        # The blob name IS the content digest; a mismatch would let a manifest point one path at
        # another path's bytes.
        raise _corrupt(path, "change %d names content that is not its own digest" % index)
    if not isinstance(raw.get("path"), str):
        # A non-string path is a broken manifest, not a merely unsafe one: report it as corrupt
        # rather than filing "42" under paths outside the workspace.
        raise _corrupt(path, "change %d has no path" % index)
    return {"path": check_relpath(raw["path"]), "action": action,
            "baseline": baseline, "target": target, "blob": blob}


def _validate(record, path):
    """Turn an untrusted manifest into a record the rest of this module may trust.

    Anything wrong raises :class:`PackArtifactError` (apply reports it as ``corrupt``); the one
    tolerated defect is a path that is unsafe rather than malformed, which is collected into
    ``record["unsafe"]`` so apply can name it instead of dying on it.
    """
    if not isinstance(record, dict):
        raise _corrupt(path, "manifest is not an object")
    schema = record.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise _corrupt(path, "schema is not an integer")
    if schema != SCHEMA:
        raise PackArtifactError("unsupported pack artifact schema: %s" % path)
    ident = record.get("id")
    expected = os.path.basename(str(path).rstrip("\\/"))
    if not isinstance(ident, str) or ident != expected:
        # A manifest that names a different artifact has been moved, copied or hand-edited; its
        # blobs and its change list may belong to different bundles.
        raise _corrupt(path, "manifest id %s does not match its directory" % _text(ident, 80))
    for field in ("workspace", "changes", "summary"):
        if field not in record:
            raise PackArtifactError("pack artifact is missing %r: %s" % (field, path))
    if not isinstance(record.get("workspace"), str) or len(record["workspace"]) > _MAX_PATH_CHARS:
        raise _corrupt(path, "workspace is not a usable path")
    if not isinstance(record.get("workspace_key"), str):
        raise _corrupt(path, "workspace key is missing")
    if not isinstance(record.get("summary"), dict):
        raise _corrupt(path, "summary is not an object")
    changes = record.get("changes")
    if not isinstance(changes, list):
        raise _corrupt(path, "change list is not a list")
    if len(changes) > _MAX_CHANGES:
        raise _corrupt(path, "change list holds %d entries" % len(changes))
    cleaned, unsafe, seen = [], [], {}
    for index, raw in enumerate(changes):
        try:
            change = _clean_change(raw, path, index)
        except UnsafePath as exc:
            named = raw.get("path") if isinstance(raw, dict) else raw
            unsafe.append({"path": _text(named, 200), "reason": str(exc)})
            continue
        # Two entries for one path (or for two paths that are the same file on a case-insensitive
        # filesystem) have no defined order: whichever ran last would silently win.
        key = change["path"].casefold()
        if key in seen:
            raise _corrupt(path, "changes %d and %d both target %s" % (
                seen[key], index, change["path"]))
        seen[key] = index
        cleaned.append(change)
    record["changes"] = cleaned
    applies = record.get("applies")
    record["applies"] = ([row for row in applies if isinstance(row, dict)]
                         if isinstance(applies, list) else [])
    record["unsupported"] = _notes(record.get("unsupported"))
    record["unreadable"] = _notes(record.get("unreadable"))
    record["unsafe"] = unsafe
    record["path"] = path
    return record


def _read_manifest(directory):
    """Read ``artifact.json`` with every bound in place before json.loads sees a byte."""
    _refuse_redirect(directory, "the pack artifact directory")
    manifest = os.path.join(directory, "artifact.json")
    _refuse_redirect(manifest, "the pack artifact manifest")
    try:
        st = os.lstat(manifest)
        if not stat.S_ISREG(st.st_mode):
            raise _corrupt(directory, "artifact.json is not a regular file")
        if st.st_size > _MAX_MANIFEST_BYTES:
            raise _corrupt(directory, "artifact.json is %d bytes" % st.st_size)
        with open(manifest, "rb") as handle:
            raw = handle.read(_MAX_MANIFEST_BYTES + 1)
    except FileNotFoundError as exc:
        raise ArtifactNotFound("no pack artifact %s under %s" % (
            os.path.basename(directory), os.path.dirname(directory))) from exc
    except OSError as exc:
        raise PackArtifactError(_os_error("cannot read pack artifact", manifest, exc)) from exc
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise _corrupt(directory, "artifact.json is larger than %d bytes" % _MAX_MANIFEST_BYTES)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise _corrupt(directory, "artifact.json is not valid JSON") from exc


def inspect_artifact(artifact_id, *, root=None, store=None):
    """Return the stored record (metadata + full change list).  No file contents, no model call.

    Raises :class:`ArtifactNotFound` or :class:`PackArtifactError` — never a raw ``ValueError`` /
    ``TypeError`` / ``AttributeError`` from a manifest somebody edited by hand.
    """
    directory = _artifact_dir(artifact_id, root=root, store=store)
    record = _read_manifest(directory)
    try:
        return _validate(record, directory)
    except (ArtifactNotFound, PackArtifactError):
        raise
    except (ValueError, TypeError, AttributeError, KeyError, IndexError,
            OverflowError, RecursionError) as exc:
        raise _corrupt(directory, "%s: %s" % (type(exc).__name__, _text(exc, 120))) from exc


def list_artifacts(*, workspace=None, root=None, store=None, limit=50):
    """Newest first.  Corrupt entries are skipped, not raised: review must stay reachable."""
    directory = store or artifacts_root(root)
    key = workspace_key(workspace) if workspace else ""
    rows = []
    try:
        names = sorted(os.listdir(directory), reverse=True)
    except OSError:
        return rows
    for name in names:
        if not _ID.match(name):
            continue
        try:
            record = inspect_artifact(name, store=directory)
        except PackArtifactError:
            continue
        if key and record.get("workspace_key") != key:
            continue
        rows.append(summarize_artifact(record))
        if len(rows) >= max(1, int(limit)):
            break
    return rows


def delete_artifact(artifact_id, *, root=None, store=None, timeout=10.0):
    """Remove one bundle on the user's explicit instruction.  True only if it is really gone.

    Unlike pruning this does not ask whether the bundle was applied — deleting your own artifact
    is allowed, and the review UI is where that decision belongs.  It does wait for an apply that
    is currently using the bundle (returning False on timeout) rather than deleting the blobs out
    from under it mid-write.
    """
    directory = _artifact_dir(artifact_id, root=root, store=store)
    lock = _artifact_lock(store or artifacts_root(root), os.path.basename(directory))
    try:
        with statelock.transaction(lock, timeout=timeout):
            shutil.rmtree(directory)
    except FileNotFoundError:
        return True
    except (OSError, statelock.StateLockTimeout):
        return False
    return True


def summarize_artifact(record, *, preview=20):
    """Compact, UI/SSE-safe view: counts and a bounded path preview, never the file contents."""
    changes = list(record.get("changes") or ())
    shown = [{"path": c.get("path"), "action": c.get("action"),
              "size": (c.get("target") or {}).get("size", 0)} for c in changes[:max(0, preview)]]
    applies = list(record.get("applies") or ())
    return {"id": record.get("id"), "path": record.get("path", ""),
            "workspace": record.get("workspace", ""),
            "created_at": record.get("created_at"), "created_iso": record.get("created_iso"),
            "summary": dict(record.get("summary") or {}),
            "metadata": dict(record.get("metadata") or {}),
            "changed": shown, "changed_truncated": max(0, len(changes) - len(shown)),
            "unsupported": list(record.get("unsupported") or ())[:20],
            "unsafe": list(record.get("unsafe") or ())[:20],
            "applied_at": (applies[-1].get("at") if applies else None),
            "apply_count": sum(1 for row in applies if row.get("status") == "applied")}


# --------------------------------------------------------------------------- apply

def _probe(path):
    """Live state of one path, in the same vocabulary the bundle stores."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"type": "absent"}
    except OSError as exc:
        return {"type": "error", "error": _os_error("cannot stat", path, exc)}
    if _link_like(path, st):
        return {"type": "link"}
    if stat.S_ISDIR(st.st_mode):
        return {"type": "dir"}
    if not stat.S_ISREG(st.st_mode):
        return {"type": "other"}
    try:
        digest, size = _hash_file(path)
    except OSError as exc:
        return {"type": "error", "error": _os_error("cannot read", path, exc)}
    return {"type": "file", "sha256": digest, "size": size, "exec": bool(st.st_mode & 0o111)}


def _same(live, expected):
    if live.get("type") != expected.get("type"):
        return False
    if live.get("type") != "file":
        return live.get("type") in ("absent", "dir")
    if live.get("sha256") != expected.get("sha256"):
        return False
    # The executable bit is meaningless on Windows and would make every cross-platform bundle a
    # conflict; content is the promise there.
    if os.name == "nt":
        return True
    return bool(live.get("exec")) == bool(expected.get("exec"))


def _describe(state):
    kind = state.get("type", "?")
    if kind == "file":
        return "file %s" % str(state.get("sha256") or "")[:12]
    return kind


def _plan(record, workspace):
    """Validate every touched path against the live tree.  Nothing here writes.

    Consumes the normalized change list ``_validate`` produced — paths, actions, states and blob
    digests are already typed and bounded here, so this function reasons about the FILESYSTEM
    rather than about what a manifest might have said.
    """
    steps, conflicts, satisfied = [], [], []
    delete_paths = {change["path"] for change in record.get("changes") or ()
                    if change["action"] == "delete"}
    for change in record.get("changes") or ():
        rel = change["path"]
        baseline = change["baseline"]
        target = change["target"]
        try:
            abs_path = resolve_within(workspace, rel)
        except UnsafePath as exc:
            conflicts.append({"path": _text(rel, 200), "reason": str(exc), "kind": "unsafe"})
            continue
        live = _probe(abs_path)
        if live.get("type") == "error":
            conflicts.append({"path": rel, "reason": live["error"], "kind": "unreadable"})
            continue
        if _same(live, target):
            satisfied.append(rel)             # already applied; re-applying must not undo anything
            continue
        if not _same(live, baseline):
            conflicts.append({
                "path": rel, "kind": "changed",
                "expected": _describe(baseline), "found": _describe(live),
                "reason": "%s changed since the pack attempt started (expected %s, found %s)" % (
                    rel, _describe(baseline), _describe(live))})
            continue
        expect = baseline
        if baseline.get("type") == "dir":
            # A directory becoming a file. Only legal when the bundle also removes everything
            # inside it; we never recursively delete descendants we were not told about.
            blockers, dir_steps = _directory_replacement(abs_path, rel, delete_paths)
            if blockers:
                conflicts.extend(blockers)
                continue
            steps.extend(dir_steps)
            # Those rmdir steps run first, so by the time the file is written the path is gone.
            expect = {"type": "absent"}
        if change.get("action") == "delete":
            steps.append({"kind": "delete", "path": rel, "abs": abs_path, "expect": expect})
        else:
            steps.append({"kind": "write", "path": rel, "abs": abs_path, "expect": expect,
                          "blob": change.get("blob"), "exec": bool(target.get("exec")),
                          "size": int(target.get("size") or 0)})
    # Deepest paths first for removals, shallowest first for writes: a file that becomes a
    # directory needs its own removal to happen before its children are created.
    steps.sort(key=lambda s: (0 if s["kind"] in ("delete", "rmdir") else 1,
                              -s["path"].count("/") if s["kind"] in ("delete", "rmdir")
                              else s["path"].count("/"), s["path"]))
    return {"steps": steps, "conflicts": conflicts, "satisfied": satisfied}


def _directory_replacement(abs_path, rel, delete_paths):
    """Check a live directory can legally become a file, and schedule its empty-dir removals."""
    blockers, dirs = [], []
    stack, seen = [(abs_path, rel)], 0
    while stack:
        current, current_rel = stack.pop()
        dirs.append((current, current_rel))
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError as exc:
            blockers.append({"path": current_rel, "kind": "unreadable",
                             "reason": _os_error("cannot read", current, exc)})
            return blockers, []
        for entry in entries:
            seen += 1
            if seen > _MAX_DIR_SCAN:
                blockers.append({"path": rel, "kind": "changed",
                                 "reason": "%s holds more than %d entries; replacing it with a "
                                           "file would delete unexpected work" % (rel,
                                                                                  _MAX_DIR_SCAN)})
                return blockers, []
            child_rel = "%s/%s" % (current_rel, entry.name)
            if _link_like(entry.path):
                blockers.append({"path": child_rel, "kind": "unsafe",
                                 "reason": "%s contains a symlink or junction" % child_rel})
                return blockers, []
            if entry.is_dir(follow_symlinks=False):
                stack.append((entry.path, child_rel))
            elif child_rel not in delete_paths:
                blockers.append({"path": child_rel, "kind": "changed",
                                 "reason": "%s still contains %s, which the winner did not "
                                           "remove" % (rel, child_rel)})
                return blockers, []
    steps = [{"kind": "rmdir", "path": child_rel, "abs": child_abs,
              "expect": {"type": "dir"}}
             for child_abs, child_rel in sorted(dirs, key=lambda row: -row[1].count("/"))]
    return [], steps


def _ensure_parents(workspace, rel, created):
    parts = rel.split("/")[:-1]
    current = os.path.abspath(workspace)
    for part in parts:
        current = os.path.join(current, part)
        if _link_like(current):
            raise UnsafePath("refusing to follow a symlink or junction: %s" % current)
        if not os.path.isdir(current):
            os.mkdir(current)
            created.append(current)


def _verify_blobs(plan, directory):
    """Hash every blob the plan needs BEFORE touching the workspace."""
    problems = []
    blobs_dir = os.path.join(directory, "blobs")
    try:
        _refuse_redirect(blobs_dir, "the pack artifact blob directory")
    except PackArtifactError as exc:
        return [str(exc)]
    for step in plan["steps"]:
        if step["kind"] != "write":
            continue
        blob = os.path.join(blobs_dir, str(step.get("blob") or ""))
        step["blob_path"] = blob
        try:
            # A blob that has become a link would make us copy whatever it points at, under a
            # name whose digest we then never re-check.
            _refuse_redirect(blob, "stored content for %s" % step["path"])
            digest, _size = _hash_file(blob)
        except PackArtifactError as exc:
            problems.append(str(exc))
            continue
        except OSError as exc:
            problems.append(_os_error("cannot read stored content for %s from" % step["path"],
                                      blob, exc))
            continue
        if digest != step.get("blob"):
            problems.append("stored content for %s is corrupt (%s)" % (step["path"], blob))
    return problems


def _revalidate(workspace, step):
    """Re-resolve a step's whole path immediately before mutating it.

    ``_plan`` resolved this path seconds ago.  Between then and now a PARENT directory can have
    become a symlink or junction — checking only the leaf would then delete or overwrite a file
    outside the workspace entirely.  Deletions need this exactly as much as writes do.
    """
    abs_path = resolve_within(workspace, step["path"])
    if os.path.normcase(abs_path) != os.path.normcase(step["abs"]):
        raise UnsafePath("%s no longer resolves to %s" % (step["path"], step["abs"]))
    return abs_path


def _restore(workspace, entry):
    """Undo one executed step.  Returns an error string or ''.

    Rollback re-resolves its path too: restoring a backup through a junction that appeared
    mid-apply would write the user's own file somewhere outside the workspace.
    """
    try:
        _revalidate(workspace, entry)
        if _link_like(entry["abs"]):
            raise UnsafePath("%s is now a symlink or junction" % entry["abs"])
        if entry["kind"] == "write" and entry.get("backup") is None:
            if os.path.lexists(entry["abs"]):
                os.remove(entry["abs"])
        elif entry.get("backup") is not None:
            shutil.copy2(entry["backup"], entry["abs"])
        elif entry["kind"] == "rmdir":
            os.makedirs(entry["abs"], exist_ok=True)
    except UnsafePath as exc:
        return "could not roll back %s: %s" % (entry["abs"], exc)
    except OSError as exc:
        return _os_error("could not roll back", entry["abs"], exc)
    return ""


def _execute(plan, workspace, directory):
    """Staged writes with per-file backups and best-effort rollback.

    Returns ``{"ok", "error", "changed", "rolled_back", "unrestored", "notes", "backup_dir"}``.
    On failure ``changed`` is empty ONLY when the rollback fully succeeded; otherwise it names
    every path this run touched, each with the backup holding its original, because a caller that
    is told "nothing changed" about a half-restored workspace has been lied to.
    """
    backup_dir = os.path.join(directory, "backups", "%s-%s" % (_stamp(), uuid.uuid4().hex[:6]))
    _refuse_redirect(os.path.join(directory, "backups"), "the pack artifact backup directory")
    os.makedirs(backup_dir, exist_ok=True)
    done, created_dirs, notes = [], [], []
    changed = []

    def _backup(step, index):
        target = os.path.join(backup_dir, "%04d-%s" % (index, step["path"].replace("/", "_")[-90:]))
        shutil.copy2(step["abs"], target)
        return target

    for index, step in enumerate(plan["steps"]):
        try:
            _revalidate(workspace, step)
            live = _probe(step["abs"])
            if live.get("type") == "link":
                raise UnsafePath("%s is now a symlink or junction; refusing to replace it"
                                 % step["abs"])
            if step["kind"] == "rmdir":
                if live.get("type") != "dir":
                    raise OSError("%s is no longer a directory" % step["abs"])
            elif not _same(live, step["expect"]):
                # An external editor moved between validation and this write. Stop honestly.
                raise OSError("%s changed while pack was applying (expected %s, found %s)" % (
                    step["abs"], _describe(step["expect"]), _describe(live)))
            if step["kind"] == "delete":
                backup = _backup(step, index)
                os.remove(step["abs"])
                done.append({"kind": "delete", "path": step["path"], "abs": step["abs"],
                             "backup": backup})
                changed.append({"path": step["path"], "action": "delete", "backup": backup})
            elif step["kind"] == "rmdir":
                os.rmdir(step["abs"])
                done.append({"kind": "rmdir", "path": step["path"], "abs": step["abs"],
                             "backup": None})
            else:
                backup = _backup(step, index) if live.get("type") == "file" else None
                _ensure_parents(workspace, step["path"], created_dirs)
                tmp = os.path.join(os.path.dirname(step["abs"]),
                                   ".collie-pack-%s.tmp" % uuid.uuid4().hex[:8])
                try:
                    shutil.copyfile(step["blob_path"], tmp)
                    if os.name != "nt":
                        base = 0o644
                        if live.get("type") == "file":
                            try:
                                base = stat.S_IMODE(os.stat(step["abs"]).st_mode)
                            except OSError:
                                pass
                        os.chmod(tmp, (base | 0o111) if step["exec"] else (base & ~0o111))
                    os.replace(tmp, step["abs"])
                except BaseException:
                    # The staged copy is ours and half-written; it must never be left in the
                    # user's tree for a build or a grep to find.
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    raise
                done.append({"kind": "write", "path": step["path"], "abs": step["abs"],
                             "backup": backup})
                changed.append({"path": step["path"], "action": "modify" if backup else "add",
                                "backup": backup or ""})
        except (OSError, UnsafePath) as exc:
            failure = (_os_error("cannot apply", step["abs"], exc)
                       if isinstance(exc, OSError) and getattr(exc, "strerror", None)
                       else str(exc))
            rollback_errors, unrestored = [], []
            for entry in reversed(done):
                error = _restore(workspace, entry)
                if error:
                    rollback_errors.append(error)
                    unrestored.append({"path": entry["path"], "action": entry["kind"],
                                       "backup": entry.get("backup") or ""})
            for path in reversed(created_dirs):
                try:
                    if not _link_like(path):
                        os.rmdir(path)
                except OSError:
                    pass
            return {"ok": False, "error": failure,
                    "changed": [] if not rollback_errors else changed,
                    "rolled_back": not rollback_errors, "unrestored": unrestored,
                    "notes": rollback_errors, "backup_dir": backup_dir}
    # Directories the winner emptied are removed only while they stay empty.
    for step in plan["steps"]:
        if step["kind"] != "delete":
            continue
        parent = os.path.dirname(step["abs"])
        root = os.path.abspath(workspace)
        while os.path.normcase(parent).startswith(os.path.normcase(root) + os.sep):
            # rmdir on a junction removes the junction, not its (empty-looking) target.
            if _link_like(parent):
                break
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)
    return {"ok": True, "error": "", "changed": changed, "rolled_back": False,
            "unrestored": [], "notes": notes, "backup_dir": backup_dir}


def _journal(directory, entry):
    manifest = os.path.join(directory, "artifact.json")
    try:
        with statelock.transaction(manifest, timeout=10):
            record = _read_manifest(directory)      # bounded read, links refused
            if not isinstance(record, dict):
                raise PackArtifactError("manifest is not an object")
            applies = record.get("applies")
            record["applies"] = ((applies if isinstance(applies, list) else []) +
                                 [entry])[-_MAX_JOURNAL:]
            _write_json(manifest, record)
    except Exception as exc:          # a lost journal line must never fail a good apply
        return _text("%s: %s" % (type(exc).__name__, exc), 200)
    return ""


def apply_artifact(artifact_id, cwd=None, *, root=None, store=None, dry_run=False,
                   allow_foreign_root=False, timeout=30.0):
    """Apply a saved winner bundle to ``cwd`` (default: the workspace it was captured from).

    Never raises for an expected outcome — the caller gets a result dict it can render:

    ``{"ok", "applied", "code", "error", "changed", "already_applied", "conflicts",
       "unsupported", "backup_dir", "rolled_back", "unrestored", "notes"}``

    ``code`` is "" on success, else one of ``not_found``, ``corrupt``, ``unsupported``,
    ``unsafe_path``, ``missing_workspace``, ``foreign_root``, ``conflict``, ``locked``,
    ``io_error``.

    What is guaranteed, and what is not:

    * A non-empty ``conflicts`` list means NOTHING was written — the whole plan is validated
      before the first byte.  ``dry_run=True`` writes nothing at all, including the apply journal.
    * Collie's own applies, saves and prunes are serialized per workspace and per artifact across
      processes, and the bundle cannot be deleted while this call is using it.
    * There is NO atomicity against an arbitrary external editor.  Every step re-resolves its path
      (parents included) and re-checks the live state immediately before mutating it, then stops
      at the first surprise; that narrows the window, it does not close it.
    * Rollback after a partial failure is BEST EFFORT.  ``rolled_back`` True means the workspace
      was returned to its pre-apply state.  False means it was not: ``changed`` then lists every
      path this call touched together with the backup holding its original, ``unrestored`` lists
      the ones the rollback itself could not put back, and ``backup_dir`` holds the originals.
    """
    out = {"ok": False, "applied": False, "code": "", "error": "",
           "artifact": _text(artifact_id, 80), "workspace": "", "dry_run": bool(dry_run),
           "changed": [], "already_applied": [], "conflicts": [], "unsupported": [],
           "notes": [], "backup_dir": "", "rolled_back": False, "unrestored": []}
    try:
        record = inspect_artifact(artifact_id, root=root, store=store)
    except ArtifactNotFound as exc:
        out.update(code="not_found", error=str(exc))
        return out
    except PackArtifactError as exc:
        out.update(code="corrupt", error=str(exc))
        return out
    directory = record["path"]
    out["workspace"] = target = os.path.abspath(str(cwd or record.get("workspace") or ""))
    if record.get("unsupported"):
        rows = list(record["unsupported"])[:20]
        # Name what is actually unreplayable (a symlink, a device node, a path we refuse) instead
        # of making the user open the manifest to find out.
        why = "; ".join("%s: %s" % (row.get("path") or "?", row.get("reason") or "unsupported")
                        for row in rows[:3])
        out.update(code="unsupported", unsupported=rows,
                   error="pack artifact %s contains %d change(s) Collie will not replay (%s); "
                         "nothing was applied" % (record["id"], len(record["unsupported"]), why))
        return out
    if record.get("unsafe"):
        out.update(code="unsafe_path", conflicts=list(record["unsafe"])[:20],
                   error="pack artifact %s names %d path(s) outside the workspace; nothing was "
                         "applied" % (record["id"], len(record["unsafe"])))
        return out
    if not os.path.isdir(target):
        out.update(code="missing_workspace", error="workspace is not a directory: %s" % target)
        return out
    if not allow_foreign_root and workspace_key(target) != record.get("workspace_key"):
        out.update(code="foreign_root",
                   error="pack artifact %s was captured from %s, not %s" % (
                       record["id"], record.get("workspace"), target))
        return out

    # One Collie apply at a time per workspace, across processes, and — under the second lock —
    # nobody may prune or delete this bundle while we are reading its blobs. This is not a claim
    # of atomicity against an arbitrary external editor: that is what the pre-write recheck is for.
    store_dir = artifacts_root(root) if store is None else store
    try:
        with statelock.transaction(_apply_lock(store_dir, target), timeout=timeout), \
                statelock.transaction(_artifact_lock(store_dir, record["id"]), timeout=timeout):
            # Re-read under the artifact lock: between the first read and here, a concurrent save
            # may legitimately have pruned this bundle (it was delivered) or a user may have
            # deleted it. Acting on the copy in memory would apply blobs that no longer exist.
            record = inspect_artifact(record["id"], store=store_dir)
            plan = _plan(record, target)
            out["already_applied"] = plan["satisfied"][:200]
            if plan["conflicts"]:
                out.update(code="conflict", conflicts=plan["conflicts"][:50],
                           error="%d path(s) changed since this pack ran; nothing was applied" %
                                 len(plan["conflicts"]))
                # A preview writes NOTHING, and the journal lives inside the artifact.
                if not dry_run:
                    journal_error = _journal(directory, {
                        "at": _now(), "cwd": target, "status": "conflict",
                        "conflicts": len(plan["conflicts"])})
                    if journal_error:
                        out["notes"].append(journal_error)
                return out
            problems = _verify_blobs(plan, directory)
            if problems:
                out.update(code="corrupt", error="; ".join(problems[:5]))
                return out
            if dry_run:
                out.update(ok=True, applied=False,
                           changed=[{"path": s["path"],
                                     "action": "delete" if s["kind"] == "delete" else "write"}
                                    for s in plan["steps"] if s["kind"] != "rmdir"])
                return out
            if not plan["steps"]:
                out.update(ok=True, applied=True)
                out["notes"].append("every change in this artifact is already present")
                return out
            result = _execute(plan, target, directory)
            out["backup_dir"] = result["backup_dir"]
            out["notes"].extend(n for n in result["notes"] if n)
            out["rolled_back"] = result["rolled_back"]
            out["unrestored"] = result["unrestored"][:100]
            if not result["ok"]:
                out.update(code="io_error", error=result["error"],
                           changed=result["changed"][:500])
                if not result["rolled_back"]:
                    out["error"] += (
                        "; the workspace is PARTIALLY changed — %d path(s) were touched and %d "
                        "could not be rolled back; the originals are in %s" % (
                            len(result["changed"]), len(result["unrestored"]),
                            result["backup_dir"]))
                journal_error = _journal(directory, {
                    "at": _now(), "cwd": target, "status": "failed",
                    "error": _text(result["error"], 300),
                    "rolled_back": result["rolled_back"], "backup": result["backup_dir"]})
                if journal_error:
                    out["notes"].append(journal_error)
                return out
            out.update(ok=True, applied=True, changed=result["changed"][:500])
            journal_error = _journal(directory, {
                "at": _now(), "cwd": target, "status": "applied",
                "changed": len(result["changed"]), "backup": result["backup_dir"]})
            if journal_error:
                out["notes"].append(journal_error)
            return out
    except statelock.StateLockTimeout as exc:
        out.update(code="locked", error=str(exc))
        return out
    except UnsafePath as exc:
        out.update(code="unsafe_path", error=str(exc))
        return out
    except ArtifactNotFound as exc:
        # The bundle was deleted between the first read and the lock. Nothing was written.
        out.update(code="not_found", error=str(exc))
        return out
    except PackArtifactError as exc:
        out.update(code="corrupt", error=str(exc))
        return out
    except OSError as exc:
        out.update(code="io_error", error=_os_error("cannot apply pack artifact", directory, exc))
        return out
