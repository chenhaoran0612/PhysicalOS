"""Issue #31, the first half: a driver package comes from the cloud library to this computer.

The cloud is ``package_cloud_fixture.FakeLibraryCloud``, a local HTTP server whose files are made
up here. Nothing reaches a real server.
"""
import os
import random
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from brainos import package_fetch
from brainos.cloud_account import CloudError
from package_cloud_fixture import FakeLibraryCloud, sha, wait_until

PACKAGE = random.Random(31).randbytes(300_000)          # does not compress, like most packages
BLOCK = 8192


class FetchCase(unittest.TestCase):
    def setUp(self):
        self.cloud = FakeLibraryCloud()
        self.addCleanup(self.cloud.close)
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.cache = Path(self.work.name) / 'package-cache'
        self.material_id = self.cloud.add('driver_1.0_arm64.deb', PACKAGE)

    def source(self):
        return {'base_url': self.cloud.url, 'token': self.cloud.token}

    def fetch(self, material_id=None, **options):
        options = {'retry_waits': (0, 0, 0), 'block_bytes': BLOCK, **options}
        return package_fetch.fetch(self.source(), self.cloud.view(material_id or self.material_id), self.cache, **options)

    def refused(self, material_id=None, **options):
        with self.assertRaises(package_fetch.FetchError) as caught:
            self.fetch(material_id, **options)
        return caught.exception

    def interrupt(self, after=100_000):
        """Cut the download once ``after`` bytes went out and keep the cloud unreachable. Returns the error."""
        self.cloud.cuts, self.cloud.offline_after_cut = [after], True
        error = self.refused()
        self.cloud.offline, self.cloud.offline_after_cut = False, False
        return error

    def cached(self):
        return sorted(path.name for path in self.cache.iterdir()) if self.cache.is_dir() else []

    def asked(self):
        return [asked for _, asked in self.cloud.content_requests()]


class Fetching(FetchCase):
    def test_a_package_arrives_whole_and_its_checksum_is_verified_here(self):
        seen = []
        result = self.fetch(on_progress=seen.append)

        self.assertEqual(Path(result['path']).read_bytes(), PACKAGE)
        self.assertEqual((result['bytes'], result['sha256'], result['resumed_from'], result['cached']),
                         (len(PACKAGE), sha(PACKAGE), 0, False))
        self.assertEqual(self.cached(), [sha(PACKAGE) + '.pkg'])               # no unfinished file is left
        received = [event['received'] for event in seen if event['stage'] == 'fetching']
        self.assertGreater(len(received), 3)                                   # in blocks
        self.assertEqual((received, received[-1]), (sorted(received), len(PACKAGE)))
        self.assertEqual([event['stage'] for event in seen][-2:], ['verifying', 'fetched'])
        self.assertEqual(self.asked(), [None])                                 # one request, no range

    def test_the_copy_on_this_computer_is_private_and_not_writable(self):
        result = self.fetch()
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(result['path']).st_mode), 0o400)

    def test_a_package_that_is_already_here_is_checked_again_and_not_fetched_twice(self):
        self.fetch()
        again = self.fetch()
        self.assertTrue(again['cached'])
        self.assertEqual(len(self.asked()), 1)
        self.assertEqual(self.cloud.served, len(PACKAGE))

    def test_a_copy_that_was_damaged_on_this_computer_is_thrown_away_and_fetched_again(self):
        first = self.fetch()
        os.chmod(first['path'], 0o600)
        with open(first['path'], 'r+b') as handle:
            handle.seek(1000)
            handle.write(b'\x00' * 8)
        again = self.fetch()
        self.assertFalse(again['cached'])
        self.assertEqual(Path(again['path']).read_bytes(), PACKAGE)

    def test_the_token_goes_in_the_header_and_nowhere_else(self):
        seen = []
        result = self.fetch(on_progress=seen.append)
        self.assertNotIn(self.cloud.token, repr((result, seen)))
        for name in self.cached():
            self.assertNotIn(self.cloud.token.encode(), (self.cache / name).read_bytes())


