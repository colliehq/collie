// Self-extracting launcher for Collie-Setup.exe — carries the whole shell payload (UI host + WebView2
// DLLs + HTML + the silent Inno backend + WebView2 bootstrapper) as an embedded zip, extracts it to a
// temp folder, and runs the shell UI. Built with /win32icon:collie.ico so the single downloadable exe
// wears the Collie mark (IExpress could not set a custom icon). Temp is removed when the shell exits.
using System;
using System.IO;
using System.IO.Compression;
using System.Diagnostics;
using System.Reflection;

class Launcher
{
    [STAThread]
    static void Main()
    {
        string tmp = Path.Combine(Path.GetTempPath(),
            "collie-setup-" + Guid.NewGuid().ToString("N").Substring(0, 8));
        try
        {
            Directory.CreateDirectory(tmp);
            var asm = Assembly.GetExecutingAssembly();
            using (var s = asm.GetManifestResourceStream("payload.zip"))
            using (var z = new ZipArchive(s, ZipArchiveMode.Read))
                z.ExtractToDirectory(tmp);

            var shell = Path.Combine(tmp, "Collie-Shell.exe");
            var p = Process.Start(new ProcessStartInfo(shell)
            { UseShellExecute = false, WorkingDirectory = tmp });
            p.WaitForExit();
        }
        catch { }
        try { Directory.Delete(tmp, true); } catch { }   // best-effort cleanup
    }
}
