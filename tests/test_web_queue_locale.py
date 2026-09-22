"""The saved language has to reach the rows the person is actually looking at.

Settings arrive over the network, so on a reload `/api/settings` normally answers *after*
the queue panel has already drawn its pending and retained rows from state.  Translating
the static chrome alone left those rows — the header, the "still here" explanation, and
every button that sends, reattaches or discards a request the person nearly lost — in
English on a Chinese page, and nothing re-rendered them afterwards.

Re-rendering is only safe if it costs nothing: the retained request keeps its identity,
text and attachment state, the composer draft is not touched, an inline edit that is
still being typed keeps its words and its caret, and no save, send, start or discard is
posted on the person's behalf just because a language turned up.

An edit being typed through an IME is the hard case.  The composition buffer lives on the
live textarea node and is not in `.value`, so no snapshot of text and caret can carry it to
a replacement node: the row has to keep the node itself until the composition finishes.
These tests drive that with the browser's own composition events, which is what the page
reacts to; they are not a test of a physical OS IME, and they do not claim to prove which
characters a real IME would have dropped.
"""
import json

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser, _Fixture   # noqa: F401
from test_web_busy_send_ui import (ASK, attach, open_cap_thread, open_read_thread,
                                   retained_rows, send)

# The "Busy fixture" prefix is what makes the staged backend refuse the start; any other
# wording picks an ordinary successful run, and there is nothing to hold back.
HELD = "Busy fixture — 这句话不能丢，也不要自动发送。"

# (LANG, header, retained explanation, resend button, discard button)
LANGS = [
    ("zh", "待执行要求", "保存尚未确认 — 仍然保留在这里", "重新发送", "丢弃"),
    ("zh-tw", "待執行要求", "儲存尚未確認 — 仍然保留在這裡", "重新傳送", "捨棄"),
]


def hold_settings(page):
    """Hold every /api/settings read open, and hand back the release for a chosen LANG."""
    pending = []
    page.route("**/api/settings**", lambda route: pending.append(route))

    def release(lang):
        assert pending, "the fixture must actually delay a settings response"
        for route in pending[:]:
            response = route.fetch()
            data = response.json()
            data.setdefault("values", {})["LANG"] = lang
            route.fulfill(response=response, json=data)
            pending.remove(route)
    return release


def retained_storage(page):
    """Everything recovery is holding on to, exactly as it is stored."""
    return page.evaluate("""() => Object.keys(window.sessionStorage)
      .filter(key => key.indexOf('collie.retained') === 0).sort()
      .map(key => key + '=' + window.sessionStorage.getItem(key)).join('\\n')""")


PARTIAL, FINAL = "还没保存的内容 pinyin", "还没保存的内容 拼音"


def open_editor(page):
    """The one pending request, with its inline editor open and focused."""
    row = page.locator(".task-queue-row:not(.retained)")
    expect(row).to_have_count(1)
    row.get_by_role("button", name="Edit", exact=True).click()
    expect(page.locator("#taskQueueList textarea")).to_be_focused()
    return row


def start_composing(page):
    """Begin an IME composition in the open editor, exactly as the browser reports one:
    compositionstart, then input with isComposing while the buffer is still open."""
    page.evaluate("""partial => {
      const el = document.querySelector('#taskQueueList textarea');
      window.composingEditor = el; el.focus();
      el.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true, data:''}));
      el.value = partial;
      el.dispatchEvent(new InputEvent('input', {bubbles:true, data:'pinyin',
        isComposing:true, inputType:'insertCompositionText'}));
    }""", PARTIAL)


def finish_composing(page, end_first, text=FINAL):
    """Commit the composition. Browsers differ on whether compositionend precedes the
    final input event, and the committed characters are only in that input event."""
    page.evaluate("""([endFirst, text]) => {
      const el = window.composingEditor;
      const end = () => el.dispatchEvent(new CompositionEvent('compositionend',
        {bubbles:true, data:'拼音'}));
      if (endFirst) end();
      el.value = text;
      el.dispatchEvent(new InputEvent('input', {bubbles:true, data:'拼音',
        isComposing:false, inputType:'insertCompositionText'}));
      if (!endFirst) end();
    }""", [end_first, text])


