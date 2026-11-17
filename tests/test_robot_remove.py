"""Issue #11: delete a robot from "我的机器人" together with its SSH connection and its investigations.

Every machine here is a local SSH protocol fixture: nothing reaches a real one.
"""
import json
import secrets
import shutil
import threading
import unittest
from unittest import mock

import test_ssh_jump as base
from brainos import adaptation, robot_remove
from brainos.engineering import ACTIVE
from brainos.robot_remove import ROBOT_RECORDS
from brainos.service import now
from ssh_fixture import DEFAULT_LOGIN

ROBOT_LOGIN = DEFAULT_LOGIN[1]


class RemoveCase(base.SshCase):
    def setUp(self):
        super().setUp()
        self.removal = self.platform.robot_remove

    def remove(self, robot_id, project_id=None):
        return self.removal.remove({'project_id': project_id or self.pid, 'robot_id': robot_id})

    def exists(self, kind, key):
        return any(x['id'] == key for x in self.store.list(kind))

    def session(self, connection, status='completed', mode='discovery'):
        record = {'id': 'agent-session-' + secrets.token_hex(4), 'project_id': self.pid, 'robot_id': connection['robot_id'],
                  'connection_id': connection['id'], 'mode': mode, 'status': status, 'name': 'Fixture session',
                  'events': [], 'created_at': now(), 'updated_at': now()}
        return self.store.put('agent_sessions', record)

    def records(self, connection, session=None):
        """One record of every kind the desktop keeps about a robot, as (kind, id) pairs."""
        made = []
        for kind in ROBOT_RECORDS:
            record = self.store.put(kind, {'id': f'{kind}-{secrets.token_hex(4)}', 'project_id': self.pid,
                                           'robot_id': connection['robot_id'], 'connection_id': connection['id'],
                                           'session_id': session['id'] if session else None, 'created_at': now()})
            made.append((kind, record['id']))
            if kind == 'hardware_scans':
                self.store.put('hardware_scan_raw', {'id': record['id'], 'stdout': 'raw scan output'})
                made.append(('hardware_scan_raw', record['id']))
        return made

    def workspace(self, session):
        """What a run leaves on disk: the files it wrote and the home of its runtime."""
        root = self.store.root / 'engineering' / session['id']
        (root / 'workspace').mkdir(parents=True)
        (root / 'runtime-home').mkdir()
        (root / 'workspace' / 'DEVICE_CONTEXT.json').write_text('{"robot": "read from the device"}')
        return root

    def flow(self, robot_id, status, template=adaptation.TEMPLATE):
        return self.store.put('workflow_runs', {
            'id': 'workflow-run-' + secrets.token_hex(4), 'project_id': self.pid, 'status': status, 'current_stage': 0,
            'stages': [], 'definition': {'template': template, 'robot_id': robot_id, 'stages': []}, 'created_at': now()})

    def assert_untouched(self, connection, made=()):
        self.assertTrue(self.exists('robots', connection['robot_id']))
        self.assertTrue(self.exists('device_connections', connection['id']))
        self.assertEqual(self.secret(connection)['password'], ROBOT_LOGIN)
        for kind, key in made:
            self.assertTrue(self.exists(kind, key), kind)


