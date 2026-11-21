"""Issue #16 (R1-02): 「验证连接」 of a robot that is already added.

One verification logs in to every hop of the saved SSH connection once, reads the system version and
the processor architecture with one fixed command, and closes the session again. It says how long
that took, or at which hop it got stuck.

Direct, through one jump host and through three: every machine here is a local SSH protocol fixture
with its own host key and login. Nothing in this file reaches a real machine.
"""
import json
import re
import secrets
import shlex
import time
import unittest
from unittest import mock

import paramiko

import test_ssh_jump as base
import test_ssh_multi_hop as chain
from brainos import connection_verify, device_access, hardware_scan
from ssh_fixture import DEFAULT_LOGIN

VERIFY = connection_verify.VERIFY_COMMAND
SYSTEM = b'##os\nUbuntu 22.04.4 LTS\n##kernel\nLinux 5.15.148-tegra\n##arch\naarch64\n##end\n'
READ = {'os': 'Ubuntu 22.04.4 LTS', 'kernel': 'Linux 5.15.148-tegra', 'arch': 'aarch64'}


class VerifyHelpers:
    """What the cases below share. ``self.target`` is the robot (tests/test_ssh_jump.py)."""

    def answer(self, output=SYSTEM):
        self.target.command_outputs[VERIFY] = output

    def verify(self, connection, **extra):
        return self.platform.connection_verify.verify(
            {'project_id': self.pid, 'robot_id': connection['robot_id'], **extra})

    def states(self, result):
        return [hop['state'] for hop in result['route']]

    def login_count(self, fixture):
        return len(fixture.auth_attempts)

    def all_closed(self, *fixtures):
        """True once every session these fixtures served has ended."""
        deadline = time.monotonic() + 3
        while any(transport.is_active() for fixture in fixtures for transport in fixture.transports):
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    def assert_stuck(self, result, hop, level, reason, states):
        self.assertIs(result['ok'], False)
        self.assertIsNone(result['system'])
        error = result['error']
        self.assertEqual((error['hop'], error['level'], error['reason'], error['stage']), (hop, level, reason, 'login'), error)
        self.assertEqual(self.states(result), states)
        self.assertIsInstance(result['duration_ms'], int)             # how long it took until it got stuck


