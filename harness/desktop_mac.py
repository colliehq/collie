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


def reveal_desktop(show=True):
    """Hide every other app so the collie desktop is what you see (or bring them all back).
    Returns False when the wallpaper is not running in this process."""
    fn = globals().get("_REVEAL")
    if not fn:
        return False
    fn("reveal" if show else "unreveal")
    return True


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


def run_app_window(url, title="Collie"):
    """`collie app` — an ordinary application window. Titled, closable, in the Dock and in Cmd-Tab.

    Deliberately NOT run() with a flag. run() is the desktop: it claims every Space, keeps a pid
    file, installs a SIGTERM handler, rebuilds itself when displays change and stays out of the
    window cycle. None of that belongs to an app window, and threading an app_window flag through
    it segfaulted on launch four different ways — while a plain titled window with a WKWebView, on
    its own, has never once crashed. The desktop and the app are different things; this is the
    smallest correct version of the second one.
    """
    ok, why = available()
    if not ok:
        print("collie app: " + why, file=sys.stderr)
        return 2

    from AppKit import (NSApplication, NSWindow, NSScreen, NSObject, NSBackingStoreBuffered,
                        NSApplicationActivationPolicyRegular)
    from Foundation import NSURL, NSURLRequest
    from WebKit import WKWebView, WKWebViewConfiguration

    TITLED, CLOSABLE, MINIATURIZABLE, RESIZABLE = 1, 2, 4, 8

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)   # Dock tile, menu bar, Cmd-Tab

    screen = NSScreen.screens()[0]
    v = screen.visibleFrame()
    w_, h_ = min(1180.0, v.size.width - 80), min(820.0, v.size.height - 80)
    frame = ((v.origin.x + (v.size.width - w_) / 2, v.origin.y + (v.size.height - h_) / 2),
             (w_, h_))

    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_screen_(
        frame, TITLED | CLOSABLE | MINIATURIZABLE | RESIZABLE, NSBackingStoreBuffered, False, screen)
    win.setTitle_(title)
    view = WKWebView.alloc().initWithFrame_configuration_(
        ((0, 0), (w_, h_)), WKWebViewConfiguration.alloc().init())
    view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
    win.setContentView_(view)
    win.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    class _AppDelegate(NSObject):
        def applicationShouldTerminateAfterLastWindowClosed_(self, _app):
            return True          # closing the window quits, as in any single-window Mac app

    delegate = _AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    _hold.extend([win, view, delegate])       # nothing here may be collected while AppKit is live
    app.run()
    return 0


# Module-level so the window, its web view and the delegate outlive the function frame. A WKWebView
# freed while the main run loop is not yet up crashes inside WebCore's deallocate-on-main-loop hop.
_hold = []


