"""Delete a robot from the project (issue #11).

"我的机器人" could add a real machine but never take one off the list: a robot that was added
twice, or is no longer used, stayed there with its SSH connection and its investigations.
Deleting it removes what this computer keeps about it:

* the robot record;
* every SSH connection saved for it and the logins saved with them (passwords and private
  keys, the jump host's too);
* its investigations, repairs and fix chats, the files those runs wrote on this computer, and
  what they found: device evidence, inventories (本体档案), hardware scans and repair reports;
* the SSH session the status page kept open.

Nothing is sent to the machine: the deletion does not log in, and what runs there keeps running.
Applications, test reports, deployments and operation records that name the robot stay, as they
do when a simulated robot is deleted. So do the audit log and workflow runs with what they
produced; an adaptation flow keeps, in its own stage directories, the copy of the inventory and
the evidence excerpts it was started from, and one that was not finished cannot go on.

Refused while something is using the robot: a running investigation or repair, a hardware scan,
or an adaptation flow whose stage is running. Simulated robots keep their own delete
(``SimRobots.remove``), which also stops their process.

Either everything goes or the robot stays. The files go first: while one of them cannot be
deleted, nothing else is. The records follow, the robot's own last, so a deletion that was
interrupted leaves the card, and deleting again finishes it.
"""
import shutil

from . import adaptation
from .engineering import ACTIVE
from .repair_policy import MARKS
from .workflows import RUNNING

SESSION_NAMES = {'discovery': '调查', 'repair': '修复'}      # as web/robot_remove.js and web/ssh_edit.js name them
# Kept per robot besides its connections (with their logins) and its sessions. The raw output of
# a hardware scan is stored apart, under the scan's id. The marks (issue #13) say which hardware
# problems a person handled on site or a repair left unresolved, with the assistant's note.
ROBOT_RECORDS = ('device_evidence', 'robot_inventories', 'hardware_scans', 'hardware_repairs', MARKS)
ROBOT_RECORDS += ('device_profiles',)      # issue #27: the device profile (brainos/device_profile.py KIND)
SIMULATED = '仿真机器人请用它卡片上的“删除”：那里会先关闭它的仿真进程'
SCANNING = '这台机器人正在扫描硬件；请等扫描结束，再删除'
ADAPTING = '这台机器人的本体适配流程正在运行；请等这一阶段结束，或先停止流程，再删除'
FILES_LEFT = '这台机器人的调研文件没能从这台电脑上删掉，机器人没有删除：{path}。请关闭正在使用这个文件夹的程序后再删除'


class RobotRemoval:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store

    def remove(self, p):
        """Delete the robot ``robot_id`` of ``project_id`` and what is kept about it.

        Returns the robot's id and how many records of each kind went."""
        engine, hardware = self.platform.engineering, self.platform.hardware
        # From the checks to the last delete no session of this robot can start (the engine's lock) and no
        # hardware scan can register (the scanner's): a scan that was about to finds the login gone and fails.
        with engine.lock, hardware.lock:
            robot = self.platform.same_project('robots', p.get('robot_id'), p.get('project_id'))
            self._refuse_while_in_use(robot)
            connections = [c for c in self.store.list('device_connections')
                           if c.get('robot_id') == robot['id'] or c['id'] == robot.get('connection_id')]
            sessions = self._of(robot, 'agent_sessions')
            for session in sessions:
                self._delete_files(session['id'])
            deleted = {'device_connections': len(connections), 'agent_sessions': len(sessions)}
            with self.store.atomic():
                for connection in connections:
                    self.store.delete('agent_secrets', connection['id'])      # the saved logins go first
                    self.store.delete('device_connections', connection['id'])
                for session in sessions:
                    self.store.delete('agent_sessions', session['id'])
                for kind in ROBOT_RECORDS:
                    records = self._of(robot, kind)
                    deleted[kind] = len(records)
                    for record in records:
                        if kind == 'hardware_scans':                          # found only through its scan: before it
                            self.store.delete('hardware_scan_raw', record['id'])
                        self.store.delete(kind, record['id'])
                deleted = {kind: count for kind, count in deleted.items() if count}
                self.store.delete('robots', robot['id'])
                self.platform.audit('robot.removed', {'robot_id': robot['id'], 'name': robot['name'],
                                                      'family': robot.get('family'), 'deleted': deleted}, robot['project_id'])
        from . import device_transfer          # here: device_transfer_record imports brainos.service
        for connection in connections:
            engine.status_sessions.forget(connection['id'])
            # Issue #18: this computer's records of unfinished transfers to it; the device is not contacted.
            device_transfer.forget_robot_transfers(self.platform, connection['id'])
        return {'removed': robot['id'], 'deleted': deleted}

    def _of(self, robot, kind):
        return [x for x in self.store.list(kind) if x.get('robot_id') == robot['id']]

    def _refuse_while_in_use(self, robot):
        if robot.get('sim'):
            raise ValueError(SIMULATED)
        live = self.platform.engineering.live
        running = next((s for s in self._of(robot, 'agent_sessions') if s['status'] in ACTIVE or s['id'] in live), None)
        if running:
            kind = SESSION_NAMES.get(running.get('mode'), '工程')
            raise ValueError(f'这台机器人有{kind}会话正在运行；请等它结束，或先停止它，再删除')
        if robot['id'] in self.platform.hardware.scanning:
            raise ValueError(SCANNING)
        for run in self.store.list('workflow_runs'):
            definition = run.get('definition') or {}
            if (definition.get('template') == adaptation.TEMPLATE and definition.get('robot_id') == robot['id']
                    and run.get('status') in RUNNING):
                raise ValueError(ADAPTING)

    def _delete_files(self, session_id):
        """Delete what a session's runs wrote on this computer; refuse to go on while something of it is left."""
        base = (self.store.root / 'engineering').resolve()
        path = (base / session_id).resolve()
        if path.parent != base:                           # never follow a record's id out of the sessions' directory
            return
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            raise ValueError(FILES_LEFT.format(path=path))