class DirectTests(VerifyHelpers, base.SshCase):
    def setUp(self):
        super().setUp()
        self.connection = self.connect_directly()
        self.answer()

    def test_success_shows_the_system_the_architecture_and_how_long_it_took(self):
        before = time.time()

        result = self.verify(self.connection)

        self.assertIs(result['ok'], True)
        self.assertEqual(result['system'], READ)
        self.assertIsInstance(result['duration_ms'], int)
        self.assertGreater(result['duration_ms'], 0)
        self.assertLess(result['duration_ms'], 30_000)
        self.assertTrue(before <= result['checked_at'] <= time.time())
        self.assertIsNone(result['error'])
        self.assertEqual(result['robot_id'], self.connection['robot_id'])
        self.assertEqual(result['route'], [{'kind': 'device', 'level': None, 'label': '目标机', 'state': 'ok',
                                            'endpoint': f'robot@127.0.0.1:{self.target.port}'}])

    def test_one_verification_is_one_login_and_one_read_only_command(self):
        logins = self.login_count(self.target)

        self.verify(self.connection)

        self.assertEqual(self.login_count(self.target), logins + 1)
        self.assertEqual(self.target.commands, [VERIFY])              # saving the connection ran none
        self.assertEqual(self.target.reads, [])                       # nothing is fetched over SFTP
        self.assertTrue(self.all_closed(self.target))                 # and no session is left behind

    def test_the_command_is_four_echoes_one_sed_that_prints_and_two_unames(self):
        # An allow-list, not a list of what is forbidden: anything else in the command fails here.
        for character in ('>', '<', '|', '&', '`', '$', '\\', '\n'):
            self.assertNotIn(character, VERIFY, character)
        steps = [shlex.split(step) for step in VERIFY.split(';')]
        self.assertEqual([step[0] for step in steps], ['echo', 'sed', 'echo', 'uname', 'echo', 'uname', 'echo'])
        self.assertEqual(steps[1], ['sed', '-n', 's/^PRETTY_NAME=//p', '/etc/os-release'])    # prints; never -i, never w
        self.assertEqual([step for step in steps if step[0] == 'uname'], [['uname', '-sr'], ['uname', '-m']])
        self.assertTrue(all(re.fullmatch(r'##[a-z]+', step[1]) for step in steps if step[0] == 'echo'))

    def test_it_does_not_scan_the_hardware_or_keep_anything_about_the_device(self):
        kinds = ('hardware_scans', 'hardware_scan_raw', 'device_evidence', 'robot_inventories', 'device_profiles')
        before = {kind: self.store.list(kind) for kind in kinds}
        robot = self.store.get('robots', self.connection['robot_id'])
        saved = self.store.get('device_connections', self.connection['id'])

        self.verify(self.connection)

        self.assertEqual({kind: self.store.list(kind) for kind in kinds}, before)
        self.assertNotIn(hardware_scan.SCAN_EXEC, self.target.commands)
        self.assertEqual(self.store.get('robots', self.connection['robot_id']), robot)
        self.assertEqual(self.store.get('device_connections', self.connection['id']), saved)

    def test_it_can_be_done_again_and_again(self):
        logins = self.login_count(self.target)

        results = [self.verify(self.connection) for _ in range(3)]

        self.assertEqual([result['ok'] for result in results], [True, True, True])
        self.assertEqual(self.login_count(self.target), logins + 3)        # every time a login of its own
        self.assertEqual(self.target.commands, [VERIFY] * 3)

    def test_a_device_that_is_off_is_where_it_gets_stuck(self):
        self.target.close()

        result = self.verify(self.connection)

        self.assert_stuck(result, 'device', None, 'unreachable', ['failed'])
        self.assertEqual(result['error']['where'], f'目标机 robot@127.0.0.1:{self.target.port}')
        self.assertIn('设备不可达', result['error']['message'])

    def test_a_device_that_refuses_the_saved_login_is_where_it_gets_stuck(self):
        self.target.password = secrets.token_hex(6)                   # changed on the device since it was saved

        result = self.verify(self.connection)

        self.assert_stuck(result, 'device', None, 'auth', ['failed'])
        self.assertIn('设备拒绝登录', result['error']['message'])
        self.assertEqual(self.target.commands, [])

    def test_a_device_with_another_host_key_gets_no_login(self):
        record = self.store.get('device_connections', self.connection['id'])
        self.store.put('device_connections', {**record, 'fingerprint': 'SHA256:' + 'A' * 43})
        logins = self.login_count(self.target)

        result = self.verify(self.connection)

        self.assert_stuck(result, 'device', None, 'fingerprint', ['failed'])
        self.assertEqual(self.login_count(self.target), logins)

    def test_the_saved_logins_never_come_back(self):
        result = self.verify(self.connection)

        self.assertNotIn(DEFAULT_LOGIN[1], json.dumps(result))
        self.assertNotIn(DEFAULT_LOGIN[1], json.dumps(self.store.list('events')))

    def test_each_verification_is_noted_with_its_outcome(self):
        self.verify(self.connection)
        self.target.close()
        self.verify(self.connection)

        noted = [event['details'] for event in self.store.list('events') if event['action'] == 'device.ssh_verified']
        passed = next(details for details in noted if details['ok'])
        failed = next(details for details in noted if not details['ok'])

        self.assertEqual(len(noted), 2)
        self.assertEqual((passed['robot_id'], passed['connection_id']), (self.connection['robot_id'], self.connection['id']))
        self.assertEqual((failed['hop'], failed['level'], failed['reason']), ('device', None, 'unreachable'))


