"""A driver package goes from the cloud library to a device and is verified there (issue #31, R1-10).

Both ends existed: the library keeps packages in the cloud (issue #33) and ``device_transfer``
puts a local file on a device, in chunks, resumable, verified by the device's own SHA-256
(issue #18). This module is the part between them and the result it leaves:

1. the material's record is read from the cloud and checked: it must be a driver package, marked
   usable (R1-13; see ``USABLE_STATUSES``), and not belong to another robot (issue #34);
2. the package is fetched to this computer and verified against the checksum the library
   recorded (``package_fetch``): a fetch that was cut goes on from where it stopped;
3. it is sent to ``~/neuxmind-pkg/library/<first 32 digits of the SHA-256>/<plain name>`` through
   the channel, which goes on from where it stopped too, and is placed there only after the
   device computed the same SHA-256;
4. what arrived is recorded (``package_deliveries``): which material, under which name, where on
   the device, its size and checksum. That record is what the repair flow is handed.

Nothing here unpacks, installs or runs a package, on this computer or on the device: the content
is untrusted (the cloud only recognised its type). Installing is the repair assistant's step,
after a person approved it, under the rules of issue #29.

Two ways in:

* ``PackageDelivery.deliver`` does all of it in the calling thread and returns the record. This
  is what the repair flow calls (issues #29 and #30 register it as a tool of the repair session).
* ``start`` / ``status`` / ``stop`` / ``list`` are the same thing as a step a person starts from
  a page: it runs in the background and is asked how far it is.

A name in the library may hold spaces, brackets or Chinese; the channel only writes names of
letters, digits and ``. _ + -``. ``device_name`` makes one and the record keeps both.

The copy on this computer lives in ``<workspace>/package-cache`` and is removed once the device
has verified the file. It is kept while a delivery is unfinished, so the next try does not fetch
again; what nobody touched for a week is swept.
"""
import hashlib
import re
import threading
import time
from pathlib import Path

from . import device_transfer, package_fetch
from . import robot_scope
from . import model_library
from . import device_transfer_target as target
from .cloud_account import CloudError
from .errors import CodedError
from .material_library import MATERIAL_KEYS, _material_id, _scalars
from .service import now, uid

KIND = 'package_deliveries'
# R1-13: only materials marked usable are delivered. A material gets there by being parsed and
# then confirmed by a person (issue #34, which named the state ``usable``; ``parsed`` is "待确认").
USABLE_STATUSES = ('usable',)
# Issue #34: a material belongs to the robot it was uploaded for (its ``device_id`` is the robot's
# id). One that belongs to another robot is not sent; one uploaded before (no owner) may go anywhere.
OWNER_KEYS = ('device_id', 'device_name')
NOT_PLAIN = '\x00'                      # stands for an owner that is not plain text: never a robot's id
PACKAGE = 'package'
DEVICE_DIR = '~/neuxmind-pkg/library'
CACHE_DIR = 'package-cache'
FILE_MODE = 0o644                       # a file to be installed later, not a program
FALLBACK_STEM = 'package'
MAX_STEM_CHARS = 80
# The suffixes of the package formats the library accepts (cloud/neuxmind_cloud/library_types.py); the first is written.
SUFFIXES = {'zip': ('.zip',), 'tar.gz': ('.tar.gz', '.tgz'), 'tar.xz': ('.tar.xz', '.txz'),
            'tar.bz2': ('.tar.bz2', '.tbz2'), 'tar': ('.tar',), 'deb': ('.deb',)}
