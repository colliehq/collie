"""What the run control and the icon-only sidebar are *called*, read off the live page.

Two defects a Chinese-reading person met on the real product page, both about names, not behaviour:

  * the send button starts out localized, but every run-state change rewrote its title and
    aria-label with English literals ("Stop run", "Stopping…", "Send"), so after one run the
    control was called Send in an otherwise Chinese page — and a language change landing mid-run
    put "Send" back on a button that was still cancelling a live run;
  * below 900px the sidebar hides each item's label text, which left New task and the primary nav
    buttons with no accessible name at all on a 390x844 phone.

Everything here is read from the rendered page: `title`, the accessible name Chromium computes
(Playwright's role+name engine), computed visibility, and the run state the page shows from
outside — the page script is an IIFE, so no test here reaches into its variables. The staged SSE
server, the held-run script and the run observer come from test_web_ui_run_status: real
index.html, real EventSource, real fetch paths, with only the answers staged. No provider is
contacted, nothing resumes on its own, and the language change is the product's own Settings write.
"""
import json

import pytest

from test_web_ui_run_status import (  # noqa: F401  (fixtures are used by name)
    TOKEN, _Fixture, _reset_fixture_state, browser, server, mark_run, await_run,
)

@pytest.fixture(autouse=True)
def _restore_fixture_language(monkeypatch):
    # This fixture class is shared with the existing run-status suite. Restore
    # its original language even if this test fails during setup.
    monkeypatch.setattr(_Fixture, "lang", _Fixture.lang)


# The strings the shipped dictionaries carry for the three states, per language: (title, name).
NAMES = {
    "en": {"idle": ("Send", "Send"), "running": ("Stop run", "Stop run"),
           "stopping": ("Stopping…", "Stopping run")},
    "zh": {"idle": ("发送", "发送"), "running": ("停止运行", "停止运行"),
           "stopping": ("正在停止…", "正在停止运行")},
}
# Sidebar items whose label text the responsive rules hide, with their localized names.
NAV = {
    "en": [("newChat", "New task"), ("navHome", "Today"), ("navMissions", "Tasks"),
           ("navLibrary", "Apps & connections")],
    "zh": [("newChat", "新任务"), ("navHome", "今天"), ("navMissions", "任务"),
           ("navLibrary", "应用与连接")],
}

PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1280, "height": 900}


def _route_settings(page, state):
    """Serve (and echo) the language in the shapes the product server uses, nothing else.

    A language change has to come through the real Settings write, and that write reads the
    language back out of the server's answer, so the schema and values are served in the shapes
    /api/settings really uses and the POST echoes what it stored. Only the Language row is
    described: no other setting is offered, and every POST is recorded so a test can say what
    was sent.
    """
    schema = [{"key": "LANG", "type": "select", "label": "Language", "group": "General",
               "options": [{"value": "auto", "label": "Auto"}, {"value": "en", "label": "English"},
                           {"value": "zh", "label": "简体中文"},
                           {"value": "zh-tw", "label": "繁體中文"}]}]
    def handler(route, request):
        if request.method == "POST":
            body = request.post_data_json or {}
            state["posts"].append(body)
            if "LANG" in body:
                state["lang"] = body["LANG"]
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"ok": True, "values": {"LANG": state["lang"]}}))
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"schema": schema,
                                              "values": {"LANG": state["lang"]}}))
    page.route("**/api/settings*", handler)


def _open(server, browser, lang, viewport, extra_routes=None):
    _reset_fixture_state()
    _Fixture.lang = lang
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    state = {"lang": lang, "posts": []}
    _route_settings(page, state)
    if extra_routes:
        extra_routes(page)
    page.goto(server + "/?token=" + TOKEN, wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    page.wait_for_timeout(400)      # the boot /api/settings answer lands and applyLang() runs
    assert page.evaluate("() => document.documentElement.lang") == lang
    return context, page, errors, state


def _switch_lang(page, lang):
    """Change the language the way a person does: Settings -> Advanced -> Language."""
    page.click("#settingsBtn")
    page.wait_for_selector(".set-nav[data-cat='advanced']", timeout=4000)
    page.click(".set-nav[data-cat='advanced']")
    page.wait_for_selector("#set_LANG", timeout=4000)
    page.select_option("#set_LANG", lang)
    page.wait_for_function("l => document.documentElement.lang === l", arg=lang, timeout=5000)
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)


