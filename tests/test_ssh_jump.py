"""SSH access through a jump host (one hop, like ``ssh -J``) and the read-only live status.

Both ends are local protocol fixtures: nothing here reaches a real machine.
"""
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import paramiko

from brainos import device_access, device_software, device_status, hardware_scan
from brainos.service import Platform
from ssh_fixture import SSHFixture

JUMP_USER, JUMP_PASSWORD = 'gate', 'jump-fixture-password'
SERVICES = ['bridge', 'nav-stack', 'recorder']
STATUS_OUTPUT = b"""##host
dog-fixture
##uptime
372.51 1480.12
##load
0.52 0.61 0.40 2/512 1234
##mem
MemTotal:       16000000 kB
MemAvailable:    4000000 kB
##disk
/dev/nvme0n1p1 100000000 25000000 75000000 25% /
##temp
45500
61300
##failed
idle-start.service loaded failed failed Idle start
##systemd
ok
##services
active
failed
inactive
##end
"""


class PlainListener:
    """A TCP port that answers, but not with SSH (a web server on the wrong port, say)."""

    def __init__(self):
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen()
        self.port = self.socket.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                client, _ = self.socket.accept()
            except OSError:
                return
            with client:
                try:
                    client.sendall(b'HTTP/1.1 400 Bad Request\r\n\r\n')
                except OSError:
                    pass

    def close(self):
        self.socket.close()


class SshCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.platform = Platform(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.platform.close)
        self.pid = self.platform.add_project({'name': 'Test', 'objective': 'Reach a robot behind a jump host'})['id']
        self.engine, self.store = self.platform.engineering, self.platform.store
        self.target = self.fixture(command_outputs={device_status.status_command(SERVICES): STATUS_OUTPUT})
        self.jump = self.fixture(username=JUMP_USER, password=JUMP_PASSWORD,
                                 forward_to=[('127.0.0.1', self.target.port)])

    def fixture(self, **options):
        fixture = SSHFixture(**options)
        self.addCleanup(fixture.close)
        return fixture

    def request(self, jump=True, **extra):
        """A scan request. ``jump``: True for the jump fixture, False for direct, or another jump host."""
        request = {'project_id': self.pid, 'host': '127.0.0.1', 'port': self.target.port, 'username': 'robot'}
        if jump:
            default = {'host': '127.0.0.1', 'port': self.jump.port, 'username': JUMP_USER}
            request['jump'] = jump if isinstance(jump, dict) else default
        return {**request, **extra}

    def jump_login(self, first, **extra):
        return {'jump_check_id': first['id'], 'trust_jump_fingerprint': first['fingerprint'],
                'jump_auth': 'password', 'jump_password': JUMP_PASSWORD, **extra}

    def connect_through_jump(self, **extra):
        first = self.engine.scan(self.request())
        second = self.engine.scan(self.request(**self.jump_login(first)))
        return self.engine.connect({'project_id': self.pid, 'host_check_id': second['id'],
                                    'trust_fingerprint': second['fingerprint'], 'auth': 'password',
                                    'password': 'fixture-password', 'jump_password': JUMP_PASSWORD,
                                    'name': 'Robot behind a jump host', 'family': 'quadruped',
                                    'read_roots': ['/opt/ros'], **extra})

    def connect_directly(self, **extra):
        check = self.engine.scan(self.request(jump=False))
        return self.engine.connect({'project_id': self.pid, 'host_check_id': check['id'],
                                    'trust_fingerprint': check['fingerprint'], 'auth': 'password',
                                    'password': 'fixture-password', 'name': 'Robot on the LAN', 'family': 'quadruped',
                                    'read_roots': ['/opt/ros'], **extra})

    def secret(self, connection):
        return self.store.get('agent_secrets', connection['id'])

    def plain_listener(self):
        listener = PlainListener()
        self.addCleanup(listener.close)
        return listener

    def closed_port(self):
        fixture = SSHFixture()
        fixture.close()
        return fixture.port

    def assert_refused(self, hop, reason, mention, call):
        with self.assertRaises(device_access.ConnectError) as caught:
            call()
        self.assertEqual((caught.exception.hop, caught.exception.reason), (hop, reason))
        self.assertIn(mention, str(caught.exception))


