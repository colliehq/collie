"""A menu-bar control for whatever collie is playing — the button, not the conversation.

Anything the agent starts that outlives the request has to leave behind a control that is NOT the
agent. Music was the first thing collie could start and keep going, and for a while the only ways to
stop it were to ask the agent again or to `pkill ffplay` in a terminal. Neither is a control; the
first is a habit nobody should be taught, and the second is not something to ask of anyone.

So: a status item that exists ONLY while something is playing, shows what it is, and stops it in one
click. It is in the menu bar because that is where macOS puts "something is running in the
background, here is how to stop it" — visible without opening any window, reachable from anywhere.

It has to live in the process that owns the player's child process, which is the same process that
serves the UI, so this is called from there rather than from a separate menu-bar app.

macOS only. `harness/nowplaying_win.py` is the tray counterpart; elsewhere the UI's own now-playing
strip is the control.
"""
from __future__ import annotations

import threading

_state = {"item": None, "handler": None}
_lock = threading.Lock()


def available() -> bool:
    """True when there is a running AppKit app to hang a status item on.

    `collie app` has one; `collie web` in a terminal does not, and a status item created without a
    run loop never appears while AppKit reports nothing at all — the worst way for a safety control
    to fail. Callers must treat False as "no button exists" and say so.
    """
    try:
        from AppKit import NSApplication
        return bool(NSApplication.sharedApplication().isRunning())
    except Exception:
        return False


_pump = {"obj": None}


def _on_main(fn) -> None:
    """Run `fn` on the main thread. AppKit ignores UI work from anywhere else — silently — and the
    request that starts music arrives on an HTTP worker.

    performSelectorOnMainThread, not addOperationWithBlock_: handing PyObjC a plain Python closure
    where it expects a block crashed the process outright (Trace/BPT trap, no traceback, nothing
    written). A selector on a real NSObject is the boring path that works.
    """
    if threading.current_thread() is threading.main_thread():
        fn()
        return
    try:
        from AppKit import NSObject

        if _pump["obj"] is None:

            class _Pump(NSObject):
                def call_(self, box):
                    try:
                        box[0]()
                    except Exception:
                        pass

            _pump["obj"] = _Pump.alloc().init()

        _pump["obj"].performSelectorOnMainThread_withObject_waitUntilDone_("call:", [fn], False)
    except Exception:
        pass


def show(title: str, on_stop) -> bool:
    """Put (or update) the now-playing item in the menu bar. `on_stop()` is called when clicked.

    Returns False when it could not be shown, so the caller can be honest about the fact that no
    button exists rather than assuming one does. The AppKit work is dispatched to the main thread,
    because the request that starts music arrives on an HTTP worker and AppKit ignores UI work from
    anywhere else — silently.
    """
    if not available():
        return False
    _state["cb"] = on_stop
    short = (title or "").strip()
    if len(short) > 40:
        short = short[:39].rstrip() + "\u2026"

    def build():
        try:
            from AppKit import (NSStatusBar, NSVariableStatusItemLength, NSObject,
                                NSMenu, NSMenuItem)
        except Exception:
            return
        with _lock:
            try:
                if _state["item"] is None:

                    class _Stop(NSObject):
                        def stop_(self, sender):
                            cb = _state.get("cb")
                            if cb:
                                try:
                                    cb()
                                except Exception:
                                    pass
                            hide()

                    handler = _Stop.alloc().init()
                    item = NSStatusBar.systemStatusBar().statusItemWithLength_(
                        NSVariableStatusItemLength)
                    menu = NSMenu.alloc().init()
                    stop = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                        "Stop", "stop:", "")
                    stop.setTarget_(handler)
                    menu.addItem_(stop)
                    item.setMenu_(menu)
                    _state["item"], _state["handler"] = item, handler

                item = _state["item"]
                # The track goes in the title, not only the tooltip: a tooltip needs a hover to find,
                # and someone hunting for the noise their computer is making should not have to look.
                item.button().setTitle_("\u266a " + short if short else "\u266a")
                item.button().setToolTip_("Collie is playing: " + (title or "") + "\nClick to stop.")
            except Exception:
                pass

    _on_main(build)
    return True


def hide() -> None:
    """Remove the item. Nothing playing must mean nothing in the menu bar — an indicator that lingers
    after the sound stops teaches people to distrust it.

    On the main thread like everything else here. Removing a status item from an HTTP worker crashed
    the whole app outright (Trace/BPT trap, no traceback), and that path is a real one: "stop the
    music" from the phone arrives on a worker.
    """
    with _lock:
        item = _state.get("item")
        _state["item"], _state["handler"], _state["cb"] = None, None, None
    if item is None:
        return

    def remove():
        try:
            from AppKit import NSStatusBar
            NSStatusBar.systemStatusBar().removeStatusItem_(item)
        except Exception:
            pass

    _on_main(remove)
