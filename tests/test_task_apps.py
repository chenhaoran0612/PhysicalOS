"""Issue #50: 任务工作台 binds robot applications; it does not make them.

Rules under test (brainos/task_apps.py), as decided with the owner on 2026-11-26:

* an application belongs to the account, not to the task it was made in: it is made and changed on
  「机器人应用」 and every task sees it;
* a task binds applications, many to many; unbinding removes the relation and nothing else;
* a task is for one brand and model: the first application bound to it says which, and only
  applications of the same one can be bound after that.

The devices are the HTTP contract fixture of tests/test_product.py; no robot is contacted.
"""
import http.client
import json
import hashlib
import os
os.environ.setdefault('NEUXMIND_MAIL_DEV_ECHO', '1')  # registration codes come back in the API response
import secrets
import shutil
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path

from brainos import robot_caps, robot_model, robot_scope, task_apps
from brainos.server import Application, make_handler
from brainos.service import Platform, now
from test_product import Server
from test_robot_model import Case as ModelCase

HOME = robot_scope.HOME
ROOT = Path(__file__).resolve().parents[1]
LINE = '<link rel="stylesheet" href="/task_apps.css"><script src="/task_apps.js" defer></script>'


def recomputed(application):
    """The digest a device computes over the application it is sent (runtime ``v1_validate``)."""
    body = {k: v for k, v in application.items() if k != 'digest'}
    return hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()


class Case(ModelCase):
    def setUp(self):
        super().setUp()
        self.task_id = self.pid                     # the task of the base class
        self.pid = HOME                             # robots, skills and applications are made for the account

    def task(self, name='Other task'):
        return self.app.add_project({'name': name, 'objective': 'What the robot should get done'})['id']

    def made(self, vendor='Acme', model='X1', name='Round', robot=None):
        """An application of the account for a robot of this brand and model."""
        robot = robot or self.robot(f'{vendor} {model} {name}', vendor, model)
        return self.application(robot, self.skill(vendor=vendor, model=model), name=name)

    def bind(self, application, task_id=None):
        return self.app.task_apps.bind({'project_id': task_id or self.task_id, 'application_id': application['id']})

    def unbind(self, application, task_id=None):
        return self.app.task_apps.unbind({'project_id': task_id or self.task_id, 'application_id': application['id']})

    def bound(self, task_id=None):
        return [b['application_id'] for b in self.app.store.list(task_apps.KIND) if b['project_id'] == (task_id or self.task_id)]


