"""A robot's management layer, reached over the robot's own SSH connection (issue #17, R1-09).

Before this a person opened ``ssh -L`` by hand and typed the tunnel's address and the token into the
console. Now a robot that is connected over SSH (directly or through any number of jump hosts) is
"linked": its endpoint becomes ``ssh-forward:<robot id>`` and every request the console makes to its
management layer travels inside a channel of an SSH session to the robot (``runtime_forward``).

* **One port on the robot's own loopback.** A channel is only ever opened to ``127.0.0.1`` on the
  robot, at the port saved with the link (8790 unless a person names another unprivileged one).
  Nothing here takes an address, and nothing listens on this computer.
* **A session of its own.** The link does not share the session the heartbeat keeps (issue #24): a
  large request cannot delay a heartbeat on the same TCP stream, and a heartbeat that gives up on its
  session cannot cut a request off. The heartbeat alone says whether a robot is online.
* **It comes back by itself.** A session that died is opened again by the next request, and without
  a request by the timer while the link was used in the last ten minutes. Between two tries the pause
  grows (2 s up to 30 s); a session that dies within 30 s of its login counts as a failed try, so a
  robot that ends every session at once is not logged in to once a second. A link nobody used for ten
  minutes is closed and opened again on demand. A session whose writes stall (the far end stopped
  acknowledging) is closed by a watchdog in ``runtime_forward`` and replaced.
* **While it is down** a request fails within its own time limit with ``device_link_down`` and
  ``sent=False``: it was not sent, so nothing has to be checked on site. A request waits for a login
  that is under way or due, as long as it has time. A request that was already on its way when the
  session died ends like any lost answer: outcome unknown.
* **A refused login is not repeated** (wrong password or key, another host key): that would lock the
  account or get this computer banned. The link then waits for the connection to be edited, for the
  heartbeat to hear the robot again, or for a person to ask ("重试"). A login the heartbeat was just
  refused is not tried for the link either, and a jump host that refused one robot's login is not
  asked again for the robots behind it for five minutes.
* **The token** is typed, or read once from the token file of the standard installation with one
  fixed command over the same SSH session. It is saved where a typed token is saved and is never
  returned, logged or put into the record of the change. It belongs to the machine it was read for:
  once the robot's SSH connection leads to a machine with another host key, nothing is sent until
  the robot is linked again (and the token read from that machine).
  Before a channel is opened, the token a request carries must still be the one saved for the robot.
  Issue #19 (installing the management layer from the console) calls ``attach`` after its install
  step, with the token it generated or with none.
* **One change of a robot's record at a time.** The connection check (``Platform.connect_robot``) and
  linking or unlinking take a lock per robot, so a check under way cannot write back what a removal
  just deleted; an address typed onto a linked robot (``configure_connection``) is refused.

The state of a link changes over time, so it is not part of the workspace state the page redraws
itself from: the page asks for it apart (``overview``).
"""
import logging
import re
import sqlite3
import threading
import time

from . import connector, runtime_forward
from .errors import CodedError
from .runtime_forward import SCHEME, endpoint_of, is_forwarded  # noqa: F401  (SCHEME: part of this module's surface)
from .runtime_link_sessions import (  # noqa: F401  (RETRY_MAX_SECONDS: part of this module's surface)
    MOVED, PORT_MAX, PORT_MIN, RETRY_MAX_SECONDS, STATES, LinkSessions, _Retired, _alive, _revision, _valid_port)
from .service import now

LOG = logging.getLogger(__name__)

DEFAULT_PORT = 8790                  # where runtime/deploy/device_install.sh starts the management layer
TOKEN_FILE = '~/neuxmind/edge-token'
TOKEN_COMMAND = 'head -c 4096 "$HOME/neuxmind/edge-token"'       # product-owned: reads that one file, nothing else
TOKEN_SHAPE = re.compile(r'[A-Za-z0-9._~+/=-]{24,2048}')
TOKEN_READ_SECONDS = 30            # a slow link takes several round trips for the one short command
ATTACH_WAIT_SECONDS = 45             # how long linking waits for the SSH session
RETRY_WAIT_SECONDS = 45              # how long a try by hand waits for its result
ADDRESS_FIELDS = ('host', 'endpoint', 'address', 'target', 'url')
STALE_FIELDS = ('manifest', 'manifest_digest', 'manifest_v2', 'manifest_v2_error', 'error')
TOKEN_SLOW = ('设备没有及时读完令牌文件（网络可能很慢），SSH 会话还在。'
              '什么都没有保存，请再试一次，或者在“令牌”里手工填写')
TOKEN_NOT_FOUND = ('没有在设备上读到管理层的令牌：标准安装的令牌文件是 ' + TOKEN_FILE + '（登录账号要能读）。'
                   '请确认管理层已经装在这个账号下，或者在“令牌”里手工填写')