LABELS = ('body_type', 'vendor', 'model', 'version', 'format')
STATUS_NAMES = {'parsing': '解析中', 'parsed': '待确认', 'failed': '没解析出来'}      # as the library page names them (#34)
KEEP_JOBS = 50
MAX_RUNNING = 4                         # deliveries a person can have running at once in a workspace
DIR_DIGITS = 32                         # of the SHA-256, for the directory on the device: two packages never share one
FILENAME_CHARS, LABEL_CHARS = 255, 64   # what is kept of the text a library user typed
JOIN_SECONDS = 30                       # what closing the app waits, in all, for running deliveries to stop
_OTHER = re.compile(r'[^A-Za-z0-9._+-]+')
_SUFFIX = re.compile(r'\.[A-Za-z0-9]{1,8}$')
_EDGES = '._+-'                         # not at either end of a name: no hidden file, nothing a command could take for an option
_ROOTS = target.check_roots(device_transfer.DEFAULT_ROOTS)


def usable(material):
    """True for a material the repair flow may use (R1-13). The one place that decides it."""
    status = material.get('status') if isinstance(material, dict) else None
    return isinstance(status, str) and status in USABLE_STATUSES


def device_name(filename, fmt):
    """A name for the device made of letters, digits and ``. _ + -`` from a library file name.

    The suffix is the one of the format the cloud recognised, not whatever the name ends in; every
    run of other characters becomes one underscore; nothing starts or ends with a dot, a dash, a
    plus or an underscore. A name of which nothing is left, or one the channel refuses (a place
    for credentials, a name it keeps for temporary files), becomes ``package`` plus the suffix."""
    filename = filename if isinstance(filename, str) else ''
    known = SUFFIXES.get(fmt) if isinstance(fmt, str) else None
    if known:
        suffix = known[0]
        ending = next((x for x in known if filename.lower().endswith(x)), '')
    else:                               # a format this version does not know: keep a plain suffix of the name
        found = _SUFFIX.search(filename)
        ending = found.group(0) if found else ''
        suffix = ending.lower()
    stem = filename[:len(filename) - len(ending)] if ending else filename
    stem = _OTHER.sub('_', stem).strip(_EDGES)[:MAX_STEM_CHARS].strip(_EDGES)
    if not any(char.isalnum() for char in stem):
        stem = FALLBACK_STEM
    try:
        target.check_remote_path('%s/%s/%s' % (DEVICE_DIR, '0' * 16, stem + suffix), _ROOTS)
    except target.TransferError:
        stem = FALLBACK_STEM
    return stem + suffix


def device_target(material):
    """Where a package goes on a device, as the channel takes it: ``~/neuxmind-pkg/library/<32 digits>/<name>``.
    The digits are the start of its SHA-256, so two packages whose names come out the same do not meet."""
    sha256 = material.get('sha256') if isinstance(material, dict) else None
    if not isinstance(sha256, str) or not package_fetch.SHA256.fullmatch(sha256):
        raise CodedError('资料记录里的校验值无效，无法确定设备上的位置', code='package_record_invalid')
    return '%s/%s/%s' % (DEVICE_DIR, sha256[:DIR_DIGITS], device_name(material.get('filename'), material.get('format')))


def failure(exc):
    """How a delivery that did not finish is told to a page: ``status`` ('failed' or 'stopped'),
    ``error``, ``code`` and whether starting it again goes on from where it stopped."""
    if isinstance(exc, device_transfer.TransferError):
        if exc.reason == 'cancelled':
            return {'status': 'stopped', 'error': None, 'code': None, 'resumable': True, 'note': str(exc)}
        if exc.reason in ('checksum', 'damaged'):
            return {'status': 'failed', 'error': str(exc), 'code': 'package_device_checksum_mismatch', 'resumable': False}
        if exc.reason == 'interrupted':               # the link: with or without something on the device to go on from
            return {'status': 'failed', 'error': str(exc), 'code': 'package_send_interrupted', 'resumable': True, 'reason': exc.reason}
        # Refused login or host key, no SFTP, a full disk, no checksum tool, a place taken: the channel's own reason says which.
        return {'status': 'failed', 'error': str(exc), 'code': 'package_send_failed', 'resumable': exc.resumable, 'reason': exc.reason}
    if isinstance(exc, package_fetch.FetchError):
        if exc.code == 'package_fetch_cancelled':
            return {'status': 'stopped', 'error': None, 'code': None, 'resumable': True, 'note': str(exc)}
        return {'status': 'failed', 'error': str(exc), 'code': exc.code, 'resumable': exc.resumable}
    if isinstance(exc, ValueError):
        code = getattr(exc, 'code', None)
        return {'status': 'failed', 'error': str(exc), 'code': code if isinstance(code, str) else None, 'resumable': False}
    return {'status': 'failed', 'error': '内部错误，这次传送没有完成；可以重新开始', 'code': 'desktop_internal_error', 'resumable': True}