class Resume(FetchCase):
    def test_a_fetch_that_was_cut_goes_on_in_the_same_call(self):
        self.cloud.cuts = [100_000]
        seen = []
        result = self.fetch(on_progress=seen.append)

        self.assertEqual(Path(result['path']).read_bytes(), PACKAGE)
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(self.asked(), [None, 'bytes=100000-'])
        self.assertEqual(self.cloud.served, len(PACKAGE))                      # nothing was fetched twice
        self.assertIn('retrying', [event['stage'] for event in seen])

    def test_a_fetch_that_was_interrupted_goes_on_in_a_later_call(self):
        error = self.interrupt(100_000)

        self.assertEqual((error.code, error.resumable, error.received, error.total),
                         ('package_fetch_interrupted', True, 100_000, len(PACKAGE)))
        self.assertIn('100000', str(error))
        self.assertEqual(self.cached(), [sha(PACKAGE) + '.part'])
        self.assertEqual((self.cache / self.cached()[0]).read_bytes(), PACKAGE[:100_000])

        result = self.fetch()
        self.assertEqual((result['resumed_from'], result['received_bytes']), (100_000, len(PACKAGE) - 100_000))
        self.assertEqual(Path(result['path']).read_bytes(), PACKAGE)
        self.assertEqual(self.cloud.served, len(PACKAGE))
        self.assertEqual(self.cached(), [sha(PACKAGE) + '.pkg'])

    def test_a_link_that_keeps_breaking_still_delivers_the_whole_package(self):
        self.cloud.cuts = [30_000] * 9                                          # every connection carries 30 KB at most
        result = self.fetch()
        self.assertEqual(Path(result['path']).read_bytes(), PACKAGE)
        self.assertEqual(result['attempts'], 10)
        self.assertEqual(self.cloud.served, len(PACKAGE))

    def test_progress_resets_the_count_of_failures_but_none_at_all_ends_the_call(self):
        self.cloud.cuts = [30_000] * 2
        self.cloud.offline_after_cut = False
        result = self.fetch(retry_waits=(0,))                                  # one try after a failure, yet two cuts pass:
        self.assertEqual((result['attempts'], Path(result['path']).read_bytes()), (3, PACKAGE))   # each came after progress
        self.cloud.offline = True
        other = self.cloud.add('other.deb', PACKAGE[::-1])
        error = self.refused(other, retry_waits=(0, 0))
        self.assertEqual((error.code, error.resumable, error.received), ('package_fetch_interrupted', True, 0))

    def test_a_cloud_that_ignores_the_range_gets_the_file_from_the_start(self):
        self.interrupt(100_000)
        self.cloud.ignore_range = True
        result = self.fetch()
        self.assertEqual(Path(result['path']).read_bytes(), PACKAGE)
        self.assertEqual(result['resumed_from'], 0)

    def test_a_range_that_starts_elsewhere_is_not_appended(self):
        self.interrupt(100_000)
        self.cloud.shift_range = 7                                             # asked from 100000, answered from 100007
        result = self.fetch()
        self.assertEqual(Path(result['path']).read_bytes(), PACKAGE)
        self.assertEqual(self.asked()[-2:], ['bytes=100000-', None])           # what was here is dropped, then all of it again

    def test_an_unfinished_file_that_is_longer_than_the_package_is_not_trusted(self):
        self.cache.mkdir(mode=0o700)
        (self.cache / (sha(PACKAGE) + '.part')).write_bytes(PACKAGE + b'more')
        result = self.fetch()
        self.assertEqual((result['resumed_from'], Path(result['path']).read_bytes()), (0, PACKAGE))

    def test_an_unfinished_file_that_is_complete_is_only_checked(self):
        self.cache.mkdir(mode=0o700)
        (self.cache / (sha(PACKAGE) + '.part')).write_bytes(PACKAGE)
        result = self.fetch()
        self.assertEqual(self.asked(), [])
        self.assertEqual(Path(result['path']).read_bytes(), PACKAGE)

    def test_a_restart_from_the_start_reports_its_progress_from_the_start(self):
        self.cloud.cuts = [200_000]
        self.cloud.ignore_range = True                                          # the second answer is the whole file again
        seen = []
        self.fetch(on_progress=seen.append)
        after = [event['received'] for event in seen[[event['stage'] for event in seen].index('retrying'):] if event['stage'] == 'fetching']
        self.assertLess(after[0], 200_000)
        self.assertEqual(after, sorted(after))

    def test_a_refused_range_and_a_range_answer_that_does_not_say_where_start_again(self):
        self.interrupt(100_000)
        self.cloud.refuse_range_once = True                                     # 416
        result = self.fetch()
        self.assertEqual((result['resumed_from'], Path(result['path']).read_bytes()), (0, PACKAGE))
        self.assertEqual(self.asked()[-2:], ['bytes=100000-', None])

        other = self.cloud.add('other.deb', PACKAGE[::-1])
        self.cloud.cuts, self.cloud.offline_after_cut = [100_000], True
        self.refused(other)
        self.cloud.offline, self.cloud.offline_after_cut = False, False
        self.cloud.drop_content_range = True                                    # 206 without Content-Range
        self.assertEqual(Path(self.fetch(other)['path']).read_bytes(), PACKAGE[::-1])

    def test_a_cloud_that_stops_sending_is_given_up_and_asked_again(self):
        self.cloud.stall_after = 50_000
        started = time.monotonic()
        result = self.fetch(stall_seconds=.5)
        self.cloud.released.set()
        self.assertEqual(Path(result['path']).read_bytes(), PACKAGE)
        self.assertGreaterEqual(result['attempts'], 2)
        self.assertLess(time.monotonic() - started, 15)

    def test_a_server_error_is_tried_again_and_a_refusal_is_not(self):
        self.cloud.failures = [503, 502]
        self.assertEqual(self.fetch()['attempts'], 3)

        gone = self.cloud.add('gone.deb', PACKAGE[:5000])
        self.cloud.failures = [404]
        with self.assertRaises(CloudError) as caught:
            self.fetch(gone)
        self.assertEqual((caught.exception.status, caught.exception.code), (404, 'content_missing'))
        self.assertEqual(len([path for path, _ in self.cloud.content_requests() if gone in path]), 1)


