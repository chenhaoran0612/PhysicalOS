"""Issue #68: an application is made for a task and a brand and model; that model's skills are ready.

Rules under test (brainos/model_skills.py), as decided with the owner on 2026-11-27:

* every skill of a brand and model can be put into an application without installing or adding it
  first: the skills on its connected robots and the skills that come with the app and fit it are
  kept as skill records when the form asks for them;
* an application is made for a task: it is created and bound to the task in one request, and refused
  as a whole when the task is for another brand and model;
* a skill of the application that the robot does not have yet is installed on it before an on-site
  test, when it comes with the app.

The devices are the HTTP contract fixture of tests/test_product.py. What a robot has installed and
what the app's skill library holds are given by the test (the module's three seams); no robot is contacted.
"""
import unittest
from pathlib import Path
from unittest import mock

from brainos import model_skills, robot_model, task_apps
from test_task_apps import Case as TaskCase

ROOT = Path(__file__).resolve().parents[1]
LINE = '<link rel="stylesheet" href="/app_create.css"><script src="/app_create.js" defer></script>'


def on_robot(key='patrol', version='1.0.0', title='巡逻', requires=('navigation.goal',)):
    return {'id': key, 'version': version, 'title': title, 'requires': list(requires), 'parameters': []}


def in_library(key='wave', version='1.0.0', title='挥手', requires=('navigation.goal',)):
    return {**on_robot(key, version, title, requires), 'example': key, 'package_digest': 'd' * 64}


class Case(TaskCase):
    def setUp(self):
        super().setUp()
        self.installed, self.library, self.installs = {}, [], []
        self.install_result = {'status': 'succeeded'}
        for name, stand_in in (('installed_on', lambda robot: self.installed.get(robot['id'])),
                               ('library_for', lambda robot: list(self.library)),
                               ('install_example', self.install)):
            patch = mock.patch.object(model_skills, name, stand_in)
            patch.start()
            self.addCleanup(patch.stop)

    def install(self, platform, robot, item):
        self.installs.append((robot['id'], item['example']))
        return dict(self.install_result)

    def connected(self, name='Dog', vendor='Acme', model='X1'):
        return self.robot(name, reports=(vendor, model))

    def ready(self, robot):
        return self.app.model_skills.ready({'project_id': self.pid, 'robot_id': robot['id']})

    def names(self, result):
        return sorted(s['name'] for s in result['skills'])

    def steps(self, skill):
        return [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 30}]


