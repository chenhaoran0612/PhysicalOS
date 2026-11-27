"""Issue #54: simulated robots are added from built-in profiles (type → brand → model). A profile's robot
model is a package next to the app, not something uploaded to the library; the simulated robots from
before the profiles are removed once.

The packages here are made-up robots built with the same tool that builds the real ones
(``scripts/robot_models.py``). Starting a robot needs the ``mujoco`` package; those tests are skipped
where it is not installed."""
import importlib.util
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import mujoco_support as ms
from brainos import robot_model, robot_scope, sim_profiles
from brainos.errors import CodedError
from brainos.service import Platform

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import robot_models  # noqa: E402

HAVE_MUJOCO = importlib.util.find_spec('mujoco') is not None
DOG = {'id': 'acme-dog', 'vendor': '智身科技', 'model': '钢镚 L2-U', 'type': 'wheel_legged', 'image': '',
       'pose': {'*_thigh_joint': 0.4, '*_calf_joint': -0.8}, 'source': {'private': 'dog', 'urdf': 'urdf/dog.urdf'}}
ARM = {'id': 'acme-arm', 'vendor': 'Acme Robotics', 'model': 'Arm-3', 'type': 'manipulator', 'image': '',
       'source': {'private': 'arm', 'urdf': 'arm.urdf'}}
MISSING = {'id': 'acme-none', 'vendor': 'Acme Robotics', 'model': 'Ghost', 'type': 'biped', 'image': '',
           'source': {'private': 'none', 'urdf': 'x.urdf'}}
BUILTIN = {'id': 'neuxmind-quadruped', 'vendor': 'NeuxMind 示例', 'model': '通用四足', 'type': 'quadruped', 'image': '',
           'builtin': 'quadruped'}
MADE_UP = (DOG, ARM, MISSING, BUILTIN)


def packages(root):
    """Model packages of the made-up dog and arm under ``root``; the folder the app would be packaged with."""
    source, out = Path(root) / 'source', Path(root) / 'models'
    (source / 'dog' / 'urdf').mkdir(parents=True)
    (source / 'dog' / 'meshes' / 'visual').mkdir(parents=True)
    (source / 'dog' / 'urdf' / 'dog.urdf').write_bytes(ms.dog_urdf())
    (source / 'dog' / 'meshes' / 'visual' / 'trunk.stl').write_bytes(ms.stl())
    (source / 'arm').mkdir()
    (source / 'arm' / 'arm.urdf').write_bytes(ms.arm_urdf())
    for profile in (DOG, ARM):
        robot_models.pack(profile, source / profile['source']['private'], out, {'private': profile['source']['private']})
    return out


