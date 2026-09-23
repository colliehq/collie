"""Two things a person meets on the real System activity panel.

  * An automation draft survives a poll and survives someone else's late answer, but not the
    panel's own lid: closing the panel leaves the half-typed editor sitting in the grid, and
    opening it again called `loadActivity()` unconditionally. The automations answer came back,
    `renderAutomations` cleared the grid, and the editor — ID, task, budgets, everything typed
    and not yet saved — went with it. Closing a panel is not discarding a form: reopening the
    *same* tab with the *same* editor still on screen now asks for nothing and redraws nothing.
    Refresh, a tab change, Close and the save's own refresh are untouched; arriving on a
    *different* tab still fetches, so no editor is ever kept over a lane it does not belong to.
  * Recovery and Automations were written before the panel had a dictionary, so a Chinese page
    showed Chinese chrome around English controls: "Recipes", "Save automation", "Permit
    external writes", "needs_you". The strings below are read off the rendered page in zh and
    zh-TW, with English asserted unchanged in the same places.

Everything is read from the shipped index.html served by the staged fixture server: the real
`t()`/data-i18n path, the real fetch paths, the product's own buttons. Requests are counted by
intercepting them, not inferred from timing. No provider is contacted and nothing is saved --
`/api/automations/upsert` is asserted never to be posted by any of this.
"""
import json

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import (  # noqa: F401  (fixtures are used by name)
    TOKEN, _Fixture, _fence, _reset_fixture_state, browser, server, ui,
)

SPECS = {"specs": [{"automation_id": "nightly-report", "task": "summarize the day",
                    "trigger": {"provider": "timer", "every_s": 3600}, "enabled": True}],
         "recipes": [{"label": "Daily digest", "description": "Summarize yesterday",
                      "spec": {"automation_id": "daily-digest", "task": "summarize",
                               "trigger": {"provider": "timer", "every_s": 86400}}}],
         "executions": [{"automation_id": "nightly-report", "execution_id": "x-1",
                         "state": "needs_you", "usage": {"model_tokens": 10, "cost_usd": 0.1}}]}

DRAFT_ID = "draft-report"
DRAFT_TASK = "words the user has not saved yet"
FENCE_REASON = "the deploy may have run"


def _automations(page, counter=None, body=None):
    """Serve the automations listing, recording every read the page makes."""
    def handler(route):
        if counter is not None:
            counter.append(route.request.url)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body if body is not None else SPECS))
    page.route("**/api/automations?*", handler)


def _open_panel(page):
    page.click("#topbarMore > summary")
    page.click("#activityBtn")
    page.locator("#activityPanel").wait_for(state="visible")
    page.evaluate("() => document.getElementById('topbarMore').removeAttribute('open')")


def _automations_tab(page):
    page.locator('[data-control-tab="automations"]').click()
    expect(page.locator("#activityGrid")).to_contain_text("nightly-report")


def _type_draft(page, name="New automation"):
    page.get_by_role("button", name=name, exact=True).click()
    page.fill("#autoId", DRAFT_ID)
    page.fill("#autoTask", DRAFT_TASK)
    page.fill("#autoCost", "3.5")
    page.check("#autoExternal")


def _reopen_panel(page):
    """Close and reopen with the panel's own controls, as a person does."""
    page.click("#activityClose")
    expect(page.locator("#activityPanel")).to_be_hidden()
    page.click("#topbarMore > summary")
    page.click("#activityBtn")
    page.locator("#activityPanel").wait_for(state="visible")
    page.evaluate("() => document.getElementById('topbarMore').removeAttribute('open')")
    page.wait_for_timeout(250)          # any fetch this opening started would land by here


def test_closing_and_reopening_the_panel_keeps_the_unsaved_draft(ui):
    page = ui.page
    reads, posts = [], []
    _automations(page, reads)
    page.route("**/api/automations/**", lambda route: posts.append(route.request.url))
    _open_panel(page)
    _automations_tab(page)
    _type_draft(page)
    before = len(reads)
    _reopen_panel(page)
    assert page.input_value("#autoId") == DRAFT_ID
    assert page.input_value("#autoTask") == DRAFT_TASK
    assert page.input_value("#autoCost") == "3.5"
    assert page.is_checked("#autoExternal")
    assert len(reads) == before, "reopening the same tab asked for the listing again: %r" % reads
    assert posts == [], "reopening a panel must never write: %r" % posts


def test_the_draft_that_survived_a_reopen_still_saves_what_was_typed(ui):
    """Keeping the form is only worth anything if the form still works afterwards."""
    page = ui.page
    posts = []
    _automations(page)

    def upsert(route):
        posts.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
    page.route("**/api/automations/upsert*", upsert)
    _open_panel(page)
    _automations_tab(page)
    _type_draft(page)
    _reopen_panel(page)
    page.click("#autoSave")
    expect(page.locator("#activityNotice")).to_have_text("Automation saved.")
    assert len(posts) == 1
    spec = posts[0]["spec"]
    assert spec["automation_id"] == DRAFT_ID and spec["task"] == DRAFT_TASK
    assert spec["budget"]["max_cost_usd"] == 3.5
    assert spec["permissions"]["external_writes"] is True


