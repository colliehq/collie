"""Collie native-app control — drive Windows apps through semantic APIs before input simulation.

Zero pip deps (like harness/desktop.py): everything goes through Windows PowerShell + .NET
`System.Windows.Automation` (UIA), MSAA/IAccessible, and documented Win32 control messages, so it
works inside the frozen embeddable python. These layers can invoke a control, set a field, and read
state WITHOUT bringing the window to the foreground or moving the system cursor.

The control ladder is UIA -> MSAA -> Win32 messages -> keyboard -> mouse. Every result names its
actual method/layer. Foreground input fallbacks are explicit options and mouse is always last.

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
  [long]$Hwnd = 0,
  [int]$Index = -1,
  [string]$Aid = "",
  [string]$Name = "",
  [string]$ControlType = "",
  [int]$Occurrence = 0,
  [string]$Operation = "activate",
  [string]$Text = "",
  [double]$Number = 0,
  [double]$Horizontal = -1,
  [double]$Vertical = -1,
  [double]$X = 0,
  [double]$Y = 0,
  [double]$Width = 0,
  [double]$Height = 0,
  [double]$Degrees = 0,
  [int]$Row = -1,
  [int]$Column = -1,
  [int]$ViewId = -1,
  [string]$HorizontalAmount = "NoAmount",
  [string]$VerticalAmount = "NoAmount",
  [string]$Dock = "None",
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
  # A real HWND is the authoritative route. Custom-rendered apps (including WeChat)
  # can be absent from AutomationElement.RootElement while FromHandle still returns
  # the provider/root pane they expose. The Python side resolves title/process/pid to
  # a HWND with Win32 EnumWindows before invoking this script.
  if ($Hwnd -ne 0) {
    try { return $AE::FromHandle([IntPtr]$Hwnd) } catch { return $null }
  }
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

function Safe-Int($v) {
  try {
    $d = [double]$v
    if ([double]::IsNaN($d) -or [double]::IsInfinity($d) -or $d -gt [int]::MaxValue -or $d -lt [int]::MinValue) { return 0 }
    return [int]$d
  } catch { return 0 }
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
  if ($e.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$tmp)) { $pats += "select" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.SelectionPattern]::Pattern, [ref]$tmp)) { $pats += "selection" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$tmp)) { $pats += "expandcollapse" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern, [ref]$tmp)) { $pats += "scrollitem" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern, [ref]$tmp)) { $pats += "scroll" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$tmp)) { $pats += "rangevalue" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$tmp)) { $pats += "text" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.GridPattern]::Pattern, [ref]$tmp)) { $pats += "grid" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.GridItemPattern]::Pattern, [ref]$tmp)) { $pats += "griditem" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.TablePattern]::Pattern, [ref]$tmp)) { $pats += "table" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.TableItemPattern]::Pattern, [ref]$tmp)) { $pats += "tableitem" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$tmp)) { $pats += "window" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.TransformPattern]::Pattern, [ref]$tmp)) { $pats += "transform" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.DockPattern]::Pattern, [ref]$tmp)) { $pats += "dock" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.MultipleViewPattern]::Pattern, [ref]$tmp)) { $pats += "multipleview" }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.VirtualizedItemPattern]::Pattern, [ref]$tmp)) { $pats += "virtualizeditem" }
  if ($e.Current.IsKeyboardFocusable) { $pats += "focus" }
  $state = [ordered]@{}
  $tmp = $null
  if ($e.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$tmp)) {
    try { $state.toggle = ($tmp -as [System.Windows.Automation.TogglePattern]).Current.ToggleState.ToString() } catch {}
  }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$tmp)) {
    try { $state.selected = ($tmp -as [System.Windows.Automation.SelectionItemPattern]).Current.IsSelected } catch {}
  }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$tmp)) {
    try { $state.expanded = ($tmp -as [System.Windows.Automation.ExpandCollapsePattern]).Current.ExpandCollapseState.ToString() } catch {}
  }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$tmp)) {
    try { $q = ($tmp -as [System.Windows.Automation.RangeValuePattern]).Current; $state.range = [ordered]@{ value=$q.Value; minimum=$q.Minimum; maximum=$q.Maximum; small_change=$q.SmallChange; large_change=$q.LargeChange; readonly=$q.IsReadOnly } } catch {}
  }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.GridPattern]::Pattern, [ref]$tmp)) {
    try { $q = ($tmp -as [System.Windows.Automation.GridPattern]).Current; $state.grid = [ordered]@{ rows=$q.RowCount; columns=$q.ColumnCount } } catch {}
  }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.GridItemPattern]::Pattern, [ref]$tmp)) {
    try { $q = ($tmp -as [System.Windows.Automation.GridItemPattern]).Current; $state.grid_item = [ordered]@{ row=$q.Row; column=$q.Column; row_span=$q.RowSpan; column_span=$q.ColumnSpan } } catch {}
  }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$tmp)) {
    try { $q = ($tmp -as [System.Windows.Automation.WindowPattern]).Current; $state.window = [ordered]@{ visual_state=$q.WindowVisualState.ToString(); interaction_state=$q.WindowInteractionState.ToString(); modal=$q.IsModal; topmost=$q.IsTopmost } } catch {}
  }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.DockPattern]::Pattern, [ref]$tmp)) {
    try { $state.dock = ($tmp -as [System.Windows.Automation.DockPattern]).Current.DockPosition.ToString() } catch {}
  }
  if ($e.TryGetCurrentPattern([System.Windows.Automation.MultipleViewPattern]::Pattern, [ref]$tmp)) {
    try { $q = ($tmp -as [System.Windows.Automation.MultipleViewPattern]); $state.view = [ordered]@{ current=$q.Current.CurrentView; supported=@($q.GetSupportedViews()) } } catch {}
  }
  [ordered]@{
    index = $i
    type  = $e.Current.ControlType.ProgrammaticName -replace "ControlType.",""
    name  = $e.Current.Name
    aid   = $e.Current.AutomationId
    native_hwnd = [long]$e.Current.NativeWindowHandle
    enabled = $e.Current.IsEnabled
    focusable = $e.Current.IsKeyboardFocusable
    focused = $e.Current.HasKeyboardFocus
    offscreen = $e.Current.IsOffscreen
    value = $val
    patterns = $pats
    state = $state
    rect = [ordered]@{ x = (Safe-Int $r.X); y = (Safe-Int $r.Y); w = (Safe-Int $r.Width); h = (Safe-Int $r.Height) }
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
  if ($Name -ne "" -or $ControlType -ne "") {
    $wantName = $Name.ToLower()
    $wantType = $ControlType.ToLower() -replace '^controltype\.',''
    $seen = 0
    foreach ($e in (Descendants $win)) {
      try {
        $gotName = ($e.Current.Name + "").ToLower()
        $gotType = (($e.Current.ControlType.ProgrammaticName -replace 'ControlType\.','') + "").ToLower()
        $nameOK = ($Name -eq "" -or $gotName -eq $wantName -or $gotName.Contains($wantName))
        $typeOK = ($ControlType -eq "" -or $gotType -eq $wantType)
        if ($nameOK -and $typeOK) {
          if ($seen -eq $Occurrence) { return $e }
          $seen++
        }
      } catch {}
    }
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
      "element" {
        $e = Pick $win
        if (-not $e) { $out = @{ ok = $false; error = "element not found (aid='$Aid' index=$Index)" } }
        else { $out = @{ ok = $true; element = (Elem-Info $e $Index) } }
      }
      "invoke" {
        $e = Pick $win
        if (-not $e -and $Index -lt 0 -and $Aid -eq "" -and $Name -eq "" -and $ControlType -eq "" -and
            $Operation -in @("move","resize","rotate","dock","set_view","window_minimize","window_maximize","window_restore","window_close","wait_ready")) { $e = $win }
        if (-not $e) { $out = @{ ok = $false; error = "element not found (aid='$Aid' index=$Index)" }; break }
        $targetInfo = Elem-Info $e $Index
        $done = ""; $method = ""; $p = $null; $details = [ordered]@{}
        try {
          if (($Operation -eq "activate" -or $Operation -eq "invoke") -and
              $e.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.InvokePattern]).Invoke(); $done = "invoke"; $method = "uia.InvokePattern"
          } elseif (($Operation -eq "activate" -or $Operation -eq "toggle") -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.TogglePattern]).Toggle(); $done = "toggle"; $method = "uia.TogglePattern"
          } elseif (($Operation -eq "activate" -or $Operation -eq "select") -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.SelectionItemPattern]).Select(); $done = "select"; $method = "uia.SelectionItemPattern.Select"
          } elseif ($Operation -eq "add_to_selection" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.SelectionItemPattern]).AddToSelection(); $done = "add_to_selection"; $method = "uia.SelectionItemPattern.AddToSelection"
          } elseif ($Operation -eq "remove_from_selection" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.SelectionItemPattern]).RemoveFromSelection(); $done = "remove_from_selection"; $method = "uia.SelectionItemPattern.RemoveFromSelection"
          } elseif (($Operation -eq "activate" -or $Operation -eq "expand") -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.ExpandCollapsePattern]).Expand(); $done = "expand"; $method = "uia.ExpandCollapsePattern.Expand"
          } elseif ($Operation -eq "collapse" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.ExpandCollapsePattern]).Collapse(); $done = "collapse"; $method = "uia.ExpandCollapsePattern.Collapse"
          } elseif ($Operation -eq "scroll_into_view" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.ScrollItemPattern]).ScrollIntoView(); $done = "scroll_into_view"; $method = "uia.ScrollItemPattern"
          } elseif ($Operation -eq "realize" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.VirtualizedItemPattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.VirtualizedItemPattern]).Realize(); $done = "realize"; $method = "uia.VirtualizedItemPattern"
          } elseif ($Operation -eq "scroll" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern, [ref]$p)) {
            $ha = [Enum]::Parse([System.Windows.Automation.ScrollAmount], $HorizontalAmount, $true)
            $va = [Enum]::Parse([System.Windows.Automation.ScrollAmount], $VerticalAmount, $true)
            ($p -as [System.Windows.Automation.ScrollPattern]).Scroll($ha, $va); $done = "scroll"; $method = "uia.ScrollPattern.Scroll"
          } elseif ($Operation -eq "set_scroll_percent" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.ScrollPattern]).SetScrollPercent($Horizontal, $Vertical); $done = "set_scroll_percent"; $method = "uia.ScrollPattern.SetScrollPercent"
          } elseif ($Operation -eq "move" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.TransformPattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.TransformPattern]).Move($X, $Y); $done = "move"; $method = "uia.TransformPattern.Move"
          } elseif ($Operation -eq "resize" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.TransformPattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.TransformPattern]).Resize($Width, $Height); $done = "resize"; $method = "uia.TransformPattern.Resize"
          } elseif ($Operation -eq "rotate" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.TransformPattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.TransformPattern]).Rotate($Degrees); $done = "rotate"; $method = "uia.TransformPattern.Rotate"
          } elseif ($Operation -eq "dock" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.DockPattern]::Pattern, [ref]$p)) {
            $where = [Enum]::Parse([System.Windows.Automation.DockPosition], $Dock, $true)
            ($p -as [System.Windows.Automation.DockPattern]).SetDockPosition($where); $done = "dock"; $method = "uia.DockPattern"
          } elseif ($Operation -eq "set_view" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.MultipleViewPattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.MultipleViewPattern]).SetCurrentView($ViewId); $done = "set_view"; $method = "uia.MultipleViewPattern"
          } elseif (($Operation -eq "window_minimize" -or $Operation -eq "window_maximize" -or $Operation -eq "window_restore") -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$p)) {
            $visual = if ($Operation -eq "window_minimize") { "Minimized" } elseif ($Operation -eq "window_maximize") { "Maximized" } else { "Normal" }
            ($p -as [System.Windows.Automation.WindowPattern]).SetWindowVisualState([Enum]::Parse([System.Windows.Automation.WindowVisualState], $visual)); $done = $Operation; $method = "uia.WindowPattern.SetWindowVisualState"
          } elseif ($Operation -eq "window_close" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$p)) {
            ($p -as [System.Windows.Automation.WindowPattern]).Close(); $done = "window_close"; $method = "uia.WindowPattern.Close"
          } elseif ($Operation -eq "wait_ready" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$p)) {
            $ready = ($p -as [System.Windows.Automation.WindowPattern]).WaitForInputIdle([Math]::Max(0, [int]$Number)); $done = "wait_ready"; $method = "uia.WindowPattern.WaitForInputIdle"; $details.ready = $ready
          } elseif ($Operation -eq "get_selection" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.SelectionPattern]::Pattern, [ref]$p)) {
            $items = @(); $j = 0; foreach ($item in (($p -as [System.Windows.Automation.SelectionPattern]).Current.GetSelection())) { $items += (Elem-Info $item $j); $j++ }
            $done = "get_selection"; $method = "uia.SelectionPattern.GetSelection"; $details.items = $items
          } elseif ($Operation -eq "get_grid_item" -and
                    $e.TryGetCurrentPattern([System.Windows.Automation.GridPattern]::Pattern, [ref]$p)) {
            if ($Row -lt 0 -or $Column -lt 0) { throw "row and column must be non-negative" }
            $item = ($p -as [System.Windows.Automation.GridPattern]).GetItem($Row, $Column)
            $done = "get_grid_item"; $method = "uia.GridPattern.GetItem"; $details.item = (Elem-Info $item -1)
          } elseif ($Operation -eq "focus") {
            $e.SetFocus(); $done = "focus"; $method = "uia.AutomationElement.SetFocus"
          }
        } catch {
          $out = @{ ok = $false; error = "$Operation failed: $($_.Exception.Message)" }
          break
        }
        if ($done -ne "") {
          try { $targetInfo = Elem-Info $e $Index } catch {}
          $out = @{ ok = $true; action = $done; method = $method; layer = "uia"; details = $details; target = $targetInfo }
        } else {
          $out = @{ ok = $false; needs_native = $true; needs_keyboard = $true; needs_coordinate = $true;
                    error = "target exposes no UIA pattern for operation '$Operation'";
                    target = (Elem-Info $e $Index) }
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
          $out = @{ ok = $true; action = "setvalue"; method = "uia.ValuePattern"; layer = "uia"; readback = $v.Current.Value }
        } else {
          $out = @{ ok = $false; needs_native = $true; needs_keyboard = $true;
                    error = "no ValuePattern on target";
                    target = (Elem-Info $e $Index) }
        }
      }
      "setrange" {
        $e = Pick $win
        if (-not $e) { $out = @{ ok = $false; error = "element not found" }; break }
        $rp = $null
        if ($e.TryGetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$rp)) {
          $range = $rp -as [System.Windows.Automation.RangeValuePattern]
          if ($range.Current.IsReadOnly) { $out = @{ ok = $false; error = "range is read-only" }; break }
          $range.SetValue($Number)
          $out = @{ ok = $true; action = "setrange"; method = "uia.RangeValuePattern"; layer = "uia"; value = $range.Current.Value;
                    minimum = $range.Current.Minimum; maximum = $range.Current.Maximum }
        } else { $out = @{ ok = $false; error = "target has no RangeValuePattern" } }
      }
      "gettext" {
        $e = Pick $win
        if (-not $e) { $out = @{ ok = $false; error = "element not found (aid='$Aid' index=$Index)" }; break }
        $vp = $null; $tp = $null; $txt = $null; $method = ""
        if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
          try { $txt = ($vp -as [System.Windows.Automation.ValuePattern]).Current.Value; $method = "uia.ValuePattern" } catch {}
        }
        if ($null -eq $txt -and $e.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$tp)) {
          try { $txt = ($tp -as [System.Windows.Automation.TextPattern]).DocumentRange.GetText($Max); $method = "uia.TextPattern" } catch {}
        }
        $info = Elem-Info $e $Index
        if ($null -ne $txt) {
          $out = @{ ok = $true; text = $txt; name = $e.Current.Name; method = $method; layer = "uia" }
        } elseif ($info.native_hwnd -ne 0) {
          $out = @{ ok = $false; needs_native = $true; error = "no UIA text pattern"; target = $info; uia_name = $e.Current.Name }
        } else {
          $out = @{ ok = $true; text = $e.Current.Name; name = $e.Current.Name; method = "uia.NameProperty"; layer = "uia" }
        }
      }
      "close" {
        $wp = $null
        if ($win.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$wp)) {
          ($wp -as [System.Windows.Automation.WindowPattern]).Close()
          $out = @{ ok = $true; action = "close"; method = "uia.WindowPattern.Close"; layer = "uia"; pid = $wpid }
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


def _run(action, match="", pid=0, hwnd=0, index=-1, aid="", name="", control_type="",
         occurrence=0, operation="activate", text="", number=0, horizontal=-1,
         vertical=-1, x=0, y=0, width=0, height=0, degrees=0, row=-1,
         column=-1, view_id=-1, horizontal_amount="NoAmount",
         vertical_amount="NoAmount", dock="None", max=60, timeout=20):
    ok, why = available()
    if not ok:
        return {"ok": False, "error": why}
    """Invoke the UIA driver and return its parsed JSON (always a dict)."""
    _ensure_driver()
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _DRIVER,
           "-Action", action, "-Match", match or "", "-PidArg", str(int(pid or 0)),
           "-Hwnd", str(int(hwnd or 0)), "-Index", str(int(index)), "-Aid", aid or "",
           "-Name", name or "", "-ControlType", control_type or "",
           "-Occurrence", str(int(occurrence or 0)), "-Operation", operation or "activate",
           "-Text", text or "", "-Number", str(float(number or 0)),
           "-Horizontal", str(float(horizontal)), "-Vertical", str(float(vertical)),
           "-X", str(float(x or 0)), "-Y", str(float(y or 0)),
           "-Width", str(float(width or 0)), "-Height", str(float(height or 0)),
           "-Degrees", str(float(degrees or 0)), "-Row", str(int(row)),
           "-Column", str(int(column)), "-ViewId", str(int(view_id)),
           "-HorizontalAmount", horizontal_amount or "NoAmount",
           "-VerticalAmount", vertical_amount or "NoAmount", "-Dock", dock or "None",
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


def windows(match="", include_hidden=False):
    """Real Win32 top-level windows, including HWND/process/bounds.

    UIA's RootElement omits some custom windows even while they are visibly on screen.
    EnumWindows is therefore the source of truth for discovery; UIA is attempted from
    the selected HWND afterwards.
    """
    try:
        from . import native_input as ni
        rows = ni.windows(include_hidden=bool(include_hidden))
    except Exception as exc:
        return {"ok": False, "error": "Win32 window enumeration failed: %s" % exc,
                "windows": []}
    want = (match or "").strip().lower()
    if want:
        rows = [row for row in rows if want in " ".join((
            row.get("title", ""), row.get("process", ""), row.get("class", ""),
            row.get("path", ""))).lower()]
    return {"ok": True, "windows": rows}


def apps():
    """Running apps that have a visible window: {"ok":True, "apps":[{"name":...}]}. 'name' is the
    process name (chrome, Notepad, Code) so a user's word matches. Mirrors native_mac.apps()."""
    got = windows()
    if not got.get("ok"):
        return got
    seen, apps_ = set(), []
    for row in got.get("windows", []):
        nm = (row.get("process") or row.get("title") or "").strip()
        if nm and nm.lower() not in seen:
            seen.add(nm.lower())
            apps_.append({"name": nm, "pid": row.get("pid"), "hwnd": row.get("hwnd"),
                          "title": row.get("title", "")})
    return {"ok": True, "apps": apps_}


