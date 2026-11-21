"""The SSH sessions behind the links to the robots' management layers (issue #17, R1-09).

One session per linked robot, apart from the heartbeat's; logins off the caller's thread, growing pauses,
refused logins not repeated, idle sessions closed, and a channel to the robot's own loopback port for
each request. The rules are described in ``runtime_link``, which builds the linking API on this class.
"""
import logging
import secrets
import sqlite3
import threading
import time

from . import connector, device_access, heartbeat, runtime_forward
from .runtime_forward import DEVICE_LOOPBACK, endpoint_of, not_configured

LOG = logging.getLogger('brainos.runtime_link')

PORT_MIN, PORT_MAX = 1024, 65535     # an unprivileged port: what a management layer run by a normal user can have
RETRY_FIRST_SECONDS, RETRY_MAX_SECONDS = 2, 30
KEEP_SECONDS = 600                   # the link is opened again by itself while a request came this recently
IDLE_CLOSE_SECONDS = 600             # a session nobody used for this long is closed
KEEPALIVE_SECONDS = 15               # an idle session says so this often: firewalls keep it, a dead one shows
SILENT_SECONDS = 30                  # no channel opened for this long despite two tries: the session is replaced
STABLE_SECONDS = 30                  # a session that died sooner after its login counts as a failed login (pause grows)
JUMP_HOLD_SECONDS = 300              # a jump host that refused a login is not asked again this long
VERDICT_SECONDS = 600                # a refusal the heartbeat noted counts for the link this long
TICK_SECONDS = 1
MAX_LOGINS_AT_ONCE = 4
NO_RETRY = ('auth', 'key', 'fingerprint')
STATES = {'idle': '待连接', 'connecting': '连接中', 'up': '已连通', 'down': '已断开，自动重连中',
          'refused': '登录被拒绝，已停止自动重连', 'moved': '需要重新接入'}
SESSION_LOST = {'hop': 'connection', 'level': None, 'reason': 'unreachable', 'message': 'SSH 会话中断'}
LOGIN_FAILED = {'hop': 'connection', 'level': None, 'reason': 'unreachable', 'message': 'SSH 连接没有建立起来'}
NOT_SENT = '这次请求没有发出'
DOWN = '到设备的 SSH 通道断开了，平台正在自动重连，连上后会自己恢复，不需要手工操作。' + NOT_SENT
SLOW = '设备在 {seconds:g} 秒内没有应答通道请求（网络可能很慢），SSH 会话还在。' + NOT_SENT
CLOSING = '应用正在退出，到设备的 SSH 通道已关闭。' + NOT_SENT
REFUSED = ('SSH 登录被拒绝，平台已停止自动重连，以免账号被锁。请在“配置连接”里核对登录信息，或点“重试”。'
           + NOT_SENT)
SERVICE_DOWN = ('设备上的管理层服务没有在监听（设备本机 {port} 端口连不上）：可能还没有安装，或者没有启动。'
                + NOT_SENT)
FORBIDDEN = ('设备的 SSH 服务不允许转发连接（AllowTcpForwarding 被关闭），平台到不了设备本机的管理层服务；'
             '请让管理员在设备的 sshd 配置里允许转发（local 即可）。' + NOT_SENT)
TOKEN_CHANGED = '这台机器人的管理层接入刚刚改过（令牌换了）；请重试。' + NOT_SENT
NO_TIME = '这次请求的时限用完了，还没来得及经 SSH 通道发出（SSH 会话还在）。' + NOT_SENT
SHORTAGE = '设备的 SSH 服务暂时打不开新的通道（资源不足），SSH 会话还在；请稍后再试。' + NOT_SENT
STORE_ERROR = '读取这台机器人的记录失败（工作空间正在关闭或正忙）；请重试。' + NOT_SENT
MOVED = ('这台机器人的 SSH 连接现在指向另一台机器（主机指纹和接入管理层时不同），保存的令牌不会发给它。'
         '请在“管理层设置”里重新接入。' + NOT_SENT)


class _SessionLost(Exception):
    """The SSH session did not carry the request for a channel: it is gone."""


class _Slow(Exception):
    """The robot did not answer the request for a channel in time; the session may still be good."""


class _Retired(Exception):
    """The link was let go of (its connection was edited or deleted) while a request waited for it."""


