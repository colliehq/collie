# Collie Wallpaper — one-click Windows installer

`collie-wallpaper.iss` builds a single `collie-wallpaper-setup.exe` that a non-technical user
double-clicks to get the live-desktop wallpaper running and auto-starting at logon — no command line,
no Python knowledge. It's a **thin wrapper over the same `collie wallpaper --install` command** the
lean path uses; the only extra job is bundling a self-contained runtime so the target machine needs
nothing preinstalled.

Two ways to ship the wallpaper, by audience:

| Audience | Path |
|---|---|
| **Developers** | `pip install collie-harness[local]` → `collie wallpaper --install`. No installer. |
| **Everyone else** | this `collie-wallpaper-setup.exe`. Bundles Python + collie + WebView2. |

## Prerequisites (maintainer machine)

- **Inno Setup 6+** — `winget install JRSoftware.InnoSetup`
- Python 3.10+ (only to stage the payload)

## Build steps

```powershell
cd installer

# 1) embeddable Python runtime
#    download python-3.12.x-embed-amd64.zip from python.org, unzip into payload\python\
#    then enable site-packages: in payload\python\python312._pth uncomment the "import site" line
mkdir payload\python
# (unzip the embed zip here)

# 2) install collie + its semantic-memory deps INTO that runtime
payload\python\python.exe -m pip install --target payload\python\Lib\site-packages "collie-harness[local]"
#    (or point pip at a locally built wheel:  ... ..\dist\collie_harness-0.18.0-py3-none-any.whl)

# 3) the WebView2 Evergreen bootstrapper (tiny; installs the runtime only if the machine lacks it)
#    download MicrosoftEdgeWebView2Setup.exe from https://developer.microsoft.com/microsoft-edge/webview2/
#    into payload\

# 4) compile the installer
iscc collie-wallpaper.iss
#    -> Output\collie-wallpaper-setup.exe
```

## What the installer does (and undoes)

On install: lays down `{app}\python` (the bundled runtime, incl. the collie package and the wallpaper
engine source/DLLs shipped in the wheel) → silently ensures the WebView2 runtime → runs
`collie wallpaper --install` (writes the hidden per-user logon autostart) → optionally starts it now.

On first run the engine's `.exe` is compiled once from the shipped C# source via the in-box .NET
Framework `csc` (no .NET SDK needed) and cached — so the installer ships source + DLLs, not a signed
binary.

On uninstall: `collie wallpaper --stop` (clean shutdown via the named quit event) →
`collie wallpaper --uninstall` (removes the autostart) → `{app}` is deleted.

## Notes

- **Per-user, no admin.** `PrivilegesRequired=lowest`; the autostart is a per-user Startup entry, so
  the whole thing installs and runs without elevation.
- **Code signing.** For distribution outside your own machines, sign both the setup `.exe` and (ideally)
  the built `collie-wallpaper.exe` to avoid SmartScreen warnings. Signing is out of scope of the `.iss`.
- **Windows only.** The behind-icons engine needs Progman + WebView2. On macOS/Linux `collie wallpaper`
  degrades to a borderless browser window — no installer needed there.
