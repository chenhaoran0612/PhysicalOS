"""Issue #9: change the SSH connection of a robot that is already connected.

Every machine here is a local SSH protocol fixture: nothing reaches a real one.
"""
import io
import json
import secrets
import unittest
from unittest import mock

import paramiko

import test_ssh_jump as base
from brainos import device_access, device_software, device_status, hardware_scan, ssh_edit
from brainos.service import now
from hardware_fixture import jetson
from software_fixture import software
from ssh_fixture import DEFAULT_LOGIN

ROBOT_LOGIN = DEFAULT_LOGIN[1]
# What a robot answers to the two slower reads of a full status (issue #8: its hardware and running software).
FULL_STATUS_OUTPUTS = {device_software.SOFTWARE_EXEC: software().encode(), hardware_scan.SCAN_EXEC: jetson().encode()}


def key_text(key):
    out = io.StringIO()
    key.write_private_key(out)
    return out.getvalue()


class EditCase(base.SshCase):
    def setUp(self):
        super().setUp()
        self.edit = self.platform.ssh_edit

    def machine(self, hostname, **options):
        """Another robot the jump host can reach: its own host key, the fixture's usual login."""
        output = base.STATUS_OUTPUT.replace(b'dog-fixture', hostname.encode())
        fixture = self.fixture(command_outputs={device_status.status_command(base.SERVICES): output}, **options)
        self.jump.forward_to.add(('127.0.0.1', fixture.port))
        return fixture

    def form(self, connection, **changes):
        """What the edit form sends when no credential field was filled in."""
        request = {'project_id': self.pid, 'edit_robot_id': connection['robot_id'], 'host': connection['host'],
                   'port': connection['port'], 'username': connection['username'], 'auth': connection['auth'],
                   'read_roots': connection['read_roots'], 'watch_services': connection['watch_services']}
        if connection.get('jump'):
            jump = connection['jump']
            request.update(jump={k: jump[k] for k in ('host', 'port', 'username')}, jump_auth=jump['auth'])
        return {**request, **changes}

    def save(self, connection, **changes):
        """Save without reading fingerprints again: allowed while no hop's address changed."""
        return self.edit.update(self.form(connection, **changes))

    def rescan(self, connection, **changes):
        """Read the fingerprints again (two steps through a jump host). Returns the request and the last check."""
        form = self.form(connection, **changes)
        check = self.engine.scan(form)
        if check.get('stage') == 'jump':
            form.update(jump_check_id=check['id'], trust_jump_fingerprint=check['fingerprint'])
            check = self.engine.scan(form)
        return form, check

    def save_confirmed(self, connection, **changes):
        form, check = self.rescan(connection, **changes)
        return self.edit.update({**form, 'host_check_id': check['id'], 'trust_fingerprint': check['fingerprint']})

    def saved(self, connection):
        return self.store.get('device_connections', connection['id'])

    def status(self, connection):
        return self.engine.status({'project_id': self.pid, 'robot_id': connection['robot_id']})

    def quick_status(self, connection):
        """The status without reading hardware and software again: it shows what the last full read kept."""
        return self.engine.status({'project_id': self.pid, 'robot_id': connection['robot_id'], 'quick': True})

    def session(self, connection, status='running', mode='discovery'):
        record = {'id': 'agent-session-' + secrets.token_hex(4), 'project_id': self.pid, 'robot_id': connection['robot_id'],
                  'connection_id': connection['id'], 'mode': mode, 'status': status, 'name': 'Fixture session',
                  'events': [], 'created_at': now(), 'updated_at': now()}
        return self.store.put('agent_sessions', record)


