# Pack the smart-shell installer into ONE self-extracting Collie-Setup.exe.
#
# The single exe (built with Windows' built-in IExpress — no third-party tools) contains the WebView2
# host + its DLLs + the HTML UI + the silent Inno backend (which carries the whole runtime payload) +
# the WebView2 bootstrapper. On run it silently extracts to a temp folder and launches Collie-Shell.exe
# — the beautiful UI — which drives the backend to do the real install. When the shell exits, IExpress
# cleans the temp files up.
#
#   powershell -File installer\shell\pack.ps1   ->  installer\Output\Collie-Setup.exe
$ErrorActionPreference = "Stop"
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$root    = Split-Path -Parent (Split-Path -Parent $here)      # repo root
$outDir  = Join-Path (Split-Path -Parent $here) "Output"      # installer\Output
$stage   = Join-Path $here "dist-stage"
$webview = Join-Path $root "harness\wallpaper"
$payload = Join-Path (Split-Path -Parent $here) "payload"

function Step($m){ Write-Host "==> $m" -ForegroundColor Cyan }

# 0) build the Inno backend (the silent file-installer that carries the runtime payload) and copy it
#    aside BEFORE the launcher overwrites installer\Output\Collie-Setup.exe — otherwise a re-run would
#    embed the launcher as its own backend.
Step "build the Inno backend"
& (Join-Path (Split-Path -Parent $here) "build.ps1")
$innoOut = Join-Path $outDir "Collie-Setup.exe"
if (-not (Test-Path $innoOut)) { throw "backend build did not produce $innoOut" }
Copy-Item $innoOut (Join-Path $here "Collie-Setup-backend.exe") -Force

# 1) ensure the shell is freshly built
Step "build the shell host"
& (Join-Path $here "build-shell.ps1")

# 2) stage exactly the runtime files (nothing else from the source folder)
Step "stage runtime files"
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$files = @(
  (Join-Path $here "Collie-Shell.exe"),
  (Join-Path $here "installer.html"),
  (Join-Path $here "fonts.css"),
  (Join-Path $here "collie-logo.png"),
  (Join-Path $here "collie.ico"),
  (Join-Path $here "Collie-Setup-backend.exe"),
  (Join-Path $webview "Microsoft.Web.WebView2.Core.dll"),
  (Join-Path $webview "Microsoft.Web.WebView2.WinForms.dll"),
  (Join-Path $webview "WebView2Loader.dll"),
  (Join-Path $payload "MicrosoftEdgeWebView2Setup.exe")
)
foreach ($f in $files) {
  if (-not (Test-Path $f)) { throw "missing runtime file: $f  (build the backend first: pack expects Collie-Setup-backend.exe alongside)" }
  Copy-Item $f $stage -Force
}

# 3) zip the staged files into the payload embedded in the launcher
Step "zip payload"
$zip = Join-Path $here "payload.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($stage, $zip)

# 4) compile the self-extracting launcher WITH the Collie icon + embedded payload
Step "compile Collie-Setup.exe (custom icon)"
$target = Join-Path $outDir "Collie-Setup.exe"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
if (Test-Path $target) { Remove-Item $target -Force }
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$ico = Join-Path $here "collie.ico"
$args = @("/nologo","/target:winexe","/platform:x64","/out:$target","/win32icon:$ico",
          "/resource:$zip,payload.zip",
          "/reference:System.IO.Compression.dll","/reference:System.IO.Compression.FileSystem.dll",
          (Join-Path $here "Launcher.cs"))
& $csc $args
if ($LASTEXITCODE -ne 0) { throw "csc failed building the launcher" }
Remove-Item $zip -Force
$mb = "{0:N1} MB" -f ((Get-Item $target).Length/1MB)
Write-Host "`nBuilt $target  ($mb, with the Collie icon)" -ForegroundColor Green
