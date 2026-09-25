"""The wallpaper clock's weather line asks two outside services; desktop.json can turn it off.

It asks ipapi.co where this network address is and api.open-meteo.com for the weather there. That
was not in the privacy policy and had no off switch. Both services are intercepted here: nothing
leaves the machine.
"""
import json
import threading

import pytest

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def ambient(tmp_path, monkeypatch):
    from harness import desktop, webapp
    from harness.httpserver import ThreadingHTTPServer
    config = tmp_path / "desktop.json"
    monkeypatch.setattr(desktop, "CONFIG_PATH", str(config))
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d/ambient" % server.server_address[1], config
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def _load(url):
    asked = []
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        def geo(route):
            asked.append("ipapi")
            route.fulfill(status=200, content_type="application/json",
                          headers={"access-control-allow-origin": "*"},
                          body=json.dumps({"latitude": 37.3, "longitude": -121.9, "city": "San Jose"}))

        def weather(route):
            asked.append("open-meteo")
            route.fulfill(status=200, content_type="application/json",
                          headers={"access-control-allow-origin": "*"},
                          body=json.dumps({"current": {"temperature_2m": 21.4, "weather_code": 0}}))

        page.route("https://ipapi.co/**", geo)
        page.route("https://api.open-meteo.com/**", weather)
        page.goto(url, wait_until="load")
        page.wait_for_timeout(2500)
        text = page.inner_text("#wWx") if page.query_selector("#wWx") else ""
        browser.close()
    return asked, text


def test_the_clock_shows_weather_by_default(ambient):
    url, _config = ambient
    asked, text = _load(url)
    assert asked == ["ipapi", "open-meteo"]
    assert "21°" in text and "San Jose" in text


def test_weather_false_asks_neither_service_and_keeps_the_clock(ambient):
    url, config = ambient
    config.write_text(json.dumps({"widgets": {"clock": {"weather": False}}}), encoding="utf-8")
    asked, text = _load(url)
    assert asked == []
    assert text == ""
