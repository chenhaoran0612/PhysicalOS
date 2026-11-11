"""ux02 (round 05): user-path fixes on the desktop backend.

* skill parameters are exposed to the run form as declared in skill.json
  (types, defaults, ranges) -- for installed skills and the example gallery;
* a device-side pre-execution refusal says which check failed;
* simulated robots still never produce field evidence (ADR-011): unchanged.

Uses real ``neuxmind_runtime serve`` child processes with simulated bodies only.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brainos import robot_caps  # noqa: E402
from brainos.service import Platform  # noqa: E402


class ParameterViewTests(unittest.TestCase):
    def test_keeps_what_a_form_needs(self):
        view = robot_caps.parameter_view({'name': 'speed', 'type': 'float', 'default': 0.5, 'min': 0, 'max': 1.5,
                                          'unit': 'm/s', 'description': '最大速度', 'extra': 'x'})
        self.assertEqual(view, {'name': 'speed', 'type': 'float', 'required': False, 'description': '最大速度',
                                'default': 0.5, 'min': 0, 'max': 1.5, 'unit': 'm/s'})

    def test_required_follows_runtime_rule(self):
        # absent → optional (runtime check_parameters); a default makes it optional too.
        self.assertFalse(robot_caps.parameter_view({'name': 'a', 'type': 'int'})['required'])
        self.assertTrue(robot_caps.parameter_view({'name': 'a', 'type': 'int', 'required': True})['required'])
        self.assertFalse(robot_caps.parameter_view({'name': 'a', 'type': 'int', 'required': True, 'default': 1})['required'])

    def test_minimum_maximum_aliases_choices_length(self):
        view = robot_caps.parameter_view({'name': 'm', 'type': 'enum', 'choices': ['a', 'b'], 'minimum': 1, 'maximum': 3})
        self.assertEqual((view['min'], view['max'], view['choices']), (1, 3, ['a', 'b']))
        self.assertEqual(robot_caps.parameter_view({'name': 'v', 'type': 'vector', 'length': 3})['length'], 3)

    def test_malformed_is_dropped_not_shown(self):
        self.assertIsNone(robot_caps.parameter_view('x'))
        self.assertIsNone(robot_caps.parameter_view({'type': 'int'}))
        view = robot_caps.parameter_view({'name': 'n', 'type': 'float', 'default': float('nan'),
                                          'min': float('inf'), 'max': True, 'length': True})
        self.assertNotIn('default', view)
        self.assertNotIn('min', view)
        self.assertNotIn('max', view)
        self.assertNotIn('length', view)
        self.assertEqual(robot_caps.parameter_views(None), [])
        self.assertEqual(len(robot_caps.parameter_views([{'name': 'a', 'type': 'int'}, 5])), 1)


class SimUserPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='nx-ux02-test-')
        cls.app = Platform(cls.tmp.name + '/ws')
        cls.robot = cls.app.add_sim_robot({'kind': 'quadruped'})
        cls.project = cls.robot['project_id']

    @classmethod
    def tearDownClass(cls):
        cls.app.close()
        cls.tmp.cleanup()

    def install(self, example, request_id):
        preview = self.app.preview_skill_package({'robot_id': self.robot['id'], 'example': example})
        return self.app.install_skill_package({'robot_id': self.robot['id'], 'example': example, 'request_id': request_id,
                                               'package_digest': preview['package_digest']})

    def test_gallery_and_installed_skills_carry_parameter_declarations(self):
        gallery = self.app.skill_examples({'project_id': self.project, 'robot_id': self.robot['id']})
        patrol = next(x for x in gallery['items'] if x['example'] == 'patrol')
        params = {p['name']: p for p in patrol['skill']['parameters']}
        self.assertEqual(params['loops']['type'], 'int')
        self.assertEqual(params['loops']['default'], 1)
        self.assertEqual(params['tolerance']['default'], 0.15)
        self.assertTrue(params['waypoints']['required'])
        self.assertEqual(self.install('patrol', 'ux02-i1')['status'], 'succeeded')
        detail = self.app.robot_detail({'robot_id': self.robot['id']})
        installed = next(s for s in detail['skills']['items'] if s['id'] == 'patrol')
        self.assertEqual({p['name'] for p in installed['parameters']}, {'waypoints', 'loops', 'tolerance'})
        self.assertEqual(next(p for p in installed['parameters'] if p['name'] == 'loops')['default'], 1)

    def application(self, entrypoint, parameters):
        skill = self.app.add_skill({'project_id': self.project, 'name': entrypoint, 'version': '0.1.0', 'entrypoint': entrypoint,
                                    'requires': ['base.navigate_to'], 'success_condition': '完成', 'stop_condition': '失败停止'})
        return self.app.add_application({'project_id': self.project, 'name': 'ux02 ' + entrypoint, 'version': '1.0',
                                         'robot_id': self.robot['id'],
                                         'steps': [{'skill_id': skill['id'], 'parameters': parameters, 'timeout_s': 60}]})

    def test_device_refusal_names_the_failing_check(self):
        app = self.application('not_installed_skill', {})
        with self.assertRaises(ValueError) as caught:
            self.app.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        message = str(caught.exception)
        self.assertTrue(message.startswith('设备端未通过应用执行前检查（'), message)
        self.assertIn('技能已安装', message)
        self.assertIn('not_installed_skill', message)

    def test_simulated_robot_still_never_deploys(self):
        self.install('patrol', 'ux02-i2')
        app = self.application('patrol', {'waypoints': [[0.2, 0.0]]})
        self.app.harness({'application_id': app['id']})
        op = self.app.onsite_test({'application_id': app['id'], 'hardware_authorized': True})
        self.assertTrue(op.get('simulated') or op['status'] in ('running', 'succeeded', 'failed', 'unknown'), op)
        reports = [e for e in self.app.store.list('evaluations') if e.get('application_id') == app['id'] and e.get('scope') != 'static']
        for report in reports:
            with self.assertRaisesRegex(ValueError, '仿真本体的测试结果不能作为现场证据'):
                self.app.deploy({'application_id': app['id'], 'report_id': report['id']})
        static = next(e for e in self.app.store.list('evaluations') if e.get('application_id') == app['id'] and e.get('scope') == 'static')
        with self.assertRaisesRegex(ValueError, '仿真本体的测试结果不能作为现场证据'):
            self.app.deploy({'application_id': app['id'], 'report_id': static['id']})
        self.app.stop_robot({'robot_id': self.robot['id']})          # leave the body latched, as a user would


if __name__ == '__main__':
    unittest.main()
