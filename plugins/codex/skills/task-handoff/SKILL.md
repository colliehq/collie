---
name: task-handoff
description: Use when the user asks to continue or hand off a registered task, or record a configured verification check using Collie Task Evidence.
---

Use the Collie Task Evidence MCP tools only with IDs the user has registered.
The native Codex conversation owns code edits and its native session. This
plugin records configured checks and point-in-time task evidence only.

1. For a new registered task, call `open_task(workspace_id, check_id)` and retain
   its returned `task_id`. Goal, scope, workspace and check recipe come from the
   user's configuration. Ask for missing registered IDs; do not invent IDs or
   supply a shell command, path, approval, goal, or test output as tool input.
2. For an existing task, call `prepare_handoff(task_id)` before continuing.
   Read its registered goal/scope and recorded check evidence. Keep the user's
   latest instructions authoritative; if they change this registered scope,
   explain that the local configuration must be updated before this plugin can
   certify the new scope. Do not silently treat the old task as new authority.
3. When the user requests its preconfigured check, call `verify_task(task_id)`.
   `accepted` records persistence; it does not mean the check passed. Report
   `passed`, process settlement, and `recovery_required` from the receipt.
4. Before handing off or relying on a stored result, call
   `prepare_handoff(task_id)` again. Use its latest freshness. Stale or unknown
   evidence is not current validation; `verified=true` covers this check only
   at its observed snapshot, not completion of the whole goal.

Never automatically retry an unknown effect or clear its recovery boundary.
Return the task/receipt/bundle IDs and concise evidence to the user. This does
not transfer hidden native session state, govern other tools, replace the
user's memory provider, or authorize communications or other external actions.
