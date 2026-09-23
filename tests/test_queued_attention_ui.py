"""Needs You counts decisions, not clocks — and the page does not decide which is which.

An accepted follow-up that the server has already scheduled to start after a
provider quota reset is progress a person arranged and the server owns.  It used
to arrive in three surfaces — the Needs You badge, what that badge opens, and
Today's attention list — filtered on nothing but ``owner_busy``, so a request
with a healthy future start time was presented as a decision waiting on the
user.  A badge you cannot clear by deciding anything is worse than no badge.

The line itself is drawn by the host, in ``scheduled_wait.progress``: only
``scheduled`` (still on the server's clock, backoff included) and ``starting``
(an admission in flight inside CLAIM_GRACE) are progress.  ``stalled``,
``unknown``, ``stopped``, a schedule the host never vouched for, an unreadable
inbox and a request nothing is going to start are all decisions, and none of them
lose their row.  Today separates the two consistently: the count, the headline
and the suggestion speak for decisions, while progress keeps its row and its link.
"""
import json
import re
import time

import pytest
from playwright.sync_api import expect
from test_web_ui_run_status import TOKEN, _Fixture, _reset_fixture_state, browser, server, ui

FUTURE = 4_100_000_000            # 2099, so "starts after" is never ambiguous
PAST = 1_000_000_000              # 2001, a time that has plainly gone by
VIEWPORTS = [{"width": 1280, "height": 900}, {"width": 390, "height": 844}]


def _row(session, **over):
    row = {"session": session, "pending": 2, "claimed": 0, "owner_busy": False,
           "title": "Prepare weekly report", "journal": "ok", "updated": 123,
           "scheduled_wait": None}
    row.update(over)
    return row


def _wait(progress="scheduled", state="waiting", retry_at=FUTURE, reason="", next_attempt_at=None):
    """What the host publishes: the reset, the real next try, and its own verdict."""
    return {"entry": "req-1", "state": state, "retry_at": retry_at, "progress": progress,
            "next_attempt_at": retry_at if next_attempt_at is None else next_attempt_at,
            "reason": reason}


def _date_text(stamp):
    """How the page writes a far-off day in `en-US` — same machine, same zone."""
    local = time.localtime(stamp)
    return "%s %d" % (time.strftime("%b", local), local.tm_mday)


def _serve(page, rows):
    page.route("**/api/task-inbox/pending*", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps({"sessions": rows})))


def _reload(ui, rows):
    _serve(ui.page, rows)
    ui.page.reload(wait_until="domcontentloaded")
    # The badge is written by the same pass that renders Today; waiting on it is
    # waiting on the state under test rather than on a duration.
    ui.page.wait_for_function(
        "() => document.querySelector('#todayAttention')?.children.length > 0")


def test_a_future_automatic_start_is_not_a_decision_but_stays_visible(ui):
    _reload(ui, [_row("auto-waiting", scheduled_wait=_wait())])
    expect(ui.page.locator("#needsYouNav")).to_be_hidden()
    # ...and Today does not answer "nothing urgent" by forgetting the work: the
    # row is there, saying when it starts, and it opens the conversation.
    attention = ui.page.locator("#todayAttention")
    expect(attention).to_contain_text("Already submitted — starts after")
    expect(attention).not_to_contain_text("Nothing urgent")
    attention.locator(".today-row").first.click()
    expect(ui.page).to_have_url(re.compile("session=auto-waiting"))


def test_a_healthy_backoff_reads_as_its_next_try_not_as_an_overdue_start(ui):
    """The reset is past and the host has moved the next try 60s out — the busy
    backoff working.  The row has to state *that* time, and must not accuse the
    server of missing a start it is actively waiting to make."""
    _reload(ui, [_row("backing-off",
                      scheduled_wait=_wait(retry_at=PAST, next_attempt_at=FUTURE))])
    expect(ui.page.locator("#needsYouNav")).to_be_hidden()
    attention = ui.page.locator("#todayAttention")
    expect(attention).to_contain_text("Already submitted — starts after")
    expect(attention).not_to_contain_text("overdue")
    # The time shown is the next try, not the reset that has already gone by — a
    # past time under "starts after" reads as a promise that already broke.
    expect(attention).to_contain_text(_date_text(FUTURE))
    expect(attention).not_to_contain_text(_date_text(PAST))


def test_an_admission_in_flight_is_progress_and_is_labelled_as_one(ui):
    """"Starting now" and "needs your decision" cannot both be true of one row.
    Inside CLAIM_GRACE the host says the claim is in flight, so this is progress."""
    _reload(ui, [_row("admitting", scheduled_wait=_wait("starting", state="admitting",
                                                        retry_at=PAST))])
    expect(ui.page.locator("#needsYouNav")).to_be_hidden()
    expect(ui.page.locator("#todayAttention")).to_contain_text("Starting now — already submitted")


