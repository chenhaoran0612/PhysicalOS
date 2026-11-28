"""What the training project recorded about each skill model of the Microduck (issue #79, docs/DUCK_TRAINING_DATA.md).

Two sources, both in the kit and both checked before anything of them is shown:

* a model package (``shows/<id>.zip``, its checksum pinned in ``duck_kit.SHOWS``) carries ``evidence/``:
  how it was trained (``training-manifest.json``, ``training-status.json``, ``protocol.json``), which
  checkpoint was locked (``model-identity.json``), and every accepted episode of both groups and of the
  control run without the trained correction (``cpu/*.json``, or ``a100/*.json`` for a model accepted on a
  GPU), with each episode's time on the 15–30 s clock (``showtime.json``). Read straight from the zip (the
  package is not unpacked for this), each member against the package's own ``SHA256SUMS``.
* the recordings bundle (``recordings.zip``, its checksum pinned in ``duck_kit.RECORDINGS``): one video and
  one record per accepted episode, listed in ``逐场索引.csv`` with the checksum of the weights it shows. A
  recording is offered for a model only when it shows that model's own weights; its bytes are checked
  against the bundle's ``SHA256SUMS`` every time they are read.

Nothing here runs code from a package: only JSON, CSV and video bytes are read.
"""
import csv
import hashlib
import io
import json
import threading
import zipfile
from pathlib import Path

MAX_MEMBER_BYTES = 4 * 1024 * 1024
MAX_VIDEO_BYTES = 16 * 1024 * 1024
MAX_CASES = 200
GROUPS = (('fixed', '固定组'), ('independent', '独立组'), ('zero', '独立组 · 只按编舞'))
GROUP_FILES = {'cpu': ('evidence/cpu/fixed-cpu.json', 'evidence/cpu/independent-cpu.json', 'evidence/cpu/independent-zero-cpu.json'),
               'cuda': ('evidence/a100/fixed.json', 'evidence/a100/confirmation.json', 'evidence/a100/confirmation_zero.json')}
INDEX = '逐场索引.csv'
INDEX_GROUPS = {'固定组': 'fixed', '独立组': 'independent'}
STOP_REASONS = {'fixed_10_of_10': '固定组十个起姿全部通过后停止'}
CONFIG_KEYS = ('num_envs', 'workers', 'horizon', 'epochs', 'minibatch', 'hidden', 'learning_rate', 'initial_action_std',
               'gamma', 'lam', 'clip', 'entropy', 'seed')


class EvidenceError(ValueError):
    """A record that is not what its checksum list says (the text is shown to the person)."""


