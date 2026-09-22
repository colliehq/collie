"""On the desktop composer too, Enter belongs to the keyboard until the candidate is committed.

Typing Chinese, Japanese or Korean means typing latin letters into an *open composition* and then
pressing Enter to accept the candidate the keyboard offers.  The desktop composer treated every
unshifted Enter as "send", so that first Enter started a task on the half-typed romanisation —
`说明：pinyin` went to /api/stream — and emptied the box before a single character had been chosen.
The same keydown also reached the follow-up shortcut (Ctrl/Cmd+Shift+Enter, which queues or steers
into a *running* worker) and the slash-command menu, so a composition could just as easily queue,
steer, or pick a command nobody typed.

These tests drive the real harness/webui/index.html in Chromium against the same staged HTTP fixture
as test_web_ui_run_status, and open a genuine composition through CDP `Input.imeSetComposition`, so
the keydown they assert on is a trusted Enter with `isComposing` true — the event an IME actually
delivers.  What they check is what a person sees across the page/request boundary: the text is still
in the box, no request left the page, and the *next* Enter sends the committed text exactly once.

Coverage and its limit.  CDP composition is Chromium's own IME path, not a physical Windows/macOS
IME, and Blink's ordering (keydown, then compositionend) is only one of the two orderings in the
wild: WebKit and older keyboards end the composition *before* the confirming keydown and may report
only the legacy `keyCode` 229.  That second ordering cannot be produced by CDP here, so it is covered
by an explicitly synthetic replay of exactly that event sequence, which the page must also refuse to
treat as a send.
"""
import json
from urllib.parse import urlparse

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser, _Fixture, await_run   # noqa: F401

PREFIX = "说明："                 # what is already committed when the composition opens
PINYIN = "pinyin"                 # what the keyboard shows while it waits for a choice
HANZI = "拼音"                    # what the person actually meant


def compose(ui, prefix, text):
    """Open a real Chromium composition of `text` after `prefix`, the way a keyboard does."""
    page = ui.page
    page.fill("#input", prefix)
    page.evaluate("""() => { const el = document.querySelector('#input'); el.focus();
                            el.setSelectionRange(el.value.length, el.value.length); }""")
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.imeSetComposition", {"text": text, "selectionStart": len(text),
                                         "selectionEnd": len(text)})
    expect(page.locator("#input")).to_have_value(prefix + text)
    return cdp


def commit(ui, cdp, prefix, text):
    """The candidate the person picks: the composition ends with real characters in the box."""
    cdp.send("Input.insertText", {"text": text})
    expect(ui.page.locator("#input")).to_have_value(prefix + text)


def asked():
    """The `q` of every /api/stream the page opened, in order."""
    return [row["q"] for row in _Fixture.stream_requests]


def watch_follow_ups(page):
    """Every follow-up body the page POSTs into the inbox from now on (queue *and* steer)."""
    seen = []
    page.on("request", lambda r: r.method == "POST" and urlparse(r.url).path == "/api/task-inbox"
            and seen.append(json.loads(r.post_data or "{}")))
    return seen


def hold_queue(ui):
    """Start the fixture's long-running task, so the composer is in follow-up mode."""
    ui.page.fill("#input", "Hold queue fixture")
    ui.page.press("#input", "Enter")
    ui.page.wait_for_function("() => document.getElementById('send').classList.contains('stop') && "
                              "!document.getElementById('input').disabled")
    ui.page.wait_for_timeout(100)


# ------------------------------- the composition itself

def test_the_enter_that_picks_a_candidate_neither_starts_a_task_nor_empties_the_box(ui):
    page = ui.page
    cdp = compose(ui, PREFIX, PINYIN)

    page.keyboard.press("Enter")                     # trusted Enter, isComposing = true
    page.wait_for_timeout(300)

    assert asked() == [], "the romanisation was sent before a character was chosen"
    assert page.input_value("#input") == PREFIX + PINYIN, "the box was emptied or split mid-word"
    assert PINYIN not in ui.log_text(), "and it was not posted into the thread either"

    commit(ui, cdp, PREFIX, HANZI)
    page.keyboard.press("Enter")                     # now an ordinary Enter
    await_run(page)

    assert asked() == [PREFIX + HANZI], "the committed text must be sent exactly once"
    assert page.input_value("#input") == ""
    assert PREFIX + HANZI in ui.log_text()


def test_the_keydown_the_composer_refused_really_was_a_composing_enter(ui):
    """The guard is about the browser's composition state, not about a flag we invented."""
    page = ui.page
    page.evaluate("""() => { window.imeSeen = [];
        document.querySelector('#input').addEventListener('keydown', e =>
            window.imeSeen.push({key: e.key, trusted: e.isTrusted,
                                 composing: e.isComposing, legacy: e.keyCode})); }""")
    compose(ui, PREFIX, PINYIN)
    page.keyboard.press("Enter")
    page.wait_for_timeout(250)

    seen = page.evaluate("window.imeSeen")
    assert [e for e in seen if e["key"] == "Enter" and e["trusted"] and e["composing"]], seen
    assert asked() == [], seen


