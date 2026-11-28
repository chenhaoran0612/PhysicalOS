"""Issue #73 「应用编排」: the skills published to the cloud for a brand and model are skills of that brand and model.

The owner's words: 「应用编排现在无法正确关联到已经发布的能力。需要关联上，能力是发布给品牌型号的」. The form that creates
an application (issue #68) listed only the skills on connected robots of the brand and model and the bundled ones that
fit; a release in 「技能发布」 (issue #43), which names the brand and model it is for (issue #48), was never among them.

Rules under test (brainos/published_skills.py, brainos/model_skills.py):

* every release the cloud holds for the brand and model of the robot is a skill of that brand and model when the form
  asks, whether or not one of its robots is connected; a release for another one, or for none, is not;
* signed out, the cloud is not asked and the form is told; a cloud that cannot be read is named and the rest goes on;
* before an on-site test, a skill of the application that the robot lacks is installed from the cloud when the release
  of the step's version is there (the bundled package goes first when it is that version); a release the robot cannot
  take stops the test with the reason.

The cloud is ``skill_cloud_fixture.FakeSkillCloud``, except in ``RealCloud`` (the cloud app of this checkout).
"""
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import model_skills, published_skills, robot_model, robot_scope, runtime_bridge  # noqa: E402
from skill_cloud_fixture import FakeSkillCloud  # noqa: E402
from test_model_skills import Case as ModelSkillsCase, in_library, on_robot  # noqa: E402

EXAMPLES = runtime_bridge.examples_dir()
L2U = {'vendor': '智身科技', 'model': '钢镚 L2-U'}
SIM = {'vendor': 'neuxmind-sim', 'model': 'sim-quadruped'}       # what the simulated quadruped reports


