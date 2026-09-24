"""A client that leaves mid-response is not a server error; a handler bug still is.

Real loopback sockets. The handler raises the exception a vanished client produces on each
platform (Windows reports 10053/10054 rather than a broken pipe), and a real server thread runs
socketserver's own error path, whose output is captured from stderr.
"""
import io
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler

import pytest

from harness import httpserver


def _serve(exc_type):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            raise exc_type("simulated")

        def log_message(self, *args):
            pass

    server = httpserver.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _request(server):
    with socket.create_connection(server.server_address[:2], timeout=5) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        try:
            s.recv(1024)
        except OSError:
            pass


def _stderr_while(exc_type, monkeypatch):
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stderr", captured)
    server, thread = _serve(exc_type)
    try:
        _request(server)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
    return captured.getvalue()


@pytest.mark.parametrize("exc_type", [BrokenPipeError, ConnectionAbortedError, ConnectionResetError])
def test_a_vanished_client_prints_no_traceback(exc_type, monkeypatch):
    assert _stderr_while(exc_type, monkeypatch) == ""


def test_a_handler_bug_is_still_reported(monkeypatch):
    out = _stderr_while(ValueError, monkeypatch)
    assert "Traceback" in out and "ValueError: simulated" in out


def test_every_shared_server_class_carries_the_rule():
    for cls in (httpserver.HTTPServer, httpserver.ThreadingHTTPServer):
        assert issubclass(cls, httpserver._QuietClientDisconnects)
    from harness import webapp
    assert issubclass(webapp.CollieHTTPServer, httpserver._QuietClientDisconnects)


def test_web_handlers_do_not_guard_client_departure_with_broken_pipe_alone():
    """On Windows the same departure is ConnectionAbortedError/ConnectionResetError."""
    import inspect
    from harness import webapp
    source = inspect.getsource(webapp)
    assert "except BrokenPipeError:" not in source
    assert source.count("except CLIENT_GONE:") >= 3
    # The two run handlers wrap the whole run: a reset there may be upstream and keeps the crash
    # path, so they accept only a local abort as "client went away".
    assert source.count("except (BrokenPipeError, ConnectionAbortedError):") == 2
    assert {ConnectionAbortedError, ConnectionResetError, BrokenPipeError} <= set(httpserver.CLIENT_GONE)
