"""Issue #43: the console against the real cloud app of this checkout (``cloud/``, a local uvicorn
on SQLite and a temporary directory, as ``tests/cloud_fixture.py`` starts it for other tests).

The other test files use a stand-in cloud, written from the contract (docs/CLOUD_SKILLS.md). This
one checks what the stand-in cannot: that the cloud's own code and the console agree — above all on
the digest of a release, which decides whether a download may be installed — and that a bundle
goes through the real library. The robots are a simulated quadruped (a real ``neuxmind_runtime``)
and ``bundle_fixture.BundleDevice``. Skipped when the cloud's dependencies are not installed or this
checkout's cloud does not have the skill library yet. Nothing leaves this computer.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import cloud_fixture
except ImportError as exc:      # cloud dependencies not installed in this interpreter
    cloud_fixture = None
    SKIP = 'cloud dependencies not installed: %s' % exc
else:
    SKIP = ''

from brainos.cloud_account import CloudError  # noqa: E402
from brainos.service import Platform  # noqa: E402

# Issue #48: a release is published for one brand and model; these are the ones the simulated quadruped reports.
SIM = {'vendor': 'neuxmind-sim', 'model': 'sim-quadruped'}

ROBOT, PROJECT, CONNECTION = 'robot-fixture', 'project-fixture', 'connection-fixture'
HOME = '/home/robot'


@unittest.skipIf(cloud_fixture is None, SKIP)
class RealCloudCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = cloud_fixture.CloudServer(models=())
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.cloud.stop()

    verified = True

    def setUp(self):
        type(self).count += 1
        self.work = tempfile.TemporaryDirectory(prefix='nx-issue43-real-')
        self.addCleanup(self.work.cleanup)
        self.platform = Platform(os.path.join(self.work.name, 'state'))
        self.addCleanup(self.platform.close)
        email = 'skills%d@issue43.test' % self.count
        self.cloud.register(email, verify=self.verified)
        self.platform.cloud.configure({'base_url': self.cloud.url})
        self.platform.cloud.login({'email': email, 'password': cloud_fixture.PASSWORD})


class SkillLibrary(RealCloudCase):
    def setUp(self):
        super().setUp()
        self.skills = self.platform.cloud_skills
        try:
            self.skills.list()
        except CloudError as exc:
            if exc.status == 404:
                self.skipTest('this checkout\'s cloud has no skill library yet')
            raise

    def test_the_examples_are_published_under_the_digest_the_console_computes(self):
        result = self.skills.publish_examples(SIM)
        self.assertEqual({r['status'] for r in result['results']}, {'published'}, result)
        self.assertTrue(all(r['digest_matches'] for r in result['results']), result)
        overview = self.skills.overview()
        self.assertEqual(len(overview['skills']), len(result['results']))
        self.assertTrue(all(e['published'] and e['same_content'] for e in overview['examples']), overview['examples'])
        by_id = {row['id']: row['latest'] for row in overview['skills']}
        self.assertTrue(by_id['patrol']['motion'])
        self.assertFalse(by_id['status_report']['motion'])
        self.assertEqual(self.skills.publish_examples(SIM)['published'], len(result['results']))     # again: nothing new

    def test_a_release_is_downloaded_verified_and_installed_on_a_simulated_robot(self):
        self.skills.publish_examples(SIM)
        robot = self.platform.add_sim_robot({'kind': 'quadruped'})
        ask = {'robot_id': robot['id'], 'project_id': robot['project_id'], 'skill_id': 'patrol', 'version': '0.1.0'}
        preview = self.skills.install_preview(ask)
        self.assertTrue(preview['can_install'], preview['compatibility'])
        result = self.skills.install({**ask, 'request_id': 'real-1', 'package_digest': preview['package_digest'], 'authorized': True})
        self.assertEqual((result['status'], result['digest_matches']), ('succeeded', True))
        installed = {s['id']: s['digest'] for s in result['skills']['items']}
        self.assertEqual(installed['patrol'], preview['release']['digest'])

    def test_other_content_under_a_version_is_refused_and_a_release_can_be_deleted(self):
        self.skills.publish_examples(SIM)
        import json
        import shutil
        from brainos import runtime_bridge
        target = Path(os.path.realpath(self.work.name)) / 'changed'
        shutil.copytree(runtime_bridge.examples_dir() / 'wave', target)
        meta = json.loads((target / 'skill.json').read_text(encoding='utf-8'))
        (target / 'skill.json').write_text(json.dumps({**meta, 'description': '改过的内容'}, ensure_ascii=False), encoding='utf-8')
        preview = self.skills.publish_preview({'path': str(target)})
        self.assertEqual(preview['state'], 'conflict')
        with self.assertRaises(CloudError) as caught:
            self.skills.publish({**SIM, 'path': str(target), 'digest': preview['digest']})
        self.assertEqual(caught.exception.code, 'skill_version_exists')

        self.assertEqual(len(self.skills.detail({'skill_id': 'wave'})['releases']), 1)
        self.skills.remove({'skill_id': 'wave', 'version': '0.1.0'})
        with self.assertRaises(CloudError) as caught:
            self.skills.detail({'skill_id': 'wave'})
        self.assertEqual(caught.exception.code, 'skill_not_found')
        self.assertEqual(self.skills.publish_preview({'path': str(target)})['state'], 'new')


class UnverifiedAccount(RealCloudCase):
    verified = False

    def test_publishing_needs_a_verified_e_mail_and_says_so(self):
        skills = self.platform.cloud_skills
        try:
            preview = skills.publish_preview({'example': 'status_report'})
        except CloudError as exc:
            if exc.status == 404 and exc.code != 'skill_not_found':
                self.skipTest('this checkout\'s cloud has no skill library yet')
            raise
        with self.assertRaises(CloudError) as caught:
            skills.publish({**SIM, 'example': 'status_report', 'digest': preview['digest']})
        self.assertEqual(caught.exception.code, 'email_unverified')
        self.assertIn('邮箱尚未验证', str(caught.exception))
        with self.assertRaises(CloudError) as caught:
            skills.publish_examples(SIM)
        self.assertEqual(caught.exception.code, 'email_unverified')


class Bundles(RealCloudCase):
    def setUp(self):
        super().setUp()
        from bundle_fixture import BundleDevice
        from test_device_transfer import quiet_paramiko
        quiet_paramiko(self)
        self.device = BundleDevice()
        self.addCleanup(self.device.close)
        connection, secret = self.device.login()
        store = self.platform.store
        store.put('robots', {'id': ROBOT, 'project_id': PROJECT, 'name': '测试机器人', 'family': 'quadruped',
                             'connection_id': CONNECTION})
        store.put('device_connections', {**connection, 'id': CONNECTION, 'robot_id': ROBOT, 'project_id': PROJECT})
        store.put('agent_secrets', {**secret, 'id': CONNECTION})
        self.bundles = self.platform.robot_bundle
        self.bundles.tuning = {'fetch': {'retry_waits': (0, 0, 0)}, 'transfer': {'retry_waits': (0, 0, 0)}, 'retry_waits': (0, 0, 0)}
        for name, data in {'walk.py': b'print("walk")\n', 'maps/office.yaml': b'places: {}\n', 'runs/1/log.txt': b'log'}.items():
            self.device.write('%s/walk_programs/%s' % (HOME, name), data)

    def test_a_directory_goes_to_the_real_library_as_a_bundle_and_comes_back(self):
        try:
            collected = self.bundles.collect(ROBOT, PROJECT, '~/walk_programs', authorized=True)
        except CloudError as exc:
            if exc.status in (400, 415, 422) and not self.platform.library.list({'kind': 'bundle'})['materials']:
                self.skipTest('this checkout\'s cloud does not take bundles yet: %s' % exc)
            raise
        material = self.bundles.material(collected['material_id'])
        self.assertEqual((material['kind'], material['status'], material['device_id'], material['sha256']),
                         ('bundle', 'usable', ROBOT, collected['sha256']))
        overview = self.bundles.overview({'robot_id': ROBOT, 'project_id': PROJECT})
        self.assertEqual([b['id'] for b in overview['bundles']], [collected['material_id']])

        installed = self.bundles.install(ROBOT, PROJECT, collected['material_id'], authorized=True)
        folder = '%s/neuxmind-bundles/walk_programs/%s' % (HOME, collected['sha256'][:16])
        self.assertEqual(installed['device_dir'], '~/neuxmind-bundles/walk_programs/' + collected['sha256'][:16])
        self.assertEqual(self.device.read(folder + '/maps/office.yaml'), b'places: {}\n')
        self.assertIsNone(self.device.read(folder + '/runs/1/log.txt'))
        self.assertEqual(self.device.read(HOME + '/walk_programs/runs/1/log.txt'), b'log')        # the original is as it was

    def test_the_real_library_does_not_parse_a_bundle_and_a_driver_delivery_does_not_take_it(self):
        try:
            collected = self.bundles.collect(ROBOT, PROJECT, '~/walk_programs', authorized=True)
        except CloudError as exc:
            self.skipTest('this checkout\'s cloud does not take bundles yet: %s' % exc)
        with self.assertRaises(ValueError) as caught:
            self.platform.package_delivery.deliver(ROBOT, PROJECT, collected['material_id'], authorized=True)
        self.assertEqual(getattr(caught.exception, 'code', None), 'package_not_deliverable')
        self.assertEqual(self.device.names(HOME + '/neuxmind-pkg'), [])


if __name__ == '__main__':
    unittest.main()
