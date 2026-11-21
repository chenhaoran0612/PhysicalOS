"""Issue #16 (R1-02): when 「验证连接」 is not available, and what it leaves alone.

While a robot is being onboarded (its hardware is being scanned, an investigation or a repair is
running, its device profile is being read, the first check after the start has not ended) the
operation is refused before any machine is contacted, and the page is told so that the button is
not offered. A verification uses an SSH session of its own: the sessions the heartbeat and the
status page keep are neither used nor closed.

Every machine here is a local SSH protocol fixture. Nothing in this file reaches a real machine.
"""
import http.client
import json
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import test_ssh_jump as base
from brainos import heartbeat
from brainos.errors import CodedError
from brainos.httpd import FastBindHTTPServer
from brainos.server import Application, make_handler
from brainos.service import now, uid
from ssh_fixture import DEFAULT_LOGIN, SSHFixture
from test_connection_verify import SYSTEM, VERIFY, VerifyHelpers

ROOT = Path(__file__).resolve().parents[1]
LONG_AGO = '2026-01-01T00:00:00+00:00'


class HoldCase(VerifyHelpers, base.SshCase):
    def setUp(self):
        super().setUp()
        self.connection = self.connect_directly(watch_services=base.SERVICES)
        self.robot_id = self.connection['robot_id']
        self.answer()
        self.verifier = self.platform.connection_verify

    def session(self, mode, status='running'):
        """An investigation or a repair of the robot, as the engine records one that is running."""
        record = {'id': uid('agent-session'), 'project_id': self.pid, 'robot_id': self.robot_id,
                  'connection_id': self.connection['id'], 'mode': mode, 'status': status, 'name': 'Run',
                  'events': [], 'created_at': now(), 'updated_at': now()}
        return self.store.put('agent_sessions', record)

    def refused(self, code, mention):
        """The verification is refused with ``code``; nothing reaches the device."""
        sessions = len(self.target.transports)
        with self.assertRaises(CodedError) as caught:
            self.verify(self.connection)
        self.assertEqual(caught.exception.code, code)
        self.assertIn(mention, str(caught.exception))
        self.assertEqual(len(self.target.transports), sessions)
        self.assertEqual(self.target.commands, [])
        return caught.exception


