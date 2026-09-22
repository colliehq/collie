import os
from pathlib import Path


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "main.py").write_text(
        "def first():\n    return 1\n\n\ndef second():\n    return first()\n",
        encoding="utf-8")
    return repo


def test_codemap_cache_survives_process_memory_and_invalidates_nested_edits(tmp_path, monkeypatch):
    from harness import codemap

    repo = _repo(tmp_path)
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))

    first, source, fingerprint = codemap.cached_tree(str(repo))
    assert source == "rebuilt" and first
    assert Path(codemap._tree_cache_path(str(repo))).is_file()

    def must_not_rebuild(_cwd):
        raise AssertionError("the persisted cache should be used")

    monkeypatch.setattr(codemap, "build_tree", must_not_rebuild)
    second, source, same = codemap.cached_tree(str(repo))
    assert source == "disk" and same == fingerprint and second == first

    target = repo / "main.py"
    previous = target.stat().st_mtime_ns
    target.write_text("def changed():\n    return 2\n\n\ndef second():\n    return changed()\n",
                      encoding="utf-8")
    os.utime(target, ns=(previous + 1_000_000, previous + 1_000_000))
    assert codemap.tree_fingerprint(str(repo)) != fingerprint

    outside = tmp_path / "outside.py"
    outside.write_text("def outside():\n    return 1\n", encoding="utf-8")
    assert codemap.read_abs(str(target), allowed_roots={str(repo)})
    assert codemap.read_abs(str(outside), allowed_roots={str(repo)}) is None


def test_web_tree_reports_rebuilt_then_memory_cache(tmp_path, monkeypatch):
    from harness import webapp

    repo = _repo(tmp_path)
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    sent = []

    class FakeHandler(webapp.Handler):
        def __init__(self):
            pass

        def _send_json(self, value, status=200):
            sent.append(value)

    here = os.getcwd()
    try:
        os.chdir(repo)
        webapp.Handler._TREE_CACHE.clear()
        webapp.Handler._MAP_ROOTS.clear()
        FakeHandler()._serve_tree({})
        FakeHandler()._serve_tree({})
        FakeHandler()._serve_file({"abs": [str(repo / "main.py")]})
    finally:
        os.chdir(here)
        webapp.Handler._TREE_CACHE.clear()
        webapp.Handler._MAP_ROOTS.clear()

    assert sent[0]["cache"]["state"] == "rebuilt"
    assert sent[1]["cache"]["state"] == "memory"
    assert sent[0]["cache"]["fingerprint"] == sent[1]["cache"]["fingerprint"]
    assert "def first" in sent[2]["source"]


def test_default_map_repo_skips_attempt_worktrees_and_remembers_selection(tmp_path, monkeypatch):
    from harness import sessions, webapp

    neutral = tmp_path / "neutral"
    neutral.mkdir()
    disposable = tmp_path / "collie_wt_attempt" / "aut_123"
    stable = tmp_path / "workspace" / "collie"
    other = tmp_path / "workspace" / "other"
    for repo in (disposable, stable, other):
        (repo / ".git").mkdir(parents=True)

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(sessions, "recent", lambda n=50: [
        {"cwd": str(disposable)}, {"cwd": str(stable)}])
    here = os.getcwd()
    cache = dict(webapp.Handler._REPOS_CACHE)
    try:
        os.chdir(neutral)
        webapp.Handler._REPOS_CACHE.clear()
        assert webapp.Handler._default_repo() == os.path.realpath(stable)
        webapp.Handler._remember_map_repo(str(stable))
        monkeypatch.setattr(sessions, "recent", lambda n=50: [{"cwd": str(other)}])
        assert webapp.Handler._default_repo() == os.path.realpath(stable)
        assert "collie_wt_" not in (tmp_path / "state" / "map-last-repo.txt").read_text()
    finally:
        os.chdir(here)
        webapp.Handler._REPOS_CACHE.clear()
        webapp.Handler._REPOS_CACHE.update(cache)
