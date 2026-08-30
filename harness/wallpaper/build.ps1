# Build the collie desktop wallpaper engine (WebView2 + WS_CHILD behind-icons + input forwarding).
# No Visual Studio / .NET SDK needed — uses the in-box .NET Framework compiler.
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$speech = "$env:WINDIR\Microsoft.NET\assembly\GAC_MSIL\System.Speech\v4.0_4.0.0.0__31bf3856ad364e35\System.Speech.dll"
Set-Location $PSScriptRoot
& $csc /nologo /target:winexe /platform:x64 /out:collie-wallpaper.exe /win32icon:collie.ico `
  /reference:'System.Windows.Forms.dll' /reference:'System.Drawing.dll' "/reference:$speech" `
  /reference:'Microsoft.Web.WebView2.Core.dll' /reference:'Microsoft.Web.WebView2.WinForms.dll' Program.cs
if ($LASTEXITCODE -ne 0) { "BUILD FAILED"; exit $LASTEXITCODE }
if (Test-Path collie-wallpaper.exe) { "BUILD OK -> $PSScriptRoot\collie-wallpaper.exe" } else { "BUILD FAILED"; exit 1 }
