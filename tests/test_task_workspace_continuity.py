"""A task owns one workspace and one writer, including shortcut and handoff routes."""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import sessions, session_owner, worktree, webapp, web_tasks
from test_web_task_inbox import web, _post, _get


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / 'project'; root.mkdir()
    monkeypatch.setenv('COLLIE_SESSIONS_DIR', str(tmp_path / 'sessions'))
    for args in (['init', '-q'], ['config', 'user.email', 'test@example.com'],
                 ['config', 'user.name', 'Test']):
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
    (root / 'a.txt').write_text('original\n', encoding='utf-8')
    for args in (['add', '.'], ['commit', '-qm', 'initial']):
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
    return root


def isolated(project, sid='workspace-task'):
    sessions.save(sid, [{'role':'user', 'content':'improve it'}], cwd=str(project))
    workspace = sessions.handoff(sid, 'isolated')['workspace']
    (Path(workspace['path']) / 'a.txt').write_text('improved\n', encoding='utf-8')
    return sid, workspace


def test_handoff_keeps_uncommitted_changes_and_moves_the_conversation(project):
    sid, workspace = isolated(project)
    (Path(workspace['path']) / 'new.txt').write_text('new file\n', encoding='utf-8')
    result = sessions.handoff(sid, 'local', confirm=True)
    assert result['workspace']['mode'] == 'local'
    assert Path(sessions.load(sid)['cwd']) == project
    assert (project / 'a.txt').read_text() == 'improved\n'
    assert (project / 'new.txt').read_text() == 'new file\n'
    assert Path(workspace['path']).is_dir()
    assert sessions.recovery_state(sid) is None


def test_saved_workspaces_and_byte_exact_binary_crlf_handoff(project):
    (project/'.gitattributes').write_bytes(b'* -text\n')
    (project/'a.txt').write_bytes(b'original\r\nsecond line\r\n')
    subprocess.run(['git','add','.'],cwd=project,check=True,capture_output=True)
    subprocess.run(['git','commit','-qm','raw line endings'],cwd=project,check=True,capture_output=True)
    sid, workspace=isolated(project)
    path=Path(workspace['path'])
    assert path.is_relative_to(project.parent/'workspaces')
    text=b'changed\r\nsecond line\r\n'
    binary=bytes(range(256))*20
    (path/'a.txt').write_bytes(text); (path/'artifact.bin').write_bytes(binary)
    sessions.handoff(sid,'local',confirm=True)
    assert (project/'a.txt').read_bytes()==text
    assert (project/'artifact.bin').read_bytes()==binary


def test_handoff_refuses_when_new_files_cannot_enter_the_patch(project, monkeypatch):
    sid, workspace=isolated(project)
    original=worktree._git
    def locked_index(args,cwd,**kwargs):
        if args==['add','-A','--intent-to-add']:
            return False,'index is locked'
        return original(args,cwd,**kwargs)
    monkeypatch.setattr(worktree,'_git',locked_index)
    with pytest.raises(ValueError,match='could not include new files'):
        sessions.handoff(sid,'local',confirm=True)
    assert (project/'a.txt').read_text()=='original\n'
    assert sessions.recovery_state(sid) is None


