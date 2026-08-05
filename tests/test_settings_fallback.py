"""A settings read that fails must not quietly become "nothing was ever saved".

From one conversation that went wrong in a way nobody could see. Seven turns answered normally,
then `push` came back with canned "Based on the tool output:" text — twice, verbatim — and the
commit it described had never been pushed. settings.json was correct on disk the whole time and
the Settings panel reported the right provider throughout.

The path: _load() blanked its cache on ANY read failure while leaving the cached mtime at the
last good value, so the next call saw an unchanged mtime, skipped the reload, and served {} for
the rest of the process's life. One transient failure — an atomic panel save racing a reader, a
scanner holding the file for a moment — latched permanently. apply() then popped every
COLLIE_<KEY> it had injected, and webapp._provider() answered the way it used to: "mock".

Two independent things had to be true for a fixture to reach a person as an answer. Both are
tested here: a failed read keeps what it had, and an unconfigured provider refuses instead of
inventing one.
"""
import importlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def _fresh_settings(path):
    """Import harness.settings pointed at `path`, with COLLIE_PROVIDER absent.

    Absent matters: _HARD_ENV is snapshotted at import, and a var already set there is classed as
    a user override that apply() must never touch — the opposite of what these cases exercise.
    """
    import harness
    os.environ.pop("COLLIE_PROVIDER", None)
    os.environ["COLLIE_SETTINGS_PATH"] = path
    sys.modules.pop("harness.settings", None)
    if hasattr(harness, "settings"):
        delattr(harness, "settings")
    return importlib.import_module("harness.settings")


def _boom(_p):
    raise OSError(13, "locked by another process")


def main():
    tmp = tempfile.mkdtemp(prefix="collie-settings-")
    path = os.path.join(tmp, "settings.json")
    old_path_env = os.environ.get("COLLIE_SETTINGS_PATH")
    old_prov = os.environ.get("COLLIE_PROVIDER")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"PROVIDER": "anthropic-oauth", "MODEL": "claude-sonnet-5"}, f)

        st = _fresh_settings(path)
        check(st._load().get("PROVIDER") == "anthropic-oauth", "a readable settings.json loads")

        # 1. a failed read keeps the last good values instead of blanking them
        real = os.path.getmtime
        os.path.getmtime = _boom
        try:
            check(st._load().get("PROVIDER") == "anthropic-oauth",
                  "a failed read keeps the last good values")
        finally:
            os.path.getmtime = real

        # 2. ...and does not latch. The file has NOT changed, so its mtime still matches the one
        #    cached before the failure — the exact condition under which the old code skipped the
        #    reload and kept serving {} forever.
        check(st._load().get("PROVIDER") == "anthropic-oauth",
              "and recovers on the next call, with the file untouched")

        # 3. a file that does not exist is a real answer, not a failure
        st_missing = _fresh_settings(os.path.join(tmp, "nope.json"))
        check(st_missing._load() == {}, "a missing settings.json is {} (nothing saved yet)")

        # 4. end to end — the step that actually reached the conversation: apply() must not pop
        #    COLLIE_PROVIDER back to unset because one read blipped.
        st2 = _fresh_settings(path)
        st2.apply()
        check(os.environ.get("COLLIE_PROVIDER") == "anthropic-oauth",
              "apply() injects the saved provider")
        os.path.getmtime = _boom
        try:
            st2.apply()
        finally:
            os.path.getmtime = real
        check(os.environ.get("COLLIE_PROVIDER") == "anthropic-oauth",
              "and a failed read does not pop it back to unset")

        # 5. the second line of defence: no provider is "", never a fixture
        from harness import webapp
        os.environ.pop("COLLIE_PROVIDER", None)
        check(webapp._provider() == "", "_provider() with nothing set is empty, not mock")
        os.environ["COLLIE_PROVIDER"] = "mock"
        check(webapp._provider() == "mock", "mock is still reachable, by name")
        os.environ["COLLIE_PROVIDER"] = "anthropic-oauth"
        check(webapp._provider() == "anthropic-oauth", "and a real provider passes through")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for k, v in (("COLLIE_SETTINGS_PATH", old_path_env), ("COLLIE_PROVIDER", old_prov)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("\n  " + ("%d FAILED" % len(fails) if fails else "settings fallback: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
