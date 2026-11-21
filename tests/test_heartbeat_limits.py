"""Issue #24, the heartbeat under strain: logins a machine refuses, many robots at once, and the
timer's start and end. The rule itself and the everyday cases are in tests/test_heartbeat.py.

Every machine here is a local SSH protocol fixture: nothing reaches a real one.
"""
import secrets
import sqlite3
import tempfile
import threading
import time
import unittest

from brainos import heartbeat
from brainos.service import Platform
from heartbeat_support import BEAT, MINUTE, Blocked, HeartbeatCase, until
from ssh_fixture import DEFAULT_LOGIN


class RefusedLoginTests(HeartbeatCase):
    """A wrong password tried every minute locks the account on the robot, or gets this computer banned by the
    jump host. A robot that is still starting up refuses logins too, for a moment: so one more try, later."""

    LATER = heartbeat.REFUSED_RETRY_SECONDS // MINUTE

    def refuse(self, machine=None):
        machine = machine or self.target
        machine.password = 'changed-' + secrets.token_hex(8)
        machine.drop_sessions()

    def test_a_refused_login_is_tried_once_more_later_and_then_left_alone(self):
        connection = self.connect_directly()
        self.minute()
        logins = self.target.logins()

        self.refuse()                                        # someone changed the password on the robot
        for _ in range(self.LATER - 1):
            self.minute()
        soon = (self.target.logins(), self.link(connection)['retrying'])
        self.minute()
        self.minute()
        later = self.target.logins()
        for _ in range(30):
            self.minute()

        link = self.link(connection)
        self.assertEqual(soon, (logins + 1, True), 'the refused login was repeated within minutes')
        self.assertEqual(later, logins + 2, 'the refused login was not tried once more later')
        self.assertEqual(self.target.logins(), logins + 2, 'the refused login kept being repeated')
        self.assertEqual((link['error']['hop'], link['error']['reason']), ('device', 'auth'))
        self.assertFalse(link['retrying'])
        self.assertEqual(link['state'], 'offline')

    def test_a_robot_that_refused_logins_while_it_started_up_comes_back_by_itself(self):
        connection = self.connect_directly()
        self.minute()
        real = self.target.password

        self.refuse()                                        # restarting: its SSH service answers, but lets nobody in yet
        self.minute()
        self.target.password = real                          # up again
        for _ in range(self.LATER + 1):
            self.minute()

        link = self.link(connection)
        self.assertEqual(link['state'], 'online')
        self.assertTrue(link['retrying'])
        self.assertIsNone(link['error'])

    def test_a_jump_host_that_refuses_is_not_asked_once_for_every_robot_behind_it(self):
        first = self.connect_through_jump()
        others = [self.another_robot_behind_the_jump_host() for _ in range(5)]
        self.minute()
        logins = self.jump.logins()

        self.refuse(self.jump)                               # the jump host's password was changed
        self.minute()
        at_once = self.jump.logins() - logins
        for _ in range(30):
            self.minute()

        self.assertLessEqual(at_once, heartbeat.MAX_LOGINS_AT_ONCE, 'every robot behind the jump host tried its login')
        self.assertLessEqual(self.jump.logins() - logins, at_once + 1, 'the refused jump login was repeated')
        for connection in [first, *(connection for _, connection in others)]:
            link = self.link(connection)
            self.assertEqual((link['state'], link['error']['hop'], link['error']['reason']), ('offline', 'jump', 'auth'))
            self.assertFalse(link['retrying'])

    def test_checking_by_hand_tries_once_more(self):
        connection = self.connect_directly()
        self.minute()
        self.refuse()
        self.minute()
        logins = self.target.logins()
        request = {'project_id': self.pid, 'robot_id': connection['robot_id']}

        still = self.hb.check(request)
        self.target.password = DEFAULT_LOGIN[1]              # set back on the robot
        back = self.hb.check(request)

        self.assertEqual(still['link']['error']['reason'], 'auth')
        self.assertEqual(self.target.logins(), logins + 2)
        self.assertEqual(back['robot_id'], connection['robot_id'])
        self.assertEqual(back['link']['state'], 'online')
        self.assertTrue(back['link']['retrying'])
        self.assertIsNone(back['link']['error'])

    def test_a_connection_that_was_edited_is_tried_again(self):
        connection = self.connect_directly()
        self.minute()
        self.refuse()
        for _ in range(self.LATER + 2):
            self.minute()
        self.assertFalse(self.link(connection)['retrying'])

        self.save(connection, password=self.target.password)     # "配置连接": the new password, tested and saved
        edited = self.link(connection)
        self.minute()

        self.assertEqual(edited['state'], 'online')          # saving logged in a moment ago
        self.assertTrue(edited['retrying'])
        self.assertIsNone(edited['error'])
        self.assertEqual(self.link(connection)['state'], 'online')
        self.assertAlmostEqual(self.link(connection)['last_heartbeat_at'], self.clock(), delta=1)


