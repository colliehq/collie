"""collie desktop wallpaper — the behind-icons live desktop (Windows / WebView2), made portable.

Nothing is hardcoded, so `collie wallpaper` works from any install location (source checkout OR a
pip/pipx install) on any machine:
  - python    : pythonw next to sys.executable (windowless — no console flash)
  - engine    : collie-wallpaper.exe, BUILT ON DEMAND from the shipped C# source via the in-box
                .NET Framework csc (no .NET SDK needed), cached next to the source
  - server    : `collie web` on a FREE port, handed to the engine via COLLIE_WALLPAPER_URL (so it
                never collides with a busy 8787)
  - autostart : `collie wallpaper --install` writes a per-machine, hidden Startup-folder launcher
                (a generated .pyw with the resolved package path + a .vbs that runs it hidden) —
                so it survives being moved and needs no console window

Windows only (it pins a WebView2 window under Progman). On macOS/Linux it degrades to a borderless
full-screen browser window (see cli._desktop_window).
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request

from . import plat

QUIT_EVENT = "collie-wallpaper-quit"


# ── path resolution (all dynamic) ────────────────────────────────────────────
def src_dir() -> str:
    """The shipped wallpaper/ dir (Program.cs + WebView2 DLLs). Repo: <root>/wallpaper; wheel:
    harness/wallpaper (package-data)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "wallpaper"), os.path.join(os.path.dirname(here), "wallpaper")):
        if os.path.exists(os.path.join(cand, "Program.cs")):
            return cand
    return os.path.join(os.path.dirname(here), "wallpaper")


def exe_path() -> str:
    return os.path.join(src_dir(), "collie-wallpaper.exe")


