"""Links a Collie page opens in a new window: Collie's pages in a Collie window, the web in the browser.

The wallpaper host answered target=_blank links by navigating the wallpaper itself to them, so
"Open Collie" or the code map replaced the desktop behind the icons with no way back. The app
window opened every link, github.com included, in a chromeless Collie window. The host is C#
compiled on the user's machine on first run, so the routing is compiled and run here as well, with
the same csc and references harness/wallpaper.py uses, into a temporary directory.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "harness" / "wallpaper"
WINDIR = os.environ.get("WINDIR", r"C:\Windows")
CSC = Path(WINDIR) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
SPEECH = (Path(WINDIR) / "Microsoft.NET" / "assembly" / "GAC_MSIL" / "System.Speech" /
          "v4.0_4.0.0.0__31bf3856ad364e35" / "System.Speech.dll")

PROBE = """using System;
static class LinkProbe
{
    static int Main(string[] args)
    {
        for (int i = 1; i < args.Length; i++)
            Console.WriteLine(CollieWallpaper.RouteNewWindow(args[i], args[0]));
        return 0;
    }
}
"""

CASES = [
    ("http://127.0.0.1:8787/map", "CollieWindow"),
    ("http://127.0.0.1:8787/?token=x#settings", "CollieWindow"),
    ("http://localhost:8787/wallpaper", "CollieWindow"),
    ("http://[::1]:8787/", "CollieWindow"),
    ("https://github.com/colliehq/collie", "DefaultBrowser"),
    ("http://example.com/", "DefaultBrowser"),
    ("http://127.0.0.1:9999/", "DefaultBrowser"),              # another local server is not Collie
    ("https://127.0.0.1:8787/", "DefaultBrowser"),
    ("http://127.0.0.1:8787@evil.example/", "DefaultBrowser"),  # userinfo, not Collie's host
    ("about:blank", "Refuse"),
    ("file:///C:/Windows/System32/calc.exe", "Refuse"),
    ("javascript:alert(1)", "Refuse"),
    ("ms-settings:privacy", "Refuse"),
    ("not a url", "Refuse"),
]


def test_new_windows_never_navigate_the_wallpaper_and_follow_one_routing():
    source = (SRC / "Program.cs").read_text(encoding="utf-8")
    main_handler = source.split("_web.CoreWebView2.NewWindowRequested +=", 1)[1].split("};", 1)[0]
    assert "OpenRequestedWindow(e2.Uri)" in main_handler
    assert ".Navigate(e2.Uri)" not in main_handler and "_windowMode" not in main_handler
    child = source.split("static void OpenChildWindow(string url)", 1)[1].split("void Pin()", 1)[0]
    assert "NewWindowRequested +=" in child and "OpenRequestedWindow(e2.Uri)" in child
    route = source.split("static void OpenRequestedWindow(string uri)", 1)[1].split(
        "static CoreWebView2Environment _env", 1)[0]
    assert "UseShellExecute = true" in route and "RouteNewWindow(uri, _baseUrl)" in route


@pytest.mark.skipif(os.name != "nt" or not CSC.exists() or not SPEECH.exists(),
                    reason="the wallpaper host is built with the in-box .NET Framework csc")
def test_the_compiled_host_routes_each_kind_of_link(tmp_path):
    for dll in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
        shutil.copy(SRC / dll, tmp_path / dll)
    shutil.copy(SRC / "Program.cs", tmp_path / "Program.cs")
    (tmp_path / "LinkProbe.cs").write_text(PROBE, encoding="utf-8")
    build = subprocess.run(
        [str(CSC), "/nologo", "/target:exe", "/platform:x64", "/main:LinkProbe",
         "/out:linkprobe.exe", "/reference:System.Windows.Forms.dll",
         "/reference:System.Drawing.dll", "/reference:" + str(SPEECH),
         "/reference:Microsoft.Web.WebView2.Core.dll",
         "/reference:Microsoft.Web.WebView2.WinForms.dll", "Program.cs", "LinkProbe.cs"],
        cwd=tmp_path, capture_output=True, text=True, errors="replace", timeout=180)
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(tmp_path / "linkprobe.exe"), "http://127.0.0.1:8787"] +
                         [uri for uri, _ in CASES],
                         cwd=tmp_path, capture_output=True, text=True, errors="replace",
                         timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    got = run.stdout.split()
    assert list(zip([uri for uri, _ in CASES], got)) == [(uri, want) for uri, want in CASES]
