"""The robot a task is done with (issue #42, third part).

Robots belong to the account (:mod:`brainos.robot_scope`). The owner's next rule: when a task is
created, a person chooses which robot (具身设备) it is done with. The task keeps that robot's id as
``robot_id``. Several tasks can name the same robot. The choice can be left open ("之后再选") and
changed later; a task without a choice works with every robot of the account, as before.

The choice does not limit what the task may do: an application of the task can still target another
robot of the account. It says which robot the task's pages show and start from.

Nothing is sent to a robot here. Deleting a robot does not touch the tasks that named it: their
pages say that the robot is gone and offer to choose another.
"""
from . import robot_scope

FIELD = 'robot_id'
NOT_A_ROBOT = '找不到所选的机器人，它可能已经被删除；请重新选择'
NOT_A_TASK = '这个任务已经不在了，可能已经被删除；请刷新页面'


def robot_of(platform, value):
    """The id of the robot ``value`` names; ``None`` for no choice. Raises when it is not a robot of the account."""
    if value in (None, ''):
        return None
    try:
        robot = platform.store.get('robots', value) if isinstance(value, str) else None
    except ValueError:
        robot = None
    if not robot:
        raise ValueError(NOT_A_ROBOT)
    return robot['id']


def keep(platform, task, p):
    """Put the robot chosen in the request ``p`` on the new ``task`` (before it is saved)."""
    chosen = robot_of(platform, p.get(FIELD))
    if chosen:
        task[FIELD] = chosen
    return task


def choose(platform, p):
    """``projects/robot``: choose, change or clear (no ``robot_id``) the robot of the task ``project_id``."""
    key = p.get('project_id')
    if robot_scope.is_home(key):
        raise ValueError(NOT_A_TASK)
    with platform.store.atomic():
        try:
            task = platform.project(key)
        except ValueError:
            raise ValueError(NOT_A_TASK) from None
        chosen, before = robot_of(platform, p.get(FIELD)), task.get(FIELD)
        platform.task_apps.check_robot(task, chosen)       # issue #50: not to a robot of another brand and model than the bound applications
        if chosen:
            task[FIELD] = chosen
        else:
            task.pop(FIELD, None)
        platform.store.put('projects', task)
    if chosen != before:
        platform.audit('project.robot_chosen', {'robot_id': chosen, 'before': before}, task['id'])
    return task
