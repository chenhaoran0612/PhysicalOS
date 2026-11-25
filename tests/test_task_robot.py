"""Issue #42 (third part): when a task is created, a person chooses the robot it is done with.

The task keeps the robot's id (brainos/task_robot.py). Several tasks can name the same robot; the
choice can be left open and changed later. Robots here are records; no device is contacted.
"""
import http.client
import json
import os
os.environ.setdefault('NEUXMIND_MAIL_DEV_ECHO', '1')  # registration codes come back in the API response
import secrets
import tempfile
import unittest

from brainos import robot_scope, task_robot
from brainos.server import Application, make_handler
from brainos.service import Platform
from test_product import Server


class Choice(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.platform = Platform(tmp.name)
        self.addCleanup(self.platform.close)
        add = lambda name: self.platform.add_robot({'project_id': robot_scope.HOME, 'name': name, 'family': 'quadruped'})['id']
        self.dog, self.other = add('Patrol 1'), add('Patrol 2')

    def task(self, **more):
        return self.platform.add_project({'name': 'Site A', 'objective': 'Inspect the site', **more})

    def saved(self, task):
        return self.platform.store.get('projects', task['id']).get('robot_id')

    def test_a_task_is_created_with_its_robot(self):
        task = self.task(robot_id=self.dog)

        self.assertEqual((task['robot_id'], self.saved(task)), (self.dog, self.dog))

    def test_the_choice_can_be_left_open(self):
        for nothing in ({}, {'robot_id': ''}, {'robot_id': None}):
            task = self.task(**nothing)
            self.assertNotIn('robot_id', task)
            self.assertIsNone(self.saved(task))

    def test_what_is_not_a_robot_is_refused_and_no_task_is_made(self):
        for wrong in ('robot-nobody', 7, ['x'], {'id': self.dog}):
            with self.assertRaisesRegex(ValueError, '找不到所选的机器人'):
                self.task(robot_id=wrong)
        self.assertEqual(robot_scope.tasks(self.platform.store.list('projects')), [])

    def test_two_tasks_can_be_done_with_the_same_robot(self):
        first, second = self.task(robot_id=self.dog), self.task(robot_id=self.dog)

        self.assertEqual((self.saved(first), self.saved(second)), (self.dog, self.dog))
        self.assertEqual(self.platform.store.get('robots', self.dog)['project_id'], robot_scope.HOME)

    def test_the_robot_of_a_task_can_be_changed_and_cleared(self):
        task = self.task(robot_id=self.dog)

        changed = task_robot.choose(self.platform, {'project_id': task['id'], 'robot_id': self.other})
        self.assertEqual((changed['robot_id'], self.saved(task)), (self.other, self.other))
        cleared = task_robot.choose(self.platform, {'project_id': task['id'], 'robot_id': ''})
        self.assertNotIn('robot_id', cleared)
        self.assertIsNone(self.saved(task))
        self.assertEqual(cleared['name'], 'Site A')         # nothing else of the task changed
        chosen = [e for e in self.platform.store.list('events') if e['action'] == 'project.robot_chosen']
        self.assertEqual([(e['details']['before'], e['details']['robot_id']) for e in reversed(chosen)],
                         [(self.dog, self.other), (self.other, None)])

    def test_a_refused_change_leaves_the_choice(self):
        task = self.task(robot_id=self.dog)

        with self.assertRaisesRegex(ValueError, '找不到所选的机器人'):
            task_robot.choose(self.platform, {'project_id': task['id'], 'robot_id': 'robot-nobody'})
        self.assertEqual(self.saved(task), self.dog)

    def test_only_a_task_has_a_robot(self):
        for key in (robot_scope.HOME, 'project-nobody', None):
            with self.assertRaisesRegex(ValueError, '任务已经不在了'):
                task_robot.choose(self.platform, {'project_id': key, 'robot_id': self.dog})
        self.assertNotIn('robot_id', self.platform.project(robot_scope.HOME))

    def test_a_deleted_robot_stays_named_until_another_is_chosen(self):
        task = self.task(robot_id=self.dog)
        self.platform.robot_remove.remove({'project_id': robot_scope.HOME, 'robot_id': self.dog})

        self.assertEqual(self.saved(task), self.dog)        # the page says it is gone and offers to choose
        task_robot.choose(self.platform, {'project_id': task['id'], 'robot_id': self.other})
        self.assertEqual(self.saved(task), self.other)

    def test_the_task_with_a_robot_can_be_deleted_and_the_robot_stays(self):
        task = self.task(robot_id=self.dog)

        self.platform.task_remove.remove({'project_id': task['id']})

        self.assertEqual({r['id'] for r in self.platform.store.list('robots')}, {self.dog, self.other})


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

    def tasks(self):
        return {p['name']: p.get('robot_id') for p in self.request('/api/state')[1]['projects']}

    def test_the_form_of_a_new_task_carries_the_robot(self):
        robot = self.request('/api/robots', {'name': 'Patrol 1', 'family': 'quadruped'})[1]['id']

        status, task, _ = self.request('/api/projects', {'name': 'Site A', 'objective': 'Inspect', 'robot_id': robot})
        self.assertEqual((status, task['robot_id']), (200, robot))
        self.assertEqual(self.request('/api/projects', {'name': 'Site B', 'objective': 'Carry', 'robot_id': ''})[0], 200)
        status, refusal, _ = self.request('/api/projects', {'name': 'Site C', 'objective': 'Clean', 'robot_id': 'robot-nobody'})
        self.assertEqual(status, 400, refusal)
        self.assertIn('找不到所选的机器人', refusal['error'])

        self.assertEqual(self.tasks(), {'Site A': robot, 'Site B': None})

    def test_the_robot_is_changed_from_the_page_of_the_task(self):
        first = self.request('/api/robots', {'name': 'Patrol 1', 'family': 'quadruped'})[1]['id']
        second = self.request('/api/robots', {'name': 'Arm 1', 'family': 'manipulator'})[1]['id']
        task = self.request('/api/projects', {'name': 'Site A', 'objective': 'Inspect', 'robot_id': first})[1]['id']

        status, changed, _ = self.request('/api/projects/robot', {'project_id': task, 'robot_id': second})
        self.assertEqual((status, changed['robot_id']), (200, second))
        self.assertEqual(self.tasks(), {'Site A': second})
        home = self.request('/api/state')[1]['robot_home']
        self.assertEqual(self.request('/api/projects/robot', {'project_id': home, 'robot_id': second})[0], 400)


if __name__ == '__main__':
    unittest.main()
