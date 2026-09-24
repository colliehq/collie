"""Low-level Windows input for Collie's native desktop controls.

UI Automation is the preferred hand: it names the exact control and often works in
the background.  Classic Win32 controls are the next hand: bounded window messages
can click a button, read/write an edit, or select a list item without moving the
cursor or stealing focus.  Custom-rendered applications (games, chat clients,
canvases and remote desktops) frequently expose neither interface, so this module
also provides the deliberate foreground fallbacks: keyboard first, then real cursor
movement, mouse buttons, wheel events and key hold/release.

There are no third-party dependencies.  Calls use user32 ``SendInput`` and stay
subject to Windows' own integrity boundary (UIPI): a normal Collie process cannot
inject into an elevated app, the UAC secure desktop, an anti-cheat protected game,
or another login session.  A failed OS boundary is reported; it is never bypassed.
"""
from __future__ import annotations

PLATFORM = "windows"

import atexit
import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000

# Native control messages.  These are deliberately limited to the documented
# system-control surface.  We never send arbitrary caller-supplied message numbers.
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_COMMAND = 0x0111
BM_GETCHECK = 0x00F0
BM_SETCHECK = 0x00F1
BM_CLICK = 0x00F5
BST_UNCHECKED = 0
BST_CHECKED = 1
BST_INDETERMINATE = 2
CB_GETCURSEL = 0x0147
CB_SETCURSEL = 0x014E
LB_GETCURSEL = 0x0188
LB_SETCURSEL = 0x0186
SMTO_ABORTIFHUNG = 0x0002

_NOWIN = 0x08000000
_MSAA_PS = r'''
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)   # plat.PS_UTF8_OUTPUT
$ErrorActionPreference = "Stop"
$src = @"
using System;
using System.Runtime.InteropServices;
using Accessibility;
public static class _CollieMsaa {
  [DllImport("oleacc.dll")]
  static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint id, ref Guid iid,
    [MarshalAs(UnmanagedType.Interface)] out object value);
  static IAccessible Get(long hwnd) {
    object value; Guid iid = new Guid("618736e0-3c3d-11cf-810c-00aa00389b71");
    int hr = AccessibleObjectFromWindow(new IntPtr(hwnd), 0xFFFFFFFC, ref iid, out value);
    if (hr < 0) Marshal.ThrowExceptionForHR(hr);
    return (IAccessible)value;
  }
  public static int State(long hwnd) { return Convert.ToInt32(Get(hwnd).get_accState(0)); }
  public static int Role(long hwnd) { return Convert.ToInt32(Get(hwnd).get_accRole(0)); }
  public static string Name(long hwnd) { return Convert.ToString(Get(hwnd).get_accName(0)); }
  public static string Value(long hwnd) { return Convert.ToString(Get(hwnd).get_accValue(0)); }
  public static string DefaultAction(long hwnd) { return Convert.ToString(Get(hwnd).get_accDefaultAction(0)); }
  public static void DoDefaultAction(long hwnd) { Get(hwnd).accDoDefaultAction(0); }
  public static void SetValue(long hwnd, string value) { Get(hwnd).set_accValue(0, value); }
}
"@
try {
  Add-Type -TypeDefinition $src -ReferencedAssemblies Accessibility
  $h = [long]$env:COLLIE_MSAA_HWND
  $op = $env:COLLIE_MSAA_OPERATION
  switch ($op) {
    "get_state" { $s = [_CollieMsaa]::State($h); $out = @{ok=$true; state=$s; checked=(($s -band 0x10) -ne 0); mixed=(($s -band 0x20) -ne 0)} }
    "get_role" { $out = @{ok=$true; role=[_CollieMsaa]::Role($h)} }
    "get_name" { $out = @{ok=$true; name=[_CollieMsaa]::Name($h)} }
    "get_value" { $out = @{ok=$true; value=[_CollieMsaa]::Value($h)} }
    "get_default_action" { $out = @{ok=$true; default_action=[_CollieMsaa]::DefaultAction($h)} }
    "do_default_action" { [_CollieMsaa]::DoDefaultAction($h); $out = @{ok=$true; action="default_action"} }
    "set_value" { [_CollieMsaa]::SetValue($h, $env:COLLIE_MSAA_TEXT); $out = @{ok=$true; action="set_value"; value=[_CollieMsaa]::Value($h)} }
    default { $out = @{ok=$false; error="unsupported MSAA operation '$op'"} }
  }
} catch { $out = @{ok=$false; error=$_.Exception.Message} }
$out["method"] = "msaa.IAccessible"
$out["layer"] = "msaa"
$out["hwnd"] = [long]$env:COLLIE_MSAA_HWND
$out | ConvertTo-Json -Compress
'''


