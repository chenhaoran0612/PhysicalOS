"""ux05-fix (round 07 review): Q7-03 global "stop not obeyed" watch, Q7-04 typed numbers, Q7-05 calibration.

* Q7-03: ``robots/safety`` reports, for every connected robot of the project (real ones too), the e-stop
  latch, the latch guard in words and ``safety.stop_not_obeyed``; bounded per robot (latched: every 2 s,
  quiet: every 10 s); an alert outlives an unreachable robot and clears when the robot is seen unlatched.
* Q7-04: a number the browser sends (it cannot tell 1.0 from 1) goes to the device as the declared choice
  it equals -- ``1.0`` stays a float -- and whole numbers for ``float`` parameters as floats.
* Q7-05: ``relaxed`` / ``safety.latch_guard_relaxed`` and non-default calibration reach the desktop.
The browser side (global banner, detail page, simulated wording Q7-11) is tests/ux05_fix_browser.cjs.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import connector, robot_caps, safety_watch, sim_robots  # noqa: E402
from brainos.edge import Gateway, make_handler as edge_handler  # noqa: E402
from brainos.service import Platform  # noqa: E402
from test_product import Server  # noqa: E402
from test_sim_evidence_and_reset import TOKEN, LatchingDevice  # noqa: E402


class TypedParameterTests(unittest.TestCase):
    decls = [
        {'name': 'speed', 'type': 'float', 'choices': [0.5, 1.0], 'options': [{'value': 0.5, 'label': '慢'}, {'value': 1.0, 'label': '快'}]},
        {'name': 'laps', 'type': 'int', 'choices': [1, 2]},
        {'name': 'gain', 'type': 'float', 'choices': [1, 2]},          # Q7-07: int choices on a float param
        {'name': 'level', 'type': 'enum', 'choices': [1.0, 2.5]},
        {'name': 'scale', 'type': 'float'},
        {'name': 'count', 'type': 'int'},
        {'name': 'mode', 'type': 'str', 'choices': ['1', 'fast']},
        {'name': 'flag', 'type': 'bool'},
    ]

    def wire(self, params, device_decls=True):
        """What actually goes over the wire to the device (JSON text). ``device_decls``: the declaration was
        read from the device (direct run); ux05-fix2 (Q7b-04) serializes desktop-held ones by declared type."""
        return json.dumps(robot_caps.typed_parameters(self.decls, params, device_decls=device_decls), sort_keys=True)

    def test_whole_float_choice_is_sent_as_declared(self):
        self.assertEqual(self.wire({'speed': 1}), '{"speed": 1.0}')
        self.assertEqual(self.wire({'speed': 0.5}), '{"speed": 0.5}')
        self.assertEqual(self.wire({'level': 1}), '{"level": 1.0}')

    def test_int_choices_stay_ints(self):
        self.assertEqual(self.wire({'laps': 2}), '{"laps": 2}')
        self.assertEqual(self.wire({'gain': 1}), '{"gain": 1}')
        self.assertEqual(self.wire({'gain': 1.0}), '{"gain": 1}')
        # ux05-fix2 (Q7b-04): a desktop-held declaration is serialized by its declared type.
        self.assertEqual(self.wire({'gain': 1}, device_decls=False), '{"gain": 1.0}')

    def test_free_float_is_a_float(self):
        self.assertEqual(self.wire({'scale': 2}), '{"scale": 2.0}')
        self.assertEqual(self.wire({'count': 2}), '{"count": 2}')

    def test_other_values_untouched(self):
        self.assertEqual(self.wire({'mode': '1', 'flag': True, 'speed': True, 'unknown': 1}),
                         '{"flag": true, "mode": "1", "speed": true, "unknown": 1}')
        self.assertEqual(self.wire({'speed': 3}), '{"speed": 3.0}')          # not a choice: the device refuses it (as a float)
        self.assertEqual(robot_caps.typed_parameters(None, {'a': 1}), {'a': 1})

    def test_options_without_choices(self):
        decls = [{'name': 'speed', 'type': 'float', 'options': [{'value': 1.0, 'label': '快'}]}]
        self.assertEqual(json.dumps(robot_caps.typed_parameters(decls, {'speed': 1})), '{"speed": 1.0}')

    def test_runtime_accepts_what_the_desktop_sends(self):
        """Against the runtime's own strict check (Q6-06), whatever its rule for int vs float."""
        try:
            from neuxmind_runtime import skills as rt
        except ImportError:
            self.skipTest('runtime not importable')
        decl = {'name': 'speed', 'type': 'float', 'options': [{'value': 0.5, 'label': '慢'}, {'value': 1.0, 'label': '快'}]}
        view = robot_caps.parameter_views([decl])
        sent = json.loads(json.dumps(robot_caps.typed_parameters(view, {'speed': 1})))
        rt._check_one(decl, sent['speed'])          # raises on a refusal