class JumpHostTests(SshCase):
    def test_first_scan_reads_only_the_jump_host_key_and_sends_no_credentials(self):
        first = self.engine.scan(self.request())

        self.assertEqual(first['stage'], 'jump')
        self.assertEqual(first['fingerprint'], device_access.fingerprint(self.jump.key))
        self.assertEqual(self.jump.auth_attempts, [])
        self.assertEqual(self.target.transports, [])                 # the robot has not been contacted at all

    def test_target_key_is_read_through_the_confirmed_jump_host(self):
        first = self.engine.scan(self.request())

        second = self.engine.scan(self.request(**self.jump_login(first)))

        self.assertEqual(second['fingerprint'], device_access.fingerprint(self.target.key))
        self.assertEqual(second['jump'], {'host': '127.0.0.1', 'port': self.jump.port, 'username': JUMP_USER,
                                          'auth': 'password', 'fingerprint': first['fingerprint']})
        self.assertEqual(self.jump.forwarded, [('127.0.0.1', self.target.port)])
        self.assertEqual(self.target.auth_attempts, [])              # robot credentials wait for its own fingerprint

    def test_unconfirmed_jump_fingerprint_stops_before_authenticating(self):
        first = self.engine.scan(self.request())

        with self.assertRaises(ValueError):
            self.engine.scan(self.request(**self.jump_login(first, trust_jump_fingerprint='SHA256:wrong')))

        self.assertEqual(self.jump.auth_attempts, [])

    def test_a_failed_first_scan_names_the_jump_host(self):
        down = {'host': '127.0.0.1', 'port': self.closed_port(), 'username': JUMP_USER}
        not_ssh = {'host': '127.0.0.1', 'port': self.plain_listener().port, 'username': JUMP_USER}

        self.assert_refused('jump', 'unreachable', '跳板机', lambda: self.engine.scan(self.request(jump=down)))
        self.assert_refused('jump', 'no_ssh', '跳板机', lambda: self.engine.scan(self.request(jump=not_ssh)))

        self.assertEqual(self.target.transports, [])

    def test_a_failed_second_scan_names_the_robot(self):
        first = self.engine.scan(self.request())
        not_ssh = self.plain_listener()
        self.jump.forward_to.add(('127.0.0.1', not_ssh.port))
        unreachable = self.closed_port()                              # not in forward_to: the jump host cannot connect

        self.assert_refused('device', 'no_ssh', '目标', lambda: self.engine.scan(
            self.request(port=not_ssh.port, **self.jump_login(first))))
        self.assert_refused('device', 'unreachable', '目标', lambda: self.engine.scan(
            self.request(port=unreachable, **self.jump_login(first))))

    def test_a_failed_direct_scan_says_what_is_wrong_with_the_address(self):
        self.assert_refused('device', 'unreachable', '不可达', lambda: self.engine.scan(
            self.request(jump=False, port=self.closed_port())))
        self.assert_refused('device', 'no_ssh', 'SSH', lambda: self.engine.scan(
            self.request(jump=False, port=self.plain_listener().port)))

    def test_a_slow_hop_is_reported_as_a_timeout_not_as_a_wrong_address(self):
        # Found on a real jump host: each round trip took seconds, a phase ran past its
        # limit and the user was told to check the address.
        connection = self.connect_through_jump()
        secret = self.secret(connection)
        with mock.patch.multiple(device_access, HANDSHAKE_TIMEOUT=1, AUTH_TIMEOUT=1):
            self.jump.auth_delay = 2
            self.assert_refused('jump', 'timeout', '超时', lambda: device_access.SSHDevice(connection, secret))
            self.jump.auth_delay = 0
            self.target.banner_delay = 2
            self.assert_refused('device', 'timeout', '超时', lambda: device_access.SSHDevice(connection, secret))

    def test_a_slow_scan_is_reported_as_a_timeout(self):
        with mock.patch.object(device_access, 'HANDSHAKE_TIMEOUT', 1):
            self.jump.banner_delay = 2
            self.assert_refused('jump', 'timeout', '超时', lambda: self.engine.scan(self.request()))

    def test_malformed_host_names_are_refused_before_any_lookup(self):
        for host in ('a..b', 'x' * 64 + '.example.com', '.example.com'):
            with self.subTest(host=host):
                with self.assertRaises(ValueError) as caught:
                    self.engine.scan(self.request(jump=False, host=host))
                self.assertIn('主机名', str(caught.exception))
                self.assertNotIn('idna', str(caught.exception))

    def test_a_scan_that_stopped_at_the_jump_host_cannot_be_used_to_connect(self):
        first = self.engine.scan(self.request())

        with self.assertRaises(ValueError):
            self.engine.connect({'project_id': self.pid, 'host_check_id': first['id'],
                                 'trust_fingerprint': first['fingerprint'], 'auth': 'password',
                                 'password': 'fixture-password', 'name': 'Wrong step', 'family': 'quadruped'})

    def test_probes_run_on_the_robot_not_on_the_jump_host(self):
        connection = self.connect_through_jump()
        device = device_access.SSHDevice(connection, self.secret(connection))
        self.addCleanup(device.close)

        result = device.probe('system')

        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.target.commands, [device_access.PROBES['system'][1]])
        self.assertEqual(self.jump.commands, [])

    def test_jump_credentials_stay_out_of_the_workspace_snapshot(self):
        connection = self.connect_through_jump()

        serialized = json.dumps(self.platform.snapshot(), ensure_ascii=False)

        self.assertEqual(connection['jump']['host'], '127.0.0.1')
        self.assertNotIn(JUMP_PASSWORD, serialized)
        self.assertNotIn('fixture-password', serialized)

    def test_a_changed_fingerprint_on_either_hop_is_refused_and_named(self):
        connection = self.connect_through_jump()
        changed = {'jump': {**connection, 'jump': {**connection['jump'], 'fingerprint': 'SHA256:changed'}},
                   'device': {**connection, 'fingerprint': 'SHA256:changed'}}
        for hop, altered in changed.items():
            with self.subTest(hop=hop):
                with self.assertRaises(device_access.ConnectError) as caught:
                    device_access.SSHDevice(altered, self.secret(connection))

                self.assertEqual((caught.exception.hop, caught.exception.reason), (hop, 'fingerprint'))

    def test_a_wrong_jump_password_is_reported_as_the_jump_host_refusing_the_login(self):
        connection = self.connect_through_jump()

        with self.assertRaises(device_access.ConnectError) as caught:
            device_access.SSHDevice(connection, {**self.secret(connection), 'jump_password': 'not-the-password'})

        self.assertEqual((caught.exception.hop, caught.exception.reason), ('jump', 'auth'))
        self.assertEqual(self.target.auth_attempts.count('robot'), 1)    # only the earlier, successful connect

    def test_an_unreadable_jump_key_is_reported_for_the_jump_host(self):
        connection = self.connect_through_jump()
        with_key = {**connection, 'jump': {**connection['jump'], 'auth': 'key'}}

        with self.assertRaises(device_access.ConnectError) as caught:
            device_access.SSHDevice(with_key, {**self.secret(connection), 'jump_private_key': 'not a private key'})

        self.assertEqual((caught.exception.hop, caught.exception.reason), ('jump', 'key'))
        self.assertIn('跳板机', str(caught.exception))

    def test_an_unreachable_jump_host_and_an_unreachable_robot_are_told_apart(self):
        connection = self.connect_through_jump()
        secret = self.secret(connection)

        self.jump.forward_to.clear()                                  # the jump host can no longer reach the robot
        with self.assertRaises(device_access.ConnectError) as caught:
            device_access.SSHDevice(connection, secret)
        self.assertEqual((caught.exception.hop, caught.exception.reason), ('device', 'unreachable'))

        self.jump.close()
        with self.assertRaises(device_access.ConnectError) as caught:
            device_access.SSHDevice(connection, secret)
        self.assertEqual((caught.exception.hop, caught.exception.reason), ('jump', 'unreachable'))

    def test_a_jump_host_that_forbids_forwarding_is_named_as_the_cause(self):
        connection = self.connect_through_jump()
        self.jump.forwarding = False

        with self.assertRaises(device_access.ConnectError) as caught:
            device_access.SSHDevice(connection, self.secret(connection))

        self.assertEqual((caught.exception.hop, caught.exception.reason), ('device', 'forbidden'))
        self.assertIn('转发', str(caught.exception))

    def test_the_second_scan_step_only_accepts_the_confirmed_jump_host(self):
        first = self.engine.scan(self.request())
        device_check = self.engine.scan(self.request(jump=False))
        other_project = self.platform.add_project({'name': 'Other', 'objective': 'Other'})['id']
        other_jump = self.fixture(username=JUMP_USER, password=JUMP_PASSWORD)
        refused = {
            'a device check used as the jump check': self.request(**self.jump_login(first, jump_check_id=device_check['id'])),
            'a check of another project': {**self.request(**self.jump_login(first)), 'project_id': other_project},
            'a different jump host than the one confirmed': {
                **self.request(**self.jump_login(first)),
                'jump': {'host': '127.0.0.1', 'port': other_jump.port, 'username': JUMP_USER}},
        }
        for case, request in refused.items():
            with self.subTest(case):
                with self.assertRaises(ValueError):
                    self.engine.scan(request)

        self.assertEqual(self.jump.auth_attempts + other_jump.auth_attempts, [])

    def test_a_direct_connection_needs_no_jump_host(self):
        connection = self.connect_directly()
        device = device_access.SSHDevice(connection, self.secret(connection))
        self.addCleanup(device.close)

        device.probe('network')

        self.assertNotIn('jump', connection)
        self.assertEqual(self.target.commands, [device_access.PROBES['network'][1]])
        self.assertEqual(self.jump.transports, [])


