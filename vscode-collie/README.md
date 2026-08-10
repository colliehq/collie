# Collie for VS Code

Collie — your personal AI operations system — docked in a VS Code sidebar. It's the full
`collie web` GUI (Missions, global Needs You, scoped verification evidence, live diffs, Pack,
Library, Activity, steering, and image upload) embedded in a Webview panel. The extension manages the server for you:
one `collie web` process, your workspace folder as its working directory, on a free port, reachable
through the webview even over WSL / Remote-SSH / Codespaces (via `asExternalUri`).

## Requirements

- The `collie` CLI on your `PATH` (or set `collie.command` to an absolute path).
- VS Code 1.84+.

## Use it

1. Download **`Collie-VSCode.vsix`** from the
   [latest release](https://github.com/colliehq/collie/releases/latest), then run
   `code --install-extension Collie-VSCode.vsix` (or use **Extensions: Install from VSIX…**).
2. Open your project folder in VS Code.
3. Click the **Collie** icon in the Activity Bar (left rail). The panel starts the server and loads
   the GUI. First open takes a second while the server warms up.
4. Ask Collie to fix, build, or explain. It operates on the open workspace.

Commands (⇧⌘P / Ctrl+Shift+P):

- **Collie: Reload Panel** — reload the webview.
- **Collie: Restart Server** — kill and respawn `collie web`.
- **Collie: Open in Browser** — open the same GUI in an external browser.
- **Collie: Show Server Log** — the server's stdout/stderr (troubleshooting).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `collie.command` | `collie` | Path to the collie CLI. |
| `collie.port` | `0` | Server port; `0` auto-picks a free one. |
| `collie.provider` | `""` | Override `COLLIE_PROVIDER` (blank = collie's default). |
| `collie.extraArgs` | `[]` | Extra args appended to `collie web`. |

## Run from source (no packaging)

```bash
code path/to/collie/vscode-collie
# then press F5 → an "Extension Development Host" window opens with Collie loaded
```

## Package a .vsix

```bash
cd path/to/collie/vscode-collie
npx --yes @vscode/vsce package        # -> collie-0.2.0.vsix
code --install-extension collie-0.2.0.vsix
```

## What this is (and isn't)

This embeds collie's **web GUI** in a panel — you get everything the browser GUI has, docked in the
editor. It is **not** the native ACP tool-call/diff rendering; for that (rendered by the editor
itself) use an ACP-native editor like Zed with `collie acp`. This extension is the "Claude-Code-style
panel" experience for VS Code specifically.
