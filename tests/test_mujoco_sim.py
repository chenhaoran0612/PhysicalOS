"""Issue #51: a simulation environment for an application — the robot model comes from the library of the
application's brand and model, the environment is written by the engineering agent (or is empty), a
person confirms it, and a MuJoCo robot of that brand and model starts in it.

The library is a stand-in that hands out made-up robot models; the agent is the deterministic stand-in
with a scripted model. The simulated robot is the real edge runtime in a child process.
Needs the ``mujoco`` package; skipped where it is not installed."""
import contextlib
import hashlib
import importlib.util
import json
import shlex
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import mujoco_support as ms
from brainos import agent_runtime, mujoco_sim, package_fetch, robot_model
from brainos.errors import CodedError
from brainos.service import Platform
from codex_standin import CodexStandIn
from test_engineering import ResponsesFixture
from test_product import Server

HAVE_MUJOCO = importlib.util.find_spec('mujoco') is not None
PRODUCT = {'vendor': 'Acme Robotics', 'model': 'Go-7'}
REAL_AVAILABLE = agent_runtime.available


class Library:
    """The cloud library as this feature uses it: ``list`` with filters, and the files behind the records."""

    def __init__(self):
        self.materials, self.files, self.parses, self.calls = [], {}, {}, []

    def add(self, filename, data, fmt, status='usable', kind='model', **labels):
        sha256 = hashlib.sha256(data).hexdigest()
        material = {'id': 'mat_' + sha256[:20], 'filename': filename, 'kind': kind, 'format': fmt, 'size': len(data),
                    'sha256': sha256, 'status': status, 'version': '1.0', 'body_type': 'quadruped',
                    'created_at': 100.0 + len(self.materials), **PRODUCT, **labels}
        self.materials.insert(0, material)
        self.files[sha256] = data
        return material

    def list(self, p=None):
        p = dict(p or {})
        self.calls.append(p)
        rows = [m for m in self.materials
                if all(str(m.get(k, '')).casefold() == str(p[k]).casefold() for k in ('kind', 'vendor', 'model', 'status') if k in p)]
        return {'materials': rows[:p.get('limit', 50)], 'total': len(rows)}

    def _call(self, method, path, payload=None, timeout=None):
        material_id = path.split('/')[3]
        if material_id not in self.parses:
            raise ValueError('这份资料没有解析结果')
        return self.parses[material_id]