_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "pause": 0x13, "capslock": 0x14, "escape": 0x1B, "esc": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "arrowleft": 0x25, "up": 0x26,
    "arrowup": 0x26, "right": 0x27, "arrowright": 0x27,
    "down": 0x28, "arrowdown": 0x28, "printscreen": 0x2C,
    "insert": 0x2D, "delete": 0x2E, "win": 0x5B, "meta": 0x5B,
    "menu": 0x5D, "numlock": 0x90, "scrolllock": 0x91,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2,
    "media_play_pause": 0xB3,
}
for _n in range(1, 25):
    _VK["f%d" % _n] = 0x6F + _n

_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}
_HELD_KEYS: set[int] = set()
_HELD_BUTTONS: set[str] = set()


def available() -> tuple[bool, str]:
    if os.name != "nt":
        return False, "low-level desktop input is Windows-only"
    return True, ""


def _user32():
    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    return ctypes.windll.user32


_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", _ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", _ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


def _window_text(hwnd: int) -> str:
    u = _user32()
    n = u.GetWindowTextLengthW(wintypes.HWND(hwnd))
    buf = ctypes.create_unicode_buffer(max(2, n + 2))
    u.GetWindowTextW(wintypes.HWND(hwnd), buf, len(buf))
    return buf.value


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32().GetClassNameW(wintypes.HWND(hwnd), buf, len(buf))
    return buf.value


def _send_message_timeout(hwnd: int, message: int, wparam: int = 0,
                          lparam: int = 0, timeout_ms: int = 1500) -> int:
    """Send one allow-listed Win32 control message without risking an app hang."""
    hwnd = int(hwnd or 0)
    if not hwnd or not _user32().IsWindow(wintypes.HWND(hwnd)):
        raise RuntimeError("native control hwnd is not a live window")
    timeout_ms = max(50, min(10000, int(timeout_ms or 1500)))
    result = _ULONG_PTR()
    u = _user32()
    u.SendMessageTimeoutW.restype = wintypes.LPARAM
    ok = u.SendMessageTimeoutW(
        wintypes.HWND(hwnd), wintypes.UINT(int(message)),
        wintypes.WPARAM(int(wparam)), wintypes.LPARAM(int(lparam)),
        wintypes.UINT(SMTO_ABORTIFHUNG), wintypes.UINT(timeout_ms),
        ctypes.byref(result),
    )
    if not ok:
        err = ctypes.get_last_error()
        raise RuntimeError(
            "SendMessageTimeout failed or target hung (message=0x%04X, Windows error %d)"
            % (message, err))
    return int(result.value)


def control_get_text(hwnd: int, timeout_ms: int = 1500,
                     max_chars: int = 1_000_000) -> dict:
    """Read a classic HWND's text through WM_GETTEXT (no focus or input injection)."""
    n = _send_message_timeout(hwnd, WM_GETTEXTLENGTH, timeout_ms=timeout_ms)
    cap = max(1, min(int(max_chars or 0), n) + 1)
    buf = ctypes.create_unicode_buffer(cap)
    copied = _send_message_timeout(
        hwnd, WM_GETTEXT, cap, ctypes.cast(buf, ctypes.c_void_p).value or 0,
        timeout_ms=timeout_ms,
    )
    value = buf.value[:max(0, copied)]
    return {"ok": True, "action": "get_text", "method": "win32.WM_GETTEXT",
            "layer": "win32", "hwnd": int(hwnd), "text": value,
            "characters": len(value)}


