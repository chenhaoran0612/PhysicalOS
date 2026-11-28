"""Issues #66 and #75: 仿真试运行 is by task — one simulated robot per task, and every skill of the task has its own
environment (built once, saved with the skill; the example robots and the duck's skill models bring their own).

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


def skill_of(view, skill_id):
    return next(s for s in view['skills'] if s['id'] == skill_id)


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

    def built(self, task, skill_id, seconds=120):
        """The row of ``skill_id`` once the build of its environment has ended."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            view = self.sim.view({'project_id': task['id']})
            if not (skill_of(view, skill_id)['build'] or {}).get('busy'):
                return skill_of(view, skill_id)
            time.sleep(0.2)
        self.fail('the environment was not built in time')

    def connected(self, task, skill_id=None, seconds=120):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            view = self.sim.view({'project_id': task['id']})
            robot = view['robot'] or {}
            if robot.get('connected') and (skill_id is None or robot.get('skill') == skill_id):
                return view
            time.sleep(0.3)
        self.fail('the task\'s robot did not come back in time')


class Listing(Case):
    def test_every_task_is_listed_with_its_brand_and_model_how_it_is_simulated_and_its_skills(self):
        dog, bare = self.task(), self.task('没定', product=None)
        ghost = self.task('没模型', product={'vendor': 'Nobody', 'model': 'X-1'})
        example = self.task('示例', product={'vendor': BUILTIN['vendor'], 'model': BUILTIN['model']})
        rows = {row['task']['id']: row for row in self.sim.list()['tasks']}
        self.assertEqual(set(rows), {dog['id'], bare['id'], ghost['id'], example['id']})
        self.assertEqual(rows[dog['id']]['label'], '智身科技 钢镚 L2-U')
        self.assertEqual(rows[dog['id']]['applications'][0]['name'], '巡检应用')
        self.assertEqual(rows[bare['id']]['problem']['code'], 'task_sim_no_product')
        self.assertEqual(rows[example['id']]['model'], {'kind': 'builtin', 'name': rows[example['id']]['model']['name'], 'staged': True})
        # The example robot brings its own stage: every skill of it is ready to be entered, nothing is built.
        self.assertEqual(rows[example['id']]['counts']['ready'], rows[example['id']]['counts']['skills'])
        if HAVE_MUJOCO:
            self.assertEqual((rows[dog['id']]['model']['kind'], rows[dog['id']]['problem']), ('profile', None))
            self.assertEqual(rows[ghost['id']]['problem']['code'], 'sim_no_robot_model')
        else:
            self.assertEqual(rows[dog['id']]['problem']['code'], 'sim_engine_missing')
        self.assertEqual(rows[dog['id']]['counts']['ready'], 0)
        self.assertTrue(all(row['robot'] is None and 'skills' not in row for row in rows.values()))       # the list reads no robot

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

    def test_a_task_made_with_an_example_robot_is_simulated_as_that_example_whatever_the_spelling(self):
        # An example robot reports itself as neuxmind-sim / sim-quadruped; that is what its applications carry.
        task = self.task('示例', product={'vendor': 'neuxmind-sim', 'model': 'sim-quadruped'})
        row = self.sim.view({'project_id': task['id']})
        self.assertEqual((row['problem'], row['model']['kind'], row['model']['staged']), (None, 'builtin', True))
        arm = self.task('臂', product={'vendor': 'NeuxMind-Sim', 'model': 'sim-arm-6dof'}, family='manipulator')
        self.assertIsNone(self.sim._profile(self.app.task_apps.product(arm['id'])))     # no arm among the made-up profiles
        self.assertEqual(task_sim.EXAMPLE_REPORTS[('neuxmind-sim', 'sim-arm-6dof')], 'arm')

    def test_the_home_of_the_robots_is_not_a_task_and_an_unknown_task_is_refused(self):
        self.assertNotIn(robot_scope.HOME, [row['task']['id'] for row in self.sim.list()['tasks']])
        for key in (robot_scope.HOME, 'project-nope', None):
            with self.assertRaises(ValueError):
                self.sim.view({'project_id': key})

    def test_building_is_refused_without_a_brand_and_model_a_skill_of_the_task_or_consent_for_the_agent(self):
        self.refused('task_sim_no_product', self.sim.build, {'project_id': self.task(product=None)['id'], 'mode': 'empty', 'skill_id': 'patrol'})
        example = self.task('示例', product={'vendor': BUILTIN['vendor'], 'model': BUILTIN['model']})
        with self.assertRaisesRegex(ValueError, '自带场地'):
            self.sim.build({'project_id': example['id'], 'mode': 'empty', 'skill_id': 'patrol'})
        if HAVE_MUJOCO:
            two = self.task('二')
            self.refused('authorization_required', self.sim.build, {'project_id': two['id'], 'mode': 'agent', 'skill_id': 'patrol'})
            for skill_id in (None, '', 'made.up'):
                with self.assertRaisesRegex(ValueError, '技能'):
                    self.sim.build({'project_id': two['id'], 'mode': 'empty', 'skill_id': skill_id})
            with self.assertRaises(ValueError):
                self.sim.build({'project_id': self.task('三')['id'], 'mode': 'something', 'skill_id': 'patrol'})

    def test_entering_needs_a_saved_environment_of_that_skill(self):
        if not HAVE_MUJOCO:
            self.skipTest('a task of a robot model needs the mujoco package to be simulated at all')
        self.refused('task_sim_not_built', self.sim.enter, {'project_id': self.task()['id'], 'skill_id': 'patrol'})

    def test_a_build_the_app_was_closed_in_is_marked_failed_and_the_saved_environment_stays(self):
        task = self.task()
        self.app.store.put(task_sim.KIND, {'id': 'task-sim-x', 'project_id': task['id'], 'envs': {'patrol': {
            'saved': {'scene_id': 's1', 'source': 'empty', 'product': DOG_PRODUCT, 'model': {'kind': 'profile'}},
            'build': {'id': 's2', 'status': 'generating', 'source': 'agent'}}}})
        self.app.close()
        view = self.open().task_sim.view({'project_id': task['id']})
        if view['problem']:
            self.skipTest('needs the mujoco package for the dog to be simulated')
        patrol = skill_of(view, 'patrol')
        self.assertEqual((patrol['build']['status'], patrol['build']['error'], patrol['build']['busy']), ('failed', task_sim.INTERRUPTED, False))
        self.assertEqual(patrol['environment']['scene_id'], 's1')

    def test_the_skills_are_the_applications_ones_in_step_order_then_the_other_skills_of_the_brand_and_model(self):
        task = self.task(steps=(('goto_place', {'place': '货架'}), ('patrol', {'loops': 2}), ('goto_place', {'place': '门口'})))
        view = self.sim.view({'project_id': task['id']})
        ids = [s['id'] for s in view['skills']]
        self.assertEqual(ids[:2], ['goto_place', 'patrol'])
        goto = view['skills'][0]
        self.assertEqual([(u['step'], u['parameters']) for u in goto['uses']], [(1, {'place': '货架'}), (3, {'place': '门口'})])
        self.assertEqual((goto['origin'], goto['installed'], goto['has_package']), ('application', False, True))  # a bundled example of that id
        # Issue #69: the bundled packages offered to this brand and model, and the generic one, come after.
        rest = {s['id']: s for s in view['skills'][2:]}
        self.assertIn('status_report', rest)
        self.assertEqual((rest['status_report']['origin'], rest['status_report']['uses']), ('model', []))
        self.assertNotIn('wave', rest)                                           # bundled for the example arm only
        made_up = self.sim.view({'project_id': self.task('别的', steps=(('made.up', {}),))['id']})
        self.assertEqual(skill_of(made_up, 'made.up')['has_package'], False)

    def test_a_task_from_before_issue_75_keeps_its_built_environment_for_each_of_its_skills(self):
        task = self.task(steps=(('patrol', {'loops': 2}), ('goto_place', {'place': '门口'})))
        old = {'scene_id': 'task-scene-old', 'source': 'agent', 'note': '', 'product': DOG_PRODUCT, 'model': {'kind': 'profile'},
               'scene': None, 'model_dir': '', 'preview': False}
        for folder in ('task-scene-old', 'task-scene-failed'):
            (self.app.mujoco.scenes_dir / folder).mkdir(parents=True)
        self.app.store.put(task_sim.KIND, {'id': 'task-sim-old', 'project_id': task['id'], 'saved': old,
                                           'build': {'id': 'task-scene-failed', 'status': 'failed', 'error': '以前失败的'}, 'robot_id': None})
        record = self.sim._record(task['id'])
        self.assertNotIn('saved', record)
        self.assertNotIn('build', record)
        self.assertEqual(record['envs']['patrol']['saved']['scene_id'], 'task-scene-old')
        self.assertEqual(record['envs']['goto_place']['saved']['scene_id'], 'task-scene-old')
        # Every skill the task lists now, not only its applications' (the reviewer of #75 found the others dropped).
        self.assertEqual(record['envs']['status_report']['saved']['scene_id'], 'task-scene-old')
        self.assertTrue(record['envs']['patrol']['saved']['carried'])
        self.assertIsNone(record['envs']['patrol'].get('build'))
        self.assertTrue(self.sim._named('task-scene-old'))
        self.assertTrue((self.app.mujoco.scenes_dir / 'task-scene-old').exists())
        self.assertFalse((self.app.mujoco.scenes_dir / 'task-scene-failed').exists())       # nothing names it any more
        # A stage of its own is not carried over: every skill of such a robot has its own now.
        staged = self.task('示例', product={'vendor': BUILTIN['vendor'], 'model': BUILTIN['model']})
        self.app.store.put(task_sim.KIND, {'id': 'task-sim-stage', 'project_id': staged['id'], 'saved': {'source': 'stage', 'scene_id': 'stage-1'}})
        self.assertEqual(self.sim._record(staged['id'])['envs'], {})


