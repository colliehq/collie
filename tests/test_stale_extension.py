"""An extension older than this Collie says why an action failed, and Live stops asking.

Chrome keeps running an unpacked extension until it is reloaded, so after an update each new action
came back as a bare "unknown action ..." -- Live Copilot's page view failed that way on every tick
of a session, 1706 times in the developer's bridge log, without a word to anyone.
"""
import json

from harness import browserbridge as bb


class _Resp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def _stale(monkeypatch, action):
    monkeypatch.setattr(bb, "_ensure_server", lambda port: True)
    monkeypatch.setattr(bb, "_health", lambda port=None, timeout=2: {"extension_version": "3.2"})
    monkeypatch.setattr(bb, "_shipped_extension_version", lambda: "4.1")
    monkeypatch.setattr(bb.urllib.request, "urlopen", lambda req, timeout=0: _Resp(
        {"ok": True, "data": {"error": "unknown action " + action}}))


def test_an_unknown_action_says_the_extension_is_older_and_how_to_reload_it(monkeypatch):
    _stale(monkeypatch, "live_observation")
    res = bb._call({"action": "live_observation"}, timeout=5)
    error = res["data"]["error"]
    assert error.startswith("unknown action live_observation")    # what the reload tool reads
    assert "running in Chrome is 3.2" in error and "ships 4.1" in error
    assert "chrome://extensions" in error and res["data"]["stale_extension"] is True


def test_the_reload_tool_still_recognises_an_extension_too_old_to_reload_itself(monkeypatch):
    _stale(monkeypatch, "reload")
    out = bb.BrowserReloadExtension().run({}, None)
    assert "too old to reload itself" in out


def test_live_says_once_that_the_page_view_needs_a_reload_and_backs_off(monkeypatch, tmp_path):
    from harness import live_copilot
    from harness.live_copilot import LiveCopilotRuntime, LiveSessionStore
    calls = []

    def observation(**_kw):
        calls.append(1)
        return {"unsupported": True, "detail": "unknown action live_observation: reload it."}

    monkeypatch.setattr(bb, "live_tab_observation", observation)
    store = LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, understand=False, observe_apps=False,
                observe_ui=False, observe_input=False, observe_screen=True)
    runtime = LiveCopilotRuntime(tmp_path, analyzer=lambda _payload: {})
    value = store.snapshot()
    assert runtime._capture_browser_visual(value, 1_000_000) is None
    assert runtime._capture_browser_visual(value, 1_060_000) is None     # a minute later
    assert len(calls) == 1, "not asked again inside ten minutes"
    notices = [e for e in store.snapshot()["events"] if e["kind"] == "notice"]
    assert len(notices) == 1 and "window screenshot" in notices[0]["text"]
    runtime._capture_browser_visual(value, 1_000_000 + 600_001)
    assert len(calls) == 2, "and asked again after them, in case it was reloaded"
