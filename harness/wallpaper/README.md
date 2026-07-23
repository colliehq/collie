# collie desktop wallpaper engine (Windows)

Renders collie's live code star-map as the desktop wallpaper (behind the icons) with a
clickable/typable chat, driven by the local `collie web` server.

## Use it (the easy way)

```powershell
collie wallpaper            # build the engine on first use, start the server, attach the wallpaper
collie wallpaper --install  # auto-start it at every logon (hidden; no console window)
collie wallpaper --stop     # clean shutdown (never -Force — that orphans WebView2 COM)
collie wallpaper --uninstall
```

`collie wallpaper` resolves everything at runtime — the interpreter, the install location, and a free
port — so it works from a source checkout, a `pip`/`pipx` install, or the one-click installer
(`installer/`), on any machine. The engine `.exe` is compiled once from `Program.cs` via the in-box
.NET Framework `csc` (**no .NET SDK needed**) and cached here.

## Requirements
- Windows 10/11 with the **WebView2 runtime** (ships with Edge; already present on most machines —
  `collie wallpaper` checks and prints the `winget install Microsoft.EdgeWebView2Runtime` hint if not).

## Build the engine manually (optional)
```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1   # -> collie-wallpaper.exe
```

## What it does
- Pins a WS_CHILD WebView2 window under Progman, z-ordered below the icon layer (raised-desktop
  compatible; re-asserted on WM_WINDOWPOSCHANGING so it can never cover the icons).
- Reads its URL from `COLLIE_WALLPAPER_URL` (the port is chosen at runtime by `collie wallpaper`, so
  it never collides with a busy 8787); falls back to `http://127.0.0.1:8787/wallpaper` for a manual run.
- Forwards desktop mouse/keyboard into the page (icons still get their own clicks — icon hit areas
  are excluded), so the on-wallpaper chat is fully interactive.
