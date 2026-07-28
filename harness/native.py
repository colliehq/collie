"""Collie native-app control — drive any Windows desktop app in the BACKGROUND via UI Automation.

Zero pip deps (like harness/desktop.py): everything goes through Windows PowerShell + .NET
`System.Windows.Automation` (UIA), so it works inside the frozen embeddable python. UIA lets us act
on an app by its accessibility tree — invoke a control, set a field, read text — WITHOUT bringing the
window to the foreground or moving the system cursor, so the user can keep working while Collie
operates another app.

The "no-foreground contract": we prefer UIA patterns (InvokePattern / ValuePattern) which don't need
focus. When a control exposes no usable pattern, we DON'T silently fall back to a blind coordinate
click — we report `needs_foreground` so the caller can decide.

SAFETY (learned the hard way): closing an app must NEVER `Stop-Process` — Win11 Notepad and many
others are one multi-window process, so killing the pid takes the user's other windows with it. We
close only the specific window via WindowPattern.Close. And `set_value` REPLACES a field's whole
contents, so it's treated as destructive by callers.
"""

# Declared, not implied — tests/test_platform_purity.py reads this. Everything below is PowerShell
# plus .NET System.Windows.Automation; there is no macOS or Linux path here at all.
import sys

PLATFORM = "windows"

from . import plat

import json
import os
import subprocess

HOME = os.path.expanduser("~")
COLLIE_DIR = os.path.join(HOME, ".collie")
_DRIVER = os.path.join(COLLIE_DIR, "native_uia.ps1")
_NOWIN = 0x08000000  # CREATE_NO_WINDOW

