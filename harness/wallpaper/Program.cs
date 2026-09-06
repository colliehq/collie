// collie-wallpaper M4 — WebView2 galaxy pinned behind the icons + INPUT FORWARDING.
// Behind-icons windows get zero OS input, so we synthesize it (exactly like Wallpaper Engine / Lively):
// install low-level mouse + keyboard hooks; when the desktop shell is the foreground surface, forward
// the events by PostMessage to the WebView2 Chromium child (Chrome_WidgetWin_1). Now the on-page chat
// is clickable and typable even though it lives on the wallpaper layer.

using System;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Speech.Recognition;
using System.Speech.Synthesis;
using System.Text;
using System.Text.RegularExpressions;
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
    const int WM_WINDOWPOSCHANGING = 0x0046, WM_NCHITTEST = 0x0084, WM_NCLBUTTONDOWN = 0x00A1,
              WM_SYSCOMMAND = 0x0112, WM_HOTKEY = 0x0312;
    const int LIVE_HANDOFF_HOTKEY = 0xC011;
    const uint MOD_ALT = 0x0001, MOD_CONTROL = 0x0002, MOD_NOREPEAT = 0x4000;
    const int SC_MAXIMIZE = 0xF030, SC_RESTORE = 0xF120;
    const int HTCLIENT = 1, HTCAPTION = 2, HTLEFT = 10, HTRIGHT = 11, HTTOP = 12,
              HTTOPLEFT = 13, HTTOPRIGHT = 14, HTBOTTOM = 15, HTBOTTOMLEFT = 16,
              HTBOTTOMRIGHT = 17;
    [StructLayout(LayoutKind.Sequential)] struct WINDOWPOS { public IntPtr hwnd, hwndInsertAfter; public int x, y, cx, cy; public uint flags; }
    const int WH_MOUSE_LL = 14, WH_KEYBOARD_LL = 13;
    const int WM_MOUSEMOVE = 0x0200, WM_LBUTTONDOWN = 0x0201, WM_LBUTTONUP = 0x0202,
              WM_RBUTTONDOWN = 0x0204, WM_RBUTTONUP = 0x0205, WM_MOUSEWHEEL = 0x020A,
              WM_XBUTTONDOWN = 0x020B, WM_XBUTTONUP = 0x020C,
              WM_KEYDOWN = 0x0100, WM_KEYUP = 0x0101, WM_CHAR = 0x0102, WM_SYSKEYDOWN = 0x0104, WM_SYSKEYUP = 0x0105;
    const int MK_LBUTTON = 0x0001, MK_RBUTTON = 0x0002, XBUTTON2 = 0x0002;

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
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowTextLengthW(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowTextW(IntPtr h, StringBuilder s, int m);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassNameW(IntPtr h, StringBuilder s, int m);
    [DllImport("user32.dll")] static extern bool ScreenToClient(IntPtr h, ref POINT p);
    [DllImport("user32.dll")] static extern bool PostMessageW(IntPtr h, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr h, EnumProc cb, IntPtr p);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] static extern bool ReleaseCapture();
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int command);
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll", SetLastError = true)] static extern bool RegisterHotKey(IntPtr h, int id, uint modifiers, uint key);
    [DllImport("user32.dll", SetLastError = true)] static extern bool UnregisterHotKey(IntPtr h, int id);
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
    [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr h, int attr, ref int val, int size);

    // The title bar is drawn by DWM, not by us and not by the page — so a dark UI inside a window
    // whose caption stays white is not a CSS problem, it is a window that was never told. 20 is
    // DWMWA_USE_IMMERSIVE_DARK_MODE on Windows 10 20H1 and later; before that the same flag lived at
    // 19. Try the current one, and fall back only if it is rejected.
    static bool _titleBarDark;
    void ApplyTitleBarTheme(bool dark)
    {
        if (!_windowMode || !IsHandleCreated) return;
        _titleBarDark = dark;
        int on = dark ? 1 : 0;
        if (DwmSetWindowAttribute(Handle, 20, ref on, sizeof(int)) != 0)
            DwmSetWindowAttribute(Handle, 19, ref on, sizeof(int));
        // DWM repaints the caption on the next frame; nudge it so the change is not deferred until
        // the user happens to move or focus the window.
        SetWindowPos(Handle, IntPtr.Zero, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020);
    }

    void ReportWindowState()
    {
        if (!_windowMode || _web == null || _web.CoreWebView2 == null) return;
        try
        {
            _web.CoreWebView2.PostWebMessageAsJson("{\"type\":\"window-state\",\"maximized\":" +
                (_customMaximized ? "true" : "false") + "}");
        }
        catch { }
    }

    // WinForms' built-in Maximized state is not reliable for a FormBorderStyle.None window. On a
    // wide/high-DPI desktop it can apply the working-area offset twice (we observed x=1288 with a
    // 5120px-wide window on a 0..5120 screen), pushing Collie's restore button off-screen. Keep the
    // ordinary bounds ourselves and make maximization a plain, reversible geometry change instead.
    Rectangle _restoreBounds = Rectangle.Empty;
    bool _customMaximized, _changingWindowBounds;

    Rectangle DefaultRestoreBounds(Screen screen)
    {
        Rectangle work = screen.WorkingArea;
        int width = Math.Min(1180, Math.Max(MinimumSize.Width, (int)(work.Width * 0.8)));
        int height = Math.Min(820, Math.Max(MinimumSize.Height, (int)(work.Height * 0.85)));
        return new Rectangle(work.Left + Math.Max(0, (work.Width - width) / 2),
                             work.Top + Math.Max(0, (work.Height - height) / 2), width, height);
    }

    bool RestoreBoundsAreUsable(Rectangle value)
    {
        if (value.Width < MinimumSize.Width || value.Height < MinimumSize.Height) return false;
        foreach (Screen screen in Screen.AllScreens)
        {
            Rectangle visible = Rectangle.Intersect(value, screen.WorkingArea);
            if (visible.Width >= 160 && visible.Height >= 80) return true;
        }
        return false;
    }

    void RememberRestoreBounds()
    {
        if (!_windowMode || _customMaximized || _changingWindowBounds ||
            WindowState != FormWindowState.Normal || !Visible) return;
        if (Bounds.Width >= MinimumSize.Width && Bounds.Height >= MinimumSize.Height)
            _restoreBounds = Bounds;
    }

    void ToggleMaximizeWindow()
    {
        if (_customMaximized || WindowState == FormWindowState.Maximized)
        {
            RestoreWindow();
            return;
        }
        if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        Rectangle current = Bounds;
        if (current.Width >= MinimumSize.Width && current.Height >= MinimumSize.Height)
            _restoreBounds = current;
        Screen screen = Screen.FromHandle(Handle);
        _changingWindowBounds = true;
        try
        {
            // Set the flag before Bounds: the resulting Resize event must not overwrite the saved
            // normal rectangle with the full-screen rectangle.
            _customMaximized = true;
            WindowState = FormWindowState.Normal;
            Bounds = screen.WorkingArea;
        }
        finally { _changingWindowBounds = false; }
        ReportWindowState();
    }

    void RestoreWindow()
    {
        Screen screen = Screen.FromHandle(Handle);
        Rectangle target = RestoreBoundsAreUsable(_restoreBounds) ? _restoreBounds : DefaultRestoreBounds(screen);
        _changingWindowBounds = true;
        try
        {
            WindowState = FormWindowState.Normal;
            Bounds = target;
            _customMaximized = false;
        }
        finally { _changingWindowBounds = false; }
        ReportWindowState();
    }

    void BeginWindowDrag()
    {
        if (_customMaximized)
        {
            // Match native Windows: pulling a maximized title bar restores the window under the
            // pointer and immediately continues the drag, instead of making the title bar feel dead.
            POINT cursor;
            if (GetCursorPos(out cursor))
            {
                Screen screen = Screen.FromPoint(new Point(cursor.x, cursor.y));
                Rectangle work = screen.WorkingArea;
                Rectangle target = RestoreBoundsAreUsable(_restoreBounds) ? _restoreBounds : DefaultRestoreBounds(screen);
                double ratio = work.Width > 0 ? (cursor.x - work.Left) / (double)work.Width : 0.5;
                ratio = Math.Max(0.1, Math.Min(0.9, ratio));
                target.X = cursor.x - (int)(target.Width * ratio);
                target.Y = work.Top;
                _changingWindowBounds = true;
                try
                {
                    WindowState = FormWindowState.Normal;
                    Bounds = target;
                    _restoreBounds = target;
                    _customMaximized = false;
                }
                finally { _changingWindowBounds = false; }
                ReportWindowState();
            }
            else RestoreWindow();
        }
        ReleaseCapture();
        SendMessageW(Handle, WM_NCLBUTTONDOWN, (IntPtr)HTCAPTION, IntPtr.Zero);
    }

    void WakeWindow()
    {
        if (!_windowMode || IsDisposed) return;
        if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        Show();
        Activate();
        BringToFront();
        SetForegroundWindow(Handle);
        ReportWindowState();
    }

    static string _log = Path.Combine(Path.GetTempPath(), "collie-wallpaper.log");
    static void Log(string s) { try { File.AppendAllText(_log, DateTime.Now.ToString("HH:mm:ss") + " " + s + "\r\n"); } catch { } }

    WebView2 _web;
    static EventWaitHandle _quit;           // signalled by another process to request a CLEAN shutdown
    static EventWaitHandle _show;           // a second shortcut launch restores/focuses the app window
    static Form _capsuleForm;
    static WebView2 _capsuleWeb;
    static SpeechRecognitionEngine _capsuleSpeech;
    static bool _capsuleSpeechDelivered;
    static bool _capsuleStopOnRelease;
    static bool _capsulePttMode, _capsulePttHeld;
    static CollieWallpaper _mainForm;
    static WebView2 _mainWeb;
    static SpeechRecognitionEngine _liveSpeech;
    static SpeechSynthesizer _liveVoice;
    static bool _liveVoiceSpeaking;
    static int _liveVoiceGeneration;
    static string _liveVoiceLastCue = "";
    static bool _liveSpeechWanted;
    static string _liveSpeechSession = "", _liveSpeechLanguage = "";
    static string _liveSpeechFailedFor = "";
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
    string _baseUrl = "http://127.0.0.1:8787";

    class LiveTarget
    {
        public IntPtr Hwnd;
        public uint Pid;
        public string ProcessName = "";
        public string Title = "";
        public string SpeechLanguage = "";
    }

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
        if (!fresh)
        {
            Log("another " + (_windowMode ? "window" : "wallpaper") + " instance is already running — exiting");
            if (_windowMode)
            {
                // A user commonly clicks the desktop/Start shortcut after minimizing Collie. The old
                // single-instance path silently exited, which looked as though the app was broken.
                try { using (EventWaitHandle show = EventWaitHandle.OpenExisting("collie-wallpaper-show-window")) show.Set(); }
                catch { }
                try
                {
                    IntPtr existing = FindWindowW(null, "Collie");
                    if (existing != IntPtr.Zero) { ShowWindow(existing, 9); SetForegroundWindow(existing); }
                }
                catch { }
            }
            return;
        }
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
        if (_windowMode && m.Msg == WM_HOTKEY && m.WParam.ToInt32() == LIVE_HANDOFF_HOTKEY)
        {
            // Capture the user's exact app BEFORE our activatable capsule takes focus. The full
            // Collie window stays where it was (usually minimized); only the lightweight voice
            // capsule appears. No keyboard hook or background key logging is involved.
            OpenLiveCapsule(CaptureLiveTarget());
            return;
        }
        if (_windowMode && m.Msg == WM_SYSCOMMAND)
        {
            int command = unchecked((int)((long)m.WParam)) & 0xFFF0;
            if (command == SC_MAXIMIZE)
            {
                if (!_customMaximized) ToggleMaximizeWindow();
                return;
            }
            // Restoring a minimized maximized window should bring it back full-size. Only Win+Down
            // (SC_RESTORE while it is visible) means "leave maximized mode".
            if (command == SC_RESTORE && _customMaximized && WindowState != FormWindowState.Minimized)
            {
                RestoreWindow();
                return;
            }
        }
        // The app window uses Collie's own integrated chrome. Preserve native resizing by returning
        // the standard non-client hit-test codes around an eight-pixel edge; the WebView owns the
        // rest of the surface and asks us to drag the window through WM_NCLBUTTONDOWN.
        if (_windowMode && m.Msg == WM_NCHITTEST)
        {
            base.WndProc(ref m);
            if ((int)m.Result == HTCLIENT && WindowState == FormWindowState.Normal && !_customMaximized)
            {
                POINT cursor;
                if (GetCursorPos(out cursor))
                {
                    Point point = PointToClient(new Point(cursor.x, cursor.y));
                    const int grip = 8;
                    bool left = point.X < grip, right = point.X >= ClientSize.Width - grip;
                    bool top = point.Y < grip, bottom = point.Y >= ClientSize.Height - grip;
                    int hit = top && left ? HTTOPLEFT : top && right ? HTTOPRIGHT :
                              bottom && left ? HTBOTTOMLEFT : bottom && right ? HTBOTTOMRIGHT :
                              left ? HTLEFT : right ? HTRIGHT : top ? HTTOP : bottom ? HTBOTTOM : HTCLIENT;
                    m.Result = (IntPtr)hit;
                }
            }
            return;
        }
        if (m.Msg == WM_WINDOWPOSCHANGING && _pinned && _progman != IntPtr.Zero)
        {
            WINDOWPOS wp = (WINDOWPOS)Marshal.PtrToStructure(m.LParam, typeof(WINDOWPOS));
            IntPtr dv = FindWindowExW(_progman, IntPtr.Zero, "SHELLDLL_DefView", null);
            if (dv != IntPtr.Zero) { wp.hwndInsertAfter = dv; wp.flags &= ~SWP_NOZORDER; Marshal.StructureToPtr(wp, m.LParam, false); }
        }
        base.WndProc(ref m);
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        // Start dark rather than starting light and correcting: the client area is black from the
        // first frame, so a light caption would flash before the page finishes loading and reports
        // its real theme. The page's own message (below) is what settles it either way.
        ApplyTitleBarTheme(true);
        if (_windowMode && !RegisterHotKey(Handle, LIVE_HANDOFF_HOTKEY,
                                           MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x20))
            Log("live handoff hotkey unavailable error=" + Marshal.GetLastWin32Error());
    }

    CollieWallpaper()
    {
        _mainForm = this;
        int w = GetSystemMetrics(0), h = GetSystemMetrics(1);
        if (_windowMode)
        {
            Text = "Collie";
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = true;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(Math.Min(1180, (int)(w * 0.8)), Math.Min(820, (int)(h * 0.85)));
            MinimumSize = new Size(720, 520);
            Padding = new Padding(1);
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
        Resize += delegate { RememberRestoreBounds(); ReportWindowState(); };
        Move += delegate { RememberRestoreBounds(); };
        Shown += delegate { RememberRestoreBounds(); ReportWindowState(); };
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
            // Per-mode name: the quit event is AutoReset, so one Set wakes ONE waiter — with a shared
            // name, "stop the wallpaper" could just as easily close the app WINDOW (same exe, both
            // listening). The wallpaper keeps the historic name so existing stop paths still work.
            _quit = new EventWaitHandle(false, EventResetMode.AutoReset,
                                        _windowMode ? "collie-wallpaper-quit-window" : "collie-wallpaper-quit");
            var qt = new Thread(delegate () { _quit.WaitOne(); try { BeginInvoke((MethodInvoker)delegate { Close(); }); } catch { } });
            qt.IsBackground = true; qt.Start();
        }
        catch { }
        if (_windowMode)
        {
            try
            {
                _show = new EventWaitHandle(false, EventResetMode.AutoReset, "collie-wallpaper-show-window");
                var st = new Thread(delegate ()
                {
                    while (true)
                    {
                        _show.WaitOne();
                        try { BeginInvoke((MethodInvoker)delegate { WakeWindow(); }); }
                        catch { return; }
                    }
                });
                st.IsBackground = true;
                st.Start();
            }
            catch { }
        }
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
            _env = env;   // child windows (the star map, the meadow) initialise from this same profile
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
            _mainWeb = _web;
            _web.DefaultBackgroundColor = Color.Black;
            // The page owns the theme (a saved choice, else the system's) and can flip it at any
            // time from the toggle in its header. It posts {type:"theme",dark:bool}; the caption is
            // ours to repaint, so this is the only way the two can agree.
            _web.CoreWebView2.WebMessageReceived += delegate (object sT, CoreWebView2WebMessageReceivedEventArgs eT)
            {
                // WebMessageAsJson, NOT TryGetWebMessageAsString: postMessage is called with an
                // OBJECT, and the string accessor throws for anything that is not a bare string —
                // so every theme report was dropped and the caption never followed the page.
                string raw = null;
                try { raw = eT.WebMessageAsJson; } catch { }
                if (string.IsNullOrEmpty(raw)) { try { raw = eT.TryGetWebMessageAsString(); } catch { return; } }
                if (!string.IsNullOrEmpty(raw) && raw.IndexOf("\"type\":\"live-native-speak\"", StringComparison.Ordinal) >= 0)
                {
                    SpeakLiveCue(JsonField(raw, "session"), JsonField(raw, "cue_id"),
                                 JsonField(raw, "text"), JsonField(raw, "language"));
                    return;
                }
                if (!string.IsNullOrEmpty(raw) && raw.IndexOf("\"type\":\"live-native-state\"", StringComparison.Ordinal) >= 0)
                {
                    bool active = Regex.IsMatch(raw, "\\\"active\\\"\\s*:\\s*true", RegexOptions.IgnoreCase);
                    bool listen = Regex.IsMatch(raw, "\\\"listen\\\"\\s*:\\s*true", RegexOptions.IgnoreCase);
                    ConfigureLiveSpeech(active && listen, JsonField(raw, "session"),
                                        JsonField(raw, "language"));
                    return;
                }
                if (!string.IsNullOrEmpty(raw) && raw.IndexOf("\"type\":\"window\"", StringComparison.Ordinal) >= 0)
                {
                    try
                    {
                        BeginInvoke((MethodInvoker)delegate
                        {
                            if (raw.IndexOf("\"action\":\"minimize\"", StringComparison.Ordinal) >= 0)
                                WindowState = FormWindowState.Minimized;
                            else if (raw.IndexOf("\"action\":\"maximize\"", StringComparison.Ordinal) >= 0)
                                ToggleMaximizeWindow();
                            else if (raw.IndexOf("\"action\":\"close\"", StringComparison.Ordinal) >= 0)
                                Close();
                            else if (raw.IndexOf("\"action\":\"drag\"", StringComparison.Ordinal) >= 0)
                                BeginWindowDrag();
                        });
                    }
                    catch { }
                    return;
                }
                if (string.IsNullOrEmpty(raw) || raw.IndexOf("\"theme\"", StringComparison.Ordinal) < 0) return;
                bool dark = raw.IndexOf("\"dark\":true", StringComparison.Ordinal) >= 0
                            || raw.IndexOf("\"dark\": true", StringComparison.Ordinal) >= 0;
                if (dark == _titleBarDark) return;
                try { BeginInvoke((MethodInvoker)delegate { ApplyTitleBarTheme(dark); }); } catch { }
            };
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
            try { _baseUrl = new Uri(url).GetLeftPart(UriPartial.Authority); } catch { }
            if (_windowMode)
                url += (url.IndexOf('?') >= 0 ? "&" : "?") + "native_shell=1";
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
        if (_windowMode)
        {
            // The native app has no wallpaper input child, but it still owns the global Live
            // handoff. Install only the low-level mouse hook so X2 can be the push-to-talk gesture;
            // MouseProc returns immediately for every other event and never records mouse data.
            if (_mouseHook == IntPtr.Zero) InstallHooks();
            Log("window mode: X2 Live handoff hook installed; skipping pin + desktop forwarding");
            return;
        }
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

    static LiveTarget CaptureLiveTarget()
    {
        LiveTarget target = new LiveTarget();
        try
        {
            target.Hwnd = GetForegroundWindow();
            target.Pid = 0;
            if (target.Hwnd != IntPtr.Zero)
            {
                GetWindowThreadProcessId(target.Hwnd, out target.Pid);
                int n = Math.Min(4096, Math.Max(1, GetWindowTextLengthW(target.Hwnd) + 1));
                StringBuilder title = new StringBuilder(n);
                GetWindowTextW(target.Hwnd, title, title.Capacity);
                target.Title = title.ToString();
                try { target.ProcessName = Process.GetProcessById((int)target.Pid).ProcessName; }
                catch { }
            }
            try { target.SpeechLanguage = InputLanguage.CurrentInputLanguage.Culture.Name; }
            catch { target.SpeechLanguage = CultureInfo.CurrentUICulture.Name; }
        }
        catch { }
        return target;
    }

    static string JsonString(string value)
    {
        StringBuilder output = new StringBuilder("\"");
        foreach (char c in value ?? "")
        {
            switch (c)
            {
                case '\\': output.Append("\\\\"); break;
                case '"': output.Append("\\\""); break;
                case '\r': output.Append("\\r"); break;
                case '\n': output.Append("\\n"); break;
                case '\t': output.Append("\\t"); break;
                default:
                    if (c < 32) output.Append("\\u" + ((int)c).ToString("x4"));
                    else output.Append(c);
                    break;
            }
        }
        output.Append('"');
        return output.ToString();
    }

    static string JsonField(string json, string name)
    {
        try
        {
            Match match = Regex.Match(json ?? "", "\\\"" + Regex.Escape(name) +
                "\\\"\\s*:\\s*\\\"((?:\\\\.|[^\\\"\\\\])*)\\\"");
            if (!match.Success) return "";
            return match.Groups[1].Value.Replace("\\\"", "\"").Replace("\\\\", "\\");
        }
        catch { return ""; }
    }

    static void PostMain(string json)
    {
        WebView2 web = _mainWeb;
        if (web == null || web.IsDisposed) return;
        try
        {
            if (web.InvokeRequired)
            {
                web.BeginInvoke((MethodInvoker)delegate { PostMain(json); });
                return;
            }
            if (web.CoreWebView2 != null) web.CoreWebView2.PostWebMessageAsJson(json);
        }
        catch { }
    }

    static void StopLiveSpeechEngine()
    {
        SpeechRecognitionEngine engine = _liveSpeech;
        _liveSpeech = null;
        if (engine == null) return;
        try { engine.RecognizeAsyncCancel(); } catch { }
        try { engine.SetInputToNull(); } catch { }
        try { engine.Dispose(); } catch { }
    }

    static void StopLiveVoice()
    {
        _liveVoiceGeneration++;
        SpeechSynthesizer voice = _liveVoice;
        _liveVoice = null;
        _liveVoiceSpeaking = false;
        if (voice == null) return;
        try { voice.SpeakAsyncCancelAll(); } catch { }
        try { voice.Dispose(); } catch { }
    }

    static void SpeakLiveCue(string session, string cueId, string text, string language)
    {
        session = (session ?? "").Trim(); cueId = (cueId ?? "").Trim();
        text = (text ?? "").Trim(); language = (language ?? "").Trim();
        if (session.Length == 0 || session != _liveSpeechSession || text.Length == 0 ||
            (cueId.Length > 0 && cueId == _liveVoiceLastCue)) return;
        if (cueId.Length > 0) _liveVoiceLastCue = cueId;
        StopLiveSpeechEngine();
        StopLiveVoice();
        _liveVoiceSpeaking = true;
        int generation = ++_liveVoiceGeneration;
        SpeechSynthesizer voice = new SpeechSynthesizer();
        _liveVoice = voice;
        try
        {
            CultureInfo culture = new CultureInfo(language.StartsWith("zh", StringComparison.OrdinalIgnoreCase)
                                                  ? "zh-CN" : "en-US");
            voice.SelectVoiceByHints(VoiceGender.NotSet, VoiceAge.NotSet, 0, culture);
        }
        catch { }
        voice.Rate = 2;
        voice.Volume = 100;
        voice.SpeakCompleted += delegate
        {
            try { voice.Dispose(); } catch { }
            if (generation != _liveVoiceGeneration) return;
            _liveVoice = null;
            _liveVoiceSpeaking = false;
            ResumeLiveSpeech();
        };
        try { voice.SpeakAsync(text); }
        catch
        {
            try { voice.Dispose(); } catch { }
            if (generation == _liveVoiceGeneration)
            {
                _liveVoice = null; _liveVoiceSpeaking = false; ResumeLiveSpeech();
            }
        }
    }

    static void ResumeLiveSpeech()
    {
        if (!_liveSpeechWanted || _liveVoiceSpeaking || _capsuleSpeech != null || _liveSpeech != null ||
            string.IsNullOrEmpty(_liveSpeechSession)) return;
        string desiredKey = _liveSpeechSession + "\0" + _liveSpeechLanguage;
        if (_liveSpeechFailedFor == desiredKey) return;
        try
        {
            RecognizerInfo info = CapsuleRecognizer(_liveSpeechLanguage);
            if (info == null) throw new InvalidOperationException("Windows has no speech recognizer installed");
            SpeechRecognitionEngine engine = new SpeechRecognitionEngine(info);
            string session = _liveSpeechSession;
            _liveSpeech = engine;
            engine.LoadGrammar(new DictationGrammar());
            engine.SpeechRecognized += delegate (object sender, SpeechRecognizedEventArgs e)
            {
                string spoken = e.Result == null ? "" : (e.Result.Text ?? "").Trim();
                if (spoken.Length == 0 || !_liveSpeechWanted || session != _liveSpeechSession) return;
                long at = (long)(DateTime.UtcNow - new DateTime(
                    1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalMilliseconds;
                PostMain("{\"type\":\"live-native-transcript\",\"session\":" +
                         JsonString(session) + ",\"at_ms\":" + at + ",\"text\":" +
                         JsonString(spoken) + "}");
            };
            engine.SetInputToDefaultAudioDevice();
            engine.RecognizeAsync(RecognizeMode.Multiple);
            _liveSpeechFailedFor = "";
            PostMain("{\"type\":\"live-native-speech-status\",\"active\":true,\"session\":" +
                     JsonString(session) + "}");
        }
        catch (Exception ex)
        {
            StopLiveSpeechEngine();
            _liveSpeechFailedFor = desiredKey;
            PostMain("{\"type\":\"live-native-speech-status\",\"active\":false,\"session\":" +
                     JsonString(_liveSpeechSession) + ",\"message\":" +
                     JsonString("Local continuous speech unavailable: " + ex.Message) + "}");
        }
    }

    static void ConfigureLiveSpeech(bool wanted, string session, string language)
    {
        session = (session ?? "").Trim();
        language = (language ?? "").Trim();
        bool changed = session != _liveSpeechSession || language != _liveSpeechLanguage;
        _liveSpeechWanted = wanted && session.Length > 0;
        _liveSpeechSession = session;
        _liveSpeechLanguage = language;
        if (!_liveSpeechWanted) { _liveSpeechFailedFor = ""; StopLiveSpeechEngine(); StopLiveVoice(); return; }
        if (changed) { _liveSpeechFailedFor = ""; StopLiveSpeechEngine(); }
        ResumeLiveSpeech();
    }

    static void SuspendLiveSpeech()
    {
        StopLiveSpeechEngine();
    }

    static void PostCapsule(string json)
    {
        Form form = _capsuleForm;
        if (form == null || form.IsDisposed) return;
        try
        {
            if (form.InvokeRequired)
            {
                form.BeginInvoke((MethodInvoker)delegate { PostCapsule(json); });
                return;
            }
            if (_capsuleWeb != null && _capsuleWeb.CoreWebView2 != null)
                _capsuleWeb.CoreWebView2.PostWebMessageAsJson(json);
        }
        catch { }
    }

    static void StopCapsuleSpeech()
    {
        SpeechRecognitionEngine engine = _capsuleSpeech;
        _capsuleSpeech = null;
        if (engine == null) return;
        try { engine.RecognizeAsyncCancel(); } catch { }
        try { engine.SetInputToNull(); } catch { }
        try { engine.Dispose(); } catch { }
    }

    static void FinishCapsuleSpeech()
    {
        _capsuleStopOnRelease = true;
        SpeechRecognitionEngine engine = _capsuleSpeech;
        if (engine == null) return;
        // Stop (rather than Cancel) lets the recognizer deliver the phrase already spoken while
        // X2 was held. It is the native push-to-talk boundary, not a background recorder.
        try { engine.RecognizeAsyncStop(); } catch { }
    }

    static RecognizerInfo CapsuleRecognizer(string requested)
    {
        RecognizerInfo first = null, language = null;
        string wanted = (requested ?? "").Trim();
        foreach (RecognizerInfo info in SpeechRecognitionEngine.InstalledRecognizers())
        {
            if (first == null) first = info;
            string name = info.Culture == null ? "" : info.Culture.Name;
            if (string.Equals(name, wanted, StringComparison.OrdinalIgnoreCase)) return info;
            if (language == null && wanted.Length >= 2 && name.StartsWith(
                    wanted.Substring(0, 2), StringComparison.OrdinalIgnoreCase)) language = info;
        }
        return language ?? first;
    }

    static void StartCapsuleSpeech(string requestedLanguage)
    {
        StopCapsuleSpeech();
        SuspendLiveSpeech();
        _capsuleSpeechDelivered = false;
        try
        {
            RecognizerInfo info = CapsuleRecognizer(requestedLanguage);
            if (info == null) throw new InvalidOperationException("Windows has no speech recognizer installed");
            SpeechRecognitionEngine engine = new SpeechRecognitionEngine(info);
            _capsuleSpeech = engine;
            engine.LoadGrammar(new DictationGrammar());
            engine.InitialSilenceTimeout = TimeSpan.FromSeconds(8);
            engine.BabbleTimeout = TimeSpan.FromSeconds(18);
            engine.EndSilenceTimeout = TimeSpan.FromMilliseconds(850);
            engine.EndSilenceTimeoutAmbiguous = TimeSpan.FromMilliseconds(1150);
            engine.SpeechHypothesized += delegate (object sender, SpeechHypothesizedEventArgs e)
            {
                if (e.Result != null && !string.IsNullOrWhiteSpace(e.Result.Text))
                    PostCapsule("{\"type\":\"capsule-speech-partial\",\"text\":" +
                                JsonString(e.Result.Text) + "}");
            };
            engine.SpeechRecognized += delegate (object sender, SpeechRecognizedEventArgs e)
            {
                string text = e.Result == null ? "" : (e.Result.Text ?? "").Trim();
                if (text.Length == 0) return;
                _capsuleSpeechDelivered = true;
                PostCapsule("{\"type\":\"capsule-speech-final\",\"text\":" +
                            JsonString(text) + "}");
            };
            engine.RecognizeCompleted += delegate
            {
                if (!_capsuleSpeechDelivered)
                    PostCapsule("{\"type\":\"capsule-speech-final\",\"text\":\"\"}");
            };
            engine.SetInputToDefaultAudioDevice();
            PostCapsule("{\"type\":\"capsule-speech-start\",\"language\":" +
                        JsonString(info.Culture.Name) + "}");
            engine.RecognizeAsync(RecognizeMode.Single);
            if (_capsuleStopOnRelease) FinishCapsuleSpeech();
        }
        catch (Exception ex)
        {
            StopCapsuleSpeech();
            ResumeLiveSpeech();
            PostCapsule("{\"type\":\"capsule-speech-error\",\"message\":" +
                        JsonString("Local speech recognition unavailable: " + ex.Message) + "}");
        }
    }

    void PostCapsuleTarget(LiveTarget target)
    {
        PostCapsule("{\"type\":\"capsule-context\",\"target\":{" +
                    "\"hwnd\":" + target.Hwnd.ToInt64() + ",\"pid\":" + target.Pid +
                    ",\"process\":" + JsonString(target.ProcessName) +
                    ",\"title\":" + JsonString(target.Title) +
                    ",\"speech_language\":" + JsonString(target.SpeechLanguage) + "}}");
    }

    void OpenLiveCapsule(LiveTarget target, bool pushToTalk = false)
    {
        try
        {
            if (_capsuleForm != null && !_capsuleForm.IsDisposed)
            {
                // A second X2 press while the capsule is already open is a fresh command, not a
                // request to close it. Reuse the captured target and start a new local recording.
                _capsulePttMode = pushToTalk;
                _capsulePttHeld = pushToTalk;
                PostCapsuleTarget(target);
                if (!pushToTalk || _capsulePttHeld)
                    PostCapsule("{\"type\":\"capsule-record-start\",\"push_to_talk\":" +
                                (pushToTalk ? "true" : "false") + "}");
                return;
            }
            _capsuleStopOnRelease = false;
            _capsulePttMode = pushToTalk;
            _capsulePttHeld = pushToTalk;
            Form form = new Form();
            _capsuleForm = form;
            form.Text = "Collie Live";
            form.FormBorderStyle = FormBorderStyle.None;
            form.ShowInTaskbar = false;
            form.TopMost = true;
            form.StartPosition = FormStartPosition.Manual;
            form.ClientSize = new Size(660, 176);
            form.BackColor = Color.FromArgb(244, 246, 242);
            form.Icon = AppIcon();
            Screen screen = target.Hwnd == IntPtr.Zero ? Screen.PrimaryScreen : Screen.FromHandle(target.Hwnd);
            Rectangle work = screen.WorkingArea;
            form.Location = new Point(work.Left + Math.Max(12, (work.Width - form.Width) / 2),
                                      work.Top + 30);
            WebView2 web = new WebView2();
            _capsuleWeb = web;
            web.Dock = DockStyle.Fill;
            web.DefaultBackgroundColor = Color.Transparent;
            web.CoreWebView2InitializationCompleted += delegate (object sender, CoreWebView2InitializationCompletedEventArgs e)
            {
                if (!e.IsSuccess) { Log("capsule webview failed"); return; }
                web.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
                web.CoreWebView2.Settings.IsStatusBarEnabled = false;
                web.CoreWebView2.Settings.IsZoomControlEnabled = false;
                web.CoreWebView2.PermissionRequested += delegate (object s, CoreWebView2PermissionRequestedEventArgs p)
                {
                    if (p.PermissionKind == CoreWebView2PermissionKind.Microphone)
                        p.State = CoreWebView2PermissionState.Allow;
                };
                web.CoreWebView2.WebMessageReceived += delegate (object s, CoreWebView2WebMessageReceivedEventArgs m)
                {
                    string raw = "";
                    try { raw = m.WebMessageAsJson ?? ""; } catch { }
                    if (raw.IndexOf("capsule-ready", StringComparison.Ordinal) >= 0)
                    {
                        PostCapsuleTarget(target);
                        if (!_capsulePttMode || _capsulePttHeld)
                            PostCapsule("{\"type\":\"capsule-record-start\",\"push_to_talk\":" +
                                        (_capsulePttMode ? "true" : "false") + "}");
                    }
                    else if (raw.IndexOf("capsule-listen", StringComparison.Ordinal) >= 0 ||
                             raw.IndexOf("capsule-language", StringComparison.Ordinal) >= 0)
                    {
                        if (!_capsulePttMode || _capsulePttHeld)
                            PostCapsule("{\"type\":\"capsule-record-start\",\"push_to_talk\":" +
                                        (_capsulePttMode ? "true" : "false") + "}");
                    }
                    else if (raw.IndexOf("capsule-open-main", StringComparison.Ordinal) >= 0)
                    {
                        try { form.Close(); } catch { }
                        WakeWindow();
                    }
                    else if (raw.IndexOf("capsule-close", StringComparison.Ordinal) >= 0)
                        try { form.Close(); } catch { }
                };
                web.CoreWebView2.Navigate(_baseUrl.TrimEnd('/') + "/live-capsule");
            };
            form.FormClosed += delegate
            {
                _capsuleStopOnRelease = false;
                _capsulePttMode = _capsulePttHeld = false;
                StopCapsuleSpeech();
                ResumeLiveSpeech();
                try { web.Dispose(); } catch { }
                _capsuleWeb = null; _capsuleForm = null;
            };
            form.Controls.Add(web);
            form.Show();
            try { int corner = 2; DwmSetWindowAttribute(form.Handle, 33, ref corner, sizeof(int)); } catch { }
            web.EnsureCoreWebView2Async(_env);
            form.Activate();
        }
        catch (Exception ex)
        {
            Log("live capsule failed: " + ex.Message);
            try { if (_capsuleForm != null) _capsuleForm.Close(); } catch { }
        }
    }

    // A second ordinary Collie window — used for target=_blank links (star map, meadow) so they stay
    // in the app instead of escaping to the browser.
    static CoreWebView2Environment _env;   // set once by InitWeb; child windows share its profile
    static void OpenChildWindow(string url)
    {
        try
        {
            Form f = new Form();
            f.Text = "Collie";
            f.StartPosition = FormStartPosition.CenterScreen;
            // The Map's project selector plus source drawer needs a real editor-sized surface. The
            // old 1100px child window forced both controls and a remembered 600px drawer into half a
            // canvas, which looked like a broken split view even though WebGL was healthy.
            f.ClientSize = new Size(1280, 820);
            f.BackColor = Color.Black;
            f.Icon = AppIcon();
            WebView2 w = new WebView2();
            w.Dock = DockStyle.Fill;
            w.DefaultBackgroundColor = Color.Black;
            w.CoreWebView2InitializationCompleted += delegate
            {
                try { w.CoreWebView2.Navigate(url); } catch (Exception e) { Log("child nav: " + e.Message); }
            };
            f.Controls.Add(w);
            f.Show();
            // Subscribing to InitializationCompleted does not START initialisation — nothing does
            // until EnsureCoreWebView2Async (or Source=) is called. Without this the event never
            // fires, Navigate never runs, and the child is a permanently black window.
            w.EnsureCoreWebView2Async(_env);
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
        if (_windowMode && IsHandleCreated) try { UnregisterHotKey(Handle, LIVE_HANDOFF_HOTKEY); } catch { }
        if (_mouseHook != IntPtr.Zero) UnhookWindowsHookEx(_mouseHook);
        if (_keyHook != IntPtr.Zero) UnhookWindowsHookEx(_keyHook);
        StopLiveSpeechEngine();
        StopLiveVoice();
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
        if (nCode >= 0)
        {
            int liveMsg = (int)wParam;
            if (_windowMode && (liveMsg == WM_XBUTTONDOWN || liveMsg == WM_XBUTTONUP))
            {
                MSLLHOOKSTRUCT liveMouse = (MSLLHOOKSTRUCT)Marshal.PtrToStructure(lParam, typeof(MSLLHOOKSTRUCT));
                int button = (int)((liveMouse.mouseData >> 16) & 0xFFFF);
                if (button == XBUTTON2)
                {
                    System.Threading.SynchronizationContext ctx = _uiCtx;
                    CollieWallpaper main = _mainForm;
                    if (liveMsg == WM_XBUTTONDOWN)
                    {
                        // Capture before the capsule opens so the action stays pinned to the app
                        // the user was operating (for example the already focused browser tab).
                        LiveTarget target = CaptureLiveTarget();
                        if (ctx != null && main != null)
                            try { ctx.Post(delegate { main.OpenLiveCapsule(target, true); }, null); } catch { }
                    }
                    else if (ctx != null)
                        try { ctx.Post(delegate { _capsulePttHeld = false; PostCapsule("{\"type\":\"capsule-record-stop\"}"); }, null); } catch { }
                    // X2 is deliberately claimed only while the normal Collie app is running; do
                    // not also let the browser interpret it as Back/Forward while it is a voice key.
                    return (IntPtr)1;
                }
            }
        }
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
