"""Capability facts must actually be visible, including on a narrow viewport."""
import pytest
from playwright.sync_api import expect
from test_web_ui_run_status import ui, server, browser  # noqa: F401
from test_web_run_capability_locale_workers import Workers
from test_web_settings_capabilities import BOOTED


@pytest.mark.parametrize('lang', ['en', 'zh'])
@pytest.mark.parametrize('width,height', [(1280, 900), (390, 500)])
def test_current_worker_and_capability_facts_are_visible_without_changing_choices(ui, lang, width, height):
    ui.page.set_viewport_size({'width': width, 'height': height})
    s = Workers(ui)
    s.values['RUNNER'] = 'claude-code'
    s.open()
    if lang != 'en':
        s.change_language(lang).close_panel()
    if ui.page.locator('#modeMenu').is_hidden():
        ui.page.click('#modeTrigger')
    before_reads = len(s.reads)
    expect(s.worker_desc).to_be_visible()
    expect(s.worker_desc).to_contain_text('claude-code')
    expect(s.worker_status('claude-code')).to_be_visible()
    expect(s.worker_status('claude-code')).to_contain_text('45%')
    expect(s.note).to_be_visible()
    expect(s.note).to_have_text(BOOTED)
    box = ui.page.locator('#modeMenu').bounding_box()
    assert box and box['x'] >= -1 and box['x'] + box['width'] <= width + 1
    assert ui.page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    expect(ui.page.locator('#runSpeed')).to_have_value('standard')
    assert len(s.reads) == before_reads
    assert s.posts == ([{'LANG': 'zh'}] if lang == 'zh' else [])