class SceneModel(ResponsesFixture):
    """The scripted model: its first answer runs one command in the workspace, its second ends the turn."""
    requests = []
    command = 'true'

    def select_item(self, data, _fallback):
        if any(x.get('type') == 'function_call_output' for x in data.get('input', [])):
            return {'id': 'msg_done', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': '环境描述已写好。', 'annotations': []}]}
        return {'id': 'fc_scene', 'type': 'function_call', 'call_id': 'call_scene', 'name': 'exec_command',
                'arguments': json.dumps({'cmd': type(self).command})}


def writes(scene):
    return 'cat SIMULATION.json > seen.json; printf %s ' + shlex.quote(json.dumps(scene, ensure_ascii=False)) + ' > scene.json'


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class Case(unittest.TestCase):
    family, urdf = 'quadruped', staticmethod(ms.dog_urdf)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Platform(self.tmp.name + '/platform')
        self.addCleanup(lambda: self.app.close())
        self.pid = self.app.add_project({'name': '巡检', 'objective': '从门口走到货架，把箱子推到墙边'})['id']
        self.library = self.app.library = Library()
        self.fetched = []

        def fetch(_source, material, cache_dir, **_more):
            path = Path(cache_dir)
            path.mkdir(parents=True, exist_ok=True)
            target = path / (material['sha256'] + '.pkg')
            target.write_bytes(self.library.files[material['sha256']])
            self.fetched.append(material['id'])
            return {'path': str(target)}
        for name, stand_in in (('fetch', fetch), ('forget', lambda cache, sha: (Path(cache) / (sha + '.pkg')).unlink(missing_ok=True))):
            patcher = mock.patch.object(package_fetch, name, stand_in)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.robot = self.app.add_robot({'project_id': self.pid, 'name': '一号', 'family': self.family, **PRODUCT})
        skill = self.app.add_skill({'project_id': self.pid, 'name': '走到门口', 'version': '1.0', 'requires': ['navigation.goal'],
                                    'entrypoint': 'navigate', 'success_condition': 'at goal', 'stop_condition': 'timeout',
                                    'model_ids': [], 'robot_id': self.robot['id']})
        self.application = self.app.add_application({'project_id': self.pid, 'name': '送货', 'version': '1.0',
                                                     'robot_id': self.robot['id'],
                                                     'steps': [{'skill_id': skill['id'], 'parameters': {'place': '门口'}, 'timeout_s': 30}]})
        self.sim = self.app.mujoco
        self.ask = {'project_id': self.pid, 'application_id': self.application['id']}

    def model(self, **more):
        return self.library.add('dog.zip', ms.dog_archive(), 'zip', **more)

    def wait(self, scene_id, *states, seconds=90):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            scene = self.sim.scene({'project_id': self.pid, 'scene_id': scene_id})
            if scene['status'] in states:
                return scene
            if not scene['busy']:
                self.fail(f"scene ended as {scene['status']}: {scene.get('error')}")
            time.sleep(0.1)
        self.fail('the scene was not ready in time')

    def refused(self, code, call, *args):
        with self.assertRaises(CodedError) as caught:
            call(*args)
        self.assertEqual(caught.exception.code, code)
        return str(caught.exception)

    def confirmed(self):
        self.model()
        scene = self.wait(self.sim.empty(self.ask)['id'], 'awaiting_review')
        return self.sim.review({'project_id': self.pid, 'scene_id': scene['id'], 'decision': 'confirm'})


class RobotModelOfTheBrandAndModel(Case):
    def test_without_a_confirmed_robot_model_the_page_is_told_what_is_missing(self):
        view = self.sim.overview(self.ask)
        self.assertEqual((view['engine'], view['robot_model'], view['problem']['code']), (True, None, 'sim_no_robot_model'))
        self.assertIn('资料库', view['problem']['message'])
        self.model(status='parsed')
        view = self.sim.overview(self.ask)
        self.assertEqual((view['unconfirmed_models'], view['problem']['code']), (1, 'sim_no_robot_model'))
        self.assertIn('已上传 1 份', view['problem']['message'])
        self.refused('sim_no_robot_model', self.sim.empty, self.ask)

    def test_the_newest_confirmed_model_of_this_brand_and_model_is_the_one_used(self):
        self.library.add('other.urdf', ms.arm_urdf(), 'urdf', vendor='Other', model='Arm-1')
        self.library.add('manual.urdf', ms.dog_urdf(None), 'urdf', kind='document')
        self.library.add('old.urdf', ms.dog_urdf(None), 'urdf', version='0.9')
        newest = self.model(version='2.0')
        view = self.sim.overview(self.ask)
        self.assertEqual((view['problem'], view['robot_model']['id'], view['family']), (None, newest['id'], 'quadruped'))
        asked = self.library.calls[-1]
        self.assertEqual((asked['kind'], asked['vendor'], asked['model']), ('model', 'Acme Robotics', 'Go-7'))

    def test_an_application_without_a_brand_and_model_cannot_be_simulated(self):
        robot = self.app.add_robot({'project_id': self.pid, 'name': '无名', 'family': 'quadruped'})
        skill = self.app.add_skill({'project_id': self.pid, 'name': '站立', 'version': '1.0', 'requires': ['navigation.goal'],
                                    'entrypoint': 'stand', 'success_condition': 'up', 'stop_condition': 'timeout', 'model_ids': []})
        application = self.app.add_application({'project_id': self.pid, 'name': '旧应用', 'version': '1.0', 'robot_id': robot['id'],
                                                'steps': [{'skill_id': skill['id'], 'parameters': {}, 'timeout_s': 30}]})
        ask = {'project_id': self.pid, 'application_id': application['id']}
        self.assertEqual(self.sim.overview(ask)['problem']['code'], 'sim_no_product')
        self.refused('sim_no_product', self.sim.empty, ask)


class EmptyEnvironment(Case):
    def test_it_is_fetched_unpacked_built_once_and_waits_for_a_person(self):
        material = self.model()
        started = self.sim.empty(self.ask)
        self.assertEqual((started['status'], started['busy'], started['source']), ('preparing', True, 'empty'))
        scene = self.wait(started['id'], 'awaiting_review')
        self.assertEqual((scene['material']['id'], scene['product'], scene['family']), (material['id'], PRODUCT, 'quadruped'))
        self.assertAlmostEqual(scene['checked']['stand_height'], 0.4, delta=0.01)
        self.assertEqual(len(scene['checked']['joints']), 8)
        self.assertTrue(scene['preview'] or scene['render_error'])
        if scene['preview']:
            self.assertEqual(self.sim.preview(scene['id'])[:8], b'\x89PNG\r\n\x1a\n')
        self.assertNotIn('model_dir', scene)
        model_dir = Path(self.app.store.root) / 'robot-models' / material['sha256']
        self.assertEqual(sorted(p.name for p in model_dir.iterdir()), ['meshes', 'model.json', 'model.urdf'])
        self.assertEqual(list((Path(self.app.store.root) / 'robot-model-cache').glob('*')), [])       # the download is not kept
        again = self.wait(self.sim.empty(self.ask)['id'], 'awaiting_review')
        self.assertEqual(self.fetched, [material['id']])                                              # the unpacked model is reused
        self.assertEqual(len(self.sim.overview(self.ask)['scenes']), 2)
        self.assertNotEqual(again['id'], scene['id'])

    def test_only_a_checked_environment_can_be_confirmed_and_a_person_may_refuse_it(self):
        self.model()
        gate = threading.Event()
        real = self.sim.prepared
        with mock.patch.object(self.sim, 'prepared', lambda material: (gate.wait(30), real(material))[1]):
            started = self.sim.empty(self.ask)
            self.refused('sim_scene_not_ready', self.sim.review, {'project_id': self.pid, 'scene_id': started['id'], 'decision': 'confirm'})
            self.refused('sim_scene_busy', self.sim.empty, self.ask)
            self.refused('sim_scene_not_ready', self.sim.add_robot, {'project_id': self.pid, 'scene_id': started['id']})
            gate.set()
            self.wait(started['id'], 'awaiting_review')
        ask = {'project_id': self.pid, 'scene_id': started['id']}
        with self.assertRaises(ValueError):
            self.sim.review({**ask, 'decision': 'maybe'})
        self.assertEqual(self.sim.review({**ask, 'decision': 'reject'})['status'], 'rejected')
        self.refused('sim_scene_not_ready', self.sim.add_robot, ask)
        self.assertEqual(self.sim.review({**ask, 'decision': 'confirm'})['status'], 'confirmed')
        self.assertEqual(self.sim.remove(ask), {'removed': started['id']})
        self.assertEqual(self.sim.overview(self.ask)['scenes'], [])
        self.assertFalse((Path(self.app.store.root) / 'mujoco-scenes' / started['id']).exists())

    def test_a_model_that_cannot_be_built_fails_this_environment_and_says_why(self):
        self.library.add('broken.zip', ms.zip_of({'a.urdf': ms.dog_urdf(), 'b.urdf': ms.dog_urdf()}), 'zip')
        started = self.sim.empty(self.ask)
        deadline = time.monotonic() + 30
        while self.sim.scene({'project_id': self.pid, 'scene_id': started['id']})['busy'] and time.monotonic() < deadline:
            time.sleep(0.05)
        scene = self.sim.scene({'project_id': self.pid, 'scene_id': started['id']})
        self.assertEqual((scene['status'], scene['error_code']), ('failed', 'sim_model_invalid'))
        self.assertIn('2 个模型文件', scene['error'])          # issue #77: URDF and MJCF files both count

    def test_what_cannot_start_as_a_robot_does_not_pass_the_check(self):
        """Review, 2026-11-26: the check built the scene but not the body, so such a model failed only after it was confirmed."""
        backwards = ms.arm_urdf().replace(b'lower="-2.0" upper="2.0"', b'lower="2.0" upper="-2.0"', 1)
        self.app.store.put('robots', {**self.app.store.get('robots', self.robot['id']), 'family': 'manipulator'}, owner=True)
        self.library.add('arm.urdf', backwards, 'urdf')
        started = self.sim.empty(self.ask)
        deadline = time.monotonic() + 60
        while self.sim.scene({'project_id': self.pid, 'scene_id': started['id']})['busy'] and time.monotonic() < deadline:
            time.sleep(0.05)
        scene = self.sim.scene({'project_id': self.pid, 'scene_id': started['id']})
        self.assertEqual((scene['status'], scene['error_code']), ('failed', 'sim_check_failed'))
        self.assertIn('lower', scene['error'])

    def test_two_environments_that_need_the_same_model_fetch_it_once(self):
        material = self.model()
        results, gate = [], threading.Barrier(2)

        def prepare():
            gate.wait(10)
            try:
                results.append(self.sim.prepared(material))
            except Exception as exc:                                    # noqa: BLE001  (the test reports whatever it was)
                results.append(exc)
        threads = [threading.Thread(target=prepare) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len({str(r) for r in results}), 1, results)
        self.assertTrue(Path(results[0]).is_dir())
        self.assertEqual(self.fetched, [material['id']])

    def test_an_environment_that_was_being_made_when_the_app_closed_is_failed_not_resumed(self):
        self.model()
        gate = threading.Event()
        self.addCleanup(gate.set)
        with mock.patch.object(self.sim, 'prepared', lambda material: gate.wait(30) and None):
            started = self.sim.empty(self.ask)
            reopened = mujoco_sim.MujocoSim(self.app)
            scene = reopened.scene({'project_id': self.pid, 'scene_id': started['id']})
            self.assertEqual((scene['status'], scene['error']), ('failed', mujoco_sim.INTERRUPTED))
            gate.set()

    def test_another_tasks_environment_is_not_reachable(self):
        scene = self.confirmed()
        other = self.app.add_project({'name': '别的任务', 'objective': '无关'})['id']
        for call in (self.sim.scene, self.sim.remove):
            with self.assertRaises(ValueError):
                call({'project_id': other, 'scene_id': scene['id']})
        with self.assertRaises(ValueError):
            self.sim.overview({'project_id': other, 'application_id': self.application['id']})


class WrittenByTheAgent(Case):
    def setUp(self):
        super().setUp()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        if not agent_runtime.binary_path():
            stack.enter_context(mock.patch.object(agent_runtime, 'AgentProcess', CodexStandIn))
            stack.enter_context(mock.patch.object(agent_runtime, 'available', lambda: {**REAL_AVAILABLE(), 'available': True}))
        SceneModel.requests = []
        self.server = Server(SceneModel)
        self.addCleanup(self.server.close)
        self.agent_model = self.app.engineering.model_add({'project_id': self.pid, 'name': 'Fixture', 'model': 'fixture-agent',
                                                           'base_url': self.server.url + '/v1', 'api_key': 'fixture-' + 'k' * 20})
        self.model()
        self.library.add('manual.md', b'# manual', 'markdown', kind='document')
        self.library.parses[self.library.materials[0]['id']] = {'values': {'driver_name': 'demo_motion_driver', 'topics': ['/cmd_vel']}}
        self.generate = {**self.ask, 'model_id': self.agent_model['id'], 'data_authorized': True, 'note': '货架靠北墙'}

    def ended(self, scene_id):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            scene = self.sim.scene({'project_id': self.pid, 'scene_id': scene_id})
            if not scene['busy']:
                return scene
            time.sleep(0.1)
        self.fail('the scene did not end')

    def test_the_agent_gets_the_application_and_the_materials_and_its_scene_is_checked_and_shown(self):
        SceneModel.command = writes(ms.scene())
        scene = self.ended(self.sim.generate(self.generate)['id'])
        self.assertEqual((scene['status'], scene['source'], scene['error']), ('awaiting_review', 'agent', ''))
        self.assertEqual([o['name'] for o in scene['scene']['objects']], ['墙', '箱子'])
        self.assertEqual(scene['checked']['objects'], ['墙', '箱子'])
        self.assertEqual(scene['checked']['places'], ['门口', '墙后'])
        self.assertEqual(len(scene['lines']), 5)
        work = Path(self.app.store.root) / 'engineering' / scene['session_id'] / 'workspace'
        seen = json.loads((work / 'seen.json').read_text())
        self.assertEqual(seen['application']['name'], '送货')
        self.assertEqual(seen['application']['steps'][0]['parameters'], {'place': '门口'})
        self.assertEqual(seen['task']['objective'], '从门口走到货架，把箱子推到墙边')
        self.assertEqual((seen['robot']['brand'], seen['robot']['body_type']), ('Acme Robotics', 'quadruped'))
        self.assertAlmostEqual(seen['robot']['standing_height_m'], 0.4, delta=0.01)
        self.assertIn('demo_motion_driver', seen['materials'][0]['confirmed_values'])
        self.assertEqual((seen['extra_note'], seen['format']['schema']), ('货架靠北墙', 'neuxmind.mujoco-scene.v1'))
        session = self.app.store.get('agent_sessions', scene['session_id'])
        self.assertEqual((session['status'], session['mode'], session['mujoco_scene_id']), ('completed', 'engineering', scene['id']))
        self.assertNotIn('SIMULATION.json', [a['path'] for a in session['artifacts']])
        self.assertIn('货架靠北墙', session['objective'])

    def test_consent_and_a_usable_model_are_asked_for_before_anything_starts(self):
        self.refused('authorization_required', self.sim.generate, {**self.generate, 'data_authorized': False})
        with self.assertRaises(ValueError):
            self.sim.generate({**self.generate, 'note': 'x' * 2001})
        with self.assertRaises(ValueError):
            self.sim.generate({**self.generate, 'model_id': 'agent-model-missing'})
        self.assertEqual(self.sim.overview(self.ask)['scenes'], [])
        self.assertEqual(SceneModel.requests, [])

    def test_what_the_agent_wrote_is_data_and_a_scene_outside_the_format_is_refused(self):
        cases = [(writes({**ms.scene(), 'include': '/etc/passwd'}), 'sim_scene_invalid', 'include'),
                 ('printf %s \'<mujoco><include file="/etc/passwd"/></mujoco>\' > scene.json', 'sim_scene_invalid', 'JSON'),
                 ('ln -s /etc/hosts scene.json', 'sim_agent_failed', 'scene.json'),
                 ('true', 'sim_agent_failed', 'scene.json'),
                 (writes(ms.scene(objects=[{'name': '砸在身上', 'shape': 'box', 'size': [0.3, 0.3, 0.3], 'pos': [0, 0, 0.4],
                                           'movable': True, 'mass': 400}] * 1)), None, '')]
        for command, code, word in cases:
            with self.subTest(command=command[:40]):
                SceneModel.command = command
                scene = self.ended(self.sim.generate(self.generate)['id'])
                if code is None:                       # a legal scene, however odd, is built and left for the person to judge
                    self.assertEqual(scene['status'], 'awaiting_review')
                    continue
                self.assertEqual((scene['status'], scene['error_code']), ('failed', code))
                self.assertIn(word, scene['error'])
                self.assertNotIn('scene', {k for k in scene if k == 'scene'})


class RobotInTheEnvironment(Case):
    def test_a_robot_of_the_applications_brand_and_model_runs_in_a_confirmed_environment(self):
        scene = self.confirmed()
        robot = self.sim.add_robot({'project_id': self.pid, 'scene_id': scene['id']})
        self.addCleanup(self.app.sims.stop_process, robot['id'])
        self.assertEqual((robot['status'], robot['sim']['kind'], robot['sim']['state'], robot['family']),
                         ('connected', 'mujoco', 'running', 'quadruped'))
        self.assertEqual(robot['name'], 'Acme Robotics Go-7 仿真')
        saved = self.app.store.get('robots', robot['id'])
        self.assertEqual((robot_model.of(saved), saved['product']['source'], saved['manifest']['simulated']), (PRODUCT, 'device', True))
        self.assertEqual(self.app.robot_models.target(self.application, robot['id'])['id'], robot['id'])
        self.assertIn(robot['id'], [r['id'] for r in self.sim.overview(self.ask)['robots']])
        live = self.app.robot_live({'project_id': robot_model.robot_scope.HOME, 'robot_id': robot['id']})
        self.assertEqual((live['reachable'], live['simulated'], live['state']['mode']), (True, True, 'sim:stand'))
        deadline, frame = time.monotonic() + 20, None
        while frame is None and time.monotonic() < deadline:
            frame = self.sim.frame(robot['id'])
            time.sleep(0.2)
        view = self.sim.view({'project_id': robot_model.robot_scope.HOME, 'robot_id': robot['id']})
        self.assertTrue(frame is not None or view['render_error'], view)
        if frame:
            self.assertEqual(frame[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual((view['touching'], view['fault'], view['scene']['application_name']), ([], '', '送货'))
        self.refused('sim_scene_in_use', self.sim.remove, {'project_id': self.pid, 'scene_id': scene['id']})
        self.refused('sim_scene_in_use', self.sim.review, {'project_id': self.pid, 'scene_id': scene['id'], 'decision': 'reject'})
        self.app.sims.remove({'robot_id': robot['id']})
        self.assertIsNone(self.sim.frame(robot['id']))
        self.assertEqual(self.sim.remove({'project_id': self.pid, 'scene_id': scene['id']}), {'removed': scene['id']})

    def test_an_example_skill_runs_on_it_and_the_robot_moves_in_the_physics(self):
        """The same install and run path as on every simulated robot: nothing here is special to MuJoCo."""
        scene = self.confirmed()
        robot = self.sim.add_robot({'project_id': self.pid, 'scene_id': scene['id']})
        self.addCleanup(self.app.sims.stop_process, robot['id'])
        # Issue #69: the bundled package is not for this made-up brand, so it is installed from its folder.
        from brainos import runtime_bridge
        folder = str(runtime_bridge.examples_dir() / 'free_walk')
        with self.assertRaises(CodedError) as refused:
            self.app.preview_skill_package({'robot_id': robot['id'], 'example': 'free_walk'})
        self.assertEqual(refused.exception.code, 'skill_other_model')
        preview = self.app.preview_skill_package({'robot_id': robot['id'], 'path': folder})
        installed = self.app.install_skill_package({'robot_id': robot['id'], 'path': folder,
                                                    'package_digest': preview['package_digest'], 'request_id': 'mujoco-install-1'})
        self.assertEqual(installed['status'], 'succeeded', installed)
        started = self.app.run_robot_skill({'robot_id': robot['id'], 'skill_id': 'free_walk', 'hardware_authorized': True,
                                            'parameters': {'duration_s': 5, 'radius': 1.0}, 'request_id': 'mujoco-run-1'})
        self.assertEqual(started['status'], 'accepted', started)
        deadline, farthest, live = time.monotonic() + 60, 0.0, {}
        while time.monotonic() < deadline:
            live = self.app.robot_live({'project_id': robot_model.robot_scope.HOME, 'robot_id': robot['id']})
            pose = (live.get('state') or {}).get('pose') or {}
            farthest = max(farthest, abs(pose.get('x', 0.0)) + abs(pose.get('y', 0.0)))
            if (live.get('run') or {}).get('status') in ('succeeded', 'failed', 'stopped', 'timeout'):
                break
            time.sleep(0.3)
        self.assertEqual(live['run']['status'], 'succeeded', live.get('run'))
        self.assertGreater(farthest, 0.3)                      # it walked, by the simulation's own odometry
        self.assertEqual(self.sim.view({'project_id': robot_model.robot_scope.HOME, 'robot_id': robot['id']})['fault'], '')

    def test_a_robot_that_is_not_a_mujoco_simulation_has_no_picture(self):
        self.assertIsNone(self.sim.frame(self.robot['id']))
        self.assertIsNone(self.sim.frame('robot-missing'))
        self.assertIsNone(self.sim.preview('mujoco-scene-missing'))
        with self.assertRaises(ValueError):
            self.sim.view({'project_id': self.pid, 'robot_id': self.robot['id']})


if __name__ == '__main__':
    unittest.main()