# The UIA driver. One script, dispatched by -Action, JSON in / JSON out. Kept on disk (written once)
# so we invoke it with -File and never fight -Command quoting.
_DRIVER_PS = r'''
param(
  [string]$Action = "windows",
  [string]$Match = "",
  [int]$PidArg = 0,
  [int]$Index = -1,
  [string]$Aid = "",
  [string]$Text = "",
  [int]$Max = 60
)
$ErrorActionPreference = "Stop"
try {
  Add-Type -AssemblyName UIAutomationClient
  Add-Type -AssemblyName UIAutomationTypes
} catch { Write-Output (@{ ok = $false; error = "UIA assemblies unavailable: $($_.Exception.Message)" } | ConvertTo-Json -Compress); exit 0 }

$AE   = [System.Windows.Automation.AutomationElement]
$SCOPE = [System.Windows.Automation.TreeScope]
$root = $AE::RootElement

function Top-Windows {
  $c = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Window)
  $root.FindAll($SCOPE::Children, $c)
}

function Find-Window {
  # by pid first (exact), else first top-level window whose Name contains $Match (case-insensitive)
  foreach ($w in (Top-Windows)) {
    try {
      if ($PidArg -gt 0) { if ($w.Current.ProcessId -eq $PidArg) { return $w } }
      elseif ($Match -ne "") { if ($w.Current.Name -and $w.Current.Name.ToLower().Contains($Match.ToLower())) { return $w } }
    } catch {}
  }
  return $null
}

function Descendants($win) {
  $win.FindAll($SCOPE::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
}

function Elem-Info($e, $i) {
  $r = $e.Current.BoundingRectangle
  $val = $null
  $vp = $null
  if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) { try { $val = $vp.Current.Value } catch {} }
  $pats = @()
  $tmp = $null
  if ($e.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$tmp)) { $pats += "invoke" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$tmp))  { $pats += "value" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$tmp)) { $pats += "toggle" }
  [ordered]@{
    index = $i
    type  = $e.Current.ControlType.ProgrammaticName -replace "ControlType.",""
    name  = $e.Current.Name
    aid   = $e.Current.AutomationId
    enabled = $e.Current.IsEnabled
    value = $val
    patterns = $pats
    rect = [ordered]@{ x = [int]$r.X; y = [int]$r.Y; w = [int]$r.Width; h = [int]$r.Height }
  }
}

function Pick($win) {
  # select an element in $win by AutomationId (preferred) or by descendant index
  if ($Aid -ne "") {
    $c = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $Aid)
    return $win.FindFirst($SCOPE::Descendants, $c)
  }
  if ($Index -ge 0) {
    $ds = Descendants $win
    if ($Index -lt $ds.Count) { return $ds[$Index] }
  }
  return $null
}

function Fg-Info {
  $sig = @"
using System; using System.Runtime.InteropServices;
public class _FG { [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid); }
"@
  if (-not ("_FG" -as [type])) { Add-Type $sig }
  $h = [_FG]::GetForegroundWindow(); $fp = 0; [void][_FG]::GetWindowThreadProcessId($h, [ref]$fp); return $fp
}

$out = $null
switch ($Action) {
  "windows" {
    $arr = @()
    foreach ($w in (Top-Windows)) { try { if ($w.Current.Name) { $arr += [ordered]@{ title = $w.Current.Name; class = $w.Current.ClassName; pid = $w.Current.ProcessId } } } catch {} }
    $out = @{ ok = $true; windows = $arr }
  }
  "foreground" { $out = @{ ok = $true; pid = (Fg-Info) } }
  default {
    $win = Find-Window
    if (-not $win) { $out = @{ ok = $false; error = "window not found (match='$Match' pid=$PidArg)" }; break }
    $wpid = $win.Current.ProcessId
    switch ($Action) {
      "tree" {
        $ds = Descendants $win; $arr = @(); $n = [Math]::Min($Max, $ds.Count)
        for ($i = 0; $i -lt $n; $i++) { try { $arr += (Elem-Info $ds[$i] $i) } catch {} }
        $out = @{ ok = $true; window = @{ title = $win.Current.Name; pid = $wpid }; count = $ds.Count; elements = $arr }
      }
      "invoke" {
        $e = Pick $win
        if (-not $e) { $out = @{ ok = $false; error = "element not found (aid='$Aid' index=$Index)" }; break }
        $ip = $null
        if ($e.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip)) {
          ($ip -as [System.Windows.Automation.InvokePattern]).Invoke()
          $out = @{ ok = $true; action = "invoke"; target = @{ name = $e.Current.Name; aid = $e.Current.AutomationId } }
        } else {
          $out = @{ ok = $false; needs_foreground = $true; error = "no InvokePattern on target (name='$($e.Current.Name)')" }
        }
      }
      "setvalue" {
        $e = Pick $win
        if (-not $e) { $out = @{ ok = $false; error = "element not found (aid='$Aid' index=$Index)" }; break }
        $vp = $null
        if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
          $v = $vp -as [System.Windows.Automation.ValuePattern]
          if ($v.Current.IsReadOnly) { $out = @{ ok = $false; error = "field is read-only" }; break }
          $v.SetValue($Text); Start-Sleep -Milliseconds 120
          $out = @{ ok = $true; action = "setvalue"; readback = $v.Current.Value }
        } else {
          $out = @{ ok = $false; needs_foreground = $true; error = "no ValuePattern on target (would need focus+keys)" }
        }
      }
      "gettext" {
        $e = Pick $win
        if (-not $e) { $out = @{ ok = $false; error = "element not found (aid='$Aid' index=$Index)" }; break }
        $vp = $null; $txt = $e.Current.Name
        if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) { try { $txt = ($vp -as [System.Windows.Automation.ValuePattern]).Current.Value } catch {} }
        $out = @{ ok = $true; text = $txt; name = $e.Current.Name }
      }
      "close" {
        $wp = $null
        if ($win.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$wp)) {
          ($wp -as [System.Windows.Automation.WindowPattern]).Close()
          $out = @{ ok = $true; action = "close"; pid = $wpid }
        } else { $out = @{ ok = $false; error = "window has no WindowPattern (cannot close safely)" } }
      }
      default { $out = @{ ok = $false; error = "unknown action '$Action'" } }
    }
  }
}
Write-Output ($out | ConvertTo-Json -Depth 6 -Compress)
'''


def _ensure_driver():
    os.makedirs(COLLIE_DIR, exist_ok=True)
    # rewrite if missing or stale (content drift), so upgrades take effect
    try:
        if os.path.exists(_DRIVER):
            with open(_DRIVER, "r", encoding="utf-8") as f:
                if f.read() == _DRIVER_PS:
                    return _DRIVER
    except OSError:
        pass
    with open(_DRIVER, "w", encoding="utf-8") as f:
        f.write(_DRIVER_PS)
    return _DRIVER


def available():
    """(ok, why). UI Automation is a Windows API. macOS has its own surface in native_mac (System
    Events, the same Accessibility tree), so say where to go rather than only that this is not it."""
    if plat.is_macos():
        return False, "use harness.native_mac on macOS (System Events, not UI Automation)"
    if not plat.is_windows():
        return False, "native app control needs Windows (UI Automation) or macOS (System Events); " \
                      "not available on " + plat.os_label()
    return True, ""