def test_the_external_writes_checkbox_is_named_on_one_line(ui):
    """At 1280 the permission's name broke over three lines beside an empty half-row."""
    page = ui.page
    _automations(page)
    _open_panel(page)
    _automations_tab(page)
    _type_draft(page)
    # Line boxes, not pixels: the name is one line or it is not. (Before the fix each of these
    # names was laid out over three lines while two thirds of the row stayed empty.)
    sizes = page.evaluate("""() => {
      const lines = (id) => {
        const label = document.getElementById(id).closest('label');
        const range = document.createRange(); range.selectNode(label.lastChild);
        return range.getClientRects().length;
      };
      const box = (id) => document.getElementById(id).closest('label').getBoundingClientRect();
      return {enabled: lines('autoEnabled'), external: lines('autoExternal'),
              room: document.querySelector('.control-checks').getBoundingClientRect().width,
              used: box('autoExternal').right - box('autoEnabled').left};
    }""")
    assert sizes["external"] == 1 and sizes["enabled"] == 1, (
        "a checkbox name broke over several lines: %r" % sizes)
    assert sizes["used"] < sizes["room"], "both checkboxes fit the row they are given: %r" % sizes


def test_an_explicit_refresh_still_redraws_the_tab_and_drops_the_editor(ui):
    """The guard is about the lid, not about every fetch: Refresh stays exactly as explicit."""
    page = ui.page
    reads = []
    _automations(page, reads)
    _open_panel(page)
    _automations_tab(page)
    _type_draft(page)
    before = len(reads)
    page.click("#activityRefresh")
    expect(page.locator(".control-editor")).to_have_count(0)
    assert len(reads) == before + 1


def test_a_tab_change_still_redraws_and_drops_the_editor(ui):
    page = ui.page
    _automations(page)
    _open_panel(page)
    _automations_tab(page)
    _type_draft(page)
    page.locator('[data-control-tab="memory"]').click()
    expect(page.locator(".control-editor")).to_have_count(0)


def test_close_on_the_editor_still_discards_it_and_reopening_shows_no_form(ui):
    page = ui.page
    _automations(page)
    _open_panel(page)
    _automations_tab(page)
    _type_draft(page)
    page.locator(".control-editor").get_by_role("button", name="Close", exact=True).click()
    expect(page.locator(".control-editor")).to_have_count(0)
    _reopen_panel(page)
    expect(page.locator(".control-editor")).to_have_count(0)
    assert "nightly-report" in page.locator("#activityGrid").text_content()


def test_arriving_on_another_tab_does_not_keep_the_editor_over_the_wrong_lane(ui):
    """A conversation's own recovery note opens Recovery. An automations draft is not Recovery."""
    page = ui.page
    _automations(page)
    _open_panel(page)
    _automations_tab(page)
    _type_draft(page)
    page.click("#activityClose")
    expect(page.locator("#activityPanel")).to_be_hidden()
    _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-fence", FENCE_REASON)
    ui.ask("Deploy the release to staging")
    page.locator(".recovery-note button").click()
    page.wait_for_selector("#activityPanel:not([hidden])", timeout=8000)
    page.wait_for_selector(".activity-row.is-focus", timeout=8000)
    assert "on" in (page.get_attribute('[data-control-tab="recovery"]', "class") or "")
    expect(page.locator(".control-editor")).to_have_count(0)
    assert _Fixture.reconcile_posts == [], "arriving is not deciding"


# ---- what the panel is called, in the languages the product ships -------------------------

