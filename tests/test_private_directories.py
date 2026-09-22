"""Private directory permissions must still let the owner traverse the directory."""
import os
import stat

import pytest

from harness import plat


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory search permission")
def test_private_directory_keeps_owner_search_permission(tmp_path):
    private = tmp_path / "state"
    private.mkdir()
    try:
        plat.chmod_private(str(private))
        assert stat.S_IMODE(private.stat().st_mode) == 0o700
        file = private / "state.json"
        file.write_text("private data")
        assert file.read_text() == "private data"
    finally:
        private.chmod(0o700)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permission")
def test_private_file_remains_nonexecutable(tmp_path):
    file = tmp_path / "credential.json"
    file.write_text("fixture only")
    file.chmod(0o777)
    plat.chmod_private(str(file))
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert file.read_text() == "fixture only"
