"""A simulated robot body backed by MuJoCo, built from a robot model in the library (issue #51).

The edge runtime loads it like any other body (``serve --adapter brainos.mujoco_body:body``), so the
capability table, skills, applications, e-stop latch and limits are the normal, unmodified path. The
body reports the brand and model of the robot it was built from and ``simulated: true``: applications
of that brand and model run on it, and what they produce here is never on-site evidence.

Two bodies, chosen by the robot's body type (see ``mujoco_build`` for what the model is):

* :class:`MujocoBase` (quadruped, mobile, humanoid): the capabilities of the reference ``SimQuadruped``
  — velocity, navigation, named places, exploring, postures — with the pose taken from physics. The
  navigator decides a velocity exactly as before; the simulation applies it to the base, and what the
  robot really did (it may be held back by an obstacle) is what odometry reports. A goal that an
  obstacle blocks fails and names the obstacle.
* :class:`MujocoArm` (manipulator): joint-space moves and the gripper of the reference ``SimArm``, with
  the URDF's own joints and limits. A move that does not arrive (blocked) fails; a gripper that is held
  open by a scene object reports ``grasped``.

Its settings come from ``mujoco.json`` in the working directory (written by ``mujoco_sim`` before the
process starts), or the file named by ``NEUXMIND_MUJOCO_CONFIG``.

The picture: a background thread renders the scene off-screen about twice a second while somebody is
watching (the console touches ``<view>/watch``) and replaces ``<view>/frame.png``; ``<view>/state.json``
holds what the robot touches and where the objects are. Nothing is sent anywhere.
"""
import json
import math
import os
import struct
import threading
import time
import zlib
from pathlib import Path

from neuxmind_runtime.adapters._sim import (CANCELLED, SimAdapterBase, SimGoal, SimHandle, failed_handle, wrap_angle)
from neuxmind_runtime.adapters.sim_quadruped import MAX_VX, NAV_SPEED, SimQuadruped
from neuxmind_runtime.clock import RealClock
from neuxmind_runtime.hal import FAILED

from . import mujoco_build

CONFIG_NAME = 'mujoco.json'
CONFIG_ENV = 'NEUXMIND_MUJOCO_CONFIG'
ADAPTER = 'brainos.mujoco_body:body'
ADAPTER_VERSION = '0.1.0'
MAX_CATCH_UP_S = 1.0          # with a real clock: after a pause (sleep, debugger) the simulation does not replay it
BLOCKED_S = 1.5
POSTURE_HEIGHT = {'stand': 1.0, 'sit': 0.65, 'lie': 0.35}     # share of the standing height
SETTLE_S = 2.0
JOINT_ARRIVED = 0.03
JOINT_AT_REST = 0.1            # rad/s (m/s): a joint swinging through its target has not arrived
FRAME_PERIOD_S = 0.4
WATCH_FRESH_S = 6.0
FRAME_SIZE = (360, 480)       # height, width
TICK_S = 0.02


def load_config():
    path = Path(os.environ.get(CONFIG_ENV) or CONFIG_NAME)
    config = json.loads(path.read_text(encoding='utf-8'))
    for key in ('model_dir', 'scene', 'family', 'vendor', 'model', 'instance_id'):
        if key not in config:
            raise ValueError(f'仿真配置缺少 {key}')
    return config


def png(rgb):
    """An H×W×3 uint8 array as PNG bytes (no image library in the app)."""
    import numpy as np
    height, width = rgb.shape[0], rgb.shape[1]
    rows = np.concatenate([np.zeros((height, 1), dtype=np.uint8), rgb.reshape(height, width * 3)], axis=1).tobytes()

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 3)) + chunk(b'IEND', b''))


