"""Issue #42: an installed skill can be run at once, on a real robot too.

``robots/run-skill`` (:func:`brainos.sim_robots.run_skill`) used to refuse every robot that the console
had not started itself as a simulation. It now runs an installed skill on any connected robot. What a
real robot needs on top of the simulated one is decided here:

* **Two confirmations.** ``hardware_authorized`` (the skill may move the robot) as before, and
  ``onsite_confirmed``: somebody is next to the robot, the area is clear and the hardware emergency stop
  is within reach. Without it nothing is sent to the device. A page written for simulated robots
  therefore cannot start a real one by accident.
  Only the console's own simulated robots are exempt; a device that calls itself simulated is not.
* **A record.** The operation and the ``skill.run`` audit entry say whether the robot was simulated and
  that the on-site confirmation was given.

Everything else is the device's own protection and is not repeated here: the management layer refuses
motion while the emergency stop is latched, while a real body has no safety limits filled in, and for
values outside what the skill declares; it caps the speed and it stops the skill on ``/v1/stop``.
"""
from .errors import CodedError

ONSITE_REQUIRED = '这是一台真实的机器人：运行技能前，需要确认现场有人看着它、周围没有人和障碍物、急停按钮够得着'


def is_simulated(robot):
    """Only a robot the console started itself as a simulation.

    What a device says about itself (``simulated: true`` in its manifest) does not count here: a real
    body with a mislabelled or copied adapter must not be able to waive the on-site confirmation."""
    return bool(robot.get('sim'))


def require_confirmations(robot, p):
    """Raise unless ``p`` carries what this robot needs before a skill may run on it.

    Returns the facts to keep on the operation record."""
    simulated = is_simulated(robot)
    if not simulated and p.get('onsite_confirmed') is not True:
        raise CodedError(ONSITE_REQUIRED, 'onsite_confirmation_required')
    return {'simulated': simulated, **({} if simulated else {'onsite_confirmed': True})}
