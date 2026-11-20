"""The device profile of a real machine (issue #27): its hardware, its system, its ROS 2 and what
its resources are used for, in one record.

Nothing here is new reading except ROS 2. The hardware with its drivers, the system and the
resource figures are a full read of the live status (``device_status``: the basic status, the
running software, the hardware scan). ROS 2 is read afterwards, on an SSH session of its own,
with the one fixed command of ``ros2_profile``; afterwards, because its tools would otherwise show
up in the CPU figures. No model is involved and nothing on the device changes.

The last profile of each robot is kept (``device_profiles``, one record per robot) and goes with
the robot when it is deleted. Every part carries the time it was read (``checked_at``, seconds
since the epoch): a part that this read did not get keeps what it showed before, with its own
time and the reason (``problem``).

The record is what other features build on (the interface table of issue #23, the motion driver
check of issue #28): its keys are listed in docs/DEVICE_PROFILE.md and change only with ``SCHEMA``.
"""
import logging
import threading
import time

from . import device_access, device_status, hardware_init, ros2_profile
from .service import now

LOG = logging.getLogger(__name__)

SCHEMA = 'device.profile.v1'
KIND = 'device_profiles'
SYSTEM_KEYS = ('model', 'os', 'kernel', 'arch', 'jetson', 'cpu', 'cores')
HARDWARE_KEYS = ('checked_at', 'duration_ms', 'complete', 'problem', 'summary', 'categories', 'devices', 'issues', 'notes')
STATUS_KEYS = ('uptime_seconds', 'load', 'memory', 'disk_root', 'temperature_c')
NO_SSH = '这台机器人没有 SSH 连接；请先通过 SSH 接入'
BUSY = '这台机器人的设备档案正在读取，请稍候'
STATUS_BUSY = '另一处（真机状态页）正在读取这台机器人，请过几秒再读取设备档案'
UNREACHABLE = 'SSH 连不上这台机器人，设备档案没有更新'
ROS2_NOT_FINISHED = 'ROS 2 的读取没有完成：设备在限定时间内没有返回结果'
ROS2_NO_SESSION = '读取 ROS 2 的 SSH 会话没有建立或中断了，这一部分没有读到'


class _NotRead(Exception):
    """The ROS 2 part was not read. The message is shown to the user."""


def profile_id(robot_id):
    return 'device-profile-' + robot_id


def read_ros2(device):
    """The ``ros2`` part of a profile, read over an open SSH session with ``ros2_profile.ROS2_EXEC``."""
    started = time.monotonic()
    # Unredacted: names and types are checked against the grammar of ROS names, and replacing a short
    # password inside them (a digit, say) would rewrite them. The parser filters the text it quotes.
    result = hardware_init.run_command(device, ros2_profile.ROS2_EXEC, ros2_profile.TIMEOUT_SECONDS,
                                       keep='head', limit=ros2_profile.OUTPUT_LIMIT, redact=False)
    if result['timed_out']:
        raise _NotRead(ROS2_NOT_FINISHED)           # half a topic list would read as "that is all there is"
    try:
        parsed = ros2_profile.parse(result['stdout'], device.clean, truncated=result['output_head_dropped'])
    except Exception:
        LOG.exception('the output of the ROS 2 command could not be parsed')
        raise _NotRead(device_status.UNPARSED) from None
    return {**parsed, 'checked_at': time.time(), 'duration_ms': round((time.monotonic() - started) * 1000), 'problem': None}


def compose(view, ros2):
    """The four parts of a profile from a full status read (``device_status``) and the ``ros2`` part."""
    status = view['status']
    hardware = view.get('hardware') or device_status.NO_HARDWARE
    software = view.get('software') or device_status.NO_SOFTWARE
    machine = hardware['system'] or {}
    return {
        # The machine as the hardware scan identified it; the host name comes with the basic status.
        'system': {'checked_at': hardware['checked_at'], 'hostname': status['hostname'],
                   **{key: machine.get(key, '') for key in SYSTEM_KEYS}},
        'hardware': {key: hardware[key] for key in HARDWARE_KEYS},
        # Load, memory, disk and temperature were read at ``checked_at``; the processes a moment later.
        'resources': {'checked_at': view['checked_at'], **{key: status[key] for key in STATUS_KEYS},
                      'cores': software['cores'], 'process_count': software['process_count'], 'top': software['top'],
                      'processes_checked_at': software['checked_at'], 'problem': software['problem']},
        'ros2': ros2,
    }


