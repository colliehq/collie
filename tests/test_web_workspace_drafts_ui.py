import base64
import json
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect
from test_web_ui_run_status import ui, server, browser, _Fixture, _hold_queue, PROJECT_CWD


def read_thread(page):
    page.locator('.thread').filter(has_text='Read README.md').first.click()
    expect(page).to_have_url(__import__('re').compile('session=s-read'))


def attach(page):
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==')
    page.locator('#fileInput').set_input_files({'name':'draft.png','mimeType':'image/png','buffer':png})
    expect(page.locator('#attachStrip .thumb')).to_have_count(1)


def test_drafts_belong_to_their_thread_and_survive_navigation_and_reload(ui):
    page=ui.page
    page.locator('#input').fill('New task draft')
    read_thread(page)
    expect(page.locator('#input')).to_have_value('')
    page.locator('#input').fill('README follow-up draft')
    attach(page)
    page.locator('#newChat').click()
    expect(page.locator('#input')).to_have_value('New task draft')
    expect(page.locator('#attachStrip .thumb')).to_have_count(0)
    read_thread(page)
    expect(page.locator('#input')).to_have_value('README follow-up draft')
    expect(page.locator('#attachStrip .thumb')).to_have_count(1)
    page.reload(wait_until='load')
    expect(page.locator('#input')).to_have_value('README follow-up draft')
    expect(page.locator('#draftNotice')).to_contain_text('Reattach files')
    page.locator('#input').press('Enter')
    assert not _Fixture.stream_requests
    page.locator('#draftNotice').get_by_role('button',name='Keep text only').click()
    expect(page.locator('#draftNotice')).to_be_hidden()
    page.locator('#input').press('Enter')
    page.wait_for_timeout(250)
    assert len(_Fixture.stream_requests)==1


def test_queue_ack_after_leaving_removes_only_the_accepted_draft(ui):
    page=ui.page
    _hold_queue(ui); _Fixture.queue_ack.clear()
    page.locator('#input').fill('Accepted after I leave')
    page.locator('#input').press('Enter')
    assert _Fixture.queue_seen.wait(3)
    page.locator('#newChat').click()
    page.locator('#input').fill('A different new task draft')
    _Fixture.queue_ack.set()
    page.wait_for_function("() => sessionStorage.getItem('collie-composer-draft:v1:s-read') === null")
    expect(page.locator('#input')).to_have_value('A different new task draft')
    read_thread(page)
    expect(page.locator('#input')).to_have_value('')
    _Fixture.queue_release.set()


def test_returning_before_queue_ack_does_not_leave_an_accepted_request_in_the_composer(ui):
    page=ui.page
    _hold_queue(ui); _Fixture.queue_ack.clear()
    page.locator('#input').fill('Queued while navigating')
    page.locator('#input').press('Enter')
    assert _Fixture.queue_seen.wait(3)
    page.locator('#newChat').click()
    page.locator('#input').fill('Unrelated draft')
    read_thread(page)
    expect(page.locator('#input')).to_have_value('Queued while navigating')
    _Fixture.queue_ack.set()
    expect(page.locator('#input')).to_have_value('')
    assert len(_Fixture.queue_posts)==1
    _Fixture.queue_release.set()


def test_folder_selection_is_checked_before_send_and_changes_check_discovery(ui):
    page=ui.page; seen=[]; streams=[]
    page.on('request',lambda request:streams.append(parse_qs(urlsplit(request.url).query))
            if urlsplit(request.url).path=='/api/stream' else None)
    def detect(route):
        query=parse_qs(urlsplit(route.request.url).query)
        folder=query.get('cwd',['/default'])[0]; seen.append(folder)
        route.fulfill(status=409 if folder=='/missing' else 200, content_type='application/json',
            body=json.dumps({'error':'workspace is missing'} if folder=='/missing' else
                {'cwd':folder,'candidates':[{'command':'check '+folder,'source':'project'}]}))
    page.route('**/api/verification*',detect)
    page.reload(wait_until='load')
    # The folder shown is the one a check answered with: the shared fixture named it on the first
    # load (as the real route always does), and this reload's check confirms that same one.
    expect(page.locator('#taskWorkspacePath')).to_have_text(PROJECT_CWD)
    page.locator('#taskWorkspace summary').click()
    page.locator('#taskFolder').fill('/missing')
    page.locator('#input').fill('Implement it')
    page.locator('#input').press('Enter')
    assert not _Fixture.stream_requests
    page.locator('#taskFolderUse').click()
    expect(page.locator('#taskWorkspaceStatus')).to_contain_text('workspace is missing')
    page.locator('#taskFolder').fill('/selected')
    page.locator('#taskFolderUse').click()
    expect(page.locator('#taskWorkspacePath')).to_have_text('/selected')
    expect(page.locator('#verifyCommand')).to_have_value('check /selected')
    page.locator('#input').press('Enter')
    page.wait_for_timeout(300)
    assert streams[-1]['cwd']==['/selected']