class RelaxedViewTests(unittest.TestCase):
    lm = {'config': {'pos_m': 0.2, 'yaw_rad': 0.05}, 'config_source': {'pos_m': 'manifest', 'yaw_rad': 'default'},
          'config_notes': ['pos_m 限幅到 0.2']}

    def test_flag_spellings(self):
        self.assertIsNone(safety_watch.relaxed_view({}, self.lm))
        for status, lm in (({}, {**self.lm, 'relaxed': True}), ({'safety': {'latch_guard_relaxed': True}}, self.lm),
                           ({}, {**self.lm, 'latch_guard_relaxed': ['pos_m']})):
            v = safety_watch.relaxed_view(status, lm)
            self.assertEqual((v['relaxed'], v['fields'], v['sources'], v['values']), (True, ['pos_m'], ['manifest'], {'pos_m': 0.2}))
            self.assertEqual(v['notes'], ['pos_m 限幅到 0.2'])

    def test_dict_flag(self):
        v = safety_watch.relaxed_view({}, {**self.lm, 'relaxed': {'fields': ['yaw_rad'], 'message': '标定已放宽'}})
        # ux05-fix2 (Q7b-07): yaw_rad = 0.05 is the default -- named, but not looser, so not listed.
        self.assertEqual((v['fields'], v['message']), ([], '标定已放宽'))
        v = safety_watch.relaxed_view({}, {**self.lm, 'relaxed': {'fields': ['pos_m'], 'message': '标定已放宽'}})
        self.assertEqual((v['fields'], v['message']), (['pos_m'], '标定已放宽'))

    def test_calibration_view(self):
        self.assertEqual(safety_watch.calibration_view(self.lm)['fields'], {'pos_m': {'source': 'manifest', 'value': 0.2}})
        self.assertIsNone(safety_watch.calibration_view({'config_source': {'pos_m': 'default'}}))
        self.assertIsNone(safety_watch.calibration_view(None))