class EditInPlaceTests(EditCase):
    def test_a_new_login_changes_the_same_robot_and_the_same_connection_record(self):
        old = self.connect_directly()
        self.target.username, self.target.password = 'operator', secrets.token_hex(8)

        new = self.save(old, username='operator', password=self.target.password)

        self.assertEqual((new['id'], new['robot_id'], new['created_at']), (old['id'], old['robot_id'], old['created_at']))
        self.assertEqual(new['username'], 'operator')
        self.assertTrue(new['updated_at'])
        self.assertEqual(len(self.store.list('robots')), 1)                 # no second robot
        self.assertEqual(len(self.store.list('device_connections')), 1)     # no second connection
        self.assertEqual(self.store.get('robots', old['robot_id'])['connection_id'], old['id'])
        self.assertEqual(self.secret(new)['password'], self.target.password)
        changed = next(e for e in self.store.list('events') if e['action'] == 'device.ssh_updated')['details']['changed']
        self.assertEqual(sorted(changed), ['credentials', 'username'])

    def test_credentials_left_empty_are_kept(self):
        old = self.connect_through_jump()
        before = self.secret(old)
        attempts = (len(self.jump.auth_attempts), len(self.target.auth_attempts))

        new = self.save(old, watch_services=base.SERVICES, read_roots=['/opt/ros', '/etc/robot'])

        self.assertEqual(self.secret(new), before)
        self.assertEqual((new['watch_services'], new['read_roots']), (base.SERVICES, ['/opt/ros', '/etc/robot']))
        self.assertEqual(new['jump'], old['jump'])
        # the kept logins were tried on both hops before anything was saved
        self.assertEqual((len(self.jump.auth_attempts), len(self.target.auth_attempts)), (attempts[0] + 1, attempts[1] + 1))

    def test_a_refused_login_changes_nothing(self):
        old = self.connect_directly()
        before = self.secret(old)

        self.assert_refused('device', 'auth', '认证失败', lambda: self.save(old, password=secrets.token_hex(8)))

        self.assertEqual(self.saved(old), old)
        self.assertEqual(self.secret(old), before)

    def test_credentials_never_come_back(self):
        old = self.connect_through_jump()
        key = paramiko.RSAKey.generate(2048)
        self.target.authorized_keys.add(key.asbytes())

        new = self.save(old, auth='key', private_key=key_text(key))

        visible = json.dumps([new, self.platform.snapshot(), self.store.list('events')], ensure_ascii=False)
        for value in (ROBOT_LOGIN, base.JUMP_PASSWORD, key_text(key), key_text(key).splitlines()[1]):
            self.assertNotIn(value, visible)
        self.assertEqual(self.secret(new)['private_key'], key_text(key))

    def test_another_login_method_needs_its_own_secret_and_drops_the_old_one(self):
        old = self.connect_directly()
        key = paramiko.RSAKey.generate(2048)
        self.target.authorized_keys.add(key.asbytes())

        with self.assertRaises(ValueError) as caught:
            self.save(old, auth='key')                                   # nothing saved to fall back on
        self.assertIn('私钥', str(caught.exception))
        new = self.save(old, auth='key', private_key=key_text(key))

        self.assertEqual(new['auth'], 'key')
        self.assertEqual(self.secret(new)['password'], '')               # the password is not kept around

    def test_only_what_the_login_method_uses_is_stored(self):
        old = self.connect_through_jump()
        key = paramiko.RSAKey.generate(2048)
        self.target.authorized_keys.add(key.asbytes())
        leftover = secrets.token_hex(8)                                  # typed before the method was switched

        new = self.save(old, auth='key', private_key=key_text(key), password=leftover)

        stored = self.secret(new)
        self.assertEqual((stored['password'], stored['private_key']), ('', key_text(key)))
        self.assertEqual(stored['jump_password'], base.JUMP_PASSWORD)    # the other hop is untouched
        self.assertNotIn(leftover, json.dumps(stored))

    def test_a_saved_private_key_is_kept_while_the_key_field_stays_empty(self):
        key = paramiko.RSAKey.generate(2048)
        self.target.authorized_keys.add(key.asbytes())
        old = self.connect_directly(auth='key', private_key=key_text(key), password='')

        new = self.save(old, watch_services=['bridge'])                  # the key field stays empty

        self.assertEqual(self.secret(new)['private_key'], key_text(key))
        self.assertGreaterEqual(self.target.key_logins, 2)                    # once to connect, once to test the edit

    def test_validation_is_the_one_of_a_new_connection(self):
        old = self.connect_through_jump()
        cases = {'主机名或 IP': {'host': 'robot; reboot'}, 'SSH 用户名无效': {'username': '-oProxyCommand=x'},
                 '跳板机：': {'jump': {'host': 'gate', 'port': 0, 'username': base.JUMP_USER}},
                 '配置目录': {'read_roots': ['/']}, '服务名': {'watch_services': ['bridge; reboot']},
                 'SSH 认证方式无效': {'auth': 'none'}}

        for mention, change in cases.items():
            with self.subTest(change=change):
                with self.assertRaises(ValueError) as caught:
                    self.save(old, **change)
                self.assertIn(mention, str(caught.exception))
        self.assertEqual(self.saved(old), old)

    def test_a_wrong_jump_login_names_the_jump_host(self):
        old = self.connect_through_jump()

        self.assert_refused('jump', 'auth', '跳板机', lambda: self.save(old, jump_password=secrets.token_hex(8)))

    def test_only_a_robot_of_this_project_with_an_ssh_connection_can_be_edited(self):
        old = self.connect_directly()
        plain = self.platform.add_robot({'project_id': self.pid, 'name': 'No SSH', 'family': 'custom'})
        elsewhere = self.platform.add_project({'name': 'Other', 'objective': 'Another project'})['id']

        with self.assertRaises(ValueError) as caught:
            self.edit.update({**self.form(old), 'edit_robot_id': plain['id']})
        self.assertIn('没有 SSH 连接', str(caught.exception))
        with self.assertRaises(ValueError):
            self.edit.update({**self.form(old), 'project_id': elsewhere})
        with self.assertRaises(ValueError):
            self.edit.update({k: v for k, v in self.form(old).items() if k != 'edit_robot_id'})


