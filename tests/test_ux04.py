"""ux04 (round 06): structured errors, the reset way out, parameter refusals, plain first screens.

* Q5-04: every error code in runtime/, cloud/, sdk/ and brainos/ has its own plain explanation
  (tests/ux04_errors.cjs, table-driven, run here through node); the desktop passes ``code`` and the
  device's HTTP status on instead of flattening them.
* Q5-03: a refused reset (409 ``reset_in_progress``) is recorded as what it is, with the device's own
  reason -- never "无法连接接入服务" or "请检查地址、凭据与协议"; a reset that does not answer in time is
  checked against /v1/status; restarting a simulated robot keeps the latch.
* Q5-05: a device's field-level refusal (``param``, ``min``, ``max``) reaches the browser.
* Q5-12: capability IDs in skill descriptions get Chinese names; the trial project is marked as such.

Device doubles are test-only protocol fixtures; the restart test uses a real simulated quadruped.
"""
import json
import os
os.environ.setdefault('NEUXMIND_MAIL_DEV_ECHO', '1')  # registration codes come back in the API response
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import connector, robot_caps, service as service_module  # noqa: E402
from brainos.edge import Gateway, make_handler as edge_handler  # noqa: E402
from brainos.errors import CodedError, error_payload, refusal_fields  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from brainos.service import Platform  # noqa: E402
from test_product import Server  # noqa: E402
from test_sim_evidence_and_reset import TOKEN, LatchingDevice  # noqa: E402

CODE_PATTERNS = [r'code\s*=\s*["\']([a-z][a-z0-9_]+)["\']', r'["\']code["\']\s*:\s*["\']([a-z][a-z0-9_]+)["\']',
                 r'status, code = \d+, "([a-z_]+)"', r'return \(?"([a-z]+_[a-z_]+)",',
                 r'CodedError\([^\n]*?, \'([a-z_]+)\'\)']


def known_codes():
    """Every stable error code the desktop can be shown (runtime, cloud, sdk, desktop)."""
    codes = set()
    for root in ('runtime/neuxmind_runtime', 'cloud', 'sdk/neuxmind', 'brainos'):
        for path in (ROOT / root).rglob('*.py'):
            if '/tests' in str(path):
                continue
            text = path.read_text(encoding='utf-8')
            for pattern in CODE_PATTERNS:
                codes.update(re.findall(pattern, text))
    return sorted(codes)


