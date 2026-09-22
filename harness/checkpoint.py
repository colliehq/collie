"""checkpoint / undo — let the model (or a wrapper) roll back file edits.

collie edits files in place; a wrong edit used to be unrecoverable without git. Now write_file and
edit_file call record() with the file's PRIOR content before mutating, building a per-project undo
stack. The `undo` tool restores the most recent change (repeat to walk further back); a file that
didn't exist before is removed on undo. State persists to ~/.collie/checkpoints/<project>.json so an
undo survives across a --continue.

Deliberately lightweight (no git dependency, works in any dir). Files above the size cap are noted
but not snapshotted (we don't want to balloon the journal on a huge generated file)."""
import json
import os
import hashlib
import base64
import stat
import threading

_DIR = os.environ.get("COLLIE_CHECKPOINT_DIR") or os.path.expanduser("~/.collie/checkpoints")
_MAX_BYTES = 512 * 1024      # don't snapshot files bigger than this into the journal
_MAX_DEPTH = 200             # cap the undo stack so a long run can't grow it without bound
_STACKS: dict = {}           # (project, canonical root) -> list[snapshot]
_LOCK = threading.RLock()


def _safe_project(project):
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (project or "default"))[:80]
    return safe or "default"


def _canonical_root(path):
    """Return the real repository root containing *path*.

    ``.git`` may be either a directory or a worktree pointer file.  Non-git callers still get an
    isolated canonical directory, rather than falling back to the process cwd (the source of the
    original cross-repository undo bug).
    """
    p = os.path.realpath(os.path.abspath(path or os.getcwd()))
    if not os.path.isdir(p):
        p = os.path.dirname(p)
    cur = p
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return os.path.normcase(os.path.realpath(cur))
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.normcase(p)
        cur = parent


