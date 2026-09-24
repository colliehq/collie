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
