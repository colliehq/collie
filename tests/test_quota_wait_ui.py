"""Real queue rows report the server-owned schedule without starting work from the tab."""
import time

import pytest
from playwright.sync_api import expect
from test_web_ui_run_status import TOKEN, _Fixture, _reset_fixture_state, browser, server


@pytest.mark.parametrize('viewport', [{'width':1280,'height':900}, {'width':390,'height':844}])
@pytest.mark.parametrize('lang,labels', [
    ('zh', ('已提交，将在此时间后开始', '已提交，正在启动', '自动启动已停止，可随时手动开始')),
    ('zh-tw', ('已提交，將在此時間後開始', '已提交，正在啟動', '自動啟動已停止，可隨時手動開始')),
])
# `progress` is the host's verdict on its own timer, and the row states that verdict
# rather than re-deciding it from the tab's clock: a healthy wait, an admission in
# flight, a schedule that gave up.
@pytest.mark.parametrize('state,progress,index', [('waiting','scheduled',0),
                                                 ('admitting','starting',1),
                                                 ('retired','stopped',2)])
def test_scheduled_queue_state_is_localized_and_does_not_submit(
        server, browser, monkeypatch, viewport, lang, labels, state, progress, index):
    _reset_fixture_state()
    monkeypatch.setattr(_Fixture, 'lang', lang)
    entry={'id':'accepted-next','session':'s-read','mode':'follow_up','seq':1,
           'state':'pending','text':'already accepted request','digest':'v1','metadata':{}}
    monkeypatch.setattr(_Fixture, 'queue_entries', {entry['id']:entry})
    reason='Scheduler could not persist the claim <inspect>'
    retry_at=int(time.time())+120
    monkeypatch.setattr(_Fixture, 'queue_status_extra', {
        'owner_busy':False,
        'scheduled_wait':{'entry':entry['id'],'state':state,'retry_at':retry_at,
                          'next_attempt_at':0 if progress=='stopped' else retry_at,
                          'progress':progress,
                          'reason':reason if state=='retired' else ''}})
    context=browser.new_context(viewport=viewport)
    page=context.new_page(); errors=[]
    page.on('pageerror',lambda error: errors.append(str(error)))
    try:
        page.goto(server+'/?token='+TOKEN+'&session=s-read',wait_until='load')
        page.wait_for_selector('#input')
        row=page.locator('.task-queue-row:not(.retained)')
        expect(row).to_have_count(1)
        expect(row.locator('.task-queue-meta')).to_contain_text(labels[index])
        if state=='retired':
            expect(row.locator('.task-queue-meta')).to_have_attribute('title',reason)
        page.fill('#input','my unsent draft')
        expect(row.locator('button')).not_to_have_count(0)
        expect(page.locator('#input')).to_have_value('my unsent draft')
        assert _Fixture.queue_entries[entry['id']]['state']=='pending'
        assert _Fixture.queue_posts==[] and _Fixture.queue_starts==[]
    finally:
        context.close()
    assert errors==[]