def _find_ps(name):
    """PowerShell that selects the first process matching `name` by process name or window title."""
    n = (name or "").replace("'", "''")
    return ("Get-Process | Where-Object { $_.MainWindowHandle -ne 0 -and "
            "($_.ProcessName -eq '%s' -or $_.MainWindowTitle -like '*%s*') } | Select-Object -First 1" % (n, n))


def focus(name="", pid=0, hwnd=0):
    """Bring an exact native window to the foreground."""
    try:
        from . import native_input as ni
        return ni.focus_window(match=name, pid=pid, hwnd=hwnd)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def quit_app(name):
    """Gracefully close an app's main window (CloseMainWindow = WM_CLOSE, lets it prompt to save) —
    NEVER Stop-Process. Mirrors native_mac.quit_app()."""
    out = _ps("$p = %s; if ($p) { [void]$p.CloseMainWindow(); 'ok' }" % _find_ps(name))
    return {"ok": True} if out.strip().endswith("ok") else {"ok": False, "error": "%r is not running" % name}


def foreground_pid():
    """PID of the current foreground window (to prove an action didn't steal focus)."""
    try:
        from . import native_input as ni
        hwnd = int(ni._user32().GetForegroundWindow() or 0)
        row = ni.find_window(hwnd=hwnd)
        return int((row or {}).get("pid") or 0)
    except Exception:
        return _run("foreground").get("pid", 0)