def test_readers_and_title_changes_do_not_wait_for_git_apply(project, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    sid, workspace=isolated(project)
    pending=threading.Event(); finish=threading.Event()
    def slow_apply(*args, before_apply, **kwargs):
        before_apply(); pending.set()
        assert finish.wait(5)
        return {'ok':True,'applied':True,'files':['a.txt']}
    monkeypatch.setattr(worktree,'handoff_to_local',slow_apply)
    with ThreadPoolExecutor(max_workers=2) as pool:
        applying=pool.submit(sessions.handoff,sid,'local',confirm=True)
        try:
            assert pending.wait(3)
            row=pool.submit(sessions.load,sid).result(timeout=2)
            assert row['active_run']['state']=='external_action'
            sessions.set_title(sid,'Title changed while applying')
        finally:
            finish.set()
        applying.result(timeout=3)
    assert sessions.load(sid)['title']=='Title changed while applying'


def test_dirty_main_checkout_is_preserved_without_a_recovery_prompt(project):
    sid, workspace = isolated(project)
    (project / 'a.txt').write_text('local work\n')
    with pytest.raises(ValueError, match='local checkout has changes'):
        sessions.handoff(sid, 'local', confirm=True)
    assert (project / 'a.txt').read_text() == 'local work\n'
    assert sessions.load(sid)['cwd'] == workspace['path']
    assert sessions.recovery_state(sid) is None


def test_handoff_cannot_race_an_executor(project):
    sid, workspace = isolated(project)
    with session_owner.acquire(sid):
        with pytest.raises(ValueError, match='conversation is running'):
            sessions.handoff(sid, 'local', confirm=True)
    assert (project / 'a.txt').read_text() == 'original\n'
    assert sessions.load(sid)['cwd'] == workspace['path']


def test_applied_patch_with_failed_journal_save_requires_inspection(project, monkeypatch):
    sid, workspace = isolated(project)
    write = sessions._atomic_dump
    def fail_after_apply(raw, path):
        if raw.get('workspace', {}).get('mode') == 'local':
            raise OSError('disk unavailable after apply')
        return write(raw, path)
    monkeypatch.setattr(sessions, '_atomic_dump', fail_after_apply)
    with pytest.raises(OSError):
        sessions.handoff(sid, 'local', confirm=True)
    assert (project / 'a.txt').read_text() == 'improved\n'
    assert sessions.recovery_state(sid)['recovery_required']
    with pytest.raises(ValueError, match='inspect the interrupted operation'):
        sessions.handoff(sid, 'local', confirm=True)
    assert sessions.load(sid)['cwd'] == workspace['path']
    monkeypatch.setattr(sessions,'_atomic_dump',write)
    sessions.reconcile_recovery(sid,'completed',confirmed=True)
    assert Path(sessions.load(sid)['cwd'])==project
    assert sessions.handoff(sid,'local',confirm=True)['existing']
    assert (project/'a.txt').read_text()=='improved\n'


def test_isolated_followups_reuse_the_saved_folder_even_with_another_requested_cwd(project, monkeypatch):
    from harness import cli, settings
    monkeypatch.chdir(project.parent)
    monkeypatch.setattr(webapp, '_provider', lambda:'mock')
    monkeypatch.setattr(settings, 'apply', lambda:None)
    monkeypatch.setattr(settings, 'get', lambda *a, **k:'')
    seen=[]
    class Harness:
        def __init__(self, cwd, gate):
            self.gate=gate; self.cwd=cwd; self.composer=SimpleNamespace(identity='')
            self.memory=self.recorder=SimpleNamespace(close=lambda:None)
            self.mode='act'; self.force_edit=True; self.max_turns=0; self._max_turns_hard_cap=None
            self.self_verify=False; self.verify_max=2; self.verify_gate=False; self.require_assert=False
        def run(self, task_id, message, history=None, **kwargs):
            seen.append(self.cwd)
            (Path(self.cwd)/'a.txt').write_text('improved\n')
            return SimpleNamespace(answer='done',error='',model='mock',prefix_tokens=0,input_tokens=0,
                output_tokens=0,total_tokens=0,turns=1,tool_calls=0,wall_ms=1,cost_usd=0,
                verified=False,canceled=False,messages=list(history or [])+
                [{'role':'user','content':message},{'role':'assistant','content':'done'}])
    monkeypatch.setattr(cli,'make_harness',lambda cwd,*a,**k:Harness(cwd,k['gate']))
    def run(extra):
        events=[]; handler=object.__new__(webapp.Handler)
        handler._sse_open=lambda:None; handler._sse=lambda kind,data:events.append((kind,data))
        webapp.Handler._serve_stream(handler, {'q':['improve file'],'session':['isolated-run'],
            'intent':['build'],'workspace':['isolated'],'verification':['auto'], **extra})
        assert events[-1][0]=='done' and not events[-1][1].get('error'), events
        return next(data for kind,data in events if kind=='start')
    first=run({'cwd':[str(project)]})
    second=run({'cwd':[str(project.parent)]})
    assert seen[0] == seen[1] != str(project)
    assert first['workspace_info'] == second['workspace_info']
    assert sessions.load('isolated-run')['workspace']['path'] == seen[0]
    assert (project/'a.txt').read_text() == 'original\n'


def test_desktop_shortcut_refuses_busy_session_before_effect_and_keeps_cwd(web, monkeypatch):
    from harness import desktop
    base, token, state=web
    cwd=state/'project'; cwd.mkdir()
    sessions.save('shortcut',[],cwd=str(cwd)); effects=[]
    monkeypatch.setattr(desktop,'desktop_intent',lambda text:effects.append(text) or {'action':'app','ok':True,'arg':'demo'})
    with session_owner.acquire('shortcut'):
        code,_=_post(base,token,'/api/desktop/intent',{'session':'shortcut','text':'open demo'})
    assert code==409 and effects==[]
    code,result=_post(base,token,'/api/desktop/intent',{'session':'shortcut','text':'open demo'})
    assert code==200 and result['session']=='shortcut' and effects==['open demo']
    saved=sessions.load('shortcut')
    assert saved['cwd']==str(cwd) and len(saved['messages'])==2
    assert sessions.recovery_state('shortcut') is None


def test_desktop_failure_keeps_a_fence_and_never_replays(project):
    effects=[]
    def interrupted():
        effects.append('effect'); raise OSError('lost acknowledgement')
    with pytest.raises(OSError):
        webapp.Handler._desktop_command('shortcut','open demo',interrupted,lambda r:'opened')
    assert sessions.recovery_state('shortcut')['recovery_required']
    with pytest.raises(web_tasks.WebInputError):
        webapp.Handler._desktop_command('shortcut','open demo',interrupted,lambda r:'opened')
    assert effects==['effect']


def test_missing_folder_can_be_explicitly_relocated_without_copying_or_bypassing_a_lease(web):
    base,token,state=web
    destination=state/'restored'; destination.mkdir()
    sessions.save('missing-folder',[{'role':'user','content':'keep my original request'}],cwd=str(state/'gone'))
    payload={'session':'missing-folder','cwd':str(destination)}
    with session_owner.acquire('missing-folder'):
        assert _post(base,token,'/api/session/relocate',payload)[0]==409
    code,result=_post(base,token,'/api/session/relocate',payload)
    assert code==200 and result['result']['cwd']==str(destination)
    saved=sessions.load('missing-folder')
    assert saved['messages'][0]['content']=='keep my original request'
    assert saved['handoffs'][-1]['previous_cwd']==str(state/'gone')
    assert list(destination.iterdir())==[]
    sessions.checkpoint('missing-folder',saved['messages'],state='external_action')
    assert _post(base,token,'/api/session/relocate',payload)[0]==409


def test_workspace_bind_failure_ends_registry_and_releases_the_owner(project, monkeypatch):
    from harness import cli, settings
    monkeypatch.chdir(project)
    monkeypatch.setattr(webapp,'_provider',lambda:'mock')
    monkeypatch.setattr(settings,'apply',lambda:None)
    monkeypatch.setattr(settings,'get',lambda *a,**k:'')
    def fail_bind(*args,**kwargs):
        raise OSError('disk unavailable')
    def must_not_launch(*args,**kwargs):
        raise AssertionError('worker must not launch after a failed workspace bind')
    monkeypatch.setattr(sessions,'bind_isolated_workspace',fail_bind)
    monkeypatch.setattr(cli,'make_harness',must_not_launch)
    events=[]; handler=object.__new__(webapp.Handler)
    handler._sse_open=lambda:None; handler._sse=lambda kind,data:events.append((kind,data))
    webapp.Handler._serve_stream(handler,{'q':['improve file'],'session':['bind-failure'],
        'intent':['build'],'workspace':['isolated'],'verification':['auto']})
    assert 'workspace could not be saved' in events[-1][1]['error']
    assert webapp.Handler._runs['bind-failure']['ended'] is not None
    assert not session_owner.probe_busy('bind-failure')


def test_relocation_cannot_detach_an_existing_isolated_workspace(web, monkeypatch):
    base,token,state=web
    source=state/'isolated'; source.mkdir()
    destination=state/'main'; destination.mkdir()
    sessions.save('isolated',[],cwd=str(source))
    sessions.bind_isolated_workspace('isolated',{'ok':True,'dir':str(source),'root':str(destination),'branch':'test'})
    code,_=_post(base,token,'/api/session/relocate',{'session':'isolated','cwd':str(destination)})
    assert code==409 and sessions.load('isolated')['workspace']['mode']=='isolated'
    assert _post(base,token,'/api/session/relocate',{'session':'isolated','cwd':str(source)})[0]==200
    assert sessions.load('isolated')['workspace']['mode']=='isolated'


def test_native_locator_is_reused_only_in_its_current_workspace(project):
    from harness.cli import _worker_session
    sid='native-move'
    sessions.save(sid,[],cwd=str(project))
    def receipt(path,locator):
        sessions.append_run_receipt(sid,{'runner':{'runner':'claude-code',
            'native_session':{'runner':'claude-code','workspace':str(path),'locator':locator}}})
    receipt(project,'main-thread')
    assert _worker_session(sid,'claude-code',cwd=str(project))['locator']=='main-thread'
    receipt(project.parent/'old-isolated','isolated-thread')
    assert _worker_session(sid,'claude-code',cwd=str(project)) is None
    receipt(project,'new-main-thread')
    assert _worker_session(sid,'claude-code',cwd=str(project))['locator']=='new-main-thread'


def test_release_only_removes_the_named_registered_worktree(project):
    root=project.parent
    manual=root/'manual-worktree'
    sibling=root/'keep.txt'; sibling.write_text('keep unrelated data')
    subprocess.run(['git','worktree','add','-b','collie/manual',str(manual)],cwd=project,
                   check=True,capture_output=True)
    assert worktree.release(str(project),force=True)['ok'] is False
    assert worktree.release(str(root),force=True)['ok'] is False
    assert worktree.release(str(manual),force=True)['ok'] is True
    assert project.is_dir() and sibling.read_text()=='keep unrelated data' and not manual.exists()
