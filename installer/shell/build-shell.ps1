# Compile the Collie smart-shell installer host (WebView2 + WinForms, borderless).
# Reuses the WebView2 assemblies collie already ships for its wallpaper engine, and the in-box .NET
# Framework csc — no .NET SDK required. Output: Collie-Shell.exe in this folder (with the WebView2
# DLLs copied alongside so it runs in place).
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$wv   = Join-Path (Split-Path -Parent (Split-Path -Parent $here)) "harness\wallpaper"
$csc  = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { throw "csc not found: $csc" }

# WebView2 runtime DLLs must sit next to the exe
foreach ($d in "Microsoft.Web.WebView2.Core.dll","Microsoft.Web.WebView2.WinForms.dll","WebView2Loader.dll") {
  Copy-Item (Join-Path $wv $d) (Join-Path $here $d) -Force
}
Copy-Item (Join-Path $wv "collie.ico") (Join-Path $here "collie.ico") -Force

$out   = Join-Path $here "Collie-Shell.exe"
$ico   = Join-Path $here "collie.ico"
$refC  = Join-Path $here "Microsoft.Web.WebView2.Core.dll"
$refW  = Join-Path $here "Microsoft.Web.WebView2.WinForms.dll"
$src   = Join-Path $here "Shell.cs"
$args  = @("/nologo","/target:winexe","/platform:x64","/out:$out","/win32icon:$ico",
           "/reference:System.Windows.Forms.dll","/reference:System.Drawing.dll",
           "/reference:$refC","/reference:$refW",$src)
& $csc $args
if ($LASTEXITCODE -ne 0) { throw "csc failed" }
Write-Host "Built $(Join-Path $here 'Collie-Shell.exe')" -ForegroundColor Green
