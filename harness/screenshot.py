"""Screen capture — the eye collie did not have.

Every other perception tool in this harness is STRUCTURED: browser_snapshot returns a DOM /
accessibility tree, desktop_inspect returns a UI Automation tree. That is the right primitive for
*acting* (you click a stable element ref, not a pixel that moves with DPI, theme and scroll), and it
is why desktop control works at all. But it means collie has been unable to see what anything LOOKS
like: it could not check a rendering, catch a layout that broke, judge whether a UI change is an
improvement, or work with a surface that publishes no accessibility tree at all — a game, a canvas
app, a remote-desktop session, a PDF preview.

This module closes that gap, and the image genuinely reaches the model: providers.py already speaks
a canonical {"type":"image","media_type","data"} block and reshapes it per provider (Anthropic
source blocks, OpenAI image_url data URIs, Ollama bare-base64 `images`), so a capture is *seen* by a
vision-capable model rather than described to it. On a text-only model it degrades to the text line
plus a marker instead of failing.

Windows capture goes through PrintWindow with PW_RENDERFULLCONTENT, which matters twice over:
  - it renders a window that is BEHIND other windows or off-screen, so capturing does not require
    stealing focus — the same principle the desktop_* tools are built on;
  - without PW_RENDERFULLCONTENT, Chromium-family and WebView2 windows come back solid black,
    because they composite off the normal GDI path. That includes collie's own web UI.
Where PrintWindow still fails (a few OpenGL/D3D exclusive surfaces) the capture falls back to
copying the window's screen rectangle, which does need the window to be unobscured — the tool says
which path it used so a suspicious image can be explained rather than silently trusted.

Zero new dependencies, matching native.py: a PowerShell driver with inline C#, using System.Drawing
(present with .NET Framework on every Windows) for PNG encoding and the downscale. Pillow is NOT a
runtime dependency of this project and this does not make it one.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile

from . import plat
from .tools import Tool

# Anthropic resizes anything larger than ~1568px on the long edge anyway, and a full 4K screenshot
# costs several thousand tokens for detail no model uses. Downscaling in GDI before encoding keeps a
# capture at roughly 1-1.5k tokens.
_MAX_DIM = 1568

_CAPTURE_PS = r'''
param([Parameter(Mandatory=$true)][string]$Path, [string]$Title = "", [int]$MaxDim = 1568)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing | Out-Null
Add-Type @"
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Text;
public class CollieCap {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  public delegate bool Callback(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint f);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr h);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool EnumWindows(Callback cb, IntPtr p);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
  [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT p);
  [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint f);

  // Is the target actually the window ON TOP across its own area? The screencopy fallback reads
  // screen pixels, so if anything covers the target we would hand back a picture of the covering
  // window while claiming it is the target — a wrong image presented as right, which is the worst
  // failure this tool could have. Probe the centre and the four quadrants; GA_ROOT (2) so a hit on
  // a child control still resolves to the top-level window.
  public static bool OnTop(IntPtr target) {
    RECT r; GetWindowRect(target, out r);
    int[,] pts = { {2,2}, {4,4}, {4,3}, {3,4}, {3,3} };
    int hits = 0, tried = 0;
    for (int i = 0; i < 5; i++) {
      POINT p = new POINT();
      p.X = r.L + (r.R - r.L) / pts[i,0] ; p.Y = r.T + (r.B - r.T) / pts[i,1];
      IntPtr h = GetAncestor(WindowFromPoint(p), 2);
      tried++;
      if (h == target) hits++;
    }
    return hits * 2 > tried;      // majority of probes land on the target
  }

  public static IntPtr Found = IntPtr.Zero;
  public static string FoundTitle = "";
  public static string Needle = "";
  public static string Seen = "";

  // Substring match on the visible title, skipping tool windows and minimised ones. Same "match by
  // title" contract the desktop_* tools use, so a title from desktop_apps works here unchanged.
  public static bool Visit(IntPtr h, IntPtr p) {
    if (!IsWindowVisible(h) || IsIconic(h)) return true;
    int n = GetWindowTextLength(h); if (n < 1) return true;
    StringBuilder sb = new StringBuilder(n + 2);
    GetWindowText(h, sb, sb.Capacity);
    string t = sb.ToString();
    RECT r; GetWindowRect(h, out r);
    if ((r.R - r.L) < 48 || (r.B - r.T) < 48) return true;
    if (Seen.Length < 900) Seen += t + "\n";
    if (Needle.Length > 0 && t.IndexOf(Needle, StringComparison.OrdinalIgnoreCase) >= 0) {
      Found = h; FoundTitle = t; return false;
    }
    return true;
  }

  public static Bitmap Window(IntPtr h, out string how) {
    RECT r; GetWindowRect(h, out r);
    int w = r.R - r.L, ht = r.B - r.T;
    how = "printwindow";
    Bitmap bmp = new Bitmap(w, ht, PixelFormat.Format32bppArgb);
    bool ok = false;
    using (Graphics g = Graphics.FromImage(bmp)) {
      IntPtr hdc = g.GetHdc();
      // 2 = PW_RENDERFULLCONTENT. Without it Chromium/WebView2 windows come back black.
      try { ok = PrintWindow(h, hdc, 2); } finally { g.ReleaseHdc(hdc); }
    }
    string why = null;
    if (!ok) why = "printwindow failed";
    else if (Blank(bmp)) why = "printwindow returned a blank image";
    else if (HollowInterior(bmp)) why = "printwindow rendered the frame but not the content";
    if (why == null) return bmp;
    bmp.Dispose();
    // The fallback reads the SCREEN, so it is only truthful while nothing covers the target.
    // Refuse rather than return a picture of whatever is on top — verified necessary: capturing an
    // occluded Chrome window this way returned the editor sitting in front of it, labelled as
    // Chrome. `how` alone would not have saved anyone from believing it.
    if (!OnTop(h)) { how = "occluded:" + why; return null; }
    how = "screencopy (" + why + ")";
    Bitmap b2 = new Bitmap(w, ht, PixelFormat.Format32bppArgb);
    using (Graphics g = Graphics.FromImage(b2)) g.CopyFromScreen(r.L, r.T, 0, 0, new Size(w, ht));
    return b2;
  }

  // The failure that actually bites, and the reason this check is not just Blank(). On a
  // Chromium-family window (Chrome, Edge, WebView2, Electron) PrintWindow renders the GDI frame —
  // tabs, address bar, bookmarks — but NOT the page, which the GPU process composites separately.
  // The result is a screenshot that looks entirely plausible and shows an empty page, which is far
  // more dangerous than a black rectangle: a model would conclude the page failed to load. So test
  // the INTERIOR alone (skip the top fifth, where the frame lives, and inset the edges) and treat a
  // uniform interior as a failed capture.
  public static bool HollowInterior(Bitmap b) {
    int x0 = b.Width / 20, x1 = b.Width - b.Width / 20;
    int y0 = b.Height / 5,  y1 = b.Height - b.Height / 20;
    if (x1 - x0 < 40 || y1 - y0 < 40) return false;      // too small to judge; trust the capture
    int first = -1;
    for (int i = 0; i < 12; i++) for (int j = 0; j < 12; j++) {
      int x = x0 + (x1 - x0) * i / 12, y = y0 + (y1 - y0) * j / 12;
      int c = b.GetPixel(x, y).ToArgb();
      if (first == -1) first = c; else if (c != first) return false;
    }
    return true;
  }

  // A black or single-colour result is how PrintWindow fails without saying so. Sample a grid
  // rather than every pixel: cheap, and a real window is never uniform across 100 spread points.
  public static bool Blank(Bitmap b) {
    int first = -1;
    for (int i = 0; i < 10; i++) for (int j = 0; j < 10; j++) {
      int x = Math.Min(b.Width - 1, b.Width * i / 10 + b.Width / 20);
      int y = Math.Min(b.Height - 1, b.Height * j / 10 + b.Height / 20);
      int c = b.GetPixel(x, y).ToArgb();
      if (first == -1) first = c; else if (c != first) return false;
    }
    return true;
  }

  // NOT named Screen(): that collides with System.Windows.Forms.Screen and the C# compiler then
  // reads `Screen.AllScreens` as a member access on this method.
  public static Bitmap FullScreen(out string how) {
    how = "virtualscreen";
    Rectangle v = Rectangle.Empty;
    foreach (System.Windows.Forms.Screen s in System.Windows.Forms.Screen.AllScreens)
      v = Rectangle.Union(v, s.Bounds);
    Bitmap bmp = new Bitmap(v.Width, v.Height, PixelFormat.Format32bppArgb);
    using (Graphics g = Graphics.FromImage(bmp)) g.CopyFromScreen(v.X, v.Y, 0, 0, v.Size);
    return bmp;
  }

  public static Bitmap Fit(Bitmap src, int maxDim) {
    int m = Math.Max(src.Width, src.Height);
    if (m <= maxDim) return src;
    double k = (double)maxDim / m;
    int w = Math.Max(1, (int)(src.Width * k)), h = Math.Max(1, (int)(src.Height * k));
    Bitmap dst = new Bitmap(w, h, PixelFormat.Format32bppArgb);
    using (Graphics g = Graphics.FromImage(dst)) {
      g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
      g.DrawImage(src, 0, 0, w, h);
    }
    src.Dispose();
    return dst;
  }
}
"@ -ReferencedAssemblies System.Drawing, System.Windows.Forms | Out-Null

# Measure in real pixels. Without this a capture on a scaled display is cropped or letterboxed.
try { [CollieCap]::SetProcessDPIAware() | Out-Null } catch {}

$how = ""
$name = ""
if ($Title -ne "") {
  [CollieCap]::Needle = $Title
  [CollieCap]::Found = [IntPtr]::Zero
  $cb = [CollieCap+Callback]{ param($h, $p) return [CollieCap]::Visit($h, $p) }
  [CollieCap]::EnumWindows($cb, [IntPtr]::Zero) | Out-Null
  if ([CollieCap]::Found -eq [IntPtr]::Zero) {
    $seen = [CollieCap]::Seen
    Write-Output (@{ ok = $false; error = "no visible window whose title contains '$Title'"; windows = $seen } | ConvertTo-Json -Compress)
    exit 0
  }
  $name = [CollieCap]::FoundTitle
  $bmp = [CollieCap]::Window([CollieCap]::Found, [ref]$how)
  if ($null -eq $bmp) {
    Write-Output (@{ ok = $false; title = $name; how = $how; error =
      ("'" + $name + "' cannot be captured accurately right now: PrintWindow did not render its " +
       "content (" + ($how -replace '^occluded:','') + ") and the window is COVERED by another one, " +
       "so copying its screen area would return a picture of whatever is in front of it. Options: " +
       "bring it to the front first (desktop_focus) and retry; capture the full screen instead " +
       "(omit title); or for a web page use browser_snapshot, which reads the page directly and " +
       "does not care what is on top.") } | ConvertTo-Json -Compress)
    exit 0
  }
} else {
  $bmp = [CollieCap]::FullScreen([ref]$how)
}

$w0 = $bmp.Width; $h0 = $bmp.Height
$bmp = [CollieCap]::Fit($bmp, $MaxDim)
$bmp.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
$out = @{ ok = $true; path = $Path; width = $bmp.Width; height = $bmp.Height;
          source_width = $w0; source_height = $h0; how = $how; title = $name }
$bmp.Dispose()
Write-Output ($out | ConvertTo-Json -Compress)
'''


def _script_path() -> str:
    """Write the driver next to the rest of collie's state and refresh it when it drifts — the same
    approach (and the same reason) as native.py's UI Automation driver: a long PowerShell script
    passed with -Command is a quoting minefield, and -File is not."""
    d = os.path.join(os.path.expanduser(os.environ.get("COLLIE_STATE_DIR") or "~/.collie"), "bin")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "capture.ps1")
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                if f.read() == _CAPTURE_PS:
                    return p
    except OSError:
        pass
    with open(p, "w", encoding="utf-8") as f:
        f.write(_CAPTURE_PS)
    return p


def capture(title: str = "", max_dim: int = _MAX_DIM, path: str = "") -> dict:
    """Capture a window (by title substring) or the whole virtual screen to a PNG.

    Returns {"ok":True, path, width, height, source_width, source_height, how, title} or
    {"ok":False, "error":..., maybe "windows": "<titles seen>"}.
    """
    if not path:
        fd, path = tempfile.mkstemp(prefix="collie-shot-", suffix=".png")
        os.close(fd)
    if plat.is_macos():
        # screencapture is part of macOS and writes PNG directly; -x silences the shutter sound.
        # sips (also built in) does the downscale, so this needs no third-party imaging either.
        # Window-targeted capture needs a CGWindowID, which System Events does not hand out — so
        # this is full-screen only for now, and says so rather than pretending `title` worked.
        try:
            r = subprocess.run(["screencapture", "-x", "-t", "png", path], capture_output=True, timeout=30)
            if r.returncode != 0 or not os.path.exists(path):
                return {"ok": False, "error": "screencapture failed (grant Screen Recording permission "
                                              "in System Settings > Privacy & Security): " +
                                              (r.stderr or b"").decode("utf-8", "ignore")[:200]}
            subprocess.run(["sips", "-Z", str(max_dim), path], capture_output=True, timeout=30)
            return {"ok": True, "path": path, "how": "screencapture",
                    "title": "", "note": "macOS captures the whole screen; `title` is Windows-only for now"}
        except Exception as e:
            return {"ok": False, "error": "screencapture: %s" % e}
    if not plat.is_windows():
        return {"ok": False, "error": "screen capture is wired up for Windows and macOS; not %s"
                                      % plat.os_label()}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", _script_path(), "-Path", path, "-Title", title or "",
                            "-MaxDim", str(int(max_dim))],
                           timeout=60, capture_output=True, text=True,
                           **plat.no_window_kwargs(),
                           encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"ok": False, "error": "capture driver failed to run: %s" % e}
    txt = (r.stdout or "").strip()
    if not txt:
        return {"ok": False, "error": "capture driver produced no output: %s"
                                      % ((r.stderr or "").strip()[:300] or "(no stderr)")}
    try:
        return json.loads(txt.splitlines()[-1])
    except Exception:
        return {"ok": False, "error": "unparseable capture output: %s" % txt[:300]}


def _b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


# --------------------------------------------------------------------------- #
def _enabled() -> bool:
    """Read live at call time (COLLIE_SCREEN_CAPTURE), so enable_capability takes effect mid-session
    without re-registering anything — same contract as native.py's desktop gate."""
    return os.environ.get("COLLIE_SCREEN_CAPTURE", "").lower() in ("1", "on", "true")


