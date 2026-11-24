"""Issue #29 (R1-10), the acceptance: a person approves, the motion driver is installed and started, the
motion interfaces appear when ROS 2 is read again, and the robot never moved.

The driver package comes from the library first (``driver_packages``, ``driver_package_deliver`` with
issue #31's delivery); installing it and starting it are ``device_fix`` commands; the start waits for a
person on site. The robot is ``driver_install_fixture.DriverDevice`` (it plays the install and the start
from the command text, nothing is run for real), the library is ``ListingLibrary`` on this computer, and
the model of the end-to-end session is a scripted local fixture. Nothing reaches a real machine.
"""
import json
import os
import queue
import random
import re
import secrets
import tempfile
import time
import unittest

from brainos import agent_runtime, delivery, device_access, device_profile, driver_repair, hardware_init, ros2_profile
from brainos.service import Platform, now
from driver_install_fixture import UNIT, DriverDevice, ListingLibrary
from motion_drivers_fixture import chassis
from package_cloud_fixture import sha
from test_device_transfer import quiet_paramiko
from test_engineering import ResponsesFixture
from test_product import Server

VELOCITY, ODOMETRY = 'motion-driver:base.velocity', 'motion-driver:base.odometry'
PACKAGE = random.Random(29).randbytes(120_000)
NAME = 'vendor_motion_driver_1.0_arm64.deb'
# What would move the robot or unlock it, in any command that reached the device.
MOVES = re.compile(r'topic\s+pub|service\s+call|send_goal|param\s+(?:set|load)|lifecycle\s+set|switch_controller|bag\s+play|teleop|'
                   r'/v[12]/(?:reset|start|runs|capabilities|conformance|safety)|neuxmind[-_]runtime|device_install\.sh')


class InstallCase(unittest.TestCase):
    def setUp(self):
        quiet_paramiko(self)
        self.device = DriverDevice()
        self.addCleanup(self.device.close)
        self.cloud = ListingLibrary()
        self.addCleanup(self.cloud.close)
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.platform = Platform(os.path.join(work.name, 'state'))
        self.addCleanup(self.platform.close)
        self.store, self.hw, self.e = self.platform.store, self.platform.hardware, self.platform.engineering
        self.pid = self.platform.add_project({'name': 'Drivers', 'objective': 'Bring up the chassis'})['id']
        connection, secret = self.device.login()
        self.robot_id, self.connection_id = 'robot-chassis', 'connection-chassis'
        # The body's safety state is latched before the repair, as R1-10 asks it to stay.
        self.store.put('robots', {'id': self.robot_id, 'project_id': self.pid, 'name': '底盘机器人', 'family': 'quadruped',
                                  'connection_id': self.connection_id, 'estop_latched': True, 'estop_reason': '急停已按下', 'estop_checked_at': now()})
        self.store.put('device_connections', {**connection, 'id': self.connection_id, 'robot_id': self.robot_id, 'project_id': self.pid})
        self.store.put('agent_secrets', {**secret, 'id': self.connection_id})
        self.plant_profile(chassis('/cmd_vel', '/odom'))
        self.cloud.sign_in(self.platform)
        self.material = self.cloud.add(NAME, PACKAGE)
        self.path = '%s/neuxmind-pkg/library/%s/%s' % (self.device.home, sha(PACKAGE)[:32], NAME)

    def plant_profile(self, ros_output):
        """The robot's device profile as issue #27 keeps it, with the ROS 2 part read from ``ros_output``."""
        connection = self.store.get('device_connections', self.connection_id)
        ros2 = {**ros2_profile.parse(ros_output), 'checked_at': time.time() - 60, 'duration_ms': 1, 'problem': None}
        self.store.put(device_profile.KIND, {'id': device_profile.profile_id(self.robot_id), 'schema': device_profile.SCHEMA,
                                             'project_id': self.pid, 'robot_id': self.robot_id, 'connection_id': self.connection_id,
                                             'connection_revision': connection.get('updated_at'), 'created_at': now(), 'ros2': ros2})

    def profile_topics(self):
        ros2 = self.store.get(device_profile.KIND, device_profile.profile_id(self.robot_id))['ros2']
        return {topic['name'] for topic in ros2['topics'] or []}

    def baseline(self):
        return self.hw.scan({'project_id': self.pid, 'robot_id': self.robot_id})

    def session(self, scan, **hardware):
        s = {'id': 'agent-session-driver', 'project_id': self.pid, 'robot_id': self.robot_id, 'connection_id': self.connection_id,
             'mode': 'repair', 'status': 'running', 'events': [],
             'hardware': {'scan_id': scan['id'], 'issue_ids': [VELOCITY, ODOMETRY], 'skill': hardware_init.skill_info(), 'fixes': 0,
                          'fixed_issue_ids': [], 'rescan_id': None, 'rescan_after_fixes': None, 'ssh_interface': 'eth0', **hardware}}
        self.store.put('agent_sessions', s)
        self.answers = queue.Queue()
        self.e.live[s['id']] = {'answers': self.answers}
        self.addCleanup(self.e.live.pop, s['id'], None)        # runs before the platform closes
        return s

    def ssh(self):
        device = device_access.SSHDevice(*self.platform.ssh_edit.saved_login(self.connection_id))
        self.addCleanup(device.close)
        return device

    def connection(self):
        return self.store.get('device_connections', self.connection_id)

    def ask(self, s, method, request, request_id):
        """The request as the engine queues it for approval; answered at once if the session runs unasked."""
        self.e._queue_request(s['id'], 'approval', {'id': request_id, 'method': method, 'request': request})
        self.hw.policy.answer_waiting(s['id'])

    def approve(self, s, request_id, onsite=False):
        if onsite:
            self.hw.driver.confirm_onsite({'project_id': self.pid, 'session_id': s['id'], 'approval_id': request_id, 'onsite': True})
        self.e.approve({'session_id': s['id'], 'approval_id': request_id, 'decision': 'accept'})
        self.assertEqual(self.answers.get_nowait(), (request_id, {'decision': 'accept'}))

    def fix(self, s, command, **extra):
        args = {'command': command, 'purpose': '安装并启动底盘的运控驱动', 'issue_ids': [VELOCITY, ODOMETRY], 'risk': 'medium',
                'rollback': 'systemctl stop %s；apt-get remove -y ros-humble-vendor-motion-driver' % UNIT, 'needs_root': True,
                'timeout_seconds': 120, 'starts_motion_driver': False, **extra}
        return self.hw.fix_request(s, args, self.connection())

    def assert_nothing_moved(self):
        reached = list(self.device.commands)
        self.assertEqual([c for c in reached if MOVES.search(c)], [], 'something that moves or unlocks the robot reached it')
        self.assertTrue(self.device.latched, "the body's own e-stop stayed latched")
        robot = self.store.get('robots', self.robot_id)
        self.assertIs(robot['estop_latched'], True)
        self.assertTrue(delivery.estop_state(self.platform, robot)['latched'], 'the platform still shows the robot as latched')
        for kind in ('operations', 'deployments', 'model_deployments', 'jobs'):
            self.assertEqual(self.store.list(kind), [], kind)


