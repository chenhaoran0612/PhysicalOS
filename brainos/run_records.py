"""What a run did, kept on this computer (issue #70, docs/RUN_RECORDS.md).

A robot is asked to run in three ways, and each request is an ``operations`` record: a skill run directly
(``skill_run``), an on-site test of an application (``onsite``), and the work of a released application
(``start``). Until this issue the record stopped at "the device accepted it": how the run ended, how long
it took, which step failed and where the robot went stayed on the device.

This module follows a run to its end and keeps it per operation, in full as ``run_record_data`` and, for
the lists, as a small ``run_records`` record without the samples and events:

* how it ended: the device's status, stop reason and error, when it started and ended, how long it took;
* the steps of an application and their outcome, and the result the skill returned;
* the device's own events of the run (started, step started/finished, progress, log, finished);
* **samples** taken about once a second while it ran: position, heading, speed, battery, progress —
  the only time series the product keeps. A long run is thinned (every other sample dropped, the interval
  doubled), so the whole run stays covered within ``MAX_SAMPLES``.

Following starts when a run is accepted (the platform's audit entry of the request) and whenever one of
the three pages asks for the list while a run is under way. It only reads from the device
(``/v2/state``, ``/v2/runs/<id>``): nothing here moves a robot or changes one. A run from before this
issue, or one that ended while the app was closed, is read back once when its record is opened; it then has
the result and the events, and no samples.

Records are not part of ``/api/state`` (they change every second while a run is followed, and the console
redraws the page whenever that state changes): the pages ask ``runs/records`` and ``runs/record``.

Following is off unless the server's entry point turns it on (``FOLLOW``): a test that counts the requests
its stand-in device received is not disturbed. ``NEUXMIND_RUN_FOLLOW=0`` turns it off for the app too.
"""
import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

from . import connector
from .contracts import required_text

KIND = 'run_records'               # one small record per run: what the lists show (``summary``)
DATA = 'run_record_data'           # the same run in full, with its samples and events
FOLLOW = False                      # the default of a new Platform; brainos.server main() sets it
FOLLOWED = ('skill_run', 'onsite', 'start')
ACTIONS = ('skill.run', 'onsite.requested', 'robot.start')     # audit entries that mean "a run may have started"
UNDER_WAY = ('accepted', 'running')
SAMPLE_S = 1.0
MAX_SAMPLES = 600
MAX_EVENTS = 300
SAVE_EVERY = 5                      # samples between two saves of a record that is being followed
LOST_AFTER = 30                     # reads in a row that failed: the device cannot be followed any more
FOLLOW_MAX_S = 2 * 60 * 60          # a run is followed this long at most
FOLLOW_WINDOW_S = 6 * 60 * 60       # an operation older than this is not picked up for following
ENDED = ('succeeded', 'failed', 'stopped', 'cancelled', 'timeout', 'unknown')
TEXT_MAX = 300
RESULT_MAX = 20000                  # characters of a skill's result that are kept
LIST_MAX = 500                      # records the lists are given, newest first
LOST_NOTE = '连续读不到这台机器人的状态，后面的数据没有收集到'
TOO_LONG_NOTE = '这次运行超过了两小时，之后的数据没有继续收集'
CLOSED_NOTE = '应用在这次运行结束之前退出了，之后的数据没有收集到'
READ_BACK_NOTE = '这次运行结束之后才读回来：有结果和事件，没有运行过程中的采样'
GAP_NOTE = '运行过程中有一段读不到这台机器人，那一段没有采样；结果是之后读回来的'
NO_ENDPOINT_NOTE = '这台机器人没有设备端地址，读不到这次运行的数据'
NOT_KEPT = '这次运行的数据没有收集到：它发生在开始收集运行数据之前，或者当时这台电脑没有连着机器人'


def enabled_by_environment():
    return os.environ.get('NEUXMIND_RUN_FOLLOW', '1') != '0'


def now():
    return datetime.now(timezone.utc).isoformat()


def _seconds(value):
    """A device or desktop timestamp (ISO text or seconds) as seconds since the epoch, or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _number(value, digits=3):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _text(value, limit=TEXT_MAX):
    return None if value in (None, '') else str(value)[:limit]


def clean(value):
    """A device's value as something the store keeps: numbers that are not finite become None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(item) for item in value]
    return value if isinstance(value, (str, int, bool)) or value is None else str(value)


def kept_result(result):
    """A skill's result as it is kept: cleaned, and cut when it is longer than a page should show."""
    result = clean(result)
    text = json.dumps(result, ensure_ascii=False)
    return result if len(text) <= RESULT_MAX else {'truncated': True, 'kept_characters': RESULT_MAX, 'text': text[:RESULT_MAX]}


