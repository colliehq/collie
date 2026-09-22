"""A branch shares its parent's workspace; neither side may delete it from under the other.

`fork` copies the parent's workspace record, so two conversations legitimately run in ONE
worktree, which the handoff read as two independent owners. What these hold to: changes still
land, the other tree survives, nothing is copied out from under a live cotenant, a kept
directory says why, and a sole owner is still cleaned up.
"""
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from harness import session_owner, sessions, worktree


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


def isolated(project, sid='parent', work=True):
    sessions.save(sid, [{'role': 'user', 'content': 'improve it'},
                        {'role': 'assistant', 'content': 'on it'}], cwd=str(project))
    workspace = sessions.handoff(sid, 'isolated')['workspace']
    if work:
        (Path(workspace['path']) / 'a.txt').write_text('improved\n', encoding='utf-8')
    return sid, workspace


def journal(sid, directory=None):
    return json.loads(Path(sessions._path(sid, directory)).read_text(encoding='utf-8'))


def background(name, fn):
    """Start fn in a named thread; the returned join re-raises whatever it hit."""
    box = {}
    def run():
        try:
            box['value'] = fn()
        except BaseException as exc:          # surfaced by finish(), never swallowed
            box['error'] = exc
    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    def finish(timeout=30):
        thread.join(timeout)
        assert not thread.is_alive(), name + ' never finished'
        if 'error' in box:
            raise box['error']
        return box.get('value')
    return finish


def pause_inside_apply(monkeypatch, entered, resume):
    """Hold worktree.handoff_to_local open, with the workspace exclusion taken."""
    real = worktree.handoff_to_local
    def paused(*a, **kw):
        entered.set()
        assert resume.wait(20)
        return real(*a, **kw)
    monkeypatch.setattr(worktree, 'handoff_to_local', paused)


def test_a_branch_shares_the_parents_worktree_instead_of_claiming_it(project):
    sid, workspace = isolated(project)
    child = sessions.fork(sid, 2, child_id='child')['id']
    shared = sessions.load(child)['workspace']
    assert shared['path'] == workspace['path'] and shared['mode'] == 'isolated'
    assert shared['shared'] is True and shared['owner'] == sid
    assert sessions.load(sid)['workspace'].get('shared') is None
    assert sessions.workspace_holders(workspace['path'])['sessions'] == [child, sid]


def test_a_branchs_cleanup_keeps_the_workspace_its_parent_still_lives_in(project):
    sid, workspace = isolated(project)
    child = sessions.fork(sid, 2, child_id='child')['id']
    (Path(workspace['path']) / 'notes.txt').write_text('branch notes\n', encoding='utf-8')
    result = sessions.handoff(child, 'local', confirm=True, remove_isolated=True)
    assert result['ok'] and result['workspace']['mode'] == 'local'
    assert (project / 'a.txt').read_text() == 'improved\n' and Path(sessions.load(child)['cwd']) == project
    assert result['workspace']['isolated_removed'] is False and sid in result['workspace']['isolated_retained']
    assert (Path(workspace['path']) / 'a.txt').read_text() == 'improved\n'
    assert (Path(workspace['path']) / 'notes.txt').read_text() == 'branch notes\n'
    assert sessions.load(sid)['workspace']['path'] == workspace['path']
    assert journal(child)['workspace']['isolated_removed'] is False


def test_a_parents_cleanup_does_not_strand_a_branch_pointing_at_it(project):
    sid, workspace = isolated(project)
    child = sessions.fork(sid, 2, child_id='child')['id']
    result = sessions.handoff(sid, 'local', confirm=True, remove_isolated=True)
    assert result['workspace']['isolated_removed'] is False and child in result['workspace']['isolated_retained']
    assert Path(workspace['path']).is_dir()
    branch = sessions.load(child)
    assert sessions.resolve_cwd(branch) == workspace['path']
    assert (Path(branch['cwd']) / 'a.txt').read_text() == 'improved\n'
    assert [m['content'] for m in branch['messages']] == ['improve it', 'on it']


