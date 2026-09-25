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
import shutil
import sys
import tempfile

import pytest

#: The suite's home directory, set before any test module imports harness. More than a dozen
#: harness modules resolve paths under ~/.collie at import time (checkpoints, MCP tokens, Slack
#: identity, the UIA driver, the settings file...), most with no env override, so a suite run
#: wrote into the real one: 10,753 checkpoint files had piled up in a developer's
#: ~/.collie/checkpoints, and a web test rewrote the desktop star-map's last-opened repository.
#: A copy of the real ~/.gitconfig keeps git's behaviour (core.autocrlf and the like) without
#: letting a test write to it.
REAL_HOME = os.path.expanduser("~")
TEST_HOME = tempfile.mkdtemp(prefix="collie-test-home-")
# Caches the suite reads but must not re-download: Playwright's browsers live under the home on
# macOS and Linux (Windows keeps them in %LOCALAPPDATA%, which does not move), and so does the
# Hugging Face model cache everywhere. Pin their real locations before the home moves.
if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and os.name != "nt":
    import sys as _sys
    _pw = (os.path.join(REAL_HOME, "Library", "Caches", "ms-playwright") if _sys.platform == "darwin"
           else os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(REAL_HOME, ".cache"),
                             "ms-playwright"))
    if os.path.isdir(_pw):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _pw
if "HF_HOME" not in os.environ:
    _hf = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(REAL_HOME, ".cache"),
                       "huggingface")
    if os.path.isdir(_hf):
        os.environ["HF_HOME"] = _hf
_gitconfig = os.path.join(REAL_HOME, ".gitconfig")
if os.path.isfile(_gitconfig):
    shutil.copyfile(_gitconfig, os.path.join(TEST_HOME, ".gitconfig"))
os.environ["HOME"] = os.environ["USERPROFILE"] = TEST_HOME

#: The suite's temp directory, for the same reason. Tests and the code under test make temp folders
#: and many never remove them: one developer's %TEMP% held 12,352 from this suite. Everything
#: made under this one goes at the end of the session, including what
#: subprocesses make (they inherit TMP/TEMP/TMPDIR). pytest's own tmp_path stays where it was,
#: with its usual keep-the-last-three, so a failure can still be looked at afterwards.
REAL_TEMP = tempfile.gettempdir()
os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", REAL_TEMP)
TEST_TEMP = tempfile.mkdtemp(prefix="collie-test-tmp-")
for _name in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_name] = TEST_TEMP
tempfile.tempdir = TEST_TEMP


def _remove_tree(path):
    # git marks its objects read-only, and Windows does not delete a read-only file, so plain
    # rmtree(ignore_errors=True) leaves every test repository behind.
    def writable(fn, target, _exc):
        try:
            os.chmod(target, 0o700)
            fn(target)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable)
    else:
        shutil.rmtree(path, onerror=writable)


def pytest_sessionfinish(session, exitstatus):
    for path in (TEST_HOME, TEST_TEMP):
        _remove_tree(path)


#: Page loads a Windows runner refuses for a while when it is out of socket buffers
#: (WSAENOBUFS): a browser test then ERRORs in setup without having tested anything. Seen on
#: 2026-09-23 and again on the merge of PR #13; the same commits passed on rerun.
_TRANSIENT_PAGE_LOAD = ("net::ERR_NO_BUFFER_SPACE",)


def _with_transient_retry(goto, attempts=4, pause_s=5.0):
    import time

    def retrying(page, url, **kwargs):
        for attempt in range(attempts):
            try:
                return goto(page, url, **kwargs)
            except Exception as exc:
                if attempt == attempts - 1 or not any(m in str(exc) for m in _TRANSIENT_PAGE_LOAD):
                    raise
                # 5, 15, 30 s: the refusals last as long as sockets sit in TIME_WAIT (up to 120 s),
                # so a short pause only fails again. Paid only when the error actually happens.
                time.sleep(pause_s * (1, 3, 6)[min(attempt, 2)])
    retrying.collie_transient_retry = True
    return retrying


try:
    from playwright.sync_api import Page as _PlaywrightPage
except Exception:                                  # playwright is optional
    _PlaywrightPage = None
if _PlaywrightPage is not None and not getattr(_PlaywrightPage.goto, "collie_transient_retry",
                                               False):
    _PlaywrightPage.goto = _with_transient_retry(_PlaywrightPage.goto)


def _script_only(path):
    """A standalone suite: no test_* function or Test* class, only an `if __name__` entry point."""
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    tests = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
                or isinstance(n, ast.ClassDef) and n.name.startswith("Test") for n in tree.body)
    entry = any(isinstance(n, ast.If) and "__main__" in ast.unparse(n.test) for n in tree.body)
    return entry and not tests


class _ScriptSuite(pytest.Item):
    """Run one standalone suite in its own interpreter; its exit status is the verdict.

    33 test files are scripts with a main() and no test functions: pytest collected nothing from
    them, so `pytest tests/` skipped them silently and only tests/run_all.sh ran them. A child
    process keeps what a script does to its interpreter (sys.path, os.environ, module globals)
    out of the rest of the suite, and it inherits this session's isolated home.
    """

    def runtest(self):
        import subprocess
        import sys
        run = subprocess.run([sys.executable, str(self.path)], cwd=str(self.path.parent.parent),
                             capture_output=True, timeout=900)
        if run.returncode != 0:
            tail = (run.stdout + run.stderr).decode("utf-8", "replace")[-6000:]
            raise AssertionError("%s exited %d:\n%s" % (self.path.name, run.returncode, tail))

    def reportinfo(self):
        return self.path, None, "script %s" % self.path.name


class _ScriptFile(pytest.File):
    def collect(self):
        yield _ScriptSuite.from_parent(self, name="script")


def pytest_collect_file(parent, file_path):
    if (file_path.suffix == ".py" and file_path.name.startswith("test_")
            and file_path.parent.name == "tests" and _script_only(file_path)):
        return _ScriptFile.from_parent(parent, path=file_path)


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