class ReadTests(VerifyHelpers, base.SshCase):
    """Logged in everywhere, but the system and the architecture did not come back."""

    def setUp(self):
        super().setUp()
        self.connection = self.connect_directly()

    def assert_not_read(self, result, reason):
        self.assertIs(result['ok'], False)
        self.assertIsNone(result['system'])
        self.assertEqual(self.states(result), ['ok'])                 # the login itself went through
        error = result['error']
        self.assertEqual((error['hop'], error['stage'], error['reason']), ('device', 'read', reason), error)
        self.assertIn('已登录', error['message'])

    def test_a_device_that_never_answers_the_command_is_reported_within_the_limit(self):
        self.target.unanswered.add(VERIFY)

        with mock.patch.object(connection_verify, 'READ_TIMEOUT_SECONDS', 0.4):
            started = time.monotonic()
            result = self.verify(self.connection)

        self.assertLess(time.monotonic() - started, 8)
        self.assert_not_read(result, 'timeout')
        self.assertTrue(self.all_closed(self.target))

    def test_an_answer_that_is_not_the_commands_own_is_not_taken_for_a_system(self):
        result = self.verify(self.connection)                         # the fixture's default text: no sections

        self.assert_not_read(result, 'not_read')

    def test_an_answer_that_was_cut_short_is_not_shown_as_complete(self):
        self.answer(b'##os\nUbuntu 22.04.4 LTS\n##kernel\nLinux 5.15\n##arch\naarch64\n')      # no end mark

        self.assert_not_read(self.verify(self.connection), 'not_read')

    def test_an_architecture_that_is_not_one_is_not_shown(self):
        self.answer(b'##os\nUbuntu\n##kernel\nLinux 5.15\n##arch\n<script>alert(1)</script>\n##end\n')

        self.assert_not_read(self.verify(self.connection), 'not_read')


class ParseTests(unittest.TestCase):
    """``parse_system``: what the device printed is data from outside, read strictly."""

    def test_the_three_fields(self):
        self.assertEqual(connection_verify.parse_system(SYSTEM.decode()), READ)

    def test_without_an_os_release_file_the_kernel_stands_in_for_the_system(self):
        parsed = connection_verify.parse_system('##os\n##kernel\nLinux 4.9.201\n##arch\narmv7l\n##end\n')

        self.assertEqual(parsed, {'os': 'Linux 4.9.201', 'kernel': 'Linux 4.9.201', 'arch': 'armv7l'})

    def test_what_a_login_script_prints_before_the_command_is_ignored(self):
        text = 'Welcome to the robot\n##arch\nfake\n' + SYSTEM.decode()

        self.assertEqual(connection_verify.parse_system(text), READ)

    def test_only_the_first_line_of_a_field_counts_and_it_is_kept_short(self):
        text = '##os\n' + 'U' * 500 + '\nsecond line\n##kernel\nLinux 6.1\n##arch\nx86_64\n##end\n'

        parsed = connection_verify.parse_system(text)

        self.assertEqual(parsed['os'], 'U' * connection_verify.FIELD_LIMIT)
        self.assertEqual(parsed['arch'], 'x86_64')

    def test_control_characters_do_not_reach_the_page(self):
        parsed = connection_verify.parse_system('##os\nUbuntu\x1b[31m 22.04\x07\n##kernel\nLinux 6.1\n##arch\nx86_64\n##end\n')

        self.assertEqual(parsed['os'], 'Ubuntu[31m 22.04')

    def test_format_characters_that_turn_or_hide_text_are_dropped(self):
        parsed = connection_verify.parse_system('##os\n\ufeffUbuntu \u202e40.22\u200b\n##kernel\nLinux 6.1\n##arch\nx86_64\n##end\n')

        self.assertEqual(parsed['os'], 'Ubuntu 40.22')

    def test_the_quotes_of_os_release_are_taken_off_either_way(self):
        for value in ('"Ubuntu 22.04.4 LTS"', "'Ubuntu 22.04.4 LTS'", 'Ubuntu 22.04.4 LTS'):
            with self.subTest(value=value):
                text = f'##os\n{value}\n##kernel\nLinux 6.1\n##arch\nx86_64\n##end\n'
                self.assertEqual(connection_verify.parse_system(text)['os'], 'Ubuntu 22.04.4 LTS')

    def test_nothing_printed_after_the_end_mark_counts(self):
        text = SYSTEM.decode() + '##arch\nx86_64\n##os\nSomething else\n'

        self.assertIsNone(connection_verify.parse_system(text))      # the last block has no end mark of its own
        self.assertEqual(connection_verify.parse_system(SYSTEM.decode() + '##arch\nx86_64\n'), READ)

    def test_a_complete_block_before_a_cut_short_answer_is_not_mixed_into_it(self):
        text = SYSTEM.decode() + '##os\nDebian 12\n##kernel\nLinux 6.1\n'

        self.assertIsNone(connection_verify.parse_system(text))

    def test_incomplete_or_implausible_answers_are_none(self):
        for text in ('', 'Linux test-fixture\n', '##os\nUbuntu\n##kernel\nLinux\n##arch\naarch64\n',
                     '##os\nUbuntu\n##kernel\nLinux\n##arch\n\n##end\n', '##os\n\n##kernel\n\n##arch\naarch64\n##end\n',
                     '##os\nUbuntu\n##kernel\nLinux\n##arch\na b\n##end\n'):
            with self.subTest(text=text):
                self.assertIsNone(connection_verify.parse_system(text))


