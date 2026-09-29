"""The wallpaper's morning scene, driven in headless Chromium against the real server.

Before noon on the day of a saved morning report, ambient.html draws the day's weather as a live
sky and puts the report's headline, its summary, progress dots and a few actions on it. These use
a fixture report (tests/_morning_fixture.py), a fixed server clock and fixed weather; nothing
leaves the machine (every https request from the page is aborted, and the server's weather is
replaced).
"""
import json
import re
import threading
import time
from types import SimpleNamespace

import pytest

from _morning_fixture import AZURE_LINK, DRAFT_ANA, PR_LINK, at, report, rewrite, save

sync_api = pytest.importorskip("playwright.sync_api")

CLEAR_DAY = {"ok": True, "temp_c": 11.2, "code": 0, "is_day": 1, "city": "San Jose",
             "country_code": "US", "observed_at": "", "fetched_at": 0, "source": "Open-Meteo",
             "stale": False}


@pytest.fixture
def desk(tmp_path, monkeypatch):
    from harness import desktop, desktop_weather, morning_desktop, webapp
    from harness.httpserver import ThreadingHTTPServer
    config = tmp_path / "desktop.json"
    monkeypatch.setattr(desktop, "CONFIG_PATH", str(config))
    monkeypatch.setattr(desktop, "COLLIE_DIR", str(tmp_path))
    monkeypatch.setattr(desktop, "_seed_apps", lambda: [])
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_LANG", "en")
    monkeypatch.setenv("COLLIE_DESKTOP_MORNING_REPORT", "on")
    clock = {"now": at(8)}
    monkeypatch.setattr(morning_desktop, "_now", lambda: clock["now"])
    monkeypatch.setattr(morning_desktop, "_spawn", lambda _fn: None)   # no resolve pass here
    weather = {"now": dict(CLEAR_DAY, fetched_at=int(time.time()))}
    monkeypatch.setattr(desktop_weather, "weather", lambda: dict(weather["now"]))
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            yield SimpleNamespace(base="http://127.0.0.1:%d" % server.server_address[1],
                                  state=str(state), config=config, clock=clock,
                                  weather=weather, browser=browser)
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


def _open(desk, *, size=(1920, 1080), fake_clock=False, reduced=False, wait=True):
    context = desk.browser.new_context(viewport={"width": size[0], "height": size[1]},
                                       reduced_motion="reduce" if reduced else "no-preference")
    page = context.new_page()
    page.route(re.compile(r"https://.*"), lambda route: route.abort())
    if fake_clock:
        page.clock.install()
    page.goto(desk.base + "/ambient", wait_until="load")
    page.wait_for_selector(".slot .clock", timeout=10000)
    if wait:
        assert _poll(page, SHOWING), "the morning scene never showed"
    return page


SHOWING = """() => document.body.classList.contains('morning') && !document.getElementById('morning').hidden
  && !!document.getElementById('mSay').textContent"""
NORMAL = "() => !document.body.classList.contains('morning') && document.getElementById('morning').hidden"


def _frames(page):
    return page.evaluate("() => window.collieMorning.frames()")


def test_it_shows_before_noon_with_the_reports_words_and_the_normal_desktop_otherwise(desk):
    save(desk.state, report())
    page = _open(desk)
    assert page.inner_text("#mSay") == "Five quick ones and you're clear."
    assert page.inner_text("#mMore").startswith("Collie 0.31.0 shipped overnight")
    assert page.locator("#mDots .m-dot").count() == 5 and page.locator("#mDots .m-dot.on").count() == 0
    assert page.inner_text("#mDots").strip() == "0 of 5 done"
    by = page.inner_text("#mBy")
    assert "Rowan" in by and "caught up at 6:38 AM" in by
    when = page.text_content("#mWhen")                    # inner_text would be upper-cased
    assert when == "Tuesday, September 29 · 11° Clear"
    # The logo steps aside; the clock stays.
    assert page.locator('.widget[data-widget="brand"]').is_hidden()
    assert page.locator("#slot-tr .clock").is_visible()
    page.close()

    desk.clock["now"] = at(12, 1)
    after = _open(desk, wait=False)
    page_wait = _poll(after, "() => window.collieMorning && window.collieMorning.checked()")
    assert page_wait and after.evaluate(NORMAL)
    assert _frames(after) == 0 and after.evaluate("() => window.collieMorning.running()") is False
    assert after.locator('.widget[data-widget="brand"]').is_visible()


