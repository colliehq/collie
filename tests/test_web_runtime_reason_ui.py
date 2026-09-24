"""Settings → General says why the runtime needs attention, and where to look.

The real index.html in Chromium over the fixture server shared with test_web_ui_run_status;
``/api/healthz`` is answered by interception with staged reason codes.
"""
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser  # noqa: F401
from test_web_settings_capabilities import Settings


class Runtime(Settings):
    def __init__(self, ui, health):
        super().__init__(ui)
        self.health = health

    def open(self):
        self.page.route("**/api/healthz*", lambda route: route.fulfill(json=self.health))
        return super().open()

    def general(self):
        self.page.click("#settingsBtn")
        self.page.click('.set-nav[data-cat="overview"]')
        return self.page.locator("#settingsRuntimeStatus")


def test_needs_attention_names_the_first_reason_and_how_many_more(ui):
    status = Runtime(ui, {"ok": False, "status": "degraded", "reasons": [
        {"code": "login_missing", "subject": "codex-oauth", "action": "run `codex login`"},
        {"code": "worker_not_reporting", "subject": "jobd"}]}).open().general()
    expect(status).to_contain_text(
        "Needs attention · codex-oauth is not signed in · run `codex login` (+1 more)")
    ui.page.locator("#settingsRuntimeDetails").click()
    expect(ui.page.locator("#setOverlay.open")).to_have_count(0)
    expect(ui.page.locator("#activityPanel")).to_be_visible()


def test_a_healthy_runtime_offers_no_details(ui):
    status = Runtime(ui, {"ok": True, "status": "ok", "reasons": []}).open().general()
    expect(status).to_have_text("Ready · runtime responding")
    expect(ui.page.locator("#settingsRuntimeDetails")).to_have_count(0)


def test_an_unknown_reason_code_falls_back_to_the_plain_notice(ui):
    status = Runtime(ui, {"ok": False, "status": "degraded",
                          "reasons": [{"code": "something_new", "subject": "x"}]}).open().general()
    expect(status).to_contain_text("Needs attention")
    expect(status).not_to_contain_text("something_new")
