"""collie in the macOS menu bar — a status item with the composer one click away.

This is the piece that only a native shell can give you. `collie app` is an application you switch
to; a status item is something that is simply there, in every Space, over every full-screen app,
without taking a Dock tile or stealing focus from what you were doing. Ask a question, read the
answer, and the popover closes when you click away.

No permission of any kind. NSStatusItem and NSPopover are ordinary AppKit; nothing here reads other
applications or listens to input, so nothing prompts.

The popover hosts the same web UI as everything else. That is the point of the split this codebase
keeps: the SHELL is native — menu bar, popover, key equivalents, Spaces behaviour — and the surface
inside it stays one implementation shared by Windows, macOS, iOS and the browser. Rewriting the
chat itself in AppKit would mean writing streaming tokens, markdown and code highlighting four
times over.
"""
from __future__ import annotations

import sys

from . import plat

PLATFORM = "macos"      # declared, not implied — tests/test_platform_purity.py reads this

# Everything AppKit hands us must outlive the function that made it. A WKWebView released before
# the run loop is up dies inside WebCore's deallocate-on-main-loop hop — the crash that cost four
# attempts at the app window earlier.
_hold = []


def available():
    if not plat.is_macos():
        return False, "the menu bar item is macOS-only"
    try:
        import AppKit  # noqa: F401
        import WebKit  # noqa: F401
    except Exception as e:
        return False, "PyObjC is not installed (%s); pip install 'collie-harness[desktop]'" % e
    return True, ""


def _icon(NSImage, size=18.0):
    """A template image, so macOS tints it for light and dark menu bars by itself. Drawn rather than
    loaded: the shipped logo is a full-colour mark and would look wrong up there."""
    from AppKit import NSBezierPath, NSColor, NSMakeRect

    img = NSImage.alloc().initWithSize_((size, size))
    img.lockFocus()
    NSColor.blackColor().set()
    # a collie head reduced to what survives at 18pt: a rounded muzzle and two ears
    head = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(3.5, 2.5, size - 7, size - 8))
    head.fill()
    for x in (2.0, size - 6.0):
        ear = NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(x, size - 8.5, 4.0, 6.5))
        ear.fill()
    img.unlockFocus()
    img.setTemplate_(True)
    return img


def run(url, title="Collie"):
    """Park a status item in the menu bar and hand the main thread to AppKit. Blocks.

    Returns an exit code. The app runs as .accessory: no Dock tile, no menu bar of its own — it IS
    the menu bar item, and putting it in the Dock as well would be one collie too many.
    """
    ok, why = available()
    if not ok:
        print("collie menubar: " + why, file=sys.stderr)
        return 2

    from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSImage, NSMenu,
                        NSMenuItem, NSObject, NSPopover, NSPopoverBehaviorTransient, NSStatusBar,
                        NSVariableStatusItemLength, NSViewController, NSMakeRect)
    from Foundation import NSURL, NSURLRequest
    from WebKit import WKWebView, WKWebViewConfiguration

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    view = WKWebView.alloc().initWithFrame_configuration_(
        NSMakeRect(0, 0, 420, 560), WKWebViewConfiguration.alloc().init())
    view.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))

    vc = NSViewController.alloc().init()
    vc.setView_(view)

    pop = NSPopover.alloc().init()
    pop.setContentViewController_(vc)
    pop.setContentSize_((420, 560))
    # Transient: clicking anywhere else dismisses it. A popover you have to close by hand is a
    # window with extra steps.
    pop.setBehavior_(NSPopoverBehaviorTransient)

    item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
    item.button().setImage_(_icon(NSImage))
    item.button().setToolTip_(title)

    class _Handler(NSObject):
        def toggle_(self, sender):
            if pop.isShown():
                pop.performClose_(sender)
                return
            b = item.button()
            pop.showRelativeToRect_ofView_preferredEdge_(b.bounds(), b, 1)   # 1 = maxY, below
            # Without this the popover appears but cannot be typed into: an .accessory app is not
            # active, and an inactive app's window does not take key input.
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            pop.contentViewController().view().window().makeFirstResponder_(view)

        def quit_(self, _sender):
            NSApplication.sharedApplication().terminate_(None)

    handler = _Handler.alloc().init()

    # Left click opens the popover; right click gets a menu, which is what a Mac user expects of a
    # status item and where Quit has to live when there is no Dock tile to right-click.
    item.button().setTarget_(handler)
    item.button().setAction_("toggle:")

    menu = NSMenu.alloc().init()
    q = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit Collie", "quit:", "q")
    q.setTarget_(handler)
    menu.addItem_(q)
    item.setMenu_(None)          # keep left-click on the popover; the menu is attached on demand

    _hold.extend([app, view, vc, pop, item, handler, menu])
    app.run()
    return 0
