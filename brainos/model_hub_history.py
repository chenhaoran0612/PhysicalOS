"""The candidate of a round of improvement, chosen from the model's history instead of uploaded
(issue #76, docs/MODEL_HUB_HISTORY.md).

Two kinds of history are offered for a round that waits for its candidate (or has one that may be swapped):

* **the model's own versions**: earlier uploads, candidates an earlier round did not take, the package that
  came with the app. Choosing one copies nothing; when the version already has a result on the round's
  evaluation set, that result is the candidate's and the round is at its decision.
* for the Microduck's skill models, **the model files the training project kept**: every update its
  dashboard on this computer evaluated, with the dashboard's own result. The dashboard names each file
  with its SHA-256 (``checkpoint_url``); the file is fetched from the dashboard, checked against it and
  kept like an upload (``model_hub_files``). A file that is already a version of the model is that version.

Only a dashboard on this computer is asked (``duck_kit.dashboard_url``), never through a proxy, never
following a redirect. Its whole answer is large (150 MB of JSON on 2026-11-28), so it is read in gzip,
parsed keeping only the fields used here, one read at a time, and remembered for a minute.
"""
import hashlib
import json
import re
import threading
import time
import urllib.request
import zlib

from . import duck_kit, model_hub as hub_records
from .contracts import required_text
from .errors import CodedError

OPEN_FOR_CANDIDATE = ('training', 'candidate', 'review')
INDEX_FRESH_S = 60
FAILED_FRESH_S = 5
DASHBOARD_TIMEOUT_S = 30
MAX_ANSWER_BYTES = 512 * 1024 * 1024      # the dashboard's answer, unpacked
MAX_FILE_BYTES = 256 * 1024 * 1024        # one model file of the training project
MAX_FILES_PER_SHOW = 5000
READ_BLOCK = 1 << 20
# The only fields the dashboard's answer is kept with; everything else is dropped while it is parsed.
KEEP = frozenset(('tasks', 'archived_tasks', 'id', 'events', 'round_number', 'iteration', 'successes', 'episodes',
                  'total_steps', 'training_backend', 'checkpoint_url', 'report_mtime', 'seeds', 'seed'))
CHECKPOINT = re.compile(r'/files/round(\d{1,9})/([a-z0-9_]{1,40})/checkpoints/update_(\d{1,9})\.pt\?v=([0-9a-f]{64})')
KEY = re.compile(r'(\d{1,9})-(\d{1,9})')
NOTE = '从训练项目的历史里选的模型文件'
_READING = threading.Lock()               # one read of the dashboard at a time in this process (it is big)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                       # the dashboard is asked, nothing it points to


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())     # loopback: never through a proxy