def backend():
    """The module that can actually drive apps here, or None. One import for callers that do not
    want to care which platform they are on."""
    if plat.is_windows():
        return sys.modules[__name__]
    if plat.is_macos():
        from . import native_mac
        return native_mac
    return None


def _run(action, match="", pid=0, index=-1, aid="", text="", max=60, timeout=20):
    ok, why = available()
    if not ok:
        return {"ok": False, "error": why}
    """Invoke the UIA driver and return its parsed JSON (always a dict)."""
    _ensure_driver()
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _DRIVER,
           "-Action", action, "-Match", match or "", "-PidArg", str(int(pid or 0)),
           "-Index", str(int(index)), "-Aid", aid or "", "-Text", text or "",
           "-Max", str(int(max or 60))]
    try:
        r = subprocess.run(cmd, creationflags=_NOWIN, timeout=timeout,
                           capture_output=True, text=True, encoding="utf-8", errors="ignore")
    except Exception as e:
        return {"ok": False, "error": "driver failed: %s" % e}
    out = (r.stdout or "").strip()
    if not out:
        return {"ok": False, "error": (r.stderr or "no output").strip()[:400]}
    try:
        return json.loads(out)
    except Exception:
        return {"ok": False, "error": "bad driver output", "raw": out[:400]}


# ── public API ────────────────────────────────────────────────────────────────────────────────
# CONTRACT: these mirror harness/native_mac.py so harness/desktop.py's composer works identically on
# both OSes — windows()/apps() return {"ok":bool, ...} dicts, focus()/quit_app() return {"ok":bool}.
# (This parity was missing: desktop_intent was coded to the mac shape and 500'd on Windows.)
def _ps(script, timeout=10):
    """Run a PowerShell snippet, return its trimmed stdout (or '')."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                           creationflags=_NOWIN, timeout=timeout, capture_output=True, text=True,
                           encoding="utf-8", errors="ignore")
        return (r.stdout or "").strip()
    except Exception:
        return ""


def windows(match=""):
    """Top-level windows: {"ok":bool, "windows":[{title, class, pid}]}. Matches native_mac.windows()."""
    return _run("windows")


def apps():
    """Running apps that have a visible window: {"ok":True, "apps":[{"name":...}]}. 'name' is the
    process name (chrome, Notepad, Code) so a user's word matches. Mirrors native_mac.apps()."""
    out = _ps("Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle } | "
              "Select-Object -ExpandProperty ProcessName -Unique")
    seen, apps_ = set(), []
    for nm in out.splitlines():
        nm = nm.strip()
        if nm and nm.lower() not in seen:
            seen.add(nm.lower()); apps_.append({"name": nm})
    return {"ok": True, "apps": apps_}


def _find_ps(name):
    """PowerShell that selects the first process matching `name` by process name or window title."""
    n = (name or "").replace("'", "''")
    return ("Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and "
            "($_.ProcessName -eq '%s' -or $_.MainWindowTitle -like '*%s*') } | Select-Object -First 1" % (n, n))


def focus(name):
    """Bring a running app's main window to the foreground. Mirrors native_mac.focus()."""
    out = _ps("$p = %s; if ($p) { (New-Object -ComObject WScript.Shell).AppActivate($p.Id) | Out-Null; 'ok' }"
              % _find_ps(name))
    return {"ok": True} if out.strip().endswith("ok") else {"ok": False, "error": "%r is not running" % name}


def quit_app(name):
    """Gracefully close an app's main window (CloseMainWindow = WM_CLOSE, lets it prompt to save) —
    NEVER Stop-Process. Mirrors native_mac.quit_app()."""
    out = _ps("$p = %s; if ($p) { [void]$p.CloseMainWindow(); 'ok' }" % _find_ps(name))
    return {"ok": True} if out.strip().endswith("ok") else {"ok": False, "error": "%r is not running" % name}


def foreground_pid():
    """PID of the current foreground window (to prove an action didn't steal focus)."""
    return _run("foreground").get("pid", 0)


def tree(match="", pid=0, max=60):
    """Accessibility tree of a window (by name substring or pid): capped list of elements with
    index / type / name / automationId / value / patterns / rect."""
    return _run("tree", match=match, pid=pid, index=-1, aid="", text="", max=max)


def invoke(match="", pid=0, index=-1, aid=""):
    """Invoke a control (button/menu/link) by AutomationId or descendant index. Background — no focus.
    Returns {ok} or {ok:False, needs_foreground:True} per the no-foreground contract."""
    return _run("invoke", match=match, pid=pid, index=index, aid=aid)