def test_without_a_report_the_desktop_keeps_its_normal_look(desk):
    page = _open(desk, wait=False)
    assert _poll(page, "() => window.collieMorning && window.collieMorning.checked()")
    page.wait_for_timeout(500)
    assert page.evaluate(NORMAL) and _frames(page) == 0
    assert page.evaluate("() => document.getElementById('skyCanvas').width") == 0


SCENES = [(0, 1, "clear", False), (1, 1, "clear", False), (2, 1, "cloudy", False),
          (3, 1, "cloudy", False), (61, 1, "rain", True), (81, 1, "rain", True),
          (95, 1, "storm", True), (73, 1, "snow", False), (45, 1, "fog", False),
          (0, 0, "night", True), (2, 0, "night", True), (61, 0, "rain", True)]


def _luminance(css):
    r, g, b = (int(x) for x in re.findall(r"\d+", css)[:3])
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def test_the_weather_code_picks_the_sky_and_the_text_turns_light_on_dark_skies(desk):
    save(desk.state, report())
    for code, is_day, sky, dark in SCENES:
        desk.weather["now"] = dict(CLEAR_DAY, code=code, is_day=is_day,
                                   fetched_at=int(time.time()))
        page = _open(desk)
        assert _poll(page, "(sky) => document.body.dataset.sky === sky", sky), (code, is_day)
        assert page.evaluate("() => document.body.classList.contains('sky-dark')") is dark
        light = _luminance(page.evaluate(
            "() => getComputedStyle(document.getElementById('mSay')).color"))
        assert (light > 0.8) if dark else (light < 0.3), (code, is_day, light)
        page.context.close()


def test_the_page_maps_every_code_as_the_report_does(desk):
    from harness import morning_report as mr
    save(desk.state, report())
    page = _open(desk)
    cases = [(code, day) for code in range(100) for day in (0, 1, None)]
    got = page.evaluate("""(cases) => cases.map(([code, day]) =>
        window.collieMorning.skyFor({ok: true, code: code, is_day: day}))""", cases)
    want = [mr.sky({"state": "ok", "code": code, "is_day": day}) for code, day in cases]
    assert got == want
    assert page.evaluate("() => window.collieMorning.skyFor({ok: false})") == "clear"


def test_with_the_weather_off_it_is_a_calm_clear_sky_without_a_temperature(desk):
    save(desk.state, report())
    desk.config.write_text(json.dumps({"widgets": {"clock": {"weather": False}}}),
                           encoding="utf-8")
    desk.weather["now"] = dict(CLEAR_DAY, code=95)          # would be a storm if it were asked
    page = _open(desk)
    assert _poll(page, "() => document.body.dataset.sky === 'clear'")
    assert page.text_content("#mWhen") == "Tuesday, September 29"
    assert page.evaluate("() => document.body.classList.contains('sky-dark')") is False


def test_the_pills_link_only_to_https_or_the_report_page(desk):
    save(desk.state, report())
    page = _open(desk)
    pills = page.evaluate("""() => [...document.querySelectorAll('#mActs a')].map(a =>
        ({label: a.textContent, href: a.getAttribute('href'), target: a.target, rel: a.rel,
          go: a.classList.contains('go')}))""")
    assert [p["label"] for p in pills] == ["Review 2 drafts", "Review Ana's pull request",
                                           "Answer Collie's question"]
    assert pills[0]["href"] == DRAFT_ANA and pills[0]["go"] is True
    assert pills[1]["href"] == PR_LINK and pills[1]["go"] is False
    assert pills[2]["href"].startswith("/report?token=")
    assert all(p["target"] == "_blank" and "noopener" in p["rel"] for p in pills)
    assert page.get_attribute("#mOpen", "href").startswith("/report?token=")
    page.close()

    rep = report()
    rep["sections"]["yours"]["items"][0]["link"] = "javascript:alert(1)"
    rep["sections"]["ready"]["items"] = []
    rewrite(desk.state, rep)
    page = _open(desk)
    hrefs = page.evaluate("() => [...document.querySelectorAll('#mActs a')].map(a => a.getAttribute('href'))")
    assert hrefs[0].startswith("/report?token=") and hrefs[2] == AZURE_LINK
    assert all(h.startswith("https://") or h.startswith("/report?token=") for h in hrefs)


def test_the_close_button_hides_it_for_today(desk):
    from harness import morning_desktop as md
    save(desk.state, report())
    page = _open(desk)
    page.click("#mClose")
    assert _poll(page, NORMAL)
    assert md.today(desk.state, now=at(8))["why"] == "dismissed"


def test_reduced_motion_draws_one_still_frame(desk):
    save(desk.state, report())
    page = _open(desk, reduced=True)
    assert _poll(page, "() => window.collieMorning.frames() >= 1")
    page.wait_for_timeout(800)
    assert _frames(page) == 1 and page.evaluate("() => window.collieMorning.running()") is False