class Binding(Case):
    def test_a_task_binds_an_application_of_the_account(self):
        application = self.made()

        binding = self.bind(application)

        self.assertEqual((binding['project_id'], binding['application_id']), (self.task_id, application['id']))
        self.assertEqual(self.app.snapshot()['task_applications'], [binding])
        self.assertEqual(self.app.store.get('applications', application['id']), application)     # the application is not changed

    def test_binding_again_keeps_one_record(self):
        application = self.made()

        first, second = self.bind(application), self.bind(application)

        self.assertEqual(first, second)
        self.assertEqual(self.bound(), [application['id']])

    def test_an_application_can_be_bound_to_several_tasks_and_a_task_to_several_applications(self):
        robot = self.robot('Dog', 'Acme', 'X1')
        one, two, other = self.made(robot=robot), self.made(robot=robot, name='Second'), self.task()

        for application in (one, two):
            self.bind(application)
        self.bind(one, other)

        self.assertEqual(sorted(self.bound()), sorted([one['id'], two['id']]))
        self.assertEqual(self.bound(other), [one['id']])

    def test_unbinding_removes_the_relation_and_nothing_else(self):
        application, other = self.made(), self.task()
        self.bind(application)
        self.bind(application, other)

        self.unbind(application)

        self.assertEqual(self.bound(), [])
        self.assertEqual(self.bound(other), [application['id']])
        self.assertEqual(self.app.store.get('applications', application['id']), application)
        self.unbind(application)                    # not bound any more: nothing to do, not an error

    def test_a_task_is_for_the_brand_and_model_of_its_first_application(self):
        first, same, other = self.made('Acme', 'X1'), self.made('ACME', 'x1', name='Same'), self.made('Acme', 'X2', name='Other')
        self.bind(first)

        self.bind(same)                             # compared without regard to case, as issue #48 does
        error = self.refused('task_other_model', self.bind, other)

        self.assertIn('Acme X1', str(error))
        self.assertIn('Acme X2', str(error))
        self.assertEqual(sorted(self.bound()), sorted([first['id'], same['id']]))

    def test_a_task_without_applications_is_free_again(self):
        first, other = self.made('Acme', 'X1'), self.made('Acme', 'X2', name='Other')
        self.bind(first)
        self.unbind(first)

        self.bind(other)

        self.assertEqual(self.bound(), [other['id']])

    def test_an_application_without_brand_and_model_is_in_a_class_of_its_robot(self):
        # Issue #48: a robot without brand and model is in a class of its own, and so is what was made for it.
        one, two = self.robot('Old one'), self.robot('Old two')
        skill = self.skill()
        first, again, other = (self.application(one, skill, 'First'), self.application(one, skill, 'Again'),
                               self.application(two, skill, 'Other'))
        self.bind(first)

        self.bind(again)
        self.refused('task_other_model', self.bind, other)
        self.refused('task_other_model', self.bind, self.made('Acme', 'X1', name='Named'))

    def test_every_version_of_an_application_is_one_binding(self):
        robot = self.robot('Dog', 'Acme', 'X1')
        skill = self.skill(vendor='Acme', model='X1')
        first = self.application(robot, skill)
        draft = {'project_id': HOME, 'source_application_id': first['id'], 'name': first['name'], 'version': '2.0',
                 'robot_id': robot['id'], 'steps': [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 60}]}
        second = self.app.save_revision({**draft, 'preview_digest': self.app.preview_revision(draft)['preview_digest']})

        self.bind(second)
        self.bind(first)

        self.assertEqual(self.bound(), [first['id']])          # kept under the first version's id
        self.assertEqual([a['id'] for a in self.app.task_apps.applications(self.task_id)], [second['id']])   # the newest version is the one in use

    def test_what_cannot_be_bound_is_refused(self):
        application = self.made()
        for p in ({'project_id': 'project-missing', 'application_id': application['id']},
                  {'project_id': HOME, 'application_id': application['id']},            # the home is not a task
                  {'application_id': application['id']}):
            with self.assertRaises(ValueError):
                self.app.task_apps.bind(p)
        self.refused('task_app_missing', self.app.task_apps.bind, {'project_id': self.task_id, 'application_id': 'app-missing'})
        self.assertEqual(self.app.store.list(task_apps.KIND), [])

    def test_a_task_follows_when_its_application_gets_a_brand_and_model_later(self):
        # Issue #48: an application made for a robot without brand and model takes the one the robot gets (RobotModels.adopt).
        robot = self.robot('Old one')
        application = self.application(robot, self.skill())
        self.bind(application)
        self.assertIsNone(self.app.task_apps.product(self.task_id))

        self.app.robot_models.assign({'project_id': HOME, 'robot_id': robot['id'], 'vendor': 'Acme', 'model': 'X1'})

        self.assertEqual(self.app.task_apps.product(self.task_id), {'vendor': 'Acme', 'model': 'X1'})
        self.bind(self.made('Acme', 'X1', name='Same model'))
        self.refused('task_other_model', self.bind, self.made('Acme', 'X2', name='Other'))

    def test_the_brand_and_model_of_a_task_is_that_of_its_first_binding(self):
        self.assertIsNone(self.app.task_apps.product(self.task_id))
        self.bind(self.made('Acme', 'X1'))

        self.assertEqual(self.app.task_apps.product(self.task_id), {'vendor': 'Acme', 'model': 'X1'})


