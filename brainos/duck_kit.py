"""The Microduck AD-P102: its model, its trained skill models and their training (issue #63, docs/DUCK_SKILLS.md).

What comes with the app (the *kit*, built by ``scripts/duck_kit.py`` from files that are not in this
repository) is a folder::

    kit.json            what is inside (written by the build script)
    shows/<id>.zip      one package per skill model, exactly as the training project delivered it:
                        the scene and meshes of the robot, the frozen choreography and judge code,
                        the policy weights, the acceptance records and a recorded replay
    engine/             the physics engine version those packages were accepted on (and onnxruntime)
    training.json       the training progress as it was when the kit was built
    NOTICE.txt          whose files these are and under which terms

This module is the console's side: the list of skill models (:data:`SHOWS`, with the checksum of each
package: a package that is not the one named here is not used), adding a simulated duck, asking it to
perform, and the training progress (from the training project's own dashboard when it is running on this
computer, otherwise the copy in the kit).
"""
import hashlib
import json
import os
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urlsplit

from . import duck_evidence, duck_paths, duck_skills, robot_scope
from .contracts import required_text
from .errors import CodedError

PROFILE_ID = 'microduck-adp102'
VENDOR, MODEL = 'Microduck', 'AD-P102'
ADAPTER = 'brainos.duck_body:body'
SIM_KIND = 'mujoco'                    # the console shows its picture like any MuJoCo simulated robot's
START_TIMEOUT_S = 60
ENGINE = {'mujoco': '3.10.0', 'onnxruntime': '1.20.1'}
DASHBOARD_ENV = 'NEUXMIND_DUCK_DASHBOARD'
DEFAULT_DASHBOARD = 'http://127.0.0.1:8767'
DASHBOARD_TIMEOUT_S = 30
TRAINING_FRESH_S = 120                 # issue #79: one read of the dashboard costs seconds of CPU and ~0.5 GB at its peak
MAX_TRAINING_BYTES = 512 * 1024 * 1024  # the dashboard's answer, unpacked (151 MB on 2026-11-28)
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024    # the kit's copy is the trimmed data (about 0.1 MB)
MAX_TRAINING_ROUNDS = 400
MAX_EVENTS = 600
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_FILES = 2000
MAX_SEED = 2 ** 32 - 1
REPLAY = 'replay.mp4'
MAX_REPLAY_BYTES = 32 * 1024 * 1024

DELIVERED, CANDIDATE, TRAINING, ARCHIVED = 'delivered', 'candidate', 'training', 'archived'
STATUS_NAMES = {DELIVERED: '已交付', CANDIDATE: '候选', TRAINING: '训练中', ARCHIVED: '已归档'}
SHOWTIME = 'showtime-15-30s-v1'        # issue #79: an accepted show lasts 15 to 30 seconds from HOME
HOME_SEEDS = (1_700_003_000, 10)       # the fixed group: the same ten start poses for every model and version
# Issue #79: the training project's delivery of 2026-11-27, two bundles. The models bundle holds the six
# packages below (``模型包/<package>``); the recordings bundle one video and one record per accepted episode.
BUNDLE = {'file': 'HD1910-六项已验收模型-15至30秒.zip', 'sha256': 'a7e725ed6d5a5cd58c66eceb30b9a9328dfba08bbea0d4273d20b1cbfa40db23',
          'folder': '模型包/'}
RECORDINGS = {'file': 'HD1910-120场原速录像与索引.zip', 'sha256': 'efa72477a158137ad28e95e2c47f118ca20e7a14bbc9d04a0771818623e72cba',
              'kit': 'recordings.zip'}
OLD_RULES = '按当时的验收要求（不看用时）验收；训练项目现在要求每段 15–30 秒，这一版不再交付，模型包不随这个安装带来。'

