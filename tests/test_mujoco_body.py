"""Issue #51: the MuJoCo-backed simulated body. A robot built from a URDF drives, is stopped by what is in
its way, pushes what can move, and an arm reaches its joint targets; the runtime accepts it as a body.

Needs the ``mujoco`` package (requirements-runtime.txt); skipped where it is not installed."""
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import mujoco_support as ms
from brainos import mujoco_model, mujoco_scene

HAVE_MUJOCO = importlib.util.find_spec('mujoco') is not None


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class BodyCase(unittest.TestCase):
    family = 'quadruped'
    urdf = staticmethod(ms.dog_urdf)

    def setUp(self):
        from neuxmind_runtime.clock import ManualClock
        self.tmp = Path(tempfile.mkdtemp(prefix='nx-mjbody-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = ManualClock()

    def body(self, scene=None, urdf=None, **config):
        from brainos import mujoco_body
        (self.tmp / 'robot.urdf').write_bytes(urdf or self.urdf())
        shutil.rmtree(self.tmp / 'model', ignore_errors=True)
        mujoco_model.prepare(self.tmp / 'robot.urdf', 'urdf', self.tmp / 'model')
        settings = {'model_dir': str(self.tmp / 'model'), 'scene': mujoco_scene.validate(scene or ms.scene()),
                    'family': self.family, 'vendor': 'Acme Robotics', 'model': 'Go-7', 'instance_id': 'mujoco-test', **config}
        cls = mujoco_body.MujocoArm if self.family == 'manipulator' else mujoco_body.MujocoBase
        made = cls(settings, clock=self.clock)
        self.addCleanup(made.close)
        return made

    def run_for(self, body, seconds, step=0.1):
        for _ in range(int(round(seconds / step))):
            self.clock.advance(step)
            body.advance()

    def finish(self, body, handle, limit=60.0):
        waited = 0.0
        while waited < limit:
            status = handle.poll()
            if status.state != 'running':
                return status
            self.clock.advance(0.1)
            waited += 0.1
        self.fail('the action did not end')


class Quadruped(BodyCase):
    def test_the_runtime_accepts_it_and_it_reports_the_brand_and_model_it_was_built_from(self):
        from neuxmind_spec.manifest import validate_manifest_v2
        body = self.body()
        manifest = validate_manifest_v2(body.manifest())
        self.assertEqual((manifest['vendor'], manifest['model'], manifest['family'], manifest['simulated']),
                         ('Acme Robotics', 'Go-7', 'quadruped', True))
        capabilities = {c['id']: c.get('params', {}) for c in manifest['capabilities']}
        self.assertEqual(capabilities['base.navigate_to_place']['places'], ['门口', '墙后'])
        self.assertIn('legged.posture', capabilities)
        self.assertEqual(manifest['safety']['workspace'], {'x': [-6.0, 6.0], 'y': [-6.0, 6.0]})
        self.assertEqual(body.manifest(), body.manifest())
        self.assertEqual(body.read('system.health'), {'ok': True, 'mode': 'sim:stand', 'errors': {}})
        self.assertEqual(body.status()['engine'], 'mujoco')

    def test_it_stands_on_its_feet_and_stays_where_it_is(self):
        body = self.body()
        self.assertAlmostEqual(body.world.stand_height, 0.4, delta=0.01)
        self.run_for(body, 3.0)
        pose = body.read('base.odometry')
        self.assertLess(max(abs(v) for v in pose['pose'][:2]), 0.005)
        self.assertLess(max(abs(v) for v in pose['velocity']), 0.005)
        self.assertLess(max(abs(q) for q in body.world.joint_positions()), 0.02)

    def test_a_velocity_command_moves_it_and_a_stop_ends_the_motion(self):
        body = self.body(ms.scene(objects=[]))
        self.assertEqual(body.invoke('base.velocity', {'vx': 0.5, 'vy': 0.0, 'wz': 0.0}), {'accepted': True})
        self.run_for(body, 2.0)
        odometry = body.read('base.odometry')
        self.assertAlmostEqual(odometry['pose'][0], 1.0, delta=0.08)
        self.assertAlmostEqual(odometry['velocity'][0], 0.5, delta=0.03)
        body.stop()
        self.run_for(body, 1.0)
        after = body.read('base.odometry')
        self.assertLess(abs(after['velocity'][0]), 0.01)
        self.assertLess(after['pose'][0] - odometry['pose'][0], 0.08)

    def test_a_wall_stops_it_and_the_goal_behind_the_wall_fails_naming_the_wall(self):
        body = self.body()
        status = self.finish(body, body.start_action('base.navigate_to_place', {'place': '墙后'}))
        self.assertEqual(status.state, 'failed')
        self.assertIn('墙', status.error)
        x = body.read('base.odometry')['pose'][0]
        self.assertGreater(x, 0.9)
        self.assertLess(x, 1.5)                               # it never got through the wall at x = 1.5
        self.assertEqual(body.status()['touching'], ['墙'])

    def test_a_place_that_is_free_is_reached(self):
        body = self.body()
        status = self.finish(body, body.start_action('base.navigate_to_place', {'place': '门口'}))
        self.assertEqual((status.state, status.result['reached'], status.result['place']), ('succeeded', True, '门口'))
        self.assertAlmostEqual(body.read('base.odometry')['pose'][0], -2.0, delta=0.15)
        with self.assertRaises(ValueError):
            body.start_action('base.navigate_to_place', {'place': '会议室'})

    def test_it_pushes_what_can_move(self):
        scene = ms.scene(objects=[{'name': '箱子', 'shape': 'box', 'size': [0.2, 0.2, 0.2], 'pos': [0.8, 0, 0.1],
                                   'movable': True, 'mass': 0.5}])
        body = self.body(scene)
        body.invoke('base.velocity', {'vx': 0.4, 'vy': 0.0, 'wz': 0.0})
        self.run_for(body, 4.0)
        self.assertGreater(body.world.object_poses()['箱子'][0], 1.2)

    def test_a_goal_it_cannot_turn_towards_fails_too(self):
        """Review, 2026-11-26: held between two walls it only tried to turn, which did not count as blocked: the goal never ended."""
        walls = [{'name': name, 'shape': 'box', 'size': [3, 0.1, 0.8], 'pos': [0, y, 0.4]} for name, y in (('左墙', 0.3), ('右墙', -0.3))]
        body = self.body(ms.scene(objects=walls, places=[]))
        status = self.finish(body, body.start_action('base.navigate_to', {'goal': [0, 2, 0, 0, 0, 0, 1]}), limit=30)
        self.assertEqual(status.state, 'failed')
        self.assertIn('墙', status.error)

    def test_a_heavy_box_that_gives_way_is_not_what_stops_it(self):
        scene = ms.scene(objects=[{'name': '重箱子', 'shape': 'box', 'size': [0.3, 0.3, 0.3], 'pos': [0.8, 0, 0.15],
                                   'movable': True, 'mass': 20}], places=[])
        body = self.body(scene)
        handle = body.start_action('base.navigate_to', {'goal': [2.5, 0, 0, 0, 0, 0, 1]})
        self.run_for(body, 8.0)
        self.assertNotEqual(handle.poll().state, 'failed', handle.poll().error)
        self.assertGreater(body.world.object_poses()['重箱子'][0], 0.9)

    def test_the_world_keeps_the_clocks_time_whatever_the_step(self):
        body = self.body(ms.scene(objects=[], places=[]))
        body.invoke('base.velocity', {'vx': 0.5, 'vy': 0.0, 'wz': 0.0})
        for _ in range(600):                           # 6 s in steps that are not multiples of the physics step
            self.clock.advance(0.01)
            body.advance()
        self.assertAlmostEqual(float(body.world.data.time), 6.0, delta=0.02)
        self.assertAlmostEqual(body.read('base.odometry')['pose'][0], 3.0, delta=0.1)

    def test_a_model_with_looks_only_stands_on_them_and_says_it_cannot_touch(self):
        urdf = (b'<?xml version="1.0"?><robot name="shell"><link name="trunk"><inertial><mass value="5"/>'
                b'<inertia ixx="0.05" iyy="0.05" izz="0.05" ixy="0" ixz="0" iyz="0"/></inertial>'
                b'<visual><geometry><box size="0.4 0.2 0.4"/></geometry></visual></link></robot>')
        body = self.body(ms.scene(objects=[], places=[]), urdf=urdf)
        self.assertAlmostEqual(body.world.stand_height, 0.2, delta=0.01)
        self.assertFalse(body.world.summary()['collides'])

    def test_lying_down_lowers_the_body_and_it_does_not_walk_until_it_stands(self):
        body = self.body(ms.scene(objects=[]))
        body.invoke('legged.posture', {'posture': 'lie'})
        self.run_for(body, 2.0)
        self.assertAlmostEqual(body.world.base_pose()[3], 0.35 * body.world.stand_height, delta=0.02)
        self.assertEqual(body.invoke('base.velocity', {'vx': 0.5, 'vy': 0.0, 'wz': 0.0}), {'accepted': False})
        body.invoke('legged.posture', {'posture': 'stand'})
        self.run_for(body, 2.0)
        self.assertAlmostEqual(body.world.base_pose()[3], body.world.stand_height, delta=0.02)

    def test_a_scene_without_places_and_a_wheeled_body_declare_less(self):
        self.family = 'mobile'
        body = self.body(ms.scene(places=[]))
        ids = [c['id'] for c in body.manifest()['capabilities']]
        self.assertNotIn('base.navigate_to_place', ids)
        self.assertNotIn('legged.posture', ids)
        self.assertIn('base.velocity', ids)

    def test_a_body_type_that_is_not_supported_says_so(self):
        from brainos.mujoco_build import BuildError
        self.family = 'aerial'
        with self.assertRaises(BuildError) as caught:
            self.body()
        self.assertIn('aerial', str(caught.exception))


class Arm(BodyCase):
    family = 'manipulator'
    urdf = staticmethod(ms.arm_urdf)

    def test_the_manifest_takes_its_joints_and_limits_from_the_urdf(self):
        from neuxmind_spec.manifest import validate_manifest_v2
        body = self.body(ms.scene(objects=[], places=[]))
        capabilities = {c['id']: c.get('params', {}) for c in validate_manifest_v2(body.manifest())['capabilities']}
        arm = capabilities['arm.joint_position']
        self.assertEqual((arm['dof'], arm['joint_names'], arm['lower'], arm['upper'], arm['max_velocity']),
                         (3, ['joint1', 'joint2', 'joint3'], [-2.0] * 3, [2.0] * 3, 2.0))
        self.assertAlmostEqual(capabilities['gripper.position']['max_width'], 0.08)
        self.assertEqual(body.read('joint.state')['names'], ['joint1', 'joint2', 'joint3'])

    def test_an_arm_without_fingers_has_no_gripper(self):
        body = self.body(ms.scene(objects=[], places=[]), urdf=ms.arm_urdf(fingers=False))
        ids = [c['id'] for c in body.manifest()['capabilities']]
        self.assertNotIn('gripper.position', ids)
        with self.assertRaises(ValueError):
            body.read('gripper.state')

    def test_a_joint_move_arrives_and_the_gripper_closes_with_both_fingers(self):
        body = self.body(ms.scene(objects=[], places=[]))
        status = self.finish(body, body.start_action('arm.joint_position', {'position': [0.5, 0.8, -0.4]}))
        self.assertEqual((status.state, status.result['reached']), ('succeeded', True))
        for got, want in zip(body.read('joint.state')['position'], [0.5, 0.8, -0.4]):
            self.assertAlmostEqual(got, want, delta=0.03)
        self.assertGreater(body.read('gripper.state')['opening'], 0.9)
        status = self.finish(body, body.start_action('gripper.position', {'opening': 0.0}))
        self.assertEqual(status.state, 'succeeded')
        self.assertLess(body.read('gripper.state')['opening'], 0.1)
        fingers = body.world.joint_positions()[3:]
        self.assertAlmostEqual(fingers[0], fingers[1], delta=0.003)       # the second finger follows the first

    def test_a_move_into_a_fixed_object_fails_and_names_it(self):
        scene = ms.scene(objects=[{'name': '柱子', 'shape': 'box', 'size': [0.2, 0.6, 1.2], 'pos': [0.35, 0, 0.6]}], places=[])
        body = self.body(scene)
        status = self.finish(body, body.start_action('arm.joint_position', {'position': [0.0, 1.5, 0.0]}))
        self.assertEqual(status.state, 'failed')
        self.assertIn('柱子', status.error)
        self.assertLess(body.read('joint.state')['position'][1], 1.2)

    def test_an_action_started_while_a_stop_comes_in_does_not_run_on(self):
        import threading
        body = self.body(ms.scene(objects=[], places=[]))
        handles, done = [], threading.Event()

        def stopper():
            while not done.is_set():
                body.stop()
        thread = threading.Thread(target=stopper)
        thread.start()
        try:
            for index in range(400):
                handles.append(body.start_action('arm.joint_position', {'position': [0.1 * (index % 5), 0.0, 0.0]}))
                handles.append(body.start_action('gripper.position', {'opening': 0.5}))
        finally:
            done.set()
            thread.join()
        body.stop()
        self.run_for(body, 0.5)
        self.assertEqual([h.poll().state for h in handles if h.poll().state == 'running'], [])

    def test_a_joint_swinging_through_its_target_has_not_arrived(self):
        from brainos import mujoco_body
        self.assertLess(mujoco_body.JOINT_AT_REST, 0.2)
        body = self.body(ms.scene(objects=[], places=[]))
        status = self.finish(body, body.start_action('arm.joint_position', {'position': [1.0, 0.0, 0.0]}))
        self.assertEqual(status.state, 'succeeded')
        self.assertLess(max(abs(v) for v in body.read('joint.state')['velocity']), mujoco_body.JOINT_AT_REST)

    def test_a_stop_holds_the_arm_where_it_is(self):
        body = self.body(ms.scene(objects=[], places=[]))
        handle = body.start_action('arm.joint_position', {'position': [1.5, 0.0, 0.0], 'max_velocity': 0.5})
        self.run_for(body, 1.0)
        body.stop()
        self.assertEqual(handle.poll().state, 'cancelled')
        self.run_for(body, 0.5)
        held = body.read('joint.state')['position'][0]
        self.run_for(body, 2.0)
        self.assertAlmostEqual(body.read('joint.state')['position'][0], held, delta=0.02)
        self.assertLess(held, 1.0)


@unittest.skipUnless(HAVE_MUJOCO, 'needs the mujoco package')
class Picture(unittest.TestCase):
    def test_a_frame_is_a_png_of_the_asked_size(self):
        import numpy as np
        from brainos import mujoco_body
        frame = np.zeros((4, 6, 3), dtype=np.uint8)
        frame[1, 2] = [255, 128, 0]
        data = mujoco_body.png(frame)
        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(data[16:24], (6).to_bytes(4, 'big') + (4).to_bytes(4, 'big'))

    def test_the_settings_file_must_be_complete(self):
        from brainos import mujoco_body
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'mujoco.json'
            path.write_text(json.dumps({'family': 'quadruped'}))
            import os
            os.environ[mujoco_body.CONFIG_ENV] = str(path)
            try:
                with self.assertRaises(ValueError):
                    mujoco_body.load_config()
            finally:
                del os.environ[mujoco_body.CONFIG_ENV]


if __name__ == '__main__':
    unittest.main()
