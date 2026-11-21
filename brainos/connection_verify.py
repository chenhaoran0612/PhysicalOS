"""「验证连接」 of a robot that is already added (issue #16, R1-02).

One verification logs in to every hop of the robot's saved SSH connection once (the jump hosts in
their order, then the device), reads the system version and the processor architecture with one
fixed command, and closes the session again. It answers with the system, the architecture and how
long it took, or with the hop it got stuck at.

* It can be repeated at will. Every verification is a login of its own, over an SSH session of
  its own: only that shows whether the saved logins still get through. The session the heartbeat
  keeps for the robot (``heartbeat``) and the one of the status page (``device_status``) are
  neither used nor closed, and nothing waits for them.
* It only reads. Nothing is written on the device (the command reads /etc/os-release and asks
  ``uname``), nothing is fetched over SFTP, the hardware is not scanned, and nothing about the
  device is kept on this computer either: the outcome goes to the audit log, and a verification
  that passed counts as an answer from the device for the heartbeat.
* It is not the 「连接检查」 of the robot's own service (``Platform.connect_robot``, HTTP): that one
  asks the runtime on the robot, this one the SSH route to the machine.
* While the robot is being onboarded (its hardware is being scanned, an investigation or a repair
  is running, its device profile is being read, the first check after the start has not ended)
  it is refused before any machine is contacted. Other work on a device says so through ``holds``.
  The guard goes one way only: a scan or a session that starts while a verification runs is not
  refused. Both only read the device, so the two can overlap without harm.
* A robot can be deleted, or its connection edited, while a verification runs. The verification
  ends with the logins it read when it started (nothing is written on the device), and its outcome
  is then neither noted in the audit log nor told to the heartbeat: it belongs to nothing any more.

No model is involved and no shell text comes from anywhere but this file.
"""
import logging
import re
import socket
import threading
import time
import unicodedata

from . import device_access
from .engineering import ACTIVE
from .errors import CodedError

LOG = logging.getLogger(__name__)

# Printed by the device in sections, so that what a login script prints first is not taken for an answer.
# Plain enough for any login shell (sh, dash, busybox, bash, zsh, csh, tcsh, a restricted bash): no
# redirection, no pipe. What goes to stderr (a device without /etc/os-release) is never read.
VERIFY_COMMAND = ("echo '##os';sed -n 's/^PRETTY_NAME=//p' /etc/os-release;"
                  "echo '##kernel';uname -sr;"
                  "echo '##arch';uname -m;"
                  "echo '##end'")
READ_TIMEOUT_SECONDS = 10        # the one command, once every hop is logged in to
FIELD_LIMIT = 120                # characters of a field that are shown
ARCHITECTURE = re.compile(r'[A-Za-z0-9_.+-]{1,40}')
SESSION_NAMES = {'discovery': '调查', 'repair': '修复'}

DEVICE = '目标机'
SCANNING = '这台机器人正在扫描硬件；扫描结束后再验证连接'
SESSION_RUNNING = '这台机器人有{kind}会话正在运行；等它结束或先停止它，再验证连接'
PROFILE_READING = '这台机器人的设备档案正在读取；读取结束后再验证连接'
FIRST_CHECK = '这台机器人还在接入中：应用启动后的第一次心跳检查还没有结束。结束后再验证连接'
IN_PROGRESS = '这台机器人的连接正在验证；等这一次结束再点'
NOT_ANSWERED = '已登录目标机，但它在 {seconds:g} 秒内没有返回系统信息'
NOT_READ = '已登录目标机，但没有读到系统版本和处理器架构（设备返回的内容不完整）'
CHANNEL_REFUSED = '已登录目标机，但它不允许打开执行命令的会话（SSH 服务限制了会话或命令）；系统信息没有读到'
SESSION_LOST = '已登录目标机，但读取系统信息时 SSH 会话中断了；没能确定是哪一跳断开的，请重试'
LOGIN_BROKE = 'SSH 连接没有建立起来，也没能确定卡在哪一跳；请重试，仍然失败时请核对连接配置'
GONE = '这台机器人刚刚被删除，或者它的 SSH 连接刚刚被换掉了；请刷新页面后再试'
NO_ROBOT = '请指定要验证连接的机器人'


