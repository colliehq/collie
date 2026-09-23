"""An answer from a screen the user already left must not paint over the one they are on.

Two things a person meets on the real control panel:

  * every tab refresh went through one `loadActivity`, and whatever came back was stored and
    rendered against *whichever* tab was current when the response landed. Hold an Overview
    request, switch to Automations, and the late Overview answer -- success or failure -- redrew
    the health badge, the notice line and the whole grid with Overview's content;
  * saving an automation wrote "Automation saved." and immediately started a refresh, which
    clears the notice line in the same tick, so the confirmation was never readable. A second
    click on Save while the first upsert was still in flight sent a second upsert.

Everything below is read off the real page: the shipped index.html served by the staged fixture
server, the product's own fetch paths, and Playwright route interception to decide *when* an
answer arrives and to record what was sent. No timing guesses stand in for the race -- each held
response is released explicitly, after the state it must not disturb is already asserted.
"""
import json
import time

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import _Fixture, browser, server, ui  # noqa: F401


SPECS_A = {"specs": [{"automation_id": "nightly-report", "task": "summarize the day",
                      "trigger": {"provider": "timer", "every_s": 3600}, "enabled": True}],
           "recipes": [], "executions": []}
SPECS_B = {"specs": [{"automation_id": "weekly-digest", "task": "summarize the week",
                      "trigger": {"provider": "timer", "every_s": 604800}, "enabled": True}],
           "recipes": [], "executions": []}
# Overview's answer carries an unavailable lane, so a stale render is loud: it writes the notice
# line and the health badge, not just rows.
ACTIVITY = {"errors": {"scheduler": "unavailable"}, "runs": [], "queue": []}


class Held:
    """Hold the first matching request; let later ones through with a staged answer."""

    def __init__(self, page, pattern, body=None, later=None, status=200):
        self.page = page
        self.pattern = pattern
        self.route = None
        self.requests = []
        self.bodies = []
        self.body = body
        self.later = later if later is not None else body
        self.status = status
        page.route(pattern, self._handle)

    def _handle(self, route):
        self.requests.append(route.request.url)
        self.bodies.append(route.request.post_data)
        if self.route is None:
            self.route = route
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(self.later))

    def wait(self):
        until = time.monotonic() + 6
        while self.route is None and time.monotonic() < until:
            self.page.wait_for_timeout(20)
        assert self.route is not None, "the page never asked for " + self.pattern

    def release(self, body=None, status=None):
        status = self.status if status is None else status
        payload = self.body if body is None else body
        self.route.fulfill(status=status, content_type="application/json",
                           body=json.dumps(payload))
        self.route = None
        # Two real paint boundaries after delivery: a state shown only briefly still counts.
        self.page.evaluate(
            "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


class HeldAll(Held):
    """Hold *every* matching request, and release them one at a time, by arrival order.

    Two forms saving at once is the point of the ownership cases: neither write may be let
    through early just because the other one is still open.
    """

    def __init__(self, page, pattern, body=None, status=200):
        self.held = []
        Held.__init__(self, page, pattern, body=body, status=status)

    def _handle(self, route):
        self.requests.append(route.request.url)
        self.bodies.append(route.request.post_data)
        self.held.append(route)

    def wait_for(self, count):
        until = time.monotonic() + 6
        while len(self.held) < count and time.monotonic() < until:
            self.page.wait_for_timeout(20)
        assert len(self.held) >= count, "only %d request(s) for %s" % (len(self.held), self.pattern)

    def release(self, index=0, body=None, status=None):
        status = self.status if status is None else status
        payload = self.body if body is None else body
        self.held[index].fulfill(status=status, content_type="application/json",
                                 body=json.dumps(payload))
        self.page.evaluate(
            "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


def _open_panel(page):
    page.click("#topbarMore > summary")
    page.click("#activityBtn")
    page.locator("#activityPanel").wait_for(state="visible")
    # The overflow menu stays open over the panel header; shut it as a user's next click would.
    page.evaluate("() => document.getElementById('topbarMore').removeAttribute('open')")


def _automations_tab(page):
    page.locator('[data-control-tab="automations"]').click()
    expect(page.locator("#activityHealth")).to_have_text("1 configured")


def _grid(page):
    return page.locator("#activityGrid").text_content()


def _notice(page):
    return page.locator("#activityNotice").text_content()


def test_a_late_overview_answer_does_not_replace_the_automations_tab(ui):
    page = ui.page
    page.route("**/api/automations?*", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps(SPECS_A)))
    held = Held(page, "**/api/activity*", ACTIVITY)
    _open_panel(page)
    held.wait()
    _automations_tab(page)
    held.release()
    expect(page.locator("#activityHealth")).to_have_text("1 configured")
    assert "nightly-report" in _grid(page)
    assert "unavailable" not in _grid(page)
    assert _notice(page) == ""


def test_a_late_overview_failure_does_not_replace_the_automations_tab(ui):
    page = ui.page
    page.route("**/api/automations?*", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps(SPECS_A)))
    held = Held(page, "**/api/activity*", ACTIVITY)
    _open_panel(page)
    held.wait()
    _automations_tab(page)
    held.release({"error": "activity store is offline"}, status=500)
    expect(page.locator("#activityHealth")).to_have_text("1 configured")
    assert "bad" not in (page.get_attribute("#activityHealth", "class") or "")
    assert "nightly-report" in _grid(page)
    assert _notice(page) == ""