def _open_lang(server, browser, lang, routes=None):
    _reset_fixture_state()
    _Fixture.lang = lang
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    if routes:
        routes(page)
    page.goto(server + "/?token=" + TOKEN, wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    page.wait_for_timeout(400)          # the boot /api/settings answer lands and applyLang() runs
    assert page.evaluate("() => document.documentElement.lang") == lang
    return context, page, errors


@pytest.fixture(autouse=True)
def _restore_fixture_language(monkeypatch):
    monkeypatch.setattr(_Fixture, "lang", _Fixture.lang)


# Automations: lane titles, row states, every action on a row, and the editor's own fields.
AUTOMATIONS = {
    "en": ["Recipes", "Configured", "Execution history", "New automation", "Use", "Edit",
           "Disable", "Preview", "Run now", "template", "enabled",
           "Build, dry-run and inspect bounded timer, file, page and webhook automations."],
    "zh": ["预置模板", "已配置", "执行记录", "新建自动化", "使用", "编辑",
           "停用", "预览", "立即运行", "模板", "已启用",
           "创建、试运行并查看受额度约束的定时、文件、页面和 Webhook 自动化。"],
    "zh-tw": ["預置範本", "已設定", "執行記錄", "新增自動化", "使用", "編輯",
              "停用", "預覽", "立即執行", "範本", "已啟用",
              "建立、試執行並查看受額度限制的定時、檔案、頁面和 Webhook 自動化。"],
}
EDITOR = {
    "en": ["Automation editor", "Trigger", "Workspace", "Isolated", "Permit external writes",
           "Enabled", "Dry run trigger", "Save automation", "Readable roots (comma/newline)",
           "Collie keeps working until the task is done or one of these budgets runs out"],
    "zh": ["自动化编辑器", "触发方式", "工作区", "隔离目录", "允许对外写入",
           "启用", "试运行触发条件", "保存自动化", "可读取目录（逗号或换行分隔）",
           "Collie 会一直做到任务完成，或其中一项额度用尽为止"],
    "zh-tw": ["自動化編輯器", "觸發方式", "工作區", "隔離目錄", "允許對外寫入",
              "啟用", "試執行觸發條件", "儲存自動化", "可讀取目錄（逗號或換行分隔）",
              "Collie 會一直做到任務完成，或其中一項額度用盡為止"],
}
# Recovery: the lane, the severity a person has to act on, and the actions offered on the row.
RECOVERY = {
    "en": ["Needs You & recovery", "needs_you", "Interrupted interactive action", "completed",
           "Not fired", "items", "need a decision"],
    "zh": ["需要你处理与恢复", "需要你决定", "交互操作已中断", "已完成", "未触发", "项", "需要决定"],
    "zh-tw": ["需要你處理與復原", "需要你決定", "互動操作已中斷", "已完成", "未觸發", "項", "需要決定"],
}


@pytest.mark.parametrize("lang", ["en", "zh", "zh-tw"])
def test_the_automations_tab_speaks_the_pages_language(server, browser, lang):
    context, page, errors = _open_lang(server, browser, lang, _automations)
    try:
        _open_panel(page)
        page.locator('[data-control-tab="automations"]').click()
        expect(page.locator("#activityGrid")).to_contain_text("nightly-report")
        shown = page.text_content("#activityPanel")
        for word in AUTOMATIONS[lang]:
            assert word in shown, "%s: %r missing from the automations tab" % (lang, word)
        assert "nightly-report" in shown, "an automation's own id is not translated"
        assert "summarize the day" in shown, "nor is the task text a person wrote"
    finally:
        context.close()
    assert errors == [], "JS errors: %r" % errors


@pytest.mark.parametrize("lang", ["en", "zh", "zh-tw"])
def test_the_automation_editor_speaks_the_pages_language(server, browser, lang):
    context, page, errors = _open_lang(server, browser, lang, _automations)
    try:
        _open_panel(page)
        page.locator('[data-control-tab="automations"]').click()
        expect(page.locator("#activityGrid")).to_contain_text("nightly-report")
        page.get_by_role("button", name=AUTOMATIONS[lang][3], exact=True).click()
        editor = page.text_content(".control-editor")
        for word in EDITOR[lang]:
            assert word in editor, "%s: %r missing from the editor" % (lang, word)
        assert page.text_content("#autoBudgetHelp"), "the budget help is never empty"
    finally:
        context.close()
    assert errors == [], "JS errors: %r" % errors


@pytest.mark.parametrize("lang", ["en", "zh", "zh-tw"])
def test_the_recovery_tab_speaks_the_pages_language(server, browser, lang):
    def routes(page):
        _automations(page)
    context, page, errors = _open_lang(server, browser, lang, routes)
    try:
        _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-fence", FENCE_REASON)
        _open_panel(page)
        page.locator('[data-control-tab="recovery"]').click()
        expect(page.locator("#activityGrid")).to_contain_text(FENCE_REASON)
        shown = page.text_content("#activityPanel")
        for word in RECOVERY[lang]:
            assert word in shown, "%s: %r missing from the recovery tab" % (lang, word)
        if lang != "en":
            assert "needs_you" not in shown, "a raw API severity is not a human label"
        assert _Fixture.reconcile_posts == [], "reading a lane resolves nothing"
    finally:
        context.close()
    assert errors == [], "JS errors: %r" % errors


def test_the_localized_row_still_confirms_and_posts_the_api_value(server, browser):
    """Translating a label must not translate what is sent: `completed` stays `completed`."""
    posts = []

    def routes(page):
        _automations(page)

        def reconcile(route):
            posts.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
        page.route("**/api/recovery/reconcile*", reconcile)
    context, page, errors = _open_lang(server, browser, "zh", routes)
    try:
        _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-fence", FENCE_REASON)
        _open_panel(page)
        page.locator('[data-control-tab="recovery"]').click()
        expect(page.locator("#activityGrid")).to_contain_text(FENCE_REASON)
        seen = []
        page.on("dialog", lambda d: (seen.append(d.message), d.accept()))
        page.locator(".activity-row").first.get_by_role("button", name="已完成", exact=True).click()
        page.wait_for_timeout(300)
        assert seen and "我已检查" in seen[0], "the confirmation is asked in the page's language"
        assert posts == [{"session": "s-fence", "resolution": "completed", "confirmed": True}]
    finally:
        context.close()
    assert errors == [], "JS errors: %r" % errors
