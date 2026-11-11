"""ux03 (round 05): application steps get the skill's parameter declarations.

* a parameter's ``title`` (human label) reaches the forms;
* a project skill can carry parameter declarations (skill.json ``parameters``), sanitized like the
  run form's; skills registered without them keep exactly the old record and digest;
* the declarations are descriptive only: application parameters are still free JSON objects that the
  device validates (no new server-side check, no new execution right).
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import robot_caps  # noqa: E402
from brainos.service import Platform  # noqa: E402


class TitleTests(unittest.TestCase):
    def test_title_is_kept_trimmed_and_bounded(self):
        self.assertEqual(robot_caps.parameter_view({'name': 'loops', 'type': 'int', 'title': ' 圈数 '})['title'], '圈数')
        self.assertEqual(len(robot_caps.parameter_view({'name': 'a', 'type': 'int', 'title': 'x' * 500})['title']), 60)
        for bad in ('', '  ', 5, None, ['x']):
            self.assertNotIn('title', robot_caps.parameter_view({'name': 'a', 'type': 'int', 'title': bad}))


class ProjectSkillParameterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nx-ux03-test-')
        self.p = Platform(self.tmp.name)
        self.pid = self.p.add_project({'name': 'ux03', 'objective': '参数表单'})['id']

    def tearDown(self):
        self.p.close()
        self.tmp.cleanup()

    def skill(self, **extra):
        return self.p.add_skill({'project_id': self.pid, 'name': '巡逻', 'version': '0.1.0', 'entrypoint': 'patrol',
                                 'requires': ['base.navigate_to'], 'model_ids': [], 'success_condition': '到达全部航点',
                                 'stop_condition': '急停或超时', **extra})

    def test_declarations_are_saved_sanitized(self):
        s = self.skill(parameters=[
            {'name': 'loops', 'type': 'int', 'default': 1, 'title': '圈数', 'unit': '圈', 'evil': '<script>'},
            {'name': 'tolerance', 'type': 'float', 'default': float('nan'), 'min': 0},
            'not a declaration', {'type': 'int'}])
        self.assertEqual(s['parameters'], [
            {'name': 'loops', 'type': 'int', 'required': False, 'description': '', 'default': 1, 'unit': '圈', 'title': '圈数'},
            {'name': 'tolerance', 'type': 'float', 'required': False, 'description': '', 'min': 0}])
        self.assertEqual(self.p.store.get('skills', s['id'])['parameters'], s['parameters'])

    def test_without_declarations_the_record_is_unchanged(self):
        plain, empty = self.skill(), self.skill(parameters=[])
        self.assertNotIn('parameters', plain)
        self.assertNotIn('parameters', empty)
        strip = lambda s: {k: v for k, v in s.items() if k not in ('id', 'created_at', 'digest')}
        self.assertEqual(strip(plain), strip(empty))

    def test_bad_declarations_are_refused(self):
        with self.assertRaises(ValueError):
            self.skill(parameters={'name': 'loops'})
        with self.assertRaises(ValueError):
            self.skill(parameters=[{'name': f'p{i}', 'type': 'int'} for i in range(51)])

    def test_declarations_do_not_constrain_application_parameters(self):
        # The device validates parameters (it has the authoritative skill.json); the console only describes them.
        s = self.skill(parameters=[{'name': 'loops', 'type': 'int', 'required': True}])
        robot = self.p.add_robot({'project_id': self.pid, 'name': '底盘', 'family': 'mobile'})
        app = self.p.add_application({'project_id': self.pid, 'name': '巡逻', 'version': '1.0', 'robot_id': robot['id'],
                                      'steps': [{'skill_id': s['id'], 'parameters': {'anything': [1, 2]}, 'timeout_s': 30}]})
        self.assertEqual(app['steps'][0]['parameters'], {'anything': [1, 2]})
        self.assertEqual(app['steps'][0]['skill_digest'], s['digest'])


if __name__ == '__main__':
    unittest.main()
