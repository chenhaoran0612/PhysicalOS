"""Issue #24: a heartbeat in the background, and 未接入 / 接入中 / 在线 / 离线.

Once a minute every robot with a saved SSH connection is asked one tiny thing over a session
that stays open. A robot is offline when no heartbeat came for three minutes in a row, and
online again with the next one. The time is a clock the tests move; nobody waits three minutes.
(Refused logins, many robots at once, starting and quitting: tests/test_heartbeat_limits.py.)

Every machine here is a local SSH protocol fixture: nothing reaches a real one.
"""
import json
import secrets
import tempfile
import time
import unittest

import test_ssh_edit as edit_tests
from brainos import device_software, device_status, hardware_scan, heartbeat
from brainos.server import Application, make_handler
from brainos.service import Platform
from heartbeat_support import BEAT, MINUTE, HeartbeatCase
from ssh_fixture import DEFAULT_LOGIN
from test_product import Server
from test_ssh_jump import JUMP_PASSWORD


class LinkStateTests(unittest.TestCase):
    """The rule itself: only times go in."""

    def state(self, age, attempted=True, watched=10 * MINUTE):
        now = 1_000_000.0
        return heartbeat.link_state(now - age, now, attempted, now - watched)

    def test_a_heartbeat_a_minute_and_offline_after_three(self):
        self.assertEqual(heartbeat.INTERVAL_SECONDS, 60)
        self.assertEqual(heartbeat.OFFLINE_AFTER_SECONDS, 180)

    def test_online_while_the_last_heartbeat_is_less_than_three_minutes_old(self):
        for age in (0, 59, 120, 179.9):
            self.assertEqual(self.state(age), 'online', age)

    def test_offline_once_three_minutes_passed_without_a_heartbeat(self):
        for age in (180, 181, 3600, 86400):
            self.assertEqual(self.state(age), 'offline', age)

    def test_at_start_an_old_heartbeat_reads_as_checking_until_the_first_answer(self):
        # The app was closed for a day: nothing was listened for, so nothing is known yet.
        self.assertEqual(self.state(86400, attempted=False, watched=2), 'onboarding')

    def test_a_first_check_that_never_ends_does_not_stay_checking_for_ever(self):
        self.assertEqual(self.state(86400, attempted=False, watched=180), 'offline')

    def test_a_connection_never_heard_of_is_not_online(self):
        now = 1_000_000.0

        self.assertEqual(heartbeat.link_state(None, now, False, now - 1), 'onboarding')
        self.assertEqual(heartbeat.link_state(None, now, True, now - 1), 'offline')

    def test_a_heartbeat_from_the_future_does_not_keep_a_robot_online(self):
        # The computer's clock was set back an hour: what was heard "in an hour" says nothing about now.
        self.assertEqual(self.state(-3600), 'offline')
        self.assertEqual(self.state(-5), 'online')           # a clock that was nudged by a few seconds

    def test_every_state_has_its_name(self):
        self.assertEqual(heartbeat.STATES, {'not_onboarded': '未接入', 'onboarding': '接入中', 'online': '在线', 'offline': '离线'})


class CommandTests(unittest.TestCase):
    def test_the_heartbeat_is_one_tiny_fixed_command_that_changes_nothing(self):
        self.assertLessEqual(len(BEAT), 16)
        for word in ('sudo', 'rm', 'mv', '>', '|', ';', '&', '$', '`', 'systemctl', 'ros'):
            self.assertNotIn(word, BEAT, word)
        self.assertNotIn(BEAT, (device_status.BASE_COMMAND, device_software.SOFTWARE_EXEC, hardware_scan.SCAN_EXEC))

    def test_a_login_that_can_never_work_is_not_repeated(self):
        # A wrong password tried every minute would lock the account on the robot or get this computer banned.
        self.assertEqual(set(heartbeat.NO_RETRY), {'auth', 'key', 'fingerprint'})