class SafetyWatchTests(unittest.TestCase):
    """robots/safety against a real-device double (not simulated), with /v1/status and telemetry faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-ux05fix-')
        self.app = Platform(self.tmp.name + '/platform')
        self.pid = self.app.add_project({'name': 'P', 'objective': 'O'})['id']
        self.device = LatchingDevice()
        self.gateway = Gateway(self.device, TOKEN, self.tmp.name + '/edge')
        self.server = Server(edge_handler(self.gateway))
        self.robot = self.app.add_robot({'project_id': self.pid, 'name': '真机 A', 'family': 'mobile',
                                         'endpoint': self.server.url, 'token': TOKEN})
        self.app.connect_robot({'robot_id': self.robot['id']})
        self.status = {'estop_latched': False, 'stop_epoch': 1, 'telemetry': {'last_seq': 0},
                       'latched_motion': {'enabled': True, 'restops': 0, 'invalid_restops': 0, 'feedback_ok': True,
                                          'feedback_problems': {}, 'stop_not_obeyed': False}}
        self.events = []
        self.asked = []
        self.down = False
        self.clock = [1000.0]
        real_call = connector.call

        def call(endpoint, path, token='', payload=None, timeout=8):
            if endpoint != self.server.url:
                return real_call(endpoint, path, token, payload, timeout)
            self.asked.append(path)
            if self.down:
                raise connector.DeviceError('unreachable', code='device_unreachable', sent=False)
            if path == '/v1/status':
                return json.loads(json.dumps(self.status))
            if path.startswith('/v2/telemetry'):
                return {'events': self.events, 'last_seq': len(self.events)}
            return real_call(endpoint, path, token, payload, timeout)
        self.patches = [patch.object(connector, 'call', call),
                        patch.object(safety_watch.time, 'monotonic', lambda: self.clock[0])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.server.close()
        self.gateway.store.close()
        self.app.close()
        self.tmp.cleanup()

    def watch(self, **extra):
        return self.app.robot_safety({'project_id': self.pid, **extra})

    def entry(self, out):
        return next(e for e in out['robots'] if e['robot_id'] == self.robot['id'])

    def latch(self, not_obeyed=False):
        self.status['estop_latched'] = True
        self.status['stop_epoch'] = 2
        self.status['latched_motion'].update(restops=12, stop_not_obeyed=not_obeyed,
                                             stop_not_obeyed_at=1700000000.0 if not_obeyed else None,
                                             stop_not_obeyed_detail={'restops': 12, 'displacement_m': 0.4} if not_obeyed else None)

    def test_real_robot_quiet_then_alert_then_cleared(self):
        out = self.watch()
        e = self.entry(out)
        self.assertEqual((e['simulated'], e['desktop_sim'], e['latched'], e['stop_not_obeyed']), (False, False, False, None))
        self.assertEqual(out['alerts'], [])
        # latched elsewhere, then the robot keeps moving: the event arrives through telemetry
        self.latch()
        self.events = [{'seq': 1, 'type': 'safety.estop'},
                       {'seq': 2, 'type': 'safety.stop_not_obeyed', 'ts': 1700000000.0, 'restops': 12, 'displacement_m': 0.4,
                        'note': '多次停止后仍在运动'}]
        self.clock[0] += 11
        out = self.watch()
        e = self.entry(out)
        self.assertTrue(e['latched'])
        self.assertEqual(out['alerts'], [self.robot['id']])
        self.assertEqual((e['stop_not_obeyed']['restops'], e['stop_not_obeyed']['ts']), (12, 1700000000.0))
        self.assertTrue(self.app.store.get('robots', self.robot['id'])['estop_latched'])     # badges everywhere
        # released on site (unlatched): the alert goes
        self.status['estop_latched'] = False
        self.clock[0] += 2.5
        out = self.watch()
        self.assertEqual((out['alerts'], self.entry(out)['stop_not_obeyed']), ([], None))

    def test_status_flag_alone_raises_the_alert(self):
        self.latch(not_obeyed=True)
        out = self.watch()
        e = self.entry(out)
        self.assertEqual(out['alerts'], [self.robot['id']])
        self.assertEqual((e['stop_not_obeyed']['restops'], e['stop_not_obeyed']['ts']), (12, 1700000000.0))

    def test_bounded_rate(self):
        self.watch()
        n = self.asked.count('/v1/status')
        self.clock[0] += 5                     # quiet robot: not before 10 s
        self.watch()
        self.assertEqual(self.asked.count('/v1/status'), n)
        self.clock[0] += 5.1
        self.watch()
        self.assertEqual(self.asked.count('/v1/status'), n + 1)
        self.latch()
        self.clock[0] += 10.1
        self.watch()
        n = self.asked.count('/v1/status')
        self.clock[0] += 1                     # latched: every 2 s
        self.watch()
        self.assertEqual(self.asked.count('/v1/status'), n)
        self.clock[0] += 1.1
        self.watch()
        self.assertEqual(self.asked.count('/v1/status'), n + 1)
        self.clock[0] += 1.1                   # the robot the user looks at: every 1 s
        self.watch(robot_id=self.robot['id'])
        self.assertEqual(self.asked.count('/v1/status'), n + 2)

    def test_unreachable_keeps_the_alert(self):
        self.latch(not_obeyed=True)
        self.watch()
        self.down = True
        self.clock[0] += 3
        out = self.watch()
        e = self.entry(out)
        self.assertEqual((e['reachable'], out['alerts']), (False, [self.robot['id']]))

    def test_reset_through_the_desktop_clears_at_once(self):
        self.latch(not_obeyed=True)
        self.watch()
        robot = self.app.store.get('robots', self.robot['id'])
        self.app.note_estop(robot, False)
        self.app.store.put('robots', robot)
        self.status['estop_latched'] = False
        out = self.watch()                     # not due yet, but the desktop saw the reset
        self.assertEqual(out['alerts'], [])

    def test_relaxed_and_calibration_reported(self):
        self.status['latched_motion'].update(relaxed=True, config={'pos_m': 0.2}, config_source={'pos_m': 'manifest'})
        out = self.watch()
        e = self.entry(out)
        self.assertEqual((out['relaxed'], e['relaxed']['fields'], e['calibration']['fields']['pos_m']['source']),
                         ([self.robot['id']], ['pos_m'], 'manifest'))

    def test_relaxed_event_from_telemetry(self):
        self.latch()
        self.events = [{'seq': 1, 'type': 'safety.latch_guard_relaxed', 'message': '标定放宽'}]
        e = self.entry(self.watch())
        self.assertEqual((e['relaxed']['relaxed'], e['relaxed']['message']), (True, '标定放宽'))

    def test_other_projects_and_unconnected_robots_are_not_asked(self):
        other = self.app.add_project({'name': 'Q', 'objective': 'O'})['id']
        self.app.add_robot({'project_id': other, 'name': 'B', 'family': 'mobile', 'endpoint': 'http://127.0.0.1:9', 'token': 'x'})
        self.app.add_robot({'project_id': self.pid, 'name': 'C', 'family': 'mobile', 'endpoint': 'http://127.0.0.1:9', 'token': 'x'})
        out = self.watch()
        self.assertEqual([e['robot_id'] for e in out['robots']], [self.robot['id']])

    def test_hardware_estop_from_manifest(self):
        robot = self.app.store.get('robots', self.robot['id'])
        robot['manifest'] = {**robot['manifest'], 'safety': {'hardware_estop': False}}
        self.app.store.put('robots', robot)
        self.assertIs(self.entry(self.watch())['hardware_estop'], False)


class LatchViewDetailTests(unittest.TestCase):
    def test_status_flag_uses_the_detail(self):
        from types import SimpleNamespace
        lm = {'enabled': True, 'restops': 3, 'stop_not_obeyed': True, 'stop_not_obeyed_at': 1700000000.0,
              'stop_not_obeyed_detail': {'restops': 11, 'displacement_m': 0.3, 'evidence': {'x': 1}}}
        v = sim_robots.latch_view(SimpleNamespace(), {'id': 'r'}, {'estop_latched': True, 'latched_motion': lm}, True)
        self.assertEqual(v['stop_not_obeyed'], {'restops': 11, 'displacement_m': 0.3, 'ts': 1700000000.0})


if __name__ == '__main__':
    unittest.main()
