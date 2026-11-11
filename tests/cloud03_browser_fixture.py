"""Browser acceptance fixture for round 03 (contract §5.3): the engineering
assistant on cloud-platform models.

Serves the desktop app together with the real cloud app (tests/cloud_fixture:
local uvicorn, SQLite, ``NEUXMIND_ENV=test``) whose models are **Echo test
doubles** (``simulated: true``):

* ``echo-code``     — tool loop: calls ``exec_command`` to write adapter.py, then answers;
* ``echo-discover`` — tool loop: calls ``device_overview``, then answers;
* ``echo-overrun``  — reports far more usage than reserved (overage lock).

The engineering runtime is the real Codex 0.146.1 app-server when one is
available (packaged, or ``NEUXMIND_TEST_AGENT_BINARY``); otherwise the
deterministic app-server stand-in tests/codex_standin.py (printed as
``runtime``). SSH discovery uses a stub device (no SSH, no robot).

A cloud account ``cloud03@example.test`` (verified, 5000 credits, entitled to
all three models) is created at start.

Usage: python3 tests/cloud03_browser_fixture.py [config.json]
"""
import json
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import agent_binary  # noqa: F401
import cloud_fixture
from codex_standin import CodexStandIn
import test_cloud_engineering as tce
from brainos import agent_runtime
from brainos.server import Application, make_handler
from test_product import Server

EMAIL = 'cloud03@example.test'

if __name__ == '__main__':
    config_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-cloud03-qa.json'
    tmp = tempfile.TemporaryDirectory()
    cloud = cloud_fixture.CloudServer(models=(tce.CODE_LOOP, tce.DISCOVERY_LOOP, tce.OVERRUN))
    customer = cloud.customer(EMAIL, credits=5000, entitle=('echo-code', 'echo-discover', 'echo-overrun'))
    patches = [patch('brainos.device_access.SSHDevice', tce.Device)]
    # ux03: without the pinned Codex binary, use the deterministic app-server stand-in, which runs the
    # model's tool calls like Codex does (the old replay never ran them, so no artifact ever appeared).
    runtime = 'codex-' + agent_runtime.RUNTIME_VERSION if agent_runtime.binary_path() else 'standin'
    if runtime == 'standin':
        real_available = agent_runtime.available
        patches += [patch.object(agent_runtime, 'AgentProcess', CodexStandIn),
                    patch.object(agent_runtime, 'available', lambda: {**real_available(), 'available': True})]
    for p in patches:
        p.start()
    app = Application(tmp.name + '/app')

    class Handler(make_handler(app)):
        def workspace(self):
            user = app.identity.user(self.token())
            return app.workspace(user) if user else None

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == '/fixture/seed-ssh':
                platform = self.workspace()
                project = parse_qs(url.query)['project'][0]
                robot = platform.add_robot({'project_id': project, 'name': '园区四足（SSH 替身）', 'family': 'quadruped'})
                platform.store.put('device_connections', {
                    'id': 'connection-fixture', 'project_id': project, 'robot_id': robot['id'], 'host': '192.0.2.10',
                    'port': 22, 'username': 'robot', 'auth': 'password', 'fingerprint': 'SHA256:fixture',
                    'read_roots': ['/opt/ros'], 'kind': 'ssh'})
                platform.store.put('agent_secrets', {'id': 'connection-fixture', 'password': 'fixture'})
                r = platform.store.get('robots', robot['id'])
                r.update(connection_id='connection-fixture', onboarding_status='connected_for_discovery')
                platform.store.put('robots', r)
                return self.respond({'robot_id': robot['id']})
            if url.path == '/fixture/state':
                platform = self.workspace()
                commerce = app.commerce
                return self.respond({
                    'cloud_wallet': cloud.wallet(customer),
                    'local_ledger_entries': len(commerce.store.list('ledger')),
                    'local_reservations': len(commerce.store.list('reservations')),
                    'sessions': [{k: s.get(k) for k in ('name', 'status', 'metering_status', 'cloud_session')}
                                 for s in platform.store.list('agent_sessions')] if platform else []})
            return super().do_GET()

    server = Server(Handler)
    config = {'url': server.url, 'cloud': cloud.url, 'email': EMAIL, 'password': cloud_fixture.PASSWORD,
              'runtime': runtime}
    with open(config_path, 'w') as f:
        json.dump(config, f)
    print(json.dumps(config), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        app.close()
        cloud.stop()
        for p in patches:
            p.stop()
        tmp.cleanup()
