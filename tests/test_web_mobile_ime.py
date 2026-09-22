"""On the phone, Enter belongs to the keyboard until the candidate is committed.

Typing Chinese, Japanese or Korean means typing latin letters into an *open composition* and then
pressing Enter to accept the candidate the keyboard is offering.  The phone composer treated every
unshifted Enter as "send", so that first Enter shipped the half-typed romanisation — `说明：pinyin` —
to /api/stream and emptied the box before the person had chosen a single character.

These tests drive the real mobile.html in Chromium against the same staged HTTP fixture as
test_web_ui_run_status, and start a genuine composition through CDP `Input.imeSetComposition`, so the
keydown they assert on is a trusted Enter with `isComposing` true — the event an IME actually
delivers.  What they check is what a person sees: the text is still in the box, nothing was
requested, and the *next* Enter sends the committed text once.

Coverage and its limit.  CDP composition is Chromium's own IME path, not a physical Android/iOS
keyboard, and Blink's ordering (keydown, then compositionend) is only one of the two orderings in the
wild: WebKit and older Android keyboards end the composition *before* the confirming keydown and may
report only the legacy `keyCode` 229.  That second ordering cannot be produced by CDP here, so it is
covered by a synthetic replay of exactly that event sequence, which the page must also refuse to
treat as a send.
"""
import json
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import phone, server, browser, _Fixture   # noqa: F401

PREFIX = "说明："                 # what is already committed when the composition opens
PINYIN = "pinyin"                 # what the keyboard is showing while it waits for a choice
HANZI = "拼音"                    # what the person actually meant


def compose(phone, prefix, text):
    """Open a real Chromium composition of `text` after `prefix`, the way a keyboard does."""
    page = phone.page
    page.locator("#input").fill(prefix)
    page.evaluate("""() => { const el = document.querySelector('#input'); el.focus();
                            el.setSelectionRange(el.value.length, el.value.length); }""")
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.imeSetComposition", {"text": text, "selectionStart": len(text),
                                         "selectionEnd": len(text)})
    expect(page.locator("#input")).to_have_value(prefix + text)
    return cdp


def watch_posts(page, path):
    """Every body the page POSTs to `path` from now on, in order."""
    seen = []
    page.on("request", lambda r: r.method == "POST" and urlparse(r.url).path == path
            and seen.append(json.loads(r.post_data or "{}")))
    return seen


def asked():
    """The `q` of every /api/stream the page opened, in order."""
    return [row["q"] for row in _Fixture.stream_requests]


def wait_idle(phone):
    """Back to a composer that would start a new run rather than steer the last one."""
    expect(phone.page.locator("#send")).not_to_have_class("send stop", timeout=8000)


# ------------------------------- the composition itself

def test_the_enter_that_picks_a_candidate_neither_sends_nor_empties_the_box(phone):
    page = phone.page
    cdp = compose(phone, PREFIX, PINYIN)

    page.keyboard.press("Enter")                     # trusted Enter, isComposing = true
    page.wait_for_timeout(250)

    assert asked() == [], "the romanisation was sent before a character was chosen"
    assert page.locator("#input").input_value() == PREFIX + PINYIN, "the box was emptied mid-word"
    assert PINYIN not in page.inner_text("#log"), "and it was not posted into the thread either"

    cdp.send("Input.insertText", {"text": HANZI})    # the candidate the person picks
    expect(page.locator("#input")).to_have_value(PREFIX + HANZI)
    page.keyboard.press("Enter")                     # now an ordinary Enter
    page.wait_for_timeout(400)

    assert asked() == [PREFIX + HANZI], "the committed text must be sent exactly once"
    assert page.locator("#input").input_value() == ""
    assert PREFIX + HANZI in page.inner_text("#log")


