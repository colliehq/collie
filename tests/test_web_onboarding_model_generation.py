"""A brain picked during onboarding has to survive the catalog read it overtook.

First run does two things at once.  The page asks `/api/models` for the model picker the moment it
loads, and — a beat later, off a second read — it offers the welcome card grid.  Choosing a card
POSTs `/api/model`, the backend answers with the provider and model it saved, and the trigger starts
naming it.  The first read is still in flight while all of that happens.

`loadModelCatalog` already knows that a catalog answer describes the model that was current when it
was *asked for*, and refuses to undo a switch that landed while it was travelling: it compares the
generation counter it captured with the one in force when the answer arrives.  The picker's own
`selectModel` steps that counter.  Onboarding did not — it wrote `modelCurrentId` and nothing else —
so the answer to the older read walked straight past the guard and put the old model (the mock
fallback a first-run machine is on) back in the trigger and in its `aria-label`, seconds after the
person had been told their brain was connected.  Nothing had failed and nothing was re-POSTed; the
label simply disagreed with what the backend had stored.

So the assertions here are about *who owns the current model*, in both orderings: a catalog answered
before the choice, and one answered after it.  A save the backend refused has to claim nothing — no
model, no generation — which is observable precisely because a read that was already in flight is
still allowed to set the current model afterwards.  And a read started *after* an accepted choice
still wins outright, list and current model both: the guard is about staleness, not about freezing.

The catalog and `/api/model` are Playwright interceptions over the invented entries shared with
test_web_model_picker_locale, driving the real harness/webui/index.html in Chromium against the
staged HTTP fixture, entered through the supported `?preview=onboarding` route so the real onboarding
handlers run.  No provider, account, key or setting is touched and no live discovery reaches
anything.  Releasing a held answer is never followed by a sleep: each release carries a sentinel
model the page can only draw once it has processed that answer.
"""
import time

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser   # noqa: F401
from test_web_model_picker_locale import ENTRIES, CURRENT
from test_web_model_catalog_selection import FRESH

OLLAMA_CARD = "Local model (Ollama)"           # OB_NAME for the provider the cards offer
LLAMA = {"id": "ollama:llama-local", "label": "Local Llama"}    # its default pick
MOCK_LABEL = "Mock"                            # what CURRENT is called in the catalog above
# A model that exists only in one specific answer, so "the page has processed *that* answer" and
# "this list came from the newer read" are both things a test can wait for rather than guess at.
STALE = {"provider": "ollama", "model": "stale-only", "id": "ollama:stale-only",
         "label": "Stale Only", "auth": "ok", "kind": "local", "via": "local"}


class Onboarding:
    """First run over an invented catalog, with the read that is already in flight held by hand."""

    def __init__(self, ui):
        self.page = ui.page
        self.posts = []            # every /api/model body the page POSTed, in order
        self.reads = []            # every /api/models URL it asked for, in order
        self.held = []             # the page's own first read, caught before it can answer
        self.refuse = None         # (status, body) to answer the next save with, if set
        self.entries = list(ENTRIES)
        self.current = CURRENT

    def _catalog(self, route):
        self.reads.append(route.request.url)
        if len(self.reads) == 1:
            self.held.append(route)
            return
        route.fulfill(json={"current": self.current, "entries": self.entries})

    def _save(self, route):
        self.posts.append(route.request.post_data_json)
        if self.refuse is not None:
            status, payload = self.refuse
            self.refuse = None
            route.fulfill(status=status, json=payload)
            return
        provider, model = route.request.post_data_json["id"].split(":", 1)
        route.fulfill(json={"ok": True, "provider": provider, "model": model})

    def open(self):
        page = self.page
        page.route("**/api/models*", self._catalog)
        page.route("**/api/model?*", self._save)
        page.goto(page.url + ("&" if "?" in page.url else "?") + "preview=onboarding", wait_until="load")
        expect(page.locator("#obOverlay")).to_be_visible()
        # The welcome came up off a later read, so the page's own first one is genuinely still open.
        assert len(self.held) == 1 and len(self.reads) >= 2, self.reads
        return self

    def release(self, sentinel, current=None):
        """Answer the read that was already in flight, and wait until the page has drawn it."""
        entries = list(ENTRIES) + [sentinel]
        self.held.pop(0).fulfill(json={"current": CURRENT if current is None else current,
                                       "entries": entries})
        expect(self.option(sentinel["id"])).to_have_count(1)
        return self

    def card(self, name=OLLAMA_CARD):
        return self.page.locator(".ob-brain").filter(has_text=name)

    def choose(self, name=OLLAMA_CARD):
        self.card(name).click()
        return self

    def cta(self, name=OLLAMA_CARD):
        return self.card(name).locator(".ob-bcta")

    def option(self, model_id):
        return self.page.locator('.model-option[data-model-id="%s"]' % model_id)

    @property
    def welcome(self):
        return self.page.locator("#obOverlay")

    @property
    def label(self):
        return self.page.locator("#modelTriggerLabel")

    @property
    def trigger(self):
        return self.page.locator("#modelTrigger")

    def finish_setup(self):
        """Dismiss the optional steps onboarding chains into; they are not what is under test."""
        page = self.page
        deadline = time.time() + 8
        while time.time() < deadline:
            if page.is_visible("#amOverlay.open"):
                page.click("#amSkip")
            elif page.is_visible("#brOverlay.open"):
                page.click("#brSkip")
            elif not page.is_visible(".ob-overlay.open"):
                return self
            page.wait_for_timeout(60)
        raise AssertionError("the optional post-choice steps never closed")

    def open_picker(self):
        self.page.keyboard.press("Control+k")
        expect(self.page.locator("#modelOverlay")).to_be_visible()
        return self