def member_name(info):
    """A zip member's name as written: names not marked UTF-8 were still written in UTF-8 by the training project."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode('cp437').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _digits(text):
    return text.isascii() and text.isdigit() and len(text) <= 12


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _sums(archive, names):
    info = names.get('SHA256SUMS')
    if info is None or info.file_size > MAX_MEMBER_BYTES:
        raise EvidenceError('没有文件校验清单')
    listed = {}
    for line in archive.read(info).decode('utf-8').splitlines():
        digest, _, name = line.partition('  ')
        if name:
            listed[name] = digest
    return listed


def _read(archive, names, listed, name, limit=MAX_MEMBER_BYTES):
    info = names.get(name)
    if info is None:
        return None
    if info.file_size > limit:
        raise EvidenceError(f'{name} 太大')
    data = archive.read(info)
    if listed.get(name) != _sha256(data):
        raise EvidenceError(f'{name} 和校验清单不一致')
    return data


def _json(archive, names, listed, name):
    data = _read(archive, names, listed, name)
    if data is None:
        return None
    value = json.loads(data)
    return value if isinstance(value, dict) else None


def _whole(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value, limit):
    return str(value)[:limit] if isinstance(value, str) else ''


def _cases(group, timing):
    """One row per episode: the episode record, with its time on the show clock when there is one."""
    clock = {o.get('seed'): o for o in (timing or {}).get('outcomes', []) if isinstance(o, dict)}
    rows = []
    for episode in (group or {}).get('episodes', [])[:MAX_CASES]:
        if not isinstance(episode, dict) or _whole(episode.get('seed')) is None:
            continue
        timed = clock.get(episode['seed']) or {}
        rows.append({'seed': episode['seed'], 'success': episode.get('success') is True, 'steps': _whole(episode.get('steps')),
                     'reason': _text(episode.get('reason'), 60), 'return': _number(episode.get('episode_return')),
                     'duration_s': _number(timed.get('seconds')), 'in_window': timed.get('in_window') if isinstance(timed.get('in_window'), bool) else None})
    return rows


def _group(key, name, record, timing):
    cases = _cases(record, timing)
    seconds = [c['duration_s'] for c in cases if c['duration_s'] is not None]
    return {'id': key, 'name': name, 'passed': sum(c['success'] for c in cases), 'total': len(cases),
            'min_s': min(seconds) if seconds else None, 'max_s': max(seconds) if seconds else None, 'cases': cases}


def read_package(path):
    """The training and acceptance records of one model package (raises :class:`EvidenceError`)."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = {member_name(i): i for i in archive.infolist() if not i.is_dir()}
            listed = _sums(archive, names)
            identity = _json(archive, names, listed, 'evidence/model-identity.json') or {}
            status = _json(archive, names, listed, 'evidence/training-status.json') or {}
            manifest = _json(archive, names, listed, 'evidence/training-manifest.json') or {}
            protocol = _json(archive, names, listed, 'evidence/protocol.json') or {}
            showtime = _json(archive, names, listed, 'evidence/showtime.json') or {}
            device = str((identity.get('independent_evaluation_platform') or {}).get('device') or 'cpu').split(':')[0]
            files = GROUP_FILES['cuda' if device == 'cuda' else 'cpu']
            records = [_json(archive, names, listed, name) for name in files]
    except (zipfile.BadZipFile, OSError, ValueError, UnicodeDecodeError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError(f'模型包里的记录读不出来：{type(exc).__name__}') from None
    timing = {'fixed': showtime.get('fixed'), 'independent': showtime.get('confirmation'), 'zero': None}
    groups = [_group(key, name, record, timing[key]) for (key, name), record in zip(GROUPS, records) if record]
    config = manifest.get('optimization') if isinstance(manifest.get('optimization'), dict) else {}
    return {
        'checkpoint_sha256': _text(identity.get('checkpoint_sha256'), 64),
        'training': {'updates': _whole(status.get('iteration')), 'requested_updates': _whole(status.get('requested_iterations')),
                     'steps': _whole(status.get('total_steps')), 'stop_reason': STOP_REASONS.get(status.get('stop_reason'), _text(status.get('stop_reason'), 60)),
                     'device': _text(protocol.get('training_device') or identity.get('training_device'), 40),
                     'physics': _text(status.get('physics'), 60), 'torch': _text(manifest.get('torch_version'), 20),
                     'mujoco': _text(manifest.get('mujoco_version') or identity.get('mujoco'), 20),
                     'registered_at': _text(protocol.get('registered_at_utc'), 40),
                     'method': _text(protocol.get('optimizer'), 300),
                     'config': {k: config[k] for k in CONFIG_KEYS if _number(config.get(k)) is not None}},
        'protocol': {'name': _text(showtime.get('protocol'), 40), 'min_s': _number(showtime.get('min_seconds')),
                     'max_s': _number(showtime.get('max_seconds')), 'program': _text(protocol.get('program'), 200),
                     'selection': _text(protocol.get('selection'), 800)},
        'groups': groups,
    }


class Recordings:
    """The recordings bundle of the kit. ``index`` is read once per bundle content; videos on demand."""

    def __init__(self, path, sha256):
        self.path, self.sha256 = Path(path), sha256
        self.lock = threading.Lock()
        self._checked = None              # (mtime, size) of the bundle whose checksum was found right
        self._refused = None              # (mtime, size) of a bundle whose checksum was found wrong: not hashed again
        self._index = None

    def _open(self):
        info = self.path.stat()
        key = (info.st_mtime_ns, info.st_size)
        if key == self._refused:
            raise EvidenceError('录像包不是登记的那一份，没有使用')
        if self._checked != key:
            digest = hashlib.sha256()
            with open(self.path, 'rb') as stream:
                for block in iter(lambda: stream.read(1 << 20), b''):
                    digest.update(block)
            if digest.hexdigest() != self.sha256:
                self._refused = key
                raise EvidenceError('录像包不是登记的那一份，没有使用')
            self._checked, self._index = key, None
        return zipfile.ZipFile(self.path)

    def index(self):
        """``{(model id, group, seed): {"video", "weights", "steps", "duration_s"}}``; ``{}`` when there is no bundle."""
        with self.lock:
            if not self.path.is_file():
                return {}
            with self._open() as archive:
                if self._index is None:
                    names = {member_name(i): i for i in archive.infolist() if not i.is_dir()}
                    listed = _sums(archive, names)
                    raw = _read(archive, names, listed, INDEX)
                    found = {}
                    for row in csv.DictReader(io.StringIO((raw or b'').decode('utf-8-sig'))):
                        group, seed = INDEX_GROUPS.get(row.get('组别')), row.get('起姿种子') or ''
                        video = row.get('录像') or ''
                        if group and _digits(seed) and video in listed:
                            steps, seconds = row.get('控制步数') or '', row.get('原速秒') or ''
                            found[(row.get('任务') or '', group, int(seed))] = {
                                'video': video, 'weights': row.get('权重 SHA-256') or '',
                                'steps': int(steps) if _digits(steps) else None,
                                'duration_s': float(seconds) if _digits(seconds.replace('.', '', 1)) else None}
                    self._index = found
                return dict(self._index)

    def video(self, show, group, seed):
        """The bytes of one recording (checked), or ``None``."""
        entry = self.index().get((show, group, seed))
        if entry is None:
            return None
        with self.lock, self._open() as archive:
            names = {member_name(i): i for i in archive.infolist() if not i.is_dir()}
            return _read(archive, names, _sums(archive, names), entry['video'], MAX_VIDEO_BYTES)