class ClassifierTableTests(unittest.TestCase):
    """web/ux04_errors.js: by code, then status, then narrow text; every known code covered."""

    def test_every_known_code_and_the_review_cases(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        codes = known_codes()
        for must in ('reset_in_progress', 'conformance_running', 'estop_latched', 'safety_rejected',
                     'hardware_not_authorized', 'authorization_required', 'simulated_only', 'joint_state_required',
                     'stop_count_mismatch', 'device_timeout', 'device_unreachable', 'simulated_evidence'):
            self.assertIn(must, codes)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'codes.json'
            path.write_text(json.dumps(codes + ['joint_names_required', 'param_out_of_range']))
            result = subprocess.run([node, str(ROOT / 'tests' / 'ux04_errors.cjs'), str(path)],
                                    capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('error classification checks passed', result.stdout)


class _Handler(BaseHTTPRequestHandler):
    reply = (200, {})
    delay = 0.0

    def do_GET(self):
        time.sleep(self.delay)
        status, body = self.reply
        data = json.dumps(body).encode() if body is not None else b'not json'
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = do_GET

    def log_message(self, *args):
        pass


class ConnectorTests(unittest.TestCase):
    def serve(self, status, body, delay=0.0):
        handler = type('H', (_Handler,), {'reply': (status, body), 'delay': delay})
        http = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        return f'http://127.0.0.1:{http.server_port}'

    def test_409_body_is_kept_not_flattened(self):
        url = self.serve(409, {'error': '适配器上一次复位（adapter.reset()）尚未返回（已 1.0 s）', 'code': 'reset_in_progress'})
        with self.assertRaises(connector.DeviceError) as ctx:
            connector.request(url, '/v1/reset', 'tok', {'request_id': 'r'})
        exc = ctx.exception
        self.assertEqual((exc.status, exc.code, exc.sent), (409, 'reset_in_progress', True))
        self.assertIn('尚未返回', str(exc))
        for wrong in ('无法连接', '地址、凭据', '协议'):
            self.assertNotIn(wrong, str(exc))
        self.assertIsInstance(exc, ValueError)       # existing callers catch ValueError

    def test_error_without_body_names_the_status_only(self):
        url = self.serve(409, None)
        with self.assertRaises(connector.DeviceError) as ctx:
            connector.request(url, '/v1/reset')
        self.assertEqual(str(ctx.exception), '接入服务返回 HTTP 409')
        self.assertIsNone(ctx.exception.code)

    def test_timeout_and_refused_have_their_own_codes(self):
        url = self.serve(200, {'ok': True}, delay=1.0)
        with self.assertRaises(connector.DeviceError) as ctx:
            connector.call(url, '/v1/reset', timeout=0.3)
        self.assertEqual((ctx.exception.code, ctx.exception.sent), ('device_timeout', None))
        self.assertIn('0.3 秒', str(ctx.exception))
        import socket
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        with self.assertRaises(connector.DeviceError) as ctx:
            connector.request(f'http://127.0.0.1:{port}', '/v1/status')
        self.assertEqual((ctx.exception.code, ctx.exception.sent), ('device_unreachable', False))


class ErrorPayloadTests(unittest.TestCase):
    def test_payload_carries_code_status_and_field(self):
        exc = connector.DeviceError('技能参数 loops 超出范围', status=400, code='param_out_of_range',
                                    body={'error': 'x', 'code': 'param_out_of_range', 'param': 'loops', 'min': 1, 'max': 3,
                                          'secret': 'not passed', 'choices': ['a', {'x': 1}]})
        self.assertEqual(error_payload(exc), {'error': '技能参数 loops 超出范围', 'code': 'param_out_of_range',
                                              'device_status': 400, 'param': 'loops', 'min': 1, 'max': 3,
                                              'choices': ['a', None]})
        self.assertEqual(error_payload(CodedError('需要授权', 'authorization_required')),
                         {'error': '需要授权', 'code': 'authorization_required'})
        self.assertEqual(error_payload(ValueError('普通错误')), {'error': '普通错误'})
        self.assertEqual(refusal_fields({'min': float('nan'), 'max': 2}), {'max': 2})


class ResetWayOutTests(unittest.TestCase):
    """Q5-03 on the desktop: the real reason of a refused or hanging reset."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Platform(self.tmp.name + '/platform')
        pid = self.app.add_project({'name': 'P', 'objective': 'O'})['id']
        self.device = LatchingDevice()
        self.gateway = Gateway(self.device, TOKEN, self.tmp.name + '/edge')
        self.server = Server(edge_handler(self.gateway))
        robot = self.app.add_robot({'project_id': pid, 'name': 'R', 'family': 'mobile',
                                    'endpoint': self.server.url, 'token': TOKEN})
        self.app.connect_robot({'robot_id': robot['id']})
        self.robot = robot
        self.device.latched = True

    def tearDown(self):
        self.server.close()
        self.gateway.store.close()
        self.app.close()
        self.tmp.cleanup()

    def reset(self, request_id):
        return self.app.reset_robot({'robot_id': self.robot['id'], 'hardware_authorized': True, 'request_id': request_id})

    def fake_device(self, reset_error, status=None):
        real_request, real_call = connector.request, connector.call

        def request(endpoint, path, token='', payload=None):
            if path == '/v1/reset':
                raise reset_error
            return real_request(endpoint, path, token, payload)

        def call(endpoint, path, token='', payload=None, timeout=8):
            if path == '/v1/reset':          # ux05-fix2: the reset goes through call() with its own timeout
                raise reset_error
            if path == '/v1/status' and status is not None:
                return {**self.device.status(), **status}
            return real_call(endpoint, path, token, payload, timeout)
        return patch.multiple(service_module.connector, request=request, call=call)

    def test_409_reset_in_progress_is_a_refusal_with_its_reason(self):
        refusal = connector.DeviceError('适配器上一次复位（adapter.reset()）尚未返回（已 3.0 s）；在它返回之前拒绝新的复位，急停保持锁存',
                                        status=409, code='reset_in_progress', sent=True)
        with self.fake_device(refusal):
            op = self.reset('r1')
        self.assertEqual((op['status'], op['code'], op['device_status']), ('rejected', 'reset_in_progress', 409))
        self.assertIn('尚未返回', op['error'])
        self.assertNotIn('无法连接', op['error'])
        self.assertNotIn('地址、凭据', op['error'])
        self.assertTrue(self.app.store.get('robots', self.robot['id']).get('estop_latched'), '拒绝后仍按锁存处理')

    def test_conformance_running_is_a_refusal_too(self):
        refusal = connector.DeviceError('一致性测试正在进行，拒绝其他来源的运动与复位（停止始终可用）', status=409,
                                        code='conformance_running', sent=True)
        with self.fake_device(refusal):
            op = self.reset('r2')
        self.assertEqual((op['status'], op['code']), ('rejected', 'conformance_running'))

    def test_hanging_reset_is_checked_against_status(self):
        lost = connector.DeviceError('接入服务没有在 8 秒内响应；请求结果未知', code='device_timeout', sent=None)
        with self.fake_device(lost, status={'reset_in_progress': True, 'estop_latched': True}):
            op = self.reset('r3')
        self.assertEqual((op['status'], op['code']), ('unknown', 'reset_in_progress'))
        self.assertIn('一直没有完成', op['error'])
        self.assertIn('仍然锁存', op['error'])
        # never resent: the same request id reports the recorded outcome
        self.assertEqual(self.reset('r3')['id'], op['id'])

    def test_lost_answer_without_reset_in_progress_stays_unknown(self):
        lost = connector.DeviceError('接入服务没有在 8 秒内响应；请求结果未知', code='device_timeout', sent=None)
        with self.fake_device(lost, status={'reset_in_progress': False, 'estop_latched': True}):
            op = self.reset('r4')
        self.assertEqual((op['status'], op['code']), ('unknown', 'device_timeout'))
        self.assertIn('请在现场核对', op['error'])

    def test_unauthorized_reset_has_a_code_over_http(self):
        application = Application(self.tmp.name + '/server')
        server = Server(make_handler(application))
        try:
            port = server.http.server_port

            def post(path, payload, cookie=''):
                req = urllib.request.Request(f'http://127.0.0.1:{port}/api/' + path, data=json.dumps(payload).encode(),
                                             headers={'Content-Type': 'application/json', 'Cookie': cookie})
                try:
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as r:
                        return r.status, json.loads(r.read()), r.headers.get('Set-Cookie', '')
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read()), ''
            form = {'name': 'QA', 'email': 'qa4@example.test', 'password': 'fixture-password-1'}
            _, user, cookie = post('register', {**form, 'code': post('register/start', form)[1]['dev_code']})
            cookie = cookie.split(';')[0]
            workspace = application.workspace(user['user'])
            pid = workspace.add_project({'name': 'P', 'objective': 'O'})['id']
            remote = workspace.add_robot({'project_id': pid, 'name': 'R', 'family': 'mobile',
                                          'endpoint': self.server.url, 'token': TOKEN})
            status, body, _ = post('robots/reset', {'robot_id': remote['id']}, cookie)
            self.assertEqual((status, body['code']), (400, 'authorization_required'))
            status, body, _ = post('robots/run-skill', {'robot_id': remote['id'], 'skill_id': 's', 'request_id': 'x',
                                                        'hardware_authorized': True}, cookie)
            self.assertEqual((status, body['code']), (400, 'simulated_only'))
        finally:
            server.close()
            application.close()


class PlainTextTests(unittest.TestCase):
    def test_capability_ids_become_chinese_names(self):
        text = '任何提供 base.navigate_to 的本体都能运行；若本体有 legged.posture，会先站立。见 skill.py 与 x.vendor_thing'
        plain = robot_caps.plain_text(text)
        self.assertNotIn('base.navigate_to', plain)
        self.assertNotIn('legged.posture', plain)
        self.assertIn('「', plain)
        self.assertIn('skill.py', plain)               # not a capability: left alone
        self.assertIn('x.vendor_thing', plain)
        self.assertEqual(robot_caps.plain_text('没有能力名'), '没有能力名')

    def test_options_alias_for_choices(self):
        view = robot_caps.parameter_view({'name': 'mode', 'type': 'enum', 'options': ['slow', 'fast']})
        self.assertEqual(view['choices'], ['slow', 'fast'])


class SimRestartTests(unittest.TestCase):
    """The operator's way out on a simulated robot: restart the runtime; the latch survives."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-ux04-test-')
        self.app = Platform(self.tmp.name + '/ws')

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_restart_keeps_latch_then_reset_works_and_trial_project_is_marked(self):
        robot = self.app.add_sim_robot({'kind': 'quadruped'})
        project = self.app.store.get('projects', robot['project_id'])
        self.assertTrue(project.get('auto_created'), '自动建的试用项目应带标记，工作台不把它算作“已明确任务”')
        self.assertEqual(self.app.stop_robot({'robot_id': robot['id']})['status'], 'accepted')
        old_pid = self.app.sims.procs[robot['id']].pid
        restarted = self.app.sims.api_restart({'robot_id': robot['id'], 'project_id': robot['project_id']})
        self.assertEqual(restarted['sim']['state'], 'running')
        self.assertNotEqual(self.app.sims.procs[robot['id']].pid, old_pid)
        live = self.app.robot_live({'robot_id': robot['id']})
        self.assertTrue(live['estop']['latched'], '重启仿真机器人不能解除急停锁存')
        self.assertFalse(live['reset_in_progress'])
        op = self.app.reset_robot({'robot_id': robot['id'], 'hardware_authorized': True, 'request_id': 'after-restart'})
        self.assertEqual(op['status'], 'accepted', op)
        self.assertTrue(op.get('simulated'))
        with self.assertRaises(ValueError):
            self.app.sims.api_restart({'robot_id': robot['id'], 'project_id': 'project-other'})


if __name__ == '__main__':
    unittest.main()