class Ready(Case):
    def test_skills_on_a_connected_robot_become_skills_of_its_brand_and_model(self):
        robot = self.connected()
        self.installed[robot['id']] = [on_robot()]

        result = self.ready(robot)

        self.assertEqual(self.names(result), ['巡逻'])
        saved = self.app.store.get('skills', result['skills'][0]['id'])
        self.assertEqual((saved['entrypoint'], saved['version'], saved['requires']), ('patrol', '1.0.0', ['navigation.goal']))
        self.assertEqual(robot_model.of(saved), {'vendor': 'Acme', 'model': 'X1'})
        self.assertEqual(result['added'], 1)

    def test_skills_that_come_with_the_app_and_fit_are_ready_without_installing(self):
        robot = self.connected()
        self.installed[robot['id']] = []
        self.library = [in_library()]

        result = self.ready(robot)

        self.assertEqual(self.names(result), ['挥手'])
        self.assertEqual(self.installs, [])                       # nothing is sent to the robot for this

    def test_a_skill_both_on_the_robot_and_in_the_library_is_kept_once(self):
        robot = self.connected()
        self.installed[robot['id']] = [on_robot('wave', title='挥手')]
        self.library = [in_library()]

        self.assertEqual(self.names(self.ready(robot)), ['挥手'])

    def test_asking_again_adds_nothing(self):
        robot = self.connected()
        self.installed[robot['id']] = [on_robot()]
        self.library = [in_library()]

        first, second = self.ready(robot), self.ready(robot)

        self.assertEqual((first['added'], second['added']), (2, 0))
        self.assertEqual(len(self.app.store.list('skills')), 2)
        self.assertEqual(self.names(second), ['巡逻', '挥手'])

    def test_every_connected_robot_of_the_brand_and_model_is_read(self):
        one, two = self.connected('One'), self.connected('Two')
        self.installed[one['id']] = [on_robot()]
        self.installed[two['id']] = [on_robot('goto', title='去地点')]

        self.assertEqual(self.names(self.ready(one)), ['去地点', '巡逻'])

    def test_skills_of_another_brand_and_model_are_not_among_them(self):
        mine, other = self.connected(), self.connected('Arm', 'Other', 'O1')
        self.installed[mine['id']] = [on_robot()]
        self.installed[other['id']] = [on_robot('grasp', title='抓取')]
        self.ready(other)

        self.assertEqual(self.names(self.ready(mine)), ['巡逻'])

    def test_a_robot_that_cannot_be_read_is_named_and_the_rest_goes_on(self):
        good, silent = self.connected('Good'), self.connected('Silent')
        self.installed[good['id']] = [on_robot()]                  # nothing is known about the other one

        result = self.ready(good)

        self.assertEqual(self.names(result), ['巡逻'])
        self.assertEqual(result['unread'], ['Silent'])

    def test_a_robot_that_is_not_connected_is_not_asked(self):
        robot = self.robot('Offline', 'Acme', 'X1')
        self.library = [in_library()]                               # what fits cannot be judged without a connected robot
        registered = self.skill(vendor='Acme', model='X1')

        result = self.ready(robot)

        self.assertEqual([s['id'] for s in result['skills']], [registered['id']])
        self.assertEqual((result['added'], result['connected']), (0, 0))

    def test_a_robot_with_no_brand_and_model_seeds_nothing(self):
        plain = self.robot('Old', reports=())
        self.installed[plain['id']] = [on_robot()]
        self.library = [in_library()]

        result = self.ready(plain)

        self.assertEqual((result['added'], result['product']), (0, None))
        self.assertEqual(self.app.store.list('skills'), [])         # nothing that every other robot would be offered

    def test_one_skill_a_device_describes_badly_does_not_stop_the_others(self):
        robot = self.connected()
        self.installed[robot['id']] = [on_robot('long', title='x' * 150), on_robot('bare', title='无要求', requires=()), on_robot()]

        result = self.ready(robot)

        self.assertEqual(self.names(result), ['巡逻'])
        self.assertEqual(sorted(result['skipped']), sorted(['x' * 100, '无要求']))

    def test_a_robot_of_another_account_or_none_is_refused(self):
        with self.assertRaises(ValueError):
            self.app.model_skills.ready({'project_id': self.pid, 'robot_id': 'robot-nowhere'})


class Create(Case):
    def test_an_application_is_created_and_bound_to_its_task(self):
        robot = self.connected()
        skill = self.skill(vendor='Acme', model='X1')

        application = self.app.model_skills.create({'project_id': self.pid, 'task_id': self.task_id, 'name': 'Round',
                                                    'version': '1.0', 'robot_id': robot['id'], 'steps': self.steps(skill)})

        self.assertEqual(self.app.store.get('applications', application['id']), application)
        self.assertEqual(self.bound(), [application['id']])
        self.assertEqual(robot_model.of(application), {'vendor': 'Acme', 'model': 'X1'})

    def test_without_a_task_nothing_is_created(self):
        robot = self.connected()
        skill = self.skill(vendor='Acme', model='X1')

        for task in (None, '', 'project-nowhere', self.pid):       # the home of the robots is not a task
            with self.assertRaises(ValueError):
                self.app.model_skills.create({'project_id': self.pid, 'task_id': task, 'name': 'Round', 'version': '1.0',
                                              'robot_id': robot['id'], 'steps': self.steps(skill)})
        self.assertEqual(self.app.store.list('applications'), [])

    def test_a_task_of_another_brand_and_model_refuses_it_and_nothing_is_created(self):
        self.bind(self.made('Other', 'O1'))
        before = len(self.app.store.list('applications'))
        robot = self.connected()
        skill = self.skill('Walk', vendor='Acme', model='X1')

        self.refused('task_other_model', self.app.model_skills.create,
                     {'project_id': self.pid, 'task_id': self.task_id, 'name': 'Round', 'version': '1.0',
                      'robot_id': robot['id'], 'steps': self.steps(skill)})

        self.assertEqual(len(self.app.store.list('applications')), before)
        self.assertEqual(len(self.bound()), 1)

    def test_a_binding_that_fails_leaves_no_application(self):
        robot = self.connected()
        skill = self.skill(vendor='Acme', model='X1')

        with mock.patch.object(self.app.task_apps, 'bind', side_effect=RuntimeError('the store failed')):
            with self.assertRaisesRegex(RuntimeError, 'the store failed'):
                self.app.model_skills.create({'project_id': self.pid, 'task_id': self.task_id, 'name': 'Round',
                                              'version': '1.0', 'robot_id': robot['id'], 'steps': self.steps(skill)})

        self.assertEqual(self.app.store.list('applications'), [])
        self.assertEqual(self.bound(), [])

    def test_an_application_that_cannot_be_saved_binds_nothing(self):
        robot = self.connected()

        with self.assertRaises(ValueError):
            self.app.model_skills.create({'project_id': self.pid, 'task_id': self.task_id, 'name': 'Round',
                                          'version': '1.0', 'robot_id': robot['id'], 'steps': []})
        self.assertEqual(self.bound(), [])