class FingerprintTests(EditCase):
    def test_a_new_address_is_refused_until_its_fingerprint_is_confirmed(self):
        old = self.connect_directly(watch_services=base.SERVICES)
        other = self.machine('dog-b')

        with self.assertRaises(ValueError) as caught:
            self.save(old, port=other.port, password=ROBOT_LOGIN)
        self.assertIn('指纹', str(caught.exception))
        form, check = self.rescan(old, port=other.port, password=ROBOT_LOGIN)
        with self.assertRaises(ValueError):
            self.edit.update({**form, 'host_check_id': check['id'], 'trust_fingerprint': old['fingerprint']})
        self.assertEqual(self.saved(old), old)
        self.assertEqual(other.auth_attempts, [])                        # no login before the fingerprint is confirmed

        new = self.edit.update({**form, 'host_check_id': check['id'], 'trust_fingerprint': check['fingerprint']})

        self.assertEqual((new['id'], new['port']), (old['id'], other.port))
        self.assertEqual(new['fingerprint'], device_access.fingerprint(other.key))
        self.assertEqual(self.status(new)['status']['hostname'], 'dog-b')

    def test_going_through_a_jump_host_confirms_both_fingerprints(self):
        old = self.connect_directly()
        route = {'jump': {'host': '127.0.0.1', 'port': self.jump.port, 'username': base.JUMP_USER},
                 'jump_auth': 'password', 'jump_password': base.JUMP_PASSWORD}

        with self.assertRaises(ValueError):
            self.save(old, **route)                                      # a new hop: not without its fingerprint
        form = self.form(old, **route)
        first = self.engine.scan(form)
        self.assertEqual((first['stage'], first['fingerprint']), ('jump', device_access.fingerprint(self.jump.key)))
        with self.assertRaises(ValueError) as caught:                    # the jump host alone is not the robot
            self.edit.update({**form, 'host_check_id': first['id'], 'trust_fingerprint': first['fingerprint']})
        self.assertIn('跳板机', str(caught.exception))
        second = self.engine.scan({**form, 'jump_check_id': first['id'], 'trust_jump_fingerprint': first['fingerprint']})

        new = self.edit.update({**form, 'host_check_id': second['id'], 'trust_fingerprint': second['fingerprint']})

        self.assertEqual(new['id'], old['id'])
        self.assertEqual(new['jump'], {'host': '127.0.0.1', 'port': self.jump.port, 'username': base.JUMP_USER,
                                       'auth': 'password', 'fingerprint': device_access.fingerprint(self.jump.key)})
        self.assertEqual(self.secret(new)['jump_password'], base.JUMP_PASSWORD)
        self.assertTrue(self.status(new)['via_jump'])

    def test_another_jump_host_confirms_both_fingerprints_and_needs_its_login_again(self):
        old = self.connect_through_jump()
        gate = self.fixture(username=base.JUMP_USER, password=base.JUMP_PASSWORD,
                            forward_to=[('127.0.0.1', self.target.port)])
        route = {'jump': {'host': '127.0.0.1', 'port': gate.port, 'username': base.JUMP_USER}}

        with self.assertRaises(ValueError):
            self.save(old, **route)
        form = self.form(old, **route)
        first = self.engine.scan(form)
        confirmed = {**form, 'jump_check_id': first['id'], 'trust_jump_fingerprint': first['fingerprint']}
        with self.assertRaises(ValueError) as caught:
            self.engine.scan(confirmed)                                  # jump password left empty
        self.assertIn('重新输入', str(caught.exception))
        self.assertEqual(gate.auth_attempts, [])                         # the saved password was not sent to it

        second = self.engine.scan({**confirmed, 'jump_password': base.JUMP_PASSWORD})
        new = self.edit.update({**confirmed, 'jump_password': base.JUMP_PASSWORD, 'host_check_id': second['id'],
                                'trust_fingerprint': second['fingerprint']})

        self.assertEqual(new['jump']['port'], gate.port)
        self.assertEqual(new['jump']['fingerprint'], device_access.fingerprint(gate.key))
        self.assertEqual(new['fingerprint'], old['fingerprint'])
        self.assertEqual(gate.forwarded, [('127.0.0.1', self.target.port)] * 2)     # device key read, then the login test

    def test_back_to_a_direct_connection_forgets_the_jump_login(self):
        old = self.connect_through_jump()

        new = self.save_confirmed(old, jump=None)

        self.assertNotIn('jump', new)
        self.assertEqual(self.secret(new).get('jump_password', ''), '')
        self.assertFalse(self.status(new)['via_jump'])

    def test_a_saved_password_is_not_sent_to_a_different_machine(self):
        old = self.connect_directly()
        other = self.machine('dog-b')
        form, check = self.rescan(old, port=other.port)
        confirmed = {**form, 'host_check_id': check['id'], 'trust_fingerprint': check['fingerprint']}

        with self.assertRaises(ValueError) as caught:
            self.edit.update(confirmed)
        self.assertIn('重新输入', str(caught.exception))
        self.assertEqual(other.auth_attempts, [])

        new = self.edit.update({**confirmed, 'password': ROBOT_LOGIN})
        self.assertEqual(new['port'], other.port)

    def test_the_same_machine_at_a_new_address_keeps_its_saved_login(self):
        old = self.connect_directly()
        moved = self.machine('dog-fixture')
        moved.key = self.target.key                                      # same host key: the same machine, new address

        new = self.save_confirmed(old, port=moved.port)

        self.assertEqual((new['port'], new['fingerprint']), (moved.port, old['fingerprint']))
        self.assertEqual(moved.auth_attempts, ['robot'])
        self.assertEqual(self.secret(new)['password'], ROBOT_LOGIN)

    def test_a_host_key_that_changed_at_the_same_address_has_to_be_confirmed_again(self):
        old = self.connect_directly()
        self.target.key = paramiko.RSAKey.generate(2048)                 # e.g. the robot was reinstalled

        self.assert_refused('device', 'fingerprint', '指纹', lambda: self.save(old, watch_services=['bridge']))
        new = self.save_confirmed(old, password=ROBOT_LOGIN)

        self.assertEqual(new['fingerprint'], device_access.fingerprint(self.target.key))
        self.assertNotEqual(new['fingerprint'], old['fingerprint'])

    def test_a_name_resolved_on_the_jump_host_is_kept_until_the_target_changes(self):
        resolve = device_access.resolve_command('dog')
        self.jump.command_outputs[resolve] = f'user robot\nhostname 127.0.0.1\nport {self.target.port}\n'.encode()
        named = self.request(host='dog', port=None, username='', resolve_on_jump=True)
        first = self.engine.scan(named)
        second = self.engine.scan({**named, **self.jump_login(first)})
        old = self.engine.connect({'project_id': self.pid, 'host_check_id': second['id'], 'auth': 'password',
                                   'trust_fingerprint': second['fingerprint'], 'password': ROBOT_LOGIN,
                                   'jump_password': base.JUMP_PASSWORD, 'name': 'Named on the jump host',
                                   'family': 'quadruped', 'read_roots': ['/opt/ros']})
        other = self.machine('dog-b')

        kept = self.save(old, watch_services=['bridge'])
        with self.assertRaises(ValueError):
            self.save(old, resolve_on_jump=True)                         # resolving again is a new look-up
        moved = self.save_confirmed(old, port=other.port, password=ROBOT_LOGIN)

        self.assertEqual(kept['alias'], 'dog')
        self.assertNotIn('alias', moved)


