"""Delete a task from 任务工作台 (issue #44).

A task is what the console calls a 任务项目 (kind ``projects``): the goal a person described, and
everything that was saved while working on it. The workbench could create tasks but never take one
off the list. Deleting a task removes what this computer keeps for it:

* the task itself;
* every record that carries its id, whatever its kind: models, datasets, skills, applications,
  jobs, test reports, releases, operation records, assistant sessions, flows, tools of the
  assistant and so on. A record kind a later feature adds needs no entry here;
* what is saved under such a record's id: the keys of the assistant's own models and tool servers,
  the raw output of a hardware scan;
* the files the task's assistant sessions wrote on this computer.

Refused while the task still has robots. A robot carries a saved login, investigations and, for a
simulated one, a running process; each kind has its own delete on "我的机器人" (brainos/robot_remove.py,
``SimRobots.remove``) that knows what may not be interrupted. Delete the robots there first.

Also refused while something of the task is still at work or still owes an answer about money:

* an assistant session or a flow that is running;
* a job that is out at an execution service, or whose submission got no clear answer (the service
  may be running it);
* a job whose reserved credits were not settled yet: only the job's record can settle them;
* an assistant session whose cloud session could not be closed: only its record can close it.

Nothing is sent anywhere: no device and no service is contacted. The audit log keeps its lines and
gets one that says what went.

A refusal leaves everything as it was. The files go first: while one of them cannot be deleted,
nothing else is. The records are deleted one by one and the task's own record last, so a deletion
that was interrupted (the app was closed) leaves the task on the list, and deleting again finishes it.

Not covered: a robot or another record that is being added to the task at the very moment it is
deleted (the check of the task and the write are two steps there) can be left behind without a task.
"""
from .engineering import ACTIVE
from .workflows import RUNNING

JOB_ACTIVE = ('dispatching', 'submitted', 'queued', 'running', 'cancelling', 'unknown')    # at a service, or maybe
METERING_OPEN = ('cloud_close_pending', 'cloud_close_failed')     # engineering.py: the cloud session still has to be closed
KEPT = ('projects', 'events')               # the task's own record goes last; the log keeps its lines
SECRET_OWNERS = ('agent_models', 'agent_mcp_servers', 'device_connections')    # agent_secrets is keyed by their ids
RAW_OWNERS = {'hardware_scans': 'hardware_scan_raw'}                           # kept apart, under the scan's id
NAMES_SHOWN = 3
GONE = '这个任务已经不在了，可能已经被删除；请刷新页面'
HAS_ROBOTS = '这个任务里还有 {count} 台机器人（{names}）。请先到“我的机器人”里删除它们，再删除任务'
SESSION_RUNNING = '这个任务有助手会话正在运行；请等它结束，或先停止它，再删除'
FLOW_RUNNING = '这个任务的协作流程正在运行；请等这一阶段结束，或先停止流程，再删除'
JOB_RUNNING = '这个任务提交给执行服务的任务还在执行，或提交结果还没有确认（{name}）；请等它结束、同步状态或先取消它，再删除'
JOB_UNSETTLED = '这个任务有预留额度还没有结算（{name}）；请先在任务记录里同步或取消它，再删除'
CLOUD_OPEN = '这个任务有助手会话的云端会话还没有关闭，费用待确认（{name}）；请先在这次会话里“关闭云端会话”，再删除'


class TaskRemoval:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store

    def remove(self, p):
        """Delete the task ``project_id`` and what is kept for it.

        Returns the task's id and how many records of each kind went."""
        engine, hardware = self.platform.engineering, self.platform.hardware
        # As when a robot is deleted: from the checks to the last delete no session can start and no scan can register.
        with engine.lock, hardware.lock:
            project = self._task(p.get('project_id'))
            self._refuse_while_in_use(project)
            for session in self._of(project, 'agent_sessions'):
                self.platform.robot_remove._delete_files(session['id'])
            deleted = {}
            with self.store.atomic():
                for kind in self.store.kinds():
                    if kind in KEPT:
                        continue
                    records = self._of(project, kind)
                    for record in records:
                        if kind in SECRET_OWNERS:
                            self.store.delete('agent_secrets', record['id'])      # the saved keys go first
                        if kind in RAW_OWNERS:
                            self.store.delete(RAW_OWNERS[kind], record['id'])
                        self.store.delete(kind, record['id'])
                    if records:
                        deleted[kind] = len(records)
                self.store.delete('projects', project['id'])
                self.platform.audit('project.removed', {'project_id': project['id'], 'name': project['name'], 'deleted': deleted})
        return {'removed': project['id'], 'deleted': deleted}

    def _task(self, key):
        try:
            return self.platform.project(key)
        except ValueError:
            raise ValueError(GONE) from None

    def _of(self, project, kind):
        return [x for x in self.store.list(kind) if isinstance(x, dict) and x.get('project_id') == project['id']]

    def _refuse_while_in_use(self, project):
        robots = self._of(project, 'robots')
        if robots:
            names = '、'.join(r['name'] for r in robots[:NAMES_SHOWN]) + (' 等' if len(robots) > NAMES_SHOWN else '')
            raise ValueError(HAS_ROBOTS.format(count=len(robots), names=names))
        live = self.platform.engineering.live
        if any(s.get('status') in ACTIVE or s['id'] in live for s in self._of(project, 'agent_sessions')):
            raise ValueError(SESSION_RUNNING)
        if any(run.get('status') in RUNNING for run in self._of(project, 'workflow_runs')):
            raise ValueError(FLOW_RUNNING)
        job = next((j for j in self._of(project, 'jobs') if j.get('status') in JOB_ACTIVE), None)
        if job:
            raise ValueError(JOB_RUNNING.format(name=job.get('name') or job['id']))
        job = next((j for j in self._of(project, 'jobs') if self._held(j)), None)
        if job:
            raise ValueError(JOB_UNSETTLED.format(name=job.get('name') or job['id']))
        session = next((s for s in self._of(project, 'agent_sessions') if s.get('metering_status') in METERING_OPEN), None)
        if session:
            raise ValueError(CLOUD_OPEN.format(name=session.get('name') or session['id']))

    def _held(self, job):
        """Whether credits reserved for the job are still held (the account's records, not the workspace's)."""
        commerce = self.platform.commerce
        if not commerce or not job.get('reservation_id'):
            return False
        try:
            return commerce.store.get('reservations', job['reservation_id']).get('status') == 'held'
        except ValueError:
            return False
