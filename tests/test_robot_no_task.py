"""Issue #56: adding a robot does not create a task, and does not need one.

Robots belong to the account (brainos/robot_scope.py). A task is made by a person, who chooses its
robot when creating it (brainos/task_robot.py). Asked through the server with no task:
  * a simulated robot is added and started, and no trial task appears;
  * a robot is added by its address;
  * the assistant's models (what an investigation of a robot runs on) are kept with the robots.
What a task makes still needs a task.
"""
import http.client
import json
import os
os.environ.setdefault('NEUXMIND_MAIL_DEV_ECHO', '1')  # registration codes come back in the API response
import secrets
import tempfile
import unittest

from brainos import robot_scope
from brainos.server import Application, make_handler
from brainos.service import Platform
from test_product import Server


class Scope(unittest.TestCase):
    def test_the_assistants_models_are_asked_for_the_home_when_no_task_is_open(self):
        for route in robot_scope.NO_TASK_ROUTES:
            for nothing in ({}, {'project_id': ''}, {'project_id': None}):
                self.assertEqual(robot_scope.request(route, {**nothing, 'name': 'm'})['project_id'], robot_scope.HOME, route)

    def test_with_a_task_open_they_are_the_tasks(self):
        for route in robot_scope.NO_TASK_ROUTES:
            scoped = robot_scope.request(route, {'project_id': 'project-a'})
            self.assertEqual(scoped, {'project_id': 'project-a'})

    def test_the_home_cannot_be_named_for_them(self):
        for route in robot_scope.NO_TASK_ROUTES:
            with self.assertRaisesRegex(ValueError, robot_scope.NOT_HERE):
                robot_scope.request(route, {'project_id': robot_scope.HOME})

    def test_what_a_task_makes_is_not_moved_to_the_home(self):
        for route in ('jobs', 'models', 'datasets', 'engineering/workflows', 'projects/remove'):
            self.assertNotIn('project_id', robot_scope.request(route, {}))
            self.assertEqual(robot_scope.request(route, {'project_id': ''})['project_id'], '')


class Profiles(unittest.TestCase):
    """Where a simulated robot of a built-in profile is kept (brainos/sim_profiles.py)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.platform = Platform(tmp.name)
        self.addCleanup(self.platform.close)

    def tasks(self):
        return robot_scope.tasks(self.platform.store.list('projects'))

    def test_no_task_is_made_for_a_robot_of_a_profile(self):
        task, owner = self.platform.sim_profiles._task({'project_id': robot_scope.HOME})

        self.assertEqual((task, owner), (None, robot_scope.HOME))
        self.assertEqual(self.tasks(), [])

    def test_the_open_task_is_where_the_page_goes_on(self):
        made = self.platform.add_project({'name': 'Site A', 'objective': 'Inspect the site'})

        task, owner = self.platform.sim_profiles._task({'project_id': robot_scope.HOME, robot_scope.TASK_FIELD: made['id']})

        self.assertEqual((task['id'], owner), (made['id'], robot_scope.HOME))
        self.assertEqual([t['id'] for t in self.tasks()], [made['id']])


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

    def sim(self, **more):
        robot = self.post('sims', {'kind': 'quadruped', **more})
        self.addCleanup(lambda: self.request('/api/sims/remove', {'robot_id': robot['id']}))
        return robot

    def test_a_simulated_robot_is_added_and_no_task_is_made(self):
        robot = self.sim()

        state = self.state()
        self.assertEqual(state['projects'], [])
        self.assertEqual([(r['id'], r['project_id'], r['status']) for r in state['robots']],
                         [(robot['id'], state['robot_home'], 'connected')])
        self.assertFalse(robot['project_id'])                # no task for the page to go on in

    def test_a_second_one_does_not_make_a_task_either(self):
        self.sim()
        self.sim()

        state = self.state()
        self.assertEqual(state['projects'], [])
        self.assertEqual(len(state['robots']), 2)

    def test_a_robot_without_a_task_can_be_used_and_removed(self):
        robot = self.sim()

        live = self.post('robots/live', {'robot_id': robot['id'], 'project_id': ''})
        self.post('robots/stop', {'robot_id': robot['id'], 'project_id': ''})
        self.post('sims/remove', {'robot_id': robot['id']})

        self.assertIn('state', live)
        self.assertEqual((self.state()['robots'], self.state()['projects']), ([], []))

    def test_a_task_made_afterwards_is_bound_to_the_robot(self):
        robot = self.sim()

        task = self.post('projects', {'name': 'Site A', 'objective': 'Inspect the site', 'robot_id': robot['id']})

        state = self.state()
        self.assertEqual([(p['id'], p.get('robot_id')) for p in state['projects']], [(task['id'], robot['id'])])
        self.assertEqual([r['project_id'] for r in state['robots']], [state['robot_home']])

    def test_a_model_for_the_assistant_is_kept_with_the_robots_when_there_is_no_task(self):
        model = self.post('engineering/models', {'name': 'Team model', 'model': 'm-1', 'project_id': '',
                                                 'base_url': 'https://models.example.test/v1', 'api_key': secrets.token_hex(12)})

        state = self.state()
        self.assertEqual(model['project_id'], state['robot_home'])
        self.assertEqual(state['projects'], [])        # (the state lists TokenFans models only, issue #4)

    def test_with_a_task_open_the_model_is_the_tasks(self):
        task = self.post('projects', {'name': 'Site A', 'objective': 'Inspect the site'})['id']

        model = self.post('engineering/models', {'name': 'Team model', 'model': 'm-1', 'project_id': task,
                                                 'base_url': 'https://models.example.test/v1', 'api_key': secrets.token_hex(12)})

        self.assertEqual(model['project_id'], task)

    def test_what_a_task_makes_still_needs_a_task(self):
        status, body, _ = self.request('/api/jobs', {'name': 'Train', 'project_id': ''})

        self.assertEqual(status, 400, body)
        self.assertEqual(self.state()['projects'], [])


if __name__ == '__main__':
    unittest.main()
