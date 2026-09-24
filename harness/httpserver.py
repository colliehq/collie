"""Local HTTP servers whose startup does not depend on reverse DNS."""
import sys
from http.server import HTTPServer as _HTTPServer, ThreadingHTTPServer as _ThreadingHTTPServer
from socketserver import TCPServer

#: What a client going away in the middle of a response looks like. BrokenPipeError is the POSIX
#: shape; Windows reports the same event as ConnectionAbortedError (WinError 10053) or
#: ConnectionResetError (10054), which the ``except BrokenPipeError`` guards in the handlers never
#: matched. Each one reached socketserver's handle_error and printed a full traceback: the
#: browser-bridge log on the developer's machine held 49 identical stacks from long polls the
#: extension had abandoned.
CLIENT_GONE = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


def loopback_listening(port, timeout=0.15):
    """Whether anything accepts connections on this loopback port, answered quickly.

    On Windows a connection to a loopback port nobody listens on is not refused at once: the stack
    retries for about two seconds, so a probe of a service that is not running waits out its whole
    HTTP timeout. Measured: 0.53 s of a 1.7 s `collie -p` with no browser bridge, spent on one
    /health probe. A listening port completes the handshake in the kernel in well under a
    millisecond, so a short connect decides it; the request that follows keeps its own timeout.
    """
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except (OSError, ValueError, TypeError):
        return False


def loopback_url_down(url):
    """True when `url` names a loopback port that nothing listens on (so it need not be asked)."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return False
    if host not in ("127.0.0.1", "localhost") or not port:
        return False
    return not loopback_listening(port)


class _AddressBinding:
    def server_bind(self):
        # HTTPServer.server_bind calls getfqdn after binding, before listening.
        # On macOS an unavailable resolver can leave even 127.0.0.1 services
        # unresponsive for tens of seconds. We only need the bound address here;
        # Host checks and authentication remain the request handler's concern.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _QuietClientDisconnects:
    def handle_error(self, request, client_address):
        # A closed tab, a suspended extension worker or an abandoned long poll is not a fault of
        # this server, and there is nobody left to answer. Everything else is still reported.
        if isinstance(sys.exc_info()[1], CLIENT_GONE):
            return
        super().handle_error(request, client_address)


class HTTPServer(_AddressBinding, _QuietClientDisconnects, _HTTPServer):
    pass


class ThreadingHTTPServer(_AddressBinding, _QuietClientDisconnects, _ThreadingHTTPServer):
    pass