def _valid_port(port):
    return isinstance(port, int) and not isinstance(port, bool) and PORT_MIN <= port <= PORT_MAX


def _revision(connection):
    """Which save of the connection a session belongs to: an edit is another route, login or machine."""
    return connection.get('updated_at') or connection.get('created_at')


def _alive(session):
    try:
        transport = session.client.get_transport()
        return transport is not None and transport.is_active()
    except Exception:                                 # a session that is half closed is not alive
        return False


def _close(session):
    try:
        session.close()
    except Exception as exc:                          # closing a dead session must not end anything else
        LOG.warning('closing the SSH session of a link failed: %s', type(exc).__name__)


def _keep_alive(session):
    """Every hop of the session says "still here" while idle: firewalls keep the connection, and one that
    died without a word is noticed."""
    clients = [session.client] + list(getattr(session.jump, 'clients', None) or [])
    for client in clients:
        try:
            client.get_transport().set_keepalive(KEEPALIVE_SECONDS)
        except Exception as exc:
            LOG.debug('no keepalive on a hop: %s', type(exc).__name__)


def _jump_keys(connection):
    """What identifies each jump host of a route, nearest first: the hosts it is reached through and itself."""
    keys, path = [], ()
    for jump in device_access.jumps_of(connection):
        if not (isinstance(jump, dict) and all(field in jump for field in ('host', 'port', 'username'))):
            break
        path += ((jump['host'], jump['port'], jump['username']),)
        keys.append(path)
    return keys


class _Link:
    """What is kept for one robot: the SSH session opened for its link, and how its logins went."""

    def __init__(self, robot_id, connection, since):
        self.robot_id, self.connection_id, self.revision = robot_id, connection['id'], _revision(connection)
        self.jumps = _jump_keys(connection)
        self.session = None
        self.state, self.since, self.error = 'idle', since, None
        self.lost_at = None                           # wall time since when it is not up; None while up or idle
        self.failures, self.next_try = 0, 0.0         # logins that failed in a row; monotonic time of the next try
        self.logging_in = False
        self.up_at = None                             # monotonic time the current session was logged in
        self.refused_at = None                        # wall time of a refused login
        self.used = self.answered = time.monotonic()  # the last request; the last time the robot answered one
        self.unanswered = 0                           # requests for a channel in a row that got no answer in time
        self.retired = False                          # its connection was edited, replaced or deleted