def test_a_failure_for_the_tab_the_user_is_on_is_still_reported(ui):
    page = ui.page
    held = Held(page, "**/api/activity*", ACTIVITY)
    _open_panel(page)
    held.wait()
    held.release({"error": "activity store is offline"}, status=500)
    expect(page.locator("#activityNotice")).to_contain_text("Activity could not refresh")
    assert "activity store is offline" in _notice(page)
    assert "bad" in (page.get_attribute("#activityHealth", "class") or "")


def test_an_older_answer_for_the_same_tab_loses_to_the_newer_one(ui):
    page = ui.page
    held = Held(page, "**/api/automations?*", SPECS_A, later=SPECS_B)
    _open_panel(page)
    page.locator('[data-control-tab="automations"]').click()
    held.wait()
    page.click("#activityRefresh")          # a second, newer request for the same tab
    expect(page.locator("#activityHealth")).to_have_text("1 configured")
    assert "weekly-digest" in _grid(page)
    held.release()                          # the first answer, arriving last
    assert "weekly-digest" in _grid(page)
    assert "nightly-report" not in _grid(page)


def test_an_answer_arriving_after_the_panel_closed_does_not_paint(ui):
    page = ui.page
    held = Held(page, "**/api/automations?*", SPECS_B, later=SPECS_A)
    _open_panel(page)
    page.locator('[data-control-tab="automations"]').click()
    held.wait(); held.release()                          # first answer draws weekly-digest
    expect(page.locator("#activityHealth")).to_have_text("1 configured")
    page.click("#activityRefresh")           # second request is held by the handler
    held.wait()
    page.click("#activityClose")
    expect(page.locator("#activityPanel")).to_be_hidden()
    held.release(SPECS_A)                   # a different listing, answering a closed panel
    assert "nightly-report" not in _grid(page)
    assert "weekly-digest" in _grid(page)


def test_a_refresh_started_before_the_editor_opened_keeps_the_unsaved_draft(ui):
    page = ui.page
    held = Held(page, "**/api/automations?*", SPECS_A, later=SPECS_A)
    _open_panel(page)
    page.locator('[data-control-tab="automations"]').click()
    held.wait(); held.release()
    expect(page.locator("#activityHealth")).to_have_text("1 configured")
    page.click("#activityRefresh")
    held.wait()
    page.get_by_role("button", name="New automation", exact=True).click()
    page.fill("#autoId", "draft-report")
    page.fill("#autoTask", "words the user has not saved yet")
    held.release()
    expect(page.locator("#autoTask")).to_have_value("words the user has not saved yet")
    assert page.input_value("#autoId") == "draft-report"


