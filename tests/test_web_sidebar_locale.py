"""The saved language has to reach the sidebar rows that are already on screen.

`/api/settings` answers over the network, so on a reload it normally lands *after* the sidebar has
drawn its group headings, its run-state badges, its saved-request badges and its turn counts from
state.  Translating the static chrome alone left all of those in English beside a Chinese page — a
Chinese nav and queue above a sidebar still reading "Today" and "PAUSED · TURN LIMIT".

The repair may not be a redraw.  The rename and delete controls live in these rows, and the list
deliberately does not rebuild on every poll because a rebuild eats the click (and the focus) that
lands on it.  So these tests hold the language back, put focus on a real rename button, and then
demand both things at once: the labels are translated, and the very same button node — with the
same thread, the same user-written title and no request of its own — is still the one under the
cursor afterwards.

Everything here is the real index.html against the staged HTTP fixture shared with
test_web_ui_run_status; only the settings answer is held open, by the same helper the queue-locale
tests use.  No dialog, rename or delete is answered on the person's behalf unless the test says so.
"""
import datetime
import json

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import SESSIONS, server, browser, _Fixture   # noqa: F401
from test_web_ui_run_status import ui as run_status_ui                   # noqa: F401
from test_web_busy_send_ui import open_read_thread
from test_web_queue_locale import hold_settings

# ------------------------------------------------------------------ one fixed day
# The sidebar groups a row by comparing its `updated` stamp against the browser's own `new Date()`
# at render time.  The shared rows are stamped once, with `time.time()`, when test_web_ui_run_status
# is imported — so in a long suite the two clocks are hours apart, and a run that crossed local
# midnight drew a correct YESTERDAY under tests that read TODAY.  That is the instrument moving, not
# the product: the grouping is right, the day it was asked about changed underneath it.
#
# So this module — and only this module — pins both ends of that comparison to one instant.  The
# page's Date is fixed, and the shared rows are re-stamped onto the same day for the length of a
# test and handed back exactly as they were found, so nothing leaks to the modules that share them.
# Only Date is frozen: the timers behind the run and queue polls, and the held-back settings answer,
# still run at their real speed.
#
# 12:10 UTC is at least ten minutes away from local midnight at every real UTC offset (they land on
# :00, :30 and :45), and mid-January is clear of daylight-saving changes, so the frozen instant and
# the rows beneath it are one unambiguous local day in whatever zone this runs.
FROZEN_DAY = datetime.datetime(2026, 1, 15, 12, 10, tzinfo=datetime.timezone.utc)


@pytest.fixture
def frozen_sessions():
    """Re-stamp the shared sidebar rows onto the frozen day, keeping their order and spacing."""
    saved = [row["updated"] for row in SESSIONS]
    newest = max(saved)
    for row, updated in zip(SESSIONS, saved):
        row["updated"] = FROZEN_DAY.timestamp() - (newest - updated)
    yield
    for row, updated in zip(SESSIONS, saved):
        row["updated"] = updated


