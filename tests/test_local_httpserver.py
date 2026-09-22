"""Local services stay reachable even when the host's reverse DNS is unavailable."""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
import socket
import threading

import pytest

from harness.httpserver import HTTPServer, ThreadingHTTPServer


@pytest.mark.parametrize("server_type", [HTTPServer, ThreadingHTTPServer])
def test_local_server_binds_and_answers_without_reverse_dns(monkeypatch, server_type):
    def unavailable(*args, **kwargs):
        raise AssertionError("local HTTP startup attempted reverse DNS")

    monkeypatch.setattr(socket, "getfqdn", unavailable)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = server_type(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = HTTPConnection(*server.server_address, timeout=3)
    try:
        client.request("GET", "/health")
        response = client.getresponse()
        assert response.status == 200 and response.read() == b"ok"
        assert server.server_name == "127.0.0.1"
        assert server.server_port == server.server_address[1] != 0
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert not thread.is_alive()
