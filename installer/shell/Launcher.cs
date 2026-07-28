// Self-extracting launcher for Collie-Setup.exe — carries the whole shell payload (UI host + WebView2
// DLLs + HTML + the silent Inno backend + WebView2 bootstrapper) as an embedded zip, extracts it to a
// temp folder, and runs the shell UI. Built with /win32icon:collie.ico so the single downloadable exe
// wears the Collie mark (IExpress could not set a custom icon). Temp is removed when the shell exits.
using System;
using System.IO;
using System.IO.Compression;
using System.Diagnostics;
using System.Reflection;
using System.Collections.Generic;

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

            // A SILENT invocation (`collie update`, scripted upgrade) must NOT pop the GUI shell —
            // it would just sit there. Forward the Inno flags straight to the backend installer.
            // Without this, `Collie-Setup.exe /SILENT` launched the interactive shell and "succeeded"
            // (exit 0) without installing anything — self-update was a silent no-op.
            var passthru = new List<string>();
            bool silent = false;
            var argv = Environment.GetCommandLineArgs();     // [0] = this exe
            for (int i = 1; i < argv.Length; i++)
            {
                passthru.Add(argv[i]);
                string u = argv[i].ToUpperInvariant();
                if (u == "/SILENT" || u == "/VERYSILENT") silent = true;
            }

            var psi = silent
                ? new ProcessStartInfo(Path.Combine(tmp, "Collie-Setup-backend.exe"))
                  { Arguments = string.Join(" ", passthru.ToArray()) }
                : new ProcessStartInfo(Path.Combine(tmp, "Collie-Shell.exe"));
            psi.UseShellExecute = false;
            psi.WorkingDirectory = tmp;
            var p = Process.Start(psi);
            p.WaitForExit();
        }
        catch { }
        try { Directory.Delete(tmp, true); } catch { }   // best-effort cleanup
    }
}
