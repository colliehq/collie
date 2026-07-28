"""Collie native-app control on macOS — the counterpart of harness/native.py.

Windows drives apps through PowerShell and .NET UI Automation. The macOS mirror of that is
`osascript` and System Events, which reaches the same Accessibility tree AppleScript has always
used. Same reasoning as the Windows side, and the same benefit: ZERO pip dependencies, so nothing
new has to be bundled into the .app and signed. The obvious alternative — pyobjc's
ApplicationServices for AXUIElement — is not in the bundle and adding it would mean more nested
Mach-O to notarise, which is the cost this whole file exists to avoid.

THE PERMISSION SPLIT IS THE IMPORTANT PART, because it decides what works before the user has been
asked for anything:

    no permission needed   list running apps, list windows and their titles, focus an app,
                           launch and quit — NSWorkspace and CGWindowList answer all of it
    Accessibility needed   read a window's element tree, press a button, type into a field

So collie can see and switch between your apps immediately, and only asks for Accessibility when
you ask it to actually drive one. `permission()` reports the state and how to grant it; every
function that needs it says so by name rather than returning an empty result.
"""
from __future__ import annotations

import json
import subprocess

from . import plat

PLATFORM = "macos"          # declared, not implied — tests/test_platform_purity.py reads this

_TIMEOUT = 20


def available():
    """(ok, why). The module itself, not the permission — see permission()."""
    if not plat.is_macos():
        return False, "native_mac is macOS-only; use harness.native on Windows"
    return True, ""


def _osa(script, timeout=_TIMEOUT):
    """Run AppleScript, return (ok, text). Accessibility denial is recognised and named, because
    'execution error -1719' tells a user nothing about what to do next."""
    try:
        r = subprocess.run(["/usr/bin/osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return False, "could not run osascript: %s" % e
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        if "not allowed assistive access" in out or "-1719" in out:
            return False, "PERMISSION: Accessibility is not granted — " + permission()["how"]
        return False, out[:300]
    return True, (r.stdout or "").strip()


def permission():
    """{'granted': bool, 'how': str}. Cheap and side-effect free: a UI query that only succeeds with
    Accessibility, chosen over prompting, because a permission dialog nobody asked for is rude."""
    ok, _out = _osa('tell application "System Events" to get name of first window of '
                    '(first process whose frontmost is true)', timeout=10)
    return {"granted": bool(ok),
            "how": "System Settings → Privacy & Security → Accessibility → enable Collie "
                   "(or your terminal, when running collie from a shell)"}


# ── no permission required ────────────────────────────────────────────────────────────────────
def apps():
    """Running applications with a UI. NSWorkspace, so no Accessibility and no AppleScript."""
    try:
        from AppKit import NSWorkspace
    except Exception as e:
        return {"ok": False, "error": "PyObjC not installed: %s" % e}
    out = []
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        if a.activationPolicy() != 0:            # 0 = regular, i.e. has a Dock tile
            continue
        out.append({"name": a.localizedName(), "pid": int(a.processIdentifier()),
                    "bundle": a.bundleIdentifier() or "", "active": bool(a.isActive()),
                    "hidden": bool(a.isHidden())})
    return {"ok": True, "apps": sorted(out, key=lambda x: (x["name"] or "").lower())}


def windows(match=""):
    """On-screen windows and their titles, via CGWindowList — also permission-free.

    Note what this cannot do: window TITLES are readable, but the contents are not. Reading inside a
    window is the Accessibility half.
    """
    try:
        import Quartz
    except Exception as e:
        return {"ok": False, "error": "PyObjC not installed: %s" % e}
    want = (match or "").lower()
    got = []
    for w in (Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID) or []):
        owner, title = (w.get("kCGWindowOwnerName") or ""), (w.get("kCGWindowName") or "")
        if want and want not in owner.lower() and want not in title.lower():
            continue
        b = w.get("kCGWindowBounds") or {}
        got.append({"app": owner, "title": title, "pid": w.get("kCGWindowOwnerPID"),
                    "id": w.get("kCGWindowNumber"),
                    "w": int(b.get("Width", 0)), "h": int(b.get("Height", 0))})
    return {"ok": True, "windows": got}


def focus(name):
    """Bring an app to the front. `activate` is a plain Apple Event — no Accessibility."""
    ok, out = _osa('tell application %s to activate' % json.dumps(name))
    return {"ok": ok} if ok else {"ok": False, "error": out}


def quit_app(name):
    ok, out = _osa('tell application %s to quit' % json.dumps(name))
    return {"ok": ok} if ok else {"ok": False, "error": out}


def hide_others():
    """Everything except collie out of the way — what Cmd-Opt-H does, and no permission for it."""
    try:
        from AppKit import NSApplication
        NSApplication.sharedApplication().hideOtherApplications_(None)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Accessibility required ────────────────────────────────────────────────────────────────────
def tree(name, max_items=60):
    """The controls of an app's front window: role, title, and whether it can be pressed.

    Deliberately shallow. A full recursive walk of a real app is thousands of elements and seconds
    of AppleScript, and what a caller wants is "what can I click here".
    """
    # No `try` around the loop. An AppleScript try block turns "Accessibility is denied" into an
    # empty string and a zero exit status, which arrives here as a cheerful {"ok": true, items: []}
    # — the app has no controls, apparently. That is the exact shape of the six bugs this codebase
    # spent a day removing, and it is worth more to fail loudly than to look tidy.
    script = ('tell application "System Events" to tell process %s\n'
              '  set out to ""\n'
              '  repeat with e in (UI elements of window 1)\n'
              '    set out to out & (role of e) & "\\t" & (name of e as string) & "\\n"\n'
              '  end repeat\n'
              '  return out\n'
              'end tell') % json.dumps(name)
    ok, out = _osa(script, timeout=30)
    if not ok:
        return {"ok": False, "error": out}
    items = []
    for line in (out or "").splitlines():
        if "\t" not in line:
            continue
        role, _, label = line.partition("\t")
        items.append({"role": role.strip(), "name": label.strip()})
        if len(items) >= max_items:
            break
    return {"ok": True, "items": items}


def click(name, label):
    """Press the button (or menu item) named `label` in the app's front window."""
    script = ('tell application "System Events" to tell process %s\n'
              '  click (first UI element of window 1 whose name is %s)\n'
              'end tell') % (json.dumps(name), json.dumps(label))
    ok, out = _osa(script, timeout=30)
    return {"ok": ok} if ok else {"ok": False, "error": out}


def type_text(name, text):
    """Type into whatever has focus in that app. keystroke, not set-value, because it works on any
    control rather than only on ones that expose an editable value."""
    script = ('tell application %s to activate\n'
              'delay 0.2\n'
              'tell application "System Events" to keystroke %s') % (json.dumps(name),
                                                                     json.dumps(text))
    ok, out = _osa(script, timeout=30)
    return {"ok": ok} if ok else {"ok": False, "error": out}


def menu(name, *path):
    """Drive a menu: menu('Safari', 'File', 'New Window'). Menus are where most app functionality
    actually lives, and they are named and stable in a way that on-screen controls are not."""
    if len(path) < 2:
        return {"ok": False, "error": "need at least a menu and an item"}
    script = ('tell application "System Events" to tell process %s\n'
              '  click menu item %s of menu 1 of menu bar item %s of menu bar 1\n'
              'end tell') % (json.dumps(name), json.dumps(path[-1]), json.dumps(path[0]))
    ok, out = _osa(script, timeout=30)
    return {"ok": ok} if ok else {"ok": False, "error": out}