class Listed(unittest.TestCase):
    """What the list itself promises."""

    def test_the_types_are_the_five_of_the_issue_in_its_order(self):
        self.assertEqual([t['name'] for t in sim_profiles.TYPES], ['机械臂', '轮足机器狗', '四足机械狗', '双足机器人', '其他'])
        self.assertTrue(all(p['type'] in sim_profiles.TYPE_BY_ID for p in sim_profiles.PROFILES))

    def test_the_five_brands_are_there_and_the_two_real_robots_are_named_as_the_user_names_them(self):
        by_vendor = {}
        for profile in sim_profiles.PROFILES:
            by_vendor.setdefault(profile['vendor'], []).append(profile)
        for vendor in ('宇树科技', '云深处科技', '智元机器人', '智身科技', '星玑科技'):
            self.assertTrue(1 <= len(by_vendor.get(vendor, [])) <= 3, vendor)
        self.assertEqual([(p['model'], p['type']) for p in by_vendor['智身科技']], [('钢镚 L2-U', 'wheel_legged')])
        self.assertEqual([(p['model'], p['type']) for p in by_vendor['星玑科技']], [('CS1', 'quadruped')])

    def test_ids_are_file_names_and_unique_and_every_profile_has_a_model_or_is_built_in(self):
        ids = [p['id'] for p in sim_profiles.PROFILES]
        self.assertEqual(len(ids), len(set(ids)))
        for profile in sim_profiles.PROFILES:
            self.assertRegex(profile['id'], r'^[a-z0-9][a-z0-9-]{0,40}$')
            self.assertTrue(bool(profile.get('source')) != bool(profile.get('builtin')), profile['id'])
            source = profile.get('source') or {}
            if source.get('repo'):
                self.assertRegex(source['commit'], r'^[0-9a-f]{40}$')
                self.assertTrue(source['licence'] and source['licence_name'], profile['id'])
                self.assertIn(source['urdf'], [p for p in source['paths']] + [source['urdf']])

    def test_no_robot_model_file_is_kept_in_the_repository(self):
        root = Path(__file__).resolve().parents[1]
        kept = [p for p in root.rglob('*') if p.suffix.lower() in ('.stl', '.dae', '.urdf', '.step')
                and not {'.build', '.git', 'dist', 'artifacts', 'node_modules'} & set(p.relative_to(root).parts)]
        self.assertEqual(kept, [])

    def test_another_spelling_of_a_known_brand_and_model_is_the_same_brand_and_model(self):
        same = robot_model.key({'vendor': '智身科技', 'model': '钢镚 L2-U'})
        for vendor, model in (('zs', 'L2U'), ('ZS', 'l2u'), ('智身', '钢镚 L2U'), ('zsibot', 'L2 Ultra')):
            self.assertEqual(robot_model.key({'vendor': vendor, 'model': model}), same, (vendor, model))
        self.assertEqual(robot_model.key({'vendor': '星玑', 'model': 'cs1'}), robot_model.key({'vendor': '星玑科技', 'model': 'CS1'}))
        self.assertNotEqual(robot_model.key({'vendor': '智身科技', 'model': '钢镚 L1'}), same)
        self.assertEqual(robot_model.key({'vendor': 'Acme Robotics', 'model': 'Go-7'}), ('acme robotics', 'go-7'))
        self.assertIsNone(robot_model.key(None))

    def test_the_standing_pose_names_joints_by_pattern_and_the_first_pattern_wins(self):
        profile = {'pose': {'F?_thigh_joint': 0.8, '*_thigh_joint': 1.0, '*_calf_joint': -1.5}}
        self.assertEqual(sim_profiles.pose_of(profile, ['FL_thigh_joint', 'RL_thigh_joint', 'RL_calf_joint', 'FL_hip_joint']),
                         {'FL_thigh_joint': 0.8, 'RL_thigh_joint': 1.0, 'RL_calf_joint': -1.5})
        self.assertEqual(sim_profiles.pose_of({}, ['a']), {})


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name + '/platform'
        self.models = packages(self.tmp.name)
        self.app = self.open()

    def open(self):
        app = Platform(self.root)
        self.addCleanup(lambda: app.close())
        app.sim_profiles.profiles, app.sim_profiles.models = MADE_UP, self.models
        return app

    def refused(self, code, call, *args):
        with self.assertRaises(CodedError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)


class Catalog(Case):
    def test_the_page_gets_types_and_profiles_and_which_ones_this_installation_can_add(self):
        catalog = self.app.snapshot()['sim_catalog']
        self.assertEqual([t['id'] for t in catalog['types']], ['manipulator', 'wheel_legged', 'quadruped', 'biped', 'other'])
        rows = {r['id']: r for r in catalog['profiles']}
        self.assertEqual(set(rows), {'acme-dog', 'acme-arm', 'acme-none', 'neuxmind-quadruped'})
        self.assertEqual((rows['neuxmind-quadruped']['available'], rows['neuxmind-quadruped']['builtin']), (True, True))
        self.assertEqual((rows['acme-none']['available'], rows['acme-none']['problem']), (False, rows['acme-none']['problem']))
        self.assertTrue(rows['acme-none']['problem'])
        self.assertEqual(rows['acme-dog']['available'], HAVE_MUJOCO)
        self.assertNotIn('source', rows['acme-dog'])

    def test_a_profile_that_is_not_listed_or_has_no_model_here_is_refused_in_words(self):
        self.refused('sim_profile_unknown', self.app.add_sim_robot, {'profile_id': 'nope'})
        self.refused('sim_profile_unknown', self.app.add_sim_robot, {'profile_id': ''})
        message = self.refused('sim_profile_unavailable', self.app.add_sim_robot, {'profile_id': 'acme-none'})
        self.assertIn('Acme Robotics Ghost', message)
        self.assertEqual(self.app.store.list('robots'), [])

    def test_the_pages_get_the_same_table_of_spellings_the_backend_compares_by(self):
        table = self.app.snapshot()['product_aliases']
        self.assertEqual((table['vendors']['zs'], table['models']['智身科技']['l2u'], table['vendors']['星玑']), ('智身科技', '钢镚 L2-U', '星玑科技'))
        for folded, vendor in table['vendors'].items():
            self.assertEqual(sim_profiles.canonical(folded, 'x')[0], vendor)

    def test_an_application_of_the_real_robot_fits_the_simulated_one_of_the_same_brand_and_model(self):
        made_for = {'product': {'vendor': 'zs', 'model': 'L2U'}}
        self.assertTrue(self.app.robot_models.fits(made_for, {'product': {'vendor': '智身科技', 'model': '钢镚 L2-U'}}))
        self.assertFalse(self.app.robot_models.fits(made_for, {'product': {'vendor': '星玑科技', 'model': 'CS1'}}))


