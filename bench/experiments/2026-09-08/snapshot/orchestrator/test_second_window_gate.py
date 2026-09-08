"""A reset window can start without an expiry, but unknown/paid usage cannot."""
import datetime as dt

import pytest
import run_second_window as controller

PLAN = {'not_before_utc': '2026-09-08T05:30:05Z'}


@pytest.fixture
def now(monkeypatch):
    actual_datetime = dt.datetime
    class FixedTime(actual_datetime):
        @classmethod
        def now(cls, timezone=None):
            return actual_datetime(2026, 9, 8, 5, 31, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(controller.dt, 'datetime', FixedTime)


@pytest.mark.parametrize('used,reset', [
    (0, None), (0, '2026-09-08T05:29:59Z'), (0, '2026-09-08T10:30:00Z'),
    (13, '2026-09-08T10:30:00Z'),
])
def test_confirmed_unused_or_active_second_window_is_accepted(now, monkeypatch, used, reset):
    receipt = {'ok': True, 'extra_usage': {'is_enabled': False},
               'five_hour': {'utilization': used, 'resets_at': reset}}
    monkeypatch.setattr(controller, 'snapshot', lambda: receipt)
    assert controller.gate(PLAN) is receipt


@pytest.mark.parametrize('receipt', [
    {'ok': False, 'http_status': 401},
    {'ok': True, 'extra_usage': {'is_enabled': True}, 'five_hour': {'utilization': 0}},
    {'ok': True, 'extra_usage': {}, 'five_hour': {'utilization': 0}},
    *[{'ok': True, 'extra_usage': {'is_enabled': False}, 'five_hour': window} for window in [
        {}, {'utilization': True}, {'utilization': 100},
        {'utilization': 5, 'resets_at': None},
        {'utilization': 5, 'resets_at': '2026-09-08T05:29:59Z'},
        {'utilization': 5, 'resets_at': '2026-09-08T15:30:00Z'},
    ]],
])
def test_unknown_exhausted_paid_or_later_windows_do_not_launch(now, monkeypatch, receipt):
    monkeypatch.setattr(controller, 'snapshot', lambda: receipt)
    with pytest.raises(SystemExit):
        controller.gate(PLAN)


def test_before_the_registration_even_the_usage_query_is_not_sent(now, monkeypatch):
    monkeypatch.setattr(controller, 'snapshot', lambda: pytest.fail('quota queried too early'))
    with pytest.raises(SystemExit):
        controller.gate({'not_before_utc': '2026-09-08T06:00:00Z'})


@pytest.mark.parametrize('outcome,passed', [('completed', True), (None, False), ('failed', False)])
def test_transport_smoke_requires_all_request_reservations_to_be_settled(outcome, passed):
    receipt = {'passed': True, 'turns': [{'request_count': 1, 'stop_reason': 'end_turn'} for _ in range(3)],
               'reservations': [{'outcome': 'completed'}, {'outcome': 'completed'}, {'outcome': outcome}]}
    assert controller.smoke_passed(receipt) is passed
