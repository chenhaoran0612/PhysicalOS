"""Issue #18: the product's own channel for putting a file on a device.

The device is ``upload_fixture.UploadDevice``: a local SSH server whose files live in a private
temporary directory. Nothing here reaches a real machine.
"""
import gzip
import hashlib
import logging
import os
import posixpath
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from brainos import device_access, device_transfer
from upload_fixture import HASH_TOOLS, FlakyLink, UploadDevice

HOME = '/home/robot'
DIRECTORY = HOME + '/neuxmind-pkg/runtime'
DEST = '~/neuxmind-pkg/runtime/payload.bin'
ARRIVED = DIRECTORY + '/payload.bin'
CHUNK = 8192
TEXT = b''.join(b'joint %04d position %08.3f torque ok\n' % (i, i * .25) for i in range(6000))    # compresses well
NOISE = random.Random(18).randbytes(120_000)                                                      # does not compress
ROBOT = 'robot-1'


def quiet_paramiko(case):
    """paramiko logs every connection these tests break on purpose, traceback included."""
    logger = logging.getLogger('paramiko')
    level = logger.level
    logger.setLevel(logging.CRITICAL)
    case.addCleanup(logger.setLevel, level)


class TransferCase(unittest.TestCase):
    tools = None

    def setUp(self):
        quiet_paramiko(self)
        self.device = UploadDevice(tools=self.tools)
        self.addCleanup(self.device.close)
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.records = Path(self.work.name) / 'records'
        self.connects = 0

    def connect(self):
        self.connects += 1
        return device_access.SSHDevice(*self.device.login())

    def source(self, data, name='payload.bin'):
        path = Path(self.work.name) / name
        path.write_bytes(data)
        return path

    def send(self, local, remote=DEST, **options):
        options = {'device_key': ROBOT, 'retry_waits': (0, 0, 0), 'chunk_bytes': CHUNK, **options}
        return device_transfer.upload(self.connect, local, remote, self.records, **options)

    def refused(self, local, remote=DEST, **options):
        with self.assertRaises(device_transfer.TransferError) as caught:
            self.send(local, remote, **options)
        return caught.exception

    def status(self, remote=DEST):
        return device_transfer.transfer_status(self.records, ROBOT, remote)

    def record_files(self):
        return sorted(self.records.rglob('*.json')) if self.records.is_dir() else []

    def interrupt(self, local, after=50_000):
        """Cut the connection once ``after`` bytes arrived and keep the device unreachable. Returns the error."""
        self.device.drops, self.device.offline_after_drop = [after], True
        error = self.refused(local)
        self.device.offline, self.device.offline_after_drop = False, False
        return error