class OneJumpTests(VerifyHelpers, base.SshCase):
    def setUp(self):
        super().setUp()
        self.connection = self.connect_through_jump()
        self.answer()

    def test_success_through_one_jump_host(self):
        jump_logins, device_logins, forwarded = self.login_count(self.jump), self.login_count(self.target), len(self.jump.forwarded)

        result = self.verify(self.connection)

        self.assertIs(result['ok'], True)
        self.assertEqual(result['system'], READ)
        self.assertGreater(result['duration_ms'], 0)
        self.assertEqual(result['route'], [
            {'kind': 'jump', 'level': 1, 'label': '跳板机', 'state': 'ok',
             'endpoint': f'{base.JUMP_USER}@127.0.0.1:{self.jump.port}'},
            {'kind': 'device', 'level': None, 'label': '目标机', 'state': 'ok',
             'endpoint': f'robot@127.0.0.1:{self.target.port}'}])
        self.assertEqual((self.login_count(self.jump), self.login_count(self.target)), (jump_logins + 1, device_logins + 1))
        self.assertEqual(len(self.jump.forwarded), forwarded + 1)
        self.assertEqual(self.jump.commands, [])                      # nothing is run on the jump host
        self.assertTrue(self.all_closed(self.jump, self.target))

    def test_a_jump_host_that_refuses_is_named_without_a_number(self):
        self.jump.password = secrets.token_hex(6)
        device_sessions = len(self.target.transports)

        result = self.verify(self.connection)

        self.assert_stuck(result, 'jump', 1, 'auth', ['failed', 'not_reached'])
        self.assertEqual(result['error']['where'], f'跳板机 {base.JUMP_USER}@127.0.0.1:{self.jump.port}')
        self.assertIn('跳板机拒绝登录', result['error']['message'])
        self.assertNotIn('第 1 级', result['error']['message'])
        self.assertEqual(len(self.target.transports), device_sessions)        # the device was not contacted

    def test_a_device_the_jump_host_cannot_reach_is_where_it_gets_stuck(self):
        self.target.close()

        result = self.verify(self.connection)

        self.assert_stuck(result, 'device', None, 'unreachable', ['ok', 'failed'])
        self.assertIn('跳板机连不上目标机', result['error']['message'])
        self.assertTrue(self.all_closed(self.jump))                   # the hop that was logged in to is let go of


