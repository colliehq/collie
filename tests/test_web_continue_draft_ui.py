"""Continuing a capped task must never spend the user's unsent draft.

The stop card's "Continue this task" button is a convenience for the common case: the run hit a
turn or output cap, the user has nothing more to say, and one click asks for one more turn. The
boundary this file guards is the *other* case — the user was already typing a clarification, or had
attached a file, when the cap landed. There, the one-click shortcut must step aside: the draft is
the user's own next turn, and Collie may neither erase it nor send it under Collie's canned words.

Everything runs against the real index.html over real HTTP/SSE with the staged fixture server from
test_web_ui_run_status; only the model answers are mocked.
"""
import base64
import json
import os
from urllib.parse import parse_qs, urlsplit

import pytest

from test_web_ui_run_status import (  # noqa: F401  (pytest fixtures)
    _Fixture, await_run, browser, server, ui,
)

EVIDENCE = os.environ.get("COLLIE_UX_EVIDENCE")
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==")


def shot(page, name):
    """Keep a picture of the state under test when evidence collection is switched on."""
    if EVIDENCE:
        os.makedirs(EVIDENCE, exist_ok=True)
        page.screenshot(path=os.path.join(EVIDENCE, name + ".png"), full_page=False)


def stream_queries(page, sink):
    """Record the full query of every /api/stream the page opens, attachments included."""
    page.on("request", lambda request: sink.append(parse_qs(urlsplit(request.url).query))
            if urlsplit(request.url).path == "/api/stream" else None)


def cap_the_run(ui):
    """Drive a real run into the turn-limit stop card."""
    ui.ask("Migrate every module to the new config loader")
    assert "Stopped at the turn limit" in ui.log_text()


def attach_image(page, name="clarification.png"):
    page.locator("#fileInput").set_input_files(
        {"name": name, "mimeType": "image/png", "buffer": PNG_1PX})
    page.wait_for_function("() => document.querySelectorAll('#attachStrip .thumb').length === 1")


def hint_text(page):
    node = page.query_selector(".stop-note .sn-draft-hint")
    return node.inner_text() if node else ""


# ---------------------------------------------------------------- text draft
def test_continue_with_a_text_draft_keeps_it_and_sends_nothing(ui):
    page = ui.page
    cap_the_run(ui)
    draft = "Only touch the auth module, and keep the old key names."
    page.fill("#input", draft)
    shot(page, "01-desktop-draft-before-continue")

    page.click(".stop-note button")
    page.wait_for_timeout(400)

    assert page.input_value("#input") == draft, "the user's unsent words survive the click"
    assert len(_Fixture.stream_requests) == 1, "no turn is started behind the draft"
    assert page.evaluate("() => document.activeElement && document.activeElement.id") == "input", \
        "the existing composer is focused, not a new one"
    assert "send" in hint_text(page).lower(), "an inline hint says sending the draft continues"
    assert page.query_selector("dialog[open], .modal.open") is None, "no confirmation modal"
    assert not page.is_disabled(".stop-note button"), "the offer stays available after clearing"
    shot(page, "02-desktop-draft-preserved-with-hint")


def test_the_preserved_draft_is_what_continues_the_same_task(ui):
    page = ui.page
    cap_the_run(ui)
    page.fill("#input", "Continue from where you stopped, but only the auth module.")
    page.click(".stop-note button")
    page.wait_for_timeout(300)
    assert len(_Fixture.stream_requests) == 1

    page.press("#input", "Enter")
    await_run(page)
    assert len(_Fixture.stream_requests) == 2, "the user's own send is what starts the turn"
    sent = _Fixture.stream_requests[1]
    assert sent["session"] == "s-cap", "it continues the same thread"
    assert sent["q"] == "Continue from where you stopped, but only the auth module.", \
        "the words sent are the user's, not Collie's canned line"