class AccountScope(Case):
    def test_a_request_about_applications_is_for_the_account_whichever_task_is_open(self):
        for route in ('skills', 'applications', 'applications/readiness', 'applications/revise', 'harness', 'onsite', 'onsite/poll', 'deployments'):
            self.assertEqual(robot_scope.request(route, {'project_id': self.task_id})['project_id'], HOME, route)
        # What is still a task's own: binding, and the work an assistant or a service does for it.
        for route in ('task-apps/bind', 'task-apps/unbind', 'jobs', 'models'):
            self.assertEqual(robot_scope.request(route, {'project_id': self.task_id})['project_id'], self.task_id, route)

    def test_an_application_made_while_a_task_is_open_is_the_accounts(self):
        robot = self.robot('Dog', 'Acme', 'X1')
        skill = self.app.add_skill(robot_scope.request('skills', {
            'project_id': self.task_id, 'name': 'Navigate', 'version': '1.0', 'requires': ['navigation.goal'], 'entrypoint': 'navigate',
            'success_condition': 'at goal', 'stop_condition': 'timeout', 'model_ids': [], 'vendor': 'Acme', 'model': 'X1'}))

        application = self.app.add_application(robot_scope.request('applications', {
            'project_id': self.task_id, 'name': 'Round', 'version': '1.0', 'robot_id': robot['id'],
            'steps': [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 30}]}))

        self.assertEqual((skill['project_id'], application['project_id']), (HOME, HOME))
        self.assertEqual(self.bound(), [])          # making an application binds it to nothing
        self.assertEqual(recomputed(application), application['digest'])

    def test_a_task_can_test_an_application_of_the_account(self):
        robot = self.robot('Dog', 'Acme', 'X1', reports=('Acme', 'X1'))
        application = self.made(robot=robot)
        self.bind(application)

        report = self.app.harness({'application_id': application['id']})

        self.assertEqual(report['project_id'], HOME)           # the evidence is kept with the application
        self.assertEqual(self.app.same_project('applications', application['id'], self.task_id), application)


class TaskRemoval(Case):
    def test_deleting_a_task_takes_its_bindings_and_leaves_the_applications(self):
        robot = self.robot('Dog', 'Acme', 'X1', reports=('Acme', 'X1'))
        application, other = self.made(robot=robot), self.task()
        self.bind(application)
        self.bind(application, other)
        report = self.app.harness({'application_id': application['id']})

        result = self.app.task_remove.remove({'project_id': self.task_id})

        self.assertEqual(result['deleted'].get(task_apps.KIND), 1)
        self.assertEqual(self.bound(other), [application['id']])
        self.assertEqual(self.app.store.get('applications', application['id']), application)
        self.assertEqual(self.app.store.get('evaluations', report['id']), report)

    def test_a_model_an_application_of_the_account_uses_outlives_the_task_it_was_chosen_in(self):
        used = self.app.store.put('models', {'id': 'model-used', 'project_id': self.task_id, 'name': 'Detector', 'digest': 'd' * 64, 'created_at': now()})
        unused = self.app.store.put('models', {'id': 'model-unused', 'project_id': self.task_id, 'name': 'Spare', 'digest': 'e' * 64, 'created_at': now()})
        robot = self.robot('Dog', 'Acme', 'X1')
        skill = self.skill(vendor='Acme', model='X1', model_ids=[used['id']])
        self.application(robot, skill)

        self.app.task_remove.remove({'project_id': self.task_id})

        self.assertEqual(self.app.store.get('models', used['id'])['project_id'], HOME)
        with self.assertRaises(ValueError):
            self.app.store.get('models', unused['id'])


