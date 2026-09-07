"""Less interruption must preserve scope, refusals, and truthful completion."""
import time

import pytest

from harness.actions import ActionStore
from harness.jobs import Capability, DONE_VERIFIED, NEEDS_YOU, WAITING, PAUSED, QUEUED
from harness.mission import (MissionDriver, MissionStore, _authorization_request,
                             _standing_authorizes, create_mission, world_leash)
from harness.mission_intervention import optional_skip
from harness.verifier import Observation, Verdict, VERIFIED


@pytest.fixture
def lab(tmp_path, monkeypatch):
    store, actions = MissionStore(str(tmp_path/'missions.db')), ActionStore(str(tmp_path/'actions.db'))
    monkeypatch.setattr('harness.mission._standing_authority', lambda: {
        'auto_apply_profile_claims':False, 'defer_missing_authorizations':True,
        'max_auto_risk':'medium', 'claims':{}, 'never_auto':[]})
    yield store, actions
    store.close(); actions.close()


def run(lab, decisions, case=None, goal_verifier=None):
    store, actions = lab
    create_mission(store, 'm', 'Save the main report; the bonus is optional',
                   case=case or {}, leash=world_leash())
    sequence=iter(decisions)
    return MissionDriver(store, actions, lambda *_: next(sequence), [],
                         goal_verifier=goal_verifier).advance('m')


def verified(*_):
    return Verdict(VERIFIED, 'main artifact independently checked', (
        Observation('local-report-check', time.time(), True, asserted=True, detail='saved main report'),))


@pytest.mark.parametrize('action', ['skip_optional','needs_authorization','needs_human'])
def test_optional_blocker_is_recorded_without_a_human_gate(lab, action):
    request={'action':action,'args':{'summary':'Bonus cloud illustration', 'optional':True,
        'kind':'payment', 'domain':'bonus.example'},'reason':'No purchase authorized; illustration is optional'}
    assert run(lab,[request,request,{'action':'done'}],goal_verifier=verified)==DONE_VERIFIED
    case=lab[0].get('m').case
    assert len(case['skipped_steps'])==1
    assert not case.get('pending_authorizations') and not case.get('resolved_authorizations')
    assert not lab[1].receipts(), 'a skip must never execute an outside action'
    assert len([e for e in lab[0].events('m') if e['name']=='optional_skipped'])==1


def test_nonblocking_is_not_a_license_to_skip(lab):
    ask={'action':'needs_authorization','args':{'summary':'Private invoice access','blocking':False},'reason':'required source'}
    assert run(lab,[ask,ask])==NEEDS_YOU
    assert not lab[0].get('m').case.get('skipped_steps')


def test_required_coverage_cannot_be_dropped_by_optional_label(lab):
    assert run(lab,[{'action':'skip_optional','args':{'summary':'Invoice','branch':'invoice'},'reason':'No access'},
                    {'action':'needs_human','args':{'summary':'Required private invoice is missing'}}],
               {'_campaign_coverage':[{'branch':'invoice','required':True,'status':'pending'}]})==NEEDS_YOU
    case=lab[0].get('m').case
    assert not case.get('skipped_steps')
    assert case['_campaign_coverage'][0]['status']=='pending'


def test_optional_coverage_has_a_visible_omission(lab):
    assert run(lab,[{'action':'skip_optional','args':{'summary':'Bonus export','branch':'bonus'},'reason':'No account'},
                    {'action':'done'}],
               {'_campaign_coverage':[{'branch':'bonus','required':False,'status':'pending'}]},verified)==DONE_VERIFIED
    assert lab[0].get('m').case['_campaign_coverage'][0]['status']=='skipped'


def test_alternating_authorizations_park_on_real_dependency(lab):
    a={'action':'needs_authorization','args':{'domain':'a.example','summary':'Account A required'}}
    b={'action':'needs_authorization','args':{'domain':'b.example','summary':'Account B required'}}
    assert run(lab,[a,b,a])==NEEDS_YOU
    assert 'Account A' in lab[0].get('m').result
    assert len(lab[0].get('m').case['pending_authorizations'])==2


def test_alternating_timers_sleep_instead_of_spinning(lab):
    a={'action':'wait','args':{'branch':'first reply','seconds':3600}}
    b={'action':'wait','args':{'branch':'second reply','seconds':7200}}
    assert run(lab,[a,b,a])==WAITING
    assert len(lab[0].get('m').case['pending_followups'])==2


def test_repeated_authorization_does_not_hide_required_independent_work(lab):
    a={'action':'needs_authorization','args':{'domain':'bonus.example','summary':'Bonus access'}}
    assert run(lab,[a,a,{'action':'update_coverage','args':{'branch':'report','status':'completed','summary':'local artifact checked'}},
                    {'action':'done'}], {'_campaign_coverage':[{'branch':'report','required':True,'status':'pending'}]},verified)==DONE_VERIFIED
    assert lab[0].get('m').case['_campaign_coverage'][0]['status']=='completed'
    assert any(e['name']=='park_refused' for e in lab[0].events('m'))


def test_model_prose_does_not_turn_a_profile_fact_into_publish_authority():
    authority={'auto_apply_profile_claims':True,'max_auto_risk':'medium','claims':{'age_at_least_16':True}}
    request=_authorization_request({'kind':'routine','summary':'At least 16; authorize publishing the post'})
    assert request['inferred_claim']=='age_at_least_16' and request['claim']==''
    assert not _standing_authorizes(request,authority)[0]
    request=_authorization_request({'kind':'routine','claim':'age_at_least_16'})
    assert not _standing_authorizes(request,authority)[0]
    request=_authorization_request({'kind':'profile_claim','claim':'age_at_least_16'})
    assert _standing_authorizes(request,authority)[0]