class _Physics:
    """What both bodies share: the world, catching up with a real clock, the fault state, the viewer."""

    def _init_physics(self, config):
        self.config = config
        self.world = mujoco_build.build(config['model_dir'], config['scene'], config['family'])
        self.fault = ''
        self.touch = []
        self._viewer = None

    def _advance_locked(self):
        if isinstance(self.clock, RealClock):
            now, most = self.clock.now(), MAX_CATCH_UP_S * max(1.0, float(getattr(self.clock, 'time_scale', 1.0)))
            if now - self._last > most:
                self._last = now - most
        super()._advance_locked()

    def _physics_step(self, dt):
        """Advance the world; a diverged simulation stops for good and says so in ``system.health``."""
        if self.fault:
            return False
        try:
            self.world.step(dt)
        except mujoco_build.BuildError as exc:
            self.fault = str(exc)
            return False
        self.touch = self.world.touching()
        return True

    def _health(self, mode):
        if self.fault:
            return {'ok': False, 'mode': mode, 'errors': {'simulation': self.fault}}
        return {'ok': True, 'mode': mode, 'errors': {}}

    def status(self):
        return {'simulated': True, 'engine': 'mujoco', 'sim_time_s': round(self._t, 3), 'touching': list(self.touch),
                'time_scale': getattr(self.clock, 'time_scale', 1.0), **({'fault': self.fault} if self.fault else {})}

    def start_background(self):
        """Keep time moving and the picture fresh. Only for a body that runs on a real clock."""
        def tick():
            while not self._closed.is_set():
                try:
                    self.advance()
                except Exception:           # the fault is on record; reads keep answering
                    pass
                self._closed.wait(TICK_S)
        self._closed = threading.Event()
        threading.Thread(target=tick, name='nx-mujoco-tick', daemon=True).start()
        view = self.config.get('view_dir')
        if view:
            self._viewer = Viewer(self, Path(view), self._closed)
            self._viewer.start()

    def close(self):
        closed = getattr(self, '_closed', None)
        if closed is not None:
            closed.set()

    def snapshot(self):
        """Under the lock: what the viewer needs to draw this moment."""
        with self._lock:
            return (self.world.data.qpos.copy(), self.world.base_pose(), list(self.touch), self.world.object_poses(),
                    round(self._t, 2))


class MujocoBase(_Physics, SimQuadruped):
    def __init__(self, config, time_scale=1.0, clock=None):
        SimQuadruped.__init__(self, time_scale, clock, instance_id=config['instance_id'])
        self._init_physics(config)
        self.x, self.y, self.yaw, _ = self.world.base_pose()
        self.places = {p['name']: (p['x'], p['y']) for p in config['scene']['places']}
        self.legged = config['family'] in ('quadruped', 'humanoid')
        self.blocked_for = 0.0
        self.blocked_by = ''
        base = SimQuadruped.manifest(self)
        skip = set() if self.legged else {'legged.posture'}
        if not self.places:
            skip.add('base.navigate_to_place')
        capabilities = []
        for capability in base['capabilities']:
            if capability['id'] in skip:
                continue
            if capability['id'] == 'base.navigate_to_place':
                capability = {**capability, 'params': {'places': list(self.places)}}
            elif capability['id'] == 'base.navigate_to':
                capability = {**capability, 'params': {'planner': 'mujoco-straight-line'}}
            capabilities.append(capability)
        fx, fy = config['scene']['floor']['size']
        self._manifest = {**base, 'instance_id': config['instance_id'], 'vendor': config['vendor'], 'model': config['model'],
                          'family': config['family'], 'adapter_version': ADAPTER_VERSION,
                          'name': config.get('name') or f'{config["vendor"]} {config["model"]}（MuJoCo 仿真）',
                          'capabilities': capabilities,
                          'safety': {**base['safety'], 'workspace': {'x': [-fx, fx], 'y': [-fy, fy]}}}

    def manifest(self):
        return json.loads(json.dumps(self._manifest))

    def _step(self, dt):
        px, py, pyaw, travelled = self.x, self.y, self.yaw, self.distance
        SimQuadruped._step(self, dt)               # decides the velocity, from where the robot really is
        vx, vy, wz = self.velocity
        cos, sin = math.cos(pyaw), math.sin(pyaw)
        height = self.world.stand_height * POSTURE_HEIGHT.get(self.posture, 1.0)
        self.world.command_base(vx * cos - vy * sin, vx * sin + vy * cos, wz, height)
        self.x, self.y, self.yaw, self.distance = px, py, pyaw, travelled
        if not self._physics_step(dt):
            self.velocity = (0.0, 0.0, 0.0)
            return
        self.x, self.y, yaw, _height = self.world.base_pose()
        self.yaw = wrap_angle(yaw)
        moved = math.hypot(self.x - px, self.y - py)
        self.distance = travelled + moved
        wvx, wvy, wwz = self.world.base_velocity()
        cos, sin = math.cos(self.yaw), math.sin(self.yaw)
        self.velocity = (wvx * cos + wvy * sin, -wvx * sin + wvy * cos, wwz)
        wanted, turn = math.hypot(vx, vy) * dt, abs(wz) * dt
        turned = abs(wrap_angle(self.yaw - pyaw))
        held = (wanted > 1e-5 and moved < 0.2 * wanted) or (wanted <= 1e-5 and turn > 1e-5 and turned < 0.2 * turn)
        # Something that gives way (a box being pushed, however slowly) is not what holds the robot.
        if held and self.touch and not self.world.moving(self.touch):
            self.blocked_for += dt
            self.blocked_by = self.touch[0]
        else:
            self.blocked_for = 0.0
        goal = self.goal
        if self.blocked_for >= BLOCKED_S and goal is not None and goal.state == 'running':
            goal.abort(FAILED, f'被“{self.blocked_by}”挡住，没有到达')
            self.blocked_for = 0.0

    def read(self, cap_id):
        if cap_id == 'system.health':
            self.advance()
            goal = self.goal
            doing = '' if goal is None else ':exploring' if goal.kind == 'base.explore' else ':navigating'
            return self._health('sim:' + self.posture + doing)
        return SimQuadruped.read(self, cap_id)

    def start_action(self, cap_id, args):
        if cap_id != 'base.navigate_to_place':
            return SimQuadruped.start_action(self, cap_id, args)
        if args['place'] not in self.places:
            raise ValueError(f'这个仿真环境里没有地点 {args["place"]}；有 {list(self.places)}')
        gx, gy = self.places[args['place']]
        with self._lock:
            self._advance_locked()
            if self.posture != 'stand':
                return failed_handle(self, cap_id, '需要站立姿态才能导航')
            if self.goal is not None:
                self.goal.abort(CANCELLED, 'preempted by new goal')
            goal = SimGoal(cap_id, (gx, gy), tolerance=0.10, place=args['place'],
                           max_speed=min(NAV_SPEED, float(args.get('max_speed', NAV_SPEED))),
                           start_dist=math.hypot(gx - self.x, gy - self.y))
            self.cmd = (0.0, 0.0, 0.0)
            self.goal = goal
        return SimHandle(self, goal)

    def status(self):
        return _Physics.status(self)


