"""Helpers of the tests of issue #19: a workspace, a robot stand-in (``install_fixture.InstallDevice``)
connected over SSH directly or through a jump host, and the console's own install package (a fake one
unless a test says otherwise). Nothing here reaches a real machine."""
import json
import logging
import os
import tempfile
import time
import unittest
from unittest import mock

from brainos import runtime_package
from brainos.service import Platform
from install_fixture import InstallDevice, fake_package
from ssh_fixture import DEFAULT_LOGIN, SSHFixture
from test_ssh_jump import JUMP_PASSWORD, JUMP_USER

FAST_BUDGETS = {'check': 20, 'upload': 60, 'remove': 20, 'install': 20, 'start': 20, 'link': 20, 'clean': 20, 'look': 3,
                'restart': 20}
WAIT_SECONDS = 90


class TokenWatch(logging.Handler):
    """Every log record of the test, to look for a token in."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:                          # a record that cannot be formatted is not ours to judge
            self.lines.append(str(record.msg))


class InstallCase(unittest.TestCase):
    real = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.package = self.make_package(os.path.join(self.tmp.name, 'package'))
        patch = mock.patch.dict(os.environ, {runtime_package.ENV: str(self.package.directory)})
        patch.start()
        self.addCleanup(patch.stop)
        self.platform = Platform(os.path.join(self.tmp.name, 'state'))
        self.addCleanup(self.platform.close)
        self.platform.heartbeat.stop_timer()
        self.platform.runtime_link.stop_timer()
        self.installs = self.platform.runtime_install
        self.installs.budgets = dict(FAST_BUDGETS)
        self.installs.waits = (0.1, 0.3, 0.5)
        self.installs.poll_seconds = 0.05
        self.engine, self.store = self.platform.engineering, self.platform.store
        self.pid = self.platform.add_project({'name': 'Install', 'objective': 'Install the management layer'})['id']
        self.device = self.robot_device()
        self.logs = TokenWatch()
        logging.getLogger().addHandler(self.logs)
        self.addCleanup(logging.getLogger().removeHandler, self.logs)

    def make_package(self, directory):
        return fake_package(directory)

    def robot_device(self, **options):
        device = InstallDevice(real=self.real, **options)
        self.addCleanup(device.close)
        return device

    # --- adding the robot the way the console does ------------------------------------------

    def connect_directly(self, device=None, family='quadruped', name='Robot on the LAN'):
        device = device or self.device
        check = self.engine.scan({'project_id': self.pid, 'host': '127.0.0.1', 'port': device.port, 'username': 'robot'})
        return self.engine.connect({'project_id': self.pid, 'host_check_id': check['id'], 'trust_fingerprint': check['fingerprint'],
                                    'auth': 'password', 'password': DEFAULT_LOGIN[1], 'name': name, 'family': family,
                                    'read_roots': ['/opt/ros']})

    def jump_host(self, device=None):
        jump = SSHFixture(username=JUMP_USER, password=JUMP_PASSWORD, forward_to=[('127.0.0.1', (device or self.device).port)])
        self.addCleanup(jump.close)
        return jump

    def connect_through_jump(self, jump, device=None, family='quadruped'):
        device = device or self.device
        request = {'project_id': self.pid, 'host': '127.0.0.1', 'port': device.port, 'username': 'robot',
                   'jump': {'host': '127.0.0.1', 'port': jump.port, 'username': JUMP_USER}}
        first = self.engine.scan(request)
        second = self.engine.scan({**request, 'jump_check_id': first['id'], 'trust_jump_fingerprint': first['fingerprint'],
                                   'jump_auth': 'password', 'jump_password': JUMP_PASSWORD})
        return self.engine.connect({'project_id': self.pid, 'host_check_id': second['id'], 'trust_fingerprint': second['fingerprint'],
                                    'auth': 'password', 'password': DEFAULT_LOGIN[1], 'jump_password': JUMP_PASSWORD,
                                    'name': 'Robot behind a jump host', 'family': family, 'read_roots': ['/opt/ros']})

    # --- installing ---------------------------------------------------------------------------

    def start(self, connection, **fields):
        return self.installs.start({'project_id': self.pid, 'robot_id': connection['robot_id'], **fields})

    def wait(self, connection, seconds=WAIT_SECONDS, until=lambda view: view['state'] != 'running'):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            view = self.installs.status({})['installs'].get(connection['robot_id'])
            if view is not None and until(view):
                return view
            time.sleep(0.05)
        self.fail('the install did not end in %s s: %s' % (seconds, self.installs.status({})))

    def installing_on_the_robot(self, seconds=30):
        """Wait until the robot's install script has begun (its marker is there; ``hold-install`` keeps it there)."""
        deadline = time.monotonic() + seconds
        while self.device.read('neuxmind/.neuxmind-device-install') is None:
            self.assertLess(time.monotonic(), deadline, 'the install script did not start on the robot')
            time.sleep(0.02)

    def install(self, connection, **fields):
        self.start(connection, **fields)
        return self.wait(connection)

    def clean(self, connection):
        self.installs.clean({'project_id': self.pid, 'robot_id': connection['robot_id']})
        return self.wait(connection)

    def robot(self, connection):
        return self.store.get('robots', connection['robot_id'])

    def heartbeat_state(self, connection):
        return self.platform.heartbeat.overview({})['links'][connection['robot_id']]['state']

    def package_dir(self, package=None):
        return 'neuxmind-pkg/console/' + (package or self.package).id

    def assert_online_and_linked(self, connection, port=8790):
        robot = self.robot(connection)
        self.assertEqual(robot['status'], 'connected', robot.get('error'))
        self.assertEqual(robot['endpoint'], 'ssh-forward:' + robot['id'])
        self.assertEqual(robot['runtime_link']['port'], port)
        self.assertEqual(robot['runtime_link']['token_source'], 'device')
        self.assertEqual(self.heartbeat_state(connection), 'online')
        return robot

    def assert_no_token_anywhere(self, token, *views):
        self.assertTrue(token)
        snapshot = json.dumps(self.platform.snapshot(), ensure_ascii=False, default=str)
        self.assertNotIn(token, snapshot)
        for view in views:
            self.assertNotIn(token, json.dumps(view, ensure_ascii=False, default=str))
        self.assertNotIn(token, json.dumps(self.installs.status({}), ensure_ascii=False, default=str))
        self.assertFalse([line for line in self.logs.lines if token in line])
        events = json.dumps(self.store.list('events'), ensure_ascii=False, default=str)
        self.assertNotIn(token, events)
