"""ux05 (round 07): typed options, finer parameter refusals, reset fields, the latch guard in words.

* Q6-07: ``options: [{value, label}]`` / ``choices`` reach the browser as typed values with labels -- never
  ``str()`` of an object (which was shown and sent verbatim and refused by the device).
* Q6-08: the runtime's ``reset_in_progress_s`` / ``cause`` / ``hint`` and ``constraint`` / ``problem`` /
  ``expected`` reach the browser; the desktop's own 500 carries ``code: desktop_internal_error``; a
  pathologically nested body is a 400, not a 500.
* Latch guard (INTERFACES §7-§8): counts for *this* e-stop, ``safety.stop_not_obeyed`` from telemetry.
* The classifier and field texts are checked in tests/ux04_errors.cjs (run by tests/test_ux04.py).
"""
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import connector, robot_caps, service as service_module, sim_robots  # noqa: E402
from brainos.edge import Gateway, make_handler as edge_handler  # noqa: E402
from brainos.errors import error_payload, refusal_fields  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from brainos.service import Platform  # noqa: E402
from test_product import Server  # noqa: E402
from test_sim_evidence_and_reset import TOKEN, LatchingDevice  # noqa: E402


class OptionsTests(unittest.TestCase):
    def test_option_objects_keep_value_and_label(self):
        view = robot_caps.parameter_view({'name': 'speed', 'type': 'str', 'options': [
            {'value': 'slow', 'label': '慢'}, {'value': 'fast', 'label': '快'}]})
        self.assertEqual(view['choices'], ['slow', 'fast'])
        self.assertEqual(view['options'], [{'value': 'slow', 'label': '慢'}, {'value': 'fast', 'label': '快'}])
        self.assertNotIn("{'value'", json.dumps(view, ensure_ascii=False))

    def test_values_keep_their_json_type(self):
        view = robot_caps.parameter_view({'name': 'n', 'type': 'int', 'choices': [1, 2, '3', True, None, 2.5]})
        self.assertEqual(view['choices'], [1, 2, '3', True, None, 2.5])
        self.assertEqual([o['label'] for o in view['options']], ['1', '2', '3', '是', '（空）', '2.5'])

    def test_unsendable_entries_are_dropped(self):
        view = robot_caps.parameter_view({'name': 'm', 'type': 'object', 'options': [
            {'value': {'nested': 1}, 'label': 'x'}, {'label': 'no value'}, [1, 2], float('nan'), 'ok',
            {'value': 'ok', 'label': '重复'}]})
        self.assertEqual(view['options'], [{'value': 'ok', 'label': 'ok'}])

    def test_label_falls_back_to_value(self):
        view = robot_caps.parameter_view({'name': 'm', 'type': 'enum', 'options': [{'value': 'a', 'label': '  '}]})
        self.assertEqual(view['options'], [{'value': 'a', 'label': 'a'}])

    def test_no_choices_no_options(self):
        view = robot_caps.parameter_view({'name': 'm', 'type': 'int'})
        self.assertNotIn('choices', view)
        self.assertNotIn('options', view)


class RefusalFieldTests(unittest.TestCase):
    def test_reset_fields_pass_under_the_runtime_names(self):
        body = {'code': 'reset_in_progress', 'reset_in_progress_s': 12.3, 'hint': '出路：' + 'x' * 300,
                'cause': 'latch_guard_reset_grace', 'in_flight_s': 9}
        out = refusal_fields(body)
        self.assertEqual(out['reset_in_progress_s'], 12.3)
        self.assertEqual(out['cause'], 'latch_guard_reset_grace')
        self.assertTrue(out['hint'].startswith('出路：') and len(out['hint']) == 303)
        self.assertNotIn('in_flight_s', out)

    def test_parameter_details_pass(self):
        body = {'code': 'param_length', 'param': 'route', 'problem': 'param_length', 'expected': 'floats',
                'constraint': {'min_length': 2, 'max_length': 5, 'deep': {'x': 1}, 'choices': ['a', {'b': 1}]},
                'value_length': 9, 'step': 2, 'min': None}
        out = refusal_fields(body)
        self.assertEqual(out['constraint'], {'min_length': 2, 'max_length': 5, 'choices': ['a', None]})
        self.assertEqual((out['problem'], out['expected'], out['value_length'], out['step']),
                         ('param_length', 'floats', 9, 2))
        self.assertNotIn('min', out)
        self.assertNotIn('constraint', refusal_fields({'constraint': 'not an object'}))

    def test_error_payload_carries_cause(self):
        exc = connector.DeviceError('复位期间又收到停止；保持锁存', status=409, code='estop_latched',
                                    body={'code': 'estop_latched', 'cause': 'stop_during_reset'})
        self.assertEqual(error_payload(exc)['cause'], 'stop_during_reset')


class ResetFieldsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-ux05-')
        self.app = Platform(self.tmp.name + '/platform')
        pid = self.app.add_project({'name': 'P', 'objective': 'O'})['id']
        self.device = LatchingDevice()
        self.gateway = Gateway(self.device, TOKEN, self.tmp.name + '/edge')
        self.server = Server(edge_handler(self.gateway))
        self.robot = self.app.add_robot({'project_id': pid, 'name': 'R', 'family': 'mobile',
                                         'endpoint': self.server.url, 'token': TOKEN})
        self.app.connect_robot({'robot_id': self.robot['id']})
        self.device.latched = True

    def tearDown(self):
        self.server.close()
        self.gateway.store.close()
        self.app.close()
        self.tmp.cleanup()

    def fake(self, reset_error, status=None):
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

    def reset(self, rid):
        return self.app.reset_robot({'robot_id': self.robot['id'], 'hardware_authorized': True, 'request_id': rid})

    def test_409_carries_seconds_and_hint(self):
        body = {'error': 'x', 'code': 'reset_in_progress', 'reset_in_progress_s': 14.2, 'hint': '确认现场安全后重启运行时'}
        refusal = connector.DeviceError('适配器上一次复位尚未返回', status=409, code='reset_in_progress', sent=True, body=body)
        with self.fake(refusal):
            op = self.reset('a1')
        self.assertEqual((op['status'], op['reset_in_progress_s'], op['hint']), ('rejected', 14.2, '确认现场安全后重启运行时'))

    def test_guard_cause_is_kept(self):
        body = {'error': 'x', 'code': 'estop_latched', 'cause': 'latch_guard_reset_grace'}
        refusal = connector.DeviceError('复位动作超过允许时间仍在运动', status=409, code='estop_latched', sent=True, body=body)
        with self.fake(refusal):
            op = self.reset('a2')
        self.assertEqual((op['status'], op['code'], op['cause']), ('rejected', 'estop_latched', 'latch_guard_reset_grace'))

    def test_hanging_reset_takes_seconds_and_hint_from_status(self):
        lost = connector.DeviceError('接入服务没有在 8 秒内响应；请求结果未知', code='device_timeout', sent=None)
        with self.fake(lost, status={'reset_in_progress': True, 'estop_latched': True, 'reset_in_progress_s': 31.0,
                                     'reset_hint': '适配器复位长时间未返回'}):
            op = self.reset('a3')
        self.assertEqual((op['status'], op['code'], op['reset_in_progress_s'], op['hint']),
                         ('unknown', 'reset_in_progress', 31.0, '适配器复位长时间未返回'))


class DesktopErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-ux05-http-')
        self.application = Application(self.tmp.name + '/server')
        self.server = Server(make_handler(self.application))
        self.port = self.server.http.server_port
        _, _, cookie = self.post('register', {'name': 'QA', 'email': 'qa5@example.test', 'password': 'fixture-password-1'})
        self.cookie = cookie.split(';')[0]

    def tearDown(self):
        self.server.close()
        self.application.close()
        self.tmp.cleanup()

    def post(self, path, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}/api/' + path, data=data,
                                     headers={'Content-Type': 'application/json', 'Cookie': getattr(self, 'cookie', '')})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as r:
                return r.status, json.loads(r.read()), r.headers.get('Set-Cookie', '')
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), ''

    def test_own_500_has_a_code(self):
        with patch.object(service_module.Platform, 'add_project', side_effect=RuntimeError('boom')):
            status, body, _ = self.post('projects', {'name': 'P', 'objective': 'O'})
        self.assertEqual((status, body['code']), (500, 'desktop_internal_error'))
        self.assertIn('操作未确认', body['error'])

    def test_deep_nesting_is_a_400(self):
        raw = ('{"robot_id": "x", "parameters": {"a": ' + '[' * 5000 + ']' * 5000 + '}}').encode()
        status, body, _ = self.post('robots/run-skill', None, raw=raw)
        self.assertEqual(status, 400, body)                 # parsed fine; refused for an ordinary reason
        with patch.object(service_module.Platform, 'add_project', side_effect=RecursionError('deep')):
            status, body, _ = self.post('projects', {'name': 'P', 'objective': 'O'})
        self.assertEqual((status, body.get('code')), (400, 'payload_too_deep'))


