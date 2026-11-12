"""HTTP server for the desktop console and the device gateway."""
import socketserver
from http.server import ThreadingHTTPServer


class FastBindHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that binds without a reverse-DNS lookup of the listen address.

    HTTPServer.server_bind() sets server_name via socket.getfqdn(host). With a slow
    resolver that lookup blocks for tens of seconds (~35 s for 127.0.0.1 on some Macs)
    before the socket listens. Nothing here reads server_name, so it keeps the literal host.
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port