class Example(Case):
    """A task of the example quadruped: nothing to generate, every skill is performed by the built-in simulation."""

    def enter(self, skill_id='patrol'):
        task = self.task('示例', product={'vendor': BUILTIN['vendor'], 'model': BUILTIN['model']})
        view = self.sim.view({'project_id': task['id']})
        self.assertEqual((skill_of(view, skill_id)['environment']['source'], skill_of(view, skill_id)['build']), ('stage', None))
        return task, self.sim.enter({'project_id': task['id'], 'skill_id': skill_id})

    def test_entering_a_skill_starts_the_tasks_own_robot_and_installs_that_skill(self):
        task, view = self.enter()
        robot = self.app.store.get('robots', view['robot']['id'])
        self.assertEqual((robot['project_id'], robot['sim']['task_id'], robot['name']), (robot_scope.HOME, task['id'], '示例 仿真'))
        self.assertEqual((view['robot']['state'], view['robot']['connected'], view['robot']['picture'], view['robot']['skill']),
                         ('running', True, False, 'patrol'))
        self.assertEqual(view['installed_now'], [{'id': 'patrol', 'installed': True}])
        patrol = skill_of(view, 'patrol')
        self.assertEqual((patrol['installed'], patrol['compatible'], patrol['uses'][0]['parameters']), (True, True, {'loops': 2}))
        self.assertTrue(patrol['parameters'])
        self.assertFalse(skill_of(view, 'goto_place')['installed'])               # only the skill that was entered
        again = self.sim.enter({'project_id': task['id'], 'skill_id': 'patrol'})  # the same robot, nothing installed twice
        self.assertEqual((again['robot']['id'], again['installed_now']), (robot['id'], []))
        other = self.sim.enter({'project_id': task['id'], 'skill_id': 'goto_place'})
        self.assertEqual((other['robot']['id'], other['robot']['skill'], other['installed_now']), (robot['id'], 'goto_place', [{'id': 'goto_place', 'installed': True}]))
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
        self.assertEqual(self.sim.enter({'project_id': task['id'], 'skill_id': 'patrol'})['robot'], {**view['robot']})
        self.app.sims.remove({'robot_id': first})
        self.assertIsNone(self.sim.view({'project_id': task['id']})['robot'])
        self.assertNotEqual(self.sim.enter({'project_id': task['id'], 'skill_id': 'patrol'})['robot']['id'], first)

    def test_deleting_the_task_takes_its_simulated_robot_and_record_with_it(self):
        task, view = self.enter()
        self.app.task_remove.remove({'project_id': task['id']})
        self.assertEqual([r for r in self.app.store.list('robots') if r.get('sim')], [])
        self.assertEqual(self.app.store.list(task_sim.KIND), [])
        self.assertFalse((self.app.sims.root / view['robot']['id']).exists())

    def test_a_robot_that_does_not_start_is_not_left_behind_and_trying_again_makes_no_second_one(self):
        task = self.task('示例', product={'vendor': BUILTIN['vendor'], 'model': BUILTIN['model']})
        real = self.app.sims.start
        self.app.sims.start = lambda robot_id: (_ for _ in ()).throw(ValueError('起不来'))
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.sim.enter({'project_id': task['id'], 'skill_id': 'patrol'})
            self.assertEqual([r['name'] for r in self.app.store.list('robots') if r.get('sim')], [])
        self.app.sims.start = real
        self.assertEqual(self.sim.enter({'project_id': task['id'], 'skill_id': 'patrol'})['robot']['name'], '示例 仿真')

    def test_a_skill_that_is_not_the_tasks_is_refused(self):
        task = self.task('示例', product={'vendor': BUILTIN['vendor'], 'model': BUILTIN['model']})
        with self.assertRaisesRegex(ValueError, '没有这个技能'):
            self.sim.enter({'project_id': task['id'], 'skill_id': 'made.up'})
        self.assertEqual([r for r in self.app.store.list('robots') if r.get('sim')], [])


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class Physics(Case):
    """A task of a profile with a robot model: each skill's environment is built, checked, saved, and used."""

    def test_an_empty_floor_is_built_for_one_skill_saved_with_it_and_the_robot_starts_in_it_with_the_picture(self):
        task = self.task(steps=GENERIC_STEPS)
        started = self.sim.build({'project_id': task['id'], 'mode': 'empty', 'skill_id': 'status_report'})
        self.assertEqual((skill_of(started, 'status_report')['build']['busy'], skill_of(started, 'status_report')['environment']), (True, None))
        self.assertEqual(started['counts']['building'], 1)
        self.refused('sim_scene_busy', self.sim.build, {'project_id': task['id'], 'mode': 'empty', 'skill_id': 'status_report'})
        row = self.built(task, 'status_report')
        self.assertIsNone(row['build'], row)
        saved = row['environment']
        self.assertEqual((saved['source'], saved['source_name'], saved['stale'], saved['product']), ('empty', '空场地', False, DOG_PRODUCT))
        self.assertEqual(self.sim._record(task['id'])['envs']['status_report']['saved']['skill_id'], 'status_report')
        if saved['preview']:
            self.assertTrue(self.sim.preview(saved['scene_id']).startswith(b'\x89PNG'))
        self.assertIsNone(self.sim.preview('task-scene-nope'))
        entered = self.sim.enter({'project_id': task['id'], 'skill_id': 'status_report'})
        robot = self.app.store.get('robots', entered['robot']['id'])
        self.assertEqual((entered['robot']['picture'], entered['robot']['skill'], robot['sim']['profile_id'], robot['family'], {k: robot['product'][k] for k in DOG_PRODUCT}),
                         (True, 'status_report', 'acme-dog', 'wheel_legged', DOG_PRODUCT))
        self.assertEqual(entered['installed_now'], [{'id': 'status_report', 'installed': True}])
        # Saved: another opening of the workspace finds it and builds nothing.
        self.app.close()
        again = self.open().task_sim.view({'project_id': task['id']})
        self.assertEqual((skill_of(again, 'status_report')['environment']['scene_id'], skill_of(again, 'status_report')['build'], again['robot']['id']),
                         (saved['scene_id'], None, robot['id']))

    def test_every_skill_has_its_own_environment_and_entering_another_moves_the_robot_there(self):
        task = self.task(steps=GENERIC_STEPS + (('goto_place', {'place': '门口'}),))
        self.sim.build({'project_id': task['id'], 'mode': 'empty', 'skill_id': 'status_report'})
        first = self.built(task, 'status_report')['environment']['scene_id']
        view = self.sim.view({'project_id': task['id']})
        self.assertIsNone(skill_of(view, 'goto_place')['environment'])           # the other skill has none yet
        self.assertEqual(view['counts'], {'skills': len(view['skills']), 'ready': 1, 'building': 0, 'failed': 0})
        robot_id = self.sim.enter({'project_id': task['id'], 'skill_id': 'status_report'})['robot']['id']
        self.refused('task_sim_not_built', self.sim.enter, {'project_id': task['id'], 'skill_id': 'goto_place'})
        self.sim.build({'project_id': task['id'], 'mode': 'empty', 'skill_id': 'goto_place'})
        second = self.built(task, 'goto_place')['environment']['scene_id']
        self.assertNotEqual(first, second)
        self.assertEqual(self.sim.view({'project_id': task['id']})['robot']['skill'], 'status_report')   # not moved by a build of another skill
        moved = self.sim.enter({'project_id': task['id'], 'skill_id': 'goto_place'})
        self.assertEqual((moved['robot']['id'], moved['robot']['skill'], moved['robot']['connected']), (robot_id, 'goto_place', True))
        import json
        config = json.loads((self.app.sims.root / robot_id / 'mujoco.json').read_text(encoding='utf-8'))
        self.assertEqual(config['scene'], self.sim._record(task['id'])['envs']['goto_place']['saved']['scene'])
        self.assertTrue((self.app.mujoco.scenes_dir / first).exists())          # the first skill's environment is still there

    def two_built(self):
        task = self.task(steps=GENERIC_STEPS + (('goto_place', {'place': '门口'}),))
        for skill_id in ('status_report', 'goto_place'):
            self.sim.build({'project_id': task['id'], 'mode': 'empty', 'skill_id': skill_id})
            self.built(task, skill_id)
        return task

    def test_a_move_that_fails_does_not_claim_the_old_environment_and_the_next_entry_puts_it_right(self):
        import json
        task = self.two_built()
        robot_id = self.sim.enter({'project_id': task['id'], 'skill_id': 'status_report'})['robot']['id']
        real = self.app.sims.start
        self.app.sims.start = lambda robot_id: (_ for _ in ()).throw(ValueError('起不来'))
        with self.assertRaises(ValueError):
            self.sim.enter({'project_id': task['id'], 'skill_id': 'goto_place'})
        self.app.sims.start = real
        self.assertIsNone(self.sim.view({'project_id': task['id']})['robot']['skill'])    # not 「在 status_report 的环境里」
        back = self.sim.enter({'project_id': task['id'], 'skill_id': 'status_report'})
        self.assertEqual((back['robot']['id'], back['robot']['skill'], back['robot']['connected']), (robot_id, 'status_report', True))
        config = json.loads((self.app.sims.root / robot_id / 'mujoco.json').read_text(encoding='utf-8'))
        self.assertEqual(config['scene'], self.sim._record(task['id'])['envs']['status_report']['saved']['scene'])

    def test_removing_an_environment_waits_for_an_entry_under_way_and_leaves_nothing_pointing_at_it(self):
        import threading
        task = self.two_built()
        self.sim.enter({'project_id': task['id'], 'skill_id': 'status_report'})
        real, removal = self.sim._unpacked, {}

        def unpacked(*args):                            # the entry has read the environment; a removal comes now
            thread = threading.Thread(target=lambda: removal.update(answer=self.sim.remove({'project_id': task['id'], 'skill_id': 'goto_place'})))
            thread.start()
            thread.join(0.5)
            removal['waited'] = thread.is_alive()
            removal['thread'] = thread
            return real(*args)
        self.sim._unpacked = unpacked
        self.sim.enter({'project_id': task['id'], 'skill_id': 'goto_place'})
        self.sim._unpacked = real
        removal['thread'].join(60)
        self.assertEqual((removal['waited'], removal['answer']), (True, {'removed': True}))
        view = self.sim.view({'project_id': task['id']})
        self.assertIsNone(skill_of(view, 'goto_place')['environment'])
        self.assertIsNone(view['robot']['skill'])                                # not the skill whose environment is gone
        self.assertIsNotNone(skill_of(view, 'status_report')['environment'])

    def test_a_new_build_of_the_skill_the_robot_is_in_replaces_it_and_the_robot_keeps_its_skills(self):
        task = self.task(steps=GENERIC_STEPS)
        self.sim.build({'project_id': task['id'], 'mode': 'empty', 'skill_id': 'status_report'})
        first = self.built(task, 'status_report')['environment']['scene_id']
        robot_id = self.sim.enter({'project_id': task['id'], 'skill_id': 'status_report'})['robot']['id']
        self.sim.build({'project_id': task['id'], 'mode': 'empty', 'skill_id': 'status_report'})
        row = self.built(task, 'status_report')
        self.assertNotEqual(row['environment']['scene_id'], first)
        self.assertFalse((self.app.mujoco.scenes_dir / first).exists())
        view = self.connected(task, 'status_report')
        self.assertEqual(view['robot']['id'], robot_id)
        self.assertTrue(skill_of(view, 'status_report')['installed'])

    def test_a_bundled_skill_of_another_brand_and_model_is_not_installed_and_the_simulation_is_entered_all_the_same(self):
        # Issue #69 (merged with #66): the bundled patrol is for the example quadruped only, not for this made-up dog.
        task = self.task()
        self.sim.build({'project_id': task['id'], 'mode': 'empty', 'skill_id': 'patrol'})
        self.built(task, 'patrol')
        entered = self.sim.enter({'project_id': task['id'], 'skill_id': 'patrol'})
        self.assertEqual([(row['id'], row['installed']) for row in entered['installed_now']], [('patrol', False)])
        self.assertIn('属于', entered['installed_now'][0]['error'])
        self.assertTrue(self.app.store.get('robots', entered['robot']['id']))

    def test_a_build_that_fails_leaves_the_saved_environment_in_use(self):
        task = self.task()
        self.sim.build({'project_id': task['id'], 'mode': 'empty', 'skill_id': 'patrol'})
        saved = self.built(task, 'patrol')['environment']['scene_id']
        real = self.app.mujoco.check
        self.app.mujoco.check = lambda *a, **k: (_ for _ in ()).throw(CodedError('建不出来', 'sim_check_failed'))
        self.addCleanup(lambda: setattr(self.app.mujoco, 'check', real))
        self.sim.build({'project_id': task['id'], 'mode': 'empty', 'skill_id': 'patrol'})
        row = self.built(task, 'patrol')
        self.assertEqual((row['build']['status'], row['build']['error'], row['environment']['scene_id']), ('failed', '建不出来', saved))

    def test_an_environment_of_another_brand_and_model_cannot_be_entered_and_a_new_one_gets_a_new_robot(self):
        task = self.task(steps=GENERIC_STEPS)
        self.sim.build({'project_id': task['id'], 'mode': 'empty', 'skill_id': 'status_report'})
        self.built(task, 'status_report')
        old = self.sim.enter({'project_id': task['id'], 'skill_id': 'status_report'})['robot']['id']
        binding = self.app.task_apps.bindings(task['id'])[0]
        self.app.task_apps.unbind({'project_id': task['id'], 'application_id': binding['application_id']})
        self.task('臂', product={'vendor': ARM['vendor'], 'model': ARM['model']}, family='manipulator', steps=GENERIC_STEPS, bind=False)
        application = next(a for a in self.app.store.list('applications') if a['name'] == '臂应用')
        self.app.task_apps.bind({'project_id': task['id'], 'application_id': application['id']})
        self.assertTrue(skill_of(self.sim.view({'project_id': task['id']}), 'status_report')['environment']['stale'])
        self.assertIn('另一个品牌型号', self.refused('task_sim_not_built', self.sim.enter, {'project_id': task['id'], 'skill_id': 'status_report'}))
        self.sim.build({'project_id': task['id'], 'mode': 'empty', 'skill_id': 'status_report'})
        self.assertEqual(self.built(task, 'status_report')['environment']['stale'], False)
        deadline = time.monotonic() + 30                    # the robot of the old brand and model goes right after the save
        while time.monotonic() < deadline and self.sim.view({'project_id': task['id']})['robot'] is not None:
            time.sleep(0.2)
        self.assertIsNone(self.sim.view({'project_id': task['id']})['robot'])
        self.assertNotIn(old, [r['id'] for r in self.app.store.list('robots')])

    def test_removing_a_skills_environment_closes_the_robot_in_it_and_leaves_the_others(self):
        task = self.task(steps=GENERIC_STEPS + (('goto_place', {'place': '门口'}),))
        for skill_id in ('status_report', 'goto_place'):
            self.sim.build({'project_id': task['id'], 'mode': 'empty', 'skill_id': skill_id})
            self.built(task, skill_id)
        robot_id = self.sim.enter({'project_id': task['id'], 'skill_id': 'status_report'})['robot']['id']
        gone = self.sim._record(task['id'])['envs']['status_report']['saved']['scene_id']
        self.assertEqual(self.sim.remove({'project_id': task['id'], 'skill_id': 'status_report'}), {'removed': True})
        view = self.sim.view({'project_id': task['id']})
        self.assertEqual((skill_of(view, 'status_report')['environment'], view['robot']['id'], view['robot']['connected'], view['robot']['skill']),
                         (None, robot_id, False, None))
        self.assertIsNotNone(skill_of(view, 'goto_place')['environment'])
        self.assertFalse((self.app.mujoco.scenes_dir / gone).exists())
        self.assertEqual(self.sim.remove({'project_id': task['id'], 'skill_id': 'status_report'}), {'removed': False})
        self.assertEqual(self.sim.enter({'project_id': task['id'], 'skill_id': 'goto_place'})['robot']['id'], robot_id)


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
        self.task_ = self.task(steps=(('goto_place', {'place': '门口'}), ('patrol', {'loops': 2})))
        self.agent_model = self.app.engineering.model_add({'project_id': self.task_['id'], 'name': 'Fixture', 'model': 'fixture-agent',
                                                           'base_url': self.server.url + '/v1', 'api_key': 'fixture-' + 'k' * 20})
        self.ask = {'project_id': self.task_['id'], 'skill_id': 'goto_place', 'mode': 'agent', 'model_id': self.agent_model['id'],
                    'data_authorized': True, 'note': '货架靠北墙'}

    def test_the_agent_gets_the_task_and_that_one_skill_with_its_steps_and_what_it_wrote_is_saved_with_the_skill(self):
        import json
        from pathlib import Path
        import mujoco_support as ms
        from test_mujoco_sim import writes
        self.model.command = writes(ms.scene())
        started = self.sim.build(self.ask)
        self.assertEqual((skill_of(started, 'goto_place')['build']['source'], skill_of(started, 'goto_place')['build']['busy']), ('agent', True))
        row = self.built(self.task_, 'goto_place')
        self.assertIsNone(row['build'], row)
        saved = row['environment']
        self.assertEqual((saved['source'], saved['source_name'], saved['places']), ('agent', '工程助手生成', ['门口', '墙后']))
        self.assertEqual(len(saved['lines']), 5)
        self.assertIsNone(skill_of(self.sim.view({'project_id': self.task_['id']}), 'patrol')['environment'])   # the other skill has none
        record = self.sim._record(self.task_['id'])
        session = self.app.store.get('agent_sessions', record['envs']['goto_place']['saved']['session_id'])
        self.assertEqual((session['status'], session['project_id']), ('completed', self.task_['id']))
        self.assertIn('一个技能', session['objective'])
        self.assertIn('货架靠北墙', session['objective'])
        seen = json.loads((Path(self.app.store.root) / 'engineering' / session['id'] / 'workspace' / 'seen.json').read_text())
        self.assertNotIn('application', seen)
        self.assertNotIn('applications', seen)
        self.assertEqual(seen['task'], {'name': '巡检', 'objective': '从门口走到货架'})
        self.assertEqual((seen['skill']['id'], seen['skill']['title']), ('goto_place', '技能 goto_place'))
        self.assertTrue(seen['skill']['parameters'])                              # from the bundled package of that id
        self.assertEqual(seen['uses'], [{'application': '巡检应用', 'step': 1, 'parameters': {'place': '门口'}}])   # not the patrol step
        self.assertEqual((seen['robot']['brand'], seen['extra_note']), ('智身科技', '货架靠北墙'))
        # The robot of the task knows the places of the environment.
        entered = self.sim.enter({'project_id': self.task_['id'], 'skill_id': 'goto_place'})
        robot = self.app.store.get('robots', entered['robot']['id'])
        places = next(c for c in robot['manifest_v2']['capabilities'] if c['id'] == 'base.navigate_to_place')['params']['places']
        self.assertEqual(sorted(places), sorted(['门口', '墙后']))

    def test_a_scene_outside_the_format_fails_the_build_and_giving_up_stops_the_agent(self):
        self.model.command = "printf '{\"schema\": \"other\"}' > scene.json"
        self.sim.build(self.ask)
        row = self.built(self.task_, 'goto_place')
        self.assertEqual((row['build']['status'], row['environment']), ('failed', None))
        self.assertIn('不符合格式', row['build']['error'])
        self.model.command = 'sleep 30'
        self.sim.build(self.ask)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and skill_of(self.sim.view({'project_id': self.task_['id']}), 'goto_place')['build']['status'] != 'generating':
            time.sleep(0.1)
        given_up = skill_of(self.sim.cancel({'project_id': self.task_['id'], 'skill_id': 'goto_place'}), 'goto_place')
        self.assertEqual((given_up['build']['status'], given_up['build']['busy'], given_up['build']['error']), ('failed', False, '已取消这次构建'))
        given = self.sim._record(self.task_['id'])['envs']['goto_place']['build']['id']
        self.sim.build({'project_id': self.task_['id'], 'mode': 'empty', 'skill_id': 'goto_place'})   # nothing is in the way of the next build
        self.assertEqual(self.built(self.task_, 'goto_place')['environment']['source'], 'empty')
        deadline = time.monotonic() + 30                                         # the folder of the build that was given up goes
        while time.monotonic() < deadline and (self.app.mujoco.scenes_dir / given).exists():
            time.sleep(0.2)
        self.assertFalse((self.app.mujoco.scenes_dir / given).exists())

    # Issue #74: on 2026-11-28 the real agent looked around the workspace three times, at about 10 400 tokens a
    # request, and was stopped by the engine's default observation limit of 30 000 before it wrote scene.json
    # (「工程助手没有写出环境描述：会话被停止了」). The stand-in below counts tokens like that real run.
    def agent_like_the_real_one(self):
        from brainos import agent_runtime
        from test_product import Server
        if agent_runtime.binary_path():
            self.skipTest('counts the tokens of the stand-in agent; a real engine counts its own')
        model_class = looks_around()
        model_class.command = self.model.command
        server = Server(model_class)
        self.addCleanup(server.close)
        model = self.app.engineering.model_add({'project_id': self.task_['id'], 'name': 'Looks around', 'model': 'fixture-agent',
                                                'base_url': server.url + '/v1', 'api_key': 'fixture-' + 'k' * 20})
        return {**self.ask, 'model_id': model['id']}

    def test_an_agent_that_looks_around_first_has_tokens_enough_to_write_the_environment(self):
        from unittest import mock
        from brainos import agent_runtime, engineering, mujoco_sim
        import mujoco_support as ms
        from test_mujoco_sim import writes
        self.model.command = writes(ms.scene())
        ask = self.agent_like_the_real_one()
        with mock.patch.object(agent_runtime, 'AgentProcess', counting(REAL_REQUEST_TOKENS)):
            self.sim.build(ask)
            row = self.built(self.task_, 'goto_place')
        self.assertIsNone(row['build'], row)
        self.assertEqual(row['environment']['source'], 'agent')
        session = self.app.store.get('agent_sessions', self.sim._record(self.task_['id'])['envs']['goto_place']['saved']['session_id'])
        self.assertGreater(session['usage']['totalTokens'], 30000)               # more than the engine's default allows
        self.assertEqual(session['max_tokens'], mujoco_sim.AGENT_TOKENS)
        self.assertGreaterEqual(mujoco_sim.AGENT_TOKENS, engineering.CLOUD_MIN_TOKENS)   # a cloud model can build too
        self.assertNotIn('cat scene.json', session['objective'])                 # a big environment is not read back whole

    def test_a_build_stopped_at_its_token_limit_says_so(self):
        from unittest import mock
        from brainos import agent_runtime, mujoco_sim
        import mujoco_support as ms
        from test_mujoco_sim import writes
        self.model.command = writes(ms.scene())
        ask = self.agent_like_the_real_one()
        with mock.patch.object(agent_runtime, 'AgentProcess', counting(REAL_REQUEST_TOKENS)), \
                mock.patch.object(mujoco_sim, 'AGENT_TOKENS', 30000):
            self.sim.build(ask)
            row = self.built(self.task_, 'goto_place')
        self.assertEqual((row['build']['status'], row['environment']), ('failed', None))
        self.assertIn('用完了这次构建的 Token 额度（30000）', row['build']['error'])


