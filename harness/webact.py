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

    def __init__(self):
        from . import browserbridge as _bb
        self._bb = _bb

    def _cmd(self, cmd):
        r = self._bb._call(cmd)
        if isinstance(r, dict):
            if r.get("ok") is False:
                raise BrowserUnavailable(r.get("error") or "bridge command failed")
            if r.get("error"):                       # in-tab failure wrapped in ok:True
                raise RuntimeError("browser: %s" % r["error"])
            return r.get("data", r)                  # the extension returns page text/result in "data"
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
        self._cmd({"action": "click", "selector": selector})
        return getattr(self, "_url", "")

    def click_text(self, text: str) -> str:
        # click a button/link by its VISIBLE text (e.g. the "Publish" button) — the
        # gated irreversible action's single deterministic step.
        self._cmd({"action": "click", "text": text})
        return getattr(self, "_url", "")

    def read(self, max_chars: int = 2000) -> str:
        r = self._cmd({"action": "read"})
        return (r if isinstance(r, str) else str(r))[:max_chars]

    def current_url(self) -> str:
        return getattr(self, "_url", "")


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

    def read(self, max_chars=2000):
        self.calls.append(("read",))
        return self.page_text[:max_chars]

    def current_url(self):
        return self._url or self.result_url
