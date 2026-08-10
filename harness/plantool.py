"""Durable, editable plan artifacts and the model-facing ``plan`` tool.

The legacy implementation persisted only a whole-array TODO list per project.
That was useful inside one process but unsafe for parallel agents and too thin to
support an Approve & build handoff.  Version 2 keeps the simple tool contract
while adding scoped identity, revisions/CAS, dependencies, files, risks, checks,
evidence, approval state, and bounded history.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
import uuid

from .tools import Tool

_VALID = ("pending", "in_progress", "completed", "blocked")
_MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "blocked": "[!]"}
_DIR = os.environ.get("COLLIE_PLAN_DIR") or os.path.expanduser("~/.collie/plans")
_LOCKS, _LOCKS_GUARD = {}, threading.Lock()
# Compatibility surface for older embedders/tests that cleared the former
# process cache to force a disk read. V2 has no authoritative process cache.
_MEM = {}


def _scope(ctx_or_scope):
    if isinstance(ctx_or_scope, str):
        return ctx_or_scope or "default"
    return (getattr(ctx_or_scope, "checkpoint_scope", "")
            or getattr(ctx_or_scope, "project", "") or "default")


def _path(scope):
    scope = _scope(scope)
    readable = "".join(c if (c.isalnum() or c in "-_") else "_" for c in scope)[:48]
    digest = hashlib.sha256(scope.encode("utf-8", "replace")).hexdigest()[:12]
    return os.path.join(_DIR, "%s-%s.json" % (readable or "plan", digest))


def _legacy_path(scope):
    safe = "".join(c if (c.isalnum() or c in "-_") else "_"
                   for c in _scope(scope))[:80]
    return os.path.join(_DIR, (safe or "default") + ".json")


def _source_path(scope):
    modern = _path(scope)
    return modern if os.path.exists(modern) else _legacy_path(scope)


@contextlib.contextmanager
def _locked(path):
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(os.path.realpath(path), threading.RLock())
    with lock:
        lock_path = path + ".lock"
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        fh = open(lock_path, "a+b")
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                if os.path.getsize(lock_path) == 0:
                    fh.write(b"\0"); fh.flush()
                fh.seek(0); msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            acquired = True
            yield
        finally:
            try:
                if acquired:
                    fh.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()


def _empty(scope):
    now = time.time()
    return {"version": 2, "scope": _scope(scope), "revision": 0,
            "title": "", "intent": "plan", "status": "draft",
            "approved": False, "created_at": now, "updated_at": now,
            "files": [], "risks": [], "checks": [], "todos": [], "history": []}


def _read_unlocked(path, scope):
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return _empty(scope)
    # Migrate the historical ``[{content,status}]`` shape in memory; next write
    # stores v2. Existing user plans remain visible.
    if isinstance(raw, list):
        out = _empty(scope)
        out["todos"] = _clean_todos(raw)
        return out
    if not isinstance(raw, dict):
        return _empty(scope)
    out = _empty(scope); out.update(raw)
    out["scope"] = _scope(scope); out["version"] = 2
    out["todos"] = _clean_todos(out.get("todos") or [])
    for field in ("files", "risks"):
        out[field] = [str(x)[:500] for x in (out.get(field) or []) if str(x).strip()][:100]
    out["checks"] = _clean_checks(out.get("checks") or [])
    out["history"] = list(out.get("history") or [])[-30:]
    return out


def _atomic_write(path, artifact):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:10])
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(artifact, fh, ensure_ascii=False, indent=2)
            fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        for attempt in range(7):
            try:
                os.replace(tmp, path); break
            except PermissionError:
                if attempt >= 6: raise
                time.sleep(.01 * (2 ** attempt))
    finally:
        try:
            if os.path.exists(tmp): os.remove(tmp)
        except OSError:
            pass


def _clean_todos(todos):
    clean, seen = [], set()
    for index, item in enumerate(todos if isinstance(todos, list) else []):
        if not isinstance(item, dict) or not str(item.get("content") or "").strip():
            continue
        content = str(item["content"]).strip()[:500]
        task_id = str(item.get("id") or "task-%02d-%s" % (
            index + 1, hashlib.sha1(content.encode("utf-8")).hexdigest()[:6]))[:80]
        if task_id in seen:
            task_id += "-%d" % (index + 1)
        seen.add(task_id)
        status = str(item.get("status") or "pending")
        if status not in _VALID: status = "pending"
        depends = [str(x)[:80] for x in (item.get("depends_on") or []) if str(x).strip()]
        clean.append({"id": task_id, "content": content, "status": status,
                      "depends_on": depends[:30],
                      "owner": str(item.get("owner") or "main")[:80],
                      "files": [str(x)[:300] for x in (item.get("files") or [])][:50],
                      "evidence": str(item.get("evidence") or "")[:4000],
                      "note": str(item.get("note") or "")[:1000]})
    return clean[:200]


def _clean_checks(checks):
    out = []
    for check in checks if isinstance(checks, list) else []:
        if isinstance(check, str):
            check = {"command": check}
        if not isinstance(check, dict) or not str(check.get("command") or "").strip():
            continue
        status = str(check.get("status") or "pending")
        if status not in ("pending", "passed", "failed", "blocked"):
            status = "pending"
        out.append({"command": str(check["command"]).strip()[:1000], "status": status,
                    "exit_code": check.get("exit_code"),
                    "ran_at": check.get("ran_at"),
                    "evidence": str(check.get("evidence") or "")[:4000]})
    return out[:100]


def _validate_dependencies(todos):
    ids = {t["id"] for t in todos}
    for todo in todos:
        unknown = [d for d in todo["depends_on"] if d not in ids]
        if unknown:
            raise ValueError("%s depends on unknown task(s): %s" %
                             (todo["id"], ", ".join(unknown)))
    visiting, done = set(), set()
    graph = {t["id"]: t["depends_on"] for t in todos}
    def visit(node):
        if node in visiting: raise ValueError("task dependency cycle includes %s" % node)
        if node in done: return
        visiting.add(node)
        for dep in graph.get(node, ()): visit(dep)
        visiting.remove(node); done.add(node)
    for node in graph: visit(node)


class RevisionConflict(RuntimeError):
    pass


class PlanArtifactStore:
    def get(self, scope):
        path = _path(scope)
        with _locked(path):
            return _read_unlocked(_source_path(scope), scope)

    def update(self, scope, patch, expected_revision=None, actor="user"):
        path = _path(scope)
        with _locked(path):
            current = _read_unlocked(_source_path(scope), scope)
            if expected_revision is not None and int(expected_revision) != int(current["revision"]):
                raise RevisionConflict("plan changed (expected revision %s, current %s)" %
                                       (expected_revision, current["revision"]))
            nxt = dict(current)
            for field in ("title", "intent", "status"):
                if field in patch:
                    nxt[field] = str(patch[field] or "")[:200]
            if "approved" in patch:
                nxt["approved"] = bool(patch["approved"])
                nxt["status"] = "approved" if nxt["approved"] else "draft"
            if "todos" in patch:
                nxt["todos"] = _clean_todos(patch.get("todos") or [])
                _validate_dependencies(nxt["todos"])
            if "files" in patch:
                nxt["files"] = [str(x)[:500] for x in (patch.get("files") or [])][:100]
            if "risks" in patch:
                nxt["risks"] = [str(x)[:500] for x in (patch.get("risks") or [])][:100]
            if "checks" in patch:
                nxt["checks"] = _clean_checks(patch.get("checks") or [])
            nxt["revision"] = int(current["revision"]) + 1
            nxt["updated_at"] = time.time()
            history = list(current.get("history") or [])
            history.append({"revision": nxt["revision"], "at": nxt["updated_at"],
                            "actor": str(actor)[:80],
                            "changed": sorted(k for k in patch if k != "expected_revision")})
            nxt["history"] = history[-30:]
            _atomic_write(path, nxt)
            return nxt


def _render(artifact):
    todos = artifact.get("todos") or []
    done = sum(1 for t in todos if t.get("status") == "completed")
    header = "plan r%d · %s · %d/%d done" % (
        artifact.get("revision", 0), artifact.get("status", "draft"), done, len(todos))
    lines = [header]
    if artifact.get("title"): lines.append("  %s" % artifact["title"])
    for todo in todos:
        deps = " <- " + ",".join(todo["depends_on"]) if todo.get("depends_on") else ""
        owner = " @" + todo["owner"] if todo.get("owner") not in (None, "", "main") else ""
        lines.append("  %s %s (%s)%s%s" % (
            _MARK.get(todo.get("status"), "[ ]"), todo["content"], todo["id"], owner, deps))
    if artifact.get("risks"):
        lines.append("risks: " + "; ".join(artifact["risks"]))
    if artifact.get("checks"):
        lines.append("checks: " + "; ".join("%s [%s]" % (c["command"], c["status"])
                                             for c in artifact["checks"]))
    if not todos: lines.append("  (empty — supply todos to create tasks)")
    return "\n".join(lines)


class PlanTool(Tool):
    name, tier = "plan", "always"
    description = ("Read or update the durable plan artifact for this run. Include stable task ids, "
                   "dependencies, owners, files, risks, and executable checks. Keep at most one task "
                   "in_progress per owner and do not declare done while tasks/checks remain. Use "
                   "expected_revision for safe parallel edits. `approved=true` is reserved for a "
                   "human/API Approve & build action and does not widen your permissions.")
    schema = {"type": "object", "properties": {
        "title": {"type": "string"}, "intent": {"type": "string"},
        "expected_revision": {"type": "integer"},
        "approved": {"type": "boolean"},
        "files": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "checks": {"type": "array", "items": {"type": "object"}},
        "todos": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "content": {"type": "string"},
            "status": {"type": "string", "enum": list(_VALID)},
            "depends_on": {"type": "array", "items": {"type": "string"}},
            "owner": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "string"}, "note": {"type": "string"}},
            "required": ["content"]}}}}

    def __init__(self, store=None):
        self.store = store or PlanArtifactStore()

    def run(self, args, ctx):
        args = args if isinstance(args, dict) else {}
        scope = _scope(ctx)
        editable = {k: args[k] for k in
                    ("title", "intent", "approved", "files", "risks", "checks", "todos")
                    if k in args}
        if not editable:
            return _render(self.store.get(scope))
        if "todos" in editable and not isinstance(editable["todos"], list):
            return "ERROR: 'todos' must be an array of task objects"
        # The model may draft a plan but cannot self-approve a Build handoff.
        if editable.get("approved") is True and getattr(ctx, "approval_source", "model") == "model":
            return "ERROR: only the user-facing Approve & build action may approve a plan"
        try:
            artifact = self.store.update(scope, editable,
                expected_revision=args.get("expected_revision"),
                actor=getattr(ctx, "approval_source", "model"))
        except (ValueError, RevisionConflict) as exc:
            return "ERROR: %s" % exc
        active = {}
        for todo in artifact["todos"]:
            if todo["status"] == "in_progress":
                owner = todo.get("owner") or "main"
                if owner in active:
                    return (_render(artifact) + "\n(note: keep ONE item in_progress per owner; "
                            "owner %s currently has %s and %s)"
                            % (owner, active[owner], todo["id"]))
                active[owner] = todo["id"]
        return _render(artifact)


def register_plan(registry):
    registry.register(PlanTool())
    return True
