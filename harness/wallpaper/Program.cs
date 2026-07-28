// collie-wallpaper M4 — WebView2 galaxy pinned behind the icons + INPUT FORWARDING.
// Behind-icons windows get zero OS input, so we synthesize it (exactly like Wallpaper Engine / Lively):
// install low-level mouse + keyboard hooks; when the desktop shell is the foreground surface, forward
// the events by PostMessage to the WebView2 Chromium child (Chrome_WidgetWin_1). Now the on-page chat
// is clickable and typable even though it lives on the wallpaper layer.

using System;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Timer = System.Windows.Forms.Timer;   // disambiguate from System.Threading.Timer
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

class CollieWallpaper : Form
{
    const uint WS_CHILD = 0x40000000, WS_CLIPSIBLINGS = 0x04000000, WS_CLIPCHILDREN = 0x02000000;
    const long WS_POPUP = 0x80000000L, WS_CAPTION = 0x00C00000L, WS_THICKFRAME = 0x00040000L, WS_BORDER = 0x00800000L;
    const int GWL_STYLE = -16, GWL_EXSTYLE = -20;
    const long WS_EX_NOACTIVATE = 0x08000000L, WS_EX_TOOLWINDOW = 0x00000080L;
    const uint SWP_NOACTIVATE = 0x10, SWP_SHOWWINDOW = 0x40, SWP_NOMOVE = 0x2, SWP_NOSIZE = 0x1, SWP_NOZORDER = 0x4;
    const int WM_WINDOWPOSCHANGING = 0x0046;
    [StructLayout(LayoutKind.Sequential)] struct WINDOWPOS { public IntPtr hwnd, hwndInsertAfter; public int x, y, cx, cy; public uint flags; }
    const int WH_MOUSE_LL = 14, WH_KEYBOARD_LL = 13;
    const int WM_MOUSEMOVE = 0x0200, WM_LBUTTONDOWN = 0x0201, WM_LBUTTONUP = 0x0202,
              WM_RBUTTONDOWN = 0x0204, WM_RBUTTONUP = 0x0205, WM_MOUSEWHEEL = 0x020A,
              WM_KEYDOWN = 0x0100, WM_KEYUP = 0x0101, WM_CHAR = 0x0102, WM_SYSKEYDOWN = 0x0104, WM_SYSKEYUP = 0x0105;
    const int MK_LBUTTON = 0x0001, MK_RBUTTON = 0x0002;