def test_an_old_shared_journal_written_before_ownership_was_recorded_still_counts(project):
    sid, workspace = isolated(project)
    # An earlier version's thread: no owner, no shared flag, no workspace record
    # at all — only the directory it runs in.
    legacy = Path(sessions._path('legacy'))
    legacy.write_text(json.dumps({'id': 'legacy', 'messages': [], 'project': 'web',
                                  'cwd': workspace['path']}), encoding='utf-8')
    result = sessions.handoff(sid, 'local', confirm=True, remove_isolated=True)
    assert result['workspace']['isolated_removed'] is False and 'legacy' in result['workspace']['isolated_retained']
    assert Path(workspace['path']).is_dir()
    assert sessions.resolve_cwd(sessions.load('legacy')) == workspace['path']


@pytest.mark.parametrize('spelling', ['trailing', 'dot', 'slash', 'case'])
def test_the_same_directory_spelled_differently_is_the_same_directory(project, spelling):
    sid, workspace = isolated(project)
    path = workspace['path']
    if spelling == 'trailing':
        other = path + os.sep
    elif spelling == 'dot':
        other = os.path.join(path, '.')
    elif spelling == 'slash':
        # How git spells a Windows path, which is where a handed-off `cwd` comes from.
        other = path.replace(os.sep, '/')
    else:
        other = path.swapcase() if os.name == 'nt' else path
        if other == path:
            pytest.skip('case-insensitive paths are a Windows behaviour')
    Path(sessions._path('sibling')).write_text(
        json.dumps({'id': 'sibling', 'messages': [],
                    'workspace': {'mode': 'isolated', 'path': other}}), encoding='utf-8')
    result = sessions.handoff(sid, 'local', confirm=True, remove_isolated=True)
    assert result['workspace']['isolated_removed'] is False and 'sibling' in result['workspace']['isolated_retained']
    assert Path(path).is_dir()


def test_an_unreadable_journal_is_not_evidence_that_nobody_lives_here(project):
    sid, workspace = isolated(project)
    Path(sessions._path('torn')).write_text('{"id": "torn", "messages": [', encoding='utf-8')
    result = sessions.handoff(sid, 'local', confirm=True, remove_isolated=True)
    assert (project / 'a.txt').read_text() == 'improved\n'      # still applied
    assert result['workspace']['isolated_removed'] is False
    assert all(s in result['workspace']['isolated_retained'] for s in ('could not be read', 'torn'))
    assert Path(workspace['path']).is_dir()


def test_a_running_sibling_whose_journal_is_torn_is_still_a_running_sibling(project):
    """The reported blocker: unreadable is not unrelated, and the copy is the damage.
    A branch is mid-edit in the shared tree when its journal stops parsing; residency
    can no longer be read, but its id can still be held - and it is."""
    sid, workspace = isolated(project, work=False)
    child = sessions.fork(sid, 2, child_id='child')['id']
    (Path(workspace['path']) / 'a.txt').write_text('half-written by the branch\n', encoding='utf-8')
    with session_owner.acquire(child, label='branch is running'):
        Path(sessions._path(child)).write_text('{', encoding='utf-8')
        holders = sessions.workspace_holders(workspace['path'], exclude=(sid,))
        assert holders['sessions'] == [] and holders['unreadable'] == [child]
        with pytest.raises(ValueError, match='is running in this shared workspace'):
            sessions.handoff(sid, 'local', confirm=True)
    # Nothing applied, nothing moved: the refusal came before the copy, not after.
    assert (project / 'a.txt').read_text() == 'original\n' and sessions.load(sid)['workspace']['mode'] == 'isolated'
    assert (Path(workspace['path']) / 'a.txt').read_text() == 'half-written by the branch\n'
    # Once that run ends the same unreadable journal no longer blocks the handoff.
    assert sessions.handoff(sid, 'local', confirm=True)['workspace']['mode'] == 'local'
    assert (project / 'a.txt').read_text() == 'half-written by the branch\n'