class NotWhileOnboardingTests(HoldCase):
    def test_not_while_the_hardware_is_being_scanned(self):
        self.platform.hardware.scanning.add(self.robot_id)

        self.refused('verify_while_onboarding', '扫描硬件')

        self.platform.hardware.scanning.discard(self.robot_id)
        self.assertIs(self.verify(self.connection)['ok'], True)       # and again once the scan has ended

    def test_not_while_an_investigation_or_a_repair_is_running(self):
        for mode, name in (('discovery', '调查'), ('repair', '修复')):
            for status in ('starting', 'running', 'waiting_input', 'cancelling'):
                with self.subTest(mode=mode, status=status):
                    session = self.session(mode, status)
                    self.refused('verify_while_onboarding', name)
                    self.store.delete('agent_sessions', session['id'])

    def test_a_session_that_has_ended_is_no_reason(self):
        for status in ('completed', 'failed', 'interrupted', 'needs_followup'):
            self.session('discovery', status)

        self.assertIs(self.verify(self.connection)['ok'], True)

    def test_a_session_of_another_robot_is_no_reason(self):
        other = self.session('discovery')
        self.store.put('agent_sessions', {**other, 'robot_id': 'robot-other'})

        self.assertIs(self.verify(self.connection)['ok'], True)

    def test_not_while_the_device_profile_is_being_read(self):
        self.platform.device_profile.reading.add(self.robot_id)

        self.refused('verify_while_onboarding', '设备档案')

    def test_not_while_the_heartbeats_first_check_is_still_logging_in(self):
        beat = self.platform.heartbeat
        beat.stop_timer()                                             # rounds run only when the test says so
        record = self.store.get('device_connections', self.connection['id'])
        self.store.put('device_connections', {**record, 'created_at': LONG_AGO})       # last heard long ago
        # Long after the start: the card no longer says 接入中 by the clock, but the first check has not ended.
        beat._started_at -= 2 * heartbeat.OFFLINE_AFTER_SECONDS
        self.target.auth_delay = 1.5
        self.assertTrue(beat.tick())
        self.assertTrue(beat.first_check_running(self.store.get('device_connections', self.connection['id'])))
        self.assertEqual(beat.link(self.robot_id)['state'], 'offline')
        logins = self.login_count(self.target)

        self.refused('verify_while_onboarding', '接入中')

        self.assertTrue(beat.wait_idle(20))
        self.assertEqual(self.login_count(self.target), logins + 1)  # the heartbeat's login, not a second one
        self.target.auth_delay = 0
        self.target.commands.clear()
        self.assertIs(self.verify(self.connection)['ok'], True)       # and offered again once it has ended

    def test_the_label_on_the_card_alone_holds_nothing_on_the_server(self):
        # 接入中 is worked out from the clock. With no check queued or running there is nothing to wait for.
        self.platform.heartbeat.stop_timer()
        record = self.store.get('device_connections', self.connection['id'])
        self.store.put('device_connections', {**record, 'created_at': LONG_AGO})
        self.assertEqual(self.platform.heartbeat.link(self.robot_id)['state'], 'onboarding')

        self.assertIs(self.verify(self.connection)['ok'], True)

    def test_other_work_on_the_device_can_say_that_it_is_running(self):
        installing = set()
        self.verifier.holds.append(lambda robot: '正在向这台机器人安装软件包' if robot['id'] in installing else '')
        self.assertIs(self.verify(self.connection)['ok'], True)
        self.target.commands.clear()
        installing.add(self.robot_id)

        self.refused('verify_while_onboarding', '正在向这台机器人安装软件包')

    def test_a_hold_that_fails_does_not_take_the_operation_away(self):
        def broken(robot):
            raise RuntimeError('a fault in someone else\'s check')
        self.verifier.holds.append(broken)

        with self.assertLogs('brainos.connection_verify', level='ERROR'):          # the fault is logged, not swallowed
            self.assertIs(self.verify(self.connection)['ok'], True)

    def test_one_verification_of_a_robot_at_a_time(self):
        self.target.auth_delay = 0.8                                  # the first one is still logging in
        results = []
        first = threading.Thread(target=lambda: results.append(self.verify(self.connection)))
        first.start()
        deadline = time.monotonic() + 5
        while self.robot_id not in self.verifier.verifying and time.monotonic() < deadline:
            time.sleep(0.01)

        with self.assertRaises(CodedError) as caught:
            self.verify(self.connection)
        first.join(timeout=20)

        self.assertEqual(caught.exception.code, 'verify_in_progress')
        self.assertEqual([result['ok'] for result in results], [True])
        self.assertEqual(self.target.commands, [VERIFY])              # one login, one command: the first one's
        self.assertEqual(self.verifier.verifying, set())
        self.assertIs(self.verify(self.connection)['ok'], True)       # and again afterwards

    def test_a_verification_that_fails_does_not_stay_in_the_way(self):
        self.target.close()

        self.assertIs(self.verify(self.connection)['ok'], False)
        self.assertEqual(self.verifier.verifying, set())
        self.assertIs(self.verify(self.connection)['ok'], False)

    def test_a_connection_that_goes_away_before_the_login_is_said_plainly(self):
        with mock.patch.object(self.platform.ssh_edit, 'saved_login', side_effect=ValueError('找不到 device_connections: x')):
            with self.assertRaises(ValueError) as caught:
                self.verify(self.connection)

        self.assertIn('刚刚被删除', str(caught.exception))
        self.assertNotIn('device_connections', str(caught.exception))
        self.assertEqual(self.verifier.verifying, set())              # it does not stay in the way

    def test_a_robot_deleted_while_its_verification_runs_gets_nothing_noted(self):
        self.target.auth_delay = 1.0
        results = []
        running = threading.Thread(target=lambda: results.append(self.verify(self.connection)))
        running.start()
        deadline = time.monotonic() + 5
        while self.robot_id not in self.verifier.verifying and time.monotonic() < deadline:
            time.sleep(0.01)

        self.platform.robot_remove.remove({'project_id': self.pid, 'robot_id': self.robot_id})
        running.join(timeout=30)

        self.assertEqual(len(results), 1)                             # it ended with the logins it had read
        actions = [event['action'] for event in self.store.list('events')]
        self.assertIn('robot.removed', actions)
        self.assertNotIn('device.ssh_verified', actions)
        with self.assertRaises(ValueError):
            self.store.get(heartbeat.RECORDS, self.connection['id'])
        self.assertEqual(self.verifier.verifying, set())
        self.assertTrue(self.all_closed(self.target))

    def test_a_robot_without_an_ssh_connection_has_nothing_to_verify(self):
        plain = self.platform.add_robot({'project_id': self.pid, 'name': 'Service only', 'family': 'quadruped'})

        with self.assertRaises(ValueError) as caught:
            self.platform.connection_verify.verify({'project_id': self.pid, 'robot_id': plain['id']})

        self.assertIn('没有 SSH 连接', str(caught.exception))

    def test_a_robot_of_another_project_is_not_verified(self):
        other = self.platform.add_project({'name': 'Other', 'objective': 'Another project'})['id']

        with self.assertRaises(ValueError):
            self.platform.connection_verify.verify({'project_id': other, 'robot_id': self.robot_id})

        self.assertEqual(self.target.commands, [])


