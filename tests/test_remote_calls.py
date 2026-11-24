"""Issue #43: the owner's "允许远程调用" switch per installed skill, as the console reads and sets it.

Most cases use ``remote_runtime_fixture.RemoteRuntime``, a stand-in for the management layer's
``/v2/remote``. The last class starts a real simulated robot (a real ``neuxmind_runtime`` child) and
goes through the same steps against the real endpoint; it is skipped when this checkout's runtime
has no ``/v2/remote`` yet. Nothing reaches a real machine.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import connector, remote_calls  # noqa: E402
from brainos.errors import CodedError  # noqa: E402
from brainos.service import Platform  # noqa: E402
from remote_runtime_fixture import OPTIONAL_MOVER, TOKEN, RemoteRuntime  # noqa: E402

ROBOT, PROJECT = 'robot-fixture', 'project-fixture'
USER = {'id': 'user-1', 'name': '张工', 'email': 'owner@example.test'}


class RemoteCase(unittest.TestCase):
    def setUp(self):
        self.runtime = RemoteRuntime()
        self.addCleanup(self.runtime.close)
        self.work = tempfile.TemporaryDirectory(prefix='nx-issue43-remote-')
        self.addCleanup(self.work.cleanup)
        self.platform = Platform(os.path.join(self.work.name, 'state'))
        self.addCleanup(self.platform.close)
        self.remote = self.platform.remote_calls
        self.platform.store.put('robots', {'id': ROBOT, 'project_id': PROJECT, 'name': '测试机器人', 'family': 'quadruped',
                                           'endpoint': self.runtime.url, 'token': TOKEN, 'status': 'connected'})

    def ask(self, **more):
        return {'robot_id': ROBOT, 'project_id': PROJECT, **more}

    def state(self):
        return self.remote.state(self.ask())

    def digest(self, skill_id):
        return next(s['digest'] for s in self.runtime.skills if s['id'] == skill_id)

    def switch(self, skill_id, allowed, **more):
        if allowed and 'digest' not in more and any(s['id'] == skill_id for s in self.runtime.skills):
            more['digest'] = self.digest(skill_id)
        return self.remote.set_skill(self.ask(skill_id=skill_id, allowed=allowed, request_id='req-%d' % len(self.runtime.posted), **more), USER)


class Reading(RemoteCase):
    def test_the_link_every_installed_skill_and_which_of_them_move_the_robot(self):
        state = self.state()
        self.assertTrue(state['supported'])
        self.assertEqual((state['link']['state'], state['link']['host']), ('online', 'cloud.example.test'))
        rows = {row['id']: row for row in state['skills']}
        self.assertEqual(sorted(rows), ['goto_place', 'status_report'])
        self.assertEqual((rows['goto_place']['motion'], rows['status_report']['motion']), (True, False))
        self.assertEqual({row['allowed'] for row in state['skills']}, {False})
        self.assertEqual(state['calls'], [])

    def test_a_skill_that_may_move_through_an_optional_capability_counts_as_moving(self):
        unknown = {**OPTIONAL_MOVER, 'id': 'vendor_thing', 'requires': [{'id': 'not.in.the.standard'}], 'optional': []}
        broken = {**OPTIONAL_MOVER, 'id': 'broken_entry', 'requires': 'not a list', 'optional': None}
        self.runtime.skills += [dict(OPTIONAL_MOVER), unknown, broken]
        rows = {row['id']: row for row in self.state()['skills']}
        self.assertTrue(rows['battery_wave']['motion'])          # requires nothing that moves, may use the arm
        self.assertTrue(rows['vendor_thing']['motion'])          # a capability the standard does not know
        self.assertTrue(rows['broken_entry']['motion'])          # when in doubt: moving
        self.assertFalse(rows['status_report']['motion'])
        self.assertEqual(rows['battery_wave']['digest'], 'd' * 64)

    def test_an_older_management_layer_is_told_in_plain_words_not_as_an_error(self):
        self.runtime.supports_remote = False
        state = self.state()
        self.assertEqual((state['supported'], state['message']), (False, '这台机器人的管理层还不支持远程调用'))
        with self.assertRaises(CodedError) as caught:
            self.switch('status_report', False)
        self.assertEqual(caught.exception.code, 'remote_unsupported')

    def test_a_management_layer_that_does_not_answer_is_not_called_unsupported(self):
        self.runtime.close()
        state = self.state()
        self.assertIsNone(state['supported'])
        self.assertIn('读不到远程调用的状态', state['message'])

    def test_recent_calls_come_through_cut_to_known_fields(self):
        self.runtime.calls = [
            {'call_id': 'cal_1', 'skill_id': 'goto_place', 'status': 'succeeded', 'run_id': 'run_1', 'started_at': 10.0,
             'finished_at': 12.5, 'error': None, 'parameters': {'place': '会议室'}},
            {'call_id': 'cal_2', 'skill_id': '<b>x</b>', 'status': 'rejected', 'run_id': None, 'started_at': None,
             'finished_at': 13.0, 'error': {'code': 'remote_not_allowed', 'message': 'm' * 1000, 'trace': 'not for the page'}},
            'not a call']
        calls = self.state()['calls']
        self.assertEqual([c['call_id'] for c in calls], ['cal_1', 'cal_2'])
        self.assertNotIn('parameters', calls[0])
        self.assertEqual(calls[1]['error']['code'], 'remote_not_allowed')
        self.assertEqual(len(calls[1]['error']['message']), 300)
        self.assertNotIn('trace', calls[1]['error'])
        self.assertEqual(calls[1]['skill_id'], '<b>x</b>')            # kept as text; the page escapes it

    def test_a_switch_set_for_other_content_of_the_skill_no_longer_counts(self):
        self.runtime.switches['goto_place'] = {'allowed': True, 'digest': 'c' * 64, 'set_by': '张工', 'set_at': 5.0}
        row = next(r for r in self.state()['skills'] if r['id'] == 'goto_place')
        self.assertEqual((row['allowed'], row['stale']), (False, True))

    def test_a_switch_the_management_layer_voided_itself_is_shown_as_stale(self):
        # The real management layer answers a voided switch with allowed false and ``invalidated``.
        self.runtime.switches['goto_place'] = {'allowed': False, 'invalidated': True, 'set_by': '张工', 'set_at': 5.0}
        row = next(r for r in self.state()['skills'] if r['id'] == 'goto_place')
        self.assertEqual((row['allowed'], row['stale']), (False, True))

    def test_a_robot_of_another_project_or_without_a_management_layer_is_refused(self):
        with self.assertRaises(ValueError):
            self.remote.state({'robot_id': ROBOT, 'project_id': 'another-project'})
        robot = self.platform.store.get('robots', ROBOT)
        self.platform.store.put('robots', {**robot, 'endpoint': ''})
        with self.assertRaises(CodedError) as caught:
            self.state()
        self.assertEqual(caught.exception.code, 'remote_no_runtime')


class Switching(RemoteCase):
    def test_switching_on_needs_a_confirmation_and_records_the_signed_in_user(self):
        with self.assertRaises(CodedError) as caught:
            self.switch('goto_place', True)
        self.assertEqual(caught.exception.code, 'remote_confirm_required')
        with self.assertRaises(CodedError):
            self.switch('goto_place', True, confirmed='yes')              # only a real True counts
        self.assertEqual(self.runtime.posted, [])

        state = self.switch('goto_place', True, confirmed=True)
        sent = self.runtime.posted[0]
        self.assertEqual((sent['skill_id'], sent['allowed'], sent['hardware_authorized'], sent['set_by']),
                         ('goto_place', True, True, '张工'))
        self.assertTrue(sent['request_id'])
        row = next(r for r in state['skills'] if r['id'] == 'goto_place')
        self.assertEqual((row['allowed'], row['set_by']), (True, '张工'))
        event = [e for e in self.platform.store.list('events') if e['action'] == 'remote.skill_switched'][0]
        self.assertEqual(event['details'], {'robot_id': ROBOT, 'skill_id': 'goto_place', 'allowed': True, 'set_by': '张工'})

    def test_switching_on_is_bound_to_the_content_the_person_was_shown(self):
        with self.assertRaises(ValueError):
            self.switch('goto_place', True, confirmed=True, digest=None)          # nothing to bind it to
        self.assertEqual(self.runtime.posted, [])
        self.switch('goto_place', True, confirmed=True)
        self.assertEqual(self.runtime.posted[0]['digest'], 'a' * 64)
        self.runtime.skills[0]['digest'] = 'e' * 64                               # installed again with other content
        with self.assertRaises(connector.DeviceError) as caught:
            self.switch('goto_place', True, confirmed=True, digest='a' * 64)
        self.assertEqual((caught.exception.code, caught.exception.status), ('skill_changed', 409))
        self.switch('goto_place', False)                                          # turning off needs none
        self.assertNotIn('digest', self.runtime.posted[-1])

    def test_switching_off_needs_no_confirmation(self):
        self.switch('goto_place', True, confirmed=True)
        state = self.switch('goto_place', False)
        self.assertEqual(self.runtime.posted[1]['hardware_authorized'], False)
        self.assertFalse(next(r for r in state['skills'] if r['id'] == 'goto_place')['allowed'])

    def test_what_is_asked_is_checked_before_anything_is_sent(self):
        for bad in ({'skill_id': 'goto_place', 'allowed': 'true'}, {'skill_id': '../x', 'allowed': False},
                    {'skill_id': 'goto_place', 'allowed': False, 'request_id': ''}):
            with self.assertRaises(ValueError):
                self.remote.set_skill(self.ask(**{'request_id': 'r', **bad}), USER)
        with self.assertRaises(ValueError):
            self.remote.set_skill(self.ask(skill_id='goto_place', allowed=False, request_id='r'), {})    # nobody signed in
        self.assertEqual(self.runtime.posted, [])

    def test_a_skill_that_is_not_installed_keeps_the_robots_own_reason(self):
        with self.assertRaises(connector.DeviceError) as caught:
            self.switch('free_walk', False)
        self.assertEqual((caught.exception.code, caught.exception.status), ('skill_not_found', 404))

    def test_an_answer_that_does_not_confirm_is_not_taken_for_done(self):
        self.runtime.fail_switch = (200, {'accepted': False})
        with self.assertRaises(CodedError) as caught:
            self.switch('status_report', False)
        self.assertEqual(caught.exception.code, 'remote_switch_unconfirmed')


class RealManagementLayer(unittest.TestCase):
    """The same steps against a real ``neuxmind_runtime`` (a simulated body)."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='nx-issue43-remote-real-')
        self.addCleanup(self.work.cleanup)
        self.platform = Platform(os.path.join(self.work.name, 'state'))
        self.addCleanup(self.platform.close)
        self.robot = self.platform.add_sim_robot({'kind': 'quadruped'})
        self.ask = {'robot_id': self.robot['id'], 'project_id': self.robot['project_id']}
        if self.platform.remote_calls.state(self.ask)['supported'] is not True:
            self.skipTest('this checkout\'s runtime has no /v2/remote')

    def install(self, example):
        preview = self.platform.preview_skill_package({'robot_id': self.robot['id'], 'example': example})
        self.platform.install_skill_package({'robot_id': self.robot['id'], 'example': example, 'request_id': 'i-' + example,
                                             'package_digest': preview['package_digest']})

    def test_the_real_endpoint_lists_switches_and_takes_on_and_off(self):
        self.install('patrol')
        remote = self.platform.remote_calls
        state = remote.state(self.ask)
        self.assertEqual(state['link']['state'], 'off')                 # no cloud configured for a simulated robot
        row = next(r for r in state['skills'] if r['id'] == 'patrol')
        self.assertEqual((row['allowed'], row['motion']), (False, True))

        with self.assertRaises(connector.DeviceError) as caught:
            remote.set_skill({**self.ask, 'skill_id': 'patrol', 'allowed': True, 'confirmed': True, 'request_id': 'on-0',
                              'digest': '0' * 64}, USER)
        self.assertEqual(caught.exception.code, 'skill_changed')
        on = remote.set_skill({**self.ask, 'skill_id': 'patrol', 'allowed': True, 'confirmed': True, 'request_id': 'on-1',
                               'digest': row['digest']}, USER)
        row = next(r for r in on['skills'] if r['id'] == 'patrol')
        self.assertEqual((row['allowed'], row['set_by'], row['stale']), (True, '张工', False))
        self.assertTrue(next(r for r in remote.state(self.ask)['skills'] if r['id'] == 'patrol')['allowed'])

        off = remote.set_skill({**self.ask, 'skill_id': 'patrol', 'allowed': False, 'request_id': 'off-1'}, USER)
        self.assertFalse(next(r for r in off['skills'] if r['id'] == 'patrol')['allowed'])

    def test_the_real_endpoint_refuses_a_skill_that_is_not_installed(self):
        with self.assertRaises(connector.DeviceError) as caught:
            self.platform.remote_calls.set_skill({**self.ask, 'skill_id': 'patrol', 'allowed': False, 'request_id': 'x-1'}, USER)
        self.assertEqual(caught.exception.code, 'skill_not_found')


class PageScript(unittest.TestCase):
    def test_the_page_script_parses_and_its_pure_functions_hold(self):
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([node, 'tests/remote_calls_logic.cjs'], capture_output=True, text=True, timeout=120, cwd=str(root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'remote calls logic checks passed: \d+')


if __name__ == '__main__':
    unittest.main()