class LatchViewTests(unittest.TestCase):
    """The live view's latch guard numbers are for this e-stop; stop_not_obeyed comes from telemetry."""

    robot = {'id': 'robot-1', 'endpoint': 'http://127.0.0.1:1', 'token': ''}

    def status(self, latched, epoch, restops=0, invalid=0, last_seq=10, **lm):
        return {'estop_latched': latched, 'stop_epoch': epoch, 'telemetry': {'last_seq': last_seq},
                'latched_motion': {'enabled': True, 'active': latched, 'restops': restops, 'invalid_restops': invalid,
                                   'seen': restops, 'feedback_ok': True, 'feedback_problems': {}, **lm}}

    def test_counts_are_for_this_latch(self):
        p = SimpleNamespace()
        sim_robots.latch_view(p, self.robot, self.status(False, 1, restops=7, invalid=2), False)
        v = sim_robots.latch_view(p, self.robot, self.status(True, 2, restops=10, invalid=3), True)
        self.assertEqual((v['restops'], v['invalid_restops'], v['since']), (3, 1, 'latch'))
        v = sim_robots.latch_view(p, self.robot, self.status(True, 2, restops=12, invalid=3), True)
        self.assertEqual(v['restops'], 5)

    def test_drift_notice_passes_through_once(self):
        p = SimpleNamespace()
        msg = '急停期间机器人离停下位置已超过 5 厘米（底盘位置），可能是里程计漂移，也可能在移动，请到现场确认'
        v = sim_robots.latch_view(p, self.robot, self.status(True, 3, drift_notice=True, drift_notice_at=12.5,
                                                             drift_notice_detail={'message': msg, 'channel': 'base_position'}), True)
        self.assertEqual(v['drift_notice'], {'message': msg, 'channel': 'base_position', 'ts': 12.5})
        v = sim_robots.latch_view(p, self.robot, self.status(True, 3), True)
        self.assertNotIn('drift_notice', v)

    def test_first_seen_during_a_latch_says_since_runtime_start(self):
        p = SimpleNamespace()
        v = sim_robots.latch_view(p, self.robot, self.status(True, 4, restops=6, invalid=0), True)
        self.assertEqual((v['restops'], v['since']), (6, 'runtime_start'))

    def test_feedback_and_disabled(self):
        p = SimpleNamespace()
        v = sim_robots.latch_view(p, self.robot, self.status(True, 1, feedback_ok=False, feedback_problems={'joint.state': 'NaN'},
                                                             read_in_progress_s=2.5, displaced=True, max_drift_m=0.12), True)
        self.assertEqual((v['feedback_ok'], v['feedback_problems'], v['read_in_progress_s'], v['displaced']),
                         (False, ['joint.state'], 2.5, True))
        self.assertEqual(sim_robots.latch_view(p, self.robot, {'latched_motion': {'enabled': False}}, False), {'enabled': False})
        self.assertIsNone(sim_robots.latch_view(p, self.robot, {}, False))

    def test_stop_not_obeyed_from_telemetry(self):
        p = SimpleNamespace()
        pages = {'/v2/telemetry?since=0': {'events': [
            {'seq': 3, 'type': 'safety.motion_while_latched'},
            {'seq': 4, 'type': 'safety.stop_not_obeyed', 'ts': 1700000000.0, 'note': '多次停止后仍在运动', 'evidence': {'big': 1}}],
            'last_seq': 4}}
        asked = []

        def get(robot, path, timeout=3):
            asked.append(path)
            return pages.get(path, {'events': [], 'last_seq': 4})
        with patch.object(sim_robots, '_get', get):
            event = sim_robots.stop_not_obeyed(p, self.robot, self.status(True, 1, last_seq=4), True)
            self.assertEqual((event['type'], event['note']), ('safety.stop_not_obeyed', '多次停止后仍在运动'))
            self.assertNotIn('evidence', event)
            # the next tick continues from the cursor and keeps the event
            self.assertEqual(sim_robots.stop_not_obeyed(p, self.robot, self.status(True, 1, last_seq=4), True)['seq'], 4)
            self.assertEqual(asked[-1], '/v2/telemetry?since=4')
            # released on site: cleared, and no telemetry is read while unlatched
            n = len(asked)
            self.assertIsNone(sim_robots.stop_not_obeyed(p, self.robot, self.status(False, 1, last_seq=9), False))
            self.assertEqual(len(asked), n)
            # a new latch reads only what came after
            sim_robots.stop_not_obeyed(p, self.robot, self.status(True, 2, last_seq=12), True)
            self.assertEqual(asked[-1], '/v2/telemetry?since=9')

    def test_a_later_reset_clears_an_old_event(self):
        p = SimpleNamespace()

        def get(robot, path, timeout=3):
            return {'events': [{'seq': 1, 'type': 'safety.stop_not_obeyed'}, {'seq': 2, 'type': 'safety.reset'},
                               {'seq': 3, 'type': 'safety.estop'}], 'last_seq': 3}
        with patch.object(sim_robots, '_get', get):
            self.assertIsNone(sim_robots.stop_not_obeyed(p, self.robot, self.status(True, 1, last_seq=3), True))

    def test_telemetry_errors_never_break_the_view(self):
        p = SimpleNamespace()

        def get(robot, path, timeout=3):
            raise connector.DeviceError('no', code='device_unreachable', sent=False)
        with patch.object(sim_robots, '_get', get):
            self.assertIsNone(sim_robots.stop_not_obeyed(p, self.robot, self.status(True, 1), True))

    def test_status_flag_also_counts(self):
        p = SimpleNamespace()
        v = sim_robots.latch_view(p, self.robot, self.status(True, 1, stop_not_obeyed=True), True)
        self.assertEqual(v['stop_not_obeyed'], {'flag': True})


if __name__ == '__main__':
    unittest.main()
