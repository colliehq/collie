"""History navigation must stay cheap without becoming a recovery authority."""
import json
import os
import subprocess
import sys

import pytest

from harness import session_index, sessions


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    return tmp_path


def save(sid, text="Task"):
    sessions.save(sid, [{"role": "user", "content": text},
                        {"role": "assistant", "content": "done"}], cwd="project", answer="done")


def test_warm_navigation_and_new_process_do_not_decode_conversations(store, monkeypatch):
    save("one")
    save("two")
    expected = sessions.recent()
    monkeypatch.setattr(sessions, "_load_raw", lambda p: pytest.fail("history decoded again"))
    assert sessions.recent() == expected
    code = (
        "import json; from harness import sessions; "
        "sessions._load_raw=lambda p: (_ for _ in ()).throw(AssertionError('decoded')); "
        "print(json.dumps(sessions.recent()))")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


def test_only_changed_conversation_is_refreshed(store, monkeypatch):
    save("one"); save("two")
    sessions.recent()
    sessions.set_title("one", "Renamed")
    reads = []
    original = sessions._load_raw
    monkeypatch.setattr(sessions, "_load_raw", lambda p: (reads.append(p), original(p))[1])
    rows = {row["id"]: row for row in sessions.recent()}
    assert rows["one"]["title"] == "Renamed"
    assert len(reads) == 1 and reads[0].endswith("one.json")


def test_same_size_replacement_with_restored_mtime_invalidates_index(store):
    save("one", "Alpha")
    sessions.recent()
    path = store / "one.json"
    before = path.stat()
    other = store / "replacement.tmp"
    other.write_text(path.read_text("utf-8").replace("Alpha", "Bravo"), encoding="utf-8")
    os.utime(other, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(other, path)
    assert path.stat().st_size == before.st_size
    assert sessions.recent()[0]["title"] == "Bravo"


def test_cache_never_hides_corrupt_recovery_or_replaces_journal(store):
    save("one")
    sessions.recent()
    path = store / "one.json"
    path.write_text('{"messages":', encoding="utf-8")
    before = path.read_bytes()
    assert sessions.recent()[0]["id"] == "one"
    assert sessions.recent()[0]["title"] == ""
    assert sessions.load_checked("one")["status"] == "invalid"
    assert sessions.recovery_state("one")["recovery_required"] is True
    assert path.read_bytes() == before


@pytest.mark.parametrize("broken", ["directory", "corrupt"])
def test_unavailable_index_keeps_history_usable(store, broken):
    save("one", "Preserve me")
    cache = store / session_index._FILENAME
    if broken == "directory":
        cache.mkdir()
    else:
        cache.write_bytes(b"not a database")
    assert sessions.recent()[0]["title"] == "Preserve me"
    assert sessions.load_checked("one")["status"] == "ok"


def test_deleted_and_recreated_identity_cannot_inherit_old_summary(store):
    save("one", "First")
    sessions.recent()
    assert sessions.delete("one")
    assert sessions.recent() == []
    save("one", "Replacement")
    assert sessions.recent()[0]["title"] == "Replacement"


def test_identical_session_ids_in_different_stores_stay_separate(store, monkeypatch):
    save("one", "Project A")
    sessions.recent()
    second = store / "second"
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(second))
    save("one", "Project B")
    assert sessions.recent()[0]["title"] == "Project B"
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(store))
    assert sessions.recent()[0]["title"] == "Project A"


def test_multimodal_titles_and_unique_tool_paths_survive_index(store):
    sessions.save("one", [
        {"role": "user", "content": [{"type": "image"}, {"type": "text", "text": "Inspect this"}]},
        {"role": "user", "source": "harness", "content": "internal note"},
        {"role": "assistant", "tool_calls": [
            {"id": "a", "name": "read_file", "args": {"path": "app.py"}},
            {"id": "b", "name": "edit_file", "args": {"path": "app.py"}},
            {"id": "c", "name": "write_file", "args": {"path": "app.py"}}]}])
    cold = sessions.recent()[0]
    assert (cold["title"], cold["turns"], cold["touches"], cold["edits"]) == ("Inspect this", 1, 1, 1)
    assert sessions.recent()[0] == cold


def test_warm_recovery_listing_reads_active_journals_but_skips_inactive_history(store, monkeypatch):
    save('done')
    sessions.checkpoint('running',[],state='external_action')
    sessions.recent()
    original=sessions._load_raw; reads=[]
    def read(path):
        reads.append(os.path.basename(path)); return original(path)
    monkeypatch.setattr(sessions,'_load_raw',read)
    active=sessions.active_runs()
    assert [row['session_id'] for row in active]==['running']
    assert reads==['running.json']
    assert active[0]['recovery_required']


def test_timeline_reads_the_parent_and_uses_index_for_fork_navigation(store, monkeypatch):
    save('parent'); save('unrelated')
    sessions.fork('parent',2,child_id='child',title='Alternate approach')
    sessions.recent()
    original=sessions._load_raw; reads=[]
    def read(path):
        reads.append(os.path.basename(path)); return original(path)
    monkeypatch.setattr(sessions,'_load_raw',read)
    timeline=sessions.timeline('parent')
    assert timeline['children'][0]['title']=='Alternate approach'
    assert reads==['parent.json']
