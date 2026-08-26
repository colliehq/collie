param(
  [string]$Output = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$source = [IO.Path]::GetFullPath((Join-Path $repoRoot "harness\browser_ext"))
if (-not $Output) {
  $version = (Get-Content -LiteralPath (Join-Path $source "manifest.json") -Raw | ConvertFrom-Json).version
  $Output = Join-Path $repoRoot ("dist\collie-browser-bridge-{0}.zip" -f $version)
}
$outputFull = [IO.Path]::GetFullPath($Output)
$outputDir = Split-Path -Parent $outputFull
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$stage = [IO.Path]::GetFullPath((Join-Path $tempRoot ("collie-browser-extension-" + [guid]::NewGuid().ToString("N"))))
if (-not $stage.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Refusing an unsafe staging path: $stage"
}

$files = @(
  "manifest.json", "background.js", "shadow.js", "presence.js",
  "popup.html", "popup.js", "sidepanel.html", "sidepanel.js",
  "icon16.png", "icon48.png", "icon128.png"
)

try {
  New-Item -ItemType Directory -Path $stage | Out-Null
  foreach ($name in $files) {
    $item = Join-Path $source $name
    if (-not (Test-Path -LiteralPath $item -PathType Leaf)) { throw "Missing extension asset: $name" }
    Copy-Item -LiteralPath $item -Destination (Join-Path $stage $name)
  }
  if (Test-Path -LiteralPath (Join-Path $stage "token.txt")) { throw "token.txt entered the store stage" }
  if (Test-Path -LiteralPath (Join-Path $stage "auth.js")) { throw "auth.js entered the store stage" }
  Get-Content -LiteralPath (Join-Path $stage "manifest.json") -Raw | ConvertFrom-Json | Out-Null
  Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $outputFull -Force
  Write-Output $outputFull
}
finally {
  $resolvedStage = [IO.Path]::GetFullPath($stage)
  if ((Test-Path -LiteralPath $resolvedStage) -and
      $resolvedStage.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
    Remove-Item -LiteralPath $resolvedStage -Recurse -Force
  }
}