class ThreeJumpTests(VerifyHelpers, chain.ChainCase):
    def setUp(self):
        super().setUp()
        self.connection = self.connect_through_chain()
        self.answer()

    def hop(self, level):
        gate, (user, _) = self.gates[level - 1], self.logins[level - 1]      # ChainCase: [(user, password)] per level
        return f'第 {level} 级跳板机 {user}@127.0.0.1:{gate.port}'

    def attempts(self):
        return [len(gate.auth_attempts) for gate in self.gates] + [len(self.target.auth_attempts)]

    def sessions(self):
        return [len(gate.transports) for gate in self.gates] + [len(self.target.transports)]

    def test_success_logs_in_to_every_hop_once_in_order(self):
        before = self.attempts()

        result = self.verify(self.connection)

        self.assertIs(result['ok'], True)
        self.assertEqual(result['system'], READ)
        self.assertGreater(result['duration_ms'], 0)
        self.assertEqual([(hop['kind'], hop['level'], hop['label'], hop['state']) for hop in result['route']],
                         [('jump', 1, '第 1 级跳板机', 'ok'), ('jump', 2, '第 2 级跳板机', 'ok'),
                          ('jump', 3, '第 3 级跳板机', 'ok'), ('device', None, '目标机', 'ok')])
        self.assertEqual([hop['endpoint'] for hop in result['route']],
                         [f'{user}@127.0.0.1:{gate.port}' for gate, (user, _) in zip(self.gates, self.logins)]
                         + [f'robot@127.0.0.1:{self.target.port}'])
        self.assertEqual(self.attempts(), [count + 1 for count in before])
        self.assertEqual([gate.commands for gate in self.gates], [[], [], []])
        self.assertEqual(self.target.commands, [VERIFY])
        self.assertTrue(self.all_closed(*self.gates, self.target))

    def test_the_logins_of_the_jump_hosts_never_come_back(self):
        text = json.dumps(self.verify(self.connection))

        for _, password in self.logins:
            self.assertNotIn(password, text)

    def test_stuck_at_the_first_level_that_cannot_be_reached(self):
        self.gates[0].close()
        before = self.sessions()

        result = self.verify(self.connection)

        self.assert_stuck(result, 'jump', 1, 'unreachable', ['failed', 'not_reached', 'not_reached', 'not_reached'])
        self.assertEqual(result['error']['where'], self.hop(1))
        self.assertIn('第 1 级跳板机', result['error']['message'])
        self.assertEqual(self.sessions()[1:], before[1:])             # nothing beyond it was contacted

    def test_stuck_at_the_second_level_that_refuses_its_login(self):
        self.gates[1].password = secrets.token_hex(6)
        before = self.sessions()

        result = self.verify(self.connection)

        self.assert_stuck(result, 'jump', 2, 'auth', ['ok', 'failed', 'not_reached', 'not_reached'])
        self.assertEqual(result['error']['where'], self.hop(2))
        self.assertIn('第 2 级跳板机拒绝登录', result['error']['message'])
        self.assertEqual(self.sessions()[2:], before[2:])
        self.assertTrue(self.all_closed(self.gates[0]))               # the first level is let go of again

    def test_stuck_at_the_second_level_when_the_first_does_not_forward(self):
        self.gates[0].forwarding = False
        second = len(self.gates[1].transports)

        result = self.verify(self.connection)

        # The hop to fix is the first: it was logged in to, but would not forward to the second, which was never contacted.
        self.assert_stuck(result, 'jump', 2, 'forbidden', ['failed', 'not_reached', 'not_reached', 'not_reached'])
        self.assertEqual(result['error']['where'], self.hop(1))
        self.assertIn('第 1 级跳板机不允许转发连接', result['error']['message'])
        self.assertEqual(len(self.gates[1].transports), second)    # the second level was not contacted

    def test_stuck_at_the_third_level_whose_host_key_changed(self):
        record = self.store.get('device_connections', self.connection['id'])
        record['jumps'][2] = {**record['jumps'][2], 'fingerprint': 'SHA256:' + 'B' * 43}
        self.store.put('device_connections', record)
        before = self.attempts()

        result = self.verify(self.connection)

        self.assert_stuck(result, 'jump', 3, 'fingerprint', ['ok', 'ok', 'failed', 'not_reached'])
        self.assertEqual(result['error']['where'], self.hop(3))
        self.assertEqual(self.attempts()[2:], before[2:])             # no login goes to a machine with another key

    def test_stuck_at_the_device_that_refuses_its_login(self):
        self.target.password = secrets.token_hex(6)

        result = self.verify(self.connection)

        self.assert_stuck(result, 'device', None, 'auth', ['ok', 'ok', 'ok', 'failed'])
        self.assertEqual(result['error']['where'], f'目标机 robot@127.0.0.1:{self.target.port}')
        self.assertIn('设备拒绝登录', result['error']['message'])
        self.assertTrue(self.all_closed(*self.gates))

    def test_stuck_at_the_device_the_last_level_cannot_reach(self):
        self.target.close()

        result = self.verify(self.connection)

        self.assert_stuck(result, 'device', None, 'unreachable', ['ok', 'ok', 'ok', 'failed'])
        self.assertIn('第 3 级跳板机连不上目标机', result['error']['message'])

    def test_a_route_that_has_used_up_its_time_is_given_up_before_the_first_hop(self):
        with mock.patch.object(device_access, 'ROUTE_TIMEOUT', -1):
            result = self.verify(self.connection)

        self.assert_stuck(result, 'jump', 1, 'timeout', ['failed', 'not_reached', 'not_reached', 'not_reached'])

    def test_a_route_that_crawls_is_given_up_at_the_hop_it_reached(self):
        # The route may take ROUTE_TIMEOUT in all. A slow login at one level uses it up: the next hop is
        # not even tried, and that hop is where it got stuck.
        cases = ((1, 'jump', 3, ['ok', 'ok', 'failed', 'not_reached']), (2, 'device', None, ['ok', 'ok', 'ok', 'failed']))
        for slow, hop, level, states in cases:
            with self.subTest(hop=hop, level=level):
                self.gates[slow].auth_delay = 4.5
                try:
                    with mock.patch.object(device_access, 'ROUTE_TIMEOUT', 4):
                        result = self.verify(self.connection)
                finally:
                    self.gates[slow].auth_delay = 0
                self.assert_stuck(result, hop, level, 'timeout', states)
                self.assertIn('超时', result['error']['message'])