NO_ADDRESS = '经 SSH 接入时不需要也不能指定地址：平台只连设备本机（127.0.0.1）上的管理层端口'
BAD_PORT = f'管理层端口须为 {PORT_MIN}–{PORT_MAX} 之间的整数（默认 {DEFAULT_PORT}）'
NOT_LINKED = '这台机器人还没有经 SSH 接入管理层'
ONLY_SSH_ROBOTS = '只有通过 SSH 接入的真机才能这样接入管理层；仿真机器人和平台资源由应用自己管理'
CHANGED_MEANWHILE = '这台机器人的 SSH 连接刚刚被换掉；请重新打开后再接入'
LINKED_ADDRESS = ('这台机器人的管理层经 SSH 通道接入，不用填服务地址。要改端口或令牌请用卡片上的“管理层设置”；'
                  '确实要改成手工填地址，先在那里“移除接入”')


class RuntimeLinks(LinkSessions):
    def __init__(self, platform, clock=time.time):
        super().__init__(platform, clock)
        self._robot_locks = {}                        # robot id -> RLock: one change of its record at a time
        self._wrap_record_writers(platform)
        connector.register_forwarder(self)

    def _robot_lock(self, robot_id):
        """The lock of a robot of this workspace. A request that names no such robot gets a lock of its own:
        it changes nothing, and kept locks stay as many as there are robots."""
        try:
            self.store.get('robots', robot_id)
        except (ValueError, TypeError, sqlite3.Error):
            return threading.RLock()
        with self._guard:
            return self._robot_locks.setdefault(robot_id, threading.RLock())

    def _wrap_record_writers(self, platform):
        """Two methods of the platform read a robot, may wait for a device, and save their copy. Around a link:

        * ``connect_robot`` (the connection check) runs one at a time with linking and taking a link away,
          so a check that was under way cannot write back the address and token a removal just deleted,
          nor the empty address an attach just replaced;
        * ``configure_connection`` (an address typed by hand) refuses a linked robot: its token was read
          from that robot and must not go to an address someone types."""
        check, configure = platform.connect_robot, platform.configure_connection

        def connect_robot(p):
            with self._robot_lock(p.get('robot_id')):
                return check(p)

        def configure_connection(p):
            if p.get('kind') != 'robots':
                return configure(p)
            with self._robot_lock(p.get('id')):
                try:
                    robot = self.store.get('robots', p.get('id'))
                except (ValueError, TypeError):
                    robot = None                      # configure says what is wrong with the request
                if robot is not None and (robot.get('runtime_link') or is_forwarded(robot.get('endpoint'))):
                    raise ValueError(LINKED_ADDRESS)
                return configure(p)
        platform.connect_robot, platform.configure_connection = connect_robot, configure_connection

    # ------------------------------------------------------------------ linking a robot

    def attach(self, p):
        """``robots/runtime-link``: reach this robot's management layer over its SSH connection.

        ``port``: where the layer listens on the robot's loopback (8790 when not given). ``token``:
        the access token; when not given it is read from the standard installation's token file on
        the robot. The link is saved, then checked like any connection (``connect_robot``): a check
        that fails leaves the link in place, so that "连接检查" is all it takes once the layer runs."""
        if any(field in p for field in ADDRESS_FIELDS):
            raise ValueError(NO_ADDRESS)
        robot, connection = self.platform.engineering._ssh_connection(p)
        if robot.get('sim') or robot.get('managed_resource_id'):
            raise ValueError(ONLY_SSH_ROBOTS)
        port = p.get('port')
        port = DEFAULT_PORT if port is None else port
        if not _valid_port(port):
            raise ValueError(BAD_PORT)
        if p.get('token') in (None, ''):
            token, source = self._read_token(robot['id'], connection), 'device'
        else:
            token, source = self.platform.token(p), 'typed'
        with self._robot_lock(robot['id']):
            return self._save_link(robot, connection, port, token, source)

    def _save_link(self, robot, connection, port, token, source):
        """Under the robot's lock: save the link, then check it like any connection."""
        with self.store.atomic():
            robot = self.store.get('robots', robot['id'])
            saved = self.store.get('device_connections', connection['id'])
            if robot.get('connection_id') != connection['id'] or _revision(saved) != _revision(connection):
                raise ValueError(CHANGED_MEANWHILE)       # the token was read from, or typed for, the connection as it was
            robot.update(endpoint=endpoint_of(robot['id']), token=token, status='unconnected', capabilities=[],
                         runtime_link={'port': port, 'token_source': source, 'attached_at': now(),
                                       'fingerprint': connection['fingerprint']})
            for field in STALE_FIELDS:
                robot.pop(field, None)
            self.store.put('robots', robot)
        self.platform.audit('robot.runtime_link_attached', {'robot_id': robot['id'], 'connection_id': connection['id'],
                                                            'port': port, 'token_source': source}, robot['project_id'])
        return self.platform.connect_robot({'robot_id': robot['id']})

    def _read_token(self, robot_id, connection):
        """The token of the standard installation, read with one fixed command over the link's session.
        What the robot printed instead of a token is neither used nor shown."""
        link = self._link(robot_id, connection)
        try:
            session = self._session(link, time.monotonic() + ATTACH_WAIT_SECONDS, by_hand=True)
        except _Retired:                              # the connection was edited or deleted meanwhile
            raise ValueError(CHANGED_MEANWHILE) from None
        slow = connector.DeviceError(TOKEN_SLOW, code='device_link_slow', sent=False)
        transport = session.client.get_transport()
        try:
            result = runtime_forward.bounded(transport, TOKEN_READ_SECONDS + runtime_forward.STALL_SECONDS,
                                             lambda: session.execute(TOKEN_COMMAND, TOKEN_READ_SECONDS, redact=False))
        except Exception as exc:                      # paramiko / EOF / socket errors: a session that died, or a slow link
            LOG.warning('reading the token of %s failed: %s', robot_id, type(exc).__name__)
            if _alive(session):
                self._unanswered(link, session)       # twice, and silent for long: the session is replaced
                raise slow from None
            self._lost(link, session)
            raise self._down(link) from None
        if result['timed_out']:
            self._unanswered(link, session)
            raise slow                                # the file may well be there: not "no token"
        link.answered, link.unanswered = time.monotonic(), 0
        token = result['stdout'].strip() if result['exit_code'] == 0 else ''
        if not TOKEN_SHAPE.fullmatch(token):
            raise CodedError(TOKEN_NOT_FOUND, 'runtime_token_not_found')
        return token

    def detach(self, p):
        """``robots/runtime-link/remove``: the robot keeps its SSH connection; its link, the saved token
        and what was read through the link go."""
        robot, connection = self.platform.engineering._ssh_connection(p)
        with self._robot_lock(robot['id']), self.store.atomic():
            robot = self.store.get('robots', robot['id'])
            if robot.get('endpoint') != endpoint_of(robot['id']):
                raise ValueError(NOT_LINKED)
            robot.update(endpoint='', token='', status='unconnected', capabilities=[])
            for field in ('runtime_link', *STALE_FIELDS):
                robot.pop(field, None)
            self.store.put('robots', robot)
        self.forget(connection['id'])
        self.platform.audit('robot.runtime_link_removed', {'robot_id': robot['id'], 'connection_id': connection['id']},
                            robot['project_id'])
        return self.platform.redact(robot)

    # ------------------------------------------------------------------ what the page gets

    def _view(self, link, port):
        if link is None:
            return {'state': 'idle', 'label': STATES['idle'], 'since': None, 'down_since': None, 'error': None,
                    'port': port, 'retry_in': None}
        waiting = link.state == 'down' and time.monotonic() - link.used < self.keep_seconds
        return {'state': link.state, 'label': STATES[link.state], 'since': link.since, 'down_since': link.lost_at,
                'error': link.error, 'port': port,
                'retry_in': max(0, round(link.next_try - time.monotonic())) if waiting else None}

    def overview(self, p):
        """``robots/runtime-link/state``: the state of the link of every linked robot of the workspace."""
        links, connections = {}, {connection['id']: connection for connection in self.store.list('device_connections')}
        for robot in self.store.list('robots'):
            port, connection = self._port(robot), connections.get(robot.get('connection_id'))
            if port is None or connection is None:
                continue
            if self._moved(robot, connection):
                links[robot['id']] = {**self._view(None, port), 'state': 'moved', 'label': STATES['moved']}
                continue
            with self._guard:
                link = self._links.get(robot['id'])
                if link is not None and (link.connection_id, link.revision) != (connection['id'], _revision(connection)):
                    link = None                       # what is kept is of an earlier save of the connection
                links[robot['id']] = self._view(link, port)
        return {'links': links}

    def retry(self, p):
        """``robots/runtime-link/retry``: one login now, also after a refusal. Waits for its result."""
        robot, connection = self.platform.engineering._ssh_connection(p)
        port = self._port(robot)
        if port is None:
            raise ValueError(NOT_LINKED)
        if self._moved(robot, connection):
            raise ValueError(MOVED)
        link = self._link(robot['id'], connection)
        try:
            self._session(link, time.monotonic() + RETRY_WAIT_SECONDS, by_hand=True)
        except connector.DeviceError:
            pass                                      # the state says what happened
        except _Retired:                              # the connection was edited or deleted meanwhile: nothing was tried
            raise ValueError(CHANGED_MEANWHILE) from None
        with self._guard:
            return {'robot_id': robot['id'], 'link': self._view(link, port)}
