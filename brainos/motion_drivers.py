"""Issue #28 (R1-10): does a device have the motion-control interfaces its body type should have?

Three things are put side by side, and none of them is new reading:

* what a body of this type should have: ``required_capabilities`` of its type in the platform's list
  of body types (issue #32);
* which ROS 2 topic or action carries each of those capabilities, and with which message type: the
  standard (``neuxmind_spec.ros2_interfaces``);
* what the device has: the ROS 2 part of its device profile (issue #27), read with the one fixed
  read-only command of ``ros2_profile``.

A capability is **there** when the device's listing has a topic (or action) of an accepted message
type, whatever its name: robots namespace and remap their topics. It is **missing** when no such
interface is listed, and of the **wrong type** when the conventional name is there with other types
only. What the standard binds to no ROS 2 interface (a stop, a posture) is **not verified** and is
never reported as missing. Nothing is guessed: a device whose type is not in the list, a ROS
distribution other than Humble and a listing that was not read in full are not judged at all.

A missing interface becomes a problem of its own kind (id ``motion-driver:<capability>``) in every
hardware scan record saved while the kept profile says so. That is all it takes to reach the repair
flow of ``hardware_init``: the problem can be ticked and handed to the assistant like any other, it
is in HARDWARE_SCAN.json with the interface that is expected, and the report rules apply. The scan
command itself is unchanged and runs no ROS 2 tool.

Whether such a problem is gone is never decided by the hardware scan. While problems of this kind are
known, the fixed ROS 2 command is run again next to the scan: by ``hardware_rescan`` of a repair
session whose baseline has them, over its fresh login (``reverify``), and by a scan a person asks
for, on a session of its own (``recheck``). A read that fails or is not conclusive proves nothing:
the problems stay as they were. A robot without such problems is scanned as before.

The whole check is read-only. It publishes nothing, calls no service, sends no goal and changes no
state of the robot or of the platform's record of it.

What it cannot tell: the listing says that a topic exists, not who publishes or subscribes it. A
``/cmd_vel`` that only a joystick node created counts as there.
"""
import logging
import time

from . import device_access, device_profile
from .contracts import FAMILIES
from .spec_bridge import capabilities as spec_caps, ros2_interfaces

LOG = logging.getLogger(__name__)

KEY = 'motion_driver_checks'
ISSUE_PREFIX = 'motion-driver:'
SOURCE = 'ros2_profile'
CATEGORY = 'motion'
FOUND_MAX = 5               # interfaces of the right type named per capability; the count says how many there are
NOTE_CHARS = 300
CONCLUSIVE = ('ok', 'missing')
UNDECIDED = ('unknown', 'not_read')       # the profile says nothing either way: problems known before are kept
# A scan a person waits for answers within the web view's limit for one request (about a minute): a ROS 2
# read that would run past it is cut and counts as not done.
SCAN_REQUEST_SECONDS = 55
READ_MIN_SECONDS = 8
SEVERITY_ORDER = {'error': 3, 'warning': 2, 'info': 1}
KINDS = {'topic': '话题', 'action': '动作'}

NOT_READ = '还没有读取这台设备的档案。读取设备档案（其中的 ROS 2 话题和动作）之后，才能核对运控驱动。'
NOTHING_TO_CHECK = '这个本体类型应有的接口在标准里都没有对应的 ROS 2 话题或动作，平台从 ROS 2 清单核对不了。'
NO_ROS2 = '设备上没有读到 ROS 2（/opt/ros 下没有安装，也没有带 ROS 环境的进程在运行）：'
NOTHING_RUNS = '设备上有 ROS 2 Humble，但没有读到正在运行的节点：'
DEFAULT_ENVIRONMENT = '这一次是按默认设置读的；节点如果由别的账号运行，或设置了别的 ROS_DOMAIN_ID，这里看不到。'
NOT_LISTED = '读到的 ROS 2 {what}清单里没有这个类型的{what}：'
CAUSE = '运控驱动没有安装，或者没有在运行。'
HINT = ('需要安装并启动这台本体的运控驱动。可以交给修复助手（issue #29）：它先查资料库里的驱动安装包，有就送到设备装上并启动，'
        '启动那一步要有人在现场确认；资料库里没有时，要你提供安装包或在现场安装。装好以后重新扫描，或重新读取设备档案：'
        '都会重新读取 ROS 2 的清单来核对，只读，不发布运动指令。')
