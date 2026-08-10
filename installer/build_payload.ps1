# Build the installer payload — the self-contained runtime that ships inside Collie-Setup.exe.
#
# Recreates installer\payload\ from reviewed bootstrap inputs: an embeddable CPython with
# collie-harness[local,remote] + its ONNX semantic-memory deps already installed, plus WebView2.
# The Python/get-pip inputs are pinned below; transitive PyPI wheels are not yet hash-locked, so this
# is not a claim of bit-for-bit reproducibility across dates. Idempotent; pass -Clean to rebuild.
#
#   powershell -File installer\build_payload.ps1                 # build/refresh the payload
#   powershell -File installer\build_payload.ps1 -Clean          # wipe payload\python first
#
# Windows only (the embeddable distribution and WebView2 are Windows). The .iss compile that
# consumes this lives in build.ps1.
[CmdletBinding()]
param(
  [string]$PyVersion = "3.12.10",
  [string]$PyEmbedSha256 = "",
  [switch]$Clean
)
$ErrorActionPreference = "Stop"
$here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo    = Split-Path -Parent $here
$payload = Join-Path $here "payload"
$py      = Join-Path $payload "python"
$tag     = ($PyVersion -split '\.')[0..1] -join ''      # "3.12.10" -> "312"
$payloadRoot = [IO.Path]::GetFullPath($py)

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }

function Assert-NativeExit([string]$Label, [int]$Code) {
  if ($Code -ne 0) { throw "$Label failed (exit $Code)" }
}

function Assert-FileSha256([string]$Path, [string]$Expected, [string]$Label) {
  if ($Expected -notmatch '^[0-9A-Fa-f]{64}$') { throw "$Label has no reviewed SHA-256" }
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "$Label is missing: $Path" }
  $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash
  if (-not $actual.Equals($Expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw "$Label SHA-256 mismatch: expected $Expected, got $actual"
  }
}

function Assert-AuthenticodePublisher([string]$Path, [string]$Publisher, [string]$Label) {
  $signature = Get-AuthenticodeSignature -LiteralPath $Path
  $subject = if ($signature.SignerCertificate) { [string]$signature.SignerCertificate.Subject } else { "" }
  if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
      $subject -notlike "*$Publisher*") {
    throw "$Label Authenticode verification failed: status=$($signature.Status), signer=$subject"
  }
}

function Remove-PayloadItem([string]$Path) {
  # This helper is intentionally unable to remove anything outside payload\python. It is used to
  # normalise a reusable staging runtime without ever touching ~/.collie or the installed app.
  $full = [IO.Path]::GetFullPath($Path)
  $prefix = $script:payloadRoot.TrimEnd('\') + '\'
  if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "refusing to remove a path outside the payload runtime: $full"
  }
  if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Recurse -Force }
}