class PackageToolTests(InstallCase):
    """Driver packages are looked up in the library first; one is sent only after the step was approved."""

    def test_the_library_is_asked_for_driver_packages_first(self):
        parsing = self.cloud.add('vendor_motion_driver_2.0_arm64.deb', b'newer', status='parsing')
        other = self.cloud.add('arm_driver.deb', b'arm', body_type='manipulator')
        self.cloud.add('manual.pdf', b'%PDF', kind='document', fmt='pdf')
        s = self.session(self.baseline())
        sent = len(self.device.commands)

        result = self.hw.tool(s['id'], s, 'driver_packages', {}, None)

        self.assertTrue(result['available'])
        self.assertEqual([p['material_id'] for p in result['packages']], [self.material, other, parsing], 'usable and matching first')
        first = result['packages'][0]
        self.assertEqual((first['filename'], first['usable'], first['matches_body_type'], first['sha256'], first['delivered']),
                         (NAME, True, True, sha(PACKAGE), None))
        self.assertFalse(next(p for p in result['packages'] if p['material_id'] == parsing)['usable'])
        self.assertIn('needs_user', result['note'])
        self.assertEqual(self.store.get('device_evidence', result['evidence_id'])['tool'], 'driver_packages')
        self.assertEqual(self.device.commands[sent:], [], 'looking up the library does not contact the robot')

    def test_without_the_cloud_the_assistant_is_told_to_leave_it_to_the_user(self):
        s = self.session(self.baseline())
        self.platform.cloud._drop_token()
        result = self.hw.tool(s['id'], s, 'driver_packages', {}, None)
        self.assertEqual((result['available'], result['packages']), (False, []))
        self.assertIn('资料库', result['reason'])
        self.assertIn('needs_user', result['note'])

    def test_what_may_not_go_to_the_robot_is_refused_before_anyone_is_asked(self):
        s = self.session(self.baseline())
        parsing = self.cloud.add('vendor_motion_driver_2.0_arm64.deb', b'newer', status='parsing')
        document = self.cloud.add('manual.pdf', b'%PDF', kind='document', fmt='pdf')
        other_robot = self.cloud.add('theirs.deb', b'theirs', device_id='robot-other', device_name='另一台底盘')
        args = {'purpose': '送驱动包', 'issue_ids': [VELOCITY]}
        for material_id, said in ((parsing, '可用'), (document, '安装包'), (other_robot, '另一台底盘'), ('mat_nothex', ''), (None, '')):
            with self.subTest(material_id=material_id), self.assertRaisesRegex(ValueError, said):
                self.hw.driver.package_request(s, {**args, 'material_id': material_id}, self.connection())
        for wrong in ({'issue_ids': ['net-down:wlan9']}, {'issue_ids': []}, {'purpose': ''}):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                self.hw.driver.package_request(s, {**args, 'material_id': self.material, **wrong}, self.connection())

    def test_the_delivery_needs_approval_and_hands_on_the_place_and_checksum(self):
        s = self.session(self.baseline())
        request = self.hw.driver.package_request(s, {'material_id': self.material, 'purpose': '送驱动包', 'issue_ids': [VELOCITY, ODOMETRY]},
                                                 self.connection())
        self.assertEqual((request['filename'], request['size'], request['sha256']), (NAME, len(PACKAGE), sha(PACKAGE)))
        self.assertTrue(request['device_target'].endswith('/' + NAME))
        with self.assertRaisesRegex(ValueError, '批准'):
            self.hw.tool(s['id'], s, 'driver_package_deliver', request, self.ssh())

        result = self.hw.tool(s['id'], s, 'driver_package_deliver', request, self.ssh(), approved=True)

        self.assertEqual((result['device_path'], result['sha256'], result['bytes']), (self.path, sha(PACKAGE), len(PACKAGE)))
        self.assertEqual(self.device.read(self.path), PACKAGE)
        self.assertEqual(self.store.get('device_evidence', result['evidence_id'])['tool'], 'driver_package_deliver')
        self.assertEqual(self.store.get('agent_sessions', s['id'])['hardware']['fixes'], 1, 'a change to the device: verified by a later rescan')
        self.assertEqual(self.hw.tool(s['id'], s, 'driver_packages', {}, None)['packages'][0]['delivered']['device_path'], self.path)
        self.assertFalse(self.device.installed, 'sending is not installing')

    def test_a_connection_that_now_leads_elsewhere_gets_no_package(self):
        s = self.session(self.baseline())
        request = self.hw.driver.package_request(s, {'material_id': self.material, 'purpose': '送驱动包', 'issue_ids': [VELOCITY]}, self.connection())
        self.store.put('device_connections', {**self.connection(), 'fingerprint': 'SHA256:' + 'A' * 43})
        with self.assertRaisesRegex(ValueError, '另一台机器'):
            self.hw.tool(s['id'], s, 'driver_package_deliver', request, self.ssh(), approved=True)
        self.assertIsNone(self.device.read(self.path), 'nothing was sent')

    def test_a_robot_moved_to_another_connection_gets_no_package(self):
        # Security review round 1: the delivery follows the robot's connection, the check followed the session's.
        s = self.session(self.baseline())
        request = self.hw.driver.package_request(s, {'material_id': self.material, 'purpose': '送驱动包', 'issue_ids': [VELOCITY]}, self.connection())
        self.store.put('device_connections', {**self.connection(), 'id': 'connection-other'})
        self.store.put('robots', {**self.store.get('robots', self.robot_id), 'connection_id': 'connection-other'})
        sent = len(self.device.commands) + len(self.device.scripts)
        with self.assertRaisesRegex(ValueError, '另一台机器'):
            self.hw.tool(s['id'], s, 'driver_package_deliver', request, self.ssh(), approved=True)
        self.assertIsNone(self.device.read(self.path), 'nothing was sent')
        self.assertEqual(len(self.device.commands) + len(self.device.scripts), sent)
        self.assertEqual(self.store.list('package_deliveries'), [])

    def test_with_auto_approve_on_the_delivery_runs_unasked(self):
        s = self.session(self.baseline(), auto_approve=True)
        request = self.hw.driver.package_request(s, {'material_id': self.material, 'purpose': '送驱动包', 'issue_ids': [VELOCITY]}, self.connection())
        self.ask(s, driver_repair.DELIVER_METHOD, request, 3)
        self.assertEqual(self.answers.get_nowait(), (3, {'decision': 'accept'}))


