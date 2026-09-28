"""The wallpaper clock's weather comes from Collie's server, and desktop.json can turn it off there.

The weather line needs two outside services: ipapi.co (where this network address is) and
api.open-meteo.com (the weather there). The page used to ask both itself, from every desktop
window; now Collie's server asks for all of them (harness/desktop_weather.py) and the page asks
only the server. `"clock": {"weather": false}` is honoured by the server as well as the page, so
nothing leaves the machine when it is off.

Nothing here reaches either service: the server's `_get` is replaced, and the browser records and
aborts any request to them.
"""
import json
import threading
import urllib.error
import urllib.request

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

OUTSIDE = ("https://ipapi.co/**", "https://api.open-meteo.com/**")


@pytest.fixture
def ambient(tmp_path, monkeypatch):
    from harness import desktop, desktop_weather, webapp
    from harness.httpserver import ThreadingHTTPServer
    config = tmp_path / "desktop.json"
    monkeypatch.setattr(desktop, "CONFIG_PATH", str(config))
    monkeypatch.setattr(desktop, "_seed_apps", lambda: [])
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(desktop_weather, "_state", {"checked": None, "failures": 0,
                                                    "data": None, "updated": None})
    server_asked = []

    def get(url):
        server_asked.append(url)
        if url.startswith(desktop_weather.GEO_URL):
            return {"latitude": 37.3, "longitude": -121.9, "city": "San Jose",
                    "country_code": "US"}
        return {"current": {"temperature_2m": 21.4, "weather_code": 0, "is_day": 1,
                            "time": "2026-09-27T14:00"}}

    monkeypatch.setattr(desktop_weather, "_get", get)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], config, server_asked
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def _load(base):
    page_asked = []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        def outside(route):
            page_asked.append(route.request.url)
            route.abort()

        for pattern in OUTSIDE:
            page.route(pattern, outside)
        page.on("request", lambda request: page_asked.append(request.url)
                if not request.url.startswith(base) else None)
        page.goto(base + "/ambient", wait_until="load")
        page.wait_for_selector("#wTime", timeout=10000)
        page.wait_for_timeout(2500)
        text = page.inner_text("#wWx") if page.query_selector("#wWx") else ""
        clock = page.inner_text("#wTime")
        browser.close()
    return page_asked, text, clock


def _get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_the_clock_shows_weather_that_collies_server_fetched(ambient):
    base, _config, server_asked = ambient
    page_asked, text, _clock = _load(base)
    assert page_asked == []                             # the page asks no outside service itself
    assert [url.split("?")[0] for url in server_asked] == [
        "https://ipapi.co/json/", "https://api.open-meteo.com/v1/forecast"]
    assert "21°" in text and "San Jose" in text


def test_weather_false_asks_neither_service_and_keeps_the_clock(ambient):
    base, config, server_asked = ambient
    config.write_text(json.dumps({"widgets": {"clock": {"weather": False}}}), encoding="utf-8")
    page_asked, text, clock = _load(base)
    assert page_asked == [] and server_asked == []
    assert text == ""
    assert clock.strip() and clock.strip() != "--:--"


def test_the_server_itself_honours_weather_false(ambient):
    from harness import webapp
    base, config, server_asked = ambient
    url = base + "/api/desktop/weather"
    assert _get_json(url)[0] == 403                     # the endpoint needs the page's token
    config.write_text(json.dumps({"widgets": {"clock": {"weather": False}}}), encoding="utf-8")
    code, body = _get_json(url + "?token=" + webapp.TOKEN)
    assert code == 200 and body == {"ok": False, "off": True}
    config.write_text(json.dumps({"widgets": {"clock": {"on": False}}}), encoding="utf-8")
    assert _get_json(url + "?token=" + webapp.TOKEN)[1] == {"ok": False, "off": True}
    assert server_asked == []
    config.write_text(json.dumps({"widgets": {"clock": {"weather": True}}}), encoding="utf-8")
    code, body = _get_json(url + "?token=" + webapp.TOKEN)
    assert code == 200 and body["ok"] is True and body["temp_c"] == 21.4
    assert len(server_asked) == 2


def test_pages_may_no_longer_connect_to_the_weather_services(ambient):
    base, _config, _server_asked = ambient
    with urllib.request.urlopen(base + "/ambient", timeout=10) as response:
        policy = response.headers["Content-Security-Policy"]
    assert "connect-src 'self';" in policy
    assert "ipapi" not in policy and "open-meteo" not in policy