def test_a_journal_that_cannot_even_be_named_is_not_evidence_of_quiet(project):
    """No id, no lease, no answer. An unusable row may not be read as an empty one."""
    sid, workspace = isolated(project)
    nameless = Path(sessions._path(sid)).with_name('x' * 200 + '.json')
    nameless.write_text('{"id": "?", "messages": []}', encoding='utf-8')
    assert sessions.workspace_holders(workspace['path'])['opaque']
    with pytest.raises(ValueError, match='cannot be checked for other running'):
        sessions.handoff(sid, 'local', confirm=True, remove_isolated=True)
    assert (project / 'a.txt').read_text() == 'original\n' and Path(workspace['path']).is_dir()
    nameless.unlink()
    assert sessions.handoff(sid, 'local', confirm=True)['workspace']['mode'] == 'local'


def test_a_sole_owner_still_gets_its_workspace_cleaned_up(project):
    sid, workspace = isolated(project)
    result = sessions.handoff(sid, 'local', confirm=True, remove_isolated=True)
    assert result['workspace']['isolated_removed'] is True and 'isolated_retained' not in result['workspace']
    assert not Path(workspace['path']).exists() and worktree.listing(project) == []
    assert (project / 'a.txt').read_text() == 'improved\n'


def test_the_last_conversation_out_of_a_shared_workspace_turns_the_lights_off(project):
    sid, workspace = isolated(project, work=False)
    child = sessions.fork(sid, 2, child_id='child')['id']
    assert sessions.handoff(child, 'local', confirm=True,
                            remove_isolated=True)['workspace']['isolated_removed'] is False
    assert Path(workspace['path']).is_dir()
    result = sessions.handoff(sid, 'local', confirm=True, remove_isolated=True)
    assert result['workspace']['isolated_removed'] is True and not Path(workspace['path']).exists()
    assert (project / 'a.txt').read_text() == 'original\n'


def test_a_live_run_in_a_shared_workspace_is_refused_before_anything_is_applied(project):
    sid, workspace = isolated(project)
    child = sessions.fork(sid, 2, child_id='child')['id']
    with session_owner.acquire(sid, label='parent is running'):
        with pytest.raises(ValueError, match='is running in this shared workspace'):
            sessions.handoff(child, 'local', confirm=True)
    assert (project / 'a.txt').read_text() == 'original\n'
    assert sessions.load(child)['cwd'] == workspace['path'] and journal(child)['workspace']['mode'] == 'isolated'
    assert sessions.recovery_state(child) is None and Path(workspace['path']).is_dir()


def test_a_sole_owner_is_not_slowed_down_by_a_neighbours_lease(project):
    sid, workspace = isolated(project)
    sessions.save('unrelated', [], cwd=str(project))
    with session_owner.acquire('unrelated', label='different workspace'):
        assert sessions.handoff(sid, 'local', confirm=True)['workspace']['mode'] == 'local'
    assert (project / 'a.txt').read_text() == 'improved\n'


def test_a_sibling_cannot_start_running_while_the_shared_tree_is_being_copied(project, monkeypatch):
    """Held through the copy, not merely sampled first: a probed neighbour can start a moment later."""
    sid, workspace = isolated(project)
    child = sessions.fork(sid, 2, child_id='child')['id']
    applying, resume = threading.Event(), threading.Event()
    pause_inside_apply(monkeypatch, applying, resume)
    handed = background('handoff', lambda: sessions.handoff(child, 'local', confirm=True))
    try:
        assert applying.wait(20)
        assert session_owner.try_acquire(sid, label='racing run') is None
        with pytest.raises(ValueError, match='is running'):
            sessions.handoff(sid, 'local', confirm=True)
    finally:
        resume.set()
    assert handed()['workspace']['mode'] == 'local' and (project / 'a.txt').read_text() == 'improved\n'
    session_owner.acquire(sid, label='freed again').release()   # handed back after


