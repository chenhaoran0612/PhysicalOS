"""Issue #12: the SSH connection of a robot can be saved whatever the robot is doing.

Until then an investigation, a repair or a hardware scan that was running refused the change
(issue #9). Now the change is saved, and a session that was already running ends on the machine
it started on. Every machine here is a local SSH protocol fixture: nothing reaches a real one.
"""
import json
import secrets
import threading
from unittest import mock

import test_ssh_edit as edit
from brainos import device_access, hardware_init, hardware_scan
from hardware_fixture import jetson

EditCase, ROBOT_LOGIN = edit.EditCase, edit.ROBOT_LOGIN


class AnyStateTests(EditCase):
    """Issue #12: the connection can be saved whatever the robot is doing."""
    def test_a_running_investigation_does_not_stop_the_change(self):
        old = self.connect_directly()
        self.session(old)

        saved = self.save(old, watch_services=['bridge'])
        _, check = self.rescan(saved)                                    # reading the fingerprints again is not refused either

        self.assertEqual(self.saved(old)['watch_services'], ['bridge'])
        self.assertEqual(check['fingerprint'], old['fingerprint'])

    def test_no_state_of_a_session_stops_the_change(self):
        old = self.connect_directly()
        states = [(status, mode) for status in ('starting', 'running', 'waiting_input', 'cancelling')
                  for mode in ('discovery', 'repair')]

        for number, (status, mode) in enumerate(states):
            with self.subTest(status=status, mode=mode):
                running = self.session(old, status=status, mode=mode)
                services = [f'bridge-{number}']

                self.assertEqual(self.save(old, watch_services=services)['watch_services'], services)

                self.store.delete('agent_sessions', running['id'])

    def test_a_hardware_scan_in_progress_does_not_stop_the_change(self):
        old = self.connect_directly()
        self.platform.hardware.scanning.add(old['robot_id'])

        self.assertEqual(self.save(old, watch_services=['bridge'])['watch_services'], ['bridge'])

    def test_a_session_that_starts_while_the_new_login_is_tested_does_not_stop_the_change(self):
        old = self.connect_directly()
        real = device_access.SSHDevice

        def test_login(connection, secret):
            self.session(old)                                            # an investigation starts during the login test
            return real(connection, secret)

        with mock.patch.object(device_access, 'SSHDevice', test_login):
            saved = self.save(old, watch_services=['bridge'])

        self.assertEqual(saved['watch_services'], ['bridge'])

    def test_a_busy_robot_can_be_pointed_at_another_machine(self):
        old = self.connect_directly()
        self.session(old, mode='repair')
        self.platform.hardware.scanning.add(old['robot_id'])
        other = self.machine('dog-b')

        new = self.save_confirmed(old, port=other.port, password=ROBOT_LOGIN)

        self.assertEqual((new['port'], new['fingerprint']), (other.port, device_access.fingerprint(other.key)))

    def test_a_refused_login_still_changes_nothing_on_a_busy_robot(self):
        old = self.connect_directly()
        self.session(old)

        self.assert_refused('device', 'auth', '认证失败', lambda: self.save(old, password=secrets.token_hex(8)))

        self.assertEqual(self.saved(old), old)


