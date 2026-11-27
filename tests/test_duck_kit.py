"""Issue #63: the Microduck AD-P102 — its profile, its skill model packages, running one on a simulated
duck, and the training progress.

The packages here are made up (``duck_support``): a box, a show that counts steps, weights written by
hand in PyTorch's file format. Starting a simulated duck needs the ``mujoco`` package. The tests of the
real kit (the delivered packages on the engine version they were accepted on) run only where
``scripts/duck_kit.py build`` has made one."""
import hashlib
import importlib.util
import io
import json
import os
import pickle
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import numpy as np

import duck_support as ds
from brainos import duck_kit, duck_paths, duck_policy, sim_profiles
from brainos.errors import CodedError
from brainos.service import Platform

ROOT = Path(__file__).resolve().parents[1]
HAVE_MUJOCO = importlib.util.find_spec('mujoco') is not None
REAL_KIT = ROOT / '.build' / 'duck-kit'
ENDED = ('succeeded', 'failed', 'stopped', 'error')


def installed_mujoco():
    import mujoco
    return str(mujoco.__version__)


class Weights(unittest.TestCase):
    """A checkpoint is read without PyTorch, and only as numbers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'nod.pt'
        self.policy = ds.weights()

    def write(self, **more):
        self.path.write_bytes(ds.checkpoint_bytes('nod', self.policy, '3.10.0', **more))

    def test_the_numbers_come_back_exactly_and_reproduce_the_recorded_digest(self):
        self.write()
        state = duck_policy.read_checkpoint(self.path)
        self.assertEqual(sorted(state['policy']), sorted(self.policy))
        for name, array in self.policy.items():
            self.assertEqual(state['policy'][name].dtype, array.dtype, name)
            self.assertTrue(np.array_equal(state['policy'][name], array), name)
        self.assertEqual(state['policy']['obs_count'].shape, ())
        loaded = duck_policy.load_policy(self.path)
        self.assertEqual(loaded.digest, ds.digest(self.policy))
        self.assertEqual((loaded.task, loaded.mujoco_version, loaded.iteration), ('nod', '3.10.0', 4))

    def test_the_action_is_the_actors_output_on_the_normalised_observation(self):
        self.write()
        policy, p = duck_policy.load_policy(self.path), self.policy
        obs = np.array([0.3, -2.0, 50.0, 0.0], dtype=np.float32)
        x = np.clip((obs - p['obs_mean']) / np.sqrt(p['obs_var'] + np.float32(1e-6)), -10, 10)
        x = np.tanh(p['actor.0.weight'] @ x + p['actor.0.bias'])
        x = np.tanh(p['actor.2.weight'] @ x + p['actor.2.bias'])
        expected = np.tanh(p['actor.4.weight'] @ x + p['actor.4.bias'])
        action = policy(obs)
        self.assertEqual(action.shape, (ds.ACT,))
        self.assertTrue(np.allclose(action, expected, atol=1e-6))
        self.assertTrue(np.all(np.abs(action) <= 1.0))

    def test_an_observation_of_another_size_or_with_a_non_finite_value_is_refused(self):
        self.write()
        policy = duck_policy.load_policy(self.path)
        for bad in (np.zeros(ds.OBS + 1), np.array([0.0, np.nan, 0.0, 0.0]), np.array([0.0, np.inf, 0.0, 0.0])):
            with self.assertRaises(ValueError):
                policy(bad)

    def test_a_file_whose_numbers_are_not_the_recorded_ones_is_refused(self):
        self.write(recorded='0' * 64)
        with self.assertRaises(duck_policy.CheckpointError):
            duck_policy.load_policy(self.path)

    def test_a_pickle_that_names_anything_but_tensors_is_refused_and_runs_nothing(self):
        marker = Path(self.tmp.name) / 'ran'

        class Run:
            def __reduce__(self):
                return os.system, (f'touch {marker}',)
        inner = io.BytesIO()
        pickle.dump({'format_version': 1, 'policy': {}, 'x': Run()}, inner, protocol=2)
        with zipfile.ZipFile(self.path, 'w') as archive:
            archive.writestr('checkpoint/data.pkl', inner.getvalue())
        with self.assertRaises(duck_policy.CheckpointError) as caught:
            duck_policy.read_checkpoint(self.path)
        self.assertIn('不认识', str(caught.exception))
        self.assertFalse(marker.exists())

    def test_what_is_not_a_show_checkpoint_is_refused_in_words(self):
        self.path.write_bytes(b'not a zip')
        with self.assertRaises(duck_policy.CheckpointError):
            duck_policy.read_checkpoint(self.path)
        self.path.write_bytes(ds.checkpoint_bytes('nod', self.policy, '3.10.0', payload={'format_version': 2, 'policy': {}}))
        with self.assertRaises(duck_policy.CheckpointError):
            duck_policy.read_checkpoint(self.path)
        missing = {k: v for k, v in self.policy.items() if k != 'obs_var'}
        self.path.write_bytes(ds.checkpoint_bytes('nod', missing, '3.10.0'))
        with self.assertRaises(duck_policy.CheckpointError):
            duck_policy.load_policy(self.path)


class Packages(unittest.TestCase):
    """A package is unpacked only inside its own folder and only when its files are the listed ones."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def unpack(self, data):
        (self.root / 'p.zip').write_bytes(data)
        return duck_kit.unpack(self.root / 'p.zip', self.root / 'out')

    def test_a_package_is_unpacked_with_its_files_checked(self):
        target = self.unpack(ds.package_bytes('nod', '3.10.0'))
        self.assertTrue((target / 'nod.pt').is_file() and (target / 'duck_show' / 'env.py').is_file())
        self.assertFalse((self.root / 'out.part').exists())

    def test_a_file_that_is_not_the_listed_one_or_no_list_at_all_is_refused_and_nothing_is_left(self):
        data = io.BytesIO(ds.package_bytes('nod', '3.10.0'))
        with zipfile.ZipFile(data, 'a') as archive:
            archive.writestr('replay.mp4', b'another video')           # the later entry wins on unpacking
        for bad in (data.getvalue(), ds.package_bytes('nod', '3.10.0', sums=False)):
            with self.assertRaises(duck_kit.KitError):
                self.unpack(bad)
            self.assertFalse((self.root / 'out').exists() or (self.root / 'out.part').exists())

    def test_a_path_out_of_the_folder_or_a_link_is_refused(self):
        for name in ('../escape.txt', '/abs.txt', 'a/../../escape.txt'):
            out = io.BytesIO()
            with zipfile.ZipFile(out, 'w') as archive:
                archive.writestr(name, b'x')
            with self.assertRaises(duck_kit.KitError):
                self.unpack(out.getvalue())
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            link = zipfile.ZipInfo('link')
            link.external_attr = 0o120777 << 16
            archive.writestr(link, '/etc/passwd')
        with self.assertRaises(duck_kit.KitError):
            self.unpack(out.getvalue())
        self.assertFalse((self.root / 'escape.txt').exists())


