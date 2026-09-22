"""What the run setup says about the model that is current, after every accepted save.

`/api/run-capabilities` is read per provider/model: which speed tiers that route reports, which
reasoning efforts it accepts, which worker is its default, and the sentence under the Fast item that
explains why Fast is or is not there. So the answer the page is holding describes *a* model, and it
stops describing anything the moment a different model is saved.

The picker's `selectModel` knew that and re-read after an accepted save. First-run onboarding — the
welcome cards, the only model choice a new person makes — POSTed the same `/api/model`, took the
same acknowledgement, and adopted the same current model, but never asked again. So the very first
setup finished showing the capabilities of the model the page happened to boot on (on a fresh
machine, the mock fallback): the Fast item and its explanation, the reasoning efforts, the default
worker all belonged to a model nobody had chosen. Reopening the picker or reloading the page fixed
it, which is exactly the manual step a successful first run must not require.

The second half of this file is about the *other* direction of the same staleness. A capability read
is not instantaneous, and the page starts one at load. If that first read (or its failure) landed
after a later save had already been answered, it was applied anyway — unconditionally, in both the
fulfilled and the rejected path — putting the old model's tiers back over the new model's, and
hiding a Fast item the person had deliberately selected while leaving `runSpeed` set to `fast`.

Everything here drives the real `harness/webui/index.html` in Chromium against the staged HTTP
fixture, entered through the supported `?preview=onboarding` route so the real welcome handlers run,
with `/api/models`, `/api/model` and `/api/run-capabilities` answered by Playwright interception over
the invented entries shared with test_web_model_picker_locale. No provider, account, key, setting or
live discovery is touched: `ollama:llama-local` is an invented row and the tiers below are staged
descriptions, not claims about Ollama. Nothing waits by sleeping — when an answer that must change
nothing is released, the test then makes a *later* request over a different endpoint and waits for
its visible result, so the released answer is known to have been delivered first.
"""
import time

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser   # noqa: F401
from test_web_model_picker_locale import ENTRIES, CURRENT

OLLAMA_CARD = "Local model (Ollama)"           # OB_NAME for the provider the welcome cards offer
LLAMA_ID = "ollama:llama-local"                # its default pick, and the save every test here makes
MOCK_ID = "anthropic:mock"                     # what the page boots on, per CURRENT

# Two staged descriptions of the same endpoint: what the page is holding when the choice is made,
# and what the route that was chosen reports. "fast" here is a staged tier on an invented row.
BEFORE = "Capabilities of the model this page booted on"
AFTER = "Capabilities of the model you just connected"
STALE_WORKER = "worker-from-the-older-read"    # only ever sent in an answer that must not apply


def caps(note, tiers=("standard",), worker="collie", efforts=()):
    return {"speed_tiers": list(tiers), "interactive_speed_default": "standard",
            "reasoning_efforts": list(efforts), "worker_default": worker, "workers": [],
            "fast_note": note}