class Stopping(FetchCase):
    def test_a_stop_request_ends_the_fetch_and_keeps_what_arrived(self):
        stop = threading.Event()

        def watch(event):
            if event['stage'] == 'fetching' and event['received'] >= 60_000:
                stop.set()
        error = self.refused(on_progress=watch, stop=stop)
        self.assertEqual((error.code, error.resumable), ('package_fetch_cancelled', True))
        self.assertGreaterEqual(error.received, 60_000)
        self.assertEqual(self.cached(), [sha(PACKAGE) + '.part'])
        result = self.fetch()
        self.assertEqual(Path(result['path']).read_bytes(), PACKAGE)
        self.assertEqual(result['resumed_from'], error.received)

    def test_a_stop_request_is_obeyed_while_the_cloud_is_silent(self):
        self.cloud.stall_after = 50_000
        stop, outcome = threading.Event(), {}

        def run():
            try:
                self.fetch(stop=stop, stall_seconds=60)
            except package_fetch.FetchError as exc:
                outcome['error'] = exc
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(wait_until(lambda: self.cloud.served >= 50_000))
        time.sleep(.3)
        started = time.monotonic()
        stop.set()
        thread.join(10)
        self.cloud.released.set()
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(outcome['error'].code, 'package_fetch_cancelled')

    def test_a_stop_request_is_obeyed_while_an_answer_without_a_length_is_silent(self):
        for kind in ('close_delimited', 'open_ended', 'chunked'):
            with self.subTest(kind=kind):
                setattr(self.cloud, kind, True)
                self.cloud.stall_after, self.cloud.served = 50_000, 0
                self.cloud.released.clear()
                stop, outcome = threading.Event(), {}

                def run():
                    try:
                        self.fetch(stop=stop, stall_seconds=60)
                    except package_fetch.FetchError as exc:
                        outcome['error'] = exc
                thread = threading.Thread(target=run)
                thread.start()
                self.assertTrue(wait_until(lambda: self.cloud.served >= 50_000))
                time.sleep(.3)
                started = time.monotonic()
                stop.set()
                thread.join(10)
                self.cloud.released.set()
                self.assertFalse(thread.is_alive())
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(outcome['error'].code, 'package_fetch_cancelled')
                setattr(self.cloud, kind, False)
                package_fetch.forget(self.cache, sha(PACKAGE))

    def test_a_stop_that_was_already_asked_for_fetches_nothing(self):
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.refused(stop=stop).code, 'package_fetch_cancelled')
        self.assertEqual(self.asked(), [])