def _consent(authorized):
    if authorized is not True:
        raise CodedError('把安装包写到设备上需要你的确认', code='package_delivery_not_authorized')


class LocalCopyChanged(RuntimeError):
    """The verified copy on this computer is not the file it was when its checksum was taken."""


class _Unchanged:
    """The channel's progress, passed on, after making sure the file it reads is still the copy that was
    verified against the library's checksum. The channel reports 'checking' once it has hashed the file
    and before it writes anything to the device; it also stops by itself if the file changes later."""

    def __init__(self, fetched, then):
        self.path, self.identity, self.then = fetched['path'], fetched['identity'], then

    def __call__(self, event):
        if event['stage'] == 'checking' and package_fetch.identity(self.path) != self.identity:
            raise LocalCopyChanged('这台电脑上核对过的安装包在送出之前被改动或替换了，没有送往设备；它已删除，重新开始会再从云平台取一次')
        self.then(event)


class _Progress:
    """One shape for what both halves report: ``phase`` ('checking', 'fetching', 'sending',
    'verifying', 'done'), ``done`` and ``total`` bytes of that half, and ``step`` (the half's own word)."""

    def __init__(self, on_progress):
        self.on_progress = on_progress

    def __call__(self, phase, done, total, **more):
        if self.on_progress is not None:
            self.on_progress({'phase': phase, 'done': done, 'total': total, **more})

    def fetching(self, event):
        self('fetching', event['received'], event['total'], step=event['stage'], attempt=event['attempt'])

    def sending(self, event):
        if event['stage'] != 'done':                  # ours comes after the record is written
            self('verifying' if event['stage'] == 'verifying' else 'sending', event['sent'], event['total'],
                 step=event['stage'], attempt=event['attempt'])