class UnexpectedTests(VerifyHelpers, base.SshCase):
    """What the login and the read can do besides the failures ``ConnectError`` names."""

    def setUp(self):
        super().setUp()
        self.connection = self.connect_directly()
        self.answer()
        self.verifier = self.platform.connection_verify

    def test_a_session_that_breaks_while_reading_names_no_hop(self):
        with mock.patch.object(connection_verify, 'read_system', side_effect=EOFError()):
            with self.assertLogs('brainos.connection_verify', level='WARNING'):
                result = self.verify(self.connection)

        self.assertIs(result['ok'], False)
        self.assertEqual((result['error']['hop'], result['error']['stage'], result['error']['reason']),
                         ('connection', 'read', 'session_lost'))
        self.assertEqual(self.states(result), ['ok'])                 # it had logged in
        self.assertIsNone(result['error']['where'])
        self.assertTrue(self.all_closed(self.target))

    def test_a_login_that_fails_outside_the_known_phases_makes_up_no_hop(self):
        def broken(connection, secret):
            raise EOFError()
        self.verifier.connect = broken

        with self.assertLogs('brainos.connection_verify', level='WARNING'):
            result = self.verify(self.connection)

        self.assertEqual((result['error']['hop'], result['error']['stage'], result['error']['reason']),
                         ('connection', 'login', 'login_failed'))
        self.assertEqual(self.states(result), ['unknown'])
        self.assertIsNone(result['error']['where'])

    def test_a_device_that_refuses_the_channel_for_the_command(self):
        opened = []

        def refusing(connection, secret):
            device = device_access.SSHDevice(connection, secret)
            opened.append(device)
            device.execute = mock.Mock(side_effect=paramiko.ChannelException(1, 'Administratively prohibited'))
            return device
        self.verifier.connect = refusing

        result = self.verify(self.connection)

        self.assertEqual((result['error']['stage'], result['error']['reason']), ('read', 'refused'))
        self.assertIn('不允许打开执行命令的会话', result['error']['message'])
        self.assertEqual(len(opened), 1)
        self.assertTrue(self.all_closed(self.target))

    def test_a_channel_that_does_not_open_in_time_is_a_timeout(self):
        def slow(connection, secret):
            device = device_access.SSHDevice(connection, secret)
            device.execute = mock.Mock(side_effect=paramiko.SSHException('Timeout opening channel.'))
            return device
        self.verifier.connect = slow

        result = self.verify(self.connection)

        self.assertEqual((result['error']['stage'], result['error']['reason']), ('read', 'timeout'))

    def test_a_robot_id_that_is_not_text_is_refused_before_anything_is_read(self):
        for robot_id in (['x'], {'id': 'x'}, 7, None, ''):
            with self.subTest(robot_id=robot_id):
                with self.assertRaises(ValueError) as caught:
                    self.verifier.verify({'project_id': self.pid, 'robot_id': robot_id})
                self.assertIn('请指定', str(caught.exception))
        self.assertEqual(self.target.commands, [])


if __name__ == '__main__':
    unittest.main()
