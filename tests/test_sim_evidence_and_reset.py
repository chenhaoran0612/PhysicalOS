"""R-08 simulation evidence and R-07 e-stop reset in the desktop control plane.

Device doubles below are test-only protocol fixtures served through the real
brainos.edge gateway; no robot is moved. One test also drives the runtime's
simulated quadruped (runtime/neuxmind_runtime) when it can be imported.
"""
import json
import os
os.environ.setdefault('NEUXMIND_MAIL_DEV_ECHO', '1')  # registration codes come back in the API response
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from brainos.contracts import validate_manifest
from brainos.delivery import SIMULATION_EVIDENCE_MESSAGE
from brainos.edge import Gateway, make_handler as edge_handler
from brainos.server import Application, make_handler
from brainos.service import Platform, digest
from test_product import DeviceFixture, Server

TOKEN = 'test-only-token-123456789000'
SIM_TEXT = '仿真本体的测试结果不能作为现场证据'


class SimulatedDevice(DeviceFixture):
    """A simulated body that honestly labels itself and its evidence."""

    def manifest(self):
        return {**super().manifest(), 'instance_id': 'sim-device', 'simulated': True,
                'vendor': 'NeuxMind', 'model': 'sim-mobile'}

    def status(self):
        return {'instance_id': 'sim-device', 'mode': 'idle', 'simulated': True}

    def evaluate(self, p):
        self.calls.append('evaluate')
        return {'passed': True, 'application_digest': p['application']['digest'],
                'robot_manifest_digest': digest(self.manifest()), 'simulated': True,
                'evidence_kind': 'simulation', 'evidence_ref': 'simulation://runs/1'}


class LatchingDevice(DeviceFixture):
    """A real-device double whose stop latches the e-stop until /v1/reset."""

    def __init__(self):
        super().__init__()
        self.latched = False
        self.reset_fails = False

    def manifest(self):
        return {**super().manifest(), 'simulated': False, 'vendor': 'Fixture', 'model': 'latching'}

    def status(self):
        return {'instance_id': 'test-device', 'mode': 'idle', 'estop_latched': self.latched,
                'latch_reason': '现场停止' if self.latched else None}

    def validate(self, application):
        self.calls.append('validate')
        cases = [{'name': '急停未锁存', 'passed': not self.latched, 'detail': '急停已锁存' if self.latched else '可运动'}]
        return {'accepted': not self.latched, 'passed': not self.latched, 'cases': cases}

    def start(self, p):
        if self.latched:
            raise RuntimeError('estop_latched')
        return super().start(p)

    def stop(self, p):
        self.latched = True
        return super().stop(p)

    def reset(self, p):
        self.calls.append('reset')
        if self.reset_fails:
            raise RuntimeError('fixture: reset outcome lost')
        self.latched = False
        return {'accepted': True, 'latched': False}


class Base(unittest.TestCase):
    device_class = DeviceFixture

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Platform(self.tmp.name + '/platform')
        self.pid = self.app.add_project({'name': 'R-07/R-08', 'objective': 'evidence and reset'})['id']
        self.device = self.device_class()
        self.gateway = Gateway(self.device, TOKEN, self.tmp.name + '/edge')
        self.server = Server(edge_handler(self.gateway))

    def tearDown(self):
        self.server.close()
        self.gateway.store.close()
        self.app.close()
        self.tmp.cleanup()

    def workflow(self):
        robot = self.app.add_robot({'project_id': self.pid, 'name': 'Robot', 'family': 'mobile',
                                    'endpoint': self.server.url, 'token': TOKEN})
        self.app.connect_robot({'robot_id': robot['id']})
        skill = self.app.add_skill({'project_id': self.pid, 'name': 'Navigate', 'version': '1.0',
                                    'requires': ['navigation.goal'], 'entrypoint': 'navigate',
                                    'success_condition': 'at goal', 'stop_condition': 'timeout', 'model_ids': []})
        app = self.app.add_application({'project_id': self.pid, 'name': 'Inspection', 'version': '1.0',
                                        'robot_id': robot['id'],
                                        'steps': [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 30}]})
        return self.app.store.get('robots', robot['id']), app

    def read(self, app):
        return self.app.delivery_readiness({'application_id': app['id'], 'project_id': self.pid})

    def onsite(self, app):
        return self.app.onsite_test({'application_id': app['id'], 'hardware_authorized': True})