def composing_node_is_live(page):
    """The node the composition is attached to is still the editor on screen and focused."""
    return page.evaluate("""() => {
      const el = window.composingEditor;
      return !!el && el.isConnected && document.activeElement === el &&
        document.querySelector('#taskQueueList textarea') === el;
    }""")


def hold_back_a_request(page):
    """A save the server refused, filed against the thread it was sent from."""
    _Fixture.busy_delay = 1.2
    _Fixture.queue_fail_once = True
    open_read_thread(page)
    attach(page)
    send(page, HELD)
    page.wait_for_timeout(150)
    open_cap_thread(page)          # the composer is another thread's, so it is retained
    page.wait_for_timeout(1800)
    open_read_thread(page)
    expect(retained_rows(page)).to_have_count(1)


@pytest.mark.parametrize("lang,header,explanation,resend,discard", LANGS)
def test_language_arriving_after_a_reload_translates_the_restored_retained_row(
        ui, lang, header, explanation, resend, discard):
    page = ui.page
    hold_back_a_request(page)
    held_id = retained_rows(page).get_attribute("data-retained")
    stored = retained_storage(page)
    assert stored and HELD in stored

    release = hold_settings(page)
    page.reload(wait_until="load")
    open_read_thread(page)
    expect(retained_rows(page)).to_have_count(1)
    # Restored while the page is still, truthfully, English.
    expect(page.locator("#taskQueueTitle")).to_have_text("Pending requests")
    expect(page.locator("html")).to_have_attribute("lang", "en")

    draft = "Draft that belongs to the person, not to the language"
    page.fill("#input", draft)
    posts, entries = len(_Fixture.queue_posts), json.dumps(_Fixture.queue_entries, sort_keys=True)

    release(lang)
    expect(page.locator("html")).to_have_attribute("lang", lang)
    # Past one queue poll: the translation is the rendered state, not a flicker between polls.
    page.wait_for_timeout(3100)

    row = retained_rows(page)
    expect(page.locator("#taskQueueTitle")).to_have_text(header)
    expect(row.locator(".task-queue-meta")).to_contain_text(explanation)
    expect(row.get_by_role("button", name=resend, exact=True)).to_be_enabled()
    expect(row.get_by_role("button", name=discard, exact=True)).to_be_visible()
    assert row.get_by_role("button", name="Send again", exact=True).count() == 0

    # The request is the same request, with the same words and the same identity.
    expect(row).to_have_count(1)
    expect(row).to_have_attribute("data-retained", held_id)
    expect(row.locator(".task-queue-text")).to_have_text(HELD)
    assert retained_storage(page) == stored, "a language must not rewrite what recovery holds"
    # And nothing was sent, started, saved or discarded on the person's behalf.
    expect(page.locator("#input")).to_have_value(draft)
    assert len(_Fixture.queue_posts) == posts
    assert json.dumps(_Fixture.queue_entries, sort_keys=True) == entries
    assert not _Fixture.queue_starts


