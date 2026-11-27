"""Issue #67: a model trained in simulation is a skill of its robot, and a simulated robot of that brand
and model comes with all of them.

The models here are the made-up ones of ``duck_support`` (a box, a show that counts steps). What runs is the
real management layer of a simulated duck: the skill is installed through it, started through it, and
stopped through it. No real robot is involved."""
import time
import unittest
from unittest import mock

from brainos import connector, duck_kit, duck_skills, robot_caps, skill_packages
from brainos.service import Platform
from neuxmind_spec import manifest as spec_manifest

from test_duck_kit import HAVE_MUJOCO, Case


def show(task='nod', **more):
    return {'id': task, 'title': '点头', 'summary': '低头再抬头。', 'status': duck_kit.DELIVERED, 'package': task + '.zip',
            'sha256': 'a' * 64, 'origin': {'round': 40, 'update': 4}, 'acceptance': {'fixed': [10, 10], 'independent': [8, 10]}, **more}


class Packages(unittest.TestCase):
    def test_a_skill_model_is_an_installable_skill_package_that_asks_for_the_standard_capability(self):
        made = duck_skills.package(show())
        skill = skill_packages.validate(made)
        self.assertEqual((skill['id'], skill['version'], skill['title']), ('duck.nod', '40.4.0', '点头'))
        self.assertEqual([r['id'] for r in skill['requires']], ['policy.run', 'safety.stop'])
        self.assertEqual([p['name'] for p in skill['parameters']], ['seed'])
        self.assertIn('固定组 10/10，独立组 8/10', skill['description'])
        self.assertEqual(list(made['files']), ['skill.py'])
        compile(made['files']['skill.py'], 'skill.py', 'exec')
        self.assertIn("MODEL = 'nod'", made['files']['skill.py'])

    def test_a_retrained_model_is_another_version_and_one_without_an_origin_still_has_one(self):
        self.assertEqual(duck_skills.version(show(origin={'round': 41, 'update': 0})), '41.0.0')
        for origin in (None, {}, {'round': '40', 'update': 4}, {'round': True, 'update': 1}, {'round': -1, 'update': 1}):
            with self.subTest(origin=origin):
                self.assertEqual(duck_skills.version(show(origin=origin)), '1.0.0')

    def test_it_fits_a_body_that_declares_the_model_capability_and_no_other(self):
        skill = spec_manifest.validate_skill_package({k: v for k, v in duck_skills.package(show()).items() if k != 'files'})
        body = {'schema': 'robot.adapter.v2', 'instance_id': 'b', 'name': 'B', 'adapter_version': '0.1.0', 'family': 'humanoid',
                'capabilities': [{'id': 'safety.stop', 'params': {'hardware_estop': False}}, {'id': 'system.health'}]}
        self.assertFalse(spec_manifest.check_compatibility(skill, body)['compatible'])
        body['capabilities'].append({'id': 'policy.run', 'params': {'policies': ['nod']}})
        self.assertTrue(spec_manifest.check_compatibility(skill, body)['compatible'])

    def test_every_registered_model_with_a_package_makes_a_valid_skill(self):
        ids = set()
        for one in duck_kit.PACKAGED:
            skill = skill_packages.validate(duck_skills.package(one))
            ids.add((skill['id'], skill['version']))
        self.assertEqual(len(ids), len(duck_kit.PACKAGED))


