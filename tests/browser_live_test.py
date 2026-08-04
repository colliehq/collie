"""The browser tools against a REAL Chrome — the checks stubs cannot make.

Everything the extension does that matters happens inside a browser: whether CDP input reaches a tab
the user is not looking at, whether a cross-origin iframe can be read at all, whether a click really
landed. Stubs answered "yes" to all three while the real browser answered "no" — this file exists so
that gap has somewhere to be caught.

It is OPT-IN and self-contained: it serves its own two origins (127.0.0.1 embeds localhost, which is
cross-SITE, so Chrome puts the child frame out of process — a real OOPIF, not a simulation), works in
its own space so it never touches a tab anyone else is using, and closes what it opened.

    COLLIE_BROWSER_LIVE=1 python tests/browser_live_test.py

Without that variable, or with no extension connected, it SKIPS rather than fails: a machine with no
browser bridge has nothing to say about any of this.
"""
import http.server
import json
import os
import re
import socket
import sys
import threading
import urllib.request

PORT = int(os.environ.get("COLLIE_BROWSER_BRIDGE_PORT", "8677"))
SPACE = "collie-live-test"
_fails = []
_ran = []

PARENT_HTML = """<!doctype html>
<meta charset="utf-8"><title>collie live test</title>
<h1>Parent page</h1>
<main>
  <p>Order total is $42.00</p>
  <button id="p1">Parent button</button>
  <input id="pin" aria-label="Parent field">
  <iframe src="http://localhost:%d/child.html" width="420" height="220"></iframe>
</main>
<div id="log">parent-untouched</div>

<!-- targets for the input actions -->
<div id="keylog">no-key</div>
<div id="menu" style="width:120px;height:30px;background:#eee">Menu
  <div id="submenu" style="display:none">Submenu item</div>
</div>
<div id="src" draggable="true" style="width:80px;height:30px;background:#cde">drag me</div>
<div id="drop" style="width:120px;height:40px;background:#dfd">drop-empty</div>
<div id="track" style="width:200px;height:40px;background:#fdd">track-idle</div>
<div id="box" style="position:absolute;left:600px;top:400px;width:60px;height:40px;background:#ffd">box</div>
<div id="boxlog">box-unclicked</div>

<script>
document.getElementById('p1').addEventListener('click', function (e) {
  document.getElementById('log').textContent = 'parent-clicked:' + e.isTrusted;
});

// keys: record what arrived, and whether the browser considered it real
document.addEventListener('keydown', function (e) {
  document.getElementById('keylog').textContent =
    'key:' + e.key + ':ctrl=' + e.ctrlKey + ':trusted=' + e.isTrusted;
});

// a menu that only opens on hover — the shape a huge share of site navigation has
document.getElementById('menu').addEventListener('mouseover', function () {
  document.getElementById('submenu').style.display = 'block';
});

// HTML5 drag and drop: only real drag events drive this, never mouse movement
var drop = document.getElementById('drop');
drop.addEventListener('dragover', function (e) { e.preventDefault(); });
drop.addEventListener('drop', function (e) {
  e.preventDefault();
  drop.textContent = 'drop-received';
});

// pointer drag: press, move, release — what a sortable list or a slider tracks
var track = document.getElementById('track'), down = false, moves = 0;
track.addEventListener('mousedown', function () { down = true; moves = 0; });
document.addEventListener('mousemove', function () { if (down) moves++; });
document.addEventListener('mouseup', function () {
  if (down) { down = false; track.textContent = 'track-dragged:' + (moves > 3); }
});

// a target with no element of its own to name — reached only by coordinates
document.getElementById('box').addEventListener('click', function (e) {
  document.getElementById('boxlog').textContent = 'box-clicked:' + e.isTrusted;
});
</script>
"""

CHILD_HTML = """<!doctype html>
<meta charset="utf-8"><title>child frame</title>
<h1>Payment frame</h1>
<input id="card" aria-label="Card number">
<button id="pay">Pay in frame</button>
<div id="out">frame-untouched</div>
<script>
document.getElementById('pay').addEventListener('click', function (e) {
  document.getElementById('out').textContent = 'frame-clicked:' + e.isTrusted;
});
</script>
"""


def check(cond, msg, detail=""):
    print(("  PASS " if cond else "  FAIL ") + msg)
    _ran.append(msg)
    if not cond:
        _fails.append(msg)
        if detail:
            print("        " + str(detail)[:500].replace("\n", "\n        "))


def serve(pages):
    """A tiny in-process server for one origin. Threaded so several can run at once."""
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = pages.get(self.path.split("?")[0])
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            b = body.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    # Port 0 = let the OS hand out one it will actually let us keep. Picking a free port first and
    # binding it afterwards fails on Windows with WinError 10013 whenever the pick lands in a range
    # Hyper-V has reserved — a flake that looks nothing like its cause.
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], srv


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness import browserbridge as bb   # noqa: E402  (for the machine's bridge token)