def _open_editor(page, task="summarize the day"):
    _open_panel(page)
    page.locator('[data-control-tab="automations"]').click()
    expect(page.locator("#activityHealth")).to_have_text("1 configured")
    page.get_by_role("button", name="New automation", exact=True).click()
    page.fill("#autoId", "nightly-report")
    page.fill("#autoTask", task)


def test_a_double_click_on_save_upserts_once_and_confirms_after_the_refresh(ui):
    page = ui.page
    posts = []
    page.route("**/api/automations?*", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps(SPECS_A)))
    upsert = Held(page, "**/api/automations/upsert*", {"ok": True})
    page.on("request", lambda request: posts.append((request.url, request.method))
            if request.method == "POST" else None)
    _open_editor(page)
    save = page.locator("#autoSave")
    save.click()
    upsert.wait()
    page.wait_for_function("() => document.getElementById('autoSave').disabled === true")
    save.click(force=True)                  # the impatient second click, first still in flight
    assert len(upsert.requests) == 1, upsert.requests
    upsert.release()
    expect(page.locator("#activityNotice")).to_have_text("Automation saved.")
    expect(page.locator("#autoSave")).to_have_count(0)
    assert "nightly-report" in _grid(page)
    upserts = [url for url, _ in posts if "/upsert" in url]
    assert len(upserts) == 1, posts
    assert not [url for url, _ in posts if "/preview" in url or "/automations/run" in url]
    assert not _Fixture.queue_posts and not _Fixture.queue_starts


def test_a_refused_save_keeps_the_form_and_gives_the_button_back(ui):
    page = ui.page
    page.route("**/api/automations?*", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps(SPECS_A)))
    upsert = Held(page, "**/api/automations/upsert*", {"error": "budget exceeds the local cap"},
                  status=400)
    _open_editor(page, task="words the user has not saved yet")
    page.click("#autoSave")
    upsert.wait(); upsert.release()
    expect(page.locator("#activityNotice")).to_have_text("budget exceeds the local cap")
    expect(page.locator("#autoTask")).to_have_value("words the user has not saved yet")
    expect(page.locator("#autoSave")).to_be_enabled()
    assert len(upsert.requests) == 1, upsert.requests


def test_a_save_confirmation_does_not_overwrite_a_later_message(ui):
    page = ui.page
    listing = Held(page, "**/api/automations?*", SPECS_A, later=SPECS_A)
    page.route("**/api/automations/upsert*", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps({"ok": True})))
    preview = Held(page, "**/api/automations/preview*", {"fired": True})
    _open_panel(page)
    page.locator('[data-control-tab="automations"]').click()
    listing.wait(); listing.release()
    expect(page.locator("#activityHealth")).to_have_text("1 configured")
    page.get_by_role("button", name="New automation", exact=True).click()
    page.fill("#autoId", "nightly-report")
    page.fill("#autoTask", "summarize the day")
    page.click("#autoSave")
    listing.wait()                          # the refresh the save started, held open
    page.click("#autoPreview")
    preview.wait()
    preview.release()
    expect(page.locator("#activityNotice")).to_contain_text("nothing was queued")
    later = _notice(page)
    listing.release()
    assert _notice(page) == later, "the save confirmation replaced a newer message"


# --- Ownership: a finished save answers the form it was typed in, and nothing else ------------
#
# The save's continuation used to reach for whatever was on screen when it happened to resolve:
# a module-wide pending notice consumed by the next refresh of *any* tab, and `$("autoSave")`,
# which names whichever editor is in the document now. Below, the screen always changes before
# the answer lands.


def _listing(page, payload=SPECS_A):
    page.route("**/api/automations?*", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps(payload)))


def test_a_save_confirmation_does_not_follow_the_user_to_another_tab(ui):
    page = ui.page
    _listing(page)
    upsert = Held(page, "**/api/automations/upsert*", {"ok": True})
    _open_editor(page)
    page.click("#autoSave")
    upsert.wait()
    page.locator('[data-control-tab="overview"]').click()   # the editor's screen is gone
    expect(page.locator("#autoSave")).to_have_count(0)
    upsert.release()
    page.wait_for_timeout(250)
    assert "Automation saved." not in _notice(page)
    assert "nightly-report" not in _grid(page)
    assert page.locator("#activityHealth").text_content() != "1 configured"