def _run_state(page):
    """What the page shows about the run from outside — the signals a person can see."""
    return page.evaluate("""() => {
      const b = document.getElementById('send'), pill = document.getElementById('statePill');
      return {stop: b.classList.contains('stop'), disabled: b.disabled,
              live: pill.classList.contains('live'),
              input_disabled: document.getElementById('input').disabled,
              gate: (document.getElementById('gate') || {}).getAttribute
                    ? document.getElementById('gate').getAttribute('data-state') : null};
    }""")


def _send_labels(page):
    return page.evaluate("""() => {
      const b = document.getElementById('send');
      return {title: b.title, aria: b.getAttribute('aria-label')};
    }""")


def _assert_send(page, lang, state):
    title, name = NAMES[lang][state]
    got = _send_labels(page)
    assert got["title"] == title, "title for %s/%s: %r" % (lang, state, got)
    assert got["aria"] == name, "aria-label for %s/%s: %r" % (lang, state, got)
    # the name Chromium actually computes for the control, not just the attribute
    assert page.get_by_role("button", name=name, exact=True).count() == 1, \
        "no button named %r on the page (%s/%s)" % (name, lang, state)


# --------------------------------------------------------------- the run control
@pytest.mark.parametrize("lang", ["en", "zh"])
def test_send_control_is_named_for_its_state_in_this_language(server, browser, lang):
    """idle -> running -> cancel-pending -> finished, with the language switched at every stop."""
    context, page, errors, state = _open(server, browser, lang, DESKTOP)
    other = "zh" if lang == "en" else "en"
    try:
        _assert_send(page, lang, "idle")

        # A run that stays live: the fixture holds the stream open until it is released.
        page.fill("#input", "Hold queue fixture")
        page.press("#input", "Enter")
        page.wait_for_function("() => document.getElementById('send').classList.contains('stop')",
                               timeout=8000)
        assert _run_state(page)["live"] is True
        _assert_send(page, lang, "running")

        # A language change while the run is live must re-translate *stop*, not restore Send.
        _switch_lang(page, other)
        _assert_send(page, other, "running")
        assert _run_state(page)["stop"] is True, "the control must still be the stop button"
        _switch_lang(page, lang)
        _assert_send(page, lang, "running")

        # Press the same control: it cancels. The fixture answers the cancel without a terminal
        # verdict, so the page stays cancel-pending — which is when apply_i18n said "Send".
        page.click("#send")
        page.wait_for_function("() => document.getElementById('send').disabled === true",
                               timeout=8000)
        _assert_send(page, lang, "stopping")
        before = _run_state(page)
        _switch_lang(page, other)
        _assert_send(page, other, "stopping")
        assert _run_state(page) == before, \
            "a language change must not move run or cancellation state: %r" % before
        assert page.locator("#send.stop").count() == 1
        _switch_lang(page, lang)
        _assert_send(page, lang, "stopping")

        # Let the held run land. Only now is the control called Send again.
        _Fixture.queue_release.set()
        page.wait_for_function("() => !document.getElementById('send').classList.contains('stop')",
                               timeout=10000)
        page.wait_for_timeout(200)
        _assert_send(page, lang, "idle")
        _switch_lang(page, other)
        _assert_send(page, other, "idle")
        assert [p for p in state["posts"] if "LANG" in p], "no language write reached the server"
    finally:
        _Fixture.queue_release.set(); _Fixture.queue_ack.set()
        context.close()
        assert errors == [], "JS errors: %r" % errors


def test_a_finished_run_leaves_the_localized_send_name(server, browser):
    """The plain path the report describes: one ordinary run, then read the button."""
    context, page, errors, _state = _open(server, browser, "zh", DESKTOP)
    try:
        _assert_send(page, "zh", "idle")
        page.fill("#input", "Summarise the ledger module")
        token = mark_run(page)
        page.press("#input", "Enter")
        await_run(page, token)
        _assert_send(page, "zh", "idle")
    finally:
        context.close()
        assert errors == [], "JS errors: %r" % errors


