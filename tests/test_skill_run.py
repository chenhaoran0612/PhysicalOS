"""Issue #42: a skill that was just installed can be run at once, on a real robot too.

Until now the run shortcut (``robots/run-skill``) was for the console's own simulated robots only; a
real robot had to go through an application, an on-site test and a deployment before an installed skill
could run. Here the console runs an installed skill on the real edge runtime behind its HTTP gateway.

The body is the simulated quadruped with a manifest that does not call itself simulated
(``runtime/tests/_limit_bodies.py``), so the runtime treats it as a real body: a person has to fill in
its safety limits, and the console asks for a second confirmation that somebody is on site. Nothing
here reaches a real machine.
"""
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from brainos import connector, sim_robots, skill_run
from brainos.errors import CodedError
from brainos.server import Application, make_handler
from brainos.service import Platform
from test_product import Server

ROOT = Path(__file__).resolve().parents[1]

PERSON = {'id': 'user-1', 'name': '张工', 'email': 'zhang@example.test'}
FILLED = {'max_linear_speed': 0.5, 'max_angular_speed': 0.8, 'command_timeout_s': 0.5}


def runtime_tests():
    path = Path(__file__).resolve().parents[1] / 'runtime' / 'tests'
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
    import _limit_bodies
    import _support
    return _support, _limit_bodies


def wait_for(predicate, seconds=60.0, step=0.1):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


