"""Issue #66: 仿真试运行 is by task — one saved environment and one simulated robot per task, with the
skills of the task's applications on it.

The robot models are the made-up packages of the profile tests; the simulated robot is the real edge
runtime with the MuJoCo body in a child process. Those tests need the ``mujoco`` package."""
import importlib.util
import tempfile
import time
import unittest

import test_duck_kit
from brainos import robot_scope, task_sim
from brainos.errors import CodedError
from brainos.service import Platform
from test_sim_profiles import ARM, BUILTIN, DOG, MADE_UP, packages

HAVE_MUJOCO = importlib.util.find_spec('mujoco') is not None
DOG_PRODUCT = {'vendor': DOG['vendor'], 'model': DOG['model']}
GENERIC_STEPS = (('status_report', {}),)     # issue #69: the one bundled package every brand and model may install


def make_task(app, name, product, family, steps, bind=True):
    """A task, with an application of ``product`` bound to it (made for a robot record of that product)."""
    task = app.add_project({'name': name, 'objective': '从门口走到货架'})
    if product is None:
        return task
    robot = app.add_robot({'project_id': robot_scope.HOME, 'name': name + '的真机', 'family': family, **product})
    made = []
    for entry, parameters in steps:
        skill = app.add_skill({'project_id': robot_scope.HOME, 'name': '技能 ' + entry, 'version': '1.0',
                               'requires': ['navigation.goal'], 'entrypoint': entry, 'success_condition': 'done',
                               'stop_condition': 'timeout', 'model_ids': [], 'robot_id': robot['id']})
        made.append({'skill_id': skill['id'], 'parameters': parameters, 'timeout_s': 30})
    application = app.add_application({'project_id': robot_scope.HOME, 'name': name + '应用', 'version': '1.0',
                                       'robot_id': robot['id'], 'steps': made})
    if bind:
        app.task_apps.bind({'project_id': task['id'], 'application_id': application['id']})
    return task


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name + '/platform'
        self.models = packages(self.tmp.name)
        self.app = self.open()
        self.sim = self.app.task_sim

    def open(self):
        app = Platform(self.root)
        self.addCleanup(lambda: app.close())
        app.sim_profiles.profiles, app.sim_profiles.models = MADE_UP, self.models
        return app

    def task(self, name='巡检', product=DOG_PRODUCT, family='quadruped', steps=(('patrol', {'loops': 2}),), bind=True):
        return make_task(self.app, name, product, family, steps, bind)

    def refused(self, code, call, *args):
        with self.assertRaises(CodedError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)

    def built(self, task, seconds=120):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            view = self.sim.view({'project_id': task['id']})
            if not (view['build'] or {}).get('busy'):
                return view
            time.sleep(0.2)
        self.fail('the environment was not built in time')


