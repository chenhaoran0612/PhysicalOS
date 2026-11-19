"""Issue #15: SSH access through several jump hosts in a row, like ``ssh -J a,b,c robot``.

Three jump hosts here, each a local SSH protocol fixture with its own host key and login; only the
last one can reach the "robot". Nothing in this file reaches a real machine.
"""
import json
import secrets
import threading
import time
import unittest
from unittest import mock

import test_ssh_jump as base
from brainos import device_access, hardware_init, hardware_scan, ssh_route
from brainos.service import Platform
from hardware_fixture import jetson

LEVELS = 3
SCAN = hardware_scan.SCAN_EXEC


def endpoint(fixture, user):
    return {'host': '127.0.0.1', 'port': fixture.port, 'username': user}


class ChainCase(base.SshCase):
    """This machine reaches gate 1 only; gate 1 reaches gate 2, gate 2 gate 3, and gate 3 the robot."""

    def setUp(self):
        super().setUp()
        self.target.command_outputs[SCAN] = jetson().encode()
        # throwaway logins, made up on every run: one user and one password per level
        self.logins = [(f'gate{level}', secrets.token_hex(6)) for level in range(1, LEVELS + 1)]
        self.gates, reach = [], self.target
        for user, password in reversed(self.logins):
            reach = self.fixture(username=user, password=password, forward_to=[('127.0.0.1', reach.port)])
            self.gates.insert(0, reach)

    def jumps(self, logins=0, gates=None):
        """The jump hosts of a request, first hop first, with the logins of the first ``logins`` of them."""
        hops = []
        for index, gate in enumerate(gates or self.gates):
            user, password = next((u, p) for u, p in self.logins if u == gate.username)
            hop = endpoint(gate, user)
            if index < logins:
                hop.update(auth='password', password=password)
            hops.append(hop)
        return hops

    def route(self, logins=0, **extra):
        return {'project_id': self.pid, 'host': '127.0.0.1', 'port': self.target.port, 'username': 'robot',
                'jumps': self.jumps(logins), **extra}

    def all_logins_from_the_start(self, logins=0, **extra):
        """A request that brings every login at once, whatever has been confirmed so far."""
        return self.route(logins=LEVELS, **extra)

    def confirm_each(self, request=None):
        """Read one fingerprint after the other and confirm each, as the form does: every check, the robot's last."""
        request = request or self.route
        checks = [self.engine.scan(request())]
        while checks[-1].get('stage') == 'jump':
            last = checks[-1]
            checks.append(self.engine.scan(request(logins=last['level'], jump_check_id=last['id'],
                                                   trust_jump_fingerprint=last['fingerprint'])))
        return checks

    def connect_through_chain(self, **extra):
        check = self.confirm_each()[-1]
        return self.engine.connect({**self.route(logins=LEVELS), 'host_check_id': check['id'],
                                    'trust_fingerprint': check['fingerprint'], 'auth': 'password',
                                    'password': 'fixture-password', 'name': 'Robot behind three jump hosts',
                                    'family': 'quadruped', 'read_roots': ['/opt/ros'], **extra})

    def open(self, connection, secret=None):
        device = device_access.SSHDevice(connection, secret or self.secret(connection))
        self.addCleanup(device.close)
        return device

    def refused_at(self, hop, level, reason, mentions, call):
        with self.assertRaises(device_access.ConnectError) as caught:
            call()
        error = caught.exception
        self.assertEqual((error.hop, error.level, error.reason), (hop, level, reason), str(error))
        for text in mentions:
            self.assertIn(text, str(error))

    def sessions_closed(self, fixture):
        """True once every session this fixture served has ended."""
        deadline = time.monotonic() + 3
        while any(transport.is_active() for transport in fixture.transports):
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True


