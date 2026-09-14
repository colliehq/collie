"""The rest of what a capability answer reports, across a language change.

Same staged Chromium setup as test_web_run_capability_locale: the worker rows under Run setup
carry the probe availability and quota the backend reported, on nodes whose initial text is a
generic placeholder the language switch translates. Relabelling the chrome must keep those
reports, must not invent one the page never received, and must not disturb a late answer.

Everything is staged over Playwright interception; no provider, worker, account or installed
setting is touched, and the quota figures below are invented fixtures.
"""
import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser  # noqa: F401
from test_web_settings_capabilities import BOOTED, caps
from test_web_run_capability_locale import LocalizedRunSettings

DETAIL = "collie-cli 2.1 at C:/tools/collie"        # opaque backend detail, never a UI phrase
WORKERS = [{"key": "claude-code", "probe": {"availability": "runnable-verified",
                                            "runnable": True, "detail": DETAIL}}]
SIGNALS = {"claude-code": {"quota": {"primary": {"used_percent": 45, "resets_at": 0}}}}
# What the dictionaries hold for the two UI-owned phrases in that line.
AVAILABLE = {"zh": "可运行 · 已验证兼容性", "zh-tw": "可執行 · 已驗證相容性"}
USED = {"zh": "额度已用", "zh-tw": "額度已用"}
PLACEHOLDER = {"zh": "正在检查安装与登录状态…", "zh-tw": "正在檢查安裝與登入狀態…"}
WORKER_PREFIX = {"zh": "使用设置中选择的工作进程", "zh-tw": "使用設定中選擇的工作程序"}


class Workers(LocalizedRunSettings):
    """A staged backend that reports worker probes, and can stop answering."""

    def __init__(self, ui):
        super().__init__(ui)
        self.fail = False

    def _caps(self, route):
        self.reads.append(dict(self.values))
        if self.fail:
            route.abort()
            return
        if self.hold_first and len(self.reads) == 1:
            self.held.append(route)
            return
        payload = caps(BOOTED, tiers=("standard", "fast"), worker=self.values["RUNNER"])
        payload.update(interactive_speed_default=self.values["INTERACTIVE_SPEED"],
                       workers=WORKERS, worker_signals=SIGNALS)
        route.fulfill(json=payload)

    def worker_status(self, key):
        return self.page.locator('[data-worker-status="%s"]' % key)


@pytest.mark.parametrize("lang", ["zh", "zh-tw"])
def test_language_change_keeps_the_reported_worker_status_and_quota(ui, lang):
    s = Workers(ui).open()
    expect(s.worker_status("claude-code")).to_have_text("runnable-verified · 45% quota used")
    before_reads = len(s.reads)

    s.change_language(lang)

    # The availability key and the word "quota used" are the UI's own phrases and are translated;
    # the 45% the backend reported, and the tooltip detail, are facts and stay.
    expect(s.worker_status("claude-code")).to_contain_text(AVAILABLE[lang])
    expect(s.worker_status("claude-code")).to_contain_text("45% " + USED[lang])
    expect(s.worker_status("claude-code")).to_have_attribute("title", DETAIL)
    assert len(s.reads) == before_reads
    assert s.posts == [{"LANG": lang}]


@pytest.mark.parametrize("lang", ["zh", "zh-tw"])
def test_a_language_change_after_a_failed_read_invents_no_capability(ui, lang):
    """A read that did not answer leaves no facts to redraw, so the placeholder is what is true."""
    s = Workers(ui).open()
    s.fail = True
    s.open_panel().save("RUNNER", "claude-code")          # this re-read is the one that fails
    expect(ui.page.locator("#runFastItem")).to_be_hidden()
    s.close_panel()

    s.change_language(lang)

    expect(s.worker_desc).to_have_text(WORKER_PREFIX[lang])   # no worker ID claimed
    expect(s.worker_status("claude-code")).to_have_text(PLACEHOLDER[lang])
    assert s.note.text_content() != BOOTED                    # nor the withdrawn pair's sentence
    assert s.posts == [{"RUNNER": "claude-code"}, {"LANG": lang}]


@pytest.mark.parametrize("lang", ["zh", "zh-tw"])
def test_an_answer_that_lands_after_the_language_change_is_drawn_in_that_language(ui, lang):
    """The read this page starts at load may still be travelling when the language is switched."""
    s = Workers(ui)
    s.hold_first = True
    s.open()
    s.change_language(lang)
    s.close_panel()

    payload = caps(BOOTED, tiers=("standard", "fast"), worker="claude-code")
    payload.update(workers=WORKERS, worker_signals=SIGNALS)
    s.release(payload)
    s.barrier("after-the-language-change")

    expect(s.note).to_have_text(BOOTED)
    expect(s.worker_desc).to_have_text(WORKER_PREFIX[lang] + ": claude-code")
    expect(s.worker_status("claude-code")).to_contain_text("45% " + USED[lang])
    assert len(s.reads) == 1