def shows_current(ob, label):
    """The trigger names this model to everyone reading it, by sight and by screen reader."""
    expect(ob.label).to_have_text(label)
    expect(ob.trigger).to_have_attribute("aria-label", "Current model %s. Switch model." % label)


def test_a_choice_is_not_undone_by_the_catalog_read_it_overtook(ui):
    """The bug, in its own ordering: accepted save first, older catalog answer second."""
    ob = Onboarding(ui).open()
    ob.choose()
    expect(ob.welcome).not_to_be_visible()
    assert ob.posts == [{"id": LLAMA["id"]}], ob.posts
    shows_current(ob, "llama-local")          # saved; the list that gives it a nicer name is still out
    ob.release(sentinel=FRESH)                # …and it says the current model is still the mock one
    shows_current(ob, LLAMA["label"])
    assert ob.posts == [{"id": LLAMA["id"]}], ob.posts          # nothing was saved twice
    expect(ob.option(FRESH["id"])).to_have_count(1)             # its *entries* still populate


def test_a_choice_made_after_that_read_landed_applies_just_the_same(ui):
    """The control ordering: the same catalog answer, processed before the card is clicked."""
    ob = Onboarding(ui).open()
    ob.release(sentinel=FRESH)
    shows_current(ob, MOCK_LABEL)
    ob.choose()
    expect(ob.welcome).not_to_be_visible()
    assert ob.posts == [{"id": LLAMA["id"]}], ob.posts
    shows_current(ob, LLAMA["label"])


def test_a_refused_save_claims_neither_the_model_nor_a_generation(ui):
    """A save the backend turned down switches nothing, so it may not invalidate anything either."""
    ob = Onboarding(ui).open()
    ob.refuse = (403, {"ok": False, "error": "That account is not connected."})
    ob.choose()
    expect(ob.cta()).to_have_text("That account is not connected. — try again →")
    expect(ob.welcome).to_be_visible()                 # no switch was claimed and nothing moved on
    assert ob.label.text_content() not in (LLAMA["label"], "llama-local"), ob.label.text_content()
    # Nothing was accepted, so the answer to the read that was already in flight is still the truth.
    ob.release(sentinel=STALE)
    shows_current(ob, MOCK_LABEL)
    assert ob.posts == [{"id": LLAMA["id"]}], ob.posts          # one save attempt, not two


def test_a_retry_after_a_refusal_switches_once_and_keeps_it(ui):
    """The failure path leaves the choice available; taking it again saves once and sticks."""
    ob = Onboarding(ui).open()
    ob.refuse = (403, {"ok": False, "error": "That account is not connected."})
    ob.choose()
    expect(ob.cta()).to_have_text("That account is not connected. — try again →")
    ob.choose()                                        # the second attempt is allowed through
    expect(ob.welcome).not_to_be_visible()
    assert ob.posts == [{"id": LLAMA["id"]}, {"id": LLAMA["id"]}], ob.posts
    ob.release(sentinel=STALE)                         # still older than the save that succeeded
    shows_current(ob, LLAMA["label"])


def test_a_catalog_read_started_after_the_choice_wins_outright(ui):
    """The guard is about staleness only: a newer read still owns both the list and the current."""
    ob = Onboarding(ui).open()
    ob.choose()
    expect(ob.welcome).not_to_be_visible()
    ob.release(sentinel=STALE)
    shows_current(ob, LLAMA["label"])
    ob.finish_setup()
    ob.current, ob.entries = FRESH["id"], list(ENTRIES) + [FRESH]
    ob.open_picker()                                   # opening refetches; this read is the newest
    expect(ob.option(FRESH["id"])).to_have_count(1)
    expect(ob.option(STALE["id"])).to_have_count(0)    # the older answer no longer owns the list
    shows_current(ob, FRESH["label"])
    assert ob.posts == [{"id": LLAMA["id"]}], ob.posts