class WhatGoesTests(RemoveCase):
    def test_the_robot_its_connection_and_its_saved_login_are_gone(self):
        connection = self.connect_through_jump()

        result = self.remove(connection['robot_id'])

        self.assertEqual(result['removed'], connection['robot_id'])
        self.assertFalse(self.exists('robots', connection['robot_id']))
        self.assertFalse(self.exists('device_connections', connection['id']))
        self.assertFalse(self.exists('agent_secrets', connection['id']))     # the robot's and the jump host's logins

    def test_investigations_repairs_and_what_they_found_go_with_it(self):
        connection = self.connect_directly()
        investigation = self.session(connection)
        repair = self.session(connection, status='interrupted', mode='repair')
        made = self.records(connection, investigation)

        result = self.remove(connection['robot_id'])

        for kind, key in [('agent_sessions', investigation['id']), ('agent_sessions', repair['id']), *made]:
            self.assertFalse(self.exists(kind, key), kind)
        self.assertEqual(result['deleted'], {'device_connections': 1, 'agent_sessions': 2, 'device_evidence': 1,
                                             'robot_inventories': 1, 'hardware_scans': 1, 'hardware_repairs': 1})

    def test_what_its_sessions_wrote_on_this_computer_is_deleted(self):
        connection = self.connect_directly()
        root = self.workspace(self.session(connection))
        other = self.workspace(self.session(self.connect_directly()))

        self.remove(connection['robot_id'])

        self.assertFalse(root.exists())
        self.assertTrue((other / 'workspace' / 'DEVICE_CONTEXT.json').is_file())

    def test_a_session_record_cannot_point_the_deletion_outside_the_sessions_directory(self):
        connection = self.connect_directly()
        other = self.workspace(self.session(self.connect_directly()))
        outside = self.store.root / 'kept'
        outside.mkdir()
        (outside / 'file').write_text('not a session')
        for bad in ('../kept', '..', '.', ''):            # a sibling, the data directory, the sessions' directory (twice)
            self.store.put('agent_sessions', {**self.session(connection), 'id': bad})

        self.remove(connection['robot_id'])

        self.assertTrue((outside / 'file').is_file())
        self.assertTrue((self.store.root / 'registry.sqlite3').is_file())
        self.assertTrue((other / 'workspace' / 'DEVICE_CONTEXT.json').is_file())

    def test_a_connection_the_robot_had_before_goes_too(self):
        first = self.connect_directly()
        check = self.engine.scan(self.request(jump=False))
        second = self.engine.connect({'project_id': self.pid, 'robot_id': first['robot_id'], 'host_check_id': check['id'],
                                      'trust_fingerprint': check['fingerprint'], 'auth': 'password',
                                      'password': ROBOT_LOGIN, 'read_roots': ['/opt/ros']})
        self.assertTrue(self.exists('agent_secrets', first['id']))           # replaced, but still on disk

        result = self.remove(first['robot_id'])

        for connection in (first, second):
            self.assertFalse(self.exists('device_connections', connection['id']))
            self.assertFalse(self.exists('agent_secrets', connection['id']))
        self.assertEqual(result['deleted']['device_connections'], 2)

    def test_one_of_two_cards_of_the_same_machine_can_go(self):
        """The case of the issue: the same robot was added twice."""
        gone, kept = self.connect_directly(), self.connect_directly(watch_services=base.SERVICES)
        self.assertEqual((gone['host'], gone['port']), (kept['host'], kept['port']))
        kept_session = self.session(kept)
        made = self.records(kept, kept_session) + [('agent_sessions', kept_session['id'])]
        self.records(gone, self.session(gone))

        self.remove(gone['robot_id'])

        self.assert_untouched(kept, made)
        status = self.engine.status({'project_id': self.pid, 'robot_id': kept['robot_id'], 'quick': True})
        self.assertTrue(status['reachable'])

    def test_the_status_page_s_ssh_session_is_closed(self):
        connection = self.connect_directly(watch_services=base.SERVICES)
        self.engine.status({'project_id': self.pid, 'robot_id': connection['robot_id'], 'quick': True})
        self.assertEqual(self.engine.status_sessions.open_connections(), [connection['id']])

        self.remove(connection['robot_id'])

        self.assertEqual(self.engine.status_sessions.open_connections(), [])

    def test_the_machine_is_not_contacted(self):
        connection = self.connect_directly()
        logins, commands = len(self.target.auth_attempts), len(self.target.commands)

        self.remove(connection['robot_id'])

        self.assertEqual((len(self.target.auth_attempts), len(self.target.commands)), (logins, commands))

    def test_a_robot_connected_by_its_service_address_can_be_deleted_too(self):
        robot = self.platform.add_robot({'project_id': self.pid, 'name': 'Runtime robot', 'family': 'quadruped',
                                         'endpoint': 'http://127.0.0.1:9', 'token': secrets.token_hex(12)})

        result = self.remove(robot['id'])

        self.assertFalse(self.exists('robots', robot['id']))
        self.assertEqual(result, {'removed': robot['id'], 'deleted': {}})

    def test_the_console_no_longer_sees_anything_of_the_robot(self):
        connection = self.connect_directly()
        self.session(connection)
        self.store.put('robot_inventories', {'id': 'inventory-' + secrets.token_hex(4), 'project_id': self.pid,
                                             'robot_id': connection['robot_id'], 'created_at': now()})

        self.remove(connection['robot_id'])

        state = {k: v for k, v in self.platform.snapshot().items() if k != 'events'}
        self.assertNotIn(connection['robot_id'], json.dumps(state))
        self.assertNotIn(connection['id'], json.dumps(state))