class MujocoArm(_Physics, SimAdapterBase):
    def __init__(self, config, time_scale=1.0, clock=None):
        SimAdapterBase.__init__(self, time_scale, clock)
        self._init_physics(config)
        joints = self.world.joints
        self.arm = [i for i, j in enumerate(joints) if not j['gripper'] and not j.get('mimic')]
        self.fingers = [i for i, j in enumerate(joints) if j['gripper'] and not j.get('mimic')]
        if not self.arm:
            raise ValueError('这个机器人模型里没有可以驱动的手臂关节')
        self.names = [joints[i]['name'] for i in self.arm]
        self.lower = [self._bound(joints[i], 'lower') for i in self.arm]
        self.upper = [self._bound(joints[i], 'upper') for i in self.arm]
        speeds = [joints[i]['velocity'] for i in self.arm if joints[i].get('velocity')]
        self.max_velocity = max(0.05, min(speeds)) if speeds else 1.0
        positions = self.world.joint_positions()
        self.q_cmd = [positions[i] for i in self.arm]
        self.opening_cmd = 1.0
        self.grasped = False
        self.arm_goal = None
        self.grip_goal = None
        self._hold = False
        self._stops = 0                 # counts stop(): an action that was being started while one came in is not left running
        self._command_fingers()
        dof = len(self.arm)
        capabilities = [
            {'id': 'safety.stop', 'params': {'hardware_estop': False}},
            {'id': 'system.health'},
            {'id': 'joint.state', 'params': {'dof': dof}},
            {'id': 'arm.joint_position', 'params': {'dof': dof, 'lower': self.lower, 'upper': self.upper,
                                                    'max_velocity': self.max_velocity, 'joint_names': list(self.names)}}]
        if self.fingers:
            width = sum(abs((joints[i]['upper'] or 0.0) - (joints[i]['lower'] or 0.0)) for i in self._finger_joints())
            force = max((joints[i].get('effort') or 20.0) for i in self.fingers)
            capabilities += [{'id': 'gripper.position', 'params': {'max_width': round(max(width, 0.001), 4), 'max_force': force}},
                             {'id': 'gripper.state'}]
        self._manifest = {
            'schema': 'robot.adapter.v2', 'spec_version': '1.1', 'instance_id': config['instance_id'],
            'name': config.get('name') or f'{config["vendor"]} {config["model"]}（MuJoCo 仿真）',
            'vendor': config['vendor'], 'model': config['model'], 'family': config['family'],
            'adapter_version': ADAPTER_VERSION, 'simulated': True, 'capabilities': capabilities,
            'model_runtimes': [], 'safety': {'hardware_estop': False}}

    @staticmethod
    def _bound(joint, side):
        value = joint.get(side)
        if joint['type'] == 'continuous' or value is None:
            return -2.0 * math.pi if side == 'lower' else 2.0 * math.pi
        return float(value)

    def _finger_joints(self):
        return [i for i, j in enumerate(self.world.joints) if j['gripper']]

    def manifest(self):
        return json.loads(json.dumps(self._manifest))

    # ------------------------------------------------------------ gripper
    def _finger_target(self, index, opening):
        joint = self.world.joints[index]
        lower, upper = joint.get('lower') or 0.0, joint.get('upper') or 0.0
        return lower + opening * (upper - lower)

    def _command_fingers(self):
        for index in self.fingers:
            self.world.command_joint(index, self._finger_target(index, self.opening_cmd))

    def _opening(self):
        if not self.fingers:
            return 0.0
        joint, position = self.world.joints[self.fingers[0]], self.world.joint_positions()[self.fingers[0]]
        lower, upper = joint.get('lower') or 0.0, joint.get('upper') or 0.0
        return 0.0 if upper <= lower else max(0.0, min(1.0, (position - lower) / (upper - lower)))

    # ------------------------------------------------------------ physics
    def _step(self, dt):
        positions = self.world.joint_positions()
        if self._hold:                               # a stop: stay where the arm is now
            self.q_cmd = [positions[i] for i in self.arm]
            self.opening_cmd = self._opening() if self.fingers else self.opening_cmd
            self._hold = False
        goal = self.arm_goal
        if goal is not None and goal.state != 'running':
            self.arm_goal = goal = None             # ended (reached, failed, stopped): cleared here, under the lock
        if self.grip_goal is not None and self.grip_goal.state != 'running':
            self.grip_goal = None
        if goal is not None and goal.state == 'running':
            target, speed = goal.target, goal.extra['speed']
            remaining = max(abs(t - q) for t, q in zip(target, self.q_cmd))
            if remaining > 1e-6:
                fraction = min(1.0, speed * dt / remaining)
                self.q_cmd = [q + (t - q) * fraction for t, q in zip(target, self.q_cmd)]
                start = goal.extra['start_remaining']
                goal.progress = 0.95 if start <= 0 else min(0.95, 1.0 - remaining / start)
            else:
                self.q_cmd = list(target)
                off = [(abs(t - positions[i]), name) for t, i, name in zip(target, self.arm, self.names)]
                worst, name = max(off)
                goal.extra['settle'] = goal.extra.get('settle', 0.0) + dt
                velocities = self.world.joint_velocities()
                if worst <= JOINT_ARRIVED and max(abs(velocities[i]) for i in self.arm) <= JOINT_AT_REST:
                    goal.finish({'reached': True, 'position': [positions[i] for i in self.arm]})
                elif goal.extra['settle'] >= SETTLE_S:
                    by = f'，碰到了“{self.touch[0]}”' if self.touch else ''
                    goal.abort(FAILED, f'没有到达目标位置：关节 {name} 还差 {worst:.2f}{by}')
                    self.q_cmd = [positions[i] for i in self.arm]
        grip = self.grip_goal
        if grip is not None and grip.state == 'running':
            delta = grip.target - self.opening_cmd
            step = 1.0 * dt
            if abs(delta) > step:
                self.opening_cmd += step if delta > 0 else -step
            else:
                self.opening_cmd = grip.target
                grip.extra['settle'] = grip.extra.get('settle', 0.0) + dt
                actual = self._opening()
                if abs(actual - grip.target) <= 0.05 or grip.extra['settle'] >= SETTLE_S:
                    self.grasped = bool(self.touch) and actual - grip.target > 0.05
                    grip.finish({'opening': actual})
        for q, index in zip(self.q_cmd, self.arm):
            self.world.command_joint(index, q)
        self._command_fingers()
        self._physics_step(dt)

    # ------------------------------------------------------------ adapter API
    def read(self, cap_id):
        self.advance()
        if cap_id == 'system.health':
            moving = any(g is not None and g.state == 'running' for g in (self.arm_goal, self.grip_goal))
            return self._health('sim:moving' if moving else 'sim:idle')
        with self._lock:
            if cap_id == 'joint.state':
                positions, velocities = self.world.joint_positions(), self.world.joint_velocities()
                return {'names': list(self.names), 'position': [positions[i] for i in self.arm],
                        'velocity': [velocities[i] for i in self.arm]}
            if cap_id == 'gripper.state' and self.fingers:
                return {'opening': self._opening(), 'grasped': self.grasped}
        raise ValueError(f'这个仿真机械臂不能读取 {cap_id}')

    def invoke(self, cap_id, args):
        if cap_id == 'safety.stop':
            self.stop()
            return {'stopped': True}
        raise ValueError(f'这个仿真机械臂不支持命令 {cap_id}')

    def start_action(self, cap_id, args):
        stops = self._stops
        with self._lock:
            self._advance_locked()
            goal = None
            if cap_id == 'arm.joint_position':
                if self.arm_goal is not None:
                    self.arm_goal.abort(CANCELLED, 'preempted by new goal')
                target = [float(v) for v in args['position']]
                speed = min(float(args.get('max_velocity', self.max_velocity)), self.max_velocity)
                remaining = max(abs(t - q) for t, q in zip(target, self.q_cmd))
                goal = self.arm_goal = SimGoal(cap_id, target, speed=speed, start_remaining=remaining)
            elif cap_id == 'gripper.position' and self.fingers:
                if self.grip_goal is not None:
                    self.grip_goal.abort(CANCELLED, 'preempted by new goal')
                self.grasped = False
                goal = self.grip_goal = SimGoal(cap_id, max(0.0, min(1.0, float(args['opening']))))
            if goal is not None:
                if self._stops != stops:            # a stop arrived while this was being set up: it covers this action too
                    goal.abort(CANCELLED, 'stopped')
                return SimHandle(self, goal)
        raise ValueError(f'这个仿真机械臂不支持动作 {cap_id}')

    def stop(self):
        self._stops += 1
        for goal in (self.arm_goal, self.grip_goal):
            if goal is not None:
                goal.abort(CANCELLED, 'stopped')
        self._hold = True
        self._try_advance()


