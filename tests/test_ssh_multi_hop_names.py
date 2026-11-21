"""Issue #15: names that a jump host resolves, with several jump hosts in a row.

``ssh gate1 'ssh mid ssh dog'`` names "mid" on gate1 and "dog" on mid; a name whose ssh config
says ``ProxyJump`` brings its own jump hosts. Each machine a name leads to is shown with its
fingerprint and confirmed like one that was typed. Local protocol fixtures only.
"""
from unittest import mock

import test_ssh_multi_hop as chain
from brainos import device_access, ssh_route

resolve = device_access.resolve_command


def config(fixture, user, extra=''):
    """What ``ssh -G name`` prints on a jump host for a name that stands for ``fixture``."""
    return f'user {user}\nhostname 127.0.0.1\nport {fixture.port}\naddressfamily any\n{extra}'.encode()


def named(name, on, **extra):
    return {'host': name, 'port': None, 'username': '', 'resolve_on': on, **extra}


class NameCase(chain.ChainCase):
    def password_of(self, user):
        return dict(self.logins)[user]

    def walk(self, request):
        """Confirm every fingerprint, sending back the route each answer gives (names resolved, hops added)
        with the logins of the hops confirmed so far. Returns every check."""
        checks = [self.engine.scan(request)]
        while checks[-1].get('stage') == 'jump':
            last = checks[-1]
            jumps = [dict(hop) for hop in last['route']['jumps']]
            for hop in jumps[:last['level']]:
                hop.update(auth='password', password=self.password_of(hop['username']))
            target = last['route']['target']
            request = {'project_id': self.pid, 'jumps': jumps, 'host': target['host'], 'port': target['port'],
                       'username': target['username'], 'jump_check_id': last['id'],
                       'trust_jump_fingerprint': last['fingerprint']}
            if 'resolve_on' in target:
                request.update(resolve_on=target['resolve_on'], resolve_plain=bool(target.get('plain')))
            checks.append(self.engine.scan(request))
        return checks

    def connect(self, checks):
        last = checks[-1]
        jumps = [{**hop, 'password': self.password_of(hop['username'])} for hop in last['jumps']]
        return self.engine.connect({'project_id': self.pid, 'jumps': jumps, 'host_check_id': last['id'],
                                    'trust_fingerprint': last['fingerprint'], 'auth': 'password',
                                    'password': 'fixture-password', 'name': 'Named robot', 'family': 'quadruped',
                                    'read_roots': ['/opt/ros']})

    def lookups(self):
        return [gate.commands for gate in self.gates]


