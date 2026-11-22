"""Issue #19: an install over a link that keeps breaking.

``upload_fixture.FlakyLink`` sits between the console and the robot and cuts each SSH connection once it
carried a set number of bytes towards the robot: what a weak network leaves a program with is waiting
and broken connections. The real packet loss of R1's weak-network line (1 Mbit/s, 300 ms, 30 % / 60 %)
is checked by ``tests/runtime_install_weak_network.py`` in a Linux container."""
import hashlib
import unittest
from unittest import mock

from brainos import device_transfer
from runtime_install_support import InstallCase
from upload_fixture import FlakyLink


class WeakLinkTests(InstallCase):
    def setUp(self):
        super().setUp()
        self.relay = FlakyLink(self.device.port, [])
        self.addCleanup(self.relay.close)
        self.installs.transfer_options = {'retry_waits': (0.1, 0.2, 0.3, 0.5)}
        self.connection = self.connect_directly(device=_Through(self.device, self.relay))

    def assert_only_whole_files(self):
        """In the package's directory every file that has its own name is the one the console sent."""
        expected = {entry['name']: entry['sha256'] for entry in self.package.files}
        for name in self.device.names(self.package_dir()):
            if name in expected:
                self.assertEqual(hashlib.sha256(self.device.read(self.package_dir() + '/' + name)).hexdigest(), expected[name])
            else:
                self.assertTrue(name.startswith('.') or name.split('.')[0] in ('install', 'start', 'remove'), name)

    def test_an_install_whose_connections_keep_breaking_still_finishes_and_installs_once(self):
        self.relay.cuts = [9000, 14000, 30000, 7000, 20000, 45000, 9000, 12000, 60000, 8000]
        view = self.install(self.connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertFalse(self.relay.cuts, 'every cut was used')
        self.assert_only_whole_files()
        self.assertEqual([c for c, *_ in self.device.runs()], ['install', 'start'])
        self.assert_online_and_linked(self.connection)

    def test_a_link_too_bad_to_log_in_leaves_no_damaged_file_and_the_install_goes_on_once_it_is_better(self):
        self.installs.budgets['upload'] = 4
        self.relay.cuts = [None, 20000] + [1200] * 400          # the check, part of the upload, then no login gets through
        view = self.install(self.connection)
        self.assertEqual((view['state'], view['stage']), ('failed', 'upload'), view)
        self.assertIn(view['failure']['code'], ('install_upload_failed', 'install_unreachable'))
        self.assert_only_whole_files()
        self.assertEqual(self.device.names('neuxmind'), [])
        received = self.device.received
        self.relay.cuts = []                                     # the network is fine again
        view = self.install(self.connection)
        self.assertEqual(view['state'], 'succeeded', view)
        self.assertLess(self.device.received - received, self.package.total_bytes, 'the upload went on from where it was')
        self.assert_only_whole_files()
        self.assertEqual([c for c, *_ in self.device.runs()], ['install', 'start'])
        self.assert_online_and_linked(self.connection)


class UploadRetryTests(InstallCase):
    """An upload that ends with nothing to resume from (issue #18: ``resumable`` is false until a byte is confirmed)
    is still tried again while there is time, when the cause is the link and not the robot or this computer."""

    def failing_first(self, *errors):
        real, raised = device_transfer.send_to_robot, list(errors)

        def send(*args, **kwargs):
            if raised:
                raise raised.pop(0)
            return real(*args, **kwargs)
        return mock.patch.object(device_transfer, 'send_to_robot', send)

    def test_a_broken_link_before_the_first_byte_is_tried_again(self):
        connection = self.connect_directly()
        broken = [device_transfer.TransferError('connect', 'no_sftp', 'timed out'),
                  device_transfer.TransferError('send', 'interrupted', 'no progress', resumable=False),
                  device_transfer.TransferError('connect', 'timeout', 'slow')]
        self.installs.upload_retry_seconds = 0.1
        with self.failing_first(*broken):
            view = self.install(connection)
        self.assertEqual(view['state'], 'succeeded', view)

    def test_what_trying_again_cannot_mend_ends_the_install(self):
        for error, code in ((device_transfer.TransferError('connect', 'auth', 'refused', resumable=True), 'install_login_refused'),
                            (device_transfer.TransferError('send', 'refused', 'disk full', resumable=True), 'install_upload_failed'),
                            (device_transfer.TransferError('send', 'source_changed', 'changed'), 'install_upload_failed')):
            with self.subTest(reason=error.reason):
                device = self.robot_device()
                connection = self.connect_directly(device=device, name='robot ' + error.reason)
                with self.failing_first(error):
                    view = self.install(connection)
                self.assertEqual((view['stage'], view['failure']['code']), ('upload', code))

    def test_a_damaged_upload_is_sent_once_more_only(self):
        connection = self.connect_directly()
        damaged = [device_transfer.TransferError('verify', 'checksum', 'mismatch') for _ in range(3)]
        self.installs.upload_retry_seconds = 0.1
        with self.failing_first(*damaged):
            view = self.install(connection)
        self.assertEqual((view['stage'], view['failure']['code']), ('upload', 'install_upload_failed'))


class _Through:
    """The robot as the console reaches it: through the relay."""

    def __init__(self, device, relay):
        self.port = relay.port


if __name__ == '__main__':
    unittest.main()