def _target(match="", pid=0, hwnd=0):
    """Resolve a window selector to one stable HWND plus metadata."""
    try:
        from . import native_input as ni
        row = ni.find_window(match=match, pid=pid, hwnd=hwnd)
    except Exception as exc:
        return None, "window discovery failed: %s" % exc
    if row:
        return row, ""
    return None, "window not found (match=%r pid=%s hwnd=%s)" % (match, pid, hwnd)


def tree(match="", pid=0, hwnd=0, max=60):
    """Accessibility tree of a window (by name substring or pid): capped list of elements with
    index / type / name / automationId / value / patterns / rect."""
    row, err = _target(match, pid, hwnd)
    if not row:
        return {"ok": False, "error": err}
    out = _run("tree", hwnd=row["hwnd"], max=max)
    if out.get("ok"):
        out["window_info"] = row
    return out


def invoke(match="", pid=0, hwnd=0, index=-1, aid="", name="", control_type="",
           occurrence=0, operation="activate", native_fallback=True,
           keyboard_fallback=True, coordinate_fallback=True):
    """Operate a control through UIA -> Win32 -> keyboard -> mouse, in that order."""
    row, err = _target(match, pid, hwnd)
    if not row:
        return {"ok": False, "error": err}
    out = _run("invoke", hwnd=row["hwnd"], index=index, aid=aid, name=name,
               control_type=control_type, occurrence=occurrence, operation=operation)
    if out.get("ok") or not out.get("needs_coordinate"):
        return out
    target = out.get("target") or {}
    native_hwnd = int(target.get("native_hwnd") or 0)
    activation = str(operation or "activate").lower() in (
        "activate", "invoke", "toggle", "select")
    attempts = []
    if native_fallback and native_hwnd and activation:
        try:
            from . import native_input as ni
            native = ni.control_action(native_hwnd, "click")
            if native.get("ok"):
                native.update({"target": target, "fallback": True})
                return native
            attempts.append(native.get("error", "Win32 control action failed"))
        except Exception as exc:
            attempts.append("Win32 control action failed: %s" % exc)
    if keyboard_fallback and activation and target.get("focusable"):
        fg = focus(hwnd=row["hwnd"])
        if fg.get("ok"):
            focused = _run("invoke", hwnd=row["hwnd"], index=index, aid=aid, name=name,
                           control_type=control_type, occurrence=occurrence, operation="focus")
            if focused.get("ok"):
                try:
                    from . import native_input as ni
                    key = "enter" if str(target.get("type") or "").lower() in (
                        "hyperlink", "menuitem") else "space"
                    pressed = ni.press(key)
                    return {"ok": True, "action": "keyboard_activate",
                            "method": "keyboard.%s" % key, "layer": "keyboard",
                            "target": target, "input": pressed, "fallback": True}
                except Exception as exc:
                    attempts.append("keyboard activation failed: %s" % exc)
            else:
                attempts.append("UIA could not focus the target")
        else:
            attempts.append("keyboard fallback could not focus the window")
    if not coordinate_fallback:
        if attempts:
            out["fallback_errors"] = attempts
        return out
    rect = (target.get("rect") or {})
    if rect.get("w", 0) <= 0 or rect.get("h", 0) <= 0:
        return out
    fg = focus(hwnd=row["hwnd"])
    if not fg.get("ok"):
        out["error"] += "; coordinate fallback could not focus the window: " + fg.get("error", "")
        return out
    try:
        from . import native_input as ni
        hit = ni.click(rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2)
        return {"ok": True, "action": "coordinate_click", "method": "mouse.SendInput",
                "layer": "mouse", "target": target, "input": hit,
                "fallback": True, "prior_errors": attempts}
    except Exception as exc:
        return {"ok": False, "error": "coordinate fallback failed: %s" % exc,
                "target": out.get("target")}


