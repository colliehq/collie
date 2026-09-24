"""Test isolation shims.

Several test modules set process-wide env vars (COLLIE_STATE_DIR /
COLLIE_NOTES_DIR) at *import* time so each can also be run standalone
(`python tests/test_xxx.py`). Under pytest every module is imported into the
SAME process, so the last import wins and earlier modules' env points at
another module's temp dir — causing FileNotFoundError when their note.append
tests run. This autouse fixture restores each test module's own env right
before the test runs, so modules stay isolated regardless of import order.
"""
import functools
import inspect
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


def _dead_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


#: Where the suite's browser bridge "is": a port nothing listens on. The bridge drives the real,
#: signed-in browser of whoever runs the suite, on a fixed default port, so a test that reached a
#: browser_* path without stubbing every call (`space_identity`, the form read, the origin check)
#: talked to it -- or, with none running, started one. A test that wants a bridge starts its own
#: and points COLLIE_BROWSER_BRIDGE_PORT at it. `COLLIE_BROWSER_LIVE=1` (the opt-in live-browser
#: tests) is the one way to reach the real one.
_NO_BRIDGE = {"COLLIE_BROWSER_BRIDGE_PORT": str(_dead_port()),
              "COLLIE_BROWSER_BRIDGE_NOSPAWN": "1",
              "COLLIE_NO_APPLE_EVENTS": "1"}
_LIVE_BROWSER = os.environ.get("COLLIE_BROWSER_LIVE") == "1"
if not _LIVE_BROWSER:
    # Now, before collection: modules that copy os.environ at import for their subprocesses
    # (surfaces_test.py's ENV) must carry it too.
    os.environ.update(_NO_BRIDGE)


@pytest.fixture(autouse=True)
def _no_real_browser_bridge(monkeypatch):
    """Put it back for every test, whatever an earlier one did to the process environment."""
    if not _LIVE_BROWSER:
        for key, value in _NO_BRIDGE.items():
            monkeypatch.setenv(key, value)
    yield


@pytest.fixture
def tmp(tmp_path):
    """Some test modules were written to be run standalone via a main() that
    passes a temp-dir *path string* (e.g. test_config_roundtrip(tmp)). Under
    pytest that param is collected as a fixture; provide it as a str path."""
    return str(tmp_path)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Make the standalone suites' soft checks real pytest failures as well.

    Their main() inspects a failure list, but pytest never calls main(). Keep
    collecting checks so test cleanup still runs, then fail this exact item.
    """
    module = getattr(item, "module", None)
    original = getattr(module, "check", None)
    signature = inspect.signature(original) if inspect.isfunction(original) else None
    condition = next((name for name in ("cond", "ok")
                      if signature and name in signature.parameters), None)
    failed = []
    if condition:
        @functools.wraps(original)
        def checked(*args, **kwargs):
            values = signature.bind(*args, **kwargs).arguments
            if not values[condition]:
                failed.append(str(next((value for name, value in values.items()
                                        if name != condition), "legacy check failed")))
            return original(*args, **kwargs)
        module.check = checked
    try:
        outcome = yield
    finally:
        if condition:
            module.check = original
    exc = outcome.excinfo
    if exc is not None and exc[0].__name__ == "_Skip":
        outcome.force_exception(pytest.skip.Exception(str(exc[1])))
    elif exc is None and failed:
        outcome.force_exception(AssertionError("Legacy checks failed:\n" + "\n".join(failed)))
