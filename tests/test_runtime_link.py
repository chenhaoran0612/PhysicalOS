"""Issue #17 (R1-09): the console reaches a robot's management layer over the robot's own SSH connection.

Before, a person opened ``ssh -L`` by hand and typed the tunnel's address and the token into the
console. Here the robot is a local SSH protocol fixture (directly, behind one jump host, behind three)
with a stand-in of the management layer on "its" loopback address; nothing reaches a real machine.

What happens when the connection drops: tests/test_runtime_link_reconnect.py.
"""
import json
import secrets
import socket
import unittest
from unittest import mock

import paramiko

import test_ssh_multi_hop as multi
from brainos import connector, runtime_link
from brainos.errors import CodedError
from heartbeat_support import HeartbeatCase, until
from runtime_link_support import LOOPBACK, PORT, LinkMixin


class DirectTests(LinkMixin, HeartbeatCase):
    def test_a_robot_on_the_lan_is_connected_without_a_tunnel_an_address_or_a_typed_token(self):
        layer = self.install_layer()
        connection = self.connect_directly()

        robot = self.attach(connection)

        self.assertEqual(robot['status'], 'connected')
        self.assertEqual(robot['capabilities'], layer.manifest_v1()['capabilities'])
        self.assertNotIn('token', robot)
        saved = self.robot(connection)
        self.assertEqual(saved['endpoint'], runtime_link.endpoint_of(saved['id']))
        self.assertEqual(saved['token'], layer.token)                 # read from the robot, not typed
        self.assertEqual(saved['runtime_link']['port'], PORT)
        self.assertEqual(layer.calls, ['GET /v1/manifest', 'GET /v1/status'])
        self.assertEqual(self.target.loopback_channels, [(LOOPBACK, PORT)] * 2)
        self.assertEqual(self.target.commands.count(runtime_link.TOKEN_COMMAND), 1)

    def test_every_later_request_of_the_console_takes_the_same_way(self):
        layer = self.install_layer()
        connection = self.connect_directly()
        self.attach(connection)

        status = self.call(connection)
        echoed = self.call(connection, '/v1/echo', {'text': 'x' * 100_000})
        checked = self.platform.connect_robot({'robot_id': connection['robot_id']})
        detail = self.platform.robot_detail({'robot_id': connection['robot_id'], 'project_id': self.pid})

        self.assertEqual(status['instance_id'], layer.instance)
        self.assertGreater(echoed['bytes'], 100_000)
        self.assertEqual(checked['status'], 'connected')
        self.assertEqual(detail['robot']['status'], 'connected')
        self.assertTrue(all(token == 'Bearer ' + layer.token for token in layer.tokens))
        self.assertEqual(len(self.target.loopback_channels), len(layer.calls))
        self.assertEqual(self.target.logins(), 2)     # the login test of "connect", and one session for the link

    def test_nothing_listens_on_this_computer_for_it(self):
        self.install_layer()
        connection = self.connect_directly()
        listening = []
        with mock.patch.object(socket.socket, 'listen', lambda sock, *args: listening.append(sock)):
            self.attach(connection)
            self.call(connection)

        self.assertEqual(listening, [], 'a port was opened on this computer for the robot')

    def test_an_error_of_the_management_layer_arrives_as_what_it_is(self):
        self.install_layer()
        connection = self.connect_directly()
        self.attach(connection)

        error = self.refused(lambda: self.call(connection, '/v1/unknown'))

        self.assertEqual((error.status, error.code, error.sent), (404, 'not_found', True))

    def test_the_token_can_be_typed_and_is_then_not_read_from_the_robot(self):
        layer = self.install_layer(token_file=False)
        connection = self.connect_directly()

        robot = self.attach(connection, token=layer.token)

        self.assertEqual(robot['status'], 'connected')
        self.assertNotIn(runtime_link.TOKEN_COMMAND, self.target.commands)
        self.assertEqual(self.robot(connection)['runtime_link']['token_source'], 'typed')

    def test_another_port_of_the_robot_can_be_named(self):
        layer = self.install_layer(port=18790)
        connection = self.connect_directly()

        self.attach(connection, port=18790)

        self.assertEqual(self.call(connection)['instance_id'], layer.instance)
        self.assertEqual(set(self.target.loopback_channels), {(LOOPBACK, 18790)})

    def test_two_robots_use_the_same_port_number_without_getting_in_each_others_way(self):
        first_layer = self.install_layer()
        first = self.connect_directly()
        machine, second = self.another_robot()
        second_layer = self.install_layer(machine)

        self.attach(first)
        self.attach(second)

        self.assertEqual(self.robot(first)['runtime_link']['port'], self.robot(second)['runtime_link']['port'])
        self.assertEqual(self.call(first)['instance_id'], first_layer.instance)
        self.assertEqual(self.call(second)['instance_id'], second_layer.instance)

    def test_the_state_of_the_link_is_asked_for_apart_and_never_part_of_the_workspace_state(self):
        self.install_layer()
        connection = self.connect_directly()
        self.attach(connection)
        before = json.dumps(self.platform.snapshot(), sort_keys=True)

        self.target.drop_sessions()                  # the session dies and is opened again
        self.call(connection)
        view = self.view(connection)

        self.assertEqual(json.dumps(self.platform.snapshot(), sort_keys=True), before)
        self.assertEqual((view['state'], view['port'], view['error']), ('up', PORT, None))
        shown = next(r for r in self.platform.snapshot()['robots'] if r['id'] == connection['robot_id'])
        self.assertTrue(shown['has_token'])
        self.assertNotIn('token', shown)
        self.assertNotIn(self.robot(connection)['token'], before)

    def test_taking_the_link_away_leaves_the_ssh_connection_and_forgets_the_token(self):
        self.install_layer()
        connection = self.connect_directly()
        self.attach(connection)

        robot = self.links.detach({'project_id': self.pid, 'robot_id': connection['robot_id']})

        saved = self.robot(connection)
        self.assertEqual((robot['endpoint'], robot['status']), ('', 'unconnected'))
        self.assertEqual((saved['token'], saved.get('runtime_link'), saved['connection_id']), ('', None, connection['id']))
        self.assertNotIn(connection['robot_id'], self.links.overview({})['links'])
        self.assertTrue(until(lambda: self.target.open_sessions() == 0), 'the session of the link stayed open')


