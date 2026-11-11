"""Shared fixtures for round 02 desktop tests (INTERFACES §4.1, §4.5).

* ``runtime_support()`` — the real runtime test helpers (simulated bodies
  served by the real runtime HTTP gateway).
* ``CloudServer`` — the real cloud FastAPI app (test environment) served by
  uvicorn on a random loopback port.
* ``AsyncDevice`` — a **test-only protocol double** of the §4.1 asynchronous
  on-site test (``POST /v1/evaluate`` with ``async: true`` and ``GET
  /v1/evaluations/{id}``). It moves nothing and labels its evidence honestly.
  It stands in for the runtime until the runtime implementation lands; the
  same client tests also run against the real runtime when it supports §4.1.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for path in (REPO / 'spec', REPO / 'cloud'):
    if str(path) not in sys.path:
        sys.path.append(str(path))

from brainos.service import digest  # noqa: E402

OPERATOR_EMAIL = 'ops@desktop.test'
PASSWORD = 'desktop-test-password-1'


def runtime_support():
    """Import ``runtime/tests/_support.py`` (raises when the runtime is unavailable)."""
    runtime_tests = REPO / 'runtime' / 'tests'
    if str(runtime_tests) not in sys.path:
        sys.path.insert(0, str(runtime_tests))
    import _support
    return _support


def runtime_supports_async_evaluation():
    """True once the runtime implements INTERFACES §4.1."""
    try:
        support = runtime_support()
    except Exception:
        return False
    core = (REPO / 'runtime' / 'neuxmind_runtime' / 'core.py').read_text(encoding='utf-8')
    gateway = (REPO / 'runtime' / 'neuxmind_runtime' / 'gateway.py').read_text(encoding='utf-8')
    return support is not None and 'evaluation_id' in core and '/v1/evaluations' in gateway


class CloudServer:
    """The real cloud app (env=test, log mail sender) on a random loopback port."""

    def __init__(self):
        import uvicorn
        from neuxmind_cloud.app import create_app
        from neuxmind_cloud.config import Settings

        self._tmp = tempfile.mkdtemp(prefix='nx-desktop-cloud-')
        settings = Settings(env='test', database_url=f'sqlite:///{self._tmp}/cloud.sqlite3',
                            operator_emails=[OPERATOR_EMAIL], register_ip_max_per_hour=10_000, scrypt_log_n=14)
        self.app = create_app(settings)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host='127.0.0.1', port=0, log_level='warning',
                                                    lifespan='off'))
        self.server.install_signal_handlers = lambda: None
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError('cloud server did not start')
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'

    def api(self, method, path, payload=None, token=None):
        import urllib.error
        import urllib.request
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url + '/api/v1' + path, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b'{}')

    def operator_token(self):
        """A verified platform operator (allow-listed address, test environment)."""
        status, body = self.api('POST', '/auth/register', {'email': OPERATOR_EMAIL, 'password': PASSWORD})
        if status != 201:
            status, body = self.api('POST', '/auth/login', {'email': OPERATOR_EMAIL, 'password': PASSWORD})
            return body['token']
        token = body['token']
        self.api('POST', '/auth/verify-email', {'token': body['dev_verification_token']}, token)
        return token

    def seed_models(self):
        """Three catalogue models: one for any body, one for quadrupeds, one arm-only."""
        token = self.operator_token()
        models = [
            {'id': 'echo-llm', 'name': '通用对话模型（测试替身）', 'source': 'closed', 'kind': 'llm',
             'license': 'test only', 'pricing': {'type': 'per_1k_tokens', 'credits': 10},
             'upstream': {'provider': 'echo'}},
            {'id': 'dog-nav-vla', 'name': '四足导航策略（测试替身）', 'source': 'native', 'kind': 'vla',
             'license': 'test only', 'pricing': {'type': 'per_call', 'credits': 2},
             'requires_capabilities': ['base.velocity'], 'families': ['quadruped'],
             'upstream': {'provider': 'echo'}},
            {'id': 'arm-grasp-policy', 'name': '机械臂抓取策略（测试替身）', 'source': 'open', 'kind': 'policy',
             'license': 'test only', 'pricing': {'type': 'free', 'credits': 0},
             'requires_capabilities': ['arm.joint_position', 'gripper.position'], 'families': ['manipulator'],
             'package': {'ref': 'https://example.test/arm.onnx', 'sha256': '0' * 64, 'runtime': 'onnxruntime'}},
        ]
        for model in models:
            status, body = self.api('POST', '/admin/models', model, token)
            if status not in (201, 409):
                raise RuntimeError(f'seed model failed: {status} {body}')

    def close(self):
        self.server.should_exit = True
        self.thread.join(10)
        self.app.state.ctx.db.engine.dispose()
        shutil.rmtree(self._tmp, ignore_errors=True)


class AsyncDevice:
    """Protocol double of a robot runtime that supports §4.1 asynchronous on-site tests."""

    TOKEN = 'round02-device-token-0123456789'

    def __init__(self, simulated=False, supports_async=True):
        self.simulated = simulated
        self.supports_async = supports_async
        self.latched = False
        self.calls = []
        self.evaluations = {}           # evaluation_id -> {'status', 'result', 'request'}
        self.by_request = {}            # request_id -> evaluation_id
        self.poll_failures = 0          # answer the next N status reads with HTTP 503
        self.force_status = None        # report this status string instead (contract violation tests)
        self.lock = threading.Lock()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.http.server_port}'

    # ------------------------------------------------------------ manifests
    def manifest_v1(self):
        return {'schema': 'robot.adapter.v1', 'instance_id': 'async-dog', 'name': '异步测试替身', 'family': 'quadruped',
                'adapter_version': '0.2.0', 'capabilities': ['safety.stop', 'system.health', 'base.velocity',
                                                             'base.navigate_to'],
                'simulated': self.simulated, 'vendor': 'Fixture', 'model': 'async-double'}

    def manifest_v2(self):
        return {'schema': 'robot.adapter.v2', 'spec_version': '1.0', 'instance_id': 'async-dog',
                'name': '异步测试替身', 'adapter_version': '0.2.0', 'vendor': 'Fixture', 'model': 'async-double',
                'family': 'quadruped', 'simulated': self.simulated,
                'capabilities': [{'id': 'safety.stop', 'params': {'hardware_estop': True}}, {'id': 'system.health'},
                                 {'id': 'base.velocity', 'params': {'max_vx': 0.8, 'max_vy': 0.0, 'max_wz': 1.0,
                                                                    'holonomic': False}},
                                 {'id': 'base.navigate_to', 'params': {}}],
                'model_runtimes': []}

    # ------------------------------------------------------------- control
    def result(self, request, passed, reason=None):
        app = request['application']
        cases = [{'name': f'步骤 {i + 1}', 'passed': passed, 'detail': reason or ('完成' if passed else '未完成')}
                 for i, _ in enumerate(app['steps'])]
        scheme = 'simulation' if self.simulated else 'fixture'
        return {'passed': passed, 'status': 'succeeded' if passed else 'failed',
                'application_digest': app['digest'], 'robot_manifest_digest': digest(self.manifest_v1()),
                'evidence_ref': f'{scheme}://async-dog/runs/{uuid.uuid4().hex[:8]}',
                'evidence_kind': 'simulation' if self.simulated else 'device_run',
                'simulated': self.simulated, 'cases': cases}

    def finish(self, passed=True):
        with self.lock:
            for item in self.evaluations.values():
                if item['status'] == 'running':
                    item.update(status='completed', result=self.result(item['request'], passed))

    def lose_all(self):
        """As after a runtime restart: unfinished evaluations become unknown."""
        with self.lock:
            for item in self.evaluations.values():
                if item['status'] == 'running':
                    item['status'] = 'unknown'

    def close(self):
        self.http.shutdown()
        self.http.server_close()

    # -------------------------------------------------------------- HTTP
    def _handler(self):
        device = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, status=200):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def ok_token(self):
                if self.headers.get('Authorization') != 'Bearer ' + device.TOKEN:
                    self.send({'error': 'unauthorized', 'code': 'unauthorized'}, 401)
                    return False
                return True

            def do_GET(self):
                if not self.ok_token():
                    return
                device.calls.append('GET ' + self.path)
                if self.path == '/v1/manifest':
                    return self.send(device.manifest_v1())
                if self.path == '/v2/manifest':
                    return self.send(device.manifest_v2())
                if self.path == '/v1/status':
                    return self.send({'instance_id': 'async-dog', 'mode': 'idle', 'estop_latched': device.latched,
                                      'latch_reason': '桌面端请求停止' if device.latched else None,
                                      'simulated': device.simulated})
                if self.path.startswith('/v1/evaluations/') and device.supports_async:
                    if device.poll_failures > 0:
                        device.poll_failures -= 1
                        return self.send({'error': 'busy', 'code': 'busy'}, 503)
                    key = self.path.rsplit('/', 1)[1]
                    with device.lock:
                        item = device.evaluations.get(key)
                        if not item:
                            return self.send({'error': 'not found', 'code': 'not_found'}, 404)
                        body = {'evaluation_id': key, 'status': device.force_status or item['status']}
                        if item['status'] == 'completed':
                            body['result'] = item['result']
                    return self.send(body)
                return self.send({'error': '未知接口', 'code': 'not_found'}, 404)

            def do_POST(self):
                if not self.ok_token():
                    return
                length = int(self.headers.get('Content-Length', '0'))
                payload = json.loads(self.rfile.read(length) or b'{}')
                device.calls.append('POST ' + self.path)
                if self.path == '/v1/validate':
                    cases = [{'name': '急停未锁存', 'passed': not device.latched,
                              'detail': '急停已锁存' if device.latched else '可运动'}]
                    return self.send({'accepted': not device.latched, 'passed': not device.latched, 'cases': cases})
                if self.path == '/v1/evaluate':
                    if payload.get('hardware_authorized') is not True:
                        return self.send({'error': 'not authorized', 'code': 'hardware_not_authorized'}, 403)
                    if device.latched:
                        return self.send({'error': 'estop', 'code': 'estop_latched'}, 409)
                    if not (device.supports_async and payload.get('async') is True):
                        return self.send(device.result(payload, True))       # legacy synchronous behaviour
                    with device.lock:
                        key = device.by_request.get(payload['request_id'])
                        if key is None:
                            key = 'eval-' + uuid.uuid4().hex[:10]
                            device.by_request[payload['request_id']] = key
                            device.evaluations[key] = {'status': 'running', 'result': None, 'request': payload}
                    return self.send({'accepted': True, 'evaluation_id': key, 'status': 'running'})
                if self.path == '/v1/stop':
                    with device.lock:
                        device.latched = True
                        for item in device.evaluations.values():
                            if item['status'] == 'running':
                                item.update(status='completed',
                                            result=device.result(item['request'], False, '收到停止请求，测试中止'))
                    return self.send({'accepted': True, 'stopped': True, 'latched': True})
                if self.path == '/v1/reset':
                    if payload.get('hardware_authorized') is not True:
                        return self.send({'error': 'not authorized', 'code': 'hardware_not_authorized'}, 403)
                    device.latched = False
                    return self.send({'latched': False})
                if self.path == '/v1/deploy':
                    return self.send({'accepted': True, 'release_id': 'async-release-1'})
                if self.path == '/v1/start':
                    if device.latched:
                        return self.send({'error': 'estop', 'code': 'estop_latched'}, 409)
                    return self.send({'accepted': True, 'state': 'running'})
                return self.send({'error': '未知接口', 'code': 'not_found'}, 404)

        return Handler


def write_skill(root, skill_id='desk.hello', requires=('system.health',), constraints=None, files=None,
                version='0.1.0', title='桌面测试技能'):
    """Create a skill folder on disk and return its path."""
    folder = Path(os.path.realpath(root)) / skill_id      # macOS: /var -> /private/var is a link
    folder.mkdir(parents=True, exist_ok=True)
    meta = {'schema': 'skill.package.v1', 'id': skill_id, 'version': version, 'title': title,
            'description': '用于桌面端测试的技能包', 'entry': 'skill.py:run',
            'requires': [{'id': r} for r in requires], 'parameters': [], 'timeout_s': 30}
    if constraints:
        meta['constraints'] = constraints
    (folder / 'skill.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    for name, text in (files or {'skill.py': 'def run(ctx):\n    return {"ok": True}\n'}).items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    return str(folder)


def copy_example(root, name):
    """Copy a runtime example skill (patrol / wave / status_report) to a temp folder."""
    target = Path(os.path.realpath(root)) / name
    shutil.copytree(REPO / 'runtime' / 'examples' / 'skills' / name, target)
    return str(target)


def env_without_proxy():
    """Loopback requests in tests must not go through the agent proxy."""
    env = dict(os.environ)
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        env.pop(key, None)
    return env
