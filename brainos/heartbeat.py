"""Heartbeat of the robots reached over SSH, and their connection state (issue #24, R1-09).

Once a minute every robot with a saved SSH connection is asked one tiny fixed command over an SSH
session that is kept open for it. The time of the last answer is saved. From that time alone a
robot is

* 在线 (online) while the last heartbeat is less than three minutes old,
* 离线 (offline) once three minutes passed without one, and online again with the next heartbeat,
* 接入中 (onboarding) only at start, while the first check of a connection last heard long ago runs,
* 未接入 (not onboarded) when it has no SSH connection and its own service was never connected.

Nothing here depends on a page being open: the timer starts when the workspace is first shown and
stops when the app quits. The status page's full read of hardware and software (``device_status``)
is separate and still happens only while that page is open. The page asks for the states on their
own (``overview``); they are not part of the workspace state, which the page redraws itself from.

What keeps many robots from holding each other up:

* a kept session is checked in one lane and logins run in two others, each with its own limit.
  Robots that do not answer a login never delay the check of one that is connected, and a robot
  that was online a few minutes ago (its session dropped) logs in again in a lane of its own,
  not behind the ones that have been silent for long;
* one check of a kept session ends within ``PING_TIMEOUT_SECONDS``; a login within the limits of
  ``device_access``; a robot whose check is still running is not checked again;
* the state is worked out from the clock when it is asked for, so a check that hangs cannot keep a
  robot "online".

A session that died is opened again within the same heartbeat. A login that a machine refused
(wrong password or key, another host key) is not repeated every minute, which would lock the
account or get this computer banned: it is tried once more five minutes later (a robot that is
still starting up refuses logins for a moment) and then left alone until the connection is
edited, the user asks ("重试"), or the app starts again. A refusal by a jump host counts for every
robot behind it: they do not each try the same login.

Robots without an SSH connection get no heartbeat. One that is connected only through its own
service address (and every simulated robot) keeps the status it had before; its entry is left out.
"""
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime

from . import device_access

LOG = logging.getLogger(__name__)

INTERVAL_SECONDS = 60            # one heartbeat a minute
OFFLINE_AFTER_SECONDS = 180      # offline after three minutes without one
PING_TIMEOUT_SECONDS = 8         # one check of a kept session, from opening the channel to the answer
MAX_BEATS_AT_ONCE = 4            # kept sessions checked at the same time
MAX_LOGINS_AT_ONCE = 4           # logins running at the same time, of which ...
LOGINS_KEPT_FOR_RECENT = 2       # ... this many only for robots heard in the last few minutes
REFUSED_RETRY_SECONDS = 300      # a refused login is tried once more this much later ...
REFUSED_ATTEMPTS = 2             # ... and after this many refusals not again by itself
CLOCK_SLACK_SECONDS = 60         # a heartbeat this far "in the future" is a nudged clock, not a clock set back
TICK_SECONDS = 1                 # how often the timer looks whether a round is due
CLOSE_WAIT_SECONDS = 1           # how long quitting waits for checks that are just ending
CHECK_WAIT_SECONDS = 45          # how long a check by hand waits for its result
HEARTBEAT_COMMAND = 'true'       # run on the device: reads nothing, writes nothing, prints nothing
RECORDS = 'device_heartbeats'    # one record per connection, under the connection's id
# A login the machine refused will be refused again: see the module text.
NO_RETRY = ('auth', 'key', 'fingerprint')
STATES = {'not_onboarded': '未接入', 'onboarding': '接入中', 'online': '在线', 'offline': '离线'}
CHECKING = '正在检查连接'
NOT_ANSWERED = '设备在 {seconds:g} 秒内没有应答心跳'
LOGIN_FAILED = {'hop': 'connection', 'reason': 'unreachable', 'message': 'SSH 连接没有建立起来；请确认设备在线'}


def link_state(last_heartbeat_at, now, attempted, watching_since, offline_after=OFFLINE_AFTER_SECONDS):
    """'online', 'offline' or 'onboarding' of a robot that has an SSH connection.

    ``attempted``: a check of it has ended since the app started. Until then a heartbeat from before
    the app was closed says nothing about now, and the state is "checking" instead of "offline",
    for no longer than it takes to call any robot offline."""
    if last_heartbeat_at is not None and -CLOCK_SLACK_SECONDS <= now - last_heartbeat_at < offline_after:
        return 'online'
    if not attempted and now - watching_since < offline_after:
        return 'onboarding'
    return 'offline'