class Replay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'replay.mp4'
        self.path.write_bytes(bytes(range(100)))

    def test_the_whole_video_or_the_byte_range_a_player_asks_for(self):
        status, headers, body = duck_kit.replay_response(self.path)
        self.assertEqual((status, headers['Content-Type'], headers['Accept-Ranges'], len(body)), (200, 'video/mp4', 'bytes', 100))
        status, headers, body = duck_kit.replay_response(self.path, 'bytes=10-19')
        self.assertEqual((status, headers['Content-Range'], body), (206, 'bytes 10-19/100', bytes(range(10, 20))))
        status, headers, body = duck_kit.replay_response(self.path, 'bytes=90-')
        self.assertEqual((status, headers['Content-Range'], headers['Content-Length']), (206, 'bytes 90-99/100', '10'))
        status, headers, body = duck_kit.replay_response(self.path, 'bytes=-5')
        self.assertEqual((status, body), (206, bytes(range(95, 100))))
        self.assertEqual(duck_kit.replay_response(self.path, 'bytes=0-999')[1]['Content-Range'], 'bytes 0-99/100')

    def test_a_range_past_the_end_is_refused_and_no_video_is_no_answer(self):
        status, headers, body = duck_kit.replay_response(self.path, 'bytes=100-')
        self.assertEqual((status, headers['Content-Range'], body), (416, 'bytes */100', b''))
        self.assertIsNone(duck_kit.replay_response(None))
        self.assertIsNone(duck_kit.replay_response(self.path.with_name('gone.mp4')))


