"""Browser actuator for the world primitives (web.submit / web.send / observe).

The RIGHT actuator is collie's OWN bridge (harness/browserbridge.py, Backend 2):
`browserbridge._call()` sends open/read/click/type commands to the bridge server,
which a Chrome extension in the user's REAL, logged-in browser executes — so an
errand runs against the user's actual Facebook / Gmail / marketplace session. That
is the only way to act on authenticated sites; a fresh Playwright browser has no
login and gets a login wall.

So `get_actuator()` returns a **BridgeActuator** when the bridge is live (an
extension is connected), and **None** otherwise — we degrade to a clean "no
browser" verdict rather than silently driving a logged-out sandbox that can't do
the real task. To make the bridge live: `collie browser-bridge` + load
harness/browser_ext/ in your Chrome (or `collie browser-bridge --browser` for a
managed browser with the extension pre-loaded, for dev/CI without a login).

Tests inject a FakeActuator and never touch a browser.
"""

from __future__ import annotations


class BrowserUnavailable(RuntimeError):
    """No browser backend is available (no bridge / extension connected)."""


class BridgeActuator:
    """Drive the user's real browser through collie's bridge (browserbridge._call).
    open/type/click/read map 1:1 to the bridge's command vocabulary — the exact
    commands the browser_* tools already use, so nothing new is invented here."""

    def __init__(self, space=""):
        from . import browserbridge as _bb
        self._bb = _bb
        self._space = (space or "")[:40]

    def _cmd(self, cmd):
        cmd = dict(cmd)
        if self._space:
            cmd["space"] = self._space
        r = self._bb._call(cmd)
        if isinstance(r, dict):
            if r.get("ok") is False:
                raise BrowserUnavailable(r.get("error") or "bridge command failed")
            if r.get("error"):                       # in-tab failure wrapped in ok:True
                raise RuntimeError("browser: %s" % r["error"])
            data = r.get("data", r)                  # extension payload lives in "data"
            if isinstance(data, dict):
                if data.get("error"):
                    raise RuntimeError("browser: %s" % data["error"])
                # click returns {click: {clicked|error}, page: ...}.  An outer
                # ok:true only means the bridge round-trip worked; it must not
                # turn a detached ref into a successful publish.
                click = data.get("click")
                if isinstance(click, dict) and click.get("error"):
                    raise RuntimeError("browser: %s" % click["error"])
            return data
        if isinstance(r, str) and r.startswith("ERROR(browser)"):
            raise RuntimeError(r)
        return r

    def open(self, url: str) -> str:
        self._cmd({"action": "open", "url": url})
        self._url = url
        return url

    def type(self, selector: str, text: str, submit: bool = False) -> bool:
        self._cmd({"action": "type", "selector": selector, "text": text or "", "submit": submit})
        return True

    def click(self, selector: str) -> str:
        # the bridge click matches by visible text OR css selector; it returns the
        # resulting page text, not a URL, so callers verify by re-observing.
        self._cmd({"action": "click", "selector": selector, "trusted": False})
        return getattr(self, "_url", "")

    def click_text(self, text: str) -> str:
        # click a button/link by its VISIBLE text (e.g. the "Publish" button) — the
        # gated irreversible action's single deterministic step.
        self._cmd({"action": "click", "text": text, "trusted": False})
        return getattr(self, "_url", "")

    def click_ref(self, ref: str) -> str:
        # Synthetic element.click targets the exact snapshotted live node.  The
        # bridge's optional trusted path converts a ref to screen coordinates and
        # can hit a different element if layout shifts during its cursor delay —
        # unacceptable for the final irreversible Mission boundary.
        self._cmd({"action": "click", "ref": ref, "trusted": False})
        return getattr(self, "_url", "")

    def snapshot(self):
        r = self._cmd({"action": "snapshot", "max": 400, "text": False})
        return r if isinstance(r, dict) else {}

    def eval(self, expr):
        return self._cmd({"action": "eval", "expr": expr})

    def read(self, max_chars: int = 2000) -> str:
        r = self._cmd({"action": "read"})
        return (r if isinstance(r, str) else str(r))[:max_chars]

    def current_url(self) -> str:
        ident = self.page_identity()
        return ident.get("url") or getattr(self, "_url", "")

    def for_space(self, space):
        return BridgeActuator(space)

    def page_identity(self):
        return self._bb.space_identity(self._space or self._bb._space())


def bridge_live() -> bool:
    """True iff collie's browser bridge is up AND an extension is connected."""
    try:
        from . import browserbridge as _bb
        return _bb._bridge_live()
    except Exception:
        return False


def get_actuator():
    """A live actuator IFF the bridge (the user's real browser) is connected, else
    None. We do NOT fall back to a logged-out Playwright browser — a real errand on
    an authenticated site needs the real session; without it, degrade honestly."""
    return BridgeActuator() if bridge_live() else None


class FakeActuator:
    """Test double: records the drive steps and returns a canned result URL, so the
    submit/send primitives can be proven without a real browser."""

    def __init__(self, result_url: str = "https://example.test/item/123", page_text: str = ""):
        self.calls = []
        self.result_url = result_url
        self.page_text = page_text
        self._url = ""

    def open(self, url):
        self.calls.append(("open", url))
        self._url = url
        return url

    def type(self, selector, text, submit=False):
        self.calls.append(("type", selector, text))
        return True

    def click(self, selector):
        self.calls.append(("click", selector))
        self._url = self.result_url
        return self.result_url

    def click_text(self, text):
        self.calls.append(("click_text", text))
        self._url = self.result_url
        return self.result_url

    def click_ref(self, ref):
        self.calls.append(("click_ref", ref))
        self._url = self.result_url
        return self.result_url

    def snapshot(self):
        self.calls.append(("snapshot",))
        clicked = any(c[0] == "click_ref" for c in self.calls)
        body = (self.page_text or "(no interactive elements found)") if clicked \
            else '[e1] button "Publish"'
        return {"url": self.current_url(), "snapshot": body,
                "count": 1}

    def eval(self, expr):
        self.calls.append(("eval",))
        return "[]"

    def read(self, max_chars=2000):
        self.calls.append(("read",))
        return self.page_text[:max_chars]

    def current_url(self):
        return self._url or self.result_url

    def for_space(self, space):
        self.space = space
        return self

    def page_identity(self):
        from urllib.parse import urlsplit
        url = self.current_url()
        return {"space": getattr(self, "space", "default"), "tab_id": 1,
                "title": self.page_text[:60], "url": url,
                "origin": "%s://%s" % (urlsplit(url).scheme, urlsplit(url).netloc)
                if url else ""}