class OneJumpTests(LinkMixin, HeartbeatCase):
    def test_a_robot_behind_a_jump_host_is_reached_through_it(self):
        layer = self.install_layer()
        connection = self.connect_through_jump()

        robot = self.attach(connection)

        self.assertEqual(robot['status'], 'connected')
        self.assertEqual(self.call(connection)['instance_id'], layer.instance)
        self.assertEqual(set(self.target.loopback_channels), {(LOOPBACK, PORT)})
        self.assertEqual(set(self.jump.forwarded), {('127.0.0.1', self.target.port)})     # the jump host only carries SSH
        self.assertEqual(self.jump.loopback_channels, [])


class ThreeJumpTests(LinkMixin, multi.ChainCase):
    def test_a_robot_behind_three_jump_hosts_is_reached_through_all_of_them(self):
        layer = self.install_layer()
        connection = self.connect_through_chain()

        robot = self.attach(connection)

        self.assertEqual(robot['status'], 'connected')
        self.assertEqual(self.call(connection)['instance_id'], layer.instance)
        self.assertEqual(self.robot(connection)['token'], layer.token)
        self.assertEqual(set(self.target.loopback_channels), {(LOOPBACK, PORT)})
        after = self.gates[1:] + [self.target]
        for gate, following in zip(self.gates, after):          # every hop only reaches the next one, over SSH
            self.assertEqual(set(gate.forwarded), {('127.0.0.1', following.port)})
            self.assertEqual(gate.loopback_channels, [])
        self.assertEqual(layer.peers and {peer[0] for peer in layer.peers}, {'127.0.0.1'})


