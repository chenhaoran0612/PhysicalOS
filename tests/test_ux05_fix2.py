"""ux05-fix2 (round 07 re-review, desktop): Q7b-04/05/06/07/08/10.

* Q7b-04: a float option 1.0 reaches the device as 1.0 on every send path -- direct run, application steps,
  validate / evaluate / deploy -- including skills whose declaration was typed into "登记技能" (the form now
  sends the text; the desktop parses it and stores numbers with their declared type), and applications saved
  before the fix (re-serialized at send time; stored digest unchanged).
* Q7b-05/06/08: ``reset_policy`` -- ``expire_s = clamp(max(8, reset_expected_s + 5), 8, 30)``, HTTP timeout
  beyond it, ``reset_expire`` only when ``/v1/status.capabilities.reset_expire === true``; a request that
  outlived its limit is recorded as this reset (``own_reset``/``expired``), without the runtime's "请稍候" hint.
* Q7b-07: the "loosened" list follows ``relaxed_keys``; without it only keys looser than the default.
* Q7b-10: ``robots/safety`` with ``all_projects`` covers every project's robots.
Browser side: tests/ux05_fix2_browser.cjs.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from brainos import connector, robot_caps, safety_watch, sim_robots  # noqa: E402
from brainos import service as service_module  # noqa: E402
from brainos.edge import Gateway, make_handler as edge_handler  # noqa: E402
from brainos.service import Platform, reset_policy  # noqa: E402
from test_product import DeviceFixture, Server  # noqa: E402
from test_sim_evidence_and_reset import TOKEN, LatchingDevice  # noqa: E402

FLOAT_DECL_TEXT = ('[{"name": "speed", "type": "float", "options": [{"value": 0.5, "label": "慢"}, '
                   '{"value": 1.0, "label": "快"}], "default": 1.0, "min": 0, "max": 2}, '
                   '{"name": "laps", "type": "int", "choices": [1, 2.0]}]')


class RecordingDevice(DeviceFixture):
    """A real (not simulated) device double that keeps every application it is sent, as JSON text."""

    def __init__(self):
        super().__init__()
        self.sent = {}

    def manifest(self):
        return {**super().manifest(), 'simulated': False, 'vendor': 'Fixture', 'model': 'recording'}

    def _keep(self, kind, application):
        self.sent[kind] = json.dumps(application['steps'][0]['parameters'], sort_keys=True)

    def validate(self, application):
        self._keep('validate', application)
        return super().validate(application)

    def evaluate(self, p):
        self._keep('evaluate', p['application'])
        return super().evaluate(p)

    def deploy(self, p):
        self._keep('deploy', p['application'])
        return super().deploy(p)


class FloatOptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-ux05fix2-')
        self.app = Platform(self.tmp.name + '/platform')
        self.pid = self.app.add_project({'name': 'P', 'objective': 'O'})['id']
        self.device = RecordingDevice()
        self.gateway = Gateway(self.device, TOKEN, self.tmp.name + '/edge')
        self.server = Server(edge_handler(self.gateway))
        self.robot = self.app.add_robot({'project_id': self.pid, 'name': '真机', 'family': 'mobile',
                                         'endpoint': self.server.url, 'token': TOKEN})
        self.app.connect_robot({'robot_id': self.robot['id']})

    def tearDown(self):
        self.server.close()
        self.gateway.store.close()
        self.app.close()
        self.tmp.cleanup()

    def skill(self, parameters):
        return self.app.add_skill({'project_id': self.pid, 'name': 'Go', 'version': '1.0', 'requires': ['navigation.goal'],
                                   'entrypoint': 'go', 'success_condition': 'done', 'stop_condition': 'timeout',
                                   'model_ids': [], 'parameters': parameters})

    def application(self, skill, params):
        return self.app.add_application({'project_id': self.pid, 'name': 'A', 'version': '1.0', 'robot_id': self.robot['id'],
                                         'steps': [{'skill_id': skill['id'], 'parameters': params, 'timeout_s': 30}]})

    def run_all_paths(self, app):
        op = self.app.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        self.assertEqual(op['status'], 'succeeded', op)
        release = self.app.deploy({'application_id': app['id'], 'report_id': op['report_id']})
        self.assertEqual(release['status'], 'deployed', release)
        return self.device.sent

    def test_hand_entered_text_keeps_float_types(self):
        skill = self.skill(FLOAT_DECL_TEXT)
        speed = next(d for d in skill['parameters'] if d['name'] == 'speed')
        self.assertEqual(json.dumps([o['value'] for o in speed['options']]), '[0.5, 1.0]')
        self.assertEqual(json.dumps([speed['default'], speed['min'], speed['max']]), '[1.0, 0.0, 2.0]')
        laps = next(d for d in skill['parameters'] if d['name'] == 'laps')
        self.assertEqual(json.dumps(laps['choices']), '[1, 2]')             # int: whole floats become ints
        stored = self.app.store.get('skills', skill['id'])
        self.assertEqual(json.dumps(stored['parameters'][0]['choices']), '[0.5, 1.0]')
        with self.assertRaisesRegex(ValueError, '参数说明不是合法 JSON'):
            self.skill('[{"name": ')
        self.assertNotIn('parameters', self.skill('   '))

    def test_browser_numbers_typed_on_every_application_path(self):
        skill = self.skill(FLOAT_DECL_TEXT)
        app = self.application(skill, {'speed': 1, 'laps': 2.0})              # what a browser sends
        self.assertEqual(json.dumps(app['steps'][0]['parameters'], sort_keys=True), '{"laps": 2, "speed": 1.0}')
        sent = self.run_all_paths(app)
        self.assertEqual(set(sent), {'validate', 'evaluate', 'deploy'})
        for kind, text in sent.items():
            self.assertEqual(text, '{"laps": 2, "speed": 1.0}', kind)

    def test_pre_fix_declaration_and_application_are_fixed_when_sent(self):
        """A skill stored before the fix (browser JSON.parse: [0.5, 1]) and an application holding ``1``."""
        skill = self.skill([{'name': 'speed', 'type': 'float', 'options': [{'value': 0.5}, {'value': 1.0}]}])
        legacy = dict(self.app.store.get('skills', skill['id']))
        legacy['parameters'] = [{'name': 'speed', 'type': 'float', 'required': False, 'description': '',
                                 'choices': [0.5, 1], 'options': [{'value': 0.5, 'label': '0.5'}, {'value': 1, 'label': '1'}]}]
        self.app.store.put('skills', legacy)
        app = self.application(legacy, {'speed': 1})
        stored = dict(self.app.store.get('applications', app['id']))
        stored['steps'] = [{**stored['steps'][0], 'parameters': {'speed': 1}}]     # saved by the old desktop
        self.app.store.put('applications', stored)
        sent = self.run_all_paths(stored)
        for kind, text in sent.items():
            self.assertEqual(text, '{"speed": 1.0}', kind)
        self.assertEqual(self.app.store.get('applications', app['id'])['digest'], stored['digest'])

    def test_typed_application_leaves_other_steps_alone(self):
        app = {'steps': [{'skill_id': 'missing', 'parameters': {'x': 1}}, 'odd'], 'digest': 'd'}
        self.assertEqual(robot_caps.typed_application(self.app, app), app)
        self.assertEqual(robot_caps.typed_application(self.app, {'name': 'x'}), {'name': 'x'})

    def test_device_declaration_literal_wins_on_direct_run(self):
        decls = [{'name': 'gain', 'type': 'float', 'choices': [1, 2]}, {'name': 'speed', 'type': 'float', 'choices': [0.5, 1.0]}]
        self.assertEqual(json.dumps(robot_caps.typed_parameters(decls, {'gain': 1, 'speed': 1}, device_decls=True), sort_keys=True),
                         '{"gain": 1, "speed": 1.0}')
        self.assertEqual(json.dumps(robot_caps.typed_parameters(decls, {'gain': 1, 'speed': 1}), sort_keys=True),
                         '{"gain": 1.0, "speed": 1.0}')

    def test_runtime_accepts_what_the_desktop_sends(self):
        try:
            from neuxmind_runtime import skills as rt
        except ImportError:
            self.skipTest('runtime not importable')
        decl = json.loads(FLOAT_DECL_TEXT)[0]
        skill = self.skill(FLOAT_DECL_TEXT)
        sent = json.loads(json.dumps(robot_caps.typed_parameters(skill['parameters'], {'speed': 1})))
        rt._check_one(decl, sent['speed'])


class ResetPolicyTests(unittest.TestCase):
    def test_expire_and_timeout(self):
        cases = [(None, 8), ({}, 8), ({'reset_expected_s': 1}, 8), ({'reset_expected_s': 3}, 8),
                 ({'reset_expected_s': 4.5}, 9.5), ({'reset_expected_s': 20}, 25), ({'reset_expected_s': 40}, 30),
                 ({'reset_expected_s': True}, 8), ({'reset_expected_s': -3}, 8), ({'reset_expected_s': float('nan')}, 8),
                 ({'safety': {'reset_expected_s': 12}}, 17)]
        for status, expire in cases:
            p = reset_policy(status)
            self.assertEqual(p['expire_s'], expire, status)
            self.assertGreater(p['timeout_s'], p['expire_s'], status)
        self.assertEqual(reset_policy({}, {'safety': {'reset_expected_s': 15}})['expire_s'], 20)

    def test_capability_must_be_advertised(self):
        self.assertFalse(reset_policy({})['reset_expire'])
        self.assertFalse(reset_policy({'capabilities': {'reset_expire': 'yes'}})['reset_expire'])
        self.assertFalse(reset_policy({'capabilities': ['reset_expire']})['reset_expire'])
        self.assertTrue(reset_policy({'capabilities': {'reset_expire': True}})['reset_expire'])


class ResetRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-ux05fix2r-')
        self.app = Platform(self.tmp.name + '/platform')
        pid = self.app.add_project({'name': 'P', 'objective': 'O'})['id']
        self.device = LatchingDevice()
        self.extra = {}
        orig = self.device.status
        self.device.status = lambda: {**orig(), **self.extra}
        self.gateway = Gateway(self.device, TOKEN, self.tmp.name + '/edge')
        self.server = Server(edge_handler(self.gateway))
        self.robot = self.app.add_robot({'project_id': pid, 'name': 'R', 'family': 'mobile',
                                         'endpoint': self.server.url, 'token': TOKEN})
        self.device.latched = True

    def tearDown(self):
        self.server.close()
        self.gateway.store.close()
        self.app.close()
        self.tmp.cleanup()

    def reset(self, rid, reset_error=None, status=None, clock=None):
        seen = []
        real_call = connector.call

        def call(endpoint, path, token='', payload=None, timeout=8):
            if path == '/v1/reset':
                seen.append((payload, timeout))
                if reset_error:
                    raise reset_error
            if path == '/v1/status' and status is not None:
                return {**self.device.status(), **status}
            return real_call(endpoint, path, token, payload, timeout)
        patches = [patch.object(service_module.connector, 'call', call)]
        if clock:
            ticks = iter(clock)
            patches.append(patch.object(service_module.time, 'monotonic', lambda: next(ticks)))
        for p in patches:
            p.start()
        try:
            op = self.app.reset_robot({'robot_id': self.robot['id'], 'hardware_authorized': True, 'request_id': rid})
        finally:
            for p in patches:
                p.stop()
        return op, seen

    def test_old_runtime_gets_default_limit_and_no_promise(self):
        op, seen = self.reset('a')
        self.assertEqual(op['status'], 'accepted')
        payload, timeout = seen[0]
        self.assertEqual(payload['expire_s'], 8)
        self.assertGreaterEqual(timeout, 8 + 2)
        self.assertEqual((op['expire_s'], op['reset_expire']), (8, False))
        robot = self.app.store.get('robots', self.robot['id'])
        self.assertEqual(robot['reset_policy']['reset_expire'], False)

    def test_slow_reset_robot_gets_a_longer_limit(self):
        self.extra = {'capabilities': {'reset_expire': True}, 'reset_expected_s': 20}
        op, seen = self.reset('b')
        payload, timeout = seen[0]
        self.assertEqual(payload['expire_s'], 25)
        self.assertGreaterEqual(timeout, 25 + 2)
        self.assertEqual((op['expire_s'], op['reset_expire']), (25, True))

    def test_own_request_past_the_limit_is_this_reset(self):
        self.extra = {'capabilities': {'reset_expire': True}}
        lost = connector.DeviceError('接入服务没有在 12 秒内响应；请求结果未知', code='device_timeout', sent=None)
        op, _ = self.reset('c', lost, status={'reset_in_progress': True, 'reset_in_progress_s': 12.3,
                                             'reset_hint': '复位正在执行（例如机器人正在起立），请稍候', 'estop_latched': True},
                           clock=[100.0, 112.5])
        self.assertEqual((op['status'], op['code'], op.get('expired'), op.get('own_reset')), ('unknown', 'reset_in_progress', True, True))
        self.assertTrue(op['reset_expire'])
        self.assertNotIn('hint', op)
        self.assertIn('这次复位', op['error'])
        self.assertNotIn('上一次', op['error'])

    def test_quick_failure_is_not_called_expired(self):
        lost = connector.DeviceError('接入服务返回 HTTP 502', status=502, sent=True)
        op, _ = self.reset('d', lost, status={'reset_in_progress': True, 'estop_latched': True}, clock=[100.0, 100.4])
        self.assertNotIn('expired', op)

    def test_sim_live_reset_facts(self):
        f = sim_robots._reset_facts({'capabilities': {'reset_expire': True}, 'reset_in_progress_s': 9.2, 'reset_expected_s': 2}, {})
        self.assertEqual(f, {'reset_expire': True, 'expire_s': 8, 'in_progress_s': 9.2})
        self.assertFalse(sim_robots._reset_facts({}, {})['reset_expire'])


class RelaxedKeysTests(unittest.TestCase):
    lm = {'config': {'pos_m': 0.03, 'yaw_drift_rad_s': 0.002, 'escalate_after': 30, 'yaw_rad': 0.05},
          'config_source': {'pos_m': 'manifest', 'yaw_drift_rad_s': 'manifest', 'escalate_after': 'manifest', 'yaw_rad': 'default'},
          'relaxed': True}

    def test_runtime_relaxed_keys_win(self):
        keys = [{'key': 'escalate_after', 'value': 30, 'default': 10, 'source': 'manifest'},
                {'key': 'yaw_drift_rad_s', 'value': 0.002, 'default': 0.0003, 'source': 'manifest'}]
        v = safety_watch.relaxed_view({}, {**self.lm, 'relaxed_keys': keys})
        self.assertEqual(v['fields'], ['escalate_after', 'yaw_drift_rad_s'])
        self.assertEqual(v['values'], {'escalate_after': 30, 'yaw_drift_rad_s': 0.002})
        self.assertEqual(v['defaults'], {'escalate_after': 10, 'yaw_drift_rad_s': 0.0003})
        self.assertEqual(v['sources'], ['manifest'])
        self.assertNotIn('pos_m', v['fields'])
        # pos_m stays visible as calibration (not as "loosened")
        self.assertIn('pos_m', safety_watch.calibration_view(self.lm)['fields'])

    def test_empty_relaxed_keys_means_not_loosened(self):
        self.assertIsNone(safety_watch.relaxed_view({}, {**self.lm, 'relaxed': False, 'relaxed_keys': []}))

    def test_fallback_compares_only_in_the_looser_direction(self):
        v = safety_watch.relaxed_view({}, self.lm)
        self.assertEqual(v['fields'], ['escalate_after', 'yaw_drift_rad_s'])
        self.assertEqual(v['defaults']['escalate_after'], 10)
        stricter = {**self.lm, 'config': {'pos_m': 0.03}, 'config_source': {'pos_m': 'manifest'}}
        self.assertEqual(safety_watch.relaxed_view({}, stricter)['fields'], [])


class AllProjectsWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-ux05fix2w-')
        self.app = Platform(self.tmp.name + '/platform')
        self.servers, self.gateways, self.robots = [], [], []
        for name in ('A', 'B'):
            pid = self.app.add_project({'name': '项目' + name, 'objective': 'O'})['id']
            device = LatchingDevice()
            gw = Gateway(device, TOKEN, self.tmp.name + '/edge' + name)
            srv = Server(edge_handler(gw))
            robot = self.app.add_robot({'project_id': pid, 'name': '机器人' + name, 'family': 'mobile',
                                        'endpoint': srv.url, 'token': TOKEN})
            self.app.connect_robot({'robot_id': robot['id']})
            device.latched = name == 'B'
            self.servers.append(srv)
            self.gateways.append(gw)
            self.robots.append(robot)

    def tearDown(self):
        for srv, gw in zip(self.servers, self.gateways):
            srv.close()
            gw.store.close()
        self.app.close()
        self.tmp.cleanup()

    def test_every_project_is_watched(self):
        a, b = self.robots
        one = self.app.robot_safety({'project_id': a['project_id']})
        self.assertEqual([e['robot_id'] for e in one['robots']], [a['id']])
        every = self.app.robot_safety({'project_id': a['project_id'], 'all_projects': True})
        self.assertTrue(every['all_projects'])
        ids = [e['robot_id'] for e in every['robots']]
        self.assertEqual(ids[0], a['id'])                                 # the project in view first
        self.assertEqual(set(ids), {a['id'], b['id']})
        entry_b = next(e for e in every['robots'] if e['robot_id'] == b['id'])
        self.assertEqual((entry_b['project_id'], entry_b['project_name'], entry_b['latched']), (b['project_id'], '项目B', True))
        self.assertEqual(every['latched'], [b['id']])
        self.assertEqual(every['poll_s'], safety_watch.LATCHED_EVERY_S)
        # the single-project view does not drop the other project's cache entry
        self.app.robot_safety({'project_id': a['project_id']})
        self.assertIn(b['id'], vars(self.app)['_safety_watch'])


if __name__ == '__main__':
    unittest.main()