class Delivery(TransferCase):
    def test_file_arrives_complete_and_its_checksum_is_verified_on_the_device(self):
        seen = []
        result = self.send(self.source(TEXT), on_progress=seen.append)

        self.assertEqual(self.device.read(ARRIVED), TEXT)
        self.assertEqual((result['path'], result['bytes'], result['sha256'], result['verified']),
                         (ARRIVED, len(TEXT), hashlib.sha256(TEXT).hexdigest(), True))
        self.assertEqual(self.device.names(DIRECTORY), ['payload.bin'])        # no temporary file is left
        self.assertEqual(self.device.mode(ARRIVED), 0o644)
        self.assertIsNone(self.status())                                       # nothing left to resume
        self.assertEqual(self.record_files(), [])
        sent = [event['sent'] for event in seen if event['stage'] == 'sending']
        self.assertGreater(len(sent), 3)                                       # in chunks
        self.assertEqual((sent, sent[-1]), (sorted(sent), len(TEXT)))
        self.assertEqual(seen[-1]['stage'], 'done')
        self.assertEqual([kind for kind, _ in self.device.scripts], ['probe', 'finalize'])
        self.assertEqual(self.device.refused, [])

    def test_text_is_compressed_on_the_way_and_packed_data_is_sent_as_it_is(self):
        packed = self.send(self.source(TEXT))
        self.assertEqual(packed['encoding'], 'gzip')
        self.assertLess(self.device.received, len(TEXT) // 3)
        self.assertEqual(packed['sent_bytes'], self.device.received)

        before = self.device.received
        plain = self.send(self.source(NOISE, 'noise.bin'), '~/neuxmind-pkg/noise.bin')
        self.assertEqual(plain['encoding'], 'identity')
        self.assertEqual(self.device.received - before, len(NOISE))
        self.assertEqual(self.device.read(HOME + '/neuxmind-pkg/noise.bin'), NOISE)

    def test_a_file_that_is_already_there_is_not_sent_again_but_gets_the_requested_mode(self):
        local = self.source(TEXT)
        self.send(local)
        before = self.device.received
        again = self.send(local, mode=0o755)
        self.assertTrue(again['already_present'])
        self.assertTrue(again['verified'])
        self.assertEqual(self.device.received, before)
        self.assertEqual(self.device.mode(ARRIVED), 0o755)

    def test_an_older_file_is_replaced_and_the_requested_mode_is_set(self):
        self.device.write(ARRIVED, b'previous version')
        self.send(self.source(TEXT), mode=0o755)
        self.assertEqual(self.device.read(ARRIVED), TEXT)
        self.assertEqual(self.device.mode(ARRIVED), 0o755)

    def test_a_different_file_of_the_same_size_is_replaced(self):
        self.device.write(ARRIVED, NOISE[::-1])
        result = self.send(self.source(NOISE))
        self.assertFalse(result['already_present'])
        self.assertEqual(self.device.read(ARRIVED), NOISE)

    def test_an_empty_file_can_be_sent(self):
        result = self.send(self.source(b''))
        self.assertEqual(self.device.read(ARRIVED), b'')
        self.assertEqual(result['sha256'], hashlib.sha256(b'').hexdigest())

    def test_a_chunk_larger_than_one_sftp_request_arrives_whole(self):
        result = self.send(self.source(NOISE), chunk_bytes=100_000)
        self.assertEqual(self.device.read(ARRIVED), NOISE)
        self.assertEqual([length for _, _, _, length in self.device.writes], [32768, 32768, 32768, 1696, 20000])
        self.assertEqual(result['sent_bytes'], len(NOISE))

    def test_what_a_login_script_prints_is_not_taken_for_an_answer(self):
        self.device.banner = b'Last login: today\nhash=broken\ngzip=no\ndest=broken\n@@NX-UPLOAD:damaged@@\n'
        result = self.send(self.source(TEXT))
        self.assertEqual((result['verified'], result['encoding']), (True, 'gzip'))
        self.assertEqual(self.device.read(ARRIVED), TEXT)

    def test_the_command_line_holds_only_plain_words(self):
        """Nothing a login shell could read as syntax: the script travels on standard input."""
        self.send(self.source(TEXT))
        for _, arguments in self.device.scripts:
            for word in arguments:
                self.assertRegex(word, r'\A[A-Za-z0-9._+/-]+\Z')
        with self.assertRaises(ValueError):
            device_transfer.target.command('/home/robot/a b')
        with self.assertRaises(ValueError):
            device_transfer.target.command('')


class Resume(TransferCase):
    def test_connection_lost_midway_resumes_from_the_last_confirmed_chunk(self):
        self.device.drops = [50_000]
        seen = []
        result = self.send(self.source(NOISE), on_progress=seen.append)

        self.assertEqual(self.device.read(ARRIVED), NOISE)
        self.assertTrue(result['verified'])
        self.assertEqual((self.device.connections, result['attempts']), (2, 2))
        confirmed = 6 * CHUNK                                                  # the seventh chunk was cut off
        again = [offset for connection, _, offset, _ in self.device.writes if connection == 2]
        self.assertEqual(min(again), confirmed)                               # nothing before it is sent twice
        self.assertEqual(self.device.received, len(NOISE) + CHUNK)
        self.assertEqual(self.device.names(DIRECTORY), ['payload.bin'])
        waiting = [event for event in seen if event['stage'] == 'retrying']
        self.assertEqual([event['sent'] for event in waiting], [confirmed])
        self.assertNotRegex(waiting[0]['error'], r'^\w*(Error|Exception)$')    # a reason a person can read, not a class name

    def test_a_compressed_upload_resumes_too(self):
        self.device.drops = [20_000]
        result = self.send(self.source(TEXT))
        self.assertEqual(self.device.read(ARRIVED), TEXT)
        self.assertEqual(result['attempts'], 2)
        again = [offset for connection, _, offset, _ in self.device.writes if connection == 2]
        self.assertGreater(min(again), 10_000)
        self.assertEqual(self.device.names(DIRECTORY), ['payload.bin'])

    def test_a_later_call_continues_where_the_failed_one_stopped(self):
        local = self.source(NOISE)
        error = self.interrupt(local)

        confirmed = 6 * CHUNK
        self.assertEqual((error.reason, error.sent, error.total, error.resumable), ('interrupted', confirmed, len(NOISE), True))
        self.assertIn(str(confirmed), str(error))                              # says where it stopped
        self.assertIn(str(len(NOISE)), str(error))
        self.assertIn(error.remote_temp, str(error))                           # and what is left on the device
        status = self.status()
        self.assertEqual((status['status'], status['sent'], status['total'], status['remote_temp']),
                         ('interrupted', confirmed, len(NOISE), error.remote_temp))
        self.assertEqual(self.device.names(DIRECTORY), [posixpath.basename(error.remote_temp)])
        self.assertEqual(self.device.mode(error.remote_temp), 0o600)
        self.assertIsNone(self.device.read(ARRIVED))

        before = len(self.device.writes)
        result = self.send(local)
        self.assertEqual(result['resumed_from'], confirmed)
        self.assertEqual(min(offset for _, _, offset, _ in self.device.writes[before:]), confirmed)
        self.assertEqual(self.device.read(ARRIVED), NOISE)
        self.assertEqual(self.device.names(DIRECTORY), ['payload.bin'])
        self.assertIsNone(self.status())

    def test_a_link_too_bad_to_finish_leaves_the_installed_file_whole_and_the_transfer_goes_on_when_it_is_back(self):
        """The 60% clause of the acceptance: no damaged file is left, and it resumes once the network is back."""
        self.device.write(ARRIVED, b'previous version')
        local = self.source(NOISE)
        error = self.interrupt(local)                                          # from here on nothing gets through
        self.assertTrue(error.resumable)
        self.assertEqual(self.device.read(ARRIVED), b'previous version')      # the file in place is whole, and still the old one
        self.assertEqual(self.device.names(DIRECTORY), sorted([posixpath.basename(error.remote_temp), 'payload.bin']))

        result = self.send(local)                                              # the network is back
        self.assertEqual((result['resumed_from'], result['verified']), (error.sent, True))
        self.assertEqual(self.device.read(ARRIVED), NOISE)
        self.assertEqual(self.device.names(DIRECTORY), ['payload.bin'])

    def test_a_lost_answer_to_the_final_check_does_not_send_the_file_again(self):
        self.device.lose_answer = {'finalize'}
        result = self.send(self.source(NOISE))
        self.assertEqual(self.device.read(ARRIVED), NOISE)
        self.assertEqual((result['attempts'], result['verified']), (2, True))
        self.assertEqual(self.device.received, len(NOISE))
        self.assertEqual(self.device.names(DIRECTORY), ['payload.bin'])

    def test_a_partial_file_that_shrank_on_the_device_is_continued_from_what_is_left(self):
        local = self.source(NOISE)
        error = self.interrupt(local)
        self.device.write(error.remote_temp, self.device.read(error.remote_temp)[:20_000])
        result = self.send(local)
        self.assertEqual(result['resumed_from'], 20_000)                       # uncompressed: every byte that is there counts
        self.assertEqual(self.device.read(ARRIVED), NOISE)

    def test_a_compressed_partial_file_that_shrank_is_continued_from_a_whole_chunk(self):
        local = self.source(TEXT)
        error = self.interrupt(local, after=20_000)
        self.device.write(error.remote_temp, self.device.read(error.remote_temp)[:12_345])
        result = self.send(local)
        self.assertTrue(0 < result['resumed_from'] < error.sent)
        self.assertEqual(result['resumed_from'] % CHUNK, 0)
        self.assertEqual(self.device.read(ARRIVED), TEXT)

    def test_a_partial_file_that_is_gone_starts_again(self):
        local = self.source(NOISE)
        error = self.interrupt(local)
        os.remove(self.device.local(error.remote_temp))
        result = self.send(local)
        self.assertEqual(result['resumed_from'], 0)
        self.assertEqual(self.device.read(ARRIVED), NOISE)

    def test_a_new_version_of_the_file_does_not_continue_the_unfinished_older_one(self):
        older = self.interrupt(self.source(NOISE))
        newer = NOISE[::-1]
        self.device.offline = True                     # the newer one cannot even connect yet:
        self.refused(self.source(newer))
        self.assertEqual(self.status()['remote_temp'], older.remote_temp)      # what the older one left is still on record
        self.device.offline = False
        result = self.send(self.source(newer))
        self.assertEqual(result['resumed_from'], 0)
        self.assertEqual(self.device.read(ARRIVED), newer)
        self.assertEqual(self.device.names(DIRECTORY), ['payload.bin'])        # the older partial file went too
        self.assertIsNone(self.status())

    def test_a_device_that_stops_answering_is_given_up_on_and_called_again(self):
        self.device.stall_after = 30_000
        started, seen = time.monotonic(), []
        result = self.send(self.source(NOISE), stall_seconds=1, on_progress=seen.append)
        self.assertEqual(self.device.read(ARRIVED), NOISE)
        self.assertEqual(result['attempts'], 2)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual([event['error'] for event in seen if event['stage'] == 'retrying'], ['设备 1 秒没有任何答复，这次连接已放弃'])

    def test_a_weak_link_that_keeps_breaking_still_delivers_the_file(self):
        link = FlakyLink(self.device.port, cuts=[40_000, 30_000, 55_000, 25_000, 45_000])
        self.addCleanup(link.close)
        connection, secret = self.device.login()

        def connect():
            return device_access.SSHDevice({**connection, 'port': link.port}, secret)

        data = NOISE + NOISE[::-1]
        result = device_transfer.upload(connect, self.source(data), DEST, self.records, device_key=ROBOT,
                                        retry_waits=(0, 0, 0), chunk_bytes=CHUNK)
        self.assertEqual(self.device.read(ARRIVED), data)
        self.assertEqual((result['attempts'], result['verified']), (6, True))
        self.assertLess(self.device.received, len(data) + 5 * 2 * CHUNK)       # each break costs at most the chunk in flight


def chunks_by_connection(seen):
    """The sizes of the chunks each connection got confirmed, in order: one list per connection."""
    runs, position = [[]], 0
    for event in seen:
        if event['stage'] == 'retrying':
            runs.append([])
        elif event['stage'] == 'sending':
            runs[-1].append(event['sent'] - position)
        position = event['sent']
    return runs


class Pace(TransferCase):
    """After a broken connection the chunks start small, so little is in flight if it breaks again,
    and grow back while the device confirms them quickly."""
    LARGE = 128 * 1024
    SMALL = device_transfer.CAREFUL_CHUNK_BYTES
    DATA = random.Random(19).randbytes(600_000)

    def test_after_a_break_the_chunks_start_small_and_grow_back_while_the_device_confirms_quickly(self):
        self.device.drops = [200_000]                                          # in the second chunk
        seen = []
        with mock.patch.object(device_transfer, 'QUICK_SECONDS', 60):
            result = self.send(self.source(self.DATA), chunk_bytes=self.LARGE, on_progress=seen.append)

        first, second = chunks_by_connection(seen)
        self.assertEqual(first, [self.LARGE])
        self.assertEqual(second[:4], [self.SMALL, 2 * self.SMALL, 4 * self.SMALL, self.LARGE])
        self.assertEqual(max(second), self.LARGE)                              # never larger than the caller asked for
        self.assertEqual(sum(first) + sum(second), len(self.DATA))
        self.assertEqual(self.device.read(ARRIVED), self.DATA)
        self.assertEqual((result['attempts'], result['verified']), (2, True))

    def test_after_a_break_the_chunks_stay_small_while_the_device_is_slow_to_confirm(self):
        data = self.DATA[:150_000]
        self.device.drops, self.device.answer_seconds = [50_000], .05         # every write takes longer than "quickly"
        seen = []
        with mock.patch.object(device_transfer, 'QUICK_SECONDS', .02):
            result = self.send(self.source(data), chunk_bytes=self.LARGE, on_progress=seen.append)

        first, second = chunks_by_connection(seen)
        self.assertEqual(first, [])                                            # the first chunk was cut off
        self.assertEqual(set(second[:-1]), {self.SMALL})
        self.assertEqual(sum(second), len(data))
        self.assertEqual(self.device.read(ARRIVED), data)
        self.assertTrue(result['verified'])

    def test_chunks_that_grew_back_shrink_again_once_the_device_gets_slow_to_confirm(self):
        self.device.drops = [50_000]                                           # in the first chunk
        seen = []

        def progress(event):
            seen.append(event)
            if event['stage'] == 'sending' and chunks_by_connection(seen)[-1][-1:] == [self.LARGE]:
                device_transfer.QUICK_SECONDS = 0                              # from here on nothing counts as quick

        with mock.patch.object(device_transfer, 'QUICK_SECONDS', 60):
            result = self.send(self.source(self.DATA), chunk_bytes=self.LARGE, on_progress=progress)

        first, second = chunks_by_connection(seen)
        self.assertEqual(first, [])
        # Up to the size asked for; the chunk after that one is the first to be found slow, and from
        # there each is half the one before, down to the small size and no further.
        self.assertEqual(second[:9], [self.SMALL, 2 * self.SMALL, 4 * self.SMALL, self.LARGE, self.LARGE,
                                      4 * self.SMALL, 2 * self.SMALL, self.SMALL, self.SMALL])
        self.assertEqual(set(second[9:-1]), {self.SMALL})
        self.assertEqual(sum(second), len(self.DATA))
        self.assertEqual(self.device.read(ARRIVED), self.DATA)
        self.assertTrue(result['verified'])

    def test_compressed_chunks_grow_back_the_same_way(self):
        data = TEXT * 3                                                        # a chunk of LARGE packs to about 15 KB
        self.device.drops = [23_000]                                           # in the second chunk
        seen = []
        with mock.patch.object(device_transfer, 'QUICK_SECONDS', 60):
            result = self.send(self.source(data), chunk_bytes=self.LARGE, on_progress=seen.append)

        first, second = chunks_by_connection(seen)
        self.assertEqual(first, [self.LARGE])
        self.assertEqual(second[:4], [self.SMALL, 2 * self.SMALL, 4 * self.SMALL, self.LARGE])
        self.assertEqual(sum(first) + sum(second), len(data))                  # counted in bytes of the file, not of the wire
        self.assertEqual((result['encoding'], result['attempts'], result['verified']), ('gzip', 2, True))
        self.assertEqual(self.device.read(ARRIVED), data)
        self.assertEqual(self.device.names(DIRECTORY), ['payload.bin'])

    def test_every_new_break_starts_small_again(self):
        # The fixture counts every byte it stored, the cut-off chunks too: the first cut comes in the
        # second chunk of LARGE, the second in the chunk of LARGE that follows 16 + 32 + 64 KB.
        self.device.drops = [200_000, 420_000]
        seen = []
        with mock.patch.object(device_transfer, 'QUICK_SECONDS', 60):
            result = self.send(self.source(self.DATA), chunk_bytes=self.LARGE, on_progress=seen.append)

        first, second, third = chunks_by_connection(seen)
        self.assertEqual(second, [self.SMALL, 2 * self.SMALL, 4 * self.SMALL])    # the large one after them was cut off
        self.assertEqual(third[:4], [self.SMALL, 2 * self.SMALL, 4 * self.SMALL, self.LARGE])
        self.assertEqual(sum(first) + sum(second) + sum(third), len(self.DATA))
        self.assertEqual(self.device.read(ARRIVED), self.DATA)
        self.assertEqual((result['attempts'], result['verified']), (3, True))

    def test_an_unbroken_transfer_keeps_the_chunk_size_it_was_given_however_slow_the_device_is(self):
        seen = []
        with mock.patch.object(device_transfer, 'QUICK_SECONDS', 0):          # nothing counts as quick
            self.send(self.source(self.DATA), chunk_bytes=self.LARGE, on_progress=seen.append)
        (only,) = chunks_by_connection(seen)
        self.assertEqual(set(only[:-1]), {self.LARGE})

    def test_a_chunk_size_below_the_small_one_is_kept_after_a_break(self):
        for quick_seconds in (60, 0):                                          # whether the device is found quick or slow
            with self.subTest(quick_seconds=quick_seconds), mock.patch.object(device_transfer, 'QUICK_SECONDS', quick_seconds):
                self.device.drops = [self.device.received + 50_000]
                seen = []
                self.send(self.source(NOISE[::-1] if quick_seconds else NOISE), on_progress=seen.append)    # chunks of CHUNK
                first, second = chunks_by_connection(seen)
                self.assertEqual(set(first) | set(second[:-1]), {CHUNK})


class Stopping(TransferCase):
    def test_a_stop_request_ends_the_transfer_and_keeps_what_arrived(self):
        local, stop = self.source(NOISE), threading.Event()

        def halt(event):
            if event['stage'] == 'sending' and event['sent'] >= 4 * CHUNK:
                stop.set()

        error = self.refused(local, on_progress=halt, stop=stop)
        self.assertEqual((error.reason, error.sent, error.resumable), ('cancelled', 4 * CHUNK, True))
        self.assertEqual(self.status()['status'], 'cancelled')
        self.assertEqual(self.send(local)['resumed_from'], 4 * CHUNK)
        self.assertEqual(self.device.read(ARRIVED), NOISE)

    def test_a_stop_request_does_not_wait_for_a_silent_device(self):
        self.device.stall_after = 30_000
        stop = threading.Event()
        timer = threading.Timer(.5, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        error = self.refused(self.source(NOISE), stop=stop, stall_seconds=60)
        self.assertEqual(error.reason, 'cancelled')
        self.assertLess(time.monotonic() - started, 8)

    def test_unfinished_transfers_of_a_device_can_be_listed_and_given_up(self):
        self.interrupt(self.source(NOISE))
        listed = device_transfer.unfinished(self.records, ROBOT)
        self.assertEqual([(entry['remote_path'], entry['status'], entry['sent']) for entry in listed], [(DEST, 'interrupted', 6 * CHUNK)])
        self.assertEqual(device_transfer.unfinished(self.records, 'another-robot'), [])

        device_transfer.discard(self.connect, DEST, self.records, device_key=ROBOT)
        self.assertEqual(self.device.names(DIRECTORY), [])
        self.assertIsNone(self.status())
        self.assertEqual(device_transfer.unfinished(self.records, ROBOT), [])

    def test_giving_up_keeps_the_record_while_the_device_cannot_be_cleaned(self):
        error = self.interrupt(self.source(NOISE))
        self.device.offline = True
        with self.assertRaises(device_transfer.TransferError) as caught:
            device_transfer.discard(self.connect, DEST, self.records, device_key=ROBOT)
        self.assertEqual(caught.exception.stage, 'connect')
        self.assertEqual(self.status()['remote_temp'], error.remote_temp)      # still known, to be cleaned later

    def test_giving_up_does_not_touch_the_file_in_place_or_another_destination(self):
        self.device.write(ARRIVED, b'previous version')
        other = self.source(NOISE, 'other.bin')
        self.send(other, '~/neuxmind-pkg/runtime/other.bin')
        # names close to the temporary files', which someone else may have put there
        bystanders = ['.payload.bin.nxpart-notes', '.payload.bin.nxtmp-draft', '.payload.bin.nxpartial', '.payload.binx.nxpart-' + '0' * 16]
        for name in bystanders:
            self.device.write(DIRECTORY + '/' + name, b'not a transfer')
        error = self.interrupt(self.source(NOISE))
        self.assertIn(posixpath.basename(error.remote_temp), self.device.names(DIRECTORY))
        device_transfer.discard(self.connect, DEST, self.records, device_key=ROBOT)
        self.assertEqual(self.device.names(DIRECTORY), sorted([*bystanders, 'other.bin', 'payload.bin']))
        self.assertEqual(self.device.read(ARRIVED), b'previous version')


class Verification(TransferCase):
    def test_a_file_that_differs_on_the_device_is_rejected_and_nothing_of_it_is_left(self):
        self.device.write(ARRIVED, b'previous version')
        self.device.corrupt_at = 70_000
        local = self.source(NOISE)
        error = self.refused(local)

        self.assertEqual((error.stage, error.reason, error.resumable, error.remote_temp), ('verify', 'checksum', False, None))
        self.assertIn(hashlib.sha256(NOISE).hexdigest()[:12], str(error))
        self.assertEqual(self.device.read(ARRIVED), b'previous version')      # the file in place was not touched
        self.assertEqual(self.device.names(DIRECTORY), ['payload.bin'])
        self.assertIsNone(self.status())
        self.assertEqual(self.device.connections, 1)                           # a wrong checksum is not retried

        result = self.send(local)                                              # a new transfer starts from nothing
        self.assertEqual(result['resumed_from'], 0)
        self.assertEqual(self.device.read(ARRIVED), NOISE)

    def test_compressed_content_that_unpacks_to_something_else_is_rejected(self):
        def swap(kind, arguments):
            if kind == 'finalize':
                self.device.write(arguments[0], gzip.compress(TEXT[::-1]))

        self.device.before_script = swap
        error = self.refused(self.source(TEXT))
        self.assertEqual((error.stage, error.reason), ('verify', 'checksum'))
        self.assertIsNone(self.device.read(ARRIVED))
        self.assertEqual(self.device.names(DIRECTORY), [])

    def test_compressed_content_that_arrived_damaged_is_rejected(self):
        members = [len(gzip.compress(TEXT[start:start + CHUNK], 6, mtime=0)) for start in range(0, 3 * CHUNK, CHUNK)]
        self.device.corrupt_at = sum(members[:2]) + members[2] // 2            # in the middle of the third chunk's data
        error = self.refused(self.source(TEXT))
        self.assertEqual((error.stage, error.reason, error.resumable), ('verify', 'damaged', False))
        self.assertIsNone(self.device.read(ARRIVED))
        self.assertEqual(self.device.names(DIRECTORY), [])
        self.assertIsNone(self.status())

    def test_no_room_to_unpack_keeps_what_was_uploaded(self):
        local = self.source(TEXT)
        self.device.write(ARRIVED, b'previous version')
        mend = self.device.break_unpacking()
        error = self.refused(local)
        self.assertEqual((error.stage, error.reason, error.resumable), ('verify', 'no_space', True))
        self.assertIn('No space left', str(error))                             # what the device said, passed on
        self.assertEqual(self.device.names(DIRECTORY), sorted([posixpath.basename(error.remote_temp), 'payload.bin']))
        self.assertEqual(self.device.read(ARRIVED), b'previous version')
        self.assertEqual(self.device.connections, 1)

        mend()                                                                 # room was made: nothing is sent again
        before = self.device.received
        result = self.send(local)
        self.assertEqual((result['resumed_from'], self.device.received), (len(TEXT), before))
        self.assertEqual(self.device.read(ARRIVED), TEXT)
        self.assertEqual(self.device.names(DIRECTORY), ['payload.bin'])

    def test_a_full_device_is_reported_with_the_position_and_not_retried(self):
        self.device.full_after = 40_000
        error = self.refused(self.source(NOISE))
        self.assertEqual((error.stage, error.reason, error.sent), ('send', 'refused', 4 * CHUNK))
        self.assertEqual(self.device.connections, 1)
        self.assertIsNone(self.device.read(ARRIVED))

    def test_a_piece_refused_in_the_middle_of_a_chunk_is_noticed(self):
        """Pieces of one chunk go out without waiting for each answer; a refusal among them must still surface."""
        self.device.full_after = 40_000
        error = self.refused(self.source(NOISE), chunk_bytes=100_000)
        self.assertEqual((error.stage, error.reason, error.sent), ('send', 'refused', 0))
        self.assertEqual(self.device.connections, 1)

    def test_a_source_file_changed_while_it_was_being_sent_is_not_delivered(self):
        local = self.source(NOISE)

        def rewrite(event):
            if event['stage'] == 'sending' and event['sent'] == 3 * CHUNK:
                local.write_bytes(NOISE[::-1])
                os.utime(local, ns=(1, 1))

        error = self.refused(local, on_progress=rewrite)
        self.assertEqual((error.stage, error.reason, error.resumable, error.sent), ('send', 'source_changed', False, 3 * CHUNK))
        self.assertEqual(self.device.received, 3 * CHUNK)                      # nothing of the changed file left this computer
        self.assertIsNone(self.device.read(ARRIVED))
        self.assertEqual(self.device.names(DIRECTORY), [])
        self.assertIsNone(self.status())

    def test_another_file_put_in_the_place_of_the_source_is_not_sent(self):
        local = self.source(NOISE)
        other = Path(self.work.name) / 'private.bin'
        other.write_bytes(bytes(len(NOISE)))

        def swap(event):
            if event['stage'] == 'sending' and event['sent'] == 3 * CHUNK:
                os.replace(other, local)

        error = self.refused(local, on_progress=swap)
        self.assertEqual((error.reason, self.device.received), ('source_changed', 3 * CHUNK))
        self.assertEqual(self.device.names(DIRECTORY), [])

    def test_a_source_replaced_before_its_first_chunk_is_reported_the_same_way(self):
        local = self.source(TEXT)
        other = Path(self.work.name) / 'private.bin'
        other.write_bytes(TEXT[::-1])

        def swap(kind, arguments):
            if kind == 'probe' and other.exists():
                os.replace(other, local)

        self.device.before_script = swap
        error = self.refused(local)
        self.assertEqual((error.stage, error.reason, self.device.received), ('send', 'source_changed', 0))
        self.assertEqual(self.device.names(DIRECTORY), [])

    def test_a_file_that_cannot_be_moved_into_place_is_reported_and_stays_private(self):
        tool = os.path.join(self.device.bin, 'mv')
        os.remove(tool)
        with open(tool, 'w') as handle:
            handle.write('#!/bin/sh\necho "mv: Operation not permitted" >&2\nexit 1\n')
        os.chmod(tool, 0o755)
        error = self.refused(self.source(NOISE), mode=0o755)
        self.assertEqual((error.stage, error.reason, error.resumable), ('verify', 'unplaced', True))
        self.assertIn('Operation not permitted', str(error))
        self.assertEqual(self.device.mode(error.remote_temp), 0o600)           # not left readable or executable by others
        self.assertIsNone(self.device.read(ARRIVED))


class DeviceTools(unittest.TestCase):
    """What the device needs: SFTP, a SHA-256 tool, and gzip only when the content is worth compressing."""

    def device(self, *tools):
        quiet_paramiko(self)
        device = UploadDevice(tools=tools)
        self.addCleanup(device.close)
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        local = Path(work.name) / 'payload.bin'
        local.write_bytes(TEXT)

        def send():
            return device_transfer.upload(lambda: device_access.SSHDevice(*device.login()), local, DEST, Path(work.name) / 'records',
                                          device_key=ROBOT, retry_waits=(0,), chunk_bytes=CHUNK)

        return device, send

    def refused(self, send):
        with self.assertRaises(device_transfer.TransferError) as caught:
            send()
        return caught.exception.stage, caught.exception.reason

    def test_every_checksum_tool_a_device_may_have_gives_the_same_answer(self):
        self.assertTrue(HASH_TOOLS)
        for tool in HASH_TOOLS:
            with self.subTest(tool=tool):
                device, send = self.device('gzip', tool)
                self.assertTrue(send()['verified'])
                self.assertEqual(device.read(ARRIVED), TEXT)

    def test_a_device_without_gzip_gets_the_file_uncompressed(self):
        device, send = self.device(HASH_TOOLS[0])
        result = send()
        self.assertEqual((result['encoding'], result['verified']), ('identity', True))
        self.assertEqual(device.received, len(TEXT))
        self.assertEqual(device.read(ARRIVED), TEXT)

    def test_a_device_without_sftp_is_told_apart_from_a_broken_connection(self):
        device, send = self.device('gzip', HASH_TOOLS[0])
        device.sftp = False
        self.assertEqual(self.refused(send), ('connect', 'no_sftp'))
        self.assertEqual(device.connections, 1)                                # asking again would get the same answer

    def test_an_sftp_service_that_hides_what_a_path_is_gets_nothing(self):
        device, send = self.device('gzip', HASH_TOOLS[0])
        device.write(HOME + '/neuxmind-pkg/placeholder', b'')
        device.hide_modes = True
        self.assertEqual(self.refused(send), ('connect', 'no_sftp'))
        self.assertEqual(device.writes, [])

    def test_a_device_that_cannot_compute_a_checksum_is_not_sent_anything(self):
        device, send = self.device('gzip')
        self.assertEqual(self.refused(send), ('verify', 'no_checksum_tool'))
        self.assertEqual(device.writes, [])
        self.assertEqual(device.names(HOME + '/neuxmind-pkg/runtime'), [])


if __name__ == '__main__':
    unittest.main()
