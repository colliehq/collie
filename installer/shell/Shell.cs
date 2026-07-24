// Collie "smart shell" installer host — a borderless WebView2 window that renders installer.html
// (the fully custom app-grade UI) and drives the real install underneath by running the Inno
// backend silently. This is the shell the Inno wizard couldn't be: every pixel is the HTML's, and
// the OS chrome is gone.
//
// Build: build-shell.ps1 (csc, references the WebView2 assemblies shipped in ../../harness/wallpaper).
// The UI talks to this host via window.chrome.webview.postMessage({action:...}); we reply with
// progress via PostWebMessageAsJson. Actions: drag, close, launch, lang, install.

using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

class Shell : Form
{
    [DllImport("user32.dll")] static extern bool ReleaseCapture();
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr h, int msg, IntPtr wp, IntPtr lp);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
    [DllImport("user32.dll")] static extern int SetWindowRgn(IntPtr h, IntPtr rgn, bool redraw);
    [DllImport("gdi32.dll")] static extern IntPtr CreateRoundRectRgn(int x1, int y1, int x2, int y2, int w, int h);
    const int WM_NCLBUTTONDOWN = 0x00A1, HTCAPTION = 2;

    readonly WebView2 web = new WebView2();
    readonly string appDir;
    string backendExe;

    [STAThread]
    static void Main()
    {
        try { SetProcessDpiAwarenessContext((IntPtr)(-4)); } catch { }   // per-monitor-v2
        Application.EnableVisualStyles();
        Application.Run(new Shell());
    }

    public Shell()
    {
        appDir = Path.GetDirectoryName(Application.ExecutablePath);
        backendExe = Path.Combine(appDir, "Collie-Setup-backend.exe");

        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.CenterScreen;
        Text = "Collie Setup";
        BackColor = Color.FromArgb(14, 16, 23);
        double s = DeviceDpi / 96.0;
        ClientSize = new Size((int)(780 * s), (int)(560 * s));
        try { Icon = new Icon(Path.Combine(appDir, "collie.ico")); } catch { }

        web.Dock = DockStyle.Fill;
        Controls.Add(web);
        Load += async (a, b) => await Init();
        // big, smooth rounded corners — DWM's fixed ~8px reads as "square" on a window this size, so we
        // clip the window to a generous round-rect region instead. Re-applied on resize.
        Shown += (a, b) => RoundWindow();
        Resize += (a, b) => RoundWindow();
    }

    void RoundWindow()
    {
        int d = (int)(38 * (DeviceDpi / 96.0)) * 2;   // corner diameter
        try { SetWindowRgn(Handle, CreateRoundRectRgn(0, 0, Width + 1, Height + 1, d, d), true); } catch { }
    }

    async Task Init()
    {
        // keep the WebView2 user-data in temp; the shell is transient
        string udf = Path.Combine(Path.GetTempPath(), "collie-shell-webview");
        var env = await CoreWebView2Environment.CreateAsync(null, udf, null);
        await web.EnsureCoreWebView2Async(env);
        var c = web.CoreWebView2;
        c.Settings.AreDefaultContextMenusEnabled = false;
        c.Settings.IsZoomControlEnabled = false;
        c.Settings.AreDevToolsEnabled = false;
        try { c.Settings.IsNonClientRegionSupportEnabled = true; } catch { }   // native app-region drag if supported
        web.DefaultBackgroundColor = Color.FromArgb(14, 16, 23);

        // serve the shell folder under a virtual host so relative assets (logo) load cleanly
        c.SetVirtualHostNameToFolderMapping("collie.setup", appDir, CoreWebView2HostResourceAccessKind.Allow);
        c.WebMessageReceived += OnMessage;
        c.Navigate("https://collie.setup/installer.html");
    }

    void OnMessage(object sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        string json;
        try { json = e.TryGetWebMessageAsString(); } catch { json = e.WebMessageAsJson; }
        string action = J(json, "action");
        switch (action)
        {
            case "drag":
                ReleaseCapture(); SendMessage(Handle, WM_NCLBUTTONDOWN, (IntPtr)HTCAPTION, IntPtr.Zero);
                break;
            case "close":
                Close();
                break;
            case "launch":
                LaunchCollie(); Close();
                break;
            case "install":
                RunInstall(J(json, "lang"), J(json, "wallpaper") == "true", J(json, "bridge") == "true");
                break;
        }
    }

    // ---- run the Inno backend silently, streaming progress to the UI ---------------------------
    void RunInstall(string lang, bool wallpaper, bool bridge)
    {
        Task.Run(() =>
        {
            try
            {
                if (!File.Exists(backendExe))
                {
                    // no backend bundled (dev preview) — simulate so the flow is demoable
                    string[,] steps = { { "Unpacking the runtime…", "45" }, { "Installing the engine…", "72" }, { "Finishing up…", "100" } };
                    for (int i = 0; i < steps.GetLength(0); i++) { Progress(steps[i, 0], "", int.Parse(steps[i, 1])); System.Threading.Thread.Sleep(700); }
                    Progress("Done", "", 100, true); return;
                }
                var tasks = (wallpaper ? "wallpaper," : "") + (bridge ? "bridge," : "") + "desktopicon";
                var psi = new ProcessStartInfo(backendExe,
                    "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOCANCEL "
                    + "/LANG=" + (string.IsNullOrEmpty(lang) ? "en" : lang)
                    + " /TASKS=\"" + tasks + "\"")
                { UseShellExecute = false, CreateNoWindow = true };

                Progress("Unpacking Collie…", "Embeddable Python + engine + extension", 20);
                var p = Process.Start(psi);
                // Inno silent gives no progress stream; animate toward 90% while it runs, finish on exit.
                int pct = 20;
                while (!p.WaitForExit(400)) { pct = Math.Min(90, pct + 3); Progress(null, null, pct); }
                if (p.ExitCode == 0) Progress("All set", "", 100, true);
                else Progress("Setup failed", "exit code " + p.ExitCode, 100);
            }
            catch (Exception ex) { Progress("Setup error", ex.Message, 100); }
        });
    }

    void Progress(string phase, string detail, int pct, bool done = false)
    {
        var sb = new System.Text.StringBuilder("{");
        if (phase != null) sb.Append("\"phase\":\"").Append(Esc(phase)).Append("\",");
        if (detail != null) sb.Append("\"detail\":\"").Append(Esc(detail)).Append("\",");
        sb.Append("\"pct\":").Append(pct);
        if (done) sb.Append(",\"done\":true");
        sb.Append("}");
        try { BeginInvoke((Action)(() => web.CoreWebView2.PostWebMessageAsJson(sb.ToString()))); } catch { }
    }

    void LaunchCollie()
    {
        try
        {
            string pyw = Environment.ExpandEnvironmentVariables(@"%LOCALAPPDATA%\Programs\Collie\python\pythonw.exe");
            if (File.Exists(pyw))
                Process.Start(new ProcessStartInfo(pyw, "-m harness.cli app")
                { WorkingDirectory = Path.GetDirectoryName(pyw), UseShellExecute = false });
        }
        catch { }
    }

    // minimal JSON string-value getter (messages are flat + known-shaped, no deps needed)
    static string J(string json, string key)
    {
        if (json == null) return "";
        int i = json.IndexOf("\"" + key + "\"");
        if (i < 0) return "";
        i = json.IndexOf(':', i); if (i < 0) return "";
        i++;
        while (i < json.Length && (json[i] == ' ' || json[i] == '\t')) i++;
        if (i >= json.Length) return "";
        if (json[i] == '"')
        {
            int j = json.IndexOf('"', i + 1);
            return j < 0 ? "" : json.Substring(i + 1, j - i - 1);
        }
        int k = i;
        while (k < json.Length && json[k] != ',' && json[k] != '}' && json[k] != ' ') k++;
        return json.Substring(i, k - i);
    }
    static string Esc(string s) { return s == null ? "" : s.Replace("\\", "\\\\").Replace("\"", "\\\""); }
}
