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

# 3) write the IExpress directive (.SED)
Step "write IExpress directive"
$names = Get-ChildItem $stage | Select-Object -ExpandProperty Name
$fileLines  = ""; $srcLines = ""
for ($i=0; $i -lt $names.Count; $i++){ $fileLines += "FILE$i=`"$($names[$i])`"`r`n"; $srcLines += "%FILE$i%=`r`n" }
$target = Join-Path $outDir "Collie-Setup.exe"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
if (Test-Path $target) { Remove-Item $target -Force }
$sed = @"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=0
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=%DisplayLicense%
FinishMessage=%FinishMessage%
TargetName=%TargetName%
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=%PostInstallCmd%
AdminQuietInstCmd=%AdminQuietInstCmd%
UserQuietInstCmd=%UserQuietInstCmd%
SourceFiles=SourceFiles
[Strings]
InstallPrompt=
DisplayLicense=
FinishMessage=
TargetName=$target
FriendlyName=Collie Setup
AppLaunched=Collie-Shell.exe
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
$fileLines
[SourceFiles]
SourceFiles0=$stage\
[SourceFiles0]
$srcLines
"@
$sedPath = Join-Path $here "collie-setup.sed"
Set-Content -Path $sedPath -Value $sed -Encoding ASCII

# 4) build the self-extractor
Step "run IExpress"
Start-Process iexpress -ArgumentList "/N","/Q",$sedPath -Wait
if (-not (Test-Path $target)) { throw "IExpress did not produce $target" }
$mb = "{0:N1} MB" -f ((Get-Item $target).Length/1MB)
Write-Host "`nBuilt $target  ($mb)" -ForegroundColor Green