class PackageDelivery:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store
        self.cache = Path(platform.store.root) / CACHE_DIR
        self.lock = threading.Lock()
        self.active = set()             # checksums of the packages being fetched or sent right now
        self.jobs, self.threads, self.stops = {}, {}, {}      # deliveries a person started; kept in memory only
        self.closing = False
        self.tuning = {}                # {'fetch': {...}, 'transfer': {...}}: tests shorten the waits here
        package_fetch.sweep(self.cache)

    # --- what is asked first ------------------------------------------------------------------

    def _target(self, robot_id, project_id):
        robot = self.platform.same_project('robots', robot_id, project_id)
        if not robot.get('connection_id'):
            raise ValueError('这台机器人没有 SSH 连接；请先通过 SSH 接入')
        return robot, self.platform.same_project('device_connections', robot['connection_id'], project_id)

    def material(self, material_id):
        """The record of a material as the cloud has it now."""
        material_id = _material_id(material_id)
        answer = self.platform.library._call('GET', '/library/materials/' + material_id)
        view = _scalars(answer, MATERIAL_KEYS + OWNER_KEYS)
        for key, value in view.items():
            if isinstance(value, str) and key not in ('id', 'sha256', 'device_id'):
                view[key] = target.shown(value, FILENAME_CHARS if key == 'filename' else LABEL_CHARS)
        owner = answer.get('device_id')
        if owner not in (None, ''):         # read from the answer itself: one that is not plain text is nobody's id
            view['device_id'] = owner if isinstance(owner, str) else NOT_PLAIN
        if view.get('id') != material_id:
            raise CodedError('云平台答复的不是所问的那份资料，没有继续', code='package_record_invalid')
        return view

    @staticmethod
    def check(material, robot):
        """Refuse what may not go to this robot: anything but a driver package, a package that is not
        marked usable (R1-13), and one that belongs to another robot (issue #34)."""
        if material.get('kind') != PACKAGE:
            raise CodedError('只有驱动安装包可以送到设备；这份资料不是安装包', code='package_not_deliverable')
        if not usable(material):
            status = material.get('status')
            name = STATUS_NAMES.get(status) or (target.shown(status, 40) if isinstance(status, str) and status else '未知')
            raise CodedError('这份安装包还没有标为“可用”（现在是“%s”），不能送到设备：资料要先解析、再由人确认' % name,
                             code='package_not_usable')
        model_library.check_delivery(material, robot)     # issue #51: its brand and model's, or still bound to it alone
        package_fetch._record(material)

    def _source(self):
        cloud = self.platform.cloud
        return {'base_url': cloud.settings().get('base_url'), 'token': cloud._token()}

    # --- the delivery -------------------------------------------------------------------------

    def deliver(self, robot_id, project_id, material_id, *, authorized, on_progress=None, stop=None, fetch=None, transfer=None):
        """Fetch the package ``material_id`` from the library, send it to the robot and verify it there.

        ``authorized`` must be ``True``: a person agreed to this file being written to this robot
        (the page's consent, or the repair session's approval of the step). Nothing is asked of
        the cloud or the device without it.

        Runs in the calling thread and may take long on a weak link; ``stop`` (a ``threading.Event``)
        ends it, and calling again goes on from where either half stopped. ``on_progress(event)``
        gets ``phase``, ``done`` and ``total``. ``fetch`` and ``transfer`` pass options on to
        ``package_fetch.fetch`` and ``device_transfer.send_to_robot``.

        Returns the delivery record: ``material_id``, ``filename`` (in the library), ``device_name``,
        ``device_path`` (where the file is on the device), ``bytes``, ``sha256``, ``verified`` (always
        True: anything else raises), ``already_present``, ``labels``, ``delivered_at``.

        Raises ``CodedError`` (not a package, not usable, not signed in), ``CloudError``,
        ``package_fetch.FetchError`` or ``device_transfer.TransferError``; the last two say whether
        calling again continues (``resumable``)."""
        _consent(authorized)
        material_id = _material_id(material_id)       # before anything is asked anywhere
        robot, connection = self._target(robot_id, project_id)
        tell = _Progress(on_progress)
        tell('checking', 0, 0)
        material = self.material(material_id)
        self.check(material, robot)
        return self._deliver(robot, connection, material, tell, stop, fetch or {}, transfer or {})

    def _deliver(self, robot, connection, material, tell, stop, fetch, transfer):
        sha256, size, place = material['sha256'], material['size'], device_target(material)
        with self.lock:
            if sha256 in self.active:
                raise CodedError('这个安装包正在取或正在送往一台设备，等它结束后再试', code='package_busy')
            self.active.add(sha256)
        try:
            package_fetch.sweep(self.cache, keep=self._in_use())
            try:
                fetched = package_fetch.fetch(self._source(), material, self.cache, on_progress=tell.fetching, stop=stop, **fetch)
            except CloudError as exc:
                if exc.code == 'unauthorized':
                    self.platform.cloud._drop_token()         # as every other call to the cloud does
                raise
            sent = device_transfer.send_to_robot(self.platform, connection['id'], fetched['path'], place, mode=FILE_MODE,
                                                 on_progress=_Unchanged(fetched, tell.sending), stop=stop, **transfer)
            # The channel hashed the file it sent and the device computed the same value. That value
            # must be the library's: anything else is not the package, wherever it now lies.
            if sent.get('verified') is not True or sent.get('sha256') != sha256 or sent.get('bytes') != size:
                raise CodedError('设备上核对过的文件不是资料库记录的那个安装包，没有记为已送达；请不要使用设备上的这个文件：%s'
                                 % sent.get('path'), code='package_delivery_unverified')
            record = self._remember(robot, connection, material, place, fetched, sent)
            package_fetch.forget(self.cache, sha256)          # verified on the device: the copy here is no longer needed
        except device_transfer.ProgressHandlerError as exc:
            # The channel wraps what its progress handler raised; ours says the local copy changed.
            if not isinstance(exc.__cause__, LocalCopyChanged):
                raise
            package_fetch.forget(self.cache, sha256)          # the next try fetches it again
            raise CodedError(str(exc.__cause__), code='package_local_copy_changed') from None
        finally:
            with self.lock:
                self.active.discard(sha256)
        tell('done', size, size)
        return record

    def _remember(self, robot, connection, material, place, fetched, sent):
        key = hashlib.sha256(('%s\n%s' % (robot['id'], material['sha256'])).encode()).hexdigest()[:20]
        record = {'id': 'package-delivery-' + key, 'project_id': robot['project_id'], 'robot_id': robot['id'],
                  'connection_id': connection['id'], 'host_fingerprint': connection.get('fingerprint'),
                  'material_id': material['id'], 'filename': material.get('filename', ''),
                  'device_name': place.rsplit('/', 1)[1], 'device_target': place, 'device_path': sent['path'],
                  'bytes': sent['bytes'], 'sha256': sent['sha256'], 'verified': True,
                  'already_present': bool(sent.get('already_present')),
                  'labels': {key: material[key] for key in LABELS if isinstance(material.get(key), str)},
                  'fetch': {key: fetched[key] for key in ('cached', 'resumed_from', 'received_bytes', 'attempts')},
                  'send': {key: sent.get(key) for key in ('resumed_from', 'sent_bytes', 'encoding', 'attempts')},
                  'delivered_at': now()}
        # The closing check and the writes are one critical section, and close() sets ``closing`` under the
        # same lock: a write that began finishes before the store is closed, and none begins after.
        with self.lock:
            if self.closing:
                raise device_transfer.TransferError('verify', 'cancelled', '应用正在退出：文件已经送到设备并核对过（%s），这次没有记下；'
                                                    '重新开始会发现它已经在设备上，不会重传' % sent['path'], resumable=True)
            with self.store.atomic():
                try:
                    self.store.get('robots', robot['id'])
                except ValueError:
                    raise ValueError('这台机器人在传送期间被删除了；文件已经在设备上（%s），没有留下记录' % sent['path']) from None
                self.store.put(KIND, record)
                self.platform.audit('library.package_delivered', {key: record[key] for key in (
                    'robot_id', 'material_id', 'filename', 'device_path', 'bytes', 'sha256')}, robot['project_id'])
        return record

    def delivered(self, robot_id):
        """The packages that were put on this robot and verified there, newest first: what the
        repair flow is handed. Only deliveries to the machine the robot's saved connection leads to
        now are listed; a record says what the device computed when the file arrived, not that the
        file is still there."""
        try:
            robot = self.store.get('robots', robot_id)
            connection = self.store.get('device_connections', robot.get('connection_id'))
        except ValueError:
            return []
        same = [record for record in self.store.list(KIND) if record.get('robot_id') == robot['id']
                and record.get('connection_id') == connection['id']
                and record.get('host_fingerprint') == connection.get('fingerprint')]
        return sorted(same, key=lambda record: record.get('delivered_at', ''), reverse=True)

    def _in_use(self):
        with self.lock:
            return set(self.active)

    # --- the step a person starts -------------------------------------------------------------

    @staticmethod
    def _view(job):
        return dict(job)

    def start(self, p):
        """Start a delivery in the background. What can be refused at once is refused here."""
        if self.closing:
            raise ValueError('应用正在退出')
        _consent(p.get('modify_authorized'))
        material_id = _material_id(p.get('material_id'))
        robot, connection = self._target(p.get('robot_id'), p.get('project_id'))
        material = self.material(material_id)
        self.check(material, robot)
        job = {'id': uid('package-delivery-job'), 'project_id': robot['project_id'], 'robot_id': robot['id'],
               'material_id': material_id, 'filename': material.get('filename', ''), 'status': 'running', 'phase': 'checking',
               'done': 0, 'total': material['size'], 'error': None, 'code': None, 'resumable': None, 'note': None,
               'delivery': None, 'started_at': now(), 'finished_at': None}
        stop = threading.Event()
        with self.lock:
            if self.closing:
                raise ValueError('应用正在退出')
            if any(other['status'] == 'running' and (other['robot_id'], other['material_id']) == (robot['id'], material_id)
                   for other in self.jobs.values()):
                raise CodedError('这个安装包正在送往这台设备，等它结束后再试', code='package_busy')
            if sum(other['status'] == 'running' for other in self.jobs.values()) >= MAX_RUNNING:
                raise CodedError('同时进行的传送已经有 %d 个，等其中一个结束后再开始' % MAX_RUNNING, code='package_busy')
            for old in [key for key, other in self.jobs.items() if other['status'] != 'running'][:-KEEP_JOBS]:
                del self.jobs[old]
            thread = threading.Thread(target=self._run, args=(job, robot, connection, material, stop), daemon=True)
            try:
                thread.start()
            except RuntimeError:
                raise CodedError('这台电脑现在开不了新的后台任务，传送没有开始；稍后再试', code='package_busy') from None
            self.jobs[job['id']], self.stops[job['id']], self.threads[job['id']] = job, stop, thread
            return self._view(job)

    def _run(self, job, robot, connection, material, stop):
        with self.lock:
            pass                        # start() registers this job under the lock it holds while starting the thread
        def progress(event):
            with self.lock:
                job.update(phase=event['phase'], done=event['done'], total=event['total'] or job['total'])
        outcome = {'status': 'failed', 'error': '内部错误，这次传送没有完成；可以重新开始', 'code': 'desktop_internal_error', 'resumable': True}
        try:
            record = self._deliver(robot, connection, material, _Progress(progress), stop,
                                   self.tuning.get('fetch', {}), self.tuning.get('transfer', {}))
            outcome = {'status': 'delivered', 'phase': 'done', 'delivery': record}
        except Exception as exc:
            outcome = failure(exc)
        finally:                        # whatever ends the thread, the job does not stay "running"
            with self.lock:
                job.update(outcome, finished_at=now())
                self.threads.pop(job['id'], None)
                self.stops.pop(job['id'], None)

    def _job(self, p):
        job_id = p.get('job_id')
        with self.lock:
            job = self.jobs.get(job_id) if isinstance(job_id, str) else None
            if job is None or robot_scope.elsewhere(job, p.get('project_id'), required=True):      # issue #42: kept with the robot, whichever task asks
                raise ValueError('找不到这次传送；应用重启后正在进行的传送不会保留，可以重新开始，已经传到的部分会接着用')
            return job

    def status(self, p):
        job = self._job(p)
        with self.lock:
            return self._view(job)

    def stop(self, p):
        """Ask a running delivery to stop. What arrived on either side is kept for the next start."""
        job = self._job(p)
        with self.lock:
            stop = self.stops.get(job['id'])
            if stop is not None:
                stop.set()
            return self._view(job)

    def list(self, p):
        """What was delivered to a robot, and the deliveries started for it since the app was opened."""
        robot = self.platform.same_project('robots', p.get('robot_id'), p.get('project_id'))
        with self.lock:
            jobs = [self._view(job) for job in self.jobs.values() if job['robot_id'] == robot['id']]
        return {'deliveries': self.delivered(robot['id']),
                'jobs': sorted(jobs, key=lambda job: job['started_at'], reverse=True)}

    def close(self):
        """Stop what is running, before the store is closed. What arrived stays for the next start."""
        with self.lock:
            self.closing = True
            threads = list(self.threads.values())
            for stop in self.stops.values():
                stop.set()
        deadline = time.monotonic() + JOIN_SECONDS
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))
