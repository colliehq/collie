# Install locally in Codex

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

## From the whole main repository

The main repository includes `.agents/plugins/marketplace.json`. Its paths
resolve from the repository root, pointing at `./plugins/codex` and
`./plugins/codex-memory`; they do not start at `.agents/plugins/`.

```text
codex plugin marketplace add /absolute/collie-source
```

Restart the desktop client, select the `collie` source, and install/enable
`collie-local-memory`. Invoke `$local-memory` with registered IDs. Enable only a plugin
whose MCP configuration you completed. After changing a configured source,
refresh/reinstall the host's installed copy and restart/reload as required;
source edits do not promise to update an already cached installation.

## From this plugin folder alone

Install the runtime from the main source or a wheel as above. Copy
`marketplace.example.json` to `.agents/plugins/marketplace.json` **inside
this plugin folder**, creating those directories first. Its `source.path="./"`
is correct only when this folder itself is the marketplace root.

```text
codex plugin marketplace add /absolute/plugin-leaf
```

Select the `collie-local-memory-local` source and install `collie-local-memory`. Do not copy this
leaf catalog unchanged to the main repository root.

The main repository's ZIP is a multi-plugin marketplace source, not a
single-plugin import ZIP. A single-plugin ZIP must contain only this leaf's
files; the Python runtime remains a separate installation.

[OpenAI Docs: package and load plugins](https://developers.openai.com/plugins/build/plugins),
checked 2026-10-14. These instructions describe local clients, not a public
directory approval or cloud execution. Offline protocol/path tests do not
establish a live native Codex integration pass.
