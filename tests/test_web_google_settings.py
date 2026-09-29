"""Settings → Connections → Google, driven in Chromium against the staged HTTP fixture.

`/api/settings` and `/api/google` are answered by Playwright interception, so no account, token or
browser sign-in is involved. The `ui` fixture fails the test on any JavaScript error.
"""
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser   # noqa: F401

SCHEMA = [{"group": "General", "key": "LANG", "label": "Language", "type": "select",
           "default": "en", "options": [{"value": "en", "label": "English"}]}]
READ = "https://www.googleapis.com/auth/gmail.readonly"
COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
CAL = "https://www.googleapis.com/auth/calendar.readonly"


def _status(state, account="", scopes=()):
    return {"state": state, "message": "", "account": account, "granted_scopes": list(scopes),
            "missing_scopes": [s for s in (READ, COMPOSE, CAL) if s not in scopes],
            "can": {"gmail_read": READ in scopes, "gmail_drafts": COMPOSE in scopes,
                    "calendar_read": CAL in scopes},
            "client_source": "packaged", "storage": "dpapi", "connected_at": 1790705135}


class Staged:
    def __init__(self, page):
        self.page = page
        self.answer = {"status": _status("not_connected"),
                       "connect": {"busy": False, "error": "", "auth_url": ""}}
        self.posts = []

    def _google(self, route):
        if route.request.method == "POST":
            body = route.request.post_data_json or {}
            self.posts.append(body)
            if body.get("action") == "connect":
                self.answer["connect"] = {"busy": True, "error": "",
                                          "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?x=1"}
                route.fulfill(json={"ok": True, "started": True,
                                    "auth_url": self.answer["connect"]["auth_url"]})
                return
            route.fulfill(json={"ok": True})
            return
        assert "token=" in route.request.url, "the Google status read must carry the token"
        route.fulfill(json=self.answer)

    def open(self):
        page = self.page
        page.route("**/api/settings*", lambda route: route.fulfill(
            json={"schema": SCHEMA, "values": {"LANG": "en"}}))
        page.route("**/api/google*", self._google)
        page.route("**/api/mcp", lambda route: route.fulfill(json={"servers": [], "catalog": []}))
        page.click("#settingsBtn")
        page.click('.set-nav[data-cat="mcp"]')
        return page.locator("#googleBox")


def test_connections_shows_google_and_starts_the_sign_in(ui):
    staged = Staged(ui.page)
    box = staged.open()
    expect(box.locator(".mcp-name")).to_have_text("Google")
    expect(box.locator(".mcp-badge")).to_have_text("not connected")
    expect(box).to_contain_text("Gmail and Calendar for your morning report")
    box.locator('[data-google="connect"]').click()
    expect(box.locator("a[href^='https://accounts.google.com/']")).to_be_visible()
    expect(box.locator("button[disabled]")).to_contain_text("Waiting for you in the browser")
    assert staged.posts == [{"action": "connect"}]


def test_connections_shows_what_google_allowed(ui):
    staged = Staged(ui.page)
    staged.answer["status"] = _status("missing_scope", "owner@example.com", (READ, CAL))
    box = staged.open()
    expect(box.locator(".mcp-target")).to_have_text("owner@example.com")
    expect(box.locator(".mcp-badge.warn")).to_have_text("some access missing")
    hint = box.locator(".set-hint")
    expect(hint).to_contain_text("✓ Read mail")
    expect(hint).to_contain_text("✗ Write drafts")
    expect(hint).to_contain_text("Collie never sends mail.")
    expect(box.locator('[data-google="connect"]')).to_have_text("Reconnect")
    ui.page.on("dialog", lambda dialog: dialog.accept())
    box.locator('[data-google="disconnect"]').click()
    for _ in range(100):
        if staged.posts:
            break
        ui.page.wait_for_timeout(50)
    assert staged.posts == [{"action": "disconnect"}]


def test_a_dead_sign_in_says_to_reconnect(ui):
    staged = Staged(ui.page)
    staged.answer["status"] = _status("needs_reconnect", "owner@example.com", (READ, COMPOSE, CAL))
    box = staged.open()
    expect(box.locator(".mcp-err")).to_contain_text("Press Reconnect")
    expect(box.locator('[data-google="connect"]')).to_have_text("Reconnect")
