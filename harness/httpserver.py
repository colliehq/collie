"""Local HTTP servers whose startup does not depend on reverse DNS."""
from http.server import HTTPServer as _HTTPServer, ThreadingHTTPServer as _ThreadingHTTPServer
from socketserver import TCPServer


class _AddressBinding:
    def server_bind(self):
        # HTTPServer.server_bind calls getfqdn after binding, before listening.
        # On macOS an unavailable resolver can leave even 127.0.0.1 services
        # unresponsive for tens of seconds. We only need the bound address here;
        # Host checks and authentication remain the request handler's concern.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class HTTPServer(_AddressBinding, _HTTPServer):
    pass


class ThreadingHTTPServer(_AddressBinding, _ThreadingHTTPServer):
    pass
