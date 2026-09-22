"""The open-inclusive listing: what it adds, what it must not change.

The defect these lock is narrow — a conversation whose retained history fills
the default list limit stopped showing the request it was waiting on — and so
is the fix.  ``list_entries(..., include_open=True)`` adds back the open rows
the cut dropped; everything else about the listing (its default, its order, its
state filter, its bound, the public shape of a row, the store itself) is
supposed to be exactly what it was.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import session_owner, task_inbox, web_tasks              # noqa: E402
from test_web_task_inbox import CONFIG, _get, _post, web              # noqa: E402,F401


def _full_history(sid):
    """Terminal entries up to the retention cap, through the real accept path."""
    for index in range(task_inbox.MAX_TERMINAL_RETAINED):
        key = 'done-' + str(index)
        task_inbox.enqueue(sid, key, 'Older instruction ' + str(index), mode='follow_up')
        task_inbox.cancel(sid, key, reason='finished before the new request')


def _accept(base, token, sid, key):
    code, response = _post(base, token, '/api/task-inbox', {
        'session': sid, 'id': key, 'text': 'New instruction ' + key,
        'mode': 'follow_up', 'config': CONFIG})
    assert code == 200 and response['accepted'] is True
    return response['entry']


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store on disk of this test's own, never the caller's state directory."""
    monkeypatch.setenv('COLLIE_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('COLLIE_SESSIONS_DIR', str(tmp_path / 'state' / 'sessions'))
    return tmp_path


# ------------------------------------------------------------------ store level

def test_default_listing_is_byte_for_byte_what_it_was_before_the_option(store):
    sid = 'bound-default'
    _full_history(sid)
    task_inbox.enqueue(sid, 'waiting', 'Still queued', mode='follow_up')

    default = task_inbox.list_entries(sid)
    assert len(default) == 200, 'the default limit itself must not move'
    assert {row['state'] for row in default} == {'canceled'}

    inclusive = task_inbox.list_entries(sid, include_open=True)
    assert inclusive[:200] == default, 'history must not be dropped to make room'
    assert [row['id'] for row in inclusive[200:]] == ['waiting']
    assert [row['seq'] for row in inclusive] == sorted(row['seq'] for row in inclusive)


def test_open_inclusive_listing_is_bounded_by_the_existing_store_caps(store):
    sid = 'bound-cap'
    _full_history(sid)
    for index in range(task_inbox.MAX_PENDING):
        task_inbox.enqueue(sid, 'waiting-' + str(index), 'Queued ' + str(index),
                           mode='follow_up')

    rows = task_inbox.list_entries(sid, include_open=True)
    assert len(rows) == 200 + task_inbox.MAX_PENDING
    assert len(rows) <= task_inbox.MAX_TERMINAL_RETAINED + task_inbox.MAX_PENDING, \
        'the listing must stay bounded by the caps the store already enforces'
    assert [row['id'] for row in rows if row['state'] in task_inbox.OPEN_STATES] == \
        ['waiting-' + str(index) for index in range(task_inbox.MAX_PENDING)]
    # A smaller page of history still carries every open row, and only those.
    small = task_inbox.list_entries(sid, limit=5, include_open=True)
    assert len(small) == 5 + task_inbox.MAX_PENDING
    assert [row['id'] for row in small[:5]] == ['done-' + str(i) for i in range(5)]


def test_state_and_mode_filters_are_not_widened_by_include_open(store):
    sid = 'bound-filter'
    _full_history(sid)
    task_inbox.enqueue(sid, 'waiting', 'Still queued', mode='follow_up')
    task_inbox.enqueue(sid, 'waiting-steer', 'Steer me', mode='steer')

    history = task_inbox.list_entries(sid, states=('canceled',), limit=7,
                                      include_open=True)
    assert len(history) == 7 and {row['state'] for row in history} == {'canceled'}, \
        'asking for history only must never smuggle open entries back in'
    steer = task_inbox.list_entries(sid, states=task_inbox.OPEN_STATES,
                                    modes=('steer',), limit=0, include_open=True)
    assert [row['id'] for row in steer] == ['waiting-steer']


def test_listing_does_not_write_to_the_store(store):
    sid = 'bound-readonly'
    _full_history(sid)
    task_inbox.enqueue(sid, 'waiting', 'Still queued', mode='follow_up')
    path = task_inbox.store_path(sid)
    before, stamp = open(path, 'rb').read(), os.stat(path).st_mtime_ns

    assert task_inbox.list_entries(sid, include_open=True)
    assert open(path, 'rb').read() == before and os.stat(path).st_mtime_ns == stamp


# -------------------------------------------------------------------- over HTTP

def test_http_listing_keeps_public_shape_and_order_for_the_open_rows(web):
    base, token, _ = web
    sid = 'bound-http-shape'
    _full_history(sid)
    receipt = _accept(base, token, sid, 'waiting')
    code, response = _get(base, token, '/api/task-inbox?session=' + sid)
    assert code == 200

    ids = [row['id'] for row in response['entries']]
    assert ids[:200] == ['done-' + str(index) for index in range(200)]
    assert ids[200:] == ['waiting'], 'the recovered row keeps its acceptance position'
    row = response['entries'][-1]
    assert row == web_tasks.public_entry(task_inbox.get(sid, 'waiting')), \
        'the added row is sanitized by exactly the same public projection'
    assert 'metadata' not in row and row['digest'] == receipt['digest']
    # Storage is untouched by reading it: history, identity and counts all stand.
    assert task_inbox.status(sid)['counts'] == dict(
        {state: 0 for state in task_inbox.STATES}, canceled=200, pending=1)


def test_a_live_claim_after_full_history_is_not_recovered_by_the_listing(web):
    base, token, _ = web
    sid = 'bound-http-owned'
    _full_history(sid)
    _accept(base, token, sid, 'in-flight')
    with session_owner.acquire(sid, label='open-listing-bound-test') as lease:
        assert task_inbox.claim(sid, lease, modes=('follow_up',), limit=1)
        code, response = _get(base, token, '/api/task-inbox?session=' + sid)
    assert code == 200 and response['owner_busy'] is True
    open_rows = {row['id']: row['state'] for row in response['entries']
                 if row['state'] in task_inbox.OPEN_STATES}
    assert open_rows == {'in-flight': 'claimed'}, \
        'a claim held by a live run stays claimed and stays visible'
    assert task_inbox.get(sid, 'in-flight')['state'] == 'claimed'


def test_the_queue_panel_would_render_the_waiting_request(web):
    """The UI half, mirrored from index.html rather than assumed.

    ``renderTaskQueue`` filters the response for pending/claimed and hides the
    panel when nothing is left, so an empty filtered array is the user-visible
    failure.  The two source lines it depends on are asserted here so this
    mirror cannot quietly drift from the page it stands in for.
    """
    page = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'harness', 'webui', 'index.html')
    source = open(page, encoding='utf-8').read()
    assert re.search(r'state\.entries\.filter\(function \(entry\) \{ return '
                     r'entry\.state === "pending" \|\| entry\.state === "claimed"; \}\)', source)
    assert 'panel.hidden = !retained.length && (!currentSession || (!entries.length' in source

    base, token, _ = web
    sid = 'bound-ui'
    _full_history(sid)
    _accept(base, token, sid, 'waiting')
    code, response = _get(base, token, '/api/task-inbox?session=' + sid)
    assert code == 200
    entries = [row for row in response['entries']
               if row['state'] == 'pending' or row['state'] == 'claimed']
    assert entries, 'an empty filtered array is exactly what hides the panel'
    assert [row['id'] for row in entries] == ['waiting']
    assert any(row['state'] == 'pending' for row in entries), '"Send next" stays offered'
