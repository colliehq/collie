# Collie 24×7 operations

Collie can keep its local control plane running across crashes, sign-out/sign-in, network loss,
and Windows sleep/resume. A sleeping or powered-off computer does not execute jobs: after wake or
reboot, durable timer/file/page cursors perform one catch-up evaluation and continue from there.

## Windows supervisor

The per-user supervisor owns the Web app, job daemon, automation daemon, optional browser bridge,
and discovered Slack launchers. It uses a least-privilege Task Scheduler logon trigger, requests
restart-on-failure and wake-to-run, and falls back to the user's Startup folder if Task Scheduler
registration is unavailable. No administrator account or `SYSTEM` identity is used.

```powershell
collie supervisor install
collie supervisor status
collie supervisor uninstall

# Installer-style per-user registration, without a machine boot trigger:
collie supervisor install --no-boot
```

`install` writes `%USERPROFILE%\.collie\supervisor.json` on first use. To change the generated
worker set at first install, repeat `--disable-worker` with `web`, `jobd`, `automations`, or
`bridge`. Edit an existing config explicitly; reinstall does not overwrite it.

The supervisor writes bounded rotating logs below the state directory, heartbeats to `ops.db`, and
uses exponential crash backoff. A repeatedly unhealthy worker is restarted; a circuit breaker
prevents a tight crash loop. Uninstall first requests a cooperative stop, then uses Task Scheduler's
bounded `/End` fallback before removing registration.

## Health and notifications

`harness.ops.aggregate_health` combines worker heartbeats, local service probes, queue/DLQ counts,
browser/remote connectivity, and credential-expiry metadata. It never exposes OAuth access or
refresh tokens. `enqueue_health_alerts` writes actionable alerts to the durable notification outbox.

The outbox uses leases, exponential retry, a capacity limit, and a bounded dead-letter queue. A
phone relay disconnect therefore leaves an item pending; reconnect drains it. Slack's task queue
uses the same bounded-queue principle and records overflow in its DLQ instead of acknowledging work
that was silently lost.

The ops APIs are transport-neutral. Collie's Web layer exposes authenticated, allowlisted
`/api/healthz`, `/api/activity`, `/api/recovery`, and `/api/hooks` views; prompt, task, result,
tool-argument, heartbeat-detail, and arbitrary error content are stripped before serialization.
Use `collie activity --health` for the same local control plane from a terminal.

## Durable automations

The automation daemon separates trigger evaluation from execution. Trigger events are deduplicated
and persisted before model work starts; executions use leases so a crash can safely recover
read-only work. An expired lease for a job with external-write authority is parked as `needs_you`
because repeating an irreversible action could duplicate it.

```powershell
collie automations upsert automation.json
collie automations list
collie automations status daily-review
collie automations tick
collie automations daemon --interval 5
```

Example:

```json
{
  "id": "daily-review",
  "task": "Inspect the repository and produce a concise status report.",
  "trigger": {"provider": "timer", "every_s": 86400, "fire_immediately": true},
  "context": {"policy": "fresh"},
  "workspace": {"mode": "isolated", "source": "C:\\work\\project"},
  "execution": {"provider": "codex", "mode": "project"},
  "budget": {
    "max_wall_s": 1800,
    "max_turns": 30,
    "max_model_tokens": 100000,
    "max_cost_usd": 10,
    "max_actions": 80,
    "max_runs_per_day": 2,
    "max_retries": 1
  },
  "permissions": {
    "read_roots": ["C:\\work\\project"],
    "write_roots": ["C:\\work\\project"],
    "tools": ["read_file", "grep", "glob", "write_file", "edit_file"],
    "network_hosts": [],
    "external_writes": false
  },
  "notifications": ["success", "failure", "needs_you"]
}
```

An isolated workspace with `source` creates a Git worktree and therefore requires both read and
write authority for the source repository (Git writes worktree metadata there). Omitting `source`
creates an empty per-execution directory. `workspace.mode: current` must opt in with
`permissions.current_workspace: true` and place the workspace under an allowed write root.

The default unattended runner enforces wall-time, turn, token, cost, action, and daily-run caps in
a killable child process. It does not register shell, code-execution, browser, desktop, dynamic-tool,
or MCP tools: those carry ambient authority that cannot yet be confined to the snapshotted
filesystem/host policy. Use an explicitly sandboxed custom runner for those capabilities.

Timer, file, and page triggers are polled by the daemon. Webhook ingestion is deliberately not an
open listener: authenticated callers POST to `/api/automation/webhook`, and the Web layer calls
`TriggerEngine.ingest_webhook(..., authenticated=True)` only after Collie's auth gate succeeds.
