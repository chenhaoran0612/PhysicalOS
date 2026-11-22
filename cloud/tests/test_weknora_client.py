"""Issue #34: the knowledge-service client against a service that misbehaves (made-up local servers only)."""
from __future__ import annotations

import socket
import threading
import time
import unittest

import support  # noqa: F401  (puts cloud/ on sys.path)
from neuxmind_cloud import weknora


class Trickle:
    """Sends ``head`` (status line and headers, or part of them) at once, then one byte of ``drip`` every
    ``pause`` seconds: a service that never quite finishes answering."""

    def __init__(self, head: bytes, drip: bytes = b" ", pause: float = 0.2, seconds: float = 20):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.head, self.drip, self.pause, self.seconds, self.stop = head, drip, pause, seconds, threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.listener.getsockname()[1]}"

    def _serve(self) -> None:
        connection, _ = self.listener.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(self.head)
            end = time.monotonic() + self.seconds
            while time.monotonic() < end and not self.stop.is_set():
                try:
                    connection.sendall(self.drip)
                except OSError:
                    return
                time.sleep(self.pause)

    def close(self) -> None:
        self.stop.set()
        self.listener.close()


class ClientTest(unittest.TestCase):
    def test_a_trickling_answer_is_cut_off_at_the_overall_limit(self):
        ok = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n"
        error = b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n"
        for what, head, drip in (("body", ok, b" "), ("headers", b"HTTP/1.1 200 OK\r\n", b"X"),
                                 ("error body", error, b" "), ("status line", b"HTTP/1.1 ", b"2")):
            with self.subTest(what=what):
                server = Trickle(head, drip)
                self.addCleanup(server.close)
                client = weknora.WeKnora(server.url, "k", "kb-1", timeout=0.5)
                started = time.monotonic()
                with self.assertRaises(weknora.WeKnoraError) as caught:
                    client.knowledge("kn-1")
                self.assertTrue(caught.exception.retry, caught.exception.message)
                self.assertLess(time.monotonic() - started, 5)

    def test_a_trickling_answer_stream_is_cut_off(self):
        server = Trickle(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n", b":")
        self.addCleanup(server.close)
        client = weknora.WeKnora(server.url, "k", "kb-1", timeout=0.5, answer_timeout=1)
        started = time.monotonic()
        with self.assertRaises(weknora.WeKnoraError):
            client._chat("s-1", "kn-1", "q")
        self.assertLess(time.monotonic() - started, 8)

    def test_identifiers_that_could_move_in_a_url_are_refused(self):
        for value in ("..", ".", "../x", "a/b", "", "x" * 200, True):
            with self.subTest(value=value), self.assertRaises(weknora.WeKnoraError):
                weknora._id(value)
        self.assertEqual(weknora._id("kn-1.2:3"), "kn-1.2:3")

    def test_the_address_must_be_plain_http(self):
        for url in ("ftp://host", "http://user:pw@host", "http://host/?x=1", "host:8080"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                weknora.WeKnora(url, "k", "kb-1")


if __name__ == "__main__":
    unittest.main()