def test_background_check_refresh_does_not_block_sending_in_the_unchanged_folder(ui):
    page=ui.page; pending=[]; hold=False
    def detect(route):
        if hold:
            pending.append(route); return
        route.fulfill(content_type='application/json',body='{"cwd":"/project","candidates":[]}')
    page.route('**/api/verification*',detect)
    page.reload(wait_until='load')
    expect(page.locator('#taskWorkspacePath')).to_have_text('/project')
    hold=True
    page.locator('#newChat').click()
    page.locator('#input').fill('/code keep working')
    page.locator('#input').press('Enter')
    expect(page.locator('#input')).to_have_value('')
    for route in pending:
        route.fulfill(content_type='application/json',body='{"cwd":"/project","candidates":[]}')


def test_expanding_pack_setup_keeps_send_reachable(ui):
    page=ui.page
    page.set_viewport_size({'width':1200,'height':820})
    page.locator('#newChat').click()
    page.locator('#modeTrigger').click()
    page.locator('[data-axis="strategy"][data-val="pack"]').click()
    page.locator('#packN').fill('7')
    page.locator('#packCheck').fill('pytest -q')
    page.locator('#input').fill('/code invalid pack should stay local')
    page.locator('#send').click(timeout=3000)
    expect(page.locator('#packN')).to_be_focused()
    assert not _Fixture.stream_requests


def test_isolated_folder_is_visible_and_apply_is_an_explicit_action(ui):
    page=ui.page; applied=[]; isolated=True
    def session(route):
        route.fulfill(content_type='application/json',body=json.dumps({'messages':[],
            'cwd':'/isolation' if isolated else '/main',
            'workspace':{'mode':'isolated','path':'/isolation','origin':'/main','branch':'task'} if isolated else {'mode':'local','path':'/main'}}))
    def handoff(route):
        nonlocal isolated
        applied.append(route.request.post_data_json); isolated=False
        route.fulfill(content_type='application/json',body='{"ok":true}')
    page.route('**/api/session/s-read',session)
    page.route('**/api/verification*',lambda route:route.fulfill(content_type='application/json',body=json.dumps({'cwd':'/isolation' if isolated else '/main','candidates':[]})))
    page.route('**/api/session/handoff*',handoff)
    read_thread(page)
    expect(page.locator('#taskWorkspaceLabel')).to_have_text('Isolated folder')
    page.locator('#taskWorkspace summary').click()
    expect(page.locator('#taskWorkspaceNote')).to_contain_text('/main')
    assert not applied
    page.locator('#taskWorkspaceApply').click()
    expect(page.locator('#taskWorkspacePath')).to_have_text('/main')
    assert applied==[{'session':'s-read','target':'local','confirm':True,'remove_isolated':False}]
    expect(page.locator('#taskWorkspaceApply')).to_be_hidden()


def test_missing_workspace_exposes_an_explicit_location_recovery(ui):
    page=ui.page; gone=True; relocated=[]
    def session(route):
        route.fulfill(content_type='application/json',body=json.dumps({'messages':[],
            'cwd':'/gone' if gone else '/restored','workspace':{'mode':'isolated','path':'/gone','origin':'/main'} if gone else {}}))
    def detect(route):
        route.fulfill(status=409 if gone else 200,content_type='application/json',
            body=json.dumps({'error':'workspace is missing','workspace_missing':True} if gone else {'cwd':'/restored','candidates':[]}))
    def relocate(route):
        nonlocal gone
        relocated.append(route.request.post_data_json); gone=False
        route.fulfill(content_type='application/json',body='{"ok":true}')
    page.route('**/api/session/s-read',session)
    page.route('**/api/verification*',detect)
    page.route('**/api/session/relocate*',relocate)
    read_thread(page)
    expect(page.locator('#taskWorkspaceNote')).to_contain_text('does not copy or recover files')
    expect(page.locator('#taskWorkspaceApply')).to_be_hidden()
    expect(page.locator('#taskFolder')).to_be_editable()
    page.locator('#taskFolder').fill('/restored')
    page.locator('#taskFolderUse').click()
    expect(page.locator('#taskWorkspacePath')).to_have_text('/restored')
    assert relocated==[{'session':'s-read','cwd':'/restored'}]
    assert not _Fixture.stream_requests
