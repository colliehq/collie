"""The durable omission journal is authoritative without unbounding the case."""
import time

import pytest

from harness.actions import ActionStore
from harness.jobs import DONE_VERIFIED, NEEDS_YOU
from harness.mission import MissionDriver, MissionStore, create_mission, world_leash
from harness.missionweb import (MissionService, _mission_report, _omission_history)
from harness.verifier import Observation, Verdict, VERIFIED


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv('COLLIE_STATE_DIR', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    store = MissionStore(str(tmp_path/'jobs.db'))
    actions = ActionStore(str(tmp_path/'actions.db'))
    yield store, actions
    store.close(); actions.close()


def verified(*_):
    return Verdict(VERIFIED, 'main artifact independently checked', (
        Observation('local-report-check', time.time(), True, asserted=True,
                    detail='saved main report'),))


def run(lab, decisions, mission_id='m', case=None, goal_verifier=verified):
    store, actions = lab
    create_mission(store, mission_id, 'Save the main report; bonus routes are optional',
                   case=case or {}, leash=world_leash())
    sequence = iter(decisions)
    return MissionDriver(store, actions, lambda *_: next(sequence), [],
                         goal_verifier=goal_verifier).advance(mission_id)


def skip(index, summary=None):
    return {'action': 'skip_optional', 'args': {
        'summary': summary or 'Optional bonus route %02d' % index,
        'branch': 'bonus-%02d' % index, 'domain': 'bonus-%02d.example' % index,
        'optional': True}, 'reason': 'Unavailable optional route; keep the main report.'}


def journal(store, mission_id='m'):
    return [row for row in store.events(mission_id, 1000)
            if row['kind'] == 'intervention' and row['name'] == 'optional_skipped']


def report_for(store, mission, mid='m', limit=None):
    history = (_omission_history(store, mission) if limit is None
               else _omission_history(store, mission, limit=limit))
    return _mission_report(mission, {'title': 'Report'}, [], [],
                           store.runtime(mid), omissions=history)


def test_legacy_case_skips_without_a_journal_record_still_reach_the_report(lab):
    store, _ = lab
    legacy = [{'id': 'skip_legacy', 'summary': 'Legacy bonus export',
               'reason': 'Recorded before the omission journal existed',
               'branch': 'legacy'}]
    assert run(lab, [{'action': 'done'}], case={'skipped_steps': legacy}) == DONE_VERIFIED
    assert not journal(store), 'the fixture deliberately has no journal record'
    report = report_for(store, store.get('m'))
    assert [x['summary'] for x in report['skipped_steps']] == ['Legacy bonus export']
    assert report['skipped_steps_total'] == 1
    assert report['skipped_steps_truncated'] is False
    assert 'Legacy bonus export' in report['markdown']


def test_a_changed_requirement_is_a_new_omission_not_a_suppressed_repeat(lab):
    store, _ = lab
    changed = skip(0, summary='Optional bonus route 00 in high resolution')
    assert run(lab, [skip(0), changed, {'action': 'done'}]) == DONE_VERIFIED
    events = journal(store)
    assert len(events) == 2, 'a different request identity is a different omission'
    assert {row['payload']['summary'] for row in events} == {
        'Optional bonus route 00', 'Optional bonus route 00 in high resolution'}
    report = report_for(store, store.get('m'))
    assert len(report['skipped_steps']) == 2 and report['skipped_steps_total'] == 2


def test_an_earlier_optional_skip_does_not_release_a_required_branch(lab):
    store, _ = lab
    coverage = [{'branch': 'bonus-00', 'required': False, 'status': 'pending'},
                {'branch': 'invoice', 'required': True, 'status': 'pending'}]
    required = {'action': 'skip_optional', 'args': {
        'summary': 'Private invoice', 'branch': 'invoice', 'optional': True},
        'reason': 'No account access'}
    assert run(lab, [skip(0), required,
                     {'action': 'needs_human', 'args': {'summary': 'Invoice is required'}}],
               case={'_campaign_coverage': coverage}) == NEEDS_YOU
    assert [row['payload']['branch'] for row in journal(store)] == ['bonus-00']
    case = store.get('m').case
    assert case['_campaign_coverage'][1]['status'] == 'pending'
    assert not case.get('resolved_authorizations')


def test_one_missions_omission_history_never_answers_for_another(lab):
    store, actions = lab
    assert run(lab, [skip(0), {'action': 'done'}], mission_id='first') == DONE_VERIFIED
    assert run(lab, [skip(0), {'action': 'done'}], mission_id='second') == DONE_VERIFIED
    assert len(journal(store, 'first')) == 1
    assert len(journal(store, 'second')) == 1, 'the other Mission is not our history'
    assert not actions.receipts()
    assert [x['summary'] for x in report_for(store, store.get('second'), 'second')
            ['skipped_steps']] == ['Optional bonus route 00']


def test_a_repeated_old_skip_is_not_recorded_twice_after_the_projection_rolls(lab):
    store, _ = lab
    assert run(lab, [skip(0), skip(1), skip(0), {'action': 'done'}]) == DONE_VERIFIED
    assert len(journal(store)) == 2
    case = store.get('m').case
    assert len(case['skipped_steps']) == 2 and not case.get('pending_authorizations')
    steps = [s for s in store.steps('m') if s['nonce'].startswith('skip_')]
    assert len(steps) == 2, 'the repeat is not executed as a new step'


def test_a_truncated_report_says_so_and_the_rest_stays_reachable(lab):
    store, _ = lab
    assert run(lab, [skip(i) for i in range(5)] + [{'action': 'done'}]) == DONE_VERIFIED
    report = report_for(store, store.get('m'), limit=3)
    assert report['skipped_steps_total'] == 5
    assert report['skipped_steps_shown'] == 3
    assert report['skipped_steps_truncated'] is True
    assert [x['summary'] for x in report['skipped_steps']] == [
        'Optional bonus route %02d' % i for i in (2, 3, 4)]
    assert 'Showing the 3 most recent of 5 recorded omissions' in report['markdown']
    service = MissionService()
    try:
        page = service.omissions('m', limit=3, offset=0)
    finally:
        service.close()
    assert page['total'] == 5
    assert [x['summary'] for x in page['skipped_steps']] == [
        'Optional bonus route %02d' % i for i in (0, 1, 2)]


def test_a_complete_report_lists_every_omission_and_states_the_count(lab):
    store, _ = lab
    assert run(lab, [skip(i) for i in range(5)] + [{'action': 'done'}]) == DONE_VERIFIED
    report = report_for(store, store.get('m'))
    assert report['skipped_steps_truncated'] is False
    assert report['skipped_steps_total'] == 5
    assert 'All 5 recorded omissions are listed.' in report['markdown']
    assert 'case' not in report
