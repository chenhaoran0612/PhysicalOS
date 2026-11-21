"""Helpers of the heartbeat tests (issue #24): a clock the test moves, a robot whose network can be cut,
and the test case both test files build on.

Everything here is a local SSH protocol fixture. Nothing reaches a real machine.
"""
import contextlib
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

import test_ssh_edit as edit_tests
from brainos import engineering, heartbeat, ssh_edit
from ssh_fixture import DEFAULT_LOGIN, SSHFixture
from test_ssh_jump import JUMP_PASSWORD

MINUTE = 60
BEAT = heartbeat.HEARTBEAT_COMMAND


class Clock:
    """The time as the heartbeat sees it. It stands still until the test moves it."""

    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def catch_up(self):
        """Not behind the real time: a connection the product has just stamped is not saved in the future."""
        self.now = max(self.now, time.time())


class ShiftedClock:
    """The real time plus what the browser fixture was told to skip: the page shows it, so it keeps running."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.time() + self.offset

    def advance(self, seconds):
        self.offset += seconds


class FlakyDevice(SSHFixture):
    """An SSH fixture whose network can be cut and restored: the same port and host key afterwards."""

    def cut(self):
        """Nothing answers on the port any more, and every open session dies."""
        self.closed = True
        self.socket.close()
        for transport in self.transports:
            transport.close()
        self.thread.join(timeout=2)

    def restore(self):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', self.port))
        listener.listen()
        listener.settimeout(.2)
        self.socket, self.closed, self.transports = listener, False, []
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def drop_sessions(self):
        """Every open session dies, but the machine still answers new logins (its SSH service restarted)."""
        for transport in self.transports:
            transport.close()

    def logins(self):
        return len(self.auth_attempts)


def until(condition, seconds=10):
    """Poll for something another thread does. True once it holds."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(.02)
    return condition()


class HeartbeatCase(edit_tests.EditCase):
    """A robot (and a jump host) whose network the test can cut; the heartbeat runs when the test says so."""

    def setUp(self):
        super().setUp()
        self.hb = self.platform.heartbeat
        self.hb.stop_timer()                 # rounds run only when the test calls tick()
        self.clock = Clock()
        self.hb.clock = self.clock
        self.hb.ping_timeout = 3

    def fixture(self, **options):
        fixture = FlakyDevice(**options)
        self.addCleanup(fixture.close)
        return fixture

    def connect_directly(self, **extra):
        return self.caught_up(super().connect_directly(**extra))

    def connect_through_jump(self, **extra):
        return self.caught_up(super().connect_through_jump(**extra))

    def save(self, connection, **changes):
        with self.stamped():
            return super().save(connection, **changes)

    def caught_up(self, saved):
        self.clock.catch_up()                # the product stamped it with the real time
        return saved

    def stamped(self):
        """What is saved meanwhile carries the time of the test's clock, which may be minutes ahead by now."""
        moment = lambda: datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
        stack = contextlib.ExitStack()
        for module in (engineering, ssh_edit):
            stack.enter_context(mock.patch.object(module, 'now', moment))
        return stack

    def another_robot_behind_the_jump_host(self):
        """One more machine that only the jump host reaches. Returns (its fixture, its connection)."""
        machine = self.machine('dog-' + secrets.token_hex(2))
        request = {**self.request(), 'port': machine.port}
        first = self.engine.scan(request)
        second = self.engine.scan({**request, **self.jump_login(first)})
        login = {'project_id': self.pid, 'host_check_id': second['id'], 'trust_fingerprint': second['fingerprint'],
                 'auth': 'password', 'name': 'Robot behind the jump host', 'family': 'quadruped'}
        login['password'], login['jump_password'] = DEFAULT_LOGIN[1], JUMP_PASSWORD
        with self.stamped():
            return machine, self.engine.connect(login)

    def another_robot(self, **fields):
        """A second machine, connected directly. Returns (its fixture, its connection)."""
        machine = self.fixture()
        check = self.engine.scan({'project_id': self.pid, 'host': '127.0.0.1', 'port': machine.port, 'username': 'robot'})
        login = {'project_id': self.pid, 'host_check_id': check['id'], 'trust_fingerprint': check['fingerprint'],
                 'auth': 'password', 'name': 'Second robot', 'family': 'quadruped', **fields}
        login['password'] = DEFAULT_LOGIN[1]
        with self.stamped():
            return machine, self.engine.connect(login)

    def link(self, connection):
        return self.hb.link(connection['robot_id'])

    def record(self, connection):
        return self.store.get(heartbeat.RECORDS, connection['id'])

    def minute(self):
        """One minute later: the next round, waited for."""
        self.clock.advance(MINUTE)
        self.assertTrue(self.hb.tick(), 'no round was due a minute after the last one')
        self.assertTrue(self.hb.wait_idle(30), 'a heartbeat did not finish')

    def beats(self, fixture=None):
        return (fixture or self.target).commands.count(BEAT)

    def age(self, connection, seconds=86400):
        """As if the connection had been saved long ago (by an earlier version, or before the app was closed)."""
        saved = self.store.get('device_connections', connection['id'])
        then = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        saved['created_at'] = then
        if saved.get('updated_at'):
            saved['updated_at'] = then
        self.store.put('device_connections', saved)
        self.store.delete(heartbeat.RECORDS, connection['id'])
        return saved

    def more_robots(self, connection, count):
        """``count`` more robots with a copy of this connection, saved long ago."""
        secret = self.secret(connection)
        copies = []
        for index in range(count):
            robot = self.platform.add_robot({'project_id': self.pid, 'name': f'Robot {index}', 'family': 'quadruped'})
            copy = {**connection, 'id': f'connection-copy-{index}', 'robot_id': robot['id']}
            self.store.put('agent_secrets', {**secret, 'id': copy['id']})
            self.store.put('device_connections', copy)
            saved = self.store.get('robots', robot['id'])
            saved['connection_id'] = copy['id']
            self.store.put('robots', saved)
            copies.append(self.age(copy))
        return copies


class Blocked:
    """Stands in for logging in: every login waits until the test lets it go, and is counted."""

    class Session:
        def close(self):
            pass

    def __init__(self):
        self.release = threading.Event()
        self.guard = threading.Lock()
        self.running = self.most = self.started = 0

    def __call__(self, connection, secret):
        with self.guard:
            self.running += 1
            self.started += 1
            self.most = max(self.most, self.running)
        self.release.wait(30)
        with self.guard:
            self.running -= 1
        return self.Session()
