"""The GUI server's listen backlog is applied at listen() time and stays collie-scoped.

Companion to test_web_listener_burst.py, which asserts that a burst of clients is actually
admitted. These two checks guard the *mechanism* that made that possible, because both
failure modes are silent: a backlog assigned after the socket is already listening has no
effect on the kernel's accept queue, and a backlog assigned onto http.server's own class
leaks collie's tuning into every other stdlib server in the process (browserbridge, jobsweb,
progtool and the many tests that build ThreadingHTTPServer directly).

Nothing here asserts a particular number: the expected value is read from the server object,
not from a literal. No model, background service, notification or browser is started.
"""
import socket
from http.server import ThreadingHTTPServer

import pytest


class ListenerObserved(Exception):
    pass


@pytest.mark.parametrize('entrypoint', ['bind_server', 'main'])
def test_backlog_reaches_the_real_listen_call(monkeypatch, entrypoint):
    """The kernel must be told the queue depth; request_queue_size only counts if listen() sees it."""
    from harness import webapp

    real_listen = socket.socket.listen
    observed = []

    def listen(sock, backlog=None):
        observed.append(backlog)
        if backlog is None:
            real_listen(sock)
        else:
            real_listen(sock, backlog)
        # Stop before either entrypoint reaches its accept loop or background services.
        # TCPServer.__init__ closes the listening socket when activation raises.
        raise ListenerObserved

    monkeypatch.setattr(socket.socket, 'listen', listen)
    with pytest.raises(ListenerObserved):
        if entrypoint == 'bind_server':
            webapp.bind_server(0)
        else:
            webapp.main(['--port', '0', '--no-open'])

    assert len(observed) == 1, observed
    # The product asked the OS for its configured depth, not the stdlib's five.
    assert observed[0] == webapp.CollieHTTPServer.request_queue_size
    assert observed[0] > ThreadingHTTPServer.request_queue_size


def test_binding_does_not_retune_other_stdlib_servers(monkeypatch):
    """A module that builds its own ThreadingHTTPServer must be unaffected by collie binding one."""
    from harness import webapp

    before = (ThreadingHTTPServer.request_queue_size, ThreadingHTTPServer.allow_reuse_address)

    real_listen = socket.socket.listen

    def listen(sock, backlog=None):
        real_listen(sock) if backlog is None else real_listen(sock, backlog)
        raise ListenerObserved

    monkeypatch.setattr(socket.socket, 'listen', listen)
    with pytest.raises(ListenerObserved):
        webapp.bind_server(0)

    assert (ThreadingHTTPServer.request_queue_size,
            ThreadingHTTPServer.allow_reuse_address) == before, 'stdlib class was mutated'

    # And the neighbouring-module case that actually matters: a plain stdlib server built after
    # collie bound one still gets the stdlib defaults.
    monkeypatch.undo()
    neighbour = ThreadingHTTPServer(('127.0.0.1', 0), webapp.Handler)
    try:
        assert neighbour.request_queue_size == before[0]
    finally:
        neighbour.server_close()
