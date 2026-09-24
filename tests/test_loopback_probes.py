"""Probing a local service that is not running is quick, on Windows too.

Windows reports a refused loopback connection only after about two seconds, so every probe of a
stopped service waited out its whole HTTP timeout: /api/healthz spent 1.0 s on the web probe and
1.5 s on the browser bridge before answering, whenever either was down.
"""
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler

from harness import doctor, ops
from harness.httpserver import ThreadingHTTPServer, loopback_listening, loopback_url_down


def _dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Health(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"ok": true, "extension_connected": true, "last_poll_secs_ago": 1.0}'
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_listening_is_answered_quickly_either_way():
    dead = _dead_port()
    t0 = time.monotonic()
    assert loopback_listening(dead) is False
    assert time.monotonic() - t0 < 0.4
    assert loopback_url_down("http://127.0.0.1:%d/health" % dead) is True
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    try:
        assert loopback_listening(server.server_address[1]) is True
        assert loopback_url_down("http://127.0.0.1:%d/x" % server.server_address[1]) is False
    finally:
        server.server_close()
    # only loopback URLs are judged; anything else is left to the request itself
    assert loopback_url_down("https://example.test/health") is False
    assert loopback_url_down("http://127.0.0.1/no-port") is False


def test_health_with_nothing_running_answers_promptly(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_BROWSER_BRIDGE_PORT", str(_dead_port()))
    with ops.OpsStore(str(tmp_path / "ops.db")) as store:
        t0 = time.monotonic()
        report = ops.aggregate_health(store, desired_workers=[], state_dir=str(tmp_path),
                                      web_port=_dead_port())
        took = time.monotonic() - t0
    assert report["services"]["web"] == {"ok": False}
    assert report["services"]["browser"]["ok"] is False
    assert took < 1.0, "health waited %.2fs on services that are not running" % took


def test_health_reports_the_bridge_on_the_port_the_tools_use(tmp_path, monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("COLLIE_BROWSER_BRIDGE_PORT", str(server.server_address[1]))
        with ops.OpsStore(str(tmp_path / "ops.db")) as store:
            report = ops.aggregate_health(store, desired_workers=[], state_dir=str(tmp_path),
                                          web_port=_dead_port())
        assert report["services"]["browser"]["ok"] is True
        assert report["services"]["browser"]["extension_connected"] is True
        assert doctor.report(str(tmp_path))["runtime"]["browser_bridge"]["ok"] is True
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_the_desktop_launch_probe_does_not_wait_on_a_server_that_is_not_there():
    from harness import wallpaper
    dead = _dead_port()
    t0 = time.monotonic()
    assert wallpaper.server_up(dead) is False
    assert time.monotonic() - t0 < 0.4

    class Ver(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("content-length", "6")
            self.end_headers()
            self.wfile.write(b"0.30.0")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Ver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert wallpaper.server_up(server.server_address[1]) is True
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def test_onboarding_browser_status_is_quick_while_no_bridge_runs(tmp_path, monkeypatch):
    import json
    import urllib.request
    from harness import webapp
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLLIE_BROWSER_BRIDGE_PORT", str(_dead_port()))
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = "http://127.0.0.1:%d/api/browser/status?token=%s" % (server.server_address[1],
                                                                 webapp.TOKEN)
        t0 = time.monotonic()
        with urllib.request.urlopen(url, timeout=10) as response:
            status = json.loads(response.read())
        took = time.monotonic() - t0
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
    assert status.get("extension_connected") in (False, None)
    assert took < 1.0, "the onboarding poll waited %.2fs on a bridge that is not running" % took


def test_onboarding_browser_status_answers_with_the_bridge_connected(tmp_path, monkeypatch):
    """It answered 500 on every poll (a NameError in the handler), so onboarding never saw the
    extension connect."""
    import json
    import urllib.request
    from harness import webapp
    health = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    web = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (health, web)]
    for t in threads:
        t.start()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("COLLIE_BROWSER_BRIDGE_PORT", str(health.server_address[1]))
    try:
        url = "http://127.0.0.1:%d/api/browser/status?token=%s" % (web.server_address[1],
                                                                 webapp.TOKEN)
        with urllib.request.urlopen(url, timeout=10) as response:
            status = json.loads(response.read())
    finally:
        for s in (health, web):
            s.shutdown(); s.server_close()
    assert status["extension_connected"] is True
    assert status["bridge_running"] is True and status["ext_path"].endswith("browser_ext")
    assert isinstance(status["browsers"], list)
