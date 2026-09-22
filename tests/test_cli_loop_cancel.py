import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from harness import cli
from harness.recorder import RunResult


def test_loop_stop_cancels_real_goal_check_and_never_starts_another_iteration(monkeypatch, tmp_path):
    ready, late = tmp_path.parent/(tmp_path.name+'-ready'), tmp_path.parent/(tmp_path.name+'-late')
    (tmp_path/'check.py').write_text(
        "from pathlib import Path\nimport time\nPath(%r).write_text('ready')\n"
        "print('goal check running',flush=True)\ntime.sleep(3)\n"
        "Path(%r).write_text('late')\n" % (str(ready),str(late)), 'utf-8')
    stopped=threading.Event(); runs=[]; receipts=[]
    result=RunResult(answer='work finished',stop_reason='completed')
    def run(*args,**kwargs):
        runs.append(args)
        return result
    closer=SimpleNamespace(close=lambda:None,set_block=lambda *a,**kw:None,
                           finish_run=lambda row:receipts.append(row.stop_reason))
    harness=SimpleNamespace(run=run,memory=closer,recorder=closer,cancelled=stopped.is_set)
    monkeypatch.setattr(cli,'make_harness',lambda *a,**kw:harness)
    args=SimpleNamespace(cwd=str(tmp_path),provider='mock',model=None,project='p',
                         goal=None,task='work',max=3,until='python check.py')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(cli.cmd_loop,args)
        deadline=time.monotonic()+15
        while not ready.exists() and not future.done() and time.monotonic()<deadline:
            time.sleep(.02)
        assert ready.exists(), future.result() if future.done() else 'check did not start'
        stop_at=time.monotonic(); stopped.set()
        assert future.result(timeout=5)==130
        assert time.monotonic()-stop_at<2
    assert len(runs)==1 and receipts==['canceled']
    assert result.verification_evidence['process_tree_terminated'] is True
    assert not result.verified
    time.sleep(3)
    assert not late.exists()


def test_failed_goal_check_is_visible_to_the_next_iteration(monkeypatch,tmp_path):
    runs=[]
    closer=SimpleNamespace(close=lambda:None,set_block=lambda *a,**kw:None,finish_run=lambda *a:None)
    def run(*args,**kwargs):
        runs.append(kwargs.get('history'))
        return RunResult(answer='working',messages=[{'role':'assistant','content':'working'}])
    monkeypatch.setattr(cli,'make_harness',lambda *a,**kw:SimpleNamespace(run=run,memory=closer,recorder=closer))
    (tmp_path/'check.py').write_text("print('acceptance: missing sort option')\nraise SystemExit(1)\n", 'utf-8')
    args=SimpleNamespace(cwd=str(tmp_path),provider='mock',model=None,project='p',
                         goal=None,task='work',max=2,until='python check.py')
    assert cli.cmd_loop(args)==1
    assert len(runs)==2
    receipt=runs[1][-1]
    assert receipt['source']=='harness' and receipt['kind']=='loop_check'
    assert 'acceptance: missing sort option' in receipt['content']