# Screen capture is gated SEPARATELY from desktop control, not folded into it. They are different
# risks: desktop control can act, but capture can read anything on screen — an open password
# manager, a bank tab, a private message — and unlike a click that is invisible to the user, the
# image then travels to whatever model is configured. Consent should be asked for what it is.
_CONSENT = (
    "⛔ Screen capture is currently OFF. It lets me SEE the screen — a window, or everything on the "
    "display — and the image is sent to the model, so whatever is visible (password managers, "
    "private messages, bank tabs) would go with it. I won't turn it on silently. Ask the user in "
    "plain words whether to enable it and what it exposes; if they agree, call enable_capability "
    "with capability=\"screen_capture\", then retry. If they decline, say the step needs eyes and "
    "cannot be done without it.")


class ScreenshotTool(Tool):
    name = "screenshot"
    description = (
        "SEE the screen — returns an actual image you can look at, not a description. Use it when "
        "the question is visual: does this UI look right, did the layout break, what does this app "
        "show, what is in this dialog. Args: title (substring of a window title — captures THAT "
        "window even if it is behind others or off-screen, without stealing focus; omit for the "
        "whole screen), max_dim (longest edge in px, default 1568). Prefer a title over full screen: "
        "one window is a clearer image and far fewer tokens. For reading or clicking structure "
        "(buttons, fields, links) prefer desktop_inspect / browser_snapshot — a tree is exact where "
        "an image is a guess; use this to judge appearance, or when there is no tree to read.")
    schema = {"type": "object", "properties": {
        "title": {"type": "string", "description": "substring of the target window's title; omit for full screen"},
        "max_dim": {"type": "integer", "description": "longest edge in pixels (default 1568)"},
    }}

    def run(self, args, ctx):
        if not _enabled():
            return _CONSENT
        args = args or {}
        title = str(args.get("title") or "").strip()
        try:
            max_dim = int(args.get("max_dim") or _MAX_DIM)
        except (TypeError, ValueError):
            max_dim = _MAX_DIM
        max_dim = max(256, min(4096, max_dim))
        res = capture(title=title, max_dim=max_dim)
        if not res.get("ok"):
            msg = "ERROR: screenshot failed: %s" % res.get("error", "unknown")
            if res.get("windows"):
                msg += "\nVisible windows right now:\n" + str(res["windows"]).strip()
            return msg
        path = res["path"]
        try:
            data = _b64(path)
        except OSError as e:
            return "ERROR: captured but could not read %s: %s" % (path, e)
        # The image rides on ctx for the loop to attach as a real image block; the STRING returned
        # here stays a string so redaction, the result preview and history elision all keep working
        # exactly as they do for every other tool.
        try:
            ctx.images.append({"type": "image", "media_type": "image/png", "data": data,
                               "label": res.get("title") or ("full screen" if not title else title)})
        except AttributeError:
            return ("ERROR: this harness build cannot attach images to the conversation "
                    "(ToolCtx has no .images) — captured to %s but you cannot see it." % path)
        what = ("window %r" % res["title"]) if res.get("title") else "the full screen"
        note = (" " + res["note"]) if res.get("note") else ""
        src = ""
        if res.get("source_width") and res["source_width"] != res.get("width"):
            src = " (downscaled from %sx%s)" % (res["source_width"], res["source_height"])
        return ("Captured %s at %sx%s%s via %s.%s The image is attached — look at it.\nSaved: %s"
                % (what, res.get("width", "?"), res.get("height", "?"), src,
                   res.get("how", "?"), note, path))


def register_screenshot(registry) -> None:
    """Always registered so collie can SEE it has eyes available; deferred + refusing to run until
    the user consents while the setting is off (native.py's pattern, same reasoning)."""
    t = ScreenshotTool()
    t.tier = "always" if _enabled() else "deferred"
    registry.register(t)