# ------------------------------------------------------------ the icon-only rail
@pytest.mark.parametrize("lang", ["en", "zh"])
@pytest.mark.parametrize("viewport,phone", [(DESKTOP, False), (PHONE, True)])
def test_sidebar_buttons_have_localized_names_when_their_text_is_hidden(
        server, browser, lang, viewport, phone):
    context, page, errors, _state = _open(server, browser, lang, viewport)
    try:
        for bid, name in NAV[lang]:
            shown = page.evaluate("""id => {
              const el = document.getElementById(id);
              const span = el.querySelector('span:not(.side-badge):not(.online-nav-status)');
              const cs = span && getComputedStyle(span);
              return {hidden: !span || cs.display === 'none' || cs.visibility === 'hidden',
                      clickable: !!el.offsetParent, width: el.getBoundingClientRect().width};
            }""", bid)
            assert shown["hidden"] is phone, \
                "%s label visibility at %r: %r" % (bid, viewport, shown)
            assert shown["clickable"] and shown["width"] > 0, \
                "%s must stay laid out and pressable: %r" % (bid, shown)
            assert page.locator("#" + bid).get_attribute("aria-label") == name
            assert page.get_by_role("button", name=name, exact=True).count() == 1, \
                "%s has no computed name %r at %r" % (bid, name, viewport)
        # "More" is a <summary>; it opens the same rail's overflow and must be named too.
        assert page.locator("#sideMore summary").get_attribute("aria-label") == \
            ("More" if lang == "en" else "更多")
        # Nothing on the rail may be left nameless at phone width.
        nameless = page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('.side-nav-item, .newbtn').forEach(el => {
            if (!el.offsetParent) return;                       // hidden rows do not count
            const name = (el.getAttribute('aria-label') || el.innerText || '').trim();
            if (!name) out.push(el.id || el.className);
          });
          return out;
        }""")
        assert nameless == [], "unnamed rail controls: %r" % nameless
    finally:
        context.close()
        assert errors == [], "JS errors: %r" % errors


# ------------------------------------------------- the status dots carry live state
# The two dots the pollers own, with the answer each endpoint really gives for the on and the off
# state, plus the shipped strings for both. The page fetches these itself: nothing is poked into
# the DOM and no page variable is read.
DOTS = {
    "live": {
        "url": "**/api/live-copilot*", "nav": "navLive", "dot": "liveNavStatus",
        "on": {"active": True, "listen": False, "session_id": "s-live"},
        "off": {"active": False, "listen": False, "session_id": ""},
        "label": {"en": "Live Copilot", "zh": u"\u5b9e\u65f6\u534f\u4f5c"},
        "on_text": {"en": "Maintaining context", "zh": u"\u6b63\u5728\u4fdd\u6301\u7406\u89e3"},
        "off_text": {"en": "Not active", "zh": u"\u672a\u5f00\u542f"},
    },
    "online": {
        # the page appends ?token= to this one, so the glob has to allow a query
        "url": "**/api/online*", "nav": "navOnline", "dot": "onlineNavStatus",
        "on": {"mode": "connected", "missions": []},
        "off": {"mode": "local", "missions": []},
        "label": {"en": "Sync & cloud", "zh": u"\u540c\u6b65\u4e0e\u4e91\u7aef"},
        "on_text": {"en": "Connected on this device", "zh": u"\u6b64\u8bbe\u5907\u5df2\u8fde\u63a5"},
        "off_text": {"en": "Not connected", "zh": u"\u672a\u8fde\u63a5"},
    },
}


def _route_dot(spec, payload):
    def routes(page):
        page.route(spec["url"], lambda route, request: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)))
    return routes


def _watch_nav(page, spec):
    """Record every title/aria-label the page writes, so a transient wrong value is caught."""
    page.evaluate("""ids => {
      window.__navSeen = [];
      const push = () => {
        const dot = document.getElementById(ids.dot), nav = document.getElementById(ids.nav);
        window.__navSeen.push([dot ? dot.title : '', nav ? nav.getAttribute('aria-label') : '']);
      };
      push();
      window.__navObs = new MutationObserver(push);
      [ids.dot, ids.nav].forEach(id => {
        const el = document.getElementById(id);
        if (el) window.__navObs.observe(el, {attributes: true,
                                             attributeFilter: ['title', 'aria-label', 'class']});
      });
    }""", {"dot": spec["dot"], "nav": spec["nav"]})


def _nav_seen(page):
    return page.evaluate("() => (window.__navSeen || []).map(pair => pair.join(' | '))")


def _reveal_nav(page, nav_id):
    """Open the rail's own overflow if the button lives there — a real click, no style edits."""
    closed = page.evaluate("""id => {
      const el = document.getElementById(id), d = el && el.closest('details');
      return !!(d && !d.open);        // a closed <details> is out of the a11y tree, CSS aside
    }""", nav_id)
    if closed:
        page.locator("#sideMore > summary").click()
        page.wait_for_function("id => !!(document.getElementById(id).closest('details') || {}).open",
                               arg=nav_id, timeout=4000)