class InstallMissing(Case):
    def made_with(self, key='wave'):
        robot = self.connected()
        skill = self.skill('挥手', vendor='Acme', model='X1', entrypoint=key, version='1.0.0')
        return robot, self.application(robot, skill)

    def test_a_library_skill_the_robot_lacks_is_installed(self):
        robot, application = self.made_with()
        self.installed[robot['id']] = []
        self.library = [in_library()]

        done = self.app.model_skills.install_missing(application, robot['id'])

        self.assertEqual(self.installs, [(robot['id'], 'wave')])
        self.assertEqual(done, ['挥手'])

    def test_a_skill_the_robot_has_is_left_alone(self):
        robot, application = self.made_with()
        self.installed[robot['id']] = [on_robot('wave', version='0.9.0')]      # another version: the robot's own is kept
        self.library = [in_library()]

        self.assertEqual(self.app.model_skills.install_missing(application, robot['id']), [])
        self.assertEqual(self.installs, [])

    def test_a_skill_that_does_not_come_with_the_app_is_left_to_the_device(self):
        robot, application = self.made_with('custom')
        self.installed[robot['id']] = []
        self.library = [in_library()]

        self.assertEqual(self.app.model_skills.install_missing(application, robot['id']), [])
        self.assertEqual(self.installs, [])

    def test_a_robot_that_cannot_be_read_gets_nothing(self):
        robot, application = self.made_with()
        self.library = [in_library()]

        self.assertEqual(self.app.model_skills.install_missing(application, robot['id']), [])
        self.assertEqual(self.installs, [])

    def test_an_install_that_fails_stops_with_the_reason(self):
        robot, application = self.made_with()
        self.installed[robot['id']] = []
        self.library = [in_library()]
        self.install_result = {'status': 'rejected', 'error': '设备拒绝安装：磁盘已满'}

        error = self.refused('skill_auto_install_failed', self.app.model_skills.install_missing, application, robot['id'])

        self.assertIn('磁盘已满', str(error))
        self.assertIn('挥手', str(error))

    def test_an_install_whose_answer_was_lost_counts_when_the_skill_is_there(self):
        robot, application = self.made_with()
        self.installed[robot['id']] = []
        self.library = [in_library()]
        self.install_result = {'status': 'unknown', 'error': '安装结果未知'}
        landed = lambda platform, target, item: self.installed[target['id']].append(on_robot('wave')) or dict(self.install_result)

        with mock.patch.object(model_skills, 'install_example', landed):
            self.assertEqual(self.app.model_skills.install_missing(application, robot['id']), ['挥手'])

    def test_an_on_site_test_installs_after_its_check_and_before_the_device_is_asked(self):
        from brainos import connector
        robot, application = self.made_with('navigate')
        order = []
        check, request = self.app.harness, connector.request

        def asked(endpoint, path, *rest, **more):
            if path == '/v1/validate':
                raise RuntimeError('the device is asked')
            return request(endpoint, path, *rest, **more)

        with mock.patch.object(self.app, 'harness', lambda p: order.append('check') or check(p)), \
                mock.patch.object(self.app.model_skills, 'install_missing', lambda app, robot_id=None: order.append(('install', app['id'], robot_id)) or []), \
                mock.patch.object(connector, 'request', asked):
            with self.assertRaisesRegex(RuntimeError, 'the device is asked'):
                self.app.onsite_test({'application_id': application['id'], 'robot_id': robot['id'], 'hardware_authorized': True})
        self.assertEqual(order, ['check', ('install', application['id'], robot['id'])])

    def test_an_application_that_fails_its_check_gets_nothing_installed(self):
        robot, application = self.made_with()
        self.app.store.put('robots', {**self.app.store.get('robots', robot['id']), 'status': 'unconnected'})
        with mock.patch.object(self.app.model_skills, 'install_missing', side_effect=AssertionError('not asked')):
            with self.assertRaises(ValueError):
                self.app.onsite_test({'application_id': application['id'], 'robot_id': robot['id'], 'hardware_authorized': True})

    def test_no_permission_no_install(self):
        robot, application = self.made_with()
        with mock.patch.object(self.app.model_skills, 'install_missing', side_effect=AssertionError('not asked')):
            with self.assertRaises(ValueError):
                self.app.onsite_test({'application_id': application['id'], 'robot_id': robot['id']})


