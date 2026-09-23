"""A deliberate model choice has to survive the catalog that is still on its way.

The model picker refetches `/api/models` every time it opens, every time live discovery is ticked,
and whenever the current provider changes — all of it over the network, all of it landing whenever it
lands.  Meanwhile the person is already walking the list with the arrow keys.  The two met badly: the
answer redrew the rows and put the highlight back on the *current* model, so an Enter pressed a beat
later switched to a model nobody had chosen.  The choice is an entry, not a row number, so these
tests hold the catalog response open with a real route, move the highlight, and only then release it —
unchanged, reordered, longer, shorter, or failing.

What is asserted here is exact: the concrete id the person highlighted is the id that is POSTed, or
for the synthetic Auto row exactly `{"auto": true}`.  An empty id is never accepted as "close
enough" for a concrete model — that is precisely the shape this bug produced.

The catalog, the model-change endpoint and every held answer are Playwright interceptions over the
invented entries shared with test_web_model_picker_locale, driving the real harness/webui/index.html
in Chromium against the staged HTTP fixture: no provider is contacted and no live discovery reaches
anything.  Nothing here waits on a timer for the race to resolve — every step waits on the state the
page is actually in.
"""
import time

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser   # noqa: F401
from test_web_model_picker_locale import Picker, ENTRIES, CURRENT, OPTION_COUNT

LLAMA = "ollama:llama-local"          # the entry below the current one, reached with one ArrowDown
AUTO = "anthropic:"                   # the synthetic Auto row of the current provider
FRESH = {"provider": "ollama", "model": "fresh-live", "id": "ollama:fresh-live",
         "label": "Fresh Live", "auth": "ok", "kind": "local", "via": "local"}


@pytest.fixture
def picker(ui):                                              # noqa: F811
    return Picker(ui).install()


def hold_catalog(page):
    """Catch every `/api/models` read from now on; they are answered by hand, one by one."""
    held = []
    page.route("**/api/models*", lambda route: held.append(route))
    return held


def wait_held(page, held, count):
    deadline = time.time() + 5
    while len(held) < count and time.time() < deadline:
        page.wait_for_timeout(10)
    assert len(held) == count, [route.request.url for route in held]
    return held


def release(route, entries, current=CURRENT):
    route.fulfill(json={"current": current, "entries": entries})


def opened_with_a_held_refresh(picker):
    """The picker reopened over the rows it already had, with its refresh still in flight.

    This is the window the race lives in, so the reopen may not wait for that refresh: it is held
    here and released by hand, test by test. `Picker.open()` settles nothing for exactly that
    reason — the first open below waits for its own answer itself, by the count it asserts.
    """
    page = picker.page
    picker.reload().open()
    expect(picker.status).to_have_text("%d models available" % OPTION_COUNT)
    page.keyboard.press("Escape")
    expect(picker.overlay).not_to_be_visible()
    held = hold_catalog(page)
    picker.open()
    return wait_held(page, held, 1)


def choose_below_current(picker):
    """One ArrowDown off the current model, the way the bug was found."""
    picker.page.keyboard.press("ArrowDown")
    active = picker.active()
    assert active["id"] == LLAMA, active
    return active


# ------------------------------------------------------------------ the choice outlives the answer

@pytest.mark.parametrize("reordered", [False, True])
def test_a_catalog_that_lands_after_a_deliberate_choice_still_switches_to_it(picker, reordered):
    page = picker.page
    held = opened_with_a_held_refresh(picker)
    chosen = choose_below_current(picker)

    release(held.pop(), list(reversed(ENTRIES)) if reordered else ENTRIES)
    expect(picker.status).to_have_text("%d models available" % OPTION_COUNT)

    # The highlight is on the same entry, wherever the new answer put it, and the screen reader is
    # told about that row and not about the one that happens to sit at the old index.
    assert picker.active() == chosen, "the arriving catalog moved the highlight off the chosen model"
    page.keyboard.press("Enter")
    expect(picker.status).to_have_text("Switched · applies to your next message")
    assert picker.posts == [{"id": LLAMA}], picker.posts


def test_a_deliberate_auto_row_is_posted_as_auto_and_never_as_an_empty_id(picker):
    page = picker.page
    held = opened_with_a_held_refresh(picker)
    page.keyboard.press("ArrowUp")                     # one row above the current model: Auto
    chosen = picker.active()
    assert chosen["id"] == AUTO, chosen

    release(held.pop(), list(reversed(ENTRIES)))
    expect(picker.status).to_have_text("%d models available" % OPTION_COUNT)

    assert picker.active() == chosen, "the arriving catalog moved the highlight off Auto"
    page.keyboard.press("Enter")
    expect(picker.status).to_have_text("Switched · applies to your next message")
    assert picker.posts == [{"auto": True}], picker.posts


# ------------------------------------------------------------------ a choice that stopped existing

