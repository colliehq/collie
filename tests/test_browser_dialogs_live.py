"""Page dialogs against a REAL Chromium running the shipped extension — what stubs cannot show.

The extension's dialog handling lives in the service worker and in Chrome's own dialog machinery;
the only honest check is a browser. This starts one with nothing else driving it (Playwright is used
only to find its Chromium: a Playwright-driven page auto-dismisses dialogs and would hide the very
thing under test), points a copy of the extension at a bridge on a spare port, and drives it through
the real tool classes.

Before the fix, the first alert() froze the bridge: every later command in every space, including
opening a new tab, waited its full 60 seconds.

    COLLIE_BROWSER_LIVE=1 python -m pytest tests/test_browser_dialogs_live.py -q
"""
import http.server
import os
import shutil
import socket
import subprocess
import threading
import time

import pytest

from harness import browserbridge as bb
from harness.httpserver import ThreadingHTTPServer

pytestmark = pytest.mark.skipif(os.environ.get("COLLIE_BROWSER_LIVE") != "1",
                                reason="starts a real Chromium; set COLLIE_BROWSER_LIVE=1")

PAGE = """<!doctype html><meta charset=utf-8><title>dialog test</title>
<button id=a onclick="alert('Saved! Your submission id is 42'); log('after-alert')">Save</button>
<button id=c onclick="log('confirm:' + confirm('Delete this post?'))">Delete</button>
<button id=p onclick="log('prompt:' + prompt('Your name?', 'anon'))">Name</button>
<button id=l onclick="setTimeout(function(){ alert('Upload finished'); log('late-alert-done'); }, 1500)">Later</button>
<button id=lc onclick="setTimeout(function(){ log('late-confirm:' + confirm('Confirm payment of $500?')); }, 1500)">Pay later</button>
<button id=n onclick="log('noop-clicked')">Noop</button>
<button id=u onclick="window.__dirty=1; log('dirty')">Make dirty</button>
<div id=log>untouched</div>
<script>
function log(t) { document.getElementById('log').textContent = t; }
window.addEventListener('beforeunload', function (e) { if (window.__dirty) { e.preventDefault(); e.returnValue = ''; } });
</script>"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Pages(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = (PAGE if self.path == "/page" else "<title>other</title>other page").encode()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def browser(tmp_path, monkeypatch):
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            chrome = p.chromium.executable_path
    except Exception as exc:                      # pragma: no cover - depends on the machine
        pytest.skip("no Playwright Chromium: %s" % exc)
    if not chrome or not os.path.exists(chrome):
        pytest.skip("no Playwright Chromium at %r" % chrome)
    monkeypatch.setenv("COLLIE_BRIDGE_DANGEROUSLY_OMIT_AUTH", "1")
    monkeypatch.setenv("COLLIE_BROWSER_BRIDGE_NOSPAWN", "1")
    monkeypatch.setenv("COLLIE_NO_APPLE_EVENTS", "1")
    monkeypatch.setattr(bb, "_home", lambda: str(tmp_path))
    monkeypatch.setattr(bb, "_CURRENT_SPACE", ["dialogs"])
    bridge_port, page_port = _free_port(), _free_port()
    monkeypatch.setenv("COLLIE_BROWSER_BRIDGE_PORT", str(bridge_port))
    ext = tmp_path / "ext"
    shutil.copytree(os.path.join(os.path.dirname(bb.__file__), "browser_ext"), ext)
    (ext / "token.txt").unlink(missing_ok=True)
    background = ext / "background.js"
    source = background.read_text(encoding="utf-8")
    assert 'const BRIDGE = "http://127.0.0.1:8677";' in source
    background.write_text(source.replace("127.0.0.1:8677", "127.0.0.1:%d" % bridge_port),
                          encoding="utf-8")
    bridge = bb._Bridge()
    servers = [ThreadingHTTPServer(("127.0.0.1", bridge_port), bb._handler(bridge)),
               ThreadingHTTPServer(("127.0.0.1", page_port), _Pages)]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--user-data-dir=" + str(tmp_path / "profile"),
         "--no-first-run", "--no-default-browser-check", "--disable-extensions-except=" + str(ext),
         "--load-extension=" + str(ext), "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 30
        while not bridge.last_poll and time.time() < deadline:
            time.sleep(0.2)
        if not bridge.last_poll:
            pytest.skip("the extension never connected in this Chromium")
        tools = {}
        for cls in (bb.BrowserOpen, bb.BrowserClick, bb.BrowserEval, bb.BrowserRead):
            tool = bb._with_dialog_notes(cls())
            tools[tool.name] = tool
        yield tools, "http://127.0.0.1:%d" % page_port
    finally:
        proc.kill()
        for server in servers:
            server.shutdown()
            server.server_close()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


def test_dialogs_are_answered_and_reported_without_freezing_the_bridge(browser):
    tools, base = browser
    took = []

    def run(name, args):
        start = time.monotonic()
        out = tools[name].run(args, None)
        took.append((name, args, time.monotonic() - start))
        return out

    def page_log():
        return run("browser_eval", {"expr": "document.getElementById('log').textContent"})

    assert "untouched" in run("browser_open", {"url": base + "/page"})

    out = run("browser_click", {"selector": "#a"})
    assert "an alert: acknowledged" in out and "after-alert" in out
    assert "Saved! Your submission id is 42" in out.split(bb._FENCE_HEAD, 1)[1]

    out = run("browser_click", {"selector": "#c"})
    assert "a confirm box: answered Cancel" in out and "confirm:false" in out

    out = run("browser_click", {"selector": "#c", "dialog": "accept"})
    assert "a confirm box: answered OK" in out and "confirm:true" in out

    out = run("browser_click", {"selector": "#p", "dialog": "accept"})
    assert "a prompt box: answered OK" in out and "prompt:anon" in out, "OK keeps the page's default"

    run("browser_click", {"selector": "#l"})
    time.sleep(2.5)                                   # the alert comes up after the click returned
    out = run("browser_read", {})
    assert "an alert: acknowledged" in out and "Upload finished" in out
    assert "late-alert-done" in page_log()

    # A confirm left over from an earlier click is never accepted by a later action.
    run("browser_click", {"selector": "#lc"})
    time.sleep(2.5)
    out = run("browser_click", {"selector": "#n", "dialog": "accept"})
    assert "came up after the previous action had returned" in out
    assert "Confirm payment of $500?" in out and "noop-clicked" in out
    run("browser_click", {"selector": "#lc"})
    time.sleep(2.5)
    out = run("browser_open", {"url": base + "/page", "dialog": "accept"})
    assert "came up after the previous action had returned" in out

    run("browser_click", {"selector": "#u"})
    out = run("browser_open", {"url": base + "/other"})
    assert "stayed on the page" in out and "dirty" in out
    out = run("browser_open", {"url": base + "/other", "dialog": "accept"})
    assert "left the page" in out and "other page" in out

    slow = [t for t in took if t[2] > 12]
    assert not slow, "a dialog held the bridge up: %r" % slow


def test_a_box_left_on_another_spaces_tab_is_cancelled_and_reported_to_that_space(browser):
    """Moving the debugger to another space's tab used to strand a leftover box on the first
    tab: never answered, and reported to nobody."""
    tools, base = browser
    bb._CURRENT_SPACE[0] = "space-a"
    assert "untouched" in tools["browser_open"].run({"url": base + "/page"}, None)
    tools["browser_click"].run({"selector": "#lc"}, None)     # confirm comes up 1.5 s later
    time.sleep(2.5)
    bb._CURRENT_SPACE[0] = "space-b"
    tools["browser_open"].run({"url": base + "/page"}, None)
    t0 = time.monotonic()
    out_b = tools["browser_click"].run({"selector": "#n"}, None)
    assert time.monotonic() - t0 < 12 and "noop-clicked" in out_b
    assert "Confirm payment" not in out_b, "space B is not told about space A's box"
    bb._CURRENT_SPACE[0] = "space-a"
    out_a = tools["browser_read"].run({}, None)
    assert "Confirm payment of $500?" in out_a and "came up after the previous action" in out_a
    assert "late-confirm:false" in out_a