# ------------------------------- the same key while a run is going

def test_a_composition_never_queues_or_steers_into_the_running_worker(ui):
    page = ui.page
    follow_ups = watch_follow_ups(page)
    try:
        hold_queue(ui)
        cdp = compose(ui, PREFIX, PINYIN)

        page.keyboard.press("Enter")                          # would queue the half-typed word
        page.wait_for_timeout(250)
        page.keyboard.press("Control+Shift+Enter")            # the queue/steer flip shortcut
        page.wait_for_timeout(250)

        assert follow_ups == [], "a candidate confirmation was sent to the running worker"
        assert page.input_value("#input") == PREFIX + PINYIN
        assert page.locator(".task-queue-row").count() == 0

        commit(ui, cdp, PREFIX, HANZI)
        page.keyboard.press("Control+Shift+Enter")            # deliberate: steer, not queue
        page.wait_for_function("() => document.getElementById('input').value === ''", timeout=8000)
        page.wait_for_timeout(250)

        assert [(f["mode"], f["text"]) for f in follow_ups] == [("steer", PREFIX + HANZI)], follow_ups
        assert asked() == ["Hold queue fixture"], "a follow-up must not open a second run"
    finally:
        _Fixture.queue_release.set()
        page.wait_for_timeout(300)


# ------------------------------- the slash menu, which reads the same Enter

def test_a_composing_enter_does_not_pick_a_slash_command_but_a_committed_one_does(ui):
    page = ui.page
    cdp = compose(ui, "/", "ch")                      # "/ch" — the command menu is open
    expect(page.locator("#slashMenu")).to_be_visible()

    page.keyboard.press("Enter")
    page.wait_for_timeout(250)

    assert page.input_value("#input") == "/ch", "the menu completed a command mid-composition"
    assert asked() == []

    commit(ui, cdp, "/", "chat")                      # the candidate replaces "ch": "/chat"
    expect(page.locator("#slashMenu")).to_be_visible()
    page.keyboard.press("Enter")                      # the menu may have this Enter now
    page.wait_for_timeout(250)

    assert page.input_value("#input") == "/chat ", "the guard outlived the composition"
    assert asked() == [], "choosing a command is not a send"


# ------------------------------- everything Enter still has to do

def test_an_ordinary_enter_and_the_send_button_each_start_exactly_one_task(ui):
    page = ui.page
    page.fill("#input", "shrink the history")
    page.press("#input", "Enter")
    await_run(page)
    assert asked() == ["shrink the history"]
    assert page.input_value("#input") == ""

    page.fill("#input", "summarize the design doc")
    page.click("#send")
    await_run(page)
    assert asked() == ["shrink the history", "summarize the design doc"]


def test_shift_enter_still_makes_a_newline_and_starts_nothing(ui):
    page = ui.page
    page.fill("#input", "first line")
    page.evaluate("""() => { const el = document.querySelector('#input'); el.focus();
                            el.setSelectionRange(el.value.length, el.value.length); }""")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("second line")
    page.wait_for_timeout(150)

    assert page.input_value("#input") == "first line\nsecond line"
    assert asked() == []

    page.keyboard.press("Enter")
    await_run(page)
    assert asked() == ["first line\nsecond line"]


# ------------------------------- the other ordering, and the legacy signal

LEGACY = """() => {
  const el = document.querySelector('#input');
  el.focus();
  const probe = new KeyboardEvent('keydown', {key: 'Enter', keyCode: 229,
                                              bubbles: true, cancelable: true});
  if (probe.keyCode !== 229) return 'unsupported';
  // WebKit / older keyboards order it the other way round: the composition ends first, so the
  // confirming keydown reports isComposing = false and only the legacy keyCode 229 identifies it.
  el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles: true}));
  el.dispatchEvent(new CompositionEvent('compositionend', {bubbles: true, data: 'x'}));
  el.dispatchEvent(probe);
  return 'dispatched';
}"""


def test_a_confirming_enter_that_arrives_after_compositionend_is_still_not_a_send(ui):
    """Explicitly synthetic: this event sequence is not reachable through Chromium's own IME."""
    page = ui.page
    page.fill("#input", PREFIX + PINYIN)
    if page.evaluate(LEGACY) == "unsupported":
        pytest.skip("this browser does not carry the legacy keyCode on a synthetic keydown")
    page.wait_for_timeout(250)

    assert asked() == [], "a keyCode 229 Enter is the keyboard confirming, not the person sending"
    assert page.input_value("#input") == PREFIX + PINYIN

    page.fill("#input", PREFIX + HANZI)               # the person then sends, deliberately
    page.press("#input", "Enter")
    await_run(page)
    assert asked() == [PREFIX + HANZI], "the guard must not outlive the composition"
