"""A real NeuxMind cloud app (cloud/) on a local uvicorn server, for desktop tests.

Test double only: SQLite in a temp dir, ``NEUXMIND_ENV=test``, the Echo
upstream (``simulated: true`` everywhere). Accounts are created through the
real HTTP API (register → dev verification token → verify), the operator is
allow-listed in the test settings. Importing this module raises ImportError
when the cloud's dependencies (FastAPI, SQLAlchemy, uvicorn, httpx) are not
installed, so callers can skip.
"""
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for path in (str(REPO / 'cloud'), str(REPO / 'spec')):
    if path not in sys.path:
        sys.path.insert(0, path)

import uvicorn  # noqa: E402  (ImportError → caller skips)
from neuxmind_cloud.app import create_app  # noqa: E402
from neuxmind_cloud.config import Settings  # noqa: E402

PASSWORD = 'correct-horse-battery'
OPERATOR = 'ops@neuxmind.test'
RECORDED = json.loads((REPO / 'cloud/tests/fixtures/codex-0.146.1-responses.json').read_text())
TOOLS_MODEL = {'id': 'echo-tools', 'name': 'Echo 工程模型（测试替身）', 'source': 'closed', 'kind': 'llm',
               'license': 'test only', 'pricing': {'type': 'per_1k_tokens', 'credits': 10},
               'upstream': {'provider': 'echo', 'behavior': 'tools'}}


class CloudServer:
    def __init__(self, models=(TOOLS_MODEL,), **settings):
        self.tmp = tempfile.mkdtemp(prefix='nx-desktop-cloud-')
        self.settings = Settings(env='test', database_url=f'sqlite:///{self.tmp}/cloud.sqlite3',
                                 operator_emails=[OPERATOR], scrypt_log_n=14, register_ip_max_per_hour=10_000,
                                 login_max_failures=50, **settings)
        self.app = create_app(self.settings)
        config = uvicorn.Config(self.app, host='127.0.0.1', port=0, log_level='warning', lifespan='off',
                                loop='asyncio', http='h11')
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError('cloud test server did not start')
            time.sleep(0.01)
        self.url = f'http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}'
        # public_base_url stays at its default (another port): the desktop must build
        # the session base_url from the address it is configured with (§4.3).
        self.operator = self.register(OPERATOR)
        for card in models:
            self.call('POST', '/admin/models', card, self.operator)

    # ------------------------------------------------------------ HTTP
    def call(self, method, path, body=None, user=None, expect=None):
        headers = {'Content-Type': 'application/json'}
        if user:
            headers['Authorization'] = 'Bearer ' + user['token']
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + '/api/v1' + path, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=20) as r:
                status, result = r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as exc:
            status, result = exc.code, json.loads(exc.read() or b'{}')
        if expect is not None and status != expect:
            raise AssertionError(f'{method} {path}: {status} {result}')
        if expect is None and status >= 400:
            raise AssertionError(f'{method} {path}: {status} {result}')
        return result

    def register(self, email, verify=True):
        user = self.call('POST', '/auth/register', {'email': email, 'password': PASSWORD})
        if verify:
            self.call('POST', '/auth/verify-email', {'token': user['dev_verification_token']}, user)
        user['org_id'] = self.call('GET', '/me', None, user)['current_org_id']
        return user

    def customer(self, email, credits=10_000, entitle=('echo-tools',), verify=True):
        user = self.register(email, verify=verify)
        for model in entitle:
            self.call('POST', '/purchases', {'kind': 'model_license', 'model_id': model}, user, expect=201)
        if credits:
            self.call('POST', f"/admin/wallets/{user['org_id']}/adjust", {'delta': credits, 'reason': 'test'},
                      self.operator)
        return user

    def wallet(self, user):
        return self.call('GET', '/wallet', None, user)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)
        shutil.rmtree(self.tmp, ignore_errors=True)