def control_set_text(hwnd: int, text: str, timeout_ms: int = 1500) -> dict:
    """Replace a classic HWND's text through WM_SETTEXT and verify with WM_GETTEXT."""
    buf = ctypes.create_unicode_buffer(str(text or ""))
    accepted = _send_message_timeout(
        hwnd, WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p).value or 0,
        timeout_ms=timeout_ms,
    )
    if not accepted:
        return {"ok": False, "error": "control refused WM_SETTEXT",
                "method": "win32.WM_SETTEXT", "layer": "win32", "hwnd": int(hwnd)}
    readback = control_get_text(hwnd, timeout_ms=timeout_ms)
    if not readback.get("ok"):
        return readback
    return {"ok": True, "action": "set_text", "method": "win32.WM_SETTEXT",
            "layer": "win32", "hwnd": int(hwnd),
            "readback": readback.get("text", "")}


def msaa_action(hwnd: int, operation: str, text: str = "", timeout_ms: int = 3000) -> dict:
    """Use Windows' built-in IAccessible (MSAA) semantics for legacy/managed controls."""
    hwnd = int(hwnd or 0)
    if not hwnd or not _user32().IsWindow(wintypes.HWND(hwnd)):
        return {"ok": False, "error": "MSAA target hwnd is not a live window"}
    env = os.environ.copy()
    env["COLLIE_MSAA_HWND"] = str(hwnd)
    env["COLLIE_MSAA_OPERATION"] = str(operation or "")
    env["COLLIE_MSAA_TEXT"] = str(text or "")
    try:
        ran = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", _MSAA_PS],
            creationflags=_NOWIN, timeout=max(1, min(10, int(timeout_ms or 3000) / 1000 + 2)),
            capture_output=True, text=True, encoding="utf-8", errors="ignore", env=env,
        )
    except Exception as exc:
        return {"ok": False, "error": "MSAA driver failed: %s" % exc,
                "method": "msaa.IAccessible", "layer": "msaa", "hwnd": hwnd}
    raw = (ran.stdout or "").strip()
    if not raw:
        return {"ok": False, "error": (ran.stderr or "MSAA driver returned no output")[:400],
                "method": "msaa.IAccessible", "layer": "msaa", "hwnd": hwnd}
    try:
        return json.loads(raw)
    except Exception:
        return {"ok": False, "error": "bad MSAA driver output", "raw": raw[:400],
                "method": "msaa.IAccessible", "layer": "msaa", "hwnd": hwnd}


