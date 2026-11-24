"""Issue #17 (R1-09): the link to a robot's management layer comes back by itself after it dropped.

What a request gets while the link is down, how often a login is tried, what a refused login does,
and that the link and the heartbeat (issue #24) do not hold each other up. The robot is a local SSH
protocol fixture whose network the test cuts and restores; nothing reaches a real machine.

Reaching the management layer at all: tests/test_runtime_link.py.
"""
import secrets
import time
import unittest

from brainos import connector, runtime_link
from heartbeat_support import HeartbeatCase, until
from runtime_link_support import LinkMixin
from ssh_fixture import DEFAULT_LOGIN

DOWN, REFUSED = 'device_link_down', 'device_link_refused'


class ReconnectCase(LinkMixin, HeartbeatCase):
    def linked(self):
        layer = self.install_layer()
        connection = self.connect_directly()
        self.attach(connection)
        # The sessions here die a moment after their login because the test cuts them: they are taken as
        # having lived long enough. A session the robot ends right after every login: test_runtime_link_review.
        self.links.stable_seconds = 0
        return layer, connection

    def counted_logins(self):
        """Count the logins the link tries from now on, whether or not they reach a machine."""
        tried, real = [], self.links.connect

        def connect(connection, secret):
            tried.append(connection['id'])
            return real(connection, secret)

        self.links.connect = connect
        return tried


class ReconnectTests(ReconnectCase):
    def test_a_session_that_died_is_opened_again_by_the_next_request(self):
        layer, connection = self.linked()
        logins = self.target.logins()

        self.target.drop_sessions()                  # the robot's SSH service restarted

        self.assertEqual(self.call(connection)['instance_id'], layer.instance)
        self.assertEqual(self.target.logins(), logins + 1)
        self.assertEqual(self.view(connection)['state'], 'up')

    def test_while_the_robot_cannot_be_reached_a_request_fails_soon_and_says_it_was_not_sent(self):
        layer, connection = self.linked()
        answered = len(layer.calls)
        self.target.cut()

        started = time.monotonic()
        error = self.refused(lambda: self.call(connection, timeout=3))

        self.assertLess(time.monotonic() - started, 3 + 3, 'it waited for the limits of a login instead of its own')
        self.assertEqual((error.code, error.sent, error.status), (DOWN, False, None))
        self.assertIn('自动重连', str(error))
        self.assertEqual(len(layer.calls), answered)
        view = self.view(connection)
        self.assertEqual((view['state'], view['error']['hop'], view['error']['reason']), ('down', 'device', 'unreachable'))
        self.assertIsNotNone(view['retry_in'])

    def test_the_link_comes_back_by_itself_once_the_robot_answers_again(self):
        layer, connection = self.linked()
        self.target.cut()
        self.refused(lambda: self.call(connection, timeout=.2))
        self.links.retry_seconds = lambda failures: .05          # the pause between two tries, shortened
        self.links.tick_seconds = .02

        self.target.restore()
        self.links.start_timer()                                 # nobody asks for anything from here on

        self.assertTrue(self.wait_for_state(connection, 'up'), self.view(connection))
        self.assertEqual(self.call(connection)['instance_id'], layer.instance)

    def test_a_dead_session_is_noticed_and_replaced_without_a_request(self):
        _, connection = self.linked()
        logins = self.target.logins()
        self.links.tick_seconds = .02
        self.links.start_timer()

        self.target.drop_sessions()

        self.assertTrue(until(lambda: self.target.logins() == logins + 1), 'the link did not log in again by itself')
        self.assertTrue(self.wait_for_state(connection, 'up'))

    def test_requests_while_it_is_down_do_not_each_try_a_login(self):
        _, connection = self.linked()
        self.target.cut()
        tried = self.counted_logins()

        for _ in range(20):
            error = self.refused(lambda: self.call(connection, timeout=.05))
            self.assertEqual((error.code, error.sent), (DOWN, False))

        self.assertLessEqual(len(tried), 2, 'every request tried to log in')

    def test_the_pause_between_tries_grows_and_stays_bounded(self):
        pauses = [self.links.retry_seconds(failures) for failures in range(1, 12)]

        self.assertEqual(pauses, sorted(pauses))
        self.assertLessEqual(pauses[0], 5)
        self.assertEqual(pauses[-1], runtime_link.RETRY_MAX_SECONDS)
        self.assertLessEqual(runtime_link.RETRY_MAX_SECONDS, 60)

    def test_a_request_waits_for_a_login_that_is_about_to_finish(self):
        layer, connection = self.linked()
        self.target.drop_sessions()
        self.target.auth_delay = .6                              # a slow link: the login takes a moment

        self.assertEqual(self.call(connection, timeout=8)['instance_id'], layer.instance)

    def test_a_request_cut_off_under_way_is_unknown_not_unsent(self):
        layer, connection = self.linked()
        layer.hold.clear()                                       # the management layer has the request and is working
        worker, box = self.in_background(lambda: self.call(connection, timeout=10))
        self.assertTrue(until(lambda: layer.waiting == 1))

        self.target.cut()
        worker.join(15)

        self.assertIsInstance(box.get('error'), connector.DeviceError, box)
        self.assertIsNone(box['error'].sent)
        self.assertIn(box['error'].code, ('device_unreachable', 'device_timeout'))

    def test_a_link_nobody_used_for_a_while_is_closed_and_opened_again_when_needed(self):
        layer, connection = self.linked()
        self.links.idle_close_seconds = 0

        self.links.tick()

        self.assertTrue(until(lambda: self.target.open_sessions() == 0))
        self.assertEqual(self.view(connection)['state'], 'idle')
        self.assertEqual(self.call(connection)['instance_id'], layer.instance)
        self.assertEqual(self.view(connection)['state'], 'up')