class BusyRobotTests(EditCase):
    def test_a_running_investigation_refuses_the_change_and_says_why(self):
        old = self.connect_directly()
        running = self.session(old)

        for attempt in (lambda: self.save(old, watch_services=['bridge']), lambda: self.rescan(old)):
            with self.assertRaises(ValueError) as caught:
                attempt()
            self.assertIn('调查', str(caught.exception))
            self.assertIn('停止', str(caught.exception))
        self.assertEqual(self.saved(old), old)

        self.store.put('agent_sessions', {**running, 'status': 'completed'})
        self.assertEqual(self.save(old, watch_services=['bridge'])['watch_services'], ['bridge'])

    def test_a_running_repair_refuses_the_change_too(self):
        old = self.connect_directly()
        self.session(old, status='waiting_input', mode='repair')

        with self.assertRaises(ValueError) as caught:
            self.save(old, watch_services=['bridge'])
        self.assertIn('修复', str(caught.exception))

    def test_a_session_that_starts_while_the_new_login_is_tested_wins(self):
        old = self.connect_directly()
        real = device_access.SSHDevice

        def test_login(connection, secret):
            self.session(old)                                            # an investigation starts during the login test
            return real(connection, secret)

        with mock.patch.object(device_access, 'SSHDevice', test_login):
            with self.assertRaises(ValueError) as caught:
                self.save(old, watch_services=['bridge'])
        self.assertIn('调查', str(caught.exception))
        self.assertEqual(self.saved(old), old)

    def test_an_edit_that_was_overtaken_by_another_one_is_refused(self):
        old = self.connect_directly()
        real, logins = device_access.SSHDevice, []

        def test_login(connection, secret):
            logins.append(connection)
            if len(logins) == 1:                                         # while the first edit tests its login,
                self.save(old, watch_services=['nav-stack'])             # a second one is tested and saved
            return real(connection, secret)

        with mock.patch.object(device_access, 'SSHDevice', test_login):
            with self.assertRaises(ValueError) as caught:
                self.save(old, watch_services=['bridge'])
        self.assertIn('刚刚被改过', str(caught.exception))
        self.assertEqual(self.saved(old)['watch_services'], ['nav-stack'])

    def test_a_hardware_scan_in_progress_refuses_the_change(self):
        old = self.connect_directly()
        self.platform.hardware.scanning.add(old['robot_id'])

        with self.assertRaises(ValueError) as caught:
            self.save(old, watch_services=['bridge'])
        self.assertIn('扫描', str(caught.exception))

    def test_sessions_of_other_robots_do_not_matter(self):
        old = self.connect_directly()
        self.session({'robot_id': 'robot-other', 'id': 'connection-other'})

        self.assertEqual(self.save(old, watch_services=['bridge'])['watch_services'], ['bridge'])