class Installing(unittest.TestCase):
    ROBOT = {'id': 'robot-1', 'endpoint': 'http://127.0.0.1:9', 'token': 't'}

    def sent(self, one, answer=None, error=None):
        seen = []

        def call(endpoint, path, token, body, timeout=None):
            seen.append((path, body))
            if error:
                raise error
            return answer or {'installed': True}
        with mock.patch.object(connector, 'call', call):
            return duck_skills.install(self.ROBOT, one), seen

    def test_the_request_is_named_after_the_content_so_changed_words_are_not_a_replay(self):
        (problem, first), (_, again) = self.sent(show()), self.sent(show())
        self.assertEqual((problem, first[0][0]), ('', '/v2/skills/install'))
        self.assertEqual(first[0][1]['request_id'], again[0][1]['request_id'])
        _, other = self.sent(show(note='新的备注。'))
        self.assertNotEqual(first[0][1]['request_id'], other[0][1]['request_id'])
        self.assertLessEqual(len(first[0][1]['request_id']), 100)

    def test_the_same_version_with_older_words_on_the_robot_is_the_skill(self):
        problem, _ = self.sent(show(), error=connector.DeviceError('版本不可变', status=409, code='version_immutable', sent=True))
        self.assertEqual(problem, '')

    def test_what_the_robot_refuses_or_does_not_confirm_is_said(self):
        problem, _ = self.sent(show(), error=connector.DeviceError('不兼容', status=409, code='incompatible', sent=True))
        self.assertIn('没有装上：不兼容', problem)
        self.assertEqual(self.sent(show(), answer={'installed': False})[0], '机器人没有确认装上')

    def test_a_skill_that_is_installed_but_could_not_be_registered_says_so(self):
        platform = mock.Mock()
        platform.store.list.return_value = []
        platform.add_skill.side_effect = ValueError('存储已关闭')
        with mock.patch.object(connector, 'call', lambda *a, **k: {'installed': True}):
            bound = duck_skills.bind(platform, {**self.ROBOT, 'project_id': 'p'}, [show()])
        self.assertEqual((bound['nod']['installed'], bound['nod']['error']), (True, '已经装上，但没有登记到技能列表：存储已关闭'))


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class OnASimulatedDuck(Case):
    def installed(self, robot_id):
        return robot_caps.installed_skills(self.app.store.get('robots', robot_id))

    def run_skill(self, robot, skill, request, parameters=None):
        return self.app.run_robot_skill({'robot_id': robot['id'], 'project_id': robot['project_id'], 'skill_id': skill,
                                         'request_id': request, 'parameters': parameters or {}, 'hardware_authorized': True})

    def live(self, robot):
        return self.app.robot_live({'robot_id': robot['id'], 'project_id': robot['project_id']})

    def ended(self, robot, op, seconds=60):
        end = time.time() + seconds
        run = {}
        while time.time() < end:
            run = self.live(robot).get('run') or {}
            if run.get('id') == op['run_id'] and run.get('status') not in ('queued', 'running'):
                return run
            time.sleep(0.1)
        self.fail(f'the run did not end within {seconds} s: {run}')

    def running(self, robot, show_id, seconds=30):
        end = time.time() + seconds
        while time.time() < end:
            status = self.app.duck.status({'robot_id': robot['id']})
            if status['show'] == show_id and status['state'] == 'running':
                return status
            time.sleep(0.05)
        self.fail(f'{show_id} did not start within {seconds} s')

    def test_it_comes_with_every_trained_model_as_an_installed_skill(self):
        robot = self.add()
        self.assertIn('policy.run', [c['id'] if isinstance(c, dict) else c for c in robot['capabilities']])
        found = self.installed(robot['id'])
        self.assertTrue(found['available'], found)
        self.assertEqual(sorted(s['id'] for s in found['items']), sorted('duck.' + task for task in self.TASKS))
        self.assertTrue(all(s['compatible'] for s in found['items']))
        self.assertEqual([p['name'] for p in found['items'][0]['parameters']], ['seed'])
        status = self.app.duck.status({'robot_id': robot['id']})
        self.assertEqual(status['skills']['nod'], {'skill': 'duck.nod', 'version': '1.4.0', 'installed': True, 'error': ''})
        self.assertEqual(sorted(status['skills']), sorted(self.TASKS))

    def test_the_models_are_skills_of_the_brand_and_model_once(self):
        robot = self.add()
        skills = [s for s in self.app.store.list('skills') if s['entrypoint'].startswith('duck.')]
        self.assertEqual(sorted(s['entrypoint'] for s in skills), sorted('duck.' + task for task in self.TASKS))
        for skill in skills:
            self.assertEqual((skill['product']['vendor'], skill['product']['model']), ('Microduck', 'AD-P102'))
            self.assertEqual((skill['requires'], skill['version']), (['policy.run', 'safety.stop'], '1.4.0'))
            self.assertEqual(skill['project_id'], robot['project_id'])
        self.add()                                                  # a second duck of the same brand and model
        self.assertEqual(len([s for s in self.app.store.list('skills') if s['entrypoint'].startswith('duck.')]), len(self.TASKS))

    def test_the_page_is_told_which_skill_a_model_is(self):
        shows = self.app.snapshot()['duck_kit']['shows']
        self.assertEqual(shows[0]['skill'], {'id': 'duck.nod', 'version': '1.4.0', 'title': shows[0]['title']})

    def test_the_skill_runs_the_model_through_the_management_layer(self):
        robot = self.add()
        op = self.run_skill(robot, 'duck.nod', 'r1', {'seed': 7})
        self.assertEqual(op['status'], 'accepted', op)
        run = self.ended(robot, op)
        self.assertEqual(run['status'], 'succeeded', run)
        self.assertEqual(run['result'], {'completed': True, 'model': 'nod', 'steps': 25, 'duration_s': 0.5})
        status = self.app.duck.status({'robot_id': robot['id']})     # and the page's stage saw the same run
        self.assertEqual((status['show'], status['state'], status['result']['phases']), ('nod', 'succeeded', ['低头', '抬头']))

    def test_a_model_that_fails_makes_the_skill_fail_with_the_reason(self):
        robot = self.add()
        run = self.ended(robot, self.run_skill(robot, 'duck.fall', 'r1'))
        self.assertEqual(run['status'], 'failed', run)
        self.assertIn('nonfoot_ground_contact', str(run['error']))
        self.assertEqual(self.ended(robot, self.run_skill(robot, 'duck.nod', 'r2'))['status'], 'succeeded')   # and it runs again

    def test_the_stop_button_ends_a_running_skill(self):
        robot = self.add()
        op = self.run_skill(robot, 'duck.long', 'r1')
        self.running(robot, 'long')
        self.app.stop_robot({'robot_id': robot['id']})
        run = self.ended(robot, op)
        self.assertEqual(run['status'], 'stopped', run)
        self.assertEqual(self.app.duck.status({'robot_id': robot['id']})['state'], 'stopped')

    def test_a_second_skill_does_not_start_while_one_is_being_performed_from_the_page(self):
        robot = self.add()
        self.app.duck.perform({'robot_id': robot['id'], 'show': 'long'})
        self.running(robot, 'long')
        run = self.ended(robot, self.run_skill(robot, 'duck.nod', 'r1'))
        self.assertEqual(run['status'], 'failed', run)
        self.assertIn('正在运行另一个技能模型', str(run['error']))
        self.assertEqual(self.app.duck.status({'robot_id': robot['id']})['show'], 'long')

    def test_a_value_that_is_not_a_seed_is_refused_by_the_robot(self):
        robot = self.add()
        op = self.run_skill(robot, 'duck.nod', 'bad', {'seed': -3})
        self.assertEqual(op['status'], 'rejected', op)

    def test_the_skill_can_be_a_step_of_a_robot_application(self):
        robot = self.add()
        skill = next(s for s in self.app.store.list('skills') if s['entrypoint'] == 'duck.nod')
        application = self.app.add_application({'project_id': robot['project_id'], 'robot_id': robot['id'], 'name': '点头', 'version': '0.1.0',
                                                'steps': [{'skill_id': skill['id'], 'parameters': {'seed': 7}, 'timeout_s': 60}]})
        self.assertEqual((application['product']['vendor'], application['steps'][0]['entrypoint']), ('Microduck', 'duck.nod'))
        target, checks = self.app.application_checks(application)
        self.assertEqual(target['id'], robot['id'])
        self.assertEqual([c for c in checks if not c['passed']], [], checks)

    def test_a_duck_from_before_gets_its_skills_when_it_is_started_again(self):
        robot = self.add()
        saved = self.app.store.get('robots', robot['id'])
        saved['sim'].pop('skills')                                   # as a duck added before this version is saved
        self.app.store.put('robots', saved)
        self.app.close()
        self.app = Platform(self.tmp.name + '/platform')
        self.addCleanup(lambda: self.app.close())
        self.app.duck.kit = self.kit
        deadline = time.monotonic() + 60
        while not self.app.store.get('robots', robot['id'])['sim'].get('skills') and time.monotonic() < deadline:
            time.sleep(0.2)
        status = self.app.duck.status({'robot_id': robot['id']})
        self.assertTrue(all(row['installed'] for row in status['skills'].values()), status)
        self.assertEqual(len([s for s in self.app.store.list('skills') if s['entrypoint'].startswith('duck.')]), len(self.TASKS))


if __name__ == '__main__':
    unittest.main()
