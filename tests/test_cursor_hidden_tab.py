"""The visible cursor must never hold a click on a page that gets no animation frames.

pageCursor glides Collie's pointer to the target on requestAnimationFrame, and every click waits
for it to land. A page that is not on screen (a space's tab in the background, a window covered
by another) gets no frames. On 2026-09-25, in Chrome, that click never happened. Every command in
every space queued behind it, the extension stopped polling, and the click ran when the tab next
came on screen, minutes after its tool call had timed out. Each case here races the real
function, sliced out of background.js, against a 3 s deadline in a real Chromium.
"""
import os

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

BACKGROUND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "harness", "browser_ext", "background.js")


def _function(prefix):
    lines = open(BACKGROUND, encoding="utf-8").read().replace("\r\n", "\n").split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith(prefix))
    depth, started, out = 0, False, []
    for line in lines[start:]:
        out.append(line)
        code = line.split("//")[0]
        for ch in code:
            if ch == "{":
                depth, started = depth + 1, True
            elif ch == "}":
                depth -= 1
        if started and depth <= 0:
            break
    return "\n".join(out)


RACE = """async () => {
  %s
  const t0 = performance.now();
  const r = await Promise.race([pageCursor(300, 200, true),
                                new Promise((res) => setTimeout(() => res('hung'), 3000))]);
  return {r: r, ms: performance.now() - t0};
}"""


@pytest.fixture
def page():
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": 800, "height": 600})
        pg.set_content("<!doctype html><body style='margin:0'></body>")
        yield pg
        browser.close()


def _race(page):
    return page.evaluate(RACE % _function("async function pageCursor("))


def test_a_hidden_page_does_not_wait_for_the_glide(page):
    page.evaluate("""() => {
      Object.defineProperty(document, 'visibilityState', {get: () => 'hidden', configurable: true});
      window.requestAnimationFrame = () => 0;      // what a page off screen gets
    }""")
    out = _race(page)
    assert out["r"] != "hung", out
    assert out["r"]["skipped"] and out["ms"] < 500, out


def test_frames_that_stop_mid_glide_do_not_hold_the_click(page):
    # Focus emulation (the trusted path) makes a background page report itself visible, so the
    # early return does not apply there; the timer has to.
    page.evaluate("() => { window.requestAnimationFrame = () => 0; }")
    out = _race(page)
    assert out["r"] != "hung", out
    assert out["r"]["arrived"] is True and out["ms"] < 1500, out


def test_a_visible_page_still_gets_the_glide(page):
    out = _race(page)
    assert out["r"] != "hung" and out["r"]["arrived"] is True, out
    assert not out["r"].get("skipped")
