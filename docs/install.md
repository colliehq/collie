# Install

Pick the path that matches you. Windows and Apple-silicon Mac users can install a packaged app; a
Linux user or developer can use **pip**.

## Windows — one-click installer (for everyone)

The friendliest path: no Python, no terminal, no configuration.

1. Download **`Collie-Setup.exe`** from the [latest release](https://github.com/colliehq/collie/releases/latest).
   Windows installers are code-signed through the [SignPath Foundation](https://signpath.org/)'s free
   code-signing program for open-source projects.
2. Double-click it. The installer itself is localized in Simplified/Traditional Chinese, English,
   Spanish, and many other languages. Collie's workbench currently offers English, Simplified
   Chinese, and Traditional Chinese.
3. Click through. When it finishes, open **Collie** from the Start menu or the desktop icon.
4. On first launch, **pick a brain**: an existing Claude, Codex, or Grok subscription connects in one
   click, or paste an API key. That's it.

Everything Collie needs ships inside the installer — an embeddable Python, the semantic-memory
engine, the desktop window (WebView2), and the browser extension. It installs per-user (no admin
prompt) and cleanly uninstalls from *Add or remove programs*.

!!! tip "Optional extras during setup"
    Two checkboxes let you turn on the **live star-map wallpaper** and the **real-browser bridge**
    at logon. Both are off by default and covered in [The desktop app](desktop.md).

## macOS — signed app

1. Download **`Collie-arm64.dmg`** from the [latest release](https://github.com/colliehq/collie/releases/latest).
2. Open the disk image and drag **Collie** to Applications.
3. Open Collie and choose a model provider. Release builds are signed with an Apple Developer ID and
   notarised; the packaged app currently targets Apple silicon and macOS 12 or newer.

## Linux and developers — pip / uv

The core is stdlib-only, so the base install is tiny.

```bash
# from source (PyPI publish is planned):
git clone https://github.com/colliehq/collie && cd collie
pip install -e ".[local,dev]"
```

Then let Collie finish provisioning itself:

```bash
collie setup     # installs optional deps, pre-downloads the memory model, picks a provider
collie           # opens the terminal chat
```

### Optional extras

| Extra | What it adds |
|---|---|
| `local` | Semantic memory — granite-107m via onnxruntime (~55 MB, multilingual). What `collie setup` installs. |
| `tui` | The rich terminal chat (`collie tui`). |
| `browser` | Playwright — only for `collie browser-bridge --browser` (a managed Chromium with the extension preloaded, for CI / no-login use). The everyday real-browser control uses your own Chrome + the extension and needs no extra. |
| `search` | Keyless web search (DuckDuckGo). |
| `acp` | Editor integration over the Agent Client Protocol (`collie acp`). |
| `fastembed` | Opt-in jina-v3 and other fastembed models. |

```bash
pip install "collie-harness[local,tui,search]"
```

!!! note "Semantic memory is optional"
    Without the `local` extra, memory runs on **BM25 keyword recall** — it works out of the box, it
    just isn't semantic. Collie never silently falls back to a low-quality embedder.

## Verify it works

```bash
collie selftest      # $0 deterministic end-to-end: mock model, real tools + memory + dashboard
```

If that prints a green verified run, you're set → **[Quickstart](quickstart.md)**.
