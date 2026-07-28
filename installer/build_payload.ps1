# Build the installer payload — the self-contained runtime that ships inside Collie-Setup.exe.
#
# Recreates installer\payload\ reproducibly so a CI runner (or any maintainer) gets the exact same
# bundle: an embeddable CPython with collie-harness[local,remote] + its ONNX semantic-memory deps already
# installed, plus the WebView2 bootstrapper. Idempotent; pass -Clean to rebuild from scratch.
#
#   powershell -File installer\build_payload.ps1                 # build/refresh the payload
#   powershell -File installer\build_payload.ps1 -Clean          # wipe payload\python first
#
# Windows only (the embeddable distribution and WebView2 are Windows). The .iss compile that
# consumes this lives in build.ps1.
[CmdletBinding()]
param(
  [string]$PyVersion = "3.12.10",
  [switch]$Clean
)
$ErrorActionPreference = "Stop"
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo    = Split-Path -Parent $here
$payload = Join-Path $here "payload"
$py      = Join-Path $payload "python"
$tag     = ($PyVersion -split '\.')[0..1] -join ''      # "3.12.10" -> "312"

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }

if ($Clean -and (Test-Path $py)) { Step "clean: removing $py"; Remove-Item -Recurse -Force $py }
New-Item -ItemType Directory -Force -Path $payload | Out-Null

# 1) embeddable CPython -------------------------------------------------------------------------
if (-not (Test-Path (Join-Path $py "python.exe"))) {
  $zip = Join-Path $env:TEMP "python-$PyVersion-embed-amd64.zip"
  $url = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
  Step "download embeddable CPython $PyVersion"
  Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
  New-Item -ItemType Directory -Force -Path $py | Out-Null
  Expand-Archive -Path $zip -DestinationPath $py -Force
  Remove-Item $zip
} else {
  Step "embeddable CPython already present"
}

# 2) enable site-packages: the embeddable ._pth ships with `import site` commented out, so pip and
#    installed packages are invisible until we turn it on and add the site-packages dir.
$pth = Join-Path $py "python$tag._pth"
if (Test-Path $pth) {
  $lines = Get-Content $pth
  if (-not ($lines -match 'Lib\\site-packages')) { $lines += 'Lib\site-packages' }
  $lines = $lines | ForEach-Object { if ($_ -match '^\s*#\s*import site\s*$') { 'import site' } else { $_ } }
  if (-not ($lines -match '^\s*import site\s*$')) { $lines += 'import site' }
  $lines | Set-Content -Encoding ASCII $pth
}

# 3) bootstrap pip (embeddable has no ensurepip) ------------------------------------------------
& (Join-Path $py "python.exe") -c "import pip" 2>$null
if ($LASTEXITCODE -ne 0) {
  Step "bootstrap pip (get-pip.py)"
  $getpip = Join-Path $env:TEMP "get-pip.py"
  Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getpip -UseBasicParsing
  & (Join-Path $py "python.exe") $getpip --no-warn-script-location
  Remove-Item $getpip
} else {
  Step "pip already present"
}

# 4) install collie + semantic-memory deps INTO the embeddable runtime --------------------------
#    setuptools/wheel first (the [local] deps build from sdist on some platforms), then the repo.
Step "pip install setuptools wheel"
& (Join-Path $py "python.exe") -m pip install --upgrade --no-warn-script-location setuptools wheel
Step "pip install collie-harness[local,remote] from the repo"
# [remote] = cryptography, for the phone-remote E2E handshake. WITHOUT it the packaged app reports
# e2e.available()=False and the desktop refuses every pairing — the whole Collie Remote feature is
# dead in a release build. It's a compiled wheel, but pip pulls the matching cp/win_amd64 wheel here.
& (Join-Path $py "python.exe") -m pip install --upgrade --no-warn-script-location "$repo[local,remote]"

# 5) WebView2 Evergreen bootstrapper (tiny; installs the runtime only if the machine lacks it) ---
$wv = Join-Path $payload "MicrosoftEdgeWebView2Setup.exe"
if (-not (Test-Path $wv)) {
  Step "download WebView2 bootstrapper"
  Invoke-WebRequest -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile $wv -UseBasicParsing
} else {
  Step "WebView2 bootstrapper already present"
}

# 6) sanity: the bundled collie must import and know its version --------------------------------
Step "verify payload"
$ver = & (Join-Path $py "python.exe") -c "import harness; print(harness.__version__)"
if ($LASTEXITCODE -ne 0) { throw "bundled collie failed to import" }
$size = "{0:N0} MB" -f ((Get-ChildItem -Recurse $py | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "payload ready: collie $ver, runtime $size" -ForegroundColor Green