def run(url, behind=True, app_window=False):
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
    TITLED, CLOSABLE, MINIATURIZABLE, RESIZABLE = 1, 2, 4, 8
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
    # app_window: an ordinary application — Dock tile, Cmd-Tab, a title bar you can close. The
    # desktop modes deliberately have none of that; `collie app` needs all of it.
    level = (CGWindowLevelForKey(kCGDesktopWindowLevelKey) if behind
             else CGWindowLevelForKey(kCGNormalWindowLevelKey) - (0 if app_window else 1))

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
    # .regular from a bare python process (no Info.plist, no bundle identity) is what AppKit calls
    # unsupported; it tears the app down at launch and WKWebView's dealloc then crashes. Inside
    # Collie.app the bundle supplies that identity and the Dock tile comes from the bundle itself,
    # so the code never has to ask for it.
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
    # WKWebView must be deallocated on a live main run loop; letting Python drop the last reference
    # before app.run() starts means -[WKWebView dealloc] fires during teardown and segfaults inside
    # WebCoreObjCScheduleDeallocateOnMainRunLoop. Hold them for the life of the process.
    views = []

    def build():
        """One window per display. Rebuilt wholesale when the screen layout changes — cheaper to
        reason about than diffing NSScreen identities across a monitor being unplugged."""
        for w in windows:
            w.orderOut_(None)
        del windows[:]
        del views[:]
        for screen in NSScreen.screens():
            # behind: the whole screen, because it IS the wallpaper and the Dock floats above it
            # anyway (kCGDockWindowLevel sits well above kCGDesktopWindowLevel).
            # front: visibleFrame, which is the screen minus the menu bar and the Dock. An
            # interactive window at normal level would otherwise sit under the Dock, and the part
            # of the page hidden there is exactly where the composer lives.
            # The desktop modes take the whole screen; the Dock and menu bar sit at far higher
            # window levels and are never covered. An app window is a window: give it a sensible
            # size in the middle of the working area.
            if app_window:
                v = screen.visibleFrame()
                w_, h_ = min(1180.0, v.size.width - 80), min(820.0, v.size.height - 80)
                frame = ((v.origin.x + (v.size.width - w_) / 2,
                          v.origin.y + (v.size.height - h_) / 2), (w_, h_))
            else:
                frame = screen.frame()
            cls = NSWindow if behind else _KeyWindow
            mask = (TITLED | CLOSABLE | MINIATURIZABLE | RESIZABLE) if app_window else BORDERLESS
            w = cls.alloc().initWithContentRect_styleMask_backing_defer_screen_(
                frame, mask, NSBackingStoreBuffered, False, screen)
            w.setLevel_(level)
            if app_window:
                w.setTitle_("Collie")
                w.center()
            else:
                # Desktop furniture: on every Space, not dragged around by Space switches, out of
                # Cmd-Tab and Mission Control. All of that is wrong for an ordinary window.
                w.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces
                                         | NSWindowCollectionBehaviorStationary
                                         | NSWindowCollectionBehaviorIgnoresCycle)
            w.setReleasedWhenClosed_(False)
            w.setIgnoresMouseEvents_(bool(behind) and not app_window)
            w.setHasShadow_(not behind)
            w.setBackgroundColor_(NSColor.blackColor())
            view = WKWebView.alloc().initWithFrame_configuration_(
                ((0, 0), (frame.size.width, frame.size.height)), WKWebViewConfiguration.alloc().init())
            view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
            views.append(view)
            w.setContentView_(view)
            w.orderFront_(None)
            windows.append(w)

    class _Watcher(NSObject):
        def screensChanged_(self, _note):
            build()

        def applicationShouldTerminateAfterLastWindowClosed_(self, _app):
            """Closing the last window quits. Without it the close button hides the window and
            leaves a running, windowless process behind — which under the desktop modes'
            accessory policy is both invisible and unkillable."""
            # Never True. AppKit asks this during launch, before build() has made any window, and
            # takes the answer as "the last one just closed" — the app terminates on the spot and
            # -[WKWebView dealloc] then runs with no live main run loop, which segfaults inside
            # WebCoreObjCScheduleDeallocateOnMainRunLoop. Quitting is Cmd-Q, as in any Mac app.
            return False

        def tick_(self, _timer):
            """Deliberately empty. app.run() is a native run loop, and CPython only dispatches
            signal handlers between bytecodes on the main thread — so while AppKit blocks there,
            nothing ever runs them and SIGTERM from `collie wallpaper --stop` was swallowed. This
            timer hands control back to Python a few times a second, which is all the interpreter
            needs to notice a pending signal."""

    # "Show desktop" for a desktop that IS a window.
    #
    # Sitting one level above the Finder icons means collie now receives the click that used to
    # reach the wallpaper, so macOS's own click-wallpaper-to-reveal never fires. hideOtherApplications
    # is the same thing by another route (it is what Cmd-Opt-H does) and needs no Accessibility
    # permission, unlike synthesising a key press. AppKit is main-thread-only and the web server
    # runs in a daemon thread, hence the hop.
    class _Reveal(NSObject):
        def reveal_(self, _arg):
            NSApplication.sharedApplication().hideOtherApplications_(None)

        def unreveal_(self, _arg):
            NSApplication.sharedApplication().unhideAllApplications_(None)

    _revealer = _Reveal.alloc().init()

    def _do(which):
        _revealer.performSelectorOnMainThread_withObject_waitUntilDone_(
            which + ":", None, False)

    globals()["_REVEAL"] = _do

    watcher = _Watcher.alloc().init()
    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        watcher, "screensChanged:", "NSApplicationDidChangeScreenParametersNotification", None)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.25, watcher, "tick:", None, True)

    build()
    # AFTER build(), never before: the delegate answers
    # applicationShouldTerminateAfterLastWindowClosed_, and with no windows yet AppKit takes that as
    # "the last window closed" and terminates the app the instant it launches.
    app.setDelegate_(watcher)
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