class PageStateTests(HoldCase):
    """What the page is told, so that the button is not offered while the robot is being onboarded."""

    def holds(self):
        try:
            return self.platform.snapshot()['connection_verify']['holds']
        finally:
            self.platform.heartbeat.stop_timer()                      # showing the workspace starts it

    def test_nothing_is_held_while_nothing_runs(self):
        self.assertEqual(self.holds(), {})

    def test_a_scan_a_session_and_a_profile_read_are_named(self):
        self.platform.hardware.scanning.add(self.robot_id)
        self.assertIn('扫描硬件', self.holds()[self.robot_id])
        self.platform.hardware.scanning.discard(self.robot_id)

        session = self.session('repair')
        self.assertIn('修复', self.holds()[self.robot_id])
        self.store.delete('agent_sessions', session['id'])

        self.platform.device_profile.reading.add(self.robot_id)
        self.assertIn('设备档案', self.holds()[self.robot_id])
        self.platform.device_profile.reading.discard(self.robot_id)

        self.assertEqual(self.holds(), {})

    def test_what_changes_by_the_clock_is_not_part_of_the_state(self):
        # The page redraws itself whenever its state changes. The first check after the start ends by
        # itself, and a verification that is running ends by itself: the page learns of both elsewhere.
        record = self.store.get('device_connections', self.connection['id'])
        self.store.put('device_connections', {**record, 'created_at': LONG_AGO})
        self.assertEqual(self.platform.heartbeat.link(self.robot_id)['state'], 'onboarding')
        self.verifier.verifying.add(self.robot_id)

        self.assertEqual(self.holds(), {})

    def test_a_verification_does_not_change_the_holds(self):
        before = self.holds()

        self.verify(self.connection)

        self.assertEqual(self.holds(), before)


class OtherSessionsTests(HoldCase):
    """A verification logs in by itself: the sessions kept for the heartbeat and the status page stay as they are."""

    def read_status(self):
        return self.engine.status({'project_id': self.pid, 'robot_id': self.robot_id, 'quick': True})

    def test_the_status_pages_session_is_neither_used_nor_closed(self):
        self.assertIs(self.read_status()['reachable'], True)
        self.assertEqual(self.engine.status_sessions.open_connections(), [self.connection['id']])
        logins = self.login_count(self.target)

        self.verify(self.connection)

        self.assertEqual(self.login_count(self.target), logins + 1)   # its own login
        self.assertEqual(self.engine.status_sessions.open_connections(), [self.connection['id']])
        self.assertIs(self.read_status()['reachable'], True)
        self.assertEqual(self.login_count(self.target), logins + 1)   # the status page went on with its session

    def test_the_heartbeats_session_is_neither_used_nor_closed(self):
        beat = self.platform.heartbeat
        request = {'project_id': self.pid, 'robot_id': self.robot_id}
        self.assertEqual(beat.check(request)['link']['state'], 'online')      # logs in and keeps the session
        logins = self.login_count(self.target)

        self.verify(self.connection)
        self.assertEqual(beat.check(request)['link']['state'], 'online')

        self.assertEqual(self.login_count(self.target), logins + 1)   # the verification's; the heartbeat kept its own
        self.assertEqual(self.target.commands.count(heartbeat.HEARTBEAT_COMMAND), 2)
        self.assertEqual(self.target.commands.count(VERIFY), 1)

    def test_a_verification_that_passed_is_an_answer_from_the_device(self):
        record = self.store.get('device_connections', self.connection['id'])
        self.store.put('device_connections', {**record, 'created_at': LONG_AGO})
        beat = self.platform.heartbeat
        beat._started_at -= 2 * heartbeat.OFFLINE_AFTER_SECONDS       # the app has been open for a while: no longer "接入中"
        self.assertEqual(beat.link(self.robot_id)['state'], 'offline')
        before = time.time()

        self.verify(self.connection)

        link = beat.link(self.robot_id)
        self.assertEqual(link['state'], 'online')
        self.assertGreaterEqual(link['last_heartbeat_at'], before)

    def test_a_verification_that_failed_says_nothing_to_the_heartbeat(self):
        self.target.close()

        self.verify(self.connection)

        with self.assertRaises(ValueError):                           # no record: nothing was noted for this connection
            self.store.get(heartbeat.RECORDS, self.connection['id'])