class Listing(Case):
    def test_every_task_is_listed_with_its_brand_and_model_and_how_it_is_simulated(self):
        dog, bare = self.task(), self.task('没定', product=None)
        ghost = self.task('没模型', product={'vendor': 'Nobody', 'model': 'X-1'})
        example = self.task('示例', product={'vendor': BUILTIN['vendor'], 'model': BUILTIN['model']})
        rows = {row['task']['id']: row for row in self.sim.list()['tasks']}
        self.assertEqual(set(rows), {dog['id'], bare['id'], ghost['id'], example['id']})
        self.assertEqual(rows[dog['id']]['label'], '智身科技 钢镚 L2-U')
        self.assertEqual(rows[dog['id']]['applications'][0]['name'], '巡检应用')
        self.assertEqual(rows[bare['id']]['problem']['code'], 'task_sim_no_product')
        self.assertEqual(rows[example['id']]['model'], {'kind': 'builtin', 'name': rows[example['id']]['model']['name'], 'staged': True})
        if HAVE_MUJOCO:
            self.assertEqual((rows[dog['id']]['model']['kind'], rows[dog['id']]['problem']), ('profile', None))
            self.assertEqual(rows[ghost['id']]['problem']['code'], 'sim_no_robot_model')
        else:
            self.assertEqual(rows[dog['id']]['problem']['code'], 'sim_engine_missing')
        self.assertTrue(all(row['environment'] is None and row['robot'] is None for row in rows.values()))

    def test_the_page_logic(self):
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        result = subprocess.run([node, 'tests/task_sim_logic.cjs'], capture_output=True, text=True, timeout=120,
                                cwd=str(Path(__file__).resolve().parents[1]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'task_sim_logic: \d+ checks passed')

    def test_another_spelling_of_the_brand_and_model_finds_the_same_profile(self):
        task = self.task(product={'vendor': 'zs', 'model': 'L2U'})
        self.assertEqual(self.sim._profile(self.app.task_apps.product(task['id']))['id'], 'acme-dog')

    def test_the_home_of_the_robots_is_not_a_task_and_an_unknown_task_is_refused(self):
        self.assertNotIn(robot_scope.HOME, [row['task']['id'] for row in self.sim.list()['tasks']])
        for key in (robot_scope.HOME, 'project-nope', None):
            with self.assertRaises(ValueError):
                self.sim.view({'project_id': key})

    def test_building_is_refused_without_a_brand_and_model_and_without_consent_for_the_agent(self):
        self.refused('task_sim_no_product', self.sim.build, {'project_id': self.task(product=None)['id'], 'mode': 'empty'})
        if HAVE_MUJOCO:
            self.refused('authorization_required', self.sim.build, {'project_id': self.task('二')['id'], 'mode': 'agent'})
            with self.assertRaises(ValueError):
                self.sim.build({'project_id': self.task('三')['id'], 'mode': 'something'})

    def test_entering_needs_a_saved_environment(self):
        self.refused('task_sim_not_built', self.sim.enter, {'project_id': self.task()['id']})

    def test_a_build_the_app_was_closed_in_is_marked_failed_and_the_saved_environment_stays(self):
        task = self.task()
        self.app.store.put(task_sim.KIND, {'id': 'task-sim-x', 'project_id': task['id'], 'saved': {'scene_id': 's1', 'source': 'empty', 'product': DOG_PRODUCT, 'model': {'kind': 'profile'}},
                                           'build': {'id': 's2', 'status': 'generating', 'source': 'agent'}})
        self.app.close()
        view = self.open().task_sim.view({'project_id': task['id']})
        self.assertEqual((view['build']['status'], view['build']['error'], view['build']['busy']), ('failed', task_sim.INTERRUPTED, False))
        self.assertEqual(view['environment']['scene_id'], 's1')

    def test_the_skills_of_the_bound_applications_are_listed_in_step_order_with_their_values(self):
        task = self.task(steps=(('goto_place', {'place': '货架'}), ('patrol', {'loops': 2}), ('goto_place', {'place': '门口'})))
        view = self.sim.view({'project_id': task['id']})
        self.assertEqual([s['id'] for s in view['skills']], ['goto_place', 'patrol'])
        goto = view['skills'][0]
        self.assertEqual([(u['step'], u['parameters']) for u in goto['uses']], [(1, {'place': '货架'}), (3, {'place': '门口'})])
        self.assertEqual((goto['installed'], goto['has_package']), (False, True))       # a bundled example of that id
        self.assertEqual(self.sim.view({'project_id': self.task('别的', steps=(('made.up', {}),))['id']})['skills'][0]['has_package'], False)


class Example(Case):
    """A task of the example quadruped: nothing to generate, the robot is the built-in simulation."""

    def enter(self):
        task = self.task('示例', product={'vendor': BUILTIN['vendor'], 'model': BUILTIN['model']})
        built = self.sim.build({'project_id': task['id']})
        self.assertEqual((built['environment']['source'], built['build']), ('stage', None))
        return task, self.sim.enter({'project_id': task['id']})

    def test_entering_starts_the_tasks_own_robot_and_installs_the_skills_of_its_applications(self):
        task, view = self.enter()
        robot = self.app.store.get('robots', view['robot']['id'])
        self.assertEqual((robot['project_id'], robot['sim']['task_id'], robot['name']), (robot_scope.HOME, task['id'], '示例 仿真'))
        self.assertEqual((view['robot']['state'], view['robot']['connected'], view['robot']['picture']), ('running', True, False))
        self.assertEqual(view['installed_now'], [{'id': 'patrol', 'installed': True}])
        patrol = view['skills'][0]
        self.assertEqual((patrol['id'], patrol['installed'], patrol['compatible'], patrol['uses'][0]['parameters']), ('patrol', True, True, {'loops': 2}))
        self.assertTrue(patrol['parameters'])
        again = self.sim.enter({'project_id': task['id']})                     # the same robot, nothing installed twice
        self.assertEqual((again['robot']['id'], again['installed_now']), (robot['id'], []))
        self.assertEqual(len([r for r in self.app.store.list('robots') if r.get('sim')]), 1)

    def test_a_skill_of_the_task_runs_on_that_robot(self):
        task, view = self.enter()
        run = self.app.run_robot_skill({'robot_id': view['robot']['id'], 'skill_id': 'patrol', 'hardware_authorized': True,
                                        'request_id': 'r1', 'project_id': task['id'],
                                        'parameters': {'waypoints': [[0.3, 0.0]], 'loops': 1}})
        self.assertEqual(run['status'], 'accepted', run)

    def test_a_robot_that_was_closed_is_started_again_and_one_that_was_deleted_is_made_again(self):
        task, view = self.enter()
        first = view['robot']['id']
        self.app.sims.api_stop({'robot_id': first})
        self.assertEqual(self.sim.view({'project_id': task['id']})['robot']['state'], 'stopped')
        self.assertEqual(self.sim.enter({'project_id': task['id']})['robot'], {**view['robot']})
        self.app.sims.remove({'robot_id': first})
        self.assertIsNone(self.sim.view({'project_id': task['id']})['robot'])
        self.assertNotEqual(self.sim.enter({'project_id': task['id']})['robot']['id'], first)

    def test_deleting_the_task_takes_its_simulated_robot_and_record_with_it(self):
        task, view = self.enter()
        self.app.task_remove.remove({'project_id': task['id']})
        self.assertEqual([r for r in self.app.store.list('robots') if r.get('sim')], [])
        self.assertEqual(self.app.store.list(task_sim.KIND), [])
        self.assertFalse((self.app.sims.root / view['robot']['id']).exists())

    def test_a_robot_that_does_not_start_is_not_left_behind_and_trying_again_makes_no_second_one(self):
        task = self.task('示例', product={'vendor': BUILTIN['vendor'], 'model': BUILTIN['model']})
        self.sim.build({'project_id': task['id']})
        real = self.app.sims.start
        self.app.sims.start = lambda robot_id: (_ for _ in ()).throw(ValueError('起不来'))
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.sim.enter({'project_id': task['id']})
            self.assertEqual([r['name'] for r in self.app.store.list('robots') if r.get('sim')], [])
        self.app.sims.start = real
        self.assertEqual(self.sim.enter({'project_id': task['id']})['robot']['name'], '示例 仿真')

    def test_an_environment_of_another_brand_and_model_cannot_be_entered_and_building_again_replaces_the_robot(self):
        task, view = self.enter()
        old = view['robot']['id']
        binding = self.app.task_apps.bindings(task['id'])[0]
        self.app.task_apps.unbind({'project_id': task['id'], 'application_id': binding['application_id']})
        self.task('臂', product={'vendor': ARM['vendor'], 'model': ARM['model']}, family='manipulator', bind=False)
        application = next(a for a in self.app.store.list('applications') if a['name'] == '臂应用')
        self.app.task_apps.bind({'project_id': task['id'], 'application_id': application['id']})
        self.assertTrue(self.sim.view({'project_id': task['id']})['environment']['stale'])
        self.assertIn('另一个品牌型号', self.refused('task_sim_not_built', self.sim.enter, {'project_id': task['id']}))
        if not HAVE_MUJOCO:
            return
        self.sim.build({'project_id': task['id'], 'mode': 'empty'})
        self.built(task)
        deadline = time.monotonic() + 30                    # the robot follows right after the environment is saved
        while time.monotonic() < deadline and self.sim.view({'project_id': task['id']})['robot'] is not None:
            time.sleep(0.2)
        after = self.sim.view({'project_id': task['id']})
        self.assertEqual((after['environment']['stale'], after['environment']['source'], after['robot']), (False, 'empty', None))
        self.assertNotIn(old, [r['id'] for r in self.app.store.list('robots')])

    def test_removing_the_environment_removes_the_robot_and_the_task_can_build_again(self):
        task, view = self.enter()
        self.assertEqual(self.sim.remove({'project_id': task['id']}), {'removed': True})
        after = self.sim.view({'project_id': task['id']})
        self.assertEqual((after['environment'], after['robot']), (None, None))
        self.assertEqual(self.sim.remove({'project_id': task['id']}), {'removed': False})


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class Physics(Case):
    """A task of a profile with a robot model: the environment is built, checked, saved, and used."""

    def test_an_empty_floor_is_built_once_saved_and_the_robot_starts_in_it_with_the_picture(self):
        task = self.task(steps=GENERIC_STEPS)
        started = self.sim.build({'project_id': task['id'], 'mode': 'empty'})
        self.assertEqual((started['build']['busy'], started['environment']), (True, None))
        self.refused('sim_scene_busy', self.sim.build, {'project_id': task['id'], 'mode': 'empty'})
        view = self.built(task)
        self.assertIsNone(view['build'], view)
        saved = view['environment']
        self.assertEqual((saved['source'], saved['source_name'], saved['stale'], saved['product']), ('empty', '空场地', False, DOG_PRODUCT))
        if saved['preview']:
            self.assertTrue(self.sim.preview(saved['scene_id']).startswith(b'\x89PNG'))
        self.assertIsNone(self.sim.preview('task-scene-nope'))
        entered = self.sim.enter({'project_id': task['id']})
        robot = self.app.store.get('robots', entered['robot']['id'])
        self.assertEqual((entered['robot']['picture'], robot['sim']['profile_id'], robot['family'], {k: robot['product'][k] for k in DOG_PRODUCT}), (True, 'acme-dog', 'wheel_legged', DOG_PRODUCT))
        self.assertEqual(entered['installed_now'], [{'id': 'status_report', 'installed': True}])
        # Saved: another opening of the workspace finds it and builds nothing.
        self.app.close()
        again = self.open().task_sim.view({'project_id': task['id']})
        self.assertEqual((again['environment']['scene_id'], again['build'], again['robot']['id']), (saved['scene_id'], None, robot['id']))

    def test_a_new_build_replaces_the_saved_one_and_the_robot_keeps_its_skills(self):
        task = self.task(steps=GENERIC_STEPS)
        self.sim.build({'project_id': task['id'], 'mode': 'empty'})
        first = self.built(task)['environment']['scene_id']
        robot_id = self.sim.enter({'project_id': task['id']})['robot']['id']
        self.sim.build({'project_id': task['id'], 'mode': 'empty'})
        view = self.built(task)
        self.assertNotEqual(view['environment']['scene_id'], first)
        self.assertFalse((self.app.mujoco.scenes_dir / first).exists())
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not self.sim.view({'project_id': task['id']})['robot']['connected']:
            time.sleep(0.3)
        view = self.sim.view({'project_id': task['id']})
        self.assertEqual((view['robot']['id'], view['robot']['connected']), (robot_id, True))
        self.assertTrue(view['skills'][0]['installed'])

    def test_a_bundled_skill_of_another_brand_and_model_is_not_installed_and_the_simulation_is_entered_all_the_same(self):
        # Issue #69 (merged with #66): the bundled patrol is for the example quadruped only, not for this made-up dog.
        task = self.task()
        self.sim.build({'project_id': task['id'], 'mode': 'empty'})
        self.built(task)
        entered = self.sim.enter({'project_id': task['id']})
        self.assertEqual([(row['id'], row['installed']) for row in entered['installed_now']], [('patrol', False)])
        self.assertIn('属于', entered['installed_now'][0]['error'])
        self.assertTrue(self.app.store.get('robots', entered['robot']['id']))

    def test_a_build_that_fails_leaves_the_saved_environment_in_use(self):
        task = self.task()
        self.sim.build({'project_id': task['id'], 'mode': 'empty'})
        saved = self.built(task)['environment']['scene_id']
        real = self.app.mujoco.check
        self.app.mujoco.check = lambda *a, **k: (_ for _ in ()).throw(CodedError('建不出来', 'sim_check_failed'))
        self.addCleanup(lambda: setattr(self.app.mujoco, 'check', real))
        self.sim.build({'project_id': task['id'], 'mode': 'empty'})
        view = self.built(task)
        self.assertEqual((view['build']['status'], view['build']['error'], view['environment']['scene_id']), ('failed', '建不出来', saved))


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class WrittenByTheAgent(Case):
    """The environment is written by the engineering agent (the scripted stand-in of the MuJoCo tests)."""

    def setUp(self):
        super().setUp()
        import contextlib
        from unittest import mock
        from brainos import agent_runtime
        from codex_standin import CodexStandIn
        from test_mujoco_sim import REAL_AVAILABLE, SceneModel
        from test_product import Server
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        if not agent_runtime.binary_path():
            stack.enter_context(mock.patch.object(agent_runtime, 'AgentProcess', CodexStandIn))
            stack.enter_context(mock.patch.object(agent_runtime, 'available', lambda: {**REAL_AVAILABLE(), 'available': True}))
        SceneModel.requests = []
        self.model = SceneModel
        self.server = Server(SceneModel)
        self.addCleanup(self.server.close)
        self.task_ = self.task(steps=(('goto_place', {'place': '门口'}),))
        self.agent_model = self.app.engineering.model_add({'project_id': self.task_['id'], 'name': 'Fixture', 'model': 'fixture-agent',
                                                           'base_url': self.server.url + '/v1', 'api_key': 'fixture-' + 'k' * 20})
        self.ask = {'project_id': self.task_['id'], 'mode': 'agent', 'model_id': self.agent_model['id'], 'data_authorized': True, 'note': '货架靠北墙'}

    def test_the_agent_gets_the_task_and_its_applications_and_what_it_wrote_is_saved(self):
        import json
        from pathlib import Path
        import mujoco_support as ms
        from test_mujoco_sim import writes
        self.model.command = writes(ms.scene())
        started = self.sim.build(self.ask)
        self.assertEqual((started['build']['source'], started['build']['busy']), ('agent', True))
        view = self.built(self.task_)
        self.assertIsNone(view['build'], view)
        saved = view['environment']
        self.assertEqual((saved['source'], saved['source_name'], saved['places']), ('agent', '工程助手生成', ['门口', '墙后']))
        self.assertEqual(len(saved['lines']), 5)
        record = self.sim._record(self.task_['id'])
        session = self.app.store.get('agent_sessions', record['saved']['session_id'])
        self.assertEqual((session['status'], session['project_id']), ('completed', self.task_['id']))
        self.assertIn('机器人任务', session['objective'])
        self.assertIn('货架靠北墙', session['objective'])
        seen = json.loads((Path(self.app.store.root) / 'engineering' / session['id'] / 'workspace' / 'seen.json').read_text())
        self.assertNotIn('application', seen)
        self.assertEqual(seen['task'], {'name': '巡检', 'objective': '从门口走到货架'})
        self.assertEqual([(a['name'], a['steps'][0]['skill'], a['steps'][0]['parameters']) for a in seen['applications']],
                         [('巡检应用', '技能 goto_place', {'place': '门口'})])
        self.assertEqual((seen['robot']['brand'], seen['extra_note']), ('智身科技', '货架靠北墙'))
        # The robot of the task knows the places of the environment.
        entered = self.sim.enter({'project_id': self.task_['id']})
        robot = self.app.store.get('robots', entered['robot']['id'])
        places = next(c for c in robot['manifest_v2']['capabilities'] if c['id'] == 'base.navigate_to_place')['params']['places']
        self.assertEqual(sorted(places), sorted(['门口', '墙后']))

    def test_a_scene_outside_the_format_fails_the_build_and_giving_up_stops_the_agent(self):
        self.model.command = "printf '{\"schema\": \"other\"}' > scene.json"
        self.sim.build(self.ask)
        view = self.built(self.task_)
        self.assertEqual((view['build']['status'], view['environment']), ('failed', None))
        self.assertIn('不符合格式', view['build']['error'])
        self.model.command = 'sleep 30'
        self.sim.build(self.ask)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and self.sim.view({'project_id': self.task_['id']})['build']['status'] != 'generating':
            time.sleep(0.1)
        given_up = self.sim.cancel({'project_id': self.task_['id']})
        self.assertEqual((given_up['build']['status'], given_up['build']['busy'], given_up['build']['error']), ('failed', False, '已取消这次构建'))
        given = self.sim._record(self.task_['id'])['build']['id']
        self.sim.build({'project_id': self.task_['id'], 'mode': 'empty'})        # nothing is in the way of the next build
        self.assertEqual(self.built(self.task_)['environment']['source'], 'empty')
        deadline = time.monotonic() + 30                                         # the folder of the build that was given up goes
        while time.monotonic() < deadline and (self.app.mujoco.scenes_dir / given).exists():
            time.sleep(0.2)
        self.assertFalse((self.app.mujoco.scenes_dir / given).exists())


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class Duck(test_duck_kit.Case):
    """A task of the Microduck: its own stage and skill models, nothing to generate (the made-up kit of the duck tests)."""

    def test_the_task_enters_the_ducks_own_stage_and_its_skill_models_can_be_performed(self):
        from brainos import duck_kit
        task = make_task(self.app, '表演', {'vendor': duck_kit.VENDOR, 'model': duck_kit.MODEL}, 'biped', (('bow', {}),))
        sim = self.app.task_sim
        view = sim.view({'project_id': task['id']})
        self.assertEqual((view['model']['kind'], view['model']['staged'], view['problem']), ('duck', True, None))
        self.assertEqual(sim.build({'project_id': task['id']})['environment']['source'], 'stage')
        entered = sim.enter({'project_id': task['id']})
        self.assertEqual((entered['robot']['duck'], entered['robot']['connected'], entered['installed_now'], entered['skills']), (True, True, [], []))
        self.assertEqual(sorted(s['id'] for s in entered['shows']), sorted(self.TASKS))
        robot = self.app.store.get('robots', entered['robot']['id'])
        self.assertEqual((robot['sim']['task_id'], robot['sim']['profile_id']), (task['id'], duck_kit.PROFILE_ID))
        run = self.app.duck.perform({'robot_id': robot['id'], 'show': 'nod', 'project_id': task['id']})
        self.assertEqual(self.wait(robot['id'], run['run_id'])['state'], 'succeeded')


if __name__ == '__main__':
    unittest.main()
