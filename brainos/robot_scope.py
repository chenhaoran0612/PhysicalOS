"""Robots belong to the account, not to one task (issue #42, second part).

A robot used to be a record of the task (任务项目) that was current when it was added: it was only
listed while that task was open, another task could not use it, and the task could not be deleted
before its robots were. The owner's rule: "我的机器人" and the tasks are independent; different tasks
can work with the same robot.

How it is kept:

* **One home for every robot.** The workspace has a record of kind ``projects`` with the fixed id
  :data:`HOME` that is not a task: the state the pages read leaves it out of the task list and names
  it as ``robot_home``. Every robot carries it as its ``project_id``, and so does what is kept *about*
  a robot: its SSH connection, investigations and repairs with their findings, hardware scans, the
  device profile, deliveries and so on. The modules that keep those records are unchanged: they still
  write "the robot's project", which is now the same for all robots.
* **Requests about robots are answered for the home**, whichever task is open in the page
  (:func:`request`, called where the server dispatches a request). The page keeps sending the task it
  shows; the modules see the home and find the robot's records.
* **A task uses any robot.** ``Platform.same_project`` accepts a record kept at the home, so every
  robot, for every task; a request for the home may name what a task holds (the assistant's model
  that was chosen in the open task). What a task makes with a robot (applications, test reports, releases,
  operation records) stays with the task and goes when the task is deleted. The robot stays.
* **Data from before** is moved once when a workspace is opened (:func:`migrate`): every robot, and
  every record that names a robot and was kept in that robot's task, except the kinds in
  :data:`TASK_KINDS` (of the operation records, those in :data:`ROBOT_OPERATIONS` move). Saved in place and in one transaction, so the order of the lists does not change
  and a move that was interrupted has not happened.

Code that calls the platform directly with a task's id (the unit tests do) still gets a robot in
that task; nothing in the application does that any more.
"""
import json
from datetime import datetime, timezone

HOME = 'project-robot-home'
HOME_NAME = '我的机器人'
HOME_OBJECTIVE = '这个账号的机器人：接入、调查和维护。它们不属于某一个任务，每个任务都可以使用。'

# Requests about a robot, or about what is kept for one. Exact names, and the groups that start with a prefix.
ROUTES = ('robots', 'sims', 'engineering/start', 'skills/examples')
PREFIXES = ('robots/', 'sims/', 'engineering/ssh/', 'engineering/hardware/', 'engineering/profile',
            'engineering/investigation/', 'engineering/adaptation/', 'skills/package-', 'library/deliver/',
            'cloud-skills/install')
# Issue #50: applications, their skills, tests and releases belong to the account too (brainos/task_apps.py);
# binding one to a task (task-apps/...) is a request for that task.
ROUTES += ('skills', 'applications', 'harness', 'onsite', 'deployments')
PREFIXES += ('applications/', 'onsite/')
# The task that was open in the page when a request about a robot was sent (a simulated robot's
# first run opens a task for the trial; see brainos/sim_robots.py).
TASK_FIELD = 'task_project_id'
NOT_HERE = '请先进入一个任务，再做这一步'
NOT_FOUND = '找不到 '                         # how brainos/store.py starts when a record is not there
SESSION_ASSET_GONE = '这次会话当时选用的模型或助手工具已经不在了（保存它们的任务可能已被删除）。不能继续这一次；请重新开始一次新的'

# What a task makes stays with the task, also when the record names a robot.
TASK_KINDS = ('projects', 'events', 'applications', 'deployments', 'evaluations', 'operations', 'jobs', 'models',
              'model_deployments', 'skills', 'datasets', 'scenes', 'services', 'engineering_workflows', 'workflow_runs',
              'conformance_reports')
# Kept for adding or changing a robot's connection, without a robot's id: the host keys that were read.
HOME_KINDS = ('host_key_checks',)
# Operation records are a task's (an application that was tested, started or stopped), except what was
# done to the robot directly, for no application: a skill package installed on it, an installed skill
# run on it, 停止 and 解除急停.
ROBOT_OPERATIONS = ('skill_install', 'skill_run', 'stop', 'reset')


def is_home(project_id):
    return project_id == HOME


def elsewhere(record, project_id, required=False):
    """Whether ``record`` (a robot, or what is kept for one) is kept in another task than the one asked for.

    Nothing kept at the home is, and a request for the home reaches a robot wherever it is kept. No
    ``project_id`` asks for no check, unless one is ``required``."""
    if not project_id:
        return required
    return record.get('project_id') != project_id and HOME not in (project_id, record.get('project_id'))


def kept(read):
    """``read()``; when what a robot's session used is gone (a model or a tool of the assistant that was
    chosen in a task, and the task was deleted since), say that instead of the store's own words."""
    try:
        return read()
    except ValueError as error:
        if str(error).startswith(NOT_FOUND):
            raise ValueError(SESSION_ASSET_GONE) from None
        raise


def ensure_home(store):
    """The home record of this workspace (made on first use)."""
    try:
        return store.get('projects', HOME)
    except ValueError:
        return store.put('projects', {'id': HOME, 'name': HOME_NAME, 'objective': HOME_OBJECTIVE,
                                      'system': True, 'created_at': datetime.now(timezone.utc).isoformat()})


def tasks(projects):
    """``projects`` without the home: the tasks a person made."""
    return [p for p in projects if not is_home(p.get('id'))]


def request(route, payload):
    """``payload`` as the modules should see it: a request about a robot is one for the home."""
    if route not in ROUTES and not route.startswith(PREFIXES):
        if is_home(payload.get('project_id')):      # what a task makes is not kept at the robots' home
            raise ValueError(NOT_HERE)
        return payload
    scoped = {**payload, 'project_id': HOME}
    asked = payload.get('project_id')
    if isinstance(asked, str) and asked and not is_home(asked):
        scoped[TASK_FIELD] = asked
    return scoped


def migrate(store):
    """Move every robot, and what is kept about it, to the home. Returns how many records moved per kind.

    Does nothing once it has been done. All or nothing: one transaction, written in place."""
    with store.lock:
        ensure_home(store)
        robots = [r for r in store.list('robots') if not is_home(r.get('project_id'))]
        was = {r['id']: r.get('project_id') for r in robots}             # robot -> the task it was kept in
        connections = {r['connection_id']: r['id'] for r in robots if r.get('connection_id')}
        moved, changes = {}, [('robots', r) for r in robots]
        for kind in store.kinds():
            if kind == 'robots' or (kind in TASK_KINDS and kind != 'operations'):
                continue
            for record in store.list(kind):
                if not isinstance(record, dict) or is_home(record.get('project_id')) or 'project_id' not in record:
                    continue
                if not isinstance(record.get('id'), str):           # nothing to save it under: left as it is
                    continue
                if kind == 'operations' and (record.get('kind') not in ROBOT_OPERATIONS or record.get('application_id')):
                    continue
                robot_id = record.get('robot_id') or (connections.get(record.get('id')) if kind == 'device_connections' else None)
                if kind in HOME_KINDS or (robot_id in was and record['project_id'] == was[robot_id]):
                    changes.append((kind, record))
        if not changes:
            return moved
        try:
            for kind, record in changes:
                record['project_id'] = HOME
                store.db.execute('UPDATE objects SET payload=? WHERE kind=? AND id=?',
                                 (json.dumps(record, ensure_ascii=False, allow_nan=False), kind, record['id']))
                moved[kind] = moved.get(kind, 0) + 1
            store.db.commit()
        except Exception:
            store.db.rollback()
            raise
        return moved