def call(cmd, timeout=60):
    body = json.dumps(dict(cmd, space=SPACE, timeout=timeout)).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/enqueue" % PORT, data=body,
                                 headers={"content-type": "application/json",
                                          "X-Collie-Bridge": "1",
                                          "Authorization": "Bearer " + bb.token()})
    with urllib.request.urlopen(req, timeout=timeout + 5) as r:
        return json.loads(r.read()).get("data")


def ev(expr):
    r = call({"action": "eval", "expr": expr})
    return r.get("value") if isinstance(r, dict) else r


def refs_for(snapshot, label):
    out = []
    for line in (snapshot or "").split("\n"):
        if label in line:
            m = re.search(r"\[([a-z0-9]+)\]", line)
            if m:
                out.append(m.group(1))
    return out


def connected():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT, timeout=3) as r:
            return bool(json.loads(r.read()).get("extension_connected"))
    except Exception:
        return False


def main():
    if os.environ.get("COLLIE_BROWSER_LIVE") != "1":
        print("  SKIP live browser suite (set COLLIE_BROWSER_LIVE=1 to run it)")
        return 0
    if not connected():
        print("  SKIP live browser suite (no extension connected on :%d)" % PORT)
        return 0

    child_port, child_srv = serve({"/child.html": CHILD_HTML})
    parent_port, parent_srv = serve({"/parent.html": PARENT_HTML % child_port})
    parent_url = "http://127.0.0.1:%d/parent.html" % parent_port
    try:
        call({"action": "open", "url": parent_url})

        # --- the tab collie works in is its OWN, and it is NOT the one you are looking at ---------
        spaces = call({"action": "spaces"})
        mine = [s for s in (spaces.get("spaces") or []) if s["space"] == SPACE]
        check(len(mine) == 1 and mine[0]["owned"] is True,
              "collie works in a tab it opened itself", spaces)
        background = bool(mine) and mine[0].get("active") is False
        check(background, "and that tab is in the BACKGROUND for this whole test", mine)

        # --- trusted input must reach it anyway (this is what focus emulation buys) ---------------
        ev("document.getElementById('pin').value=''; document.getElementById('log')"
           ".textContent='parent-untouched'; 1")
        snap = call({"action": "snapshot", "max": 200}) or {}
        field = refs_for(snap.get("snapshot"), "Parent field")
        button = refs_for(snap.get("snapshot"), "Parent button")
        if field:
            typed = call({"action": "type", "ref": field[0], "text": "typed-in-background"})
            check(typed.get("landed") is True,
                  "a trusted type lands in a background tab", typed)
            check(typed.get("trusted") is True, "and it really was the trusted path", typed)
        if button:
            call({"action": "click", "ref": button[0]})
            log = str(ev("document.getElementById('log').textContent"))
            check(log.startswith("parent-clicked"), "a trusted click lands in a background tab", log)
            check(log.endswith(":true"), "and the page saw isTrusted=true", log)
        after = call({"action": "spaces"})
        still = [s for s in (after.get("spaces") or []) if s["space"] == SPACE]
        check(bool(still) and still[0].get("active") is False,
              "and collie never had to steal the tab you were on", after)

        # --- cross-origin iframe ------------------------------------------------------------------
        plain = call({"action": "snapshot", "max": 200}) or {}
        check("cross-origin" in (plain.get("snapshot") or ""),
              "an unreadable cross-origin iframe is reported, not skipped", plain.get("snapshot"))
        deep = call({"action": "snapshot", "max": 200, "frames": True}, timeout=90) or {}
        ds = deep.get("snapshot") or ""
        check("frames_error" not in deep, "frames=true reaches into it", deep.get("frames_error"))
        check("Pay in frame" in ds, "the frame's controls become visible", ds)
        pay = refs_for(ds, "Pay in frame")
        check(bool(pay) and pay[0].startswith("f"), "with frame-tagged refs", pay)
        if pay:
            r = call({"action": "click", "ref": pay[0]}, timeout=90)
            inner = (r or {}).get("click", r) if isinstance(r, dict) else {}
            deep2 = call({"action": "snapshot", "max": 200, "frames": True, "text": True},
                         timeout=90) or {}
            check("frame-clicked" in (deep2.get("snapshot") or ""),
                  "a click on a frame ref really lands inside the frame", deep2.get("snapshot"))
            check("frame-clicked:true" in (deep2.get("snapshot") or ""),
                  "and the frame saw isTrusted=true", deep2.get("snapshot"))
            check(inner.get("trusted") is True, "reported as trusted", inner)

        # --- batching ------------------------------------------------------------------------------
        if field and button:
            ev("document.getElementById('log').textContent='parent-untouched'; 1")
            snap = call({"action": "snapshot", "max": 200}) or {}
            f2 = refs_for(snap.get("snapshot"), "Parent field")
            b2 = refs_for(snap.get("snapshot"), "Parent button")
            script = call({"action": "script", "steps": [
                {"action": "type", "ref": f2[0], "text": "by script"},
                {"action": "click", "ref": b2[0]},
                {"action": "wait_for", "text": "parent-clicked", "timeout_ms": 5000},
                {"action": "read"},
            ]}, timeout=120) or {}
            check(script.get("ok") is True and script.get("ran") == 4,
                  "a four-step script runs to the end in one round trip", script)
            check("parent-clicked" in str(script.get("result", "")),
                  "and the last step returns the real page", str(script.get("result"))[:200])

            bad = call({"action": "script", "steps": [
                {"action": "click", "ref": "e9999"},
                {"action": "type", "ref": f2[0], "text": "must-not-happen"},
            ]}, timeout=60) or {}
            check(bad.get("ok") is False and bad.get("ran") == 1,
                  "a failing step stops the script", bad)
            check("must-not-happen" not in str(ev("document.getElementById('pin').value")),
                  "and the steps after it really did not run")

        # --- keys ---------------------------------------------------------------------------------
        r = call({"action": "press", "key": "Escape"}) or {}
        log = str(ev("document.getElementById('keylog').textContent"))
        check(log.startswith("key:Escape"), "a named key reaches the page", log)
        check(log.endswith("trusted=true"), "and the page saw a REAL key press", log)
        check(r.get("trusted") is True, "reported as trusted", r)

        # The modifier trap: Ctrl+A must SELECT, not type the letter a into the field.
        snap = call({"action": "snapshot", "max": 200}) or {}
        f3 = refs_for(snap.get("snapshot"), "Parent field")
        if f3:
            call({"action": "type", "ref": f3[0], "text": "abc"})
            call({"action": "press", "key": "a", "modifiers": ["ctrl"]})
            val = str(ev("document.getElementById('pin').value"))
            check(val == "abc", "ctrl+a does not TYPE an 'a' (it still reads %r)" % val)
            klog = str(ev("document.getElementById('keylog').textContent"))
            check(":ctrl=true" in klog, "and the page saw the modifier held", klog)

        r = call({"action": "press", "key": "ArrowDown", "repeat": 3}) or {}
        check(r.get("times") == 3, "repeat presses the key that many times", r)
        bad = call({"action": "press", "key": "Ctrl+Shift+Whatever"}) or {}
        check(bool(bad.get("error")), "an unknown key is refused, not silently dropped", bad)

        # --- hover --------------------------------------------------------------------------------
        check(str(ev("getComputedStyle(document.getElementById('submenu')).display")) == "none",
              "the hover menu starts closed")
        call({"action": "hover", "text": "Menu"})
        check(str(ev("getComputedStyle(document.getElementById('submenu')).display")) == "block",
              "hovering opens it — reachable without a click")

        # --- drag: both mechanisms ----------------------------------------------------------------
        snap = call({"action": "snapshot", "max": 300}) or {}
        check(bool(refs_for(snap.get("snapshot"), "drag me")),
              "a draggable element gets a ref, so a board has handles to grab",
              snap.get("snapshot"))
        r = call({"action": "drag",
                  "from": {"selector": "#src"}, "to": {"selector": "#drop"}}, timeout=90) or {}
        check(str(ev("document.getElementById('drop').textContent")) == "drop-received",
              "an HTML5 drag-and-drop target receives the drop", r)
        check(r.get("dragged") == "html5", "and it used the HTML5 path, not mouse movement", r)

        r = call({"action": "drag",
                  "from": {"selector": "#track"}, "to": {"x": 900, "y": 250}}, timeout=90) or {}
        tr = str(ev("document.getElementById('track').textContent"))
        check(tr.startswith("track-dragged"), "a pointer drag presses, moves and releases", tr)
        check(tr.endswith(":true"), "with real intermediate movement, not one jump", tr)

        # --- a point with no element to name ------------------------------------------------------
        pos = json.loads(str(ev(
            "JSON.stringify((function(){var r=document.getElementById('box')"
            ".getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})())")))
        r = call({"action": "click", "x": pos["x"], "y": pos["y"]}, timeout=90) or {}
        inner = r.get("click", r) if isinstance(r, dict) else {}
        blog = str(ev("document.getElementById('boxlog').textContent"))
        check(blog.startswith("box-clicked"), "clicking a bare coordinate lands", blog)
        check(blog.endswith(":true"), "as a real click", blog)
        check((inner.get("hit") or {}).get("id") == "box",
              "and the result says WHAT was under the point", inner.get("hit"))

        # --- all of it available inside one script ------------------------------------------------
        ev("document.getElementById('keylog').textContent='no-key'; "
           "document.getElementById('submenu').style.display='none'; 1")
        sc = call({"action": "script", "steps": [
            {"action": "hover", "text": "Menu"},
            {"action": "press", "key": "Escape"},
            {"action": "read"},
        ]}, timeout=120) or {}
        check(sc.get("ok") is True and sc.get("ran") == 3,
              "hover and press work as script steps too", sc)

        rel = call({"action": "release", "close": True}) or {}
        check(rel.get("closed") is True, "releasing closes the tab collie opened", rel)
    finally:
        try:
            call({"action": "release", "close": True})
        except Exception:
            pass
        parent_srv.shutdown()
        child_srv.shutdown()

    print("\n== live browser: %d/%d checks passed ==%s"
          % (len(_ran) - len(_fails), len(_ran),
             "" if not _fails else " FAILS: " + "; ".join(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