def test_language_arriving_while_a_pending_request_is_being_edited_keeps_the_words_and_caret(ui):
    """The editor is a live textarea whose text lives in state, not in the entry. A
    re-render for the language must not drop the half-typed edit, move focus away, or
    save/cancel the edit the person has not finished."""
    page = ui.page
    open_read_thread(page)
    send(page, ASK)                                  # a refused start becomes a pending request
    expect(page.locator(".task-queue-row:not(.retained)")).to_have_count(1)

    release = hold_settings(page)
    page.reload(wait_until="load")
    open_read_thread(page)
    row = page.locator(".task-queue-row:not(.retained)")
    expect(row).to_have_count(1)
    row.get_by_role("button", name="Edit", exact=True).click()

    typing = "改到一半的要求 — 还没保存"
    editor = page.locator("#taskQueueList textarea")
    expect(editor).to_be_visible()
    editor.fill(typing)
    page.evaluate("""() => {
      const el = document.querySelector('#taskQueueList textarea');
      el.focus(); el.setSelectionRange(2, 5);
    }""")
    posts, entries = len(_Fixture.queue_posts), json.dumps(_Fixture.queue_entries, sort_keys=True)

    release("zh")
    expect(page.locator("html")).to_have_attribute("lang", "zh")
    page.wait_for_timeout(3100)                      # and across a queue poll re-render

    expect(page.locator("#taskQueueTitle")).to_contain_text("待执行要求")
    expect(page.get_by_label("编辑待执行要求")).to_have_value(typing)
    expect(row.get_by_role("button", name="保存", exact=True)).to_be_visible()
    expect(row.get_by_role("button", name="取消", exact=True)).to_be_visible()
    # The caret is still where the person left it, in the editor they are still using.
    assert page.evaluate("""() => {
      const el = document.querySelector('#taskQueueList textarea');
      return el && document.activeElement === el &&
        el.selectionStart === 2 && el.selectionEnd === 5;
    }"""), "the open editor kept focus and selection across the language change"

    # An unfinished edit is not a saved edit, and not a cancelled one.
    assert len(_Fixture.queue_posts) == posts
    assert json.dumps(_Fixture.queue_entries, sort_keys=True) == entries
    assert next(iter(_Fixture.queue_entries.values()))["text"] == ASK
    assert not _Fixture.queue_starts

    # It can still be finished afterwards, in the language now on screen.
    row.get_by_role("button", name="保存", exact=True).click()
    page.wait_for_function(
        "() => document.querySelector('.task-queue-text')?.textContent === '%s'" % typing)
    assert next(iter(_Fixture.queue_entries.values()))["text"] == typing


@pytest.mark.parametrize("end_first", [False, True])
def test_a_language_arriving_mid_composition_keeps_the_editor_being_composed_in(ui, end_first):
    """A composition in progress is held by the textarea node itself. The language landing
    must not replace that node underneath it, and the characters committed afterwards —
    whichever way round the browser sends compositionend and the final input — have to be
    the ones the person ends up owning."""
    page = ui.page
    open_read_thread(page)
    send(page, ASK)
    expect(page.locator(".task-queue-row:not(.retained)")).to_have_count(1)

    release = hold_settings(page)
    page.reload(wait_until="load")
    open_read_thread(page)
    row = open_editor(page)
    start_composing(page)
    posts, entries = len(_Fixture.queue_posts), json.dumps(_Fixture.queue_entries, sort_keys=True)

    release("zh")
    expect(page.locator("html")).to_have_attribute("lang", "zh")
    page.wait_for_timeout(3100)                      # and across a queue poll re-render
    assert composing_node_is_live(page), "the language render replaced a composing editor"

    finish_composing(page, end_first)
    editor = page.get_by_label("编辑待执行要求")
    expect(editor).to_have_value(FINAL)              # the committed characters, not the buffer
    expect(editor).to_be_focused()
    expect(page.locator("#taskQueueTitle")).to_contain_text("待执行要求")
    expect(row.get_by_role("button", name="保存", exact=True)).to_be_visible()
    expect(row.get_by_role("button", name="取消", exact=True)).to_be_visible()
    # Composing is not saving: the request still says what the server was told it says.
    assert len(_Fixture.queue_posts) == posts
    assert json.dumps(_Fixture.queue_entries, sort_keys=True) == entries
    assert next(iter(_Fixture.queue_entries.values()))["text"] == ASK
    assert not _Fixture.queue_starts

    # And the composed words are what the person saves, when they choose to.
    row.get_by_role("button", name="保存", exact=True).click()
    page.wait_for_function(
        "() => document.querySelector('.task-queue-text')?.textContent === '%s'" % FINAL)
    assert next(iter(_Fixture.queue_entries.values()))["text"] == FINAL