def copy_of(work, example, **changes):
    """A bundled example in a plain folder, with ``skill.json`` fields changed (a new id or version)."""
    target = Path(os.path.realpath(work)) / ('skill-' + secrets.token_hex(4))
    shutil.copytree(EXAMPLES / example, target)
    if changes:
        meta = json.loads((target / 'skill.json').read_text(encoding='utf-8'))
        meta.update(changes)
        (target / 'skill.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
    return str(target)


def publish(platform, work, example, product, **changes):
    """Publish a copy of a bundled example for ``product``, as 「技能发布」 does; the release."""
    path = copy_of(work, example, **changes)
    preview = platform.cloud_skills.publish_preview({'path': path})
    return platform.cloud_skills.publish({**product, 'path': path, 'digest': preview['digest']})['release']


class Case(ModelSkillsCase):
    """The robots and the bundled library are the stand-ins of tests/test_model_skills.py; the cloud is a stand-in too."""

    def setUp(self):
        super().setUp()
        self.cloud = FakeSkillCloud()
        self.addCleanup(self.cloud.close)
        self.cloud.sign_in(self.app)
        self.work = tempfile.mkdtemp(prefix='nx-issue73-')
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cloud_installs, self.cloud_result = [], {'status': 'succeeded'}
        patch = mock.patch.object(model_skills, 'install_published', self.install_from_cloud)
        patch.start()
        self.addCleanup(patch.stop)

    def install_from_cloud(self, platform, robot, item):
        self.cloud_installs.append((robot['id'], item['id'], item['version']))
        return dict(self.cloud_result)

    def publish(self, example='status_report', product=None, **changes):
        return publish(self.app, self.work, example, product or {'vendor': 'Acme', 'model': 'X1'}, **changes)

    def offline_l2u(self):
        return self.robot('L2U', L2U['vendor'], L2U['model'])           # no endpoint: never connected

    def entries(self, result):
        return sorted((s['entrypoint'], s['version']) for s in result['skills'])


class Ready(Case):
    def test_releases_for_the_brand_and_model_are_offered_with_no_robot_of_it_connected(self):
        robot = self.offline_l2u()
        walk = self.publish('patrol', L2U, id='free_walk', title='无图自由走')
        self.publish('status_report', L2U)

        result = self.ready(robot)

        self.assertEqual(self.entries(result), [('free_walk', '0.1.0'), ('status_report', '0.1.0')])
        saved = self.app.store.get('skills', next(s['id'] for s in result['skills'] if s['entrypoint'] == 'free_walk'))
        self.assertEqual(saved['name'], '无图自由走')
        self.assertEqual(saved['requires'], [c['id'] for c in walk['requires']])
        self.assertEqual(robot_model.of(saved), L2U)
        self.assertTrue(saved.get('parameters'))                        # the step form is generated from them
        self.assertEqual(result['cloud'], {'status': 'ok', 'count': 2, 'error': None})
        self.assertEqual((result['added'], result['connected']), (2, 0))

    def test_a_release_for_another_brand_and_model_or_for_none_is_not_offered(self):
        robot = self.offline_l2u()
        self.publish('status_report', L2U)
        self.publish('patrol', {'vendor': 'Acme', 'model': 'X1'})
        self.publish('patrol', L2U, id='old_walk')
        self.cloud.releases[('old_walk', '0.1.0')]['release']['product'] = None      # published before releases named one

        self.assertEqual(self.entries(self.ready(robot)), [('status_report', '0.1.0')])

    def test_every_version_published_for_it_is_offered_and_one_for_another_is_not(self):
        robot = self.offline_l2u()
        self.publish('status_report', L2U)
        self.publish('status_report', L2U, version='0.2.0')
        self.publish('status_report', {'vendor': 'Acme', 'model': 'X1'}, version='0.3.0')     # the newest is not for it

        result = self.ready(robot)

        self.assertEqual(self.entries(result), [('status_report', '0.1.0'), ('status_report', '0.2.0')])
        self.assertEqual(result['cloud']['count'], 2)

    def test_another_spelling_of_the_same_brand_and_model_is_the_same_one(self):
        robot = self.robot('Dog', 'zsibot', 'L2U')
        self.publish('status_report', L2U)

        self.assertEqual(self.entries(self.ready(robot)), [('status_report', '0.1.0')])

    def test_asking_again_adds_nothing(self):
        robot = self.offline_l2u()
        self.publish('status_report', L2U)

        first, second = self.ready(robot), self.ready(robot)

        self.assertEqual((first['added'], second['added']), (1, 0))
        self.assertEqual(len(self.app.store.list('skills')), 1)

    def test_a_skill_on_a_robot_and_in_the_cloud_is_kept_once(self):
        robot = self.connected('Dog', 'Acme', 'X1')
        self.installed[robot['id']] = [on_robot('status_report', '0.1.0', '状态报告', ('system.health',))]
        self.publish('status_report')

        result = self.ready(robot)

        self.assertEqual(self.entries(result), [('status_report', '0.1.0')])
        self.assertEqual(result['cloud']['count'], 1)

    def test_signed_out_the_cloud_is_not_asked_and_the_form_is_told(self):
        robot = self.offline_l2u()
        self.publish('status_report', L2U)
        self.app.cloud._drop_token()
        asked = len(self.cloud.requests)

        result = self.ready(robot)

        self.assertEqual(result['skills'], [])
        self.assertEqual(result['cloud'], {'status': 'signed_out', 'count': 0, 'error': None})
        self.assertEqual(len(self.cloud.requests), asked)

    def test_a_cloud_that_cannot_be_reached_is_named_and_the_rest_goes_on(self):
        robot = self.connected('Dog', 'Acme', 'X1')
        self.installed[robot['id']] = [on_robot()]
        self.cloud.offline = True

        result = self.ready(robot)

        self.assertEqual(self.entries(result), [('patrol', '1.0.0')])
        self.assertEqual(result['cloud']['status'], 'failed')
        self.assertIn('云平台', result['cloud']['error'])

    def test_a_cloud_that_does_not_answer_holds_the_form_only_briefly(self):
        robot = self.offline_l2u()
        silent = socket.socket()
        silent.bind(('127.0.0.1', 0))
        silent.listen(8)                                                # connections are taken and never answered
        self.addCleanup(silent.close)
        self.app.cloud._save_settings({'base_url': 'http://127.0.0.1:%d' % silent.getsockname()[1], 'email': 'dev@fixture.test'})
        self.app.cloud._store_token(self.cloud.token)                  # signed in, as FakeSkillCloud.sign_in does
        started = time.monotonic()

        with mock.patch.object(published_skills, 'READ_TIMEOUT_S', 0.5):
            result = self.ready(robot)

        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result['cloud']['status'], 'failed')
        self.assertLess(published_skills.READ_TIMEOUT_S, 10)            # the console's own pages wait 30 s

    def test_a_cloud_that_drips_its_answer_holds_the_form_only_briefly(self):
        robot = self.offline_l2u()
        drip, stop = socket.socket(), threading.Event()
        drip.bind(('127.0.0.1', 0))
        drip.listen(8)
        self.addCleanup(drip.close)
        self.addCleanup(stop.set)

        def answer():                                                   # every read gets a byte in time; the answer never ends
            conn, _ = drip.accept()
            conn.recv(65536)
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n{')
            while not stop.wait(0.1):
                try:
                    conn.sendall(b' ')
                except OSError:
                    break
            conn.close()
        threading.Thread(target=answer, daemon=True).start()
        self.app.cloud._save_settings({'base_url': 'http://127.0.0.1:%d' % drip.getsockname()[1], 'email': 'dev@fixture.test'})
        self.app.cloud._store_token(self.cloud.token)
        started = time.monotonic()

        with mock.patch.object(published_skills, 'READ_TIMEOUT_S', 1.0):
            result = self.ready(robot)

        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result['cloud']['status'], 'failed')
        self.assertIn('太慢', result['cloud']['error'])

    def test_a_garbled_answer_from_the_cloud_is_named_and_the_rest_goes_on(self):
        robot = self.connected('Dog', 'Acme', 'X1')
        self.installed[robot['id']] = [on_robot()]
        garbled = socket.socket()
        garbled.bind(('127.0.0.1', 0))
        garbled.listen(8)
        self.addCleanup(garbled.close)

        def answer():                                                   # a chunked body cut off in the middle
            for _ in range(2):
                try:
                    conn, _ = garbled.accept()
                except OSError:
                    return
                conn.recv(65536)
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n'
                             b'40\r\n{"skills": [')
                conn.close()
        threading.Thread(target=answer, daemon=True).start()
        self.app.cloud._save_settings({'base_url': 'http://127.0.0.1:%d' % garbled.getsockname()[1], 'email': 'dev@fixture.test'})
        self.app.cloud._store_token(self.cloud.token)

        result = self.ready(robot)

        self.assertEqual(self.entries(result), [('patrol', '1.0.0')])
        self.assertEqual(result['cloud']['status'], 'failed')

    def test_one_bad_release_does_not_hide_the_others(self):
        robot = self.offline_l2u()
        self.publish('status_report', L2U)
        self.publish('patrol', L2U)
        self.cloud.releases[('patrol', '0.1.0')]['release']['skill_id'] = 'Not An Id!'     # a row the console cannot read

        result = self.ready(robot)

        self.assertEqual(self.entries(result), [('status_report', '0.1.0')])
        self.assertEqual(result['cloud']['status'], 'ok')

    def test_the_versions_of_many_skills_are_read_together_within_the_wait(self):
        robot = self.offline_l2u()
        for key in ('a_walk', 'b_walk', 'c_walk', 'd_walk', 'e_walk', 'f_walk'):
            self.publish('patrol', L2U, id=key)
            self.publish('patrol', L2U, id=key, version='0.2.0')          # two versions: each skill's own page is read
        call = published_skills.library.CloudSkills._call

        def slow(skills, *args, **kwargs):
            time.sleep(0.3)
            return call(skills, *args, **kwargs)

        started = time.monotonic()
        with mock.patch.object(published_skills.library.CloudSkills, '_call', slow), \
                mock.patch.object(published_skills, 'READ_TIMEOUT_S', 1.5):
            result = self.ready(robot)

        self.assertLess(time.monotonic() - started, 1.5)                 # 7 reads of 0.3 s, one after another, would be 2.1 s
        self.assertEqual((result['cloud']['status'], result['cloud']['count']), ('ok', 12))

    def test_a_robot_with_no_brand_and_model_asks_the_cloud_nothing(self):
        plain = self.robot('Old', reports=())
        self.publish('status_report')
        asked = len(self.cloud.requests)

        result = self.ready(plain)

        self.assertEqual((result['skills'], result['product']), ([], None))
        self.assertEqual(len(self.cloud.requests), asked)


