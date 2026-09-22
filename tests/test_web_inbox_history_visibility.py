"""An old queue history must not hide accepted work that is still waiting."""
from harness import session_owner, task_inbox
from test_web_task_inbox import CONFIG, _get, _post, web


def _history(sid):
    for index in range(task_inbox.MAX_TERMINAL_RETAINED):
        key = 'finished-' + str(index)
        task_inbox.enqueue(sid, key, 'Older instruction ' + str(index), mode='follow_up')
        task_inbox.cancel(sid, key, reason='Historical fixture completed before the new request')


def _accept(base, token, sid, key):
    code, response = _post(base, token, '/api/task-inbox', {
        'session': sid, 'id': key, 'text': 'New instruction ' + key,
        'mode': 'follow_up', 'config': CONFIG})
    assert code == 200 and response['accepted'] is True
    return response['entry']


def test_pending_request_is_visible_after_terminal_history_reaches_retention_limit(web):
    base, token, _ = web
    sid = 'history-visible-pending'
    _history(sid)
    receipt = _accept(base, token, sid, 'new-task')
    code, response = _get(base, token, '/api/task-inbox?session=' + sid)
    assert code == 200
    rows = {row['id']: row for row in response['entries'] if row['state'] in task_inbox.OPEN_STATES}
    assert set(rows) == {'new-task'}, 'The UI listing must contain the accepted pending request'
    assert rows['new-task']['state'] == 'pending'
    assert rows['new-task']['digest'] == receipt['digest']


def test_live_claim_and_later_pending_request_are_both_visible_after_history(web):
    base, token, _ = web
    sid = 'history-visible-claimed'
    _history(sid)
    _accept(base, token, sid, 'in-flight')
    with session_owner.acquire(sid, label='history-visibility-test') as lease:
        assert task_inbox.claim(sid, lease, modes=('follow_up',), limit=1)[0]['id'] == 'in-flight'
        _accept(base, token, sid, 'waiting-next')
        code, response = _get(base, token, '/api/task-inbox?session=' + sid)
        assert code == 200 and response['owner_busy'] is True
        rows = {row['id']: row['state'] for row in response['entries'] if row['state'] in task_inbox.OPEN_STATES}
        assert rows == {'in-flight': 'claimed', 'waiting-next': 'pending'}


def test_every_request_at_the_actual_pending_cap_is_visible_after_full_history(web):
    base, token, _ = web
    sid = 'history-visible-full-inbox'
    _history(sid)
    expected = ['pending-' + str(index) for index in range(task_inbox.MAX_PENDING)]
    for key in expected:
        _accept(base, token, sid, key)
    code, response = _get(base, token, '/api/task-inbox?session=' + sid)
    assert code == 200
    rows = [row for row in response['entries'] if row['state'] in task_inbox.OPEN_STATES]
    assert [row['id'] for row in rows] == expected, 'Retained history must never consume the pending listing capacity'


def test_listing_does_not_discard_completed_history_or_change_request_identity(web):
    base, token, _ = web
    sid = 'history-visible-preserved'
    _history(sid)
    receipt = _accept(base, token, sid, 'keep-new')
    limit = task_inbox.MAX_TERMINAL_RETAINED + task_inbox.MAX_PENDING
    before = task_inbox.list_entries(sid, limit=limit)
    code, _ = _get(base, token, '/api/task-inbox?session=' + sid)
    assert code == 200
    assert task_inbox.list_entries(sid, limit=limit) == before
    assert task_inbox.get(sid, 'keep-new')['digest'] == receipt['digest']
    assert task_inbox.status(sid)['counts']['canceled'] == task_inbox.MAX_TERMINAL_RETAINED