@pytest.fixture
def ui(frozen_sessions, run_status_ui):
    """The shared surface, reading the frozen day at both ends.

    `frozen_sessions` is named first so the rows are already on the frozen day when the imported
    fixture loads the page, and the page is drawn once more after the fixed Date is installed, so
    the sidebar a test is handed was grouped against that same day rather than the wall clock.
    """
    page = run_status_ui.page
    page.clock.set_fixed_time(FROZEN_DAY)
    page.reload(wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    return run_status_ui


# (LANG, group heading, capped run badge, turn count, rename title, rename aria, delete aria)
LANGS = [
    ("zh", "今天", "已暂停 · 轮次上限", "1 轮", "重命名", "重命名对话", "删除对话"),
    ("zh-tw", "今天", "已暫停 · 輪次上限", "1 輪", "重新命名", "重新命名對話", "刪除對話"),
]
CAP_TITLE = "Migrate every module to the new config loader"
READ_TITLE = "Read README.md and tell me what this tool does"


def watch_requests(page):
    """Every request the page makes from now on, in order."""
    seen = []
    page.on("request", lambda request: seen.append(request.method + " " + request.url))
    return seen


def drawn_in_english(page):
    """A sidebar drawn from state while the saved language is still being held back."""
    # Both of these are uppercased by CSS, so they are read as rendered, the way a person sees them.
    expect(page.locator("#threads .thread-group").first).to_have_text("TODAY", use_inner_text=True)
    expect(page.locator("#threads .t-state.paused")).to_have_text("PAUSED · TURN LIMIT",
                                                                 use_inner_text=True)
    expect(page.locator("html")).to_have_attribute("lang", "en")


def hold_focus_on_rename(page):
    """Focus a real rename control and remember the exact node and row it belongs to."""
    button = page.locator('#threads [data-a="rename"]').first
    button.focus()
    page.evaluate("""() => {
      window.watchedAction = document.activeElement;
      window.watchedThread = document.activeElement.closest('.thread');
    }""")
    return button


def watched_identity(page):
    return page.evaluate("""() => ({
      actionConnected: window.watchedAction.isConnected,
      threadConnected: window.watchedThread.isConnected,
      focused: document.activeElement === window.watchedAction,
      sameRow: window.watchedAction.closest('.thread') === window.watchedThread})""")


@pytest.mark.parametrize("lang,today,paused,turns,rename,rename_aria,delete_aria", LANGS)
def test_a_late_language_translates_the_sidebar_rows_already_on_screen(
        ui, lang, today, paused, turns, rename, rename_aria, delete_aria):
    page = ui.page
    release = hold_settings(page)
    page.reload(wait_until="load")
    open_read_thread(page)
    expect(page.locator("#threads .t-state.paused")).to_have_count(1)
    page.wait_for_timeout(2800)      # past a run poll and a queue poll: this is the settled list
    drawn_in_english(page)

    titles = page.locator("#threads .t-last").all_text_contents()
    assert titles == [CAP_TITLE, READ_TITLE], titles
    button = hold_focus_on_rename(page)
    requests = watch_requests(page)

    release(lang)
    expect(page.locator("html")).to_have_attribute("lang", lang)
    page.wait_for_timeout(3100)      # a further poll interval: the labels are the settled state

    # The group name, the capped run's badge, the turn count and the controls' own strings.
    expect(page.locator("#threads .thread-group").first).to_have_text(today, use_inner_text=True)
    expect(page.locator("#threads .t-state.paused")).to_have_text(paused, use_inner_text=True)
    expect(page.locator("#threads .t-turns").first).to_have_text(turns)
    assert button.get_attribute("title") == rename
    assert button.get_attribute("aria-label") == rename_aria
    assert page.locator('#threads [data-a="delete"]').first.get_attribute("aria-label") == delete_aria
    # The row's own label is translated around the title, never over it.
    assert page.locator("#threads .thread").first.get_attribute("aria-label").endswith(CAP_TITLE)

    # The person's words are untouched and the control they were aiming at is the same node.
    assert page.locator("#threads .t-last").all_text_contents() == titles
    identity = watched_identity(page)
    assert all(identity.values()), identity

    # Nothing was refetched or acted on because a language turned up.
    assert not [row for row in requests if "/api/sessions" in row], requests
    assert not [row for row in requests
                if "/api/rename/" in row or "/api/delete/" in row or not row.startswith("GET ")], requests


@pytest.mark.parametrize("lang", ["zh", "zh-tw"])
def test_rename_and_delete_still_reach_the_server_after_a_late_language(ui, lang):
    page = ui.page
    renames, deletes = [], []
    page.route("**/api/rename/**", lambda route: (renames.append(route.request.url),
                                                  route.fulfill(status=200, content_type="application/json",
                                                                body=json.dumps({"ok": True}))))
    page.route("**/api/delete/**", lambda route: (deletes.append(route.request.url),
                                                  route.fulfill(status=200, content_type="application/json",
                                                                body=json.dumps({"ok": True}))))
    release = hold_settings(page)
    page.reload(wait_until="load")
    open_read_thread(page)
    expect(page.locator("#threads .t-state.paused")).to_have_count(1)
    page.wait_for_timeout(2600)
    drawn_in_english(page)
    button = hold_focus_on_rename(page)

    release(lang)
    expect(page.locator("html")).to_have_attribute("lang", lang)
    page.wait_for_timeout(2600)
    assert all(watched_identity(page).values())

    # The focused node still carries its click handler, for the thread it was drawn for.
    page.once("dialog", lambda dialog: dialog.accept("重命名后的任务"))
    button.click()
    page.wait_for_timeout(400)
    assert len(renames) == 1 and "/api/rename/s-cap" in renames[0], renames
    assert "title=%E9%87%8D%E5%91%BD%E5%90%8D%E5%90%8E%E7%9A%84%E4%BB%BB%E5%8A%A1" in renames[0], renames
    assert not deletes

    page.once("dialog", lambda dialog: dialog.accept())
    page.locator('#threads [data-a="delete"]').first.click()
    page.wait_for_timeout(400)
    assert len(deletes) == 1 and "/api/delete/s-cap" in deletes[0], deletes
    assert page.locator("#threads .thread").count() >= 1
    # A dismissed dialog still sends nothing at all.
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.locator('#threads [data-a="delete"]').first.click()
    page.wait_for_timeout(300)
    assert len(deletes) == 1, deletes


@pytest.mark.parametrize("lang,working", [("zh", "运行中"), ("zh-tw", "執行中")])
def test_a_run_that_moves_after_the_language_redraws_in_that_language(ui, lang, working):
    """The in-place relabel must not strand the row: when the run actually changes the ordinary
    redraw runs, and it has to draw the new state in the language that has since arrived."""
    page = ui.page
    release = hold_settings(page)
    page.reload(wait_until="load")
    open_read_thread(page)
    expect(page.locator("#threads .t-state.paused")).to_have_count(1)
    page.wait_for_timeout(2600)
    drawn_in_english(page)

    release(lang)
    expect(page.locator("#threads .t-state.paused")).to_have_text(
        "已暂停 · 轮次上限" if lang == "zh" else "已暫停 · 輪次上限", use_inner_text=True)

    # The same session, now running: a different run id, so this is a change the poll must act on.
    page.route("**/api/runs**", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"runs": [{"session": "s-cap", "run": "r4", "state": "running",
                                   "verified": False, "turns": 1, "ask": CAP_TITLE, "error": ""}]})))
    expect(page.locator("#threads .thread.r-running .t-state")).to_have_text(
        working, use_inner_text=True, timeout=8000)
    expect(page.locator("#threads .t-state.paused")).to_have_count(0)
    assert page.locator('#threads [data-a="rename"]').first.get_attribute("aria-label") in (
        "重命名对话", "重新命名對話")
    assert page.locator("#threads .t-last").first.inner_text() == CAP_TITLE