HINT_WRONG_TYPE = ('需要让运控驱动用上面的消息类型提供这个接口（改驱动的配置或版本）。修复助手可以排查，并在批准后改驱动的配置或换装资料库里的驱动版本，'
                   '重新启动驱动时要有人在现场确认。改好以后重新扫描或重新读取设备档案核对。')
UNBOUND = {
    'safety.stop': '停止没有自己的 ROS 2 接口：ROS 2 本体靠运动接口停下（发零速度、取消动作），所以不单独核对。',
    'system.health': '系统健康没有固定的 ROS 2 接口，不从话题清单核对。',
}
UNBOUND_DEFAULT = '标准里没有规定这一项对应哪个 ROS 2 话题或动作（各厂商做法不同），平台从 ROS 2 清单核对不了，需要人确认。'
CARRIED = '这一次没有读到可以下结论的 ROS 2 清单（{problem}），运控驱动的问题按之前的结果保留，不能据此判断已经解决。'
KEPT_FROM_SCAN = '运控驱动的问题按上一次扫描的结果保留，不能据此判断已经解决：'
NO_TIME_LEFT = '硬件扫描用完了这一次请求的时间，没有再读取 ROS 2'


def expected(body_type):
    """The interfaces a body of this type should have, in ROS 2 terms, in the order of the type."""
    items = []
    for cap_id in body_type.get('required_capabilities') or []:
        capability, interface = spec_caps.CAPABILITIES.get(cap_id), ros2_interfaces.get(cap_id)
        item = {'capability': cap_id, 'title': capability.title if capability else cap_id,
                'kind': None, 'role': None, 'name': None, 'types': [], 'note': ''}
        if interface is None:
            item['note'] = UNBOUND.get(cap_id, UNBOUND_DEFAULT)
        else:
            item.update(kind=interface.kind, role=interface.role, name=interface.name, types=list(interface.types))
        items.append(item)
    return items


def _verdict(status, reason, items, body_type=None, cause=''):
    named = {'id': body_type['id'], 'name': body_type['name']} if body_type else None
    return {'status': status, 'reason': reason, 'cause': cause, 'body_type': named, 'items': items}


def _shown(text):
    return ' '.join(str(text or '').split())[:NOTE_CHARS]


def inconclusive(ros2):
    """Why this ROS 2 part says nothing about what the device has, as a clause; '' when it can be judged."""
    status = ros2.get('status')
    if status == 'ok':
        return ''
    if status == 'absent' and ros2.get('complete'):
        return ''                                   # the command ran to its end and found no ROS at all
    if status == 'unsupported':
        return f"这台设备运行的是 ROS {ros2.get('distro') or '未知'}，平台只支持 ROS 2 Humble"
    if status == 'not_read' or ros2.get('found') is None:
        why = _shown(ros2.get('problem')).rstrip('。')
        return 'ROS 2 这一部分没有读到' + (f'：{why}' if why else '')
    notes = _shown('；'.join(note.rstrip('。') for note in ros2.get('notes') or []))
    return 'ROS 2 的清单不完整' + (f'：{notes}' if notes else '')


def _cause(ros2):
    """What the profile says about why interfaces are not there, as the opening of a sentence; '' when it says nothing."""
    if ros2.get('status') == 'absent':
        return NO_ROS2
    if ros2.get('nodes') == []:
        default = (ros2.get('environment') or {}).get('source') == 'default'
        return NOTHING_RUNS + (DEFAULT_ENVIRONMENT if default else '')
    return ''


def _judged(item, ros2):
    """One expected interface against the listing: present, missing, wrong_type or unverified."""
    if item['kind'] is None:
        return {**item, 'state': 'unverified', 'found': [], 'found_total': 0}
    rows = ros2.get('topics' if item['kind'] == 'topic' else 'actions') or []
    accepted = set(item['types'])
    matches = sorted((row for row in rows if accepted & set(row['types'])), key=lambda row: (row['name'] != item['name'], row['name']))
    if matches:
        return {**item, 'state': 'present', 'found': [dict(row) for row in matches[:FOUND_MAX]], 'found_total': len(matches)}
    named = [dict(row) for row in rows if row['name'] == item['name']]
    state = 'wrong_type' if named else 'missing'
    return {**item, 'state': state, 'found': named, 'found_total': len(named), 'issue_id': ISSUE_PREFIX + item['capability']}


