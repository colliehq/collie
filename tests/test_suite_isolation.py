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