class InstallMissing(Case):
    def made_from_the_cloud(self, example='status_report', **changes):
        """A connected robot with nothing installed, and an application whose step is a release published for it."""
        robot = self.connected('Dog', 'Acme', 'X1')
        self.installed[robot['id']] = []
        release = self.publish(example, **changes)
        skill = next(s for s in self.ready(robot)['skills'] if s['entrypoint'] == release['skill_id'])
        return robot, self.application(robot, skill), release

    def test_a_published_skill_the_robot_lacks_is_installed_from_the_cloud(self):
        robot, application, release = self.made_from_the_cloud(id='report_cloud', title='云端状态报告')

        done = self.app.model_skills.install_missing(application, robot['id'])

        self.assertEqual(self.cloud_installs, [(robot['id'], 'report_cloud', '0.1.0')])
        self.assertEqual(done, ['云端状态报告'])

    def test_the_version_of_the_step_is_the_one_installed(self):
        robot, application, _ = self.made_from_the_cloud(version='2.0.0')
        self.library = [in_library('status_report', '0.1.0', '状态报告')]     # the bundled one is another version

        self.app.model_skills.install_missing(application, robot['id'])

        self.assertEqual(self.cloud_installs, [(robot['id'], 'status_report', '2.0.0')])
        self.assertEqual(self.installs, [])

    def test_the_bundled_package_of_that_version_goes_first_and_the_cloud_is_not_asked(self):
        robot, application, _ = self.made_from_the_cloud()
        self.library = [in_library('status_report', '0.1.0', '状态报告')]
        asked = len(self.cloud.requests)

        self.app.model_skills.install_missing(application, robot['id'])

        self.assertEqual((self.installs, self.cloud_installs), ([(robot['id'], 'status_report')], []))
        self.assertEqual(len(self.cloud.requests), asked)

    def test_a_skill_the_robot_has_is_left_alone(self):
        robot, application, _ = self.made_from_the_cloud()
        self.installed[robot['id']] = [on_robot('status_report', '0.0.9')]

        self.assertEqual(self.app.model_skills.install_missing(application, robot['id']), [])
        self.assertEqual(self.cloud_installs, [])

    def test_a_release_that_fails_to_install_stops_the_test_with_the_reason(self):
        robot, application, _ = self.made_from_the_cloud()
        self.cloud_result = {'status': 'refused', 'error': '技能包与这台机器人不兼容，不能安装：缺少 机械臂关节位置'}

        error = self.refused('skill_auto_install_failed', self.app.model_skills.install_missing, application, robot['id'])

        self.assertIn('不兼容', str(error))
        self.assertIn('状态报告', str(error))

    def test_a_cloud_that_cannot_be_read_stops_the_test_with_the_reason(self):
        robot, application, _ = self.made_from_the_cloud()
        self.cloud.offline = True

        error = self.refused('skill_auto_install_failed', self.app.model_skills.install_missing, application, robot['id'])

        self.assertIn('云平台', str(error))
        self.assertEqual(self.cloud_installs, [])

    def test_a_version_republished_with_other_content_since_the_application_was_made_is_not_installed(self):
        robot, application, release = self.made_from_the_cloud()
        del self.cloud.releases[(release['skill_id'], release['version'])]        # deleted in 「技能发布」, then the same version again
        self.publish(requires=[{'id': 'base.navigate_to', 'major': 1}])

        error = self.refused('skill_auto_install_failed', self.app.model_skills.install_missing, application, robot['id'])

        self.assertIn('和创建应用时不一样', str(error))
        self.assertEqual(self.cloud_installs, [])

    def test_signed_out_a_skill_that_is_not_bundled_is_left_to_the_device(self):
        robot, application, _ = self.made_from_the_cloud()
        self.app.cloud._drop_token()

        self.assertEqual(self.app.model_skills.install_missing(application, robot['id']), [])
        self.assertEqual(self.cloud_installs, [])

    def test_a_skill_that_is_neither_bundled_nor_published_is_left_to_the_device(self):
        robot = self.connected('Dog', 'Acme', 'X1')
        self.installed[robot['id']] = []
        skill = self.skill('自己的技能', vendor='Acme', model='X1', entrypoint='custom', version='1.0.0')
        application = self.application(robot, skill)

        self.assertEqual(self.app.model_skills.install_missing(application, robot['id']), [])
        self.assertEqual(self.cloud_installs, [])