def set_value(text, match="", pid=0, hwnd=0, index=-1, aid="", name="",
              control_type="", occurrence=0, mode="replace", native_fallback=True,
              keyboard_fallback=True, interval_ms=0, submit=False):
    """Write an editable control through UIA -> Win32 -> keyboard."""
    row, err = _target(match, pid, hwnd)
    if not row:
        return {"ok": False, "error": err}
    mode = str(mode or "replace").lower()
    if mode not in ("replace", "append"):
        return {"ok": False, "error": "mode must be replace or append"}
    selector = dict(hwnd=row["hwnd"], index=index, aid=aid, name=name,
                    control_type=control_type, occurrence=occurrence)
    if mode == "append":
        current = _run("gettext", max=1_000_000, **selector)
        if current.get("ok") and current.get("method") == "uia.ValuePattern":
            out = _run("setvalue", text=str(current.get("text") or "") + str(text or ""),
                       **selector)
        else:
            out = current if not current.get("ok") else {
                "ok": False, "needs_keyboard": True,
                "error": "target is not appendable through UIA", "target": current.get("target")}
    else:
        out = _run("setvalue", text=text, **selector)
    if out.get("ok"):
        if submit:
            try:
                from . import native_input as ni
                focus(hwnd=row["hwnd"]); ni.press("enter")
                out["submitted"] = True
            except Exception as exc:
                return {"ok": False, "error": "value landed but Enter failed: %s" % exc,
                        "partial": out}
        return out
    target = out.get("target") or {}
    native_hwnd = int(target.get("native_hwnd") or 0)
    if native_fallback and native_hwnd:
        try:
            from . import native_input as ni
            native_text = str(text or "")
            if mode == "append":
                before = ni.control_get_text(native_hwnd)
                if not before.get("ok"):
                    raise RuntimeError(before.get("error") or "WM_GETTEXT failed")
                native_text = str(before.get("text") or "") + native_text
            native = ni.control_set_text(native_hwnd, native_text)
            if native.get("ok"):
                native.update({"mode": mode, "fallback": True})
                if submit:
                    fg = focus(hwnd=row["hwnd"])
                    if not fg.get("ok"):
                        return {"ok": False, "error": "text landed but submit could not focus window",
                                "partial": native}
                    ni.press("enter"); native["submitted"] = True
                return native
        except Exception as exc:
            out["native_error"] = str(exc)
    if not keyboard_fallback or not out.get("needs_keyboard"):
        return out
    fg = focus(hwnd=row["hwnd"])
    if not fg.get("ok"):
        return {"ok": False, "error": "keyboard fallback could not focus window: %s" % fg.get("error", "")}
    try:
        from . import native_input as ni
        focused = _run("invoke", hwnd=row["hwnd"], index=index, aid=aid, name=name,
                       control_type=control_type, occurrence=occurrence, operation="focus")
        if not focused.get("ok"):
            return {"ok": False, "error": "target cannot receive keyboard focus: %s" % focused.get("error", "")}
        if mode == "replace":
            ni.press("a", modifiers=["ctrl"])
        typed = ni.type_text(text, interval_ms=interval_ms)
        if submit:
            ni.press("enter")
        return {"ok": True, "action": "keyboard_type", "mode": mode,
                "method": "keyboard.SendInput", "layer": "keyboard",
                "submitted": bool(submit), "input": typed, "fallback": True}
    except Exception as exc:
        return {"ok": False, "error": "keyboard fallback failed: %s" % exc}


def get_text(match="", pid=0, hwnd=0, index=-1, aid="", name="", control_type="",
             occurrence=0, native_fallback=True, max_chars=1_000_000):
    """Read through UIA Value/Text patterns, then WM_GETTEXT, then UIA Name."""
    row, err = _target(match, pid, hwnd)
    if not row:
        return {"ok": False, "error": err}
    out = _run("gettext", hwnd=row["hwnd"], index=index, aid=aid, name=name,
               control_type=control_type, occurrence=occurrence,
               max=max(1, min(1_000_000, int(max_chars or 0))))
    if out.get("ok") or not native_fallback or not out.get("needs_native"):
        return out
    target = out.get("target") or {}
    native_hwnd = int(target.get("native_hwnd") or 0)
    if native_hwnd:
        try:
            from . import native_input as ni
            native = ni.control_get_text(native_hwnd, max_chars=max_chars)
            if native.get("ok"):
                native["fallback"] = True
                return native
        except Exception as exc:
            out["native_error"] = str(exc)
    if out.get("uia_name") is not None:
        return {"ok": True, "text": out.get("uia_name", ""),
                "method": "uia.NameProperty", "layer": "uia",
                "native_error": out.get("native_error")}
    return out


_UIA_OPERATIONS = {
    "invoke", "toggle", "select", "add_to_selection", "remove_from_selection",
    "expand", "collapse", "scroll_into_view", "realize", "scroll",
    "set_scroll_percent", "move", "resize", "rotate", "dock", "set_view",
    "window_minimize", "window_maximize", "window_restore", "window_close",
    "wait_ready", "get_selection",
    "get_grid_item", "focus",
}


def uia_action(operation, match="", pid=0, hwnd=0, index=-1, aid="", name="",
               control_type="", occurrence=0, **options):
    """Run an explicit semantic UI Automation operation with no simulated-input fallback."""
    operation = str(operation or "").strip().lower()
    if operation not in _UIA_OPERATIONS:
        return {"ok": False, "error": "unsupported UIA operation %r" % operation,
                "supported": sorted(_UIA_OPERATIONS)}
    window, err = _target(match, pid, hwnd)
    if not window:
        return {"ok": False, "error": err}
    allowed = {
        "text", "number", "horizontal", "vertical", "x", "y", "width",
        "height", "degrees", "row", "column", "view_id", "horizontal_amount",
        "vertical_amount", "dock",
    }
    forwarded = {key: value for key, value in options.items()
                 if key in allowed and value is not None}
    return _run("invoke", hwnd=window["hwnd"], index=index, aid=aid, name=name,
                control_type=control_type, occurrence=occurrence,
                operation=operation, **forwarded)


def win32_action(operation, match="", pid=0, hwnd=0, index=-1, aid="", name="",
                 control_type="", occurrence=0, text="", selected_index=-1,
                 checked=None, timeout_ms=1500):
    """Operate the selected classic control through a bounded Win32 message."""
    window, err = _target(match, pid, hwnd)
    if not window:
        return {"ok": False, "error": err}
    picked = _run("element", hwnd=window["hwnd"], index=index, aid=aid, name=name,
                  control_type=control_type, occurrence=occurrence)
    if not picked.get("ok"):
        return picked
    target = picked.get("element") or {}
    control_hwnd = int(target.get("native_hwnd") or 0)
    if not control_hwnd:
        return {"ok": False, "error": "selected UIA element has no native HWND",
                "target": target}
    try:
        from . import native_input as ni
        out = ni.control_action(control_hwnd, operation, text=text,
                                index=selected_index, checked=checked,
                                timeout_ms=timeout_ms)
        out["target"] = target
        return out
    except Exception as exc:
        return {"ok": False, "error": str(exc), "target": target,
                "layer": "win32"}


def set_range(value, match="", pid=0, hwnd=0, index=-1, aid="", name="",
              control_type="", occurrence=0):
    row, err = _target(match, pid, hwnd)
    if not row:
        return {"ok": False, "error": err}
    return _run("setrange", hwnd=row["hwnd"], index=index, aid=aid, name=name,
                control_type=control_type, occurrence=occurrence, number=value)


def close_window(match="", pid=0, hwnd=0):
    """Close ONE window via WindowPattern.Close — never Stop-Process (won't take sibling windows)."""
    row, err = _target(match, pid, hwnd)
    if not row:
        return {"ok": False, "error": err}
    out = _run("close", hwnd=row["hwnd"])
    if out.get("ok"):
        return out
    try:
        from . import native_input as ni
        fallback = ni.window_state("close", hwnd=row["hwnd"])
        if fallback.get("ok"):
            fallback["fallback"] = "WM_CLOSE"
            return fallback
    except Exception:
        pass
    return out