@pytest.mark.parametrize("lang,waiting,untitled", [
    ("zh", "2 条已保存要求待继续", "未命名任务"),
    ("zh-tw", "2 個已儲存要求待繼續", "未命名任務")])
def test_a_late_language_reaches_saved_request_badges_and_rows_with_no_name(ui, lang, waiting, untitled):
    """A session with saved requests and no name of its own gets a row before it has a file. Both
    the badge's count and the placeholder title are ours to translate — and only those."""
    page = ui.page
    page.route("**/api/task-inbox/pending**", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"sessions": [{"session": "s-queued", "title": "", "pending": 1, "claimed": 1,
                                       "updated": 0, "owner_busy": False}]})))
    release = hold_settings(page)
    page.reload(wait_until="load")
    expect(page.locator("#threads .t-state.queued-input")).to_have_text(
        "2 SAVED REQUESTS WAITING", use_inner_text=True)
    row = page.locator("#threads .thread").first
    expect(row.locator(".t-last")).to_have_text("Untitled task")
    page.wait_for_timeout(1200)
    requests = watch_requests(page)

    release(lang)
    expect(page.locator("html")).to_have_attribute("lang", lang)
    page.wait_for_timeout(2600)
    expect(page.locator("#threads .t-state.queued-input")).to_have_text(waiting, use_inner_text=True)
    expect(row.locator(".t-last")).to_have_text(untitled)
    assert row.get_attribute("aria-label").endswith(untitled)
    # The named threads beside it keep their own words.
    assert CAP_TITLE in page.locator("#threads .t-last").all_text_contents()
    assert not [line for line in requests if "/api/sessions" in line], requests


@pytest.mark.parametrize("lang,empty", [
    ("zh", "你最近的工作会显示在这里。"), ("zh-tw", "你最近的工作會顯示在這裡。")])
def test_a_late_language_reaches_an_empty_sidebar(ui, lang, empty):
    page = ui.page
    page.route("**/api/sessions**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"sessions": []})))
    page.route("**/api/runs**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"runs": []})))
    release = hold_settings(page)
    page.reload(wait_until="load")
    expect(page.locator("#threads .empty")).to_have_text("Your recent work will appear here.")

    release(lang)
    expect(page.locator("html")).to_have_attribute("lang", lang)
    page.wait_for_timeout(1200)
    expect(page.locator("#threads .empty")).to_have_text(empty)


def test_this_module_reads_one_fixed_day_whatever_the_wall_clock_says(ui):
    """A control on the instrument these tests use, not on the product.

    The page's own Date and the stamps the fixture serves have to name the same local day, however
    long ago this module was collected and whatever the suite's wall clock has since crossed. If
    they ever part company again, this says so here instead of as a group heading somewhere else.
    """
    page = ui.page
    assert page.evaluate("() => Date.now()") == int(FROZEN_DAY.timestamp() * 1000)
    stamps = [row["updated"] for row in SESSIONS]
    assert page.evaluate("""(stamps) => {
      const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
      return stamps.every(s => s * 1000 >= midnight.getTime() && s * 1000 <= Date.now());
    }""", stamps), stamps
    expect(page.locator("#threads .thread-group")).to_have_count(1)
    expect(page.locator("#threads .thread-group").first).to_have_text("TODAY", use_inner_text=True)


def test_an_english_page_keeps_its_english_sidebar(ui):
    """The relabel reads a dictionary, so `en` has to leave every label exactly as drawn."""
    page = ui.page
    release = hold_settings(page)
    page.reload(wait_until="load")
    expect(page.locator("#threads .t-state.paused")).to_have_count(1)
    page.wait_for_timeout(1600)
    before = page.locator("#threads").inner_text()

    release("en")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    page.wait_for_timeout(1600)
    assert page.locator("#threads").inner_text() == before
    expect(page.locator("#threads .thread-group").first).to_have_text("TODAY", use_inner_text=True)
    expect(page.locator("#threads .t-state.paused")).to_have_text("PAUSED · TURN LIMIT",
                                                                 use_inner_text=True)
    assert page.locator('#threads [data-a="delete"]').first.get_attribute("aria-label") == "Delete thread"
