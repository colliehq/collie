"""Socket reuse and response framing for the shared browser fixture."""
import json
import socket
import threading

import pytest

from test_web_ui_run_status import (TOKEN, FixtureHTTPServer, _Fixture,  # noqa: F401
                                    _reset_fixture_state, browser, server)


@pytest.fixture
def wire():
    """The shared fixture on its own listener, with each accepted connection recorded."""
    seen = []
    original = _Fixture.setup

    def setup(self):
        seen.append(self.client_address)
        return original(self)

    _reset_fixture_state()
    _Fixture.setup = setup
    httpd = FixtureHTTPServer(("127.0.0.1", 0), _Fixture)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1], seen
    finally:
        _Fixture.setup = original
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _read_response(sock):
    """Head and body of one response, stopping at the length the response itself declared."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        headers[name.strip().lower().decode()] = value.strip().decode()
    length = headers.get("content-length")
    if length is not None:
        while len(body) < int(length):
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
    return head.split(b"\r\n")[0].decode(), headers, body


def _get(sock, base, path):
    host = base.rsplit("/", 1)[-1]
    sock.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (path, host)).encode())
    return _read_response(sock)


def test_several_reads_share_one_connection_instead_of_one_each(wire):
    """The boot's repeated API reads must not each cost a socket and a TIME_WAIT entry."""
    base, seen = wire
    paths = ["/api/whoami", "/api/sessions", "/api/runs", "/api/settings", "/api/models",
             "/api/task-inbox/pending", "/api/approvals", "/api/missions"]
    sock = socket.create_connection(("127.0.0.1", int(base.rsplit(":", 1)[-1])), timeout=10)
    try:
        for path in paths:
            status, headers, body = _get(sock, base, path)
            assert status.endswith("200 OK"), (path, status)
            assert headers.get("connection", "").lower() != "close", (path, headers)
            assert "content-length" in headers, (path, headers)
            json.loads(body)                     # framed exactly, with nothing of the next reply
    finally:
        sock.close()
    assert len(seen) == 1, \
        "%d reads opened %d connections; keep-alive is not in effect" % (len(paths), len(seen))


def test_a_read_after_an_event_stream_is_a_new_connection_not_a_hang(wire):
    """The stream's body ends at EOF, so it must say `close` and must actually close.

    This is the negative control for the keep-alive above: an unframed body on a connection the
    client believes it may reuse is the failure mode keep-alive would introduce, and it would show
    up as the next read blocking rather than as anything about the stream itself.
    """
    base, seen = wire
    port = int(base.rsplit(":", 1)[-1])
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        sock.sendall(("GET /api/stream?q=Busy%%20fixture&session=s-read HTTP/1.1\r\n"
                      "Host: 127.0.0.1:%d\r\n\r\n" % port).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(65536)
            assert chunk, "stream closed before its response headers"
            head += chunk
        header_block, _, rest = head.partition(b"\r\n\r\n")
        assert b"text/event-stream" in header_block
        assert b"Connection: close" in header_block, header_block
        sock.settimeout(10)
        while True:                              # the stream ends by closing, and it does end
            chunk = sock.recv(65536)
            if not chunk:
                break
            rest += chunk
        assert b"event: done" in rest and b'"busy": true' in rest
    finally:
        sock.close()
    assert len(seen) == 1
    # ...and the page's next read works, on a connection of its own.
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        status, headers, body = _get(sock, base, "/api/sessions")
        assert status.endswith("200 OK")
        assert json.loads(body)["sessions"]
    finally:
        sock.close()
    assert len(seen) == 2


def test_a_boot_opens_far_fewer_connections_than_it_makes_requests(server, browser):  # noqa: F811
    """The whole point, measured the way the suite actually loads the page."""
    requests = []
    original = _Fixture.parse_request
    connections = []
    original_setup = _Fixture.setup

    def setup(self):
        connections.append(self.client_address)
        return original_setup(self)

    def parse_request(self):
        result = original(self)
        if result:
            requests.append(self.path)
        return result

    _Fixture.setup = setup
    _Fixture.parse_request = parse_request
    try:
        _reset_fixture_state()
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        try:
            page = context.new_page()
            page.goto(server + "/?token=" + TOKEN, wait_until="load")
            page.wait_for_selector("#input", timeout=8000)
            page.wait_for_timeout(400)
        finally:
            context.close()
    finally:
        _Fixture.setup = original_setup
        _Fixture.parse_request = original
    assert len(requests) > 12, "boot served too little to say anything: %r" % requests
    assert len(connections) * 3 < len(requests), \
        "%d requests over %d connections: the boot is still one socket per request" % (
            len(requests), len(connections))
