"""Desktop HTTP servers bind without a reverse-DNS lookup of the listen address.

http.server.HTTPServer.server_bind() calls socket.getfqdn(host). With a slow resolver
the reverse lookup of 127.0.0.1 took ~35 s, so the console was not listening in time
for scripts/demo.py (30 s budget) and every start paid that delay.
"""
import socketserver
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

from brainos import edge, server
from brainos.httpd import FastBindHTTPServer


def no_reverse_lookup():
    return mock.patch("socket.getfqdn", side_effect=AssertionError("reverse DNS lookup during bind"))


def stop_serving():
    return mock.patch.object(socketserver.BaseServer, "serve_forever", side_effect=KeyboardInterrupt)


class FastBindHTTPServerTests(unittest.TestCase):
    def test_binds_without_reverse_dns_lookup(self):
        with no_reverse_lookup():
            httpd = FastBindHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        try:
            self.assertEqual(httpd.server_name, "127.0.0.1")
            self.assertGreater(httpd.server_port, 0)
        finally:
            httpd.server_close()


class EntryPointTests(unittest.TestCase):
    def test_console_main_binds_without_reverse_dns_lookup(self):
        with tempfile.TemporaryDirectory() as state, no_reverse_lookup(), stop_serving(), \
                mock.patch.object(sys, "argv", ["brainos.server", "--port", "0", "--state-dir", state]), \
                mock.patch("signal.signal"), mock.patch("builtins.print"):
            server.main()

    def test_edge_main_binds_without_reverse_dns_lookup(self):
        with tempfile.TemporaryDirectory() as state, no_reverse_lookup(), stop_serving(), \
                mock.patch.object(edge, "Gateway"), \
                mock.patch.object(sys, "argv", ["brainos.edge", "--adapter", "unittest.mock:MagicMock",
                                                "--port", "0", "--state-dir", state]), \
                mock.patch("builtins.print"):
            edge.main()


if __name__ == "__main__":
    unittest.main()