class Migration(Case):
    """A workspace from before: applications, and what was made with them, kept in the task."""

    def setUp(self):
        super().setUp()
        self.pid = self.task_id                     # as it was: everything is made in the task
        self.robot_one = self.robot('Dog', 'Acme', 'X1', reports=('Acme', 'X1'))
        robot_scope.migrate(self.app.store)          # the robot is the account's (issue #42); the rest is the task's
        self.skill_one = self.skill(vendor='Acme', model='X1')
        self.old = self.application(self.robot_one, self.skill_one)
        self.report = self.app.harness({'application_id': self.old['id']})
        self.release = self.app.store.put('deployments', {'id': 'release-1', 'project_id': self.task_id, 'application_id': self.old['id'],
                                                          'robot_id': self.robot_one['id'], 'status': 'deployed', 'created_at': now()})
        self.started = self.app.store.put('operations', {'id': 'operation-1', 'project_id': self.task_id, 'release_id': 'release-1',
                                                         'robot_id': self.robot_one['id'], 'kind': 'start', 'created_at': now()})
        self.job = self.app.store.put('jobs', {'id': 'job-1', 'project_id': self.task_id, 'name': 'Training', 'status': 'succeeded', 'created_at': now()})

    def reopen(self):
        self.app.close()
        self.app = Platform(self.root)
        return self.app

    def test_before_the_move_everything_is_in_the_task(self):
        self.assertEqual({self.old['project_id'], self.skill_one['project_id'], self.report['project_id']}, {self.task_id})

    def test_applications_and_what_was_made_with_them_become_the_accounts(self):
        app = self.reopen()

        for kind, record in (('applications', self.old), ('skills', self.skill_one), ('evaluations', self.report),
                             ('deployments', self.release), ('operations', self.started)):
            self.assertEqual(app.store.get(kind, record['id'])['project_id'], HOME, kind)
        self.assertEqual(app.store.get('jobs', self.job['id'])['project_id'], self.task_id)    # a task's own work stays

    def test_the_task_they_were_in_has_them_bound(self):
        other = self.task()
        app = self.reopen()

        self.assertEqual(self.bound(), [self.old['id']])
        self.assertEqual(self.bound(other), [])
        self.assertEqual(app.task_apps.product(self.task_id), {'vendor': 'Acme', 'model': 'X1'})

    def test_a_moved_application_is_still_the_one_that_was_tested(self):
        app = self.reopen()
        moved = app.store.get('applications', self.old['id'])

        self.assertEqual(moved['digest'], self.old['digest'])
        self.assertEqual(moved[task_apps.MADE_IN], self.task_id)
        sent = robot_caps.typed_application(app, moved)        # what a device gets, and checks the digest of
        self.assertEqual(recomputed(sent), self.old['digest'])
        self.assertEqual(sent, self.old)
        # The evidence of before still counts.
        readiness = app.delivery_readiness({'project_id': HOME, 'application_id': moved['id']})
        self.assertEqual(readiness['static_report']['id'], self.report['id'])

    def test_a_task_with_applications_of_two_brands_keeps_both_and_takes_no_third(self):
        second = self.application(self.robot('Arm', 'Acme', 'X2'), self.skill('Grasp', vendor='Acme', model='X2'), name='Other')
        app = self.reopen()

        self.assertEqual(sorted(self.bound()), sorted([self.old['id'], second['id']]))
        self.assertEqual(app.task_apps.product(self.task_id), {'vendor': 'Acme', 'model': 'X1'})    # the older one says which
        third = app.add_application({'project_id': HOME, 'name': 'Third', 'version': '1.0', 'robot_id': second['robot_id'],
                                     'steps': [{'skill_id': second['steps'][0]['skill_id'], 'parameters': {}, 'timeout_s': 30}]})
        self.refused('task_other_model', app.task_apps.bind, {'project_id': self.task_id, 'application_id': third['id']})

    def test_it_is_done_once_and_keeps_the_order_of_the_lists(self):
        second = self.application(self.robot_one, self.skill_one, name='Second')
        before = [a['id'] for a in self.app.store.list('applications')]
        app = self.reopen()
        after_first = {kind: app.store.list(kind) for kind in ('applications', 'skills', 'evaluations', task_apps.KIND)}

        self.assertEqual(task_apps.migrate(app.store), {})
        app = self.reopen()

        self.assertEqual([a['id'] for a in app.store.list('applications')], before)
        self.assertEqual({kind: app.store.list(kind) for kind in after_first}, after_first)
        self.assertEqual(len(self.bound()), 2)
        self.assertIn(second['id'], self.bound())

    def test_an_application_whose_task_is_gone_is_the_accounts_and_bound_to_nothing(self):
        orphan = self.app.store.put('applications', {**self.old, 'id': 'app-orphan', 'project_id': 'project-gone'})
        app = self.reopen()

        self.assertEqual(app.store.get('applications', orphan['id'])['project_id'], HOME)
        self.assertEqual(self.bound('project-gone'), [])


