"""Accepted routing settings must reach the existing run setup without a page reload.

Real Chromium page and interactions; settings and capability HTTP answers are staged.
The staged fields mirror webapp's settings-derived worker and interactive-speed defaults.
No native worker, provider, real account, or installed setting is used.
"""
import pytest
from playwright.sync_api import expect

from harness import settings
from test_web_ui_run_status import ui, server, browser  # noqa: F401
from test_web_settings_capabilities import Settings, SCHEMA, BOOTED, caps


class RunSettings(Settings):
    def __init__(self, ui):
        super().__init__(ui)
        self.values.update(RUNNER='collie', RUNNER_POOL='collie', INTERACTIVE_SPEED='standard')

    def _settings(self, route):
        if route.request.method != 'POST':
            rows = [row for row in settings.SCHEMA
                    if row['key'] in ('RUNNER', 'RUNNER_POOL', 'INTERACTIVE_SPEED')]
            route.fulfill(json={'schema':SCHEMA+rows, 'values':dict(self.values)})
            return
        super()._settings(route)

    def _caps(self, route):
        self.reads.append(dict(self.values))
        note = BOOTED if self.values['INTERACTIVE_SPEED'] == 'standard' else 'Current default: fast'
        payload = caps(note, tiers=('standard','fast'), worker=self.values['RUNNER'])
        payload.update(interactive_speed_default=self.values['INTERACTIVE_SPEED'],
                       worker_pool=self.values['RUNNER_POOL'])
        route.fulfill(json=payload)

    def choose(self, axis, value):
        self.page.click('#modeTrigger')
        self.page.click(f'.mode-item[data-axis="{axis}"][data-val="{value}"]')
        self.page.click('#modeClose')
        return self

    def save(self, key, value):
        self.page.select_option('#set_'+key, value)
        expect(self.status).to_have_class('set-status ok')
        return self


def test_saved_worker_description_changes_without_reopening_or_reloading(ui):
    s = RunSettings(ui).open()
    ui.page.fill('#input', 'keep my unfinished request')
    s.open_panel().save('RUNNER','claude-code')
    expect(s.worker_desc).to_contain_text('claude-code')
    assert ui.page.input_value('#runRunner') == ''  # still inherits Settings
    assert ui.page.input_value('#input') == 'keep my unfinished request'
    assert s.posts == [{'RUNNER':'claude-code'}]
    assert len(s.reads) == 2
    assert not s.reloaded()


def test_saved_interactive_speed_changes_the_inherited_run_choice(ui):
    s = RunSettings(ui).open()
    expect(ui.page.locator('#runSpeed')).to_have_value('standard')
    s.open_panel().save('INTERACTIVE_SPEED','fast')
    expect(ui.page.locator('#runSpeed')).to_have_value('fast')
    assert not ui.page.evaluate('() => !!window.runExplicitAxes.speed')
    assert s.posts == [{'INTERACTIVE_SPEED':'fast'}]
    assert len(s.reads) == 2
    assert not s.reloaded()


def test_a_saved_speed_does_not_replace_an_explicit_standard_choice(ui):
    s = RunSettings(ui).open().choose('speed','standard')
    assert ui.page.evaluate('() => window.runExplicitAxes.speed')
    s.open_panel().save('INTERACTIVE_SPEED','fast')
    expect(s.note).to_have_text('Current default: fast')
    expect(ui.page.locator('#runSpeed')).to_have_value('standard')
    assert s.reads[-1]['INTERACTIVE_SPEED'] == 'fast'
    assert ui.page.evaluate('() => window.runExplicitAxes.speed')


def test_pack_stays_standard_when_interactive_default_changes(ui):
    s = RunSettings(ui).open().choose('strategy','pack')
    s.open_panel().save('INTERACTIVE_SPEED','fast')
    expect(s.note).to_have_text('Current default: fast')
    expect(ui.page.locator('#runSpeed')).to_have_value('standard')
    assert s.reads[-1]['INTERACTIVE_SPEED'] == 'fast'
    assert not ui.page.evaluate('() => !!window.runExplicitAxes.speed')


def test_new_external_default_keeps_a_plan_runnable_with_collie(ui):
    s = RunSettings(ui).open().choose('intent','plan')
    s.open_panel().save('RUNNER','claude-code')
    expect(s.worker_desc).to_contain_text('claude-code')
    expect(ui.page.locator('#runRunner')).to_have_value('collie')
    assert ui.page.evaluate('() => window.runExplicitAxes.runner')


@pytest.mark.parametrize('key,value', [('RUNNER','claude-code'),('INTERACTIVE_SPEED','fast')])
@pytest.mark.parametrize('refused', [True,False], ids=['refused','environment-held'])
def test_unsaved_routing_defaults_do_not_refresh_or_change_the_run(ui, key, value, refused):
    s = RunSettings(ui).open().open_panel()
    before_reads = list(s.reads)
    if refused:
        s.refuse = (500, {'ok':False,'error':'Could not save the setting.'})
    else:
        s.pinned[key] = s.values[key]
    ui.page.select_option('#set_'+key, value)
    expect(s.status).to_have_class('set-status err')
    expect(s.worker_desc).to_contain_text('collie')
    expect(ui.page.locator('#runSpeed')).to_have_value('standard')
    assert s.reads == before_reads
    assert not s.reloaded()