def control_action(hwnd: int, operation: str, text: str = "", index: int = -1,
                   checked=None, timeout_ms: int = 1500) -> dict:
    """Use a documented native operation on a classic system control.

    The control class is checked before class-specific numeric messages are sent,
    because (for example) BM_CLICK's value means something else to an Edit control.
    """
    hwnd = int(hwnd or 0)
    operation = str(operation or "").strip().lower()
    cls = _window_class(hwnd).lower()
    if operation in ("get_text", "read"):
        return control_get_text(hwnd, timeout_ms=timeout_ms)
    if operation in ("set_text", "write"):
        return control_set_text(hwnd, text, timeout_ms=timeout_ms)
    if operation in ("get_state", "get_role", "get_name", "get_value",
                     "get_default_action", "do_default_action"):
        return msaa_action(hwnd, operation, text=text, timeout_ms=timeout_ms)
    if operation in ("click", "invoke", "activate"):
        semantic = msaa_action(hwnd, "do_default_action", timeout_ms=timeout_ms)
        if semantic.get("ok"):
            semantic["action"] = "click"
            return semantic
        if "button" not in cls:
            return {"ok": False, "error": "BM_CLICK is only valid for a native Button control",
                    "class": cls, "hwnd": hwnd, "msaa_error": semantic.get("error")}
        _send_message_timeout(hwnd, BM_CLICK, timeout_ms=timeout_ms)
        return {"ok": True, "action": "click", "method": "win32.BM_CLICK",
                "layer": "win32", "hwnd": hwnd}
    if operation == "message_click":
        if "button" not in cls:
            return {"ok": False, "error": "BM_CLICK is only valid for a native Button control",
                    "class": cls, "hwnd": hwnd}
        _send_message_timeout(hwnd, BM_CLICK, timeout_ms=timeout_ms)
        return {"ok": True, "action": "click", "method": "win32.BM_CLICK",
                "layer": "win32", "hwnd": hwnd}
    if operation == "get_check":
        semantic = msaa_action(hwnd, "get_state", timeout_ms=timeout_ms)
        if semantic.get("ok"):
            semantic["action"] = "get_check"
            return semantic
        if "button" not in cls:
            return {"ok": False, "error": "BM_GETCHECK is only valid for a native Button control",
                    "class": cls, "hwnd": hwnd}
        state = _send_message_timeout(hwnd, BM_GETCHECK, timeout_ms=timeout_ms)
        return {"ok": True, "action": "get_check", "method": "win32.BM_GETCHECK",
                "layer": "win32", "hwnd": hwnd, "state": state,
                "checked": state == BST_CHECKED}
    if operation == "set_check":
        if "button" not in cls:
            return {"ok": False, "error": "BM_SETCHECK is only valid for a native Button control",
                    "class": cls, "hwnd": hwnd}
        state = (BST_INDETERMINATE if str(checked).lower() == "indeterminate" else
                 BST_CHECKED if bool(checked) else BST_UNCHECKED)
        semantic = msaa_action(hwnd, "get_state", timeout_ms=timeout_ms)
        if semantic.get("ok") and state in (BST_UNCHECKED, BST_CHECKED):
            wanted = state == BST_CHECKED
            if bool(semantic.get("checked")) != wanted:
                changed = msaa_action(hwnd, "do_default_action", timeout_ms=timeout_ms)
                if not changed.get("ok"):
                    return changed
                semantic = msaa_action(hwnd, "get_state", timeout_ms=timeout_ms)
            if semantic.get("ok") and bool(semantic.get("checked")) == wanted:
                semantic.update({"action": "set_check", "checked": wanted,
                                 "state": int(semantic.get("state", state))})
                return semantic
            return {"ok": False, "error": "MSAA default action did not enter requested state",
                    "method": "msaa.IAccessible", "layer": "msaa", "hwnd": hwnd,
                    "requested_state": state, "actual": semantic}
        before = _send_message_timeout(hwnd, BM_GETCHECK, timeout_ms=timeout_ms)
        _send_message_timeout(hwnd, BM_SETCHECK, state, timeout_ms=timeout_ms)
        actual = _send_message_timeout(hwnd, BM_GETCHECK, timeout_ms=timeout_ms)
        # Some managed Button wrappers ignore BM_SETCHECK but correctly implement
        # the native default action.  Toggle once only when it moves toward the
        # caller's requested binary state, then verify instead of claiming success.
        if actual != state and state in (BST_UNCHECKED, BST_CHECKED) and before != state:
            _send_message_timeout(hwnd, BM_CLICK, timeout_ms=timeout_ms)
            actual = _send_message_timeout(hwnd, BM_GETCHECK, timeout_ms=timeout_ms)
        if actual != state:
            return {"ok": False, "error": "control did not enter the requested check state",
                    "method": "win32.BM_SETCHECK", "layer": "win32", "hwnd": hwnd,
                    "requested_state": state, "actual_state": actual}
        return {"ok": True, "action": "set_check", "method": "win32.BM_SETCHECK",
                "layer": "win32", "hwnd": hwnd, "state": actual,
                "checked": actual == BST_CHECKED}
    if operation in ("get_selected_index", "set_selected_index"):
        combo = "combobox" in cls
        listing = "listbox" in cls
        if not (combo or listing):
            return {"ok": False, "error": "selection messages require ComboBox or ListBox",
                    "class": cls, "hwnd": hwnd}
        get_msg = CB_GETCURSEL if combo else LB_GETCURSEL
        set_msg = CB_SETCURSEL if combo else LB_SETCURSEL
        msg = get_msg if operation.startswith("get_") else set_msg
        selected = _send_message_timeout(hwnd, msg, int(index), timeout_ms=timeout_ms)
        # CB/LB_ERR is -1, represented as pointer-sized all-ones by LRESULT.
        if selected in (-1, (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1):
            return {"ok": False, "error": "native control rejected the selection index",
                    "class": cls, "hwnd": hwnd, "index": int(index)}
        if operation == "set_selected_index":
            # CB/LB_SETCURSEL updates the control but intentionally does not notify
            # its owner. Deliver the documented selection-change notification so
            # managed/native application logic observes the semantic action.
            parent = int(_user32().GetParent(wintypes.HWND(hwnd)) or 0)
            control_id = int(_user32().GetDlgCtrlID(wintypes.HWND(hwnd))) & 0xFFFF
            if parent:
                _send_message_timeout(parent, WM_COMMAND, control_id | (1 << 16), hwnd,
                                      timeout_ms=timeout_ms)
        return {"ok": True, "action": operation,
                "method": "win32.%s" % ("CB" if combo else "LB"),
                "layer": "win32", "hwnd": hwnd, "index": selected}
    return {"ok": False, "error": "unsupported native control operation %r" % operation,
            "class": cls, "hwnd": hwnd}


def _process_path(pid: int) -> str:
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel.CloseHandle(handle)


def window_rect(hwnd: int) -> dict:
    rect = _RECT()
    if not _user32().GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect failed for hwnd=%s" % hwnd)
    return {"x": int(rect.left), "y": int(rect.top),
            "w": int(rect.right - rect.left), "h": int(rect.bottom - rect.top)}


def windows(include_hidden: bool = False) -> list[dict]:
    """Enumerate real Win32 top-level windows instead of trusting UIA's root tree.

    Custom-rendered apps such as WeChat can have a valid HWND while being absent from
    ``AutomationElement.RootElement``.  Keeping HWND as the stable handle lets the UIA
    layer try ``AutomationElement.FromHandle`` and lets the pixel fallback act even if
    the app publishes no accessibility descendants at all.
    """
    u = _user32()
    try:
        u.SetProcessDPIAware()
    except Exception:
        pass
    rows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _lparam):
        h = int(hwnd)
        visible = bool(u.IsWindowVisible(hwnd))
        if not include_hidden and not visible:
            return True
        title = _window_text(h)
        cls = _window_class(h)
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            rect = window_rect(h)
        except Exception:
            rect = {"x": 0, "y": 0, "w": 0, "h": 0}
        # Tiny owner/tool windows are noise unless they have a useful title.
        if not title and (rect["w"] < 48 or rect["h"] < 48):
            return True
        path = _process_path(pid.value)
        rows.append({"hwnd": h, "title": title, "class": cls,
                     "pid": int(pid.value), "process": os.path.splitext(os.path.basename(path))[0],
                     "path": path, "visible": visible, "enabled": bool(u.IsWindowEnabled(hwnd)),
                     "minimized": bool(u.IsIconic(hwnd)), "rect": rect})
        return True

    cb = callback_type(visit)
    if not u.EnumWindows(cb, 0):
        raise RuntimeError("EnumWindows failed")
    fg = int(u.GetForegroundWindow() or 0)
    rows.sort(key=lambda row: (row["hwnd"] != fg, not row["visible"],
                               not bool(row["title"]), row["title"].lower()))
    return rows


