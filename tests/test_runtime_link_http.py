"""Issue #17 over HTTP: the routes the page uses to link a robot's management layer, what they answer
when it cannot be reached, and that the page's own script and its logic checks are in place.

The robot is a local SSH protocol fixture with a stand-in of the management layer; nothing reaches a
real machine. The rules themselves: tests/test_runtime_link.py and tests/test_runtime_link_reconnect.py.
"""
import json
import secrets
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from brainos import runtime_link
from brainos.server import Application, make_handler
from management_fixture import ManagementLayer
from runtime_link_support import PORT, LoopbackDevice
from ssh_fixture import DEFAULT_LOGIN
from test_product import Server

ROOT = Path(__file__).resolve().parents[1]


class HttpCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.application = Application(tmp.name)
        self.addCleanup(self.application.close)
        self.server = Server(make_handler(self.application))
        self.addCleanup(self.server.close)
        # A throwaway account, made up on every run.
        form = {'name': 'QA', 'email': f'qa-{secrets.token_hex(4)}@example.test', 'password': secrets.token_hex(8) + 'a1'}
        _, started, _ = self.post('register/start', form)
        _, user, cookie = self.post('register', {**form, 'code': started['dev_code']})
        self.cookie = cookie.split(';')[0]
        self.workspace = self.application.workspace(user['user'])
        self.pid = self.workspace.add_project({'name': 'P', 'objective': 'Reach the management layer over SSH'})['id']
        self.workspace.runtime_link.stop_timer()
        self.robot = LoopbackDevice()
        self.addCleanup(self.robot.close)
        self.layer = ManagementLayer()
        self.addCleanup(self.layer.close)
        self.robot.serve_on_loopback(PORT, self.layer.port)
        self.robot.command_outputs[runtime_link.TOKEN_COMMAND] = (self.layer.token + '\n').encode()
        engine = self.workspace.engineering
        check = engine.scan({'project_id': self.pid, 'host': '127.0.0.1', 'port': self.robot.port, 'username': 'robot'})
        login = {'project_id': self.pid, 'host_check_id': check['id'], 'trust_fingerprint': check['fingerprint'],
                 'auth': 'password', 'name': 'Robot on the LAN', 'family': 'quadruped'}
        login['password'] = DEFAULT_LOGIN[1]
        self.robot_id = engine.connect(login)['robot_id']

    def request(self, path, payload=None, cookie=None):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {'Cookie': self.cookie if cookie is None else cookie}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.server.url + path, data=data, headers=headers)
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def post(self, path, payload, cookie=None):
        status, body, headers = self.request('/api/' + path, payload, getattr(self, 'cookie', '') if cookie is None else cookie)
        return status, json.loads(body), headers.get('Set-Cookie', '')

    def ask(self, path='robots/runtime-link', **fields):
        status, body, _ = self.post(path, {'project_id': self.pid, 'robot_id': self.robot_id, **fields})
        return status, body


class RouteTests(HttpCase):
    def test_linking_checking_the_state_and_taking_the_link_away(self):
        status, robot = self.ask()

        self.assertEqual((status, robot['status'], robot['endpoint']), (200, 'connected', runtime_link.endpoint_of(self.robot_id)))
        self.assertNotIn('token', robot)
        self.assertNotIn(self.layer.token, json.dumps(robot))
        status, state = self.post('robots/runtime-link/state', {})[:2]
        self.assertEqual((status, state['links'][self.robot_id]['state'], state['links'][self.robot_id]['port']), (200, 'up', PORT))
        status, retried = self.ask('robots/runtime-link/retry')
        self.assertEqual((status, retried['link']['state']), (200, 'up'))
        status, snapshot, _ = self.request('/api/state')
        shown = next(r for r in json.loads(snapshot)['robots'] if r['id'] == self.robot_id)
        self.assertEqual((shown['has_token'], shown['runtime_link']['port']), (True, PORT))
        self.assertNotIn(self.layer.token, snapshot.decode())
        status, robot = self.ask('robots/runtime-link/remove')
        self.assertEqual((status, robot['endpoint'], robot['status']), (200, '', 'unconnected'))
        self.assertEqual(self.post('robots/runtime-link/state', {})[1], {'links': {}})

    def test_a_refusal_comes_with_its_code(self):
        self.robot.stop_serving(PORT)

        status, body = self.ask()
        self.assertEqual((status, body['code']), (400, 'device_service_down'))

        self.robot.command_outputs[runtime_link.TOKEN_COMMAND] = b''
        status, body = self.ask('robots/runtime-link/remove')
        self.assertEqual(status, 200)
        status, body = self.ask()
        self.assertEqual((status, body['code']), (400, 'runtime_token_not_found'))
        self.assertNotIn(self.layer.token, json.dumps(body))

    def test_an_address_or_a_privileged_port_is_refused(self):
        for fields in ({'host': '10.0.0.5'}, {'endpoint': 'http://10.0.0.5:8790'}, {'port': 22}, {'port': '8790'}):
            with self.subTest(fields=fields):
                status, body = self.ask(**fields)
                self.assertEqual(status, 400, body)
        self.assertEqual(self.robot.loopback_channels, [])

    def test_nobody_else_gets_at_the_routes(self):
        for path in ('robots/runtime-link', 'robots/runtime-link/remove', 'robots/runtime-link/state', 'robots/runtime-link/retry'):
            with self.subTest(path=path):
                status, body, _ = self.post(path, {'project_id': self.pid, 'robot_id': self.robot_id}, cookie='brain_session=nobody')
                self.assertEqual((status, body.get('code')), (401, 'unauthorized'))
        self.assertEqual(self.robot.loopback_channels, [])

    def test_the_page_gets_its_script_and_style(self):
        status, page, _ = self.request('/')
        self.assertEqual(status, 200)
        for name, mime in (('runtime_link.js', 'text/javascript'), ('runtime_link.css', 'text/css')):
            self.assertIn(f'/{name}'.encode(), page)
            status, body, headers = self.request('/' + name)
            self.assertEqual((status, body), (200, (ROOT / 'web' / name).read_bytes()))
            self.assertTrue(headers['Content-Type'].startswith(mime))
        # It wraps what heartbeat.js and ssh_edit.js wrapped: loaded after both.
        text = page.decode()
        self.assertLess(text.index('/heartbeat.js'), text.index('/runtime_link.js'))
        self.assertLess(text.index('/ssh_edit.js'), text.index('/runtime_link.js'))


class PageLogicTests(unittest.TestCase):
    def test_the_logic_of_the_page_script(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        result = subprocess.run([node, 'tests/runtime_link_logic.cjs'], capture_output=True, text=True,
                                timeout=120, cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('runtime link logic checks passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
