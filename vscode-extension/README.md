# Collie for VS Code

A thin extension that runs Collie's polished chat GUI in a VS Code panel. It spawns `collie web`
scoped to your open workspace and shows it in a Webview (port-forwarded via `asExternalUri`, so it
works locally and over Remote-SSH / Codespaces / WSL). The Python agent loop is unchanged.

## Try it (dev)

```bash
# 1. collie CLI must be installed and on PATH (or set collie.command in Settings)
which collie   # e.g. ~/.local/bin/collie

# 2. open this folder in VS Code and press F5 (Run Extension) — a dev host opens
code path/to/collie/vscode-extension
```

In the dev host: **Cmd/Ctrl-Shift-P → "Collie: Open Chat"**. The Collie chat opens beside your
editor, operating on the workspace folder. "Collie: Restart Server" reloads it.

## Package (share / install)

```bash
npm i -g @vscode/vsce
cd vscode-extension && vsce package        # -> collie-0.1.0.vsix
code --install-extension collie-0.1.0.vsix
```

## Settings

- `collie.command` — path to the CLI (default `collie`).
- `collie.provider` — `COLLIE_PROVIDER` for the server (default `anthropic-oauth`; `mock` = $0).
- `collie.port` — preferred port (auto-falls-back if busy).

## How it relates to the other surfaces

- **This extension** — the GUI in a VS Code panel (thin client over `collie web`).
- **`collie acp`** — Agent Client Protocol; native in Zed/JetBrains/neovim, or VS Code via a
  community ACP client. Renders the verification gate + diffs with the editor's own primitives.
- **`collie tui` / `collie -p`** — terminal.

The extension is deliberately thin (Claude-Code model): Collie's core stays a CLI, so every
surface reuses the same loop.
