"""The person's exact acknowledgement crosses HTTP, durable scope, and resume."""
from harness.actions import ActionStore
from harness.jobs import NEEDS_YOU, QUEUED, RUNNING
from harness.mission import MissionStore, MissionDriver, create_mission, world_leash
from harness.missionweb import MissionService
from test_mission_notes_api import _Server, _request


def test_handled_requirement_is_authenticated_exact_and_idempotent(tmp_path,monkeypatch):
    monkeypatch.setenv('COLLIE_STATE_DIR',str(tmp_path))
    monkeypatch.setattr('harness.settings.apply',lambda: None)
    store=MissionStore(str(tmp_path/'jobs.db'));actions=ActionStore(str(tmp_path/'actions.db'))
    create_mission(store,'ack-http','Prepare the report from the private document',leash=world_leash())
    driver=MissionDriver(store,actions,lambda *_:{'action':'needs_authorization','args':{
        'summary':'Sign in to the private source','kind':'security_key','domain':'private.example','blocking':True}},[])
    assert driver.advance('ack-http')==NEEDS_YOU
    request=store.get('ack-http').case['pending_authorizations'][0]
    try:
        with _Server() as server:
            endpoint=server.root+'/api/mission/authorization'
            body={'id':'ack-http','request_id':request['id'],'decision':'handled'}
            assert _request(endpoint,'POST',body)[0]==403
            assert _request(endpoint+server.token,'POST',dict(body,decision='allow_everything'))[0]==400
            assert _request(endpoint+server.token,'POST',dict(body,request_id='wrong'))[0]==409
            code,out=_request(endpoint+server.token,'POST',body)
            assert code==200 and out['acknowledged'] and out['state']==QUEUED
            assert out['summary']['pending_authorizations']==[]
            after=store.get('ack-http').case
            code,out=_request(endpoint+server.token,'POST',body)
            assert code==200 and out['duplicate']
            assert store.get('ack-http').case==after
            assert not actions.receipts()
    finally:
        store.close();actions.close()


def test_acknowledgement_cannot_race_a_live_owner(tmp_path):
    store=MissionStore(str(tmp_path/'missions.db'))
    create_mission(store,'owned','Work',case={'pending_authorizations':[{'id':'one','summary':'Requirement'}]},leash=world_leash())
    token=store.claim_run('owned')
    assert token and store.get('owned').state==RUNNING
    try:
        assert not store.acknowledge_authorization('owned','one')['ok']
        assert store.owns_run('owned',token)
        assert not store.db.in_transaction
    finally:store.close()
