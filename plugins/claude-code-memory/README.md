# Collie Local Memory for Claude Code

This independent plugin exposes only `recall` and pending `propose` over an
explicitly chosen Collie SQLite database. It uses the base `collie-harness`
runtime; optional model SDKs, browser automation, speech, desktop frameworks,
embeddings, and EchoMem are not required.

Install the main repository's source or its single `collie_harness` wheel into
a new virtual environment. The [installation guide](INSTALL_LOCAL.md) covers
source archives, the main repository's marketplace, and direct `--plugin-dir`
loading of this folder. This folder contains no runtime copy; the unified
wheel contains the service.

Copy `config.example.json` to a user-owned absolute path. Choose one absolute
database path in an existing directory, assign its stable `store_id`, and map
each registered workspace ID to a unique canonical `workspace:<UUID>`
namespace. Replace the sample UUID; do not derive scope from a folder basename.
In `.mcp.json`, use the environment's absolute Python executable and replace
`REPLACE_WITH_ABSOLUTE_USER_MEMORY_CONFIG_PATH` with that configuration file.
Then load this folder in native Claude Code and invoke
`/collie-local-memory:local-memory` with a registered workspace ID.

```text
python -m harness.memory_bridge --config /absolute/user/memory-config.json
```

The service uses existing SqliteMemory initialization and offline BM25 recall.
It grants only the exact registered namespace, checks project/scope/status
metadata, never includes global or pending claims, and filters recognized
credential-like text from both content and provenance. Recall updates backend
access counters. `propose` creates only unreviewed `proposed` rows with
server-generated source/evidence/provenance; an independent connection checks
commit visibility before acknowledgement. Only a separate trusted local
reviewer can promote them; this bridge provides no such tool.

Tool arguments cannot select db/cwd/scope/status/source or forge producer
metadata. References are opaque `{backend, store_id, opaque_id}`, not raw
SQLite IDs. Errors mean refused/unavailable, never successful empty recall or
permission to broaden scope. Keep the configured store identity bound to the
same database when moving it. The local account can still edit its files;
this interface is not OS isolation.

No database is auto-discovered, imported, remapped from old basename scopes,
exported, migrated to another service or dual-written to EchoMem. Existing
rows outside configured namespaces remain unavailable. This is a native
plugin consumer, not a Collie runner setup that disables plugins. Implementation
tests used only fixed temporary SQLite data; no live Claude Code integration
or real user-memory export is claimed.
