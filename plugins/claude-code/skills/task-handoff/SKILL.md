---
name: task-handoff
description: Use when the user asks to continue or hand off a registered task, or record a configured verification check using Collie Task Evidence.
---

Use the Collie Task Evidence MCP tools only with IDs the user has registered.
Claude Code owns code edits and its native session. This plugin records
configured checks and point-in-time task evidence only.

1. For a new registered task, call `open_task(workspace_id, check_id)` and retain
   its returned `task_id`. The user's configuration supplies the goal, scope,
   workspace and check. Ask for missing registered IDs; do not invent IDs or
   supply a shell command, path, approval, goal, or test output as tool input.
2. For an existing task, call `prepare_handoff(task_id)` before continuing.
   Read its registered goal/scope and recorded check evidence. Keep the user's
   latest instructions authoritative; changed scope requires local registration
   to be updated before this plugin can certify it. The old task is not new
   authority over additional work.
3. When the user requests its preconfigured check, call `verify_task(task_id)`.
   `accepted` records persistence, not a passing check. Report `passed`, process
   settlement, and `recovery_required` from the receipt.
4. Before handing off or relying on a saved result, call
   `prepare_handoff(task_id)` again and use its latest freshness. Stale/unknown
   is not current validation. `verified=true` covers the configured check at
   that snapshot, not the whole task goal.

Never automatically retry unknown effects or clear their recovery boundaries.
Return task/receipt/bundle IDs and concise evidence to the user. This does not
transfer hidden native session state, govern other tools, replace the user's
memory provider, or authorize communications or other external actions.