class NamedHopTests(NameCase):
    def test_the_robot_named_on_the_last_of_three_jump_hosts(self):
        self.gates[2].command_outputs[resolve('dog')] = config(self.target, 'robot')

        checks = self.walk({**self.route(), 'host': 'dog', 'port': None, 'username': '', 'resolve_on_jump': True})

        last = checks[-1]
        self.assertEqual(len(checks), chain.LEVELS + 1)
        self.assertEqual((last['alias'], last['host'], last['port'], last['username']),
                         ('dog', '127.0.0.1', self.target.port, 'robot'))
        self.assertEqual(self.lookups(), [[], [], [resolve('dog')]])    # asked on the last hop, nowhere else

    def test_nothing_is_looked_up_on_a_hop_before_its_fingerprint_is_confirmed(self):
        self.gates[0].command_outputs[resolve('mid')] = config(self.gates[1], 'gate2')
        jumps = [self.jumps()[0], named('mid', 1), self.jumps()[2]]

        first = self.engine.scan({**self.route(), 'jumps': jumps})

        self.assertEqual((first['stage'], first['level']), ('jump', 1))
        self.assertEqual(self.lookups(), [[], [], []])
        self.assertEqual(first['route']['jumps'][1], named('mid', 1))

    def test_a_jump_host_named_on_the_one_before_it(self):
        # ssh gate1 'ssh mid ssh dog': gate1 knows "mid", and mid knows "dog"
        self.gates[0].command_outputs[resolve('mid')] = config(self.gates[1], 'gate2')
        self.gates[1].command_outputs[resolve('dog')] = config(self.target, 'robot')
        self.gates[1].forward_to.add(('127.0.0.1', self.target.port))

        checks = self.walk({'project_id': self.pid, 'jumps': [self.jumps()[0], named('mid', 1)],
                            'host': 'dog', 'port': None, 'username': '', 'resolve_on_jump': True})

        second, last = checks[1], checks[-1]
        self.assertEqual((second['level'], second['alias'], second['port'], second['username']),
                         (2, 'mid', self.gates[1].port, 'gate2'))
        self.assertEqual(second['fingerprint'], device_access.fingerprint(self.gates[1].key))
        self.assertEqual(last['alias'], 'dog')
        self.assertEqual(self.lookups(), [[resolve('mid')], [resolve('dog')], []])
        connection = self.connect(checks)
        self.assertEqual([hop.get('alias') for hop in connection['jumps']], [None, 'mid'])
        self.assertEqual(connection['alias'], 'dog')
        self.assertEqual(self.open(connection).probe('system')['exit_code'], 0)
        self.assertEqual(self.lookups(), [[resolve('mid')], [resolve('dog')], []])     # later connections ask nothing

    def test_a_name_resolved_on_an_earlier_hop_than_the_one_before_it(self):
        # ssh gate1 'ssh -J mid dog': gate1 resolves both names, and the robot is reached from mid
        self.gates[0].command_outputs.update({resolve('mid'): config(self.gates[1], 'gate2'),
                                              resolve('dog'): config(self.target, 'robot', 'proxyjump elsewhere\n')})
        self.gates[1].forward_to.add(('127.0.0.1', self.target.port))

        checks = self.walk({'project_id': self.pid, 'jumps': [self.jumps()[0], named('mid', 1)],
                            'host': 'dog', 'port': None, 'username': '', 'resolve_on': 1, 'resolve_plain': True})

        self.assertEqual(checks[-1]['alias'], 'dog')
        self.assertEqual(len(checks[-1]['jumps']), 2)                   # the -J on the command line wins over the config
        self.assertEqual(self.lookups(), [[resolve('mid'), resolve('dog')], [], []])

    def test_a_user_or_port_given_with_the_name_wins_over_the_config(self):
        self.gates[0].command_outputs[resolve('mid')] = b'user someone-else\nhostname 127.0.0.1\nport 1\n'
        jumps = [self.jumps()[0], named('mid', 1, port=self.gates[1].port, username='gate2'), self.jumps()[2]]

        checks = self.walk({**self.route(), 'jumps': jumps})

        self.assertEqual((checks[1]['port'], checks[1]['username']), (self.gates[1].port, 'gate2'))
        self.assertEqual(checks[-1]['fingerprint'], device_access.fingerprint(self.target.key))

    def test_a_name_must_be_resolved_by_a_hop_that_comes_before_it(self):
        first = self.jumps()[0]
        refused = {'the first hop has nobody to ask': [named('gate', 1)],
                   'a hop cannot resolve itself': [first, named('mid', 2)],
                   'nor can a later one': [first, named('mid', 3), self.jumps()[2]],
                   'level zero': [first, named('mid', 0)],
                   'not a number': [first, named('mid', 'first')],
                   'true is not a level': [first, named('mid', True)],
                   'shell text in a name': [first, named('mid;reboot', 1)]}
        for case, jumps in refused.items():
            with self.subTest(case):
                with self.assertRaises(ValueError):
                    self.engine.scan({**self.route(), 'jumps': jumps})
        with self.assertRaises(ValueError):                             # the robot's name: only three hops exist
            self.engine.scan({**self.route(), 'host': 'dog', 'resolve_on': 4})

        self.assertEqual(self.gates[0].transports, [])