class CheckTests(HeartbeatCase):
    def test_a_check_by_hand_does_not_wait_for_the_next_round(self):
        connection = self.connect_directly()
        self.minute()
        self.target.cut()
        for _ in range(3):
            self.minute()
        self.assertEqual(self.link(connection)['state'], 'offline')

        self.target.restore()
        result = self.hb.check({'project_id': self.pid, 'robot_id': connection['robot_id']})

        self.assertEqual(result['link']['state'], 'online')

    def test_a_check_is_refused_for_another_project_or_a_robot_without_ssh(self):
        connection = self.connect_directly()
        other = self.platform.add_project({'name': 'Other', 'objective': 'Another project'})['id']
        plain = self.platform.add_robot({'project_id': self.pid, 'name': 'No SSH', 'family': 'quadruped'})

        with self.assertRaises(ValueError):
            self.hb.check({'project_id': other, 'robot_id': connection['robot_id']})
        with self.assertRaisesRegex(ValueError, 'SSH'):
            self.hb.check({'project_id': self.pid, 'robot_id': plain['id']})
        self.assertEqual(self.beats(), 0)


class ManyRobotsTests(HeartbeatCase):
    def blocked(self):
        logins = Blocked()
        self.addCleanup(logins.release.set)
        self.hb.connect = logins
        self.hb.ping = lambda session: True
        return logins

    def test_a_robot_that_hangs_does_not_delay_the_heartbeat_of_another(self):
        slow = self.connect_directly()
        other, quick = self.another_robot()
        self.minute()
        self.hb.ping_timeout = 4
        self.target.unanswered.add(BEAT)                     # the first robot stops answering

        self.clock.advance(MINUTE)
        self.hb.tick()

        self.assertTrue(until(lambda: other.commands.count(BEAT) == 2, 2), 'the second robot waited for the first')
        self.assertFalse(self.hb.wait_idle(0), 'the first robot should still be waited for')
        self.assertEqual(self.link(quick)['last_heartbeat_at'], self.clock())
        self.assertTrue(self.hb.wait_idle(30))
        self.assertEqual(self.link(slow)['error']['reason'], 'timeout')

    def test_no_more_logins_run_at_once_than_allowed(self):
        logins = self.blocked()
        connection = self.age(self.connect_directly())
        self.more_robots(connection, heartbeat.MAX_LOGINS_AT_ONCE + 3)
        cold = heartbeat.MAX_LOGINS_AT_ONCE - heartbeat.LOGINS_KEPT_FOR_RECENT    # none of them was heard lately

        self.hb.tick()
        self.assertTrue(until(lambda: logins.running == cold))
        time.sleep(.2)
        self.assertEqual(logins.running, cold)
        logins.release.set()

        self.assertTrue(self.hb.wait_idle(30))
        self.assertEqual(logins.most, cold)
        self.assertEqual(logins.started, heartbeat.MAX_LOGINS_AT_ONCE + 4, 'not every robot was checked')

    def test_a_robot_that_was_online_a_minute_ago_logs_in_past_the_ones_that_never_answer(self):
        healthy = self.connect_directly()
        self.minute()
        real_connect, real_ping = self.hb.connect, self.hb.ping
        logins = self.blocked()
        self.hb.ping = real_ping
        self.hb.connect = lambda c, s: (real_connect if c['id'] == healthy['id'] else logins)(c, s)
        self.more_robots(healthy, heartbeat.MAX_LOGINS_AT_ONCE + 2)      # robots that never answer the login
        self.clock.advance(MINUTE)
        self.hb.tick()
        stuck = heartbeat.MAX_LOGINS_AT_ONCE - heartbeat.LOGINS_KEPT_FOR_RECENT
        self.assertTrue(until(lambda: logins.running == stuck and self.beats() == 2))
        before = self.target.logins()

        self.target.drop_sessions()                          # its session died; the robot itself is fine
        self.clock.advance(MINUTE)
        self.hb.tick()

        self.assertTrue(until(lambda: self.beats() == 3, 5), 'its login waited behind the robots that never answer')
        self.assertEqual(self.target.logins(), before + 1)
        self.assertEqual(logins.running, stuck)
        self.assertEqual(self.link(healthy)['last_heartbeat_at'], self.clock())

    def test_a_kept_session_is_checked_while_every_login_is_stuck(self):
        healthy = self.connect_directly()
        self.minute()                                        # its session is open now
        real_connect, real_ping = self.hb.connect, self.hb.ping
        logins = self.blocked()
        self.hb.ping = real_ping
        self.hb.connect = lambda c, s: (real_connect if c['id'] == healthy['id'] else logins)(c, s)
        self.more_robots(healthy, heartbeat.MAX_LOGINS_AT_ONCE + 2)      # robots that never answer the login

        self.clock.advance(MINUTE)
        self.hb.tick()

        self.assertTrue(until(lambda: self.beats() == 2, 3), 'the reachable robot waited behind the stuck logins')
        self.assertEqual(logins.running, heartbeat.MAX_LOGINS_AT_ONCE - heartbeat.LOGINS_KEPT_FOR_RECENT)
        self.assertEqual(self.link(healthy)['last_heartbeat_at'], self.clock())

    def test_a_check_that_is_still_running_is_not_started_a_second_time(self):
        logins = self.blocked()
        self.age(self.connect_directly())

        self.hb.tick()
        self.assertTrue(until(lambda: logins.started == 1))
        self.clock.advance(MINUTE)
        self.hb.tick()
        self.clock.advance(MINUTE)
        self.hb.tick()
        time.sleep(.2)

        self.assertEqual(logins.started, 1)


