"""Real filesystem regressions for temporary worktree cleanup."""
import os
import stat

import pytest

from harness import plat


def test_readonly_file_cleanup_preserves_sibling(tmp_path):
    target = tmp_path / "owned"
    nested = target / ".git" / "objects"
    nested.mkdir(parents=True)
    readonly = nested / "blob"
    readonly.write_bytes(b"git object")
    readonly.chmod(stat.S_IREAD)
    sibling = tmp_path / "keep"
    sibling.write_bytes(b"unchanged")
    plat.rmtree(str(target))
    assert not target.exists()
    assert sibling.read_bytes() == b"unchanged"


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing denies delete")
def test_busy_file_is_kept_and_can_be_cleaned_later(tmp_path):
    target = tmp_path / "owned"
    target.mkdir()
    file = target / "open"
    file.write_bytes(b"open")
    with file.open("rb"):
        plat.rmtree(str(target))
        assert file.read_bytes() == b"open"
    plat.rmtree(str(target))
    assert not target.exists()


def test_directory_symlink_cleanup_preserves_destination(tmp_path):
    sibling = tmp_path / "keep"
    sibling.mkdir()
    (sibling / "valuable").write_bytes(b"unchanged")
    target = tmp_path / "owned"
    target.mkdir()
    try:
        (target / "link").symlink_to(sibling, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    plat.rmtree(str(target))
    assert not target.exists()
    assert (sibling / "valuable").read_bytes() == b"unchanged"
