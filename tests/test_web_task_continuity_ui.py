import json
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect
from test_web_ui_run_status import ui, server, browser


def open_session(ui, sid):
    page=ui.page
    url=page.url.split('?')[0]+'?session='+sid
    page.goto(url, wait_until='domcontentloaded')
    page.wait_for_timeout(250)


def choose(ui, axis, value):
    page=ui.page
    if not page.locator('#modeMenu').is_visible():page.locator('#modeTrigger').click()
    page.locator('.mode-item[data-axis="%s"][data-val="%s"]'%(axis,value)).click()


def test_required_check_survives_refresh_and_stays_with_its_conversation(ui):
    open_session(ui,'saved-choice')
    choose(ui,'verification','required')
    ui.page.locator('#modeClose').click()
    ui.page.locator('#verifyCommand').fill('python -m unittest -q')
    ui.page.reload(wait_until='domcontentloaded')
    expect(ui.page.locator('#modeLabel')).to_contain_text('Verify')
    expect(ui.page.locator('#verifyCommand')).to_have_value('python -m unittest -q')
    ui.page.locator('#newChat').click()
    expect(ui.page.locator('#modeLabel')).to_have_text('Auto')
    expect(ui.page.locator('#verifyCommand')).to_have_value('')
    open_session(ui,'saved-choice')
    expect(ui.page.locator('#modeLabel')).to_contain_text('Verify')


def test_automatic_application_is_never_restored_and_reset_is_available(ui):
    open_session(ui,'isolated-choice')
    choose(ui,'strategy','pack')
    ui.page.locator('#modeClose').click()
    ui.page.locator('#packApply').check()
    ui.page.reload(wait_until='domcontentloaded')
    expect(ui.page.locator('#packApply')).not_to_be_checked()
    ui.page.locator('#modeTrigger').click()
    ui.page.locator('#resetRunSetup').click()
    expect(ui.page.locator('#modeLabel')).to_have_text('Auto')


def test_check_discovery_uses_session_and_keeps_a_manual_command(ui):
    seen=[]
    def detect(route):
        sid=parse_qs(urlsplit(route.request.url).query).get('session',[''])[0]
        seen.append(sid)
        route.fulfill(content_type='application/json',body=json.dumps({
            'session':sid,'cwd':'/project/'+sid,'candidates':[{'command':'test-'+sid,'source':'project'}]}))
    ui.page.route('**/api/verification*',detect)
    open_session(ui,'one')
    expect(ui.page.locator('#verifyCommand')).to_have_value('test-one')
    choose(ui,'verification','required')
    ui.page.locator('#modeClose').click()
    ui.page.locator('#verifyCommand').fill('manual-check')
    ui.page.reload(wait_until='domcontentloaded')
    expect(ui.page.locator('#verifyCommand')).to_have_value('manual-check')
    open_session(ui,'two')
    expect(ui.page.locator('#verifyCommand')).to_have_value('test-two')
    assert 'one' in seen and 'two' in seen


def test_pending_requests_are_visible_on_today_and_after_reload(ui):
    row={'session':'waiting-task','pending':2,'claimed':0,'owner_busy':False,
         'title':'Prepare weekly report','journal':'ok','updated':123}
    ui.page.route('**/api/task-inbox/pending*',lambda route:route.fulfill(
        content_type='application/json',body=json.dumps({'sessions':[row]})))
    ui.page.reload(wait_until='domcontentloaded')
    expect(ui.page.locator('#todayAttention')).to_contain_text('2 saved requests waiting')
    expect(ui.page.locator('#needsYouNav')).to_be_visible()
    expect(ui.page.locator('.queued-input')).to_contain_text('2 saved requests waiting')
    ui.page.locator('#needsYouNav').click()
    expect(ui.page).to_have_url(__import__('re').compile('session=waiting-task'))


def test_scheduler_failure_is_visible_only_when_tasks_need_automatic_progress(ui):
    data={'missions':[{'mission_id':'waiting','state':'queued','title':'Prepare report'}],
          'scheduler':{'last_error':'Model connection unavailable'}}
    ui.page.route('**/api/missions*',lambda route:route.fulfill(
        content_type='application/json',body=json.dumps(data)))
    ui.page.reload(wait_until='domcontentloaded')
    expect(ui.page.locator('#todayAttention')).to_contain_text('Automatic task progress is temporarily blocked')
    ui.page.locator('#navMissions').click()
    expect(ui.page.locator('#missionsNotice')).to_contain_text('Model connection unavailable')
    data['scheduler']['last_error']=''
    ui.page.locator('#missionsRefresh').click()
    expect(ui.page.locator('#missionsNotice')).to_have_text('')
