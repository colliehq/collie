"""Actionable human handoffs without extra dialogs or reset form choices."""
import json

from playwright.sync_api import expect
from test_web_ui_run_status import ui, server, browser
from test_web_mission_updates_ui import stage, open_task


def test_local_essential_blocker_appears_outside_the_today_page(ui):
    data=stage(ui)
    data['state'].update(state='needs_you',needs_human=True,controls=['continue','cancel'])
    ui.page.locator('#newChat').click()
    expect(ui.page.locator('#needsYouNav')).to_be_visible(timeout=12000)
    expect(ui.page.locator('#needsYouCount')).to_have_text('1')
    ui.page.locator('#needsYouNav').click()
    expect(ui.page.locator('#pageTitle')).to_have_text(data['state']['goal'])


def test_optional_omissions_and_step_details_stay_open_across_poll(ui):
    data=stage(ui)
    data['state']['case']['skipped_steps']=[{'summary':'Bonus image','reason':'No purchase authorized'}]
    data['state']['steps']=[{'name':'inspect','verdict':'verified'}]*30
    open_task(ui)
    omitted=ui.page.locator('.mission-skipped')
    expect(omitted).to_contain_text('Optional steps skipped (1)')
    expect(ui.page.locator('#needsYouNav')).to_be_hidden()
    omitted.locator('summary').click()
    steps=ui.page.locator('details[data-detail-key="steps"]')
    steps.locator('summary').click()
    before=data['reads']; ui.page.wait_for_timeout(2400)
    assert data['reads']>before
    expect(omitted).to_have_attribute('open','')
    expect(steps).to_have_attribute('open','')
    expect(omitted).to_contain_text('No purchase authorized')
    controls=ui.page.locator('.macts')
    assert controls.bounding_box()['y']<omitted.bounding_box()['y']


def test_recovery_uses_unselected_outcome_choices_and_survives_refresh(ui):
    data=stage(ui)
    data['state'].update(state='recovery_required',controls=['reconcile','cancel'],
        code_session_recovery={'recovery_required':True,'reason':'uncertain tool boundary'},
        latest_checkpoint={'seq':9})
    posts=[]
    def reconcile(route):
        posts.append(route.request.post_data_json)
        data['state'].update(state='paused',controls=['resume','cancel'])
        route.fulfill(content_type='application/json',body=json.dumps(data['state']))
    ui.page.route('**/api/mission/reconcile*',reconcile)
    ui.page.locator('#navMissions').click()
    ui.page.locator('.mission-list-card').click()
    ui.page.get_by_role('button',name='Reconcile uncertain run',exact=True).click()
    panel=ui.page.locator('.mission-recovery')
    submit=panel.get_by_role('button',name='Save outcome and continue')
    expect(submit).to_be_disabled()
    assert panel.locator('input[type=radio]:checked').count()==0
    panel.get_by_label('The action did not happen',exact=True).check()
    expect(submit).to_be_disabled()
    panel.get_by_label('I checked the actual outcome',exact=True).check()
    before=data['reads']; ui.page.wait_for_timeout(2400)
    assert data['reads']>before
    expect(panel.get_by_label('The action did not happen',exact=True)).to_be_checked()
    expect(panel.get_by_label('I checked the actual outcome',exact=True)).to_be_checked()
    ui.page.set_viewport_size({'width':390,'height':844})
    assert ui.page.evaluate('document.documentElement.scrollWidth<=document.documentElement.clientWidth+1')
    submit.click()
    expect(panel).to_be_hidden()
    assert posts==[{'id':'mission-ui','note':'user inspected uncertain external state','code_resolution':'not_fired'}]


def test_acknowledgement_targets_the_exact_requirement_and_keeps_pause(ui):
    data=stage(ui)
    data['state'].update(state='paused',controls=['resume','cancel'])
    data['state']['summary']['pending_authorizations']=[{'id':'auth_one','summary':'Use security key to sign in','kind':'security_key'}]
    posts=[]
    def acknowledge(route):
        posts.append(route.request.post_data_json)
        data['state']['summary']['pending_authorizations']=[]
        route.fulfill(content_type='application/json',body=json.dumps(dict(data['state'],acknowledged=True)))
    ui.page.route('**/api/mission/authorization*',acknowledge)
    open_task(ui)
    ui.page.get_by_role('button',name='I handled this requirement',exact=True).click()
    expect(ui.page.locator('.mauth')).to_have_count(0)
    expect(ui.page.get_by_role('button',name='Resume',exact=True)).to_be_visible()
    assert posts==[{'id':'mission-ui','request_id':'auth_one','decision':'handled'}]


def test_resolved_authorization_projection_hides_stale_case_entries(ui):
    data=stage(ui)
    data['state']['case']['pending_authorizations']=[{'id':'old','summary':'Already handled'}]
    data['state']['summary']['pending_authorizations']=[]
    open_task(ui)
    expect(ui.page.locator('.mauth')).to_have_count(0)


def test_unrelated_pending_authorization_does_not_remove_handoff_controls(ui):
    data=stage(ui)
    data['state'].update(state='needs_you',needs_human=True,controls=['continue','accept','pause','cancel'])
    data['state']['summary'].update(current='The required document needs review',
        pending_authorizations=[{'id':'bonus','summary':'Bonus source access','blocking':False}])
    open_task(ui)
    expect(ui.page.get_by_role('button',name='I handled this step — continue',exact=True)).to_be_visible()
    expect(ui.page.get_by_role('button',name='End mission & take over',exact=True)).to_be_visible()
    expect(ui.page.get_by_role('button',name='I handled this requirement',exact=True)).to_be_visible()


def test_cancel_has_one_action_and_keeps_audit_history(ui):
    data=stage(ui); posts=[]; dialogs=[]
    def cancel(route):
        posts.append(route.request.post_data_json)
        data['state'].update(state='cancelled',controls=[])
        route.fulfill(content_type='application/json',body=json.dumps(data['state']))
    ui.page.route('**/api/mission/cancel*',cancel)
    ui.page.on('dialog',lambda dialog: (dialogs.append(dialog.message),dialog.dismiss()))
    open_task(ui)
    ui.page.get_by_role('button',name='Cancel',exact=True).click()
    expect(ui.page.locator('.state-cancelled')).to_be_visible()
    assert posts==[{'id':'mission-ui'}] and not dialogs


def test_late_language_settings_translate_a_paused_task_without_resetting_draft(ui):
    data=stage(ui)
    data['state'].update(state='paused',controls=['resume','cancel'])
    held=[]
    ui.page.route('**/api/settings',lambda route:held.append(route))
    ui.page.reload(wait_until='load')
    open_task(ui)
    field=ui.page.locator('.mission-updates textarea')
    field.fill('Keep this unsent instruction')
    assert held
    for route in held:
        route.fulfill(content_type='application/json',body=json.dumps({'values':{'LANG':'zh'}}))
    expect(ui.page.get_by_role('button',name='继续',exact=True)).to_be_visible()
    expect(ui.page.locator('.mission-updates h3')).to_have_text('补充这项任务')
    expect(ui.page.locator('.mission-updates button')).to_have_text('保存补充说明')
    expect(field).to_have_value('Keep this unsent instruction')
