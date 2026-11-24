"""Issue #17: what the two reviews of the link found, each as a case that failed before it was fixed.

A robot whose sshd ends every session right after the login; a login that dies of an unexpected error;
a connection check under way while the link is taken away or changed; a request out of time; an address
typed onto a linked robot; a token from an older copy of the record; garbled, cut-off and never-ending
answers from whatever listens on the robot's port. Local fixtures only; nothing reaches a real machine.
"""
import secrets
import socket
import threading
import time
import unittest
from unittest import mock

import paramiko

from brainos import connector, runtime_forward, runtime_link, runtime_link_sessions
from heartbeat_support import HeartbeatCase, until
from runtime_link_support import PORT, LinkMixin


class RawService:
    """Something that listens on the robot's port and answers whatever ``answer(sock)`` writes."""

    def __init__(self, answer):
        self.answer, self.closed = answer, threading.Event()
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen()
        self.port = self.socket.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while not self.closed.is_set():
            try:
                client, _ = self.socket.accept()
            except OSError:
                return
            threading.Thread(target=self.one, args=(client,), daemon=True).start()

    def one(self, client):
        with client:
            try:
                received = b''
                while b'\r\n\r\n' not in received:
                    data = client.recv(4096)
                    if not data:
                        return
                    received += data
                self.answer(client, self.closed)
            except OSError:
                pass

    def close(self):
        self.closed.set()
        self.socket.close()