class RealRuntime(unittest.TestCase):
    """Nothing replaced: the real management layer with the simulated quadruped, and the app's own skill library."""

    def setUp(self):
        import secrets
        import tempfile
        from brainos import robot_scope, sim_robots
        from brainos.service import Platform
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        access = secrets.token_urlsafe(24)
        device = sim_robots.SimProcess('fixture-dog', 'sim_quadruped', self.tmp.name + '/device', access)
        self.addCleanup(device.stop)
        url = device.start()
        self.app = Platform(self.tmp.name + '/platform')
        self.addCleanup(self.app.close)
        self.home = robot_scope.HOME
        robot = self.app.robot_models.register({'project_id': self.home, 'name': 'Dog', 'family': 'quadruped',
                                                'endpoint': url, 'token': access})
        self.app.connect_robot({'robot_id': robot['id']})
        self.robot = self.app.store.get('robots', robot['id'])          # as kept, with what is needed to reach the device

    def test_library_skills_are_ready_and_installed_only_when_needed(self):
        self.assertEqual(model_skills.installed_on(self.robot), [])           # a new device has no skill

        result = self.app.model_skills.ready({'project_id': self.home, 'robot_id': self.robot['id']})

        by_entry = {s['entrypoint']: s for s in result['skills']}
        self.assertIn('status_report', by_entry)
        self.assertNotIn('wave', by_entry)                                    # the arm's skill does not fit a quadruped
        self.assertEqual(model_skills.installed_on(self.robot), [])           # making them ready installs nothing
        self.assertEqual(self.app.model_skills.ready({'project_id': self.home, 'robot_id': self.robot['id']})['added'], 0)

        application = self.app.add_application({'project_id': self.home, 'name': 'Report', 'version': '1.0', 'robot_id': self.robot['id'],
                                                'steps': [{'skill_id': by_entry['status_report']['id'], 'parameters': {}, 'timeout_s': 30}]})
        done = self.app.model_skills.install_missing(application, self.robot['id'])

        self.assertEqual(done, [by_entry['status_report']['name']])
        self.assertEqual([s['id'] for s in model_skills.installed_on(self.robot)], ['status_report'])
        self.assertEqual(self.app.model_skills.install_missing(application, self.robot['id']), [])      # nothing is sent twice


class Wiring(unittest.TestCase):
    def test_the_page_script_is_served_and_loaded_once_after_what_it_builds_on(self):
        server = (ROOT / 'brainos' / 'server.py').read_text(encoding='utf-8')
        index = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
        for name in ('app_create.js', 'app_create.css'):
            self.assertTrue((ROOT / 'web' / name).is_file(), name)
            self.assertIn(f"'/{name}'", server)
        self.assertEqual(index.count(LINE), 1)
        for earlier in ('/app_gallery.js', '/task_apps.js', '/robot_model.js'):
            self.assertLess(index.index(earlier), index.index(LINE), earlier)

    def test_the_page_logic(self):
        import shutil
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        for script in ('web/app_create.js', 'tests/app_create_logic.cjs'):
            subprocess.run([node, '--check', script], cwd=ROOT, check=True)
        done = subprocess.run([node, 'tests/app_create_logic.cjs'], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_the_requests_are_routed_and_are_the_account_s(self):
        from brainos import robot_scope
        server = (ROOT / 'brainos' / 'server.py').read_text(encoding='utf-8')
        for route in ('model-skills/ready', 'model-skills/create'):
            self.assertIn(f"'{route}'", server)
            self.assertTrue(route in robot_scope.ROUTES or route.startswith(robot_scope.PREFIXES), route)

    def test_every_refusal_has_a_plain_sentence(self):
        table = (ROOT / 'web' / 'ux04_errors.js').read_text(encoding='utf-8')
        for code in ('app_task_required', 'skill_auto_install_failed'):
            self.assertIn(code + ':', table)

    def test_the_kind_of_a_robot_is_the_one_tasks_compare_by(self):
        self.assertIs(model_skills.robot_kind, task_apps.robot_kind)


if __name__ == '__main__':
    unittest.main()
