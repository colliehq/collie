"""The wallpaper clock keeps AM/PM beside the time, on the locale's side, at the same distance.

The period was a span with a left margin after the locale's own space: in English "11:13   PM"
sat far apart, and in Traditional Chinese, where the period comes first, "下午11:13" was glued
together with the margin on the outside. Measured on the rendered page in Chromium; the outside
weather requests are replaced so nothing leaves the machine.
"""
import threading

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

# Sunday 2026-09-27 23:13 in Los Angeles: a time every 12-hour locale marks with a period.
NIGHT = 1790576000

GEOMETRY = """() => {
  const t = document.getElementById('wTime');
  const ap = t.querySelector('.ampm');
  const size = parseFloat(getComputedStyle(t).fontSize);
  const boxes = [];
  const walker = document.createTreeWalker(t, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const n = walker.currentNode;
    if (ap && ap.contains(n)) continue;
    const text = n.textContent, start = text.length - text.trimStart().length;
    const end = text.trimEnd().length;
    if (end <= start) continue;
    const r = document.createRange(); r.setStart(n, start); r.setEnd(n, end);
    boxes.push(r.getBoundingClientRect());
  }
  const digits = {left: Math.min(...boxes.map(b => b.left)), right: Math.max(...boxes.map(b => b.right))};
  const out = {size, text: t.textContent, label: t.getAttribute('aria-label'),
               period: ap && !ap.hidden && ap.textContent.trim() ? ap.textContent.trim() : ''};
  if (out.period) {
    const a = ap.getBoundingClientRect();
    out.first = a.right <= digits.left + 0.5;
    out.gap = out.first ? digits.left - a.right : a.left - digits.right;
  }
  return out;
}"""


@pytest.fixture(scope="module")
def ambient():
    import os
    import tempfile
    from harness import desktop, desktop_weather, webapp
    from harness.httpserver import ThreadingHTTPServer
    saved = (desktop.CONFIG_PATH, desktop._seed_apps, desktop_weather._get)
    desktop.CONFIG_PATH = os.path.join(tempfile.mkdtemp(), "desktop.json")
    desktop._seed_apps = lambda: []

    def offline(_url):
        raise OSError("no network in tests")

    desktop_weather._get = offline
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            yield "http://127.0.0.1:%d/ambient" % server.server_address[1], browser
        finally:
            browser.close()
            server.shutdown(); server.server_close(); thread.join(timeout=3)
            desktop.CONFIG_PATH, desktop._seed_apps, desktop_weather._get = saved


def _clock(ambient, locale, monkeypatch, lang):
    url, browser = ambient
    monkeypatch.setenv("COLLIE_LANG", lang)
    context = browser.new_context(locale=locale, timezone_id="America/Los_Angeles",
                                  viewport={"width": 1400, "height": 800})
    page = context.new_page()
    page.route("https://**", lambda route: route.abort())
    page.clock.install(time=NIGHT)
    page.goto(url, wait_until="load")
    page.wait_for_selector("#wTime", timeout=10000)
    # The page's CSP forbids eval, which wait_for_function relies on, so poll from here.
    for _ in range(100):
        if page.evaluate("(lang) => document.documentElement.lang === lang &&"
                         " /\\d/.test(document.getElementById('wTime').textContent)", lang):
            break
        page.wait_for_timeout(100)
    geometry = page.evaluate(GEOMETRY)
    context.close()
    return geometry


@pytest.mark.parametrize("locale, lang, period, first", [("en-US", "en", "PM", False),
                                                         ("zh-TW", "zh-tw", "下午", True)])
def test_the_period_sits_beside_the_time_on_the_locales_side(ambient, monkeypatch, locale, lang,
                                                            period, first):
    clock = _clock(ambient, locale, monkeypatch, lang)
    assert clock["period"] == period
    assert clock["first"] is first
    # A clear gap, but one gap: not glued to the digits, not a space plus a margin.
    assert 0.08 * clock["size"] <= clock["gap"] <= 0.3 * clock["size"], clock
    assert "11:13" in clock["label"] and period in clock["label"]


def test_a_24_hour_locale_shows_no_period(ambient, monkeypatch):
    clock = _clock(ambient, "zh-CN", monkeypatch, "zh")
    assert clock["period"] == "" and "23:13" in clock["text"]