# The skill models. `acceptance`: whole-episode passes out of ten on the fixed group (HOME_SEEDS) and on the
# model's own pre-registered independent group (ten seeds from `independent_from`), as the training project
# recorded them; `seconds`: the shortest and longest accepted episode on the show clock. `history`: the
# versions that came with earlier releases (issues #63-#71), kept so that what was recorded about them still
# has its version; they are not in the kit and do not run. A version pinned before issue #79 is `legacy`:
# its imported evaluations keep the ids they had then.
SHOWS = (
    {'id': 'bow_roll', 'title': '鞠躬鞠过头', 'status': DELIVERED,
     'summary': '渐深鞠躬，贴地前滚一圈，落脚停半拍，补一个浅鞠躬后回正；再向左右两席致意，回到正中俯身谢幕、昂首收礼。约 18 秒。',
     'package': 'AD-P102-bow_roll-round140-u1-cpu-verified.zip',
     'sha256': 'a9ed7ddaaa63b8acbbb30460140f5bb95f28dfda70f4f56d71890cffbf6ca946',
     'origin': {'round': 140, 'update': 1, 'steps': 1920, 'device': 'Mac CPU'},
     'acceptance': {'fixed': [10, 10], 'independent': [10, 10]}, 'independent_from': 2_986_140_700, 'seconds': [18.22, 18.58],
     'history': ({'package': 'AD-P102-bow_roll-round40-u4-cpu-verified.zip', 'legacy': True,
                  'sha256': '1b08fd1c172853d310d7879f845d749a0d3b4c2b966eee67eb16f98693feb504',
                  'origin': {'round': 40, 'update': 4, 'steps': 2048, 'device': 'Mac CPU'},
                  'acceptance': {'fixed': [10, 10], 'independent': [10, 10]}, 'independent_from': 1_991_724_000},)},
    {'id': 'drunk_circle', 'title': '醉鸭不倒（绕场两圈）', 'status': DELIVERED,
     'summary': '踉跄着沿同一方向绕场两整圈，每圈四段醉步，有左右侧倾和补步，最后收脚站稳。约 28–30 秒。',
     'package': 'AD-P102-drunk_circle-round115791-u14-cpu-verified.zip',
     'sha256': '806511b3bce014b24ac42916b24efe9f47d51ff6e45e0771127b1be8f0a76ff6',
     'origin': {'round': 115791, 'update': 14, 'steps': 129024, 'device': 'Mac CPU'},
     'acceptance': {'fixed': [10, 10], 'independent': [10, 10]}, 'independent_from': 3_715_791_100, 'seconds': [28.0, 29.68],
     'history': ({'package': 'AD-P102-drunk-circle-round19-u108-candidate.zip', 'legacy': True,
                  'sha256': 'ff78ccc0ecb8805afe7bd55c92f1e2305ab102ee02f5510986fe405f8503275c',
                  'origin': {'round': 19, 'update': 108, 'steps': 69120, 'device': 'Mac CPU'},
                  'acceptance': {'fixed': [10, 10], 'independent': [8, 10]}, 'independent_from': 1_919_274_600},)},
    {'id': 'water', 'title': '单脚探水', 'status': DELIVERED,
     'summary': '走到岸线，左右察看后低头看水边；单脚悬起，探头近水面又猛地缩头，保持单脚抖一抖，再落脚站稳。约 17 秒。',
     'package': 'AD-P102-water-round132-u1-cpu-verified.zip',
     'sha256': 'dd55812dc78e46c1743fdd04280b458748edca243d82c6e1468067651d2762c3',
     'origin': {'round': 132, 'update': 1, 'steps': 3456, 'device': 'Mac CPU'},
     'acceptance': {'fixed': [10, 10], 'independent': [10, 10]}, 'independent_from': 3_293_174_000, 'seconds': [16.76, 16.78],
     'history': ({'package': 'AD-P102-water-round6-u4.zip', 'legacy': True,
                  'sha256': '66a74cb6718868e454f61a87b1638a3d3887d56b7545e45b9acdd1e4ebd98b1a',
                  'origin': {'round': 6, 'update': 4, 'steps': 8192, 'device': 'NVIDIA A100'},
                  'acceptance': {'fixed': [10, 10], 'independent': [10, 10]}, 'independent_from': 1_826_014_600},)},
    {'id': 'floor_spin', 'title': '地板舞定格', 'status': DELIVERED,
     'summary': '侧坐，单脚蹬地带着低位机身转动，头转向观众定格；起身后向右、向左各致意两次，回正站稳。约 18 秒。',
     'package': 'AD-P102-floor_spin-round157-u1-cpu-verified.zip',
     'sha256': '1b94b2d03cb876f54f7b4783cff45bcd81d945f6893308ad5d9006427aa11331',
     'origin': {'round': 157, 'update': 1, 'steps': 1792, 'device': 'Mac CPU'},
     'acceptance': {'fixed': [10, 10], 'independent': [10, 10]}, 'independent_from': 2_487_486_980, 'seconds': [17.9, 17.94]},
    {'id': 'penalty', 'title': '假动作点球', 'status': DELIVERED,
     'summary': '退两步歪头瞄球，向左虚晃，右脚把球踢向前方；转向观众左右庆祝，收稳后向两侧观众致意谢幕。约 18 秒。',
     'package': 'AD-P102-penalty-round167-u1-cpu-verified.zip',
     'sha256': '6dbcbcb206d31a672c17dc5d9937a90ad304c219f32f711cbb1059e0f774d079',
     'origin': {'round': 167, 'update': 1, 'steps': 2304, 'device': 'Mac CPU'},
     'acceptance': {'fixed': [10, 10], 'independent': [10, 10]}, 'independent_from': 3_155_167_850, 'seconds': [17.9, 17.9],
     'history': ({'package': 'AD-P102-penalty-round6-u108-cpu-verified.zip', 'legacy': True,
                  'sha256': 'f0dd49e4efeba1fba2412ac23e6f5cba0ded1bdd8621a159e867ea34ef0f7e2e',
                  'origin': {'round': 6, 'update': 108, 'steps': 221184, 'device': 'NVIDIA A100'},
                  'acceptance': {'fixed': [10, 10], 'independent': [10, 10]}, 'independent_from': 1_842_190_600},)},
    {'id': 'recharge', 'title': '没电了，又满了', 'status': DELIVERED,
     'summary': '小步走着渐渐垂头减速，缓慢趴下趴住两秒；先歪头醒来，再撑起身，小碎步快步离场。约 24–26 秒。',
     'package': 'AD-P102-recharge-round1241-u3-mujoco-full-stage.zip',
     'sha256': '5fcadd6f17d05398f542300ae10feb58c76e52e6ad6e220c91d034c3601b6688',
     'origin': {'round': 1241, 'update': 3, 'steps': 13824, 'device': 'Mac CPU'},
     'acceptance': {'fixed': [10, 10], 'independent': [10, 10]}, 'independent_from': 3_541_241_000, 'seconds': [24.18, 25.68]},
    {'id': 'drunk', 'title': '醉鸭不倒（旧版短节目）', 'status': ARCHIVED,
     'summary': '约 4.5 秒：三次侧倾、补步、站直张望。已被绕场两圈的版本取代。',
     'note': '训练项目从 2026-11-27 起不再交付这个短节目（新的要求是每段 15–30 秒）；它的模型包不随这个安装带来，也不再是仿真小鸭的技能。',
     'history': ({'package': 'AD-P102-drunk-round3-u16.zip', 'legacy': True,
                  'sha256': 'f749db98a877c9d118a77903d35a4f738e0e2790942e3965805397603d580978',
                  'origin': {'round': 3, 'update': 16, 'steps': 21504, 'device': 'NVIDIA A100'},
                  'acceptance': {'fixed': [10, 10], 'independent': [10, 10]}, 'independent_from': 1_820_011_900},)},
)
BY_ID = {show['id']: show for show in SHOWS}
PACKAGED = tuple(show for show in SHOWS if show.get('package'))


