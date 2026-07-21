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

_DIR = os.environ.get("COLLIE_CHECKPOINT_DIR") or os.path.expanduser("~/.collie/checkpoints")
_MAX_BYTES = 512 * 1024      # don't snapshot files bigger than this into the journal
_MAX_DEPTH = 200             # cap the undo stack so a long run can't grow it without bound
_STACKS: dict = {}           # project -> list[snapshot]


def _path(project):
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (project or "default"))[:80]
    return os.path.join(_DIR, safe + ".json")


def _load(project):
    if project in _STACKS:
        return _STACKS[project]
    try:
        with open(_path(project), encoding="utf-8") as f:
            data = json.load(f)
        _STACKS[project] = data if isinstance(data, list) else []
    except (OSError, ValueError):
        _STACKS[project] = []
    return _STACKS[project]


def _persist(project):
    try:
        os.makedirs(_DIR, exist_ok=True)
        tmp = _path(project) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_STACKS.get(project, []), f, ensure_ascii=False)
        os.replace(tmp, _path(project))
    except OSError:
        pass


def record(project, abspath):
    """Snapshot a file's current state BEFORE it's about to be written/edited. Best-effort: never
    raises into the caller (a checkpoint failure must not block the edit itself)."""
    try:
        existed = os.path.exists(abspath)
        prev = None
        too_big = False
        if existed:
            if os.path.getsize(abspath) > _MAX_BYTES:
                too_big = True
            else:
                with open(abspath, encoding="utf-8", errors="replace") as f:
                    prev = f.read()
        stack = _load(project)
        stack.append({"path": abspath, "existed": existed, "prev": prev, "too_big": too_big})
        if len(stack) > _MAX_DEPTH:
            del stack[:len(stack) - _MAX_DEPTH]
        _persist(project)
    except Exception:
        pass


def _undo_one(project):
    stack = _load(project)
    if not stack:
        return None
    snap = stack.pop()
    _persist(project)
    p = snap["path"]
    try:
        if snap.get("too_big"):
            return "cannot undo %s (was too large to snapshot)" % p
        if not snap["existed"]:
            if os.path.exists(p):
                os.remove(p)
            return "undid: removed %s (it did not exist before)" % p
        with open(p, "w", encoding="utf-8") as f:
            f.write(snap["prev"] or "")
        _invalidate(project, p)
        return "undid: restored %s to its prior content" % p
    except Exception as e:
        return "ERROR restoring %s: %s" % (p, e)


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
        stack = _load(ctx.project)
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
            r = _undo_one(ctx.project)
            if r is None:
                break
            out.append(r)
        return "\n".join(out)


def register_undo(registry):
    registry.register(UndoTool())
    return True