def launch(target):
    """Start an app (path or shell target). Returns True on success. Window discovery is by name via
    windows()/tree() afterward (Win11 packaged apps run under a different pid than the launcher).

    Off Windows this defers to desktop.launch, which knows `open` and `xdg-open` — so a caller that
    reaches here on a Mac opens the app instead of silently returning False."""
    return launch_detail(target)[0]


def launch_detail(target):
    """(ok, reason) — launch, keeping why it failed. See desktop.launch_detail: a bare False forces
    every caller to report "could not launch", which names the outcome and hides the cause."""
    if not target:
        return False, "no target given"
    if not plat.is_windows():
        from . import desktop
        return desktop.launch_detail(target)
    try:
        os.startfile(target)  # noqa: S606 - launching a user app is the point
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def _selector(args):
    args = args or {}
    return ((args.get("match") or args.get("window") or "").strip(),
            int(args.get("pid", 0) or 0), int(args.get("hwnd", 0) or 0))


def _element_args(args):
    args = args or {}
    return {
        "index": int(args["index"]) if args.get("index") not in (None, "") else -1,
        "aid": str(args.get("aid") or ""), "name": str(args.get("name") or ""),
        "control_type": str(args.get("control_type") or ""),
        "occurrence": int(args.get("occurrence", 0) or 0),
    }


def _screen_point(args, prefix=""):
    """Convert screen/window/screenshot-relative coordinates to real screen pixels."""
    args = args or {}
    xk, yk = prefix + "x", prefix + "y"
    if args.get(xk) is None or args.get(yk) is None:
        return None, "both %s and %s are required" % (xk, yk)
    try:
        x, y = float(args[xk]), float(args[yk])
    except (TypeError, ValueError):
        return None, "coordinates must be numbers"
    space = str(args.get("space") or "window").lower()
    if space == "screen":
        return (x, y), ""
    if space != "window":
        return None, "space must be window or screen"
    match, pid, hwnd = _selector(args)
    row, err = _target(match, pid, hwnd)
    if not row:
        return None, err
    rect = row.get("rect") or {}
    iw = args.get(prefix + "image_width") or args.get("image_width")
    ih = args.get(prefix + "image_height") or args.get("image_height")
    try:
        if iw:
            x *= float(rect.get("w", 0)) / float(iw)
        if ih:
            y *= float(rect.get("h", 0)) / float(ih)
    except (TypeError, ValueError, ZeroDivisionError):
        return None, "image_width/image_height must be positive numbers"
    return (float(rect.get("x", 0)) + x, float(rect.get("y", 0)) + y), ""


def mouse_action(args):
    """Execute one low-level mouse action, optionally bound to a target window."""
    from . import native_input as ni
    args = args or {}
    action = str(args.get("action") or "click").lower()
    if action == "release_all":
        return ni.release_all()
    match, pid, hwnd = _selector(args)
    if args.get("focus", True) and (match or pid or hwnd) and action not in ("move", "up"):
        fg = focus(match, pid, hwnd)
        if not fg.get("ok"):
            return fg
    if action == "scroll":
        if args.get("x") is not None:
            point, err = _screen_point(args)
            if not point:
                return {"ok": False, "error": err}
            ni.move(*point)
        return ni.scroll(int(args.get("delta", -120) or 0),
                         horizontal=bool(args.get("horizontal")))
    if action in ("down", "up") and args.get("x") is None:
        return ni.mouse_button(args.get("button", "left"), action=action)
    point, err = _screen_point(args)
    if not point:
        return {"ok": False, "error": err}
    if action == "move":
        return ni.move(*point, duration_ms=int(args.get("duration_ms", 0) or 0),
                       steps=int(args.get("steps", 0) or 0))
    if action in ("click", "double_click"):
        count = 2 if action == "double_click" else int(args.get("count", 1) or 1)
        return ni.click(*point, button=args.get("button", "left"), count=count,
                        interval_ms=int(args.get("interval_ms", 80) or 0))
    if action in ("down", "up"):
        ni.move(*point)
        return ni.mouse_button(args.get("button", "left"), action=action)
    return {"ok": False, "error": "unknown mouse action %r" % action}


def drag_action(args):
    from . import native_input as ni
    args = args or {}
    match, pid, hwnd = _selector(args)
    if match or pid or hwnd:
        fg = focus(match, pid, hwnd)
        if not fg.get("ok"):
            return fg
    start, err = _screen_point(args, "from_")
    if not start:
        return {"ok": False, "error": err}
    end, err = _screen_point(args, "to_")
    if not end:
        return {"ok": False, "error": err}
    try:
        return ni.drag(*start, *end, button=args.get("button", "left"),
                       duration_ms=int(args.get("duration_ms", 350) or 350),
                       steps=int(args.get("steps", 0) or 0))
    except Exception as exc:
        ni.release_all()
        return {"ok": False, "error": str(exc)}


def key_action(args):
    from . import native_input as ni
    args = args or {}
    action = str(args.get("action") or "press").lower()
    if action == "release_all":
        return ni.release_all()
    match, pid, hwnd = _selector(args)
    if match or pid or hwnd:
        fg = focus(match, pid, hwnd)
        if not fg.get("ok"):
            return fg
    try:
        if action == "type":
            return ni.type_text(str(args.get("text") or ""),
                                interval_ms=int(args.get("interval_ms", 0) or 0))
        if action == "down":
            return ni.key_down(args.get("key"))
        if action == "up":
            return ni.key_up(args.get("key"))
        if action == "press":
            return ni.press(args.get("key"), modifiers=args.get("modifiers") or [],
                            repeat=int(args.get("repeat", 1) or 1),
                            hold_ms=int(args.get("hold_ms", 35) or 0))
        return {"ok": False, "error": "unknown key action %r" % action}
    except Exception as exc:
        ni.release_all()
        return {"ok": False, "error": str(exc)}


def wait_for(args):
    """Poll for a window/control/text postcondition without asking the model to guess sleeps."""
    args = args or {}
    kind = str(args.get("kind") or "element").lower()
    state = str(args.get("state") or "exists").lower()
    if state not in ("exists", "missing"):
        return {"ok": False, "error": "state must be exists or missing"}
    timeout_ms = max(0, min(120000, int(args.get("timeout_ms", 10000) or 0)))
    interval = max(100, min(5000, int(args.get("interval_ms", 300) or 300))) / 1000.0
    deadline = __import__("time").monotonic() + timeout_ms / 1000.0
    last = None
    while True:
        match, pid, hwnd = _selector(args)
        if kind == "window":
            row, err = _target(match, pid, hwnd)
            present = row is not None
            last = row or {"error": err}
        elif kind in ("element", "text"):
            picked = get_text(match=match, pid=pid, hwnd=hwnd, **_element_args(args))
            present = bool(picked.get("ok"))
            last = picked
            contains = args.get("contains")
            if present and contains is not None:
                present = str(contains).lower() in str(picked.get("text", "")).lower()
        else:
            return {"ok": False, "error": "kind must be window, element, or text"}
        if present == (state == "exists"):
            return {"ok": True, "kind": kind, "state": state, "result": last}
        if __import__("time").monotonic() >= deadline:
            return {"ok": False, "error": "timed out waiting for %s to be %s" % (kind, state),
                    "last": last}
        __import__("time").sleep(interval)


