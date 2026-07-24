"""Browser control — Backend 1: Playwright (public / general / CI web).

IMPORTANT, and stated in every tool description: this is a FRESH, ISOLATED, LOGGED-OUT browser. It
has none of the user's cookies, sessions or logins, so it can never read "my account" pages. The
user's REAL logged-in browser is Backend 2 — the Chrome extension + `collie browser-bridge`
(harness/browserbridge.py), which auto-registers whenever the bridge is live. Saying so in the tool
descriptions matters: with a vague "Open a URL in the browser" the model happily tries to check a
user's insurance/bank account here, fails on a login wall, and reports a confusing "not logged in".

Optional dep: `playwright` (+ `playwright install chromium`). Tools register only when it's
importable and browser mode is enabled (opt-in, so normal coding runs aren't bloated).

THREADING: playwright's sync API is thread-affine — the session may only be touched from the thread
that created it. collie's web GUI is a threading HTTP server, so a session created on one request
thread blew up on the next with "cannot switch to a different thread (which happens to have
exited)". Everything therefore runs on ONE dedicated owner thread via _call().
"""
import queue
import threading

from .tools import Tool

_S = {"pw": None, "browser": None, "page": None}
_Q = None                       # work queue to the owner thread
_OWNER = None                   # the single thread allowed to touch playwright

_LOGGED_OUT = (" NOTE: this is a fresh, isolated, LOGGED-OUT browser (no cookies/sessions) — it "
               "cannot open the user's accounts. For logged-in sites use the collie browser bridge "
               "(the user's real browser).")


def _owner_loop():
    while True:
        fn, box, done = _Q.get()
        try:
            box["r"] = fn()
        except Exception as e:                      # marshalled back to the caller thread
            box["e"] = e
        finally:
            done.set()


def _call(fn, timeout=120):
    """Run fn() on the one playwright-owning thread and return its result (or re-raise)."""
    global _Q, _OWNER
    if _OWNER is None or not _OWNER.is_alive():
        _Q = queue.Queue()
        _OWNER = threading.Thread(target=_owner_loop, name="collie-playwright", daemon=True)
        _OWNER.start()
    box, done = {}, threading.Event()
    _Q.put((fn, box, done))
    if not done.wait(timeout):
        raise TimeoutError("browser call timed out after %ds" % timeout)
    if "e" in box:
        raise box["e"]
    return box.get("r")


def _page():
    if _S["page"] is None:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        br = pw.chromium.launch(headless=True)
        _S.update(pw=pw, browser=br, page=br.new_page())
    return _S["page"]


def close_session():
    def _shut():
        try:
            if _S["browser"]:
                _S["browser"].close()
            if _S["pw"]:
                _S["pw"].stop()
        except Exception:
            pass
        _S.update(pw=None, browser=None, page=None)
    try:
        _call(_shut, timeout=20)
    except Exception:
        _S.update(pw=None, browser=None, page=None)


class BrowserNavigate(Tool):
    name, tier = "browser_navigate", "always"
    description = "Open a URL in a scratch browser. Args: url." + _LOGGED_OUT
    schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def run(self, args, ctx):
        # Only allow http(s). Playwright's goto() will happily load file:// (and other schemes),
        # which would let an injected instruction read local files (~/.ssh/id_rsa, .env, /etc/passwd)
        # via browser_read. Restrict the scheme to close that local-file / SSRF vector.
        url = str(args.get("url", ""))
        if not url.lower().startswith(("http://", "https://")):
            return "ERROR(browser_navigate): only http(s) URLs are allowed"

        def _go():
            pg = _page()
            pg.goto(url, timeout=30000, wait_until="domcontentloaded")
            return "navigated to %s | title: %s" % (pg.url, pg.title())
        try:
            return _call(_go)
        except Exception as e:
            return "ERROR(browser_navigate): %s" % e


class BrowserRead(Tool):
    name, tier = "browser_read", "always"
    description = ("Read the current scratch-browser page's visible text (token-cheap). Args: "
                   "optional max_chars." + _LOGGED_OUT)
    schema = {"type": "object", "properties": {"max_chars": {"type": "integer"}}}

    def run(self, args, ctx):
        try:
            txt = _call(lambda: _page().inner_text("body"))
            return txt[:int(args.get("max_chars", 4000))] or "(no visible text)"
        except Exception as e:
            return "ERROR(browser_read): %s" % e


class BrowserClick(Tool):
    name, tier = "browser_click", "always"
    description = ("Click an element by CSS selector or text= selector in the scratch browser. "
                   "Args: selector." + _LOGGED_OUT)
    schema = {"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]}

    def run(self, args, ctx):
        def _click():
            pg = _page()
            pg.click(args["selector"], timeout=10000)
            return "clicked %s | url: %s" % (args["selector"], pg.url)
        try:
            return _call(_click)
        except Exception as e:
            return "ERROR(browser_click): %s" % e


class BrowserType(Tool):
    name, tier = "browser_type", "always"
    description = ("Type text into an input by selector in the scratch browser. Args: selector, "
                   "text." + _LOGGED_OUT)
    schema = {"type": "object", "properties": {
        "selector": {"type": "string"}, "text": {"type": "string"}}, "required": ["selector", "text"]}

    def run(self, args, ctx):
        try:
            _call(lambda: _page().fill(args["selector"], args["text"], timeout=10000))
            return "typed into %s" % args["selector"]
        except Exception as e:
            return "ERROR(browser_type): %s" % e


class BrowserScreenshot(Tool):
    name, tier = "browser_screenshot", "always"
    description = ("Screenshot the current scratch-browser page to a PNG file. Args: optional "
                   "path." + _LOGGED_OUT)
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}

    def run(self, args, ctx):
        import os
        path = args.get("path") or os.path.join(ctx.cwd, "screenshot.png")
        try:
            _call(lambda: _page().screenshot(path=path, full_page=True))
            return "saved screenshot to %s" % path
        except Exception as e:
            return "ERROR(browser_screenshot): %s" % e


def register_browser(registry) -> bool:
    """Register Playwright browser tools if available. Opt-in (call explicitly)."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    for t in (BrowserNavigate(), BrowserRead(), BrowserClick(),
              BrowserType(), BrowserScreenshot()):
        registry.register(t)
    return True