def test_a_healthy_due_queue_does_not_claim_a_start_or_ask_for_a_restart(ui):
    _reload(ui, [_row("queued", scheduled_wait=_wait("queued", retry_at=PAST))])
    expect(ui.page.locator("#needsYouNav")).to_be_hidden()
    expect(ui.page.locator("#todayAttentionCount")).to_be_empty()
    expect(ui.page.locator("#todayAttention")).to_contain_text(
        "Waiting for automatic start — already submitted")
    expect(ui.page.locator("#todayAttention")).not_to_contain_text("Starting now")
    expect(ui.page.locator("#todaySummary")).to_contain_text("Automatic work is scheduled")
    expect(ui.page.locator("#todayAttentionTitle")).to_have_text("Task progress")


def test_an_unconfirmable_start_stays_visible_without_claiming_it_is_starting(ui):
    """Nothing here could be read. That is not progress and not a clean bill of
    health: keep asking, and do not put the word "starting" on it."""
    _reload(ui, [_row("unknowable", scheduled_wait=_wait("unknown", state="admitting",
                                                         retry_at=PAST))])
    expect(ui.page.locator("#needsYouCount")).to_have_text("1")
    attention = ui.page.locator("#todayAttention")
    expect(attention).to_contain_text("Automatic start can't be confirmed — open to check")
    expect(attention).not_to_contain_text("Starting now")


@pytest.mark.parametrize("name,over", [
    ("unscheduled", {}),
    ("stopped", {"scheduled_wait": _wait("stopped", state="retired", retry_at=PAST,
                                         reason="the wait could not be written")}),
    ("stalled", {"scheduled_wait": _wait("stalled", retry_at=PAST)}),
    ("unknown", {"scheduled_wait": _wait("unknown", retry_at=PAST)}),
    ("unvouched", {"scheduled_wait": {"entry": "req-1", "state": "waiting",
                                      "retry_at": FUTURE, "reason": ""}}),
    ("unreadable-journal", {"journal": "invalid", "scheduled_wait": _wait()}),
    ("store-error", {"error": "inbox unreadable", "scheduled_wait": _wait()}),
])
def test_anything_the_host_cannot_vouch_for_still_needs_you(ui, name, over):
    """Every one of these is a case where progress cannot be confirmed — including
    a future time beside a known store error, which a date cannot talk its way
    out of. The honest answer is to keep asking."""
    _reload(ui, [_row(name, **over)])
    expect(ui.page.locator("#needsYouNav")).to_be_visible()
    expect(ui.page.locator("#needsYouCount")).to_have_text("1")
    expect(ui.page.locator("#todayAttention")).not_to_contain_text("Nothing urgent")


def test_needs_you_opens_the_row_that_actually_needs_you(ui):
    """The click target is the part a miscount is felt through: with a scheduled
    row sorted ahead of it, the old filter opened the one nothing was waiting on."""
    _reload(ui, [_row("auto-waiting", title="Scheduled report", scheduled_wait=_wait()),
                 _row("stranded", title="Stranded request")])
    expect(ui.page.locator("#needsYouCount")).to_have_text("1")
    attention = ui.page.locator("#todayAttention")
    expect(attention).to_contain_text("Stranded request")
    expect(attention).to_contain_text("Scheduled report"), "both stay visible"
    ui.page.locator("#needsYouNav").click()
    expect(ui.page).to_have_url(re.compile("session=stranded"))


def test_today_does_not_call_a_scheduled_row_a_thing_that_needs_you(ui):
    """The badge was already hidden for this fixture while the headline still said
    "1 thing needs your attention" — the same row, counted both ways. Count,
    headline and suggestion all speak for decisions now."""
    _reload(ui, [_row("auto-waiting", scheduled_wait=_wait())])
    expect(ui.page.locator("#needsYouNav")).to_be_hidden()
    expect(ui.page.locator("#todayAttentionCount")).to_be_empty()
    summary = ui.page.locator("#todaySummary")
    expect(summary).not_to_contain_text("needs your attention")
    expect(summary).to_contain_text("Automatic work is scheduled")
    # ...and the suggestion stops telling a person to clear things they cannot.
    expect(ui.page.locator("#todaySuggestion")).to_contain_text("Keep today realistic")
    expect(ui.page.locator("#todaySuggestion")).not_to_contain_text("Clear the important things first")
    # The work itself is still one click away from Today, in the recent list too.
    expect(ui.page.locator("#threads")).to_contain_text("Already submitted — starts after")


def test_a_decision_beside_progress_owns_the_count_and_the_headline(ui):
    """One of each: the count, the headline and the suggestion are about the one
    that needs deciding, and the scheduled row is neither hidden nor tallied."""
    _reload(ui, [_row("auto-waiting", title="Scheduled report", scheduled_wait=_wait()),
                 _row("stranded", title="Stranded request")])
    expect(ui.page.locator("#todayAttentionCount")).to_have_text("1")
    expect(ui.page.locator("#todayAttentionTitle")).to_have_text("Needs attention")
    expect(ui.page.locator("#todaySummary")).to_contain_text(
        "1 thing needs your attention. Everything else can wait.")
    expect(ui.page.locator("#todaySuggestion")).to_contain_text("Clear the important things first")
    expect(ui.page.locator("#todayAttention")).to_contain_text("Scheduled report")


