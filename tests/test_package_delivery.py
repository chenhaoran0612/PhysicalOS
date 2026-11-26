"""Issue #31: a driver package goes from the cloud library to a device and is verified there.

The cloud is ``package_cloud_fixture.FakeLibraryCloud`` and the device is
``upload_fixture.UploadDevice`` (a local SSH server whose files live in a private temporary
directory). Every package is made up here. Nothing reaches a real server or a real machine, and
nothing is unpacked, installed or run.
"""
import json
import os
import random
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from brainos import device_transfer, device_transfer_target, package_delivery, package_fetch
from brainos.cloud_account import CloudError
from brainos.errors import CodedError
from brainos.service import Platform
from package_cloud_fixture import FakeLibraryCloud, sha, wait_until
from test_device_transfer import quiet_paramiko
from upload_fixture import FlakyLink, UploadDevice

PACKAGE = random.Random(31).randbytes(300_000)
OTHER = random.Random(32).randbytes(40_000)
ROBOT, PROJECT, CONNECTION = 'robot-fixture', 'project-fixture', 'connection-fixture'
HOME = '/home/robot'
LIBRARY = HOME + '/neuxmind-pkg/library'
QUICK = {'fetch': {'retry_waits': (0, 0, 0), 'block_bytes': 8192}, 'transfer': {'retry_waits': (0, 0, 0), 'chunk_bytes': 8192}}


def folder(data):
    """Where a package with these bytes goes on the device."""
    return '%s/%s' % (LIBRARY, sha(data)[:32])


class DeliveryCase(unittest.TestCase):
    def setUp(self):
        quiet_paramiko(self)
        self.device = UploadDevice()
        self.addCleanup(self.device.close)
        self.cloud = FakeLibraryCloud()
        self.addCleanup(self.cloud.close)
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.platform = Platform(os.path.join(self.work.name, 'state'))
        self.addCleanup(self.platform.close)
        self.store, self.delivery = self.platform.store, self.platform.package_delivery
        self.attach(self.device.port)
        self.cloud.sign_in(self.platform)

    def attach(self, port):
        """The robot of this workspace and its saved SSH connection, which leads to ``port``."""
        connection, secret = self.device.login()
        # Issue #51: a package is its brand and model's. The stand-in cloud labels every package 示例厂家 X1.
        self.store.put('robots', {'id': ROBOT, 'project_id': PROJECT, 'name': '测试机器人', 'family': 'quadruped',
                                  'connection_id': CONNECTION, 'product': {'vendor': '示例厂家', 'model': 'X1', 'source': 'manual'}})
        self.store.put('device_connections', {**connection, 'port': port, 'id': CONNECTION, 'robot_id': ROBOT, 'project_id': PROJECT})
        self.store.put('agent_secrets', {**secret, 'id': CONNECTION})

    def deliver(self, material_id, **options):
        return self.delivery.deliver(ROBOT, PROJECT, material_id, **{'authorized': True, **QUICK, **options})

    def refused(self, material_id, kind=ValueError, **options):
        with self.assertRaises(kind) as caught:
            self.deliver(material_id, **options)
        return caught.exception

    def cache(self):
        directory = Path(self.store.root) / 'package-cache'
        return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []

    def events(self, action):
        return [event for event in self.store.list('events') if event['action'] == action]