def pythonw() -> str:
    """The windowless interpreter next to the running one (pipx/uv/pythoncore all keep pythonw.exe
    beside python.exe). Falls back to sys.executable where there is none."""
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def _pkg_parent() -> str:
    """The directory that must be on sys.path for `import harness` — the repo root (source) or
    site-packages (installed)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _collie_home() -> str:
    d = os.path.expanduser(os.path.join("~", ".collie"))
    os.makedirs(d, exist_ok=True)
    return d


def _startup_vbs() -> str:
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
                        "collie-wallpaper.vbs")


# ── server + port ────────────────────────────────────────────────────────────
def free_port(preferred: int = 8787) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def server_up(port: int) -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/api/ver" % port, timeout=0.8).read()
        return True
    except Exception:
        return False


def start_server_windowless(port: int) -> None:
    """Spawn `collie web` in a windowless pythonw child. subprocess.Popen (unlike PowerShell
    Start-Process) quotes the list args correctly, so an inline -c is safe here."""
    log = os.path.join(_collie_home(), "wallpaper-web.log")
    code = ("import sys,os;"
            "sys.path.insert(0, r'%s');"
            "sys.stdin=open(os.devnull,'r');"
            "f=open(r'%s','a',encoding='utf-8');sys.stdout=sys.stderr=f;"
            "from harness.webapp import main;"
            "sys.exit(main(['--port','%d','--no-open']))" % (_pkg_parent(), log, port))
    kw = {}
    if plat.is_windows():
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    subprocess.Popen([pythonw(), "-c", code], **kw)


# ── engine: build-on-demand + WebView2 check ─────────────────────────────────
def webview2_present() -> bool:
    if not plat.is_windows():
        return False
    import winreg
    key = r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as k:
                v, _ = winreg.QueryValueEx(k, "pv")
                if v and v != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def build_engine(force: bool = False) -> "str | None":
    """Build collie-wallpaper.exe from the shipped C# source using the in-box .NET Framework csc
    (present on every Windows — NO .NET SDK needed). Cached: skipped if the exe already exists."""
    exe = exe_path()
    if os.path.exists(exe) and not force:
        return exe
    if not plat.is_windows():
        return None
    csc = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                       r"Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    if not os.path.exists(csc):
        return None
    d = src_dir()
    cmd = [csc, "/nologo", "/target:winexe", "/platform:x64", "/out:collie-wallpaper.exe",
           "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
           "/reference:Microsoft.Web.WebView2.Core.dll",
           "/reference:Microsoft.Web.WebView2.WinForms.dll", "Program.cs"]
    try:
        subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=120)
    except Exception:
        return None
    return exe if os.path.exists(exe) else None


def launch_engine(port: int) -> bool:
    exe = build_engine()
    if not exe:
        return False
    # /ambient is the calm, theme-adaptive live wallpaper (clock + weather + Collie watermark). The
    # older /wallpaper (dark galaxy + on-desktop chat) stays available for anyone who navigates to it.
    env = dict(os.environ, COLLIE_WALLPAPER_URL="http://127.0.0.1:%d/ambient" % port)
    try:
        subprocess.Popen([exe], cwd=src_dir(), env=env)
        return True
    except Exception:
        return False


def engine_running() -> bool:
    if not plat.is_windows():
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq collie-wallpaper.exe"],
                             capture_output=True, text=True, timeout=10).stdout
        return "collie-wallpaper.exe" in out
    except Exception:
        return False


# ── the operations ───────────────────────────────────────────────────────────
def run(port_pref: int = 8787, boot: bool = False) -> int:
    """Bring the wallpaper up: ensure the server (free port) → build+attach the engine. On `boot`
    (windowless autostart entry) the server is run IN THIS process so pythonw stays alive hosting it;
    interactively it is spawned as a child so the shell returns."""
    if not plat.is_windows():
        print("collie wallpaper: the behind-icons engine is Windows-only. On this OS use `collie "
              "web` (browser) or the borderless-window fallback.", file=sys.stderr)
        return 2
    if not webview2_present():
        print("collie wallpaper: WebView2 runtime not found. install it:\n"
              "  winget install Microsoft.EdgeWebView2Runtime", file=sys.stderr)
        return 3
    # REUSE a collie server already serving the preferred port — only pick a different free port
    # when nothing of ours is there (otherwise a second `collie wallpaper` spawns a duplicate server).
    port = port_pref if server_up(port_pref) else free_port(port_pref)
    if not server_up(port):
        start_server_windowless(port)
    for _ in range(90):                                    # wait up to ~45s
        if server_up(port):
            break
        time.sleep(0.5)
    if not server_up(port):
        print("collie wallpaper: server did not come up on port %d — see %s"
              % (port, os.path.join(_collie_home(), "wallpaper-web.log")), file=sys.stderr)
        return 1
    if not engine_running():
        ok = launch_engine(port)
        print("collie wallpaper · http://127.0.0.1:%d/wallpaper · %s"
              % (port, "engine launched" if ok else "engine failed to build/launch"), flush=True)
        if not ok:
            return 1
    else:
        print("collie wallpaper · already running · http://127.0.0.1:%d/wallpaper" % port)
    return 0


def run_app(port_pref: int = 8787) -> int:
    """Open collie as a normal desktop APP WINDOW — the same WebView2 host in --window mode, showing
    the full GUI, with the server started windowless behind it. This is what the installer's desktop
    shortcut launches: a real program with a taskbar entry and icon, instead of a browser tab showing
    127.0.0.1:8787 that gets lost among the user's other tabs."""
    if not plat.is_windows():
        print("collie app: the native window is Windows-only — use `collie web` here.", file=sys.stderr)
        return 2
    if not webview2_present():
        print("collie app: WebView2 runtime not found. install it:\n"
              "  winget install Microsoft.EdgeWebView2Runtime", file=sys.stderr)
        return 3
    port = port_pref if server_up(port_pref) else free_port(port_pref)
    if not server_up(port):
        start_server_windowless(port)
    for _ in range(90):
        if server_up(port):
            break
        time.sleep(0.5)
    if not server_up(port):
        print("collie app: server did not come up on port %d — see %s"
              % (port, os.path.join(_collie_home(), "wallpaper-web.log")), file=sys.stderr)
        return 1
    exe = build_engine()
    if not exe:
        print("collie app: could not build the window host", file=sys.stderr)
        return 1
    env = dict(os.environ, COLLIE_WALLPAPER_URL="http://127.0.0.1:%d/" % port)
    try:
        subprocess.Popen([exe, "--window"], cwd=src_dir(), env=env)
    except Exception as e:
        print("collie app: %s" % e, file=sys.stderr)
        return 1
    print("collie app · http://127.0.0.1:%d/ · window opened" % port)
    return 0


