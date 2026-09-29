"""The wallpaper stops drawing while a window hides it, and never goes black doing so.

A window pinned behind the desktop icons is never told it is covered (Chromium tracks occlusion of
top-level windows; the wallpaper is a child of Progman), so under a maximized browser the page drew
every frame for nobody. The host now looks at the foreground window itself and turns the WebView
off while it covers the wallpaper, leaving the last frame on the form behind it. The coverage test
is compiled and run here with the same csc harness/wallpaper.py uses.
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
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PROBE = """using System;
using System.Drawing;
static class CoverProbe
{
    static Rectangle R(string[] a, int i)
    {
        return Rectangle.FromLTRB(int.Parse(a[i]), int.Parse(a[i + 1]), int.Parse(a[i + 2]), int.Parse(a[i + 3]));
    }
    static int Main(string[] args)
    {
        for (int i = 0; i + 12 <= args.Length; i += 12)
            Console.WriteLine(CollieWallpaper.CoversWallpaper(R(args, i), R(args, i + 4), R(args, i + 8)));
        return 0;
    }
}
"""

SCREEN = (0, 0, 5120, 1440)
WORK = (0, 0, 5120, 1392)                        # taskbar along the bottom
CASES = [
    # (window frame, wallpaper, work area) -> covers
    ((0, 0, 5120, 1392), SCREEN, WORK, True),     # maximized: stops at the taskbar
    ((0, 0, 5120, 1440), SCREEN, WORK, True),     # full screen: a game, a video
    ((-8, -8, 5128, 1400), SCREEN, WORK, True),   # a frame reaching past the edges
    ((0, 0, 5119, 1392), SCREEN, WORK, False),    # one column of the wallpaper still shows
    ((0, 1, 5120, 1392), SCREEN, WORK, False),
    ((40, 40, 2000, 1000), SCREEN, WORK, False),  # an ordinary window
    ((5120, 0, 7680, 1440), SCREEN, WORK, False),  # maximized on another monitor
    ((48, 0, 5120, 1440), SCREEN, (48, 0, 5120, 1440), True),  # taskbar on the left
    ((0, 0, 0, 0), SCREEN, WORK, False),          # no frame at all
    ((0, 0, 5120, 1440), SCREEN, (5120, 0, 7680, 1440), False),  # work area off the wallpaper
]


def test_the_wallpaper_watches_for_cover_and_keeps_a_still():
    source = (SRC / "Program.cs").read_text(encoding="utf-8")
    ready = source.split("void OnWebReady(", 1)[1].split("static bool _attached;", 1)[0]
    window_branch, wallpaper_part = ready.split("Pin();", 1)
    assert "StartCoverWatch()" not in window_branch    # the app window is a normal window
    assert "StartCoverWatch();" in wallpaper_part
    covered = source.split("async void SetCovered(bool covered)", 1)[1].split("void SetStill(", 1)[0]
    # The still goes up before the WebView goes away, and a cover that ended while the frame was
    # taken never hides the live page.
    assert covered.index("CapturePreviewAsync") < covered.index("SetStill(new Bitmap(frame))") \
        < covered.index("_web.Visible = false")
    assert covered.count("gen != _coverGen") == 2
    assert "_web.Visible = true" in covered


@pytest.mark.skipif(os.name != "nt" or not CSC.exists() or not SPEECH.exists(),
                    reason="the wallpaper host is built with the in-box .NET Framework csc")
def test_the_compiled_host_decides_cover(tmp_path):
    for dll in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
        shutil.copy(SRC / dll, tmp_path / dll)
    shutil.copy(SRC / "Program.cs", tmp_path / "Program.cs")
    (tmp_path / "CoverProbe.cs").write_text(PROBE, encoding="utf-8")
    build = subprocess.run(
        [str(CSC), "/nologo", "/target:exe", "/platform:x64", "/main:CoverProbe",
         "/out:coverprobe.exe", "/reference:System.Windows.Forms.dll",
         "/reference:System.Drawing.dll", "/reference:" + str(SPEECH),
         "/reference:Microsoft.Web.WebView2.Core.dll",
         "/reference:Microsoft.Web.WebView2.WinForms.dll", "Program.cs", "CoverProbe.cs"],
        cwd=tmp_path, capture_output=True, text=True, errors="replace", timeout=180,
        creationflags=NO_WINDOW)
    assert build.returncode == 0, build.stdout + build.stderr
    args = [str(n) for window, wallpaper, work, _ in CASES for rect in (window, wallpaper, work)
            for n in rect]
    run = subprocess.run([str(tmp_path / "coverprobe.exe")] + args,
                         cwd=tmp_path, capture_output=True, text=True, errors="replace",
                         timeout=60, creationflags=NO_WINDOW)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.split() == [str(want) for *_, want in CASES]
