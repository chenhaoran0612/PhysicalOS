"""Stand-in for the management layer on a robot's own loopback address (issue #17, R1-09). Test only.

It answers what the console's connection check reads (``GET /v1/manifest``, ``GET /v1/status``), the v2
pages of a robot's detail (``/v2/manifest``, ``/v2/skills``) and ``POST /v1/echo``, which says how many
bytes it was sent. Nothing moves. The access token is made up for every instance, the way the install
script makes one up on a device.

``hold`` (an event, set by default): while it is cleared a request that has arrived gets no answer yet,
which is a request under way; ``waiting`` counts those.
"""
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler

from brainos.httpd import FastBindHTTPServer

CAPABILITIES = ['safety.stop', 'system.health', 'base.velocity']
HOLD_LIMIT_SECONDS = 30              # a held request is let go after this long, whatever the test does


class ManagementLayer:
    def __init__(self, instance=None):
        self.token = secrets.token_urlsafe(36)
        self.instance = instance or 'layer-' + secrets.token_hex(3)
        self.calls = []                  # 'GET /v1/status', in the order they arrived
        self.tokens = []                 # the Authorization header of every request
        self.peers = []                  # where every request came from
        self.hold = threading.Event()
        self.hold.set()
        self.waiting = 0
        self.lock = threading.Lock()
        self.closed = False
        self.http = FastBindHTTPServer(('127.0.0.1', 0), self._handler())
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.http.server_port

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.hold.set()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(5)

    def manifest_v1(self):
        return {'schema': 'robot.adapter.v1', 'instance_id': self.instance, 'name': '管理层测试替身',
                'family': 'quadruped', 'adapter_version': '0.1.0', 'capabilities': list(CAPABILITIES),
                'simulated': True, 'vendor': 'Fixture', 'model': 'forward-double'}

    def manifest_v2(self):
        return {'schema': 'robot.adapter.v2', 'spec_version': '1.0', 'instance_id': self.instance,
                'name': '管理层测试替身', 'adapter_version': '0.1.0', 'vendor': 'Fixture', 'model': 'forward-double',
                'family': 'quadruped', 'simulated': True,
                'capabilities': [{'id': 'safety.stop', 'params': {'hardware_estop': True}}, {'id': 'system.health'},
                                 {'id': 'base.velocity', 'params': {'max_vx': 1.0, 'max_vy': 0.0, 'max_wz': 1.5,
                                                                    'holonomic': False}}],
                'model_runtimes': []}

    def status(self):
        return {'instance_id': self.instance, 'mode': 'idle', 'estop_latched': False, 'latch_reason': None,
                'simulated': True}

    def _handler(self):
        layer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, status=200):
                data = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def arrived(self, method):
                """Note the request, wait while the layer is held, and say whether its token is the right one."""
                with layer.lock:
                    layer.calls.append(method + ' ' + self.path)
                    layer.tokens.append(self.headers.get('Authorization'))
                    layer.peers.append(self.client_address)
                    layer.waiting += 1
                try:
                    layer.hold.wait(HOLD_LIMIT_SECONDS)
                finally:
                    with layer.lock:
                        layer.waiting -= 1
                if self.headers.get('Authorization') == 'Bearer ' + layer.token:
                    return True
                self.send({'error': 'unauthorized'}, 401)
                return False

            def do_GET(self):
                if not self.arrived('GET'):
                    return None
                answers = {'/v1/manifest': layer.manifest_v1, '/v2/manifest': layer.manifest_v2,
                           '/v1/status': layer.status, '/v2/skills': lambda: {'skills': []}}
                if self.path in answers:
                    return self.send(answers[self.path]())
                return self.send({'error': 'not_found', 'code': 'not_found'}, 404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if not self.arrived('POST'):
                    return None
                if self.path == '/v1/echo':
                    return self.send({'bytes': len(body)})
                return self.send({'error': 'not_found', 'code': 'not_found'}, 404)

        return Handler