class HttpTests(unittest.TestCase):
    """The route of the console, with a real session cookie."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Application(Path(self.tmp.name), '')
        self.addCleanup(self.app.close)
        self.httpd = FastBindHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.host = f'127.0.0.1:{self.httpd.server_port}'
        login = 'A1' + secrets.token_hex(8)                           # made up for this run
        account = {'email': f'verify-{secrets.token_hex(4)}@example.test', 'password': login, 'name': '王工'}
        session, user, _ = self.app.identity.sign_in(account, register=True)
        self.cookie = 'brain_session=' + session
        self.workspace = self.app.workspace(user)
        self.pid = self.workspace.add_project({'name': 'Verify', 'objective': 'over HTTP'})['id']
        self.robot = SSHFixture(command_outputs={VERIFY: SYSTEM})
        self.addCleanup(self.robot.close)
        engine = self.workspace.engineering
        check = engine.scan({'project_id': self.pid, 'host': '127.0.0.1', 'port': self.robot.port, 'username': DEFAULT_LOGIN[0]})
        self.connection = engine.connect({'project_id': self.pid, 'host_check_id': check['id'],
                                          'trust_fingerprint': check['fingerprint'], 'auth': 'password',
                                          'password': DEFAULT_LOGIN[1], 'name': 'Robot', 'family': 'quadruped'})

    def call(self, method, path, payload=None, cookie=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port, timeout=30)
        self.addCleanup(connection.close)
        headers = {'Host': self.host, 'Content-Type': 'application/json'}
        if cookie:
            headers['Cookie'] = self.cookie
        connection.request(method, path, body=None if payload is None else json.dumps(payload), headers=headers)
        response = connection.getresponse()
        return response.status, response.getheader('Content-Type'), response.read().decode()

    def verify(self, **extra):
        status, _, body = self.call('POST', '/api/engineering/ssh/verify',
                                    {'project_id': self.pid, 'robot_id': self.connection['robot_id']}, **extra)
        return status, json.loads(body)

    def test_the_route_answers_with_the_result(self):
        status, result = self.verify()

        self.assertEqual(status, 200)
        self.assertIs(result['ok'], True)
        self.assertEqual(result['system']['arch'], 'aarch64')
        self.assertIsInstance(result['duration_ms'], int)

    def test_a_connection_that_does_not_get_through_is_a_result_not_an_error(self):
        self.robot.close()

        status, result = self.verify()

        self.assertEqual(status, 200)
        self.assertIs(result['ok'], False)
        self.assertEqual(result['error']['hop'], 'device')

    def test_a_refusal_carries_its_code(self):
        self.workspace.hardware.scanning.add(self.connection['robot_id'])

        status, result = self.verify()

        self.assertEqual(status, 400)
        self.assertEqual(result['code'], 'verify_while_onboarding')
        self.assertIn('扫描硬件', result['error'])

    def test_it_needs_a_signed_in_account(self):
        status, _ = self.verify(cookie=False)

        self.assertEqual(status, 401)
        self.assertEqual(self.robot.commands, [])

    def test_the_page_loads_the_script_and_style_after_what_they_build_on(self):
        _, _, page = self.call('GET', '/')
        status, mime, script = self.call('GET', '/connection_verify.js')
        css_status, css_mime, _ = self.call('GET', '/connection_verify.css')

        self.assertIn('<script src="/connection_verify.js" defer></script>', page)
        self.assertIn('<link rel="stylesheet" href="/connection_verify.css">', page)
        self.assertLess(page.index('/heartbeat.js'), page.index('/connection_verify.js'))     # the state 接入中
        self.assertLess(page.index('/ssh_edit.js'), page.index('/connection_verify.js'))      # the button 配置连接
        self.assertEqual((status, mime), (200, 'text/javascript; charset=utf-8'))
        self.assertEqual((css_status, css_mime), (200, 'text/css; charset=utf-8'))
        self.assertIn('engine-ssh-verify', script)


class PageLogicTests(unittest.TestCase):
    def test_the_page_script(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        for script in ('tests/connection_verify_logic.cjs', 'tests/connection_verify_dialog_logic.cjs'):
            with self.subTest(script=script):
                result = subprocess.run([node, script], capture_output=True, text=True, timeout=120, cwd=str(ROOT))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class WordingTests(unittest.TestCase):
    def test_the_two_checks_are_told_apart(self):
        """The robot's own service has had a 「连接检查」 (HTTP) for long. The page says which one this is."""
        script = (ROOT / 'web' / 'connection_verify.js').read_text(encoding='utf-8')

        self.assertIn('验证连接', script)
        self.assertIn('SSH', script)
        self.assertIn('连接检查', script)                              # named, to say that it is the other one


if __name__ == '__main__':
    unittest.main()