HIDE = """(hidden) => {
  Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden});
  Object.defineProperty(document, 'visibilityState', {configurable: true,
                                                      get: () => hidden ? 'hidden' : 'visible'});
  document.dispatchEvent(new Event('visibilitychange'));
}"""


def _rate(page, seconds=2.0):
    start, began = _frames(page), time.time()
    page.wait_for_timeout(int(seconds * 1000))
    return (_frames(page) - start) / (time.time() - began)


def test_the_sky_is_capped_and_stops_while_the_page_is_hidden(desk):
    save(desk.state, report())
    desk.weather["now"] = dict(CLEAR_DAY, code=63, fetched_at=int(time.time()))
    rain = _open(desk)
    assert _poll(rain, "() => window.collieMorning.frames() > 5")
    assert 10 <= _rate(rain) <= 31                         # falling rain: at most 30 a second
    rain.context.close()
    desk.weather["now"] = dict(CLEAR_DAY, fetched_at=int(time.time()))
    page = _open(desk)
    assert _poll(page, "() => window.collieMorning.frames() > 5")
    assert 8 <= _rate(page) <= 21                          # drifting clouds: at most 20
    page.evaluate(HIDE, True)
    paused = _frames(page)
    page.wait_for_timeout(1000)
    assert _frames(page) - paused <= 1 and page.evaluate("() => window.collieMorning.running()") is False
    page.evaluate(HIDE, False)
    assert _poll(page, "(n) => window.collieMorning.frames() > n + 3", paused)


OBSTACLES = """() => [...document.querySelectorAll('.slot > .widget, .opsbar, .composer, #editbtn')]
  .filter(el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden')
  .map(el => { const r = el.getBoundingClientRect();
               return {name: el.dataset.widget || el.className || el.id,
                       x: r.left, y: r.top, r: r.right, b: r.bottom}; })
  .filter(r => r.r - r.x > 0 && r.b - r.y > 0)"""
TEXT = """() => ['mWhen', 'mSay', 'mMore', 'mDots', 'mActs', 'mBy']
  .map(id => document.getElementById(id)).filter(el => !el.hidden).map(el => {
  const r = el.getBoundingClientRect();
  return {name: el.id, x: r.left, y: r.top, r: r.right, b: r.bottom}; })"""
BUSY = {"widgets": {"clock": {"on": True, "slot": "tl"}, "music": {"on": True, "slot": "tl"},
                    "system": {"on": True, "slot": "bl"}, "brand": {"on": True, "slot": "center"}}}
#: The words keep at least this much sky between themselves and anything else on the desktop.
CLEARANCE = 16


@pytest.mark.parametrize("size", [(1366, 768), (1920, 1080)])
@pytest.mark.parametrize("layout", ["default", "busy"])
def test_widgets_never_cover_the_words(desk, size, layout):
    save(desk.state, report())
    if layout == "busy":
        desk.config.write_text(json.dumps(BUSY), encoding="utf-8")
    page = _open(desk, size=size)
    page.wait_for_timeout(600)                            # widgets settle, then the fit runs
    obstacles, text = page.evaluate(OBSTACLES), page.evaluate(TEXT)
    names = {o["name"] for o in obstacles}
    assert "clock" in names and "music" in names and any("composer" in n for n in names)
    if layout == "busy":
        assert "system" in names                          # the CPU line
    assert {t["name"] for t in text} >= {"mSay", "mMore", "mDots", "mActs", "mBy"}
    for t in text:
        assert t["x"] >= 0 and t["y"] >= 0 and t["r"] <= size[0] and t["b"] <= size[1], t
        for o in obstacles:
            c = CLEARANCE
            apart = (t["r"] + c <= o["x"] or o["r"] + c <= t["x"] or t["b"] + c <= o["y"]
                     or o["b"] + c <= t["y"])
            assert apart, (layout, size, t, o)


