"""Parent regression: the user-facing omission history outlives a model projection."""
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from harness import webapp
from harness.actions import ActionStore
from harness.jobs import DONE_VERIFIED, WAITING
from harness.mission import MissionDriver, MissionStore, create_mission, world_leash
from harness.verifier import Observation, Verdict, VERIFIED


@pytest.fixture
def completed_omission_history(tmp_path, monkeypatch):
    monkeypatch.setenv('COLLIE_STATE_DIR', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    artifact = tmp_path / 'main-report.txt'
    content = 'The owned main report is present before the optional-route decisions.\n'
    artifact.write_text(content, encoding='utf-8')
    store = MissionStore(str(tmp_path / 'jobs.db'))
    actions = ActionStore(str(tmp_path / 'actions.db'))
    decisions = [{'action': 'skip_optional', 'args': {
        'summary': 'Optional bonus route %02d' % i, 'branch': 'bonus-%02d' % i,
        'domain': 'bonus-%02d.example' % i, 'optional': True},
        'reason': 'Unavailable optional route; preserve the existing main report.'} for i in range(45)]
    decisions.extend([decisions[0], {'action': 'done'}])
    sequence = iter(decisions)
    calls = []

    def decide(*_):
        step = next(sequence)
        calls.append(step)
        return step

    def verify(*_):
        assert artifact.read_text(encoding='utf-8') == content
        return Verdict(VERIFIED, 'owned fixture report checked', (
            Observation('fixture-read', time.time(), True, asserted=True, detail='preexisting report'),))

    try:
        create_mission(store, 'omission-history', 'Keep the main report. Bonus routes are optional.',
                       case={}, leash=world_leash(max_model_calls=100, max_total_steps=100))
        driver = MissionDriver(store, actions, decide, [], goal_verifier=verify)
        assert driver.advance('omission-history') == WAITING
        assert len(calls) == 40, 'The cooperative planning slice remains bounded'
        until = time.monotonic() + 12
        second = WAITING
        while second == WAITING and time.monotonic() < until:
            second = driver.wake('omission-history', force=False)
            if second == WAITING:
                time.sleep(.05)
        assert second == DONE_VERIFIED
        assert len(calls) == 47
        yield store, actions
    finally:
        store.close()
        actions.close()


def test_old_identical_optional_skip_is_not_recorded_again_after_projection_rolls(completed_omission_history):
    store, _ = completed_omission_history
    events = [row for row in store.events('omission-history', 1000)
              if row['kind'] == 'intervention' and row['name'] == 'optional_skipped']
    assert len(events) == 45, 'Exactly the45 distinct optional decisions belong in the journal'
    assert sum(row['payload'].get('summary') == 'Optional bonus route 00' for row in events) == 1


def test_public_http_report_includes_the45_short_optional_omissions(completed_omission_history):
    _store, _actions = completed_omission_history
    server = ThreadingHTTPServer(('127.0.0.1', 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = 'http://127.0.0.1:%d/api/mission/report?id=omission-history&token=%s' % (
            server.server_address[1], webapp.TOKEN)
        with urllib.request.urlopen(url, timeout=8) as response:
            assert response.status == 200
            report = json.loads(response.read())
        expected = {'Optional bonus route %02d' % i for i in range(45)}
        assert {row['summary'] for row in report['skipped_steps']} == expected
        assert all(summary in report['markdown'] for summary in expected)
        assert 'case' not in report, 'Complete omissions do not expose the internal Mission case'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_optional_omissions_keep_the_case_bounded_and_do_not_request_permission(completed_omission_history):
    store, actions = completed_omission_history
    mission = store.get('omission-history')
    assert len(mission.case.get('skipped_steps', [])) <= 40
    assert not mission.case.get('pending_authorizations')
    assert not actions.receipts()