class FingerprintStepTests(ChainCase):
    def test_every_hop_is_read_and_confirmed_in_turn_and_no_login_goes_out_before_its_fingerprint(self):
        first = self.engine.scan(self.route())

        self.assertEqual((first['stage'], first['level'], first['total']), ('jump', 1, LEVELS))
        self.assertEqual(first['fingerprint'], device_access.fingerprint(self.gates[0].key))
        self.assertEqual(self.gates[0].auth_attempts, [])
        self.assertEqual(self.gates[1].transports, [])                # nothing beyond the first hop is contacted

        second = self.engine.scan(self.route(logins=1, jump_check_id=first['id'],
                                             trust_jump_fingerprint=first['fingerprint']))

        self.assertEqual((second['stage'], second['level']), ('jump', 2))
        self.assertEqual(second['fingerprint'], device_access.fingerprint(self.gates[1].key))
        self.assertEqual(self.gates[0].auth_attempts, ['gate1'])
        self.assertEqual(self.gates[1].auth_attempts, [])             # its login waits for its own fingerprint
        self.assertEqual(self.gates[2].transports, [])

        third = self.engine.scan(self.route(logins=2, jump_check_id=second['id'],
                                            trust_jump_fingerprint=second['fingerprint']))

        self.assertEqual((third['stage'], third['level']), ('jump', 3))
        self.assertEqual(third['fingerprint'], device_access.fingerprint(self.gates[2].key))
        self.assertEqual(self.gates[2].auth_attempts, [])
        self.assertEqual(self.target.transports, [])

        last = self.engine.scan(self.route(logins=3, jump_check_id=third['id'],
                                           trust_jump_fingerprint=third['fingerprint']))

        self.assertNotIn('stage', last)
        self.assertEqual(last['fingerprint'], device_access.fingerprint(self.target.key))
        self.assertEqual(self.target.auth_attempts, [])               # the robot's login waits for its fingerprint too
        self.assertEqual(last['jumps'], [
            {**endpoint(gate, user), 'auth': 'password', 'fingerprint': device_access.fingerprint(gate.key)}
            for gate, (user, _) in zip(self.gates, self.logins)])

    def test_logins_that_arrive_ahead_of_time_still_wait_for_their_fingerprints(self):
        first = self.engine.scan(self.all_logins_from_the_start())

        self.assertEqual([gate.auth_attempts for gate in self.gates], [[], [], []])

        second = self.engine.scan(self.all_logins_from_the_start(jump_check_id=first['id'],
                                                                 trust_jump_fingerprint=first['fingerprint']))

        self.assertEqual(second['level'], 2)
        self.assertEqual([gate.auth_attempts for gate in self.gates], [['gate1'], [], []])
        self.assertEqual(self.confirm_each(self.all_logins_from_the_start)[-1]['fingerprint'],
                         device_access.fingerprint(self.target.key))

    def test_a_fingerprint_that_was_not_confirmed_stops_the_walk_before_any_login(self):
        first = self.engine.scan(self.route())
        second = self.engine.scan(self.route(logins=1, jump_check_id=first['id'],
                                             trust_jump_fingerprint=first['fingerprint']))

        with self.assertRaises(ValueError) as caught:
            self.engine.scan(self.route(logins=2, jump_check_id=second['id'], trust_jump_fingerprint='SHA256:wrong'))

        self.assertIn('第 2 级', str(caught.exception))
        self.assertEqual(self.gates[1].auth_attempts, [])

    def test_a_step_takes_only_the_route_it_was_confirmed_for(self):
        first = self.engine.scan(self.route())
        second = self.engine.scan(self.route(logins=1, jump_check_id=first['id'],
                                             trust_jump_fingerprint=first['fingerprint']))
        device_check = self.engine.scan(self.request(jump=False))
        other = self.fixture(username='gate2', password=self.logins[1][1], forward_to=[('127.0.0.1', self.gates[2].port)])
        confirmed = {'jump_check_id': second['id'], 'trust_jump_fingerprint': second['fingerprint']}
        swapped = self.route(logins=2, **confirmed)
        swapped['jumps'][1]['port'] = other.port
        refused = {
            'another machine in a confirmed place': swapped,
            'a hop fewer than was confirmed': {**self.route(logins=2, **confirmed), 'jumps': self.jumps(2)[:2]},
            'a device check used as a jump check': self.route(logins=1, jump_check_id=device_check['id'],
                                                              trust_jump_fingerprint=device_check['fingerprint']),
            'the login of a confirmed hop is missing': self.route(logins=1, **confirmed),
        }
        for case, request in refused.items():
            with self.subTest(case):
                with self.assertRaises(ValueError):
                    self.engine.scan(request)

        self.assertEqual(other.transports, [])
        self.assertEqual(self.gates[1].auth_attempts, [])

    def test_a_walk_that_stopped_at_a_jump_host_cannot_be_used_to_connect(self):
        checks = self.confirm_each()

        for check in checks[:-1]:
            with self.subTest(level=check['level']):
                with self.assertRaises(ValueError):
                    self.engine.connect({**self.route(logins=LEVELS), 'host_check_id': check['id'],
                                         'trust_fingerprint': check['fingerprint'], 'auth': 'password',
                                         'password': 'fixture-password', 'name': 'Wrong step', 'family': 'quadruped'})

        self.assertEqual(self.store.list('robots'), [])

    def test_more_jump_hosts_than_the_limit_are_refused_before_anything_is_contacted(self):
        too_many = self.jumps() * 2

        with self.assertRaises(ValueError) as caught:
            self.engine.scan({**self.route(), 'jumps': too_many[:ssh_route.MAX_JUMPS + 1]})

        self.assertIn(str(ssh_route.MAX_JUMPS), str(caught.exception))
        self.assertEqual(self.gates[0].transports, [])

    def test_a_malformed_list_of_jump_hosts_is_a_plain_refusal_that_names_the_level(self):
        broken, no_port = self.jumps(), self.jumps()
        broken[1] = {'host': 'gate;reboot', 'port': 22, 'username': 'ops'}
        no_port[2] = {'host': '127.0.0.1', 'port': 0, 'username': 'ops'}
        cases = {'not a list': ('gate', '跳板机'), 'an entry that is not a hop': (['gate'], '跳板机'),
                 'one of two is not a hop': ([self.jumps()[0], 'gate'], '第 2 级跳板机'),
                 'a name with shell text': (broken, '第 2 级跳板机：'), 'a port out of range': (no_port, '第 3 级跳板机：')}

        for case, (jumps, mention) in cases.items():
            with self.subTest(case):
                with self.assertRaises(ValueError) as caught:
                    self.engine.scan({**self.route(), 'jumps': jumps})
                self.assertIn(mention, str(caught.exception))          # which entry was wrong

        self.assertEqual(self.gates[0].transports, [])

    def test_a_connect_request_must_bring_one_login_per_confirmed_hop(self):
        check = self.confirm_each()[-1]
        confirmed = {'host_check_id': check['id'], 'trust_fingerprint': check['fingerprint'], 'auth': 'password',
                     'password': 'fixture-password', 'name': 'Short of a login', 'family': 'quadruped'}
        attempts = [len(gate.auth_attempts) for gate in self.gates]

        for case, jumps in {'a hop fewer': self.jumps(LEVELS)[:2], 'none': [], 'not a list': 'gate',
                            'a hop without a login': self.jumps(2)}.items():
            with self.subTest(case):
                with self.assertRaises(ValueError):
                    self.engine.connect({**self.route(), **confirmed, 'jumps': jumps})

        self.assertEqual(self.store.list('robots'), [])
        self.assertEqual([len(gate.auth_attempts) for gate in self.gates], attempts)      # nothing was tried


