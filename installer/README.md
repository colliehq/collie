# Collie — installers

`collie.iss` builds **`Collie-Setup.exe`**: a single file a non-technical user double-clicks to get
Collie — a real desktop app with a Start-menu/desktop icon, no Python, no terminal, no `pip`, no PATH
surgery. Everything ships inside: an embeddable CPython with `collie-harness[local]` (semantic memory
included), the WebView2-based desktop window and live-wallpaper engine, the browser extension, and
the WebView2 bootstrapper.

| Audience | Path |
|---|---|
| **Everyone** | `Collie-Setup.exe` — bundles Python + collie + WebView2. From the [releases page](https://github.com/colliehq/collie/releases). |
| **Developers** | `pip install collie-harness[local]` → `collie setup`. No installer. |

## What's in this directory

| File | Role |
|---|---|
| `collie.iss` | The Inno Setup script: branded wizard, a custom card-style language page (33 languages, Simplified Chinese up front), tasks, uninstall. |
| `build.ps1` | **The one command to build the exe.** Reads the version, generates art + language data, stages the payload, compiles. |
| `build_payload.ps1` | Recreates `payload/` — the embeddable-Python runtime with collie installed. Called by `build.ps1`; idempotent. |
| `make_art.py` | Generates the wizard's star-map branding BMPs from the logo (reproducible). |
| `gen_langs.py` | Emits `languages.iss` + `langdata.iss` (the `[Languages]` section and the chip/dropdown data) from the `.isl` files present. Edit the `CHIPS`/`MORE` lists here to change which languages are offered. |
| `gen_zhtw.py` | Regenerates the webui's Traditional-Chinese dict from the Simplified one via OpenCC (maintainer tool). |
| `fetch_languages.py` | Downloads Inno's unofficial upstream translations into `lang/` and test-compiles each. Run once when adding new languages. |
| `lang/` | Vendored `.isl` translations not bundled with Inno (committed so builds are hermetic). |

Generated/large paths (`payload/`, `Output/`, `art/`, `languages.iss`, `langdata.iss`) are
`.gitignore`d — `build.ps1` recreates them.

## Build

```powershell
# prerequisites (maintainer/CI machine):
#   - Inno Setup 6+       winget install JRSoftware.InnoSetup
#   - a system Python with Pillow (make_art) — pip install pillow
#   - network access (build_payload downloads the embeddable CPython + WebView2 on first run)

powershell -File installer\build.ps1                 # -> installer\Output\Collie-Setup.exe
powershell -File installer\build.ps1 -CleanPayload   # also rebuild the bundled runtime
```

The version comes from `harness/__init__.py` (single source of truth) and is passed to `iscc` as
`/DAppVer`. CI does the same in `.github/workflows/release.yml`, triggered by pushing a `v*` tag.

## What the installer does

- Lays down `{localappdata}\Programs\Collie\python` (the bundled runtime, per-user, no admin).
- Silently ensures the WebView2 runtime (needed by the desktop window).
- Applies the language you picked to Collie itself (`collie config LANG <code>`), so the first launch
  is already in your language.
- Optional tasks: the live star-map wallpaper and the real-browser bridge, each auto-starting at
  logon.
- Start-menu + desktop shortcuts to `collie app` (the native window), plus a *Collie Settings*
  shortcut.

On uninstall it stops the wallpaper, removes both logon autostarts, and deletes `{app}`.

## macOS — `build_mac.sh`

```bash
bash installer/build_mac.sh                 # build + ad-hoc sign (local use)
bash installer/build_mac.sh --sign          # sign with the best identity in the keychain
bash installer/build_mac.sh --sign --dmg    # …and wrap it in Collie-<ver>.dmg
bash installer/build_mac.sh --sign --dmg --notarize <profile>
```

**Why a bundle when `pip install collie-harness` already works: identity.** macOS attaches TCC
permissions to the *application*, so a pip install makes `collie record` ask for Screen Recording on
behalf of your **terminal** — which then holds blanket screen access forever, and System Settings
lists "Terminal" rather than Collie. The PyObjC desktop wallpaper has the same problem in reverse:
unbundled, it appears in the window list as "Python". The bundle fixes both.

Distribution needs a **Developer ID Application** certificate — an *Apple Development* cert signs
for local use only, and both Gatekeeper and notarisation reject it (the script says so and carries
on, so a local build still works). Create one in Xcode → Settings → Accounts → Manage Certificates
→ **+** → Developer ID Application; only the team's Account Holder can. Then:

```bash
xcrun notarytool store-credentials collie --apple-id <id> --team-id <team> --password <app-specific>
```

This builds the **developer** bundle: it launches the collie already installed on the machine.
Bundling a private CPython — what `build_payload.ps1` does on Windows, for users with no Python —
is a separate step and the thing standing between this and a real one-click macOS download.

## Notes

- **Per-user, no admin.** `PrivilegesRequired=lowest`; autostarts are per-user Startup entries.
- **The desktop engine `.exe`** is compiled once on first run from the shipped C# source via the
  in-box .NET Framework `csc` (no .NET SDK needed).
- **Code signing** is out of scope of the `.iss`. For distribution outside your own machines, sign
  the setup `.exe` to avoid SmartScreen warnings.
- **Windows only.** On macOS/Linux, `pip install collie-harness` + `collie` is the path; the desktop
  window degrades to the browser GUI and the wallpaper to a borderless window.
