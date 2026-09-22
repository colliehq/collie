from pathlib import Path

from harness import runner_compat


def test_windows_fixture_inherits_scratch_permissions_without_changing_existing_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_compat.plat, "is_windows", lambda: True)
    monkeypatch.setattr(runner_compat, "_git_init", lambda path: "")
    created = []
    original_mkdir = runner_compat.os.mkdir

    def mkdir(path, mode=0o777, **kwargs):
        created.append((str(path), mode))
        return original_mkdir(path, mode, **kwargs)

    monkeypatch.setattr(runner_compat.os, "mkdir", mkdir)
    first, note = runner_compat._make_fixture(str(tmp_path), "codex-sdk")
    second, _ = runner_compat._make_fixture(str(tmp_path), "codex-sdk")
    assert first != second and note == ""
    assert created == [(first, 0o777), (second, 0o777)]
    for path in (first, second):
        assert Path(path).parent == tmp_path
        assert (Path(path) / "hello.py").read_text() == runner_compat._FIXTURE_BODY