def find_window(match: str = "", pid: int = 0, hwnd: int = 0,
                include_hidden: bool = False) -> dict | None:
    hwnd = int(hwnd or 0)
    rows = windows(include_hidden=include_hidden)
    if hwnd:
        return next((row for row in rows if row["hwnd"] == hwnd), None)
    pid = int(pid or 0)
    if pid:
        return next((row for row in rows if row["pid"] == pid), None)
    want = str(match or "").strip().lower()
    if not want:
        return None
    exact = [row for row in rows if want in (
        row["title"].lower(), row["process"].lower(), row["class"].lower())]
    if exact:
        return exact[0]
    return next((row for row in rows if want in " ".join((
        row["title"], row["process"], row["class"], row["path"])).lower()), None)


def focus_window(match: str = "", pid: int = 0, hwnd: int = 0) -> dict:
    row = find_window(match=match, pid=pid, hwnd=hwnd)
    if not row:
        return {"ok": False, "error": "window not found", "match": match,
                "pid": int(pid or 0), "hwnd": int(hwnd or 0)}
    u = _user32()
    target = wintypes.HWND(row["hwnd"])
    if row["minimized"]:
        u.ShowWindow(target, 9)  # SW_RESTORE
    u.BringWindowToTop(target)
    ok = bool(u.SetForegroundWindow(target))
    time.sleep(0.08)
    live = int(u.GetForegroundWindow() or 0)
    if live != row["hwnd"]:
        # Windows normally prevents a background process from stealing focus. Attach
        # this thread to the current/target input queues just long enough to request
        # activation, then detach immediately. This remains inside UIPI: it cannot
        # cross into an elevated process or the secure desktop.
        current_tid = int(ctypes.windll.kernel32.GetCurrentThreadId())
        target_pid = wintypes.DWORD()
        target_tid = int(u.GetWindowThreadProcessId(target, ctypes.byref(target_pid)))
        foreground = wintypes.HWND(live)
        foreground_pid = wintypes.DWORD()
        foreground_tid = int(u.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))) if live else 0
        attached = []
        try:
            for tid in (target_tid, foreground_tid):
                if tid and tid != current_tid and tid not in attached:
                    if u.AttachThreadInput(current_tid, tid, True):
                        attached.append(tid)
            u.BringWindowToTop(target)
            u.SetActiveWindow(target)
            ok = bool(u.SetForegroundWindow(target)) or ok
        finally:
            for tid in reversed(attached):
                u.AttachThreadInput(current_tid, tid, False)
        time.sleep(0.08)
        live = int(u.GetForegroundWindow() or 0)
    if live != row["hwnd"]:
        return {"ok": False, "error": "Windows refused foreground activation",
                "hwnd": row["hwnd"], "foreground_hwnd": live,
                "note": "click the target once, or run Collie at the same integrity level"}
    return {"ok": ok or live == row["hwnd"], "hwnd": row["hwnd"],
            "title": row["title"], "pid": row["pid"]}