function Remove-RepoBuildArtifact([string]$Path) {
  # setuptools reuses build/lib and will silently put a deleted source file into a later wheel.
  # Only these two conventional generated paths are eligible; source and dist/ are never touched.
  $full = [IO.Path]::GetFullPath($Path)
  $repoPrefix = [IO.Path]::GetFullPath($script:repo).TrimEnd('\') + '\'
  $leaf = [IO.Path]::GetFileName($full)
  if (-not $full.StartsWith($repoPrefix, [StringComparison]::OrdinalIgnoreCase) -or
      $leaf -notin @("build", "collie_harness.egg-info")) {
    throw "refusing to remove a non-generated repository path: $full"
  }
  if (Test-Path -LiteralPath $full) { Remove-Item -LiteralPath $full -Recurse -Force }
}

$expectedLines = @(& python -c "import sys; sys.path.insert(0, r'$repo'); import harness; print(harness.__version__)")
Assert-NativeExit "read repository Collie version" $LASTEXITCODE
if (-not $expectedLines) { throw "repository Collie version is empty" }
$expectedVer = ([string]$expectedLines[-1]).Trim()
if (-not $expectedVer) { throw "repository Collie version is empty" }

Step "clean stale setuptools build state"
Remove-RepoBuildArtifact (Join-Path $repo "build")
Remove-RepoBuildArtifact (Join-Path $repo "collie_harness.egg-info")

if ($Clean -and (Test-Path $py)) { Step "clean: removing $py"; Remove-Item -Recurse -Force $py }
New-Item -ItemType Directory -Force -Path $payload | Out-Null

# 1) embeddable CPython -------------------------------------------------------------------------
$knownPythonHashes = @{
  # https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip
  "3.12.10" = "4ACBED6DD1C744B0376E3B1CF57CE906F9DC9E95E68824584C8099A63025A3C3"
}
if (-not $PyEmbedSha256) { $PyEmbedSha256 = $knownPythonHashes[$PyVersion] }
if (-not $PyEmbedSha256) {
  throw "No reviewed embeddable-Python hash for $PyVersion; pass -PyEmbedSha256 after verification"
}
if (-not (Test-Path (Join-Path $py "python.exe"))) {
  $zip = Join-Path $env:TEMP "python-$PyVersion-embed-amd64.zip"
  $url = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
  Step "download embeddable CPython $PyVersion"
  Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
  Assert-FileSha256 $zip $PyEmbedSha256 "embeddable CPython $PyVersion archive"
  New-Item -ItemType Directory -Force -Path $py | Out-Null
  Expand-Archive -Path $zip -DestinationPath $py -Force
  Remove-Item $zip
} else {
  Step "embeddable CPython already present"
}
Assert-AuthenticodePublisher (Join-Path $py "python.exe") "Python Software Foundation" "python.exe"

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
# The probe must not be fatal, and under $ErrorActionPreference="Stop" it was: a native command
# that writes to stderr raises a terminating NativeCommandError regardless of its exit code, and
# a missing pip prints a traceback. So the check that exists to detect "no pip yet" aborted the
# build on precisely that case — every FIRST payload build on a clean machine, which is the only
# time this step has anything to do. Same shape as the empty-array abort in build_mac_payload.sh:
# the branch written for the absent thing was the branch that could not run without it.
$site = Join-Path $py "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $site | Out-Null
$pipInfos = @(Get-ChildItem -LiteralPath $site -Directory -Filter "pip-*.dist-info" -ErrorAction SilentlyContinue)
$hasPip = $false
try {
  $ErrorActionPreference = "Continue"
  & (Join-Path $py "python.exe") -m pip --version 2>&1 | Out-Null
  # A reusable/overlaid payload with several metadata directories is not healthy even when
  # `import pip` happens to work. That exact state produced a mixed pip which failed in its CLI.
  $hasPip = ($LASTEXITCODE -eq 0 -and $pipInfos.Count -eq 1)
} finally {
  $ErrorActionPreference = "Stop"
}
if (-not $hasPip) {
  Step "bootstrap pip (get-pip.py)"
  Remove-PayloadItem (Join-Path $site "pip")
  foreach ($info in $pipInfos) { Remove-PayloadItem $info.FullName }
  $getpip = Join-Path $env:TEMP "get-pip.py"
  # Pin both immutable source revision and digest: this file is executable Python containing a pip
  # wheel, so TLS alone is not an adequate release-build trust boundary.
  $getPipCommit = "af54dfe793b24685f8dc4ebba0630d9f2d77653c"
  $getPipSha256 = "FB24E693BAB954209A063D90953621412CCAD4A500905A726286E038F508DDF6"
  $getPipUrl = "https://raw.githubusercontent.com/pypa/get-pip/$getPipCommit/public/get-pip.py"
  Invoke-WebRequest -Uri $getPipUrl -OutFile $getpip -UseBasicParsing
  try {
    Assert-FileSha256 $getpip $getPipSha256 "get-pip.py"
    & (Join-Path $py "python.exe") $getpip --no-warn-script-location
    Assert-NativeExit "bootstrap pip" $LASTEXITCODE
  } finally {
    if (Test-Path -LiteralPath $getpip) { Remove-Item -LiteralPath $getpip -Force }
  }
} else {
  Step "pip already present"
}

# 4) install collie + semantic-memory deps INTO the embeddable runtime --------------------------
#    setuptools/wheel first (the [local] deps build from sdist on some platforms), then the repo.
Step "pip install setuptools wheel"
& (Join-Path $py "python.exe") -m pip install --upgrade --no-warn-script-location setuptools wheel
Assert-NativeExit "install payload build dependencies" $LASTEXITCODE

# An incremental payload build must be just as deterministic as -Clean. Remove only Collie's staged
# package and its metadata; third-party wheels remain cached in the payload and user data lives
# elsewhere. This also prevents importlib.metadata from selecting an old version after an overlay.
Remove-PayloadItem (Join-Path $site "harness")
foreach ($info in @(Get-ChildItem -LiteralPath $site -Directory -Filter "collie_harness-*.dist-info" -ErrorAction SilentlyContinue)) {
  Remove-PayloadItem $info.FullName
}
Step "pip install collie-harness[local,remote] from the repo"
# [remote] = cryptography, for the phone-remote E2E handshake. WITHOUT it the packaged app reports
# e2e.available()=False and the desktop refuses every pairing — the whole Collie Remote feature is
# dead in a release build. It's a compiled wheel, but pip pulls the matching cp/win_amd64 wheel here.
& (Join-Path $py "python.exe") -m pip install --upgrade --no-build-isolation --no-warn-script-location "$repo[local,remote]"
Assert-NativeExit "install Collie into payload" $LASTEXITCODE

# 5) WebView2 Evergreen bootstrapper (tiny; installs the runtime only if the machine lacks it) ---
$wv = Join-Path $payload "MicrosoftEdgeWebView2Setup.exe"
if (-not (Test-Path $wv)) {
  Step "download WebView2 bootstrapper"
  Invoke-WebRequest -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile $wv -UseBasicParsing
} else {
  Step "WebView2 bootstrapper already present"
}
Assert-AuthenticodePublisher $wv "Microsoft Corporation" "WebView2 bootstrapper"

# 6) sanity: code, metadata, runtime modules and private assets must agree -----------------------
Step "verify payload"
$verify = @'
import importlib
import importlib.metadata as metadata
import pathlib
import sys

expected = sys.argv[1]
import harness
root = pathlib.Path(harness.__file__).resolve().parent
code = harness.__version__
dist = metadata.version("collie-harness")
infos = list(root.parent.glob("collie_harness-*.dist-info"))
if code != expected or dist != expected:
    raise SystemExit("payload version mismatch: expected=%s code=%s metadata=%s" %
                     (expected, code, dist))
if len(infos) != 1:
    raise SystemExit("payload needs exactly one Collie dist-info, found %d" % len(infos))
for name in ("harness.supervisor", "harness.automations", "harness.ops"):
    importlib.import_module(name)
for rel in ("oauth_ext/pi-oauth-proxy.js", "oauth_ext/opencode.jsonc",
            "browser_ext/manifest.json", "webui/index.html"):
    if not (root / rel).is_file():
        raise SystemExit("payload asset missing: " + rel)
for private in ("browser_ext/token.txt", "browser_ext/auth.js"):
    if (root / private).exists():
        raise SystemExit("private browser credential leaked into the payload: " + private)
print(code)
'@
$verifyPath = Join-Path $env:TEMP ("collie-payload-verify-{0}.py" -f $PID)
try {
  # Windows PowerShell's native-command marshalling strips quotes from a multiline
  # `python -c $verify` value. Execute an exact temporary source file so assertions such as
  # glob("collie_harness-*.dist-info") reach Python byte-for-byte.
  Set-Content -LiteralPath $verifyPath -Value $verify -Encoding UTF8
  $ver = & (Join-Path $py "python.exe") $verifyPath $expectedVer
  Assert-NativeExit "verify bundled Collie" $LASTEXITCODE
} finally {
  if (Test-Path -LiteralPath $verifyPath) { Remove-Item -LiteralPath $verifyPath -Force }
}
& (Join-Path $py "python.exe") -m pip --version | Out-Null
Assert-NativeExit "verify bundled pip" $LASTEXITCODE
$size = "{0:N0} MB" -f ((Get-ChildItem -Recurse $py | Measure-Object Length -Sum).Sum / 1MB)
Write-Host "payload ready: collie $ver, runtime $size" -ForegroundColor Green
