"""The menu-bar control for whatever collie is playing — driven inside a real AppKit run loop.

The point of this control is that stopping the music must never require asking the agent again. So
what has to be true is: the item appears when playback starts, it says what is playing, clicking it
kills the player AND removes itself, and none of that crashes when it is driven from an HTTP worker
— which is where "stop the music" from the phone arrives.

That last one is not hypothetical. Touching NSStatusBar off the main thread took the whole app down
with a Trace/BPT trap and no traceback, twice, while this was being written.

Needs a window server, so it is not part of run_all.sh; run it directly:

    python3 tests/nowplaying_ui_check.py
"""
import threading, time, os, signal, AppKit
from harness import desktop as dt, plat
from harness import nowplaying_mac as np
LOG = os.environ.get("NOWPLAYING_LOG", "/tmp/collie-nowplaying-check.txt")
def log(m):
    with open(LOG, "a") as f: f.write(m + "\n"); f.flush()

class P:
    def __init__(self): self.killed = False
    def poll(self): return None
dt.resolve_audio = lambda *a, **k: {"ok": True, "url": "x", "title": "Taylor Swift - Cruel Summer"}
plat.play_stream = lambda u: P()
plat.stop_stream = lambda p: (setattr(p, "killed", True), True)[1] if p else False

app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

fails = []


def check(ok, what):
    log(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def work():
    time.sleep(2)
    r = dt.play_here("Cruel Summer")
    check(r.get("menubar") is True, "starting playback puts a control in the menu bar")
    time.sleep(1.5)
    it = np._state.get("item")
    title = it.button().title() if it else ""
    check(it is not None, "the status item exists")
    check("Cruel Summer" in title, "and names what is playing (%r)" % title)
    proc = dt._playing["proc"]

    # Clicking Stop — but reached from THIS thread, which is what makes it a real test: the same
    # callback runs when the phone says "stop the music", and that arrives on an HTTP worker.
    np._state["cb"]()
    time.sleep(1.5)
    check(proc.killed, "clicking Stop kills the player")
    check(np._state.get("item") is None, "and removes the item, so nothing playing means nothing shown")
    log("" if not fails else "%d FAILED" % len(fails))
    log("nowplaying: all green" if not fails else "nowplaying: %d FAILED" % len(fails))
    os.kill(os.getpid(), signal.SIGTERM)
threading.Thread(target=work, daemon=True).start()
app.run()