class AfterTheChangeTests(EditCase):
    def test_the_cached_status_session_of_the_old_route_is_closed(self):
        old = self.connect_directly(watch_services=base.SERVICES)
        other = self.machine('dog-b')
        self.assertEqual(self.status(old)['status']['hostname'], 'dog-fixture')
        self.assertEqual(self.engine.status_sessions.open_connections(), [old['id']])

        new = self.save_confirmed(old, port=other.port, password=ROBOT_LOGIN)

        self.assertEqual(self.engine.status_sessions.open_connections(), [])
        self.assertEqual(self.status(new)['status']['hostname'], 'dog-b')

    def test_a_new_password_alone_also_ends_the_cached_session(self):
        old = self.connect_directly(watch_services=base.SERVICES)
        self.status(old)
        self.target.password = secrets.token_hex(8)

        self.save(old, password=self.target.password)

        self.assertEqual(self.engine.status_sessions.open_connections(), [])

    def test_saving_without_a_new_scan_leaves_no_host_check_behind(self):
        old = self.connect_directly()
        checks = len(self.store.list('host_key_checks'))

        self.save(old, watch_services=['bridge'])
        self.assert_refused('device', 'auth', '认证失败', lambda: self.save(old, password=secrets.token_hex(8)))

        self.assertEqual(len(self.store.list('host_key_checks')), checks)

    def test_a_poll_that_read_the_old_record_cannot_leave_its_session_cached(self):
        old = self.connect_directly(watch_services=base.SERVICES)
        old_secret = self.secret(old)
        other = self.machine('dog-b')
        new = self.save_confirmed(old, port=other.port, password=ROBOT_LOGIN)

        # A status poll that had read the connection just before the change reaches the session cache only now.
        late = self.engine.status_sessions.read(old, old_secret)
        view = self.status(new)

        self.assertEqual(late['status']['hostname'], 'dog-fixture')
        self.assertEqual(view['status']['hostname'], 'dog-b')

    def test_a_change_forgets_the_hardware_and_software_the_status_page_kept(self):
        self.target.command_outputs.update(FULL_STATUS_OUTPUTS)
        old = self.connect_directly(watch_services=base.SERVICES)
        before = self.status(old)
        other = self.machine('dog-b')

        new = self.save_confirmed(old, port=other.port, password=ROBOT_LOGIN)
        view = self.quick_status(new)

        self.assertTrue(before['hardware']['devices'])
        self.assertTrue(before['software']['services'])
        self.assertEqual(view['status']['hostname'], 'dog-b')
        self.assertIsNone(view['hardware'])               # dog-a's devices are not dog-b's
        self.assertIsNone(view['software'])

    def test_a_poll_that_read_the_old_record_cannot_leave_its_hardware_and_software_either(self):
        self.target.command_outputs.update(FULL_STATUS_OUTPUTS)
        old = self.connect_directly(watch_services=base.SERVICES)
        old_secret = self.secret(old)
        other = self.machine('dog-b')
        new = self.save_confirmed(old, port=other.port, password=ROBOT_LOGIN)

        # A full status poll that had read the connection just before the change reaches the session cache only now.
        late = self.engine.status_sessions.read(old, old_secret, full=True)
        view = self.quick_status(new)

        self.assertTrue(late['hardware']['devices'])
        self.assertTrue(late['software']['services'])
        self.assertEqual(view['status']['hostname'], 'dog-b')
        self.assertIsNone(view['hardware'])
        self.assertIsNone(view['software'])

    def test_records_of_the_robot_are_kept(self):
        old = self.connect_directly()
        rid, records = old['robot_id'], {}
        for kind in ('robot_inventories', 'hardware_scans', 'device_evidence', 'hardware_repairs'):
            records[kind] = self.store.put(kind, {'id': kind + '-kept', 'project_id': self.pid, 'robot_id': rid,
                                                  'connection_id': old['id'], 'created_at': now()})
        finished = self.session(old, status='completed')
        robot = self.store.get('robots', rid)
        robot.update(inventory_id='robot_inventories-kept', onboarding_status='discovered')
        self.store.put('robots', robot)
        other = self.machine('dog-b')

        new = self.save_confirmed(old, port=other.port, password=ROBOT_LOGIN)

        for kind, record in records.items():
            self.assertEqual(self.store.get(kind, record['id']), record)
        self.assertEqual(self.store.get('agent_sessions', finished['id']), finished)
        self.assertEqual(self.store.get('robots', rid), robot)
        # continuing the earlier investigation goes to the new address
        self.assertEqual(self.store.get('device_connections', finished['connection_id'])['port'], new['port'])

    def test_the_change_is_audited(self):
        old = self.connect_through_jump()
        other = self.machine('dog-b')

        new = self.save_confirmed(old, port=other.port, password=ROBOT_LOGIN, watch_services=['bridge'])

        event = next(e for e in self.store.list('events') if e['action'] == 'device.ssh_updated')
        details = event['details']
        self.assertEqual(event['project_id'], self.pid)
        self.assertEqual((details['robot_id'], details['connection_id']), (new['robot_id'], new['id']))
        self.assertEqual(details['before']['target'], f"robot@127.0.0.1:{old['port']}")
        self.assertEqual(details['after']['target'], f"robot@127.0.0.1:{other.port}")
        self.assertEqual(details['after']['jump'], f"{base.JUMP_USER}@127.0.0.1:{self.jump.port}")
        self.assertTrue(details['fingerprint_changed'])
        self.assertEqual(sorted(details['changed']), ['fingerprint', 'port', 'watch_services'])     # the login itself is the same

    def test_fields_of_the_record_that_the_edit_does_not_know_are_kept(self):
        old = self.connect_directly()
        self.store.put('device_connections', {**old, 'added_later': {'by': 'another feature'}})

        new = self.save(old, watch_services=['bridge'])

        self.assertEqual(new['added_later'], {'by': 'another feature'})

    def test_a_change_of_nothing_is_still_a_tested_login_and_is_audited_as_such(self):
        old = self.connect_directly()

        new = self.save(old)

        event = next(e for e in self.store.list('events') if e['action'] == 'device.ssh_updated')
        self.assertEqual(event['details']['changed'], [])
        self.assertEqual({k: v for k, v in new.items() if k != 'updated_at'}, old)


