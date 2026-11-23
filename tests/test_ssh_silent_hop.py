"""A host that takes the TCP connection and then does not speak SSH in time (found with issue #24).

Seen with a real robot behind a public jump host while nothing came back from the jump host's port:
the heartbeat showed "连不上跳板机；请核对跳板机的地址、端口和网络", although the address was right and
the answer was only late.

paramiko uses the limit it is given for dialling (``timeout``) for the key exchange as well. For the
hop this computer dials itself that is ``TCP_TIMEOUT``, which is shorter than ``HANDSHAKE_TIMEOUT``:
the wait ends before the limit that the report used to be judged by. The tests patch the two limits
in that order, as they are in the product.

Local SSH protocol fixtures only. Nothing reaches a real machine.
"""
from unittest import mock

from brainos import device_access
from heartbeat_support import HeartbeatCase
from test_ssh_jump import SshCase

IN_ORDER = dict(TCP_TIMEOUT=1, HANDSHAKE_TIMEOUT=4)      # dialling is given less time than the handshake
SILENT_FOR = 2                                           # seconds a fixture waits before it speaks SSH
# For what must not be taken for a timeout: the same order, with room for a key exchange on a busy machine.
ROOMY = dict(TCP_TIMEOUT=3, HANDSHAKE_TIMEOUT=12)
PAST_THE_LIMIT, WITHIN_THE_LIMIT = 4, 1                  # seconds, against ROOMY's limit for dialling


class SilentHopTests(SshCase):
    def test_a_jump_host_that_connects_and_then_says_nothing_is_a_timeout(self):
        connection = self.connect_through_jump()
        secret = self.secret(connection)
        self.jump.banner_delay = SILENT_FOR

        with mock.patch.multiple(device_access, **IN_ORDER):
            self.assert_refused('jump', 'timeout', '应答超时', lambda: device_access.SSHDevice(connection, secret))

    def test_a_device_dialled_directly_that_connects_and_then_says_nothing_is_a_timeout(self):
        connection = self.connect_directly()
        secret = self.secret(connection)
        self.target.banner_delay = SILENT_FOR

        with mock.patch.multiple(device_access, **IN_ORDER):
            self.assert_refused('device', 'timeout', '应答超时', lambda: device_access.SSHDevice(connection, secret))

    def test_a_port_that_takes_no_connection_is_still_unreachable(self):
        connection = self.connect_directly()
        secret = self.secret(connection)
        self.target.close()

        with mock.patch.multiple(device_access, **ROOMY):
            self.assert_refused('device', 'unreachable', '不可达', lambda: device_access.SSHDevice(connection, secret))

    def test_a_login_that_is_refused_slowly_is_still_a_refusal(self):
        # A refusal must never be taken for a slow network: a refused login is not tried again every minute.
        connection = self.connect_directly()
        secret = {**self.secret(connection), 'password': 'not-the-password'}
        self.target.auth_delay = PAST_THE_LIMIT

        with mock.patch.multiple(device_access, **ROOMY):
            self.assert_refused('device', 'auth', '拒绝登录', lambda: device_access.SSHDevice(connection, secret))

    def test_a_host_that_is_slow_but_in_time_still_logs_in(self):
        connection = self.connect_directly()
        secret = self.secret(connection)
        self.target.banner_delay = WITHIN_THE_LIMIT

        with mock.patch.multiple(device_access, **ROOMY):
            session = device_access.SSHDevice(connection, secret)
        self.addCleanup(session.close)

        self.assertTrue(session.client.get_transport().is_active())

    def test_another_host_key_on_a_slow_host_is_still_a_changed_fingerprint(self):
        connection = {**self.connect_directly(), 'fingerprint': 'SHA256:' + 'A' * 43}
        secret = self.secret(connection)
        self.target.banner_delay = WITHIN_THE_LIMIT

        with mock.patch.multiple(device_access, **ROOMY):
            self.assert_refused('device', 'fingerprint', '指纹', lambda: device_access.SSHDevice(connection, secret))


class HeartbeatOfASilentHopTests(HeartbeatCase):
    def test_the_card_says_the_answer_was_late_and_not_that_the_address_is_wrong(self):
        connection = self.connect_through_jump()
        self.minute()
        self.jump.drop_sessions()
        self.jump.banner_delay = SILENT_FOR

        with mock.patch.multiple(device_access, **IN_ORDER):
            self.minute()

        link = self.link(connection)
        self.assertEqual((link['error']['hop'], link['error']['reason']), ('jump', 'timeout'))
        self.assertIn('应答超时', link['error']['message'])
        self.assertTrue(link['retrying'], 'a late answer is tried again at the next heartbeat')
        self.assertEqual(link['state'], 'online', 'one late answer is not three minutes without a heartbeat')
