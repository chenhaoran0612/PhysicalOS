"""The robot of an MJCF model, for a simulation (issue #77, docs/MUJOCO_SIM.md).

``mujoco_build.build`` reads a URDF robot with MuJoCo's URDF reader. An MJCF robot (``model.xml`` as
``mujoco_mjcf.prepare`` wrote it) is read here and made into the same kind of thing, as the user chose
("和 URDF 一样用"):

* the robot is the body at the top of the world with the most hinge and slide joints below it; everything
  else the file puts into the world (floor, lights, cameras, sites, other bodies) is removed — the scene is
  the simulation's own;
* the robot's free joint is removed: the simulation carries the robot (``mujoco_build``);
* the file's own actuators, sensors, keyframes, contact pairs and exclusions, tendons and equality
  constraints are removed, except an equality that couples one joint to another (that joint then follows,
  like a URDF ``mimic``). Every other hinge and slide joint gets the simulation's position actuator;
* the limits are MuJoCo's own reading (default classes applied, degrees turned into radians); a joint's
  effort is its ``actuatorfrcrange``, else what its own actuators could apply (their force range, or for a
  plain motor its control range times its gain and gear). MJCF states no velocity limit.

Imports MuJoCo: only the simulation's own process and the checking tool use it.
"""
import mujoco

MOVABLE = (mujoco.mjtJoint.mjJNT_HINGE, mujoco.mjtJoint.mjJNT_SLIDE)
LIMITED, AUTO = mujoco.mjtLimited.mjLIMITED_TRUE, mujoco.mjtLimited.mjLIMITED_AUTO


def _movable(body):
    return sum(1 for joint in body.joints if joint.type in MOVABLE) + sum(_movable(child) for child in body.bodies)


def _limited(flag, span):
    return flag == LIMITED or (flag == AUTO and float(span[0]) < float(span[1]))


def _efforts(spec):
    """{joint name: the largest effort the file's own actuators could apply to it}."""
    found = {}
    for actuator in spec.actuators:
        if actuator.trntype != mujoco.mjtTrn.mjTRN_JOINT or not actuator.target:
            continue
        force, control, value = list(actuator.forcerange), list(actuator.ctrlrange), None
        if _limited(actuator.forcelimited, force):
            value = max(abs(force[0]), abs(force[1]))
        elif (actuator.biastype == mujoco.mjtBias.mjBIAS_NONE and actuator.gaintype == mujoco.mjtGain.mjGAIN_FIXED
              and _limited(actuator.ctrllimited, control)):
            value = max(abs(control[0]), abs(control[1])) * abs(float(actuator.gainprm[0])) * abs(float(actuator.gear[0]))
        if value:
            found[actuator.target] = max(found.get(actuator.target, 0.0), float(value))
    return found


def _keep_robot(spec, notes):
    """Remove from the world all but the robot; returns the robot's body."""
    world = spec.worldbody
    tops = list(world.bodies)
    if not tops:
        raise ValueError('MJCF 里没有刚体（body），没有机器人')
    counts = [_movable(body) for body in tops]
    keep = counts.index(max(counts))
    if sum(1 for count in counts if count) > 1:
        notes.append(f'文件里有 {sum(1 for c in counts if c)} 个带关节的刚体树，仿真只用关节最多的「{tops[keep].name or "第一个"}」')
    for index, body in enumerate(tops):
        if index != keep:
            spec.delete(body)
    for items in (world.geoms, world.sites, world.cameras, world.lights):
        for item in list(items):
            spec.delete(item)
    robot = list(world.bodies)[0]
    for joint in list(robot.joints):
        if joint.type == mujoco.mjtJoint.mjJNT_FREE:
            spec.delete(joint)
    return robot


def same_options(robot, spec):
    """Give the robot the simulation's own ``<option>`` values. An MJCF file may set its own (time step, solver
    iterations); MuJoCo keeps the simulation's anyway when attaching, but warns and writes MUJOCO_LOG.TXT into the
    working directory for every difference."""
    for name in dir(spec.option):
        if not name.startswith('_') and not callable(getattr(spec.option, name)):
            setattr(robot.option, name, getattr(spec.option, name))


def load(path):
    """``(spec, joints, notes)``: the MJCF robot at ``path`` ready to be attached, and its movable joints
    ``[{name, type, lower, upper, velocity, effort, mimic?}]`` in the file's order. Raises ``ValueError``
    or ``RuntimeError`` (MuJoCo's own words) when it cannot be read."""
    spec, notes = mujoco.MjSpec.from_file(str(path)), []
    _keep_robot(spec, notes)
    for index, body in enumerate(spec.bodies):
        if body.name != 'world' and not body.name:
            body.name = f'unnamed_body_{index}'          # the simulation tells the robot's shapes by their bodies' names
    efforts = _efforts(spec)
    kept = {joint.name for joint in spec.joints}
    follows = {}
    for equality in list(spec.equalities):
        if equality.type == mujoco.mjtEq.mjEQ_JOINT and equality.name1 in kept and (not equality.name2 or equality.name2 in kept):
            if equality.name2:
                follows[equality.name1] = equality.name2
            continue
        spec.delete(equality)
    for items in (spec.actuators, spec.sensors, spec.keys, spec.pairs, spec.excludes, spec.tendons):
        for item in list(items):
            spec.delete(item)
    # The bounds a URDF gets from mujoco_model.COMPILER: a body without shapes (a frame of the model) still compiles.
    # They belong to the robot's own spec: an attached spec keeps its compiler settings.
    spec.compiler.balanceinertia = True
    spec.compiler.boundmass = max(float(spec.compiler.boundmass), 0.001)
    spec.compiler.boundinertia = max(float(spec.compiler.boundinertia), 1e-6)
    model = spec.copy().compile()
    joints = []
    for index in range(model.njnt):
        kind = int(model.jnt_type[index])                # a number here, an enum in the spec
        if kind not in {int(k) for k in MOVABLE}:
            continue
        name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_JOINT, index) or ''
        hinge, limited = kind == int(mujoco.mjtJoint.mjJNT_HINGE), bool(model.jnt_limited[index])
        item = {'name': name, 'type': ('revolute' if limited else 'continuous') if hinge else 'prismatic',
                'lower': float(model.jnt_range[index][0]) if limited else None,
                'upper': float(model.jnt_range[index][1]) if limited else None, 'velocity': None, 'effort': None}
        span = model.jnt_actfrcrange[index]
        if model.jnt_actfrclimited[index] and float(span[0]) < float(span[1]):
            item['effort'] = max(abs(float(span[0])), abs(float(span[1])))
        elif efforts.get(name):
            item['effort'] = efforts[name]
        if name in follows:
            item['mimic'] = {'joint': follows[name]}
        joints.append(item)
    return spec, joints, notes
