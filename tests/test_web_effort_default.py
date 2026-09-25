"""The run menu names the reasoning effort an unchosen run will actually use.

An effort that is not chosen in the menu is sent as Auto, and the server resolves Auto to the
Settings "Default reasoning effort" (or the one frozen when a queued request was accepted). The menu
still said "Auto by task — Use the lowest reasoning level that fits" while a saved High ran every
time. These tests drive the real index.html and mobile.html in Chromium with staged settings and
capability answers, plus the real ``/api/run-capabilities`` route for the field itself. The value
sent with a run is deliberately unchanged: it stays Auto, so the server's frozen-default rule holds.
"""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from playwright.sync_api import expect

from harness import settings
from test_web_ui_run_status import ui, phone, server, browser  # noqa: F401
from test_web_settings_capabilities import Settings, SCHEMA, caps

EFFORT_ROW = [row for row in settings.SCHEMA if row["key"] == "REASONING_EFFORT"]


class EffortSettings(Settings):
    def __init__(self, ui, saved="high"):
        super().__init__(ui)
        self.values["REASONING_EFFORT"] = saved

    def _settings(self, route):
        if route.request.method != "POST":
            route.fulfill(json={"schema": SCHEMA + EFFORT_ROW, "values": dict(self.values)})
            return
        super()._settings(route)

    def _caps(self, route):
        self.reads.append(dict(self.values))
        payload = caps("Capabilities of the pair this page booted on",
                       efforts=("low", "medium", "high"))
        payload["effort_default"] = self.values["REASONING_EFFORT"]
        route.fulfill(json=payload)

    def auto_item(self):
        return self.page.locator('.mode-item[data-axis="effort"][data-val="auto"]')


def test_the_inherited_effort_names_the_saved_default(ui):
    s = EffortSettings(ui, saved="high").open()
    s.page.click("#modeTrigger")
    item = s.auto_item()
    expect(item.locator(".mi-name")).to_have_text("Saved default")
    expect(item.locator(".mi-desc")).to_have_text("Use the reasoning level selected in Settings: High")
    # What is sent is unchanged: Auto, resolved on the server under the frozen-default rule.
    assert s.page.input_value("#runEffort") == "auto"
    assert not s.page.evaluate("() => !!window.runExplicitAxes.effort")


def test_a_saved_auto_keeps_the_auto_description(ui):
    s = EffortSettings(ui, saved="auto").open()
    s.page.click("#modeTrigger")
    expect(s.auto_item().locator(".mi-name")).to_have_text("Auto by task")
    expect(s.auto_item().locator(".mi-desc")).to_have_text("Use the lowest reasoning level that fits")


def test_saving_the_default_effort_refreshes_the_menu_without_a_reload(ui):
    s = EffortSettings(ui, saved="auto").open()
    before = len(s.reads)
    s.open_panel()
    s.page.select_option("#set_REASONING_EFFORT", "medium")
    expect(s.status).to_have_class("set-status ok")
    assert s.posts == [{"REASONING_EFFORT": "medium"}]
    expect(s.auto_item().locator(".mi-desc")).to_have_text(
        "Use the reasoning level selected in Settings: Medium")
    assert len(s.reads) == before + 1
    assert not s.reloaded()


@pytest.mark.parametrize("saved,expected", [("high", "high"), ("auto", "auto"), ("extreme", "auto")])
def test_the_capability_route_reports_the_effort_an_unchosen_run_uses(monkeypatch, tmp_path,
                                                                       saved, expected):
    from harness import webapp
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLLIE_REASONING_EFFORT", saved)
    monkeypatch.setattr(webapp, "_provider", lambda: "mock")
    from harness import runner_registry
    monkeypatch.setattr(runner_registry, "probe_all", lambda keys: {})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = "http://127.0.0.1:%d/api/run-capabilities" % httpd.server_address[1]
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read())
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(timeout=3)
    assert payload["effort_default"] == expected


def test_the_phone_names_the_saved_default_in_both_languages(phone):
    page = phone.page
    page.route("**/api/run-capabilities*", lambda route: route.fulfill(json={
        "speed_tiers": ["standard"], "reasoning_efforts": ["low", "medium", "high"],
        "workers": [], "effort_default": "high"}))
    page.evaluate("() => refreshRunCapabilities()")
    auto = page.locator('#mEffort option[value="auto"]')
    expect(auto).to_have_text("Saved default · High")
    assert page.input_value("#mEffort") == "auto"
    page.evaluate("() => { UI_LANG = 'zh'; applyStatic(); }")
    expect(auto).to_have_text("已保存的默认值 · 高")
