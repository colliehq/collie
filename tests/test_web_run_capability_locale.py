"""A language change must retain what the current run capabilities actually report.

Real Chromium DOM, with settings/model/capability HTTP responses staged by existing fixtures.
Changing chrome language may relabel a saved worker and a capability explanation, but must
not replace those facts with the HTML's generic initial placeholder or refetch/reselect a run.
"""
import pytest
from playwright.sync_api import expect
from harness import settings
from test_web_ui_run_status import ui, server, browser  # noqa: F401
from test_web_settings_capabilities import SCHEMA, BOOTED
from test_web_settings_run_defaults import RunSettings


class LocalizedRunSettings(RunSettings):
    def _settings(self, route):
        if route.request.method != 'POST':
            schema = [row for row in SCHEMA if row['key'] != 'LANG']
            schema.extend(row for row in settings.SCHEMA
                          if row['key'] in ('LANG','RUNNER','INTERACTIVE_SPEED'))
            route.fulfill(json={'schema':schema,'values':dict(self.values)})
            return
        super()._settings(route)

    def change_language(self, lang):
        self.open_panel()
        self.page.click('.set-nav[data-cat="advanced"]')
        self.page.select_option('#set_LANG', lang)
        expect(self.page.locator('html')).to_have_attribute('lang',lang)
        return self


@pytest.mark.parametrize('lang',['zh','zh-tw'])
def test_language_change_keeps_the_saved_worker_identity(ui,lang):
    s=LocalizedRunSettings(ui)
    s.values['RUNNER']='claude-code'
    s.open()
    expect(s.worker_desc).to_contain_text('claude-code')
    before_reads=len(s.reads)
    s.change_language(lang)
    expect(s.worker_desc).to_contain_text('claude-code')
    expected='使用设置中选择的工作进程' if lang=='zh' else '使用設定中選擇的工作程序'
    # The ID is a backend fact; the UI's introductory sentence should still be localized.
    expect(s.worker_desc).to_have_text(expected+': claude-code')
    assert len(s.reads)==before_reads
    assert s.posts==[{'LANG':lang}]


@pytest.mark.parametrize('lang',['zh','zh-tw'])
def test_language_change_keeps_the_current_capability_explanation(ui,lang):
    s=LocalizedRunSettings(ui).open()
    expect(s.note).to_have_text(BOOTED)
    before_reads=len(s.reads)
    s.change_language(lang)
    expect(s.note).to_have_text(BOOTED)  # opaque backend note, not the static placeholder
    assert len(s.reads)==before_reads


@pytest.mark.parametrize('lang',['zh','zh-tw'])
def test_language_change_keeps_explicit_speed_and_the_unfinished_message(ui,lang):
    s=LocalizedRunSettings(ui).open().choose('speed','fast')
    ui.page.fill('#input','Keep this request until I send it.')
    before_reads=len(s.reads)
    s.change_language(lang)
    expect(ui.page.locator('#runSpeed')).to_have_value('fast')
    assert ui.page.evaluate('() => window.runExplicitAxes.speed')
    expect(ui.page.locator('#input')).to_have_value('Keep this request until I send it.')
    assert len(s.reads)==before_reads
    assert s.posts==[{'LANG':lang}]