def assess(body_type, ros2, family=None):
    """Compare what a body type should have with the ``ros2`` part of a device profile (None: no profile).

    ``status``: ``ok`` (nothing that can be checked is missing), ``missing``, ``unlisted_type`` (the
    device's type is not in the list: nothing is guessed), ``nothing_to_check`` (no interface the type
    asks for has a ROS 2 binding), ``not_read`` (no profile yet) or ``unknown`` (the profile cannot be
    judged; ``reason`` says why)."""
    if body_type is None:
        name = FAMILIES.get(family, family) if isinstance(family, str) and family else '（没有类型）'
        return _verdict('unlisted_type', f'这台设备的类型“{_shown(name)}”不在本体类型清单里，平台不知道它应该有哪些运控接口，所以没有核对。'
                        '先把它改成清单里的类型，再读取设备档案。', [])
    items = expected(body_type)
    if not any(item['kind'] for item in items):
        # A verdict about the type, whatever was read: problems another type asked for are not kept for it.
        return _verdict('nothing_to_check', NOTHING_TO_CHECK, [_judged(item, ros2 or {}) for item in items], body_type)
    unchecked = [{**item, 'state': 'unchecked', 'found': [], 'found_total': 0} for item in items]
    if ros2 is None:
        return _verdict('not_read', NOT_READ, unchecked, body_type)
    why = inconclusive(ros2)
    if why:
        again = '' if ros2.get('status') == 'unsupported' else '重新读取设备档案后再核对。'
        return _verdict('unknown', f'{why}。这一次没有核对运控驱动。{again}', unchecked, body_type)
    judged = [_judged(item, ros2) for item in items]
    missing = any(item['state'] in ('missing', 'wrong_type') for item in judged)
    return _verdict('missing' if missing else 'ok', '', judged, body_type, _cause(ros2) if missing else '')


def _issue(item, result):
    what, types = KINDS[item['kind']], ' 或 '.join(item['types'])
    wanted = (f"按本体类型“{result['body_type']['name']}”，运控驱动应提供{what} {item['name']}，消息类型 {types}"
              '（名称可以不同，类型要对）。')
    if item['state'] == 'wrong_type':
        actual = '、'.join(item['found'][0]['types'])
        title = f"运控接口的消息类型不对：{item['title']}（{item['capability']}）"
        detail = wanted + f"设备上有{what} {item['name']}，但它的消息类型是 {actual}，不是上面的类型。"
        hint = HINT_WRONG_TYPE
    else:
        title = f"缺少运控接口：{item['title']}（{item['capability']}）"
        detail = wanted + (result['cause'] or NOT_LISTED.format(what=what)) + CAUSE
        hint = HINT
    return {'id': item['issue_id'], 'severity': 'error', 'category': CATEGORY, 'title': title[:200], 'detail': detail[:800],
            'hint': hint[:600], 'device_id': None, 'fixable': 'auto', 'source': SOURCE,
            'capability': item['capability'], 'state': item['state'],
            'expected': {key: item[key] for key in ('kind', 'role', 'name', 'types')}}


def issues(result):
    """The problems of a check, shaped like the problems of a hardware scan (``hardware_scan._Builder.issue``).
    ``fixable`` is ``auto``: the problem can be handed to the assistant, which is what the requirement asks."""
    if result['status'] != 'missing':
        return []
    return [_issue(item, result) for item in result['items'] if item['state'] in ('missing', 'wrong_type')]


def _ros2_of(record, connection):
    """The ROS 2 part of a kept device profile, unless the SSH connection was changed since it was read:
    what was read over another route is not shown as this robot's (the rule of ``DeviceProfile._kept``)."""
    if not record or not connection:
        return None
    if (record.get('connection_id'), record.get('connection_revision')) != (connection['id'], connection.get('updated_at')):
        return None
    return record.get('ros2')