@pytest.mark.parametrize("widgets, shown", [
    ({}, True),                                            # the clock is in the other corner
    ({"clock": {"on": True, "slot": "tl"}}, True),         # the words move to the free side
    ({"clock": {"on": True, "slot": "bl"}}, False),        # right under the words: said already
    ({"clock": {"on": False}}, True),                      # no clock: the words say the date
])
def test_the_date_line_is_left_to_a_clock_on_the_same_side(desk, widgets, shown):
    save(desk.state, report())
    desk.config.write_text(json.dumps({"widgets": widgets}), encoding="utf-8")
    context = desk.browser.new_context(viewport={"width": 1920, "height": 1080})
    page = context.new_page()
    page.route(re.compile(r"https://.*"), lambda route: route.abort())
    page.goto(desk.base + "/ambient", wait_until="load")
    assert _poll(page, SHOWING)
    page.wait_for_timeout(600)
    assert page.evaluate("() => !document.getElementById('mWhen').hidden") is shown, widgets
    side = page.evaluate("() => document.getElementById('morning').dataset.side")
    clock = page.evaluate("""() => { const c = document.querySelector('.widget[data-widget="clock"]');
      if (!c) return ''; const r = c.getBoundingClientRect(); return r.left + r.width / 2 < innerWidth / 2 ? 'left' : 'right'; }""")
    assert (clock == side) is (not shown) or not clock


def test_the_morning_blocks_own_styles_never_reach_the_page_itself(desk):
    """body carries the class "morning" while the scene shows; the block's layout, colour and
    click-through must stay on the block."""
    save(desk.state, report())
    page = _open(desk)
    body = page.evaluate("""() => { const b = getComputedStyle(document.body);
      return {position: b.position, pointer: b.pointerEvents, display: b.display, opacity: b.opacity}; }""")
    assert body == {"position": "static", "pointer": "auto", "display": "block", "opacity": "1"}
    # The centre slot covers the screen; its empty space must not start taking clicks.
    assert page.evaluate("() => getComputedStyle(document.getElementById('slot-center')).pointerEvents") == "none"
    # A reply dims the widgets and the words, never the page with the reply on it.
    page.evaluate("() => document.body.classList.add('chatting')")
    assert page.evaluate("() => getComputedStyle(document.body).opacity") == "1"
    assert _poll(page, "() => getComputedStyle(document.getElementById('morning')).opacity === '0.42'")


def test_the_code_map_keeps_its_dark_look_over_a_bright_morning(desk, monkeypatch):
    """With the code map on, a run over a clear-sky morning must look as it does any other time:
    light clock with its shadow, dark composer. The morning's own tokens are for the sky only."""
    from harness import webapp
    monkeypatch.setenv("COLLIE_DESKTOP_CODE_MAP", "on")
    save(desk.state, report())
    page = desk.browser.new_context(viewport={"width": 1920, "height": 1080},
                                    color_scheme="light").new_page()
    page.route(re.compile(r"https://.*"), lambda route: route.abort())
    page.route(re.compile(r".*/wallpaper\?bg=1.*"), lambda route: route.fulfill(
        status=200, content_type="text/html", body="<!doctype html><title>map</title>"))
    subscribers = len(webapp.Handler._live_subs)
    page.goto(desk.base + "/ambient", wait_until="load")
    assert _poll(page, SHOWING) and _poll(page, "() => document.body.classList.contains('sky-light')")
    deadline = time.time() + 10
    while len(webapp.Handler._live_subs) <= subscribers and time.time() < deadline:
        time.sleep(0.05)
    webapp.Handler._live_pub("start", {"session": "morning-code-map", "run": "r1"})
    for _ in range(40):          # until the page has read the setting that lets the map in
        webapp.Handler._live_pub("edit", {"session": "morning-code-map", "path": "harness/x.py"})
        if _poll(page, "() => document.body.classList.contains('coding')", seconds=0.3):
            break
    assert _poll(page, "() => document.body.classList.contains('coding')")
    look = page.evaluate("""() => { const c = getComputedStyle(document.querySelector('.slot .clock'));
      const box = getComputedStyle(document.querySelector('.composer'));
      return {clock: c.color, shadow: c.textShadow, composer: box.backgroundColor}; }""")
    assert _luminance(look["clock"]) > 0.8, look                  # light words on the dark map
    assert look["shadow"] != "none", look
    assert _luminance(look["composer"]) < 0.3, look              # the dark panel, not white
    assert page.evaluate("() => window.collieMorning.running()") is False


def _wcag(a, b):
    def lum(rgb):
        out = []
        for c in rgb:
            c = c / 255
            out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


READABLE = [("#mWhen", "date line"), ("#mMore", "summary"), ("#mDots span", "dots label"),
            ("#mByText", "by-line"), ("#mOpen", "Open report"), (".m-pill.go", "first button"),
            (".m-pill.soft", "glass button")]


