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