def install() -> int:
    """Register a per-machine, hidden logon autostart. Generates a .pyw launcher with THIS machine's
    resolved package path + a .vbs that runs it windowless — no hardcoded repo/python paths."""
    if not plat.is_windows():
        print("collie wallpaper --install is Windows-only.", file=sys.stderr)
        return 2
    boot_pyw = os.path.join(_collie_home(), "wallpaper-boot.pyw")
    log = os.path.join(_collie_home(), "wallpaper-boot.log")
    with open(boot_pyw, "w", encoding="utf-8") as f:
        f.write(
            "# auto-generated by `collie wallpaper --install` — launches the wallpaper at logon.\n"
            "import sys, os\n"
            "sys.path.insert(0, r'%s')\n"
            "sys.stdin = open(os.devnull, 'r')\n"
            "f = open(r'%s', 'a', encoding='utf-8'); sys.stdout = sys.stderr = f\n"
            "from harness.cli import main\n"
            "sys.argv = ['collie', 'wallpaper', '--boot']\n"
            "sys.exit(main())\n" % (_pkg_parent(), log))
    vbs = _startup_vbs()
    os.makedirs(os.path.dirname(vbs), exist_ok=True)
    with open(vbs, "w", encoding="utf-8") as f:
        # Chr(34) is a literal double-quote — safer than VBScript's ""-doubling for quoting the two
        # paths (which often contain spaces, e.g. "C:\Users\First Last"). 0 = hidden, False = no wait.
        f.write("' collie desktop wallpaper - hidden logon autostart (auto-generated).\n"
                "q = Chr(34)\n"
                'CreateObject("WScript.Shell").Run q & "%s" & q & " " & q & "%s" & q, 0, False\n'
                % (pythonw(), boot_pyw))
    # Also START it now, not only at the next logon — someone who just ticked "enable the wallpaper"
    # (in Setup or via this command) expects to SEE it immediately, not after a reboot. Spawn the very
    # same windowless launcher the .vbs fires at logon, detached so it outlives this process.
    started = False
    try:
        flags = 0x00000008 | 0x08000000   # DETACHED_PROCESS | CREATE_NO_WINDOW
        subprocess.Popen([pythonw(), boot_pyw], creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = True
    except Exception:
        pass
    print("collie wallpaper: autostart installed%s\n"
          "  launcher: %s\n  startup : %s\n  disable : collie wallpaper --uninstall"
          % (" + started now" if started else " (starts at next logon)", boot_pyw, vbs))
    return 0


def uninstall() -> int:
    vbs = _startup_vbs()
    boot_pyw = os.path.join(_collie_home(), "wallpaper-boot.pyw")
    removed = []
    for p in (vbs, boot_pyw):
        try:
            if os.path.exists(p):
                os.remove(p)
                removed.append(p)
        except OSError:
            pass
    print("collie wallpaper: autostart removed" if removed else "collie wallpaper: autostart was not installed")
    return 0


def stop() -> int:
    """Signal the engine's named-event clean shutdown (never -Force — that orphans WebView2 COM),
    then best-effort reap."""
    if plat.is_windows():
        try:
            import ctypes
            EVENT_MODIFY_STATE = 0x0002
            h = ctypes.windll.kernel32.OpenEventW(EVENT_MODIFY_STATE, False, QUIT_EVENT)
            if h:
                ctypes.windll.kernel32.SetEvent(h)
                ctypes.windll.kernel32.CloseHandle(h)
                time.sleep(2)
        except Exception:
            pass
    plat.rmtree  # noqa: keep import used
    print("collie wallpaper: stop signalled")
    return 0