class ManifestTests(unittest.TestCase):
    def test_v1_identity_fields_are_kept_and_legacy_manifest_is_unchanged(self):
        legacy = DeviceFixture().manifest()
        self.assertEqual(validate_manifest(legacy), legacy)
        sim = {**legacy, 'simulated': True, 'vendor': 'NeuxMind', 'model': 'sim', 'unexpected': 1}
        kept = validate_manifest(sim)
        self.assertEqual((kept['simulated'], kept['vendor'], kept['model']), (True, 'NeuxMind', 'sim'))
        self.assertNotIn('unexpected', kept)
        self.assertNotEqual(digest(kept), digest(validate_manifest({**sim, 'simulated': False})))
        for bad in ({**legacy, 'simulated': 'yes'}, {**legacy, 'vendor': ''}):
            with self.assertRaises(ValueError):
                validate_manifest(bad)


class SimulationEvidenceTests(Base):
    device_class = SimulatedDevice

    def test_simulated_body_report_is_simulation_scope_and_cannot_be_deployed(self):
        robot, app = self.workflow()
        self.assertTrue(robot['manifest']['simulated'])
        self.assertTrue(self.app.snapshot()['robots'][0]['manifest']['simulated'])
        op = self.onsite(app)
        self.assertEqual((op['status'], op['scope'], op['simulated']), ('succeeded', 'simulation', True))
        report = self.app.store.get('evaluations', op['report_id'])
        self.assertEqual(report['scope'], 'simulation')
        self.assertTrue(report['passed'])                 # kept as returned
        self.assertTrue(report['simulated'])
        self.assertEqual(report['evidence_note'], SIMULATION_EVIDENCE_MESSAGE)
        self.assertGreaterEqual(len(report['simulation_reasons']), 3)
        d = self.read(app)
        self.assertIsNone(d['onsite_report'])
        self.assertEqual(d['simulation_report']['id'], report['id'])
        self.assertTrue(d['robot']['simulated'])
        self.assertEqual(d['simulation_notice'], SIMULATION_EVIDENCE_MESSAGE)
        self.assertEqual(d['next_step'], 'onsite')
        self.assertFalse(d['can_publish'])
        with self.assertRaisesRegex(ValueError, SIM_TEXT):
            self.app.deploy({'application_id': app['id'], 'report_id': report['id']})
        self.assertNotIn('deploy', self.device.calls)

    def test_pre_fix_onsite_record_with_simulation_details_is_refused(self):
        robot, app = self.workflow()
        op = self.onsite(app)
        report = self.app.store.get('evaluations', op['report_id'])
        report.update(scope='onsite', id='legacy-report')
        for key in ('simulated', 'simulation_reasons', 'evidence_note'):
            report.pop(key)
        self.app.store.put('evaluations', report)
        self.assertIsNone(self.read(app)['onsite_report'])
        with self.assertRaisesRegex(ValueError, SIM_TEXT):
            self.app.deploy({'application_id': app['id'], 'report_id': 'legacy-report'})
        release = {'id': 'legacy-release', 'project_id': self.pid, 'application_id': app['id'],
                   'application_digest': app['digest'], 'robot_id': robot['id'], 'report_id': 'legacy-report',
                   'remote_release_id': 'edge-release-1', 'status': 'deployed', 'created_at': report['created_at']}
        self.app.store.put('deployments', release)
        with self.assertRaisesRegex(ValueError, SIM_TEXT):
            self.app.operate({'release_id': 'legacy-release', 'action': 'start', 'hardware_authorized': True})
        self.assertNotIn('start', self.device.calls)
        self.assertEqual(self.app.operate({'release_id': 'legacy-release', 'action': 'stop'})['status'], 'accepted')


class RealDeviceEvidenceTests(Base):
    device_class = LatchingDevice

    def test_real_device_report_is_onsite_and_deployable(self):
        _, app = self.workflow()
        op = self.onsite(app)
        report = self.app.store.get('evaluations', op['report_id'])
        self.assertEqual(report['scope'], 'onsite')
        self.assertNotIn('simulated', report)
        self.assertFalse(self.read(app)['robot']['simulated'])
        release = self.app.deploy({'application_id': app['id'], 'report_id': report['id']})
        self.assertEqual(release['status'], 'deployed')
        started = self.app.operate({'release_id': release['id'], 'action': 'start', 'hardware_authorized': True})
        self.assertEqual(started['status'], 'accepted')

    def test_simulation_markers_on_a_non_simulated_manifest_still_mean_simulation(self):
        _, app = self.workflow()
        evaluate = self.device.evaluate
        for change in ({'evidence_kind': 'simulation'}, {'simulated': True}, {'evidence_ref': 'simulation://x'},
                       {'evidence_kind': None}):
            with patch.object(self.device, 'evaluate', side_effect=lambda p: {
                    k: v for k, v in {**evaluate(p), **change}.items() if v is not None}):
                op = self.onsite(app)
            self.assertEqual(op['scope'], 'simulation', change)
            with self.assertRaisesRegex(ValueError, SIM_TEXT):
                self.app.deploy({'application_id': app['id'], 'report_id': op['report_id']})
        self.assertNotIn('deploy', self.device.calls)