def run_script(steps, defaults=None):
    """Run a bounded desktop sequence locally and stop at the first failed postcondition."""
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "error": "steps must be a non-empty list"}
    if len(steps) > 100:
        return {"ok": False, "error": "at most 100 desktop steps are allowed"}
    defaults = defaults or {}
    results = []
    had_errors = False
    for number, raw in enumerate(steps, 1):
        if not isinstance(raw, dict):
            return {"ok": False, "error": "step %d is not an object" % number,
                    "stopped_at": number, "results": results}
        # A script carrying a standing rule for one app must not smuggle a different
        # window (or arbitrary screen coordinates) into an inner step.
        for key in ("match", "pid", "hwnd"):
            if key in defaults and raw.get(key) not in (None, "", defaults[key]):
                return {"ok": False, "error": "step %d overrides bound %s" % (number, key),
                        "stopped_at": number, "results": results}
        if defaults and raw.get("space") == "screen":
            return {"ok": False, "error": "step %d escapes the script's bound window" % number,
                    "stopped_at": number, "results": results}
        args = dict(defaults); args.update(raw)
        action = str(args.get("action") or "").lower()
        try:
            if action == "inspect":
                match, pid, hwnd = _selector(args)
                out = tree(match, pid, hwnd, max=int(args.get("max", 60) or 60))
            elif action == "click":
                match, pid, hwnd = _selector(args)
                out = invoke(match, pid, hwnd, operation=args.get("operation", "activate"),
                             coordinate_fallback=bool(args.get("coordinate_fallback", True)),
                             **_element_args(args))
            elif action == "type":
                match, pid, hwnd = _selector(args)
                out = set_value(str(args.get("text") or ""), match, pid, hwnd,
                                mode=args.get("mode", "replace"),
                                keyboard_fallback=bool(args.get("keyboard_fallback", True)),
                                interval_ms=int(args.get("interval_ms", 0) or 0),
                                submit=bool(args.get("submit")), **_element_args(args))
            elif action == "read":
                match, pid, hwnd = _selector(args)
                out = get_text(match, pid, hwnd, **_element_args(args))
            elif action == "range":
                match, pid, hwnd = _selector(args)
                out = set_range(float(args.get("value", 0)), match, pid, hwnd, **_element_args(args))
            elif action == "uia":
                match, pid, hwnd = _selector(args)
                options = {key: args.get(key) for key in (
                    "text", "number", "horizontal", "vertical", "x", "y", "width",
                    "height", "degrees", "row", "column", "view_id",
                    "horizontal_amount", "vertical_amount", "dock")
                    if args.get(key) is not None}
                out = uia_action(args.get("operation"), match, pid, hwnd,
                                 **_element_args(args), **options)
            elif action == "win32":
                match, pid, hwnd = _selector(args)
                out = win32_action(args.get("operation"), match, pid, hwnd,
                                   text=str(args.get("text") or ""),
                                   selected_index=int(args.get("selected_index", -1)),
                                   checked=args.get("checked"),
                                   timeout_ms=int(args.get("timeout_ms", 1500) or 1500),
                                   **_element_args(args))
            elif action == "key":
                out = key_action(args)
            elif action == "mouse":
                out = mouse_action(args)
            elif action == "drag":
                out = drag_action(args)
            elif action == "wait_for":
                out = wait_for(args)
            elif action == "wait":
                ms = max(0, min(30000, int(args.get("ms", 250) or 0)))
                __import__("time").sleep(ms / 1000.0)
                out = {"ok": True, "waited_ms": ms}
            elif action == "focus":
                match, pid, hwnd = _selector(args); out = focus(match, pid, hwnd)
            elif action == "release_all":
                from . import native_input as ni
                out = ni.release_all()
            else:
                out = {"ok": False, "error": "unknown desktop script action %r" % action}
        except Exception as exc:
            out = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        # Typed text is deliberately not reflected into results/audit previews.
        summary = {"step": number, "action": action, "ok": bool(out.get("ok"))}
        for key in ("error", "action", "method", "layer", "text", "value", "readback",
                    "title", "hwnd", "fallback", "submitted", "waited_ms"):
            if key in out and not (raw.get("action") == "type" and key == "text"):
                summary[key] = out[key]
        results.append(summary)
        if not out.get("ok"):
            had_errors = True
        if not out.get("ok") and str(args.get("on_error") or "stop").lower() != "continue":
            try:
                from . import native_input as ni
                ni.release_all()
            except Exception:
                pass
            return {"ok": False, "error": out.get("error", "step failed"),
                    "stopped_at": number, "ran": number, "of": len(steps), "results": results}
    try:
        from . import native_input as ni
        ni.release_all()
    except Exception:
        pass
    return {"ok": not had_errors, "error": "one or more continued steps failed" if had_errors else "",
            "ran": len(steps), "of": len(steps), "results": results}


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
        if d.get("needs_native"):
            e += " — no UIA pattern; a compatible classic control may support desktop_win32"
        if d.get("needs_coordinate"):
            e += " — use coordinate_fallback or desktop_mouse with the live target rectangle"
        if d.get("needs_keyboard"):
            e += " — enable keyboard_fallback or use desktop_key after focusing the field"
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
        if e.get("native_hwnd"):
            parts.append("native_hwnd=%s" % e.get("native_hwnd"))
        if val not in (None, ""):
            parts.append("value=%s" % str(val)[:50])
        if pats:
            parts.append("<%s>" % pats)
        if e.get("state"):
            parts.append("state=%s" % json.dumps(e.get("state"), ensure_ascii=False,
                                                   separators=(",", ":")))
        rect = e.get("rect") or {}
        if rect.get("w", 0) > 0 and rect.get("h", 0) > 0:
            parts.append("rect=%s,%s %sx%s" % (
                rect.get("x", 0), rect.get("y", 0), rect.get("w", 0), rect.get("h", 0)))
        if e.get("offscreen"):
            parts.append("offscreen")
        if e.get("focused"):
            parts.append("focused")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _dc_enabled():
    """The 'Control desktop apps' setting (COLLIE_DESKTOP_CONTROL), read live at call time."""
    return os.environ.get("COLLIE_DESKTOP_CONTROL", "").lower() in ("1", "on", "true")


# What a desktop_* tool returns when it's called while the capability is off. It does NOT fail hard —
# it tells collie to get the user's consent, then flip it on via enable_capability. So collie always
# SEES the desktop hand (the tools are always registered) and can reach for it the moment it's needed.
_DC_CONSENT = (
    "⛔ Desktop control is currently OFF. This is a powerful capability — it lets me drive ANY native "
    "app window on this machine (inspect controls; use UIA/MSAA/Win32 semantics plus keyboard/mouse "
    "fallbacks; click, drag, scroll and hold keys; manage windows and clipboard; run bounded scripts, including "
    "system dialogs and custom-rendered apps). I won't turn it on silently. Ask the user in plain "
    "words whether to enable it and what "
    "it grants; if they agree, call enable_capability with capability=\"desktop_control\", then retry "
    "this action. If they decline, tell them this step can't be done without it.")


def _register_gated(registry, tools):
    """Register the desktop_* tools ALWAYS, so collie can see the capability exists. When the setting
    is off they ride the deferred tier (advertised by name, lean prompt) and refuse to run until the
    user consents; when on, they're always-on and run normally. The gate is re-checked at call time,
    so enable_capability takes effect for the rest of the session with no re-registration."""
    on = _dc_enabled()
    for t in tools:
        t.tier = "always" if on else "deferred"
        _orig = t.run

        def gated(args, ctx, _orig=_orig):
            if not _dc_enabled():
                return _DC_CONSENT
            return _orig(args, ctx)

        t.run = gated
        registry.register(t)


