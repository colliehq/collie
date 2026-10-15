# Collie Local Memory for Codex

A separate native-consumer plugin for Collie's own SQLite memory. Install the
base `collie-harness` runtime; optional model SDKs, browser automation, speech,
desktop frameworks, embeddings, and EchoMem are not required.
It exposes only `recall` and `propose`. Codex retains native execution.

Install the main repository's source or its single `collie_harness` wheel into
a new virtual environment. The [installation guide](INSTALL_LOCAL.md) covers
source archives, the whole-repository marketplace, and this folder on its own.
This folder contains no runtime copy; the unified wheel contains the service.

Before enabling this folder as a local Codex plugin:

1. Copy `config.example.json` to a user-owned absolute configuration path.
   Explicitly choose one database in an existing user-owned directory and a
   stable `store_id` for that database. No default user database is discovered.
2. Replace the example workspace UUID with your own canonical UUID. Map each
   registered `workspace_id` to a unique `workspace:<UUID>` namespace. Paths
   and folder basenames grant no memory scope; `global` is never granted.
3. In `.mcp.json`, set `command` to the runtime environment's absolute Python
   executable and replace `REPLACE_WITH_ABSOLUTE_USER_MEMORY_CONFIG_PATH` with
   your file. The placeholder intentionally refuses startup.
4. Install/enable the local-marketplace plugin in native Codex. Invoke `$local-memory` with the
   registered workspace ID. Existing native sessions need their host's normal
   plugin/MCP reload procedure.

```text
python -m harness.memory_bridge --config /absolute/user/memory-config.json
```

The stdlib stdio MCP frame and memory implementation are internal modules of
the same runtime. Native tool arguments select only registered workspace IDs
and bounded query/claim text. Results carry opaque backend/store references;
internal SQLite integer IDs are not the public contract.

Recall uses SqliteMemory's offline BM25-only mode. It explicitly passes only
the configured scope, checks physical project/scope/status metadata, excludes
pending/rejected/invalidated/global rows, and suppresses recognized credential
text in content or provenance. Recall still updates the backend's access
counters. Proposals are committed as `proposed` with server-generated producer
metadata and independently checked for commit visibility. Failed writes are
not acknowledged; this service never promotes claims. Reviewing them remains
a separate trusted local-host/user operation.

This does not remap old basename namespaces, import/migrate another database,
or dual-write to EchoMem. Opening the selected store uses the existing
SqliteMemory initialization/schema compatibility behavior. Existing rows in
other namespaces remain unavailable through this consumer. Keep `store_id`
bound to the same database when relocating it. This is an application scope
boundary, not OS isolation against the local account editing its database.

Only temporary SQLite/framing fixtures were exercised during implementation.
They do not establish a live native Codex integration pass. No actual user
memory was read, exported, or moved.
