"""Scheduled waits show local dates without parsing the agent's result prose."""
import json
import re

import pytest
from playwright.sync_api import expect
from test_web_ui_run_status import ui, server, browser
from test_web_mission_updates_ui import stage, open_task


@pytest.mark.parametrize('language,width,theme,label,date', [
    ('en', 1280, 'light', 'Next check', 'Sep 7, 2026'),
    ('zh', 390, 'dark', '下次检查', '2026年9月7日'),
    ('zh-tw', 320, 'light', '下次檢查', '2026年9月7日'),
])
def test_wait_shows_the_local_day_time_and_zone_without_a_human_prompt(
        ui, language, width, theme, label, date):
    data = stage(ui)
    data['state'].update(state='waiting', next_wake_at=1788845400,
                         result='code slice checkpointed; continuing automatically',
                         controls=['pause', 'cancel'])
    data['state']['summary']['next'] = 'Collie will continue checking at the scheduled time.'
    data['state']['summary']['current'] = data['state']['result']
    ui.page.route('**/api/settings', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'values': {'LANG': language}})))
    ui.page.context.new_cdp_session(ui.page).send(
        'Emulation.setTimezoneOverride', {'timezoneId': 'America/Los_Angeles'})
    ui.page.reload(wait_until='load')
    open_task(ui)
    ui.page.emulate_media(color_scheme=theme)
    ui.page.set_viewport_size({'width': width, 'height': 844})
    row = ui.page.locator('.msummary-row').filter(has=ui.page.get_by_text(label, exact=True))
    expect(row).to_contain_text(date)
    expect(row).to_contain_text(re.compile(r'10:30|22:30'))
    expect(row).to_contain_text(re.compile(r'PDT|GMT-7'))
    expect(ui.page.locator('#needsYouNav')).to_be_hidden()
    expect(ui.page.locator('.msummary')).not_to_contain_text('1788845400')
    if language != 'en':
        expect(ui.page.locator('.msummary')).not_to_contain_text('code slice checkpointed')
    assert not ui.page.evaluate('document.documentElement.scrollWidth > innerWidth')
    assert not data['posts']


def test_a_stale_wait_timer_disappears_when_the_task_resumes(ui):
    data = stage(ui)
    data['state'].update(state='waiting', next_wake_at=1788845400)
    open_task(ui)
    row = ui.page.locator('.msummary-row').filter(has=ui.page.get_by_text('Next check', exact=True))
    expect(row).to_be_visible()
    data['state']['state'] = 'running'
    expect(row).to_have_count(0, timeout=15000)