class Setup:
    """One first-run page, with every capability read recorded and holdable."""

    def __init__(self, ui):
        self.page = ui.page
        self.posts = []          # every /api/model body the page POSTed, in order
        self.reads = []          # every /api/run-capabilities the page asked for, in order
        self.held = []           # reads caught before they may answer
        self.hold_first = False  # catch the read the page starts at load
        self.answer = caps(BEFORE)
        self.fail_next = False   # answer the next read the way an unreachable backend does
        self.refuse = None       # (status, body) for the next save, if set
        self.entries = list(ENTRIES)

    # -- staged endpoints ---------------------------------------------------
    def _caps(self, route):
        self.reads.append(route.request.url)
        if self.hold_first and len(self.reads) == 1:
            self.held.append(route)
            return
        if self.fail_next:
            self.fail_next = False
            route.abort("failed")
            return
        route.fulfill(json=self.answer)

    def _catalog(self, route):
        route.fulfill(json={"current": CURRENT, "entries": self.entries})

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
        page.route("**/api/run-capabilities", self._caps)
        page.route("**/api/models*", self._catalog)
        page.route("**/api/model?*", self._save)
        page.goto(page.url + ("&" if "?" in page.url else "?") + "preview=onboarding",
                  wait_until="load")
        expect(self.welcome).to_be_visible()
        return self

    def release(self, answer):
        """Answer the load-time read that a later save has already overtaken."""
        route = self.held.pop(0)
        if answer is None:
            route.abort("failed")
        else:
            route.fulfill(json=answer)
        return self

    def barrier(self, name):
        """Make a later request over another endpoint and wait for what it draws.

        Anything released before this returns has been delivered to the page ahead of the catalog
        answer that is now on screen, so "it changed nothing" is an observation rather than a guess.
        """
        sentinel = {"provider": "ollama", "model": name, "id": "ollama:" + name, "label": name,
                    "auth": "ok", "kind": "local", "via": "local"}
        self.entries = list(ENTRIES) + [sentinel]
        self.page.keyboard.press("Control+k")
        expect(self.page.locator("#modelOverlay")).to_be_visible()
        expect(self.option(sentinel["id"])).to_have_count(1)
        self.page.keyboard.press("Escape")
        expect(self.page.locator("#modelOverlay")).not_to_be_visible()
        return self

    # -- what a person sees -------------------------------------------------
    @property
    def welcome(self):
        return self.page.locator("#obOverlay")

    @property
    def note(self):
        return self.page.locator("#runFastHelp")

    @property
    def worker_desc(self):
        return self.page.locator("#savedWorkerDesc")

    def card(self, name=OLLAMA_CARD):
        return self.page.locator(".ob-brain").filter(has_text=name)

    def cta(self, name=OLLAMA_CARD):
        return self.card(name).locator(".ob-bcta")

    def option(self, model_id):
        return self.page.locator('.model-option[data-model-id="%s"]' % model_id)

    def fast_offered(self):
        return self.page.evaluate("() => !document.getElementById('runFastItem').hidden")

    def speed(self):
        return self.page.evaluate("() => document.getElementById('runSpeed').value")

    def speed_is_explicit(self):
        return self.page.evaluate("() => !!(window.runExplicitAxes || {}).speed")

    # -- what a person does -------------------------------------------------
    def choose_card(self, name=OLLAMA_CARD):
        self.card(name).click()
        return self

    def skip_welcome(self):
        self.page.locator("#obSkip").click()
        expect(self.welcome).not_to_be_visible()
        return self

    def switch_in_picker(self, model_id):
        page = self.page
        page.keyboard.press("Control+k")
        expect(page.locator("#modelOverlay")).to_be_visible()
        self.option(model_id).click()
        expect(page.locator("#modelStatus")).to_contain_text("Switched")
        expect(page.locator("#modelOverlay")).not_to_be_visible()
        return self

    def choose_fast(self):
        """Select the Fast tier the way the run setup offers it: open the sheet, pick the item."""
        page = self.page
        page.click("#modeTrigger")
        expect(page.locator("#modeMenu")).to_be_visible()
        page.click("#runFastItem")
        page.keyboard.press("Escape")
        assert self.speed() == "fast" and self.speed_is_explicit()
        return self

    def finish_optional_steps(self):
        """Dismiss the optional steps the choice chains into; they are not what is under test."""
        page, deadline = self.page, time.time() + 8
        while time.time() < deadline:
            if page.is_visible("#amOverlay.open"):
                page.click("#amSkip")
            elif page.is_visible("#brOverlay.open"):
                page.click("#brSkip")
            elif not page.is_visible(".ob-overlay.open"):
                return self
            page.wait_for_timeout(60)
        raise AssertionError("the optional post-choice steps never closed")


def test_an_onboarding_choice_shows_the_capabilities_of_the_model_it_saved(ui):
    """The bug: first run's own model choice left the previous model's capabilities on screen."""
    s = Setup(ui).open()
    expect(s.note).to_have_text(BEFORE)
    assert not s.fast_offered()
    before_reads = len(s.reads)

    s.answer = caps(AFTER, tiers=("standard", "fast"), worker="local-worker")
    s.choose_card()
    expect(s.welcome).not_to_be_visible()
    assert s.posts == [{"id": LLAMA_ID}], s.posts

    expect(s.note).to_have_text(AFTER)                     # the sentence under Fast is per-model…
    expect(s.worker_desc).to_contain_text("local-worker")  # …and so is the default worker beside it
    assert s.fast_offered()                                # the tier this route reports is offered
    assert len(s.reads) == before_reads + 1, s.reads       # asked again exactly once
    assert s.posts == [{"id": LLAMA_ID}], s.posts          # and saved exactly once


def test_the_picker_control_refreshes_the_same_capabilities_for_the_same_save(ui):
    """The control: the same POST through the picker always did re-read, and still does."""
    s = Setup(ui).open()
    s.skip_welcome()
    expect(s.note).to_have_text(BEFORE)
    before_reads = len(s.reads)

    s.answer = caps(AFTER, tiers=("standard", "fast"), worker="local-worker")
    s.switch_in_picker(LLAMA_ID)

    expect(s.note).to_have_text(AFTER)
    expect(s.worker_desc).to_contain_text("local-worker")
    assert s.fast_offered()
    assert s.posts == [{"id": LLAMA_ID}], s.posts
    assert len(s.reads) == before_reads + 1, s.reads       # one save, one read — not two