class RunOnARealRobot(unittest.TestCase):
    def setUp(self):
        try:
            self.support, bodies = runtime_tests()
            self.state = tempfile.TemporaryDirectory()
            self.adapter = bodies.RealQuadruped(time_scale=20.0)
            self.runtime = self.support.EdgeRuntime(self.adapter, self.state.name, sandbox='inprocess')
            self.server = self.support.GatewayServer(self.runtime, self.support.TOKEN, port=0).start()
        except Exception as exc:          # pragma: no cover - depends on the runtime being importable
            self.skipTest(f'runtime 不可用：{exc}')
        self.addCleanup(self.close_runtime)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.platform = Platform(self.tmp.name)
        self.addCleanup(self.platform.close)
        self.pid = self.platform.add_project({'name': 'Run', 'objective': 'run an installed skill'})['id']
        self.robot_id = self.platform.add_robot({'project_id': self.pid, 'name': 'Dog', 'family': 'quadruped',
                                                 'endpoint': self.server.url, 'token': self.support.TOKEN})['id']
        self.platform.connect_robot({'robot_id': self.robot_id})

    def close_runtime(self):
        self.server.close()
        self.runtime.runner.stop_all('test teardown')
        self.runtime.supervisor.halt_motion('test teardown')
        self.runtime.close()
        self.state.cleanup()

    def fill_limits(self):
        self.platform.safety_limits.save({'robot_id': self.robot_id, 'project_id': self.pid,
                                          'limits': {'base': FILLED}}, PERSON)

    def install(self, example):
        preview = self.platform.preview_skill_package({'robot_id': self.robot_id, 'example': example})
        self.assertTrue(preview['can_install'], preview['compatibility'])
        done = self.platform.install_skill_package({'robot_id': self.robot_id, 'example': example,
                                                    'request_id': 'install-' + example,
                                                    'package_digest': preview['package_digest']})
        self.assertEqual(done['status'], 'succeeded', done)

    def run_skill(self, skill, request_id, parameters, **confirmations):
        return self.platform.run_robot_skill({'robot_id': self.robot_id, 'project_id': self.pid, 'skill_id': skill,
                                              'request_id': request_id, 'parameters': parameters, **confirmations})

    def live(self):
        return self.platform.robot_live({'robot_id': self.robot_id, 'project_id': self.pid})

    def ended(self, op, seconds=60.0):
        def done():
            run = self.live().get('run') or {}
            return run if run.get('id') == op['run_id'] and run.get('status') not in ('queued', 'running') else None
        run = wait_for(done, seconds)
        self.assertTrue(run, f'运行没有在 {seconds:g} 秒内结束')
        return run

    def device_runs(self):
        return connector.call(self.server.url, '/v2/runs', self.support.TOKEN)['runs']

    def pose(self):
        return (self.adapter.x, self.adapter.y)

    # ---------------------------------------------------------------- the two confirmations
    def test_the_robot_is_treated_as_real(self):
        robot = self.platform.store.get('robots', self.robot_id)
        self.assertFalse(skill_run.is_simulated(robot))

    def test_without_the_on_site_confirmation_nothing_is_sent(self):
        self.fill_limits()
        self.install('goto_place')
        with self.assertRaises(CodedError) as refused:
            self.run_skill('goto_place', 'r1', {'place': '会议室'}, hardware_authorized=True)
        self.assertEqual(refused.exception.code, 'onsite_confirmation_required')
        self.assertIn('现场', str(refused.exception))
        self.assertEqual([o for o in self.platform.store.list('operations') if o['kind'] == 'skill_run'], [])
        self.assertEqual(self.device_runs(), [])
        self.assertEqual(self.pose(), (0.0, 0.0))

    def test_without_the_authorization_nothing_is_sent(self):
        self.fill_limits()
        self.install('goto_place')
        with self.assertRaises(CodedError) as refused:
            self.run_skill('goto_place', 'r1', {'place': '会议室'}, onsite_confirmed=True)
        self.assertEqual(refused.exception.code, 'authorization_required')
        self.assertEqual(self.device_runs(), [])

    def test_only_a_literal_true_counts_as_the_on_site_confirmation(self):
        self.fill_limits()
        self.install('goto_place')
        for value in ('true', 1, False, None, 'yes'):
            with self.subTest(value=value), self.assertRaises(CodedError) as refused:
                self.run_skill('goto_place', 'r1', {'place': '会议室'}, hardware_authorized=True, onsite_confirmed=value)
            self.assertEqual(refused.exception.code, 'onsite_confirmation_required')
        self.assertEqual(self.device_runs(), [])

    def test_a_device_that_calls_itself_simulated_cannot_waive_it(self):
        robot = self.platform.store.get('robots', self.robot_id)
        robot['manifest'] = {**robot['manifest'], 'simulated': True}      # what a mislabelled adapter would report
        self.platform.store.put('robots', robot)
        self.assertFalse(skill_run.is_simulated(robot))
        with self.assertRaises(CodedError) as refused:
            self.run_skill('goto_place', 'r1', {'place': '会议室'}, hardware_authorized=True)
        self.assertEqual(refused.exception.code, 'onsite_confirmation_required')
        self.assertEqual(self.device_runs(), [])

    # ---------------------------------------------------------------- running
    def test_an_installed_skill_runs_with_the_parameters_from_the_form(self):
        self.fill_limits()
        self.install('goto_place')
        installed = self.platform.robot_detail({'robot_id': self.robot_id, 'project_id': self.pid})['skills']['items']
        place = next(p for p in installed[0]['parameters'] if p['name'] == 'place')
        self.assertTrue(place['required'])
        self.assertEqual([o['value'] for o in place['options']], ['会议室', '二楼'])
        op = self.run_skill('goto_place', 'r1', {'place': '会议室', 'max_speed': 0.4},
                            hardware_authorized=True, onsite_confirmed=True)
        self.assertEqual(op['status'], 'accepted', op)
        self.assertEqual((op['simulated'], op['onsite_confirmed']), (False, True))
        self.assertEqual(op['parameters'], {'place': '会议室', 'max_speed': 0.4})
        run = self.ended(op)
        self.assertEqual(run['status'], 'succeeded', run)
        self.assertEqual(run['result']['place'], '会议室')
        self.assertAlmostEqual(self.adapter.x, 3.0, delta=0.2)
        audit = [e for e in self.platform.store.list('events') if e['action'] == 'skill.run']
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]['details']['simulated'], audit[0]['details']['onsite_confirmed']), (False, True))

    def test_the_same_request_is_not_sent_twice(self):
        self.fill_limits()
        self.install('goto_place')
        first = self.run_skill('goto_place', 'r1', {'place': '会议室'}, hardware_authorized=True, onsite_confirmed=True)
        again = self.run_skill('goto_place', 'r1', {'place': '会议室'}, hardware_authorized=True, onsite_confirmed=True)
        self.assertEqual(again['run_id'], first['run_id'])
        # the recorded outcome comes back for the same id whatever else the request says: nothing is sent
        self.assertEqual(self.run_skill('goto_place', 'r1', {'place': '二楼'}, hardware_authorized=True)['run_id'], first['run_id'])
        self.assertEqual(len(self.device_runs()), 1)
        self.ended(first)

    def test_a_value_outside_the_declaration_is_refused_by_the_device(self):
        self.fill_limits()
        self.install('goto_place')
        op = self.run_skill('goto_place', 'bad', {'place': '天台'}, hardware_authorized=True, onsite_confirmed=True)
        self.assertEqual((op['status'], op['code'], op['param']), ('rejected', 'param_choice', 'place'))
        self.assertEqual(self.pose(), (0.0, 0.0))

    def test_stop_ends_the_run_and_latches(self):
        self.fill_limits()
        self.install('free_walk')
        op = self.run_skill('free_walk', 'walk', {'duration_s': 60, 'radius': 2.0},
                            hardware_authorized=True, onsite_confirmed=True)
        self.assertEqual(op['status'], 'accepted', op)
        self.assertTrue(wait_for(lambda: abs(self.adapter.x) + abs(self.adapter.y) > 0.2, 30), '自由走开始后位置没有变化')
        self.assertEqual(self.platform.stop_robot({'robot_id': self.robot_id})['status'], 'accepted')
        run = self.ended(op, 15)
        self.assertEqual(run['status'], 'stopped')
        self.assertTrue(self.live()['estop']['latched'])
        with self.assertRaises(CodedError) as refused:
            self.run_skill('free_walk', 'walk-2', {'duration_s': 60}, hardware_authorized=True, onsite_confirmed=True)
        self.assertEqual(refused.exception.code, 'estop_latched')

    # ---------------------------------------------------------------- the device's own protection stays
    def test_without_filled_limits_the_robot_does_not_move_and_the_reason_is_shown(self):
        self.install('goto_place')
        op = self.run_skill('goto_place', 'r1', {'place': '会议室'}, hardware_authorized=True, onsite_confirmed=True)
        if op['status'] == 'accepted':                  # the run starts; its first motion call is refused
            run = self.ended(op)
            self.assertEqual(run['status'], 'failed', run)
            self.assertIn('安全限值', run['error'])
        else:
            self.assertEqual((op['status'], op['code']), ('rejected', 'safety_limits_missing'), op)
        self.assertEqual(self.pose(), (0.0, 0.0))