def _path(project, root=None):
    safe = _safe_project(project)
    if root is None:                         # legacy journal path (pre repo scoping)
        return os.path.join(_DIR, safe + ".json")
    digest = hashlib.sha256(root.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return os.path.join(_DIR, "%s-%s.json" % (safe, digest))


def _inside(path, root):
    try:
        path = os.path.normcase(os.path.realpath(path))
        root = os.path.normcase(os.path.realpath(root))
        return os.path.commonpath([path, root]) == root
    except (OSError, ValueError):
        return False


def _load(project, root):
    root = _canonical_root(root)
    key = (project, root)
    with _LOCK:
        if key in _STACKS:
            return _STACKS[key]
        data = None
        try:
            with open(_path(project, root), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            pass
        if not isinstance(data, list):
            # Backward compatibility: old versions stored every repo using the same project in one
            # <project>.json.  Import only records physically inside this root.  Leave the legacy
            # file untouched so another repo can migrate its own records later.
            try:
                with open(_path(project), encoding="utf-8") as f:
                    legacy = json.load(f)
            except (OSError, ValueError):
                legacy = []
            data = [s for s in legacy if isinstance(s, dict)
                    and _inside(s.get("path", ""), root)] if isinstance(legacy, list) else []
        _STACKS[key] = data
        return data


def _persist(project, root):
    tmp = None
    try:
        os.makedirs(_DIR, exist_ok=True)
        key = (project, root)
        tmp = "%s.%d.%s.tmp" % (_path(project, root), os.getpid(), os.urandom(4).hex())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_STACKS.get(key, []), f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, _path(project, root))
    except OSError:
        pass
    finally:
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def record(project, abspath, cwd=None):
    """Snapshot a file's current state BEFORE it's about to be written/edited. Best-effort: never
    raises into the caller (a checkpoint failure must not block the edit itself)."""
    try:
        root = _canonical_root(cwd or abspath)
        abspath = os.path.realpath(os.path.abspath(abspath))
        # A checkpoint for repo A must never acquire authority over a path in repo B.  The edit
        # itself remains best-effort-compatible; it simply will not create a misleading undo entry.
        if not _inside(abspath, root):
            return
        existed = os.path.exists(abspath)
        prev = None
        prev_b64 = None
        too_big = False
        if existed:
            if os.path.getsize(abspath) > _MAX_BYTES:
                too_big = True
            else:
                # Exact bytes matter: replacement decoding corrupted non-UTF-8 files on undo.
                with open(abspath, "rb") as f:
                    prev_b64 = base64.b64encode(f.read()).decode("ascii")
        with _LOCK:
            stack = _load(project, root)
            stack.append({"path": abspath, "root": root, "project": project,
                          "existed": existed, "prev": prev, "prev_b64": prev_b64,
                          "too_big": too_big})
            if len(stack) > _MAX_DEPTH:
                del stack[:len(stack) - _MAX_DEPTH]
            _persist(project, root)
    except Exception:
        pass


def _undo_one(project, root):
    root = _canonical_root(root)
    with _LOCK:
        stack = _load(project, root)
        if not stack:
            return None
        # Do not discard the only prior copy until the filesystem operation succeeds.
        snap = stack[-1]
        p = snap["path"]
        if not _inside(p, root) or (snap.get("root") and snap.get("root") != root):
            stack.pop(); _persist(project, root)
            return "ERROR refusing cross-repository undo of %s" % p
        if snap.get("too_big"):
            stack.pop(); _persist(project, root)
            return "cannot undo %s (was too large to snapshot)" % p
        try:
            if not snap["existed"]:
                if os.path.exists(p):
                    os.remove(p)
                result = "undid: removed %s (it did not exist before)" % p
            else:
                raw = (base64.b64decode(snap["prev_b64"], validate=True)
                       if snap.get("prev_b64") is not None
                       else (snap.get("prev") or "").encode("utf-8"))
                mode = stat.S_IMODE(os.stat(p).st_mode) if os.path.exists(p) else None
                tmp = "%s.%d.%s.undo" % (p, os.getpid(), os.urandom(4).hex())
                try:
                    with open(tmp, "wb") as f:
                        f.write(raw); f.flush(); os.fsync(f.fileno())
                    if mode is not None:
                        try:
                            os.chmod(tmp, mode)
                        except OSError:
                            pass
                    os.replace(tmp, p)
                finally:
                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except OSError:
                        pass
                result = "undid: restored %s to its prior content" % p
        except Exception as e:
            return "ERROR restoring %s: %s" % (p, e)
        stack.pop()
        _persist(project, root)
        _invalidate(project, p)
        return result


def _invalidate(project, path):
    try:
        from .codeindex import invalidate
        invalidate(os.path.dirname(path))
    except Exception:
        pass


from .tools import Tool


class UndoTool(Tool):
    name, tier = "undo", "always"
    description = ("Roll back file edits made this session. Call with no args (or {\"n\":1}) to undo "
                   "the LAST write/edit; n>1 undoes that many, newest first. {\"action\":\"list\"} "
                   "shows what can be undone. A file that didn't exist before is removed on undo.")
    schema = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["undo", "list"]},
        "n": {"type": "integer"}}}

    def run(self, args, ctx):
        args = args if isinstance(args, dict) else {}
        root = _canonical_root(ctx.cwd)
        scope = getattr(ctx, "checkpoint_scope", "") or ctx.project
        stack = _load(scope, root)
        if args.get("action") == "list":
            if not stack:
                return "(nothing to undo)"
            lines = ["undoable edits (newest last, %d total):" % len(stack)]
            for s in stack[-20:]:
                tag = "new file" if not s["existed"] else ("too large" if s.get("too_big") else "modified")
                lines.append("  %s (%s)" % (s["path"], tag))
            return "\n".join(lines)
        try:
            n = max(1, int(args.get("n", 1)))
        except (TypeError, ValueError):
            n = 1
        if not stack:
            return "(nothing to undo)"
        out = []
        for _ in range(min(n, len(stack))):
            r = _undo_one(scope, root)
            if r is None:
                break
            out.append(r)
        return "\n".join(out)


def register_undo(registry):
    registry.register(UndoTool())
    return True
