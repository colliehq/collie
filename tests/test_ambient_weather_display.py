"""What the wallpaper clock's weather line says, rendered by the real page against the real server.

The line used to draw a sun at midnight, say "Overcast" on a Chinese desktop, stay blank while it
waited or after a failure, and keep old conditions looking current. These drive ambient.html in
Chromium with the server's outside requests replaced (`desktop_weather._get`); nothing leaves the
machine.
"""
import re
import threading
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
CLEAR_NIGHT = {"temperature_2m": 12.2, "weather_code": 0, "is_day": 0, "time": "2026-09-27T23:00"}


@pytest.fixture
def ambient(tmp_path, monkeypatch):
    from harness import desktop, desktop_weather, webapp
    from harness.httpserver import ThreadingHTTPServer
    monkeypatch.setattr(desktop, "CONFIG_PATH", str(tmp_path / "desktop.json"))
    monkeypatch.setattr(desktop, "_seed_apps", lambda: [])
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLLIE_LANG", "en")
    monkeypatch.setattr(desktop_weather, "_state", {"checked": None, "failures": 0,
                                                    "data": None, "updated": None})
    ctl = {"current": dict(CLEAR_NIGHT), "fail": False, "gate": None}

    def get(url):
        if ctl["gate"] is not None:
            ctl["gate"].wait(15)
        if ctl["fail"]:
            raise OSError("offline")
        if url.startswith(desktop_weather.GEO_URL):
            return {"latitude": 37.3, "longitude": -121.9, "city": "San Jose",
                    "country_code": "US"}
        return {"current": dict(ctl["current"])}

    monkeypatch.setattr(desktop_weather, "_get", get)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            yield "http://127.0.0.1:%d" % server.server_address[1], ctl, browser
        finally:
            browser.close()
            server.shutdown(); server.server_close(); thread.join(timeout=3)


def _open(base, browser, locale="en-US"):
    page = browser.new_context(locale=locale, viewport={"width": 1280, "height": 800}).new_page()
    page.route(re.compile(r"https://(ipapi\.co|api\.open-meteo\.com)/.*"), lambda r: r.abort())
    page.goto(base + "/ambient", wait_until="load")
    page.wait_for_selector("#wWx", timeout=10000)
    return page


def _settled(page, state):
    page.wait_for_selector('#wWx[data-state="%s"]' % state, timeout=10000)
    return page.inner_text("#wWx")


def _shown(page):
    """Conditions on screen (the temperature is drawn), however the line marks its state."""
    page.wait_for_selector("#wWx .t", timeout=10000)
    return page.inner_text("#wWx")


def test_a_clear_night_shows_a_moon_and_a_clear_day_a_sun(ambient):
    base, ctl, browser = ambient
    page = _open(base, browser)
    _shown(page)
    assert page.query_selector("#wWx svg .moon") and not page.query_selector("#wWx svg .sun")
    from harness import desktop_weather
    desktop_weather._state["checked"] = None              # let the next ask refresh
    ctl["current"] = dict(CLEAR_NIGHT, is_day=1)
    page.reload(wait_until="load")
    _shown(page)
    assert page.query_selector("#wWx svg .sun") and not page.query_selector("#wWx svg .moon")


@pytest.mark.parametrize("lang, locale, overcast", [("en", "en-US", "Overcast"),
                                                     ("zh", "zh-CN", "阴"),
                                                     ("zh-tw", "zh-TW", "陰")])
def test_the_condition_is_named_in_the_desktops_language(ambient, monkeypatch, lang, locale,
                                                        overcast):
    base, ctl, browser = ambient
    monkeypatch.setenv("COLLIE_LANG", lang)
    ctl["current"] = dict(CLEAR_NIGHT, weather_code=3)
    page = _open(base, browser, locale)
    # The page's CSP forbids eval, which wait_for_function relies on, so poll from here.
    for _ in range(100):
        if page.evaluate("(lang) => document.documentElement.lang === lang", lang):
            break
        page.wait_for_timeout(100)
    text = _shown(page)
    assert overcast + " · San Jose" in text and "12°" in text


