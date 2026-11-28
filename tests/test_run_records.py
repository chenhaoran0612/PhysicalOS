"""Issue #70 — what a run did is kept: a run is followed to its end, with its result, events and samples.

The pages are checked in ``tests/run_boards_logic.cjs`` (pure functions) and ``tests/run_boards_browser.cjs``.
"""
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from brainos import connector, run_records
from brainos.server import Application, make_handler
from brainos.service import Platform, now
from test_product import Server

ROOT = Path(__file__).resolve().parents[1]
LINE = ('<link rel="stylesheet" href="/run_boards.css"><script src="/run_record_view.js" defer></script>'
        '<script src="/run_boards.js" defer></script>')
QUAT = [0, 0, 0, 1]


def odometry(x, y, speed=0.4):
    return {'base.odometry': {'pose': [x, y, 0, *QUAT], 'velocity': [speed, 0, 0]}, 'sensor.battery': {'percent': 90 - x}}


class Device:
    """What a robot answers: its state moves one metre a read, its run ends after ``ends_after`` reads."""

    def __init__(self, ends_after=3, fail=False, run=None):
        self.reads, self.ends_after, self.fail, self.paths, self.lock = 0, ends_after, fail, [], threading.Lock()
        self.run = run or {}

    def __call__(self, robot, path):
        with self.lock:
            self.paths.append(path)
            if self.fail:
                raise connector.DeviceError('连不上', sent=False)
            if path == '/v2/state':
                self.reads += 1
                return odometry(float(self.reads), 0.0)
            over = self.reads >= self.ends_after
            return {'id': path.rsplit('/', 1)[-1], 'kind': 'skill', 'target': 'patrol_places', 'status': 'succeeded' if over else 'running',
                    'progress': 1.0 if over else 0.5, 'created_at': '2026-11-27T10:00:00+00:00', 'started_at': '2026-11-27T10:00:01+00:00',
                    'ended_at': '2026-11-27T10:00:13.500000+00:00' if over else None, 'stop_reason': None, 'error': None,
                    'result': {'visited': ['会议室', '二楼']},
                    'events': [{'type': 'started', 'ts': 1795773601.0}, {'type': 'log', 'ts': 1795773605.0, 'message': '到达 会议室'},
                               'not an event', {'type': 'finished', 'ts': 1795773613.5, 'status': 'succeeded'}], **self.run}


class PureTests(unittest.TestCase):
    def test_a_sample_holds_position_heading_speed_battery_and_progress(self):
        point = run_records.sample(odometry(1.23456, 2.0, speed=0.3), 4.04, 0.25)
        self.assertEqual(point, {'t': 4.0, 'x': 1.235, 'y': 2.0, 'yaw': 0.0, 'speed': 0.3, 'battery': 88.8, 'progress': 0.25})

    def test_a_state_without_anything_to_draw_gives_no_sample(self):
        self.assertIsNone(run_records.sample({'system.health': {'ok': True}}, 1.0))
        self.assertIsNone(run_records.sample({'base.odometry': {'pose': [1, 2]}, 'sensor.battery': {'percent': True}}, 1.0))

    def test_thinning_keeps_the_first_and_the_last_sample(self):
        samples = [{'t': i} for i in range(7)]
        self.assertEqual([s['t'] for s in run_records.thin(samples)], [0, 2, 4, 6])
        self.assertEqual([s['t'] for s in run_records.thin([{'t': i} for i in range(6)])], [0, 2, 4, 5])

    def test_distance_is_the_length_of_the_sampled_route(self):
        self.assertEqual(run_records.distance([{'x': 0, 'y': 0}, {'t': 1}, {'x': 3, 'y': 4}, {'x': 3, 'y': 5}]), 6.0)
        self.assertIsNone(run_records.distance([{'x': 0, 'y': 0}]))

    def test_the_steps_of_an_application_and_the_cases_of_a_report(self):
        run = {'result': {'steps': [{'index': 0, 'skill': 'go', 'status': 'succeeded'}, {'index': 1, 'entrypoint': 'look', 'status': 'failed', 'error': '没看到'}]}}
        self.assertEqual(run_records.steps_of(run), [{'name': 'go', 'status': 'succeeded', 'detail': None},
                                                     {'name': 'look', 'status': 'failed', 'detail': '没看到'}])
        report = {'details': {'cases': [{'name': '步骤 1 go', 'passed': True, 'detail': '完成'}, {'name': '步骤 2', 'passed': False}]}}
        self.assertEqual([s['status'] for s in run_records.steps_of({}, report)], ['succeeded', 'failed'])

    def test_the_run_of_an_operation_is_found_where_each_kind_keeps_it(self):
        self.assertEqual(run_records.run_id_of({'run_id': 'a'}), 'a')
        self.assertEqual(run_records.run_id_of({'response': {'accepted': True, 'run_id': 'b'}}), 'b')
        self.assertEqual(run_records.run_id_of({'kind': 'onsite'}, {'details': {'run_id': 'c'}}), 'c')
        self.assertIsNone(run_records.run_id_of({'response': {'run_id': 7}}))


class FollowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.platform = Platform(self.tmp.name)
        self.addCleanup(self.platform.close)
        self.records = self.platform.run_records
        self.platform.store.put('robots', {'id': 'robot-1', 'project_id': 'project-robot-home', 'name': '巡检狗', 'endpoint': 'http://127.0.0.1:9',
                                           'token': '', 'status': 'connected', 'sim': {'kind': 'quadruped'}})
        for name, value in (('SAMPLE_S', 0.01), ('LOST_AFTER', 3)):
            patcher = mock.patch.object(run_records, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def operation(self, key='skill-run-1', **fields):
        operation = {'id': key, 'project_id': 'project-robot-home', 'robot_id': 'robot-1', 'kind': 'skill_run', 'skill_id': 'patrol_places',
                     'status': 'accepted', 'run_id': 'run-1', 'created_at': now(), **fields}
        self.platform.store.put('operations', operation)
        return operation

    def follow(self, device):
        self.records.follow = True
        self.records._get = device
        self.records.ensure()
        for thread in list(self.records.threads.values()):
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())
        return self.records._record

    def test_a_platform_does_not_follow_unless_it_was_turned_on(self):
        self.operation()
        self.records._get = Device()
        self.records.ensure()
        self.platform.audit('skill.run', {'operation_id': 'skill-run-1'})
        self.assertEqual(self.records.threads, {})
        self.assertEqual(self.records.overview({}), {'records': [], 'following': [], 'collecting': False})
        self.assertEqual(self.records._get.paths, [])

    def test_a_run_is_followed_to_its_end_and_kept(self):
        self.operation()
        device = Device(ends_after=3)
        record = self.follow(device)('skill-run-1')
        self.assertEqual((record['state'], record['run_status'], record['duration_s'], record['simulated']), ('final', 'succeeded', 12.5, True))
        self.assertEqual([s['x'] for s in record['samples']], [1.0, 2.0, 3.0])
        self.assertEqual(record['distance_m'], 2.0)
        self.assertEqual([e['type'] for e in record['events']], ['started', 'log', 'finished'])
        self.assertEqual(record['result'], {'visited': ['会议室', '二楼']})
        self.assertEqual(set(device.paths), {'/v2/state', '/v2/runs/run-1'})         # reads only
        summary = self.records.overview({})['records'][0]
        self.assertEqual((summary['samples'], summary['events'], summary['operation_id']), (3, 3, 'skill-run-1'))
        self.assertNotIn('result', summary)
        # It is not followed a second time, and it is not part of the state the console redraws on.
        self.records.ensure()
        self.assertFalse(self.records._following('skill-run-1'))
        self.assertNotIn(run_records.KIND, self.platform.snapshot())
        self.assertNotIn(run_records.DATA, self.platform.snapshot())
        self.assertEqual(self.platform.store.list(run_records.KIND)[0]['samples'], 3)       # the list keeps the count, not the samples

    def test_the_request_that_starts_a_run_starts_the_following(self):
        self.records.follow = True
        self.records._get = Device(ends_after=1)
        self.operation()
        self.platform.audit('skill.run', {'operation_id': 'skill-run-1'}, 'project-robot-home')
        self.records.threads['skill-run-1'].join(timeout=20)
        self.assertEqual(self.records._record('skill-run-1')['state'], 'final')
        self.assertEqual([e['action'] for e in self.platform.store.list('events') if e['action'] == 'skill.run'], ['skill.run'])

    def test_the_steps_of_a_started_application_are_kept(self):
        self.operation('operation-1', kind='start', run_id=None, release_id='release-1', response={'accepted': True, 'run_id': 'run-9'})
        steps = {'steps': [{'index': 0, 'skill': 'go_to', 'status': 'succeeded'}, {'index': 1, 'skill': 'look', 'status': 'failed', 'error': '超时'}]}
        record = self.follow(Device(ends_after=2, run={'kind': 'application', 'result': steps}))('operation-1')
        self.assertEqual([(s['name'], s['status']) for s in record['steps']], [('go_to', 'succeeded'), ('look', 'failed')])
        self.assertEqual(self.records.overview({})['records'][0]['steps_passed'], 1)

    def test_an_on_site_test_is_sampled_until_its_operation_ends(self):
        self.operation('onsite-1', kind='onsite', run_id=None, status='running', application_id='app-1')
        device = Device()
        store = self.platform.store

        def answer(robot, path):
            out = device(robot, path)
            if device.reads == 2:          # the page's poll found the test finished
                store.put('evaluations', {'id': 'onsite-report-1', 'project_id': 'project-robot-home', 'application_id': 'app-1', 'robot_id': 'robot-1',
                                          'scope': 'simulation', 'passed': True, 'created_at': now(),
                                          'details': {'cases': [{'name': '步骤 1 patrol', 'passed': True, 'detail': '完成'}]}})
                store.put('operations', {**next(o for o in store.list('operations') if o['id'] == 'onsite-1'), 'status': 'succeeded',
                                         'report_id': 'onsite-report-1'})
            return out
        record = self.follow(answer)('onsite-1')
        self.assertEqual(record['state'], 'final')
        self.assertGreaterEqual(len(record['samples']), 2)
        self.assertEqual(record['steps'], [{'name': '步骤 1 patrol', 'status': 'succeeded', 'detail': '完成'}])

    def test_a_device_that_stops_answering_ends_the_following_and_says_so(self):
        self.operation()
        record = self.follow(Device(fail=True))('skill-run-1')
        self.assertEqual((record['state'], record['note'], record['samples']), ('lost', run_records.LOST_NOTE, []))

    def test_an_old_or_finished_operation_is_not_followed(self):
        self.operation('skill-run-old', created_at='2026-01-01T00:00:00+00:00')
        self.operation('skill-run-rejected', status='rejected')
        self.operation('stop-1', kind='stop')
        self.follow(Device())
        self.assertEqual(self.records.threads, {})

    def test_a_run_nobody_followed_is_read_back_once_when_it_is_opened(self):
        self.operation('skill-run-old', created_at='2026-01-01T00:00:00+00:00')
        self.records.follow = True
        self.records._get = Device(ends_after=0)
        answer = self.records.detail({'operation_id': 'skill-run-old'})
        self.assertEqual((answer['record']['state'], answer['record']['read_back'], answer['record']['samples']), ('final', True, []))
        self.assertEqual(answer['record']['duration_s'], 12.5)
        self.assertIsNone(answer['missing'])
        self.assertEqual(self.records._get.paths, ['/v2/runs/run-1'])
        self.records.detail({'operation_id': 'skill-run-old'})
        self.assertEqual(self.records._get.paths, ['/v2/runs/run-1'])                 # kept: not asked again

    # Issue #75: a robot's 「2 次，0 次完成」 counted two runs that had both succeeded: the older one had ended unseen
    # and stayed 「已受理」 in every list until its own page was opened.
    def settle(self, device):
        self.records.follow = True
        self.records._get = device
        self.records.overview({})
        thread = self.records.settling
        if thread is not None:
            thread.join(timeout=20)
            self.assertFalse(thread.is_alive())

    def test_the_lists_read_back_the_end_of_a_run_nobody_followed(self):
        self.operation('skill-run-old', created_at='2026-01-01T00:00:00+00:00')
        device = Device(ends_after=0)
        self.settle(device)
        summary = self.platform.store.list(run_records.KIND)[0]
        self.assertEqual((summary['operation_id'], summary['state'], summary['run_status'], summary['note']),
                         ('skill-run-old', 'final', 'succeeded', run_records.READ_BACK_NOTE))
        self.assertEqual(device.paths, ['/v2/runs/run-1'])
        self.settle(device)                                                       # once: a run with its record is not asked about again
        self.assertEqual(device.paths, ['/v2/runs/run-1'])

    def test_a_robot_that_cannot_answer_is_asked_again_only_after_a_while(self):
        self.operation('skill-run-old', created_at='2026-01-01T00:00:00+00:00')
        device = Device(fail=True)
        self.settle(device)
        self.settle(device)
        self.assertEqual(device.paths, ['/v2/runs/run-1'])
        self.assertEqual(self.platform.store.list(run_records.KIND), [])
        with mock.patch.object(run_records, 'SETTLE_RETRY_S', 0):
            self.settle(Device(ends_after=0))
        self.assertEqual(self.platform.store.list(run_records.KIND)[0]['run_status'], 'succeeded')

    def test_a_run_whose_following_was_cut_off_is_read_back_by_the_lists_too(self):
        self.operation()
        self.follow(Device(fail=True))
        self.assertEqual(self.records._record('skill-run-1')['state'], 'lost')
        self.settle(Device(ends_after=0))
        record = self.records._record('skill-run-1')
        self.assertEqual((record['state'], record['run_status'], record['note']), ('final', 'succeeded', run_records.READ_BACK_NOTE))   # it had no samples before it was cut off

    def test_a_young_run_with_no_record_is_left_to_its_follower(self):
        self.operation()
        self.records.follow = True
        self.assertEqual(self.records._unsettled(), [])                           # ensure() follows it (and reads it back when it had ended)

    def test_a_run_without_data_says_why_and_an_unknown_one_is_refused(self):
        self.operation('skill-run-old', created_at='2026-01-01T00:00:00+00:00')
        self.records.follow = True
        self.records._get = Device(fail=True)
        answer = self.records.detail({'operation_id': 'skill-run-old'})
        self.assertEqual((answer['record'], answer['missing']), (None, run_records.NOT_KEPT))
        with self.assertRaises(ValueError):
            self.records.detail({'operation_id': 'nothing'})

    def test_a_run_that_had_ended_before_it_was_followed_gets_its_result_and_no_sample_from_now(self):
        self.operation()
        device = Device(ends_after=0)
        record = self.follow(device)('skill-run-1')
        self.assertEqual((record['state'], record['read_back'], record['samples'], record['note']), ('final', True, [], run_records.READ_BACK_NOTE))
        self.assertEqual(device.paths, ['/v2/runs/run-1'])                           # the robot's state now is not this run's

    def test_a_result_the_store_cannot_keep_is_cleaned_and_a_long_one_is_cut(self):
        self.operation()
        record = self.follow(Device(ends_after=2, run={'result': {'score': float('nan'), 'ok': True, 'far': [float('inf'), 1.5]}}))('skill-run-1')
        self.assertEqual((record['state'], record['result']), ('final', {'score': None, 'ok': True, 'far': [None, 1.5]}))
        self.operation('skill-run-2', run_id='run-2')
        record = self.follow(Device(ends_after=2, run={'result': {'log': 'x' * 50000}}))('skill-run-2')
        self.assertEqual((record['result']['truncated'], len(record['result']['text'])), (True, run_records.RESULT_MAX))

    def test_thinning_the_samples_does_not_shorten_the_distance(self):
        self.operation()
        with mock.patch.object(run_records, 'MAX_SAMPLES', 4):
            record = self.follow(Device(ends_after=9))('skill-run-1')
        self.assertLess(len(record['samples']), 9)
        self.assertEqual(record['distance_m'], 8.0)
        self.assertGreater(record['sample_interval_s'], 0.01)

    def test_a_run_whose_following_was_cut_off_gets_its_result_when_it_is_opened(self):
        self.operation()
        self.follow(Device(fail=True))
        self.records._get = Device(ends_after=0)
        record = self.records.detail({'operation_id': 'skill-run-1'})['record']
        self.assertEqual((record['state'], record['run_status'], record['duration_s']), ('final', 'succeeded', 12.5))
        self.assertEqual(self.platform.store.list(run_records.KIND)[0]['state'], 'final')

    def test_a_robot_without_an_address_is_not_tried_again_and_again(self):
        self.platform.store.put('robots', {**self.platform.store.get('robots', 'robot-1'), 'endpoint': ''})
        self.operation()
        device = Device()
        record = self.follow(device)('skill-run-1')
        self.assertEqual((record['state'], record['note']), ('lost', run_records.NO_ENDPOINT_NOTE))
        self.records.ensure()
        self.assertEqual((self.records.threads, device.paths), ({}, []))

    def test_a_failure_while_looking_for_runs_does_not_fail_the_request_that_was_sent(self):
        self.records.follow = True
        with mock.patch.object(self.records, 'ensure', side_effect=RuntimeError('cannot start a thread')):
            self.platform.audit('skill.run', {'operation_id': 'skill-run-1'})
        self.assertEqual([e['action'] for e in self.platform.store.list('events') if e['action'] == 'skill.run'], ['skill.run'])

    def test_closing_the_app_leaves_a_followed_run_marked(self):
        self.operation()
        self.records.follow = True
        self.records._get = Device(ends_after=10 ** 9)
        self.records.ensure()
        thread = self.records.threads['skill-run-1']
        deadline = time.monotonic() + 20
        while not (self.records._record('skill-run-1') or {}).get('samples'):
            self.assertLess(time.monotonic(), deadline, 'no sample within 20 seconds')
            thread.join(timeout=0.02)
        self.records.close()
        self.assertFalse(thread.is_alive())
        record = self.records._record('skill-run-1')
        self.assertEqual((record['state'], record['note']), ('lost', run_records.CLOSED_NOTE))


class ServedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.application = Application(self.tmp.name)
        self.addCleanup(self.application.close)
        self.server = Server(make_handler(self.application))
        self.addCleanup(self.server.close)

    def test_the_page_files_are_served_and_loaded_once(self):
        for path, mime in (('/run_boards.js', 'text/javascript'), ('/run_record_view.js', 'text/javascript'), ('/run_boards.css', 'text/css')):
            with urllib.request.urlopen(self.server.url + path, timeout=10) as response:
                self.assertTrue(response.headers.get('Content-Type', '').startswith(mime), path)
                self.assertEqual(response.read(), (ROOT / 'web' / path[1:]).read_bytes())
        with urllib.request.urlopen(self.server.url + '/', timeout=10) as response:
            index = response.read().decode('utf-8')
        self.assertEqual(index.count(LINE), 1)

    def test_only_the_entry_point_of_the_app_turns_the_following_on(self):
        self.assertFalse(run_records.FOLLOW)
        with mock.patch.dict('os.environ', {'NEUXMIND_RUN_FOLLOW': '0'}):
            self.assertFalse(run_records.enabled_by_environment())
        with mock.patch.dict('os.environ', {}, clear=True):
            self.assertTrue(run_records.enabled_by_environment())
        self.assertIn('run_records.FOLLOW=run_records.enabled_by_environment()', (ROOT / 'brainos' / 'server.py').read_text(encoding='utf-8'))


class PageTests(unittest.TestCase):
    def test_the_page_scripts_parse_and_their_pure_functions_hold(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node 不可用')
        result = subprocess.run([node, 'tests/run_boards_logic.cjs'], capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r'run boards logic: \d+ checks passed')


if __name__ == '__main__':
    unittest.main()