def _register_windows(registry):
    """Register the complete Windows desktop hand: UIA first, real input as fallback."""
    from .tools import Tool

    window_props = {
        "match": {"type": "string", "description": "window title/process/class substring"},
        "pid": {"type": "integer"}, "hwnd": {"type": "integer"},
    }
    element_props = dict(window_props, **{
        "index": {"type": "integer"}, "aid": {"type": "string"},
        "name": {"type": "string"}, "control_type": {"type": "string"},
        "occurrence": {"type": "integer"},
    })

    class DesktopApps(Tool):
        name, tier = "desktop_apps", "always"
        description = ("List every visible Win32 top-level window with its stable hwnd, process, pid, "
                       "title, class and screen rectangle. This uses EnumWindows, so it still finds "
                       "custom-rendered apps that are absent from the UI Automation root. No args.")
        schema = {"type": "object", "properties": {}}

        def run(self, args, ctx):
            d = windows(); err = _dt_err(d)
            if err:
                return err
            lines = []
            for row in d.get("windows", []):
                r = row.get("rect") or {}
                lines.append('[%s] %s pid=%s title="%s" class=%s rect=%s,%s %sx%s%s' % (
                    row.get("hwnd"), row.get("process") or "?", row.get("pid"),
                    (row.get("title") or "")[:90], row.get("class") or "?", r.get("x", 0),
                    r.get("y", 0), r.get("w", 0), r.get("h", 0),
                    " minimized" if row.get("minimized") else ""))
            return _dt_fence("\n".join(lines) if lines else "(no visible windows)")

    class DesktopInspect(Tool):
        name, tier = "desktop_inspect", "always"
        description = ("Snapshot a native window's UI Automation descendants. Each entry includes "
                       "index, type, name, automationId, value, bounding rectangle and supported "
                       "patterns, native HWND, current state, grid/range/window metadata. Covers "
                       "Invoke/Value/Text/Toggle/Selection/Expand/Scroll/Grid/Table/Window/Transform/"
                       "Dock/MultipleView/VirtualizedItem. Select the window "
                       "by match, pid, or preferably hwnd from desktop_apps. Args: match|pid|hwnd; max.")
        schema = {"type": "object", "properties": dict(window_props, **{"max": {"type": "integer"}})}

        def run(self, args, ctx):
            match, pid, hwnd = _selector(args)
            d = tree(match, pid, hwnd, max=int(args.get("max", 100) or 100))
            err = _dt_err(d)
            return err if err else _dt_fence(_dt_elements(d))

    class DesktopClick(Tool):
        name, tier = "desktop_click", "always"
        description = ("Operate one exact control. `activate` automatically uses the control's best "
                       "UIA pattern: Invoke, Toggle, SelectionItem, or ExpandCollapse. If UIA is "
                       "missing it tries a native Win32 Button message, then keyboard activation, "
                       "and only then a mouse click. "
                       "Explicit operation may be invoke/toggle/select/expand/collapse/scroll_into_view/"
                       "focus. If no semantic action exists, coordinate_fallback=true clicks the "
                       "control's live bounding-box centre in the foreground. Target by hwnd + "
                       "index/aid/name/control_type. Re-inspect after UI changes.")
        schema = {"type": "object", "properties": dict(element_props, **{
            "operation": {"type": "string"}, "native_fallback": {"type": "boolean"},
            "keyboard_fallback": {"type": "boolean"},
            "coordinate_fallback": {"type": "boolean"}})}

        def run(self, args, ctx):
            match, pid, hwnd = _selector(args)
            d = invoke(match, pid, hwnd, operation=args.get("operation", "activate"),
                       native_fallback=args.get("native_fallback", True),
                       keyboard_fallback=args.get("keyboard_fallback", True),
                       coordinate_fallback=args.get("coordinate_fallback", True), **_element_args(args))
            err = _dt_err(d)
            return err if err else "ok — %s via %s" % (
                d.get("action", "activated"), d.get("method", d.get("layer", "unknown")))

    class DesktopType(Tool):
        name, tier = "desktop_type", "always"
        description = ("Write text into an exact editable control. Priority: background UIA "
                       "ValuePattern, native Win32 WM_SETTEXT, then focused Unicode keyboard input. "
                       "Args: text, match|pid|hwnd, element selector.")
        schema = {"type": "object", "properties": dict(element_props, **{
            "text": {"type": "string"}, "mode": {"type": "string"},
            "native_fallback": {"type": "boolean"},
            "keyboard_fallback": {"type": "boolean"}, "interval_ms": {"type": "integer"},
            "submit": {"type": "boolean"}}), "required": ["text"]}

        def run(self, args, ctx):
            match, pid, hwnd = _selector(args)
            d = set_value(args.get("text", ""), match, pid, hwnd,
                          mode=args.get("mode", "replace"),
                          native_fallback=args.get("native_fallback", True),
                          keyboard_fallback=args.get("keyboard_fallback", True),
                          interval_ms=int(args.get("interval_ms", 0) or 0),
                          submit=bool(args.get("submit")), **_element_args(args))
            err = _dt_err(d)
            return err if err else "ok — %s via %s%s" % (
                d.get("action", "set"), d.get("method", d.get("layer", "unknown")),
                " and submitted" if d.get("submitted") else "")

    class DesktopRead(Tool):
        name, tier = "desktop_read", "always"
        description = ("Read a control's current text/value by UIA Value/Text, then native "
                       "WM_GETTEXT, then its accessibility name. Select by hwnd plus index/aid/name/type. Use "
                       "after an action to verify the expected postcondition rather than trusting a click.")
        schema = {"type": "object", "properties": dict(element_props, **{
            "native_fallback": {"type": "boolean"}, "max_chars": {"type": "integer"}})}

        def run(self, args, ctx):
            match, pid, hwnd = _selector(args)
            d = get_text(match, pid, hwnd,
                         native_fallback=args.get("native_fallback", True),
                         max_chars=int(args.get("max_chars", 1_000_000) or 1_000_000),
                         **_element_args(args)); err = _dt_err(d)
            return err if err else _dt_fence("method=%s\n%s" % (
                d.get("method", d.get("layer", "unknown")),
                str(d.get("text", d.get("value", "")))))

    class DesktopRange(Tool):
        name, tier = "desktop_range", "always"
        description = ("Set a slider/spinner/progress-style control through UIA RangeValuePattern. "
                       "Args: value plus window and element selector.")
        schema = {"type": "object", "properties": dict(element_props, **{"value": {"type": "number"}}),
                  "required": ["value"]}

        def run(self, args, ctx):
            match, pid, hwnd = _selector(args)
            d = set_range(args.get("value", 0), match, pid, hwnd, **_element_args(args)); err = _dt_err(d)
            return err if err else "ok — range=%s via %s" % (
                d.get("value"), d.get("method", "uia.RangeValuePattern"))

    class DesktopUIA(Tool):
        name, tier = "desktop_uia", "always"
        description = ("Run an explicit semantic UI Automation operation with no input simulation. "
                       "Operations: invoke, toggle, select/add_to_selection/remove_from_selection, "
                       "expand/collapse, scroll_into_view/realize, scroll/set_scroll_percent, "
                       "move/resize/rotate/dock, set_view, window_minimize/window_maximize/"
                       "window_restore/window_close/wait_ready, get_selection, get_grid_item, focus. "
                       "Extra args depend on "
                       "operation: text/number, horizontal/vertical percentages, horizontal_amount/"
                       "vertical_amount, x/y, width/height, degrees, dock, view_id, row/column.")
        schema = {"type": "object", "properties": dict(element_props, **{
            "operation": {"type": "string"}, "text": {"type": "string"},
            "number": {"type": "number"}, "horizontal": {"type": "number"},
            "vertical": {"type": "number"}, "horizontal_amount": {"type": "string"},
            "vertical_amount": {"type": "string"}, "x": {"type": "number"},
            "y": {"type": "number"}, "width": {"type": "number"},
            "height": {"type": "number"}, "degrees": {"type": "number"},
            "row": {"type": "integer"}, "column": {"type": "integer"},
            "view_id": {"type": "integer"}, "dock": {"type": "string"}}),
                  "required": ["operation"]}

        def run(self, args, ctx):
            match, pid, hwnd = _selector(args)
            options = {key: args.get(key) for key in (
                "text", "number", "horizontal", "vertical", "x", "y", "width",
                "height", "degrees", "row", "column", "view_id", "horizontal_amount",
                "vertical_amount", "dock") if args.get(key) is not None}
            d = uia_action(args.get("operation"), match, pid, hwnd,
                           **_element_args(args), **options)
            return _dt_err(d) or json.dumps(d, ensure_ascii=False)

    class DesktopWin32(Tool):
        name, tier = "desktop_win32", "always"
        description = ("Operate a selected classic/managed Windows control through MSAA or "
                       "allow-listed Win32 messages, without keyboard or mouse. Operations: click/"
                       "invoke (MSAA first), message_click (BM_CLICK), get_text/set_text, get_check/"
                       "set_check, get_selected_index/set_selected_index, get_state/get_role/get_name/"
                       "get_value/get_default_action/do_default_action. The element must expose native_hwnd; "
                       "select it by parent window plus index/aid/name/control_type.")
        schema = {"type": "object", "properties": dict(element_props, **{
            "operation": {"type": "string"}, "text": {"type": "string"},
            "selected_index": {"type": "integer"},
            "checked": {"type": ["boolean", "string"]},
            "timeout_ms": {"type": "integer"}}), "required": ["operation"]}

        def run(self, args, ctx):
            match, pid, hwnd = _selector(args)
            d = win32_action(args.get("operation"), match, pid, hwnd,
                             text=args.get("text", ""),
                             selected_index=int(args.get("selected_index", -1)),
                             checked=args.get("checked"),
                             timeout_ms=int(args.get("timeout_ms", 1500) or 1500),
                             **_element_args(args))
            return _dt_err(d) or json.dumps(d, ensure_ascii=False)

    class DesktopMouse(Tool):
        name, tier = "desktop_mouse", "always"
        description = ("Real foreground mouse input for any custom-rendered app: move, click, "
                       "double_click, down, up, scroll, or release_all. Coordinates default to the "
                       "target WINDOW (match/pid/hwnd); space=screen uses virtual-screen pixels. If "
                       "coordinates came from a downscaled screenshot, pass image_width/image_height "
                       "and they are scaled back to the real window. Supports left/right/middle, "
                       "negative multi-monitor coordinates, wheel delta and smooth movement.")
        schema = {"type": "object", "properties": dict(window_props, **{
            "action": {"type": "string"}, "space": {"type": "string"},
            "x": {"type": "number"}, "y": {"type": "number"},
            "image_width": {"type": "number"}, "image_height": {"type": "number"},
            "button": {"type": "string"}, "count": {"type": "integer"},
            "delta": {"type": "integer"}, "horizontal": {"type": "boolean"},
            "duration_ms": {"type": "integer"}, "steps": {"type": "integer"},
            "focus": {"type": "boolean"}})}

        def run(self, args, ctx):
            try:
                d = mouse_action(args)
            except Exception as exc:
                d = {"ok": False, "error": str(exc)}
            return _dt_err(d) or json.dumps(d, ensure_ascii=False)

    class DesktopDrag(Tool):
        name, tier = "desktop_drag", "always"
        description = ("Drag between two real points in a window or on the screen with held mouse "
                       "input. Args: from_x/from_y, to_x/to_y, match|pid|hwnd or space=screen; "
                       "optional screenshot image dimensions, button, duration_ms, steps.")
        schema = {"type": "object", "properties": dict(window_props, **{
            "space": {"type": "string"}, "from_x": {"type": "number"},
            "from_y": {"type": "number"}, "to_x": {"type": "number"},
            "to_y": {"type": "number"}, "image_width": {"type": "number"},
            "image_height": {"type": "number"}, "button": {"type": "string"},
            "duration_ms": {"type": "integer"}, "steps": {"type": "integer"}}),
                  "required": ["from_x", "from_y", "to_x", "to_y"]}

        def run(self, args, ctx):
            d = drag_action(args)
            return _dt_err(d) or json.dumps(d, ensure_ascii=False)

    class DesktopKey(Tool):
        name, tier = "desktop_key", "always"
        description = ("Send real keyboard input to a foreground native window. Actions: press, "
                       "down, up, type, release_all. Supports named keys, Ctrl/Alt/Shift/Win "
                       "modifiers, repeat and hold_ms up to 30 seconds. `down` remains held until a "
                       "matching `up` or release_all; failures automatically release held inputs.")
        schema = {"type": "object", "properties": dict(window_props, **{
            "action": {"type": "string"}, "key": {"type": ["string", "integer"]},
            "modifiers": {"type": "array", "items": {"type": "string"}},
            "repeat": {"type": "integer"}, "hold_ms": {"type": "integer"},
            "text": {"type": "string"}, "interval_ms": {"type": "integer"}})}

        def run(self, args, ctx):
            d = key_action(args)
            return _dt_err(d) or json.dumps(d, ensure_ascii=False)

    class DesktopWindow(Tool):
        name, tier = "desktop_window", "always"
        description = ("Manage one exact top-level window: focus, show, hide, minimize, maximize, "
                       "restore, or gracefully request close. Select by match/pid/hwnd; closing posts "
                       "WM_CLOSE and never kills the process.")
        schema = {"type": "object", "properties": dict(window_props, **{"action": {"type": "string"}}),
                  "required": ["action"]}

        def run(self, args, ctx):
            from . import native_input as ni
            match, pid, hwnd = _selector(args)
            d = ni.window_state(args.get("action"), match, pid, hwnd)
            return _dt_err(d) or json.dumps(d, ensure_ascii=False)

    class DesktopClipboard(Tool):
        name, tier = "desktop_clipboard", "always"
        description = ("Get or set Unicode text in the Windows clipboard. Reading may expose "
                       "sensitive clipboard contents, so this remains behind desktop control and "
                       "external-action approval. Args: action=get|set; text for set; max_chars for get.")
        schema = {"type": "object", "properties": {
            "action": {"type": "string"}, "text": {"type": "string"},
            "max_chars": {"type": "integer"}}, "required": ["action"]}

        def run(self, args, ctx):
            from . import native_input as ni
            d = (ni.clipboard_get(int(args.get("max_chars", 100000) or 100000))
                 if args.get("action") == "get" else ni.clipboard_set(args.get("text", "")))
            err = _dt_err(d)
            if err:
                return err
            return _dt_fence(d.get("text", "")) if args.get("action") == "get" else "ok — clipboard set"

    class DesktopWait(Tool):
        name, tier = "desktop_wait", "always"
        description = ("Wait for a native window/control/text postcondition instead of guessing a "
                       "sleep. kind=window|element|text; state=exists|missing; optional contains, "
                       "timeout_ms (max 120000), interval_ms, and normal selectors.")
        schema = {"type": "object", "properties": dict(element_props, **{
            "kind": {"type": "string"}, "state": {"type": "string"},
            "contains": {"type": "string"}, "timeout_ms": {"type": "integer"},
            "interval_ms": {"type": "integer"}})}

        def run(self, args, ctx):
            d = wait_for(args)
            return _dt_err(d) or json.dumps(d, ensure_ascii=False)

    class DesktopScript(Tool):
        name, tier = "desktop_script", "always"
        description = ("Run up to 100 bounded desktop steps in one local call, eliminating model "
                       "round trips between known actions. Root match/pid/hwnd is inherited by every "
                       "step. Step actions: inspect, click, type, read, range, uia, win32, key, mouse, drag, "
                       "wait_for, wait, focus, release_all. Stops on the first failure unless that "
                       "step sets on_error=continue, and always releases held keys/buttons on failure. "
                       "Use postcondition wait_for/read steps around irreversible actions.")
        schema = {"type": "object", "properties": dict(window_props, **{
            "steps": {"type": "array", "items": {"type": "object"}}}), "required": ["steps"]}

        def run(self, args, ctx):
            defaults = {k: args[k] for k in ("match", "pid", "hwnd") if args.get(k) not in (None, "")}
            d = run_script(args.get("steps"), defaults=defaults)
            return ("ERROR(desktop): %s\n%s" % (d.get("error"), json.dumps(d, ensure_ascii=False))
                    if not d.get("ok") else json.dumps(d, ensure_ascii=False))

    class DesktopLaunch(Tool):
        name, tier = "desktop_launch", "always"
        description = ("Start a native app by executable, shortcut, document, URI, or full path. "
                       "Afterwards use desktop_wait(kind=window) then desktop_apps/inspect. Args: target.")
        schema = {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}

        def run(self, args, ctx):
            ok, why = launch_detail(args.get("target", ""))
            return "ok — launched" if ok else "ERROR(desktop): could not launch %r — %s" % (
                args.get("target", ""), why)

    class DesktopFocus(Tool):
        name, tier = "desktop_focus", "always"
        description = ("Bring one exact window to the foreground by match/pid/hwnd. Prefer hwnd "
                       "when an app owns multiple windows.")
        schema = {"type": "object", "properties": window_props}

        def run(self, args, ctx):
            match, pid, hwnd = _selector(args)
            d = focus(match, pid, hwnd); err = _dt_err(d)
            return err if err else "ok — focused hwnd=%s" % d.get("hwnd", hwnd)

    _register_gated(registry, [
        DesktopApps(), DesktopInspect(), DesktopClick(), DesktopType(), DesktopRead(),
        DesktopRange(), DesktopUIA(), DesktopWin32(), DesktopMouse(), DesktopDrag(), DesktopKey(), DesktopWindow(),
        DesktopClipboard(), DesktopWait(), DesktopScript(), DesktopLaunch(), DesktopFocus(),
    ])


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

    _register_gated(registry, [DesktopApps(), DesktopInspect(), DesktopClick(), DesktopType(),
                               DesktopMenu(), DesktopFocus(), DesktopLaunch()])


def register_native(registry):
    """Register the desktop_* app-control tools for THIS platform: Windows UI Automation (by index /
    automationId) or macOS System Events (by control name/label, plus desktop_menu). Same tool names
    on both, so the agent drives native apps the same way regardless of OS."""
    if plat.is_macos():
        _register_mac(registry)
    elif plat.is_windows():
        _register_windows(registry)