def window_state(action: str, match: str = "", pid: int = 0, hwnd: int = 0) -> dict:
    row = find_window(match=match, pid=pid, hwnd=hwnd, include_hidden=True)
    if not row:
        return {"ok": False, "error": "window not found"}
    action = str(action or "focus").lower()
    if action == "focus":
        return focus_window(hwnd=row["hwnd"])
    show = {"hide": 0, "show": 5, "minimize": 6, "maximize": 3, "restore": 9}
    if action in show:
        ok = bool(_user32().ShowWindow(wintypes.HWND(row["hwnd"]), show[action]))
        return {"ok": True, "action": action, "hwnd": row["hwnd"],
                "previously_visible": ok}
    if action == "close":
        ok = bool(_user32().PostMessageW(wintypes.HWND(row["hwnd"]), 0x0010, 0, 0))
        return {"ok": ok, "action": "close", "hwnd": row["hwnd"],
                "error": "WM_CLOSE was refused" if not ok else ""}
    return {"ok": False, "error": "window action must be focus, show, hide, minimize, maximize, restore, or close"}


def clipboard_set(text: str) -> dict:
    u, kernel = _user32(), ctypes.windll.kernel32
    kernel.GlobalAlloc.restype = ctypes.c_void_p
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel.GlobalFree.argtypes = [ctypes.c_void_p]
    u.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    for _ in range(20):
        if u.OpenClipboard(None):
            break
        time.sleep(0.025)
    else:
        return {"ok": False, "error": "clipboard is busy"}
    handle = None
    try:
        if not u.EmptyClipboard():
            return {"ok": False, "error": "EmptyClipboard failed"}
        raw = (str(text or "") + "\0").encode("utf-16-le")
        handle = kernel.GlobalAlloc(0x0042, len(raw))  # GHND
        if not handle:
            return {"ok": False, "error": "GlobalAlloc failed"}
        ptr = kernel.GlobalLock(handle)
        if not ptr:
            kernel.GlobalFree(handle); handle = None
            return {"ok": False, "error": "GlobalLock failed"}
        ctypes.memmove(ptr, raw, len(raw))
        kernel.GlobalUnlock(handle)
        if not u.SetClipboardData(13, handle):  # CF_UNICODETEXT
            kernel.GlobalFree(handle); handle = None
            return {"ok": False, "error": "SetClipboardData failed"}
        handle = None  # clipboard owns it now
        return {"ok": True, "action": "clipboard_set", "characters": len(str(text or ""))}
    finally:
        u.CloseClipboard()
        if handle:
            kernel.GlobalFree(handle)


