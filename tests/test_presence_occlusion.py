"""Collie's presence pill must not stand between a click and the page, in a real Chromium.

The pill is fixed over the page's top-right corner, where sites keep account menus, settings and
close buttons. It took every click on it, so on 2026-09-25 a person could not reach a dialog's close
button under it, and Collie's own approved click on the Settings button of its own desktop page was
refused as "moved or became covered before click". The page point for a text or selector click was
not checked at all, so that click landed on the pill (and could have pressed its Stop).

The real presence.js runs here with chrome.runtime stubbed, next to the real page functions sliced out
of background.js, because the question is layout and hit-testing, which only a browser answers.
"""
import os
import re

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

EXT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness", "browser_ext")

STUB = """
window.chrome = {runtime: {lastError: null,
  onMessage: {addListener: function (f) { window.__presenceOnMessage = f; }},
  sendMessage: function (msg, cb) {
    (window.__sent = window.__sent || []).push(msg);
    if (cb) cb({state: {attached: true, state: "observing", reason: "screenshot"}});
  }}};
"""


def _function(name):
    """The shipped function, sliced out of background.js by brace matching (as browser_ext_test.js does)."""
    lines = open(os.path.join(EXT, "background.js"), encoding="utf-8").read().replace("\r\n", "\n").split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("function %s(" % name))
    depth, out = 0, []
    for line in lines[start:]:
        out.append(line)
        code = line.split("//")[0]
        depth += code.count("{") - code.count("}")
        if depth <= 0 and "{" in "".join(out):
            break
    return "\n".join(out)


@pytest.fixture
def page():
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 1000, "height": 700})
        pg.set_content("<!doctype html><body style='margin:0;height:1400px'></body>")
        pg.add_script_tag(content=STUB)
        pg.add_script_tag(content=open(os.path.join(EXT, "presence.js"), encoding="utf-8").read())
        pg.wait_for_function("(() => { const h = document.getElementById('__colliePresenceHost');"
                             " return h && h.getBoundingClientRect().width > 100; })()")
        yield pg
        browser.close()


def _target_under(pg, where):
    """Put a fixed button under the pill's copy ("copy") or its Stop button ("stop"); return its centre."""
    return pg.evaluate("""(where) => {
      const r = document.getElementById('__colliePresenceHost').getBoundingClientRect();
      const x = where === 'stop' ? r.right - 18 : r.left + 40, y = r.top + r.height / 2;
      const b = document.createElement('button');
      b.id = 'target'; b.textContent = 'Account';
      b.style.cssText = 'position:fixed;width:24px;height:16px;left:' + (x - 12) + 'px;top:' + (y - 8) + 'px';
      b.onclick = () => { window.__clicked = (window.__clicked || 0) + 1; };
      document.body.appendChild(b);
      return [x, y];
    }""", where)


def test_a_click_on_the_pills_body_reaches_the_page_under_it(page):
    x, y = _target_under(page, "copy")
    page.mouse.click(x, y)
    assert page.evaluate("window.__clicked || 0") == 1, "the pill swallowed the click"
    assert not any(m.get("type") == "collie:pause" for m in page.evaluate("window.__sent"))


def test_the_stop_button_still_takes_its_click(page):
    r = page.evaluate("(() => { const r = document.getElementById('__colliePresenceHost')"
                      ".getBoundingClientRect(); return [r.right - 18, r.top + r.height / 2]; })()")
    page.mouse.click(*r)
    assert any(m.get("type") == "collie:pause" for m in page.evaluate("window.__sent"))


@pytest.mark.parametrize("fn", ["pagePointStillRef", "pagePointRef"])
def test_an_approved_click_under_the_stop_button_moves_the_pill_first(page, fn):
    x, y = _target_under(page, "stop")
    page.evaluate("window.__collieRefs = new Map([['e1', document.getElementById('target')]])")
    point = page.evaluate("(() => { %s\n return %s('e1'); })()" % (_function(fn), fn))
    assert not point.get("error"), point
    top = page.evaluate("document.elementFromPoint(%f, %f).id" % (point["x"], point["y"]))
    assert top == "target", "the pill still covers the approved control"
    page.mouse.click(point["x"], point["y"])
    assert page.evaluate("window.__clicked || 0") == 1
    assert not any(m.get("type") == "collie:pause" for m in page.evaluate("window.__sent")), \
        "Collie's own click pressed its Stop"


def test_a_text_click_under_the_stop_button_moves_the_pill_first(page):
    _target_under(page, "stop")
    point = page.evaluate("(() => { %s\n return pagePoint('Account', ''); })()" % _function("pagePoint"))
    assert not point.get("error"), point
    assert page.evaluate("document.elementFromPoint(%f, %f).id" % (point["x"], point["y"])) == "target"
