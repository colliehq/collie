"""Quota must not automatically restart an undelivered failed entry with a fresh budget."""
import time
from types import SimpleNamespace
import pytest
from harness import quota_resume, session_owner, sessions, task_inbox, web_tasks
from test_web_execution_manager import _Result, _accept, lab


@pytest.mark.parametrize('has_later_request', [False, True])
def test_quota_settlement_does_not_schedule_its_own_undelivered_request(lab, has_later_request):
    sid='quota-before-delivery'
    sessions.save(sid, [], project='web', cwd=str(lab.state))
    original=_accept(lab,sid,'failed-request','original accepted request')
    if has_later_request:
        _accept(lab,sid,'later-request','do this after the original')
    lease=session_owner.try_acquire(sid,label='parent-settlement-test')
    assert lease is not None
    try:
        entry=web_tasks.claim_next(sid,lease,('follow_up',))
        assert entry['id']==original['id']
        result=_Result(answer='',error='rate limit reached',success=False,
                       retry_at=int(time.time())+120)
        outcome=web_tasks.terminal_outcome(result)
        assert outcome['provider_wait'] is True
        handler=SimpleNamespace(_stream_outcome=outcome,_input_handed=[])
        assert web_tasks._settle_and_schedule(handler,sid,lease,entry) is False
        restored=task_inbox.get(sid,entry['id'])
        assert restored['state']=='pending'
        assert restored['config']==original['config']
        assert quota_resume.read(sid) is None
        assert quota_resume.tick(now=time.time()+300)==[]
        assert lab.calls==[]
        if has_later_request:
            assert task_inbox.get(sid,'later-request')['state']=='pending'
    finally:
        lease.release()