class Verification(FetchCase):
    def test_bytes_that_do_not_match_the_library_checksum_are_refused_and_removed(self):
        wrong = self.cloud.add('tampered.deb', PACKAGE, declared_sha256=sha(b'what the library recorded'))
        error = self.refused(wrong)
        self.assertEqual((error.code, error.resumable), ('package_checksum_mismatch', False))
        self.assertIn(sha(PACKAGE), str(error))                                # what arrived
        self.assertIn(sha(b'what the library recorded'), str(error))            # what was expected
        self.assertEqual(self.cached(), [])

    def test_a_checksum_header_that_differs_from_the_record_stops_the_fetch_at_once(self):
        self.cloud.header_checksum = sha(b'another file')
        error = self.refused()
        self.assertEqual((error.code, error.resumable, error.received), ('package_content_changed', False, 0))
        self.assertEqual(self.cached(), [])                                    # the header was enough: nothing was kept
        self.assertEqual(len(self.asked()), 1)                                 # and it was not asked again

    def test_a_file_of_another_length_than_recorded_is_not_accepted(self):
        short = self.cloud.add('short.deb', PACKAGE[:5000], declared_size=6000)
        self.assertEqual(self.refused(short).code, 'package_content_changed')
        for kind in ('close_delimited', 'chunked'):
            with self.subTest(kind=kind):
                setattr(self.cloud, kind, True)
                self.cloud.pad = b'trailing bytes'
                error = self.refused()
                self.assertEqual(error.code, 'package_content_changed')
                self.assertEqual(self.cached(), [])
                setattr(self.cloud, kind, False)

    def test_a_response_without_a_length_is_read_to_its_end_and_checked(self):
        for kind in ('close_delimited', 'chunked'):
            with self.subTest(kind=kind):
                setattr(self.cloud, kind, True)
                self.assertEqual(Path(self.fetch()['path']).read_bytes(), PACKAGE)
                package_fetch.forget(self.cache, sha(PACKAGE))
                setattr(self.cloud, kind, False)

    def test_an_answer_without_a_length_on_a_connection_that_stays_open_is_not_waited_for(self):
        self.cloud.open_ended = True
        started = time.monotonic()
        result = self.fetch(stall_seconds=20)
        self.cloud.released.set()
        self.assertEqual(Path(result['path']).read_bytes(), PACKAGE)
        self.assertLess(time.monotonic() - started, 10)                        # the probe after the last byte waits a second, not the stall time

    def test_a_record_that_makes_no_sense_is_refused_before_anything_is_asked(self):
        view = self.cloud.view(self.material_id)
        for change in ({'sha256': 'abc'}, {'sha256': '../' + 'a' * 61}, {'sha256': None}, {'size': 0}, {'size': -1},
                       {'size': '300000'}, {'size': True}, {'size': package_fetch.MAX_PACKAGE_BYTES + 1},
                       {'id': '../../admin'}, {'id': None}):
            with self.subTest(change=change):
                with self.assertRaises(package_fetch.FetchError) as caught:
                    package_fetch.fetch(self.source(), {**view, **change}, self.cache)
                self.assertEqual(caught.exception.code, 'package_record_invalid')
        self.assertEqual(self.cloud.requests, [])
        self.assertEqual(self.cached(), [])

    def test_a_login_that_ended_is_said_and_not_retried(self):
        with self.assertRaises(CloudError) as caught:
            package_fetch.fetch({'base_url': self.cloud.url, 'token': 'nxs_not_the_one'}, self.cloud.view(self.material_id),
                                self.cache, retry_waits=(0, 0))
        self.assertEqual((caught.exception.status, caught.exception.code), (401, 'unauthorized'))
        self.assertNotIn('nxs_not_the_one', str(caught.exception))

    def test_the_clouds_own_words_are_cleaned_and_cut_before_they_are_shown(self):
        self.cloud.failures = [(403, {'error': 'nope \x1b[31mred\u202e' + 'x' * 150_000, 'code': 'forbidden'})]
        with self.assertRaises(CloudError) as caught:
            self.fetch()
        text = str(caught.exception)
        self.assertLessEqual(len(text), package_fetch.SHOWN_CHARS)
        self.assertNotIn('\x1b', text)
        self.assertNotIn('\u202e', text)
        self.assertEqual(caught.exception.body, {})

    def test_an_address_the_policy_refuses_is_not_asked_again_and_a_name_that_does_not_resolve_is(self):
        from brainos import cloud_account
        refused = mock.Mock(side_effect=ValueError('云平台地址解析到内网或链路本地地址 10.0.0.1；默认不允许。'))
        with mock.patch.object(cloud_account._CheckedHTTPConnection, 'connect', refused):
            with self.assertRaises(CloudError) as caught:
                self.fetch()
        self.assertIn('不允许', str(caught.exception))
        self.assertEqual(refused.call_count, 1)

        unresolved = mock.Mock(side_effect=ValueError(package_fetch.UNRESOLVED))
        with mock.patch.object(cloud_account._CheckedHTTPConnection, 'connect', unresolved):
            error = self.refused(retry_waits=(0, 0))
        self.assertEqual(error.code, 'package_fetch_interrupted')
        self.assertEqual(unresolved.call_count, 3)

    def test_a_progress_handler_that_raises_is_not_taken_for_the_network(self):
        def broken(event):
            if event['stage'] == 'fetching':
                raise KeyError('the page went away')
        with self.assertRaises(package_fetch.ProgressHandlerError):
            self.fetch(on_progress=broken)
        self.assertEqual(len(self.asked()), 1)
        self.assertEqual(Path(self.fetch()['path']).read_bytes(), PACKAGE)    # what arrived was kept

    def test_a_redirect_is_not_followed(self):
        self.cloud.failures = [302]
        with self.assertRaises(CloudError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(len(self.asked()), 1)


class ThisComputer(FetchCase):
    def test_a_disk_without_room_is_noticed_before_the_fetch(self):
        usage = mock.Mock(free=len(PACKAGE) - 1)
        with mock.patch('brainos.package_fetch.shutil.disk_usage', return_value=usage):
            error = self.refused()
        self.assertEqual((error.code, error.resumable), ('package_no_local_space', True))
        self.assertEqual(self.asked(), [])

    def test_a_write_that_fails_here_is_not_taken_for_a_network_problem(self):
        real = package_fetch._append
        calls = []

        def failing(fd, data):
            calls.append(len(data))
            if len(calls) == 3:
                raise OSError(28, 'No space left on device')
            return real(fd, data)
        with mock.patch('brainos.package_fetch._append', side_effect=failing):
            error = self.refused()
        self.assertEqual((error.code, error.resumable), ('package_cache_unusable', True))
        self.assertEqual(len(self.asked()), 1)                                 # not asked again
        self.assertEqual(Path(self.fetch()['path']).read_bytes(), PACKAGE)     # room again: it goes on

    def test_a_disk_that_takes_a_little_at_a_time_still_gets_every_byte(self):
        real = os.write
        with mock.patch('brainos.package_fetch.os.write', side_effect=lambda fd, data: real(fd, bytes(data[:100]))):
            result = self.fetch()
        self.assertEqual(Path(result['path']).read_bytes(), PACKAGE)

    def test_a_stop_request_is_obeyed_while_the_copy_is_being_checked(self):
        self.fetch()
        stop = threading.Event()
        real = package_fetch._digest

        def slow(path, stopped=None):
            stop.set()                                                         # asked to stop while hashing
            return real(path, stopped)
        with mock.patch('brainos.package_fetch._digest', side_effect=slow):
            self.assertEqual(self.refused(stop=stop).code, 'package_fetch_cancelled')
        self.assertEqual(self.cached(), [sha(PACKAGE) + '.pkg'])               # the verified copy stays

    def test_a_link_in_the_place_of_the_cache_or_of_a_file_is_not_written_through(self):
        outside = Path(self.work.name) / 'outside'
        outside.mkdir()
        self.cache.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.refused().code, 'package_cache_unusable')
        self.assertEqual(list(outside.iterdir()), [])
        self.cache.unlink()

        self.cache.mkdir(mode=0o700)
        victim = outside / 'victim'
        victim.write_bytes(b'keep me')
        (self.cache / (sha(PACKAGE) + '.part')).symlink_to(victim)
        self.assertEqual(self.refused().code, 'package_cache_unusable')
        self.assertEqual(victim.read_bytes(), b'keep me')

    def test_old_leftovers_are_swept_and_fresh_ones_stay(self):
        self.cache.mkdir(mode=0o700)
        old, fresh, other = self.cache / ('a' * 64 + '.part'), self.cache / ('b' * 64 + '.pkg'), self.cache / 'notes.txt'
        for path in (old, fresh, other):
            path.write_bytes(b'x')
        long_ago = time.time() - package_fetch.STALE_SECONDS - 60
        os.utime(old, (long_ago, long_ago))
        os.utime(other, (long_ago, long_ago))
        in_use = self.cache / ('c' * 64 + '.pkg')
        in_use.write_bytes(b'x')
        os.utime(in_use, (long_ago, long_ago))
        self.assertEqual(package_fetch.sweep(self.cache, keep={'c' * 64}), 1)
        self.assertEqual(self.cached(), [fresh.name, in_use.name, other.name])  # only this module's own old files go, not those in use

    def test_a_copy_that_is_used_again_is_not_swept_as_old(self):
        first = self.fetch()
        long_ago = time.time() - package_fetch.STALE_SECONDS - 60
        os.utime(first['path'], (long_ago, long_ago))
        self.assertTrue(self.fetch()['cached'])
        self.assertEqual(package_fetch.sweep(self.cache), 0)
        self.assertEqual(self.cached(), [sha(PACKAGE) + '.pkg'])

    def test_the_cache_is_not_worked_on_through_a_link(self):
        self.fetch()
        link = Path(self.work.name) / 'link'
        link.symlink_to(self.cache, target_is_directory=True)
        self.assertIsNone(package_fetch.cached(link, sha(PACKAGE)))
        self.assertFalse(package_fetch.forget(link, sha(PACKAGE)))
        self.assertEqual(package_fetch.sweep(link, older_than=-60), 0)
        self.assertEqual(self.cached(), [sha(PACKAGE) + '.pkg'])

    def test_the_identity_of_the_verified_copy_comes_with_the_result(self):
        result = self.fetch()
        self.assertEqual(result['identity'], package_fetch.identity(result['path']))
        self.assertEqual(self.fetch()['identity'], package_fetch.identity(result['path']))

    def test_forgetting_a_package_removes_both_of_its_files(self):
        self.interrupt(100_000)
        self.assertTrue(package_fetch.forget(self.cache, sha(PACKAGE)))
        self.assertEqual(self.cached(), [])
        self.assertFalse(package_fetch.forget(self.cache, sha(PACKAGE)))
        self.assertFalse(package_fetch.forget(self.cache, '../outside'))


if __name__ == '__main__':
    unittest.main()