class AcceptanceTests(ChainCase):
    """The acceptance of the issue: a robot is added and reached directly, and through three jump hosts."""

    def test_a_robot_is_added_directly_and_another_through_three_jump_hosts(self):
        direct = self.connect_directly(watch_services=base.SERVICES)
        chained = self.connect_through_chain(watch_services=base.SERVICES)

        self.assertNotIn('jumps', direct)
        self.assertEqual(len(chained['jumps']), LEVELS)
        self.assertEqual(len(self.store.list('robots')), 2)
        for connection, via_jump in ((direct, False), (chained, True)):
            with self.subTest(via_jump=via_jump):
                view = self.engine.status({'project_id': self.pid, 'robot_id': connection['robot_id']})
                self.assertTrue(view['reachable'], view)
                self.assertEqual(view['via_jump'], via_jump)
                self.assertEqual(view['status']['hostname'], 'dog-fixture')

    def test_the_record_keeps_an_ordered_list_with_each_hops_own_address_login_and_fingerprint(self):
        connection = self.connect_through_chain()

        saved = self.store.get('device_connections', connection['id'])
        self.assertNotIn('jump', saved)
        self.assertEqual(saved['jumps'], [
            {**endpoint(gate, user), 'auth': 'password', 'fingerprint': device_access.fingerprint(gate.key)}
            for gate, (user, _) in zip(self.gates, self.logins)])
        secret = self.secret(connection)
        self.assertEqual([hop['password'] for hop in secret['jumps']], [password for _, password in self.logins])
        self.assertEqual(secret['password'], 'fixture-password')
        self.assertFalse([key for key in secret if key.startswith('jump_')])

    def test_commands_run_on_the_robot_and_each_hop_forwards_to_the_next_one_only(self):
        connection = self.connect_through_chain()
        for gate in self.gates:
            gate.forwarded.clear()

        result = self.open(connection).probe('system')

        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.target.commands, [device_access.PROBES['system'][1]])
        for gate, following in zip(self.gates, self.gates[1:] + [self.target]):
            self.assertEqual(gate.commands, [])
            self.assertEqual(gate.forwarded, [('127.0.0.1', following.port)])

    def test_only_what_a_hops_login_method_uses_is_kept(self):
        # the form still holds a password typed before the hop was switched to another method
        check = self.confirm_each()[-1]
        jumps = self.jumps(logins=LEVELS)
        jumps[1].update(private_key='left over from an earlier try', passphrase='left over too')

        connection = self.engine.connect({**self.route(), 'jumps': jumps, 'host_check_id': check['id'],
                                          'trust_fingerprint': check['fingerprint'], 'auth': 'password',
                                          'password': 'fixture-password', 'private_key': 'left over as well',
                                          'name': 'Tidy', 'family': 'quadruped', 'read_roots': ['/opt/ros']})

        secret = self.secret(connection)
        self.assertEqual(secret['jumps'][1], {'password': self.logins[1][1], 'private_key': '', 'passphrase': ''})
        self.assertEqual((secret['password'], secret['private_key']), ('fixture-password', ''))

    def test_no_login_of_any_hop_reaches_the_page(self):
        connection = self.connect_through_chain()

        page = json.dumps(self.platform.snapshot(), ensure_ascii=False)
        # the checks of the fingerprint steps keep the route they were made for: addresses and fingerprints, no login
        checks = json.dumps(self.store.list('host_key_checks'), ensure_ascii=False)

        self.assertIn(self.logins[2][0], page)                         # the route itself is shown
        self.assertIn(connection['jumps'][2]['fingerprint'], checks)
        for _, password in self.logins:
            self.assertNotIn(password, page)
            self.assertNotIn(password, checks)
        self.assertNotIn('fixture-password', page)
        self.assertNotIn('fixture-password', checks)
        self.assertEqual(connection['jumps'][0]['host'], '127.0.0.1')

    def test_a_session_that_ends_closes_every_hop(self):
        connection = self.connect_through_chain()
        device = device_access.SSHDevice(connection, self.secret(connection))

        device.close()

        for gate in self.gates + [self.target]:
            self.assertTrue(self.sessions_closed(gate))


