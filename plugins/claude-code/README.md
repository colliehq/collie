# Collie Task Evidence for Claude Code

This local plugin adds the same three runtime-backed MCP tools as the Codex
package: `open_task`, `verify_task`, `prepare_handoff`. Claude Code retains its
native edits and session; no native model, API, or memory backend is launched by
the bridge. The base `collie-harness` runtime provides framing and operations;
this folder is only the host-specific manifest and workflow content.

Install the main repository's source or its single `collie_harness` wheel into
a new virtual environment. Optional model SDKs, browser automation, speech,
desktop frameworks, and embeddings are not required. The
[installation guide](INSTALL_LOCAL.md) covers source archives, the main
repository's marketplace, and direct `--plugin-dir` loading of this folder.
This folder contains no runtime copy; the unified wheel contains the service.

Install the Python runtime, copy `config.example.json` to a user-owned
absolute path outside every source workspace, and replace the goal, scope,
checks and paths. `state_dir` must also be outside registered workspaces.
In `.mcp.json`, replace `command` with the installed environment's absolute
Python executable and replace `REPLACE_WITH_ABSOLUTE_USER_CONFIG_PATH` with
the configuration file. The placeholder deliberately cannot start a service.
Then load this local plugin folder in native Claude Code and invoke
`/collie-task-evidence:task-handoff` with registered IDs or a returned task ID.

```text
python -m harness.evidence_bridge --config /absolute/user/config.json
```

The registered command runs project code as the local user via the existing
owned-process verifier; this is not an OS sandbox or an all-tools policy hook.
Only preconfigure checks you approve. Configuration changes require a service
restart and invalidate old task registrations. Fingerprints conservatively
cover the configured bytes: source edits make results stale, while incomplete
or linked snapshots are unknown. The bridge does not accept model-authored
commands, cwd, approvals, goals, or fabricated test results.

An unknown check effect remains fenced for that physical workspace even if
another task is opened. Receipt persistence and fence retirement are atomic;
failed writes are not acknowledged. The serial stdio server waits for each
configured check or timeout; queued cancellation notifications do not interrupt
it. No reconciliation or automatic replay is provided. Native edits remain
outside this lease, so reported freshness is a point-in-time observation.

Test this plugin in native Claude Code, not Collie's constrained native runner
that disables/limits these components. Passing core/stdio fixtures does not
establish a live native integration pass.
