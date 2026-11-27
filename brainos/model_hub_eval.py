"""Running an evaluation of a duck's skill model on a simulated duck (issue #71, docs/MODEL_HUB.md).

One evaluation is one thread: for each start pose of the set it asks the duck to perform the skill model
from that pose (``duck_kit.DuckKit.perform``), waits for the judge of the model package to say how the
run ended, and writes the run into the evaluation's record. The measures are computed when every pose
was run; an evaluation that was stopped, interrupted or lost its duck measures nothing.

A simulated duck runs one evaluation at a time. Somebody pressing 停止 on the duck, or starting another
show on it, ends the evaluation as interrupted: the runs so far are kept, not counted.
"""
import threading
import time

EVALS = 'hub_evaluations'
START_S = 60                # how long a duck may take to begin a run (the first one loads the package)
CASE_S = 600                # how long one run may take
ENDED = ('succeeded', 'failed')


class Runner:
    def __init__(self, hub):
        self.hub = hub
        self.lock = threading.Lock()
        self.threads = {}               # evaluation id → its thread
        self.robots = {}                # robot id → the evaluation running on it
        self.stopping = set()
        self.closed = False

    def going(self, evaluation_id):
        with self.lock:
            return evaluation_id in self.threads

    def busy(self, robot_id):
        with self.lock:
            return robot_id in self.robots

    def start(self, record, show):
        thread = threading.Thread(target=self._run, args=(record['id'], show), name='nx-hub-evaluation', daemon=True)
        with self.lock:
            self.threads[record['id']] = thread
            self.robots[record['robot_id']] = record['id']
        thread.start()

    def stop(self, evaluation_id):
        with self.lock:
            if evaluation_id in self.threads:
                self.stopping.add(evaluation_id)

    def close(self):
        with self.lock:
            self.closed = True
            threads = list(self.threads.values())
        for thread in threads:
            thread.join(timeout=5)

    def _asked_to_end(self, evaluation_id):
        with self.lock:
            return self.closed or evaluation_id in self.stopping

    # ------------------------------------------------------------ one run
    def _wait(self, evaluation_id, robot_id, run_id):
        """How one run ended: ``('case', {...})``, or ``(status, why)`` for an evaluation that cannot go on."""
        began, seen = time.monotonic(), False
        while True:
            if self._asked_to_end(evaluation_id):
                try:
                    self.hub.halt({'robot_id': robot_id})
                except ValueError:
                    pass                                    # the duck is gone already: nothing to stop
                return 'stopped', ''
            status = self.hub.status({'robot_id': robot_id})
            mine = status.get('run_id') == run_id
            seen = seen or mine
            if mine and status.get('state') in ENDED and isinstance(status.get('result'), dict):
                result = status['result']
                return 'case', {'success': bool(result.get('success')), 'reason': str(result.get('reason') or '')[:80],
                                'steps': result.get('steps') if isinstance(result.get('steps'), int) else None,
                                'duration_s': result.get('duration_s') if isinstance(result.get('duration_s'), (int, float)) else None}
            if mine and status.get('state') == 'stopped':
                return 'interrupted', '有人在这个仿真小鸭上按了停止'
            if mine and status.get('state') == 'error':
                return 'error', str(status.get('error') or '仿真小鸭报告了错误')[:300]
            if seen and not mine:
                return 'interrupted', '这个仿真小鸭在评测中途被用去做别的了'
            waited = time.monotonic() - began
            if (not seen and waited > START_S) or waited > CASE_S:
                return 'error', '仿真小鸭没有在时限内' + ('开始' if not seen else '做完') + '这一回合'
            time.sleep(self.hub.poll_s)

    @staticmethod
    def _measures(cases):
        """The measures of a finished evaluation, from its runs."""
        def mean(key):
            values = [c[key] for c in cases if isinstance(c.get(key), (int, float))]
            return round(sum(values) / len(values), 3) if values else None
        found = {'success_rate': round(100 * sum(1 for c in cases if c['success']) / len(cases), 2),
                 'mean_duration_s': mean('duration_s'), 'mean_steps': mean('steps')}
        return {key: value for key, value in found.items() if value is not None}

    def _run(self, evaluation_id, show):
        hub, store = self.hub, self.hub.store
        record = store.get(EVALS, evaluation_id)
        status, error = 'done', ''
        try:
            for seed in record['seeds']:
                if self._asked_to_end(evaluation_id):
                    status = 'stopped'
                    break
                run = hub.perform({'robot_id': record['robot_id'], 'show': show, 'seed': seed})
                kind, outcome = self._wait(evaluation_id, record['robot_id'], run['run_id'])
                if kind != 'case':
                    status, error = kind, outcome
                    break
                record['cases'].append({'seed': seed, **outcome})
                record['passed'] = sum(1 for c in record['cases'] if c['success'])
                store.put(EVALS, record)
        except ValueError as exc:                           # the duck refused (not running, gone): said in its own words
            status, error = 'error', str(exc)[:300]
        except Exception as exc:                            # whatever else: the evaluation ends, the app goes on
            status, error = 'error', f'评测没有跑完：{type(exc).__name__}'
        finally:
            with self.lock:
                closed = self.closed
            if not closed:
                record.update(status=status, error=error, finished_at=hub._now(),
                              metrics=self._measures(record['cases']) if status == 'done' and record['cases'] else {})
                try:
                    store.put(EVALS, record)
                    hub.finished(record)
                except Exception:
                    pass                                    # the workspace is closing: the record stays "running" and is shown as interrupted
            with self.lock:
                self.threads.pop(evaluation_id, None)
                self.stopping.discard(evaluation_id)
                if self.robots.get(record['robot_id']) == evaluation_id:
                    del self.robots[record['robot_id']]