class RunningSessionTests(EditCase):
    """Issue #12: what a repair or an investigation that is already running does once the connection changed.

    It ends on the machine it started on. A later login of it uses the newly saved settings while they
    still lead to that machine; it never follows the robot to another one."""
    COMMAND = 'lsmod'

    def setUp(self):
        super().setUp()
        self.hw = self.platform.hardware
        self.target.command_outputs[hardware_scan.SCAN_EXEC] = jetson().encode()
        self.old = self.connect_directly()
        scan = self.hw.scan({'project_id': self.pid, 'robot_id': self.old['robot_id']})
        self.issue = scan['issues'][0]['id']
        self.repair = self.store.put('agent_sessions', {
            **self.session(self.old, mode='repair'),
            'hardware': {'scan_id': scan['id'], 'issue_ids': [self.issue], 'skill': hardware_init.skill_info(), 'fixes': 0,
                         'fixed_issue_ids': [], 'rescan_id': None, 'rescan_after_fixes': None, 'ssh_interface': 'eth0'}})
        self.device = device_access.SSHDevice(self.old, self.secret(self.old))     # the SSH session the repair opened
        self.addCleanup(self.device.close)

    def tool(self, name, args=None):
        return self.hw.tool(self.repair['id'], self.repair, name, args or {}, self.device, approved=True)

    def exec_request(self, command=COMMAND):
        return hardware_init.exec_request(self.repair, {'command': command, 'purpose': '查看已加载模块', 'timeout_seconds': 10})

    def fix_request(self):
        args = {'command': 'usermod -aG dialout robot', 'purpose': '让 SSH 用户可以读写串口', 'issue_ids': [self.issue],
                'risk': 'low', 'rollback': 'gpasswd -d robot dialout', 'needs_root': True, 'timeout_seconds': 10}
        return self.hw.fix_request(self.repair, args, self.old)

    def move_to_another_machine(self):
        other = self.machine('dog-b')
        other.command_outputs[hardware_scan.SCAN_EXEC] = jetson().encode()
        self.save_confirmed(self.old, port=other.port, password=ROBOT_LOGIN)
        return other

    def test_the_ssh_session_it_opened_keeps_working(self):
        link = self.device.client.get_transport()

        self.save(self.old, watch_services=['bridge'])
        result = self.tool('device_exec', self.exec_request())

        self.assertEqual(result['exit_code'], 0)
        self.assertIs(self.device.client.get_transport(), link)             # the same login, not a new one
        self.assertTrue(link.is_active())

    def test_a_later_login_on_the_same_machine_uses_the_newly_saved_one(self):
        self.target.password = secrets.token_hex(8)                      # the robot's password was changed, then saved here
        self.save(self.old, password=self.target.password)
        self.device.client.close()                                       # the repair's SSH link drops

        result = self.tool('device_exec', self.exec_request())

        self.assertEqual(result['exit_code'], 0)

    def test_a_newly_saved_login_is_hidden_from_what_the_session_reads(self):
        self.target.password = secrets.token_hex(8)
        self.target.command_outputs[self.COMMAND] = f'login {self.target.password} seen\n'.encode()
        self.save(self.old, password=self.target.password)

        result = self.tool('device_exec', self.exec_request())

        self.assertNotIn(self.target.password, json.dumps(result))
        self.assertIn('seen', result['stdout'])

    def test_it_runs_nothing_more_once_the_robot_points_at_another_machine(self):
        requests = {'device_exec': self.exec_request(), 'device_fix': self.fix_request(), 'hardware_rescan': {}}
        before = list(self.target.commands)
        other = self.move_to_another_machine()

        for name, request in requests.items():
            with self.subTest(tool=name):
                with self.assertRaises(ValueError) as caught:
                    self.tool(name, request)
                self.assertIn('另一台机器', str(caught.exception))
        self.assertEqual(self.target.commands, before)                   # nothing more on the machine it started on,
        self.assertEqual(other.commands, [])                             # and nothing at all on the new one

    def test_the_first_scan_of_an_investigation_does_not_follow_the_robot_either(self):
        run = self.session(self.old)
        run = self.store.put('agent_sessions', {**run, 'status': 'starting', 'investigation': {'full_flow': True}})
        self.store.delete('agent_sessions', self.repair['id'])
        other = self.move_to_another_machine()

        self.platform.investigation.flow_scan(run['id'], {'stop': threading.Event(), 'device': self.device})

        self.assertIn('另一台机器', self.store.get('agent_sessions', run['id'])['investigation']['scan_error'])
        self.assertEqual(other.commands, [])

    def test_an_approved_fix_gets_the_newly_saved_password_for_sudo(self):
        request = self.fix_request()
        self.target.password = secrets.token_hex(8)
        self.save(self.old, password=self.target.password)
        ran = {'exit_code': 0, 'timed_out': False, 'stdout': '', 'stderr': '', 'output_head_dropped': False, 'cancelled': False}

        with mock.patch.object(hardware_init, 'run_command', return_value=ran) as run:
            self.tool('device_fix', request)

        self.assertEqual(request['sudo'], 'sudo_password')
        self.assertEqual(run.call_args.args[3], self.target.password + '\n')     # what sudo reads on stdin

    def test_a_newly_saved_login_is_hidden_from_a_running_investigation_too(self):
        run = self.session(self.old)
        self.engine.live[run['id']] = {'stop': threading.Event(), 'device': self.device}     # as Engineering._run registers it
        self.addCleanup(self.engine.live.pop, run['id'], None)
        self.target.password = secrets.token_hex(8)
        self.target.command_outputs[self.COMMAND] = f'login {self.target.password} seen\n'.encode()

        self.save(self.old, password=self.target.password)
        request = {'command': self.COMMAND, 'purpose': '查看已加载模块', 'timeout_seconds': 10}
        result = self.engine._tool(run['id'], 'device_exec', request, self.device, approved=True)

        self.assertIn('seen', result['stdout'])
        self.assertNotIn(self.target.password, json.dumps(result))
        self.assertNotIn(self.target.password, json.dumps(self.store.list('device_evidence')))

    def test_sessions_over_other_connections_are_left_alone(self):
        elsewhere = self.session({'robot_id': 'robot-other', 'id': 'connection-other'})
        self.engine.live[elsewhere['id']] = {'stop': threading.Event(), 'device': self.device}
        self.addCleanup(self.engine.live.pop, elsewhere['id'], None)
        before = list(self.device.secrets)
        self.target.password = secrets.token_hex(8)

        self.save(self.old, password=self.target.password)

        self.assertEqual(self.device.secrets, before)

    def test_the_record_and_its_logins_are_read_as_one(self):
        connection, secret = self.platform.ssh_edit.saved_login(self.old['id'])

        self.assertEqual((connection, secret), (self.saved(self.old), self.secret(self.old)))
        self.assertEqual(self.platform.ssh_edit.session_login(None, self.old['id']), (connection, secret))

    def test_an_ssh_session_knows_the_machine_it_was_opened_for(self):
        self.assertEqual(self.device.fingerprint, device_access.fingerprint(self.target.key))