def _revision(connection):
    """Which save of the connection a heartbeat belongs to: an edit is another route, login or machine."""
    return connection.get('updated_at') or connection.get('created_at')


def _saved_at(connection):
    """When the connection was saved. It is only ever saved right after its login was tested
    (``Engineering.connect``), so that moment is its first heartbeat."""
    try:
        return datetime.fromisoformat(_revision(connection)).timestamp()
    except (TypeError, ValueError):
        return None


def _jump_addresses(connection):
    """What identifies each jump host of a connection, nearest first: a login refused at one is refused for
    every robot behind it. A jump host further along is named with the hosts it is reached through (issue
    #15: several levels), so the first one keeps the key of a connection with a single jump host."""
    keys, path = [], ('jump',)
    for jump in device_access.jumps_of(connection):
        if not (isinstance(jump, dict) and all(key in jump for key in ('host', 'port', 'username'))):
            break
        path += (jump['host'], jump['port'], jump['username'])
        keys.append(path)
    return keys


def _refused_jump(connection, error):
    """The jump host that refused, by the level the error names (1 when it names none), or None."""
    keys, level = _jump_addresses(connection), error.get('level') or 1
    return keys[level - 1] if error['hop'] == 'jump' and 0 < level <= len(keys) else None


def _fresh(robot, connection):
    return {'id': connection['id'], 'robot_id': robot['id'], 'project_id': robot.get('project_id'),
            'revision': _revision(connection), 'last_heartbeat_at': _saved_at(connection),
            'last_attempt_at': None, 'last_error': None}


def _not_onboarded(robot):
    """A robot without an SSH connection whose own service is not connected either."""
    failed = robot.get('status') == 'error' and robot.get('error')
    error = {'hop': None, 'reason': 'connect_failed', 'message': str(robot['error'])[:500]} if failed else None
    return {'state': 'not_onboarded', 'label': STATES['not_onboarded'], 'last_heartbeat_at': None, 'error': error,
            'retrying': False, 'step': None}


def _close(session):
    try:
        session.close()
    except Exception as exc:                          # closing a dead session must not end a round
        LOG.warning('closing a heartbeat session failed: %s', type(exc).__name__)


class _Lane:
    """At most ``size`` jobs at a time, on daemon threads: quitting never waits for a machine that does not answer."""

    def __init__(self, size, name):
        self.size, self.name = size, name
        self.jobs = queue.SimpleQueue()
        self.guard = threading.Lock()
        self.workers, self.closed = [], False

    def submit(self, job, *args):
        """False once the lane is closed: the job will not run."""
        with self.guard:
            if self.closed:
                return False
            self.jobs.put((job, args))
            if len(self.workers) < self.size:
                worker = threading.Thread(target=self._work, daemon=True, name=f'{self.name}-{len(self.workers)}')
                self.workers.append(worker)
                worker.start()
        return True

    def _work(self):
        while True:
            item = self.jobs.get()
            if item is None:
                return
            job, args = item
            try:
                job(*args)
            except Exception:
                LOG.exception('a heartbeat job failed')

    def close(self):
        """Jobs that have not started are dropped. Returns the workers, for the caller to wait for."""
        with self.guard:
            self.closed = True
            workers = list(self.workers)
        try:
            while True:
                self.jobs.get_nowait()
        except queue.Empty:
            pass
        for _ in workers:
            self.jobs.put(None)
        return workers