def test_it_says_loading_and_then_unavailable_instead_of_staying_blank(ambient):
    base, ctl, browser = ambient
    ctl["gate"], ctl["fail"] = threading.Event(), True
    page = _open(base, browser)
    assert _settled(page, "loading") == "Loading weather…"
    ctl["gate"].set()
    assert _settled(page, "unavailable") == "Weather unavailable"
    assert not page.query_selector("#wWx .t")


def test_a_second_window_is_answered_at_once_and_fills_in_when_the_first_fetch_lands(ambient):
    from harness import desktop_weather
    base, ctl, browser = ambient
    ctl["gate"] = threading.Event()
    first = _open(base, browser)
    until = time.time() + 10
    while not desktop_weather._state.get("refreshing") and time.time() < until:
        time.sleep(0.05)
    assert desktop_weather._state.get("refreshing"), "the first window never started a fetch"
    second = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
    second.route(re.compile(r"https://.*"), lambda r: r.abort())
    began = time.time()
    with second.expect_response(lambda r: "/api/desktop/weather" in r.url,
                                timeout=10000) as answered:
        second.goto(base + "/ambient", wait_until="load")
    assert answered.value.json() == {"ok": False, "pending": True}
    assert time.time() - began < 5                  # not held behind the first window's fetch
    assert _settled(second, "loading") == "Loading weather…"
    ctl["gate"].set()
    assert "12°" in _settled(second, "ready")       # arrives by the page's own retry, no reload
    assert "12°" in _settled(first, "ready")


def test_a_failed_refresh_shows_the_last_conditions_as_cached_with_their_time(ambient):
    from harness import desktop_weather
    base, ctl, browser = ambient
    ago = 20 * 60
    desktop_weather._state.update(
        checked=time.monotonic() - ago, updated=time.monotonic() - ago, failures=0,
        data={"ok": True, "temp_c": 18.0, "code": 1, "is_day": 1, "city": "San Jose",
              "country_code": "US", "observed_at": "", "fetched_at": int(time.time()) - ago,
              "source": "Open-Meteo"})
    ctl["fail"] = True
    page = _open(base, browser)
    text = _settled(page, "stale")
    assert "18°" in text and "as of " in text
    assert page.get_attribute("#wWx", "title").startswith("Last available conditions")


def test_when_collies_server_stops_answering_old_conditions_are_marked_then_dropped(ambient):
    base, ctl, browser = ambient
    page = _open(base, browser)
    _settled(page, "ready")
    page.route(re.compile(r".*/api/desktop/weather.*"), lambda r: r.abort())
    _show(page)
    _settled(page, "stale")
    # An answer more than an hour old is not kept once the server cannot be reached.
    page.evaluate("() => { const d = new Date(Date.now() + 2 * 3600 * 1000);"
                  " Date.now = () => d.getTime(); }")
    _show(page)
    assert _settled(page, "unavailable") == "Weather unavailable"


def _show(page):
    page.evaluate("() => { Object.defineProperty(document, 'hidden', {value: false,"
                  " configurable: true}); document.dispatchEvent(new Event('visibilitychange')); }")


def test_the_desktop_coming_back_into_view_asks_for_the_weather_at_once(ambient):
    base, _ctl, browser = ambient
    page = _open(base, browser)
    _shown(page)
    page.wait_for_timeout(500)
    asked = []
    page.on("request", lambda request: asked.append(request.url)
            if "/api/desktop/weather" in request.url else None)
    _show(page)
    page.wait_for_timeout(1500)
    assert len(asked) == 1


def test_every_weather_condition_has_both_chinese_names():
    page = (ROOT / "harness/webui/ambient.html").read_text(encoding="utf-8")
    table = page.split("var WX = {", 1)[1].split("};", 1)[0]
    labels = set(re.findall(r'\["[^"]*","([^"]+)"\]', table))
    zh = page.split("Object.assign(OPS_ZH,", 1)[1].split("Object.assign(OPS_ZHTW,", 1)[0]
    zhtw = page.split("Object.assign(OPS_ZHTW,", 1)[1].split("function resolveOpsLang", 1)[0]
    assert len(labels) >= 16
    for label in labels:
        assert '"%s":' % label in zh, label
        assert '"%s":' % label in zhtw, label