class Training(unittest.TestCase):
    DATA = {'required_successes': 10, 'generated_at': 100.0, 'rounds': [{}, {}, {}],
            'tasks': [{'id': 'water', 'title': '单脚探水', 'active': True, 'qualified': True,
                       'best': {'round_number': 6, 'iteration': 4, 'successes': 10, 'episodes': 10, 'total_steps': 8192, 'training_backend': 'cuda'},
                       'current': {'round_number': 9, 'iteration': 8, 'successes': 7, 'episodes': 10, 'total_steps': 99, 'training_backend': 'cpu'},
                       'events': [{'round_number': 9, 'iteration': 8, 'successes': 7, 'episodes': 10, 'report_mtime': 30.0, 'report_url': '/files/x'},
                                  {'round_number': 6, 'iteration': 4, 'successes': 10, 'episodes': 10, 'report_mtime': 20.0},
                                  {'round_number': 5, 'iteration': 8, 'successes': None, 'episodes': 10}, 'junk']}],
            'archived_tasks': [{'id': 'drunk', 'title': '旧版', 'active': False, 'events': []}],
            'source': {'last_update': 90.0, 'sync': {'state': 'error', 'last_success': 80.0, 'error': 'ssh: secret host'}}}

    def test_the_page_gets_best_latest_and_the_series_in_time_order_and_nothing_else(self):
        out = duck_kit.trim_training(self.DATA)
        water, drunk = out['tasks']
        self.assertEqual(water['best'], {'round': 6, 'update': 4, 'successes': 10, 'episodes': 10, 'steps': 8192, 'device': 'GPU'})
        self.assertEqual(water['latest']['device'], 'CPU')
        self.assertEqual(water['series'], [[6, 4, 10, 10, 20.0], [9, 8, 7, 10, 30.0]])
        self.assertEqual((water['evaluations'], drunk['active'], drunk['best'], out['rounds'], out['sync_state']), (2, False, None, 3, 'error'))
        self.assertNotIn('secret', json.dumps(out))
        self.assertNotIn('/files/', json.dumps(out))

    def test_figures_that_are_not_whole_numbers_are_left_out_not_passed_on(self):
        data = {'tasks': [{'id': 't', 'best': {'round_number': 1, 'iteration': 2, 'successes': 1, 'episodes': '<img src=x>'},
                           'events': [{'round_number': 'r9', 'iteration': 1, 'successes': 3, 'episodes': 10, 'report_mtime': 'late'},
                                      {'round_number': 2, 'iteration': '4', 'successes': 5, 'episodes': 10, 'report_mtime': 5},
                                      {'round_number': 1, 'iteration': 1, 'successes': True, 'episodes': 10}]}]}
        task = duck_kit.trim_training(data)['tasks'][0]
        self.assertIsNone(task['best']['episodes'])
        self.assertEqual(task['series'], [[None, 1, 3, 10, None], [2, None, 5, 10, 5]])

    def test_what_is_not_the_dashboards_data_is_nothing(self):
        for bad in (None, [], {'tasks': 'x'}, {'rounds': []}):
            self.assertIsNone(duck_kit.trim_training(bad))

    def test_only_a_dashboard_on_this_computer_is_ever_asked(self):
        for url, expected in (('', 'http://127.0.0.1:8767'), ('http://localhost:9000/x', 'http://localhost:9000'),
                              ('http://10.0.0.5:8767', ''), ('https://127.0.0.1:8767', ''), ('http://user:pw@127.0.0.1:1', ''),
                              ('file:///etc/passwd', ''), ('http://127.0.0.1:abc', '')):
            with mock.patch.dict(os.environ, {duck_kit.DASHBOARD_ENV: url}):
                self.assertEqual(duck_kit.dashboard_url(), expected, url)