def _assert_dot(page, spec, lang, on):
    """Tooltip and computed nav name both say the state the dot is really in. One value only."""
    want = spec["on_text" if on else "off_text"][lang]
    name = spec["label"][lang] + u" \u2014 " + want
    got = page.evaluate("""ids => {
      const dot = document.getElementById(ids.dot), nav = document.getElementById(ids.nav);
      return {title: dot.title, connected: dot.classList.contains('connected'),
              aria: nav.getAttribute('aria-label')};
    }""", {"dot": spec["dot"], "nav": spec["nav"]})
    assert got["connected"] is on, "the dot's own state changed: %r" % got
    assert got["title"] == want, "dot tooltip (%s, on=%s): %r" % (lang, on, got)
    assert got["aria"] == name, "nav aria-label (%s, on=%s): %r" % (lang, on, got)
    # The name Chromium computes, not just the attribute. Sync & cloud sits in the rail's
    # collapsed overflow, which is display:none and therefore not in the a11y tree at all; open
    # it first so the computed name is really asserted for both dots.
    _reveal_nav(page, spec["nav"])
    assert page.get_by_role("button", name=name, exact=True).count() == 1, \
        "no nav button named %r (%s, on=%s)" % (name, lang, on)


@pytest.mark.parametrize("dot", ["live", "online"])
@pytest.mark.parametrize("on", [True, False])
@pytest.mark.parametrize("lang,other", [("en", "zh"), ("zh", "en")])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_status_dot_name_follows_real_state_across_language_switches(
        server, browser, dot, on, lang, other, viewport):
    """A language change re-derives the dot's state; it never writes the off string over an on dot.

    State comes from the endpoint the page polls, answered in the shape the real server uses.
    applyLang() used to translate a stored data-i18n-title, so a switch made a connected dot say
    "Not active"/"Not connected" until the next poll — up to ~10s for cloud.
    """
    spec = DOTS[dot]
    context, page, errors, _state = _open(server, browser, lang, viewport,
                                          _route_dot(spec, spec["on" if on else "off"]))
    try:
        page.wait_for_function("""ids => {
          const el = document.getElementById(ids.dot);
          return !!el && el.classList.contains('connected') === ids.on;
        }""", arg={"dot": spec["dot"], "on": on}, timeout=12000)
        _assert_dot(page, spec, lang, on)

        _watch_nav(page, spec)
        _switch_lang(page, other)
        _assert_dot(page, spec, other, on)          # right away, not after the next poll
        wrong = spec["off_text" if on else "on_text"]
        bad = [seen for seen in _nav_seen(page)
               if wrong[lang] in seen or wrong[other] in seen]
        assert bad == [], "the switch showed the wrong state first: %r" % bad

        page.wait_for_timeout(1800)                 # the live poll runs every 1.5s
        _assert_dot(page, spec, other, on)
        _switch_lang(page, lang)
        _assert_dot(page, spec, lang, on)
    finally:
        context.close()
        assert errors == [], "JS errors: %r" % errors


