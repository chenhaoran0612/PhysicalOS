"""Build the MuJoCo model of one simulation (issue #51, docs/MUJOCO_SIM.md).

Input: a prepared robot model directory (``mujoco_model.prepare``) and a validated scene
(``mujoco_scene.validate``). Output: a :class:`World` — the compiled model, its data and the handles the
simulated body needs (which actuator moves what, which geoms are the robot, which are named objects).

What the model is, and is not:

* The robot is the URDF's links, joints, inertias and shapes. Every movable joint gets a position
  actuator (gain from the joint's effort limit); a ``mimic`` joint follows its master.
* A manipulator's base is fixed where the scene puts the robot. Any other body stands on a carrier with
  four joints (x, y, height, heading) that velocity commands drive directly: **legs and wheels do not
  propel the robot, and no gait is simulated.** The robot's weight is compensated, so it does not sag
  or fall over.
* The robot collides with the scene's objects (it is stopped by fixed ones and pushes movable ones) but
  not with the floor and not with itself.

This module imports MuJoCo and numpy. It is only imported inside the simulation's own process
(``mujoco_body``) and the checking tool (``mujoco_tool``), never by the console's backend.
"""
import json
import math
from pathlib import Path

import mujoco
import numpy as np

TIMESTEP = 0.004
ROBOT = 'robot/'
BASE_JOINTS = ('base_x', 'base_y', 'base_z', 'base_yaw')
GRIPPER_WORDS = ('finger', 'gripper', 'claw', 'jaw', 'hand')
ROBOT_BITS, OBJECT_BITS, FLOOR_BITS = (1, 0), (2, 3), (2, 2)          # (contype, conaffinity)
FIXED_BASE = ('manipulator',)
FAMILIES = ('manipulator', 'quadruped', 'mobile', 'humanoid')


class BuildError(ValueError):
    """The model could not be built; the message is MuJoCo's own or says what is missing."""


def _rgba(color):
    return [int(color[i:i + 2], 16) / 255.0 for i in (1, 3, 5)] + [1.0]


def _object_geom(body, item, bits):
    half = [v / 2.0 for v in item['size']]
    shape = item['shape']
    if shape == 'box':
        kind, size = mujoco.mjtGeom.mjGEOM_BOX, half
    elif shape == 'sphere':
        kind, size = mujoco.mjtGeom.mjGEOM_SPHERE, [item['size'][0], 0, 0]
    else:
        kind = mujoco.mjtGeom.mjGEOM_CYLINDER if shape == 'cylinder' else mujoco.mjtGeom.mjGEOM_CAPSULE
        size = [item['size'][0], half[1], 0]
    geom = body.add_geom(type=kind, size=size, name='object/' + item['name'], rgba=_rgba(item['color']),
                         contype=bits[0], conaffinity=bits[1])
    geom.friction = [1.0, 0.01, 0.001]
    return geom


def _yaw_quat(yaw):
    return [math.cos(yaw / 2.0), 0.0, 0.0, math.sin(yaw / 2.0)]


def _add_scene(spec, scene):
    world = spec.worldbody
    fx, fy = scene['floor']['size']
    floor = world.add_geom(type=mujoco.mjtGeom.mjGEOM_PLANE, size=[fx, fy, 0.1], name='floor', rgba=[0.86, 0.87, 0.89, 1.0],
                           contype=FLOOR_BITS[0], conaffinity=FLOOR_BITS[1])
    try:                                    # looks only: a sky and a one-metre grid, so distances can be read off the picture
        spec.add_texture(name='sky', type=mujoco.mjtTexture.mjTEXTURE_SKYBOX, builtin=mujoco.mjtBuiltin.mjBUILTIN_GRADIENT,
                         rgb1=[0.93, 0.95, 0.96], rgb2=[0.70, 0.78, 0.82], width=128, height=768)
        spec.add_texture(name='grid', type=mujoco.mjtTexture.mjTEXTURE_2D, builtin=mujoco.mjtBuiltin.mjBUILTIN_CHECKER,
                         rgb1=[0.90, 0.91, 0.90], rgb2=[0.84, 0.86, 0.85], width=256, height=256)
        material = spec.add_material(name='grid', texrepeat=[fx, fy], reflectance=0.0)
        material.textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = 'grid'
        floor.material, floor.rgba = 'grid', [1.0, 1.0, 1.0, 1.0]
    except (AttributeError, TypeError, ValueError):
        pass                                # a MuJoCo without these: the plain floor stays
    sun = world.add_light(pos=[0, 0, 8], dir=[-0.3, 0.25, -1], diffuse=[0.55, 0.55, 0.55])
    try:                                    # an even daylight instead of a spot: the whole floor is lit
        sun.type = mujoco.mjtLightType.mjLIGHT_DIRECTIONAL
        spec.visual.headlight.ambient = [0.42, 0.42, 0.42]
        spec.visual.headlight.diffuse = [0.35, 0.35, 0.35]
    except (AttributeError, TypeError, ValueError):
        pass
    for item in scene['objects']:
        body = world.add_body(name='object/' + item['name'], pos=item['pos'], quat=_yaw_quat(item['yaw']))
        geom = _object_geom(body, item, OBJECT_BITS)
        if item['movable']:
            body.add_freejoint()
            geom.mass = item['mass']
    for place in scene['places']:
        world.add_site(name='place/' + place['name'], pos=[place['x'], place['y'], 0.005], size=[0.12, 0.12, 0.002],
                       type=mujoco.mjtGeom.mjGEOM_CYLINDER, rgba=[0.15, 0.55, 0.95, 0.6])