def _merged(parsed, result, found, reverified=False, problem=None, note=None):
    """A parsed hardware scan with the motion driver problems ``found`` added and what the check said."""
    combined = sorted(parsed['issues'] + found, key=lambda issue: -SEVERITY_ORDER[issue['severity']])     # stable: hardware first
    summary = {**parsed['summary'],
               'errors': parsed['summary']['errors'] + sum(1 for issue in found if issue['severity'] == 'error'),
               'warnings': parsed['summary']['warnings'] + sum(1 for issue in found if issue['severity'] == 'warning')}
    said = {'status': result['status'], 'reason': result['reason'], 'body_type': result['body_type'],
            'checked_at': result['checked_at'], 'reverified': reverified, 'problem': problem or result['problem'],
            'missing': [issue['id'] for issue in found]}
    return {**parsed, 'issues': combined, 'summary': summary, 'notes': parsed['notes'] + ([note] if note else []),
            'motion_drivers': said}


class MotionDrivers:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store

    # ---- the check of one robot ----------------------------------------------
    def judge(self, robot, ros2, body_type):
        """The check of ``robot`` against a ``ros2`` part; ``body_type`` is its type in the list, or None."""
        ros2 = ros2 or None
        return {'robot_id': robot['id'], 'project_id': robot.get('project_id'), 'family': robot.get('family'),
                **assess(body_type, ros2, robot.get('family')),
                'checked_at': ros2['checked_at'] if ros2 else None, 'problem': ros2.get('problem') if ros2 else None}

    def _kept(self, robot):
        """(kept profile record or None, its connection or None) of a robot."""
        try:
            connection = self.store.get('device_connections', robot.get('connection_id'))
        except ValueError:
            return None, None
        try:
            return self.store.get(device_profile.KIND, device_profile.profile_id(robot['id'])), connection
        except ValueError:
            return None, connection

    def current(self, robot):
        """The check of a robot against the profile kept for it. Contacts no machine."""
        return self.judge(robot, _ros2_of(*self._kept(robot)), self.platform.body_types.get(robot.get('family')))

    def snapshot(self):
        """``motion_driver_checks``: one entry for every real robot with an SSH connection. Everything in it
        changes only when a profile is read or a type is changed, never on a timer."""
        robots = sorted((r for r in self.store.list('robots') if r.get('connection_id') and not r.get('sim')), key=lambda r: r['id'])
        if not robots:
            return {KEY: []}
        types = {t['id']: t for t in self.platform.body_types.list()}
        connections = {c['id']: c for c in self.store.list('device_connections')}
        profiles = {p['id']: p for p in self.store.list(device_profile.KIND)}
        return {KEY: [self.judge(robot, _ros2_of(profiles.get(device_profile.profile_id(robot['id'])),
                                                 connections.get(robot['connection_id'])), types.get(robot.get('family')))
                      for robot in robots]}

    # ---- in a hardware scan record ---------------------------------------------
    def _previous(self, robot):
        """The motion driver problems of the robot's latest saved scan (newest first; the first of equal times wins)."""
        latest = None
        for scan in self.store.list('hardware_scans'):
            if scan['robot_id'] == robot['id'] and (latest is None or scan['created_at'] > latest['created_at']):
                latest = scan
        return [issue for issue in (latest or {}).get('issues', []) if issue['id'].startswith(ISSUE_PREFIX)]

    def _known(self, robot, result):
        """The motion driver problems known for a robot. While the kept profile cannot be judged (an incomplete
        read, a connection changed since it was read), the ones the latest scan listed: not knowing is not the
        same as gone, and a problem that silently left a scan would also lose its mark (``scan_saved``)."""
        if result['status'] == 'missing':
            return issues(result)
        return self._previous(robot) if result['status'] in UNDECIDED else []

    def attach(self, robot, parsed):
        """A parsed hardware scan as it is saved: with the motion driver problems the kept profile shows.
        The scan read no ROS 2 for this (``hardware_scan.SCAN_COMMAND`` never does)."""
        if 'motion_drivers' in parsed:
            return parsed                           # a repair's rescan has just verified them itself
        result = self.current(robot)
        known = self._known(robot, result)
        if result['status'] in UNDECIDED and known:
            return _merged(parsed, result, known, note=KEPT_FROM_SCAN + result['reason'])
        return _merged(parsed, result, issues(result))

    def recheck(self, robot, connection, parsed, deadline=None):
        """For a scan a person asked for: while motion driver problems are known, ROS 2 is read again (on an
        SSH session of its own, after the scan), so that "scan again" sees a driver that was installed on
        site. A robot without such problems is scanned as before: no ROS 2 tool runs. ``deadline``
        (``time.monotonic()``) is when the whole request has to be answered."""
        known = self._known(robot, self.current(robot))
        if not known:
            return self.attach(robot, parsed)
        return self._verified(robot, connection, parsed, known, lambda: self._read(connection, deadline))

    def reverify(self, s, robot, connection, device, parsed):
        """For ``hardware_rescan``: when the session's baseline scan has motion driver problems, ROS 2 is
        read again over ``device`` (the rescan's fresh login) and the problems are judged by that."""
        baseline = self.store.get('hardware_scans', s['hardware']['scan_id'])
        carried = [issue for issue in baseline['issues'] if issue['id'].startswith(ISSUE_PREFIX)]
        if not carried:
            return self.attach(robot, parsed)
        return self._verified(robot, connection, parsed, carried, lambda: device_profile.read_ros2(device), s['id'])

    def _read(self, connection, deadline=None):
        if deadline is not None and deadline - time.monotonic() < READ_MIN_SECONDS:
            raise device_profile._NotRead(NO_TIME_LEFT)
        device = device_access.SSHDevice(*self.platform.ssh_edit.saved_login(connection['id']))     # issue #12
        try:
            if deadline is not None:
                if deadline - time.monotonic() < READ_MIN_SECONDS:
                    raise device_profile._NotRead(NO_TIME_LEFT)      # the login took what was left
                device.deadline = deadline          # ``hardware_init.run_command`` stops there; the read then counts as not done
            return device_profile.read_ros2(device)
        finally:
            try:
                device.close()
            except Exception as exc:                # closing a dead session must not hide the real error
                LOG.warning('closing the SSH session of a ROS 2 read failed: %s', type(exc).__name__)

    def _verified(self, robot, connection, parsed, carried, read, session_id=None):
        """``parsed`` with the motion driver problems a fresh ROS 2 read (``read()``) shows. The read is
        kept as the ROS 2 part of the robot's profile. A read that fails or cannot be judged proves
        nothing: the problems known before (``carried``) stay, with the reason in the scan's notes."""
        ros2, problem, started = None, None, time.time()
        try:
            ros2 = read()
        except (device_profile._NotRead, device_access.ConnectError) as exc:
            problem = str(exc)
        except Exception as exc:                    # paramiko / EOF / socket errors of a session that died
            LOG.warning('reading ROS 2 to verify the motion interfaces failed: %s', type(exc).__name__)
            problem = device_profile.ROS2_NO_SESSION
        if ros2 is not None:
            result = self.judge(robot, ros2, self.platform.body_types.get(robot.get('family')))
            if result['status'] in CONCLUSIVE:
                self._keep(robot, connection, lambda earlier: ros2, ros2['checked_at'])
                found = issues(result)
                self.platform.audit('device.motion_drivers_reverified', {
                    'robot_id': robot['id'], 'session_id': session_id, 'status': result['status'],
                    'missing': [issue['id'] for issue in found]}, robot.get('project_id'))
                return _merged(parsed, result, found, reverified=True)
            problem = inconclusive(ros2) or result['reason']
        problem = problem.rstrip('。')
        if problem != NO_TIME_LEFT:                 # a read that was never tried did not fail
            self._keep(robot, connection, lambda earlier: {**earlier, 'problem': problem}, started)
        return _merged(parsed, self.current(robot), carried, problem=problem, note=CARRIED.format(problem=problem))

    def _keep(self, robot, connection, change, read_at):
        """Replace the ROS 2 part of the robot's kept profile by ``change(earlier part)``. Nothing is
        written when no profile is kept over this connection, when the robot is gone, or when the kept part
        was read after ``read_at`` (a profile read that ran at the same time and finished first wins)."""
        # The lock a deletion holds from its checks to its last delete (as ``DeviceProfile._read`` does).
        with self.platform.engineering.lock:
            record, saved = self._kept(robot)
            if _ros2_of(record, saved) is None or saved.get('updated_at') != connection.get('updated_at'):
                return
            if (record['ros2'].get('checked_at') or 0) > read_at:
                return
            try:
                self.store.get('robots', robot['id'])
            except ValueError:
                return
            self.store.put(device_profile.KIND, {**record, 'ros2': change(record['ros2'])})