class DeviceProfile:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store
        self.lock = threading.Lock()
        self.reading = set()

    def snapshot(self):
        """For ``Platform.snapshot``: which robots have a profile kept, so that the deletion's confirmation
        (web/robot_remove.js) names it, since it goes with the robot. Not the profile itself, which is asked
        for apart (``saved``); nor its time, so that reading it again does not redraw the page."""
        return {KIND: [{key: record[key] for key in ('id', 'project_id', 'robot_id')} for record in self.store.list(KIND)]}

    def _robot(self, p):
        robot = self.platform.same_project('robots', p.get('robot_id'), p.get('project_id'))
        if not robot.get('connection_id'):
            raise ValueError(NO_SSH)
        return robot, self.platform.same_project('device_connections', robot['connection_id'], robot['project_id'])

    def _kept(self, robot, connection):
        """The profile kept for the robot, unless its SSH connection was changed since (issue #9): what was
        read over another route, with another login or from another machine is not shown as this one's."""
        record = next((x for x in self.store.list(KIND) if x['id'] == profile_id(robot['id'])), None)
        current = record and (record['connection_id'], record['connection_revision']) == (connection['id'], connection.get('updated_at'))
        return record if current else None

    def saved(self, p):
        """The profile kept for a robot (None before the first read) and whether one is being read now.
        Contacts no machine."""
        robot, connection = self._robot(p)
        return {'profile': self._kept(robot, connection), 'reading': robot['id'] in self.reading}

    def read(self, p):
        """Read the profile of ``robot_id`` now, keep it in place of the earlier one and return it."""
        robot, connection = self._robot(p)
        with self.lock:
            if robot['id'] in self.reading:
                raise ValueError(BUSY)
            self.reading.add(robot['id'])
        try:
            return self._read(robot, connection)
        finally:
            with self.lock:
                self.reading.discard(robot['id'])

    def _read(self, robot, connection):
        started = time.monotonic()
        view = self.platform.engineering.status({'project_id': robot['project_id'], 'robot_id': robot['id']})
        if view.get('refreshing'):
            raise ValueError(STATUS_BUSY)           # what came back is the previous read, not this one
        if not view['reachable']:
            raise ValueError((view['error'] or {}).get('message') or UNREACHABLE)
        earlier = (self._kept(robot, connection) or {}).get('ros2')
        ros2 = self._ros2(connection, earlier)
        record = {'id': profile_id(robot['id']), 'schema': SCHEMA, 'project_id': robot['project_id'], 'robot_id': robot['id'],
                  'connection_id': connection['id'], 'connection_revision': connection.get('updated_at'), 'created_at': now(),
                  **compose(view, ros2)}
        record['duration_ms'] = round((time.monotonic() - started) * 1000)
        # Under the lock a deletion holds from its checks to its last delete (issue #11): a robot that was
        # deleted while this read ran gets no profile, and none is written into a deletion half way.
        with self.platform.engineering.lock:
            self.store.get('robots', robot['id'])
            self.store.put(KIND, record)
        self.platform.audit('device.profile_read', {'robot_id': robot['id'], 'profile_id': record['id'],
                                                    'ros2': ros2['status'], 'distro': ros2['distro']}, robot['project_id'])
        return record

    def _ros2(self, connection, earlier):
        """The ``ros2`` part. It cannot fail the profile: when it was not read, the earlier one is kept
        with its own time and the reason, or every field is unknown."""
        device = None
        try:
            device = device_access.SSHDevice(*self.platform.ssh_edit.saved_login(connection['id']))     # issue #12
            return read_ros2(device)
        except _NotRead as exc:
            problem = str(exc)
        except device_access.ConnectError as exc:
            problem = str(exc)
        except Exception as exc:                    # paramiko / EOF / socket errors of a session that died
            LOG.warning('reading ROS 2 over SSH failed: %s', type(exc).__name__)
            problem = ROS2_NO_SESSION
        finally:
            if device is not None:
                try:
                    device.close()
                except Exception as exc:            # closing a dead session must not hide the real error
                    LOG.warning('closing the SSH session of a ROS 2 read failed: %s', type(exc).__name__)
        return {**(earlier or {**ros2_profile.NOT_READ, 'checked_at': None, 'duration_ms': None}), 'problem': problem}