class Acceptance(DeliveryCase):
    def test_a_package_goes_from_the_library_to_the_device_and_is_verified_there(self):
        material_id = self.cloud.add('driver_1.0_arm64.deb', PACKAGE)
        seen = []
        result = self.deliver(material_id, on_progress=seen.append)

        arrived = folder(PACKAGE) + '/driver_1.0_arm64.deb'
        self.assertEqual(self.device.read(arrived), PACKAGE)
        self.assertEqual((result['device_path'], result['bytes'], result['sha256'], result['verified']),
                         (arrived, len(PACKAGE), sha(PACKAGE), True))
        self.assertEqual(self.device.names(folder(PACKAGE)), ['driver_1.0_arm64.deb'])      # no temporary file is left
        self.assertEqual(self.device.mode(arrived), 0o644)                                  # a file, not a program
        # The device was only asked to check and to verify: nothing was unpacked, installed or run.
        self.assertEqual([kind for kind, _ in self.device.scripts], ['probe', 'finalize'])
        self.assertEqual(self.device.refused, [])
        self.assertEqual(self.cache(), [])                                                  # the copy on this computer is gone
        phases = [event['phase'] for event in seen]
        self.assertEqual((phases[0], phases[-1]), ('checking', 'done'))
        self.assertLess(phases.index('fetching'), phases.index('sending'))
        self.assertEqual(self.events('library.package_delivered')[0]['details']['sha256'], sha(PACKAGE))

    def test_the_result_tells_the_repair_flow_what_is_where(self):
        material_id = self.cloud.add('驱动 安装包 (v1.0).deb', PACKAGE, vendor='示例厂家', model='X1', version='1.0')
        self.deliver(material_id)

        handed = self.delivery.delivered(ROBOT)
        self.assertEqual(len(handed), 1)
        record = handed[0]
        self.assertEqual((record['material_id'], record['robot_id'], record['connection_id'], record['project_id']),
                         (material_id, ROBOT, CONNECTION, PROJECT))
        self.assertEqual((record['filename'], record['device_name']), ('驱动 安装包 (v1.0).deb', 'v1.0.deb'))
        self.assertEqual(record['device_path'], folder(PACKAGE) + '/v1.0.deb')
        self.assertEqual(record['device_target'], '~/neuxmind-pkg/library/%s/v1.0.deb' % sha(PACKAGE)[:32])
        self.assertEqual((record['sha256'], record['bytes'], record['verified']), (sha(PACKAGE), len(PACKAGE), True))
        self.assertEqual(record['labels'], {'body_type': 'quadruped', 'vendor': '示例厂家', 'model': 'X1', 'version': '1.0', 'format': 'deb'})
        self.assertTrue(record['delivered_at'])
        self.assertEqual(self.device.read(record['device_path']), PACKAGE)
        self.assertEqual(self.delivery.delivered('another-robot'), [])

    def test_delivering_again_finds_the_file_on_the_device_and_sends_nothing(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        first = self.deliver(material_id)
        sent = self.device.received
        again = self.deliver(material_id)
        self.assertTrue(again['already_present'])
        self.assertTrue(again['verified'])
        self.assertEqual(self.device.received, sent)
        self.assertEqual([record['id'] for record in self.delivery.delivered(ROBOT)], [first['id']])     # still one record

    def test_a_file_that_was_changed_on_the_device_is_replaced_by_the_verified_one(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        self.device.write(folder(PACKAGE) + '/driver.deb', b'not the package' * 10)
        result = self.deliver(material_id)
        self.assertFalse(result['already_present'])
        self.assertEqual(self.device.read(folder(PACKAGE) + '/driver.deb'), PACKAGE)


class OnlyUsablePackages(DeliveryCase):
    def test_a_material_that_is_not_marked_usable_is_not_fetched_and_not_sent(self):
        for status in ('parsing', 'parsed', 'failed', 'available', '', None, 'USABLE', ['usable']):
            with self.subTest(status=status):
                error = self.refused(self.cloud.add('driver.deb', PACKAGE, status=status), CodedError)
                self.assertEqual(error.code, 'package_not_usable')
        self.assertEqual(self.cloud.content_requests(), [])
        self.assertEqual(self.device.connections, 0)
        self.assertEqual(self.delivery.delivered(ROBOT), [])

    def test_the_usable_state_is_decided_in_one_place(self):
        self.assertEqual(package_delivery.USABLE_STATUSES, ('usable',))       # issue #34: 「可用」 after a person confirmed it
        self.assertTrue(package_delivery.usable({'status': 'usable'}))
        self.assertFalse(package_delivery.usable({'status': 'parsed'}))       # 待确认: parsed, not yet confirmed
        self.assertFalse(package_delivery.usable({'status': 'parsing'}))
        self.assertFalse(package_delivery.usable({}))

    def test_the_refusal_names_the_state_as_the_page_does(self):
        for status, name in (('parsing', '解析中'), ('parsed', '待确认'), ('failed', '没解析出来')):
            with self.subTest(status=status):
                error = self.refused(self.cloud.add('driver.deb', PACKAGE, status=status), CodedError)
                self.assertIn('“%s”' % name, str(error))

    def test_a_document_is_not_a_package(self):
        error = self.refused(self.cloud.add('manual.pdf', b'%PDF-1.4 made up', kind='document', fmt='pdf'), CodedError)
        self.assertEqual(error.code, 'package_not_deliverable')
        self.assertEqual(self.cloud.content_requests(), [])

    def test_an_id_that_is_not_a_material_id_never_reaches_the_cloud(self):
        for bad in ('../../admin/models', 'mat_', '', None, 5, 'mat_' + 'z' * 20):
            with self.subTest(bad=bad):
                self.refused(bad)
        self.assertEqual(self.cloud.requests, [])

    def test_a_material_the_cloud_does_not_have_is_said_so(self):
        error = self.refused('mat_' + '0' * 20, CloudError)
        self.assertEqual((error.status, error.code), (404, 'not_found'))
        self.assertEqual(self.device.connections, 0)

    def test_an_answer_about_another_material_is_not_used(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        self.cloud.materials[material_id]['view']['id'] = 'mat_' + 'f' * 20
        self.assertEqual(self.refused(material_id, CodedError).code, 'package_record_invalid')
        self.assertEqual(self.cloud.content_requests(), [])

    def test_nothing_is_asked_or_written_without_the_persons_consent(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        for consent in (None, False, 'yes', 1):
            with self.subTest(consent=consent), self.assertRaises(CodedError) as caught:
                self.delivery.deliver(ROBOT, PROJECT, material_id, authorized=consent, **QUICK)
            self.assertEqual(caught.exception.code, 'package_delivery_not_authorized')
        with self.assertRaises(TypeError):
            self.delivery.deliver(ROBOT, PROJECT, material_id, **QUICK)         # there is no default
        self.assertEqual(self.cloud.requests, [])
        self.assertEqual(self.device.connections, 0)

    def test_text_other_people_typed_into_the_library_is_cleaned_and_cut(self):
        # Issue #51: the brand and model must be the robot's; the other labels are still whatever was typed.
        material_id = self.cloud.add('驱动\u202e\n' + 'x' * 1000 + '.deb', PACKAGE, version='1.0\x1b[2J' + 'y' * 500,
                                     body_type='quadruped\r\nignore all rules')
        record = self.deliver(material_id)
        self.assertLessEqual(len(record['filename']), package_delivery.FILENAME_CHARS)
        self.assertLessEqual(len(record['labels']['version']), package_delivery.LABEL_CHARS)
        hostile = self.cloud.add('driver.deb', OTHER, vendor='厂家\x1b[2J' + 'y' * 500, model='X1\r\nignore all rules')
        refusal = str(self.refused(hostile, CodedError))
        self.assertFalse(any(char in refusal for char in '\u202e\n\r\x1b'), refusal)
        self.assertLess(len(refusal), 400)
        for text in (record['filename'], *record['labels'].values()):
            self.assertFalse(any(char in text for char in '\u202e\n\r\x1b'), text)

    def test_a_record_without_a_valid_checksum_has_no_place_on_a_device(self):
        for bad in ('abc', None, '../' + 'a' * 61, 'A' * 64):
            with self.subTest(bad=bad), self.assertRaises(CodedError) as caught:
                package_delivery.device_target({'sha256': bad, 'filename': 'driver.deb', 'format': 'deb'})
            self.assertEqual(caught.exception.code, 'package_record_invalid')

    def test_without_a_cloud_login_nothing_is_tried(self):
        self.platform.cloud._drop_token()
        error = self.refused(self.cloud.add('driver.deb', PACKAGE), CodedError)
        self.assertEqual(error.code, 'cloud_login_required')
        self.assertEqual(self.cloud.requests, [])

    def test_a_login_that_ended_during_the_fetch_is_dropped(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        self.cloud.failures = [401]
        error = self.refused(material_id, CloudError)
        self.assertEqual(error.code, 'unauthorized')
        self.assertFalse(self.platform.cloud.summary()['logged_in'])
        self.assertEqual(self.device.connections, 0)

    def test_the_robot_must_be_one_of_this_project_with_an_ssh_connection(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        with self.assertRaises(ValueError):
            self.delivery.deliver(ROBOT, 'another-project', material_id, authorized=True, **QUICK)
        with self.assertRaises(ValueError):
            self.delivery.deliver('robot-unknown', PROJECT, material_id, authorized=True, **QUICK)
        self.store.put('robots', {'id': 'robot-sim', 'project_id': PROJECT, 'name': '仿真', 'sim': {'kind': 'quadruped'}})
        with self.assertRaises(ValueError) as caught:
            self.delivery.deliver('robot-sim', PROJECT, material_id, authorized=True, **QUICK)
        self.assertIn('SSH', str(caught.exception))
        self.assertEqual(self.cloud.requests, [])
        self.assertEqual(self.device.connections, 0)


class Resume(DeliveryCase):
    def test_the_fetch_and_the_send_each_go_on_from_where_they_stopped(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)

        # 1. The link to the cloud breaks during the fetch.
        self.cloud.cuts, self.cloud.offline_after_cut = [100_000], True
        error = self.refused(material_id, package_fetch.FetchError)
        self.assertEqual((error.code, error.resumable, error.received), ('package_fetch_interrupted', True, 100_000))
        self.assertEqual(self.device.connections, 0)                           # nothing went to the device yet
        self.assertEqual(self.cache(), [sha(PACKAGE) + '.part'])
        self.cloud.offline, self.cloud.offline_after_cut = False, False

        # 2. The fetch goes on from there; then the link to the device breaks during the send.
        self.device.drops, self.device.offline_after_drop = [50_000], True
        error = self.refused(material_id, device_transfer.TransferError)
        self.assertTrue(error.resumable)
        self.assertEqual(self.cloud.served, len(PACKAGE))                      # the cloud sent every byte once
        self.assertEqual(self.cloud.content_requests()[-1][1], 'bytes=100000-')
        self.assertEqual(self.cache(), [sha(PACKAGE) + '.pkg'])                # kept for the next try
        self.assertEqual(self.device.read(folder(PACKAGE) + '/driver.deb'), None)       # no half file at the destination
        self.assertEqual(self.delivery.delivered(ROBOT), [])
        self.device.offline, self.device.offline_after_drop = False, False

        # 3. The send goes on from there, without asking the cloud for the file again.
        result = self.deliver(material_id)
        self.assertTrue(result['verified'])
        self.assertTrue(result['fetch']['cached'])
        self.assertGreater(result['send']['resumed_from'], 0)
        self.assertEqual(self.cloud.served, len(PACKAGE))
        self.assertLessEqual(self.device.received, len(PACKAGE) + QUICK['transfer']['chunk_bytes'])    # only the chunk in flight went twice
        self.assertEqual(self.device.read(folder(PACKAGE) + '/driver.deb'), PACKAGE)
        self.assertEqual(self.cache(), [])

    def test_links_that_keep_breaking_on_both_sides_still_deliver_in_one_call(self):
        link = FlakyLink(self.device.port, cuts=[60_000, 60_000, 60_000])
        self.addCleanup(link.close)
        self.attach(link.port)
        material_id = self.cloud.add('driver.deb', PACKAGE)
        self.cloud.cuts = [70_000] * 3
        result = self.deliver(material_id)
        self.assertTrue(result['verified'])
        self.assertGreater(result['send']['attempts'], 1)
        self.assertEqual(len(self.cloud.content_requests()), 4)
        self.assertEqual(self.device.read(folder(PACKAGE) + '/driver.deb'), PACKAGE)

    def test_a_stop_request_ends_either_half_and_the_next_call_goes_on(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        stop = threading.Event()

        def during(phase):
            def watch(event):
                if event['phase'] == phase and event['done'] >= 60_000:
                    stop.set()
            return watch
        error = self.refused(material_id, package_fetch.FetchError, on_progress=during('fetching'), stop=stop)
        self.assertEqual(error.code, 'package_fetch_cancelled')
        self.assertEqual(self.device.connections, 0)

        stop.clear()
        error = self.refused(material_id, device_transfer.TransferError, on_progress=during('sending'), stop=stop)
        self.assertEqual((error.reason, error.resumable), ('cancelled', True))

        stop.clear()
        result = self.deliver(material_id)
        self.assertGreater(result['send']['resumed_from'], 0)
        self.assertEqual(self.device.read(folder(PACKAGE) + '/driver.deb'), PACKAGE)


class Verification(DeliveryCase):
    def test_bytes_that_differ_from_the_library_checksum_never_reach_the_device(self):
        material_id = self.cloud.add('driver.deb', PACKAGE, declared_sha256=sha(b'what the library recorded'))
        error = self.refused(material_id, package_fetch.FetchError)
        self.assertEqual(error.code, 'package_checksum_mismatch')
        self.assertEqual(self.device.connections, 0)
        self.assertEqual(self.device.names(HOME + '/neuxmind-pkg'), [])
        self.assertEqual(self.delivery.delivered(ROBOT), [])
        self.assertEqual(self.cache(), [])

    def test_a_package_damaged_on_the_way_to_the_device_is_removed_there_and_not_recorded(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        self.device.corrupt_at = 5000
        error = self.refused(material_id, device_transfer.TransferError)
        self.assertEqual((error.stage, error.reason, error.resumable), ('verify', 'checksum', False))
        self.assertEqual(self.device.names(folder(PACKAGE)), [])               # neither the file nor a temporary one
        self.assertEqual(self.delivery.delivered(ROBOT), [])
        self.assertEqual(self.events('library.package_delivered'), [])

        result = self.deliver(material_id)                                     # the copy here was good: sent again
        self.assertTrue(result['fetch']['cached'])
        self.assertEqual(self.device.read(folder(PACKAGE) + '/driver.deb'), PACKAGE)

    def test_a_result_that_does_not_carry_the_library_checksum_is_not_recorded_as_verified(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        real = device_transfer.send_to_robot

        def lying(*arguments, **options):
            return {**real(*arguments, **options), 'sha256': sha(b'something else')}
        with mock.patch('brainos.package_delivery.device_transfer.send_to_robot', side_effect=lying):
            error = self.refused(material_id, CodedError)
        self.assertEqual(error.code, 'package_delivery_unverified')
        self.assertIn(folder(PACKAGE) + '/driver.deb', str(error))             # where the file that must not be used is
        self.assertEqual(self.delivery.delivered(ROBOT), [])
        self.assertEqual(self.events('library.package_delivered'), [])

    def test_a_delivery_to_another_machine_is_not_handed_over_for_this_one(self):
        self.deliver(self.cloud.add('driver.deb', PACKAGE))
        self.assertEqual(len(self.delivery.delivered(ROBOT)), 1)
        connection = self.store.get('device_connections', CONNECTION)
        self.store.put('device_connections', {**connection, 'fingerprint': 'SHA256:' + 'A' * 43})     # the connection now leads elsewhere
        self.assertEqual(self.delivery.delivered(ROBOT), [])
        self.store.put('device_connections', connection)
        self.assertEqual(len(self.delivery.delivered(ROBOT)), 1)

    def test_a_copy_that_changed_after_it_was_verified_is_not_sent(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        real = package_fetch.fetch

        def changed_afterwards(*arguments, **options):
            result = real(*arguments, **options)
            os.chmod(result['path'], 0o600)
            Path(result['path']).write_bytes(OTHER[:len(PACKAGE)].ljust(len(PACKAGE), b'\0'))   # same size, other bytes
            return result
        with mock.patch('brainos.package_delivery.package_fetch.fetch', side_effect=changed_afterwards):
            error = self.refused(material_id, CodedError)
        self.assertEqual(error.code, 'package_local_copy_changed')
        self.assertEqual(self.device.writes, [])                               # nothing of it reached the device
        self.assertEqual(self.device.read(folder(PACKAGE) + '/driver.deb'), None)
        self.assertEqual(self.cache(), [])                                     # the next try fetches it again
        self.assertEqual(self.delivery.delivered(ROBOT), [])
        self.assertEqual(self.device.read(self.deliver(material_id)['device_path']), PACKAGE)

    def test_the_channel_is_given_the_verified_copy_and_the_default_place_only(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        seen = {}
        real = device_transfer.send_to_robot

        def recording(platform, connection_id, local_path, remote_path, **options):
            seen.update(connection_id=connection_id, remote_path=remote_path, options=options,
                        local=Path(local_path).read_bytes(), inside=Path(local_path).parent)
            return real(platform, connection_id, local_path, remote_path, **options)
        with mock.patch('brainos.package_delivery.device_transfer.send_to_robot', side_effect=recording):
            self.deliver(material_id)
        self.assertEqual((seen['connection_id'], seen['local']), (CONNECTION, PACKAGE))
        self.assertEqual(seen['inside'], Path(self.store.root) / 'package-cache')
        self.assertNotIn('roots', seen['options'])                             # only ~/neuxmind-pkg, the channel's default
        self.assertEqual(seen['options'].get('mode', 0o644), 0o644)


class BelongsToADevice(DeliveryCase):
    """Issue #34 bound a material to the robot it was uploaded for (``device_id``); since issue #51 it
    belongs to a brand and model, and only what was not moved yet is still one device's."""

    def test_a_package_of_this_robot_is_delivered(self):
        record = self.deliver(self.cloud.add('driver.deb', PACKAGE, device_id=ROBOT, device_name='测试机器人'))
        self.assertEqual(self.device.read(record['device_path']), PACKAGE)

    def test_a_package_of_another_robot_is_not_fetched_and_not_sent(self):
        material_id = self.cloud.add('driver.deb', PACKAGE, device_id='robot-0123456789ab', device_name='另一台 \u202e')
        error = self.refused(material_id, CodedError)
        self.assertEqual(error.code, 'package_other_device')
        self.assertIn('另一台', str(error))
        self.assertNotIn('\u202e', str(error))
        self.assertEqual(self.cloud.content_requests(), [])
        self.assertEqual(self.device.connections, 0)
        with self.assertRaises(CodedError) as caught:
            self.delivery.start({'project_id': PROJECT, 'robot_id': ROBOT, 'material_id': material_id, 'modify_authorized': True})
        self.assertEqual(caught.exception.code, 'package_other_device')

    def test_a_package_of_this_robots_brand_and_model_is_delivered(self):
        for owner in ({}, {'device_id': ''}, {'device_id': None}, {'vendor': '示例厂家 ', 'model': 'x1'}):
            with self.subTest(owner=owner):
                record = self.deliver(self.cloud.add('driver.deb', random.Random(str(owner)).randbytes(20_000), **owner))
                self.assertTrue(record['verified'])

    def test_a_package_of_another_brand_and_model_is_not_fetched_and_not_sent(self):
        for labels in ({'vendor': '别的厂家'}, {'model': 'X2'}, {'vendor': ''}, {'model': None}):
            with self.subTest(labels=labels):
                material_id = self.cloud.add('driver.deb', PACKAGE, **labels)
                error = self.refused(material_id, CodedError)
                self.assertEqual(error.code, 'package_other_model')
                self.assertIn('示例厂家 X1', str(error))
        self.assertEqual(self.cloud.content_requests(), [])
        self.assertEqual(self.device.connections, 0)
        with self.assertRaises(CodedError) as caught:
            self.delivery.start({'project_id': PROJECT, 'robot_id': ROBOT, 'material_id': material_id, 'modify_authorized': True})
        self.assertEqual(caught.exception.code, 'package_other_model')

    def test_a_robot_without_a_brand_and_model_gets_only_what_is_still_bound_to_it(self):
        robot = self.store.get('robots', ROBOT)
        self.store.put('robots', {k: v for k, v in robot.items() if k != 'product'}, owner=True)
        self.assertEqual(self.refused(self.cloud.add('driver.deb', PACKAGE), CodedError).code, 'robot_model_missing')
        self.assertEqual(self.cloud.content_requests(), [])
        record = self.deliver(self.cloud.add('own.deb', OTHER, device_id=ROBOT, device_name='测试机器人', vendor='随便写的'))
        self.assertTrue(record['verified'])

    def test_an_owner_that_is_not_plain_text_is_not_taken_for_this_robot(self):
        for owner in (5, True, [ROBOT]):
            with self.subTest(owner=owner):
                error = self.refused(self.cloud.add('driver.deb', PACKAGE, device_id=owner), CodedError)
                self.assertEqual(error.code, 'package_other_device')


class DeviceNames(unittest.TestCase):
    CASES = [
        ('driver_1.0_arm64.deb', 'deb', 'driver_1.0_arm64.deb'),
        ('驱动 安装包 (v1.0).deb', 'deb', 'v1.0.deb'),
        ('运控驱动.tar.gz', 'tar.gz', 'package.tar.gz'),
        ('ros-humble-driver_2.1.0~rc1-1_arm64.deb', 'deb', 'ros-humble-driver_2.1.0_rc1-1_arm64.deb'),
        ('Motor Driver (ARM64).TAR.GZ', 'tar.gz', 'Motor_Driver_ARM64.tar.gz'),
        ('sdk.tgz', 'tar.gz', 'sdk.tar.gz'),
        ('.hidden.zip', 'zip', 'hidden.zip'),
        ('-rf.deb', 'deb', 'rf.deb'),
        ('--force-all.deb', 'deb', 'force-all.deb'),
        ('id_rsa.zip', 'zip', 'package.zip'),
        ('credentials backup.zip', 'zip', 'package.zip'),
        ('a.nxpart-b.zip', 'zip', 'package.zip'),
        ('a/b\\c.deb', 'deb', 'a_b_c.deb'),
        ('driver‮gnp.deb', 'deb', 'driver_gnp.deb'),
        ('$(reboot);`id`.deb', 'deb', 'reboot_id.deb'),
        ('x' * 300 + '.deb', 'deb', 'x' * 80 + '.deb'),
        ('...deb', 'deb', 'package.deb'),
        ('', 'zip', 'package.zip'),
        ('driver.deb', 'zip', 'driver.deb.zip'),                               # the format the cloud recognised decides
        ('firmware.rpm', 'rpm', 'firmware.rpm'),                               # a format this version does not know yet
        ('firmware.bin!', 'something new', 'firmware.bin'),
    ]

    def test_a_library_name_becomes_a_name_the_channel_accepts(self):
        for filename, fmt, expected in self.CASES:
            with self.subTest(filename=filename):
                self.assertEqual(package_delivery.device_name(filename, fmt), expected)

    def test_every_name_passes_the_rules_of_the_channel(self):
        roots = device_transfer_target.check_roots(device_transfer.DEFAULT_ROOTS)
        odd = [case[0] for case in self.CASES] + [None, 7, '\x00', '名字' * 200, '~/.ssh/authorized_keys', 'a' * 5000, '.env.zip']
        for filename in odd:
            for fmt in ('deb', 'tar.gz', None, '../x'):
                with self.subTest(filename=filename, fmt=fmt):
                    place = package_delivery.device_target({'filename': filename, 'format': fmt, 'sha256': 'ab' * 32})
                    names = device_transfer_target.check_remote_path(place, roots)
                    self.assertEqual(names[:3], ('neuxmind-pkg', 'library', 'ab' * 16))
                    self.assertEqual(len(names), 4)
                    self.assertNotIn(names[3][0], '-+._')


class NamesOnTheDevice(DeliveryCase):
    def test_a_name_with_spaces_brackets_and_chinese_arrives_under_a_plain_name(self):
        material_id = self.cloud.add('运控 驱动（正式版）v2.3 (arm64).tar.gz', PACKAGE, fmt='tar.gz')
        result = self.deliver(material_id)
        self.assertEqual(result['filename'], '运控 驱动（正式版）v2.3 (arm64).tar.gz')
        self.assertEqual(result['device_name'], 'v2.3_arm64.tar.gz')
        self.assertEqual(self.device.read(folder(PACKAGE) + '/v2.3_arm64.tar.gz'), PACKAGE)
        self.assertEqual(self.device.refused, [])

    def test_two_packages_whose_names_become_the_same_do_not_meet(self):
        first = self.deliver(self.cloud.add('驱动 (1).deb', PACKAGE))
        second = self.deliver(self.cloud.add('驱动 [1].deb', OTHER))
        self.assertEqual((first['device_name'], second['device_name']), ('1.deb', '1.deb'))
        self.assertNotEqual(first['device_path'], second['device_path'])
        self.assertEqual(self.device.read(first['device_path']), PACKAGE)
        self.assertEqual(self.device.read(second['device_path']), OTHER)
        self.assertEqual(len(self.delivery.delivered(ROBOT)), 2)


class PlatformStep(DeliveryCase):
    """The step a person starts: it runs in the background and is asked how far it is."""

    def setUp(self):
        super().setUp()
        self.delivery.tuning = QUICK            # no waiting between the tries of a test

    def start(self, material_id, **more):
        return self.delivery.start({'project_id': PROJECT, 'robot_id': ROBOT, 'material_id': material_id,
                                    'modify_authorized': True, **more})

    def finished(self, job):
        self.assertTrue(wait_until(lambda: self.delivery.status({'job_id': job['id'], 'project_id': PROJECT})['status'] != 'running'))
        return self.delivery.status({'job_id': job['id'], 'project_id': PROJECT})

    def test_a_started_delivery_runs_to_the_end_and_says_what_it_did(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        job = self.start(material_id)
        self.assertEqual((job['status'], job['robot_id'], job['material_id']), ('running', ROBOT, material_id))
        done = self.finished(job)
        self.assertEqual((done['status'], done['phase'], done['error']), ('delivered', 'done', None))
        self.assertEqual(done['delivery']['device_path'], folder(PACKAGE) + '/driver.deb')
        self.assertEqual(self.device.read(folder(PACKAGE) + '/driver.deb'), PACKAGE)
        listed = self.delivery.list({'project_id': PROJECT, 'robot_id': ROBOT})
        self.assertEqual([record['sha256'] for record in listed['deliveries']], [sha(PACKAGE)])
        self.assertEqual([entry['id'] for entry in listed['jobs']], [job['id']])
        self.assertNotIn(self.cloud.token, json.dumps([done, listed], ensure_ascii=False))

    def test_writing_to_the_device_needs_the_persons_consent(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        for consent in (None, False, 'yes', 1):
            with self.subTest(consent=consent), self.assertRaises(CodedError) as caught:
                self.start(material_id, modify_authorized=consent)
            self.assertEqual(caught.exception.code, 'package_delivery_not_authorized')
        self.assertEqual(self.cloud.requests, [])
        self.assertEqual(self.device.connections, 0)

    def test_what_cannot_be_delivered_is_refused_when_it_is_started(self):
        with self.assertRaises(CodedError) as caught:
            self.start(self.cloud.add('driver.deb', PACKAGE, status='parsing'))
        self.assertEqual(caught.exception.code, 'package_not_usable')
        with self.assertRaises(ValueError):
            self.delivery.start({'project_id': 'another-project', 'robot_id': ROBOT, 'material_id': 'mat_' + '0' * 20,
                                 'modify_authorized': True})
        self.assertEqual(self.delivery.list({'project_id': PROJECT, 'robot_id': ROBOT})['jobs'], [])

    def test_a_failure_is_kept_with_its_code_and_whether_it_can_go_on(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        self.device.offline = True
        failed = self.finished(self.start(material_id))
        self.assertEqual((failed['status'], failed['code'], failed['resumable']), ('failed', 'package_send_interrupted', True))
        self.assertTrue(failed['error'])
        self.device.offline = False

        self.device.corrupt_at = 5000
        failed = self.finished(self.start(material_id))
        self.assertEqual((failed['status'], failed['code'], failed['resumable']), ('failed', 'package_device_checksum_mismatch', False))

        wrong = self.cloud.add('wrong.deb', OTHER, declared_sha256=sha(b'recorded'))
        failed = self.finished(self.start(wrong))
        self.assertEqual((failed['status'], failed['code'], failed['resumable']), ('failed', 'package_checksum_mismatch', False))

    def test_a_delivery_can_be_stopped_and_started_again(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        self.cloud.stall_after = 50_000
        job = self.start(material_id)
        self.assertTrue(wait_until(lambda: self.cloud.served >= 50_000))
        with self.assertRaises(CodedError) as caught:
            self.start(material_id)
        self.assertEqual(caught.exception.code, 'package_busy')
        self.assertEqual(self.delivery.stop({'job_id': job['id'], 'project_id': PROJECT})['id'], job['id'])
        stopped = self.finished(job)
        self.cloud.released.set()
        self.assertEqual((stopped['status'], stopped['resumable'], stopped['code']), ('stopped', True, None))

        done = self.finished(self.start(material_id))
        self.assertEqual(done['status'], 'delivered')
        self.assertGreater(done['delivery']['fetch']['resumed_from'], 0)

    def test_progress_is_reported_in_bytes_of_the_half_that_is_running(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        self.cloud.stall_after = 50_000
        job = self.start(material_id)
        self.assertTrue(wait_until(lambda: self.delivery.status({'job_id': job['id'], 'project_id': PROJECT})['done'] >= 40_000))
        view = self.delivery.status({'job_id': job['id'], 'project_id': PROJECT})
        self.assertEqual((view['phase'], view['total']), ('fetching', len(PACKAGE)))
        self.cloud.released.set()
        self.assertEqual(self.finished(job)['status'], 'delivered')

    def test_a_job_is_only_seen_and_stopped_within_its_project(self):
        job = self.start(self.cloud.add('driver.deb', PACKAGE))
        for other in ('another-project', None):
            with self.subTest(project=other):
                with self.assertRaises(ValueError):
                    self.delivery.status({'job_id': job['id'], 'project_id': other})
                with self.assertRaises(ValueError):
                    self.delivery.stop({'job_id': job['id'], 'project_id': other})
        self.assertEqual(self.finished(job)['status'], 'delivered')

    def test_only_a_few_deliveries_run_at_once(self):
        self.cloud.stall_every = 1                                              # every answer holds until released
        for index in range(package_delivery.MAX_RUNNING):
            self.start(self.cloud.add('driver-%d.deb' % index, random.Random(index).randbytes(20_000)))
        with self.assertRaises(CodedError) as caught:
            self.start(self.cloud.add('one-more.deb', OTHER))
        self.assertEqual(caught.exception.code, 'package_busy')
        self.cloud.released.set()

    def test_a_thread_that_cannot_be_started_leaves_no_job_behind(self):
        import types

        class Refused(threading.Thread):
            def start(self):
                raise RuntimeError("can't start new thread")
        material_id = self.cloud.add('driver.deb', PACKAGE)
        only_here = types.SimpleNamespace(Thread=Refused, Event=threading.Event, Lock=threading.Lock)
        with mock.patch('brainos.package_delivery.threading', only_here):      # the fake cloud's own threads still start
            with self.assertRaises(CodedError) as caught:
                self.start(material_id)
        self.assertEqual(caught.exception.code, 'package_busy')
        self.assertEqual(self.delivery.list({'project_id': PROJECT, 'robot_id': ROBOT})['jobs'], [])
        self.delivery.close()                                                  # nothing half started to wait for

    def test_a_failure_of_the_device_is_not_called_a_broken_link(self):
        bare = UploadDevice(tools=('gzip',))                                    # no sha256sum, shasum or openssl
        self.addCleanup(bare.close)
        self.device = bare
        self.attach(bare.port)
        failed = self.finished(self.start(self.cloud.add('driver.deb', PACKAGE)))
        self.assertEqual((failed['code'], failed['reason'], failed['resumable']), ('package_send_failed', 'no_checksum_tool', False))
        for reason, resumable in (('auth', True), ('fingerprint', True), ('no_sftp', True), ('refused', True), ('no_space', True)):
            with self.subTest(reason=reason):
                view = package_delivery.failure(device_transfer.TransferError('send', reason, 'x', resumable=resumable))
                self.assertEqual((view['code'], view['reason'], view['resumable']), ('package_send_failed', reason, resumable))
        view = package_delivery.failure(device_transfer.TransferError('send', 'interrupted', 'x', resumable=False))
        self.assertEqual((view['code'], view['resumable']), ('package_send_interrupted', True))

    def test_what_ends_after_the_app_began_to_close_leaves_the_store_alone(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        real = device_transfer.send_to_robot

        def closing_meanwhile(*arguments, **options):
            result = real(*arguments, **options)
            self.delivery.closing = True
            return result
        with mock.patch('brainos.package_delivery.device_transfer.send_to_robot', side_effect=closing_meanwhile):
            with self.assertRaises(device_transfer.TransferError) as caught:
                self.deliver(material_id)
        self.assertEqual((caught.exception.reason, caught.exception.resumable), ('cancelled', True))
        self.assertEqual(self.store.list(package_delivery.KIND), [])
        self.assertEqual(self.device.read(folder(PACKAGE) + '/driver.deb'), PACKAGE)

    def test_an_unknown_job_is_an_error(self):
        for bad in ('package-delivery-missing', None, 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.delivery.status({'job_id': bad, 'project_id': PROJECT})

    def test_closing_the_workspace_stops_what_is_running(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        self.cloud.stall_after = 50_000
        job = self.start(material_id)
        self.assertTrue(wait_until(lambda: self.cloud.served >= 50_000))
        self.delivery.close()
        self.cloud.released.set()
        self.assertEqual(self.delivery.status({'job_id': job['id'], 'project_id': PROJECT})['status'], 'stopped')
        with self.assertRaises(ValueError):
            self.start(material_id)


class OverHttp(unittest.TestCase):
    """The routes of the desktop API, as a page would call them."""

    def setUp(self):
        from brainos.httpd import FastBindHTTPServer
        from brainos.server import Application, make_handler
        import secrets
        quiet_paramiko(self)
        self.device = UploadDevice()
        self.addCleanup(self.device.close)
        self.cloud = FakeLibraryCloud()
        self.addCleanup(self.cloud.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Application(Path(self.tmp.name), '')
        self.addCleanup(self.app.close)
        self.httpd = FastBindHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        self.httpd.daemon_threads = True
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.host = '127.0.0.1:%d' % self.httpd.server_port
        made_up = 'desk-%s-7' % secrets.token_hex(8)
        token, user, _ = self.app.identity.sign_in({'email': 'dev@delivery.test', 'password': made_up, 'name': 'Dev'}, register=True)
        self.cookie = 'brain_session=' + token
        workspace = self.app.workspace(user)
        connection, secret = self.device.login()
        workspace.store.put('robots', {'id': ROBOT, 'project_id': PROJECT, 'name': '测试机器人', 'connection_id': CONNECTION,
                                       'product': {'vendor': '示例厂家', 'model': 'X1', 'source': 'manual'}})
        workspace.store.put('device_connections', {**connection, 'id': CONNECTION, 'robot_id': ROBOT, 'project_id': PROJECT})
        workspace.store.put('agent_secrets', {**secret, 'id': CONNECTION})
        self.cloud.sign_in(workspace)

    def call(self, path, payload):
        request = urllib.request.Request('http://%s/api/%s' % (self.host, path), data=json.dumps(payload).encode(),
                                         headers={'Host': self.host, 'Cookie': self.cookie, 'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_a_delivery_is_started_followed_and_listed_over_the_desktop_api(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        target = {'project_id': PROJECT, 'robot_id': ROBOT}
        status, job = self.call('library/deliver/start', {**target, 'material_id': material_id, 'modify_authorized': True})
        self.assertEqual((status, job['status']), (200, 'running'))

        def state():
            return self.call('library/deliver/status', {'job_id': job['id'], 'project_id': PROJECT})[1]
        self.assertTrue(wait_until(lambda: state()['status'] != 'running'))
        self.assertEqual(state()['status'], 'delivered')
        self.assertEqual(self.device.read(folder(PACKAGE) + '/driver.deb'), PACKAGE)
        status, listed = self.call('library/deliver/list', target)
        self.assertEqual((status, [record['device_path'] for record in listed['deliveries']]),
                         (200, [folder(PACKAGE) + '/driver.deb']))
        self.assertNotIn(self.cloud.token, json.dumps([job, state(), listed]))

    def test_refusals_come_back_with_their_code(self):
        target = {'project_id': PROJECT, 'robot_id': ROBOT, 'modify_authorized': True}
        status, body = self.call('library/deliver/start', {**target, 'material_id': self.cloud.add('driver.deb', PACKAGE, status='parsing')})
        self.assertEqual((status, body['code']), (400, 'package_not_usable'))
        status, body = self.call('library/deliver/start', {**target, 'material_id': '../../admin'})
        self.assertEqual(status, 400)
        status, body = self.call('library/deliver/start', {**target, 'modify_authorized': False, 'material_id': 'mat_' + '0' * 20})
        self.assertEqual((status, body['code']), (400, 'package_delivery_not_authorized'))
        status, body = self.call('library/deliver/stop', {'job_id': 'package-delivery-missing', 'project_id': PROJECT})
        self.assertEqual(status, 400)
        self.assertEqual(self.device.connections, 0)


class RobotRemoved(DeliveryCase):
    def test_a_robot_that_was_deleted_meanwhile_gets_no_record(self):
        material_id = self.cloud.add('driver.deb', PACKAGE)
        real = device_transfer.send_to_robot

        def deleted_meanwhile(*arguments, **options):
            result = real(*arguments, **options)
            self.store.delete('robots', ROBOT)
            return result
        with mock.patch('brainos.package_delivery.device_transfer.send_to_robot', side_effect=deleted_meanwhile):
            with self.assertRaises(ValueError) as caught:
                self.deliver(material_id)
        self.assertIn('被删除', str(caught.exception))
        self.assertIn(folder(PACKAGE) + '/driver.deb', str(caught.exception))
        self.assertEqual(self.store.list(package_delivery.KIND), [])

    def test_the_records_are_among_what_goes_with_a_robot(self):
        from brainos.robot_remove import ROBOT_RECORDS
        self.assertIn(package_delivery.KIND, ROBOT_RECORDS)
        record = self.deliver(self.cloud.add('driver.deb', PACKAGE))
        self.platform.robot_remove.remove({'project_id': PROJECT, 'robot_id': ROBOT})
        self.assertEqual(self.store.list(package_delivery.KIND), [])
        self.assertEqual(self.device.read(record['device_path']), PACKAGE)     # the device itself is not contacted


if __name__ == '__main__':
    unittest.main()
