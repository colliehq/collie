; collie desktop wallpaper — one-click Windows installer (Inno Setup 6+).
;
; Produces a single collie-wallpaper-setup.exe that a non-technical user double-clicks to get the
; live-desktop wallpaper running + auto-starting at logon, with no command line.
;
; WHAT IT DOES
;   1. lays down a self-contained runtime under {app}: an embeddable Python + the collie package +
;      its deps (onnxruntime/tokenizers for semantic memory) + the wallpaper engine source & DLLs
;   2. chains the Microsoft Edge WebView2 Evergreen bootstrapper (the engine's only OS dependency)
;   3. registers the logon autostart by running  collie wallpaper --install  post-install
;   4. uninstall reverses it (collie wallpaper --uninstall + removes {app})
;
; BUILD (on a maintainer machine, NOT scripted blind here):
;   - install Inno Setup 6:            winget install JRSoftware.InnoSetup
;   - stage the bundled runtime into installer\payload\  (see installer\README.md):
;       payload\python\           <- python-3.x-embed-amd64 unpacked
;       payload\python\Lib\site-packages\  <- pip install --target here: collie-harness[local]
;       payload\MicrosoftEdgeWebView2Setup.exe   <- the Evergreen bootstrapper
;   - compile:                         iscc installer\collie-wallpaper.iss
;   - output:                          installer\Output\collie-wallpaper-setup.exe
;
; The .iss below is complete; it only needs those payload files present at compile time.

#define AppName    "Collie Wallpaper"
#define AppVer     "0.18.0"
#define Publisher  "Collie"
#define PyDir      "{app}\python"
#define PyW        "{app}\python\pythonw.exe"

[Setup]
AppId={{7F3C2A10-COLLIE-4E2A-9B1D-DESKTOPWALL01}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher={#Publisher}
DefaultDirName={autopf}\CollieWallpaper
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=collie-wallpaper-setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
PrivilegesRequired=lowest             ; per-user install; the logon autostart is per-user anyway
WizardStyle=modern

[Files]
; the bundled runtime (embeddable python + collie package + deps + wallpaper engine source/DLLs)
Source: "payload\python\*"; DestDir: "{#PyDir}"; Flags: recursesubdirs createallsubdirs ignoreversion
; the WebView2 Evergreen bootstrapper (tiny; it downloads the runtime if the machine lacks it)
Source: "payload\MicrosoftEdgeWebView2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Run]
; 1) ensure the WebView2 runtime (silent; no-op if already present)
Filename: "{tmp}\MicrosoftEdgeWebView2Setup.exe"; Parameters: "/silent /install"; \
  StatusMsg: "Installing the WebView2 runtime..."; Flags: waituntilterminated

; 2) register the logon autostart (collie wallpaper --install) and launch it now
Filename: "{#PyW}"; Parameters: "-m harness.cli wallpaper --install"; \
  WorkingDir: "{#PyDir}"; StatusMsg: "Setting up the desktop wallpaper..."; Flags: runhidden waituntilterminated
Filename: "{#PyW}"; Parameters: "-m harness.cli wallpaper"; \
  WorkingDir: "{#PyDir}"; Description: "Start the wallpaper now"; Flags: runhidden postinstall nowait

[UninstallRun]
; remove the logon autostart + stop the running engine before files are deleted
Filename: "{#PyW}"; Parameters: "-m harness.cli wallpaper --stop"; \
  WorkingDir: "{#PyDir}"; RunOnceId: "StopWallpaper"; Flags: runhidden waituntilterminated
Filename: "{#PyW}"; Parameters: "-m harness.cli wallpaper --uninstall"; \
  WorkingDir: "{#PyDir}"; RunOnceId: "UninstallAutostart"; Flags: runhidden waituntilterminated

[Icons]
Name: "{group}\Start Collie Wallpaper"; Filename: "{#PyW}"; Parameters: "-m harness.cli wallpaper"; Flags: runminimized
Name: "{group}\Stop Collie Wallpaper";  Filename: "{#PyW}"; Parameters: "-m harness.cli wallpaper --stop"; Flags: runminimized
Name: "{group}\Uninstall {#AppName}";   Filename: "{uninstallexe}"