class KeyLoginTests(SshCase):
    """Logins without a password: pasted private keys, and the local SSH agent (what a pasted
    ``ssh`` command is set up to use)."""

    def setUp(self):
        super().setUp()
        self.device_key, self.jump_key = paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)
        self.target = self.fixture(authorized_keys=[self.device_key])
        self.jump = self.fixture(username=JUMP_USER, authorized_keys=[self.jump_key],
                                 forward_to=[('127.0.0.1', self.target.port)])

    @staticmethod
    def pem(key):
        text = io.StringIO()
        key.write_private_key(text)
        return text.getvalue()

    def connect(self, jump_login, device_login):
        first = self.engine.scan(self.request())
        second = self.engine.scan(self.request(jump_check_id=first['id'], trust_jump_fingerprint=first['fingerprint'],
                                               **jump_login))
        return self.engine.connect({'project_id': self.pid, 'host_check_id': second['id'],
                                    'trust_fingerprint': second['fingerprint'], 'name': 'Key login', 'family': 'quadruped',
                                    'read_roots': ['/opt/ros'], **jump_login, **device_login})

    def assert_probe_reaches_the_robot(self, connection):
        device = device_access.SSHDevice(connection, self.secret(connection))
        self.addCleanup(device.close)
        self.assertEqual(device.probe('system')['exit_code'], 0)
        self.assertEqual(self.target.commands, [device_access.PROBES['system'][1]])
        self.assertEqual(self.jump.auth_attempts + self.target.auth_attempts, [])      # no password was tried

    def test_a_private_key_for_each_hop(self):
        connection = self.connect({'jump_auth': 'key', 'jump_private_key': self.pem(self.jump_key)},
                                  {'auth': 'key', 'private_key': self.pem(self.device_key)})

        self.assert_probe_reaches_the_robot(connection)
        self.assertNotIn('PRIVATE KEY', json.dumps(self.platform.snapshot()))

    @unittest.skipUnless(shutil.which('ssh-agent') and shutil.which('ssh-add'), 'OpenSSH agent tools required')
    def test_the_local_ssh_agent_for_both_hops(self):
        self.start_agent_with(self.device_key, self.jump_key)

        connection = self.connect({'jump_auth': 'agent'}, {'auth': 'agent'})

        self.assert_probe_reaches_the_robot(connection)
        self.assertEqual({k: v for k, v in self.secret(connection).items() if k != 'id' and v}, {})   # nothing stored

    def start_agent_with(self, *keys):
        directory = tempfile.mkdtemp(prefix='nxa', dir='/tmp')        # short: socket paths are limited to ~100 bytes
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        socket_path = os.path.join(directory, 'agent.sock')
        agent = subprocess.Popen(['ssh-agent', '-D', '-a', socket_path], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_process, agent)
        environment = mock.patch.dict(os.environ, {'SSH_AUTH_SOCK': socket_path})
        environment.start()
        self.addCleanup(environment.stop)
        deadline = time.monotonic() + 5
        while True:                                                   # `ssh-add -l` exits 2 until the agent answers
            listing = subprocess.run(['ssh-add', '-l'], env={**os.environ}, timeout=10,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if listing.returncode != 2:
                break
            self.assertLess(time.monotonic(), deadline, 'ssh-agent did not start')
            time.sleep(0.05)
        for index, key in enumerate(keys):
            path = os.path.join(directory, f'key{index}')
            key.write_private_key_file(path)
            subprocess.run(['ssh-add', '-q', path], check=True, env={**os.environ}, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


    @staticmethod
    def stop_process(process):
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)


class StatusParsingTests(unittest.TestCase):
    def test_status_output_becomes_readable_fields(self):
        status = device_status.parse_status(STATUS_OUTPUT.decode(), SERVICES)

        self.assertEqual(status, {
            'hostname': 'dog-fixture', 'uptime_seconds': 372, 'load': [0.52, 0.61, 0.4],
            'memory': {'total_kb': 16000000, 'available_kb': 4000000, 'used_percent': 75},
            'disk_root': {'total_kb': 100000000, 'used_kb': 25000000, 'used_percent': 25},
            'temperature_c': 61.3, 'failed_units': ['idle-start.service'],
            'services': [{'name': 'bridge', 'state': 'active'}, {'name': 'nav-stack', 'state': 'failed'},
                         {'name': 'recorder', 'state': 'inactive'}]})

    def test_missing_sections_are_reported_as_unknown_not_as_zero(self):
        status = device_status.parse_status('##host\nbare-board\n##end\n', ['bridge'])

        self.assertEqual(status['hostname'], 'bare-board')
        self.assertIsNone(status['uptime_seconds'])
        self.assertIsNone(status['memory'])
        self.assertIsNone(status['temperature_c'])
        self.assertIsNone(status['failed_units'])                     # no usable systemd: unknown, not "none failed"
        self.assertEqual(status['services'], [{'name': 'bridge', 'state': 'unknown'}])

    def test_hostile_device_output_never_breaks_the_status(self):
        hostile = ('##host\n' + 'h' * 5000 + '\n##uptime\ninf 1\n##load\nnan nan nan\n##mem\nMemTotal: inf kB\n'
                   '##disk\n/dev/x nan nan nan nan% /\n##temp\ninf\nnan\n##services\nconstructor\n' + 'x' * 400 + '\n##end\n')

        status = device_status.parse_status(hostile, ['bridge', 'nav-stack', 'recorder'])

        json.dumps(status, allow_nan=False)                           # the API refuses NaN / Infinity
        self.assertEqual(len(status['hostname']), 255)
        self.assertIsNone(status['uptime_seconds'])
        self.assertIsNone(status['load'])
        self.assertIsNone(status['memory'])
        self.assertIsNone(status['temperature_c'])
        self.assertEqual([s['state'] for s in status['services']], ['constructor', 'unknown', 'unknown'])

    def test_watched_service_names_cannot_carry_shell_text(self):
        for names in (['bridge; reboot'], ['$(reboot)'], ['a b'], ['-rf'], [''], ['x' * 200], ['ok'] * 13, 'bridge'):
            with self.subTest(names=names):
                with self.assertRaises(ValueError):
                    device_status.validate_services(names)

        self.assertEqual(device_status.validate_services(['nav-stack.service', 'ros@2.target']),
                         ['nav-stack.service', 'ros@2.target'])


class LiveStatusTests(SshCase):
    def status(self, connection):
        return self.engine.status({'project_id': self.pid, 'robot_id': connection['robot_id']})

    def test_status_of_a_robot_behind_a_jump_host(self):
        connection = self.connect_through_jump(watch_services=SERVICES)

        view = self.status(connection)

        self.assertTrue(view['reachable'], view)
        self.assertTrue(view['via_jump'])
        self.assertIsNone(view['error'])
        self.assertGreaterEqual(view['latency_ms'], 0)
        self.assertEqual(view['status']['hostname'], 'dog-fixture')
        self.assertEqual(view['status']['services'][1], {'name': 'nav-stack', 'state': 'failed'})
        # Fixed commands only: the basic status, then (issue #8) the running software and the hardware scan.
        self.assertEqual(self.target.commands, [device_status.status_command(SERVICES), device_software.SOFTWARE_EXEC,
                                                hardware_scan.SCAN_EXEC])
        self.assertEqual(self.jump.commands, [])

    def test_status_of_a_directly_connected_robot(self):
        connection = self.connect_directly(watch_services=SERVICES)

        view = self.status(connection)

        self.assertTrue(view['reachable'], view)
        self.assertFalse(view['via_jump'])
        self.assertEqual(view['status']['uptime_seconds'], 372)

    def test_a_short_password_does_not_corrupt_the_numbers(self):
        self.target.password = '5'                                    # a digit that occurs all over the status
        connection = self.connect_directly(watch_services=SERVICES, password='5')

        view = self.status(connection)

        self.assertEqual(view['status']['load'], [0.52, 0.61, 0.4])
        self.assertEqual(view['status']['memory']['used_percent'], 75)

    def test_a_robot_that_never_answers_the_request_to_run_the_status_command_is_not_waited_for(self):
        connection = self.connect_directly(watch_services=SERVICES)
        self.target.exec_answer_delay = 8                             # the session opens; the request itself is ignored
        with mock.patch.object(device_status, 'COMMAND_TIMEOUT_SECONDS', 1):
            started = time.monotonic()
            view = self.status(connection)
            waited = time.monotonic() - started

        self.assertFalse(view['reachable'])
        self.assertEqual((view['error']['hop'], view['error']['reason']), ('device', 'timeout'))
        self.assertLess(waited, 6)

    def test_polling_reuses_one_ssh_session(self):
        connection = self.connect_through_jump(watch_services=SERVICES)
        self.status(connection)
        sessions = (len(self.jump.transports), len(self.target.transports))

        view = self.status(connection)

        self.assertTrue(view['reachable'], view)
        self.assertEqual((len(self.jump.transports), len(self.target.transports)), sessions)

    def test_a_dropped_session_is_reopened_without_reporting_the_robot_offline(self):
        connection = self.connect_directly(watch_services=SERVICES)
        self.status(connection)
        for transport in list(self.target.transports):
            transport.close()                                           # e.g. the robot restarted its SSH server

        view = self.status(connection)

        self.assertTrue(view['reachable'], view)

    def test_an_unreachable_hop_is_named_in_the_status(self):
        connection = self.connect_through_jump(watch_services=SERVICES)
        self.jump.forward_to.clear()

        view = self.status(connection)

        self.assertFalse(view['reachable'])
        self.assertIsNone(view['status'])
        self.assertEqual((view['error']['hop'], view['error']['reason']), ('device', 'unreachable'))
        self.assertIn('跳板机', view['error']['message'])

    def test_watched_services_can_be_changed_after_connecting(self):
        connection = self.connect_directly()

        updated = self.engine.watch_services({'project_id': self.pid, 'robot_id': connection['robot_id'],
                                              'services': SERVICES})
        view = self.status(connection)

        self.assertEqual(updated['watch_services'], SERVICES)
        self.assertEqual([s['name'] for s in view['status']['services']], SERVICES)

    def test_reconnecting_a_robot_closes_the_session_of_its_old_connection(self):
        old = self.connect_directly(watch_services=SERVICES)
        self.status(old)
        self.assertEqual(self.engine.status_sessions.open_connections(), [old['id']])

        new = self.connect_directly(robot_id=old['robot_id'], watch_services=SERVICES)

        self.assertNotEqual(new['id'], old['id'])
        self.assertEqual(self.engine.status_sessions.open_connections(), [])

    def test_a_robot_without_an_ssh_connection_has_no_ssh_status(self):
        robot = self.platform.add_robot({'project_id': self.pid, 'name': 'No SSH', 'family': 'custom'})

        with self.assertRaises(ValueError):
            self.engine.status({'project_id': self.pid, 'robot_id': robot['id']})


class FakeDevice:
    """Stands in for SSHDevice: answers the status command, remembers whether it was closed."""
    opened = []

    def __init__(self, connection, secret):
        self.connection_id, self.closed = connection['id'], False
        FakeDevice.opened.append(self)

    def execute(self, command, timeout, redact=True):
        return {'stdout': STATUS_OUTPUT.decode(), 'timed_out': False}

    def close(self):
        self.closed = True


class StatusSessionLifetimeTests(unittest.TestCase):
    def setUp(self):
        FakeDevice.opened = []
        patcher = mock.patch.object(device_access, 'SSHDevice', FakeDevice)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sessions(self, **options):
        sessions = device_status.StatusSessions(**options)
        self.addCleanup(sessions.close)
        return sessions

    @staticmethod
    def connection(key):
        return {'id': key, 'host': '127.0.0.1', 'port': 22, 'username': 'robot', 'watch_services': []}

    def test_an_idle_session_is_closed_even_while_another_robot_is_polled(self):
        sessions = self.sessions(idle_seconds=0.2, sweep_seconds=0.05)
        sessions.read(self.connection('idle'), {})
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            sessions.read(self.connection('busy'), {})
            time.sleep(0.03)

        idle, busy = FakeDevice.opened
        self.assertTrue(idle.closed)
        self.assertFalse(busy.closed)
        self.assertEqual(sessions.open_connections(), ['busy'])

    def test_a_status_command_that_times_out_is_reported_and_its_session_dropped(self):
        sessions = self.sessions()
        with mock.patch.object(FakeDevice, 'execute', return_value={'stdout': '', 'timed_out': True}):
            view = sessions.read(self.connection('slow'), {})

        self.assertFalse(view['reachable'])
        self.assertEqual((view['error']['hop'], view['error']['reason']), ('device', 'timeout'))
        self.assertTrue(FakeDevice.opened[0].closed)

    def test_a_poll_during_the_first_connect_says_connecting_not_unreachable(self):
        sessions = self.sessions()
        connecting, release = threading.Event(), threading.Event()

        def slow_connect(device, connection, secret):
            connecting.set()
            release.wait(5)
            device.closed = False

        with mock.patch.object(FakeDevice, '__init__', slow_connect):
            first = threading.Thread(target=sessions.read, args=(self.connection('new'), {}))
            first.start()
            self.addCleanup(first.join)
            self.addCleanup(release.set)
            self.assertTrue(connecting.wait(5))

            view = sessions.read(self.connection('new'), {})

        self.assertIsNone(view['reachable'])
        self.assertIsNone(view['error'])
        self.assertTrue(view['refreshing'])

    def test_a_lost_session_through_a_jump_host_does_not_blame_the_robot(self):
        sessions = self.sessions()
        through_jump = {**self.connection('behind-jump'), 'jump': {'host': '127.0.0.1', 'port': 22, 'username': 'gate'}}
        with mock.patch.object(FakeDevice, 'execute', side_effect=OSError('connection reset')):
            via_jump = sessions.read(through_jump, {})
            direct = sessions.read(self.connection('direct'), {})

        self.assertEqual(via_jump['error']['hop'], 'connection')      # either hop may have dropped it
        self.assertIn('跳板机', via_jump['error']['message'])
        self.assertEqual(direct['error']['hop'], 'device')
        self.assertNotIn('跳板机', direct['error']['message'])

    def test_forgetting_a_connection_does_not_wait_for_a_poll_in_flight(self):
        sessions = self.sessions()
        reading, release = threading.Event(), threading.Event()

        def slow_execute(device, command, timeout, redact=True):
            reading.set()
            release.wait(5)
            return {'stdout': STATUS_OUTPUT.decode(), 'timed_out': False}

        with mock.patch.object(FakeDevice, 'execute', slow_execute):
            poll = threading.Thread(target=sessions.read, args=(self.connection('replaced'), {}))
            poll.start()
            self.assertTrue(reading.wait(5))
            started = time.monotonic()
            sessions.forget('replaced')
            waited = time.monotonic() - started
            release.set()
            poll.join(5)

        self.assertLess(waited, 1.0)
        self.assertTrue(FakeDevice.opened[0].closed)                  # the poll closes its own session when it ends
        self.assertEqual(sessions.open_connections(), [])

    def test_a_forgotten_connection_closes_its_session(self):
        sessions = self.sessions()
        sessions.read(self.connection('replaced'), {})

        sessions.forget('replaced')

        self.assertTrue(FakeDevice.opened[0].closed)
        self.assertEqual(sessions.open_connections(), [])

    def test_no_session_is_opened_after_close(self):
        sessions = self.sessions()
        sessions.close()

        with self.assertRaises(ValueError):
            sessions.read(self.connection('late'), {})

        self.assertEqual(FakeDevice.opened, [])


class ForgetfulChannel:
    """paramiko's Channel when close() lands just before exec_command starts to wait: exec_command first
    clears the channel's event (Channel._event_pending), so the wakeup that close() gave is lost."""

    def __init__(self):
        self.event, self.closed = threading.Event(), False

    def close(self):
        self.closed = True
        self.event.set()

    def exec_command(self, command):
        time.sleep(0.2)                                               # the time limit passes here
        self.event.clear()
        if not self.event.wait(5):
            raise AssertionError('exec_command was never woken')
        raise paramiko.SSHException('Channel closed.')


class RequestCommandTests(unittest.TestCase):
    def test_a_close_that_exec_command_forgot_still_ends_the_wait(self):
        started = time.monotonic()

        with self.assertRaises(TimeoutError):
            device_access.request_command(ForgetfulChannel(), 'true', 0.05)

        self.assertLess(time.monotonic() - started, 2)

    def test_nothing_is_asked_when_no_time_is_left(self):
        channel = mock.Mock()

        for remaining in (0, -3):                                     # e.g. the job's own deadline has passed
            with self.subTest(remaining=remaining):
                with self.assertRaises(TimeoutError):
                    device_access.request_command(channel, 'true', remaining)

        channel.exec_command.assert_not_called()

    def test_an_error_before_the_time_limit_is_passed_on_as_it_is(self):
        channel = mock.Mock()
        channel.exec_command.side_effect = paramiko.SSHException('Channel is not open')

        with self.assertRaises(paramiko.SSHException):
            device_access.request_command(channel, 'true', 5)


if __name__ == '__main__':
    unittest.main()