class FailedHopTests(ChainCase):
    """Whatever does not let us through, the error says which level it was."""

    def test_a_hop_that_cannot_be_reached_while_reading_fingerprints(self):
        not_ssh = self.plain_listener()
        self.gates[0].forward_to.add(('127.0.0.1', not_ssh.port))

        def second(port):
            def request(logins=0, **step):
                jumps = self.jumps(logins)
                jumps[1]['port'] = port
                return self.route(jumps=jumps, **step)
            first = self.engine.scan(request())
            return self.engine.scan(request(logins=1, jump_check_id=first['id'], trust_jump_fingerprint=first['fingerprint']))

        self.refused_at('jump', 2, 'no_ssh', ['第 2 级跳板机'], lambda: second(not_ssh.port))
        self.refused_at('jump', 2, 'unreachable', ['第 1 级跳板机连不上第 2 级跳板机'], lambda: second(self.closed_port()))
        self.assertTrue(self.sessions_closed(self.gates[0]))

    def test_the_first_hop_down_is_level_one(self):
        down = self.jumps()
        down[0]['port'] = self.closed_port()

        self.refused_at('jump', 1, 'unreachable', ['第 1 级跳板机'], lambda: self.engine.scan({**self.route(), 'jumps': down}))

    def test_each_level_that_stops_a_saved_connection_is_named(self):
        connection = self.connect_through_chain()
        secret = self.secret(connection)

        def with_jump(level, **changes):
            jumps = [dict(hop) for hop in connection['jumps']]
            jumps[level - 1].update(changes)
            return {**connection, 'jumps': jumps}

        def with_password(level, password):
            jumps = [dict(hop) for hop in secret['jumps']]
            jumps[level - 1]['password'] = password
            return {**secret, 'jumps': jumps}

        self.refused_at('jump', 3, 'auth', ['第 3 级跳板机拒绝登录'],
                        lambda: self.open(connection, with_password(3, secrets.token_hex(6))))
        self.refused_at('jump', 2, 'fingerprint', ['第 2 级跳板机的主机指纹'],
                        lambda: self.open(with_jump(2, fingerprint='SHA256:changed')))
        self.refused_at('jump', 2, 'key', ['第 2 级跳板机'], lambda: self.open(with_jump(2, auth='key')))

        self.gates[1].forwarding = False
        self.refused_at('jump', 3, 'forbidden', ['第 2 级跳板机不允许转发'], lambda: self.open(connection))
        self.gates[1].forwarding = True

        self.gates[2].forward_to.clear()                               # gate 3 can no longer reach the robot
        self.refused_at('device', None, 'unreachable', ['第 3 级跳板机连不上目标机'], lambda: self.open(connection))

        self.gates[0].forward_to.clear()                               # gate 1 can no longer reach gate 2
        self.refused_at('jump', 2, 'unreachable', ['第 1 级跳板机连不上第 2 级跳板机'], lambda: self.open(connection))
        for gate in self.gates:
            self.assertTrue(self.sessions_closed(gate), 'a failed login leaves no hop connected')

    def test_a_slow_hop_in_the_middle_is_a_timeout_of_that_level(self):
        connection = self.connect_through_chain()
        with mock.patch.multiple(device_access, HANDSHAKE_TIMEOUT=1, AUTH_TIMEOUT=1):
            self.gates[1].auth_delay = 2

            self.refused_at('jump', 2, 'timeout', ['第 2 级跳板机', '超时'], lambda: self.open(connection))

    def test_a_route_that_crawls_is_given_up_at_the_next_hop_instead_of_adding_up_every_limit(self):
        connection = self.connect_through_chain()
        contacted = len(self.gates[1].transports)
        self.gates[0].auth_delay = 1                                   # the first hop alone uses up the time for the route
        with mock.patch.object(device_access, 'ROUTE_TIMEOUT', 0.5):
            self.refused_at('jump', 2, 'timeout', ['第 2 级跳板机', '超时'], lambda: self.open(connection))

        self.assertEqual(len(self.gates[1].transports), contacted)     # the second hop was not tried at all
        self.assertTrue(self.sessions_closed(self.gates[0]))

    def test_closing_a_session_closes_the_hops_even_when_the_device_session_fails_to_close(self):
        connection = self.connect_through_chain()
        device = device_access.SSHDevice(connection, self.secret(connection))
        client = device.client
        self.addCleanup(client.close)

        with mock.patch.object(client, 'close', side_effect=OSError('already gone')):
            with self.assertRaises(OSError):
                device.close()

        for gate in self.gates:
            self.assertTrue(self.sessions_closed(gate))

    def test_a_wrong_robot_login_behind_three_hops_is_the_robots_refusal(self):
        connection = self.connect_through_chain()

        self.refused_at('device', None, 'auth', ['设备拒绝登录'],
                        lambda: self.open(connection, {**self.secret(connection), 'password': secrets.token_hex(6)}))

    def test_the_status_page_names_the_level_that_is_down(self):
        connection = self.connect_through_chain(watch_services=base.SERVICES)
        self.gates[1].forward_to.clear()

        view = self.engine.status({'project_id': self.pid, 'robot_id': connection['robot_id']})

        self.assertFalse(view['reachable'])
        self.assertEqual((view['error']['hop'], view['error']['level'], view['error']['reason']), ('jump', 3, 'unreachable'))
        self.assertIn('第 3 级跳板机', view['error']['message'])