class AcceptanceTests(InstallCase):
    """人批准后装上驱动，重新读取档案后运动接口的话题出现，全程机器人没有动。"""

    def test_after_a_person_approves_the_driver_is_installed_its_topics_appear_and_nothing_moved(self):
        baseline = self.baseline()
        self.assertEqual({i['id'] for i in baseline['issues']} & {VELOCITY, ODOMETRY}, {VELOCITY, ODOMETRY})
        self.assertNotIn('/cmd_vel', self.profile_topics())
        s = self.session(baseline)
        device = self.ssh()

        packages = self.hw.tool(s['id'], s, 'driver_packages', {}, device)
        material = packages['packages'][0]['material_id']
        send = self.hw.driver.package_request(s, {'material_id': material, 'purpose': '把厂家驱动包送到设备', 'issue_ids': [VELOCITY, ODOMETRY]},
                                              self.connection())
        self.ask(s, driver_repair.DELIVER_METHOD, send, 1)
        self.assertTrue(self.answers.empty(), 'asks for each step')
        self.approve(s, 1)
        sent = self.hw.tool(s['id'], s, 'driver_package_deliver', send, device, approved=True)

        install = self.fix(s, "DEBIAN_FRONTEND=noninteractive apt-get install -y '%s'" % sent['device_path'], build_step=True, timeout_seconds=1800)
        self.ask(s, driver_repair.FIX_METHOD, install, 2)
        self.approve(s, 2)
        self.assertEqual(self.hw.tool(s['id'], s, 'device_fix', install, device, approved=True)['exit_code'], 0)
        self.assertTrue(self.device.installed)

        start = self.fix(s, 'systemctl start ' + UNIT, starts_motion_driver=True)
        self.ask(s, driver_repair.FIX_METHOD, start, 3)
        with self.assertRaisesRegex(ValueError, '现场'):
            self.approve(s, 3)
        self.assertFalse(self.device.running, 'not started before someone said they are on site')
        self.approve(s, 3, onsite=True)
        self.assertEqual(self.hw.tool(s['id'], s, 'device_fix', start, device, approved=True)['exit_code'], 0)

        rescan = self.hw.tool(s['id'], s, 'hardware_rescan', {}, None)
        self.assertEqual(set(rescan['resolved_since_baseline']) & {VELOCITY, ODOMETRY}, {VELOCITY, ODOMETRY})
        self.assertEqual((rescan['motion_drivers']['reverified'], rescan['motion_drivers']['status']), (True, 'ok'))
        self.assertLessEqual({'/cmd_vel', '/odom'}, self.profile_topics(), 'the profile now lists the motion topics')
        results = [{'issue_id': issue_id, 'outcome': 'fixed', 'actions': '从资料库取驱动包装上并启动了驱动服务', 'verification': '重新扫描时重新读取了 ROS 2',
                    'evidence_ids': [sent['evidence_id']]} for issue_id in (VELOCITY, ODOMETRY)]
        saved = self.hw.tool(s['id'], s, 'save_repair_report', {'summary': '运控驱动已安装并启动', 'results': results, 'followups': []}, None)
        report = self.store.get('hardware_repairs', saved['repair_id'])
        self.assertEqual({r['issue_id']: r['outcome'] for r in report['results']}, {VELOCITY: 'fixed', ODOMETRY: 'fixed'})
        self.assert_nothing_moved()
        installs = [c for c in self.device.commands if 'apt-get install' in c]
        self.assertEqual(len(installs), 1)
        self.assertNotIn('/opt/ros/humble/setup.sh', installs[0], 'the environment is only loaded when asked for')
        self.assertEqual(self.device.refused, [], 'every command that reached the robot was one the repair meant')