def set_value(text, match="", pid=0, index=-1, aid=""):
    """Set an editable field's value. DESTRUCTIVE: replaces the whole field. Background — no focus."""
    return _run("setvalue", match=match, pid=pid, index=index, aid=aid, text=text)


def get_text(match="", pid=0, index=-1, aid=""):
    """Read a control's value/text."""
    return _run("gettext", match=match, pid=pid, index=index, aid=aid)


def close_window(match="", pid=0):
    """Close ONE window via WindowPattern.Close — never Stop-Process (won't take sibling windows)."""
    return _run("close", match=match, pid=pid)


def launch(target):
    """Start an app (path or shell target). Returns True on success. Window discovery is by name via
    windows()/tree() afterward (Win11 packaged apps run under a different pid than the launcher).

    Off Windows this defers to desktop.launch, which knows `open` and `xdg-open` — so a caller that
    reaches here on a Mac opens the app instead of silently returning False."""
    if not plat.is_windows():
        from . import desktop
        return desktop.launch(target)
    try:
        os.startfile(target)  # noqa: S606 - launching a user app is the point
        return True
    except Exception:
        return False


# ── agent tools ─────────────────────────────────────────────────────────────────────────────────
# Wire the UI-Automation surface above into collie's tool registry, so the agent can DRIVE any native
# app the way browser_* drives the browser: list apps → inspect a window's controls → click / type /
# read by a stable index or automationId, all in the BACKGROUND (no focus theft). Opt-in and Windows-
# only — powerful, so off unless COLLIE_DESKTOP_CONTROL=1 (the "Control desktop apps" setting).

def _dt_fence(s):
    return "```\n%s\n```" % s


def _dt_err(d):
    if isinstance(d, dict) and d.get("ok") is False:
        e = d.get("error") or "failed"
        if d.get("needs_foreground"):
            e += " — the window must be in the foreground for this; call desktop_focus first"
        return "ERROR(desktop): %s" % e
    return None


