"""plan — a task list the model maintains across turns for multi-step work.

Coding tasks that span many turns drift: the model forgets a sub-step, or declares done with items
unfinished. A plan tool (Claude Code's TodoWrite, Hermes' todo) fixes that — the model writes the
plan, updates statuses as it goes, and the rendered list rides back in each tool result so the next
turn sees exactly what's left. State is per-project, persisted to ~/.collie/plans/<project>.json so a
--continue picks the plan back up.
"""
import json
import os

from .tools import Tool

_VALID = ("pending", "in_progress", "completed", "blocked")
_MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "blocked": "[!]"}
_DIR = os.environ.get("COLLIE_PLAN_DIR") or os.path.expanduser("~/.collie/plans")
_MEM: dict = {}                      # project -> list[{content,status}] (in-process, authoritative)


def _path(project):
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (project or "default"))[:80]
    return os.path.join(_DIR, safe + ".json")


def _load(project):
    if project in _MEM:
        return _MEM[project]
    try:
        with open(_path(project), encoding="utf-8") as f:
            data = json.load(f)
        _MEM[project] = data if isinstance(data, list) else []
    except (OSError, ValueError):
        _MEM[project] = []
    return _MEM[project]


def _save(project, todos):
    _MEM[project] = todos
    try:
        os.makedirs(_DIR, exist_ok=True)
        tmp = _path(project) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _path(project))
    except OSError:
        pass


def _render(todos):
    if not todos:
        return "(plan is empty — pass `todos` to set it)"
    done = sum(1 for t in todos if t.get("status") == "completed")
    lines = ["plan (%d/%d done):" % (done, len(todos))]
    for t in todos:
        lines.append("  %s %s" % (_MARK.get(t.get("status"), "[ ]"), t.get("content", "")))
    left = [t for t in todos if t.get("status") not in ("completed",)]
    if not left:
        lines.append("all items complete.")
    return "\n".join(lines)


class PlanTool(Tool):
    name, tier = "plan", "always"
    description = ("Track a multi-step task. Call with `todos` (an array of {content, status}) to "
                   "SET the whole plan; status is one of pending/in_progress/completed/blocked. Keep "
                   "exactly one item in_progress. Call with no args to read the current plan. Update "
                   "it as you finish steps — don't declare done with items still pending.")
    schema = {"type": "object", "properties": {
        "todos": {"type": "array", "items": {"type": "object", "properties": {
            "content": {"type": "string"},
            "status": {"type": "string", "enum": list(_VALID)}}, "required": ["content"]}}}}

    def run(self, args, ctx):
        todos = args.get("todos") if isinstance(args, dict) else None
        if todos is None:
            return _render(_load(ctx.project))
        if not isinstance(todos, list):
            return "ERROR: 'todos' must be an array of {content, status}"
        clean = []
        for t in todos:
            if not isinstance(t, dict) or not t.get("content"):
                return "ERROR: each todo needs a 'content' string"
            st = t.get("status", "pending")
            if st not in _VALID:
                st = "pending"
            clean.append({"content": str(t["content"])[:200], "status": st})
        n_prog = sum(1 for t in clean if t["status"] == "in_progress")
        _save(ctx.project, clean)
        out = _render(clean)
        if n_prog > 1:
            out += "\n(note: keep just ONE item in_progress at a time.)"
        return out


def register_plan(registry):
    registry.register(PlanTool())
    return True
