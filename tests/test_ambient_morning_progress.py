"""The morning scene's progress on the real wallpaper page: dots, sentence, the check, all clear.

Same setup as test_ambient_morning (fixture report, fixed server clock, fixed weather, every
https request aborted). Page timers run on Playwright's clock, fast-forwarded so a minute passes at once, and
reduced motion keeps the sky to one still frame while it does.
"""
import pytest

from _morning_fixture import at, report, save
from test_ambient_morning import NORMAL, _open, _poll, desk  # noqa: F401 - fixture

pytest.importorskip("playwright.sync_api")

SAYS = "(text) => document.getElementById('mSay').textContent === text"


def test_progress_follows_the_server_and_the_check_marks_an_item_done(desk):
    from harness import morning_desktop as md
    save(desk.state, report())
    page = _open(desk, fake_clock=True, reduced=True)
    keys = {i["title"]: i["key"] for i in md.today(desk.state, now=at(8))["items"]}
    md.act(desk.state, {"action": "done", "keys": [keys["Review Ana's pull request"]]}, now=at(8))
    page.clock.fast_forward(61000)                             # the page asks again each minute
    assert _poll(page, SAYS, "One down, four to go.")
    assert page.inner_text("#mDots").strip() == "1 of 5 done"
    assert page.locator("#mDots i.on").count() == 1
    # The small check beside a pill marks that one done, here and on the server.
    act = page.locator("#mActs .m-act").nth(1)
    assert act.locator("a").inner_text() == "Answer Collie's question"
    act.hover()
    act.locator("button.m-check").click()
    assert _poll(page, SAYS, "Two down, three to go.")
    assert md.today(desk.state, now=at(8))["done"] == 2
    assert "Undo" in page.inner_text("#mNote")
    page.click("#mNote button")
    assert _poll(page, SAYS, "One down, four to go.")
    assert md.today(desk.state, now=at(8))["done"] == 1


def test_all_done_is_a_moment_and_then_the_desktop_returns_to_normal(desk):
    from harness import morning_desktop as md
    save(desk.state, report())
    keys = {i["title"]: i["key"] for i in md.today(desk.state, now=at(8))["items"]}
    md.act(desk.state, {"action": "done", "keys": [k for t, k in keys.items()
                                                   if t != "Pay the Azure invoice"]}, now=at(8))
    page = _open(desk, fake_clock=True, reduced=True)
    assert page.inner_text("#mSay") == "One to go. Almost there."
    act = page.locator("#mActs .m-act").first
    act.hover()
    act.locator("button.m-check").click()
    assert _poll(page, SAYS, "All clear for today.")
    assert page.locator("#mDots i.on").count() == 5 and page.locator("#mActs a").count() == 0
    assert page.evaluate("() => document.body.classList.contains('all-clear')")
    assert page.inner_text("#mNote") == ""
    page.clock.fast_forward(15000)
    assert _poll(page, NORMAL)
    assert md.today(desk.state, now=at(8, 5))["why"] == "dismissed"


def test_while_it_shows_the_page_asks_for_the_resolve_pass(desk, monkeypatch):
    from harness import morning_desktop as md
    save(desk.state, report())
    started = []
    monkeypatch.setattr(md, "_spawn", started.append)
    page = _open(desk, fake_clock=True, reduced=True)
    assert len(started) == 1                                   # the first answer asked for it
    desk.clock["now"] = at(8, 5)
    page.clock.fast_forward(5 * 60 * 1000)                     # the page asks again ...
    page.wait_for_timeout(500)
    assert len(started) == 1                                   # ... but not for another pass
    desk.clock["now"] = at(8, 16)
    page.clock.fast_forward(11 * 60 * 1000)
    for _ in range(50):
        if len(started) == 2:
            break
        page.wait_for_timeout(100)
    assert len(started) == 2
