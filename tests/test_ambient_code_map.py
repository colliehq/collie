"""The code map takes over the ambient desktop only when the person has asked for it.

The ambient wallpaper swapped its whole background for the code map (the star-map galaxy) as soon
as any run edited a file. It is now a setting, Settings → Desktop → "Show the code map while Collie
edits code", off by default. These tests read the setting's declaration and drive the real page
against the real server, publishing run events on the server's own live feed.
"""
import re
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_the_setting_exists_is_off_by_default_and_is_in_the_desktop_panel():
    from harness import settings
    row = next(s for s in settings.SCHEMA if s["key"] == "DESKTOP_CODE_MAP")
    assert row["group"] == "Desktop" and row["type"] == "bool" and row["default"] == "off"
    assert row.get("label_zh") and row.get("hint_zh")
    page = (ROOT / "harness/webui/index.html").read_text(encoding="utf-8")
    devices = page.split('{ id: "devices"', 1)[1].split("}", 1)[0]
    assert '"DESKTOP_CODE_MAP"' in devices


def test_a_manual_run_of_the_wallpaper_host_opens_the_calm_desktop():
    source = (ROOT / "harness/wallpaper/Program.cs").read_text(encoding="utf-8")
    assert ('url = _windowMode ? "http://127.0.0.1:8787/" : "http://127.0.0.1:8787/ambient";'
            in source)
    readme = (ROOT / "harness/wallpaper/README.md").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8787/ambient" in readme


@pytest.fixture
def ambient(tmp_path, monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    from harness import desktop, desktop_weather, webapp
    from harness.httpserver import ThreadingHTTPServer
    monkeypatch.setattr(desktop, "CONFIG_PATH", str(tmp_path / "desktop.json"))
    monkeypatch.setattr(desktop, "_seed_apps", lambda: [])
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    # A language other than the page's built-in "en" tells the test the settings have arrived.
    monkeypatch.setenv("COLLIE_LANG", "zh-tw")

    def offline(_url):
        raise OSError("no network in tests")

    monkeypatch.setattr(desktop_weather, "_get", offline)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            yield "http://127.0.0.1:%d" % server.server_address[1], browser, webapp.Handler
        finally:
            browser.close()
            server.shutdown(); server.server_close(); thread.join(timeout=3)


def _poll(page, script, arg=None, seconds=10):
    # The page's CSP forbids eval, which wait_for_function relies on, so poll from here.
    deadline = time.time() + seconds
    while time.time() < deadline:
        if page.evaluate(script, arg):
            return True
        page.wait_for_timeout(100)
    return False


def _open(base, browser, handler):
    page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
    page.route(re.compile(r"https://.*"), lambda route: route.abort())
    page.route(re.compile(r".*/wallpaper\?bg=1.*"), lambda route: route.fulfill(
        status=200, content_type="text/html", body="<!doctype html><title>map</title>"))
    page.clock.install()
    before = len(handler._live_subs)
    page.goto(base + "/ambient", wait_until="load")
    assert _poll(page, "() => document.documentElement.lang === 'zh-tw'"), "settings never arrived"
    deadline = time.time() + 10
    while len(handler._live_subs) <= before and time.time() < deadline:
        time.sleep(0.05)
    assert len(handler._live_subs) > before, "the page never subscribed to the live feed"
    return page


def _code_work(handler):
    handler._live_pub("start", {"session": "code-map-test", "run": "r1"})
    handler._live_pub("edit", {"session": "code-map-test", "path": "harness/x.py"})


STATE = """() => { const g = document.getElementById('galaxy');
  return {coding: document.body.classList.contains('coding'), src: g.getAttribute('src'),
          display: g.style.display}; }"""


def test_code_work_leaves_the_calm_desktop_alone_by_default(ambient):
    base, browser, handler = ambient
    page = _open(base, browser, handler)
    _code_work(handler)
    page.wait_for_timeout(1500)
    state = page.evaluate(STATE)
    assert state["coding"] is False
    assert state["src"] is None                      # the map was never even loaded
    assert state["display"] in ("", "none")


def test_when_asked_for_the_map_shows_during_code_work_and_turning_it_off_restores_the_desktop(
        ambient, monkeypatch):
    base, browser, handler = ambient
    monkeypatch.setenv("COLLIE_DESKTOP_CODE_MAP", "on")
    page = _open(base, browser, handler)
    _code_work(handler)
    assert _poll(page, "() => document.body.classList.contains('coding')"), page.evaluate(STATE)
    assert page.evaluate(STATE)["src"] == "/wallpaper?bg=1"
    page.clock.run_for(1000)
    assert page.evaluate("() => document.getElementById('galaxy').classList.contains('on')")
    # Switched off in Settings while the map is showing: the next settings read restores the desktop.
    monkeypatch.setenv("COLLIE_DESKTOP_CODE_MAP", "off")
    page.clock.run_for(16000)
    assert _poll(page, "() => !document.body.classList.contains('coding')"), page.evaluate(STATE)
    page.clock.run_for(1000)
    state = page.evaluate(STATE)
    assert state["display"] == "none"
    assert not page.evaluate("() => document.getElementById('galaxy').classList.contains('on')")
    _code_work(handler)
    page.wait_for_timeout(1000)
    assert page.evaluate(STATE)["coding"] is False