def test_leaving_a_composing_editor_for_another_thread_does_not_follow_the_person(ui):
    """Switching threads while composing: the other thread is drawn immediately, and the
    composition ending afterwards is a stale event — it must not put the abandoned editor
    back, and must not touch the thread now open."""
    page = ui.page
    open_read_thread(page)
    send(page, ASK)
    expect(page.locator(".task-queue-row:not(.retained)")).to_have_count(1)
    open_editor(page)
    start_composing(page)
    posts, entries = len(_Fixture.queue_posts), json.dumps(_Fixture.queue_entries, sort_keys=True)

    open_cap_thread(page)
    assert page.locator("#taskQueueList textarea").count() == 0, "the editor followed the switch"
    finish_composing(page, end_first=True)           # the detached node's composition ends
    page.wait_for_timeout(300)
    assert page.locator("#taskQueueList textarea").count() == 0, "a stale composition reopened it"
    assert page.locator(".task-queue-row:not(.retained)").count() == 0
    expect(page.locator("#input")).to_have_value("")

    # Back on the thread it belongs to, the request is the request the server holds.
    open_read_thread(page)
    row = page.locator(".task-queue-row:not(.retained)")
    expect(row).to_have_count(1)
    expect(row.locator(".task-queue-text")).to_have_text(ASK)
    assert page.locator("#taskQueueList textarea").count() == 0
    assert len(_Fixture.queue_posts) == posts
    assert json.dumps(_Fixture.queue_entries, sort_keys=True) == entries
    assert next(iter(_Fixture.queue_entries.values()))["text"] == ASK


def test_closing_a_composing_editor_ends_it_and_later_renders_still_run(ui):
    """Cancelling mid-composition closes the edit for good, and the row it left behind is
    still re-rendered normally afterwards — a composition nobody finished cannot wedge the
    panel in the language it happened to be drawn in."""
    page = ui.page
    open_read_thread(page)
    send(page, ASK)
    expect(page.locator(".task-queue-row:not(.retained)")).to_have_count(1)

    release = hold_settings(page)
    page.reload(wait_until="load")
    open_read_thread(page)
    row = open_editor(page)
    start_composing(page)
    posts, entries = len(_Fixture.queue_posts), json.dumps(_Fixture.queue_entries, sort_keys=True)

    row.get_by_role("button", name="Cancel", exact=True).click()
    expect(row.locator(".task-queue-text")).to_have_text(ASK)
    finish_composing(page, end_first=False)          # stale: the node is already gone
    page.wait_for_timeout(300)
    assert page.locator("#taskQueueList textarea").count() == 0, "a stale composition reopened it"
    expect(row.locator(".task-queue-text")).to_have_text(ASK)

    release("zh")
    expect(page.locator("html")).to_have_attribute("lang", "zh")
    expect(page.locator("#taskQueueTitle")).to_contain_text("待执行要求")
    expect(row.get_by_role("button", name="编辑", exact=True)).to_be_visible()
    expect(row.locator(".task-queue-text")).to_have_text(ASK)
    assert len(_Fixture.queue_posts) == posts
    assert json.dumps(_Fixture.queue_entries, sort_keys=True) == entries
    assert not _Fixture.queue_starts


def test_a_language_arriving_mid_run_does_not_invent_follow_up_guidance(ui):
    """The composer placeholder during a run states what this worker can be asked to do
    right now — here, that its capabilities are not known yet; elsewhere, that it cannot be
    steered at all. A language change has no idea which of those is true.

    It is the static `data-i18n-placeholder` pass that resets this composer on a language
    change, which is long-standing behaviour and not what this change is about. What must
    not happen is the language change *asserting* something about the run: answering with
    the follow-up/steer line would tell the person they can queue or steer in states where
    the page has just told them they cannot.
    """
    page = ui.page
    _Fixture.start_delay = 3.0                       # the start frame, and its capabilities, are late
    release = hold_settings(page)
    page.reload(wait_until="load")
    open_read_thread(page)
    send(page, "Accepted fixture — take your time")
    expect(page.locator("#input")).to_have_attribute("placeholder", "Waiting for worker capabilities…")

    release("zh")
    expect(page.locator("html")).to_have_attribute("lang", "zh")
    page.wait_for_timeout(400)
    placeholder = page.get_attribute("#input", "placeholder")
    assert "Follow up while the worker runs" not in placeholder, placeholder
    assert "Steer while the worker runs" not in placeholder, placeholder
    # The static composer string it falls back to, translated — a statement about the
    # composer, not a claim about the worker.
    assert placeholder == "描述你想达成的结果…", placeholder