class RefusedLoginTests(ReconnectCase):
    def test_a_refused_login_is_not_tried_again_by_itself(self):
        _, connection = self.linked()
        self.target.password = secrets.token_hex(8)              # the password was changed on the robot
        self.target.drop_sessions()
        attempts = self.target.logins()

        for _ in range(4):
            error = self.refused(lambda: self.call(connection, timeout=2))
            self.assertEqual((error.code, error.sent), (REFUSED, False))
            self.links.tick()

        self.assertEqual(self.target.logins(), attempts + 1, 'a refused login was repeated')
        view = self.view(connection)
        self.assertEqual((view['state'], view['error']['reason'], view['retry_in']), ('refused', 'auth', None))

    def test_asking_by_hand_tries_once_more(self):
        layer, connection = self.linked()
        self.target.password = secrets.token_hex(8)
        self.target.drop_sessions()
        self.refused(lambda: self.call(connection, timeout=2))
        ask = {'project_id': self.pid, 'robot_id': connection['robot_id']}

        still = self.links.retry(ask)
        self.target.password = DEFAULT_LOGIN[1]                  # put right on the robot
        back = self.links.retry(ask)

        self.assertEqual(still['link']['state'], 'refused')
        self.assertEqual(back['link']['state'], 'up')
        self.assertEqual(self.call(connection)['instance_id'], layer.instance)

    def test_a_login_the_heartbeat_was_refused_is_not_tried_again_for_the_link(self):
        _, connection = self.linked()
        self.target.password = secrets.token_hex(8)
        self.target.drop_sessions()
        self.minute()                                            # the heartbeat logs in, and is refused
        self.assertEqual(self.link(connection)['error']['reason'], 'auth')
        attempts = self.target.logins()

        error = self.refused(lambda: self.call(connection, timeout=2))

        self.assertEqual(error.code, REFUSED)
        self.assertEqual(self.target.logins(), attempts, 'the link repeated a login the heartbeat was just refused')

    def test_once_the_heartbeat_hears_the_robot_again_the_link_tries_again(self):
        layer, connection = self.linked()
        self.target.password = secrets.token_hex(8)
        self.target.drop_sessions()
        self.refused(lambda: self.call(connection, timeout=2))
        self.target.password = DEFAULT_LOGIN[1]

        self.clock.advance(5)                                    # later than the refusal
        checked = self.hb.check({'project_id': self.pid, 'robot_id': connection['robot_id']})

        self.assertEqual(checked['link']['state'], 'online')
        self.assertEqual(self.call(connection)['instance_id'], layer.instance)

    def test_a_wrong_fingerprint_is_refused_and_no_token_goes_to_that_machine(self):
        layer, connection = self.linked()
        impostor = self.fixture()                                # another machine at the robot's address
        impostor_layer = self.install_layer(impostor)
        saved = self.store.get('device_connections', connection['id'])
        self.store.put('device_connections', {**saved, 'port': impostor.port})
        self.target.drop_sessions()

        error = self.refused(lambda: self.call(connection, timeout=3))

        self.assertEqual(error.code, REFUSED)
        self.assertEqual(self.view(connection)['error']['reason'], 'fingerprint')
        self.assertEqual((impostor_layer.calls, impostor.logins()), ([], 0))
        self.assertNotIn('Bearer ' + layer.token, impostor_layer.tokens)


