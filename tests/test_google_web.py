"""The Web API behind Settings → Connections → Google, and the Settings entry itself."""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from harness import google_connect as gc
from _google_fakes import REFRESH, connected, make_env

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield make_env(tmp_path, monkeypatch)
    gc._reset_cache()


@pytest.fixture
def web(env, monkeypatch):
    from harness import webapp
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(env["state"] / "sessions"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], webapp.TOKEN
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
        webapp._GOOGLE_CONNECT.update({"busy": False, "error": "", "auth_url": ""})


def _json(url, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=8) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_web_google_endpoints_require_the_token(web):
    base, token = web
    assert _json(base + "/api/google")[0] == 403
    assert _json(base + "/api/google", "POST", {"action": "disconnect"})[0] == 403
    assert _json(base + "/api/google?token=wrong")[0] == 403


def test_web_status_shows_state_without_secrets(web, env):
    base, token = web
    connected()
    code, body = _json(base + "/api/google?token=" + token)
    assert code == 200 and body["status"]["state"] == "connected"
    assert body["status"]["account"] == "owner@example.com"
    assert body["connect"] == {"busy": False, "error": "", "auth_url": ""}
    assert REFRESH not in json.dumps(body) and "FAKE-client-secret" not in json.dumps(body)


def test_web_connect_runs_in_the_background_and_reports_the_link(web, env, monkeypatch):
    base, token = web
    release = threading.Event()

    def fake_connect(open_browser=None, timeout=300, announce=None, **kw):
        announce("https://accounts.google.com/o/oauth2/v2/auth?client_id=x")
        release.wait(5)
        connected()
        return gc.status()
    monkeypatch.setattr(gc, "connect", fake_connect)
    code, body = _json(base + "/api/google?token=" + token, "POST", {"action": "connect"})
    assert code == 200 and body["started"] is True
    assert body["auth_url"].startswith("https://accounts.google.com/")
    code, again = _json(base + "/api/google?token=" + token, "POST", {"action": "connect"})
    assert again["busy"] is True
    release.set()
    for _ in range(50):
        state = _json(base + "/api/google?token=" + token)[1]
        if not state["connect"]["busy"]:
            break
        time.sleep(0.1)
    assert state["status"]["state"] == "connected" and state["connect"]["error"] == ""


def test_web_connect_failure_is_reported(web, env, monkeypatch):
    base, token = web

    def refused(**kw):
        raise gc.GoogleError("denied", "You chose not to give Collie access. Nothing was saved.")
    monkeypatch.setattr(gc, "connect", refused)
    _json(base + "/api/google?token=" + token, "POST", {"action": "connect"})
    for _ in range(50):
        state = _json(base + "/api/google?token=" + token)[1]
        if not state["connect"]["busy"]:
            break
        time.sleep(0.1)
    assert "Nothing was saved" in state["connect"]["error"]


def test_web_disconnect_and_unknown_action(web, env):
    base, token = web
    connected()
    env["fake"].on("POST", gc.REVOKE_URI, {})
    code, body = _json(base + "/api/google?token=" + token, "POST", {"action": "disconnect"})
    assert code == 200 and body["result"]["removed"] is True
    assert body["status"]["state"] == "not_connected"
    assert _json(base + "/api/google?token=" + token, "POST", {"action": "send"})[0] == 400


def test_settings_connections_lists_google():
    page = (ROOT / "harness" / "webui" / "index.html").read_text(encoding="utf-8")
    assert '<div id="googleBox"></div><div id="mcpBox"></div>' in page
    assert 'fetch("/api/google?token=" + encodeURIComponent(CT)' in page
    assert "function googleRender(" in page and "googleLoad();" in page
    assert 'data-google="connect"' in page and 'data-google="disconnect"' in page