REAL_REQUEST_TOKENS = 10400


def counting(per_request):
    """The stand-in agent, counting ``per_request`` tokens for every request to the model."""
    from codex_standin import CodexStandIn

    class Counting(CodexStandIn):
        asked = 0

        def _emit(self, method, params, request_id=None):
            if method == 'thread/tokenUsage/updated':
                self.asked += 1
                params = {**params, 'tokenUsage': {'total': {'totalTokens': per_request * self.asked}, 'last': {'totalTokens': per_request}}}
            return super()._emit(method, params, request_id)
    return Counting


def looks_around():
    import json
    from test_mujoco_sim import SceneModel

    class LooksAround(SceneModel):
        """Like the real agent: three commands that look around the workspace, then the one that writes, then done."""
        looks = ('ls -1', 'cat SIMULATION.json', 'wc -c SIMULATION.json')

        def select_item(self, data, _fallback):
            done = sum(1 for x in data.get('input', []) if x.get('type') == 'function_call_output')
            if done > len(self.looks):
                return {'id': 'msg_done', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': '环境描述已写好。', 'annotations': []}]}
            command = self.looks[done] if done < len(self.looks) else type(self).command
            return {'id': f'fc_{done}', 'type': 'function_call', 'call_id': f'call_{done}', 'name': 'exec_command',
                    'arguments': json.dumps({'cmd': command})}
    return LooksAround


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class Duck(test_duck_kit.Case):
    """A task of the Microduck: each skill model is its own environment — the stage it was trained on, inside its
    package (issue #75: they were all on one stage) — nothing to generate (the made-up kit of the duck tests)."""

    def stage(self, robot_id, show, seconds=60):
        deadline = time.monotonic() + seconds
        status = {}
        while time.monotonic() < deadline:
            status = self.app.duck.status({'robot_id': robot_id})
            if status['stage'] == show and not status['staging']:
                return status
            time.sleep(0.1)
        self.fail(f'the duck did not get onto the stage of {show}: {status}')

    def test_every_skill_model_is_its_own_environment_and_entering_one_puts_the_duck_on_its_stage(self):
        from brainos import duck_kit, duck_skills
        task = make_task(self.app, '表演', {'vendor': duck_kit.VENDOR, 'model': duck_kit.MODEL}, 'biped', (('duck.long', {}),))
        sim = self.app.task_sim
        view = sim.view({'project_id': task['id']})
        self.assertEqual((view['model']['kind'], view['model']['staged'], view['problem']), ('duck', True, None))
        models = [s for s in view['skills'] if s.get('show')]
        self.assertEqual(sorted(s['show'] for s in models), sorted(self.TASKS))
        self.assertEqual(view['skills'][0]['id'], 'duck.long')                  # the application's step first
        for row in models:
            self.assertEqual((row['id'], row['environment']['source'], row['build']), (duck_skills.PREFIX + row['show'], 'stage', None))
            self.assertIn('训练时用的场地', row['environment']['summary'])
        with self.assertRaisesRegex(ValueError, '自带场地'):
            sim.build({'project_id': task['id'], 'skill_id': 'duck.nod', 'mode': 'empty'})
        entered = sim.enter({'project_id': task['id'], 'skill_id': 'duck.nod'})
        self.assertEqual((entered['robot']['duck'], entered['robot']['connected'], entered['robot']['skill'], entered['installed_now']), (True, True, 'duck.nod', []))
        robot = self.app.store.get('robots', entered['robot']['id'])
        self.assertEqual((robot['sim']['task_id'], robot['sim']['profile_id']), (task['id'], duck_kit.PROFILE_ID))
        self.stage(robot['id'], 'nod')
        self.assertTrue(skill_of(entered, 'duck.nod')['installed'])              # every skill model is put on the duck when it starts
        moved = sim.enter({'project_id': task['id'], 'skill_id': 'duck.long'})   # another skill: the same duck, on that skill's stage
        self.assertEqual((moved['robot']['id'], moved['robot']['skill']), (robot['id'], 'duck.long'))
        self.stage(robot['id'], 'long')
        # Run through the runtime like any skill, so it is an operation with a run record.
        op = self.app.run_robot_skill({'robot_id': robot['id'], 'project_id': task['id'], 'skill_id': 'duck.nod', 'request_id': 'r1',
                                       'parameters': {}, 'hardware_authorized': True})
        self.assertEqual(op['status'], 'accepted', op)
        deadline = time.monotonic() + 60
        run = {}
        while time.monotonic() < deadline:
            run = self.app.robot_live({'robot_id': robot['id'], 'project_id': task['id']}).get('run') or {}
            if run.get('id') == op['run_id'] and run.get('status') not in ('queued', 'running'):
                break
            time.sleep(0.1)
        self.assertEqual(run.get('status'), 'succeeded', run)
        self.assertEqual(self.app.duck.status({'robot_id': robot['id']})['stage'], 'nod')     # performing it moved it to its own stage

    def test_a_duck_without_the_skill_model_is_replaced_by_one_that_has_it(self):
        import json
        from brainos import duck_kit
        task = make_task(self.app, '表演', {'vendor': duck_kit.VENDOR, 'model': duck_kit.MODEL}, 'biped', (('duck.nod', {}),))
        first = self.app.task_sim.enter({'project_id': task['id'], 'skill_id': 'duck.nod'})['robot']['id']
        path = self.app.sims.root / first / 'duck.json'                          # a duck added before the model was in the kit
        config = json.loads(path.read_text(encoding='utf-8'))
        config['shows'].pop('long')
        path.write_text(json.dumps(config), encoding='utf-8')
        entered = self.app.task_sim.enter({'project_id': task['id'], 'skill_id': 'duck.long'})
        self.assertNotEqual(entered['robot']['id'], first)
        self.assertNotIn(first, [r['id'] for r in self.app.store.list('robots')])
        self.stage(entered['robot']['id'], 'long')

    def test_a_stage_is_not_changed_under_a_show_that_is_being_performed(self):
        from brainos import duck_kit
        task = make_task(self.app, '表演', {'vendor': duck_kit.VENDOR, 'model': duck_kit.MODEL}, 'biped', (('duck.nod', {}),))
        robot_id = self.app.task_sim.enter({'project_id': task['id'], 'skill_id': 'duck.long'})['robot']['id']
        self.stage(robot_id, 'long')
        run = self.app.duck.perform({'robot_id': robot_id, 'show': 'long', 'project_id': task['id']})
        self.wait(robot_id, run['run_id'], states=('running',))
        self.app.duck.stage({'robot_id': robot_id, 'show': 'nod'})
        time.sleep(1.0)
        status = self.app.duck.status({'robot_id': robot_id})
        self.assertEqual((status['stage'], status['state'], status['run_id']), ('long', 'running', run['run_id']))
        self.refused('duck_show_missing', self.app.duck.stage, {'robot_id': robot_id, 'show': 'nothing'})


if __name__ == '__main__':
    unittest.main()