def _gain(effort):
    kp = 2.0 * max(10.0, min(float(effort) if effort else 50.0, 400.0))
    return kp, 0.1 * kp


class World:
    """A compiled simulation and what the body needs to drive and read it. Not thread-safe: the body
    calls it under its own lock."""

    def __init__(self, model, data, *, family, joints, places, start):
        self.model, self.data, self.family, self.places, self.start = model, data, family, places, start
        self.fixed_base = family in FIXED_BASE
        name = lambda kind, index: mujoco.mj_id2name(model, kind, index) or ''      # noqa: E731
        self._actuator = {name(mujoco.mjtObj.mjOBJ_ACTUATOR, i): i for i in range(model.nu)}
        self._qpos = {}
        self._dof = {}
        for index in range(model.njnt):
            self._qpos[name(mujoco.mjtObj.mjOBJ_JOINT, index)] = int(model.jnt_qposadr[index])
            self._dof[name(mujoco.mjtObj.mjOBJ_JOINT, index)] = int(model.jnt_dofadr[index])
        self.joints = joints                       # [{name, type, lower, upper, velocity, effort, gripper}] in URDF order
        self.robot_geoms = set()
        self.collides = False                      # whether the robot has any shape that can touch the scene
        self.object_of_geom = {}
        for index in range(model.ngeom):
            body = name(mujoco.mjtObj.mjOBJ_BODY, int(model.geom_bodyid[index]))
            geom = name(mujoco.mjtObj.mjOBJ_GEOM, index)
            if geom.startswith('object/'):
                self.object_of_geom[index] = geom[len('object/'):]
            elif body.startswith(ROBOT) or body == 'carrier':
                self.robot_geoms.add(index)
                self.collides = self.collides or bool(model.geom_contype[index])
        self.stand_height = 0.0
        self._owed = 0.0
        self.movable = set()

    # ------------------------------------------------------------ base
    def base_pose(self):
        """``(x, y, yaw, height)`` of the robot's base in the world."""
        if self.fixed_base:
            return self.start['x'], self.start['y'], self.start['yaw'], 0.0
        q = self.data.qpos
        return (float(q[self._qpos['base_x']]), float(q[self._qpos['base_y']]),
                float(q[self._qpos['base_yaw']]), float(q[self._qpos['base_z']]))

    def base_velocity(self):
        """World-frame ``(vx, vy, wz)`` actually achieved."""
        if self.fixed_base:
            return 0.0, 0.0, 0.0
        v = self.data.qvel
        return float(v[self._dof['base_x']]), float(v[self._dof['base_y']]), float(v[self._dof['base_yaw']])

    def command_base(self, vx_world, vy_world, wz, height):
        if self.fixed_base:
            return
        ctrl = self.data.ctrl
        ctrl[self._actuator['v_base_x']], ctrl[self._actuator['v_base_y']] = vx_world, vy_world
        ctrl[self._actuator['v_base_yaw']], ctrl[self._actuator['p_base_z']] = wz, height

    # ------------------------------------------------------------ joints
    def joint_positions(self):
        return [float(self.data.qpos[self._qpos[ROBOT + j['name']]]) for j in self.joints]

    def joint_velocities(self):
        return [float(self.data.qvel[self._dof[ROBOT + j['name']]]) for j in self.joints]

    def command_joint(self, index, target):
        actuator = self._actuator.get('p_' + self.joints[index]['name'])
        if actuator is not None:
            self.data.ctrl[actuator] = target

    # ------------------------------------------------------------ time
    def step(self, seconds):
        self._owed += seconds / TIMESTEP            # what does not fill a step is carried, so the world keeps the clock's time
        steps = int(self._owed + 1e-9)
        self._owed -= steps
        for _ in range(steps):
            mujoco.mj_step(self.model, self.data)
        if not np.isfinite(self.data.qpos).all():
            raise BuildError('仿真发散了（数值变成无穷大）；请检查 URDF 的质量和惯量，或环境里物体的大小')

    def touching(self):
        """Names of the scene objects the robot is in contact with right now."""
        found = []
        for contact in self.data.contact[:self.data.ncon]:
            a, b = int(contact.geom1), int(contact.geom2)
            for robot, other in ((a, b), (b, a)):
                if robot in self.robot_geoms and other in self.object_of_geom:
                    name = self.object_of_geom[other]
                    if name not in found:
                        found.append(name)
        return found

    def moving(self, names, floor=0.02):
        """Those of the named objects that are moving faster than ``floor`` m/s (being pushed, rolling)."""
        found = set()
        for index, name in self.object_of_geom.items():
            if name in names:
                velocity = self.data.cvel[int(self.model.geom_bodyid[index])][3:6]
                if float(np.linalg.norm(velocity)) > floor:
                    found.add(name)
        return found

    def object_poses(self):
        out = {}
        for index, name in self.object_of_geom.items():
            pos = self.data.geom_xpos[index]
            out[name] = [round(float(pos[0]), 4), round(float(pos[1]), 4), round(float(pos[2]), 4)]
        return out

    def summary(self):
        return {'bodies': int(self.model.nbody), 'joints': [j['name'] for j in self.joints], 'collides': self.collides,
                'gripper_joints': [j['name'] for j in self.joints if j['gripper']],
                'objects': sorted(self.object_of_geom.values()), 'places': [p['name'] for p in self.places],
                'stand_height': round(self.stand_height, 4), 'fixed_base': self.fixed_base,
                'extent': round(float(self.model.stat.extent), 3)}