# ------------------------------------------------------- attachments/context
def test_continue_with_an_attachment_never_sends_it_under_canned_text(ui):
    page = ui.page
    streams = []
    stream_queries(page, streams)
    cap_the_run(ui)
    attach_image(page)
    assert page.input_value("#input") == "", "an attachment alone, with no words yet"

    page.click(".stop-note button")
    page.wait_for_timeout(400)

    assert page.locator("#attachStrip .thumb").count() == 1, "the attachment is still attached"
    assert page.input_value("#input") == "", "and no canned text was written over the composer"
    assert len(_Fixture.stream_requests) == 1, "nothing was sent"
    assert _Fixture.uploads == [], "and nothing was uploaded"
    assert not any(q.get("img") for q in streams[1:]), "no run carried the unsent attachment"
    assert "send" in hint_text(page).lower()
    shot(page, "03-desktop-attachment-preserved")


# ------------------------------------------------------------- empty composer
def test_empty_composer_still_continues_in_one_click_exactly_once(ui):
    page = ui.page
    cap_the_run(ui)
    assert page.input_value("#input") == ""

    page.click(".stop-note button")
    await_run(page)

    assert len(_Fixture.stream_requests) == 2, "Continue starts exactly one more turn"
    resumed = _Fixture.stream_requests[1]
    assert resumed["session"] == "s-cap", "resume stays in the same thread"
    assert "continue" in resumed["q"].lower()
    assert "Converted the remaining modules." in ui.log_text()
    assert page.is_disabled(".stop-note button"), "the resume offer is spent once used"
    assert hint_text(page) == "", "no draft hint when there was no draft"
    shot(page, "04-desktop-empty-one-click-continued")


def test_a_rejected_start_gives_the_offer_back_instead_of_a_dead_button(ui):
    """Workspace/config validation runs at send time. A refusal must not eat the button."""
    page = ui.page
    cap_the_run(ui)
    # Demand a check without proposing one: validateRunConfig rejects the start.
    page.evaluate("""() => {
      document.getElementById('taskWorkspace').open = true;
      const v = document.getElementById('runVerification');
      v.value = 'required'; v.dispatchEvent(new Event('change', {bubbles: true}));
      document.getElementById('verifyCommand').value = '';
    }""")
    page.click(".stop-note button")
    page.wait_for_timeout(500)

    assert len(_Fixture.stream_requests) == 1, "the rejected start opened no stream"
    assert not page.is_disabled(".stop-note button"), "a rejected start must not disable it forever"
    assert page.input_value("#input") == "", "the canned line is not left behind as a draft"
    shot(page, "05-desktop-rejected-start-button-restored")


# ------------------------------------------------- saved history + old session
def test_saved_history_stop_card_on_a_390px_screen_preserves_the_draft(ui):
    page = ui.page
    page.set_viewport_size({"width": 390, "height": 780})
    # At 390px the sidebar is off-screen; a phone reaches a saved thread by its own URL.
    page.goto(page.url + "&session=s-cap", wait_until="load")
    page.wait_for_selector(".stop-note button", timeout=8000)
    page.fill("#input", "先只改配置读取，别动测试。")
    shot(page, "06-mobile390-saved-card-draft")

    page.click(".stop-note button")
    page.wait_for_timeout(400)

    assert page.input_value("#input") == "先只改配置读取，别动测试。"
    assert _Fixture.stream_requests == [], "reading saved history starts no run"
    assert "send" in hint_text(page).lower()
    assert page.locator(".stop-note .sn-draft-hint").is_visible(), "the hint is visible at 390px"
    shot(page, "07-mobile390-draft-preserved-with-hint")


def test_a_stop_card_from_another_session_never_starts_a_turn(ui):
    """The control: an old card whose thread the user has already left stays inert."""
    page = ui.page
    cap_the_run(ui)
    page.fill("#input", "a draft that belongs to the capped thread")
    page.locator(".thread").filter(has_text="Read README.md").first.click()
    page.wait_for_timeout(400)
    stale = page.evaluate("""() => {
      const btn = document.querySelector('.stop-note button');
      if (!btn) return 'gone';
      btn.click();
      return btn.disabled ? 'disabled' : 'idle';
    }""")
    page.wait_for_timeout(300)
    assert stale in ("gone", "idle"), "a stale offer neither runs nor pretends to be running"
    assert len(_Fixture.stream_requests) == 1, "nothing was sent into the thread now on screen"
    assert page.query_selector(".stop-note .sn-draft-hint") is None, \
        "and no hint is shown for a thread the user has left"