def test_the_keydown_the_page_refused_really_was_a_composing_enter(phone):
    """The guard is about the browser's composition state, not about a flag we invented."""
    page = phone.page
    page.evaluate("""() => { window.imeSeen = [];
        document.querySelector('#input').addEventListener('keydown', e =>
            window.imeSeen.push({key: e.key, trusted: e.isTrusted,
                                 composing: e.isComposing, legacy: e.keyCode})); }""")
    compose(phone, PREFIX, PINYIN)
    page.keyboard.press("Enter")
    page.wait_for_timeout(200)

    seen = page.evaluate("window.imeSeen")
    assert [e for e in seen if e["key"] == "Enter" and e["trusted"] and e["composing"]], seen
    assert asked() == [], seen


# ------------------------------- the other ordering, and the legacy signal

LEGACY = """() => {
  const el = document.querySelector('#input');
  el.focus();
  const probe = new KeyboardEvent('keydown', {key: 'Enter', keyCode: 229,
                                              bubbles: true, cancelable: true});
  if (probe.keyCode !== 229) return 'unsupported';
  // WebKit / older Android order: the composition ends first, so the confirming keydown
  // reports isComposing = false and only the legacy keyCode 229 identifies it.
  el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
  el.dispatchEvent(new CompositionEvent('compositionend', {bubbles: true, data: 'x'}));
  el.dispatchEvent(probe);
  return 'dispatched';
}"""


def test_a_confirming_enter_that_arrives_after_compositionend_is_still_not_a_send(phone):
    page = phone.page
    page.locator("#input").fill(PREFIX + PINYIN)
    if page.evaluate(LEGACY) == "unsupported":
        pytest.skip("this browser does not carry the legacy keyCode on a synthetic keydown")
    page.wait_for_timeout(200)

    assert asked() == [], "a keyCode 229 Enter is the keyboard confirming, not the person sending"
    assert page.locator("#input").input_value() == PREFIX + PINYIN

    page.locator("#input").fill(PREFIX + HANZI)      # the person then sends, deliberately
    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    assert asked() == [PREFIX + HANZI], "the guard must not outlive the composition"


# ------------------------------- everything Enter still has to do

def test_an_ordinary_enter_and_the_send_button_each_send_once(phone):
    # Two staged runs, because the same run id twice in one page is deduplicated as a replay.
    page = phone.page
    page.locator("#input").fill("shrink the history")
    page.keyboard.press("Enter")
    wait_idle(phone)
    assert asked() == ["shrink the history"]
    assert page.locator("#input").input_value() == ""

    page.locator("#input").fill("summarize the design doc")
    page.click("#send")
    wait_idle(phone)
    assert asked() == ["shrink the history", "summarize the design doc"]


def test_shift_enter_still_makes_a_newline_and_sends_nothing(phone):
    page = phone.page
    page.locator("#input").fill("first line")
    page.evaluate("""() => { const el = document.querySelector('#input'); el.focus();
                            el.setSelectionRange(el.value.length, el.value.length); }""")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("second line")
    page.wait_for_timeout(150)

    assert page.locator("#input").input_value() == "first line\nsecond line"
    assert asked() == []

    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    assert asked() == ["first line\nsecond line"]


# ------------------------------- and the same key while a run is going

def test_a_composition_during_an_active_run_never_steers_half_typed_text(phone):
    page = phone.page
    steers = watch_posts(page, "/api/steer")
    try:
        page.locator("#input").fill("Hold queue fixture")
        page.keyboard.press("Enter")
        expect(page.locator("#input")).to_have_attribute(
            "placeholder", "Steer while Collie works…", timeout=8000)

        cdp = compose(phone, PREFIX, PINYIN)
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)

        assert steers == [], "half-typed romanisation was steered into the running worker"
        assert page.locator("#input").input_value() == PREFIX + PINYIN

        cdp.send("Input.insertText", {"text": HANZI})
        expect(page.locator("#input")).to_have_value(PREFIX + HANZI)
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)

        assert [s.get("q") for s in steers] == [PREFIX + HANZI], steers
        assert asked() == ["Hold queue fixture"], "steering must not open a second run"
    finally:
        _Fixture.queue_release.set()
        page.wait_for_timeout(300)