class LinkSessions:
    def __init__(self, platform, clock=time.time):
        self.platform, self.store = platform, platform.store
        self.clock = clock                            # wall-clock seconds; a test moves it instead of waiting
        self.connect = device_access.SSHDevice        # opens a session: pinned host keys, the saved login
        self.tick_seconds, self.keep_seconds, self.idle_close_seconds = TICK_SECONDS, KEEP_SECONDS, IDLE_CLOSE_SECONDS
        self._guard = threading.Condition()           # a leaf lock: never held while the store or a device is used
        self._links = {}                              # robot id -> _Link
        self._refused_jumps = {}                      # jump host (see _jump_keys) -> (monotonic time, its refusal)
        self._logins = threading.BoundedSemaphore(MAX_LOGINS_AT_ONCE)
        self.stable_seconds = STABLE_SECONDS
        self._timer, self._stop, self._timer_allowed, self._closed = None, threading.Event(), True, False
        # A connection that is replaced, edited or deleted is let go of at once, after whoever was told before.
        sessions = platform.engineering.status_sessions
        told_before = sessions.on_forget

        def forgotten(key):
            told_before(key)
            self.forget(key)
        sessions.on_forget = forgotten

    # ------------------------------------------------------------------ the timer

    def start_timer(self):
        with self._guard:
            self._timer_allowed = True
            self._ensure_timer()

    def _ensure_timer(self):
        """Under the guard. The timer runs while there is a link to look after."""
        if not self._timer_allowed or self._closed or self.running() or not self._links:
            return
        self._stop = threading.Event()
        self._timer = threading.Thread(target=self._run_timer, args=(self._stop,), daemon=True, name='nx-runtime-link')
        try:
            self._timer.start()
        except RuntimeError:                          # no thread to be had: requests still log in, the next one tries again
            LOG.warning('no thread for the timer of the links')
            self._timer = None

    def stop_timer(self):
        """Links are then looked after only when ``tick`` is called (the tests do)."""
        with self._guard:
            self._timer_allowed = False
            timer, self._timer = self._timer, None
            self._stop.set()
        if timer is not None and timer is not threading.current_thread():
            timer.join(timeout=2)

    def running(self):
        timer = self._timer
        return timer is not None and timer.is_alive()

    def _run_timer(self, stop):
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                LOG.exception('looking after the links failed')
            with self._guard:
                if not self._links and self._timer is threading.current_thread():
                    self._timer = None                # nothing to look after: the next link starts it again
                    return
            stop.wait(self.tick_seconds)

    def tick(self):
        """One look at every link: a session that died is replaced while the link is in use, and a
        session nobody used for a while is closed. Never waits for a device, never touches the store."""
        closing, moment = [], time.monotonic()
        with self._guard:
            if self._closed:
                return
            for link in list(self._links.values()):
                if link.session is not None and not _alive(link.session):
                    closing.append(self._dropped(link))
                if link.session is not None and moment - link.used >= self.idle_close_seconds:
                    closing.append(link.session)
                    if link.up_at is not None and moment - link.up_at >= self.stable_seconds:
                        link.failures = 0
                    link.session, link.lost_at, link.up_at = None, None, None
                    self._set(link, 'idle')
                elif link.session is None and link.state == 'down' and not link.logging_in:
                    if moment - link.used >= self.keep_seconds:
                        link.lost_at = None
                        self._set(link, 'idle')       # nobody needs it now: the next request logs in
                    elif moment >= link.next_try:
                        self._start_login(link)
        for session in closing:
            _close(session)

    @staticmethod
    def retry_seconds(failures):
        """The pause before the next login after this many failed in a row."""
        return min(RETRY_MAX_SECONDS, RETRY_FIRST_SECONDS * 2 ** max(failures - 1, 0))

    # ------------------------------------------------------------------ which robot, which port

    @staticmethod
    def _port(robot):
        """The port of a linked robot's management layer, or None when the record is not that of a link."""
        link = robot.get('runtime_link')
        if (robot.get('endpoint') != endpoint_of(robot['id']) or not isinstance(link, dict)
                or not robot.get('connection_id') or robot.get('sim')):
            return None
        return link['port'] if _valid_port(link.get('port')) else None

    def port_of(self, robot_id):
        """For ``runtime_forward.transport``: the port, or None when this workspace has no such linked robot."""
        try:
            return self._port(self.store.get('robots', robot_id))
        except (ValueError, sqlite3.Error):           # not a robot of this workspace, or the workspace was closed
            return None

    def _target(self, robot_id, token=None):
        """(link, port) of a linked robot, as its record and its SSH connection are saved right now.
        ``token``: the one the request carries. It must still be the robot's: a request built from a copy
        of the record read before the robot was linked again does not take its old token anywhere."""
        try:
            robot = self.store.get('robots', robot_id)
            port = self._port(robot)
            connection = self.store.get('device_connections', robot['connection_id']) if port is not None else None
        except ValueError:
            raise not_configured() from None
        except sqlite3.Error:
            raise connector.DeviceError(STORE_ERROR, code='device_link_down', sent=False) from None
        if connection is None or connection.get('robot_id') != robot_id:
            raise not_configured()
        if self._moved(robot, connection):
            raise connector.DeviceError(MOVED, code='device_link_not_configured', sent=False)
        saved = str(robot.get('token') or '').encode('utf-8', 'surrogatepass')
        if token is not None and not secrets.compare_digest(saved, str(token).encode('utf-8', 'surrogatepass')):
            raise connector.DeviceError(TOKEN_CHANGED, code='device_link_not_configured', sent=False)
        return self._link(robot_id, connection), port

    @staticmethod
    def _moved(robot, connection):
        """The SSH connection leads to another machine than the one the robot was linked on: the token
        saved for that machine is not sent to this one."""
        return robot['runtime_link'].get('fingerprint') != connection.get('fingerprint')

    def _link(self, robot_id, connection):
        """What is kept for this robot's link, for the connection as it is saved now. Nothing of an
        earlier save (another route, login or machine) is used: its session is closed."""
        retired = None
        with self._guard:
            if self._closed:
                raise connector.DeviceError(CLOSING, code='device_link_down', sent=False)
            link = self._links.get(robot_id)
            if link is not None and (link.connection_id, link.revision) != (connection['id'], _revision(connection)):
                retired, link = self._retire(link), None
            if link is None:
                link = self._links[robot_id] = _Link(robot_id, connection, self.clock())
            link.used = time.monotonic()
        if retired is not None:
            _close(retired)
        return link

    # ------------------------------------------------------------------ one request

    def open(self, robot_id, timeout, token=None):
        """A channel to the management layer of this robot, for one request (``runtime_forward``).
        ``token``: what the request will present (see ``_target``).

        Raises ``connector.DeviceError`` with ``sent=False`` when there is none to be had: the link is
        down or its login was refused, the robot's SSH service does not forward, or nothing listens
        on the port."""
        deadline = time.monotonic() + max(timeout, 0)
        link, port = self._target(robot_id, token)
        for _ in range(2):                            # a session that turns out dead is replaced once
            try:
                session = self._session(link, deadline)
            except _Retired:                          # its connection was just edited: the link of the new save
                link, port = self._target(robot_id, token)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise connector.DeviceError(NO_TIME, code='device_link_slow', sent=False)
            try:
                channel = self._channel(link, session, port, remaining)
            except _SessionLost:
                self._lost(link, session)             # opened again at once, if this request has time left
                continue
            except _Slow:
                self._unanswered(link, session)
                raise connector.DeviceError(SLOW.format(seconds=max(1, round(remaining))), code='device_link_slow', sent=False) from None
            finally:
                link.used = time.monotonic()
            return channel
        raise self._down(link)

    def _channel(self, link, session, port, seconds):
        import paramiko
        transport = session.client.get_transport()
        if transport is None:                         # closed under us
            raise _SessionLost()
        try:
            # The request for the channel is a write to the session: bounded like the request itself (runtime_forward).
            channel = runtime_forward.bounded(transport, seconds + runtime_forward.STALL_SECONDS, lambda: transport.open_channel(
                'direct-tcpip', (DEVICE_LOOPBACK, port), (DEVICE_LOOPBACK, 0), timeout=seconds))
        except paramiko.ChannelException as exc:      # the robot answered: it will not, or cannot, connect there
            link.answered, link.unanswered = time.monotonic(), 0
            if exc.code == paramiko.OPEN_FAILED_CONNECT_FAILED:
                raise connector.DeviceError(SERVICE_DOWN.format(port=port), code='device_service_down', sent=False) from None
            if exc.code == paramiko.OPEN_FAILED_RESOURCE_SHORTAGE:
                raise connector.DeviceError(SHORTAGE, code='device_link_slow', sent=False) from None
            raise connector.DeviceError(FORBIDDEN, code='device_forward_forbidden', sent=False) from None
        except (OSError, EOFError, paramiko.SSHException) as exc:
            if 'timeout' in str(exc).lower() and _alive(session):
                raise _Slow() from None
            raise _SessionLost() from None
        link.answered, link.unanswered = time.monotonic(), 0
        return channel

    def _unanswered(self, link, session):
        """A request for a channel got no answer in time. Once that happened twice in a row and nothing
        was answered for ``SILENT_SECONDS``, the session counts as dead."""
        with self._guard:
            link.unanswered += 1
            silent = time.monotonic() - link.answered >= SILENT_SECONDS
            dead = self._dropped(link) if link.session is session and link.unanswered >= 2 and silent else None
        if dead is not None:
            _close(dead)

    def _lost(self, link, session):
        with self._guard:
            dead = self._dropped(link) if link.session is session else None
        if dead is not None:
            _close(dead)

    def _session(self, link, deadline, by_hand=False):
        """The open SSH session of a link; logged in to first when there is none.

        Waits for a login that is under way or due, as long as the caller has time. Starts none that
        must not be tried. ``by_hand``: a person asked, so a refusal and the pause between tries do
        not count, for one login."""
        verdict = None if _alive_session(link) else self._heartbeat_verdict(link)     # read outside the lock
        dead, waited = [], False
        try:
            with self._guard:
                while True:
                    if self._closed:
                        raise connector.DeviceError(CLOSING, code='device_link_down', sent=False)
                    if link.retired:
                        raise _Retired()
                    if link.session is not None:
                        if _alive(link.session):
                            return link.session
                        dead.append(self._dropped(link))
                    if not link.logging_in:
                        if by_hand and waited:
                            raise self._failure(link)             # the one login a person asked for has ended
                        self._judge(link, verdict, by_hand and not waited)
                        verdict = None
                        if link.state == 'refused':
                            raise self._failure(link)
                        pause = link.next_try - time.monotonic()
                        if pause <= 0:
                            self._start_login(link)
                        elif pause > deadline - time.monotonic():
                            raise self._down(link)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise self._down(link)
                    if link.logging_in:
                        waited = True
                        self._guard.wait(remaining)
                    else:
                        self._guard.wait(min(remaining, max(link.next_try - time.monotonic(), 0)))
        finally:
            for session in dead:
                _close(session)

    def _heartbeat_verdict(self, link):
        """What the heartbeat (issue #24) last noted for this save of the connection: (the refusal if its
        last login was refused, the time of its last heartbeat); None when it noted nothing."""
        try:
            record = self.store.get(heartbeat.RECORDS, link.connection_id)
        except (ValueError, sqlite3.Error):
            return None
        if record.get('revision') != link.revision:
            return None
        error, tried = record.get('last_error'), record.get('last_attempt_at')
        recent = isinstance(tried, (int, float)) and 0 <= self.clock() - tried < VERDICT_SECONDS
        refused = error if recent and isinstance(error, dict) and error.get('reason') in NO_RETRY else None
        return refused, record.get('last_heartbeat_at')

    def _judge(self, link, verdict, by_hand):
        """Under the guard, before a login: may it be tried? Sets or lifts 'refused'."""
        moment = time.monotonic()
        if by_hand:
            for jump in link.jumps:
                self._refused_jumps.pop(jump, None)
            link.refused_at, link.next_try = None, 0.0
            if link.state == 'refused':
                self._set(link, 'down', link.error)
            return
        if link.state == 'refused':
            heard = verdict[1] if verdict else None
            if verdict and verdict[0] is None and heard is not None and link.refused_at is not None and heard > link.refused_at:
                link.refused_at, link.next_try = None, 0.0        # the robot let the heartbeat in since
                self._set(link, 'down', link.error)
            return
        held = next((self._refused_jumps[jump] for jump in link.jumps
                     if jump in self._refused_jumps and moment - self._refused_jumps[jump][0] < JUMP_HOLD_SECONDS), None)
        refusal = (verdict[0] if verdict else None) or (held[1] if held else None)
        if refusal is not None:                                   # the same login was just refused: not again
            self._refuse(link, refusal)

    def _refuse(self, link, error):
        link.refused_at = self.clock()
        link.lost_at = link.lost_at or link.refused_at
        self._set(link, 'refused', error)

    def _start_login(self, link):
        """Under the guard."""
        link.logging_in = True
        if link.state == 'idle':
            self._set(link, 'connecting')
        try:
            threading.Thread(target=self._login, args=(link,), daemon=True, name='nx-runtime-link-login').start()
        except RuntimeError:                          # no thread to be had: counted as a failed login, tried again later
            LOG.warning('no thread for the login of the link of %s', link.robot_id)
            link.logging_in = False
            link.failures += 1
            link.next_try = time.monotonic() + self.retry_seconds(link.failures)
            self._set(link, 'down', dict(LOGIN_FAILED))
            return
        self._ensure_timer()

    def _login(self, link):
        """Log in for a link, off the caller's thread: a caller that runs out of time does not end it,
        and the next request finds the session. At most ``MAX_LOGINS_AT_ONCE`` run at a time."""
        session = error = None
        gone = False
        try:
            with self._logins:
                if self._closed or link.retired:
                    return                            # waited for a slot while the link was let go of
                try:
                    connection, secret = self.platform.ssh_edit.saved_login(link.connection_id)
                except (ValueError, sqlite3.Error):   # deleted, or the workspace was closed
                    connection, gone = None, True
                if connection is not None and _revision(connection) != link.revision:
                    gone = True                       # edited meanwhile: the next request starts afresh
                elif connection is not None:
                    with self._guard:
                        self._judge(link, None, False)    # a jump host on its route refused someone else meanwhile
                        held = link.state == 'refused'
                    if not held:
                        try:
                            session = self.connect(connection, secret)
                        except device_access.ConnectError as exc:
                            error = {'hop': exc.hop, 'level': exc.level, 'reason': exc.reason, 'message': str(exc)}
                        except Exception as exc:      # paramiko / EOF / socket errors outside the phases ConnectError names
                            LOG.warning('the SSH login of the link of %s failed: %s', link.robot_id, type(exc).__name__)
                            error = dict(LOGIN_FAILED)
            if session is not None:
                _keep_alive(session)
        except Exception:                             # whatever went wrong, the link must not stay "logging in" for ever
            LOG.exception('the login of the link of %s failed', link.robot_id)
            error = error or dict(LOGIN_FAILED)
        finally:
            session = self._logged_in(link, session, error, gone)
        if session is not None:
            _close(session)                           # quitting, or the connection changed while this login ran

    def _logged_in(self, link, session, error, gone):
        """The outcome of a login. Returns a session the link did not take, for the caller to close."""
        retired = None
        with self._guard:
            link.logging_in = False
            if session is not None and not (self._closed or link.retired):
                link.session, session = session, None
                link.up_at, link.next_try, link.lost_at, link.refused_at = time.monotonic(), 0.0, None, None
                link.answered, link.unanswered = time.monotonic(), 0
                for jump in link.jumps:
                    self._refused_jumps.pop(jump, None)
                self._set(link, 'up')
            elif error is not None and not link.retired:
                link.failures += 1
                link.next_try = time.monotonic() + self.retry_seconds(link.failures)
                link.lost_at = link.lost_at or self.clock()
                if error['reason'] in NO_RETRY:
                    if error['hop'] == 'jump' and 0 < (error.get('level') or 1) <= len(link.jumps):
                        self._refused_jumps[link.jumps[(error.get('level') or 1) - 1]] = (time.monotonic(), error)
                    self._refuse(link, error)
                else:
                    self._set(link, 'down', error)
            elif gone and not link.retired:
                retired = self._retire(link)
            self._guard.notify_all()
        if retired is not None:
            _close(retired)
        return session

    def _dropped(self, link):
        """Under the guard: the session of a link is gone. Returns it, for the caller to close outside."""
        session, link.session = link.session, None
        moment = time.monotonic()
        if link.up_at is not None and moment - link.up_at < self.stable_seconds:
            # Let in, and gone again at once (a session the robot's sshd ends right after the login): counted
            # as a failed login, so that such a robot is not logged in to once a second.
            link.failures += 1
            link.next_try = moment + self.retry_seconds(link.failures)
        else:
            link.failures, link.next_try = 0, 0.0
        link.up_at = None
        link.lost_at = link.lost_at or self.clock()
        self._set(link, 'down', dict(SESSION_LOST))
        return session

    def _retire(self, link):
        """Under the guard: nothing of this link is used again. Returns its session, to close outside."""
        link.retired = True
        session, link.session = link.session, None
        if self._links.get(link.robot_id) is link:
            del self._links[link.robot_id]
        self._guard.notify_all()
        return session

    def _set(self, link, state, error=None):
        if link.state != state:
            link.since = self.clock()
        link.state, link.error = state, error

    def _failure(self, link):
        return self._refused_error(link) if link.state == 'refused' else self._down(link)

    @staticmethod
    def _reason(link):
        return f'（{link.error["message"]}）' if link.error and link.error.get('message') else ''

    def _down(self, link):
        return connector.DeviceError(DOWN + self._reason(link), code='device_link_down', sent=False)

    def _refused_error(self, link):
        return connector.DeviceError(REFUSED + self._reason(link), code='device_link_refused', sent=False)

    # ------------------------------------------------------------------ letting go

    def forget(self, connection_id):
        """A connection was replaced, edited or deleted (``StatusSessions.forget``): its link's session
        is closed at once, and the next request starts afresh. Never waits for a device, never touches
        the store and never fails: this runs inside someone else's work."""
        try:
            with self._guard:
                sessions = [self._retire(link) for link in list(self._links.values())
                            if link.connection_id == connection_id]
            for session in sessions:
                if session is not None:
                    _close(session)
        except Exception:
            LOG.exception('letting go of a link failed')

    def close(self):
        """Stop the timer and close every session. A login that still hangs is left behind on its daemon
        thread: its session is closed when it ends."""
        self.stop_timer()
        with self._guard:
            if self._closed:
                return
            self._closed = True
            sessions = [self._retire(link) for link in list(self._links.values())]
            self._guard.notify_all()
        connector.unregister_forwarder(self)
        for session in sessions:
            if session is not None:
                _close(session)


def _alive_session(link):
    session = link.session
    return session is not None and _alive(session)