class DriverInstallModel(ResponsesFixture):
    """Scripted model: look up the library, send the package, install it, start the driver, rescan, report fixed."""
    requests = []
    material, path = None, None

    def select_item(self, data, fallback):
        step = len(type(self).requests) - 1
        ids = [VELOCITY, ODOMETRY]
        fix = {'purpose': '安装并启动底盘的运控驱动', 'issue_ids': ids, 'risk': 'medium', 'needs_root': True,
               'rollback': 'systemctl stop %s；apt-get remove -y ros-humble-vendor-motion-driver' % UNIT}
        results = [{'issue_id': issue_id, 'outcome': 'fixed', 'actions': '从资料库取厂家驱动包，装上并启动了驱动服务',
                    'verification': '最后一次重新扫描重新读取了 ROS 2，接口已经出现', 'evidence_ids': []} for issue_id in ids]
        calls = [('driver_packages', {}),
                 ('driver_package_deliver', {'material_id': type(self).material, 'purpose': '把厂家驱动包送到设备', 'issue_ids': ids}),
                 ('device_fix', {**fix, 'command': "DEBIAN_FRONTEND=noninteractive apt-get install -y '%s'" % type(self).path,
                                 'build_step': True, 'timeout_seconds': 1800, 'starts_motion_driver': False}),
                 ('device_fix', {**fix, 'command': 'systemctl start ' + UNIT, 'timeout_seconds': 120, 'starts_motion_driver': True}),
                 ('hardware_rescan', {}),
                 ('save_repair_report', {'summary': '运控驱动已安装并启动，运动接口已出现', 'results': results, 'followups': []})]
        if step >= len(calls):
            return {'id': 'msg_done', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': '运控驱动已装上并启动，已保存报告。', 'annotations': []}]}
        name, args = calls[step]
        return {'id': f'fc_{step}', 'type': 'function_call', 'call_id': f'call_{step}', 'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}


class RepairSessionTests(InstallCase):
    """The same through the bundled engine runtime, a scripted model and 默认允许执行: only the start waits for a person."""

    @unittest.skipUnless(agent_runtime.binary_path(), 'Bundled runtime required')
    def test_the_assistant_installs_and_starts_the_driver_and_only_the_start_waits_for_a_person_on_site(self):
        baseline = self.baseline()
        DriverInstallModel.requests, DriverInstallModel.material, DriverInstallModel.path = [], self.material, self.path
        server = Server(DriverInstallModel)
        self.addCleanup(server.close)
        model = self.e.model_add({'project_id': self.pid, 'name': 'Fixture', 'model': 'fixture-agent', 'base_url': server.url + '/v1',
                                  'api_key': secrets.token_hex(12)})
        s = self.hw.start_repair({'project_id': self.pid, 'robot_id': self.robot_id, 'scan_id': baseline['id'], 'issue_ids': [VELOCITY, ODOMETRY],
                                  'model_id': model['id'], 'data_authorized': True, 'modify_authorized': True, 'auto_approve': True,
                                  'max_tokens': 40000, 'max_minutes': 3})
        confirmed, deadline = [], time.monotonic() + 120
        while time.monotonic() < deadline:
            current = self.store.get('agent_sessions', s['id'])
            if current['status'] not in ('starting', 'running', 'waiting_input', 'cancelling'):
                break
            pending = current.get('pending_approval')
            # Other requests are queued and answered at once by 默认允许执行; only the start stays.
            if pending and driver_repair.is_driver_start(pending) and not confirmed:
                self.assertFalse(self.device.running)
                self.assertTrue(self.device.installed, 'the package was delivered and installed unasked first')
                self.hw.driver.confirm_onsite({'project_id': self.pid, 'session_id': s['id'], 'approval_id': pending['id'], 'onsite': True})
                self.e.approve({'session_id': s['id'], 'approval_id': pending['id'], 'decision': 'accept'})
                confirmed.append(pending['id'])
            time.sleep(.05)
        current = self.store.get('agent_sessions', s['id'])
        self.assertEqual(current['status'], 'completed', current.get('error') or json.dumps(current['events'][-8:], ensure_ascii=False))
        self.assertEqual(len(confirmed), 1)
        unasked = [e['message'] for e in current['events'] if e['message'].startswith('默认允许执行，未询问')]
        self.assertEqual(len(unasked), 2, 'the delivery and the install ran unasked, the start did not')
        self.assertIn('apt-get install', unasked[1])
        report = self.store.get('hardware_repairs', current['repair_report_id'])
        self.assertEqual({r['issue_id']: r['outcome'] for r in report['results']}, {VELOCITY: 'fixed', ODOMETRY: 'fixed'})
        final = self.store.get('hardware_scans', report['final_scan_id'])
        self.assertEqual((final['motion_drivers']['reverified'], final['motion_drivers']['status']), (True, 'ok'))
        self.assertLessEqual({'/cmd_vel', '/odom'}, self.profile_topics())
        self.assertTrue(self.device.running)
        self.assertEqual(len(current['hardware']['onsite_confirmations']), 1)
        errors = [e['message'] for e in current['events'] if e['kind'] == 'tool_error']
        self.assertEqual(errors, [])
        self.assert_nothing_moved()
        self.assertEqual(self.device.refused, [])


if __name__ == '__main__':
    unittest.main()
