"""Test isolation shims.

Several test modules set process-wide env vars (COLLIE_STATE_DIR /
COLLIE_NOTES_DIR) at *import* time so each can also be run standalone
(`python tests/test_xxx.py`). Under pytest every module is imported into the
SAME process, so the last import wins and earlier modules' env points at
another module's temp dir — causing FileNotFoundError when their note.append
tests run. This autouse fixture restores each test module's own env right
before the test runs, so modules stay isolated regardless of import order.
"""
import os

import pytest


def _module_env(mod):
    """Recover the (state_dir, notes_dir) a test module declared at import."""
    state = getattr(mod, "_state", None)
    # notes dir preference: explicit module var, else <state>/notes
    notes = (getattr(mod, "_notes", None)
             or getattr(mod, "_tmp_notes", None))
    if notes is None and state is not None:
        notes = os.path.join(state, "notes")
    return state, notes


@pytest.fixture(autouse=True)
def _restore_module_env(request):
    mod = request.module
    state, notes = _module_env(mod)
    if state is not None:
        os.environ["COLLIE_STATE_DIR"] = state
    if notes is not None:
        os.environ["COLLIE_NOTES_DIR"] = notes
    yield


@pytest.fixture
def tmp(tmp_path):
    """Some test modules were written to be run standalone via a main() that
    passes a temp-dir *path string* (e.g. test_config_roundtrip(tmp)). Under
    pytest that param is collected as a fixture; provide it as a str path."""
    return str(tmp_path)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Test modules that predate pytest raise their own `_Skip` exception to
    signal "not supported on this OS" (e.g. symlink creation without Windows
    Developer Mode). Translate that into a real pytest skip instead of a fail.
    """
    outcome = yield
    exc = outcome.excinfo
    if exc is not None and exc[0].__name__ == "_Skip":
        outcome.force_exception(pytest.skip.Exception(str(exc[1])))