class LifecycleTests(HeartbeatCase):
    def heartbeat_threads(self):
        return [thread for thread in threading.enumerate() if thread.name.startswith('nx-heartbeat') and thread.is_alive()]

    def test_the_timer_beats_without_anything_asking_for_the_status(self):
        connection = self.age(self.connect_directly())
        self.hb.clock, self.hb.interval, self.hb.tick_seconds = time.time, .2, .05

        self.hb.start_timer()

        self.assertTrue(until(lambda: self.beats() >= 3), f'only {self.beats()} heartbeats arrived')
        self.assertEqual(self.engine.status_sessions.open_connections(), [])
        self.assertEqual(self.hb.link(connection['robot_id'])['state'], 'online')

    def test_the_timer_goes_on_after_a_round_that_failed(self):
        self.age(self.connect_directly())
        self.hb.clock, self.hb.interval, self.hb.tick_seconds = time.time, .2, .05
        real_list, failures = self.store.list, []

        def flaky(kind):
            if len(failures) < 2:
                failures.append(kind)
                raise sqlite3.OperationalError('database is locked')
            return real_list(kind)

        self.store.list = flaky
        self.addCleanup(setattr, self.store, 'list', real_list)
        with self.assertLogs('brainos.heartbeat', level='ERROR'):
            self.hb.start_timer()
            self.assertTrue(until(lambda: self.beats() >= 2), 'the heartbeat stopped for good after one failed round')
        self.assertTrue(self.hb.running())

    def test_opening_the_workspace_starts_the_timer_and_closing_stops_it(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        platform = Platform(tmp.name)
        self.addCleanup(platform.close)
        self.assertFalse(platform.heartbeat.running())

        platform.snapshot()                                  # what the page asks for when it opens
        started = platform.heartbeat.running()
        platform.close()

        self.assertTrue(started)
        self.assertFalse(platform.heartbeat.running())

    def test_closing_ends_the_timer_and_every_session(self):
        connection = self.connect_directly()
        self.minute()
        sessions = [transport for transport in self.target.transports if transport.is_active()]
        self.hb.start_timer()

        self.platform.close()

        self.assertTrue(sessions)
        self.assertTrue(until(lambda: not any(transport.is_active() for transport in sessions), 5), 'a session stayed open')
        self.assertTrue(until(lambda: not self.heartbeat_threads(), 5), 'a heartbeat thread is still running')
        self.clock.advance(10 * MINUTE)
        self.assertFalse(self.hb.tick())
        self.assertEqual(self.beats(), 1)
        self.hb.forget(connection['id'])                     # still called by whatever closes later: harmless

    def test_closing_does_not_wait_for_a_login_that_hangs(self):
        logins = Blocked()
        self.addCleanup(logins.release.set)
        self.hb.connect = logins
        self.age(self.connect_directly())
        self.hb.tick()
        self.assertTrue(until(lambda: logins.running == 1))

        started = time.monotonic()
        self.hb.close()

        self.assertLess(time.monotonic() - started, 3)

    def test_a_login_that_ends_after_closing_keeps_no_session_and_writes_nothing(self):
        closed = []

        class Late(Blocked.Session):
            def close(self):
                closed.append(True)

        logins = Blocked()
        logins.Session = Late
        self.hb.connect = logins
        self.hb.ping = lambda session: True
        connection = self.age(self.connect_directly())
        self.hb.tick()
        self.assertTrue(until(lambda: logins.running == 1))

        self.hb.close()
        logins.release.set()

        self.assertTrue(until(lambda: closed), 'the session of a login that ended late was left open')
        with self.assertRaises(ValueError):
            self.record(connection)

    def test_a_login_that_ends_after_the_app_closed_its_store_leaves_no_session_open(self):
        closed = []

        class Late(Blocked.Session):
            def close(self):
                closed.append(True)

        logins = Blocked()
        logins.Session = Late
        self.hb.connect = logins
        self.hb.ping = lambda session: True
        self.age(self.connect_directly())
        self.hb.tick()
        self.assertTrue(until(lambda: logins.running == 1))

        self.platform.close()                                # the store is closed too
        with self.assertNoLogs('brainos.heartbeat', level='ERROR'):
            logins.release.set()
            self.assertTrue(until(lambda: closed), 'the session of a login that ended late was left open')

    def test_a_deleted_robot_is_let_go_of_at_once(self):
        connection = self.connect_directly()
        self.minute()
        sessions = [transport for transport in self.target.transports if transport.is_active()]

        self.platform.robot_remove.remove({'project_id': self.pid, 'robot_id': connection['robot_id']})

        self.assertTrue(until(lambda: not any(transport.is_active() for transport in sessions), 5), 'its session stayed open')
        self.assertNotIn(connection['robot_id'], self.hb.overview({})['links'])
        self.assertFalse(self.hb.tick())                     # the timer's next look, a second later: no round is due
        self.assertEqual(self.store.list(heartbeat.RECORDS), [], 'the heartbeat record of a deleted robot was kept')
        self.minute()
        self.assertEqual(self.beats(), 1, 'a deleted robot was asked again')

    def test_deleting_a_robot_does_not_wait_for_the_heartbeat_or_touch_its_records_itself(self):
        # The deletion holds the engine's locks from its first read to its last delete (issue #11). What the
        # heartbeat kept goes right after, on the timer's thread, not in the middle of someone else's work.
        connection = self.connect_directly()
        self.minute()
        real_delete, deleted = self.store.delete, []
        self.store.delete = lambda kind, key: (deleted.append(kind), real_delete(kind, key))[1]
        self.addCleanup(setattr, self.store, 'delete', real_delete)

        self.platform.robot_remove.remove({'project_id': self.pid, 'robot_id': connection['robot_id']})

        self.assertNotIn(heartbeat.RECORDS, deleted)

    def test_a_robot_deleted_while_the_app_was_closed_leaves_no_heartbeat_behind(self):
        connection = self.connect_directly()
        self.minute()
        self.store.delete('robots', connection['robot_id'])

        self.minute()

        self.assertEqual(self.store.list(heartbeat.RECORDS), [])
        self.assertEqual(self.beats(), 1)

    def test_a_connection_that_was_replaced_is_no_longer_checked(self):
        first = self.connect_directly()
        self.minute()

        other, second = self.another_robot(robot_id=first['robot_id'])       # the robot is another machine now
        self.minute()
        self.minute()

        self.assertEqual(self.beats(), 1)                    # the machine the robot used to be
        self.assertEqual(other.commands, [BEAT] * 2)
        self.assertEqual(self.link(second)['state'], 'online')
        self.assertEqual([record['id'] for record in self.store.list(heartbeat.RECORDS)], [second['id']])


if __name__ == '__main__':
    unittest.main()