def clipboard_get(max_chars: int = 100000) -> dict:
    u, kernel = _user32(), ctypes.windll.kernel32
    u.GetClipboardData.restype = ctypes.c_void_p
    u.GetClipboardData.argtypes = [wintypes.UINT]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel.GlobalUnlock.argtypes = [ctypes.c_void_p]
    for _ in range(20):
        if u.OpenClipboard(None):
            break
        time.sleep(0.025)
    else:
        return {"ok": False, "error": "clipboard is busy"}
    try:
        handle = u.GetClipboardData(13)
        if not handle:
            return {"ok": False, "error": "clipboard has no Unicode text"}
        ptr = kernel.GlobalLock(handle)
        if not ptr:
            return {"ok": False, "error": "GlobalLock failed"}
        try:
            text = ctypes.wstring_at(ptr)[:max(0, min(1000000, int(max_chars or 0)))]
        finally:
            kernel.GlobalUnlock(handle)
        return {"ok": True, "action": "clipboard_get", "text": text,
                "characters": len(text)}
    finally:
        u.CloseClipboard()


def _send(*inputs: _INPUT) -> None:
    if not inputs:
        return
    arr = (_INPUT * len(inputs))(*inputs)
    sent = _user32().SendInput(len(arr), ctypes.byref(arr), ctypes.sizeof(_INPUT))
    if sent != len(arr):
        err = ctypes.get_last_error()
        raise RuntimeError(
            "SendInput delivered %d/%d events (Windows error %d). The target may be "
            "elevated or on the secure desktop." % (sent, len(arr), err))