class NextToTheHeartbeatTests(ReconnectCase):
    def test_a_request_under_way_does_not_keep_the_heartbeat_waiting(self):
        layer, connection = self.linked()
        self.minute()                                            # the heartbeat has its own session now
        layer.hold.clear()                                       # the management layer takes its time
        worker, box = self.in_background(lambda: self.call(connection, timeout=20))
        self.assertTrue(until(lambda: layer.waiting == 1))
        beats = self.beats()

        self.minute()                                            # waits for the heartbeat round to end

        self.assertEqual(self.beats(), beats + 1)
        self.assertEqual(self.link(connection)['state'], 'online')
        self.assertEqual(layer.waiting, 1, 'the request was no longer under way while the heartbeat ran')
        layer.hold.set()
        worker.join(10)
        self.assertEqual(box.get('result', {}).get('instance_id'), layer.instance, box)

    def test_the_heartbeat_and_the_link_each_have_a_session_of_their_own(self):
        layer, connection = self.linked()
        self.minute()
        logins = self.target.logins()

        self.assertEqual(self.target.open_sessions(), 2)
        self.hb.forget(connection['id'])                         # the heartbeat lets go of its session
        self.assertTrue(until(lambda: self.target.open_sessions() == 1))
        self.assertEqual(self.call(connection)['instance_id'], layer.instance)
        self.assertEqual(self.target.logins(), logins, 'the link lost its session with the heartbeat')

    def test_a_heartbeat_that_fails_does_not_close_the_link(self):
        layer, connection = self.linked()
        self.minute()
        self.target.unanswered.add('true')                       # the heartbeat's command gets no answer

        self.minute()

        self.assertEqual(self.link(connection)['error']['reason'], 'timeout')
        self.assertEqual(self.call(connection)['instance_id'], layer.instance)
        self.assertEqual(self.view(connection)['state'], 'up')


class ConnectionChangeTests(ReconnectCase):
    def test_an_edited_connection_gets_a_session_of_its_own(self):
        layer, connection = self.linked()

        self.save(connection, read_roots=['/opt/ros', '/etc/ros'])

        self.assertTrue(until(lambda: self.target.open_sessions() == 0), 'the session of the old connection stayed open')
        self.assertEqual(self.call(connection)['instance_id'], layer.instance)
        self.assertEqual(self.target.open_sessions(), 1)

    def test_the_token_of_one_machine_is_not_sent_to_the_machine_the_connection_was_moved_to(self):
        layer, connection = self.linked()
        moved = self.fixture()
        moved_layer = self.install_layer(moved)

        self.save_confirmed(connection, port=moved.port, password=DEFAULT_LOGIN[1])

        error = self.refused(lambda: self.call(connection))
        self.assertEqual((error.code, error.sent), ('device_link_not_configured', False))
        self.assertIn('另一台机器', str(error))
        self.assertEqual((moved_layer.calls, moved.loopback_channels), ([], []))
        self.assertEqual(self.view(connection)['state'], 'moved')
        self.assertTrue(until(lambda: self.target.open_sessions() == 0))
        with self.assertRaises(ValueError):
            self.links.retry({'project_id': self.pid, 'robot_id': connection['robot_id']})

        self.attach(connection)                                  # linked again: the token is read from that machine

        self.assertEqual(self.call(connection)['instance_id'], moved_layer.instance)
        self.assertEqual(self.robot(connection)['token'], moved_layer.token)
        self.assertNotIn('Bearer ' + layer.token, moved_layer.tokens)

    def test_deleting_the_robot_closes_its_session_and_ends_the_link(self):
        _, connection = self.linked()
        endpoint, token = self.robot(connection)['endpoint'], self.robot(connection)['token']

        self.platform.robot_remove.remove({'project_id': self.pid, 'robot_id': connection['robot_id']})

        self.assertTrue(until(lambda: self.target.open_sessions() == 0), 'the session of a deleted robot stayed open')
        error = self.refused(lambda: connector.call(endpoint, '/v1/status', token))
        self.assertEqual(error.code, 'device_link_not_configured')
        self.assertEqual(self.links.overview({})['links'], {})

    def test_closing_the_workspace_closes_every_session(self):
        _, connection = self.linked()
        endpoint, token = self.robot(connection)['endpoint'], self.robot(connection)['token']

        self.platform.close()

        self.assertTrue(until(lambda: self.target.open_sessions() == 0))
        self.assertFalse(self.links.running())
        error = self.refused(lambda: connector.call(endpoint, '/v1/status', token, timeout=1))
        self.assertIn(error.code, (DOWN, 'device_link_not_configured'))


if __name__ == '__main__':
    unittest.main()
