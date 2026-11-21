"""Issue #15: changing the SSH connection of a robot that sits behind several jump hosts.

The rules are those of issue #9, hop by hop: an empty credential field keeps that hop's saved login,
a saved login only goes to the machine it was saved for, and a hop whose address changed has every
fingerprint read and confirmed again. Local protocol fixtures only.
"""
import json
import secrets
import unittest

import test_ssh_multi_hop as chain
from brainos import device_access, ssh_edit

ROBOT_LOGIN = 'fixture-password'


class ChainEditCase(chain.ChainCase):
    def setUp(self):
        super().setUp()
        self.edit = self.platform.ssh_edit

    def form(self, connection, **changes):
        """What the edit form sends when no credential field was filled in."""
        return {'project_id': self.pid, 'edit_robot_id': connection['robot_id'], 'host': connection['host'],
                'port': connection['port'], 'username': connection['username'], 'auth': connection['auth'],
                'read_roots': connection['read_roots'], 'watch_services': connection['watch_services'],
                'jumps': [{key: hop[key] for key in ('host', 'port', 'username', 'auth')}
                          for hop in connection.get('jumps', [])], **changes}

    def with_hop(self, connection, level, **changes):
        jumps = self.form(connection)['jumps']
        jumps[level - 1].update(changes)
        return jumps

    def save(self, connection, **changes):
        """Save without reading fingerprints again: allowed while no hop's address changed."""
        return self.edit.update(self.form(connection, **changes))

    def rescan(self, connection, **changes):
        """Read every fingerprint again and confirm each. Returns the request and the robot's check."""
        form = self.form(connection, **changes)
        check = self.engine.scan(form)
        while check.get('stage') == 'jump':
            form = {**form, 'jump_check_id': check['id'], 'trust_jump_fingerprint': check['fingerprint']}
            check = self.engine.scan(form)
        return form, check

    def save_confirmed(self, connection, **changes):
        form, check = self.rescan(connection, **changes)
        return self.edit.update({**form, 'host_check_id': check['id'], 'trust_fingerprint': check['fingerprint']})

    def logins_tried(self):
        return [len(gate.auth_attempts) for gate in self.gates]

    def audit(self):
        return next(e for e in self.store.list('events') if e['action'] == 'device.ssh_updated')['details']


