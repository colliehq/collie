from copy import deepcopy

import pytest

from session_receipts import summarize

RECEIPT = [{'session_enabled': True, 'structured_output': False, 'had_session': True,
            'close_reported': True, 'session_retired': True,
            'unresolved_workers': 0, 'active_registrations': 0,
            'evidence': {'sessions_opened': 1, 'session_turns': 7, 'stateless_turns': 0,
                         'longest_session_turns': 7, 'resets': {'benchmark_finished': 1},
                         'fallbacks': {}, 'unresolved_cleanups': 0, 'cleanup_retries_confirmed': 0}}]


def test_a_correct_patch_cannot_erase_unresolved_worker_ownership():
    receipt = deepcopy(RECEIPT)
    receipt[0]['unresolved_workers'] = 1
    result = summarize(receipt, True)
    assert result['valid'] and not result['clean']
    assert result['failure'] == 'session_cleanup_unresolved'


def test_failed_initial_close_with_confirmed_cleanup_retains_its_history():
    receipt = deepcopy(RECEIPT)
    receipt[0]['close_reported'] = False
    receipt[0]['evidence'].update(unresolved_cleanups=1, cleanup_retries_confirmed=1)
    result = summarize(receipt, True)
    assert result['clean'] and result['totals']['cleanup_retries_confirmed'] == 1


@pytest.mark.parametrize('bad', [[], None, [{}], [{'session_enabled': True}],
                               [dict(RECEIPT[0], unresolved_workers=False)]])
def test_missing_or_malformed_proof_is_not_a_clean_trial(bad):
    assert not summarize(bad, True)['clean']


def test_stateless_control_does_not_require_a_session():
    receipt = deepcopy(RECEIPT)
    receipt[0].update(session_enabled=False, had_session=False, close_reported=False)
    receipt[0]['evidence'].update(sessions_opened=0, session_turns=0,
                                 longest_session_turns=0, stateless_turns=7, resets={})
    assert summarize(receipt, False)['clean']
    assert not summarize(receipt, True)['clean']


def test_treatment_with_only_fallback_calls_did_not_exercise_persistence():
    receipt = deepcopy(RECEIPT)
    receipt[0]['evidence'].update(sessions_opened=0, session_turns=0,
                                 longest_session_turns=0, stateless_turns=7)
    assert summarize(receipt, True)['failure'] == 'session_not_exercised'
