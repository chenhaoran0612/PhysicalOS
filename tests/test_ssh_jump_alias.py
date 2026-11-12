"""SSH access to a robot that only the jump host can name: ``ssh gate 'ssh dog'``.

Both ends are local protocol fixtures: nothing here reaches a real machine.
"""
import time
from unittest import mock

import test_ssh_jump as base
from brainos import device_access


class JumpAliasTests(base.SshCase):
    """`ssh gate 'ssh dog'`: the target is a name that only the jump host can resolve (an alias in its
    ssh config). The app asks the jump host what `ssh dog` would connect to, then goes there through it."""

    ALIAS = 'dog'

    def setUp(self):
        super().setUp()
        self.resolve = device_access.resolve_command(self.ALIAS)
        self.jump = self.fixture(username=base.JUMP_USER, password=base.JUMP_PASSWORD,
                                 forward_to=[('127.0.0.1', self.target.port)],
                                 command_outputs={self.resolve: self.ssh_config()})

    def ssh_config(self, user='robot', extra=''):
        return f'user {user}\nhostname 127.0.0.1\nport {self.target.port}\naddressfamily any\n{extra}'.encode()

    def named(self, **extra):
        return self.request(**{'host': self.ALIAS, 'port': None, 'username': '', 'resolve_on_jump': True, **extra})

    def scan_both(self, **extra):
        first = self.engine.scan(self.named(**extra))
        return self.engine.scan(self.named(**self.jump_login(first), **extra))

    def test_nothing_runs_on_the_jump_host_before_its_fingerprint_is_confirmed(self):
        first = self.engine.scan(self.named())

        self.assertEqual(first['stage'], 'jump')
        self.assertEqual(self.jump.commands, [])
        self.assertEqual(self.jump.auth_attempts, [])

    def test_the_name_is_resolved_by_the_jump_hosts_ssh_config(self):
        second = self.scan_both()

        self.assertEqual((second['host'], second['port'], second['username']), ('127.0.0.1', self.target.port, 'robot'))
        self.assertEqual(second['alias'], self.ALIAS)
        self.assertEqual(second['fingerprint'], device_access.fingerprint(self.target.key))
        self.assertEqual(self.jump.commands, [self.resolve])           # the one fixed, read-only command

    def test_a_robot_named_on_the_jump_host_can_be_connected_and_watched(self):
        second = self.scan_both()
        connection = self.engine.connect({'project_id': self.pid, 'host_check_id': second['id'],
                                          'trust_fingerprint': second['fingerprint'], 'auth': 'password',
                                          'password': 'fixture-password', 'jump_password': base.JUMP_PASSWORD,
                                          'name': 'Named on the jump host', 'family': 'quadruped',
                                          'read_roots': ['/opt/ros'], 'watch_services': base.SERVICES})

        view = self.engine.status({'project_id': self.pid, 'robot_id': connection['robot_id']})

        self.assertEqual(connection['alias'], self.ALIAS)
        self.assertEqual(connection['host'], '127.0.0.1')              # later connections need no lookup
        self.assertTrue(view['reachable'], view)
        self.assertEqual(view['status']['hostname'], 'dog-fixture')
        self.assertEqual(self.jump.commands, [self.resolve])

    def test_a_user_or_port_given_explicitly_wins_over_the_jump_hosts_config(self):
        self.jump.command_outputs[self.resolve] = (f'user someone-else\nhostname 127.0.0.1\nport 1\n').encode()

        second = self.scan_both(username='robot', port=self.target.port)

        self.assertEqual((second['port'], second['username']), (self.target.port, 'robot'))

    def test_a_name_cannot_carry_shell_text_to_the_jump_host(self):
        for name in ('dog;reboot', 'dog $(reboot)', '-oProxyCommand=reboot', 'dog`id`'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.engine.scan(self.named(host=name))

        self.assertEqual(self.jump.commands, [])
        self.assertEqual(self.jump.transports, [])

    def test_a_name_that_needs_a_further_hop_is_refused(self):
        for setting in ('proxyjump inner-gate', 'proxycommand ssh -W %h:%p inner-gate'):
            with self.subTest(setting=setting):
                self.jump.command_outputs[self.resolve] = self.ssh_config(extra=setting + '\n')
                with self.assertRaises(ValueError) as caught:
                    self.scan_both()
                self.assertIn('多跳', str(caught.exception))

        self.assertEqual(self.target.transports, [])

    def test_a_jump_host_that_cannot_resolve_the_name_says_so(self):
        del self.jump.command_outputs[self.resolve]                   # e.g. an ssh without -G

        with self.assertRaises(ValueError) as caught:
            self.scan_both()

        self.assertIn('解析', str(caught.exception))
        self.assertEqual(self.target.transports, [])

    def test_a_jump_host_that_never_answers_the_lookup_does_not_hang_the_scan(self):
        self.jump.unanswered.add(self.resolve)
        with mock.patch.object(device_access, 'CHANNEL_TIMEOUT', 1):
            started = time.monotonic()
            with self.assertRaises(ValueError) as caught:
                self.scan_both()
            waited = time.monotonic() - started

        self.assertIn('解析', str(caught.exception))
        self.assertLess(waited, 6)

    def test_a_jump_host_that_never_answers_the_request_to_run_the_lookup_does_not_hang_the_scan(self):
        self.jump.exec_answer_delay = 8                               # the session opens; the request itself is ignored
        with mock.patch.object(device_access, 'CHANNEL_TIMEOUT', 1):
            started = time.monotonic()
            with self.assertRaises(ValueError) as caught:
                self.scan_both()
            waited = time.monotonic() - started

        self.assertIn('解析', str(caught.exception))
        self.assertLess(waited, 6)

    def test_a_jump_session_that_is_already_gone_is_a_plain_refusal(self):
        client = mock.Mock()
        client.get_transport.return_value = None                      # what paramiko answers once the connection is closed

        with self.assertRaises(ValueError) as caught:
            device_access.resolve_on_jump(client, {'host': self.ALIAS, 'port': None, 'username': ''})

        self.assertIn('解析', str(caught.exception))

    def test_an_exit_status_that_arrives_before_the_output_does_not_lose_the_answer(self):
        self.jump.exit_before_output.add(self.resolve)                # an ssh server may report the exit first

        second = self.scan_both()

        self.assertEqual((second['host'], second['port']), ('127.0.0.1', self.target.port))

    def test_an_answer_longer_than_the_limit_is_not_trusted(self):
        # The last value of a setting counts, so an answer that was cut short cannot be relied on.
        self.jump.command_outputs[self.resolve] = self.ssh_config() + b'x' * (device_access.RESOLVE_OUTPUT_LIMIT + 1)

        with self.assertRaises(ValueError) as caught:
            self.scan_both()

        self.assertIn('解析', str(caught.exception))
        self.assertEqual(self.target.transports, [])

    def test_values_from_the_jump_host_are_checked_like_typed_ones(self):
        answers = {'a host that reads as an option': 'hostname -oProxyCommand=reboot\nuser robot\nport 22\n',
                   'shell text in the host': 'hostname 127.0.0.1;reboot\nuser robot\nport 22\n',
                   'shell text in the user': 'hostname 127.0.0.1\nuser robot$(reboot)\nport 22\n',
                   'a port that is not a number': 'hostname 127.0.0.1\nuser robot\nport ssh\n',
                   'a port out of range': 'hostname 127.0.0.1\nuser robot\nport 99999\n'}
        for case, answer in answers.items():
            with self.subTest(case=case):
                self.jump.command_outputs[self.resolve] = answer.encode()
                with self.assertRaises(ValueError):
                    self.scan_both()

        self.assertEqual(self.jump.forwarded, [])                     # nothing was opened towards such an address
        self.assertEqual(self.target.transports, [])

    def test_text_printed_after_the_answer_that_is_not_a_setting_changes_nothing(self):
        self.jump.command_outputs[self.resolve] = self.ssh_config() + b'\nsession closed, goodbye\n'

        second = self.scan_both()

        self.assertEqual((second['host'], second['port'], second['username']), ('127.0.0.1', self.target.port, 'robot'))

    def test_noise_before_the_answer_is_not_taken_for_the_answer(self):
        # A login script on the jump host may print anything before ssh's own output.
        self.jump.command_outputs[self.resolve] = b'hostname 192.0.2.99\nwelcome to the gate\n' + self.ssh_config()

        second = self.scan_both()

        self.assertEqual((second['host'], second['port']), ('127.0.0.1', self.target.port))

    def test_a_request_with_a_malformed_host_is_a_plain_refusal(self):
        for host in (42, ['dog'], None, {'name': 'dog'}):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    self.engine.scan(self.named(host=host))
                with self.assertRaises(ValueError):
                    self.engine.scan(self.request(jump=False, host=host))

    def test_resolving_on_the_jump_host_needs_a_jump_host(self):
        with self.assertRaises(ValueError):
            self.engine.scan(self.request(jump=False, host=self.ALIAS, resolve_on_jump=True))
