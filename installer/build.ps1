# Build Collie-Setup.exe end to end.
#
#   powershell -File installer\build.ps1                # full build
#   powershell -File installer\build.ps1 -CleanPayload  # rebuild the bundled runtime too
#
# Steps: read the version from harness/__init__.py (single source of truth) -> generate branding
# art + the language data -> ensure the payload runtime exists -> compile the .iss with that version
# passed as /DAppVer. Output: installer\Output\Collie-Setup.exe.
#
# Requires (maintainer/CI machine): Inno Setup 6 (iscc), and a system Python with Pillow + opencc for
# the generators. Windows only.
[CmdletBinding()]
param([switch]$CleanPayload)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }

# 1) version — the one place it lives
$ver = & python -c "import sys; sys.path.insert(0, r'$repo'); import harness; print(harness.__version__)"
if ($LASTEXITCODE -ne 0 -or -not $ver) { throw "could not read harness.__version__" }
Step "building Collie $ver"

# 2) generators (system Python: they need Pillow / opencc, which the embeddable runtime lacks)
Step "branding art"
& python (Join-Path $here "make_art.py")
Step "language data (77 discovered -> curated set)"
& python (Join-Path $here "gen_langs.py")

# 3) payload runtime
Step "payload runtime"
$pp = @{}
if ($CleanPayload) { $pp["Clean"] = $true }
& (Join-Path $here "build_payload.ps1") @pp

# 4) locate iscc
$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if (-not $iscc) {
  foreach ($c in @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
                   "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                   "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
    if (Test-Path $c) { $iscc = $c; break }
  }
} else { $iscc = $iscc.Source }
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found — winget install JRSoftware.InnoSetup" }

# 5) compile with the version injected
Step "compile installer"
& $iscc "/DAppVer=$ver" (Join-Path $here "collie.iss")
if ($LASTEXITCODE -ne 0) { throw "iscc failed" }

$exe = Join-Path $here "Output\Collie-Setup.exe"
$mb = "{0:N1} MB" -f ((Get-Item $exe).Length / 1MB)
Write-Host "`nBuilt $exe  ($mb, Collie $ver)" -ForegroundColor Green