def versions(show):
    """Every version of a show's model the app knows, the one in the kit first: ``(version, current)`` pairs.
    A version has ``package``, ``sha256``, ``origin``, ``acceptance``, ``independent_from`` and maybe
    ``seconds`` and ``legacy``."""
    found = [(show, True)] if show.get('package') else []
    return found + [(old, False) for old in show.get('history', ())]


def seed_group(first):
    """The ten seeds of a group that starts at ``first``."""
    return tuple(range(first, first + HOME_SEEDS[1]))


class KitError(ValueError):
    """A package of the kit that cannot be used (the text is shown to the person)."""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def read_kit(kit=None):
    """``kit.json`` of the kit on this computer, or ``None`` when there is no (readable) kit."""
    path = (Path(kit) if kit else duck_paths.kit_dir()) / 'kit.json'
    try:
        if path.stat().st_size > 1_000_000:
            return None
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get('shows'), dict) else None


def unpack(package, target):
    """Unpack a show package into ``target`` (which must not exist) and check every file against the
    package's own ``SHA256SUMS``. Nothing outside ``target`` is written; a link is not followed or made."""
    package, target = Path(package), Path(target)
    part = target.with_name(target.name + '.part')
    shutil.rmtree(part, ignore_errors=True)
    try:
        with zipfile.ZipFile(package) as archive:
            members = archive.infolist()
            if len(members) > MAX_PACKAGE_FILES or sum(m.file_size for m in members) > MAX_PACKAGE_BYTES:
                raise KitError('技能模型包太大')
            part.mkdir(parents=True, mode=0o700)
            root = part.resolve()
            for member in members:
                name = member.filename
                if member.is_dir():
                    continue
                if name.startswith('/') or '\\' in name or '..' in Path(name).parts or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise KitError('技能模型包里有不该有的路径')
                destination = (root / name).resolve()
                if not destination.is_relative_to(root):
                    raise KitError('技能模型包里有不该有的路径')
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, open(destination, 'wb') as out:
                    shutil.copyfileobj(source, out, 1 << 20)
        sums = part / 'SHA256SUMS'
        if not sums.is_file():
            raise KitError('技能模型包里没有文件校验清单')
        checked = 0
        for line in sums.read_text(encoding='utf-8').splitlines():
            expected, _, name = line.partition('  ')
            file = (root / name).resolve()
            if not name or not file.is_relative_to(root) or not file.is_file() or _sha256(file) != expected:
                raise KitError(f'技能模型包里的文件和它的校验清单不一致：{name[:80]}')
            checked += 1
        if not checked:
            raise KitError('技能模型包的文件校验清单是空的')
        os.replace(part, target)
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(part, ignore_errors=True)
        raise KitError(f'技能模型包打不开：{type(exc).__name__}') from None
    except KitError:
        shutil.rmtree(part, ignore_errors=True)
        raise
    return target