def test_a_chosen_model_the_new_catalog_dropped_is_never_the_one_submitted(picker):
    page = picker.page
    held = opened_with_a_held_refresh(picker)
    choose_below_current(picker)

    without_llama = [entry for entry in ENTRIES if entry["id"] != LLAMA]
    release(held.pop(), without_llama)
    expect(page.locator(".model-option")).to_have_count(len(without_llama) + 1)
    expect(picker.option(LLAMA)).to_have_count(0)

    # The choice is gone, so the highlight is back on the model actually in use — and the removed
    # entry is not switched to on its way out.
    assert picker.active()["id"] == CURRENT, picker.active()
    page.keyboard.press("Enter")
    expect(picker.status).to_have_text("Switched · applies to your next message")
    assert picker.posts == [{"id": CURRENT}], picker.posts


def test_a_chosen_model_that_lost_its_credentials_is_never_the_one_submitted(picker):
    page = picker.page
    held = opened_with_a_held_refresh(picker)
    choose_below_current(picker)

    unusable = [dict(entry, auth="missing-key") if entry["id"] == LLAMA else entry for entry in ENTRIES]
    release(held.pop(), unusable)
    expect(picker.option(LLAMA)).to_be_disabled()

    assert picker.active()["id"] == CURRENT, picker.active()
    page.keyboard.press("Enter")
    expect(picker.status).to_have_text("Switched · applies to your next message")
    assert picker.posts == [{"id": CURRENT}], picker.posts


def test_the_search_may_move_the_highlight_to_another_visible_entry(picker):
    """Filtering is the person's own act: it re-picks, and what it re-picks is what is submitted."""
    page = picker.page
    picker.reload().open().settled()                   # no held read here: the refresh may land first
    choose_below_current(picker)

    picker.field.fill("local")                         # the chosen model is still one of the rows
    expect(page.locator(".model-option")).to_have_count(1)
    assert picker.active()["id"] == LLAMA, picker.active()

    picker.field.fill("mock")                          # now it is not: another visible entry is picked
    expect(page.locator(".model-option")).to_have_count(1)
    assert picker.active()["id"] == CURRENT, picker.active()
    page.keyboard.press("Enter")
    expect(picker.status).to_have_text("Switched · applies to your next message")
    assert picker.posts == [{"id": CURRENT}], picker.posts


# ------------------------------------------------------------------ overlapping reads

def overlapping_reads(picker):
    """A reopen refresh and a live-discovery read, both in flight, oldest first."""
    page = picker.page
    held = opened_with_a_held_refresh(picker)
    page.check("#modelDiscover")
    wait_held(page, held, 2)
    assert "discover=1" not in held[0].request.url, held[0].request.url
    assert "discover=1" in held[1].request.url, held[1].request.url
    return held


def test_the_newest_catalog_read_wins_when_an_older_one_answers_last(picker):
    page = picker.page
    held = overlapping_reads(picker)

    release(held[1], ENTRIES + [FRESH])                # the newest read answers first…
    expect(page.locator(".model-option")).to_have_count(OPTION_COUNT + 1)
    expect(picker.option("ollama:fresh-live")).to_have_count(1)

    release(held[0], [ENTRIES[1]])                     # …and the older one lands afterwards
    page.wait_for_timeout(300)
    expect(picker.option("ollama:fresh-live")).to_have_count(1)
    assert picker.status.inner_text() == "%d models available" % (OPTION_COUNT + 1)
    assert picker.posts == [], picker.posts


def test_an_older_catalog_failure_does_not_bury_the_list_that_answered(picker):
    page = picker.page
    held = overlapping_reads(picker)

    release(held[1], ENTRIES + [FRESH])
    expect(page.locator(".model-option")).to_have_count(OPTION_COUNT + 1)

    held[0].fulfill(status=503, json={"error": "down"})
    page.wait_for_timeout(300)
    expect(page.locator(".model-option")).to_have_count(OPTION_COUNT + 1)
    assert picker.status.inner_text() == "%d models available" % (OPTION_COUNT + 1)
    assert not page.get_attribute("#modelStatus", "class").endswith("err"), "a stale failure took over"


# ------------------------------------------------------------------ a switch is not undone

def test_a_catalog_answer_sent_before_a_switch_does_not_undo_it(picker):
    """The held answer says the old model is current; it arrives after the switch has happened."""
    page = picker.page
    posts = []

    def change(route):
        posts.append(route.request.post_data_json)
        route.fulfill(json={"ok": True, "provider": "ollama", "model": "llama-local"})

    page.route("**/api/model?*", change)
    held = opened_with_a_held_refresh(picker)
    choose_below_current(picker)

    page.keyboard.press("Enter")
    expect(picker.status).to_have_text("Switched · applies to your next message")
    assert posts == [{"id": LLAMA}], posts
    expect(page.locator("#modelTriggerLabel")).to_have_text("Local Llama")

    release(held.pop(), ENTRIES, current=CURRENT)      # the answer asked for before the switch
    page.wait_for_timeout(300)

    expect(page.locator("#modelTriggerLabel")).to_have_text("Local Llama")
    expect(picker.overlay).not_to_be_visible()
    picker.open()                                      # its refresh is caught by the hold and stays there
    assert picker.active()["id"] == LLAMA, picker.active()
    assert picker.option(LLAMA).get_attribute("aria-selected") == "true"
    assert posts == [{"id": LLAMA}], posts
