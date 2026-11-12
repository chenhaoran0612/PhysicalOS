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
        self.jump.command_outputs[self.resolve] = self.ssh_config(extra='proxyjump inner-gate\n')

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