class Listed(unittest.TestCase):
    def test_the_duck_is_a_biped_profile_of_its_brand_and_model(self):
        profile = sim_profiles.BY_ID[duck_kit.PROFILE_ID]
        self.assertEqual((profile['vendor'], profile['model'], profile['type'], profile.get('duck')), ('Microduck', 'AD-P102', 'biped', True))
        self.assertEqual(sim_profiles.canonical('microduck', 'adp102'), ('Microduck', 'AD-P102'))
        self.assertTrue((ROOT / 'web' / 'robot_images' / 'microduck-adp102.webp').is_file())

    def test_every_packaged_skill_model_names_one_package_by_checksum_and_how_it_was_accepted(self):
        ids = [show['id'] for show in duck_kit.SHOWS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([s['id'] for s in duck_kit.SHOWS if s['status'] == duck_kit.TRAINING], ['floor_spin', 'recharge'])
        for show in duck_kit.PACKAGED:
            self.assertRegex(show['sha256'], r'^[0-9a-f]{64}$')
            self.assertTrue(show['package'].endswith('.zip') and show['origin'] and show['acceptance'], show['id'])
            fixed, independent = show['acceptance']['fixed'], show['acceptance']['independent']
            delivered = fixed == [10, 10] and independent == [10, 10]
            self.assertEqual(show['status'] == duck_kit.DELIVERED, delivered, show['id'])     # 交付要求两组各 10/10

    def test_no_weights_video_or_mesh_of_the_duck_is_kept_in_the_repository(self):
        kept = [p for p in ROOT.rglob('*') if p.suffix.lower() in ('.pt', '.onnx', '.mp4', '.stl')
                and not {'.build', '.git', 'dist', 'artifacts', 'node_modules'} & set(p.relative_to(ROOT).parts)]
        self.assertEqual(kept, [])

    def test_the_page_logic(self):
        import shutil
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        result = subprocess.run([node, 'tests/duck_logic.cjs'], capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'duck_logic: \d+ checks passed')


class Case(unittest.TestCase):
    TASKS = ('nod', 'long', 'fall', 'crash', 'stuck')
    VERSIONS = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kit, shows = ds.kit(Path(self.tmp.name) / 'kit', self.TASKS, installed_mujoco() if HAVE_MUJOCO else '0', self.VERSIONS)
        for name, value in (('SHOWS', shows), ('BY_ID', {s['id']: s for s in shows}), ('PACKAGED', shows)):
            patch = mock.patch.object(duck_kit, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        dead = mock.patch.dict(os.environ, {duck_kit.DASHBOARD_ENV: 'http://127.0.0.1:9'})     # nothing listens there
        dead.start()
        self.addCleanup(dead.stop)
        self.app = Platform(self.tmp.name + '/platform')
        self.addCleanup(lambda: self.app.close())
        self.app.duck.kit = self.kit

    def refused(self, code, call, *args):
        with self.assertRaises(CodedError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)

    def add(self):
        return self.app.add_sim_robot({'profile_id': duck_kit.PROFILE_ID})

    def wait(self, robot_id, run_id, states=ENDED, seconds=60):
        end = time.time() + seconds
        while time.time() < end:
            status = self.app.duck.status({'robot_id': robot_id})
            if status['run_id'] == run_id and status['state'] in states:
                return status
            time.sleep(0.05)
        self.fail(f'no {states} within {seconds} s: {status}')


class Catalog(Case):
    def test_the_page_gets_the_skill_models_and_which_ones_have_a_package_here(self):
        catalog = self.app.snapshot()['duck_kit']
        self.assertEqual((catalog['vendor'], catalog['model'], catalog['available'], catalog['problem']), ('Microduck', 'AD-P102', True, ''))
        self.assertEqual([(s['id'], s['packaged'], s['status_name']) for s in catalog['shows']],
                         [(task, True, '已交付') for task in self.TASKS])
        row = [p for p in self.app.sim_profiles.catalog()['profiles'] if p['id'] == duck_kit.PROFILE_ID][0]
        self.assertTrue(row['available'])

    def test_without_a_kit_or_its_engine_the_duck_cannot_be_added_and_says_why(self):
        self.app.duck.kit = Path(self.tmp.name) / 'nothing'
        self.assertIn('没有它的机器人模型', self.app.duck.problem())
        self.assertIn('没有它的机器人模型', self.refused('sim_profile_unavailable', self.add))
        self.assertEqual(self.app.store.list('robots'), [])
        self.app.duck.kit = self.kit
        kit = json.loads((self.kit / 'kit.json').read_text())
        kit['engine'] = {'mujoco': '3.10.0'}
        (self.kit / 'kit.json').write_text(json.dumps(kit))
        self.assertIn('物理引擎', self.app.duck.problem())
        row = [p for p in self.app.sim_profiles.catalog()['profiles'] if p['id'] == duck_kit.PROFILE_ID][0]
        self.assertEqual((row['available'], row['problem']), (False, self.app.duck.problem()))

    def test_one_package_that_cannot_be_used_does_not_take_the_others_with_it(self):
        (self.kit / 'shows' / 'fall.zip').write_bytes(b'not a package')
        if not HAVE_MUJOCO:
            self.skipTest('needs the mujoco package')
        robot = self.add()
        self.assertEqual(self.app.duck.status({'robot_id': robot['id']})['shows'], sorted(set(self.TASKS) - {'fall'}))

    def test_a_package_that_is_not_the_registered_one_is_not_used(self):
        (self.kit / 'shows' / 'nod.zip').write_bytes(ds.package_bytes('nod', '3.10.0', more={'extra.txt': b'changed'}))
        self.assertIn('不是登记的那一份', self.refused('duck_package_changed', self.app.duck.package, 'nod'))
        self.refused('duck_show_missing', self.app.duck.package, 'unknown')
        self.assertIsNone(self.app.duck.replay('nod'))

    def test_the_replay_of_a_skill_model_is_the_video_in_its_package(self):
        path = self.app.duck.replay('nod')
        self.assertEqual(path.read_bytes()[4:12], b'ftypmp42')
        self.assertTrue(path.is_relative_to(Path(self.app.store.root) / 'duck-kit'))

    def test_the_training_progress_is_the_kits_copy_when_no_dashboard_answers(self):
        self.assertEqual(self.app.duck.training()['source'], 'none')
        (self.kit / 'training.json').write_text(json.dumps(duck_kit.trim_training(Training.DATA)))
        out = self.app.duck.training()
        self.assertEqual((out['source'], out['tasks'][0]['id']), ('snapshot', 'water'))


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class Performing(Case):
    def test_a_simulated_duck_is_an_online_robot_of_its_brand_and_model(self):
        robot = self.add()
        self.assertEqual((robot['status'], robot['family'], robot['sim']['state'], robot['sim']['duck']), ('connected', 'biped', 'running', True))
        self.assertEqual((robot['product']['vendor'], robot['product']['model']), ('Microduck', 'AD-P102'))
        self.assertEqual(sorted(c['id'] if isinstance(c, dict) else c for c in robot['capabilities']), ['policy.run', 'safety.stop', 'system.health'])   # issue #67: its skill models are run by name
        second = self.add()
        self.assertNotEqual(second['name'], robot['name'])

    def test_it_performs_a_skill_model_and_reports_phases_and_the_result(self):
        robot = self.add()
        run = self.app.duck.perform({'robot_id': robot['id'], 'show': 'nod', 'seed': 7})
        status = self.wait(robot['id'], run['run_id'])
        self.assertEqual(status['state'], 'succeeded', status)
        self.assertEqual(status['result'], {'success': True, 'reason': 'complete', 'steps': 25, 'duration_s': 0.5, 'phases': ['低头', '抬头']})
        self.assertEqual((status['engine'], status['shows'], status['error']), (installed_mujoco(), sorted(self.TASKS), ''))
        audited = [e for e in self.app.store.list('events') if e.get('action') == 'duck.show']
        self.assertEqual(len(audited), 1)

    def test_a_show_that_the_judge_fails_is_reported_as_failed_with_the_reason(self):
        robot = self.add()
        status = self.wait(robot['id'], self.app.duck.perform({'robot_id': robot['id'], 'show': 'fall'})['run_id'])
        self.assertEqual((status['state'], status['result']['success'], status['result']['reason']), ('failed', False, 'nonfoot_ground_contact'))
        status = self.wait(robot['id'], self.app.duck.perform({'robot_id': robot['id'], 'show': 'crash'})['run_id'])
        self.assertEqual((status['state'], status['result']['reason']), ('failed', 'simulation_error'))
        status = self.wait(robot['id'], self.app.duck.perform({'robot_id': robot['id'], 'show': 'stuck'})['run_id'])
        self.assertEqual((status['state'], status['result']['reason'], status['result']['steps']), ('failed', 'simulation_error', 0))
        status = self.wait(robot['id'], self.app.duck.perform({'robot_id': robot['id'], 'show': 'nod'})['run_id'])
        self.assertEqual(status['state'], 'succeeded')                       # and the duck performs again afterwards

    def test_when_the_ducks_process_is_gone_a_show_is_not_reported_as_still_running(self):
        robot = self.add()
        run = self.app.duck.perform({'robot_id': robot['id'], 'show': 'long'})
        self.wait(robot['id'], run['run_id'], ('running',))
        self.app.sims.procs[robot['id']].proc.kill()
        self.app.sims.procs[robot['id']].proc.wait(10)
        status = self.app.duck.status({'robot_id': robot['id']})
        self.assertEqual((status['state'], status['running']), ('error', False))
        self.assertIn('进程已经不在了', status['error'])
        self.refused('duck_not_running', self.app.duck.perform, {'robot_id': robot['id'], 'show': 'nod'})

    def test_a_show_is_stopped_from_the_page_and_by_the_robots_own_stop(self):
        robot = self.add()
        run = self.app.duck.perform({'robot_id': robot['id'], 'show': 'long'})
        self.wait(robot['id'], run['run_id'], ('running',))
        self.app.duck.stop({'robot_id': robot['id']})
        status = self.wait(robot['id'], run['run_id'])
        self.assertEqual((status['state'], status['result']['reason']), ('stopped', 'stopped'))
        run = self.app.duck.perform({'robot_id': robot['id'], 'show': 'long'})
        self.wait(robot['id'], run['run_id'], ('running',))
        self.app.stop_robot({'robot_id': robot['id']})                       # the stop button of 我的机器人
        status = self.wait(robot['id'], run['run_id'])
        self.assertEqual(status['state'], 'stopped')

    def test_what_cannot_be_performed_is_refused_in_words(self):
        robot = self.add()
        self.refused('duck_show_missing', self.app.duck.perform, {'robot_id': robot['id'], 'show': 'nothing'})
        self.refused('duck_show_missing', self.app.duck.perform, {'robot_id': robot['id'], 'show': ['nod']})
        self.refused('duck_robot_unknown', self.app.duck.status, {'robot_id': {'x': 1}})
        for seed in (-1, 2 ** 32, 1.5, True, '7'):
            self.refused('duck_seed_invalid', self.app.duck.perform, {'robot_id': robot['id'], 'show': 'nod', 'seed': seed})
        self.refused('duck_robot_unknown', self.app.duck.status, {'robot_id': 'robot-none'})
        other = self.app.add_sim_robot({'kind': 'quadruped'})
        self.refused('duck_robot_unknown', self.app.duck.perform, {'robot_id': other['id'], 'show': 'nod'})
        self.app.sims.stop_process(robot['id'])
        self.assertIn('没有在运行', self.refused('duck_not_running', self.app.duck.perform, {'robot_id': robot['id'], 'show': 'nod'}))

    def test_it_comes_back_when_the_app_is_opened_again(self):
        robot = self.add()
        self.app.close()
        self.app = Platform(self.tmp.name + '/platform')
        self.addCleanup(lambda: self.app.close())
        self.app.duck.kit = self.kit
        deadline = time.monotonic() + 60            # brought back in the background, as every simulated robot is
        while self.app.store.get('robots', robot['id'])['sim']['state'] != 'running' and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(self.app.store.get('robots', robot['id'])['sim']['state'], 'running')
        status = self.wait(robot['id'], self.app.duck.perform({'robot_id': robot['id'], 'show': 'nod'})['run_id'])
        self.assertEqual(status['state'], 'succeeded')


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class OtherEngine(Case):
    TASKS = ('nod',)
    VERSIONS = {'nod': '0.0.1'}

    def test_a_skill_model_accepted_on_another_engine_version_is_not_run(self):
        robot = self.add()
        run = self.app.duck.perform({'robot_id': robot['id'], 'show': 'nod'})
        status = self.wait(robot['id'], run['run_id'])
        self.assertEqual(status['state'], 'error')
        self.assertIn('MuJoCo 0.0.1', status['error'])
        self.assertIn(installed_mujoco(), status['error'])


@unittest.skipUnless((REAL_KIT / 'kit.json').is_file() and (REAL_KIT / 'engine' / 'mujoco').is_dir(), 'needs the kit (scripts/duck_kit.py build)')
class RealKit(unittest.TestCase):
    """The delivered packages on the engine they were accepted on, through the physics child itself."""

    def test_the_kit_holds_the_registered_packages(self):
        kit = duck_kit.read_kit(REAL_KIT)
        for show in duck_kit.PACKAGED:
            if show['id'] in kit['shows']:
                with open(REAL_KIT / 'shows' / (show['id'] + '.zip'), 'rb') as stream:
                    self.assertEqual(hashlib.file_digest(stream, 'sha256').hexdigest(), show['sha256'], show['id'])
        self.assertTrue(kit['shows'])

    def test_the_bow_runs_exactly_as_recorded_without_pytorch_on_the_pinned_engine(self):
        kit = duck_kit.read_kit(REAL_KIT)
        if 'bow_roll' not in kit['shows']:
            self.skipTest('no bow_roll package in this kit')
        with tempfile.TemporaryDirectory() as tmp:
            package = duck_kit.unpack(REAL_KIT / 'shows' / 'bow_roll.zip', Path(tmp) / 'bow')
            env = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(ROOT), os.environ.get('PYTHONPATH', '')])}
            child = subprocess.Popen([sys.executable, '-u', '-m', 'brainos.duck_engine', '--package', str(package), '--view', str(Path(tmp) / 'view'),
                                      '--engine', str(duck_paths.engine_dir(REAL_KIT))], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env, text=True)
            try:
                ready = json.loads(child.stdout.readline())
                self.assertEqual((ready['event'], ready['task'], ready['mujoco']), ('ready', 'bow_roll', duck_kit.ENGINE['mujoco']), ready)
                child.stdin.write(json.dumps({'cmd': 'show', 'id': 'r', 'seed': 1_700_003_000}) + '\n')
                child.stdin.flush()
                done = None
                for line in child.stdout:
                    event = json.loads(line)
                    if event['event'] == 'done':
                        done = event
                        break
            finally:
                child.stdin.close()
                child.wait(10)
                child.stdout.close()
        # The delivered replay of this seed: 227 control steps, 4.54 s (the package's 使用说明.md).
        self.assertEqual((done['success'], done['steps'], done['duration_s'], done['phases'][-1]), (True, 227, 4.54, '回正'), done)


if __name__ == '__main__':
    unittest.main()
