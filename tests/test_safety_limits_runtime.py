"""Issue #22 (R1-06): the console against the real edge runtime.

``tests/test_safety_limits.py`` checks the console against a protocol double. This file checks that
the double and the runtime agree: the same console code reads, fills and re-reads the limits of a
real ``EdgeRuntime`` behind its HTTP gateway, and the runtime's refusals arrive with their fields.

The body is the simulated quadruped with a manifest that does not call itself simulated
(``runtime/tests/_limit_bodies.py``): nothing reaches a real machine.
"""
import sys
import tempfile
import unittest
from pathlib import Path

from brainos import connector
from brainos.errors import error_payload
from brainos.service import Platform

PERSON = {'id': 'user-1', 'name': '张工', 'email': 'zhang@example.test'}
FILLED = {'max_linear_speed': 0.5, 'max_angular_speed': 0.8, 'command_timeout_s': 0.5}
MOVE = '/v2/capabilities/base.velocity/invoke'


def runtime_tests():
    """``runtime/tests/_support.py`` and the stand-in bodies (raises when the runtime is unavailable)."""
    path = Path(__file__).resolve().parents[1] / 'runtime' / 'tests'
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    import _limit_bodies
    import _support
    return _support, _limit_bodies


class ConsoleAgainstTheRuntimeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.support, bodies = runtime_tests()
            self.state = tempfile.TemporaryDirectory()
            self.adapter = bodies.RealQuadruped(time_scale=20.0)
            # no watchdog thread: a velocity setpoint stays until the test ends it
            self.runtime = self.support.EdgeRuntime(self.adapter, self.state.name, sandbox='inprocess',
                                                    start_monitor=False)
            self.server = self.support.GatewayServer(self.runtime, self.support.TOKEN, port=0).start()
        except Exception as exc:          # pragma: no cover - depends on the runtime being importable
            self.skipTest(f'runtime 不可用：{exc}')
        self.addCleanup(self.close_runtime)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.platform = Platform(self.tmp.name)
        self.addCleanup(self.platform.close)
        self.pid = self.platform.add_project({'name': 'Limits', 'objective': 'fill in safety limits'})['id']
        self.robot_id = self.platform.add_robot({'project_id': self.pid, 'name': 'Dog', 'family': 'quadruped',
                                                 'endpoint': self.server.url, 'token': self.support.TOKEN})['id']

    def close_runtime(self):
        self.server.close()
        self.runtime.runner.stop_all('test teardown')
        self.runtime.supervisor.halt_motion('test teardown')
        self.runtime.close()
        self.state.cleanup()

    def move(self, vx, request_id):
        return connector.call(self.server.url, MOVE, self.support.TOKEN,
                              {'args': {'vx': vx, 'wz': 0.0}, 'hardware_authorized': True, 'request_id': request_id})

    def save(self, **changes):
        return self.platform.safety_limits.save({'robot_id': self.robot_id, 'project_id': self.pid,
                                                 'limits': {'base': {**FILLED, **changes}}}, PERSON)

    def stored_summary(self):
        return self.platform.store.get('robots', self.robot_id)['device_status']['safety_limits']

    def test_a_person_fills_in_the_limits_of_a_real_runtime_through_the_console(self):
        # the connection check already tells the robot's card that nothing may move
        self.platform.connect_robot({'robot_id': self.robot_id})
        self.assertEqual((self.stored_summary()['motion_blocked'], self.stored_summary()['filled']), (True, False))

        view = self.platform.safety_limits.read({'robot_id': self.robot_id, 'project_id': self.pid})
        self.assertEqual((view['supported'], view['required'], view['filled'], view['motion_blocked']),
                         (True, True, False, True))
        self.assertIn('安全限值', view['reason'])
        self.assertEqual(view['bounds']['base']['max_linear_speed'],
                         {'unit': 'm/s', 'min': 0.0, 'min_exclusive': True, 'max': 1.0, 'platform_max': 3.0,
                          'body_max': 1.0})

        with self.assertRaises(connector.DeviceError) as refused:
            self.move(0.2, 'move-1')
        self.assertEqual((refused.exception.status, refused.exception.code), (409, 'safety_limits_missing'))
        self.assertEqual(self.adapter.cmd, (0.0, 0.0, 0.0))

        # above what the body declares: the runtime's refusal reaches the form with its field and range
        with self.assertRaises(connector.DeviceError) as refused:
            self.save(max_linear_speed=1.5)
        payload = error_payload(refused.exception)
        self.assertEqual((payload['code'], payload['field'], payload['max'], payload['bound']),
                         ('limits_out_of_range', 'max_linear_speed', 1.0, 'body'))
        self.assertTrue(self.runtime.device_limits.view()['motion_blocked'])

        saved = self.save()
        self.assertEqual((saved['accepted'], saved['filled'], saved['motion_blocked'], saved['set_by']),
                         (True, True, False, '张工'))
        self.assertEqual(saved['limits'], {'base': FILLED})
        on_device = self.runtime.device_limits.view()
        self.assertEqual((on_device['set_by'], on_device['limits'], on_device['revision']), ('张工', {'base': FILLED}, 1))
        self.assertEqual((self.stored_summary()['motion_blocked'], self.stored_summary()['set_by']), (False, '张工'))
        audit = [e for e in self.platform.store.list('events') if e['action'] == 'robot.limits_set']
        self.assertEqual((len(audit), audit[0]['details']['set_by']), (1, '张工'))

        # the very request that was refused now goes through, limited to what was filled in
        self.assertEqual(self.move(5.0, 'move-2'), {'accepted': True})
        self.assertEqual(self.adapter.cmd[0], 0.5)

        # while that setpoint is in effect the runtime does not take a change
        with self.assertRaises(connector.DeviceError) as refused:
            self.save(max_linear_speed=0.3)
        self.assertEqual((refused.exception.status, refused.exception.code), (409, 'runs_active'))
        self.assertEqual(self.platform.safety_limits.read({'robot_id': self.robot_id, 'project_id': self.pid})['revision'], 1)

    def test_the_doubles_view_has_the_keys_of_the_runtimes(self):
        from safety_limits_fixture import LimitsDevice
        double = LimitsDevice()
        self.addCleanup(double.close)
        theirs = connector.call(double.url, '/v2/safety/limits', double.token)
        ours = connector.call(self.server.url, '/v2/safety/limits', self.support.TOKEN)
        self.assertEqual(sorted(theirs), sorted(ours))
        self.assertEqual(sorted(theirs['bounds']['base']['max_linear_speed']), sorted(ours['bounds']['base']['max_linear_speed']))


if __name__ == '__main__':
    unittest.main()