class SameRouteEverywhereTests(ChainCase):
    """The status page, the hardware scan and an investigation all go through the saved jump hosts."""

    def forwarded(self):
        return [len(gate.forwarded) for gate in self.gates]

    def test_the_status_page_shows_the_whole_route_in_order(self):
        connection = self.connect_through_chain(watch_services=base.SERVICES)

        view = self.engine.status({'project_id': self.pid, 'robot_id': connection['robot_id']})

        self.assertTrue(view['reachable'], view)
        self.assertTrue(view['via_jump'])
        self.assertEqual(view['jump'], ' → '.join(f'{user}@127.0.0.1:{gate.port}'
                                                  for gate, (user, _) in zip(self.gates, self.logins)))
        self.assertEqual([gate.commands for gate in self.gates], [[], [], []])

    def test_a_hardware_scan_runs_on_the_robot_through_every_hop(self):
        connection = self.connect_through_chain()
        before = self.forwarded()

        scan = self.platform.hardware.scan({'project_id': self.pid, 'robot_id': connection['robot_id'], 'source': 'connect'})

        self.assertIn('serial-perm:/dev/ttyUSB0', {issue['id'] for issue in scan['issues']})
        self.assertIn(SCAN, self.target.commands)
        self.assertEqual([count - earlier for count, earlier in zip(self.forwarded(), before)], [1, 1, 1])
        detail = json.dumps(self.platform.hardware.scan_detail({'project_id': self.pid, 'scan_id': scan['id']}))
        for _, password in self.logins:
            self.assertNotIn(password, detail)

    def test_a_repair_session_that_lost_its_connection_logs_in_again_through_every_hop(self):
        # hardware_init.ensure_connected: a network fix may drop the session; it is reopened in place
        connection = self.connect_through_chain()
        device = self.open(connection)
        for gate in self.gates:
            for transport in list(gate.transports):
                transport.close()
        # The drop reaches this computer's end of the tunnel a moment later: wait until it is seen, as it is
        # after a network fix (checked at once, the session sometimes still looked alive and was not reopened).
        deadline = time.monotonic() + 10
        while device.client.get_transport().is_active() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(device.client.get_transport().is_active(), 'the dropped connection was never noticed')
        before = self.forwarded()

        hardware_init.ensure_connected(device, connection, self.secret(connection))

        self.assertEqual(device.probe('system')['exit_code'], 0)
        self.assertEqual([count - earlier for count, earlier in zip(self.forwarded(), before)], [1, 1, 1])
        self.assertEqual(len(device.jump.clients), LEVELS)

    def test_the_login_of_a_hop_is_hidden_in_what_the_robot_prints(self):
        connection = self.connect_through_chain()
        leak = self.logins[1][1]
        self.target.command_outputs['env'] = f'GATE={leak}\n'.encode()

        result = self.open(connection).execute('env')

        self.assertNotIn(leak, result['stdout'])
        self.assertIn('[已隐藏连接凭据]', result['stdout'])

    def test_the_hardware_scan_of_an_investigation_takes_the_same_route(self):
        connection = self.connect_through_chain()
        before = self.forwarded()
        session = {'id': 'agent-session-chain', 'project_id': self.pid, 'robot_id': connection['robot_id'],
                   'connection_id': connection['id'], 'mode': 'discovery', 'status': 'running', 'events': [],
                   'name': 'fixture', 'created_at': '2026-11-19T10:00:00', 'updated_at': '2026-11-19T10:00:00',
                   'investigation': {'full_flow': True}}
        self.store.put('agent_sessions', session)

        self.platform.investigation.flow_scan(session['id'], {'stop': threading.Event()})

        result = self.store.get('agent_sessions', session['id'])['investigation']
        self.assertIsNone(result.get('scan_error'))
        self.assertTrue(result['scan_id'])
        self.assertEqual([count - earlier for count, earlier in zip(self.forwarded(), before)], [1, 1, 1])


