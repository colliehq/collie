"""In the desktop's edit mode, each widget can be turned on or off and given a corner.

Until now edit mode could only drag a visible widget to another corner: a widget that was off could
not be brought back, and one that was on could not be put away, without editing
~/.collie/desktop.json by hand or asking Collie. These drive the real page against the real server
and read desktop.json back from disk. The weather service is replaced; nothing leaves the machine.
"""
import json
import re
import threading
import time

import pytest

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def ambient(tmp_path, monkeypatch):
    from harness import desktop, desktop_weather, webapp
    from harness.httpserver import ThreadingHTTPServer
    config = tmp_path / "desktop.json"
    monkeypatch.setattr(desktop, "CONFIG_PATH", str(config))
    monkeypatch.setattr(desktop, "COLLIE_DIR", str(tmp_path))
    monkeypatch.setattr(desktop, "_seed_apps", lambda: [])
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLLIE_LANG", "en")

    def offline(_url):
        raise OSError("no network in tests")

    monkeypatch.setattr(desktop_weather, "_get", offline)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            yield "http://127.0.0.1:%d" % server.server_address[1], config, browser
        finally:
            browser.close()
            server.shutdown(); server.server_close(); thread.join(timeout=3)


def _open(base, browser, lang="en"):
    page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
    page.route(re.compile(r"https://.*"), lambda route: route.abort())
    page.goto(base + "/ambient", wait_until="load")
    page.wait_for_selector("#slot-tr .clock", timeout=10000)
    deadline = time.time() + 10
    while time.time() < deadline and page.evaluate(
            "(lang) => document.documentElement.lang !== lang", lang):
        page.wait_for_timeout(100)
    return page


def _saved(config, name):
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            return json.loads(config.read_text(encoding="utf-8"))["widgets"][name]
        except (OSError, ValueError, KeyError):
            time.sleep(0.1)
    raise AssertionError("desktop.json was never written")


def _row(page, name):
    return page.locator("#widgetPanel .wp-row", has_text=name)


def test_edit_mode_lists_every_widget_with_its_state(ambient):
    base, _config, browser = ambient
    page = _open(base, browser)
    assert page.locator("#widgetPanel").is_hidden()
    page.click("#editbtn")
    panel = page.locator("#widgetPanel")
    assert panel.is_visible()
    names = panel.locator(".wp-row .wp-name").all_inner_texts()
    assert [n.strip() for n in names] == ["Name and logo", "Clock & weather", "Apps", "Music",
                                          "System", "Projects"]
    assert _row(page, "Music").locator("input[type=checkbox]").is_checked()
    assert not _row(page, "System").locator("input[type=checkbox]").is_checked()
    clock = _row(page, "Clock & weather")
    assert clock.get_by_role("button", name="Top right").get_attribute("aria-pressed") == "true"
    page.click("#editbtn")
    assert panel.is_hidden()


def test_a_widget_can_be_turned_off_and_back_on(ambient):
    base, config, browser = ambient
    page = _open(base, browser)
    page.click("#editbtn")
    music = _row(page, "Music").locator("input[type=checkbox]")
    music.uncheck()
    page.wait_for_selector(".music", state="detached", timeout=10000)
    assert _saved(config, "music")["on"] is False
    music = _row(page, "Music").locator("input[type=checkbox]")
    music.check()
    page.wait_for_selector(".music", timeout=10000)
    assert _saved(config, "music")["on"] is True


def test_choosing_a_corner_moves_the_widget_and_keeps_everything_else(ambient):
    base, config, browser = ambient
    page = _open(base, browser)
    # Collie edits desktop.json after the page loaded; the panel must not write over that.
    config.write_text(json.dumps({"widgets": {"mine": {"on": True, "slot": "br"},
                                              "clock": {"weather": False}}}), encoding="utf-8")
    page.click("#editbtn")
    _row(page, "Clock & weather").get_by_role("button", name="Bottom left").click()
    page.wait_for_selector("#slot-bl .clock", timeout=10000)
    saved = json.loads(config.read_text(encoding="utf-8"))["widgets"]
    assert saved["clock"]["slot"] == "bl" and saved["clock"]["weather"] is False
    assert saved["mine"] == {"on": True, "slot": "br"}
    # A corner for a widget that is off also shows it there.
    _row(page, "System").get_by_role("button", name="Top left").click()
    page.wait_for_selector("#slot-tl .sys", timeout=10000)
    assert _saved(config, "system") == {"on": True, "slot": "tl"}


def test_an_unreadable_desktop_json_is_explained_and_not_overwritten(ambient):
    base, config, browser = ambient
    raw = b'{"widgets": {"clock": {"weather": false},}}'
    config.write_bytes(raw)
    page = _open(base, browser)
    page.click("#editbtn")
    assert "desktop.json could not be read" in page.inner_text("#widgetPanel .wp-error")
    music = _row(page, "Music").locator("input[type=checkbox]")
    assert music.is_disabled()
    assert _row(page, "Clock & weather").get_by_role("button", name="Bottom left").is_disabled()
    assert config.read_bytes() == raw


def test_the_panel_speaks_the_desktops_language(ambient, monkeypatch):
    base, _config, browser = ambient
    monkeypatch.setenv("COLLIE_LANG", "zh-tw")
    page = _open(base, browser, "zh-tw")
    page.click("#editbtn")
    assert page.inner_text("#widgetPanel h2") == "桌面元件"
    assert _row(page, "時鐘與天氣").get_by_role("button", name="右上").get_attribute(
        "aria-pressed") == "true"