class SavedConnectionTests(HeartbeatCase):
    def test_a_connection_that_was_just_saved_is_online_since_its_login(self):
        logins = self.target.logins()
        connection = self.connect_directly()

        link = self.link(connection)

        self.assertEqual((link['state'], link['label']), ('online', '在线'))
        self.assertAlmostEqual(link['last_heartbeat_at'], time.time(), delta=30)
        self.assertIsNone(link['error'])
        # Saving tested the login a moment ago: that was the heartbeat, and the first round does not log in again.
        self.hb.tick()
        self.assertTrue(self.hb.wait_idle(10))
        self.assertEqual(self.beats(), 0)
        self.assertEqual(self.target.logins(), logins + 1)

    def test_one_tiny_command_a_minute_over_a_session_that_stays_open(self):
        connection = self.connect_directly()
        logins = self.target.logins()

        for _ in range(3):
            self.minute()

        self.assertEqual(self.target.commands, [BEAT] * 3)
        self.assertEqual(self.target.logins(), logins + 1, 'the heartbeat logged in again instead of keeping its session')
        self.assertEqual(self.link(connection)['state'], 'online')

    def test_each_heartbeat_moves_the_time_of_the_last_one(self):
        connection = self.connect_directly()
        self.minute()
        first = self.link(connection)['last_heartbeat_at']

        self.minute()

        self.assertEqual(self.link(connection)['last_heartbeat_at'] - first, MINUTE)
        self.assertEqual(self.link(connection)['last_heartbeat_at'], self.clock())

    def test_the_heartbeat_never_reads_the_hardware_or_the_software(self):
        connection = self.connect_directly()
        self.target.command_outputs.update(edit_tests.FULL_STATUS_OUTPUTS)
        events = len(self.store.list('events'))

        for _ in range(4):
            self.minute()

        self.assertEqual(set(self.target.commands), {BEAT})
        self.assertEqual(self.store.list('hardware_scans'), [])
        self.assertEqual(self.platform.hardware.scanning, set())
        self.assertEqual(len(self.store.list('events')), events, 'a heartbeat wrote to the audit log')
        self.assertEqual(self.engine.status_sessions.open_connections(), [], 'the status page session was opened')
        self.assertEqual(self.link(connection)['state'], 'online')

    def test_the_time_of_the_last_heartbeat_is_saved_and_survives_a_restart(self):
        connection = self.connect_directly()
        self.minute()
        heard = self.link(connection)['last_heartbeat_at']
        self.assertEqual(self.record(connection)['last_heartbeat_at'], heard)
        self.platform.close()

        again = Platform(self.tmp.name)
        self.addCleanup(again.close)
        again.heartbeat.stop_timer()
        again.heartbeat.clock = self.clock

        link = again.heartbeat.link(connection['robot_id'])
        self.assertEqual(link['last_heartbeat_at'], heard)
        self.assertEqual(link['state'], 'online')            # heard less than three minutes ago

    def test_nothing_of_the_login_is_kept_with_the_heartbeat(self):
        connection = self.connect_through_jump()
        self.minute()

        kept = json.dumps([self.record(connection), self.hb.overview({})])

        self.assertNotIn(DEFAULT_LOGIN[1], kept)
        self.assertNotIn(JUMP_PASSWORD, kept)

    def test_through_a_jump_host_the_heartbeat_reaches_the_robot_and_runs_nothing_on_the_jump_host(self):
        connection = self.connect_through_jump()

        self.minute()
        self.minute()

        self.assertEqual(self.target.commands, [BEAT] * 2)
        self.assertEqual(self.jump.commands, [])
        self.assertEqual(self.link(connection)['state'], 'online')