def run_id_of(operation, report=None):
    """The device's run of an operation, or None while it is not known."""
    for source in (operation, operation.get('response'), (report or {}).get('details')):
        if isinstance(source, dict) and isinstance(source.get('run_id'), str) and source['run_id']:
            return source['run_id']
    return None


def sample(state, t, progress=None):
    """One sample from the device's ``/v2/state``; None when it has nothing a chart could show."""
    out = {'t': round(t, 1)}
    odom = state.get('base.odometry') or state.get('localization.pose')
    if isinstance(odom, dict) and isinstance(odom.get('pose'), list) and len(odom['pose']) == 7:
        pose = odom['pose']
        out['x'], out['y'] = _number(pose[0]), _number(pose[1])
        try:
            x, y, z, w = (float(v) for v in pose[3:7])
            out['yaw'] = _number(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
        except (TypeError, ValueError):
            pass
        velocity = odom.get('velocity')
        if isinstance(velocity, list) and len(velocity) >= 2:
            vx, vy = _number(velocity[0]), _number(velocity[1])
            if vx is not None and vy is not None:
                out['speed'] = _number(math.hypot(vx, vy))
    battery = state.get('sensor.battery')
    if isinstance(battery, dict):
        out['battery'] = _number(battery.get('percent'), 1)
    if progress is not None:
        out['progress'] = _number(progress)
    out = {key: value for key, value in out.items() if value is not None}
    return out if len(out) > 1 else None


def thin(samples):
    """Every other sample, the first and the last kept."""
    if len(samples) < 3:
        return list(samples)
    kept = samples[:-1:2]
    return kept + [samples[-1]]


def distance(samples):
    """Metres along the sampled positions; None without two positions."""
    points = [(s['x'], s['y']) for s in samples if 'x' in s and 'y' in s]
    if len(points) < 2:
        return None
    return round(sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:])), 2)


def events_of(run):
    """The device's events of a run, cut to what a page shows."""
    out = []
    for event in run.get('events') if isinstance(run.get('events'), list) else []:
        if not isinstance(event, dict):
            continue
        entry = {'type': _text(event.get('type'), 60), 'ts': event.get('ts') if isinstance(event.get('ts'), (int, float, str)) else None,
                 'message': _text(event.get('message')), 'status': _text(event.get('status'), 40),
                 'index': event.get('index') if isinstance(event.get('index'), int) else None, 'error': _text(event.get('error'))}
        out.append({key: value for key, value in entry.items() if value is not None})
    return out[-MAX_EVENTS:]


def steps_of(run, report=None):
    """The steps of an application's run, or the cases of an on-site report: [{name, status, detail}]."""
    result = run.get('result') if isinstance(run, dict) else None
    steps = result.get('steps') if isinstance(result, dict) else None
    if isinstance(steps, list) and steps:
        return [{'name': _text(step.get('skill') or step.get('entrypoint') or f"步骤 {index + 1}", 120),
                 'status': _text(step.get('status'), 40), 'detail': _text(step.get('error'))}
                for index, step in enumerate(steps) if isinstance(step, dict)]
    cases = ((report or {}).get('details') or {}).get('cases') or (report or {}).get('cases')
    if isinstance(cases, list):
        return [{'name': _text(case.get('name'), 120), 'status': 'succeeded' if case.get('passed') is True else 'failed',
                 'detail': _text(case.get('detail'))} for case in cases if isinstance(case, dict)]
    return []


def ended(run):
    return isinstance(run, dict) and (run.get('ended_at') not in (None, '') or run.get('status') in ENDED)


def apply_run(record, run, report=None):
    """Put what the device says about the run into the record (in place: the record is this module's own)."""
    start, end = _seconds(run.get('started_at') or run.get('created_at')), _seconds(run.get('ended_at'))
    record.update(run_status=_text(run.get('status'), 40), target=_text(run.get('target'), 160), run_kind=_text(run.get('kind'), 40),
                  progress=_number(run.get('progress')), stop_reason=_text(run.get('stop_reason')), error=_text(run.get('error')),
                  started_at=run.get('started_at') or run.get('created_at'), ended_at=run.get('ended_at'),
                  duration_s=round(end - start, 1) if start is not None and end is not None and end >= start else None,
                  events=events_of(run), steps=steps_of(run, report))
    result = run.get('result')
    if isinstance(result, (dict, list, str, int, float)) and not isinstance(result, bool):
        record['result'] = kept_result(result)
    return record


def summary(record):
    """A record without its samples and events, for the lists."""
    steps = record.get('steps') or []
    return {**{key: record.get(key) for key in ('id', 'operation_id', 'robot_id', 'run_id', 'kind', 'state', 'run_status', 'target',
                                                'progress', 'stop_reason', 'error', 'started_at', 'ended_at', 'duration_s',
                                                'distance_m', 'note', 'simulated', 'followed_at')},
            'samples': len(record.get('samples') or []), 'events': len(record.get('events') or []),
            'steps': len(steps), 'steps_passed': sum(1 for step in steps if step.get('status') == 'succeeded')}