class RealRuntime(unittest.TestCase):
    """The real management layer with the simulated quadruped; the cloud is the stand-in. Nothing else is replaced."""

    def setUp(self):
        from brainos import sim_robots
        from brainos.service import Platform
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-issue73-real-')
        self.addCleanup(self.tmp.cleanup)
        access = secrets.token_urlsafe(24)
        device = sim_robots.SimProcess('fixture-dog', 'sim_quadruped', self.tmp.name + '/device', access)
        self.addCleanup(device.stop)
        url = device.start()
        self.app = Platform(self.tmp.name + '/platform')
        self.addCleanup(self.app.close)
        self.cloud = FakeSkillCloud()
        self.addCleanup(self.cloud.close)
        self.cloud.sign_in(self.app)
        robot = self.app.robot_models.register({'project_id': robot_scope.HOME, 'name': 'Dog', 'family': 'quadruped',
                                                'endpoint': url, 'token': access})
        self.app.connect_robot({'robot_id': robot['id']})
        self.robot = self.app.store.get('robots', robot['id'])
        self.assertEqual(robot_model.key(robot_model.of(self.robot)), robot_model.key(SIM))

    def application_of(self, entrypoint):
        result = self.app.model_skills.ready({'project_id': robot_scope.HOME, 'robot_id': self.robot['id']})
        skill = next(s for s in result['skills'] if s['entrypoint'] == entrypoint)
        return self.app.add_application({'project_id': robot_scope.HOME, 'name': 'Round', 'version': '1.0', 'robot_id': self.robot['id'],
                                         'steps': [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 30}]}), skill

    def test_a_published_skill_is_installed_on_the_robot_only_when_an_on_site_test_needs_it(self):
        publish(self.app, self.tmp.name, 'status_report', SIM, id='report_cloud', version='0.3.0', title='云端状态报告')

        application, skill = self.application_of('report_cloud')
        self.assertEqual(model_skills.installed_on(self.robot), [])             # making it ready installed nothing

        done = self.app.model_skills.install_missing(application, self.robot['id'])

        self.assertEqual(done, ['云端状态报告'])
        on_robot_now = {(s['id'], s['version']) for s in model_skills.installed_on(self.robot)}
        self.assertEqual(on_robot_now, {('report_cloud', '0.3.0')})
        operation = next(o for o in self.app.store.list('operations') if o.get('kind') == 'skill_install')
        self.assertEqual((operation['source'], operation['status'], operation['skill_id']), ('cloud', 'succeeded', 'report_cloud'))
        self.assertEqual(self.app.model_skills.install_missing(application, self.robot['id']), [])     # nothing is sent twice

    def test_a_package_that_is_not_what_the_record_was_made_from_is_not_sent(self):
        publish(self.app, self.tmp.name, 'status_report', SIM, id='report_cloud')
        item = next(i for i in model_skills.published_for(self.app, self.robot)['items'] if i['id'] == 'report_cloud')

        for changed in ({'requires': ['base.navigate_to']}, {'digest': 'e' * 64}):
            outcome = published_skills.install(self.app, self.robot, {**item, **changed})
            self.assertEqual(outcome['status'], 'refused', changed)
        self.assertEqual(model_skills.installed_on(self.robot), [])

    def test_a_published_release_the_robot_cannot_take_stops_with_the_reason(self):
        publish(self.app, self.tmp.name, 'wave', SIM, id='wave_cloud', title='云端挥手')    # an arm's skill, published for the quadruped

        application, _ = self.application_of('wave_cloud')                       # offered: the person published it for this model
        with self.assertRaises(ValueError) as caught:
            self.app.model_skills.install_missing(application, self.robot['id'])

        self.assertEqual(getattr(caught.exception, 'code', None), 'skill_auto_install_failed')
        self.assertIn('云端挥手', str(caught.exception))
        self.assertIn('不兼容', str(caught.exception))
        self.assertEqual(model_skills.installed_on(self.robot), [])


