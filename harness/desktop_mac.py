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
                        NSApplicationActivationPolicyAccessory,
                        NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,
                        NSWindowCollectionBehaviorCanJoinAllSpaces,
                        NSWindowCollectionBehaviorStationary,
                        NSWindowCollectionBehaviorIgnoresCycle)
    from Foundation import NSURL, NSURLRequest, NSNotificationCenter, NSTimer
    from WebKit import WKWebView, WKWebViewConfiguration
    from Quartz import (CGWindowLevelForKey, kCGDesktopWindowLevelKey,
                        kCGNormalWindowLevelKey)

    BORDERLESS = 0
    # THE WHOLE DESIGN IS THIS NUMBER.
    #
    #   kCGDesktopWindowLevel      -2147483623   under the icons — a wallpaper you only look at
    #   kCGDesktopIconWindowLevel  -2147483603   the Finder icons
    #   normal - 1                 -1            over the icons, under every app window
    #   kCGNormalWindowLevel        0            level with apps
    #   kCGDockWindowLevel         20            the Dock, always above all of this
    #
    # Interactive mode used to sit at 0, level with ordinary apps, which is what made it possible
    # for a full-screen borderless window to cover everything with no way out but a reboot. One
    # below normal is the answer: it takes clicks (the composer works), it covers the desktop icons
    # (fine — it IS the desktop), and every app window in the system floats above it, so it can
    # never trap anything. That also means it needs no Dock tile to escape from.
    level = (CGWindowLevelForKey(kCGDesktopWindowLevelKey) if behind
             else CGWindowLevelForKey(kCGNormalWindowLevelKey) - 1)

    app = NSApplication.sharedApplication()
    # THE ESCAPE HATCH, and it is not optional. Each of the wallpaper's window settings is right for
    # a wallpaper and fatal for an interactive window; together they made a full-screen, always-on-
    # every-Space window with no close button, no Dock tile, no menu bar and no entry in Cmd-Tab.
    # There was no way out of it short of rebooting the machine — which is exactly what happened.
    #
    # So the two modes get opposite treatment:
    #   behind   .accessory, borderless, all-Spaces, out of the cycle — a wallpaper you look at
    #   front    .regular, titled+closable, ordinary Space behaviour — a window you can quit
    #
    # A borderless NSWindow also returns NO from canBecomeKeyWindow, so the composer could never
    # have taken a keystroke anyway; .titled fixes that at the same time.
    # .accessory in both modes: no Dock tile, no menu bar. This is a desktop, not an app you
    # switch to — and with the window a level below every app it does not need to be escapable
    # from the Dock.
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    class _KeyWindow(NSWindow):
        """A borderless NSWindow answers NO to canBecomeKeyWindow, which is why the composer could
        never take a keystroke. Overriding it keeps the full-bleed look and lets the page be typed
        into — no title bar required."""

        def canBecomeKeyWindow(self):
            return True

        def canBecomeMainWindow(self):
            return True

    windows = []

    def build():
        """One window per display. Rebuilt wholesale when the screen layout changes — cheaper to
        reason about than diffing NSScreen identities across a monitor being unplugged."""
        for w in windows:
            w.orderOut_(None)
        del windows[:]
        for screen in NSScreen.screens():
            # behind: the whole screen, because it IS the wallpaper and the Dock floats above it
            # anyway (kCGDockWindowLevel sits well above kCGDesktopWindowLevel).
            # front: visibleFrame, which is the screen minus the menu bar and the Dock. An
            # interactive window at normal level would otherwise sit under the Dock, and the part
            # of the page hidden there is exactly where the composer lives.
            # Full screen in both modes. The Dock and the menu bar have window levels far above
            # this one, so they are never covered and visibleFrame would only carve out a strip
            # for no reason.
            frame = screen.frame()
            cls = NSWindow if behind else _KeyWindow
            w = cls.alloc().initWithContentRect_styleMask_backing_defer_screen_(
                frame, BORDERLESS, NSBackingStoreBuffered, False, screen)
            w.setLevel_(level)
            # Both modes are desktop furniture: on every Space, not dragged around by Space
            # switches, out of Cmd-Tab and Mission Control.
            w.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | NSWindowCollectionBehaviorStationary
                                     | NSWindowCollectionBehaviorIgnoresCycle)
            w.setReleasedWhenClosed_(False)
            w.setIgnoresMouseEvents_(bool(behind))
            w.setHasShadow_(not behind)
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

        def applicationShouldTerminateAfterLastWindowClosed_(self, _app):
            """Closing the window quits. Without this the traffic light hides the window and leaves
            a running, windowless app behind — which for the wallpaper's accessory policy would be
            invisible and unkillable, the same trap from the other direction."""
            return not behind

        def tick_(self, _timer):
            """Deliberately empty. app.run() is a native run loop, and CPython only dispatches
            signal handlers between bytecodes on the main thread — so while AppKit blocks there,
            nothing ever runs them and SIGTERM from `collie wallpaper --stop` was swallowed. This
            timer hands control back to Python a few times a second, which is all the interpreter
            needs to notice a pending signal."""

    watcher = _Watcher.alloc().init()
    app.setDelegate_(watcher)
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