    [StructLayout(LayoutKind.Sequential)] struct POINT { public int x, y; }
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int left, top, right, bottom; }
    [StructLayout(LayoutKind.Sequential)] struct MSLLHOOKSTRUCT { public POINT pt; public uint mouseData; public uint flags; public uint time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)] struct KBDLLHOOKSTRUCT { public uint vkCode; public uint scanCode; public uint flags; public uint time; public IntPtr dwExtraInfo; }

    delegate IntPtr HookProc(int nCode, IntPtr wParam, IntPtr lParam);
    delegate bool EnumProc(IntPtr h, IntPtr l);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr FindWindowW(string cls, string name);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr FindWindowExW(IntPtr parent, IntPtr after, string cls, string name);
    [DllImport("user32.dll")] static extern IntPtr GetWindowLongPtrW(IntPtr h, int idx);
    [DllImport("user32.dll")] static extern IntPtr SetWindowLongPtrW(IntPtr h, int idx, IntPtr val);
    [DllImport("user32.dll", SetLastError = true)] static extern IntPtr SetParent(IntPtr child, IntPtr parent);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int w, int hh, uint flags);
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int i);
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassNameW(IntPtr h, StringBuilder s, int m);
    [DllImport("user32.dll")] static extern bool ScreenToClient(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] static extern bool PostMessageW(IntPtr h, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr h, EnumProc cb, IntPtr p);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll", SetLastError = true)] static extern IntPtr SetWindowsHookExW(int id, HookProc proc, IntPtr hMod, uint thread);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr h, int code, IntPtr w, IntPtr l);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr GetModuleHandleW(string name);
    [DllImport("user32.dll")] static extern int ToUnicode(uint vk, uint scan, byte[] state, [Out] StringBuilder buf, int bufLen, uint flags);
    [DllImport("user32.dll")] static extern bool GetKeyboardState(byte[] state);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("kernel32.dll")] static extern uint GetCurrentProcessId();
    [DllImport("user32.dll")] static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool attach);
    [DllImport("user32.dll")] static extern IntPtr SetFocus(IntPtr h);
    [DllImport("user32.dll")] static extern IntPtr SendMessageW(IntPtr h, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] static extern IntPtr SendMessageTimeoutW(IntPtr h, uint msg, IntPtr w, IntPtr l, uint flags, uint timeout, out IntPtr res);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr VirtualAllocEx(IntPtr proc, IntPtr addr, IntPtr size, uint type, uint protect);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool WriteProcessMemory(IntPtr proc, IntPtr addr, byte[] buf, IntPtr size, out IntPtr wrote);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool ReadProcessMemory(IntPtr proc, IntPtr addr, byte[] buf, IntPtr size, out IntPtr read);
    [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr h, ref POINT p);

    static string _log = Path.Combine(Path.GetTempPath(), "collie-wallpaper.log");
    static void Log(string s) { try { File.AppendAllText(_log, DateTime.Now.ToString("HH:mm:ss") + " " + s + "\r\n"); } catch { } }

    WebView2 _web;
    static EventWaitHandle _quit;           // signalled by another process to request a CLEAN shutdown
    static IntPtr _progman, _input;         // Chromium child to post to
    static bool _pinned;                    // once true, WndProc forces our z-order below the icons
    static IntPtr _icons, _iconProc, _iconMem;   // desktop icon ListView + explorer handle + remote LVHITTESTINFO
    static IntPtr _mouseHook, _keyHook;
    static HookProc _mouseProc, _keyProc;   // keep delegates alive
    static EnumProc _enumProc;
    static int _buttons;
    static int _lastMove;                   // throttle mouse-move forwarding (the LL hook fires 100s/sec)
    static IntPtr _enumFound; static int _enumArea;

    // The Collie mark for the window title bar + taskbar. Load the shipped multi-resolution
    // collie.ico (16/48/128) directly — ExtractAssociatedIcon only returns one size and often
    // renders as a generic icon at the taskbar/alt-tab sizes.
    static Icon AppIcon()
    {
        try
        {
            var ico = Path.Combine(Path.GetDirectoryName(Application.ExecutablePath), "collie.ico");
            if (File.Exists(ico)) return new Icon(ico);
        }
        catch { }
        try { return Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { return null; }
    }

    // ONE binary, TWO modes. Default = the behind-the-icons wallpaper. `--window` = an ordinary app
    // window (title bar, taskbar entry, icon) hosting the same page — what the installer's desktop
    // shortcut launches, so a non-technical user gets a real program instead of a browser tab that
    // shows 127.0.0.1:8787 in the address bar and gets lost among their other tabs.
    static bool _windowMode;
    static Mutex _instanceMutex;   // held for the life of the process — keeps duplicate launches out

    [STAThread]
    static void Main(string[] args)
    {
        for (int i = 0; args != null && i < args.Length; i++)
            if (args[i] == "--window" || args[i] == "-w") _windowMode = true;
        // SINGLE-INSTANCE, per mode. The logon autostart + a `collie wallpaper` invocation could each
        // fire the engine, and two instances then fought over the ONE shared WebView2 profile lock —
        // the loser died with exit -1 and the desktop was left BLANK ("the wallpaper won't come back").
        // A named mutex makes every duplicate exit cleanly (0) before it ever touches the profile.
        bool fresh;
        try { _instanceMutex = new Mutex(true, _windowMode ? "collie-wallpaper-window" : "collie-wallpaper-bg", out fresh); }
        catch { fresh = true; }
        if (!fresh) { Log("another " + (_windowMode ? "window" : "wallpaper") + " instance is already running — exiting"); return; }
        try { File.Delete(_log); } catch { }
        Log("start M4 mode=" + (_windowMode ? "window" : "wallpaper"));
        SetProcessDpiAwarenessContext((IntPtr)(-4));
        Application.EnableVisualStyles();
        Application.Run(new CollieWallpaper());
    }

    // Force WS_EX_NOACTIVATE (+ TOOLWINDOW) at handle creation. WinForms manages window styles and
    // overwrites a post-hoc SetWindowLongPtr(GWL_EXSTYLE), so it MUST be set here to stick. Without it
    // the wallpaper could become the foreground window and break desktop icon double-click.
    protected override CreateParams CreateParams
    {
        get
        {
            CreateParams cp = base.CreateParams;
            // window mode wants a NORMAL, activatable, alt-tabbable window — the NOACTIVATE +
            // TOOLWINDOW styles below exist only to keep the WALLPAPER from stealing focus.
            if (!_windowMode) cp.ExStyle |= 0x08000000 | 0x00000080;
            return cp;
        }
    }

    // The CORRECT, event-driven way to stay behind the icons: intercept every z-order change and force
    // our window to insert directly below SHELLDLL_DefView. It can never come on top of the icons — not
    // even for a single frame — so clicking the galaxy no longer makes the icons flash away.
    protected override void WndProc(ref Message m)
    {
        if (m.Msg == WM_WINDOWPOSCHANGING && _pinned && _progman != IntPtr.Zero)
        {
            WINDOWPOS wp = (WINDOWPOS)Marshal.PtrToStructure(m.LParam, typeof(WINDOWPOS));
            IntPtr dv = FindWindowExW(_progman, IntPtr.Zero, "SHELLDLL_DefView", null);
            if (dv != IntPtr.Zero) { wp.hwndInsertAfter = dv; wp.flags &= ~SWP_NOZORDER; Marshal.StructureToPtr(wp, m.LParam, false); }
        }
        base.WndProc(ref m);
    }

    CollieWallpaper()
    {
        int w = GetSystemMetrics(0), h = GetSystemMetrics(1);
        if (_windowMode)
        {
            Text = "Collie";
            FormBorderStyle = FormBorderStyle.Sizable;
            ShowInTaskbar = true;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(Math.Min(1180, (int)(w * 0.8)), Math.Min(820, (int)(h * 0.85)));
            MinimumSize = new Size(720, 520);
            Icon = AppIcon();   // the Collie mark in the title bar + taskbar
        }
        else
        {
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            StartPosition = FormStartPosition.Manual;
            Bounds = new Rectangle(0, 0, w, h);
        }
        BackColor = Color.Black;
        _web = new WebView2();
        _web.Dock = DockStyle.Fill;
        _web.CoreWebView2InitializationCompleted += OnWebReady;
        Controls.Add(_web);
        Load += delegate { InitWeb(); };
        FormClosed += delegate { Cleanup(); };
        // Also tear the hook + input attachment down on ANY process exit / unhandled crash, not only a
        // clean FormClosed — a half-installed hook or a dangling AttachThreadInput must never outlive us.
        AppDomain.CurrentDomain.ProcessExit += delegate { Cleanup(); };
        AppDomain.CurrentDomain.UnhandledException += delegate { Cleanup(); };
        // Clean-shutdown channel: another process Sets this named event -> we Close() gracefully, which
        // disposes WebView2 (browser process exits cleanly) instead of being -Force killed (which orphans
        // COM/GPU processes -> DCOM 10010 storm -> the Hyper-V/WSL network cascade).
        try
        {
            _quit = new EventWaitHandle(false, EventResetMode.AutoReset, "collie-wallpaper-quit");
            var qt = new Thread(delegate () { _quit.WaitOne(); try { BeginInvoke((MethodInvoker)delegate { Close(); }); } catch { } });
            qt.IsBackground = true; qt.Start();
        }
        catch { }
    }

    async void InitWeb()
    {
        try
        {
            // Per-mode profile dir: the wallpaper and the app-window are DIFFERENT processes that may run
            // at the same time; one shared profile means whichever starts second can't lock it and comes
            // up blank. Separate dirs let both live.
            string udf = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                      "collie", _windowMode ? "webview2-win" : "webview2");
            var opts = new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required");
            var env = await CoreWebView2Environment.CreateAsync(null, udf, opts);
            await _web.EnsureCoreWebView2Async(env);
        }
        catch (Exception ex) { Log("InitWeb EXCEPTION: " + ex.Message); }
    }

    void OnWebReady(object sender, CoreWebView2InitializationCompletedEventArgs e)
    {
        if (!e.IsSuccess) { Log("webview init FAILED: " + (e.InitializationException == null ? "?" : e.InitializationException.Message)); return; }
        try
        {
            _web.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
            _web.CoreWebView2.Settings.IsStatusBarEnabled = false;
            _web.CoreWebView2.Settings.IsZoomControlEnabled = false;
            _web.DefaultBackgroundColor = Color.Black;
            // The wallpaper is pinned behind the desktop icons and can never show a permission prompt,
            // so auto-grant microphone — that's what the composer's voice input (Web Speech API) needs.
            _web.CoreWebView2.PermissionRequested += delegate (object s3, CoreWebView2PermissionRequestedEventArgs e3)
            {
                if (e3.PermissionKind == CoreWebView2PermissionKind.Microphone)
                    e3.State = CoreWebView2PermissionState.Allow;
            };
            // URL is passed by `collie wallpaper` via COLLIE_WALLPAPER_URL (the port is picked at
            // runtime, not hardcoded, so it never collides with a busy 8787). Fallback for a manual run.
            string url = Environment.GetEnvironmentVariable("COLLIE_WALLPAPER_URL");
            // window mode shows the full GUI; wallpaper mode shows the desktop /wallpaper page
            if (string.IsNullOrEmpty(url))
                url = _windowMode ? "http://127.0.0.1:8787/" : "http://127.0.0.1:8787/wallpaper";
            // Keep target=_blank links (the star map, the meadow) INSIDE the app. Unhandled they
            // escape to a bare popup / the system browser, which is exactly what makes a native shell
            // feel like a browser wrapper. Each opens its own titled Collie window instead.
            _web.CoreWebView2.NewWindowRequested += delegate (object s2, CoreWebView2NewWindowRequestedEventArgs e2)
            {
                e2.Handled = true;
                if (_windowMode) OpenChildWindow(e2.Uri);
                else _web.CoreWebView2.Navigate(e2.Uri);   // wallpaper has no window manager: navigate in place
            };
            // SELF-HEAL the startup race: right after login the engine can load before the local
            // server binds its port, and WebView2 would then sit on a blank error page FOREVER — the
            // "wallpaper is running but the desktop is blank" bug. Retry every ~2s until it loads.
            _web.CoreWebView2.NavigationCompleted += delegate (object sN, CoreWebView2NavigationCompletedEventArgs eN)
            {
                if (eN.IsSuccess) return;
                var rt = new Timer(); rt.Interval = 2000;
                rt.Tick += delegate { rt.Stop(); rt.Dispose(); try { _web.CoreWebView2.Navigate(url); } catch { } };
                rt.Start();
            };
            _web.CoreWebView2.Navigate(url);
        }
        catch (Exception ex) { Log("navigate EXCEPTION: " + ex.Message); }
        // Everything below is WALLPAPER-only: pinning under the desktop icons and forwarding desktop
        // mouse/keyboard into the page. A normal window is activatable and WebView2 gets input natively.
        if (_windowMode) { Log("window mode: skipping pin + input hooks"); return; }
        Pin();

        // resolve the Chromium child + install input hooks a moment after the page starts
        var t = new Timer();
        t.Interval = 1500;
        t.Tick += delegate
        {
            IntPtr input = FindInput();
            if (input != IntPtr.Zero) { _input = input; }
            if (_input != IntPtr.Zero && _mouseHook == IntPtr.Zero)
            {
                InstallHooks(); Log("input=" + _input + " hooks installed");
                // watchdog: keep the window pinned BELOW the icons (so it can never cover them), and
                // re-resolve the Chromium input HWND if it goes stale (e.g. after a page reload).
                var wd = new Timer(); wd.Interval = 2000;
                wd.Tick += delegate { RepinZ(); if (_input == IntPtr.Zero) { IntPtr ni = FindInput(); if (ni != IntPtr.Zero) _input = ni; } };
                wd.Start();
                if (Environment.GetEnvironmentVariable("COLLIE_SELFTEST") == "1")
                {
                    var st = new Timer(); st.Interval = 2500;
                    st.Tick += delegate { st.Stop(); SelfTest(); };
                    st.Start();
                }
            }
            if (_input != IntPtr.Zero) t.Stop();
        };
        t.Start();
    }

    static bool _attached;
    static void EnsureFocus()
    {
        if (_input == IntPtr.Zero) return;
        uint ipid; uint it = GetWindowThreadProcessId(_input, out ipid);
        uint mt = GetCurrentThreadId();
        if (!_attached && it != mt) { AttachThreadInput(mt, it, true); _attached = true; }
        SetFocus(_input);
    }

    void SelfTest()
    {
        uint ipid; uint it = GetWindowThreadProcessId(_input, out ipid);
        Log("selftest input=" + _input + " inputPid=" + ipid + " ourPid=" + GetCurrentProcessId() + " inputThread=" + it + " ourThread=" + GetCurrentThreadId());
        EnsureFocus();
        int x = 2450, y = 1355; IntPtr lp = (IntPtr)((y << 16) | x);
        PostMessageW(_input, (uint)WM_MOUSEMOVE, IntPtr.Zero, lp);
        PostMessageW(_input, (uint)WM_LBUTTONDOWN, (IntPtr)MK_LBUTTON, lp);
        PostMessageW(_input, (uint)WM_LBUTTONUP, IntPtr.Zero, lp);
        foreach (char c in "hello collie") PostMessageW(_input, (uint)WM_CHAR, (IntPtr)c, IntPtr.Zero);
        Log("selftest posted click+text");
    }

    // A second ordinary Collie window — used for target=_blank links (star map, meadow) so they stay
    // in the app instead of escaping to the browser.
    static void OpenChildWindow(string url)
    {
        try
        {
            Form f = new Form();
            f.Text = "Collie";
            f.StartPosition = FormStartPosition.CenterScreen;
            f.ClientSize = new Size(1100, 780);
            f.BackColor = Color.Black;
            f.Icon = AppIcon();
            WebView2 w = new WebView2();
            w.Dock = DockStyle.Fill;
            w.CoreWebView2InitializationCompleted += delegate
            {
                try { w.CoreWebView2.Navigate(url); } catch (Exception e) { Log("child nav: " + e.Message); }
            };
            f.Controls.Add(w);
            f.Show();
        }
        catch (Exception ex) { Log("child window failed: " + ex.Message); }
    }

    void Pin()
    {
        _progman = FindWindowW("Progman", null);
        // Win10/11: ask Progman to spawn the "behind the icons" WorkerW. On builds where the desktop
        // wallpaper is painted on top of a plain Progman child, this splits the paint onto a WorkerW
        // BELOW us — without it a SetParent-to-Progman child stays hidden under the wallpaper (the
        // "engine runs but the desktop is blank/shows the default wallpaper" case). Harmless if already split.
        IntPtr smRes;
        SendMessageTimeoutW(_progman, 0x052C, IntPtr.Zero, IntPtr.Zero, 0x0002 /*SMTO_ABORTIFHUNG*/, 1000, out smRes);
        IntPtr defview = FindWindowExW(_progman, IntPtr.Zero, "SHELLDLL_DefView", null);
        IntPtr hwnd = this.Handle;
        long style = (long)GetWindowLongPtrW(hwnd, GWL_STYLE);
        style = (style & ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_BORDER)) | WS_CHILD | WS_CLIPSIBLINGS | WS_CLIPCHILDREN;
        SetWindowLongPtrW(hwnd, GWL_STYLE, (IntPtr)style);
        // WS_EX_NOACTIVATE: the wallpaper must NEVER become the foreground/active window. Without this,
        // a forwarded click let Chromium activate our window, so the next desktop click was an "activating
        // click" and icon double-click broke. Keyboard still reaches the chat via AttachThreadInput+SetFocus.
        long ex = (long)GetWindowLongPtrW(hwnd, GWL_EXSTYLE);
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (IntPtr)(ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW));
        SetParent(hwnd, _progman);
        int w = GetSystemMetrics(0), h = GetSystemMetrics(1);
        // Z-order below the icons. If 0x052C reparented SHELLDLL_DefView under a WorkerW (a known
        // Win10-vs-some-Win11 split), the lookup above returns Zero — and SetWindowPos treats Zero as
        // HWND_TOP, which would slam the wallpaper OVER the icons and break double-click. Fall back to
        // HWND_BOTTOM (1) so we can never land on top of the icons even when DefView isn't found.
        IntPtr insertAfter = defview != IntPtr.Zero ? defview : (IntPtr)1;   // (IntPtr)1 = HWND_BOTTOM
        SetWindowPos(hwnd, insertAfter, 0, 0, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW);
        _pinned = true;   // from now on WndProc keeps us below the icons on every z-order change
        Log("pinned progman=" + _progman + " defview=" + defview + " hwnd=" + hwnd + " " + w + "x" + h);
    }

    // Re-assert the wallpaper's z-order directly below the desktop icons. Called on a watchdog timer so
    // the window can never drift on top of the icons (which is what made them "disappear").
    void RepinZ()
    {
        if (_progman == IntPtr.Zero) return;
        IntPtr defview = FindWindowExW(_progman, IntPtr.Zero, "SHELLDLL_DefView", null);
        if (defview != IntPtr.Zero) SetWindowPos(this.Handle, defview, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
    }

    IntPtr FindInput()
    {
        _enumFound = IntPtr.Zero; _enumArea = 0;
        if (_enumProc == null) _enumProc = new EnumProc(EnumCb);
        EnumChildWindows(this.Handle, _enumProc, IntPtr.Zero);
        return _enumFound;
    }
    static bool EnumCb(IntPtr h, IntPtr l)
    {
        StringBuilder c = new StringBuilder(64); GetClassNameW(h, c, 64);
        if (c.ToString() == "Chrome_WidgetWin_1")
        {
            RECT r; GetWindowRect(h, out r);
            int a = (r.right - r.left) * (r.bottom - r.top);
            if (a > _enumArea) { _enumArea = a; _enumFound = h; }
        }
        return true;
    }

    static System.Threading.SynchronizationContext _uiCtx;   // the UI message loop, for deferring focus

    void InstallHooks()
    {
        _uiCtx = System.Threading.SynchronizationContext.Current;
        IntPtr hMod = GetModuleHandleW(null);
        _mouseProc = new HookProc(MouseProc);
        _mouseHook = SetWindowsHookExW(WH_MOUSE_LL, _mouseProc, hMod, 0);
        // NO keyboard hook: a click's EnsureFocus() gives the Chromium window REAL keyboard focus, so
        // Windows delivers keystrokes AND IME composition (Chinese/日本語) to it natively. Forwarding
        // keys on top of that doubled every character. Mouse still must be forwarded (hit-tested by
        // z-order, so desktop clicks never reach a behind-icons window).
        SetupIconHitTest();
        RefreshIconRects();
        var rt = new Timer(); rt.Interval = 1500; rt.Tick += delegate { RefreshIconRects(); }; rt.Start();
    }

    // Cache every desktop-icon rectangle (screen coords). Done on the UI thread via a timer — NEVER inside
    // the mouse hook — so the hook can decide "over an icon?" with a cheap cached rect test and no blocking call.
    static RECT[] _iconRects = new RECT[0];
    void RefreshIconRects()
    {
        if (_icons == IntPtr.Zero || _iconMem == IntPtr.Zero) return;
        int n = SendMessageW(_icons, 0x1004 /* LVM_GETITEMCOUNT */, IntPtr.Zero, IntPtr.Zero).ToInt32();
        if (n < 0) n = 0; if (n > 1000) n = 1000;
        RECT[] arr = new RECT[n]; int cnt = 0;
        for (int i = 0; i < n; i++)
        {
            byte[] rb = new byte[16]; IntPtr w;                       // left=0 => LVIR_BOUNDS
            WriteProcessMemory(_iconProc, _iconMem, rb, (IntPtr)16, out w);
            SendMessageW(_icons, 0x100E /* LVM_GETITEMRECT */, (IntPtr)i, _iconMem);
            byte[] rb2 = new byte[16]; IntPtr rd;
            if (!ReadProcessMemory(_iconProc, _iconMem, rb2, (IntPtr)16, out rd)) continue;
            POINT tl; tl.x = BitConverter.ToInt32(rb2, 0); tl.y = BitConverter.ToInt32(rb2, 4);
            POINT br; br.x = BitConverter.ToInt32(rb2, 8); br.y = BitConverter.ToInt32(rb2, 12);
            ClientToScreen(_icons, ref tl); ClientToScreen(_icons, ref br);
            RECT r; r.left = tl.x; r.top = tl.y; r.right = br.x; r.bottom = br.y;
            arr[cnt++] = r;
        }
        RECT[] outp = new RECT[cnt]; Array.Copy(arr, outp, cnt); _iconRects = outp;
        if (!_dumped && cnt > 0) { _dumped = true; for (int i = 0; i < cnt; i++) Log("ICONRECT[" + i + "] " + outp[i].left + "," + outp[i].top + " " + (outp[i].right - outp[i].left) + "x" + (outp[i].bottom - outp[i].top)); }
    }
    static bool _dumped = false;
    static bool OverIconCached(int sx, int sy)
    {
        RECT[] a = _iconRects;
        for (int i = 0; i < a.Length; i++) if (sx >= a[i].left && sx < a[i].right && sy >= a[i].top && sy < a[i].bottom) return true;
        return false;
    }
    static void DetachInput()
    {
        // Undo the AttachThreadInput(mt, it, true) EnsureFocus made — a cross-process input attachment
        // left dangling when our thread dies is a classic way to wedge the system input queue.
        try
        {
            if (_attached && _input != IntPtr.Zero)
            {
                uint ipid; uint it = GetWindowThreadProcessId(_input, out ipid);
                uint mt = GetCurrentThreadId();
                if (it != mt) AttachThreadInput(mt, it, false);
            }
        }
        catch { }
        _attached = false;
    }

    static bool _cleaned;
    void Cleanup()
    {
        if (_cleaned) return; _cleaned = true;
        if (_mouseHook != IntPtr.Zero) UnhookWindowsHookEx(_mouseHook);
        if (_keyHook != IntPtr.Zero) UnhookWindowsHookEx(_keyHook);
        DetachInput();
        try { if (_web != null) { _web.Dispose(); } } catch { }   // dispose WebView2 -> browser process exits cleanly (no orphaned COM)
    }

    // Resolve the desktop icon ListView and prepare a remote LVHITTESTINFO in explorer's address space,
    // so we can ask "is a real icon under the cursor?" (LVM_HITTEST) before deciding to forward a click.
    void SetupIconHitTest()
    {
        IntPtr defview = FindWindowExW(_progman, IntPtr.Zero, "SHELLDLL_DefView", null);
        _icons = FindWindowExW(defview, IntPtr.Zero, "SysListView32", null);
        if (_icons == IntPtr.Zero) { Log("icons listview not found"); return; }
        uint pid; GetWindowThreadProcessId(_icons, out pid);
        _iconProc = OpenProcess(0x0008 | 0x0010 | 0x0020, false, pid); // VM_OPERATION | VM_READ | VM_WRITE
        if (_iconProc != IntPtr.Zero) _iconMem = VirtualAllocEx(_iconProc, IntPtr.Zero, (IntPtr)32, 0x3000, 0x04);
        Log("iconhittest icons=" + _icons + " proc=" + _iconProc + " mem=" + _iconMem);
    }
    static bool OverIcon(int sx, int sy)
    {
        if (_icons == IntPtr.Zero || _iconMem == IntPtr.Zero) return false;
        POINT p; p.x = sx; p.y = sy; ScreenToClient(_icons, ref p);
        byte[] buf = new byte[32];
        BitConverter.GetBytes(p.x).CopyTo(buf, 0);
        BitConverter.GetBytes(p.y).CopyTo(buf, 4);
        IntPtr wrote;
        WriteProcessMemory(_iconProc, _iconMem, buf, (IntPtr)32, out wrote);
        IntPtr r = SendMessageW(_icons, 0x1012 /* LVM_HITTEST */, IntPtr.Zero, _iconMem);
        return r.ToInt64() >= 0;   // >=0 means an icon item is under the cursor
    }

    // Cheap rectangle test for "is this click in the chat box?" (bottom-center, ~92px above the taskbar).
    // Used to decide whether to grab keyboard focus — no cross-process calls, safe inside the LL hook.
    static bool InChat(int sx, int sy)
    {
        int w = GetSystemMetrics(0), h = GetSystemMetrics(1);
        int cw = Math.Min(680, (int)(w * 0.92));
        int cx = w / 2, halfx = cw / 2 + 30;
        int bottom = h - 92 + 8, top = h - 92 - 380;   // generous upward for a grown log/composer
        return sx >= cx - halfx && sx <= cx + halfx && sy >= top && sy <= bottom;
    }

    static bool DesktopIsForeground()
    {
        IntPtr fg = GetForegroundWindow();
        if (fg == _progman) return true;
        StringBuilder c = new StringBuilder(32); GetClassNameW(fg, c, 32);
        string s = c.ToString();
        return s == "WorkerW" || s == "Progman";
    }

    static IntPtr MouseProc(int nCode, IntPtr wParam, IntPtr lParam)
    {
        // Keep this callback CHEAP — it runs for every mouse event system-wide. No file I/O, no blocking
        // calls, and a fast early-out over desktop icons so Explorer's click/double-click is never delayed.
        if (nCode >= 0 && _input != IntPtr.Zero)
        {
            int msg = (int)wParam;
            bool isBtn = (msg == WM_LBUTTONDOWN || msg == WM_LBUTTONUP || msg == WM_RBUTTONDOWN || msg == WM_RBUTTONUP);
            if (isBtn || msg == WM_MOUSEMOVE || msg == WM_MOUSEWHEEL)
            {
                // THROTTLE moves to ~70Hz. Forwarding every raw move floods Chromium (behind the icons)
                // with repaints → flicker + laggy clicks. Buttons/wheel are rare, never throttled.
                if (msg == WM_MOUSEMOVE)
                {
                    int now = Environment.TickCount;
                    if (now - _lastMove < 14) return CallNextHookEx(_mouseHook, nCode, wParam, lParam);
                    _lastMove = now;
                }
                MSLLHOOKSTRUCT m = (MSLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(MSLLHOOKSTRUCT));
                // over a real icon on a click => do nothing (short-circuits before any other work)
                if (!(isBtn && OverIconCached(m.pt.x, m.pt.y)) && DesktopIsForeground())
                {
                    if (msg == WM_MOUSEWHEEL)
                    {
                        int delta = (short)((m.mouseData >> 16) & 0xFFFF);
                        PostMessageW(_input, WM_MOUSEWHEEL, (IntPtr)(delta << 16), (IntPtr)((m.pt.y << 16) | (m.pt.x & 0xFFFF)));
                    }
                    else
                    {
                        // DEFER focus off the hook callback. EnsureFocus() does a synchronous, cross-process
                        // AttachThreadInput+SetFocus; running it INSIDE a WH_MOUSE_LL callback stalls the
                        // SYSTEM-WIDE mouse queue (a slow/blocked call froze left-click everywhere). BeginInvoke
                        // queues it onto our message loop, so the hook returns immediately.
                        if (msg == WM_LBUTTONDOWN) { _buttons |= MK_LBUTTON; if (InChat(m.pt.x, m.pt.y)) { var ctx = _uiCtx; if (ctx != null) { try { ctx.Post(delegate { EnsureFocus(); }, null); } catch { } } } }
                        else if (msg == WM_LBUTTONUP) _buttons &= ~MK_LBUTTON;
                        else if (msg == WM_RBUTTONDOWN) _buttons |= MK_RBUTTON;
                        else if (msg == WM_RBUTTONUP) _buttons &= ~MK_RBUTTON;
                        POINT c = m.pt; ScreenToClient(_input, ref c);
                        PostMessageW(_input, (uint)msg, (IntPtr)_buttons, (IntPtr)((c.y << 16) | (c.x & 0xFFFF)));
                    }
                }
            }
        }
        return CallNextHookEx(_mouseHook, nCode, wParam, lParam);
    }

    static IntPtr KeyProc(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0 && _input != IntPtr.Zero && DesktopIsForeground())
        {
            int msg = (int)wParam;
            KBDLLHOOKSTRUCT k = (KBDLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(KBDLLHOOKSTRUCT));
            bool down = (msg == WM_KEYDOWN || msg == WM_SYSKEYDOWN);
            uint scan = k.scanCode;
            IntPtr lp = down ? (IntPtr)(1 | (int)(scan << 16)) : (IntPtr)(1 | (int)(scan << 16) | (0xC0 << 24));
            // Post ONLY WM_KEYDOWN/WM_KEYUP — Chromium's own message pump runs TranslateMessage and
            // generates WM_CHAR itself. Posting WM_CHAR too would double every character.
            PostMessageW(_input, (uint)(down ? WM_KEYDOWN : WM_KEYUP), (IntPtr)k.vkCode, lp);
        }
        return CallNextHookEx(_keyHook, nCode, wParam, lParam);
    }
}