def _keyboard(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    return _INPUT(type=INPUT_KEYBOARD,
                  u=_INPUTUNION(ki=_KEYBDINPUT(vk, scan, flags, 0, 0)))


def _mouse(flags: int, data: int = 0) -> _INPUT:
    return _INPUT(type=INPUT_MOUSE,
                  u=_INPUTUNION(mi=_MOUSEINPUT(0, 0, data, flags, 0, 0)))


def key_code(key: str | int) -> int:
    """Resolve a model-facing key name to a Windows virtual-key code."""
    if isinstance(key, int):
        if 0 <= key <= 255:
            return key
        raise ValueError("virtual-key code must be between 0 and 255")
    raw = str(key or "").strip()
    if not raw:
        raise ValueError("key is required")
    low = raw.lower().replace("_", "").replace("-", "").replace(" ", "")
    aliases = {k.replace("_", ""): v for k, v in _VK.items()}
    if low in aliases:
        return aliases[low]
    if len(raw) == 1:
        c = raw.upper()
        if "A" <= c <= "Z" or "0" <= c <= "9":
            return ord(c)
        packed = _user32().VkKeyScanW(ord(raw))
        if packed != -1:
            return packed & 0xFF
    raise ValueError("unknown key %r" % raw)


def key_down(key: str | int) -> dict:
    vk = key_code(key)
    _send(_keyboard(vk=vk))
    _HELD_KEYS.add(vk)
    return {"ok": True, "key": str(key), "vk": vk, "action": "down"}


def key_up(key: str | int) -> dict:
    vk = key_code(key)
    _send(_keyboard(vk=vk, flags=KEYEVENTF_KEYUP))
    _HELD_KEYS.discard(vk)
    return {"ok": True, "key": str(key), "vk": vk, "action": "up"}


def press(key: str | int, modifiers=(), repeat: int = 1, hold_ms: int = 35) -> dict:
    mods = [key_code(m) for m in (modifiers or [])]
    vk = key_code(key)
    repeat = max(1, min(100, int(repeat or 1)))
    hold = max(0, min(30000, int(hold_ms or 0))) / 1000.0
    for mod in mods:
        _send(_keyboard(vk=mod)); _HELD_KEYS.add(mod)
    try:
        for _ in range(repeat):
            _send(_keyboard(vk=vk)); _HELD_KEYS.add(vk)
            if hold:
                time.sleep(hold)
            _send(_keyboard(vk=vk, flags=KEYEVENTF_KEYUP)); _HELD_KEYS.discard(vk)
    finally:
        for mod in reversed(mods):
            _send(_keyboard(vk=mod, flags=KEYEVENTF_KEYUP)); _HELD_KEYS.discard(mod)
    return {"ok": True, "key": str(key), "modifiers": list(modifiers or []),
            "repeat": repeat, "hold_ms": int(hold * 1000), "action": "press"}


def type_text(text: str, interval_ms: int = 0) -> dict:
    """Type Unicode into the focused control without touching the clipboard."""
    delay = max(0, min(1000, int(interval_ms or 0))) / 1000.0
    units = str(text or "").encode("utf-16-le", "surrogatepass")
    count = 0
    for i in range(0, len(units), 2):
        scan = units[i] | (units[i + 1] << 8)
        _send(_keyboard(scan=scan, flags=KEYEVENTF_UNICODE),
              _keyboard(scan=scan, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        count += 1
        if delay:
            time.sleep(delay)
    return {"ok": True, "action": "type", "utf16_units": count}


def cursor() -> dict:
    p = _POINT()
    if not _user32().GetCursorPos(ctypes.byref(p)):
        raise RuntimeError("GetCursorPos failed")
    return {"ok": True, "x": int(p.x), "y": int(p.y)}


def move(x: float, y: float, duration_ms: int = 0, steps: int = 0) -> dict:
    x, y = int(round(float(x))), int(round(float(y)))
    duration = max(0, min(30000, int(duration_ms or 0)))
    if duration <= 0:
        if not _user32().SetCursorPos(x, y):
            raise RuntimeError("SetCursorPos failed")
        return {"ok": True, "action": "move", "x": x, "y": y}
    start = cursor()
    n = max(2, min(300, int(steps or max(2, duration // 16))))
    for i in range(1, n + 1):
        px = round(start["x"] + (x - start["x"]) * i / n)
        py = round(start["y"] + (y - start["y"]) * i / n)
        if not _user32().SetCursorPos(px, py):
            raise RuntimeError("SetCursorPos failed during movement")
        time.sleep(duration / n / 1000.0)
    return {"ok": True, "action": "move", "x": x, "y": y,
            "duration_ms": duration, "steps": n}


def mouse_button(button: str = "left", action: str = "click", count: int = 1,
                 interval_ms: int = 80) -> dict:
    button = str(button or "left").lower()
    if button not in _BUTTONS:
        raise ValueError("button must be left, right, or middle")
    down, up = _BUTTONS[button]
    action = str(action or "click").lower()
    if action == "down":
        _send(_mouse(down)); _HELD_BUTTONS.add(button)
    elif action == "up":
        _send(_mouse(up)); _HELD_BUTTONS.discard(button)
    elif action in ("click", "double_click"):
        clicks = 2 if action == "double_click" else max(1, min(20, int(count or 1)))
        for i in range(clicks):
            _send(_mouse(down), _mouse(up))
            if i + 1 < clicks:
                time.sleep(max(0, min(1000, int(interval_ms or 0))) / 1000.0)
    else:
        raise ValueError("mouse action must be click, double_click, down, or up")
    p = cursor()
    return {"ok": True, "action": action, "button": button,
            "x": p["x"], "y": p["y"]}


def click(x: float, y: float, button: str = "left", count: int = 1,
          interval_ms: int = 80) -> dict:
    move(x, y)
    action = "double_click" if int(count or 1) == 2 else "click"
    return mouse_button(button, action=action, count=count, interval_ms=interval_ms)


def drag(from_x: float, from_y: float, to_x: float, to_y: float,
         button: str = "left", duration_ms: int = 350, steps: int = 0) -> dict:
    move(from_x, from_y)
    mouse_button(button, "down")
    try:
        move(to_x, to_y, duration_ms=max(1, int(duration_ms or 350)), steps=steps)
    finally:
        mouse_button(button, "up")
    return {"ok": True, "action": "drag", "button": button,
            "from": [int(from_x), int(from_y)], "to": [int(to_x), int(to_y)]}


def scroll(delta: int, horizontal: bool = False) -> dict:
    delta = max(-12000, min(12000, int(delta or 0)))
    # mouseData is an unsigned DWORD in the struct; preserve a negative wheel delta's bits.
    data = ctypes.c_uint32(delta).value
    _send(_mouse(MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL, data))
    return {"ok": True, "action": "scroll", "delta": delta,
            "horizontal": bool(horizontal)}


def release_all() -> dict:
    errors = []
    for vk in list(_HELD_KEYS):
        try:
            _send(_keyboard(vk=vk, flags=KEYEVENTF_KEYUP))
        except Exception as exc:
            errors.append(str(exc))
        _HELD_KEYS.discard(vk)
    for button in list(_HELD_BUTTONS):
        try:
            _send(_mouse(_BUTTONS[button][1]))
        except Exception as exc:
            errors.append(str(exc))
        _HELD_BUTTONS.discard(button)
    return {"ok": not errors, "action": "release_all", "errors": errors}


atexit.register(release_all)