class WhatStaysTests(RemoveCase):
    def test_applications_and_run_records_that_name_the_robot_stay(self):
        connection = self.connect_directly()
        rid = connection['robot_id']
        kept = [(kind, self.store.put(kind, {'id': f'{kind}-kept', 'project_id': self.pid, 'robot_id': rid,
                                             'created_at': now()})['id'])
                for kind in ('applications', 'evaluations', 'deployments', 'operations')]

        self.remove(rid)

        for kind, key in kept:
            self.assertTrue(self.exists(kind, key), kind)

    def test_a_flow_that_is_not_running_stays_as_a_record(self):
        connection = self.connect_directly()
        for status in ('awaiting_review', 'paused', 'blocked', 'interrupted', 'completed'):
            self.flow(connection['robot_id'], status)

        self.remove(connection['robot_id'])

        self.assertEqual(len(self.store.list('workflow_runs')), 5)

    def test_the_deletion_is_audited_without_any_credential(self):
        connection = self.connect_through_jump()
        self.session(connection)

        self.remove(connection['robot_id'])

        event = next(e for e in self.store.list('events') if e['action'] == 'robot.removed')
        self.assertEqual(event['project_id'], self.pid)
        self.assertEqual(event['details']['robot_id'], connection['robot_id'])
        self.assertEqual(event['details']['name'], 'Robot behind a jump host')
        self.assertEqual(event['details']['deleted'], {'device_connections': 1, 'agent_sessions': 1})
        text = json.dumps(event)
        self.assertNotIn(ROBOT_LOGIN, text)
        self.assertNotIn(base.JUMP_PASSWORD, text)


class RefusedTests(RemoveCase):
    def assert_refused(self, robot_id, *mentions, project_id=None):
        with self.assertRaises(ValueError) as caught:
            self.remove(robot_id, project_id)
        for mention in mentions:
            self.assertIn(mention, str(caught.exception))

    def test_a_running_investigation_refuses_the_deletion_and_says_why(self):
        connection = self.connect_directly()
        running = self.session(connection, status='running')
        made = self.records(connection, running) + [('agent_sessions', running['id'])]

        self.assert_refused(connection['robot_id'], '调查', '停止')

        self.assert_untouched(connection, made)

    def test_a_running_repair_refuses_it_too(self):
        connection = self.connect_directly()
        self.session(connection, status='waiting_input', mode='repair')

        self.assert_refused(connection['robot_id'], '修复')

        self.assert_untouched(connection)

    def test_every_state_of_a_session_that_is_still_going_refuses(self):
        connection = self.connect_directly()
        session = self.session(connection)
        for status in ACTIVE:
            with self.subTest(status=status):
                self.store.put('agent_sessions', {**session, 'status': status})
                self.assert_refused(connection['robot_id'], '正在运行')
        self.assert_untouched(connection)

    def test_a_hardware_scan_in_progress_refuses(self):
        connection = self.connect_directly()
        self.platform.hardware.scanning.add(connection['robot_id'])

        self.assert_refused(connection['robot_id'], '扫描硬件')

        self.assert_untouched(connection)

    def test_an_adaptation_flow_that_is_running_refuses(self):
        connection = self.connect_directly()
        for status in ('running', 'stopping'):
            with self.subTest(status=status):
                run = self.flow(connection['robot_id'], status)
                self.assert_refused(connection['robot_id'], '本体适配')
                self.store.delete('workflow_runs', run['id'])
        self.assert_untouched(connection)

    def test_sessions_scans_and_flows_of_other_robots_do_not_matter(self):
        connection, other = self.connect_directly(), self.connect_directly()
        self.session(other, status='running')
        self.platform.hardware.scanning.add(other['robot_id'])
        self.flow(other['robot_id'], 'running')

        self.remove(connection['robot_id'])

        self.assertFalse(self.exists('robots', connection['robot_id']))

    def test_only_a_robot_of_this_project_can_be_deleted(self):
        connection = self.connect_directly()
        elsewhere = self.platform.add_project({'name': 'Other', 'objective': 'Another project'})['id']

        self.assert_refused(connection['robot_id'], '不属于当前项目', project_id=elsewhere)
        self.assert_refused('robot-unknown', '找不到')

        self.assert_untouched(connection)

    def test_a_simulated_robot_is_left_to_its_own_delete_button(self):
        robot = self.store.put('robots', {'id': 'robot-sim', 'project_id': self.pid, 'name': 'Sim', 'family': 'quadruped',
                                          'sim': {'kind': 'quadruped', 'enabled': False}, 'created_at': now()})

        self.assert_refused(robot['id'], '仿真')

        self.assertTrue(self.exists('robots', robot['id']))

    def test_a_session_whose_thread_is_still_registered_refuses(self):
        """The record says it ended, the engine still holds its run: it is not over yet."""
        connection = self.connect_directly()
        ended = self.session(connection, status='interrupted')
        self.engine.live[ended['id']] = {'stop': threading.Event(), 'thread': threading.Thread(target=lambda: None)}
        self.addCleanup(self.engine.live.clear)

        self.assert_refused(connection['robot_id'], '正在运行')

        self.assert_untouched(connection)