class OldSims(Case):
    def plant(self):
        """What a workspace from before the profiles holds: two simulated robots, a real one, their operations."""
        store = self.app.store
        store.delete('settings', sim_profiles.SETTING)
        for robot_id, sim in (('robot-old-dog', {'kind': 'quadruped', 'adapter': 'sim_quadruped', 'enabled': False, 'state': 'stopped'}),
                              ('robot-old-mujoco', {'kind': 'mujoco', 'adapter': 'brainos.mujoco_body:body', 'enabled': False,
                                                    'state': 'stopped', 'scene_id': 'mujoco-scene-1'}),
                              ('robot-real', None)):
            record = {'id': robot_id, 'project_id': robot_scope.HOME, 'name': robot_id, 'family': 'quadruped', 'endpoint': '',
                      'token': 't', 'status': 'unconnected', 'capabilities': [], 'created_at': '2026-11-20T00:00:00+00:00'}
            if sim:
                record['sim'] = sim
                (self.app.sims.root / robot_id).mkdir(parents=True)
                (self.app.sims.root / robot_id / 'state.json').write_text('{}')
            store.put('robots', record)
            store.put('operations', {'id': 'operation-' + robot_id, 'robot_id': robot_id, 'status': 'accepted',
                                     'project_id': robot_scope.HOME, 'created_at': '2026-11-20T00:00:00+00:00'})
        self.app.close()

    def test_they_are_removed_once_with_their_files_and_operations_and_real_robots_stay(self):
        self.plant()
        app = self.open()
        self.assertEqual([r['id'] for r in app.store.list('robots')], ['robot-real'])
        self.assertEqual([o['id'] for o in app.store.list('operations')], ['operation-robot-real'])
        self.assertFalse((app.sims.root / 'robot-old-dog').exists())
        self.assertFalse((app.sims.root / 'robot-old-mujoco').exists())
        self.assertEqual(app.store.get('settings', sim_profiles.SETTING)['removed'], 2)
        self.assertEqual(sorted(e['details']['robot_id'] for e in app.store.list('events') if e['action'] == 'robot.sim_removed'),
                         ['robot-old-dog', 'robot-old-mujoco'])

    def test_a_simulated_robot_added_afterwards_is_never_removed(self):
        self.plant()
        app = self.open()
        self.assertIsNone(app.sim_profiles.remove_old_sims())
        robot = app.add_sim_robot({'profile_id': 'neuxmind-quadruped'})
        app.close()
        again = self.open()
        self.assertEqual(sorted(r['id'] for r in again.store.list('robots')), sorted(['robot-real', robot['id']]))

    def rewrite(self, change):
        """Change the closed workspace's records behind the app's back (what a crash leaves is not reachable through it)."""
        from brainos.store import Store
        store = Store(self.root)
        change(store)
        store.db.close()

    def test_a_removal_that_was_interrupted_is_finished_the_next_time(self):
        self.plant()

        def killed_after_the_robots(store):         # the robots are gone, their operations and the mark are not
            for robot_id in ('robot-old-dog', 'robot-old-mujoco'):
                store.delete('robots', robot_id)
            store.put('settings', {'id': sim_profiles.SETTING, 'removing': ['robot-old-dog', 'robot-old-mujoco']})
        self.rewrite(killed_after_the_robots)
        app = self.open()
        self.assertEqual([o['id'] for o in app.store.list('operations')], ['operation-robot-real'])
        self.assertFalse((app.sims.root / 'robot-old-dog').exists())
        self.assertEqual([r['id'] for r in app.store.list('robots')], ['robot-real'])
        self.assertTrue(app.store.get('settings', sim_profiles.SETTING)['old_sims_removed'])

    def test_one_that_was_switched_on_is_removed_and_not_started_again(self):
        self.plant()

        def switched_on(store):
            robot = store.get('robots', 'robot-old-dog')
            robot['sim'].update(enabled=True, state='running')
            store.put('robots', robot)
        self.rewrite(switched_on)
        app = self.open()
        self.assertEqual([r['id'] for r in app.store.list('robots')], ['robot-real'])
        self.assertEqual(app.sims.procs, {})

    def test_a_new_workspace_has_nothing_to_remove_and_is_marked_done(self):
        self.assertEqual(self.app.store.get('settings', sim_profiles.SETTING)['removed'], 0)
        self.assertIsNone(self.app.sim_profiles.remove_old_sims())


