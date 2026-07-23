# collie desktop wallpaper engine (Windows)

Renders collie's live code star-map as the desktop wallpaper (behind the icons) with a
clickable/typable chat, driven by the local `collie web` server.

## Requirements
- Windows 10/11 with the **WebView2 runtime** (ships with Edge; already present on most machines).
- `collie web` running (locally on Windows, or in WSL — the page is served at http://127.0.0.1:8787/wallpaper).

## Run
```powershell
# 1) start collie's server (serves /wallpaper)
collie web --no-open
# 2) build the engine (once) and launch it
powershell -ExecutionPolicy Bypass -File .\build.ps1
Start-Process .\collie-wallpaper.exe
```
Stop it cleanly (never -Force kill — that orphans WebView2 COM):
```powershell
powershell -File .\stop-wallpaper.ps1
```

## What it does
- Pins a WS_CHILD WebView2 window under Progman, z-ordered below the icon layer (raised-desktop
  compatible; re-asserted on WM_WINDOWPOSCHANGING so it can never cover the icons).
- Forwards desktop mouse/keyboard into the page (icons still get their own clicks — icon hit areas
  are excluded), so the on-wallpaper chat is fully interactive.