def test_a_save_answered_after_the_panel_closed_does_not_greet_the_next_visit(ui):
    page = ui.page
    _listing(page)
    upsert = Held(page, "**/api/automations/upsert*", {"ok": True})
    _open_editor(page)
    page.click("#autoSave")
    upsert.wait()
    page.click("#activityClose")
    expect(page.locator("#activityPanel")).to_be_hidden()
    upsert.release()                        # accepted by the server, with nobody to tell
    page.wait_for_timeout(250)
    _open_panel(page)                       # a fresh visit, with its own reason to be here
    expect(page.locator("#activityPanel")).to_be_visible()
    page.wait_for_timeout(250)
    assert _notice(page) == "", "a save from the last visit spoke on this one"


def test_a_finished_save_does_not_re_enable_the_editor_that_replaced_it(ui):
    page = ui.page
    _listing(page)
    upsert = HeldAll(page, "**/api/automations/upsert*", {"ok": True})
    _open_editor(page, task="the first form")
    page.click("#autoSave")
    upsert.wait_for(1)
    page.get_by_role("button", name="Edit", exact=True).first.click()   # a second form, B
    page.locator("#autoId").first.fill("weekly-digest")
    page.locator("#autoSave").first.click()
    upsert.wait_for(2)
    upsert.release(0)                       # the first form's answer, arriving under the second
    expect(page.locator("#autoSave").first).to_be_disabled()
    page.locator("#autoSave").first.click(force=True)
    page.wait_for_timeout(200)
    assert len(upsert.requests) == 2, upsert.requests


def test_a_refresh_started_under_one_editor_does_not_discard_the_next_one(ui):
    page = ui.page
    listing = Held(page, "**/api/automations?*", SPECS_A, later=SPECS_A)
    _open_panel(page)
    page.locator('[data-control-tab="automations"]').click()
    listing.wait(); listing.release()
    expect(page.locator("#activityHealth")).to_have_text("1 configured")
    page.get_by_role("button", name="New automation", exact=True).click()
    page.locator("#autoId").first.fill("first-draft")
    page.click("#activityRefresh")          # asked for with the first editor on screen
    listing.wait()
    page.get_by_role("button", name="Edit", exact=True).first.click()   # a different editor
    page.locator("#autoTask").first.fill("words typed into the second form")
    listing.release()
    expect(page.locator("#autoTask").first).to_have_value("words typed into the second form")


def test_the_form_refuses_edits_while_its_save_is_in_flight_instead_of_losing_them(ui):
    page = ui.page
    _listing(page)
    upsert = Held(page, "**/api/automations/upsert*", {"ok": True})
    _open_editor(page, task="summarize the day")
    page.click("#autoSave")
    upsert.wait()
    expect(page.locator("#autoTask")).to_be_disabled()
    before = page.input_value("#autoTask")
    page.evaluate("() => document.getElementById('autoTask').focus()")
    page.keyboard.type(" and also the night")
    assert page.input_value("#autoTask") == before, \
        "the form took typing the refresh is about to throw away"
    upsert.release()
    expect(page.locator("#autoSave")).to_have_count(0)
    sent = json.loads(upsert.bodies[0])["spec"]
    assert sent["task"] == "summarize the day", sent       # what was on screen, unedited
    assert len(upsert.requests) == 1, upsert.requests


def test_a_refused_save_hands_the_fields_back_with_their_text(ui):
    page = ui.page
    _listing(page)
    upsert = Held(page, "**/api/automations/upsert*", {"error": "budget exceeds the local cap"},
                  status=400)
    _open_editor(page, task="summarize the day")
    page.click("#autoSave")
    upsert.wait(); upsert.release()
    expect(page.locator("#autoTask")).to_be_enabled()
    expect(page.locator("#autoTask")).to_have_value("summarize the day")
    page.fill("#autoTask", "summarize the day and the night")
    expect(page.locator("#autoTask")).to_have_value("summarize the day and the night")