class ProxyJumpTests(NameCase):
    """A name whose ssh config on the jump host says ProxyJump: those hosts join the route."""

    def setUp(self):
        super().setUp()
        self.gates[0].command_outputs.update({
            resolve('dog'): config(self.target, 'robot', 'proxyjump inner-a,gate3@inner-b\n'),
            resolve('inner-a'): config(self.gates[1], 'gate2'),
            resolve('inner-b'): config(self.gates[2], 'someone-else', 'proxyjump ignored-for-this-one\n')})

    def typed(self, **extra):
        """What the user typed: one jump host and a name."""
        return {'project_id': self.pid, 'jumps': self.jumps()[:1], 'host': 'dog', 'port': None, 'username': '',
                'resolve_on_jump': True, **extra}

    def test_the_hosts_of_a_proxyjump_are_added_and_each_is_confirmed_like_a_typed_one(self):
        checks = self.walk(self.typed())

        second, third, last = checks[1], checks[2], checks[3]
        self.assertEqual(len(checks), 4)
        self.assertEqual(second['expanded'], {'at': 2, 'count': 2, 'by': 1})
        self.assertEqual((second['stage'], second['level'], second['total'], second['alias']), ('jump', 2, 3, 'inner-a'))
        self.assertEqual(second['fingerprint'], device_access.fingerprint(self.gates[1].key))
        self.assertEqual((third['level'], third['alias'], third['username']), (3, 'inner-b', 'gate3'))
        self.assertNotIn('expanded', third)
        self.assertEqual((last['alias'], last['port']), ('dog', self.target.port))
        self.assertEqual([hop.get('alias') for hop in last['jumps']], [None, 'inner-a', 'inner-b'])
        # only the fixed look-up ran on the jump host, once per name
        self.assertEqual(self.lookups(), [[resolve('dog'), resolve('inner-a'), resolve('inner-b')], [], []])

    def test_a_robot_found_behind_a_proxyjump_is_connected_and_read(self):
        connection = self.connect(self.walk(self.typed()))

        self.assertEqual(len(connection['jumps']), 3)
        self.assertEqual(self.open(connection).probe('system')['exit_code'], 0)
        self.assertEqual(self.target.commands, [device_access.PROBES['system'][1]])

    def test_a_proxycommand_is_still_refused_and_says_so(self):
        self.gates[0].command_outputs[resolve('dog')] = config(self.target, 'robot', 'proxycommand ssh -W %h:%p inner-a\n')
        first = self.engine.scan(self.typed())

        with self.assertRaises(ValueError) as caught:
            self.engine.scan({**self.typed(), 'jumps': self.jumps(1)[:1], 'jump_check_id': first['id'],
                              'trust_jump_fingerprint': first['fingerprint']})

        self.assertIn('ProxyCommand', str(caught.exception))
        self.assertEqual(self.target.transports, [])

    def test_a_route_that_grows_past_the_limit_is_refused(self):
        first = self.engine.scan(self.typed())

        with mock.patch.object(ssh_route, 'MAX_JUMPS', 2):
            with self.assertRaises(ValueError) as caught:
                self.engine.scan({**self.typed(), 'jumps': self.jumps(1)[:1], 'jump_check_id': first['id'],
                                  'trust_jump_fingerprint': first['fingerprint']})

        self.assertIn('2', str(caught.exception))
        self.assertEqual(self.gates[1].transports, [])

    def test_a_proxyjump_that_points_at_itself_ends_at_the_limit_instead_of_looping(self):
        self.gates[0].command_outputs[resolve('inner-a')] = config(self.gates[1], 'gate2', 'proxyjump inner-a\n')
        first = self.engine.scan(self.typed())

        with self.assertRaises(ValueError):
            self.engine.scan({**self.typed(), 'jumps': self.jumps(1)[:1], 'jump_check_id': first['id'],
                              'trust_jump_fingerprint': first['fingerprint']})

        self.assertLessEqual(len(self.gates[0].commands), 2 * ssh_route.MAX_JUMPS + 2)
        self.assertEqual(self.gates[1].transports, [])

    def test_what_a_jump_host_says_about_further_hops_is_checked_like_typed_input(self):
        answers = {'an option instead of a host': 'proxyjump -oProxyCommand=reboot',
                   'shell text in the host': 'proxyjump inner-a;reboot',
                   'shell text in the user': 'proxyjump ops$(reboot)@inner-a',
                   'a port that is not a number': 'proxyjump inner-a:ssh',
                   'a port in digits ssh would not read': 'proxyjump inner-a:\u0662\u0662',
                   'an empty entry': 'proxyjump inner-a,,inner-b'}
        for case, answer in answers.items():
            with self.subTest(case):
                self.gates[0].command_outputs[resolve('dog')] = config(self.target, 'robot', answer + '\n')
                first = self.engine.scan(self.typed())
                with self.assertRaises(ValueError):
                    self.engine.scan({**self.typed(), 'jumps': self.jumps(1)[:1], 'jump_check_id': first['id'],
                                      'trust_jump_fingerprint': first['fingerprint']})

        self.assertEqual(self.gates[0].forwarded, [])                   # nothing was opened towards such an address
        self.assertEqual(self.gates[1].transports, [])

    def test_a_numeric_address_in_brackets_is_read_as_ssh_prints_it(self):
        self.gates[0].command_outputs[resolve('dog')] = config(
            self.target, 'robot', f'proxyjump gate2@[127.0.0.1]:{self.gates[1].port}\n')
        self.gates[0].command_outputs[resolve('127.0.0.1')] = b'user gate1\nhostname 127.0.0.1\nport 22\n'
        self.gates[1].forward_to.add(('127.0.0.1', self.target.port))

        checks = self.walk(self.typed())

        self.assertEqual((checks[1]['host'], checks[1]['port'], checks[1]['username']),
                         ('127.0.0.1', self.gates[1].port, 'gate2'))
        self.assertEqual(checks[-1]['fingerprint'], device_access.fingerprint(self.target.key))

    def test_a_route_that_differs_from_the_one_the_last_answer_gave_is_refused(self):
        checks = [self.engine.scan(self.typed())]
        checks.append(self.engine.scan({**self.typed(), 'jumps': self.jumps(1)[:1], 'jump_check_id': checks[0]['id'],
                                        'trust_jump_fingerprint': checks[0]['fingerprint']}))
        second = checks[1]

        with self.assertRaises(ValueError):                             # still the route as typed, without the found hops
            self.engine.scan({**self.typed(), 'jumps': self.jumps(1)[:1], 'jump_check_id': second['id'],
                              'trust_jump_fingerprint': second['fingerprint']})

        self.assertEqual(self.gates[1].auth_attempts, [])