# ------------------------------------------------- the run flow's own status copy
# Visible copy the phone DOM check found still in English on a Chinese page, with the shipped
# translations. Each one is written by the run flow itself — not by a provider, not by the person.
FLOW = {
    "en": {"working": u"working\u2026",
           "placeholder": u"Follow up while the worker runs \u2014 Enter queues",
           # the cancel path writes "stopping\u2026" and then, once the server accepts the request,
           # "cancellation pending\u2026"; both belong to the flow, so both must be in this language
           "cancel": (u"stopping\u2026", u"cancellation pending\u2026")},
    "zh": {"working": u"\u8fd0\u884c\u4e2d",
           "placeholder": (u"\u8fd0\u884c\u671f\u95f4\u53ef\u7ee7\u7eed\u8f93\u5165"
                           u"\u2014\u2014\u56de\u8f66\u52a0\u5165\u961f\u5217"),
           "cancel": (u"\u6b63\u5728\u505c\u6b62\u2026", u"\u6b63\u5728\u7b49\u5f85\u53d6\u6d88\u2026")},
}


def _pstat(page):
    """The status line's own words, without the elapsed clock the timer appends."""
    return page.evaluate("""() => {
      const el = document.querySelector('.pstat');
      return el ? el.textContent.replace(/ \u00b7 \\d+s$/, '') : null;
    }""")


@pytest.mark.parametrize("lang", ["en", "zh"])
@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_run_flow_status_copy_is_localized(server, browser, lang, viewport):
    """Hold a run, then cancel it: working..., the composer hint, and stopping... in-language."""
    expect = pytest.importorskip("playwright.sync_api").expect
    context, page, errors, _state = _open(server, browser, lang, viewport)
    want = FLOW[lang]
    try:
        page.fill("#input", "Hold queue fixture")
        page.press("#input", "Enter")
        page.wait_for_function("() => document.getElementById('send').classList.contains('stop')",
                               timeout=8000)
        page.wait_for_selector(".pstat", timeout=8000)
        assert _pstat(page) == want["working"], "status line: %r" % _pstat(page)
        # The composer stays unavailable until the independent capabilities
        # response arrives. A running status alone does not settle that fetch.
        expect(page.locator("#input")).to_have_attribute("placeholder", want["placeholder"])

        page.click("#send")
        page.wait_for_function("() => document.getElementById('send').disabled === true",
                               timeout=8000)
        # the cancel path writes "stopping..." and then, once the server accepts, "cancellation
        # pending..." — both belong to the flow, so both have to be in this language.
        page.wait_for_function("""cancel => {
          const el = document.querySelector('.pstat');
          return !!el && cancel.some(w => el.textContent.indexOf(w) === 0);
        }""", arg=list(want["cancel"]), timeout=8000)
        assert _pstat(page) in want["cancel"], "cancel status: %r" % _pstat(page)
        page.wait_for_timeout(300)
        assert _pstat(page) in want["cancel"], "cancel status settled: %r" % _pstat(page)
        if lang == "zh":            # no flow copy left in English on a Chinese page
            log = page.inner_text("#log")
            for english in ("working...", u"working\u2026", "stopping...", u"stopping\u2026",
                            u"still working\u2026", "Follow up while the worker runs"):
                assert english not in log, english
            assert "Follow up" not in (page.locator("#input").get_attribute("placeholder") or "")
    finally:
        _Fixture.queue_release.set(); _Fixture.queue_ack.set()
        context.close()
        assert errors == [], "JS errors: %r" % errors


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_more_tools_overflow_is_named_in_this_language(server, browser, lang):
    name = "More tools" if lang == "en" else u"\u66f4\u591a\u5de5\u5177"
    context, page, errors, _state = _open(server, browser, lang, DESKTOP)
    try:
        summary = page.locator("#topbarMore > summary")
        assert summary.get_attribute("aria-label") == name
        assert summary.get_attribute("title") == name
    finally:
        context.close()
        assert errors == [], "JS errors: %r" % errors