class OnlyTheRobotsOwnLoopbackTests(LinkMixin, HeartbeatCase):
    """The link is not a general port forward: it goes to one port on the robot's own loopback address."""

    def linked(self):
        self.install_layer()
        connection = self.connect_directly()
        self.attach(connection)
        return connection

    def test_every_channel_the_console_asks_for_ends_on_the_robots_loopback_port(self):
        asked, real = [], paramiko.Transport.open_channel

        def recording(transport, kind, dest_addr=None, src_addr=None, *args, **kwargs):
            if kind != 'session':
                asked.append((kind, tuple(dest_addr)))
            return real(transport, kind, dest_addr, src_addr, *args, **kwargs)

        with mock.patch.object(paramiko.Transport, 'open_channel', recording):
            connection = self.linked()
            self.call(connection)
            self.call(connection, '/v1/echo', {'n': 1})

        self.assertEqual(set(asked), {('direct-tcpip', (LOOPBACK, PORT))})

    def test_an_address_cannot_be_given(self):
        self.install_layer()
        connection = self.connect_directly()

        for field, value in (('host', '10.0.0.5'), ('endpoint', 'http://10.0.0.5:8790'), ('address', '10.0.0.5:22'),
                             ('target', 'gateway.example')):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.attach(connection, **{field: value})

        self.assertEqual(self.robot(connection)['endpoint'], '')
        self.assertEqual(self.target.loopback_channels, [])

    def test_only_an_unprivileged_port_number_is_accepted(self):
        self.install_layer()
        connection = self.connect_directly()

        for port in (0, -1, 22, 80, 443, 1023, 65536, '8790', 8790.0, True, [8790]):
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.attach(connection, port=port)

        self.assertEqual(self.robot(connection)['endpoint'], '')
        self.assertEqual(self.target.loopback_channels, [])

    def test_an_endpoint_of_this_kind_cannot_be_typed_into_a_form(self):
        connection = self.linked()
        endpoint = self.robot(connection)['endpoint']
        plain = self.platform.add_robot({'project_id': self.pid, 'name': 'Plain', 'family': 'quadruped'})

        with self.assertRaises(ValueError):
            self.platform.add_robot({'project_id': self.pid, 'name': 'Borrower', 'family': 'quadruped',
                                     'endpoint': endpoint})
        with self.assertRaises(ValueError):
            self.platform.configure_connection({'kind': 'robots', 'id': plain['id'], 'endpoint': endpoint})
        with self.assertRaises(ValueError):
            self.platform.add_service({'project_id': self.pid, 'name': 'S', 'kind': 'inference', 'endpoint': endpoint})

        self.assertEqual(self.store.get('robots', plain['id'])['endpoint'], '')

    def test_a_malformed_endpoint_reaches_nothing(self):
        connection = self.linked()
        channels = len(self.target.loopback_channels)
        prefix = runtime_link.SCHEME + ':'

        for endpoint in (prefix, prefix + '//evil.example:22', prefix + connection['robot_id'] + '/../x',
                         prefix + connection['robot_id'] + '@evil.example', prefix + 'robot-unknown',
                         prefix + 'a' * 500, prefix + connection['robot_id'] + ' ', prefix + '/' + connection['robot_id']):
            with self.subTest(endpoint=endpoint):
                error = self.refused(lambda: connector.call(endpoint, '/v1/status', 'x'))
                self.assertEqual((error.code, error.sent), ('device_link_not_configured', False))

        self.assertEqual(len(self.target.loopback_channels), channels)

    def test_a_record_someone_tampered_with_reaches_nothing_else(self):
        connection = self.linked()
        channels = len(self.target.loopback_channels)
        endpoint, token = self.robot(connection)['endpoint'], self.robot(connection)['token']

        def tampered(**link):
            robot = self.robot(connection)
            robot['runtime_link'] = {**robot['runtime_link'], **link}
            self.store.put('robots', robot)

        tampered(host='10.0.0.5', address='10.0.0.5:22')          # fields the link does not have are not read
        self.assertIn('instance_id', connector.call(endpoint, '/v1/status', token))
        self.assertEqual(set(self.target.loopback_channels), {(LOOPBACK, PORT)})
        channels = len(self.target.loopback_channels)

        for port in (22, 0, '8790', None):
            tampered(port=port)
            with self.subTest(port=port):
                error = self.refused(lambda: connector.call(endpoint, '/v1/status', token))
                self.assertEqual((error.code, error.sent), ('device_link_not_configured', False))
        self.assertEqual(len(self.target.loopback_channels), channels)

    def test_a_robot_whose_address_was_typed_by_hand_is_not_reached_over_ssh(self):
        connection = self.linked()
        endpoint, token = self.robot(connection)['endpoint'], self.robot(connection)['token']
        robot = self.robot(connection)
        robot['endpoint'] = 'http://127.0.0.1:9'                   # as if someone had typed an address instead
        self.store.put('robots', robot)
        channels = len(self.target.loopback_channels)

        error = self.refused(lambda: connector.call(endpoint, '/v1/status', token))

        self.assertEqual(error.code, 'device_link_not_configured')
        self.assertEqual(len(self.target.loopback_channels), channels)


