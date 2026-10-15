# Install locally in Claude Code

## Install the runtime first

Use a new virtual environment. The old component distributions share the same
Python namespace, so do not overlay this unified installation onto their environment.

```text
python -m venv /absolute/collie-plugin-env
/absolute/collie-plugin-env/bin/python -m pip install /absolute/collie-source
```

Replace the paths with your own. On Windows the environment's executable is
`/absolute/collie-plugin-env/Scripts/python.exe`. `collie-source` is the main
repository root containing `pyproject.toml`, obtained by cloning or extracting
a source archive. Both use the same command; no Git metadata is needed.

Alternatively, install one matching release wheel into that new environment:

```text
/absolute/collie-plugin-env/bin/python -m pip install /absolute/releases/collie_harness-0.31.1-py3-none-any.whl
```

Use the wheel supplied with your source/release version. A plugin-only folder
does not contain the Python runtime. Base `collie-harness` contains both bridges
and has no third-party runtime dependencies. Optional SDK, browser, embedding,
and voice extras are unnecessary for these plugins. Installing a wheel does
not install plugin manifests into a host; retain this source/plugin folder.

## Configure locally before enabling

Copy `config.example.json` to an explicit user-owned configuration file.
Follow the scope and state rules in this folder's `README.md`. In the local
`.mcp.json`, set `command` to the new environment's absolute Python executable
and replace the configuration placeholder with your file's absolute path.
Keep actual state/database files outside source workspaces. Do not commit your
local configuration or machine-specific paths.

The checked-in placeholder intentionally fails startup. Registering a remote
GitHub marketplace alone does not configure a working service. Configure your
local source first; the examples below use it. Neither plugin chooses a user
database or approved command.

## Load this folder directly

```text
claude --plugin-dir /absolute/collie-source/plugins/claude-code
```

For a standalone copy, substitute `/absolute/plugin-leaf`. Invoke
`/collie-task-evidence:task-handoff` with registered IDs. Start a new session or use the
host's reload procedure after editing configuration.

## Use the main repository's marketplace

The main repository includes `.claude-plugin/marketplace.json`. Relative
`source` paths start at the repository root and select its two Claude Code
plugin folders; no extra repository or Python package is needed.

```text
claude plugin marketplace add /absolute/collie-source
claude plugin install collie-task-evidence@collie
```

Install only a plugin whose MCP configuration you completed. A local relative
marketplace source uses local plugin files; reload/start a new session after
edits. Hosted marketplace installs use the host's cached copy and update
procedure. Do not solve configuration by editing cache internals.

A plugin-only archive contains this leaf's manifest, skills, examples, and
instructions. Install the Python runtime separately. A main source archive
contains both catalogs and all four host folders; extract it before using the
local commands above.

[Claude Code marketplace guide](https://code.claude.com/docs/en/plugin-marketplaces)
and [plugin folder loading](https://code.claude.com/docs/en/plugins),
checked 2026-10-14. Public source hosting does not imply official-directory
approval. Offline protocol/path tests do not establish a live native Claude
Code integration pass.
