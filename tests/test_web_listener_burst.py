"""Both product startup paths must absorb a modest local connection burst.

Exercise the real listening socket before the accept loop starts. No model,
background services, native notifications or browser is started. The assertion
is successful connection admission, not a wall-time performance threshold or
an implementation constant.
"""
import concurrent.futures
from http.server import ThreadingHTTPServer
import socket
import threading

import pytest


class ListenerObserved(Exception):
    pass


@pytest.mark.parametrize('entrypoint', ['bind_server', 'main'])
@pytest.mark.parametrize('repeat', range(3))
def test_listener_admits_overlapping_client_bursts_before_accept_loop(monkeypatch, entrypoint, repeat):
    from harness import webapp
    activate = ThreadingHTTPServer.server_activate
    evidence = []

    def probe(server):
        activate(server)  # actual listen(), with the product's configured backlog
        connections = []
        failures = []
        # Two overlapping groups of12 clients, as used by the measured pilot.
        # OS accept queues are approximate; a single smaller wave can overcommit
        # the nominal backlog on Windows even when repeated bursts are delayed.
        barrier = threading.Barrier(24, timeout=10)

        def connect(_):
            barrier.wait()
            try:
                return socket.create_connection(('127.0.0.1', server.server_address[1]), timeout=2)
            except OSError as error:
                return error

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=24) as pool:
                for result in pool.map(connect, range(24)):
                    if isinstance(result, OSError):
                        failures.append(type(result).__name__ + ': ' + str(result))
                    else:
                        connections.append(result)
            evidence.append({'connected': len(connections), 'failed': failures,
                             'address': server.server_address, 'backlog': server.request_queue_size})
        finally:
            for connection in connections:
                connection.close()
        # Abort construction before either entrypoint starts background services.
        # TCPServer.__init__ closes the listening socket when activation raises.
        raise ListenerObserved

    monkeypatch.setattr(ThreadingHTTPServer, 'server_activate', probe)
    with pytest.raises(ListenerObserved):
        if entrypoint == 'bind_server':
            webapp.bind_server(0)
        else:
            webapp.main(['--port', '0', '--no-open'])
    assert len(evidence) == 1
    assert evidence[0]['connected'] > 0, 'Loopback networking must be available for this test'
    assert evidence[0]['connected'] == 24, evidence[0]
