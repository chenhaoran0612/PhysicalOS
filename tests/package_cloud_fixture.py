"""Stand-ins for the cloud library as issue #31 reads it. Nothing here reaches a real server.

``FakeLibraryCloud`` answers the two routes a package delivery uses, on this computer only:
``GET /api/v1/library/materials/{id}`` (the labels, size, SHA-256 and status of a material) and
``GET /api/v1/library/materials/{id}/content`` (the bytes, ``Range`` honoured, the checksum of the
whole file in ``X-Checksum-SHA256``). Every file is made up by the test that adds it, and the
session token is made up for each instance.

Faults a test can switch on: ``cuts`` (close the connection once that many bytes of a file went
out), ``offline`` (answer no new connection), ``offline_after_cut``, ``ignore_range`` (send the
whole file whatever was asked for), ``shift_range`` (answer a range that starts elsewhere),
``header_checksum`` (announce another checksum), ``stall_after`` (stop sending but keep the
connection, once), ``stall_every`` (the same in every answer, until ``released``), ``failures`` (HTTP statuses, or (status, JSON body) pairs, to answer the next file
requests with), ``close_delimited`` (no ``Content-Length``, the end of the file is the end of the
connection), ``chunked`` (``Transfer-Encoding: chunked``), ``open_ended`` (no length and the
connection stays open), ``refuse_range_once`` (one 416), ``drop_content_range`` (a 206 that does
not say which bytes it holds) and ``pad`` (bytes sent after the end of the file).

``CuttingRelay`` sits in front of a real cloud app (``cloud_fixture.CloudServer``) and closes a
connection once it carried a set number of bytes of a file towards the desktop. TCP hides lost
packets from a program: what a weak link leaves is waiting and broken connections, and the
second of those is what both stand for.
"""
import hashlib
import json
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler

from brainos.httpd import FastBindHTTPServer

