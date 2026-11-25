"""Issue #42 (second part): robots belong to the account, not to one task.

"我的机器人" and the tasks are independent: every task lists and uses the same robots, a robot added
while one task was open is there when another one is, and deleting a task leaves the robots. The rule
is in brainos/robot_scope.py: one home for every robot (a record that is not a task), requests about
robots answered for that home, and data from before moved there when a workspace is opened.

No device is contacted: the robots here are records, the simulated one is the bundled simulation.
"""
import http.client
import json
import os
os.environ.setdefault('NEUXMIND_MAIL_DEV_ECHO', '1')  # registration codes come back in the API response
import secrets
import tempfile
import unittest

from brainos import robot_scope
from brainos.robot_scope import HOME
from brainos.server import Application, make_handler
from brainos.service import Platform
from test_product import Server


def open_platform(case, root):
    platform = Platform(root)
    case.addCleanup(platform.close)
    return platform


class OldWorkspace(unittest.TestCase):
    """A workspace as it was written before: each robot, and what is kept about it, in a task."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        platform = open_platform(self, self.root)
        store = platform.store
        self.a = platform.add_project({'name': 'Site A', 'objective': 'Inspect the site'})['id']
        self.b = platform.add_project({'name': 'Site B', 'objective': 'Carry parts'})['id']
        self.first = platform.add_robot({'project_id': self.a, 'name': 'Patrol 1', 'family': 'quadruped'})['id']
        self.second = platform.add_robot({'project_id': self.b, 'name': 'Arm 1', 'family': 'manipulator'})['id']
        self.third = platform.add_robot({'project_id': self.a, 'name': 'Patrol 2', 'family': 'quadruped'})['id']
        robot = store.get('robots', self.first)
        store.put('robots', {**robot, 'connection_id': 'connection-1'})
        store.put('device_connections', {'id': 'connection-1', 'project_id': self.a, 'host': 'device.test'})
        for kind, key in (('agent_sessions', 'session-1'), ('robot_inventories', 'inventory-1'),
                          ('hardware_scans', 'scan-1'), ('device_profiles', 'profile-1')):
            store.put(kind, {'id': key, 'project_id': self.a, 'robot_id': self.first, 'status': 'completed'})
        store.put('host_key_checks', {'id': 'check-1', 'project_id': self.a, 'host': 'device.test'})
        # What the tasks made with the robot, and a task's own assistant session.
        store.put('applications', {'id': 'application-1', 'project_id': self.a, 'robot_id': self.first, 'name': 'Round'})
        store.put('operations', {'id': 'operation-1', 'project_id': self.a, 'robot_id': self.first, 'kind': 'stop',
                                 'application_id': 'application-1'})
        # What was done to the robot directly: a skill installed on it, an installed skill run.
        store.put('operations', {'id': 'operation-install', 'project_id': self.a, 'robot_id': self.first, 'kind': 'skill_install'})
        store.put('operations', {'id': 'operation-run', 'project_id': self.a, 'robot_id': self.first, 'kind': 'skill_run'})
        store.put('agent_sessions', {'id': 'session-task', 'project_id': self.a, 'robot_id': None, 'status': 'completed'})
        store.put('conformance_reports', {'id': 'conformance-1', 'project_id': self.a, 'robot_id': self.first})   # of a task's flow
        store.put('device_evidence', {'project_id': self.a, 'robot_id': self.first, 'id': 7})     # a record nothing could save again
        # A record another task kept about the robot (it should not exist; it is left alone).
        store.put('robot_inventories', {'id': 'inventory-other', 'project_id': self.b, 'robot_id': self.first})
        self.order = [r['id'] for r in store.list('robots')]
        self.sessions = [s['id'] for s in store.list('agent_sessions')]
        # Undo what opening the workspace did, as a version from before would have left it.
        for kind in store.kinds():
            for record in store.list(kind):
                if record.get('project_id') == HOME:
                    self.fail(f'{kind} was written at the home by code that was given a task')
        store.delete('projects', HOME)
        platform.close()

    def project_of(self, store, kind, key):
        return store.get(kind, key)['project_id']

    def test_robots_and_what_is_kept_about_them_move_to_the_home(self):
        store = open_platform(self, self.root).store

        self.assertEqual({r['id']: r['project_id'] for r in store.list('robots')},
                         {self.first: HOME, self.second: HOME, self.third: HOME})
        for kind, key in (('device_connections', 'connection-1'), ('agent_sessions', 'session-1'),
                          ('robot_inventories', 'inventory-1'), ('hardware_scans', 'scan-1'),
                          ('device_profiles', 'profile-1'), ('host_key_checks', 'check-1'),
                          ('operations', 'operation-install'), ('operations', 'operation-run')):
            self.assertEqual(self.project_of(store, kind, key), HOME, key)

    def test_what_a_task_made_stays_with_the_task(self):
        store = open_platform(self, self.root).store

        for kind, key in (('applications', 'application-1'), ('operations', 'operation-1'), ('agent_sessions', 'session-task'),
                          ('conformance_reports', 'conformance-1')):
            self.assertEqual(self.project_of(store, kind, key), self.a, kind)
        self.assertEqual(self.project_of(store, 'robot_inventories', 'inventory-other'), self.b)

    def test_the_lists_keep_their_order(self):
        store = open_platform(self, self.root).store

        self.assertEqual([r['id'] for r in store.list('robots')], self.order)
        self.assertEqual([s['id'] for s in store.list('agent_sessions')], self.sessions)

    def test_moving_happens_once(self):
        store = open_platform(self, self.root).store

        self.assertEqual(robot_scope.migrate(store), {})

    def test_report_says_what_moved(self):
        from brainos.store import Store
        store = Store(self.root)
        self.addCleanup(store.close)

        moved = robot_scope.migrate(store)

        self.assertEqual(moved, {'robots': 3, 'device_connections': 1, 'agent_sessions': 1, 'robot_inventories': 1,
                                 'hardware_scans': 1, 'device_profiles': 1, 'host_key_checks': 1, 'operations': 2})

    def test_a_task_that_had_robots_can_be_deleted_and_the_robots_stay(self):
        platform = open_platform(self, self.root)

        removed = platform.task_remove.remove({'project_id': self.a})

        self.assertEqual(removed['deleted'], {'agent_sessions': 1, 'applications': 1, 'operations': 1, 'conformance_reports': 1,
                                              'device_evidence': 1})
        store = platform.store
        self.assertEqual({r['id'] for r in store.list('robots')}, {self.first, self.second, self.third})
        self.assertEqual(self.project_of(store, 'device_connections', 'connection-1'), HOME)
        self.assertEqual([s['id'] for s in store.list('agent_sessions')], ['session-1'])


class HomeRules(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.platform = open_platform(self, tmp.name)
        self.a = self.platform.add_project({'name': 'Site A', 'objective': 'Inspect the site'})['id']
        self.b = self.platform.add_project({'name': 'Site B', 'objective': 'Carry parts'})['id']
        self.robot = self.platform.add_robot({'project_id': HOME, 'name': 'Patrol 1', 'family': 'quadruped'})['id']

    def test_the_home_is_not_a_task(self):
        state = self.platform.snapshot()

        self.assertEqual([p['id'] for p in state['projects']], [self.b, self.a])
        self.assertEqual(state['robot_home'], HOME)
        self.assertEqual([r['project_id'] for r in state['robots']], [HOME])

    def test_the_home_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            self.platform.task_remove.remove({'project_id': HOME})

        self.assertEqual(self.platform.project(HOME)['id'], HOME)
        self.assertEqual(len(self.platform.store.list('robots')), 1)

    def test_every_task_can_use_the_robot(self):
        for task in (self.a, self.b):
            self.assertEqual(self.platform.same_project('robots', self.robot, task)['id'], self.robot)

    def test_a_request_for_the_home_can_name_what_a_task_holds(self):
        self.platform.store.put('agent_models', {'id': 'model-1', 'project_id': self.a})

        self.assertEqual(self.platform.same_project('agent_models', 'model-1', HOME)['id'], 'model-1')

    def test_a_task_can_read_what_is_kept_at_the_home(self):
        self.platform.store.put('robot_inventories', {'id': 'inventory-1', 'project_id': HOME, 'robot_id': self.robot})

        self.assertEqual(self.platform.same_project('robot_inventories', 'inventory-1', self.b)['id'], 'inventory-1')

    def test_tasks_still_do_not_share_their_own_records(self):
        self.platform.store.put('agent_models', {'id': 'model-1', 'project_id': self.a})

        with self.assertRaises(ValueError):
            self.platform.same_project('agent_models', 'model-1', self.b)

    def test_a_task_check_counts_the_robots_of_the_account(self):
        result = self.platform.run_agent({'project_id': self.b, 'agent_id': 'solution'})

        self.assertNotIn('项目尚未登记机器人', json.dumps(result, ensure_ascii=False))
        self.assertIn('Patrol 1', json.dumps(result, ensure_ascii=False))


class Requests(unittest.TestCase):
    def test_requests_about_robots_are_for_the_home(self):
        for route in ('robots', 'robots/detail', 'robots/run-skill', 'robots/limits/save', 'robots/runtime-install/start',
                      'sims', 'sims/remove', 'engineering/ssh/scan', 'engineering/ssh/connect', 'engineering/hardware/scan',
                      'engineering/profile', 'engineering/profile/read', 'engineering/investigation/fix', 'engineering/start',
                      'engineering/adaptation/start', 'skills/package-install', 'skills/examples', 'library/deliver/start',
                      'cloud-skills/install', 'cloud-skills/install-preview'):
            scoped = robot_scope.request(route, {'project_id': 'project-a', 'robot_id': 'robot-1'})
            self.assertEqual(scoped, {'project_id': HOME, 'robot_id': 'robot-1', 'task_project_id': 'project-a'}, route)

    def test_requests_of_a_task_keep_their_task(self):
        for route in ('projects', 'projects/remove', 'applications', 'deployments', 'operations', 'onsite', 'harness',
                      'engineering/work', 'engineering/workflows', 'engineering/models', 'engineering/skills', 'skills',
                      'models/deploy', 'jobs', 'agents/run', 'cloud-skills/publish', 'library/list'):
            payload = {'project_id': 'project-a', 'robot_id': 'robot-1'}
            self.assertIs(robot_scope.request(route, payload), payload, route)

    def test_no_task_open_is_not_a_task(self):
        for asked in ({}, {'project_id': ''}, {'project_id': None}, {'project_id': HOME}):
            self.assertEqual(robot_scope.request('robots', asked), {**asked, 'project_id': HOME})

    def test_nothing_a_task_makes_is_kept_at_the_home(self):
        for route in ('applications', 'jobs', 'scenes', 'skills', 'engineering/work', 'engineering/workflows', 'projects/remove'):
            with self.assertRaisesRegex(ValueError, '请先进入一个任务'):
                robot_scope.request(route, {'project_id': HOME, 'name': 'x'})

    def test_what_a_session_used_and_is_gone_is_said_in_plain_words(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = open_platform(self, tmp.name).store
        store.put('agent_models', {'id': 'model-1', 'project_id': 'project-a'})

        self.assertEqual(robot_scope.kept(lambda: store.get('agent_models', 'model-1'))['id'], 'model-1')
        with self.assertRaisesRegex(ValueError, '已经不在了.*重新开始'):
            robot_scope.kept(lambda: store.get('agent_models', 'model-of-a-deleted-task'))
        with self.assertRaisesRegex(ValueError, '已停用'):       # any other refusal is passed on as it is
            robot_scope.kept(lambda: (_ for _ in ()).throw(ValueError('会话所用 Skill 已停用；请重新启用或新建任务')))


class OverHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(self.tmp.name)
        self.server = Server(make_handler(self.app))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.app.close)
        self.addCleanup(self.server.close)
        form = {'name': 'Workspace', 'email': 'a@example.test', 'password': 'pw' + secrets.token_hex(8) + '7'}
        code = self.request('/api/register/start', form)[1]['dev_code']
        self.cookie = self.request('/api/register', {**form, 'code': code})[2].split(';')[0]

    def request(self, path, payload=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.http.server_port)
        headers = {'Cookie': getattr(self, 'cookie', ''), **({'Content-Type': 'application/json'} if payload is not None else {})}
        conn.request('POST' if payload is not None else 'GET', path,
                     json.dumps(payload) if payload is not None else None, headers)
        response = conn.getresponse()
        result = (response.status, json.loads(response.read()), response.getheader('Set-Cookie'))
        conn.close()
        return result

    def post(self, path, payload):
        status, body, _ = self.request('/api/' + path, payload)
        self.assertEqual(status, 200, body)
        return body

    def state(self):
        return self.request('/api/state')[1]

    def task(self, name):
        return self.post('projects', {'name': name, 'objective': 'Inspect the site'})['id']

    def test_a_robot_added_in_one_task_is_the_accounts(self):
        a, b = self.task('Site A'), self.task('Site B')

        robot = self.post('robots', {'project_id': a, 'name': 'Patrol 1', 'family': 'quadruped'})

        state = self.state()
        self.assertEqual(robot['project_id'], state['robot_home'])
        self.assertEqual([(r['name'], r['project_id']) for r in state['robots']], [('Patrol 1', state['robot_home'])])
        self.assertEqual(sorted(p['id'] for p in state['projects']), sorted([a, b]))

    def test_another_task_works_with_the_same_robot(self):
        a, b = self.task('Site A'), self.task('Site B')
        robot = self.post('robots', {'project_id': a, 'name': 'Patrol 1', 'family': 'quadruped'})['id']

        self.post('robots/body-type', {'project_id': b, 'robot_id': robot, 'family': 'manipulator'})
        detail = self.post('robots/detail', {'project_id': b, 'robot_id': robot})

        self.assertEqual(detail['robot']['family'], 'manipulator')

    def test_a_robot_can_be_added_with_no_task(self):
        robot = self.post('robots', {'name': 'Patrol 1', 'family': 'quadruped'})

        state = self.state()
        self.assertEqual(state['projects'], [])
        self.assertEqual([r['id'] for r in state['robots']], [robot['id']])

    def test_deleting_the_task_leaves_the_robot(self):
        a, b = self.task('Site A'), self.task('Site B')
        robot = self.post('robots', {'project_id': a, 'name': 'Patrol 1', 'family': 'quadruped'})['id']

        self.post('projects/remove', {'project_id': a})

        state = self.state()
        self.assertEqual([p['id'] for p in state['projects']], [b])
        self.assertEqual([r['id'] for r in state['robots']], [robot])
        self.post('robots/remove', {'project_id': b, 'robot_id': robot})
        self.assertEqual(self.state()['robots'], [])

    def test_the_home_is_not_offered_and_not_deleted(self):
        self.task('Site A')
        home = self.state()['robot_home']

        status, body, _ = self.request('/api/projects/remove', {'project_id': home})

        self.assertEqual(status, 400, body)

    def test_a_simulated_robot_is_the_accounts_and_its_trial_goes_on_in_a_task(self):
        a = self.task('Site A')

        robot = self.post('sims', {'kind': 'quadruped', 'project_id': a})
        self.addCleanup(lambda: self.request('/api/sims/remove', {'robot_id': robot['id']}))

        state = self.state()
        self.assertEqual(robot['project_id'], a)             # where the page goes on
        self.assertEqual([r['project_id'] for r in state['robots']], [state['robot_home']])
        self.post('robots/live', {'project_id': self.task('Site B'), 'robot_id': robot['id']})

    def test_a_first_simulated_robot_still_opens_a_trial_task(self):
        robot = self.post('sims', {'kind': 'quadruped'})
        self.addCleanup(lambda: self.request('/api/sims/remove', {'robot_id': robot['id']}))

        state = self.state()
        self.assertEqual([p['id'] for p in state['projects']], [robot['project_id']])
        self.assertEqual([r['project_id'] for r in state['robots']], [state['robot_home']])


if __name__ == '__main__':
    unittest.main()