# ---------------------------------------------------------------- training progress
def _whole(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _figures(row):
    if not isinstance(row, dict):
        return None
    return {'round': _whole(row.get('round_number')), 'update': _whole(row.get('iteration')), 'successes': _whole(row.get('successes')),
            'episodes': _whole(row.get('episodes')), 'steps': _whole(row.get('total_steps')),
            'device': 'GPU' if row.get('training_backend') == 'cuda' else 'CPU' if row.get('training_backend') == 'cpu' else ''}


def _pair(group):
    """``[passes, episodes]`` of a group record of the dashboard, or ``None``."""
    if not isinstance(group, dict) or _whole(group.get('successes')) is None or not _whole(group.get('episodes')):
        return None
    return [group['successes'], group['episodes']]


def _delivered(row):
    """A delivered version as the dashboard records it (``release``, ``historical_models``): its round and
    update, both groups and the control without the trained correction, and its time on the show clock."""
    if not isinstance(row, dict) or _whole(row.get('round_number')) is None:
        return None
    duration = row.get('duration') if isinstance(row.get('duration'), dict) else {}
    times = [g.get(k) for g in (duration.get('fixed'), duration.get('confirmation')) if isinstance(g, dict) for k in ('min_s', 'max_s')]
    times = [t for t in times if isinstance(t, (int, float)) and not isinstance(t, bool)]
    seeds = (row.get('confirmation') or {}).get('seeds') if isinstance(row.get('confirmation'), dict) else None
    return {'round': row['round_number'], 'update': _whole(row.get('iteration')),
            'device': str(row.get('training_device') or row.get('device') or '')[:40],
            'fixed': _pair(row.get('fixed')), 'independent': _pair(row.get('confirmation')), 'zero': _pair(row.get('zero')),
            'independent_from': seeds[0] if isinstance(seeds, list) and seeds and _whole(seeds[0]) is not None else None,
            'seconds': [min(times), max(times)] if times else None,
            'timed': duration.get('qualified') if isinstance(duration.get('qualified'), bool) else None}


def _training_rounds(task):
    """Issue #79: the training runs of one show, oldest first: round, state, updates, steps, device. The
    dashboard lists every round for every show; a round that never started this show is left out."""
    rows = []
    for row in task.get('rounds') if isinstance(task.get('rounds'), list) else []:
        if isinstance(row, dict) and _whole(row.get('number')) is not None and row.get('state') != 'not_started':
            rows.append({'round': row['number'], 'state': str(row.get('state') or '')[:20], 'updates': _whole(row.get('iteration')),
                         'steps': _whole(row.get('total_steps')), 'device': str(row.get('accelerator') or row.get('device') or '')[:40],
                         'last_update': row.get('last_update') if isinstance(row.get('last_update'), (int, float)) and not isinstance(row.get('last_update'), bool) else None})
    rows.sort(key=lambda r: r['round'])
    return rows[-MAX_TRAINING_ROUNDS:]


def trim_training(data):
    """What the page shows of the training dashboard's data (``/api/evolution``): per show the best and
    the latest whole-episode evaluation and the series of evaluations; since issue #79 also the delivered
    version, the versions delivered before it and the training runs. ``None`` when it is not that data."""
    if not isinstance(data, dict) or not isinstance(data.get('tasks'), list):
        return None
    tasks = []
    for task in [*data['tasks'], *(data.get('archived_tasks') if isinstance(data.get('archived_tasks'), list) else [])]:
        if not isinstance(task, dict) or not isinstance(task.get('id'), str):
            continue
        events = [e for e in (task.get('events') if isinstance(task.get('events'), list) else []) if isinstance(e, dict)]
        series = [[_whole(e.get('round_number')), _whole(e.get('iteration')), e['successes'], e['episodes'],
                   e.get('report_mtime') if isinstance(e.get('report_mtime'), (int, float)) and not isinstance(e.get('report_mtime'), bool) else None]
                  for e in events if _whole(e.get('successes')) is not None and _whole(e.get('episodes')) is not None]
        series.sort(key=lambda row: (row[4] or 0, row[0] or 0, row[1] or 0))
        history = task.get('historical_models') if isinstance(task.get('historical_models'), list) else []
        runs = _training_rounds(task)
        tasks.append({'id': task['id'][:40], 'title': str(task.get('title') or task['id'])[:60], 'active': task.get('active') is not False,
                      'qualified': bool(task.get('qualified')), 'best': _figures(task.get('best')), 'latest': _figures(task.get('current')),
                      'evaluations': len(series), 'series': series[-MAX_EVENTS:],
                      'release': _delivered(task.get('release')), 'history': [r for r in map(_delivered, history[:40]) if r],
                      'runs': runs, 'run_count': len(runs), 'run_steps': sum(r['steps'] or 0 for r in runs)})
    source = data.get('source') if isinstance(data.get('source'), dict) else {}
    sync = source.get('sync') if isinstance(source.get('sync'), dict) else {}
    return {'generated_at': data.get('generated_at'), 'required_successes': data.get('required_successes'),
            'rounds': len(data['rounds']) if isinstance(data.get('rounds'), list) else None,
            'last_update': source.get('last_update'), 'sync_state': str(sync.get('state') or '')[:20],
            'sync_last_success': sync.get('last_success'), 'tasks': tasks}


def dashboard_url():
    """The training dashboard on this computer. Only a loopback http address is ever asked."""
    url = os.environ.get(DASHBOARD_ENV) or DEFAULT_DASHBOARD
    try:
        parts = urlsplit(url)
        port = parts.port or 80
    except ValueError:
        return ''
    if parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', 'localhost') or parts.username or parts.password:
        return ''
    return f'http://{parts.hostname}:{port}'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                                  # the dashboard is asked, nothing it points to


# Issue #79: the only fields of the dashboard's answer that are kept while it is parsed (its whole answer was
# 151 MB of JSON on 2026-11-28, most of it per-step detail the page does not show).
TRAINING_FIELDS = frozenset((
    'tasks', 'archived_tasks', 'id', 'title', 'active', 'qualified', 'best', 'current', 'events', 'round_number', 'iteration',
    'successes', 'episodes', 'total_steps', 'training_backend', 'report_mtime', 'source', 'last_update', 'sync', 'state',
    'last_success', 'generated_at', 'required_successes', 'rounds', 'number', 'accelerator', 'device', 'release',
    'historical_models', 'fixed', 'confirmation', 'zero', 'seeds', 'duration', 'min_s', 'max_s', 'training_device'))


def _slim(pairs):
    return {key: value for key, value in pairs if key in TRAINING_FIELDS}


def _read_answer(response):
    """The body of the dashboard's answer, unpacked when it came in gzip; ``None`` when it is too large."""
    unpack = zlib.decompressobj(16 + zlib.MAX_WBITS) if response.headers.get('Content-Encoding') == 'gzip' else None
    parts, size = [], 0
    while True:
        block = response.read(1 << 20)
        if not block:
            break
        block = unpack.decompress(block, MAX_TRAINING_BYTES + 1 - size) if unpack else block
        size += len(block)
        if size > MAX_TRAINING_BYTES or (unpack and unpack.unconsumed_tail):
            return None
        parts.append(block)
    if unpack:
        parts.append(unpack.flush())
    return b''.join(parts)


def fetch_training(url):
    """The dashboard's data, trimmed; ``None`` when it does not answer."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())     # loopback: never through a proxy
    request = urllib.request.Request(url + '/api/evolution', headers={'Accept-Encoding': 'gzip'})
    try:
        with opener.open(request, timeout=DASHBOARD_TIMEOUT_S) as response:
            raw = _read_answer(response)
        return trim_training(json.loads(raw, object_pairs_hook=_slim)) if raw is not None else None
    except Exception:                                # whatever answered there is not the dashboard's data
        return None


def replay_response(path, range_header=None):
    """``(status, headers, body)`` for a replay video, honouring one byte range (a WebKit player asks for
    ranges and does not play without them); ``None`` when there is no video."""
    if path is None:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return video_response(data, range_header)


def video_response(data, range_header=None):
    """``(status, headers, body)`` for the bytes of a video (issue #79: a recording comes out of a zip)."""
    if data is None:
        return None
    size, first, last, status = len(data), 0, len(data) - 1, 200
    headers = {'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes'}
    text = (range_header or '').strip()
    if text.startswith('bytes=') and ',' not in text:
        start, _, end = text[6:].partition('-')
        try:
            if start == '':                              # the last N bytes
                first, last = max(0, size - int(end)), size - 1
            else:
                first, last = int(start), min(int(end), size - 1) if end else size - 1
        except ValueError:
            first, last = 0, size - 1
        else:
            if first < 0 or first > last or first >= size:
                return 416, {**headers, 'Content-Range': f'bytes */{size}', 'Content-Length': '0'}, b''
            status = 206
            headers['Content-Range'] = f'bytes {first}-{last}/{size}'
    body = data[first:last + 1]
    return status, {**headers, 'Content-Length': str(len(body))}, body


class DuckKit:
    def __init__(self, platform, kit=None):
        self.platform, self.store = platform, platform.store
        self.kit = Path(kit) if kit else None
        self.root = Path(self.store.root) / 'duck-kit'       # one unpacked copy per package content
        self.lock = threading.Lock()
        self.locks = {}
        self._training = {'at': 0.0, 'live': None, 'busy': False}
        self._checked = {}                                   # (package path, mtime, size) → its checksum
        self._evidence = {}                                  # issue #79: package checksum → its training and acceptance records
        self._recorded = None                                # issue #79: the recordings bundle of the kit

    # ------------------------------------------------------------ what is here
    def _dir(self):
        return self.kit or duck_paths.kit_dir()

    def _zip(self, show):
        """The package of a show when the kit has it (any checksum: it is checked when it is unpacked)."""
        kit = read_kit(self._dir())
        entry = (kit or {}).get('shows', {}).get(show['id']) if show.get('package') else None
        path = self._dir() / 'shows' / (show['id'] + '.zip')
        return path if isinstance(entry, dict) and path.is_file() else None

    def _engine(self):
        kit = read_kit(self._dir()) or {}
        if isinstance(kit.get('engine'), dict) and kit['engine'].get('installed'):
            return ''                                        # a kit for tests: the MuJoCo that is installed
        path = duck_paths.engine_dir(self._dir())
        return path if (path / 'mujoco' / '__init__.py').is_file() else None

    def problem(self):
        """Why a simulated duck cannot be added on this computer ('' when it can)."""
        if read_kit(self._dir()) is None:
            return '这个安装包里没有它的机器人模型和技能模型'
        if self._engine() is None:
            return '这个安装包里没有技能模型验收时用的物理引擎'
        if not any(self._zip(show) for show in PACKAGED):
            return '这个安装包里没有它的技能模型包'
        return ''

    def catalog(self):
        """The skill models for the page (part of the state the page reads; nothing here changes on a timer)."""
        kit, problem = read_kit(self._dir()) or {}, self.problem()
        shows = []
        for show in SHOWS:
            shows.append({'id': show['id'], 'title': show['title'], 'summary': show['summary'], 'status': show['status'],
                          'status_name': STATUS_NAMES[show['status']], 'origin': show.get('origin'),
                          'acceptance': show.get('acceptance'), 'note': show.get('note', ''),
                          'sha256': show.get('sha256', ''), 'packaged': bool(self._zip(show)),
                          # issue #67: a model with a package is a skill of the robot
                          'skill': duck_skills.describe(show) if show.get('package') else None,
                          # issue #79: its time on the show clock, its own independent group, the versions before it
                          'seconds': show.get('seconds'), 'independent_from': show.get('independent_from'),
                          'history': [{'round': old['origin']['round'], 'update': old['origin']['update'], 'device': old['origin'].get('device', ''),
                                       'acceptance': old['acceptance'], 'independent_from': old.get('independent_from'), 'package': old['package']}
                                      for old in show.get('history', ())]})
        return {'profile_id': PROFILE_ID, 'vendor': VENDOR, 'model': MODEL, 'available': not problem, 'problem': problem,
                'engine': dict(ENGINE), 'built_at': kit.get('built_at'), 'shows': shows,
                'dashboard': dashboard_url(), 'showtime': SHOWTIME, 'recordings': self._recordings().path.is_file()}

    # ------------------------------------------------------------ packages
    def _registered(self, show_id):
        """``(show, zip path)`` of a show whose package in the kit is the one :data:`SHOWS` names."""
        show = BY_ID.get(show_id) if isinstance(show_id, str) else None
        path = self._zip(show) if show else None
        if path is None:
            raise CodedError('这个安装里没有这个技能模型', 'duck_show_missing')
        info = path.stat()
        key = (str(path), info.st_mtime_ns, info.st_size)
        with self.lock:
            found = self._checked.get(key)
        if found is None:
            found = _sha256(path)
            with self.lock:
                self._checked[key] = found
        if found != show['sha256']:
            raise CodedError(f'「{show["title"]}」的技能模型包不是登记的那一份，没有使用', 'duck_package_changed')
        return show, path

    def package(self, show_id):
        """The unpacked package of a show (unpacked once per content, checked against :data:`SHOWS`)."""
        show, path = self._registered(show_id)
        target = self.root / show['sha256']
        with self.lock:
            one = self.locks.setdefault(show['sha256'], threading.Lock())
        with one:
            if not target.is_dir():
                try:
                    self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                    unpack(path, target)
                except (KitError, OSError) as exc:
                    raise CodedError(f'「{show["title"]}」的技能模型包不能用：{exc}', 'duck_package_invalid') from None
        return target

    def replay(self, show_id):
        """The recorded replay of a show's accepted run (a video file) or ``None``."""
        try:
            path = self.package(show_id) / REPLAY
        except ValueError:
            return None
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REPLAY_BYTES:
                return None
        except OSError:
            return None
        return path

    # ------------------------------------------------------------ what the training project recorded (issue #79)
    def _recordings(self):
        place = self._dir() / RECORDINGS['kit']
        with self.lock:
            if self._recorded is None or self._recorded.path != place:
                self._recorded = duck_evidence.Recordings(place, RECORDINGS['sha256'])
            return self._recorded

    def _records(self, show):
        """The package's own records of how the model was trained and accepted (cached per package content)."""
        _show, path = self._registered(show['id'])
        with self.lock:
            found = self._evidence.get(show['sha256'])
        if found is None:
            try:
                found = duck_evidence.read_package(path)
            except duck_evidence.EvidenceError as exc:
                raise CodedError(f'「{show["title"]}」的训练记录不能用：{exc}', 'duck_evidence_invalid') from None
            with self.lock:
                self._evidence[show['sha256']] = found
        return found

    def evidence(self, p):
        """How a skill model was trained and accepted, episode by episode (route ``duck/evidence``): the records
        in its package, which accepted episodes have a recording, and the versions before it."""
        show = BY_ID.get(p.get('show')) if isinstance(p.get('show'), str) else None
        if show is None:
            raise CodedError('没有这个技能模型', 'duck_show_missing')
        records, problem = None, ''
        if show.get('package'):
            try:
                records = self._records(show)
            except CodedError as exc:
                problem = str(exc)
        try:
            index = self._recordings().index() if records else {}
        except (duck_evidence.EvidenceError, OSError, ValueError) as exc:
            index, problem = {}, problem or f'录像不能用：{exc}'
        if records:
            weights = records['checkpoint_sha256']
            groups = []
            for group in records['groups']:
                cases = [{**case, 'recording': bool(weights) and (index.get((show['id'], group['id'], case['seed'])) or {}).get('weights') == weights}
                         for case in group['cases']]
                groups.append({**group, 'cases': cases})
            records = {**records, 'groups': groups}
        return {'show': show['id'], 'title': show['title'], 'records': records, 'problem': problem,
                'versions': [{'current': current, 'round': v['origin']['round'], 'update': v['origin']['update'], 'steps': v['origin'].get('steps'),
                              'device': v['origin'].get('device', ''), 'acceptance': v['acceptance'], 'seconds': v.get('seconds'),
                              'independent_from': v.get('independent_from'), 'package': v['package'], 'sha256': v['sha256'],
                              'in_kit': current and bool(self._zip(show))} for v, current in versions(show)]}

    def recording(self, show_id, group, seed):
        """The bytes of the recording of one accepted episode of the model in the kit, or ``None``."""
        show = BY_ID.get(show_id) if isinstance(show_id, str) else None
        if show is None or not show.get('package') or group not in ('fixed', 'independent') or not isinstance(seed, int):
            return None
        try:
            weights = self._records(show)['checkpoint_sha256']
            entry = self._recordings().index().get((show['id'], group, seed))
            if not weights or not entry or entry['weights'] != weights:
                return None
            return self._recordings().video(show['id'], group, seed)
        except (CodedError, duck_evidence.EvidenceError, OSError, ValueError):
            return None

    # ------------------------------------------------------------ adding a simulated duck
    def _usable(self):
        """``({show id: package checksum}, why one was refused)`` of the packages in the kit that can be used."""
        shows, refused = {}, ''
        for show in PACKAGED:
            if self._zip(show):
                try:
                    self.package(show['id'])
                except CodedError as exc:                    # one package that cannot be used does not take the others with it
                    refused = str(exc)
                    continue
                shows[show['id']] = show['sha256']
        return shows, refused

    def refresh(self, robot_id):
        """Issue #79: before a simulated duck starts, it gets the packages of this installation, so that it performs
        the models the page lists (a duck added with older packages would otherwise keep performing those). When
        this installation has none it can use, the duck keeps what it had. Never raises."""
        try:
            path = self.platform.sims.root / robot_id / 'duck.json'
            config = json.loads(path.read_text(encoding='utf-8'))
            shows, _refused = self._usable()
            if not isinstance(config, dict) or not shows or config.get('shows') == shows:
                return False
            config = {**config, 'shows': shows, 'default_show': config.get('default_show') if config.get('default_show') in shows else next(iter(shows)),
                      'packages_root': str(self.root)}
            part = path.with_name(path.name + '.part')
            part.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
            os.replace(part, path)
            return True
        except (OSError, ValueError):
            return False

    def add(self, profiles, profile, p):
        """Add and start a simulated duck (called by ``SimProfiles.add`` for the duck's profile)."""
        from .service import now, uid
        problem = self.problem()
        if problem:
            raise CodedError(f'「{VENDOR} {MODEL}」现在不能添加：{problem}', 'sim_profile_unavailable')
        shows, refused = self._usable()
        if not shows:
            raise CodedError(f'「{VENDOR} {MODEL}」现在不能添加：{refused or "没有可以用的技能模型包"}', 'sim_profile_unavailable')
        project, owner = profiles._task(p)
        taken = {r['name'] for r in self.store.list('robots') if r.get('project_id') == owner}
        name = required_text(p.get('name') or f'{VENDOR} {MODEL} 仿真', '机器人名称', 100)
        base, n = name, 2
        while name in taken:
            name, n = f'{base} {n}', n + 1
        sims = self.platform.sims
        robot = {'id': uid('robot'), 'project_id': owner, 'name': name, 'family': profile['type'], 'endpoint': '',
                 'token': secrets.token_urlsafe(32), 'status': 'unconnected', 'capabilities': [],
                 'sim': {'kind': SIM_KIND, 'adapter': ADAPTER, 'enabled': True, 'state': 'starting', 'duck': True,
                         'profile_id': profile['id'], 'start_timeout_s': START_TIMEOUT_S},
                 'created_at': now()}
        state_dir = sims.root / robot['id']
        state_dir.mkdir(parents=True, exist_ok=True)
        config = {'packages_root': str(self.root), 'shows': shows, 'default_show': next(iter(shows)),
                  'vendor': VENDOR, 'model': MODEL, 'name': name, 'instance_id': 'duck-' + robot['id'][-12:],
                  'view_dir': str(state_dir / 'view'), 'control_dir': str(state_dir / 'duck'),
                  **({'kit_dir': str(self.kit)} if self.kit else {})}
        (state_dir / 'duck.json').write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        self.store.put('robots', robot)
        self.platform.audit('robot.sim_added', {'robot_id': robot['id'], 'kind': SIM_KIND, 'profile_id': profile['id']}, owner)
        return {**sims.start(robot['id']), 'project_id': project['id'] if project else ''}

    # ------------------------------------------------------------ its skills (issue #67)
    def bind_skills(self, robot_id):
        """Every skill model a running simulated duck has is installed on it as a skill and registered for
        its brand and model (called whenever one starts). What happened is kept on the robot; never raises."""
        try:
            robot = self.store.get('robots', robot_id)
            if not (robot.get('sim') or {}).get('duck') or not robot.get('endpoint'):
                return {}
            have = self._config(robot).get('shows', {})
            bound = duck_skills.bind(self.platform, robot, [show for show in PACKAGED if show['id'] in have])
            robot = self.store.get('robots', robot_id)
            if robot['sim'].get('skills') != bound:
                robot['sim']['skills'] = bound
                self.store.put('robots', robot)
            return bound
        except Exception:                                    # the duck runs without them; the page says which are missing
            return {}

    # ------------------------------------------------------------ performing
    def _duck(self, p):
        try:
            if not isinstance(p.get('robot_id'), str):
                raise ValueError('robot_id')
            robot = self.store.get('robots', p.get('robot_id'))
        except ValueError:
            raise CodedError('没有找到这个仿真小鸭', 'duck_robot_unknown') from None
        if not (robot.get('sim') or {}).get('duck'):
            raise CodedError('这不是仿真小鸭', 'duck_robot_unknown')
        if robot_scope.elsewhere(robot, p.get('project_id')):
            raise CodedError('该机器人不属于当前项目', 'duck_robot_unknown')
        return robot

    def _control(self, robot):
        return self.platform.sims.root / robot['id'] / 'duck'

    def _alive(self, robot):
        process = self.platform.sims.procs.get(robot['id'])
        return robot['sim'].get('state') == 'running' and process is not None and process.alive

    def _command(self, robot, command):
        if not self._alive(robot):
            raise CodedError('这个仿真小鸭没有在运行，请先在「我的机器人」里启动它', 'duck_not_running')
        control = self._control(robot)
        control.mkdir(parents=True, exist_ok=True)
        record = {'id': 'run-' + secrets.token_hex(8), **command}
        part = control / 'command.json.part'
        part.write_text(json.dumps(record, ensure_ascii=False), encoding='utf-8')
        os.replace(part, control / 'command.json')
        return record

    def perform(self, p):
        """Ask a simulated duck to perform one show from HOME (route ``sims/duck/perform``)."""
        robot = self._duck(p)
        show = BY_ID.get(p.get('show')) if isinstance(p.get('show'), str) else None
        if show is None or not show.get('package'):
            raise CodedError('请选择一个有技能模型包的节目', 'duck_show_missing')
        config = self._config(robot)
        if show['id'] not in config.get('shows', {}):
            raise CodedError('这个仿真小鸭是在这个技能模型加入之前添加的，请重新添加一个仿真小鸭', 'duck_show_missing')
        seed = p.get('seed')
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED):
            raise CodedError('起姿编号需要是 0 到 4294967295 之间的整数', 'duck_seed_invalid')
        record = self._command(robot, {'cmd': 'show', 'show': show['id'], **({'seed': seed} if seed is not None else {})})
        self.platform.audit('duck.show', {'robot_id': robot['id'], 'show': show['id'], 'run_id': record['id']}, robot.get('project_id'))
        return {'run_id': record['id'], 'robot_id': robot['id'], 'show': show['id']}

    def stage(self, p):
        """Issue #75: put a simulated duck on the stage one of its skill models was trained on (each skill has its own
        environment). The body loads it in the background; ``status`` says ``staging`` until it stands there."""
        robot = self._duck(p)
        show = p.get('show') if isinstance(p.get('show'), str) else ''
        if show not in self._config(robot).get('shows', {}):
            raise CodedError('这个仿真小鸭没有这个技能模型', 'duck_show_missing')
        return self._command(robot, {'cmd': 'stage', 'show': show})

    def stop(self, p):
        """End the show a simulated duck is performing (route ``sims/duck/stop``)."""
        robot = self._duck(p)
        self._command(robot, {'cmd': 'home' if p.get('home') else 'stop'})
        return self.status(p)

    def _config(self, robot):
        try:
            config = json.loads((self.platform.sims.root / robot['id'] / 'duck.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            config = {}
        return config if isinstance(config, dict) else {}

    def status(self, p):
        """What a simulated duck is doing (route ``sims/duck/status``); asking keeps its picture fresh."""
        robot = self._duck(p)
        view = self.platform.sims.root / robot['id'] / 'view'
        try:
            view.mkdir(parents=True, exist_ok=True)
            (view / 'watch').touch()
        except OSError:
            pass
        record = {}
        try:
            raw = (self._control(robot) / 'status.json').read_bytes()
            record = json.loads(raw) if len(raw) <= 100_000 else {}
        except (OSError, ValueError):
            pass
        record = record if isinstance(record, dict) else {}
        alive = self._alive(robot)
        if not alive and record.get('state') in ('starting', 'running'):
            # The body's process is gone (stopped, or it died): what it last wrote is not what is happening.
            record = {**record, 'state': 'error', 'error': '这个仿真小鸭的进程已经不在了，演出没有完成；请在「我的机器人」里重新启动它'}
        result = record.get('result') if isinstance(record.get('result'), dict) else None
        engine = record.get('engine') if isinstance(record.get('engine'), dict) else {}
        return {'robot_id': robot['id'], 'running': alive,
                'run_id': str(record.get('id') or '')[:40], 'show': str(record.get('show') or '')[:40],
                'state': str(record.get('state') or 'idle')[:20], 'phase': str(record.get('phase') or '')[:40],
                'progress': record.get('progress') if isinstance(record.get('progress'), (int, float)) else 0.0,
                'steps': record.get('steps') if isinstance(record.get('steps'), int) else 0,
                'result': None if result is None else {
                    'success': bool(result.get('success')), 'reason': str(result.get('reason') or '')[:80],
                    'steps': result.get('steps') if isinstance(result.get('steps'), int) else 0,
                    'duration_s': result.get('duration_s') if isinstance(result.get('duration_s'), (int, float)) else None,
                    'phases': [str(x)[:40] for x in result.get('phases', []) if isinstance(x, str)][:12]},
                'error': str(record.get('error') or record.get('fault') or '')[:500],
                'engine': str(engine.get('mujoco') or '')[:20],
                'stage': str(record.get('stage') or '')[:40], 'staging': str(record.get('staging') or '')[:40] if alive else '',
                'shows': sorted(self._config(robot).get('shows', {})),
                'skills': {str(k): {'skill': str(v.get('skill') or ''), 'version': str(v.get('version') or ''),
                                    'installed': v.get('installed') is True, 'error': str(v.get('error') or '')[:300]}
                           for k, v in (robot['sim'].get('skills') or {}).items() if isinstance(v, dict)},
                'has_frame': (view / 'frame.png').is_file()}

    # ------------------------------------------------------------ training
    def _snapshot(self):
        try:
            path = self._dir() / 'training.json'
            if path.stat().st_size > MAX_SNAPSHOT_BYTES:
                return None
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and isinstance(data.get('tasks'), list) else None

    def _refresh(self, url):
        live = None
        try:
            live = fetch_training(url)
        finally:
            with self.lock:
                self._training.update(busy=False, at=time.time(), live=live)

    def training(self, p=None):
        """The training progress (route ``duck/training``): from the dashboard on this computer when it
        answers, else the copy that came with the kit. Never waits for the dashboard."""
        url = dashboard_url()
        with self.lock:
            cached = dict(self._training)
            stale = time.time() - cached['at'] > TRAINING_FRESH_S
            if url and stale and not cached['busy']:
                self._training['busy'] = True
                threading.Thread(target=self._refresh, args=(url,), name='nx-duck-training', daemon=True).start()
        if cached['live']:
            return {'source': 'live', 'dashboard': url, 'checking': False, **cached['live']}
        snapshot = self._snapshot()
        checking = bool(url) and cached['at'] == 0.0
        if snapshot:
            return {'source': 'snapshot', 'dashboard': url, 'checking': checking, **snapshot}
        return {'source': 'none', 'dashboard': url, 'checking': checking, 'tasks': []}