PREFIX = '/api/v1/library/materials/'
RANGE = re.compile(r'bytes=(\d+)-(\d*)')


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeLibraryCloud:
    def __init__(self):
        self.token = 'nxs_' + secrets.token_hex(16)
        self.materials = {}
        self.cuts, self.failures = [], []
        self.offline = self.offline_after_cut = self.ignore_range = self.close_delimited = False
        self.chunked = self.open_ended = self.refuse_range_once = self.drop_content_range = False
        self.shift_range, self.header_checksum, self.stall_after, self.pad = 0, None, None, b''
        self.stall_every = None
        self.requests = []              # (path, Range header or None) of every request that was answered
        self.served = 0                 # bytes of files that went out
        self.released = threading.Event()
        self.lock = threading.Lock()
        owner = self

        class Server(FastBindHTTPServer):
            daemon_threads = True

            def verify_request(self, request, client_address):
                return not owner.offline

            def handle_error(self, request, client_address):
                pass                    # a connection a test cut on purpose

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, fmt, *args):
                pass

            def do_GET(self):
                owner.answer(self)

        self.httpd = Server(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    # --- what a test prepares and reads -------------------------------------------------------

    def add(self, filename, data, status='usable', kind='package', fmt='deb', declared_sha256=None,
            declared_size=None, **labels):
        """A material as the cloud would describe it. ``declared_*`` make the description disagree with the bytes."""
        material_id = 'mat_' + secrets.token_hex(10)
        view = {'id': material_id, 'filename': filename, 'kind': kind, 'format': fmt, 'body_type': 'quadruped',
                'vendor': '示例厂家', 'model': 'X1', 'version': '1.0', 'description': '',
                'size': len(data) if declared_size is None else declared_size,
                'sha256': declared_sha256 or sha(data), 'status': status, 'status_detail': '',
                'created_at': '2026-11-21T00:00:00+00:00', 'updated_at': '2026-11-21T00:00:00+00:00', **labels}
        self.materials[material_id] = {'view': view, 'data': data}
        return material_id

    def view(self, material_id):
        return dict(self.materials[material_id]['view'])

    def sign_in(self, platform):
        """Point a workspace at this stand-in, as if its user had signed in to the cloud."""
        platform.cloud._save_settings({'base_url': self.url, 'email': 'dev@fixture.test', 'email_verified': True})
        platform.cloud._store_token(self.token)

    def content_requests(self):
        return [(path, asked) for path, asked in self.requests if path.endswith('/content')]

    def close(self):
        self.released.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    # --- the cloud side -----------------------------------------------------------------------

    def _json(self, handler, status, body):
        raw = json.dumps(body).encode()
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    def answer(self, handler):
        path = handler.path.split('?')[0]
        asked = handler.headers.get('Range')
        if handler.headers.get('Authorization') != 'Bearer ' + self.token:
            return self._json(handler, 401, {'error': 'not signed in', 'code': 'unauthorized'})
        if not path.startswith(PREFIX):
            return self._json(handler, 404, {'error': 'no such route', 'code': 'not_found'})
        material_id, _, rest = path[len(PREFIX):].partition('/')
        material = self.materials.get(material_id)
        with self.lock:
            self.requests.append((path, asked))
        if material is None or rest not in ('', 'content'):
            return self._json(handler, 404, {'error': 'no such material', 'code': 'not_found'})
        if rest == '':
            return self._json(handler, 200, material['view'])
        with self.lock:
            failure = self.failures.pop(0) if self.failures else None
        if failure:
            status, body = failure if isinstance(failure, tuple) else (
                failure, {'error': 'refused by the test', 'code': 'content_missing' if failure == 404 else 'fixture_failure'})
            return self._json(handler, status, body)
        self._content(handler, material, asked)

    def _content(self, handler, material, asked):
        data, start = material['data'], 0
        match = RANGE.fullmatch(asked or '')
        if match and self.refuse_range_once:
            self.refuse_range_once = False
            handler.send_response(416)
            handler.send_header('Content-Length', '0')
            handler.end_headers()
            return
        if match and not self.ignore_range:
            start = int(match.group(1)) + self.shift_range
            if start >= len(data):
                handler.send_response(416)
                handler.send_header('Content-Range', 'bytes */%d' % len(data))
                handler.send_header('Content-Length', '0')
                handler.end_headers()
                return
            end = min(int(match.group(2)), len(data) - 1) if match.group(2) else len(data) - 1
            body = data[start:end + 1]
            handler.send_response(206)
            if not self.drop_content_range:
                handler.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, len(data)))
        else:
            body = data
            handler.send_response(200)
        body += self.pad
        handler.send_header('Content-Type', 'application/octet-stream')
        handler.send_header('X-Checksum-SHA256', self.header_checksum or material['view']['sha256'])
        if self.close_delimited:
            handler.send_header('Connection', 'close')
        elif self.chunked:
            handler.send_header('Transfer-Encoding', 'chunked')
        elif not self.open_ended:
            handler.send_header('Content-Length', str(len(body)))
        handler.end_headers()
        handler.close_connection = self.close_delimited or handler.close_connection
        with self.lock:
            budget = self.cuts.pop(0) if self.cuts else None
        sent = 0
        while sent < len(body):
            if self.stall_after is not None and sent >= self.stall_after:
                self.stall_after = None
                self.released.wait(30)          # no more bytes: the desktop has to give up on its own
                break
            if self.stall_every is not None and sent >= self.stall_every:
                self.released.wait(30)
                break
            step = min(4096, len(body) - sent, *(() if budget is None else (budget - sent,)))
            if step <= 0:
                break
            piece = body[sent:sent + step]
            try:
                handler.wfile.write(b'%x\r\n%s\r\n' % (len(piece), piece) if self.chunked else piece)
                handler.wfile.flush()
            except OSError:
                break
            sent += step
            with self.lock:
                self.served += step
        if sent == len(body) and self.chunked:
            handler.wfile.write(b'0\r\n\r\n')
        if sent == len(body) and self.open_ended:
            self.released.wait(30)              # the whole file went out, and the connection stays open
        if sent < len(body):
            if budget is not None and self.offline_after_cut:
                self.offline = True
            handler.close_connection = True
            try:
                handler.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class CuttingRelay:
    """A relay in front of a cloud. ``cuts`` are budgets for the next connections that ask for the
    content of a material: once such a connection carried that many bytes towards the desktop it
    is closed. Every other connection passes untouched."""

    def __init__(self, upstream_port, cuts=()):
        self.upstream, self.cuts, self.closed, self.sockets = upstream_port, list(cuts), False, []
        self.content_connections = 0
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen()
        self.socket.settimeout(.2)
        self.port = self.socket.getsockname()[1]
        self.url = 'http://127.0.0.1:%d' % self.port
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed:
            try:
                client, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        try:
            client.settimeout(10)
            head = client.recv(65536)
            if not head:
                client.close()
                return
            budget = None
            if b'/content' in head.split(b'\r\n', 1)[0]:
                self.content_connections += 1
                budget = self.cuts.pop(0) if self.cuts else None
            cloud = socket.create_connection(('127.0.0.1', self.upstream), timeout=5)
            cloud.settimeout(None)
            client.settimeout(None)
            self.sockets += [client, cloud]
            cloud.sendall(head)
        except OSError:
            client.close()
            return
        threading.Thread(target=self._copy, args=(client, cloud, None), daemon=True).start()
        self._copy(cloud, client, budget)

    @staticmethod
    def _copy(source, sink, budget):
        try:
            while budget is None or budget > 0:
                data = source.recv(4096 if budget is None else min(4096, budget))
                if not data:
                    break
                sink.sendall(data)
                budget = None if budget is None else budget - len(data)
        except OSError:
            pass
        finally:
            for end in (source, sink):
                try:
                    end.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                end.close()

    def close(self):
        self.closed = True
        self.socket.close()
        for end in self.sockets:
            try:
                end.close()
            except OSError:
                pass


def wait_until(condition, seconds=20, step=.02):
    """True once ``condition()`` holds; False when it did not within ``seconds``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return bool(condition())
