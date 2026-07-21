"""Browser control — Backend 1: Playwright (public / general / CI web).

Backend 2 (a Chrome extension bridged to the user's REAL logged-in browser via
`fetch localhost`, for authenticated + bot-protected sites like Manheim/Akamai and
job portals — where Playwright gets fingerprinted/403'd) is specced in
docs/VSCODE_BROWSER.md and reuses the user's existing extension skeletons.

Optional dep: `playwright` (+ `playwright install chromium`). Tools register only
when it's importable and browser mode is enabled (opt-in, so normal coding runs
aren't bloated). One lazy headless session persists across tool calls in a run.
"""
from .tools import Tool

_S = {"pw": None, "browser": None, "page": None}


def _page():
    if _S["page"] is None:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        br = pw.chromium.launch(headless=True)
        _S.update(pw=pw, browser=br, page=br.new_page())
    return _S["page"]


def close_session():
    try:
        if _S["browser"]:
            _S["browser"].close()
        if _S["pw"]:
            _S["pw"].stop()
    except Exception:
        pass
    _S.update(pw=None, browser=None, page=None)


class BrowserNavigate(Tool):
    name, tier = "browser_navigate", "always"
    description = "Open a URL in the browser. Args: url."
    schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def run(self, args, ctx):
        # Only allow http(s). Playwright's goto() will happily load file:// (and other schemes),
        # which would let an injected instruction read local files (~/.ssh/id_rsa, .env, /etc/passwd)
        # via browser_read. Restrict the scheme to close that local-file / SSRF vector.
        import urllib.parse
        url = args.get("url") if isinstance(args.get("url"), str) else ""
        scheme = urllib.parse.urlparse(url).scheme.lower()
        if scheme not in ("http", "https"):
            return ("ERROR(browser_navigate): only http(s) URLs are allowed (refused %r) — "
                    "file:// and other schemes are blocked for local-file/SSRF safety."
                    % (scheme or "relative/none"))
        try:
            pg = _page()
            pg.goto(url, timeout=30000, wait_until="domcontentloaded")
            return "navigated to %s | title: %s" % (pg.url, pg.title())
        except Exception as e:
            return "ERROR(browser_navigate): %s" % e


class BrowserRead(Tool):
    name, tier = "browser_read", "always"
    description = "Read the current page's visible text (token-cheap). Args: optional max_chars."
    schema = {"type": "object", "properties": {"max_chars": {"type": "integer"}}}

    def run(self, args, ctx):
        try:
            txt = _page().inner_text("body")
            return txt[:int(args.get("max_chars", 4000))] or "(no visible text)"
        except Exception as e:
            return "ERROR(browser_read): %s" % e


class BrowserClick(Tool):
    name, tier = "browser_click", "always"
    description = "Click an element by CSS selector or text= selector. Args: selector."
    schema = {"type": "object", "properties": {"selector": {"type": "string"}}, "required": ["selector"]}

    def run(self, args, ctx):
        try:
            pg = _page()
            pg.click(args["selector"], timeout=10000)
            return "clicked %s | url: %s" % (args["selector"], pg.url)
        except Exception as e:
            return "ERROR(browser_click): %s" % e


class BrowserType(Tool):
    name, tier = "browser_type", "always"
    description = "Type text into an input by selector. Args: selector, text."
    schema = {"type": "object", "properties": {
        "selector": {"type": "string"}, "text": {"type": "string"}}, "required": ["selector", "text"]}

    def run(self, args, ctx):
        try:
            _page().fill(args["selector"], args["text"], timeout=10000)
            return "typed into %s" % args["selector"]
        except Exception as e:
            return "ERROR(browser_type): %s" % e


class BrowserScreenshot(Tool):
    name, tier = "browser_screenshot", "always"
    description = "Screenshot the current page to a PNG file. Args: optional path."
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}

    def run(self, args, ctx):
        import os
        path = args.get("path") or os.path.join(ctx.cwd, "screenshot.png")
        try:
            _page().screenshot(path=path, full_page=True)
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
