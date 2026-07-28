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


def _run(action, match="", pid=0, index=-1, aid="", text="", timeout=20):
    ok, why = available()
    if not ok:
        return {"ok": False, "error": why}
    """Invoke the UIA driver and return its parsed JSON (always a dict)."""
    _ensure_driver()
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _DRIVER,
           "-Action", action, "-Match", match or "", "-PidArg", str(int(pid or 0)),
           "-Index", str(int(index)), "-Aid", aid or "", "-Text", text or ""]
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
    return _run("tree", match=match, pid=pid, index=-1, aid="", text="")


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