class BuiltIn(Case):
    def test_the_example_brand_adds_the_robot_the_app_always_had(self):
        robot = self.app.add_sim_robot({'profile_id': 'neuxmind-quadruped'})
        self.assertEqual((robot['status'], robot['sim']['kind'], robot['family']), ('connected', 'quadruped', 'quadruped'))

    def test_adding_by_kind_still_works_for_what_was_written_against_it(self):
        robot = self.app.add_sim_robot({'kind': 'arm'})
        self.assertEqual((robot['status'], robot['sim']['kind']), ('connected', 'arm'))


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class Added(Case):
    def setUp(self):
        super().setUp()

        class NoCloud:
            def __getattr__(self, name):
                raise AssertionError('a profile does not ask the library: ' + name)
        self.app.library = NoCloud()

    def add(self, profile_id, **more):
        robot = self.app.add_sim_robot({'profile_id': profile_id, **more})
        return robot

    def test_a_robot_of_the_profile_starts_from_its_package_without_the_library(self):
        robot = self.add('acme-dog')
        self.assertEqual((robot['status'], robot['sim']['kind'], robot['sim']['state'], robot['family']),
                         ('connected', 'mujoco', 'running', 'wheel_legged'))     # issue #58: the profile's type
        self.assertEqual(robot['name'], '智身科技 钢镚 L2-U 仿真')
        saved = self.app.store.get('robots', robot['id'])
        self.assertEqual(robot_model.of(saved), {'vendor': '智身科技', 'model': '钢镚 L2-U'})
        self.assertEqual((saved['sim']['profile_id'], saved['manifest']['simulated']), ('acme-dog', True))
        self.assertEqual(robot_scope.tasks(self.app.store.list('projects'))[0]['id'], robot['project_id'])
        live = self.app.robot_live({'project_id': robot_scope.HOME, 'robot_id': robot['id']})
        self.assertEqual((live['reachable'], live['simulated'], live['state']['mode']), (True, True, 'sim:stand'))
        view = self.app.mujoco.view({'project_id': robot_scope.HOME, 'robot_id': robot['id']})
        self.assertEqual((view['fault'], view['scene']), ('', None))

    def test_it_stands_in_the_profiles_pose(self):
        robot = self.add('acme-dog')
        config = json.loads((self.app.sims.root / robot['id'] / 'mujoco.json').read_text())
        self.assertEqual(set(config['pose'].values()), {0.4, -0.8})
        self.assertEqual(len(config['pose']), 8)

    def test_two_robots_of_one_profile_share_the_unpacked_model_and_get_their_own_names(self):
        first, second = self.add('acme-dog'), self.add('acme-dog')
        self.assertEqual((first['name'], second['name']), ('智身科技 钢镚 L2-U 仿真', '智身科技 钢镚 L2-U 仿真 2'))
        self.assertEqual(len([p for p in (Path(self.root) / 'robot-models').iterdir() if p.is_dir()]), 1)

    def test_a_link_with_shapes_for_looks_does_not_show_its_collision_shapes_as_well(self):
        from brainos import mujoco_build, mujoco_model, mujoco_scene
        import mujoco
        target = Path(self.tmp.name) / 'looks'
        mujoco_model.prepare(self.models / 'acme-dog.zip', 'zip', target)
        world = mujoco_build.build(target, mujoco_scene.validate(mujoco_scene.EMPTY), 'quadruped')
        groups = {}
        for geom in world.robot_geoms:
            body = mujoco.mj_id2name(world.model, mujoco.mjtObj.mjOBJ_BODY, int(world.model.geom_bodyid[geom]))
            groups.setdefault(body, []).append((bool(world.model.geom_contype[geom]), int(world.model.geom_group[geom])))
        self.assertEqual(sorted(groups['robot/trunk']), [(False, 1), (True, 3)])      # its mesh is shown (MuJoCo's group for looks), its box is not
        self.assertEqual(groups['robot/fl_thigh'], [(True, 0)])                      # a leg has only its collision shape: shown
        self.assertTrue(world.collides)

    def test_an_arm_profile_is_a_fixed_arm(self):
        robot = self.add('acme-arm')
        self.assertEqual((robot['status'], robot['family']), ('connected', 'manipulator'))
        self.assertIn('arm.joint_position', robot['capabilities'])

    def test_it_comes_back_when_the_app_is_opened_again(self):
        robot = self.add('acme-dog')
        self.app.close()
        again = self.open()
        deadline = time.monotonic() + 60            # brought back in the background, as every simulated robot is
        while again.store.get('robots', robot['id'])['sim']['state'] != 'running' and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(again.store.get('robots', robot['id'])['sim']['state'], 'running')

    def test_a_package_that_is_not_a_robot_model_is_refused_and_nothing_is_added(self):
        with zipfile.ZipFile(self.models / 'acme-arm.zip', 'w') as archive:
            archive.writestr('readme.txt', 'no model here')
        self.refused('sim_model_invalid', self.app.add_sim_robot, {'profile_id': 'acme-arm'})
        self.assertEqual(self.app.store.list('robots'), [])


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class RealPackages(unittest.TestCase):
    """The packages of the real profiles, where they have been built on this computer
    (``python scripts/robot_models.py``): each one unpacks, builds with its pose and stands still."""

    def test_every_package_that_is_here_builds_and_stands(self):
        from brainos import mujoco_build, mujoco_model, mujoco_scene
        here = [p for p in sim_profiles.PROFILES if p.get('source') and (sim_profiles.models_dir() / (p['id'] + '.zip')).is_file()]
        if not here:
            self.skipTest('no model package has been built here')
        with tempfile.TemporaryDirectory() as tmp:
            for profile in here:
                with self.subTest(profile=profile['id']):
                    target = Path(tmp) / profile['id']
                    meta = mujoco_model.prepare(sim_profiles.models_dir() / (profile['id'] + '.zip'), 'zip', target)
                    self.assertEqual(meta.get('warnings'), [], profile['id'])
                    names = [j['name'] for j in meta['joints']]
                    pose = sim_profiles.pose_of(profile, names)
                    self.assertEqual(len(pose) > 0, bool(profile.get('pose')), 'the pose names joints this model does not have')
                    family = sim_profiles.TYPE_BY_ID[profile['type']]['family']
                    world = mujoco_build.build(target, mujoco_scene.validate(mujoco_scene.EMPTY), family, pose)
                    before = world.joint_positions()
                    summary = mujoco_build.settle(world, 2.0)
                    drift = max(abs(a - b) for a, b in zip(before, world.joint_positions()))
                    self.assertLess(drift, 0.05, 'it does not hold its pose')
                    if family != 'manipulator':
                        self.assertGreater(summary['stand_height'], 0.2)
                        self.assertLess(abs(world.base_pose()[3] - summary['stand_height']), 0.02)


if __name__ == '__main__':
    unittest.main()
