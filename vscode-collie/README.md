# Collie for VS Code

Collie is a local-first coding workbench beside the editor. It can use the active file or selection,
accept explicit files and diagnostics, run more than one agent, and open edits in VS Code's native
diff view. The selected Collie worker remains the execution engine; the extension does not require a
cloud model.

## Interaction model

- **No focus stealing by default.** Activation only registers lightweight editor integrations. The
  local runtime starts lazily when a Collie surface is opened. `openOnStartup` is off.
- **Editor context without switching panels.** Use the editor context menu to add a selection,
  file, or current diagnostics. The item appears as a removable chip in Collie and does not reveal
  the sidebar unless `revealOnContextAdd` is enabled.
- **Automatic active context.** The active file, selected lines, and a bounded recent-tab list are
  observed through VS Code APIs. Source text is uploaded to the authenticated loopback runtime as a
  bounded, one-shot attachment—not placed in a URL.
- **Independent agents.** `Collie: New Background Agent` creates another retained editor panel and,
  by default, preserves focus on the current code.
- **Predictable follow-ups.** Enter during a run queues a new turn by default. Set
  `followUpQueueMode` to `steer` to alter the active run when the worker supports it.
  Ctrl/Cmd+Shift+Enter uses the opposite behavior once.
- **Native review.** Diff cards in the workbench and `Collie: Review Changes in Native Diff` open
  VS Code's diff editor. Every requested path is resolved to a real file inside the open workspace.
- **Project map.** The code galaxy opens in a wide editor tab; selecting a star opens the real file.

On VS Code 1.106+ Collie lives in the secondary (right) sidebar. Older supported versions retain an
Activity Bar entrance. The webview keeps its state when hidden.

## Install and use

1. Install the Collie desktop app, put `collie` on `PATH`, or set an absolute `collie.command`.
2. Install `Collie-VSCode.vsix` with **Extensions: Install from VSIX…**.
3. Open a trusted project folder and run **Collie: Open Sidebar**.

Useful commands:

- **Collie: Add Selection to Thread**
- **Collie: Add File to Thread**
- **Collie: Add File Problems to Thread**
- **Collie: New Background Agent**
- **Collie: Review Changes in Native Diff**
- **Collie: Open Project Map**
- **Collie: Restart Server**
- **Collie: Show Server Log**

TODO/FIXME comments also receive an optional **Implement with Collie** CodeLens.

## Settings

| Setting | Default | Meaning |
|---|---:|---|
| `collie.command` | `collie` | CLI name or absolute path; machine scoped. |
| `collie.port` | `0` | Managed local server port; `0` chooses a free port. |
| `collie.provider` | `""` | Optional provider override; machine scoped. |
| `collie.extraArgs` | `[]` | Additional safe `collie web` args; machine scoped. |
| `collie.openOnStartup` | `false` | Explicitly focus Collie after startup. |
| `collie.revealOnContextAdd` | `false` | Reveal Collie after adding editor context. |
| `collie.newAgentPreserveFocus` | `true` | Open an agent without leaving the editor. |
| `collie.followUpQueueMode` | `queue` | Queue a follow-up or steer the active run. |
| `collie.commentCodeLensEnabled` | `true` | Show TODO/FIXME CodeLens actions. |
| `collie.maxSelectionChars` | `12000` | Bound for explicitly attached selections. |

Settings capable of changing the spawned command are machine scoped, managed network-exposure
flags are rejected, and no process starts for an untrusted workspace.

## Develop and package

```bash
code path/to/collie/vscode-collie
# Press F5 in that window to start an Extension Development Host.

cd path/to/collie/vscode-collie
npx --yes @vscode/vsce package
code --install-extension collie-0.4.0.vsix --force
```

The framed workbench accepts messages only from its exact forwarded origin. The host bridge exposes
a small allowlist (open workspace file/map/diff and status); camera, microphone, clipboard-read, and
filesystem webview roots are not granted.