class ChainEditTests(ChainEditCase):
    def test_logins_left_empty_are_kept_on_every_hop(self):
        old = self.connect_through_chain()
        before, tried = self.secret(old), self.logins_tried()

        new = self.save(old, watch_services=['bridge'])

        self.assertEqual(self.secret(new), before)
        self.assertEqual(new['jumps'], old['jumps'])
        self.assertEqual((new['id'], new['robot_id']), (old['id'], old['robot_id']))
        # the kept logins were tried on every hop before anything was saved
        self.assertEqual(self.logins_tried(), [count + 1 for count in tried])
        self.assertEqual(self.audit()['changed'], ['watch_services'])

    def test_a_new_password_for_one_hop_leaves_the_others_as_they_were(self):
        old = self.connect_through_chain()
        before = self.secret(old)
        self.gates[1].password = secrets.token_hex(6)

        new = self.save(old, jumps=self.with_hop(old, 2, password=self.gates[1].password))

        after = self.secret(new)
        self.assertEqual(after['jumps'][1]['password'], self.gates[1].password)
        self.assertEqual((after['jumps'][0], after['jumps'][2], after['password']),
                         (before['jumps'][0], before['jumps'][2], before['password']))
        self.assertEqual(self.audit()['changed'], ['jump_credentials'])

    def test_the_name_a_hop_was_found_under_stays_with_it(self):
        old = self.connect_through_chain()
        named = [dict(hop) for hop in old['jumps']]
        named[1]['alias'] = 'mid'                                       # as when the first hop's ssh config resolved it
        self.store.put('device_connections', {**old, 'jumps': named})

        new = self.save(old, watch_services=['bridge'])

        self.assertEqual([hop.get('alias') for hop in new['jumps']], [None, 'mid', None])

    def test_a_wrong_login_names_its_level_and_changes_nothing(self):
        old = self.connect_through_chain()
        before = self.secret(old)

        self.refused_at('jump', 3, 'auth', ['第 3 级跳板机拒绝登录'],
                        lambda: self.save(old, jumps=self.with_hop(old, 3, password=secrets.token_hex(6))))

        self.assertEqual(self.store.get('device_connections', old['id']), old)
        self.assertEqual(self.secret(old), before)

    def test_another_login_method_on_a_hop_needs_that_hops_secret(self):
        old = self.connect_through_chain()

        with self.assertRaises(ValueError) as caught:
            self.save(old, jumps=self.with_hop(old, 2, auth='key'))

        self.assertIn('第 2 级跳板机', str(caught.exception))
        self.assertIn('私钥', str(caught.exception))

    def test_any_change_of_the_route_needs_the_fingerprints_again(self):
        old = self.connect_through_chain()
        moved = self.with_hop(old, 2, port=self.closed_port())
        named = self.with_hop(old, 2, resolve_on=1)
        changes = {'another address in the middle': {'jumps': moved},
                   'a hop fewer': {'jumps': self.form(old)['jumps'][:2]},
                   'a hop more': {'jumps': self.form(old)['jumps'] + [{**moved[0], 'port': 2200}]},
                   'no jump hosts at all': {'jumps': []},
                   'a hop to be looked up again': {'jumps': named}}
        for case, change in changes.items():
            with self.subTest(case):
                with self.assertRaises(ValueError) as caught:
                    self.save(old, **change)
                self.assertEqual(str(caught.exception), ssh_edit.ADDRESS_CHANGED)

        self.assertEqual(self.store.get('device_connections', old['id']), old)

    def test_a_replaced_hop_is_confirmed_and_the_other_hops_keep_their_saved_logins(self):
        old = self.connect_through_chain()
        before = self.secret(old)
        other = self.fixture(username='gate2', password=secrets.token_hex(6),
                             forward_to=[('127.0.0.1', self.gates[2].port)])
        self.gates[0].forward_to.add(('127.0.0.1', other.port))

        new = self.save_confirmed(old, jumps=self.with_hop(old, 2, port=other.port, password=other.password))

        self.assertEqual((new['jumps'][1]['port'], new['jumps'][1]['fingerprint']),
                         (other.port, device_access.fingerprint(other.key)))
        self.assertEqual((new['jumps'][0], new['jumps'][2]), (old['jumps'][0], old['jumps'][2]))
        after = self.secret(new)
        self.assertEqual(after['jumps'][1]['password'], other.password)
        self.assertEqual((after['jumps'][0], after['jumps'][2]), (before['jumps'][0], before['jumps'][2]))
        self.assertEqual(self.open(new).probe('system')['exit_code'], 0)
        details = self.audit()
        self.assertEqual(sorted(details['changed']), ['jump', 'jump_credentials'])
        self.assertIn(f'gate2@127.0.0.1:{other.port}', details['after']['jump'])
        self.assertNotIn(other.password, json.dumps(details))

    def test_a_saved_login_is_never_sent_to_another_machine_in_its_place(self):
        old = self.connect_through_chain()
        other = self.fixture(username='gate2', password=secrets.token_hex(6),
                             forward_to=[('127.0.0.1', self.gates[2].port)])
        self.gates[0].forward_to.add(('127.0.0.1', other.port))

        with self.assertRaises(ValueError) as caught:
            self.rescan(old, jumps=self.with_hop(old, 2, port=other.port))      # its password field left empty

        self.assertIn('第 2 级跳板机', str(caught.exception))
        self.assertIn('重新输入', str(caught.exception))
        self.assertEqual(other.auth_attempts, [])

    def test_a_dropped_hop_takes_its_login_with_it(self):
        old = self.connect_through_chain()
        dropped = self.secret(old)['jumps'][2]['password']
        self.gates[1].forward_to.add(('127.0.0.1', self.target.port))

        new = self.save_confirmed(old, jumps=self.form(old)['jumps'][:2])

        self.assertEqual(len(new['jumps']), 2)
        self.assertEqual(len(self.secret(new)['jumps']), 2)
        self.assertNotIn(dropped, json.dumps(self.secret(new)))
        self.assertEqual(self.open(new).probe('system')['exit_code'], 0)

    def test_back_to_a_direct_connection_forgets_every_jump_login(self):
        old = self.connect_through_chain()

        new = self.save_confirmed(old, jumps=[])

        self.assertNotIn('jumps', new)
        self.assertNotIn('jumps', self.store.get('device_connections', old['id']))
        self.assertEqual(self.secret(new).get('jumps', []), [])
        self.assertFalse(self.engine.status({'project_id': self.pid, 'robot_id': new['robot_id']})['via_jump'])

    def test_a_direct_robot_can_be_moved_behind_three_jump_hosts(self):
        old = self.connect_directly()

        new = self.save_confirmed(old, jumps=self.jumps(logins=chain.LEVELS))

        self.assertEqual((new['id'], new['fingerprint']), (old['id'], old['fingerprint']))
        self.assertEqual(len(new['jumps']), chain.LEVELS)
        self.assertEqual([hop['password'] for hop in self.secret(new)['jumps']], [p for _, p in self.logins])
        self.assertEqual(self.secret(new)['password'], ROBOT_LOGIN)     # the robot's own login was kept

    def test_the_status_page_follows_the_new_route(self):
        old = self.connect_through_chain(watch_services=chain.base.SERVICES)
        status = lambda: self.engine.status({'project_id': self.pid, 'robot_id': old['robot_id']})
        self.assertIn('gate3', status()['jump'])
        self.gates[1].forward_to.add(('127.0.0.1', self.target.port))

        self.save_confirmed(old, jumps=self.form(old)['jumps'][:2])

        view = status()
        self.assertTrue(view['reachable'], view)
        self.assertNotIn('gate3', view['jump'])