class OverHttp(unittest.TestCase):
    """As the page does it: a signed-in account, the real routes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Application(self.tmp.name)
        self.addCleanup(self.app.close)
        self.server = Server(make_handler(self.app))
        self.addCleanup(self.server.close)
        self.host = self.server.url.removeprefix('http://')

    def call(self, path, payload=None, cookie=None):
        connection = http.client.HTTPConnection(self.host, timeout=20)
        headers = {'Content-Type': 'application/json', **({'Cookie': cookie} if cookie else {})}
        connection.request('POST' if payload is not None else 'GET', '/api/' + path,
                           json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        body = json.loads(response.read())
        set_cookie = response.getheader('Set-Cookie')
        connection.close()
        return response.status, body, set_cookie

    def account(self, email):
        form = {'name': email.split('@')[0], 'email': email, 'password': 'Aa1-' + secrets.token_hex(8)}
        _, sent, _ = self.call('register/start', form)
        status, body, cookie = self.call('register', {**form, 'code': sent['dev_code']})
        self.assertEqual(status, 200, body)
        return cookie.split(';')[0]

    def ok(self, path, payload, cookie):
        status, body, _ = self.call(path, payload, cookie)
        self.assertEqual(status, 200, body)
        return body

    def made(self, cookie, task_id, vendor='Acme', model='X1', name='Round'):
        """An application made the way the page makes one while the task ``task_id`` is open."""
        robot = self.ok('robots', {'project_id': task_id, 'name': f'{name} robot', 'family': 'quadruped', 'vendor': vendor, 'model': model}, cookie)
        skill = self.ok('skills', {'project_id': task_id, 'robot_id': robot['id'], 'name': 'Navigate', 'version': '1.0',
                                   'requires': ['navigation.goal'], 'entrypoint': 'navigate', 'success_condition': 'at goal',
                                   'stop_condition': 'timeout', 'model_ids': []}, cookie)
        return self.ok('applications', {'project_id': task_id, 'name': name, 'version': '1.0', 'robot_id': robot['id'],
                                        'steps': [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 30}]}, cookie)

    def test_a_task_binds_what_the_account_made_and_no_other_brand_and_model(self):
        me = self.account('first@example.test')
        task = self.ok('projects', {'name': '仓库巡检', 'objective': '巡检'}, me)
        other = self.ok('projects', {'name': '夜间巡检', 'objective': '再巡一圈'}, me)

        application = self.made(me, task['id'])
        different = self.made(me, other['id'], model='X2', name='Other')

        self.assertEqual({application['project_id'], different['project_id']}, {HOME})     # made while a task was open: the account's
        self.assertEqual(self.call('state', cookie=me)[1]['task_applications'], [])
        for task_id in (task['id'], other['id']):
            self.ok('task-apps/bind', {'project_id': task_id, 'application_id': application['id']}, me)
        status, body, _ = self.call('task-apps/bind', {'project_id': task['id'], 'application_id': different['id']}, me)
        self.assertEqual((status, body['code']), (400, 'task_other_model'), body)
        state = self.call('state', cookie=me)[1]
        self.assertEqual(sorted((b['project_id'], b['application_id']) for b in state['task_applications']),
                         sorted((t['id'], application['id']) for t in (task, other)))

        self.assertTrue(self.ok('task-apps/unbind', {'project_id': task['id'], 'application_id': application['id']}, me)['unbound'])
        self.ok('task-apps/bind', {'project_id': task['id'], 'application_id': different['id']}, me)
        self.ok('projects/remove', {'project_id': other['id']}, me)
        state = self.call('state', cookie=me)[1]
        self.assertEqual([(b['project_id'], b['application_id']) for b in state['task_applications']], [(task['id'], different['id'])])
        self.assertEqual(len(state['applications']), 2)

    def test_an_application_can_be_made_with_no_task_open(self):
        me = self.account('first@example.test')

        application = self.made(me, '')

        self.assertEqual(application['project_id'], HOME)

    def test_one_account_cannot_bind_in_or_from_another(self):
        first, second = self.account('first@example.test'), self.account('second@example.test')
        mine = self.ok('projects', {'name': '甲的任务', 'objective': '巡检'}, first)
        theirs = self.ok('projects', {'name': '乙的任务', 'objective': '搬运'}, second)
        application = self.made(first, mine['id'])

        for task_id in (mine['id'], theirs['id']):          # the other account has neither that task nor that application
            status, body, _ = self.call('task-apps/bind', {'project_id': task_id, 'application_id': application['id']}, second)
            self.assertEqual(status, 400, body)
        self.assertEqual(self.call('task-apps/bind', {'project_id': mine['id'], 'application_id': application['id']})[0], 401)
        self.assertEqual(self.call('state', cookie=second)[1]['task_applications'], [])
        self.assertEqual(self.call('state', cookie=first)[1]['task_applications'], [])

    def test_the_page_files_are_served_and_loaded_once(self):
        for path, mime in (('/task_apps.js', 'text/javascript'), ('/task_apps.css', 'text/css')):
            with urllib.request.urlopen(self.server.url + path, timeout=10) as response:
                self.assertTrue(response.headers.get('Content-Type', '').startswith(mime), path)
                self.assertEqual(response.read(), (ROOT / 'web' / path[1:]).read_bytes())
        with urllib.request.urlopen(self.server.url + '/', timeout=10) as response:
            index = response.read().decode('utf-8')
        self.assertEqual(index.count(LINE), 1)
        # It takes over what these two draw, so it is loaded after them.
        for earlier in ('/task_workbench.js', '/task_skills.js', '/robot_model.js'):
            self.assertGreater(index.index(LINE), index.index(earlier), earlier)


class PageTests(unittest.TestCase):
    def test_the_page_script_parses_and_its_pure_functions_hold(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        result = subprocess.run([node, 'tests/task_apps_logic.cjs'], capture_output=True, text=True, timeout=120, cwd=str(ROOT))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'task_apps_logic: \d+ checks passed')

    def test_the_page_and_the_server_compare_brand_and_model_the_same_way(self):
        # The dialog says beforehand what the server will refuse: both ignore case, and both fall back to the robot.
        script = (ROOT / 'web' / 'task_apps.js').read_text(encoding='utf-8')
        self.assertIn("'product:' + p.vendor.toLowerCase() + '\\n' + p.model.toLowerCase() : 'robot:' + (app.robot_id || '')", script)
        with_product = {'robot_id': 'r', 'product': {'vendor': 'Acme', 'model': 'X1'}}
        self.assertEqual(task_apps.kind_of(with_product), ('product', ('acme', 'x1')))
        self.assertEqual(task_apps.kind_of({'robot_id': 'r'}), ('robot', 'r'))


if __name__ == '__main__':
    unittest.main()