def test_a_refused_onboarding_save_neither_switches_nor_re_reads_capabilities(ui):
    """A save the backend turned down changes no model, so it may not claim its capabilities."""
    s = Setup(ui).open()
    expect(s.note).to_have_text(BEFORE)
    before_reads = len(s.reads)

    s.refuse = (403, {"ok": False, "error": "That account is not connected."})
    s.answer = caps(AFTER, tiers=("standard", "fast"))
    s.choose_card()

    expect(s.cta()).to_have_text("That account is not connected. — try again →")
    expect(s.welcome).to_be_visible()
    expect(s.note).to_have_text(BEFORE)                    # still describing the model in force
    assert not s.fast_offered()
    assert len(s.reads) == before_reads, s.reads           # nothing was re-read off a failed save
    assert s.posts == [{"id": LLAMA_ID}], s.posts


def test_the_optional_steps_after_a_choice_keep_the_refresh_and_re_post_nothing(ui):
    """Onboarding chains into optional screens; neither they nor their dismissal may undo the save."""
    s = Setup(ui).open()
    s.answer = caps(AFTER, tiers=("standard", "fast"))
    s.choose_card()
    expect(s.welcome).not_to_be_visible()
    s.finish_optional_steps()

    expect(s.note).to_have_text(AFTER)
    assert s.fast_offered()
    assert s.posts == [{"id": LLAMA_ID}], s.posts          # presentation never re-sends the save


def test_an_older_capability_answer_does_not_overwrite_the_one_the_choice_asked_for(ui):
    """The read that was already in flight describes the model that used to be current."""
    s = Setup(ui)
    s.hold_first = True
    s.open()
    assert len(s.held) == 1, s.reads

    s.answer = caps(AFTER, tiers=("standard", "fast"), worker="local-worker")
    s.choose_card()
    expect(s.welcome).not_to_be_visible()
    expect(s.note).to_have_text(AFTER)
    assert s.fast_offered()

    # …and now the load-time read finally answers, about the model nobody is on any more.
    s.release(caps(BEFORE, worker=STALE_WORKER))
    s.barrier("after-the-older-answer")

    expect(s.note).to_have_text(AFTER)
    expect(s.worker_desc).not_to_contain_text(STALE_WORKER)
    assert s.fast_offered()
    assert s.posts == [{"id": LLAMA_ID}], s.posts


def test_an_older_capability_failure_does_not_clear_the_fresh_view_or_an_explicit_choice(ui):
    """The same staleness through the rejected path: a stale failure is not news about this model."""
    s = Setup(ui)
    s.hold_first = True
    s.open()
    assert len(s.held) == 1, s.reads

    s.answer = caps(AFTER, tiers=("standard", "fast"))
    s.choose_card()
    expect(s.welcome).not_to_be_visible()
    expect(s.note).to_have_text(AFTER)
    s.finish_optional_steps()                              # the run setup is behind them
    s.choose_fast()                                        # a deliberate, supported choice

    s.release(None)                                        # the older read fails, long after
    s.barrier("after-the-older-failure")

    assert s.fast_offered()                                # the tier the current model reports
    assert s.speed() == "fast" and s.speed_is_explicit()   # and the choice made within it
    expect(s.note).to_have_text(AFTER)


def test_a_failed_capability_read_after_the_choice_keeps_the_safe_fallback(ui):
    """When the *latest* lookup is the one that fails, the established conservative view stands."""
    s = Setup(ui).open()
    expect(s.note).to_have_text(BEFORE)

    s.fail_next = True
    s.choose_card()
    expect(s.welcome).not_to_be_visible()
    assert s.posts == [{"id": LLAMA_ID}], s.posts

    s.barrier("after-the-failed-read")
    assert not s.fast_offered()                            # unknown is not "supported"
    assert s.speed() == "standard"                         # and never a faster, dearer tier
    assert not s.speed_is_explicit()
    assert s.posts == [{"id": LLAMA_ID}], s.posts          # a failed read is not a failed save


def test_a_refresh_reporting_fast_keeps_an_explicit_choice_and_never_selects_it_by_itself(ui):
    """Reporting a tier offers it; only the person picks it, and only they unpick it."""
    s = Setup(ui).open()
    s.skip_welcome()

    s.answer = caps(AFTER, tiers=("standard", "fast"))
    s.switch_in_picker(LLAMA_ID)
    assert s.fast_offered()
    assert s.speed() == "standard" and not s.speed_is_explicit()   # offered, not chosen

    s.choose_fast()
    s.answer = caps(AFTER, tiers=("standard", "fast"), worker="local-worker")
    s.switch_in_picker(MOCK_ID)                            # another save, same reported tier
    expect(s.worker_desc).to_contain_text("local-worker")  # the newer read has been applied
    assert s.speed() == "fast" and s.speed_is_explicit()   # the explicit, still-supported choice