class AgentLoginEditTests(EditCase):
    start_agent_with = base.KeyLoginTests.start_agent_with
    stop_process = staticmethod(base.KeyLoginTests.stop_process)

    def test_switching_to_the_local_ssh_agent_stores_no_secret(self):
        old = self.connect_directly()
        key = paramiko.RSAKey.generate(2048)
        self.target.authorized_keys.add(key.asbytes())
        self.start_agent_with(key)

        new = self.save(old, auth='agent')

        self.assertEqual(new['auth'], 'agent')
        self.assertEqual({k: v for k, v in self.secret(new).items() if k != 'id' and v}, {})
        self.assertGreaterEqual(self.target.key_logins, 1)


class AddressRuleTests(unittest.TestCase):
    """When an edit may be saved without reading fingerprints again. The form decides the same way
    (``sshEditAddressChanged`` in web/ssh_edit.js, checked with the same cases in tests/ssh_edit_logic.cjs)."""

    DIRECT = {'id': 'connection-1', 'project_id': 'project-1', 'host': '192.0.2.10', 'port': 22, 'username': 'robot',
              'auth': 'password', 'fingerprint': 'SHA256:device'}
    JUMP = {'host': '203.0.113.10', 'port': 2222, 'username': 'ops', 'auth': 'password', 'fingerprint': 'SHA256:jump'}

    def changed(self, saved, **request):
        request = {'host': saved['host'], 'port': saved['port'], 'username': saved['username'], **request}
        if saved.get('jump') and 'jump' not in request:
            request['jump'] = {k: saved['jump'][k] for k in ('host', 'port', 'username')}
        try:
            ssh_edit.ConnectionEdit._confirmed_before(request, saved)
        except ValueError as exc:
            self.assertEqual(str(exc), ssh_edit.ADDRESS_CHANGED)
            return True
        return False

    def test_a_direct_connection(self):
        saved = self.DIRECT
        self.assertFalse(self.changed(saved))
        self.assertFalse(self.changed(saved, host=' 192.0.2.10 '))
        self.assertFalse(self.changed(saved, username='operator'))
        self.assertTrue(self.changed(saved, host='192.0.2.11'))
        self.assertTrue(self.changed(saved, port=2222))
        self.assertTrue(self.changed(saved, jump={'host': '203.0.113.10', 'port': 22, 'username': 'ops'}))

    def test_a_connection_through_a_jump_host(self):
        saved = {**self.DIRECT, 'jump': self.JUMP}
        self.assertFalse(self.changed(saved))
        self.assertFalse(self.changed(saved, jump={'host': '203.0.113.10', 'port': 2222, 'username': 'admin'}))
        self.assertTrue(self.changed(saved, jump={'host': '203.0.113.11', 'port': 2222, 'username': 'ops'}))
        self.assertTrue(self.changed(saved, jump={'host': '203.0.113.10', 'port': 22, 'username': 'ops'}))
        self.assertTrue(self.changed(saved, jump=None))
        self.assertTrue(self.changed(saved, resolve_on_jump=True))

    def test_the_stand_in_check_carries_the_fingerprints_confirmed_before(self):
        saved = {**self.DIRECT, 'jump': self.JUMP, 'alias': 'dog'}
        request = {'host': '192.0.2.10', 'port': 22, 'username': 'operator', 'jump_auth': 'key',
                   'jump': {'host': '203.0.113.10', 'port': 2222, 'username': 'admin'}}

        check = ssh_edit.ConnectionEdit._confirmed_before(request, saved)

        self.assertEqual((check['fingerprint'], check['username'], check['alias']), ('SHA256:device', 'operator', 'dog'))
        self.assertEqual(check['jump'], {'host': '203.0.113.10', 'port': 2222, 'username': 'admin', 'auth': 'key',
                                         'fingerprint': 'SHA256:jump'})
        self.assertNotIn('stage', check)