class Viewer(threading.Thread):
    """Draws the scene off-screen while somebody is watching. Owns its OpenGL context (one thread)."""

    def __init__(self, body, view_dir, closed):
        super().__init__(name='nx-mujoco-view', daemon=True)
        self.body, self.dir, self.closed = body, view_dir, closed
        self.error = ''

    def _watched(self):
        try:
            return time.time() - (self.dir / 'watch').stat().st_mtime < WATCH_FRESH_S
        except OSError:
            return False

    def _write(self, name, data):
        part = self.dir / (name + '.part')
        part.write_bytes(data)
        os.replace(part, self.dir / name)

    def run(self):
        import mujoco
        self.dir.mkdir(parents=True, exist_ok=True)
        world = self.body.world
        renderer = camera = data = None
        first = True
        while not self.closed.is_set():
            if first or self._watched():
                qpos, base, touching, objects, at = self.body.snapshot()
                state = {'t': at, 'touching': touching, 'objects': objects, 'fault': self.body.fault,
                         'pose': [round(v, 4) for v in base], 'render_error': self.error}
                try:
                    if renderer is None and not self.error:
                        renderer = mujoco.Renderer(world.model, FRAME_SIZE[0], FRAME_SIZE[1])
                        data = mujoco.MjData(world.model)
                        camera = mujoco.MjvCamera()
                        camera.type = mujoco.mjtCamera.mjCAMERA_FREE
                        camera.azimuth, camera.elevation = 135.0, -28.0
                        reach = 1.2 if world.fixed_base else max(world.stand_height * 2.0, 0.6)
                        camera.distance = max(2.2, 4.0 * reach)
                    if renderer is not None:
                        data.qpos[:] = qpos
                        mujoco.mj_forward(world.model, data)
                        camera.lookat[:] = [base[0], base[1], 0.4 if world.fixed_base else base[3]]
                        renderer.update_scene(data, camera)
                        self._write('frame.png', png(renderer.render()))
                except Exception as exc:        # no OpenGL here: the state is still worth showing
                    self.error = state['render_error'] = f'画面渲染不可用：{str(exc)[:200]}'
                    renderer = None
                self._write('state.json', json.dumps(state, ensure_ascii=False).encode())
                first = False
            self.closed.wait(FRAME_PERIOD_S)


def body(time_scale=1.0):
    """What ``--adapter brainos.mujoco_body:body`` loads."""
    config = load_config()
    cls = MujocoArm if config['family'] in mujoco_build.FIXED_BASE else MujocoBase
    made = cls(config, time_scale)
    made.start_background()
    return made