def _is_gripper(joint, joints):
    name = joint['name'].lower()
    if any(word in name for word in GRIPPER_WORDS):
        return True
    master = (joint.get('mimic') or {}).get('joint', '').lower()
    return any(word in master for word in GRIPPER_WORDS)


def build(model_dir, scene, family, pose=None):
    """Compile the simulation of ``scene`` with the robot in ``model_dir``. Raises :class:`BuildError`.

    ``pose`` (issue #54): ``{joint name: angle}`` the robot starts and is held in, for a robot whose URDF zero is
    not how it stands (a dog with straight legs). Angles are kept inside the joint's own limits."""
    pose = pose if isinstance(pose, dict) else {}
    model_dir = Path(model_dir)
    if family not in FAMILIES:
        raise BuildError(f'这一版的仿真只支持这些本体类型：{"、".join(FAMILIES)}（这台是 {family}）')
    try:
        meta = json.loads((model_dir / 'model.json').read_text(encoding='utf-8'))
        robot = mujoco.MjSpec.from_file(str(model_dir / 'model.urdf'))
    except (OSError, ValueError) as exc:
        raise BuildError(f'机器人模型读不出来：{exc}') from None
    for geom in robot.geoms:
        if geom.contype or geom.conaffinity:
            geom.contype, geom.conaffinity = ROBOT_BITS
        if not geom.material and all(abs(float(c) - 0.5) < 1e-6 for c in geom.rgba[:3]):      # no colour in the URDF
            geom.rgba = [0.16, 0.36, 0.30, float(geom.rgba[3])]
    for body in robot.bodies:
        if body.name != 'world':
            body.gravcomp = 1.0
        # Issue #54: a link that has shapes for looks shows those, not its collision shapes as well. A link with
        # collision shapes only keeps showing them.
        own = list(body.geoms)
        if any(not (geom.contype or geom.conaffinity) for geom in own):
            for geom in own:
                if geom.contype or geom.conaffinity:
                    geom.group = 3
    spec = mujoco.MjSpec()
    spec.option.timestep = TIMESTEP
    spec.option.integrator = mujoco.mjtIntegrator.mjINT_IMPLICITFAST
    spec.compiler.balanceinertia = True
    spec.compiler.boundmass, spec.compiler.boundinertia = 0.001, 1e-6
    _add_scene(spec, scene)
    start = scene['robot']
    fixed = family in FIXED_BASE
    try:
        if fixed:
            frame = spec.worldbody.add_frame(pos=[start['x'], start['y'], 0.0], quat=_yaw_quat(start['yaw']))
        else:
            carrier = spec.worldbody.add_body(name='carrier')
            hinge, slide = mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE
            for name, kind, axis in (('base_x', slide, [1, 0, 0]), ('base_y', slide, [0, 1, 0]),
                                     ('base_z', slide, [0, 0, 1]), ('base_yaw', hinge, [0, 0, 1])):
                carrier.add_joint(name=name, type=kind, axis=axis)
            carrier.add_geom(type=mujoco.mjtGeom.mjGEOM_SPHERE, size=[0.01, 0, 0], mass=1.0, contype=0, conaffinity=0,
                             rgba=[0, 0, 0, 0])
            carrier.gravcomp = 1.0
            frame = carrier.add_frame()
        spec.attach(robot, frame=frame, prefix=ROBOT)
        joints, by_name = [], {j['name']: j for j in meta.get('joints', [])}
        joint_type = mujoco.mjtTrn.mjTRN_JOINT
        for joint in meta.get('joints', []):
            item = {key: joint.get(key) for key in ('name', 'type', 'lower', 'upper', 'velocity', 'effort')}
            item['gripper'] = _is_gripper(joint, by_name)
            mimic = joint.get('mimic')
            if mimic and mimic['joint'] in by_name and mimic['joint'] != joint['name']:
                item['mimic'] = mimic['joint']      # MuJoCo's URDF reader made the constraint; here it only gets no actuator
            else:
                actuator = spec.add_actuator(name='p_' + joint['name'], target=ROBOT + joint['name'], trntype=joint_type)
                kp, kv = _gain(joint.get('effort'))
                actuator.set_to_position(kp=kp, kv=kv)
                if joint.get('effort'):
                    actuator.forcelimited = True
                    actuator.forcerange = [-joint['effort'] * 4.0, joint['effort'] * 4.0]
            joints.append(item)
        if not fixed:
            for name in ('base_x', 'base_y', 'base_yaw'):
                spec.add_actuator(name='v_' + name, target=name, trntype=joint_type).set_to_velocity(kv=800.0)
            spec.add_actuator(name='p_base_z', target='base_z', trntype=joint_type).set_to_position(kp=4000.0, kv=600.0)
        model = spec.compile()
    except (ValueError, RuntimeError, TypeError, KeyError) as exc:
        raise BuildError(f'MuJoCo 没能建立这个模型：{str(exc)[:600]}') from None
    data = mujoco.MjData(model)
    world = World(model, data, family=family, joints=joints, places=scene['places'], start=start)
    for index, joint in enumerate(joints):
        lower, upper = joint.get('lower'), joint.get('upper')
        wanted = pose.get(joint['name'])
        rest = float(wanted) if isinstance(wanted, (int, float)) and not isinstance(wanted, bool) and math.isfinite(wanted) else 0.0
        if joint['type'] != 'continuous' and lower is not None and upper is not None and lower < upper:
            rest = min(max(rest, lower), upper)
        data.qpos[world._qpos[ROBOT + joint['name']]] = rest
        world.command_joint(index, rest)
    if not fixed:
        lift = 5.0
        data.qpos[world._qpos['base_z']] = lift
        mujoco.mj_forward(model, data)
        floor = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, 'floor')
        fromto = np.zeros(6)
        carrier = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, 'carrier')
        shapes = [g for g in world.robot_geoms if int(model.geom_bodyid[g]) != carrier]
        solid = [g for g in shapes if model.geom_contype[g]] or shapes      # a model with looks only still stands on them
        lowest = min((mujoco.mj_geomDistance(model, data, geom, floor, 100.0, fromto) for geom in solid), default=lift)
        world.stand_height = max(0.0, lift - lowest + 0.003)
        data.qpos[world._qpos['base_x']], data.qpos[world._qpos['base_y']] = start['x'], start['y']
        data.qpos[world._qpos['base_yaw']], data.qpos[world._qpos['base_z']] = start['yaw'], world.stand_height
        world.command_base(0.0, 0.0, 0.0, world.stand_height)
    mujoco.mj_forward(model, data)
    return world


def settle(world, seconds=1.0):
    """Run the world for a moment; the error if it is unstable. Used before a scene is offered for confirmation."""
    world.step(seconds)
    return world.summary()
