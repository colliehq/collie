# Install

Pick the path that matches you. Windows and Apple-silicon Mac users can install a packaged app; a
Linux user or developer can use **pip**.

## Windows — one-click installer (for everyone)

The friendliest path: no Python, no terminal, no configuration.

1. Download **`Collie-Setup.exe`** from the [latest release](https://github.com/colliehq/collie/releases/latest).
   Release installers are signed with Azure Artifact Signing; the release workflow verifies the
   Windows trust chain before publishing the file.
2. Double-click it. The installer itself is localized in Simplified/Traditional Chinese, English,
   Spanish, and many other languages. Collie's workbench currently offers English, Simplified
   Chinese, and Traditional Chinese.
3. Click through. When it finishes, open **Collie** from the Start menu or the desktop icon.
4. On first launch, **pick a brain**: an existing Claude, Codex, or Grok subscription connects in one
   click, or paste an API key. That's it.

Everything Collie needs ships inside the installer — an embeddable Python, the semantic-memory
engine, the desktop window (WebView2), and the browser extension. It installs per-user (no admin
prompt) and cleanly uninstalls from *Add or remove programs*. During an upgrade, Setup keeps the
previous bundled runtime until the new install succeeds and restores it if copying is cancelled or
fails; your `~/.collie` settings, memory, and missions are outside that replacement boundary.

!!! tip "Optional extras during setup"
    Two checkboxes let you turn on the **live desktop** (a clock, weather and your apps behind your
    icons) and the **real-browser bridge**
    at logon. Both are off by default and covered in [The desktop app](desktop.md).

## macOS — signed app

1. Download **`Collie-arm64.dmg`** from the [latest release](https://github.com/colliehq/collie/releases/latest).
2. Open the disk image and drag **Collie** to Applications.
3. Open Collie and choose a model provider. Release builds are signed with an Apple Developer ID and
   notarised; the packaged app currently targets Apple silicon and macOS 12 or newer.

## Source, archives, and developers — pip

The unified `collie-harness` package includes the runtime. Base installation
has no third-party runtime dependencies. Start with a new environment instead
of mixing it with the old experimental component distributions.

```text
# run from a cloned repository or extracted source archive root:
python -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/collie --help
```

On Windows use `.venv/Scripts/python.exe` and `.venv/Scripts/collie.exe`.
A supplied `collie_harness-0.31.1-py3-none-any.whl` can replace `.` in the
install command; no sibling component wheels or Git metadata are needed.
For editable development, install `-e ".[dev]"` into the new environment.
Extras add dependencies rather than removing modules from the wheel.

For the full application, optionally provision the workflows you need:

```text
collie setup     # can install optional dependencies and download a memory model
collie           # opens the terminal chat
```

The native Task Evidence and BM25 Local Memory plugins use only the base
installation and do not require `collie setup`. Configure them explicitly
before loading their source folder; a Python wheel alone does not install or
enable host plugins. See [architecture and plugin installation](architecture.md).

### Optional extras

| Extra | What it adds |
|---|---|
| `local` | Semantic memory — granite-107m via onnxruntime (~55 MB, multilingual). What `collie setup` installs. |
| `tui` | The rich terminal chat (`collie tui`). |
| `browser` | Playwright — only for `collie browser-bridge --browser` (a managed Chromium with the extension preloaded, for CI / no-login use). The everyday real-browser control uses your own Chrome + the extension and needs no extra. |
| `search` | Keyless web search (DuckDuckGo). |
| `acp` | Editor integration over the Agent Client Protocol (`collie acp`). |
| `fastembed` | Opt-in jina-v3 and other fastembed models. |
| `claude`, `codex` | Optional model/native SDK integrations; unnecessary for plugins loaded by an already running native host. |
| `speech` | Local transcription dependencies; model downloads remain separate. |

```bash
# run from the cloned checkout above
pip install -e ".[local,tui,search]"
```

!!! note "Semantic memory is optional"
    Without the `local` extra, memory runs on **BM25 keyword recall** — it works out of the box, it
    just isn't semantic. Collie never silently falls back to a low-quality embedder.

## Staying up to date

Open **Settings → General → Updates** and press **Check for updates**, or turn on *Check for
updates automatically* to have Collie ask GitHub about once a day (off by default; see
[Privacy](privacy.md)). When a newer release exists, a small dot appears on the Settings button and
the Updates card shows its release notes.

- **Windows installer copies** get **Install and restart**: Collie downloads the release you were
  shown, checks its published digest and Authenticode signature, closes, runs Setup silently and
  starts the same pieces again. A task that is running at that moment is interrupted. If a newer
  release appears between reading the notice and pressing Install, nothing is installed and the
  card asks you to check again.
- **The macOS app** points to the release page: download the new `Collie-arm64.dmg` and replace
  Collie in Applications (the app puts no `collie` command on your PATH).
- **Homebrew** copies show `brew upgrade collie`; **pip** installs show `collie update --yes`.

From a terminal, `collie update` reports the latest release and `collie update --yes` installs it.

After an update, reload the Collie browser extension once: open `chrome://extensions` and press the
reload arrow on the Collie card. Chrome keeps running an unpacked extension until it is reloaded, so
until then the browser tools use the old one. *Settings → General* says so while Chrome is still
running an older copy.

## Verify it works

```bash
collie selftest      # $0 deterministic end-to-end: mock model, real tools + memory + dashboard
```

If that prints a green verified run, you're set → **[Quickstart](quickstart.md)**.