class RequestShapeTests(unittest.TestCase):
    """A request written the way of before issue #15 (one ``jump``, its login in ``jump_*``) is a list of one."""

    OLD = {'project_id': 'project-1', 'host': '192.0.2.10', 'port': 22, 'username': 'robot',
           'jump': {'host': '203.0.113.10', 'port': 2222, 'username': 'ops'}, 'jump_auth': 'password',
           'jump_password': 'typed', 'jump_check_id': 'ssh-key-1', 'trust_jump_fingerprint': 'SHA256:first'}

    def test_the_old_form_becomes_a_list_of_one_and_keeps_everything_else(self):
        request = ssh_route.canonical(self.OLD)

        self.assertEqual(request['jumps'], [{'host': '203.0.113.10', 'port': 2222, 'username': 'ops',
                                             'auth': 'password', 'password': 'typed'}])
        self.assertEqual({key: request[key] for key in ('host', 'jump_check_id', 'trust_jump_fingerprint')},
                         {'host': '192.0.2.10', 'jump_check_id': 'ssh-key-1', 'trust_jump_fingerprint': 'SHA256:first'})
        self.assertFalse({'jump', 'jump_auth', 'jump_password'} & set(request))
        self.assertIn('jump', self.OLD)                                 # the request it was given is not changed

    def test_a_list_wins_over_the_old_fields_and_a_direct_request_stays_as_it_is(self):
        listed = {**self.OLD, 'jumps': [{'host': '10.0.0.5', 'port': 22, 'username': 'relay'}]}
        direct = {'project_id': 'project-1', 'host': '192.0.2.10', 'port': 22, 'username': 'robot', 'jump': None,
                  'jump_auth': 'password'}

        self.assertIs(ssh_route.canonical(listed), listed)
        self.assertEqual(ssh_route.request_route(listed)['jumps'], [{'host': '10.0.0.5', 'port': 22, 'username': 'relay'}])
        self.assertIs(ssh_route.canonical(direct), direct)
        self.assertEqual(ssh_route.request_route(direct)['jumps'], [])

    def test_a_route_needs_a_login_for_every_hop_it_logs_in_to(self):
        hop = {'host': '127.0.0.1', 'port': 1, 'username': 'ops', 'auth': 'password', 'fingerprint': 'SHA256:x'}

        with self.assertRaises(ValueError):
            device_access.Route([hop, hop], [{'password': 'typed'}])