class Heartbeat:
    def __init__(self, platform, clock=time.time):
        self.platform, self.store = platform, platform.store
        self.clock = clock                            # wall-clock seconds; a test moves it instead of waiting
        self.interval, self.offline_after = INTERVAL_SECONDS, OFFLINE_AFTER_SECONDS
        self.ping_timeout, self.tick_seconds = PING_TIMEOUT_SECONDS, TICK_SECONDS
        self.connect = device_access.SSHDevice        # opens a session: pinned host keys, the saved login
        self.ping = self._ping
        self._guard = threading.Condition()           # a leaf lock: never held while the store or a device is used
        self._sessions = {}                           # connection id -> (revision, the SSH session kept for it)
        self._busy = set()                            # connections whose check is queued or running
        self._attempted = {}                          # connection id -> revision checked at least once since the start
        # Who refused a login -> {count, at, error, revision}. Keys: a connection's id (its device refused),
        # or the address of a jump host (it refused: that holds for every robot behind it).
        self._refused = {}
        self._next_round, self._sweep_due = 0.0, False
        self._started_at = clock()
        self._beats = _Lane(MAX_BEATS_AT_ONCE, 'nx-heartbeat-beat')
        self._relogins = _Lane(LOGINS_KEPT_FOR_RECENT, 'nx-heartbeat-relogin')
        self._logins = _Lane(MAX_LOGINS_AT_ONCE - LOGINS_KEPT_FOR_RECENT, 'nx-heartbeat-login')
        self._timer, self._stop, self._timer_allowed, self._closed = None, threading.Event(), True, False
        # A connection that is replaced, edited or deleted is let go of at once, like the status page's session.
        platform.engineering.status_sessions.on_forget = self.forget

    # ------------------------------------------------------------------ the timer

    def start(self):
        """Called when the workspace is shown: from now on rounds run by themselves until the app quits."""
        if self._timer_allowed:
            self.start_timer()

    def start_timer(self):
        with self._guard:
            if self._closed or self.running():
                return
            self._timer_allowed, self._stop = True, threading.Event()
            self._timer = threading.Thread(target=self._run_timer, args=(self._stop,), daemon=True, name='nx-heartbeat-timer')
            self._timer.start()

    def stop_timer(self):
        """Rounds then run only when ``tick`` is called (the tests and the browser fixture do)."""
        with self._guard:
            self._timer_allowed = False
            timer, self._timer = self._timer, None
            self._stop.set()
        if timer is not None and timer is not threading.current_thread():
            timer.join(timeout=CLOSE_WAIT_SECONDS)

    def running(self):
        timer = self._timer
        return timer is not None and timer.is_alive()

    def _run_timer(self, stop):
        """Until it is stopped. A round that fails (the store was busy, say) is logged, and the next one runs."""
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                if stop.is_set() or self._closed:     # the store was closed under the last look
                    return
                LOG.exception('a heartbeat round failed')
            stop.wait(self.tick_seconds)

    def tick(self):
        """Start a round if one is due. True if it did. Never waits for a device."""
        now = self.clock()
        with self._guard:
            if self._closed:
                return False
            set_back = now < self._next_round - self.interval      # the computer's clock was turned back
            due = now >= self._next_round or set_back
            if due:
                self._next_round = now + self.interval
            sweep, self._sweep_due = self._sweep_due, False
        if due:
            self._round(now)
        elif sweep:
            self._sweep()
        return due

    def wait_idle(self, timeout=None):
        """True once no check is queued or running."""
        with self._guard:
            return self._guard.wait_for(lambda: not self._busy, timeout)

    # ------------------------------------------------------------------ one round

    def _watched(self):
        """{connection id: (robot, connection)} of every robot that is reached over SSH now."""
        connections = {connection['id']: connection for connection in self.store.list('device_connections')}
        return {robot['connection_id']: (robot, connections[robot['connection_id']]) for robot in self.store.list('robots')
                if not robot.get('sim') and robot.get('connection_id') in connections}

    def _sweep(self):
        """Let go of what was kept for connections that no robot uses any more. Returns the ones in use."""
        with self._guard:
            kept = set(self._sessions)
        kept.update(record['id'] for record in self.store.list(RECORDS))
        watched = self._watched()                     # read last: a connection saved meanwhile is not taken for gone
        for key in kept - set(watched):
            self._release(key)
            self.store.delete(RECORDS, key)
        return watched

    def _round(self, now):
        for key, (robot, connection) in self._sweep().items():
            heard = self._saved(robot, connection)['last_heartbeat_at']
            if heard is not None and 0 <= now - heard < self.interval / 2:
                continue                              # heard a moment ago: a login that was just saved, a status read
            self._submit(key, self._recent(heard, now))

    def _recent(self, heard, now):
        """Heard in the last few minutes: online, or only just gone."""
        return heard is not None and now - heard < self.offline_after + self.interval

    def _saved(self, robot, connection, record=None):
        """The heartbeat record of this save of the connection; a fresh one when nothing was noted for it yet."""
        if record is None:
            try:
                record = self.store.get(RECORDS, connection['id'])
            except ValueError:
                record = None
        return record if record and record.get('revision') == _revision(connection) else _fresh(robot, connection)

    def _submit(self, key, recent, by_hand=False):
        with self._guard:
            if self._closed or key in self._busy:
                return False
            self._busy.add(key)
            kept = key in self._sessions
        login_lane = self._relogins if recent else self._logins
        accepted = (self._beats.submit(self._check_kept, key, login_lane, by_hand) if kept
                    else login_lane.submit(self._check_new, key, by_hand))
        if not accepted:
            self._done(key)
        return True

    def _done(self, key):
        with self._guard:
            self._busy.discard(key)
            self._guard.notify_all()

    def _login(self, key):
        """(connection, saved logins) as one edit left them, or None when the connection is gone."""
        try:
            return self.platform.ssh_edit.saved_login(key)
        except (ValueError, sqlite3.Error):
            return None

    def _check_kept(self, key, login_lane, by_hand):
        """The heartbeat of a connection whose session is open. A session that died is not a missed
        heartbeat yet: a new one is opened first, in a lane of the logins."""
        handed_over = False
        try:
            login = self._login(key)
            if login is None:
                return
            session = self._kept(key, _revision(login[0]))
            if session is not None and self._answers(session):
                return self._note(login[0], None)
            self._release(key, forget=False)
            handed_over = login_lane.submit(self._check_new, key, by_hand)
        finally:
            if not handed_over:
                self._done(key)

    def _check_new(self, key, by_hand):
        """The heartbeat of a connection without a session: log in, ask, and keep the session for the next minute."""
        session = None
        try:
            login = self._login(key)
            if login is None:
                return
            connection, secret = login
            seen = self._may_login(connection, by_hand)
            if seen is None:                          # refused before, and not to be tried again yet
                standing = self._standing(connection)
                if standing:                          # this robot's record says why, also when another robot was the one refused
                    self._note(connection, standing[-1][1]['error'], counted=False)
                return
            try:
                session = self.connect(connection, secret)
            except device_access.ConnectError as exc:
                return self._note(connection, {'hop': exc.hop, 'level': getattr(exc, 'level', None),
                                               'reason': exc.reason, 'message': str(exc)}, seen)
            except Exception as exc:                  # paramiko / EOF / socket errors outside the phases ConnectError names
                LOG.warning('the heartbeat login of %s failed: %s', key, type(exc).__name__)
                return self._note(connection, dict(LOGIN_FAILED), seen)
            if not self._answers(session):
                return self._note(connection, {'hop': 'device', 'reason': 'timeout',
                                               'message': NOT_ANSWERED.format(seconds=self.ping_timeout)}, seen)
            if self._keep(key, _revision(connection), session):
                session = None                        # kept for the next minute
                self._note(connection, None)
        finally:
            if session is not None:
                _close(session)                       # not answered, quitting, or the connection changed meanwhile
            self._done(key)

    def _answers(self, session):
        try:
            return bool(self.ping(session))
        except Exception as exc:                      # paramiko / EOF / socket errors of a dead session; no answer in time
            LOG.debug('a heartbeat was not answered: %s', type(exc).__name__)
            return False

    def _ping(self, session):
        """True if the device ran the heartbeat command and said so, within ``ping_timeout`` seconds in all."""
        deadline = time.monotonic() + self.ping_timeout
        channel = session.client.get_transport().open_session(timeout=self.ping_timeout)
        try:
            device_access.request_command(channel, HEARTBEAT_COMMAND, deadline - time.monotonic())
            # A channel that was closed under us sets this event too: only an exit status is an answer.
            return channel.status_event.wait(max(deadline - time.monotonic(), 0)) and channel.recv_exit_status() != -1
        finally:
            channel.close()

    # ------------------------------------------------------------------ refused logins

    def _standing(self, connection):
        """[(who, refusal)] in the way of a login over this connection: its device's, then its jump hosts'."""
        own, jumps = connection['id'], _jump_addresses(connection)
        with self._guard:
            device = self._refused.get(own)
            found = [(own, device)] if device and device['revision'] == _revision(connection) else []
            return found + [(jump, self._refused[jump]) for jump in jumps if jump in self._refused]

    def _may_login(self, connection, by_hand):
        """{who: refusals so far} if a login may be tried now, None while a refused one must not be repeated.
        The one later try that is allowed is taken by whoever asks first: of all the robots behind a jump
        host that refused, a single one tries again. By hand it is always allowed, once."""
        now, own = self.clock(), connection['id']
        with self._guard:
            if by_hand:
                for who in [own, *_jump_addresses(connection)]:
                    self._refused.pop(who, None)
            standing = self._standing(connection)
            if any(refusal['count'] >= REFUSED_ATTEMPTS or 0 <= now - refusal['at'] < REFUSED_RETRY_SECONDS
                   for _, refusal in standing):
                return None
            for _, refusal in standing:
                refusal['at'] = now
            return {who: refusal['count'] for who, refusal in standing}

    def _refusal(self, connection, error, seen, now):
        """Count a refused login. Logins that started together and were all refused count once."""
        jump = _refused_jump(connection, error)
        who = jump or connection['id']
        count = max(self._refused.get(who, {}).get('count', 0), seen.get(who, 0) + 1)
        self._refused[who] = {'count': count, 'at': now, 'error': error,
                              'revision': None if jump else _revision(connection)}

    # ------------------------------------------------------------------ what is kept

    def _owner(self, key, revision=None):
        """The robot that uses this connection (in this revision, if given) right now, or None."""
        try:
            connection = self.store.get('device_connections', key)
            robot = self.store.get('robots', connection.get('robot_id'))
        except (ValueError, sqlite3.Error):           # gone, or the store was closed while a check ran
            return None
        if robot.get('connection_id') != key or (revision is not None and _revision(connection) != revision):
            return None
        return robot

    def _note(self, connection, error, seen=None, counted=True):
        """Save the outcome of one check: the time of the heartbeat, or why none came. ``seen``: the refusals
        this login knew of when it started. ``counted=False``: no login was tried, the reason is an earlier one."""
        key, revision, now = connection['id'], _revision(connection), self.clock()
        with self._guard:
            if self._closed:
                return
        try:
            with self.store.atomic():
                robot = self._owner(key, revision)
                if robot is None:
                    return                            # deleted or edited while this check ran: it belongs to nothing
                record = {**self._saved(robot, connection), 'last_attempt_at': now, 'last_error': error}
                if error is None:
                    record['last_heartbeat_at'] = now
                self.store.put(RECORDS, record)
        except sqlite3.Error:                         # the store was closed while this check ran
            return
        with self._guard:
            self._attempted[key] = revision
            if error is None:
                for who in [key, *_jump_addresses(connection)]:
                    self._refused.pop(who, None)
            elif counted and error['reason'] in NO_RETRY:
                self._refusal(connection, error, seen or {}, now)

    def heard(self, connection):
        """The device has just answered something else over this connection (a read of the status page):
        that is a heartbeat too, and the page does not show "offline" next to figures it has just read.
        Never fails the read it is told of."""
        try:
            self._note(connection, None)
        except Exception:
            LOG.exception('noting an answer from a device as its heartbeat failed')

    def _kept(self, key, revision):
        """The open session of this revision of the connection; one of an earlier revision is closed."""
        with self._guard:
            kept = self._sessions.get(key)
        if kept is None or kept[0] == revision:
            return kept and kept[1]
        self._release(key, forget=False)
        return None

    def _keep(self, key, revision, session):
        """False when quitting, or when the connection was edited or deleted while its login ran."""
        with self._guard:
            if self._closed:
                return False
        if self._owner(key, revision) is None:
            return False
        with self._guard:
            if self._closed:
                return False
            replaced, self._sessions[key] = self._sessions.get(key), (revision, session)
        if replaced is not None:
            _close(replaced[1])
        return True

    def _release(self, key, forget=True):
        """Close the session kept for a connection and, with ``forget``, drop what was remembered of its checks."""
        with self._guard:
            kept = self._sessions.pop(key, None)
            if forget:
                self._attempted.pop(key, None)
                self._refused.pop(key, None)
        if kept is not None:
            _close(kept[1])

    def forget(self, key):
        """A connection was replaced, edited or deleted (``StatusSessions.forget``).

        Its kept session is closed at once. An edited connection starts afresh from the login its edit
        has just tested, and a login its jump host refused may be tried again. Never waits for a device,
        never touches the store and never fails: this runs inside someone else's work (a deletion holds
        the engine's locks around its own reads and deletes). The record of a connection that no robot
        uses any more is deleted by the timer's next look, a moment later."""
        try:
            self._release(key)
            with self._guard:
                for who in [who for who in self._refused if isinstance(who, tuple)]:
                    del self._refused[who]
                self._sweep_due = True
        except Exception:
            LOG.exception('letting go of a connection failed')

    # ------------------------------------------------------------------ what the page gets

    def links(self, robots, connections):
        """{robot id: its connection state}. ``connections``: {id: saved SSH connection}."""
        now = self.clock()
        records = {record['id']: record for record in self.store.list(RECORDS)}
        result = {}
        for robot in robots:
            connection = connections.get(robot.get('connection_id'))
            if robot.get('sim'):
                continue
            if connection is not None:
                result[robot['id']] = self._view(robot, connection, records.get(connection['id']), now)
            elif robot.get('status') != 'connected':
                result[robot['id']] = _not_onboarded(robot)
        return result

    def _view(self, robot, connection, record, now):
        record, key = self._saved(robot, connection, record or {}), connection['id']
        with self._guard:
            attempted = self._attempted.get(key) == record['revision']
        given_up = any(refusal['count'] >= REFUSED_ATTEMPTS for _, refusal in self._standing(connection))
        state = link_state(record['last_heartbeat_at'], now, attempted, self._started_at, self.offline_after)
        return {'state': state, 'label': STATES[state], 'last_heartbeat_at': record['last_heartbeat_at'],
                'error': record['last_error'], 'retrying': not given_up, 'step': CHECKING if state == 'onboarding' else None}

    def link(self, robot_id):
        """The connection state of one robot, or None when it has none (see ``links``)."""
        robot = self.store.get('robots', robot_id)
        connections = {}
        if robot.get('connection_id'):
            try:
                connections = {robot['connection_id']: self.store.get('device_connections', robot['connection_id'])}
            except ValueError:
                pass
        return self.links([robot], connections).get(robot_id)

    def snapshot(self):
        """For ``Platform.snapshot``. The workspace is being shown, so the timer starts. Only the rhythm is
        part of the state: the page redraws itself whenever its state changes, and a heartbeat a minute
        must not do that. The states themselves are asked for apart (``overview``)."""
        self.start()
        return {'heartbeat': {'interval_seconds': self.interval, 'offline_after_seconds': self.offline_after}}

    def overview(self, p):
        """``engineering/ssh/links``: the connection state of every robot of the workspace that has one."""
        connections = {connection['id']: connection for connection in self.store.list('device_connections')}
        return {'links': self.links(self.store.list('robots'), connections)}

    def check(self, p):
        """``engineering/ssh/heartbeat``: ask this robot now instead of at the next round ("重试").
        A login that was refused is tried once more. Waits for the answer, but not for ever."""
        robot, connection = self.platform.engineering._ssh_connection(p)
        key = connection['id']
        self._submit(key, recent=True, by_hand=True)  # a check that is already running is the one waited for
        with self._guard:
            finished = self._guard.wait_for(lambda: self._closed or key not in self._busy, CHECK_WAIT_SECONDS)
        return {'robot_id': robot['id'], 'link': self.link(robot['id']), 'checking': not finished}

    # ------------------------------------------------------------------ quitting

    def close(self):
        """Stop the timer, close every kept session, and wait a moment for checks that are ending.
        A login that still hangs is left behind on its daemon thread: its session is closed when it
        ends and nothing of it is saved."""
        self.stop_timer()
        with self._guard:
            if self._closed:
                return
            self._closed = True
            sessions = [session for _, session in self._sessions.values()]
            self._sessions.clear()
            self._guard.notify_all()
        workers = self._beats.close() + self._relogins.close() + self._logins.close()
        for session in sessions:
            _close(session)                           # also ends a check that is waiting on it
        deadline = time.monotonic() + CLOSE_WAIT_SECONDS
        for worker in workers:
            worker.join(timeout=max(deadline - time.monotonic(), 0))