class FakeDevice:
    opened = []
    during_execute = None              # called while a status command "runs"

    def __init__(self, connection, secret):
        self.host, self.closed = connection['host'], False
        FakeDevice.opened.append(self)

    def execute(self, command, timeout, redact=True):
        if FakeDevice.during_execute:
            FakeDevice.during_execute()
        return {'stdout': base.STATUS_OUTPUT.decode(), 'timed_out': False}

    def close(self):
        self.closed = True


class StatusSessionRevisionTests(unittest.TestCase):
    def setUp(self):
        FakeDevice.opened, FakeDevice.during_execute = [], None
        self.sessions = device_status.StatusSessions()
        self.addCleanup(self.sessions.close)
        patch = mock.patch.object(device_access, 'SSHDevice', FakeDevice)
        patch.start()
        self.addCleanup(patch.stop)

    def connection(self, **changes):
        return {'id': 'connection-1', 'host': '192.0.2.10', 'port': 22, 'username': 'robot', **changes}

    def test_a_session_is_reused_while_the_connection_is_unchanged(self):
        self.sessions.read(self.connection(), {})
        self.sessions.read(self.connection(), {})

        self.assertEqual(len(FakeDevice.opened), 1)

    def test_a_changed_connection_does_not_reuse_the_session_opened_before(self):
        self.sessions.read(self.connection(), {})

        self.sessions.read(self.connection(host='192.0.2.20', updated_at='2026-11-14T00:00:00+00:00'), {})

        self.assertEqual([device.host for device in FakeDevice.opened], ['192.0.2.10', '192.0.2.20'])
        self.assertTrue(FakeDevice.opened[0].closed)
        self.assertFalse(FakeDevice.opened[1].closed)

    def test_a_poll_during_the_first_read_after_a_change_is_not_shown_the_old_machine(self):
        old = self.connection()
        new = self.connection(host='192.0.2.20', updated_at='2026-11-14T00:00:00+00:00')
        self.sessions.read(old, {})
        meanwhile = []
        # Another poll arrives while the first read of the changed connection is still running.
        FakeDevice.during_execute = lambda: meanwhile.append(self.sessions.read(new, {}))

        view = self.sessions.read(new, {})

        self.assertEqual([(seen['target'], seen['reachable'], seen['refreshing']) for seen in meanwhile],
                         [('robot@192.0.2.20:22', None, True)])
        self.assertEqual((view['target'], view['reachable']), ('robot@192.0.2.20:22', True))


if __name__ == '__main__':
    unittest.main()