class ResetTests(Base):
    device_class = LatchingDevice

    def released(self):
        robot, app = self.workflow()
        op = self.onsite(app)
        release = self.app.deploy({'application_id': app['id'], 'report_id': op['report_id']})
        return robot, app, release

    def test_stop_latches_reset_needs_authorization_then_start_works(self):
        robot, app, release = self.released()
        self.assertFalse(self.read(app)['estop']['latched'])
        stopped = self.app.operate({'release_id': release['id'], 'action': 'stop'})
        self.assertEqual((stopped['status'], stopped['robot_id']), ('accepted', robot['id']))
        d = self.read(app)
        self.assertTrue(d['estop']['latched'])
        self.assertEqual(d['estop']['source'], 'last_stop')
        self.assertTrue(d['can_reset'])
        # validate reports the latch: on-site testing explains how to proceed.
        with self.assertRaisesRegex(ValueError, '急停已锁存'):
            self.onsite(app)
        self.assertEqual(self.read(app)['estop']['source'], 'device')
        # start is refused locally with a clear message; the device is not asked.
        with self.assertRaisesRegex(ValueError, '解除急停'):
            self.app.operate({'release_id': release['id'], 'action': 'start', 'hardware_authorized': True})
        self.assertNotIn('start', self.device.calls)
        for p in ({'robot_id': robot['id']}, {'robot_id': robot['id'], 'hardware_authorized': 'true'}):
            with self.assertRaisesRegex(ValueError, '现场确认'):
                self.app.reset_robot(p)
        self.assertNotIn('reset', self.device.calls)
        reset = self.app.reset_robot({'robot_id': robot['id'], 'hardware_authorized': True, 'request_id': 'ui-1'})
        self.assertEqual(reset['status'], 'accepted')
        self.assertEqual(reset['kind'], 'reset')
        again = self.app.reset_robot({'robot_id': robot['id'], 'hardware_authorized': True, 'request_id': 'ui-1'})
        self.assertEqual(again, reset)
        self.assertEqual(self.device.calls.count('reset'), 1)
        with self.assertRaisesRegex(ValueError, '其他操作'):
            self.app.reset_robot({'robot_id': 'robot-other', 'hardware_authorized': True, 'request_id': 'ui-1'})
        d = self.read(app)
        self.assertFalse(d['estop']['latched'])
        self.assertIn('reset', [o['kind'] for o in d['operations']])
        self.assertIn('robot.reset', [e['action'] for e in self.app.store.list('events')])
        started = self.app.operate({'release_id': release['id'], 'action': 'start', 'hardware_authorized': True})
        self.assertEqual(started['status'], 'accepted')
        self.assertIn('start', self.device.calls)

    def test_unconfirmed_reset_is_unknown_and_never_retried(self):
        robot, app, release = self.released()
        self.app.operate({'release_id': release['id'], 'action': 'stop'})
        self.device.reset_fails = True
        first = self.app.reset_robot({'robot_id': robot['id'], 'hardware_authorized': True, 'request_id': 'ui-2'})
        self.assertEqual(first['status'], 'unknown')
        self.assertIn('不会自动重试', first['error'])
        self.device.reset_fails = False
        again = self.app.reset_robot({'robot_id': robot['id'], 'hardware_authorized': True, 'request_id': 'ui-2'})
        self.assertEqual(again['status'], 'unknown')
        self.assertEqual(self.device.calls.count('reset'), 1)
        self.assertTrue(self.read(app)['estop']['latched'])
        # Without a device-reported latch, an unknown reset still leaves the stop inference in place.
        robot = self.app.store.get('robots', robot['id'])
        robot.pop('estop_latched')
        self.app.store.put('robots', robot)
        self.assertEqual(self.read(app)['estop']['source'], 'last_stop')

    def test_stop_stays_available_while_latched(self):
        _, _, release = self.released()
        for _ in range(2):
            self.assertEqual(self.app.operate({'release_id': release['id'], 'action': 'stop'})['status'], 'accepted')

    def test_reset_api_route(self):
        robot, app, release = self.released()
        self.app.operate({'release_id': release['id'], 'action': 'stop'})
        application = Application(self.tmp.name + '/server')
        server = Server(make_handler(application))
        try:
            port = server.http.server_port

            def post(path, payload, cookie=''):
                req = urllib.request.Request(f'http://127.0.0.1:{port}/api/' + path, data=json.dumps(payload).encode(),
                                             headers={'Content-Type': 'application/json', 'Cookie': cookie})
                try:
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req) as r:
                        return r.status, json.loads(r.read()), r.headers.get('Set-Cookie', '')
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read()), ''
            form = {'name': 'QA', 'email': 'qa@example.test', 'password': 'fixture-password-1'}
            _, user, cookie = post('register', {**form, 'code': post('register/start', form)[1]['dev_code']})
            cookie = cookie.split(';')[0]
            workspace = application.workspace(user['user'])
            pid = workspace.add_project({'name': 'P', 'objective': 'O'})['id']
            remote = workspace.add_robot({'project_id': pid, 'name': 'R', 'family': 'mobile',
                                          'endpoint': self.server.url, 'token': TOKEN})
            status, body, _ = post('robots/reset', {'robot_id': remote['id']}, cookie)
            self.assertEqual(status, 400)
            self.assertIn('现场确认', body['error'])
            status, body, _ = post('robots/reset', {'robot_id': remote['id'], 'hardware_authorized': True}, cookie)
            self.assertEqual((status, body['status']), (200, 'accepted'))
        finally:
            server.close()
            application.close()