class RefusalTests(LinkMixin, HeartbeatCase):
    def test_a_robot_without_a_management_layer_says_so(self):
        layer = self.install_layer()
        self.target.stop_serving(PORT)                          # installed once, not running now
        connection = self.connect_directly()

        error = self.refused(lambda: self.attach(connection))

        self.assertEqual((error.code, error.sent), ('device_service_down', False))
        self.assertIn(str(PORT), str(error))
        saved = self.robot(connection)
        self.assertEqual((saved['status'], saved['endpoint']), ('error', runtime_link.endpoint_of(saved['id'])))
        self.assertEqual(layer.calls, [])
        self.target.serve_on_loopback(PORT, layer.port)         # started on the robot: the same check now passes
        self.assertEqual(self.platform.connect_robot({'robot_id': saved['id']})['status'], 'connected')

    def test_a_robot_whose_ssh_service_does_not_forward_says_so(self):
        self.install_layer()
        connection = self.connect_directly()
        self.target.forwarding = False

        error = self.refused(lambda: self.attach(connection))

        self.assertEqual((error.code, error.sent), ('device_forward_forbidden', False))
        self.assertIn('AllowTcpForwarding', str(error))

    def test_a_wrong_token_is_refused_by_the_management_layer_itself(self):
        self.install_layer()
        connection = self.connect_directly()

        wrong = 'wrong-' + secrets.token_hex(12)                     # made up on every run
        error = self.refused(lambda: self.attach(connection, token=wrong))

        self.assertEqual((error.status, error.sent), (401, True))
        self.assertEqual(self.robot(connection)['status'], 'error')

    def test_no_token_file_on_the_robot_asks_for_the_token_and_saves_nothing(self):
        self.install_layer(token_file=False)
        self.target.command_outputs[runtime_link.TOKEN_COMMAND] = b''
        connection = self.connect_directly()

        with self.assertRaises(CodedError) as caught:
            self.attach(connection)

        self.assertEqual(caught.exception.code, 'runtime_token_not_found')
        saved = self.robot(connection)
        self.assertEqual((saved['endpoint'], saved['token'], saved.get('runtime_link')), ('', '', None))
        self.assertEqual(self.target.loopback_channels, [])

    def test_what_the_robot_printed_instead_of_a_token_is_not_used_and_not_shown(self):
        self.install_layer(token_file=False)
        connection = self.connect_directly()

        for printed in (b'cat: /home/robot/neuxmind/edge-token: Permission denied\n', b'short\n',
                        b'two lines of sixteen characters each\nand a second line of the same kind\n',
                        b'a-token-with a space in the middle of it\n', 'a-token-with-an-accent-é-in-the-middle\n'.encode(),
                        b'x' * 5000):
            self.target.command_outputs[runtime_link.TOKEN_COMMAND] = printed
            with self.subTest(printed=printed[:30]), self.assertRaises(CodedError) as caught:
                self.attach(connection)
            self.assertEqual(caught.exception.code, 'runtime_token_not_found')
            self.assertNotIn(printed[:12].decode('utf-8', 'replace'), str(caught.exception))

        self.assertEqual(self.robot(connection)['token'], '')

    def test_a_token_file_that_is_read_too_slowly_is_a_slow_link_not_a_missing_token(self):
        self.install_layer()
        self.target.unanswered.add(runtime_link.TOKEN_COMMAND)        # the command gets no answer in time
        connection = self.connect_directly()

        with mock.patch.object(runtime_link, 'TOKEN_READ_SECONDS', 1):
            error = self.refused(lambda: self.attach(connection))

        self.assertEqual((error.code, error.sent), ('device_link_slow', False))
        self.assertEqual(self.robot(connection)['endpoint'], '')
        self.assertEqual(self.target.open_sessions(), 1, 'a slow link was taken for a dead one')

    def test_a_typed_token_must_look_like_one(self):
        self.install_layer()
        connection = self.connect_directly()

        for token in ('two\nlines-of-a-token-000000000000', 12345, 'x' * 3000, ['token']):
            with self.subTest(token=str(token)[:20]), self.assertRaises(ValueError):
                self.attach(connection, token=token)

        self.assertEqual(self.robot(connection)['endpoint'], '')

    def test_only_a_robot_with_an_ssh_connection_of_its_own_can_be_linked(self):
        plain = self.platform.add_robot({'project_id': self.pid, 'name': 'Plain', 'family': 'quadruped'})
        other = self.platform.add_project({'name': 'Other', 'objective': 'Another project'})['id']
        self.install_layer()
        connection = self.connect_directly()
        _, simulated = self.another_robot()
        marked = self.robot(simulated)               # the app runs simulated robots itself, on this computer
        self.store.put('robots', {**marked, 'sim': {'kind': 'quadruped', 'enabled': False}})

        for request in ({'project_id': self.pid, 'robot_id': plain['id']},
                        {'project_id': self.pid, 'robot_id': simulated['robot_id']},
                        {'project_id': other, 'robot_id': connection['robot_id']},
                        {'project_id': self.pid, 'robot_id': 'robot-unknown'}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.links.attach(request)

        self.assertEqual(self.target.loopback_channels, [])

    def test_the_record_of_the_change_names_no_token(self):
        layer = self.install_layer()
        connection = self.connect_directly()

        self.attach(connection)
        self.links.detach({'project_id': self.pid, 'robot_id': connection['robot_id']})

        events = [e for e in self.store.list('events') if e['action'].startswith('robot.runtime_link')]
        self.assertEqual(sorted(e['action'] for e in events), ['robot.runtime_link_attached', 'robot.runtime_link_removed'])
        self.assertNotIn(layer.token, json.dumps(events))


if __name__ == '__main__':
    unittest.main()