class AllOrNothingTests(RemoveCase):
    def test_nothing_can_start_between_the_checks_and_the_last_delete(self):
        """Starting a session takes the engine's lock and registering a hardware scan the scanner's:
        the deletion holds both from its first read of the store to its last delete."""
        connection = self.connect_directly()
        self.records(connection, self.session(connection))
        free, real_list, real_delete = [], self.store.list, self.store.delete

        def probe(lock):
            taken = lock.acquire(timeout=0.05)
            if taken:
                lock.release()
            free.append(taken)

        def probed(real):
            def call(*args):
                for lock in (self.engine.lock, self.platform.hardware.lock):
                    thread = threading.Thread(target=probe, args=(lock,))
                    thread.start()
                    thread.join()
                return real(*args)
            return call

        self.store.list, self.store.delete = probed(real_list), probed(real_delete)
        try:
            self.remove(connection['robot_id'])
        finally:
            self.store.list, self.store.delete = real_list, real_delete

        self.assertGreater(len(free), 20)                 # every read and every delete was probed, on both locks
        self.assertFalse(any(free))

    def test_files_that_cannot_be_deleted_keep_the_robot_and_everything_else(self):
        connection = self.connect_directly()
        session = self.session(connection)
        root = self.workspace(session)
        made = self.records(connection, session) + [('agent_sessions', session['id'])]

        with mock.patch.object(shutil, 'rmtree', lambda *args, **kwargs: None):
            with self.assertRaises(ValueError) as caught:
                self.remove(connection['robot_id'])

        self.assertIn(str(root), str(caught.exception))
        self.assertIn('机器人没有删除', str(caught.exception))
        self.assert_untouched(connection, made)
        self.assertFalse(any(e['action'] == 'robot.removed' for e in self.store.list('events')))

    def test_a_deletion_that_was_interrupted_is_finished_by_deleting_again(self):
        """Whichever delete the first attempt dies in, the card is still there and the second attempt leaves nothing."""
        kinds = ('agent_secrets', 'device_connections', 'agent_sessions', *ROBOT_RECORDS, 'hardware_scan_raw', 'robots')
        for dies_in in kinds:
            with self.subTest(dies_in=dies_in):
                connection = self.connect_directly()
                session = self.session(connection)
                root = self.workspace(session)
                made = self.records(connection, session) + [('agent_sessions', session['id']),
                                                            ('device_connections', connection['id']),
                                                            ('agent_secrets', connection['id'])]
                real_delete = self.store.delete

                def delete(kind, key):
                    if kind == dies_in:
                        raise OSError('disk full')
                    return real_delete(kind, key)

                self.store.delete = delete
                try:
                    with self.assertRaises(OSError):
                        self.remove(connection['robot_id'])
                finally:
                    self.store.delete = real_delete
                self.assertTrue(self.exists('robots', connection['robot_id']))

                self.remove(connection['robot_id'])

                self.assertFalse(self.exists('robots', connection['robot_id']))
                for kind, key in made:
                    self.assertFalse(self.exists(kind, key), kind)
                self.assertFalse(root.exists())
                self.assertEqual(sum(e['action'] == 'robot.removed' and e['details']['robot_id'] == connection['robot_id']
                                     for e in self.store.list('events')), 1)

    def test_a_scan_that_was_about_to_start_fails_instead_of_saving_a_record(self):
        """A scan reads the robot first and registers afterwards. One that read it just before the deletion
        registers after it, finds the saved login gone and saves nothing."""
        connection = self.connect_directly()
        hardware = self.platform.hardware
        resolved = hardware._connection                   # what scan() calls first
        deleted = threading.Event()

        def resolve_then_lose_the_race(robot_id, project_id):
            found = resolved(robot_id, project_id)
            self.remove(robot_id)
            deleted.set()
            return found

        with mock.patch.object(hardware, '_connection', resolve_then_lose_the_race):
            with self.assertRaises(ValueError):
                hardware.scan({'project_id': self.pid, 'robot_id': connection['robot_id']})

        self.assertTrue(deleted.is_set())
        self.assertEqual(self.store.list('hardware_scans'), [])
        self.assertEqual(self.store.list('hardware_scan_raw'), [])
        self.assertEqual(hardware.scanning, set())


class ModuleTests(unittest.TestCase):
    def test_the_names_of_running_sessions_match_the_connection_editor(self):
        """Both refusals name the session the same way."""
        from brainos import ssh_edit
        self.assertEqual(robot_remove.SESSION_NAMES, ssh_edit.SESSION_NAMES)


if __name__ == '__main__':
    unittest.main()