try:
    import cloud_fixture
except ImportError as exc:          # the cloud's dependencies are not installed in this interpreter
    cloud_fixture, CLOUD_SKIP = None, 'cloud dependencies not installed: %s' % exc
else:
    CLOUD_SKIP = ''


@unittest.skipIf(cloud_fixture is None, CLOUD_SKIP)
class RealCloud(unittest.TestCase):
    """The cloud app of this checkout: its list of skills and the brand and model of its releases agree with the console."""

    @classmethod
    def setUpClass(cls):
        cls.cloud = cloud_fixture.CloudServer(models=())

    @classmethod
    def tearDownClass(cls):
        cls.cloud.stop()

    def setUp(self):
        from brainos.service import Platform
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-issue73-cloud-')
        self.addCleanup(self.tmp.cleanup)
        self.app = Platform(self.tmp.name + '/platform')
        self.addCleanup(self.app.close)
        email = 'published%s@issue73.test' % secrets.token_hex(4)
        self.cloud.register(email, verify=True)
        self.app.cloud.configure({'base_url': self.cloud.url})
        self.app.cloud.login({'email': email, 'password': cloud_fixture.PASSWORD})

    def robot(self, name, product):
        return self.app.add_robot({'project_id': robot_scope.HOME, 'name': name, 'family': 'quadruped', **product})

    def test_the_examples_published_for_a_brand_and_model_are_its_skills(self):
        published = self.app.cloud_skills.publish_examples(L2U)
        self.assertEqual({r['status'] for r in published['results']}, {'published'}, published)
        publish(self.app, self.tmp.name, 'status_report', L2U, version='0.2.0')     # a second version: read through the skill's own page
        mine, other = self.robot('L2U', L2U), self.robot('Arm', {'vendor': 'Acme', 'model': 'X1'})

        result = self.app.model_skills.ready({'project_id': robot_scope.HOME, 'robot_id': mine['id']})

        expected = {(r['skill_id'], r['version']) for r in published['results']} | {('status_report', '0.2.0')}
        self.assertEqual({(s['entrypoint'], s['version']) for s in result['skills']}, expected)
        self.assertEqual(result['cloud'], {'status': 'ok', 'count': len(expected), 'error': None})
        self.assertEqual(self.app.model_skills.ready({'project_id': robot_scope.HOME, 'robot_id': other['id']})['skills'], [])


class Seams(unittest.TestCase):
    def test_the_cloud_is_reached_through_the_module_the_tests_replace(self):
        self.assertTrue(callable(model_skills.published_for) and callable(model_skills.install_published))
        self.assertTrue(callable(published_skills.releases_for) and callable(published_skills.install))


if __name__ == '__main__':
    unittest.main()