def test_progress_never_displaces_a_decision_from_the_short_list(ui):
    """Today shows three rows. Rows the server is handling on a clock go last, so
    they cannot push out the ones a person is the only answer to."""
    _reload(ui, [_row("auto-1", title="Scheduled one", scheduled_wait=_wait()),
                 _row("auto-2", title="Scheduled two", scheduled_wait=_wait()),
                 _row("auto-3", title="Scheduled three", scheduled_wait=_wait()),
                 _row("stranded", title="Stranded request")])
    expect(ui.page.locator("#needsYouCount")).to_have_text("1")
    expect(ui.page.locator("#todayAttentionCount")).to_have_text("1")
    rows = ui.page.locator("#todayAttention .today-row")
    expect(rows).to_have_count(3)
    expect(rows.first).to_contain_text("Stranded request")


def test_an_owner_that_is_busy_or_unknown_is_unchanged_by_the_schedule(ui):
    """`owner_busy` is a separate truth and the schedule does not edit it."""
    _reload(ui, [_row("busy", owner_busy=True, scheduled_wait=_wait()),
                 _row("unknown-owner", owner_busy=None)])
    # A busy conversation was never a decision; an owner nobody could determine
    # always was, and still is.
    expect(ui.page.locator("#needsYouCount")).to_have_text("1")
    ui.page.locator("#needsYouNav").click()
    expect(ui.page).to_have_url(re.compile("session=unknown-owner"))


@pytest.mark.parametrize("lang,scheduled,stopped,unknown,headline", [
    ("zh", "已提交，将在此时间后开始", "自动启动已停止，可随时手动开始",
     "无法确认自动启动状态，请打开查看", "任务已安排自动处理"),
    ("zh-tw", "已提交，將在此時間後開始", "自動啟動已停止，可隨時手動開始",
     "無法確認自動啟動狀態，請開啟查看", "任務已安排自動處理"),
])
def test_the_schedule_copy_is_localized_on_today(ui, monkeypatch, lang, scheduled, stopped,
                                                 unknown, headline):
    monkeypatch.setattr(_Fixture, "lang", lang)
    _reload(ui, [_row("auto-waiting", scheduled_wait=_wait()),
                 _row("gave-up", scheduled_wait=_wait("stopped", state="retired", reason="store")),
                 _row("unknowable", scheduled_wait=_wait("unknown", retry_at=PAST))])
    attention = ui.page.locator("#todayAttention")
    expect(attention).to_contain_text(scheduled)
    expect(attention).to_contain_text(stopped)
    expect(attention).to_contain_text(unknown)
    expect(attention).not_to_contain_text("Already submitted")
    # The new headline is translated too, on the fixture that produces it.
    _reload(ui, [_row("auto-waiting", scheduled_wait=_wait())])
    expect(ui.page.locator("#todaySummary")).to_contain_text(headline)


@pytest.mark.parametrize("viewport", VIEWPORTS)
def test_rendering_the_schedule_never_submits_or_starts_anything(server, browser, viewport):
    """The page reports the server's timer. It must not own one, and above all
    it must not be the thing that starts queued work."""
    _reset_fixture_state()
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    writes = []
    page.on("request", lambda request: request.method != "GET"
            and writes.append((request.method, request.url)))
    try:
        _serve(page, [_row("auto-waiting", scheduled_wait=_wait()),
                      _row("stranded", title="Stranded request")])
        page.goto(server + "/?token=" + TOKEN, wait_until="load")
        page.wait_for_selector("#input")
        page.wait_for_function(
            "() => document.querySelector('#todayAttention')?.children.length > 0")
        expect(page.locator("#todayAttention")).to_contain_text("Already submitted — starts after")
        expect(page.locator("#needsYouCount")).to_have_text("1")
        assert _Fixture.queue_posts == [] and _Fixture.queue_starts == []
        assert [write for write in writes
                if "/api/task-inbox" in write[1] or "/api/stream" in write[1]] == []
    finally:
        context.close()
    assert errors == []


@pytest.mark.parametrize("viewport", VIEWPORTS)
@pytest.mark.parametrize("progress", ["scheduled", "queued", "starting"])
def test_a_scheduled_only_today_reads_the_same_on_a_phone(server, browser, viewport, progress):
    """Desktop and phone share one render pass; the separation of progress from
    decisions has to hold at both widths, including the headline."""
    _reset_fixture_state()
    context = browser.new_context(viewport=viewport)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        _serve(page, [_row("auto-waiting", scheduled_wait=_wait(progress))])
        page.goto(server + "/?token=" + TOKEN, wait_until="load")
        page.wait_for_selector("#input")
        page.wait_for_function(
            "() => document.querySelector('#todayAttention')?.children.length > 0")
        expect(page.locator("#needsYouNav")).to_be_hidden()
        expect(page.locator("#todayAttentionCount")).to_be_empty()
        expect(page.locator("#todaySummary")).to_contain_text(
            "Automatic work is scheduled")
        label = {"scheduled": "Already submitted — starts after",
                 "queued": "Waiting for automatic start — already submitted",
                 "starting": "Starting now — already submitted"}[progress]
        expect(page.locator("#todayAttention")).to_contain_text(label)
    finally:
        context.close()
    assert errors == []