def runtime_fixture():
    runtime_tests = Path(__file__).resolve().parents[1] / 'runtime' / 'tests'
    if str(runtime_tests) not in sys.path:
        sys.path.insert(0, str(runtime_tests))
    import _support
    return _support


class RuntimeSimulatedBodyTests(unittest.TestCase):
    """The real runtime simulated quadruped: its evidence must never count as on-site."""

    def setUp(self):
        try:
            self.support = runtime_fixture()
            self.fx = self.support.RuntimeFixture(http=True, time_scale=20.0)
        except Exception as exc:          # pragma: no cover - depends on runtime state
            self.skipTest(f'runtime 仿真本体不可用：{exc}')
        self.fx.install(self.support.example('status_report'))
        self.tmp = tempfile.TemporaryDirectory()
        self.platform = Platform(self.tmp.name)

    def tearDown(self):
        self.platform.close()
        self.fx.close()
        self.tmp.cleanup()

    def test_runtime_sim_report_is_simulation_and_deploy_is_refused(self):
        p = self.platform
        pid = p.add_project({'name': 'sim', 'objective': 'sim'})['id']
        robot = p.add_robot({'project_id': pid, 'name': 'SimQuadruped', 'family': 'quadruped',
                             'endpoint': self.fx.url, 'token': self.support.TOKEN})
        robot = p.connect_robot({'robot_id': robot['id']})
        skill = p.add_skill({'project_id': pid, 'name': '状态报告', 'version': '0.1.0', 'requires': ['system.health'],
                             'entrypoint': 'status_report', 'success_condition': '报告生成', 'stop_condition': '超时',
                             'model_ids': []})
        app = p.add_application({'project_id': pid, 'name': '状态', 'version': '1.0', 'robot_id': robot['id'],
                                 'steps': [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 30}]})
        op = p.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        # Round 02 (INTERFACES 4.1): a runtime with asynchronous evaluation answers
        # "running" first; the desktop polls until the result is in.
        deadline = time.monotonic() + 20
        while op['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.1)
            op = p.poll_onsite({'operation_id': op['id']})
        self.assertEqual(op['status'], 'succeeded', op.get('error'))
        report = p.store.get('evaluations', op['report_id'])
        self.assertEqual(report['scope'], 'simulation')
        self.assertTrue(report['evidence_ref'].startswith('simulation://'))
        with self.assertRaisesRegex(ValueError, SIM_TEXT):
            p.deploy({'application_id': app['id'], 'report_id': report['id']})


if __name__ == '__main__':
    unittest.main()