def _whole(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _slim(pairs):
    return {key: value for key, value in pairs if key in KEEP}


def gunzip(data, limit):
    """Unpack a gzip answer; refused when it unpacks to more than ``limit`` bytes."""
    unpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = unpacker.decompress(data, limit + 1)
    if len(out) > limit or unpacker.unconsumed_tail:
        raise ValueError('the answer is too large')
    return out


def _entry(show, event, fixed):
    """One evaluated model file of a show, or ``None`` when the event does not name one exactly."""
    found = CHECKPOINT.fullmatch(event.get('checkpoint_url') or '') if isinstance(event.get('checkpoint_url'), str) else None
    passed, total = _whole(event.get('successes')), _whole(event.get('episodes'))
    if not found or found[2] != show or passed is None or not total or not 0 <= passed <= total:
        return None
    number, update = int(found[1]), int(found[3])
    if _whole(event.get('round_number')) != number or _whole(event.get('iteration')) != update:
        return None
    seeds = event.get('seeds') if isinstance(event.get('seeds'), list) else []
    at = event.get('report_mtime')
    return {'key': f'{number}-{update}', 'round': number, 'update': update, 'passed': passed, 'total': total,
            'steps': _whole(event.get('total_steps')),
            'device': {'cuda': 'GPU', 'cpu': 'CPU'}.get(event.get('training_backend'), ''),
            'at': at if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
            'fixed': [s.get('seed') if isinstance(s, dict) else s for s in seeds] == list(fixed), 'sha256': found[4]}


def entries(data):
    """``{show: [model file, newest first]}`` from the dashboard's answer (``/api/evolution``)."""
    if not isinstance(data, dict):
        return {}
    shows = {}
    for task in [*(data.get('tasks') or []), *(data.get('archived_tasks') or [])]:
        if not isinstance(task, dict) or not isinstance(task.get('id'), str) or not isinstance(task.get('events'), list):
            continue
        kept = shows.setdefault(task['id'], {})
        for event in task['events']:
            one = _entry(task['id'], event, hub_records.FIXED_SEEDS) if isinstance(event, dict) else None
            if one and (one['key'] not in kept or (one['at'] or 0) >= (kept[one['key']]['at'] or 0)):
                kept[one['key']] = one
    order = lambda e: (e['at'] or 0, e['round'], e['update'])
    return {show: sorted(kept.values(), key=order, reverse=True)[:MAX_FILES_PER_SHOW] for show, kept in shows.items()}


def read_index(url):
    """The evaluated model files of every show, read from the dashboard at ``url``."""
    request = urllib.request.Request(url + '/api/evolution', headers={'Accept-Encoding': 'gzip'})
    with _READING:
        with _opener().open(request, timeout=DASHBOARD_TIMEOUT_S) as response:
            raw = response.read(MAX_ANSWER_BYTES + 1)
            packed = response.headers.get('Content-Encoding', '').strip().lower() == 'gzip'
        if len(raw) > MAX_ANSWER_BYTES:
            raise ValueError('the answer is too large')
        text = gunzip(raw, MAX_ANSWER_BYTES) if packed else raw
        del raw
        return entries(json.loads(text, object_pairs_hook=_slim))


def fetch_file(url, show, entry):
    """The content of one model file of the training project, checked against the SHA-256 the dashboard gave."""
    path = f"/files/round{entry['round']}/{show}/checkpoints/update_{entry['update']:06d}.pt?v={entry['sha256']}"
    try:
        with _opener().open(url + path, timeout=DASHBOARD_TIMEOUT_S) as response:
            digest, data = hashlib.sha256(), bytearray()
            for block in iter(lambda: response.read(READ_BLOCK), b''):
                if len(data) + len(block) > MAX_FILE_BYTES:
                    raise CodedError(f'这个模型文件太大（超过 {MAX_FILE_BYTES // 1024 ** 2} MB），没有取', 'hub_history_unavailable')
                digest.update(block)
                data.extend(block)
    except CodedError:
        raise
    except Exception:
        raise CodedError(f'训练看板（{url}）没有给出这个模型文件，请确认它还开着', 'hub_history_unavailable') from None
    if digest.hexdigest() != entry['sha256']:
        raise CodedError('取到的模型文件和训练看板记下的校验值不一致，没有保存', 'hub_history_mismatch')
    return data


class History:
    def __init__(self, hub):
        self.hub = hub
        self.lock = threading.Lock()
        self._index = {'url': None, 'at': 0.0, 'shows': None, 'problem': ''}

    # ------------------------------------------------------------ what is offered
    def _open(self, round_id):
        """A round that takes a candidate now, and its model."""
        record = self.hub._round(round_id)
        if record['status'] not in OPEN_FOR_CANDIDATE:
            raise CodedError('这一轮现在不是选候选版本的时候：先完成基线评测并写下短板', 'hub_round_state')
        return record, self.hub._model(record['model_id'])

    def _start_origin(self, model, record):
        """The training round and update the round started from, when its start version names them."""
        start = next((v for v in self.hub._versions(model) if v['id'] == record['version_id']), None)
        origin = (start or {}).get('origin') or {}
        return (origin.get('round'), origin.get('update')) if 'round' in origin and 'update' in origin else None

    def choices(self, p):
        """The model's versions a round's candidate can be (route ``model-hub/rounds/history``)."""
        record, model = self._open(p.get('round_id'))
        done = [e for e in self.hub._evaluations(model) if e['status'] == 'done' and e['set_id'] == record['set_id']]
        candidates = {r['candidate_version_id']: r['number'] for r in sorted(self.hub._of(hub_records.ROUNDS, model['id']), key=lambda r: r['number'])
                      if r.get('candidate_version_id')}
        versions = []
        for version in self.hub._versions(model):
            if version['id'] == record['version_id']:
                continue
            measured = next((e for e in done if e['version_id'] == version['id']), None)
            view = self.hub._evaluation_view(measured) if measured else None
            versions.append({**{k: version.get(k) for k in ('id', 'label', 'origin', 'note', 'created_at')}, 'builtin': bool(version.get('builtin')),
                             'files': [{k: f.get(k) for k in ('filename', 'size', 'sha256')} for f in version.get('files', [])],
                             'candidate': version['id'] == record.get('candidate_version_id'), 'round_number': candidates.get(version['id']),
                             'evaluation': {k: view[k] for k in ('id', 'metrics', 'passed', 'total', 'judgement')} if view else None})
        return {'round_id': record['id'], 'model_id': model['id'], 'set_id': record['set_id'], 'start_version_id': record['version_id'],
                'candidate_version_id': record.get('candidate_version_id', ''), 'versions': versions,
                'training': {'show': model['show']} if model.get('show') else None}

    def _read(self, fresh=False):
        """``(shows, url, problem)``: the dashboard's model files, remembered for a minute."""
        url = duck_kit.dashboard_url()
        if not url:
            return None, '', '训练看板的地址不在这台电脑上：只会读本机的训练看板'
        with self.lock:
            cached = self._index
            limit = INDEX_FRESH_S if cached['shows'] is not None else FAILED_FRESH_S
            if not fresh and cached['url'] == url and time.monotonic() - cached['at'] < limit:
                return cached['shows'], url, cached['problem']
            try:
                shows, problem = read_index(url), ''
            except Exception:                     # not answering, or what answered is not the dashboard's data
                shows, problem = None, f'训练看板（{url}）没有回答：它开着，才能从训练项目里选模型文件'
            self._index = {'url': url, 'at': time.monotonic(), 'shows': shows, 'problem': problem}
            return shows, url, problem

    def training_files(self, p):
        """The model files the training project kept for this model's show (route ``model-hub/rounds/history/training``)."""
        record, model = self._open(p.get('round_id'))
        if not model.get('show'):
            return {'available': False, 'problem': '只有小黄鸭的技能模型有训练项目', 'dashboard': '', 'files': []}
        shows, url, problem = self._read()
        if shows is None:
            return {'available': False, 'problem': problem, 'dashboard': url, 'files': []}
        known = {f['sha256']: v['id'] for v in self.hub._versions(model) for f in v.get('files', []) if f.get('sha256')}
        start = self._start_origin(model, record)
        files = [{**{k: e[k] for k in ('key', 'round', 'update', 'passed', 'total', 'steps', 'device', 'at', 'sha256')},
                  'version_id': known.get(e['sha256'], ''), 'start': (e['round'], e['update']) == start or known.get(e['sha256']) == record['version_id'],
                  'candidate': bool(known.get(e['sha256'])) and known[e['sha256']] == record.get('candidate_version_id')}
                 for e in shows.get(model['show'], [])]
        return {'available': True, 'problem': '', 'dashboard': url, 'files': files}

    # ------------------------------------------------------------ choosing
    def pick(self, p):
        """Make a version of the model, or a model file of the training project, the round's candidate
        (route ``model-hub/rounds/candidate``)."""
        if p.get('version_id'):
            with self.hub.lock:
                record, model = self._open(p.get('round_id'))
                return self._candidate(record, model, self.hub._version(model, p['version_id']))
        if p.get('training_key'):
            return self._pick_training(p)
        raise ValueError('请选择一个历史版本，或训练项目里的一个模型文件')

    def _candidate(self, record, model, version):
        """Under the hub's lock: ``version`` is the candidate; a result it already has on the round's set is its result."""
        if version['id'] == record['version_id']:
            raise CodedError('这一版是这一轮的起点，选一个别的版本当候选', 'hub_candidate_start')
        if version['id'] == record.get('candidate_version_id'):
            return self.hub._changed(record)
        measured = next((e for e in self.hub._evaluations(model) if e['version_id'] == version['id'] and e['set_id'] == record['set_id']
                         and e['status'] == 'done'), None)
        changed = self.hub._changed({**record, 'candidate_version_id': version['id'], 'candidate_evaluation_id': measured['id'] if measured else '',
                                     'status': 'review' if measured else 'candidate'})
        self.hub.platform.audit('hub.candidate_chosen', {'model_id': model['id'], 'round_id': record['id'], 'version_id': version['id'],
                                                        'measured': bool(measured)})
        return changed

    def _find(self, show, key):
        """The dashboard's model file ``key`` of ``show``: read again once when it is not in what was read."""
        found = KEY.fullmatch(key) if isinstance(key, str) else None
        if not found:
            raise CodedError('训练项目里没有这个模型文件', 'hub_history_unknown')
        for fresh in (False, True):
            shows, url, problem = self._read(fresh)
            entry = next((e for e in (shows or {}).get(show, []) if e['key'] == key), None)
            if entry:
                return entry, url
        if shows is None:
            raise CodedError(problem, 'hub_history_unavailable')
        raise CodedError('训练看板里没有这个模型文件了（训练项目可能清理过）', 'hub_history_unknown')

    def _by_content(self, model, sha256):
        return next((v for v in self.hub._versions(model) if any(f.get('sha256') == sha256 for f in v.get('files', []))), None)

    def _pick_training(self, p):
        record, model = self._open(p.get('round_id'))
        if not model.get('show'):
            raise CodedError('只有小黄鸭的技能模型能从训练项目里选模型文件', 'hub_history_unknown')
        entry, url = self._find(model['show'], p['training_key'])
        if (entry['round'], entry['update']) == self._start_origin(model, record):
            raise CodedError('这个模型文件和这一轮的起点是同一轮次、同一次更新，选一个别的', 'hub_candidate_start')
        kept = None
        if not self._by_content(model, entry['sha256']):
            data = fetch_file(url, model['show'], entry)                   # outside the lock: it can take a while
            kept = self.hub.files.adopt(data, f"{model['show']}-round{entry['round']}-update{entry['update']:06d}.pt")
        with self.hub.lock:
            used = False
            try:
                record, model = self._open(p.get('round_id'))               # it may have moved on meanwhile
                version = self._by_content(model, entry['sha256'])
                if version is None:
                    if kept is None:
                        raise CodedError('这个模型文件的版本刚被删掉了，请重新选择', 'hub_history_unknown')
                    version, used = self._new_version(model, record, entry, kept), True
                return self._candidate(record, model, version)
            finally:
                if kept and not used:                                       # fetched for nothing: not kept for a day
                    self.hub.files.remove([kept['id']])

    def _new_version(self, model, record, entry, kept):
        labels = {v['label'] for v in self.hub._versions(model)}
        base = f"第 {entry['round']} 轮 · 第 {entry['update']} 次更新"
        label = next(name for name in (base, base + '（训练项目）', *(f'{base}（训练项目 {n}）' for n in range(2, 1000))) if name not in labels)
        origin = {k: v for k, v in (('round', entry['round']), ('update', entry['update']), ('steps', entry['steps'])) if v is not None}
        if entry['device']:
            origin['device'] = entry['device']
        record_ = {'id': hub_records._id('hubv-'), 'model_id': model['id'], 'label': required_text(label, '版本名', 80),
                   'files': [self.hub.files.view(kept['id'])], 'origin': origin, 'note': NOTE, 'round_id': record['id'], 'builtin': False,
                   'source': 'training_project', 'created_at': self.hub._now()}
        if entry['fixed']:                                                  # the dashboard's own result, on the fixed start poses
            record_['training_result'] = {'passed': entry['passed'], 'total': entry['total']}
        self.hub.store.put(hub_records.VERSIONS, record_)
        self.hub.platform.audit('hub.version_added', {'model_id': model['id'], 'version_id': record_['id'], 'sha256': [kept['sha256']],
                                                     'source': 'training_project'})
        return record_