def _dt_elements(d):
    """Render a UIA tree dict as a compact numbered control list the model acts on by index/aid —
    the desktop analogue of browser_snapshot's `[e5] button \"Add to cart\"`."""
    els = d.get("elements") or d.get("tree") or d.get("controls") or d.get("nodes") or []
    if not els:
        return "(no controls found — try a broader window match, or this window exposes no UIA tree)"
    lines = []
    for e in els:
        typ = e.get("type") or e.get("controlType") or e.get("control") or "?"
        name = e.get("name") or e.get("text") or ""
        aid = e.get("automationId") or e.get("aid") or e.get("id") or ""
        val = e.get("value")
        pats = e.get("patterns") or e.get("actions") or ""
        if isinstance(pats, list):
            pats = ",".join(str(p) for p in pats)
        parts = ["[%s]" % e.get("index", "?"), str(typ)]
        if name:
            parts.append('"%s"' % str(name)[:70])
        if aid:
            parts.append("aid=%s" % aid)
        if val not in (None, ""):
            parts.append("value=%s" % str(val)[:50])
        if pats:
            parts.append("<%s>" % pats)
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _register_windows(registry):
    """Register the Windows desktop_* tools — UI Automation, addressed by index / automationId."""
    from .tools import Tool

    class DesktopApps(Tool):
        name, tier = "desktop_apps", "always"
        description = ("List the native desktop apps that currently have a visible window (Notepad, "
                       "Chrome, Code, …). START HERE to see what's open before inspecting or acting. "
                       "No args.")
        schema = {"type": "object", "properties": {}}

        def run(self, args, ctx):
            d = apps()
            err = _dt_err(d)
            if err:
                return err
            names = [a.get("name", "") for a in d.get("apps", [])]
            return _dt_fence("\n".join(names) if names else "(no apps with a visible window)")

    class DesktopInspect(Tool):
        name, tier = "desktop_inspect", "always"
        description = ("Snapshot a native window's controls as a numbered list — each with a stable "
                       "index, control type, accessible name, automationId, current value and its UIA "
                       "patterns, e.g. `[7] Button \"Save\" aid=saveBtn <Invoke>`. PREFER this over "
                       "guessing: pass an index or aid to desktop_click / desktop_type / desktop_read "
                       "to act on that exact control. Match a window by title/process substring (from "
                       "desktop_apps) or by pid. Args: match (window substring) OR pid; optional max.")
        schema = {"type": "object", "properties": {
            "match": {"type": "string"}, "pid": {"type": "integer"}, "max": {"type": "integer"}}}

        def run(self, args, ctx):
            d = tree(match=args.get("match", ""), pid=int(args.get("pid", 0) or 0),
                     max=int(args.get("max", 60) or 60))
            err = _dt_err(d)
            return err if err else _dt_fence(_dt_elements(d))

    class DesktopClick(Tool):
        name, tier = "desktop_click", "always"
        description = ("Invoke a control (button, menu item, link, checkbox) in a native window — a "
                       "real UIA Invoke, in the BACKGROUND (no focus theft). Identify the control by "
                       "the `index` or `aid` from desktop_inspect, plus the window `match`/`pid`. "
                       "Args: match|pid, and index OR aid.")
        schema = {"type": "object", "properties": {
            "match": {"type": "string"}, "pid": {"type": "integer"},
            "index": {"type": "integer"}, "aid": {"type": "string"}}}

        def run(self, args, ctx):
            d = invoke(match=args.get("match", ""), pid=int(args.get("pid", 0) or 0),
                       index=int(args.get("index") or -1), aid=args.get("aid", ""))
            err = _dt_err(d)
            return err if err else "ok — invoked"

    class DesktopType(Tool):
        name, tier = "desktop_type", "always"
        description = ("Set an editable field's value in a native window (DESTRUCTIVE — replaces the "
                       "whole field, not append). Identify the field by `index`/`aid` from "
                       "desktop_inspect plus the window `match`/`pid`. Args: text, match|pid, and "
                       "index OR aid.")
        schema = {"type": "object", "properties": {
            "text": {"type": "string"}, "match": {"type": "string"}, "pid": {"type": "integer"},
            "index": {"type": "integer"}, "aid": {"type": "string"}}, "required": ["text"]}

        def run(self, args, ctx):
            d = set_value(args.get("text", ""), match=args.get("match", ""),
                          pid=int(args.get("pid", 0) or 0), index=int(args.get("index") or -1),
                          aid=args.get("aid", ""))
            err = _dt_err(d)
            return err if err else "ok — set"

    class DesktopRead(Tool):
        name, tier = "desktop_read", "always"
        description = ("Read a control's text/value in a native window (by `index`/`aid` from "
                       "desktop_inspect + the window `match`/`pid`). Use to verify an action landed or "
                       "to pull text out of an app. Args: match|pid, and index OR aid.")
        schema = {"type": "object", "properties": {
            "match": {"type": "string"}, "pid": {"type": "integer"},
            "index": {"type": "integer"}, "aid": {"type": "string"}}}

        def run(self, args, ctx):
            d = get_text(match=args.get("match", ""), pid=int(args.get("pid", 0) or 0),
                         index=int(args.get("index") or -1), aid=args.get("aid", ""))
            err = _dt_err(d)
            if err:
                return err
            return _dt_fence(str(d.get("text", d.get("value", ""))))

    class DesktopLaunch(Tool):
        name, tier = "desktop_launch", "always"
        description = ("Start a native app by name or path (e.g. \"notepad\", \"calc\", or a full "
                       "path). After launching, call desktop_apps / desktop_inspect to find its window "
                       "(packaged apps run under a different pid than the launcher). Args: target.")
        schema = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}

        def run(self, args, ctx):
            return "ok — launched" if launch(args.get("target", "")) else "ERROR(desktop): could not launch %r" % args.get("target", "")

    class DesktopFocus(Tool):
        name, tier = "desktop_focus", "always"
        description = ("Bring a native app's window to the foreground by name/title substring. Most "
                       "desktop_* actions work in the background, but a few controls only respond when "
                       "focused — call this if desktop_click reports it needs the foreground. Args: name.")
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

        def run(self, args, ctx):
            d = focus(args.get("name", ""))
            err = _dt_err(d)
            return err if err else "ok — focused"

    for t in (DesktopApps(), DesktopInspect(), DesktopClick(), DesktopType(),
              DesktopRead(), DesktopLaunch(), DesktopFocus()):
        registry.register(t)