def test_a_fork_racing_a_cleanup_never_lands_on_a_deleted_directory(project, monkeypatch):
    """The branch is taken while the relocation is in flight: the fork starts only
    once it has demonstrably reached the held exclusion, so it is serialized behind
    the move and inherits where the parent lives now, not a deleted folder."""
    sid, workspace = isolated(project)
    applying, resume, at_claim = threading.Event(), threading.Event(), threading.Event()
    pause_inside_apply(monkeypatch, applying, resume)
    real_claim = sessions._workspace_claim
    def watched_claim(key, directory=None):
        if key and threading.current_thread().name == 'fork':
            at_claim.set()            # the branch has reached the held exclusion
        return real_claim(key, directory)
    monkeypatch.setattr(sessions, '_workspace_claim', watched_claim)
    handed = background('handoff', lambda: sessions.handoff(
        sid, 'local', confirm=True, remove_isolated=True))
    try:
        assert applying.wait(20)
        forked = background('fork', lambda: sessions.fork(sid, 2, child_id='child'))
        assert at_claim.wait(20)
    finally:
        resume.set()
    result, child = handed(), sessions.load(forked()['id'])
    assert result['workspace']['isolated_removed'] is True
    assert not Path(workspace['path']).exists()
    assert child['workspace']['mode'] == 'local' and Path(child['cwd']).is_dir()
    assert (Path(child['cwd']) / 'a.txt').read_text() == 'improved\n'
    assert [m['content'] for m in child['messages']] == ['improve it', 'on it']


def test_a_cleanup_that_cannot_run_is_not_a_failed_handoff(project, monkeypatch):
    """Removal is optional; the applied patch and the recorded location are not."""
    sid, workspace = isolated(project)
    def refuse(path, force=False):
        raise OSError(13, 'permission denied')
    monkeypatch.setattr(worktree, 'release', refuse)
    result = sessions.handoff(sid, 'local', confirm=True, remove_isolated=True)
    assert result['ok'] and result['workspace']['mode'] == 'local'
    assert (project / 'a.txt').read_text() == 'improved\n' and result['workspace']['isolated_removed'] is False
    assert 'permission denied' in result['workspace']['isolated_retained']
    assert Path(workspace['path']).is_dir() and journal(sid)['workspace']['mode'] == 'local'


def test_an_unwritable_cleanup_note_still_reports_the_durable_location(project, monkeypatch):
    """The footnote is the only thing that may be lost, and losing it is reported."""
    sid, workspace = isolated(project)
    real_dump = sessions._atomic_dump
    def refuse_note(obj, path):
        if 'isolated_removed' in (obj.get('workspace') or {}):
            raise OSError(28, 'no space left on device')
        return real_dump(obj, path)
    monkeypatch.setattr(sessions, '_atomic_dump', refuse_note)
    result = sessions.handoff(sid, 'local', confirm=True, remove_isolated=True)
    assert result['ok'] and result['workspace']['isolated_removed'] is True
    assert not Path(workspace['path']).exists() and 'no space left' in result['workspace']['cleanup_note_error']
    assert (Path(journal(sid)['workspace']['path']) / 'a.txt').read_text() == 'improved\n'


def test_cleanup_never_touches_a_directory_that_is_not_a_registered_worktree(project, tmp_path):
    sid, workspace = isolated(project)
    outsider = tmp_path / 'not-a-worktree'; outsider.mkdir()
    (outsider / 'keep.txt').write_text('unrelated data', encoding='utf-8')
    sessions.bind_isolated_workspace(
        sid, {'ok': True, 'dir': str(outsider), 'root': str(project), 'branch': 'collie/x'})
    cleanup = sessions._release_vacated_workspace(sid, str(outsider))
    assert cleanup['removed'] is False and 'unregistered' in cleanup['reason']
    assert (outsider / 'keep.txt').read_text() == 'unrelated data'
