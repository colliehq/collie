"""The suite runs in its own home directory, never the developer's.

A suite run used to write into the real ~/.collie: thousands of checkpoint files, and the desktop
star-map's last-opened repository. conftest points HOME/USERPROFILE at a temporary directory
before any test module imports harness; this checks that the paths harness resolves at import
time actually landed there.
"""
import os

import conftest


def _inside(path, root):
    path, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def test_home_is_the_suites_own():
    assert os.path.normcase(os.path.expanduser("~")) == os.path.normcase(conftest.TEST_HOME)
    assert os.path.normcase(conftest.TEST_HOME) != os.path.normcase(conftest.REAL_HOME)


def test_temp_is_the_suites_own_in_this_process_and_its_children(tmp_path):
    import subprocess
    import sys
    import tempfile
    assert _inside(tempfile.mkdtemp(prefix="isolation-"), conftest.TEST_TEMP)
    child = subprocess.run([sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
                           capture_output=True, text=True, timeout=60).stdout.strip()
    assert _inside(child, conftest.TEST_TEMP), child
    # pytest's own tmp_path is kept outside, so it survives the session for a post-mortem.
    assert not _inside(str(tmp_path), conftest.TEST_TEMP), tmp_path


def test_the_session_end_removes_read_only_files(tmp_path):
    import stat
    tree = tmp_path / "repo" / ".git" / "objects" / "ab"
    tree.mkdir(parents=True)
    obj = tree / "cdef"
    obj.write_bytes(b"x")
    os.chmod(obj, stat.S_IREAD)        # what git does to every object it writes
    conftest._remove_tree(str(tmp_path / "repo"))
    assert not (tmp_path / "repo").exists()


def test_a_transient_page_load_refusal_is_retried_and_nothing_else_is():
    calls = []

    def goto(page, url, **kw):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("Page.goto: net::ERR_NO_BUFFER_SPACE at %s" % url)
        return "loaded"

    retrying = conftest._with_transient_retry(goto, pause_s=0)
    assert retrying(None, "http://127.0.0.1:1/") == "loaded" and len(calls) == 2

    def broken(page, url, **kw):
        calls.append(url)
        raise RuntimeError("Page.goto: net::ERR_CONNECTION_REFUSED")

    calls.clear()
    import pytest
    with pytest.raises(RuntimeError, match="REFUSED"):
        conftest._with_transient_retry(broken, pause_s=0)(None, "http://127.0.0.1:1/")
    assert len(calls) == 1, "a real failure is not retried"


def test_import_time_state_paths_are_under_the_suites_home():
    from harness import checkpoint, mcpclient, native, ops, plantool, settings, slackbot
    paths = {
        "checkpoint._DIR": checkpoint._DIR,
        "mcpclient._TOKENS": mcpclient._TOKENS,
        "mcpclient._CACHE": mcpclient._CACHE,
        "native.COLLIE_DIR": native.COLLIE_DIR,
        "ops.DEFAULT_STATE_DIR": ops.DEFAULT_STATE_DIR,
        "plantool._DIR": plantool._DIR,
        "settings._PATH": settings._PATH,
        "slackbot.IDENTITY": slackbot.IDENTITY,
        "slackbot.THREADS": slackbot.THREADS,
    }
    real = os.path.join(conftest.REAL_HOME, ".collie")
    leaks = {k: v for k, v in paths.items() if _inside(v, real)}
    assert not leaks, "resolved into the developer's own ~/.collie: %r" % leaks