class RunRecords:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store
        self.follow = FOLLOW
        self.lock = threading.Lock()
        self.threads = {}                   # operation id -> thread
        self.stopping = threading.Event()
        audit = platform.audit

        def audited(action, details, project_id=None):
            audit(action, details, project_id)
            if action in ACTIONS:
                try:
                    self.ensure()
                except Exception:           # the request already reached the robot: following is not allowed to fail it
                    pass
        platform.audit = audited            # a request that may have started a run is followed from its first second

    # ------------------------------------------------------------ requests of the pages
    def overview(self, p):
        """Every record without its samples, and which operations are being followed right now."""
        self.ensure()
        with self.lock:
            following = sorted(key for key, thread in self.threads.items() if thread.is_alive())
        return {'records': self.store.list(KIND)[:LIST_MAX], 'following': following, 'collecting': self.follow}

    def detail(self, p):
        """One operation's record in full. A run nobody followed is read back from the device once."""
        operation_id = required_text(p.get('operation_id'), '运行记录', 200)
        operation = self._operation(operation_id)
        if operation is None:
            raise ValueError('找不到这条运行记录，它可能已经被删除；请刷新页面')
        record = self._record(operation_id)
        if self.follow and not self._following(operation_id):
            if record is None:
                record = self._read_back(operation)
            elif record.get('state') == 'lost':
                record = self._read_back(operation, record) or record
        return {'operation': operation, 'record': record, 'following': self._following(operation_id),
                'missing': None if record else NOT_KEPT}

    # ------------------------------------------------------------ following
    def ensure(self):
        """Start following every run that is under way and has no follower."""
        if not self.follow or self.stopping.is_set():
            return
        limit = time.time() - FOLLOW_WINDOW_S
        with self.lock:
            self.threads = {key: thread for key, thread in self.threads.items() if thread.is_alive()}
        for operation in self.store.list('operations'):
            if operation.get('kind') not in FOLLOWED or operation.get('status') not in UNDER_WAY:
                continue
            created = _seconds(operation.get('created_at'))
            if created is None or created < limit:
                continue
            # The store is read before the lock is taken (a request may hold the store's lock while it audits).
            # A follower that ends meanwhile is still in ``threads`` (they are only dropped above), so it is not restarted.
            if (self._record(operation['id']) or {}).get('state', 'following') != 'following':
                continue
            with self.lock:                 # the check, the entry and the start are one step: never two followers of a run
                if operation['id'] in self.threads or self.stopping.is_set():
                    continue
                thread = threading.Thread(target=self._run, args=(operation['id'],), name='run-follow', daemon=True)
                self.threads[operation['id']] = thread
                thread.start()

    def close(self):
        self.stopping.set()
        with self.lock:
            threads = list(self.threads.values())
        for thread in threads:
            thread.join(timeout=5)

    def _following(self, operation_id):
        with self.lock:
            thread = self.threads.get(operation_id)
        return bool(thread and thread.is_alive())

    def _operation(self, operation_id):
        return next((o for o in self.store.list('operations') if o['id'] == operation_id), None)

    def _record(self, operation_id):
        try:
            return self.store.get(DATA, 'run-' + operation_id)
        except ValueError:
            return None

    def _robot(self, robot_id):
        return next((r for r in self.store.list('robots') if r['id'] == robot_id), None)

    def _report(self, operation):
        key = operation.get('report_id')
        return next((e for e in self.store.list('evaluations') if e['id'] == key), None) if key else None

    def _new(self, operation, robot):
        return {'id': 'run-' + operation['id'], 'project_id': operation.get('project_id'), 'operation_id': operation['id'],
                'robot_id': operation.get('robot_id'), 'kind': operation.get('kind'), 'run_id': run_id_of(operation),
                'simulated': bool((robot or {}).get('sim')) or operation.get('simulated') is True,
                'state': 'following', 'samples': [], 'events': [], 'steps': [], 'sample_interval_s': SAMPLE_S, 'followed_at': now()}

    def _get(self, robot, path):
        return connector.call(robot['endpoint'], path, robot.get('token', ''), timeout=3)

    def _run(self, operation_id):
        try:
            self._follow(operation_id)
        except Exception as exc:        # a follower must not take the app down; what happened is kept with the record
            try:
                record = self._record(operation_id)
                if record and record.get('state') == 'following':
                    record.pop('result', None)
                    record.update(state='lost', note=f'收集这次运行的数据时出错：{str(exc)[:200]}', finished_at=now())
                    self._save(record)
            except Exception:           # the store is closed, or the record cannot be kept at all
                pass

    def _save(self, record):
        if self.stopping.is_set() and record.get('state') == 'following':
            record = {**record, 'state': 'lost', 'note': CLOSED_NOTE, 'finished_at': now()}
        if 'distance_m' not in record:
            record['distance_m'] = distance(record.get('samples') or [])
        try:
            self.store.put(DATA, record)
            self.store.put(KIND, {**summary(record), 'project_id': record.get('project_id')})
        except Exception:               # the store is closed: the app is quitting
            if not self.stopping.is_set():
                raise

    def _follow(self, operation_id):
        operation = self._operation(operation_id)
        robot = self._robot(operation.get('robot_id')) if operation else None
        if not operation or not robot:
            return
        record = self._record(operation_id)
        if not robot.get('endpoint'):       # kept, so that it is not tried again on every request
            self._save({**(record or self._new(operation, robot)), 'state': 'lost', 'note': NO_ENDPOINT_NOTE, 'finished_at': now()})
            return
        if record is None and self._read_back(operation):
            return                          # it had ended before anybody followed it: its result, and no sample from now
        record = record or self._new(operation, robot)
        record['state'] = 'following'
        began = _seconds(operation.get('created_at')) or time.time()
        interval, failures, unsaved, run = float(record.get('sample_interval_s') or SAMPLE_S), 0, 0, None
        placed = [s for s in record['samples'] if 'x' in s and 'y' in s]
        metres, at = float(record.get('distance_m') or 0), ((placed[-1]['x'], placed[-1]['y']) if placed else None)
        self._save(record)
        while not self.stopping.wait(interval):
            operation = self._operation(operation_id)
            robot = self._robot(record['robot_id'])
            if not operation:               # deleted with its task: its record goes too
                self.store.delete(DATA, record['id'])
                self.store.delete(KIND, record['id'])
                return
            if not robot or not robot.get('endpoint'):
                record.update(state='lost', note='这台机器人已经被删除', finished_at=now())
                break
            report = self._report(operation)
            record['run_id'] = record.get('run_id') or run_id_of(operation, report)
            try:
                state = self._get(robot, '/v2/state')
                if record['run_id']:
                    run = self._get(robot, '/v2/runs/' + quote(record['run_id'], safe=''))
                failures = 0
            except connector.DeviceError:
                failures += 1
                if failures >= LOST_AFTER:
                    record.update(state='lost', note=LOST_NOTE, finished_at=now())
                    break
                continue
            progress = (run or {}).get('progress') if run else operation.get('progress')
            point = sample(state if isinstance(state, dict) else {}, time.time() - began, progress)
            if point:
                if 'x' in point and 'y' in point:       # the distance is summed as it goes: thinning the samples does not shorten it
                    if at:
                        metres += math.hypot(point['x'] - at[0], point['y'] - at[1])
                    at = (point['x'], point['y'])
                    record['distance_m'] = round(metres, 2)
                record['samples'].append(point)
                if len(record['samples']) > MAX_SAMPLES:
                    record['samples'] = thin(record['samples'])
                    interval *= 2
                    record['sample_interval_s'] = interval
            if run:
                apply_run(record, run, report)
            over = ended(run) if record['run_id'] else operation.get('status') not in UNDER_WAY
            if over:
                record.update(state='final', finished_at=now())
                break
            if time.time() - began > FOLLOW_MAX_S:
                record.update(state='lost', note=TOO_LONG_NOTE, finished_at=now())
                break
            unsaved += 1
            if unsaved >= SAVE_EVERY:
                unsaved = 0
                self._save(record)
        if record.get('state') == 'final' and not record.get('steps'):
            record['steps'] = steps_of(run or {}, self._report(self._operation(operation_id) or operation))
        self._save(record)

    def _read_back(self, operation, lost=None):
        """The result and the events of a run nobody followed to its end, asked from the device once.

        ``lost`` is the record of a run whose following was cut off: it keeps its samples and gets the result."""
        robot = self._robot(operation.get('robot_id'))
        report = self._report(operation)
        run_id = run_id_of(operation, report)
        if not robot or not robot.get('endpoint') or not run_id or operation.get('kind') not in FOLLOWED:
            return None
        try:
            run = self._get(robot, '/v2/runs/' + quote(run_id, safe=''))
        except connector.DeviceError:
            return None
        if not isinstance(run, dict) or not ended(run):
            return None
        record = apply_run({**(lost or self._new(operation, robot)), 'run_id': run_id, 'state': 'final', 'read_back': True,
                            'note': GAP_NOTE if lost and lost.get('samples') else READ_BACK_NOTE, 'finished_at': now()}, run, report)
        self._save(record)
        return record