class RunOnASimulatedRobot(unittest.TestCase):
    """The console's own simulated robot keeps its one confirmation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-issue42-test-')
        self.addCleanup(self.tmp.cleanup)
        self.app = Platform(self.tmp.name + '/ws')
        self.addCleanup(self.app.close)
        self.quad = self.app.add_sim_robot({'kind': 'quadruped'})

    def test_one_confirmation_is_enough(self):
        self.assertTrue(skill_run.is_simulated(self.app.store.get('robots', self.quad['id'])))
        preview = self.app.preview_skill_package({'robot_id': self.quad['id'], 'example': 'status_report'})
        self.app.install_skill_package({'robot_id': self.quad['id'], 'example': 'status_report', 'request_id': 'i1',
                                        'package_digest': preview['package_digest']})
        op = self.app.run_robot_skill({'robot_id': self.quad['id'], 'skill_id': 'status_report', 'request_id': 'r1',
                                       'parameters': sim_robots.EXAMPLE_PRESETS['status_report'],
                                       'hardware_authorized': True})
        self.assertEqual(op['status'], 'accepted', op)
        self.assertEqual((op['simulated'], op.get('onsite_confirmed')), (True, None))


class PageTests(unittest.TestCase):
    """The console serves web/skill_run.js and loads it; its rules are checked in tests/skill_run_logic.cjs."""

    def test_the_page_script_is_served_and_loaded_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = Application(tmp.name)
        self.addCleanup(app.close)
        server = Server(make_handler(app))
        self.addCleanup(server.close)
        with urllib.request.urlopen(server.url + '/skill_run.js', timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertTrue(response.headers.get('Content-Type', '').startswith('text/javascript'))
            self.assertEqual(response.read(), (ROOT / 'web' / 'skill_run.js').read_bytes())
        with urllib.request.urlopen(server.url + '/', timeout=10) as response:
            self.assertEqual(response.read().decode('utf-8').count('<script src="/skill_run.js" defer></script>'), 1)

    def test_the_page_rules_hold(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        result = subprocess.run([node, 'tests/skill_run_logic.cjs'], capture_output=True, text=True,
                                timeout=120, cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'skill_run_logic: \d+ checks passed')


if __name__ == '__main__':
    unittest.main()