class ChainAddressRuleTests(unittest.TestCase):
    """The stand-in check of an edit that keeps every address (``ConnectionEdit._confirmed_before``)."""

    SAVED = {'id': 'connection-1', 'project_id': 'project-1', 'host': '192.0.2.10', 'port': 22, 'username': 'robot',
             'auth': 'password', 'fingerprint': 'SHA256:device',
             'jumps': [{'host': '203.0.113.10', 'port': 2222, 'username': 'ops', 'auth': 'password',
                        'fingerprint': 'SHA256:first'},
                       {'host': '10.0.0.5', 'port': 22, 'username': 'relay', 'auth': 'agent',
                        'fingerprint': 'SHA256:second', 'alias': 'mid'}]}

    def test_the_stand_in_check_carries_every_fingerprint_confirmed_before(self):
        request = {'host': '192.0.2.10', 'port': 22, 'username': 'operator',
                   'jumps': [{'host': '203.0.113.10', 'port': 2222, 'username': 'admin', 'auth': 'key'},
                             {'host': '10.0.0.5', 'port': 22, 'username': 'relay', 'auth': 'agent'}]}

        check = ssh_edit.ConnectionEdit._confirmed_before(request, self.SAVED)

        self.assertEqual(check['jumps'], [
            {'host': '203.0.113.10', 'port': 2222, 'username': 'admin', 'auth': 'key', 'fingerprint': 'SHA256:first'},
            {'host': '10.0.0.5', 'port': 22, 'username': 'relay', 'auth': 'agent', 'fingerprint': 'SHA256:second',
             'alias': 'mid'}])
        self.assertEqual((check['fingerprint'], check['username']), ('SHA256:device', 'operator'))
        self.assertNotIn('stage', check)

    def test_hops_in_another_order_are_another_route(self):
        swapped = [{key: hop[key] for key in ('host', 'port', 'username', 'auth')} for hop in reversed(self.SAVED['jumps'])]

        with self.assertRaises(ValueError) as caught:
            ssh_edit.ConnectionEdit._confirmed_before({'host': '192.0.2.10', 'port': 22, 'username': 'robot',
                                                       'jumps': swapped}, self.SAVED)

        self.assertEqual(str(caught.exception), ssh_edit.ADDRESS_CHANGED)
