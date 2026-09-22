# Lifecycle hooks

Collie hooks are deterministic host policy around the agent loop. They are not
tools the model can call, and they never widen a run's permission leash.

Place user hooks in `~/.collie/hooks.json`. A trusted repository may also
declare `.collie/hooks.json`, but Collie will not load it until both the
workspace and the exact hook-file hash have been reviewed. Editing a trusted
hook changes its hash and returns it to pending review.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "edit_file|write_file",
        "hooks": [
          {
            "type": "command",
            "command": "python .collie/hooks/format_changed.py",
            "timeout": 30
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python .collie/hooks/verify_done.py",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

The command receives a JSON event on stdin. Exit zero with no output to allow
the event, or print a JSON decision:

```json
{"decision":"block","reason":"pytest has not passed after the last edit"}
```

`PreToolUse`, `UserPromptSubmit`, `Stop`, and `TaskCompleted` fail closed when a
hook rejects, crashes, or times out. Observer hooks such as `PostToolUse` are
reported but do not turn a completed tool call into an authorization decision.
A hook may also return `additionalContext`; Collie adds the bounded string to
the next model turn as trusted host context.

Events emitted by the core loop are `SessionStart`, `UserPromptSubmit`,
`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop`, and `SessionEnd`.
Mission/task orchestration can additionally emit `TaskCreated`,
`TaskCompleted`, and `Notification` through the same dispatcher.

Hooks inherit the local process environment and can execute commands. Treat
them as code: keep them small, review changes, and never place secrets directly
in their definitions.
