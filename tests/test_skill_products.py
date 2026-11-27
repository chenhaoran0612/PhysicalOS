"""Issue #69: a bundled skill package is for the brands and models it names, or for every robot.

「安装技能包」 lists what this robot's brand and model can install plus the packages for every robot;
a simulated robot and a real one of the same brand and model get the same list. The sim tests start
real ``neuxmind_runtime serve`` child processes with the built-in bodies; nothing touches hardware.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import runtime_bridge, skill_products  # noqa: E402
from brainos.errors import CodedError  # noqa: E402
from brainos.service import Platform  # noqa: E402

L2U = {'vendor': '智身科技', 'model': '钢镚 L2-U'}
DUCK = {'vendor': 'Microduck', 'model': 'AD-P102'}
PLACE_SKILLS = ['free_walk', 'goto_place', 'patrol_places']


def names(product):
    return sorted(name for name in skill_products.table() if skill_products.offered(name, product))


class IndexTests(unittest.TestCase):
    def test_every_bundled_package_says_who_it_is_for(self):
        root = runtime_bridge.examples_dir()
        folders = sorted(p.name for p in root.iterdir() if (p / 'skill.json').is_file())
        table = skill_products.table()
        self.assertEqual(sorted(table), folders)
        for name, entry in table.items():
            self.assertTrue(entry['generic'] or entry['products'], name)
            self.assertFalse(entry['generic'] and entry['products'], name)

    def test_the_status_package_is_for_every_robot(self):
        for product in (L2U, DUCK, {'vendor': 'Acme', 'model': 'X1'}, None):
            self.assertIn('status_report', names(product), product)

    def test_a_product_gets_its_own_packages_and_the_generic_ones(self):
        self.assertEqual(names(L2U), sorted(PLACE_SKILLS + ['status_report']))
        self.assertEqual(names(DUCK), ['status_report'])
        # The built-in simulated quadruped declares the place capabilities so that these can be tried without hardware.
        self.assertEqual(names({'vendor': 'NeuxMind 示例', 'model': '通用四足'}), sorted(PLACE_SKILLS + ['patrol', 'status_report']))
        self.assertEqual(names({'vendor': '宇树科技', 'model': 'Go2'}), ['status_report'])
        self.assertEqual(names({'vendor': 'NeuxMind 示例', 'model': '通用六轴机械臂'}), ['status_report', 'wave'])

    def test_another_spelling_of_the_brand_and_model_is_the_same_product(self):
        self.assertEqual(names({'vendor': 'zsibot', 'model': 'l2u'}), names(L2U))

    def test_a_robot_without_brand_and_model_gets_only_the_generic_ones(self):
        self.assertEqual(names(None), ['status_report'])

    def test_a_package_the_index_does_not_name_is_offered_to_nobody(self):
        self.assertFalse(skill_products.offered('not_in_the_index', L2U))
        self.assertEqual(skill_products.scope('not_in_the_index'), {'generic': False, 'products': []})


class IndexFileTests(unittest.TestCase):
    def read(self, body):
        with tempfile.TemporaryDirectory(prefix='nx-skill-products-') as tmp:
            Path(tmp, skill_products.INDEX).write_text(body if isinstance(body, str) else json.dumps(body), 'utf-8')
            return skill_products.table(Path(tmp))

    def test_a_missing_or_broken_index_offers_nothing(self):
        with tempfile.TemporaryDirectory(prefix='nx-skill-products-') as tmp:
            self.assertEqual(skill_products.table(Path(tmp)), {})
        self.assertEqual(self.read('{not json'), {})
        self.assertEqual(self.read({'schema': 'something.else', 'packages': {'a': {'generic': True}}}), {})

    def test_entries_that_say_nothing_usable_are_left_out(self):
        table = self.read({'schema': skill_products.SCHEMA, 'packages': {
            'both': {'generic': True, 'products': [L2U]},
            'neither': {},
            'blank': {'products': [{'vendor': ' ', 'model': 'X'}]},
            'not_a_dict': 'generic',
            'good': {'products': [L2U, 'junk']},
            'all': {'generic': True}}})
        self.assertEqual(sorted(table), ['all', 'good'])
        self.assertEqual(table['good'], {'generic': False, 'products': [{**L2U, 'alias': False}]})

    def test_another_spelling_counts_but_is_not_named(self):
        table = self.read({'schema': skill_products.SCHEMA, 'packages': {
            'p': {'products': [L2U, {'vendor': 'acme', 'model': 'x1', 'alias': True}]}}})
        self.assertTrue(skill_products.offered('p', {'vendor': 'ACME', 'model': 'X1'}, table))
        self.assertEqual(skill_products.view('p', table), {'generic': False, 'products': ['智身科技 钢镚 L2-U']})


class GalleryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-issue69-test-')
        self.app = Platform(self.tmp.name + '/ws')

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def gallery(self, robot):
        return self.app.skill_examples({'project_id': robot['project_id'], 'robot_id': robot['id']})

    def test_the_list_for_a_robot_has_its_packages_and_the_generic_ones(self):
        quad = self.app.add_sim_robot({'kind': 'quadruped'})
        arm = self.app.add_sim_robot({'kind': 'arm', 'project_id': quad['project_id']})
        for_quad, for_arm = self.gallery(quad), self.gallery(arm)
        self.assertEqual([x['example'] for x in for_quad['items']], sorted(PLACE_SKILLS + ['patrol', 'status_report']))
        self.assertEqual([x['example'] for x in for_arm['items']], ['status_report', 'wave'])
        self.assertEqual((for_quad['left_out'], for_arm['left_out']), (1, 4))
        scopes = {x['example']: x['scope'] for x in for_quad['items']}
        self.assertTrue(scopes['status_report']['generic'])
        self.assertFalse(scopes['patrol']['generic'])
        self.assertEqual(scopes['patrol']['products'], ['NeuxMind 示例 通用四足'])
        self.assertTrue(for_quad['product'])

    def test_without_a_robot_every_package_is_listed(self):
        quad = self.app.add_sim_robot({'kind': 'quadruped'})
        gallery = self.app.skill_examples({'project_id': quad['project_id']})
        self.assertEqual(len(gallery['items']), 6)
        self.assertEqual(gallery['left_out'], 0)

    def test_a_package_of_another_product_is_refused_before_anything_is_sent(self):
        quad = self.app.add_sim_robot({'kind': 'quadruped'})
        arm = self.app.add_sim_robot({'kind': 'arm', 'project_id': quad['project_id']})
        for robot, example in ((quad, 'wave'), (arm, 'goto_place')):
            quad = robot
            with self.assertRaises(CodedError) as caught:
                self.app.preview_skill_package({'robot_id': quad['id'], 'example': example})
            self.assertEqual(caught.exception.code, 'skill_other_model')
            with self.assertRaises(CodedError) as caught:
                self.app.install_skill_package({'robot_id': quad['id'], 'example': example,
                                                'request_id': 'r-' + example, 'package_digest': 'x' * 64})
            self.assertEqual(caught.exception.code, 'skill_other_model')
        self.assertEqual([o for o in self.app.store.list('operations') if o.get('kind') == 'skill_install'], [])

    def test_the_generic_package_installs_on_both_robots(self):
        quad = self.app.add_sim_robot({'kind': 'quadruped'})
        arm = self.app.add_sim_robot({'kind': 'arm', 'project_id': quad['project_id']})
        for robot in (quad, arm):
            preview = self.app.preview_skill_package({'robot_id': robot['id'], 'example': 'status_report'})
            self.assertTrue(preview['can_install'], preview['compatibility'])
            done = self.app.install_skill_package({'robot_id': robot['id'], 'example': 'status_report',
                                                   'request_id': 'i-' + robot['id'],
                                                   'package_digest': preview['package_digest']})
            self.assertEqual(done['status'], 'succeeded', done)


if __name__ == '__main__':
    unittest.main()