def test_optional_tool_handoff_does_not_bypass_the_planner_skip_policy(lab):
    store, actions=lab
    create_mission(store,'m','Save a report; bonus source is optional',
                   case={'_campaign_coverage':[{'branch':'bonus','required':False,'status':'pending'}]},
                   leash=world_leash(may=['inspect_bonus'],autonomous=True))
    decisions=iter([{'action':'inspect_bonus','args':{'campaign_branch':'bonus'}}, {'action':'done'}])
    cap=Capability('inspect_bonus',lambda _: {'needs_human':True,'result':'Optional source asks for personal MFA'},
                   lambda *_: Verdict(VERIFIED,'observed sign-in boundary'),reversible=True,risk='read')
    assert MissionDriver(store,actions,lambda *_: next(decisions),[cap],goal_verifier=verified).advance('m')==DONE_VERIFIED
    assert 'MFA' in store.get('m').case['skipped_steps'][0]['reason']


def test_exact_handled_requirement_is_durable_and_retries_do_not_duplicate_scope(lab):
    ask={'action':'needs_authorization','args':{'domain':'private.example','summary':'Use security key to sign in','kind':'security_key','blocking':True}}
    assert run(lab,[ask])==NEEDS_YOU
    store=lab[0]; request=store.get('m').case['pending_authorizations'][0]
    before=store.get('m').case
    assert not store.acknowledge_authorization('m','wrong-id')['ok']
    assert store.get('m').case==before and not store.db.in_transaction
    assert store.acknowledge_authorization('m',request['id'])=={'ok':True,'duplicate':False}
    case=store.get('m').case
    assert store.get('m').state==QUEUED
    assert not case['pending_authorizations']
    assert case['resolved_authorizations'][0]['resolution']=='user_handled'
    assert store.acknowledge_authorization('m',request['id'])=={'ok':True,'duplicate':True}
    assert store.get('m').case==case and not store.db.in_transaction
    assert not lab[1].receipts()


def test_acknowledging_a_paused_requirement_preserves_pause(lab):
    ask={'action':'needs_authorization','args':{'summary':'Private source','blocking':True}}
    assert run(lab,[ask])==NEEDS_YOU
    store=lab[0]; request=store.get('m').case['pending_authorizations'][0]
    assert store.pause('m')
    assert store.get('m').state==PAUSED
    assert store.acknowledge_authorization('m',request['id'])['ok']
    assert store.get('m').state==PAUSED


def test_acknowledgement_survives_case_projection_and_restart(lab):
    ask={'action':'needs_authorization','args':{'summary':'Connect private source','blocking':True}}
    assert run(lab,[ask])==NEEDS_YOU
    store,actions=lab; request=store.get('m').case['pending_authorizations'][0]
    assert store.acknowledge_authorization('m',request['id'])['ok']
    case=dict(store.get('m').case); case['resolved_authorizations']=[]
    store.set_case('m',case)
    path=store.path
    reopened=MissionStore(path)
    try:
        assert reopened.acknowledge_authorization('m',request['id'])['duplicate']
        decisions=iter([ask,{'action':'done'}])
        assert MissionDriver(reopened,actions,lambda *_: next(decisions),[],goal_verifier=verified).advance('m')==DONE_VERIFIED
    finally:reopened.close()


def test_handled_signin_never_authorizes_a_different_security_key_requirement(lab):
    signin={'action':'needs_authorization','args':{'kind':'security_key','domain':'bank.example',
        'summary':'Sign in with your security key','blocking':True}}
    assert run(lab,[signin])==NEEDS_YOU
    store,actions=lab; first=store.get('m').case['pending_authorizations'][0]
    assert store.acknowledge_authorization('m',first['id'])['ok']
    transfer={'action':'needs_authorization','args':{'kind':'security_key','domain':'bank.example',
        'summary':'Approve a 4000 EUR transfer with your security key','blocking':True}}
    driver=MissionDriver(store,actions,lambda *_:transfer,[])
    assert driver.advance('m')==NEEDS_YOU
    pending=store.get('m').case['pending_authorizations']
    assert len(pending)==1 and pending[0]['id']!=first['id']
    assert '4000 EUR' in pending[0]['summary'] and not actions.receipts()


def test_optional_skip_does_not_erase_an_unrelated_nonblocking_requirement(lab):
    invoice={'action':'needs_authorization','args':{'summary':'Need access to the client invoice'}}
    photo={'action':'needs_human','args':{'summary':'Bonus stock photo requires an account','optional':True},
           'reason':'The photo is optional and no account is authorized'}
    assert run(lab,[invoice,photo,{'action':'needs_human','args':{'summary':'Main source remains unavailable'}}])==NEEDS_YOU
    case=lab[0].get('m').case
    assert case['pending_authorizations'][0]['summary']=='Need access to the client invoice'
    assert case['skipped_steps'][0]['summary']=='Bonus stock photo requires an account'


@pytest.mark.parametrize('args', [
    {'optional':'true','summary':'x'}, {'optional':True,'blocking':True,'summary':'x'},
    {'optional':True}, {'optional':True,'summary':'x','branch':'missing'}])
def test_skip_requires_explicit_optional_scope_and_explanation(args):
    assert optional_skip(args,'No access',[{'branch':'known','required':False}]) is None