class OfflineAndBackTests(HeartbeatCase):
    def test_a_robot_whose_network_is_cut_is_offline_within_three_minutes_and_back_by_itself(self):
        """The acceptance of the issue: 断开设备的网络，3 分钟内变「离线」；恢复后自动回到「在线」."""
        connection = self.connect_directly()
        self.minute()
        heard = self.link(connection)['last_heartbeat_at']
        cut_at = self.clock()

        self.target.cut()
        self.minute()                                        # one minute without an answer
        self.assertEqual(self.link(connection)['state'], 'online')
        self.minute()                                        # two
        self.assertEqual(self.link(connection)['state'], 'online')
        self.clock.advance(MINUTE - .001)
        self.assertEqual(self.link(connection)['state'], 'online', 'offline before three minutes had passed')
        self.clock.advance(.001)                             # three minutes after the last heartbeat: no round needed

        link = self.link(connection)
        self.assertEqual((link['state'], link['label']), ('offline', '离线'))
        self.assertLessEqual(self.clock() - cut_at, 180, 'it took more than three minutes to say offline')
        self.assertEqual(link['last_heartbeat_at'], heard, 'the time of the last heartbeat moved without a heartbeat')
        self.assertEqual((link['error']['hop'], link['error']['reason']), ('device', 'unreachable'))
        self.assertTrue(link['retrying'])
        self.hb.tick()
        self.assertTrue(self.hb.wait_idle(30))
        self.assertEqual(self.link(connection)['state'], 'offline')

        self.target.restore()                                # nobody touches the app
        self.minute()

        link = self.link(connection)
        self.assertEqual(link['state'], 'online')
        self.assertEqual(link['last_heartbeat_at'], self.clock())
        self.assertIsNone(link['error'])

    def test_one_missed_heartbeat_does_not_show_the_robot_offline(self):
        connection = self.connect_directly()
        self.minute()

        self.target.cut()
        self.minute()
        missed = self.link(connection)
        self.target.restore()
        self.minute()

        self.assertEqual(missed['state'], 'online')
        self.assertEqual(self.link(connection)['state'], 'online')

    def test_a_session_that_died_is_opened_again_within_the_same_heartbeat(self):
        connection = self.connect_directly()
        self.minute()
        logins = self.target.logins()

        self.target.drop_sessions()                          # the robot's SSH service restarted
        self.minute()

        link = self.link(connection)
        self.assertEqual(self.target.logins(), logins + 1)
        self.assertEqual(link['last_heartbeat_at'], self.clock())
        self.assertIsNone(link['error'])

    def test_a_jump_host_that_is_gone_is_named_as_the_hop_that_failed(self):
        connection = self.connect_through_jump()
        self.minute()

        self.jump.cut()
        for _ in range(3):
            self.minute()

        link = self.link(connection)
        self.assertEqual(link['state'], 'offline')
        self.assertEqual(link['error']['hop'], 'jump')
        self.assertIn('跳板机', link['error']['message'])

    def test_a_robot_that_does_not_answer_in_time_is_not_heard(self):
        connection = self.connect_directly()
        self.minute()
        heard = self.link(connection)['last_heartbeat_at']
        self.hb.ping_timeout = .4

        self.target.unanswered.add(BEAT)                     # it accepts the session and then says nothing
        self.minute()

        link = self.link(connection)
        self.assertEqual(link['last_heartbeat_at'], heard)
        self.assertEqual((link['error']['hop'], link['error']['reason']), ('device', 'timeout'))

    def test_a_connection_saved_long_ago_is_checked_as_soon_as_the_app_starts(self):
        connection = self.connect_directly()
        self.age(connection)

        before = self.link(connection)
        self.assertTrue(self.hb.tick())
        self.assertTrue(self.hb.wait_idle(30))

        self.assertEqual((before['state'], before['label'], before['step']), ('onboarding', '接入中', '正在检查连接'))
        self.assertEqual(self.beats(), 1)
        self.assertEqual(self.link(connection)['state'], 'online')

    def test_a_robot_that_is_off_when_the_app_starts_is_offline_after_the_first_check(self):
        connection = self.connect_directly()
        self.age(connection)
        self.target.cut()

        self.hb.tick()
        self.assertTrue(self.hb.wait_idle(30))

        link = self.link(connection)
        self.assertEqual(link['state'], 'offline')           # not "接入中" for three more minutes
        self.assertEqual(link['error']['reason'], 'unreachable')


class StatusPageTests(HeartbeatCase):
    """The status page reads over its own session, only while it is open. What it learns is not thrown away."""

    def read(self, connection):
        return self.engine.status({'project_id': self.pid, 'robot_id': connection['robot_id'], 'quick': True})

    def test_an_answer_to_the_status_page_is_a_heartbeat_too(self):
        connection = self.connect_directly()
        self.minute()
        self.target.cut()
        for _ in range(3):
            self.minute()
        self.assertEqual(self.link(connection)['state'], 'offline')
        self.target.restore()
        self.clock.advance(20)                               # the next round is not due yet

        view = self.read(connection)

        link = self.link(connection)
        self.assertTrue(view['reachable'], view)
        self.assertEqual(link['state'], 'online')            # not "offline" next to figures read a second ago
        self.assertEqual(link['last_heartbeat_at'], self.clock())
        self.assertIsNone(link['error'])

    def test_a_read_that_fails_does_not_make_the_robot_offline(self):
        connection = self.connect_directly()
        self.minute()
        before = self.link(connection)

        self.target.cut()
        view = self.read(connection)

        self.assertFalse(view['reachable'])
        self.assertEqual(self.link(connection), before)      # three minutes without a heartbeat do that, nothing else

    def test_the_status_page_keeps_its_own_session_and_the_heartbeat_its_own(self):
        connection = self.connect_directly()
        self.minute()
        self.read(connection)
        self.minute()

        self.assertEqual(self.target.commands.count(BEAT), 2)
        self.assertEqual(self.engine.status_sessions.open_connections(), [connection['id']])


