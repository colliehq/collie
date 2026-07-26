"""collie's live desktop on macOS — the real one, behind the Finder icons.

Windows pins a WebView2 window under Progman. macOS has no Progman; it has window LEVELS, and the
SDK puts kCGDesktopWindowLevel exactly 20 below kCGDesktopIconWindowLevel:

    kCGDesktopWindowLevel     = kCGMinimumWindowLevel + 20      (-2147483623)
    kCGDesktopIconWindowLevel = kCGDesktopWindowLevel  + 20      (-2147483603)

so a window parked at the first renders *under* the icons. Same outcome as Progman, different
mechanism — and it needs a real NSWindow, which no browser can provide (Chrome exposes no
window-level switch), so this is the one piece of collie's desktop that cannot be a browser window.

No compiler and no Xcode: PyObjC drives the same AppKit/WebKit objects a Swift app would, from
Python, which keeps this inside the one codebase instead of forking a native app.
    pip install collie-harness[desktop]

Clicks pass straight through to Finder (setIgnoresMouseEvents_), so the icons stay usable and the
wallpaper is a *view*. That costs nothing, because every way you actually talk to collie — the
terminal, `collie web`, the phone, an ACP editor — already drives the same live feed this page
renders. `--front` opts out and gives an ordinary interactive window instead.
"""
import json
import os
import signal
import sys

from . import plat

STATE_DIR = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
STATE = os.path.join(STATE_DIR, "desktop-mac.json")


def available():
    """(ok, reason) — is the native path usable on this machine?"""
    if not plat.is_macos():
        return False, "not macOS"
    try:
        import AppKit, WebKit, Quartz            # noqa: F401
    except ImportError:
        return False, ("the native desktop needs PyObjC — install it with:\n"
                       "    pip install 'collie-harness[desktop]'\n"
                       "  (without it `collie wallpaper` still opens a borderless browser window, "
                       "which sits *over* the desktop rather than behind the icons)")
    return True, ""


# ── the running instance (start and stop are separate CLI invocations) ───────────────────────────
def _save(pid):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE, "w") as f:
        json.dump({"pid": pid}, f)


def _load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return None


def _clear():
    try:
        os.remove(STATE)
    except OSError:
        pass


def running_pid():
    """The pid of a live desktop process, or None. Verified to still be a collie, so a recycled pid
    is never mistaken for a running wallpaper."""
    st = _load() or {}
    pid = st.get("pid")
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
    except OSError:
        _clear()
        return None
    return int(pid)


def stop():
    pid = running_pid()
    if not pid:
        _clear()
        return "collie wallpaper: not running"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return "collie wallpaper: could not stop pid %s (%s)" % (pid, e)
    _clear()
    return "collie wallpaper: stopped (pid %s)" % pid


def run(url, behind=True):
    """Park a WKWebView on every display and hand the main thread to AppKit. Blocks until the
    process is told to stop. Returns an exit code."""
    ok, why = available()
    if not ok:
        print("collie wallpaper: " + why, file=sys.stderr)
        return 2

    from AppKit import (NSApplication, NSWindow, NSScreen, NSColor, NSObject,
                        NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered,
                        NSWindowCollectionBehaviorCanJoinAllSpaces,
                        NSWindowCollectionBehaviorStationary,
                        NSWindowCollectionBehaviorIgnoresCycle)
    from Foundation import NSURL, NSURLRequest, NSNotificationCenter, NSTimer
    from WebKit import WKWebView, WKWebViewConfiguration
    from Quartz import (CGWindowLevelForKey, kCGDesktopWindowLevelKey,
                        kCGNormalWindowLevelKey)

    BORDERLESS = 0
    level = CGWindowLevelForKey(kCGDesktopWindowLevelKey if behind else kCGNormalWindowLevelKey)

    app = NSApplication.sharedApplication()
    # .accessory: no Dock tile and no menu bar. A wallpaper is not an app you switch to.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    windows = []

    def build():
        """One window per display. Rebuilt wholesale when the screen layout changes — cheaper to
        reason about than diffing NSScreen identities across a monitor being unplugged."""
        for w in windows:
            w.orderOut_(None)
        del windows[:]
        for screen in NSScreen.screens():
            frame = screen.frame()
            w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_screen_(
                frame, BORDERLESS, NSBackingStoreBuffered, False, screen)
            w.setLevel_(level)
            # canJoinAllSpaces + stationary: present on every Space and not dragged by Space
            # switches; ignoresCycle keeps it out of Cmd-Tab and Mission Control.
            w.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | NSWindowCollectionBehaviorStationary
                                     | NSWindowCollectionBehaviorIgnoresCycle)
            w.setIgnoresMouseEvents_(bool(behind))
            w.setHasShadow_(False)
            w.setBackgroundColor_(NSColor.blackColor())
            view = WKWebView.alloc().initWithFrame_configuration_(
                ((0, 0), (frame.size.width, frame.size.height)), WKWebViewConfiguration.alloc().init())
            view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
            w.setContentView_(view)
            w.orderFront_(None)
            windows.append(w)

    class _Watcher(NSObject):
        def screensChanged_(self, _note):
            build()

        def tick_(self, _timer):
            """Deliberately empty. app.run() is a native run loop, and CPython only dispatches
            signal handlers between bytecodes on the main thread — so while AppKit blocks there,
            nothing ever runs them and SIGTERM from `collie wallpaper --stop` was swallowed. This
            timer hands control back to Python a few times a second, which is all the interpreter
            needs to notice a pending signal."""

    watcher = _Watcher.alloc().init()
    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        watcher, "screensChanged:", "NSApplicationDidChangeScreenParametersNotification", None)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.25, watcher, "tick:", None, True)

    build()
    _save(os.getpid())

    def _bye(_sig, _frm):
        _clear()
        app.terminate_(None)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    print("collie wallpaper · %s · %d display%s · %s" %
          (url, len(windows), "" if len(windows) == 1 else "s",
           "behind the icons (clicks pass through)" if behind else "interactive window"),
          flush=True)
    try:
        app.run()
    except SystemExit:
        pass
    finally:
        _clear()
    return 0
