# Architecture and distribution

Collie is one product, one main repository, and one `collie-harness` Python
distribution. The desktop, web, terminal, editor, and connected-device surfaces
assemble the same runtime. Internal module boundaries let capabilities be used
independently without turning each implementation detail into another package
or repository.

## Internal boundaries

| Area | Responsibility | Boundary to preserve |
|---|---|---|
| Runtime and application ports | Task/session state, ownership, checks, recovery, and service binding | A low-level service does not import and start the full CLI merely to obtain a factory. |
| Memory | SQLite storage, claim status, retrieval, and optional embeddings | Source, scope, and claim review stay explicit; a model proposal is not a verified fact. |
| Models and native runners | Provider transports and owned native-agent subprocesses | Provider selection and native session references remain specific to the runner. |
| Tools and security | Local effects, permissions, policy, and audit | Guarantees apply to the execution paths actually controlled and tested. |
| Workflows and Library | Missions, scheduling, task composition, reviewed skills and connections | Installing content does not itself grant execution authority. |
| Connections and devices | Channel transports, pairing, relay, and optional connected mode | Pairing, accounts, and credentials are explicit setup, not import-time discovery. |
| Frontends | CLI, web, desktop, editor, and phone-facing interaction | UI is an application assembly, not a dependency of a narrow stdio service. |
| Evaluation | Fixed fixtures, benchmark utilities, and verification receipts | Test success records a named check and scope, not universal runtime correctness. |

These areas share the `harness` namespace inside the unified distribution.
Application assembly is explicit for a full CLI/worker; narrow plugin bridges
call their application services directly. Internal boundaries do not promise
that every module can run without its documented optional dependencies.

## Two capabilities, two native hosts

| Capability | Runtime module | Codex folder | Claude Code folder |
|---|---|---|---|
| Task Handoff and Evidence | `harness.evidence_bridge` | `plugins/codex` | `plugins/claude-code` |
| Local Memory | `harness.memory_bridge` | `plugins/codex-memory` | `plugins/claude-code-memory` |

The four folders contain host manifests, skills, examples, and documentation.
They do not contain copies of the Python implementation. Both capabilities use
the same base runtime installation and stdio MCP transport. A user may enable
either capability without enabling the other.

Task Evidence accepts only registered workspace/check IDs or its returned task
IDs. The user configures the actual command, workspace, scope, timeout, and
state location. It records a configured check and recomputes scoped freshness
at handoff. Changed bytes are stale; incomplete observation or an unresolved
check effect can be unknown. A persistence failure is not acknowledged as a
successful receipt. Native edits remain outside its lease, so freshness is a
point-in-time observation. The check runs as the local user, not in an OS
sandbox. It does not migrate hidden native session state or certify other host
tools.

Local Memory uses one explicitly chosen SQLite database and a stable store ID.
Each registered workspace maps to a unique `workspace:<UUID>` namespace. Its
tools offer recall and pending proposal, not promotion, export, or arbitrary
database/scope selection. BM25 recall excludes pending and out-of-scope facts
and filters recognized credential-like content. This is application scoping,
not OS isolation from the database owner. It does not import another provider,
dual-write to EchoMem, or change the user's default memory service.

## Dependencies and installation

Base installation contains the runtime modules and application assets, with no
third-party runtime dependencies. The two bridges require no model SDK,
browser binary, embedding download, or voice engine. Optional extras add only
the dependencies for selected workflows:

| Extra | Purpose |
|---|---|
| `claude`, `codex` | Optional provider/native SDK integrations |
| `browser` | Playwright for the optional managed-browser path |
| `local`, `fastembed` | Optional semantic-memory backends |
| `speech` | Local transcription dependencies |
| `remote`, `online`, `extensions` | Optional cryptographic connection/signature features |
| `desktop`, `tui`, `acp`, `search` | Selected desktop, terminal, editor, or search integrations |

Extras do not remove Python modules from the wheel. Installing a provider SDK
also does not authorize or start a provider session. Browser/model downloads
and account setup are separate operations. The base package includes
integration code rather than model weights, vendor CLIs, connected accounts,
or a running local model server. Tool modules still need the application or
device setup documented for their execution surface.

For a source checkout or extracted source archive, create a **fresh** virtual
environment, then install its root:

```text
python -m venv /absolute/collie-env
/absolute/collie-env/bin/python -m pip install /absolute/collie-source
```

On Windows use `/absolute/collie-env/Scripts/python.exe`. No Git metadata is
needed. A supplied `collie_harness-0.31.1-py3-none-any.whl` can be installed
instead of the source root; it is a complete runtime, not a compatibility
metapackage. Keep a plugin source folder separately, because installing the
Python wheel does not install or enable a host plugin. Use a new environment
instead of mixing this package with old component distributions that share
the same namespace.

## Catalogs and local plugin setup

The main repository provides two catalogs:

- `.agents/plugins/marketplace.json` lists the two Codex folders.
- `.claude-plugin/marketplace.json` lists the two Claude Code folders.

All source paths resolve from the main repository root, not the directory
holding the catalog. The same structure works after extracting a source ZIP,
including its hidden manifest/catalog directories.

First install the runtime, copy the desired configuration example to a
user-owned location, then fill the local plugin's `.mcp.json` with the absolute
Python and configuration paths. Configure only commands and databases you
intend to expose. The templates deliberately contain unusable placeholders.
Register the configured local repository as a marketplace, or load a Claude
Code leaf with `--plugin-dir`. See the self-contained installation guides:
[Codex evidence](../plugins/codex/INSTALL_LOCAL.md),
[Codex memory](../plugins/codex-memory/INSTALL_LOCAL.md),
[Claude evidence](../plugins/claude-code/INSTALL_LOCAL.md), and
[Claude memory](../plugins/claude-code-memory/INSTALL_LOCAL.md).

The Codex leaves also retain catalog examples with `source.path="./"` for a
standalone leaf used as the marketplace root. Those examples must not replace
the main catalog unchanged. A whole-repository ZIP is a multi-plugin source;
it is not a single-plugin import ZIP. Leaf ZIPs contain one plugin, with the
Python runtime installed separately.

Publishing this repository does not configure a user's local service or imply
approval in an official plugin directory. Offline stdio tests establish the
tested protocol and fixture behavior, not a live native-host integration or
control over all host actions. The host keeps its execution and session.

Catalog/layout references checked 2026-10-14:
[OpenAI Docs](https://developers.openai.com/plugins/build/plugins),
[Claude marketplace](https://code.claude.com/docs/en/plugin-marketplaces), and
[Claude plugin loading](https://code.claude.com/docs/en/plugins).