def _register_mac(registry):
    """Register the macOS desktop_* tools — System Events / Accessibility, addressed by control NAME
    (label). Same tool names as Windows so the agent's model is identical; the difference is you click
    by label instead of index/aid, and macOS adds desktop_menu (where most Mac functionality lives)."""
    from .tools import Tool
    from . import native_mac as nm
    from . import desktop as _desktop

    def _err(d):
        if isinstance(d, dict) and d.get("ok") is False:
            return "ERROR(desktop): %s" % (d.get("error") or "failed")
        return None

    class DesktopApps(Tool):
        name, tier = "desktop_apps", "always"
        description = ("List the native macOS apps that have a UI (Safari, Notes, Finder, …). START "
                       "HERE to see what's open before inspecting or acting. No args.")
        schema = {"type": "object", "properties": {}}

        def run(self, args, ctx):
            d = nm.apps(); e = _err(d)
            if e:
                return e
            return _dt_fence("\n".join(a.get("name", "") for a in d.get("apps", [])) or "(none)")

    class DesktopInspect(Tool):
        name, tier = "desktop_inspect", "always"
        description = ("List the controls of an app's FRONT window as `role \"name\"` lines. The name "
                       "is the handle you pass to desktop_click / desktop_type. Needs macOS Accessibility "
                       "permission for Collie. Args: match (the app name, e.g. \"Safari\"); optional max.")
        schema = {"type": "object", "properties": {
            "match": {"type": "string"}, "max": {"type": "integer"}}, "required": ["match"]}

        def run(self, args, ctx):
            d = nm.tree(args.get("match", ""), max_items=int(args.get("max", 60) or 60)); e = _err(d)
            if e:
                return e
            items = d.get("items", [])
            if not items:
                return "(no controls — grant Accessibility to Collie, or the app has no front window)"
            return _dt_fence("\n".join('%s "%s"' % (i.get("role", "?"), i.get("name", "")) for i in items))

    class DesktopClick(Tool):
        name, tier = "desktop_click", "always"
        description = ("Click a control (button, menu item) by its NAME in an app's front window (from "
                       "desktop_inspect). Args: match (app name), label (the control's name).")
        schema = {"type": "object", "properties": {
            "match": {"type": "string"}, "label": {"type": "string"}}, "required": ["match", "label"]}

        def run(self, args, ctx):
            return _err(nm.click(args.get("match", ""), args.get("label", ""))) or "ok — clicked"

    class DesktopType(Tool):
        name, tier = "desktop_type", "always"
        description = ("Type text into an app — into whatever control currently has focus, so click the "
                       "field first with desktop_click if needed. Brings the app to the front. Args: "
                       "match (app name), text.")
        schema = {"type": "object", "properties": {
            "match": {"type": "string"}, "text": {"type": "string"}}, "required": ["match", "text"]}

        def run(self, args, ctx):
            return _err(nm.type_text(args.get("match", ""), args.get("text", ""))) or "ok — typed"

    class DesktopMenu(Tool):
        name, tier = "desktop_menu", "always"
        description = ("Drive an app's menu bar — where most macOS functionality actually lives, and "
                       "more stable than on-screen controls, e.g. match=\"Safari\" menu=\"File\" "
                       "item=\"New Window\". Args: match (app name), menu (top-level menu), item.")
        schema = {"type": "object", "properties": {
            "match": {"type": "string"}, "menu": {"type": "string"}, "item": {"type": "string"}},
            "required": ["match", "menu", "item"]}

        def run(self, args, ctx):
            return _err(nm.menu(args.get("match", ""), args.get("menu", ""), args.get("item", ""))) or "ok — menu"

    class DesktopFocus(Tool):
        name, tier = "desktop_focus", "always"
        description = ("Bring a macOS app to the front by name. Args: name.")
        schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

        def run(self, args, ctx):
            return _err(nm.focus(args.get("name", ""))) or "ok — focused"

    class DesktopLaunch(Tool):
        name, tier = "desktop_launch", "always"
        description = ("Open/launch a macOS app by name or path (e.g. \"Safari\", \"Notes\"). After "
                       "launching, call desktop_apps / desktop_inspect to work with it. Args: target.")
        schema = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}

        def run(self, args, ctx):
            try:
                ok = _desktop.launch(args.get("target", ""))
            except Exception as ex:
                return "ERROR(desktop): %s" % ex
            return "ok — launched" if ok else "ERROR(desktop): could not launch %r" % args.get("target", "")

    for t in (DesktopApps(), DesktopInspect(), DesktopClick(), DesktopType(),
              DesktopMenu(), DesktopFocus(), DesktopLaunch()):
        registry.register(t)


def register_native(registry):
    """Register the desktop_* app-control tools for THIS platform: Windows UI Automation (by index /
    automationId) or macOS System Events (by control name/label, plus desktop_menu). Same tool names
    on both, so the agent drives native apps the same way regardless of OS."""
    if plat.is_macos():
        _register_mac(registry)
    elif plat.is_windows():
        _register_windows(registry)
