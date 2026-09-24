"""The update routes on the real web handler: token, who may install, and what reaches the feed.

A real ``webapp.Handler`` serves over loopback HTTP. The release feed and the installer child are
staged through ``update_notice``; nothing contacts GitHub or runs an installer.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from harness import update, update_notice


@pytest.fixture
def web(monkeypatch, tmp_path):
    from harness import webapp

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setenv("COLLIE_UPDATE_STATUS", str(tmp_path / "update-status.json"))
    monkeypatch.setenv("COLLIE_UPDATE_INSTALL_LOG", str(tmp_path / "update-install.log"))
    monkeypatch.setenv("COLLIE_UPDATE_JOURNAL", str(tmp_path / "update-journal.json"))
    monkeypatch.setattr(update_notice, "__version__", "0.29.1")
    monkeypatch.setattr(update_notice, "_checking", {"active": False})
    monkeypatch.setattr(update_notice, "_child", {"proc": None})
    monkeypatch.setattr(update_notice, "auto_enabled", lambda: False)
    monkeypatch.setattr(update, "install_kind", lambda: "setup")
    monkeypatch.setattr(update_notice, "_one_press_supported", lambda kind: kind == "setup")
    calls = {"feed": 0, "spawned": []}

    def feed(channel):
        calls["feed"] += 1
        return {"current": "0.29.1", "latest": "0.30.0", "newer": True, "channel": "stable",
                "prerelease": False, "kind": "setup", "notes": "Notes", "url": "https://x.test",
                "assets": {}, "digests": {}}

    monkeypatch.setattr(update, "check", feed)
    real_start = update_notice.start_install

    class Child:
        pid = 777

        def poll(self):
            return None

        def wait(self):
            return 0

    def start(version, **kw):
        kw.setdefault("spawn", lambda argv, **_k: calls["spawned"].append(argv) or Child())
        kw.setdefault("watch", lambda fn: None)
        return real_start(version, **kw)

    monkeypatch.setattr(update_notice, "start_install", start)
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], webapp.TOKEN, calls
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def _json(url, method="GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method,
                                     headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_every_update_route_requires_the_page_token(web):
    base, _token, calls = web
    assert _json(base + "/api/update")[0] == 403
    assert _json(base + "/api/update/check", "POST", {})[0] == 403
    assert _json(base + "/api/update/install", "POST", {"version": "0.30.0"})[0] == 403
    assert calls["feed"] == 0 and calls["spawned"] == []


def test_reading_the_notice_does_not_reach_the_feed_and_check_does(web):
    base, token, calls = web
    code, value = _json(base + "/api/update?token=" + token)
    assert code == 200 and value["current"] == "0.29.1" and value["newer"] is False
    assert calls["feed"] == 0
    code, value = _json(base + "/api/update/check?token=" + token, "POST", {})
    assert code == 200 and value["latest"] == "0.30.0" and value["newer"] is True
    assert value["one_press"] is True and calls["feed"] == 1


def test_install_from_this_computer_starts_the_bound_cli(web):
    base, token, calls = web
    _json(base + "/api/update/check?token=" + token, "POST", {})
    code, value = _json(base + "/api/update/install?token=" + token, "POST", {"version": "0.30.0"})
    assert code == 200 and value["install"]["state"] == "running"
    assert calls["spawned"] and calls["spawned"][0][-2:] == ["--expect", "0.30.0"]


def test_a_relayed_phone_request_may_read_but_not_install(web):
    base, token, calls = web
    _json(base + "/api/update/check?token=" + token, "POST", {})
    relay = {"X-Collie-Relay": "1"}
    code, value = _json(base + "/api/update?token=" + token, headers=relay)
    assert code == 200 and value["newer"] is True and value["one_press"] is False
    code, refused = _json(base + "/api/update/install?token=" + token, "POST",
                          {"version": "0.30.0"}, headers=relay)
    assert code == 403 and "this computer" in refused["error"]
    assert calls["spawned"] == []


def test_a_stale_version_is_refused_with_the_current_notice(web):
    base, token, calls = web
    _json(base + "/api/update/check?token=" + token, "POST", {})
    code, refused = _json(base + "/api/update/install?token=" + token, "POST", {"version": "0.29.9"})
    assert code == 409 and "check again" in refused["error"]
    assert refused["update"]["latest"] == "0.30.0"
    assert calls["spawned"] == []