class MigrationTests(ChainCase):
    """A connection saved before this change has one jump host in a field of its own and its login in
    ``jump_*`` fields. Opening the workspace turns both into a list of one."""

    def legacy(self):
        """Save a one-hop connection the way 0.19.0 stored it. Returns (connection id, the hop, its password)."""
        connection = self.connect_through_jump()
        saved, secret = self.store.get('device_connections', connection['id']), self.secret(connection)
        hop, login = saved.pop('jumps')[0], secret.pop('jumps')[0]
        self.store.put('device_connections', {**saved, 'jump': hop})
        self.store.put('agent_secrets', {**secret, **{'jump_' + key: value for key, value in login.items()}})
        return connection['id'], hop, login['password']

    def reopened(self):
        self.platform.close()
        self.platform = Platform(self.tmp.name)
        self.addCleanup(self.platform.close)
        return self.platform

    def test_a_saved_one_hop_connection_becomes_a_list_of_one_and_still_connects(self):
        cid, hop, password = self.legacy()
        direct = self.connect_directly()

        platform = self.reopened()

        saved = platform.store.get('device_connections', cid)
        secret = platform.store.get('agent_secrets', cid)
        self.assertNotIn('jump', saved)
        self.assertEqual(saved['jumps'], [hop])
        self.assertEqual(secret['jumps'], [{'password': password, 'private_key': '', 'passphrase': ''}])
        self.assertFalse([key for key in secret if key.startswith('jump_')])
        self.assertEqual(platform.store.get('device_connections', direct['id']), direct)      # a direct one is untouched
        view = platform.engineering.status({'project_id': self.pid, 'robot_id': saved['robot_id']})
        self.assertTrue(view['reachable'], view)
        self.assertTrue(view['via_jump'])

    def test_migrating_twice_changes_nothing_more(self):
        cid, _, _ = self.legacy()
        platform = self.reopened()
        once = (platform.store.get('device_connections', cid), platform.store.get('agent_secrets', cid))

        ssh_route.migrate(platform.store)

        self.assertEqual((platform.store.get('device_connections', cid), platform.store.get('agent_secrets', cid)), once)

    def test_a_record_that_was_not_migrated_yet_still_connects(self):
        # e.g. written back by an older copy of the app that shares the data directory
        cid, _, _ = self.legacy()

        device = self.open(self.store.get('device_connections', cid), self.store.get('agent_secrets', cid))

        self.assertEqual(device.probe('system')['exit_code'], 0)
        self.assertEqual(self.jump.forwarded[-1], ('127.0.0.1', self.target.port))