def truncated_chunked(client, closed):
    client.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n{"a":')


def garbage(client, closed):
    client.sendall(b'NOT HTTP AT ALL\r\n\r\n')


def continue_forever(client, closed):
    while not closed.wait(.1):
        client.sendall(b'HTTP/1.1 100 Continue\r\n\r\n')


class ReviewCase(LinkMixin, HeartbeatCase):
    def linked(self):
        layer = self.install_layer()
        connection = self.connect_directly()
        self.attach(connection)
        return layer, connection

    def raw(self, answer):
        service = RawService(answer)
        self.addCleanup(service.close)
        return service


class SessionTests(ReviewCase):
    def test_a_robot_that_ends_every_session_right_after_the_login_is_not_logged_in_to_once_a_second(self):
        _, connection = self.linked()
        real = self.links.connect

        def let_in_then_drop(saved, secret):
            session = real(saved, secret)
            session.client.get_transport().close()     # the robot's sshd ends it at once
            return session
        self.links.connect = let_in_then_drop
        self.links.tick_seconds = .02
        self.target.drop_sessions()
        logins = self.target.logins()

        self.links.start_timer()
        time.sleep(3.5)
        self.links.stop_timer()

        self.assertLessEqual(self.target.logins() - logins, 3, 'logged in again without growing pauses')
        self.assertGreaterEqual(self.links._links[connection['robot_id']].failures, 2, 'the pause did not grow')
        self.assertEqual(self.view(connection)['state'], 'down')

    def test_a_login_that_dies_of_an_unexpected_error_does_not_leave_the_link_logging_in_for_ever(self):
        layer, connection = self.linked()
        self.links.stable_seconds = 0
        real, failed = self.platform.ssh_edit.saved_login, []

        def broken_once(key):
            if not failed:
                failed.append(key)
                raise RuntimeError('a fault nobody expected')
            return real(key)
        self.target.drop_sessions()

        with mock.patch.object(self.platform.ssh_edit, 'saved_login', broken_once):
            try:
                self.call(connection, timeout=1)
            except connector.DeviceError:
                pass                                     # it may or may not have had time for a second login
            self.assertTrue(until(lambda: not self.links._links[connection['robot_id']].logging_in, 5))
            self.assertEqual(self.call(connection, timeout=8)['instance_id'], layer.instance)
        self.assertEqual(len(failed), 1)

    def test_a_request_out_of_time_with_the_session_up_is_not_called_a_dropped_link(self):
        _, connection = self.linked()

        error = self.refused(lambda: self.links.open(connection['robot_id'], 0))

        self.assertEqual((error.code, error.sent), ('device_link_slow', False))
        self.assertNotIn('断开', str(error))
        self.assertEqual(self.view(connection)['state'], 'up')

    def test_a_link_nobody_needs_any_more_is_waiting_for_use_not_reconnecting(self):
        _, connection = self.linked()
        self.links.stable_seconds = 0
        self.target.cut()
        self.refused(lambda: self.call(connection, timeout=.5))
        self.assertEqual(self.view(connection)['state'], 'down')

        self.links.keep_seconds = 0
        self.links.tick()

        self.assertEqual(self.view(connection)['state'], 'idle')


    def test_a_session_whose_writes_stall_is_closed_and_replaced(self):
        layer, connection = self.linked()
        self.links.stable_seconds = 0
        real = paramiko.Channel.send

        def stalled(channel, data):
            """What paramiko does when the far end stops acknowledging: it keeps trying until the session is closed."""
            transport = channel.get_transport()
            if transport is None or transport.server_mode:          # the fixture's own side sends as usual
                return real(channel, data)
            while transport.is_active():
                time.sleep(.05)
            raise EOFError()

        started = time.monotonic()
        with mock.patch.object(runtime_forward, 'STALL_SECONDS', 1), mock.patch.object(runtime_forward, 'STALL_MARGIN_SECONDS', 0), \
                mock.patch.object(paramiko.Channel, 'send', stalled):
            error = self.refused(lambda: self.call(connection, '/v1/echo', {'n': 1}, timeout=1))

        self.assertLess(time.monotonic() - started, 6, 'a stalled write held the request')
        self.assertEqual((error.code, error.sent), ('device_timeout', None))
        self.assertEqual(self.call(connection)['instance_id'], layer.instance)      # on a new session


    def test_a_session_that_stopped_answering_the_token_read_is_replaced(self):
        layer = self.install_layer()
        connection = self.connect_directly()
        self.target.unanswered.add(runtime_link.TOKEN_COMMAND)          # the session no longer answers anything
        logins = self.target.logins()

        with mock.patch.object(runtime_link, 'TOKEN_READ_SECONDS', 1), mock.patch.object(runtime_link_sessions, 'SILENT_SECONDS', 0):
            for _ in range(2):
                self.assertEqual(self.refused(lambda: self.attach(connection)).code, 'device_link_slow')
            self.assertTrue(until(lambda: self.target.open_sessions() == 0), 'the silent session was kept')
            self.target.unanswered.discard(runtime_link.TOKEN_COMMAND)
            self.assertEqual(self.attach(connection)['status'], 'connected')

        self.assertEqual(self.target.logins() - logins, 2, 'not exactly one new login for the link')
        self.assertEqual(self.call(connection)['instance_id'], layer.instance)


    def test_asking_again_while_the_connection_is_edited_is_a_plain_refusal(self):
        _, connection = self.linked()
        self.links.stable_seconds = 0
        real = self.links.connect

        def slow(saved, secret):
            time.sleep(1.5)
            return real(saved, secret)
        self.links.connect = slow
        self.target.drop_sessions()
        worker, box = self.in_background(lambda: self.links.retry({'project_id': self.pid, 'robot_id': connection['robot_id']}))
        time.sleep(.6)
        self.links.forget(connection['id'])                     # as an edit or a deletion of the connection does
        worker.join(10)

        self.assertIsInstance(box.get('error'), ValueError, box)
        self.assertIn('刚刚被换掉', str(box['error']))

    def test_naming_robots_that_do_not_exist_keeps_no_locks(self):
        for n in range(50):
            with self.assertRaises(ValueError):
                self.platform.connect_robot({'robot_id': f'robot-none-{n}'})

        self.assertEqual(self.links._robot_locks, {})


class RecordTests(ReviewCase):
    def test_a_check_under_way_does_not_bring_back_a_link_that_was_taken_away(self):
        layer, connection = self.linked()
        layer.hold.clear()                               # the connection check waits for the management layer
        check, _ = self.in_background(lambda: self.platform.connect_robot({'robot_id': connection['robot_id']}))
        self.assertTrue(until(lambda: layer.waiting == 1))
        removal, removed = self.in_background(
            lambda: self.links.detach({'project_id': self.pid, 'robot_id': connection['robot_id']}))

        layer.hold.set()
        check.join(15)
        removal.join(15)

        self.assertIn('result', removed, removed)
        saved = self.robot(connection)
        self.assertEqual((saved['endpoint'], saved['token'], saved.get('runtime_link')), ('', '', None))

    def test_a_check_under_way_does_not_undo_a_new_port(self):
        layer, connection = self.linked()
        other = self.install_layer(port=18790)
        layer.hold.clear()
        check, _ = self.in_background(lambda: self.platform.connect_robot({'robot_id': connection['robot_id']}))
        self.assertTrue(until(lambda: layer.waiting == 1))
        change, _ = self.in_background(lambda: self.attach(connection, port=18790, token=other.token))

        layer.hold.set()
        check.join(15)
        change.join(30)

        saved = self.robot(connection)
        self.assertEqual((saved['runtime_link']['port'], saved['token']), (18790, other.token))

    def test_an_address_cannot_be_typed_onto_a_linked_robot(self):
        _, connection = self.linked()
        before = self.robot(connection)

        with self.assertRaises(ValueError) as caught:
            self.platform.configure_connection({'kind': 'robots', 'id': connection['robot_id'],
                                                'endpoint': 'http://10.0.0.5:8790'})

        self.assertIn('移除接入', str(caught.exception))
        self.assertEqual(self.robot(connection), before)

    def test_a_request_built_from_an_older_copy_of_the_record_does_not_carry_its_token_anywhere(self):
        layer, connection = self.linked()
        endpoint, old = self.robot(connection)['endpoint'], self.robot(connection)['token']
        with self.assertRaises(connector.DeviceError):                       # linked again meanwhile, with
            self.attach(connection, token='later-' + secrets.token_hex(12))  # a token this layer refuses
        calls = len(layer.calls)

        error = self.refused(lambda: connector.call(endpoint, '/v1/status', old))

        self.assertEqual((error.code, error.sent), ('device_link_not_configured', False))
        self.assertEqual(len(layer.calls), calls)


class AnswerTests(ReviewCase):
    def serve_raw(self, answer):
        service = self.raw(answer)
        layer, connection = self.linked()
        self.target.serve_on_loopback(PORT, service.port)    # the robot's port is now this service
        robot = self.robot(connection)
        return robot['endpoint'], robot['token']

    def test_a_garbled_or_cut_off_answer_is_a_lost_answer_over_the_channel(self):
        for answer in (truncated_chunked, garbage):
            with self.subTest(answer=answer.__name__):
                endpoint, token = self.serve_raw(answer)
                error = self.refused(lambda: connector.call(endpoint, '/v1/status', token, timeout=3))
                self.assertEqual((error.code, error.sent), ('device_unreachable', None))

    def test_a_garbled_or_cut_off_answer_is_a_lost_answer_over_plain_http_too(self):
        for answer in (truncated_chunked, garbage):
            with self.subTest(answer=answer.__name__):
                service = self.raw(answer)
                error = self.refused(lambda: connector.call(f'http://127.0.0.1:{service.port}', '/v1/status', 'x', timeout=3))
                self.assertEqual((error.code, error.sent), ('device_unreachable', None))

    def test_an_answer_that_never_ends_cannot_hold_the_console(self):
        endpoint, token = self.serve_raw(continue_forever)

        started = time.monotonic()
        with mock.patch.object(runtime_forward, 'EXCHANGE_GRACE_SECONDS', 1):
            error = self.refused(lambda: connector.call(endpoint, '/v1/status', token, timeout=1))

        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual((error.code, error.sent), ('device_timeout', None))
        self.assertTrue(until(lambda: not any(self.links._links[r].session.client.get_transport()._channels.values()
                                              for r in self.links._links), 5), 'the channel stayed open')


if __name__ == '__main__':
    unittest.main()
