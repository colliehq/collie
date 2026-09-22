# Build a local, inspectable Collie research bundle without publishing anything.
#
# Output:
#   dist\local-bundle\                  unpacked artifacts + hashes
#   dist\Collie-Research-Bundle-X.zip   one archive for review/install testing
[CmdletBinding()]
param(
  [string]$OutputDir = "",
  [switch]$IncludeInstaller,
  [switch]$CleanInstallerPayload
)

$ErrorActionPreference = "Stop"
$here = [IO.Path]::GetFullPath($PSScriptRoot)
$repo = [IO.Path]::GetFullPath((Join-Path $here ".."))
$distRoot = [IO.Path]::GetFullPath((Join-Path $repo "dist"))
if (-not $OutputDir) { $OutputDir = Join-Path $distRoot "local-bundle" }
$output = [IO.Path]::GetFullPath($OutputDir)
$distPrefix = $distRoot.TrimEnd('\') + '\'
if (-not $output.StartsWith($distPrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "OutputDir must stay inside $distRoot"
}

function Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Assert-Exit([string]$Action) {
  if ($LASTEXITCODE -ne 0) { throw "$Action failed (exit $LASTEXITCODE)" }
}

$version = & python -c "import sys; sys.path.insert(0, r'$repo'); import harness; print(harness.__version__)"
Assert-Exit "read Collie version"
$version = "$version".Trim()
if (-not $version) { throw "Collie version is empty" }

if (Test-Path -LiteralPath $output) {
  $resolved = [IO.Path]::GetFullPath($output)
  if (-not $resolved.StartsWith($distPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing unsafe cleanup path: $resolved"
  }
  Remove-Item -LiteralPath $resolved -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $output | Out-Null
$pythonOut = New-Item -ItemType Directory -Force -Path (Join-Path $output "python")
$extensionsOut = New-Item -ItemType Directory -Force -Path (Join-Path $output "extensions")

Step "Python wheel + source distribution"
& python -m build --no-isolation --outdir $pythonOut.FullName $repo
Assert-Exit "Python package build"

Step "VS Code IDE extension"
$vscodeVersion = (Get-Content -LiteralPath (Join-Path $repo "vscode-collie\package.json") -Raw | ConvertFrom-Json).version
$vsix = Join-Path $extensionsOut.FullName ("Collie-VSCode-{0}.vsix" -f $vscodeVersion)
Push-Location (Join-Path $repo "vscode-collie")
try {
  & npx --yes '@vscode/vsce' package --out $vsix
  Assert-Exit "VS Code extension package"
}
finally { Pop-Location }

Step "Chrome browser bridge"
$browserVersion = (Get-Content -LiteralPath (Join-Path $repo "harness\browser_ext\manifest.store.json") -Raw | ConvertFrom-Json).version
$browserZip = Join-Path $extensionsOut.FullName ("Collie-Browser-Bridge-Store-{0}.zip" -f $browserVersion)
$browserPowerZip = Join-Path $extensionsOut.FullName ("Collie-Browser-Bridge-Power-{0}.zip" -f $browserVersion)
& (Join-Path $here "package_browser_extension.ps1") -Output $browserZip | Out-Null
Assert-Exit "browser extension package"
& (Join-Path $here "package_browser_extension.ps1") -Power -Output $browserPowerZip | Out-Null
Assert-Exit "browser extension Power package"

Step "Collie Online deployable reference"
$onlineStage = Join-Path $output "online-stage"
$onlinePackage = Join-Path $output ("Collie-Online-{0}.zip" -f $version)
New-Item -ItemType Directory -Force -Path (Join-Path $onlineStage "docs") | Out-Null
foreach ($name in @("worker.js", "schema.sql", "wrangler.toml", "README.md")) {
  Copy-Item -LiteralPath (Join-Path $repo "online\$name") -Destination (Join-Path $onlineStage $name)
}
Copy-Item -LiteralPath (Join-Path $repo "docs\COLLIE_ONLINE_V1.md") -Destination (Join-Path $onlineStage "docs\COLLIE_ONLINE_V1.md")
Copy-Item -LiteralPath (Join-Path $repo "docs\online.md") -Destination (Join-Path $onlineStage "docs\online.md")
Copy-Item -LiteralPath (Join-Path $repo "tests\online_worker_test.js") -Destination (Join-Path $onlineStage "online_worker_test.js")
Compress-Archive -Path (Join-Path $onlineStage "*") -DestinationPath $onlinePackage -Force
Remove-Item -LiteralPath $onlineStage -Recurse -Force

if ($IncludeInstaller) {
  Step "Windows all-in-one installer"
  $installerArgs = @{}
  if ($CleanInstallerPayload) { $installerArgs["CleanPayload"] = $true }
  & (Join-Path $here "build.ps1") @installerArgs
  Assert-Exit "Windows installer build"
  Copy-Item -LiteralPath (Join-Path $here "Output\Collie-Setup.exe") -Destination (Join-Path $output "Collie-Setup.exe")
}

@"
# Collie local research bundle $version

- `python/`: wheel and source distribution for the local runtime and Online client.
- `extensions/Collie-VSCode-*.vsix`: sidebar workbench plus the editor-area Project Map.
- `extensions/Collie-Browser-Bridge-Store-*.zip`: least-privilege Web Store build; no device token.
- `extensions/Collie-Browser-Bridge-Power-*.zip`: local-only trusted-input build with debugger access.
- `Collie-Online-*.zip`: Cloudflare Worker, D1 schema, frozen protocol contract, and offline worker test.
- `Collie-Setup.exe`: present only when `-IncludeInstaller` was requested and Inno Setup was available.

This is a local research artifact. It contains no production OAuth secret, vault key, device token,
browser credential, Cloudflare resource id, or deployment action. `wrangler.toml` intentionally keeps
its placeholder D1 id until a reviewed environment is selected.
"@ | Set-Content -LiteralPath (Join-Path $output "README.md") -Encoding UTF8

git -C $repo status --short | Set-Content -LiteralPath (Join-Path $output "SOURCE-STATE.txt") -Encoding UTF8
$artifacts = Get-ChildItem -LiteralPath $output -Recurse -File | Where-Object { $_.Name -ne "BUNDLE-MANIFEST.json" }
$manifest = [ordered]@{
  product = "Collie local research bundle"
  version = $version
  vscode_version = $vscodeVersion
  browser_version = $browserVersion
  built_at_utc = [DateTime]::UtcNow.ToString("o")
  online_included = $true
  installer_included = [bool]$IncludeInstaller
  artifacts = @($artifacts | Sort-Object FullName | ForEach-Object {
    [ordered]@{
      path = $_.FullName.Substring($output.Length + 1).Replace('\', '/')
      bytes = $_.Length
      sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
  })
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $output "BUNDLE-MANIFEST.json") -Encoding UTF8

$bundleZip = Join-Path $distRoot ("Collie-Research-Bundle-{0}.zip" -f $version)
if (Test-Path -LiteralPath $bundleZip) { Remove-Item -LiteralPath $bundleZip -Force }
Compress-Archive -Path (Join-Path $output "*") -DestinationPath $bundleZip -Force

Write-Host "`nBuilt:" -ForegroundColor Green
Write-Host "  $output"
Write-Host "  $bundleZip"
