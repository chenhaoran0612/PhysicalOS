"""Issue #43: skills kept in the cloud, as the console uses them.

The cloud is ``skill_cloud_fixture.FakeSkillCloud`` (it computes a release's digest with the
management layer's own function). The robot is a simulated quadruped: a real ``neuxmind_runtime``
child process with a simulated body, so the install at the end is the real ``/v2/skills/install``.
Nothing reaches a real server or a real machine.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import cloud_skills, runtime_bridge, skill_packages  # noqa: E402
from brainos.cloud_account import CloudError  # noqa: E402
from brainos.errors import CodedError  # noqa: E402
from brainos.service import Platform  # noqa: E402
from skill_cloud_fixture import FakeSkillCloud  # noqa: E402

EXAMPLES = runtime_bridge.examples_dir()


class CloudSkillsCase(unittest.TestCase):
    def setUp(self):
        self.cloud = FakeSkillCloud()
        self.addCleanup(self.cloud.close)
        self.work = tempfile.TemporaryDirectory(prefix='nx-issue43-')
        self.addCleanup(self.work.cleanup)
        self.platform = Platform(os.path.join(self.work.name, 'state'))
        self.addCleanup(self.platform.close)
        self.skills = self.platform.cloud_skills
        self.cloud.sign_in(self.platform)

    def folder(self, example='status_report', **changes):
        """A copy of a bundled example in a plain directory, with ``skill.json`` fields changed."""
        target = Path(os.path.realpath(self.work.name)) / ('skill-%d' % len(os.listdir(self.work.name)))
        shutil.copytree(EXAMPLES / example, target)
        if changes:
            meta = json.loads((target / 'skill.json').read_text(encoding='utf-8'))
            meta.update(changes)
            (target / 'skill.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        return str(target)

    def publish(self, path, **more):
        preview = self.skills.publish_preview({'path': path})
        return self.skills.publish({'path': path, 'digest': preview['digest'], **more})

    def events(self, action):
        return [e for e in self.platform.store.list('events') if e['action'] == action]


class Library(CloudSkillsCase):
    def test_signed_out_the_page_gets_an_empty_overview_and_every_call_says_to_sign_in(self):
        self.platform.cloud._drop_token()
        self.assertEqual(self.skills.overview()['skills'], [])
        self.assertFalse(self.skills.overview()['account']['logged_in'])
        for call in (lambda: self.skills.publish_preview({'example': 'patrol'}), self.skills.publish_examples,
                     lambda: self.skills.detail({'skill_id': 'patrol'})):
            with self.assertRaises(CodedError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'cloud_login_required')
        self.assertEqual(self.cloud.requests, [])

    def test_a_folder_is_previewed_then_published_under_the_digest_the_person_saw(self):
        path = self.folder()
        preview = self.skills.publish_preview({'path': path})
        self.assertEqual((preview['state'], preview['can_publish'], preview['skill']['id']), ('new', True, 'status_report'))
        self.assertFalse(preview['motion'])
        self.assertEqual(self.cloud.releases, {})                    # the preview sent nothing

        result = self.skills.publish({'path': path, 'digest': preview['digest'], 'notes': '第一次发布'})
        self.assertTrue(result['digest_matches'])
        release = result['release']
        self.assertEqual((release['skill_id'], release['version'], release['digest'], release['notes']),
                         ('status_report', '0.1.0', preview['digest'], '第一次发布'))
        held = self.cloud.releases[('status_report', '0.1.0')]['package']
        self.assertEqual(sorted(held['files']), sorted(f['path'] for f in preview['files']))
        self.assertEqual(self.events('cloud_skill.published')[0]['details']['digest'], preview['digest'])

    def test_the_digest_is_the_one_the_management_layer_computes_at_install(self):
        package, _ = skill_packages.read_folder(self.folder('patrol'))
        from neuxmind_runtime.skills import package_digest
        from neuxmind_spec import manifest as spec_manifest
        spec = spec_manifest.validate_skill_package({k: v for k, v in package.items() if k != 'files'})
        self.assertEqual(cloud_skills.release_digest(package), package_digest({'package': spec, 'files': package['files']}))

    def test_a_folder_changed_after_the_preview_is_not_published(self):
        path = self.folder()
        preview = self.skills.publish_preview({'path': path})
        with open(os.path.join(path, 'skill.py'), 'a', encoding='utf-8') as handle:
            handle.write('\n# changed after the preview\n')
        with self.assertRaises(CodedError) as caught:
            self.skills.publish({'path': path, 'digest': preview['digest']})
        self.assertEqual(caught.exception.code, 'skill_changed_since_preview')
        self.assertEqual(self.cloud.releases, {})

    def test_the_same_content_twice_is_one_release_and_other_content_under_the_version_is_refused(self):
        path = self.folder()
        first = self.publish(path)
        again = self.skills.publish_preview({'path': path})
        self.assertEqual((again['state'], again['can_publish']), ('same', True))
        self.assertEqual(self.publish(path)['release']['digest'], first['release']['digest'])
        self.assertEqual(len(self.cloud.releases), 1)

        other = self.folder(description='别的内容')
        preview = self.skills.publish_preview({'path': other})
        self.assertEqual((preview['state'], preview['can_publish']), ('conflict', False))
        with self.assertRaises(CloudError) as caught:
            self.skills.publish({'path': other, 'digest': preview['digest']})
        self.assertEqual(caught.exception.code, 'skill_version_exists')
        self.assertIn('提高版本号', str(caught.exception))

    def test_a_folder_that_is_not_a_skill_is_refused_before_anything_is_sent(self):
        empty = os.path.join(os.path.realpath(self.work.name), 'not-a-skill')
        os.makedirs(empty)
        with self.assertRaises(ValueError) as caught:
            self.skills.publish_preview({'path': empty})
        self.assertIn('skill.json', str(caught.exception))
        self.assertEqual([r for r in self.cloud.requests if r[0] == 'POST'], [])

    def test_the_list_shows_what_the_cloud_holds_and_text_from_the_cloud_is_cut_to_known_fields(self):
        self.publish(self.folder('patrol'))
        self.publish(self.folder('patrol', version='0.2.0'))
        held = self.cloud.releases[('patrol', '0.2.0')]['release']
        held.update(title='<img src=x onerror=alert(1)>', secret='hidden', internal='not for the page', description='x' * 5000)
        overview = self.skills.overview()
        self.assertEqual([row['id'] for row in overview['skills']], ['patrol'])
        row = overview['skills'][0]
        self.assertEqual(sorted(row['versions']), ['0.1.0', '0.2.0'])
        self.assertTrue(row['latest']['motion'])
        self.assertNotIn('secret', row['latest'])
        self.assertEqual(len(row['latest']['description']), 2000)
        self.assertEqual(row['latest']['title'], '<img src=x onerror=alert(1)>')     # kept as text; the page escapes it
        detail = self.skills.detail({'skill_id': 'patrol'})
        self.assertEqual(len(detail['releases']), 2)

    def test_a_release_is_deleted_by_id_and_version_only(self):
        self.publish(self.folder())
        for bad in ({'skill_id': '../x', 'version': '0.1.0'}, {'skill_id': 'status_report', 'version': '1/../2'}):
            with self.assertRaises(ValueError):
                self.skills.remove(bad)
        self.assertEqual(self.skills.remove({'skill_id': 'status_report', 'version': '0.1.0'})['deleted'], True)
        self.assertEqual(self.cloud.releases, {})
        with self.assertRaises(CloudError) as caught:
            self.skills.remove({'skill_id': 'status_report', 'version': '0.1.0'})
        self.assertEqual(caught.exception.code, 'skill_not_found')

    def test_a_session_the_cloud_no_longer_accepts_is_dropped(self):
        self.cloud.token = 'another'
        overview = self.skills.overview()
        self.assertFalse(overview['account']['logged_in'])
        self.assertEqual(overview['skills'], [])


class BundledExamples(CloudSkillsCase):
    def test_one_action_publishes_every_bundled_example_and_doing_it_again_changes_nothing(self):
        names = sorted(d.name for d in EXAMPLES.iterdir() if (d / 'skill.json').is_file())
        result = self.skills.publish_examples()
        self.assertEqual(sorted(r['example'] for r in result['results']), names)
        self.assertEqual({r['status'] for r in result['results']}, {'published'})
        self.assertTrue(all(r['digest_matches'] for r in result['results']))
        self.assertEqual(len(self.cloud.releases), len(names))

        self.assertEqual(self.skills.publish_examples()['published'], len(names))
        self.assertEqual(len(self.cloud.releases), len(names))
        examples = self.skills.overview()['examples']
        self.assertTrue(all(e['published'] and e['same_content'] for e in examples), examples)

    def test_an_example_whose_version_holds_other_content_is_reported_and_the_rest_still_go(self):
        self.publish(self.folder('wave', description='改过的内容'))
        result = self.skills.publish_examples()
        by_name = {r['example']: r for r in result['results']}
        self.assertEqual(by_name['wave']['status'], 'conflict')
        self.assertEqual(by_name['patrol']['status'], 'published')
        examples = {e['example']: e for e in self.skills.overview()['examples']}
        self.assertEqual((examples['wave']['published'], examples['wave']['same_content']), (True, False))

    def test_a_refusal_that_holds_for_all_of_them_is_said_once(self):
        self.cloud.quota = 0
        with self.assertRaises(CloudError) as caught:
            self.skills.publish_examples()
        self.assertEqual(caught.exception.code, 'skill_quota_exceeded')


class InstallOnARobot(CloudSkillsCase):
    def setUp(self):
        super().setUp()
        self.robot = self.platform.add_sim_robot({'kind': 'quadruped'})
        self.skills.publish_examples()

    def ask(self, skill_id='patrol', **more):
        ask = {'robot_id': self.robot['id'], 'project_id': self.robot['project_id'], 'skill_id': skill_id, 'version': '0.1.0', **more}
        if 'request_id' in ask:
            ask.setdefault('authorized', True)
        return ask

    def test_installing_needs_a_preview_a_persons_consent_and_the_robots_own_project(self):
        digest = self.cloud.releases[('patrol', '0.1.0')]['release']['digest']          # known without any preview
        with self.assertRaises(CodedError) as caught:
            self.skills.install(self.ask(request_id='n1', package_digest=digest))
        self.assertEqual(caught.exception.code, 'skill_preview_required')
        self.skills.install_preview(self.ask())
        for authorized in (None, False, 'yes', 1):
            with self.assertRaises(CodedError) as caught:
                self.skills.install(self.ask(request_id='n2', package_digest=digest, authorized=authorized))
            self.assertEqual(caught.exception.code, 'skill_install_not_authorized')
        for project in (None, 'another-project'):
            ask = {**self.ask(request_id='n3', package_digest=digest), 'project_id': project}
            with self.assertRaises(ValueError):
                self.skills.install(ask)
            with self.assertRaises(ValueError):
                self.skills.install_preview({**self.ask(), 'project_id': project})
        self.assertEqual(self.platform.store.list('operations'), [])
        self.assertEqual(self.platform.robot_detail({'robot_id': self.robot['id']})['skills']['items'], [])

    def test_download_verify_explain_install_and_the_robot_reports_the_same_digest(self):
        preview = self.skills.install_preview(self.ask())
        release = self.cloud.releases[('patrol', '0.1.0')]['release']
        self.assertEqual((preview['source'], preview['package_digest'], preview['can_install']), ('cloud', release['digest'], True))
        self.assertEqual(preview['robot']['simulated'], True)
        self.assertEqual(self.platform.store.list('operations'), [])     # nothing was sent to the robot

        result = self.skills.install(self.ask(request_id='r1', package_digest=preview['package_digest']))
        self.assertEqual((result['status'], result['skill_id'], result['version'], result['source']),
                         ('succeeded', 'patrol', '0.1.0', 'cloud'))
        self.assertTrue(result['digest_matches'])
        installed = {s['id']: s for s in result['skills']['items']}
        self.assertEqual(installed['patrol']['digest'], release['digest'])
        self.assertEqual(self.events('skill.install')[0]['details']['source'], 'cloud')

    def test_the_same_request_id_returns_the_recorded_outcome_and_downloads_nothing_more(self):
        preview = self.skills.install_preview(self.ask())
        first = self.skills.install(self.ask(request_id='r1', package_digest=preview['package_digest']))
        before = len(self.cloud.requests)
        again = self.skills.install(self.ask(request_id='r1', package_digest=preview['package_digest']))
        self.assertEqual((again['id'], again['status']), (first['id'], 'succeeded'))
        self.assertEqual(len(self.cloud.requests), before)
        with self.assertRaises(ValueError):
            self.skills.install(self.ask(request_id='r1', package_digest='0' * 64))

    def test_a_package_that_is_not_what_the_release_records_never_reaches_the_robot(self):
        def tamper(package):
            package['files']['skill.py'] += '\nimport os  # slipped in on the way\n'
        self.cloud.tamper = tamper
        with self.assertRaises(CodedError) as caught:
            self.skills.install_preview(self.ask())
        self.assertEqual(caught.exception.code, 'skill_digest_mismatch')
        self.cloud.tamper = None
        preview = self.skills.install_preview(self.ask())                # an honest preview, then the download is changed
        self.cloud.tamper = tamper
        with self.assertRaises(CodedError) as caught:
            self.skills.install(self.ask(request_id='r2', package_digest=preview['package_digest']))
        self.assertEqual(caught.exception.code, 'skill_digest_mismatch')
        self.assertEqual(self.platform.store.list('operations'), [])
        self.assertEqual(self.platform.robot_detail({'robot_id': self.robot['id']})['skills']['items'], [])

    def test_a_download_that_breaks_the_package_rules_is_refused(self):
        tampers = {'another skill': lambda p: p.update(id='wave'),
                   'no files': lambda p: p.update(files={}),
                   'a binary file': lambda p: p['files'].update({'x.bin': 7}),
                   'an oversized file': lambda p: p['files'].update({'big.txt': 'x' * (skill_packages.MAX_FILE_BYTES + 1)})}
        for name, tamper in tampers.items():
            with self.subTest(name):
                self.cloud.tamper = tamper
                with self.assertRaises(CodedError) as caught:
                    self.skills.install_preview(self.ask())
                self.assertEqual(caught.exception.code, 'skill_download_invalid')

    def test_a_release_that_changed_between_preview_and_install_is_not_installed(self):
        preview = self.skills.install_preview(self.ask())
        del self.cloud.releases[('patrol', '0.1.0')]
        path = self.folder('patrol', description='重新发布的别的内容')
        self.publish(path)
        with self.assertRaises(CodedError) as caught:
            self.skills.install(self.ask(request_id='r3', package_digest=preview['package_digest']))
        self.assertEqual(caught.exception.code, 'skill_changed_since_preview')
        self.assertEqual(self.platform.store.list('operations'), [])

    def test_an_incompatible_release_is_explained_and_not_sent(self):
        preview = self.skills.install_preview(self.ask('wave'))          # needs an arm; this robot is a quadruped
        self.assertFalse(preview['can_install'])
        self.assertTrue(preview['compatibility']['missing_detail'])
        with self.assertRaises(ValueError) as caught:
            self.skills.install(self.ask('wave', request_id='r4', package_digest=preview['package_digest']))
        self.assertIn('不兼容', str(caught.exception))
        self.assertEqual(self.platform.store.list('operations'), [])

    def test_a_release_that_is_gone_says_so(self):
        with self.assertRaises(CloudError) as caught:
            self.skills.install_preview(self.ask('patrol', version='9.9.9'))
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
        result = subprocess.run([node, 'tests/cloud_skills_logic.cjs'], capture_output=True, text=True, timeout=120, cwd=str(root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'cloud skills logic checks passed: \d+')


if __name__ == '__main__':
    unittest.main()