class _NotRead(Exception):
    """Every hop was logged in to, but the system and the architecture did not come back."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def _field(lines):
    """The first line of a section as it may be shown: no control or format characters (bidirectional
    overrides, zero-width marks), no quotes around it (os-release may quote its value either way), not
    longer than a line."""
    if not lines:
        return ''
    shown = ''.join(c for c in lines[0] if not unicodedata.category(c).startswith('C'))
    return shown.strip().strip('"\'').strip()[:FIELD_LIMIT]


def parse_system(text):
    """{os, kernel, arch} from the output of ``VERIFY_COMMAND``, or None when it is not a complete
    answer. The text comes from the device: only the last block that starts with the command's first
    mark and runs to its end mark counts (a login script prints before the command, and nothing
    printed after the end mark is read), a field is one short line, and the architecture has to look
    like one."""
    lines = text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == '##os']
    if not starts:
        return None
    sections, current = {}, None
    for line in lines[starts[-1]:]:
        if line.startswith('##'):
            name = line[2:].strip()
            if name == 'end':
                break
            current = sections.setdefault(name, [])
        elif current is not None and line.strip():
            current.append(line)
    else:
        return None                                       # no end mark: cut short, and half an answer is not an answer
    kernel, arch = _field(sections.get('kernel')), _field(sections.get('arch'))
    system = _field(sections.get('os')) or kernel         # a device without /etc/os-release: its kernel says what it is
    if not system or not ARCHITECTURE.fullmatch(arch):
        return None
    return {'os': system, 'kernel': kernel, 'arch': arch}


def read_system(device):
    """The system and the architecture over an open SSH session: within ``READ_TIMEOUT_SECONDS`` once
    the session channel is open, which itself may take up to ``device_access.CHANNEL_TIMEOUT``."""
    import paramiko
    # Unredacted, like the basic status: the output is this module's own fields, and replacing a short
    # password inside them (a digit, say) would turn "22.04" into something else. Only parsed fields are shown.
    try:
        result = device.execute(VERIFY_COMMAND, READ_TIMEOUT_SECONDS, redact=False)
    except paramiko.ChannelException:
        raise _NotRead('refused', CHANNEL_REFUSED) from None
    except (socket.timeout, TimeoutError, paramiko.SSHException) as exc:
        if 'timeout' not in str(exc).lower() and not isinstance(exc, (socket.timeout, TimeoutError)):
            raise                                         # anything else: the session broke (``ConnectionVerify._run``)
        raise _NotRead('timeout', NOT_ANSWERED.format(seconds=READ_TIMEOUT_SECONDS)) from None
    if result['timed_out']:
        raise _NotRead('timeout', NOT_ANSWERED.format(seconds=READ_TIMEOUT_SECONDS))
    system = parse_system(result['stdout'])
    if system is None:
        raise _NotRead('not_read', NOT_READ)
    return system


def _endpoint(hop):
    return f"{hop['username']}@{hop['host']}:{hop['port']}"


def route_of(connection):
    """Every hop of a saved connection in the order it is logged in to: the jump hosts, then the device."""
    jumps = device_access.jumps_of(connection)
    hops = [{'kind': 'jump', 'level': level, 'label': device_access.jump_label(level, len(jumps)), 'endpoint': _endpoint(jump)}
            for level, jump in enumerate(jumps, 1)]
    return hops + [{'kind': 'device', 'level': None, 'label': DEVICE, 'endpoint': _endpoint(connection)}]


def _stuck_index(error, route):
    """The index of the hop a login got stuck at. A hop that would not forward the connection
    ('forbidden') is the one to fix, not the hop behind it that was never contacted."""
    index = error.level - 1 if error.hop == 'jump' else len(route) - 1
    if error.reason == 'forbidden':
        index -= 1
    return min(max(index, 0), len(route) - 1)            # whatever an error says, a hop of this route is named


def _with_states(route, stuck_at):
    """The route with what became of each hop: logged in to ('ok'), the one it got stuck at ('failed'),
    or never tried because an earlier one did not let us through ('not_reached'). ``stuck_at``: the
    index of the hop that failed; None when every hop was logged in to."""
    def state(index):
        if stuck_at is None or index < stuck_at:
            return 'ok'
        return 'failed' if index == stuck_at else 'not_reached'
    return [{**hop, 'state': state(index)} for index, hop in enumerate(route)]


class ConnectionVerify:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store
        self.connect = device_access.SSHDevice        # logs in to every hop in turn, each pinned to its confirmed host key
        self.lock = threading.Lock()
        self.verifying = set()                        # robots whose connection is being verified right now
        # Other work on a device that makes it "being onboarded" (an installation, say) adds a function
        # here: given the robot record, it returns why the connection must not be verified now, or ''.
        self.holds = []

    # ------------------------------------------------------------------ when it is not available

    def _work_in_progress(self, robot, sessions):
        """Why the robot counts as being onboarded right now, by work that starts and ends with an
        action (never by the clock alone); '' when nothing of that kind runs."""
        if robot['id'] in self.platform.hardware.scanning:
            return SCANNING
        live = self.platform.engineering.live
        running = next((s for s in sessions if s.get('robot_id') == robot['id'] and (s['status'] in ACTIVE or s['id'] in live)), None)
        if running:
            return SESSION_RUNNING.format(kind=SESSION_NAMES.get(running.get('mode'), '工程'))
        if robot['id'] in self.platform.device_profile.reading:
            return PROFILE_READING
        for hold in list(self.holds):
            try:
                reason = hold(robot)
            except Exception:                         # a fault in someone else's check does not take the operation away
                LOG.exception('a hold on verifying a connection failed')
                continue
            if reason:
                return str(reason)[:300]
        return ''

    def _first_check(self, connection):
        """接入中: the heartbeat's first check of this connection since the start is queued or running.
        That check is a login too, and the two must not double up on a machine that may refuse. Asked of
        the heartbeat itself, not read from the label on the card, which is worked out from the clock."""
        if self.platform.heartbeat.first_check_running(connection):
            return FIRST_CHECK
        return ''

    def snapshot(self, robots, sessions):
        """For ``Platform.snapshot``, from the robots and agent sessions it has just listed:
        {robot id: why 「验证连接」 is not offered right now}. Only work that starts and ends with an
        action is named here. What ends by itself (the first check after the start, a verification
        that is running) is left out: the page redraws itself whenever its state changes, and learns
        of those two elsewhere."""
        holds = {}
        for robot in robots:
            if robot.get('sim') or not robot.get('connection_id'):
                continue
            reason = self._work_in_progress(robot, sessions)
            if reason:
                holds[robot['id']] = reason
        return {'connection_verify': {'holds': holds}}

    # ------------------------------------------------------------------ one verification

    def verify(self, p):
        """``engineering/ssh/verify``: log in to every hop of the robot's saved connection once and read
        the system and the architecture. A connection that does not get through is a result, not an
        error: it says at which hop it got stuck."""
        if not isinstance(p.get('robot_id'), str) or not p['robot_id']:
            raise ValueError(NO_ROBOT)
        robot, connection = self.platform.engineering._ssh_connection(p)
        with self.lock:
            if robot['id'] in self.verifying:
                raise CodedError(IN_PROGRESS, 'verify_in_progress')
            reason = self._work_in_progress(robot, self.store.list('agent_sessions')) or self._first_check(connection)
            if reason:
                raise CodedError(reason, 'verify_while_onboarding')
            self.verifying.add(robot['id'])
        try:
            connection, result = self._run(connection['id'])
        finally:
            with self.lock:
                self.verifying.discard(robot['id'])
        result = {'robot_id': robot['id'], 'name': robot['name'], **result}
        self._note(robot, connection, result)
        return result

    def _run(self, connection_id):
        """(the connection as it was saved, the outcome). Always closes what it opened."""
        # Issue #12: the record and its logins as one and the same edit left them.
        try:
            connection, secret = self.platform.ssh_edit.saved_login(connection_id)
        except ValueError:                            # deleted or replaced since the robot was looked up
            raise ValueError(GONE) from None
        route, device, system, error, stuck_at = route_of(connection), None, None, None, None
        started, checked_at = time.monotonic(), time.time()
        try:
            device = self.connect(connection, secret)
            system = read_system(device)
        except device_access.ConnectError as exc:
            stuck_at = _stuck_index(exc, route)
            error = {'hop': exc.hop, 'level': exc.level, 'stage': 'login', 'reason': exc.reason, 'message': str(exc)}
        except _NotRead as exc:
            error = {'hop': 'device', 'level': None, 'stage': 'read', 'reason': exc.reason, 'message': str(exc)}
        except Exception as exc:                      # paramiko / EOF / socket errors outside the phases ConnectError names
            LOG.warning('verifying the SSH connection %s failed: %s', connection_id, type(exc).__name__)
            logged_in = device is not None
            error = {'hop': 'connection', 'level': None, 'stage': 'read' if logged_in else 'login',
                     'reason': 'session_lost' if logged_in else 'login_failed',
                     'message': SESSION_LOST if logged_in else LOGIN_BROKE}
        duration_ms = max(round((time.monotonic() - started) * 1000), 1)
        if device is not None:
            try:
                device.close()                        # every hop: nothing stays connected for a verification
            except Exception as exc:                  # closing a dead session must not hide the outcome
                LOG.warning('closing the SSH session of a verification failed: %s', type(exc).__name__)
        unknown = error is not None and error['hop'] == 'connection' and error['stage'] == 'login'
        hops = [{**hop, 'state': 'unknown'} for hop in route] if unknown else _with_states(route, stuck_at)
        if error is not None:
            stuck = hops[stuck_at] if stuck_at is not None else hops[-1] if error['hop'] == 'device' else None
            error['where'] = f"{stuck['label']} {stuck['endpoint']}" if stuck else None
        return connection, {'ok': error is None, 'checked_at': checked_at, 'duration_ms': duration_ms,
                            'route': hops, 'system': system, 'error': error}

    def _note(self, robot, connection, result):
        """The audit entry of one verification, and the heartbeat's share of one that passed. Neither can
        fail the verification: its outcome is already known. A robot that was deleted while it ran, or
        that uses another connection now, gets neither."""
        error = result['error'] or {}
        try:
            # Under the lock a deletion holds from its checks to its last delete (issue #11): nothing is
            # written for a robot that is gone, and nothing is written into a deletion half way.
            with self.platform.engineering.lock:
                try:
                    current = self.store.get('robots', robot['id']).get('connection_id') == connection['id']
                except ValueError:
                    current = False
                if not current:
                    return
                self.platform.audit('device.ssh_verified',
                                    {'robot_id': robot['id'], 'connection_id': connection['id'], 'ok': result['ok'],
                                     'duration_ms': result['duration_ms'], 'hops': len(result['route']),
                                     'hop': error.get('hop'), 'level': error.get('level'), 'reason': error.get('reason')},
                                    robot['project_id'])
        except Exception:
            LOG.exception('noting a verification of an SSH connection failed')
        if result['ok']:
            # Issue #24: the device has just answered over this connection, so it is not shown "离线" next
            # to a verification that passed. A verification that failed says nothing: one failed login
            # is not three minutes without a heartbeat.
            self.platform.heartbeat.heard(connection)