class WhatThePageGetsTests(HeartbeatCase):
    """``engineering/ssh/links``: asked for by the page on its own, apart from the state it draws itself from."""

    def put(self, **fields):
        robot = self.platform.add_robot({'project_id': self.pid, 'name': 'Robot', 'family': 'quadruped'})
        saved = {**self.store.get('robots', robot['id']), **fields}
        self.store.put('robots', saved)
        return saved

    def links(self):
        return self.hb.overview({})['links']

    def test_every_robot_reached_over_ssh_has_its_state_and_last_heartbeat(self):
        connection = self.connect_directly()
        self.minute()

        link = self.links()[connection['robot_id']]

        self.assertEqual((link['state'], link['label']), ('online', '在线'))
        self.assertEqual(link['last_heartbeat_at'], self.record(connection)['last_heartbeat_at'])
        self.assertEqual(set(link), {'state', 'label', 'last_heartbeat_at', 'error', 'retrying', 'step'})
        json.dumps(self.hb.overview({}), allow_nan=False)

    def test_a_heartbeat_does_not_make_the_page_draw_itself_again(self):
        # The page redraws everything when its state changes: an open explanation closes, the keyboard focus is
        # lost. A heartbeat a minute must not do that, so nothing of it is in the state.
        connection = self.connect_directly()
        self.minute()
        before = json.dumps(self.platform.snapshot(), sort_keys=True)

        self.minute()
        self.target.cut()
        for _ in range(3):
            self.minute()

        self.assertEqual(self.link(connection)['state'], 'offline')
        self.assertEqual(json.dumps(self.platform.snapshot(), sort_keys=True), before)

    def test_the_page_is_told_the_rhythm(self):
        self.assertEqual(self.platform.snapshot()['heartbeat'], {'interval_seconds': 60, 'offline_after_seconds': 180})

    def test_a_robot_that_was_never_connected_is_not_onboarded(self):
        robot = self.put()

        link = self.links()[robot['id']]

        self.assertEqual((link['state'], link['label']), ('not_onboarded', '未接入'))
        self.assertIsNone(link['last_heartbeat_at'])
        self.assertIsNone(link['error'])

    def test_a_saved_connection_failure_is_not_onboarded_with_its_reason(self):
        # A record of an earlier version: "连接失败" with the reason the check gave.
        robot = self.put(status='error', error='设备身份不一致', endpoint='http://127.0.0.1:9')

        link = self.links()[robot['id']]

        self.assertEqual(link['state'], 'not_onboarded')
        self.assertEqual(link['error']['message'], '设备身份不一致')

    def test_saved_records_keep_their_old_status_field(self):
        connection = self.connect_directly()
        self.minute()
        service_only = self.put(status='connected', endpoint='http://127.0.0.1:9')

        robots = {robot['id']: robot for robot in self.platform.snapshot()['robots']}

        self.assertEqual(robots[connection['robot_id']]['status'], 'unconnected')     # the control channel, as before
        self.assertEqual(robots[service_only['id']]['status'], 'connected')
        self.assertNotIn('link', robots[connection['robot_id']])
        # Reached only through its own service and not over SSH: nothing sends it a heartbeat, so none is claimed.
        self.assertNotIn(service_only['id'], self.links())

    def test_a_simulated_robot_has_no_connection_state(self):
        simulated = {'id': 'robot-sim', 'project_id': self.pid, 'name': 'Sim', 'status': 'connected', 'sim': {'kind': 'quadruped'}}

        self.assertEqual(self.hb.links([simulated], {}), {})

    def test_nothing_of_a_robot_but_its_connection_state_is_sent(self):
        self.put(status='error', error='refused', endpoint='http://127.0.0.1:9', token='t-' + secrets.token_hex(8))
        token = self.store.list('robots')[0]['token']

        self.assertNotIn(token, json.dumps(self.hb.overview({})))


class ServedFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Application(self.tmp.name)
        self.addCleanup(self.app.close)
        self.server = Server(make_handler(self.app))
        self.addCleanup(self.server.close)

    def get(self, path):
        import http.client
        connection = http.client.HTTPConnection('127.0.0.1', self.server.http.server_port, timeout=10)
        self.addCleanup(connection.close)
        connection.request('GET', path, headers={'Host': f'127.0.0.1:{self.server.http.server_port}'})
        response = connection.getresponse()
        return response.status, response.getheader('Content-Type'), response.read().decode()

    def test_the_page_loads_the_heartbeat_script_and_style(self):
        _, _, page = self.get('/')
        status, mime, script = self.get('/heartbeat.js')
        css_status, css_mime, _ = self.get('/heartbeat.css')

        self.assertIn('<script src="/heartbeat.js" defer></script>', page)
        self.assertIn('<link rel="stylesheet" href="/heartbeat.css">', page)
        self.assertLess(page.index('/live_status.js'), page.index('/heartbeat.js'))      # it wraps the status page's head
        self.assertEqual((status, mime), (200, 'text/javascript; charset=utf-8'))
        self.assertEqual((css_status, css_mime), (200, 'text/css; charset=utf-8'))
        self.assertIn('engine-heartbeat-check', script)


if __name__ == '__main__':
    unittest.main()