def test_every_small_word_reads_at_4_5_to_1_on_every_sky(desk):
    """The sky actually painted behind each word (a still frame with the words made clear),
    blended with the word's own colour: the worst tenth of the box must reach WCAG AA."""
    image = pytest.importorskip("PIL.Image")
    import io
    save(desk.state, report())
    low = []
    for code, is_day, sky, _dark in [s for s in SCENES if s[:2] in ((0, 1), (3, 1), (61, 1), (95, 1),
                                                                     (73, 1), (45, 1), (0, 0))]:
        desk.weather["now"] = dict(CLEAR_DAY, code=code, is_day=is_day, fetched_at=int(time.time()))
        page = _open(desk, reduced=True)
        assert _poll(page, "(sky) => document.body.dataset.sky === sky && window.collieMorning.frames() > 0", sky)
        where, settled = None, False                # the words have found their place
        for _ in range(30):
            page.wait_for_timeout(250)
            now = page.evaluate("() => JSON.stringify(document.getElementById('morning').getBoundingClientRect())")
            settled, where = now == where, now
            if settled:
                break
        assert settled
        # The box of the words themselves (a range over the text), so a button's round ends,
        # which show the sky and carry no text, are not measured as if they did.
        boxes = page.evaluate("""(sels) => sels.map(sel => { const el = document.querySelector(sel);
            if (!el || el.closest('[hidden]')) return null;
            const range = document.createRange(); range.selectNodeContents(el);
            const r = range.getBoundingClientRect(), c = getComputedStyle(el).color.match(/[\\d.]+/g).map(Number);
            return {x: r.left, y: r.top, w: r.width, h: r.height, rgb: c.slice(0, 3), a: c.length > 3 ? c[3] : 1}; })""",
                              [sel for sel, _ in READABLE])
        page.add_style_tag(content="#morning, #morning * {color: transparent !important; text-shadow: none !important}")
        page.wait_for_timeout(100)
        shot = image.open(io.BytesIO(page.screenshot())).convert("RGB")
        for (sel, name), box in zip(READABLE, boxes):
            if not box:
                continue
            ratios = []
            for y in range(int(box["y"]), int(box["y"] + box["h"]), 2):
                for x in range(int(box["x"]), int(box["x"] + box["w"]), 3):
                    under = shot.getpixel((x, y))
                    ink = tuple(box["a"] * c + (1 - box["a"]) * u for c, u in zip(box["rgb"], under))
                    ratios.append(_wcag(ink, under))
            ratios.sort()
            worst = ratios[len(ratios) // 10]
            if worst < 4.5:
                low.append("%s: %s %.2f" % (sky, name, worst))
        page.context.close()
    assert not low, "below 4.5:1 -- " + "; ".join(low)


def test_a_morning_closed_for_the_day_can_be_shown_again_from_the_widget_panel(desk):
    from harness import morning_desktop as md
    save(desk.state, report())
    page = _open(desk)
    page.click("#mClose")
    assert _poll(page, NORMAL)
    page.click("#editbtn")
    again = page.locator('#widgetPanel [data-wp="morning:again"]')
    assert again.is_visible()
    again.click()
    assert _poll(page, SHOWING)
    assert md.today(desk.state, now=at(8))["show"] is True
    assert page.locator('#widgetPanel [data-wp="morning:again"]').count() == 0


def test_turning_the_morning_back_on_brings_back_one_closed_for_the_day(desk, monkeypatch):
    from harness import morning_desktop as md
    save(desk.state, report())
    page = _open(desk)
    page.click("#mClose")
    assert _poll(page, NORMAL)
    page.click("#editbtn")
    page.locator('#widgetPanel [data-wp="morning:on"]').uncheck()
    page.wait_for_timeout(500)
    page.locator('#widgetPanel [data-wp="morning:on"]').check()
    assert _poll(page, SHOWING)
    # The same from Settings: off, then on again.
    page.click("#editbtn")
    page.click("#mClose")
    assert _poll(page, NORMAL)
    monkeypatch.setenv("COLLIE_DESKTOP_MORNING_REPORT", "off")
    page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(16000)                              # the page re-reads Settings every 15 s
    monkeypatch.setenv("COLLIE_DESKTOP_MORNING_REPORT", "on")
    assert _poll(page, SHOWING, seconds=20)
    assert md.today(desk.state, now=at(8))["show"] is True


def test_the_widget_panel_turns_the_morning_scene_off_and_on(desk):
    save(desk.state, report())
    page = _open(desk)
    page.click("#editbtn")
    toggle = page.locator('#widgetPanel [data-wp="morning:on"]')
    assert toggle.is_checked()
    toggle.uncheck()
    assert _poll(page, NORMAL)
    assert json.loads(desk.config.read_text(encoding="utf-8"))["widgets"]["morning"]["on"] is False
    page.locator('#widgetPanel [data-wp="morning:on"]').check()
    assert _poll(page, SHOWING)
