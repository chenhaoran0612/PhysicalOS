"""A local stand-in for the TokenFans endpoints the desktop app uses (never the paid service).

Shapes follow the TokenFans console and API docs (https://tokenfans.ai/integrations/api):

* ``POST /auth/login`` {email, password} -> {token, user}
* ``GET /auth/my-key`` (Bearer console token) -> {key}
* ``GET /v1/me/billing/balance`` (Bearer console token) -> {data: {balance_minor, currency}}
* ``POST /auth/logout`` (Bearer console token)
* ``GET /codex/v1/models`` (Bearer API key) -> {object: "list", data: [{id}, ...]}
"""
import json
import secrets
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Fake credentials, freshly random for every run: nothing credential-like is committed.
EMAIL = 'robot@example.test'
PASSWORD = 'pw-' + secrets.token_hex(8)
KEYLESS_EMAIL = 'keyless@example.test'
CONSOLE_TOKEN = 'console-' + secrets.token_hex(12)
KEYLESS_TOKEN = 'keyless-' + secrets.token_hex(12)
API_KEY = 'sk-tkf-' + secrets.token_hex(16)
MODELS = ['gpt-5.5-codex', 'kimi-k2.6']


class _QuickServer(ThreadingHTTPServer):
    # Skip the reverse DNS lookup HTTPServer.server_bind does (slow on some Macs).
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class TokenFansFixture:
    def __init__(self):
        self.requests = []
        self.models = list(MODELS)
        self.deep_logout = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _bearer(self):
                value = self.headers.get('Authorization', '')
                return value[7:] if value.startswith('Bearer ') else ''

            def _record(self, body=None):
                fixture.requests.append({'method': self.command, 'path': self.path,
                                         'authorization': self.headers.get('Authorization'), 'body': body})

            def do_GET(self):
                self._record()
                token = self._bearer()
                if self.path == '/codex/v1/models':
                    if token != API_KEY:
                        return self._send(401, {'error': {'message': 'unauthorized', 'type': 'unauthorized'}})
                    return self._send(200, {'object': 'list', 'data': [{'id': m, 'object': 'model'} for m in fixture.models]})
                if self.path == '/auth/my-key':
                    if token == CONSOLE_TOKEN:
                        return self._send(200, {'key': API_KEY})
                    if token == KEYLESS_TOKEN:
                        return self._send(200, {'key': None})
                    return self._send(401, {'error': 'Unauthorized'})
                if self.path == '/v1/me/billing/balance':
                    if token not in (CONSOLE_TOKEN, KEYLESS_TOKEN):
                        return self._send(401, {'error': 'Unauthorized'})
                    return self._send(200, {'data': {'balance_minor': '12345', 'currency': 'CNY'}})
                return self._send(404, {'error': 'not_found'})

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length) or b'{}')
                self._record(body)
                if self.path == '/auth/login':
                    if body.get('email') == EMAIL and body.get('password') == PASSWORD:
                        return self._send(200, {'token': CONSOLE_TOKEN, 'user': {'email': EMAIL}})
                    if body.get('email') == KEYLESS_EMAIL and body.get('password') == PASSWORD:
                        return self._send(200, {'token': KEYLESS_TOKEN, 'user': {'email': KEYLESS_EMAIL}})
                    return self._send(401, {'error': 'Invalid email or password'})
                if self.path == '/auth/logout':
                    if fixture.deep_logout:     # absurdly nested JSON (RecursionError in json.loads)
                        raw = b'[' * 200000 + b']' * 200000
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(raw)))
                        self.end_headers()
                        return self.wfile.write(raw)
                    return self._send(200, {'ok': True})
                return self._send(404, {'error': 'not_found'})

        self.http = _QuickServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.http.server_port}'

    def paths(self):
        return [r['path'] for r in self.requests]

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
