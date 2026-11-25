"""A robot's programs and scene data kept in the cloud (issue #43, docs/CLOUD_SKILLS.md section 2).

Some robots walk by programs on their own computer, and those programs with the scene data they use
(place marks, reference maps) are one directory. This module moves such a directory both ways:

* **收进云端** (``collect``): over the robot's saved SSH connection a fixed script packs the
  directory into one ``.tar.gz`` (``robot_bundle_device.PACK_SCRIPT``), the archive is read back
  over SFTP and compared with the checksum the robot computed, then it goes to the material library
  as a ``bundle`` that belongs to this robot, in chunks, like every other material.
* **装到机器人** (``install``): the bundle is fetched from the library and verified
  (``package_fetch``), sent to ``~/neuxmind-bundles/<name>/<16 digits>.tar.gz`` through the upload
  channel of issue #18, unpacked next to it by a fixed script that refuses anything that would
  leave that directory, and ``~/neuxmind-bundles/<name>/current`` is pointed at the result.

Neither touches the directory the programs run from, and neither changes the adapter's
configuration: to use what was installed a person points ``ros2-config.json`` at ``current``.
Both need a person's consent (``authorized=True``); nothing is asked of the cloud or the robot
without it. Nothing a person typed becomes part of a command (see ``robot_bundle_device``).

A person starts either from the robot's page; it runs in the background and is asked how far it
is (``status``). What was done is kept in the audit log (``bundle.collected``,
``bundle.installed``), which is also where ``overview`` reads the history from: no record of its
own is kept per robot.
"""
import os
import re
import secrets
import stat
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from . import device_access, device_transfer, hardware_init, package_fetch
from . import robot_scope
from . import robot_bundle_device as on_device
from .cloud_account import CloudError
from .device_transfer_target import shown
from .errors import CodedError
from .material_library import COMPLETE_TIMEOUT_S, MATERIAL_KEYS, _scalars
from .service import now, uid

KIND = 'bundle'
CACHE_DIR = 'bundle-cache'
ROOTS = (on_device.STORE,)
SUFFIXES = ('.tar.gz', '.tgz')
VENDOR = '机器人上的程序'                 # the library wants a vendor label; a bundle has none
PACK_SECONDS, EXTRACT_SECONDS = 900, 900
STALL_SECONDS = 120
READ_BYTES = 256 * 1024
RETRY_WAITS = (1, 2, 5, 10, 20, 30)
CHUNK_TRIES = 3
OUTPUT_LIMIT = 16384
KEEP_JOBS, HISTORY = 30, 20
JOIN_SECONDS = 30
ACTIONS = {'bundle.collected': 'collect', 'bundle.installed': 'install'}
CONFIG_HINT = '没有改动机器人上原来的目录，也没有改适配器的配置；要让管理层用这一份，请把 ros2-config.json 里的路径改到 current'
_PACK_REFUSALS = {
    'missing': ('机器人上没有这个目录，或者它不是目录', 'bundle_directory_missing'),
    'unreadable': ('登录账号进不了这个目录（没有权限）', 'bundle_directory_missing'),
    'toowide': ('这个目录是登录账号的主目录或它的上级，范围太大，不能整个收进云端；请填写程序所在的那个目录', 'bundle_directory_refused'),
    'inside': ('这个目录在 ~/neuxmind-bundles 里，是从云端装下来的内容，不能再收进云端', 'bundle_directory_refused'),
    'refused': ('机器人没有接受这个目录的写法', 'bundle_directory_refused'),
    'sensitive': ('这个目录实际指向存放登录凭据的位置（例如 .ssh、.git、.config），不能收进云端', 'bundle_directory_refused'),
    'toolarge': ('这个目录打包后超过 %d MiB 的上限，没有收。运行记录（runs/）已经不算在内；'
                 '请把大文件移出这个目录后再试' % (on_device.MAX_ARCHIVE_BYTES // 2 ** 20), 'bundle_too_large'),
    'nohome': ('登录账号在机器人上没有主目录，无法打包', 'bundle_pack_failed'),
    'moved': ('机器人上的 ~/neuxmind-bundles 要经过符号链接才能到达，没有使用它', 'bundle_pack_failed'),
    'nohash': ('机器人上没有 sha256sum、shasum 或 openssl，无法核对打好的包，所以没有收', 'bundle_pack_failed'),
}
_EXTRACT_REFUSALS = {
    'occupied': ('机器人上要用的位置被别的东西占着，没有覆盖它', 'bundle_occupied'),
    'damaged': ('这个包解不开（不是完整的 tar.gz），没有解到机器人上', 'bundle_unsafe'),
    'toolarge': ('这个包解开后超过 %d MiB，没有解到机器人上' % (on_device.MAX_UNPACKED_BYTES // 2 ** 20), 'bundle_unsafe'),
    'missing': ('送到机器人上的包不见了，没有解开；请重新开始', 'bundle_extract_failed'),
    'moved': ('机器人上这个包的目录要经过符号链接才能到达，没有使用它', 'bundle_extract_failed'),
    'nohome': ('登录账号在机器人上没有主目录，无法解开', 'bundle_extract_failed'),
    'unplaced': ('包已经解开，但没能把 current 指向它', 'bundle_extract_failed'),
}
_UNSAFE = {'path': '里面有写到目录之外的路径（绝对路径或 ..）', 'kind': '里面有设备文件、管道或硬链接',
           'link': '里面有指向目录之外的链接'}


_FATAL_LOGIN = ('auth', 'key', 'fingerprint', 'forbidden')       # asking again changes nothing, and may lock the account
_STAMPED = re.compile(r'(.+)-\d{8}-\d{6}')                       # "<name>-<date>-<time>", as ``_fields`` names a file


_ASIDE = re.compile(r'[0-9a-f]{16}\.changed-[0-9]{8}-[0-9]{6}(?:\.[0-9]+)?')    # the name ``EXTRACT_SCRIPT`` gives a set-aside directory


class _Retry(Exception):
    """This connection got no further; another one may."""


def consent(authorized):
    if authorized is not True:
        raise CodedError('读取或写入机器人上的目录需要你的确认', 'bundle_not_authorized')


def _stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')


def bundle_name(material):
    """The name a bundle gets on a device: the label it was collected under, else its file name
    without the suffix and the date."""
    label = material.get('model') if isinstance(material.get('model'), str) else ''
    if on_device.NAME.fullmatch(label) and not on_device.CHANNEL_REFUSES.search(label):
        return label
    filename = material.get('filename') if isinstance(material.get('filename'), str) else ''
    stem = next((filename[:-len(s)] for s in SUFFIXES if filename.lower().endswith(s)), filename)
    stamped = _STAMPED.fullmatch(stem)
    return on_device.plain_name(stamped.group(1) if stamped else stem)


def failure(exc):
    """How a job that did not finish is told to a page: status, error, code."""
    if isinstance(exc, CodedError) and exc.code == 'bundle_cancelled':
        return {'status': 'stopped', 'error': None, 'code': None, 'note': str(exc)}
    if isinstance(exc, device_transfer.TransferError):
        if exc.reason == 'cancelled':
            return {'status': 'stopped', 'error': None, 'code': None, 'note': str(exc)}
        return {'status': 'failed', 'error': str(exc), 'code': 'bundle_send_failed'}
    if isinstance(exc, package_fetch.FetchError) and exc.code == 'package_fetch_cancelled':
        return {'status': 'stopped', 'error': None, 'code': None, 'note': str(exc)}
    if isinstance(exc, ValueError):
        code = getattr(exc, 'code', None)
        return {'status': 'failed', 'error': str(exc), 'code': code if isinstance(code, str) else None}
    return {'status': 'failed', 'error': '内部错误，这一步没有完成；可以重新开始', 'code': 'desktop_internal_error'}


class RobotBundles:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store
        self.cache = Path(platform.store.root) / CACHE_DIR
        self.lock = threading.Lock()
        self.jobs, self.threads, self.stops = {}, {}, {}        # what a person started; kept in memory only
        self.closing = False
        self.tuning = {}                # {'fetch': {...}, 'transfer': {...}, 'retry_waits': (...)}: tests shorten the waits
        package_fetch.sweep(self.cache)

    # --- what is asked first ------------------------------------------------------------------

    def _target(self, robot_id, project_id):
        robot = self.platform.same_project('robots', robot_id, project_id)
        if not robot.get('connection_id'):
            raise CodedError('这台机器人没有保存 SSH 连接；程序和场景数据要经 SSH 读写，请先通过 SSH 接入', 'bundle_no_ssh')
        return robot, self.platform.same_project('device_connections', robot['connection_id'], project_id)

    def _check_stop(self, stop):
        if stop is not None and stop.is_set():
            raise CodedError('已按你的要求停止；机器人上原来的目录没有动', 'bundle_cancelled')

    def _waits(self):
        return self.tuning.get('retry_waits', RETRY_WAITS)

    # --- the robot side -----------------------------------------------------------------------

    def _connect(self, connection_id, again=False):
        """A new SSH session to the robot. With ``again`` (a step that goes on from where it stopped) a
        robot that does not answer right now is something to retry; a refused login never is."""
        try:
            return device_access.SSHDevice(*self.platform.ssh_edit.saved_login(connection_id))
        except device_access.ConnectError as exc:
            if again and exc.reason not in _FATAL_LOGIN:
                raise _Retry(str(exc)) from None
            raise CodedError('连不上这台机器人：%s' % exc, 'bundle_device_unreachable') from None

    def _script(self, device, kind, seconds, stop, *arguments):
        """The answer of one of the two scripts, run on the robot by ``sh -s``. Nothing else is run."""
        import paramiko
        guard = device_transfer._Guard(device, stop, seconds + 15)
        try:
            device.stop_event = stop
            result = hardware_init.run_command(device, on_device.command(*arguments), seconds, stdin=on_device.SCRIPTS[kind],
                                               keep='head', limit=OUTPUT_LIMIT, redact=False)
        except (OSError, EOFError, paramiko.SSHException, AttributeError):
            self._check_stop(stop)
            raise CodedError('和机器人的连接断了，这一步没有完成；可以重新开始', 'bundle_device_unreachable') from None
        finally:
            guard.finish()
        self._check_stop(stop)
        if result['timed_out']:
            raise CodedError('机器人在 %d 秒内没有做完这一步；可以重新开始' % seconds, 'bundle_device_unreachable')
        return result['stdout']

    def _pack(self, device, directory, token, stop):
        text = self._script(device, 'pack', PACK_SECONDS, stop, on_device.hex_argument(directory), token,
                            on_device.MAX_ARCHIVE_BYTES)
        word, checksum, note = on_device.verdict(text)
        if word == 'packed' and checksum:
            found = on_device.fields(text)
            try:
                return {'sha256': checksum, 'size': int(found['size']), 'files': int(found.get('files') or 0),
                        'changed': found.get('changed') == 'yes', 'outside_links': int(found.get('outside') or 0),
                        'skipped': self._left_out(device, text, found, 'skipped', 'skip'),
                        'guarded': self._left_out(device, text, found, 'guarded', 'guard')}
            except (KeyError, ValueError):
                raise CodedError('机器人打包后的答复看不懂，没有继续', 'bundle_pack_failed') from None
        if word == 'special':
            raise CodedError('这个目录里有设备文件、管道或套接字（%s），不能收进云端' % (shown(device.clean(note), 120) or '位置不明'),
                             'bundle_directory_refused')
        if word in _PACK_REFUSALS:
            raise CodedError(*_PACK_REFUSALS[word])
        detail = shown(device.clean(note), 200) if word == 'failed' else ''
        raise CodedError('机器人没能把这个目录打成包%s' % ('：' + detail if detail else '（没有答复）'), 'bundle_pack_failed')

    @staticmethod
    def _left_out(device, text, found, count_key, name_key):
        """How many paths stayed behind for one reason, and the first of their names, cleaned for showing."""
        names = [shown(device.clean(name), 200) for name in on_device.listed(text, name_key)[:on_device.LISTED]]
        return {'count': max(int(found.get(count_key) or 0), len(names)), 'names': names}

    def _read_back(self, connection_id, token, packed, local, tell, stop):
        """Read the packed archive from the robot into ``local``. A connection that breaks costs what
        was in flight: the next one goes on from what is here. Returns after the checksum was compared."""
        import paramiko
        waits, failures, have = self._waits(), 0, 0
        while have < packed['size']:
            self._check_stop(stop)
            before = have
            try:
                have = self._read_some(connection_id, token, packed, local, tell, stop)
                continue
            except (_Retry, OSError, EOFError, paramiko.SSHException) as exc:
                problem = shown(str(exc)) or type(exc).__name__
                have = os.path.getsize(local) if os.path.exists(local) else 0
            self._check_stop(stop)
            failures = 1 if have > before else failures + 1
            if failures > len(waits):
                raise CodedError('从机器人取回打好的包时连接中断，连续 %d 次都没有新的进展（最后一次：%s）；可以重新开始'
                                 % (failures, problem), 'bundle_fetch_interrupted')
            (stop.wait if stop is not None else time.sleep)(waits[failures - 1])
        got = package_fetch._digest(local)
        if got != packed['sha256']:
            raise CodedError('取回的包和机器人上算出的校验值不一致，没有送往云端：机器人算出 %s，取回的是 %s。请重新开始'
                             % (packed['sha256'][:16], (got or '')[:16]), 'bundle_checksum_mismatch')

    def _read_some(self, connection_id, token, packed, local, tell, stop):
        device = self._connect(connection_id, again=True)
        guard = device_transfer._Guard(device, stop, self.tuning.get('stall_seconds', STALL_SECONDS))
        try:
            sftp = device_transfer._sftp(device)
            remote = '%s/%s/%s/%s.tar.gz' % (sftp.normalize('.'), on_device.STORE[2:], on_device.OUTGOING, token)
            info = sftp.lstat(remote)
            if not stat.S_ISREG(info.st_mode or 0) or info.st_size != packed['size']:
                raise CodedError('机器人上打好的包变了（大小和打包时不同），没有取回；请重新开始', 'bundle_checksum_mismatch')
            with open(os.open(local, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600), 'ab') as out:
                have = os.fstat(out.fileno()).st_size
                with sftp.open(remote, 'rb') as source:
                    source.seek(have)
                    while have < packed['size']:
                        data = source.read(min(READ_BYTES, packed['size'] - have))
                        if not data:
                            raise _Retry('机器人在文件传完之前不再答复')
                        out.write(data)
                        out.flush()
                        have += len(data)
                        guard.confirm()
                        tell('fetching', have, packed['size'])
                        self._check_stop(stop)
            return have
        finally:
            guard.finish()
            device_transfer._close(device)

    def _forget_on_robot(self, connection_id, token):
        """Remove the packed archive from the robot. Best effort: one left behind is swept by the next pack."""
        import paramiko
        try:
            device = self._connect(connection_id)
        except CodedError:
            return False
        try:
            sftp = device_transfer._sftp(device)
            sftp.remove('%s/%s/%s/%s.tar.gz' % (sftp.normalize('.'), on_device.STORE[2:], on_device.OUTGOING, token))
            return True
        except (OSError, EOFError, paramiko.SSHException, device_transfer.TransferError):
            return False
        finally:
            device_transfer._close(device)

    def _extract(self, connection_id, name, digits, stop):
        device = self._connect(connection_id)
        try:
            text = self._script(device, 'extract', EXTRACT_SECONDS, stop, name, digits, on_device.MAX_UNPACKED_BYTES)
            word, _, note = on_device.verdict(text)
            note = shown(device.clean(note), 200)
        finally:
            device_transfer._close(device)
        if word == 'placed':
            found = on_device.fields(text)
            aside = found.get('aside', '')
            return {'already': found.get('already') == 'yes', 'files': int(found['files']) if found.get('files', '').isdigit() else None,
                    'aside': aside if _ASIDE.fullmatch(aside) else None}
        if word == 'unsafe':
            reason = _UNSAFE.get(note.split('\n')[0].split(' ')[0], '里面有不能解到机器人上的内容')
            raise CodedError('这个包没有解到机器人上：%s。机器人上没有留下解开的内容' % reason, 'bundle_unsafe')
        if word in _EXTRACT_REFUSALS:
            message, code = _EXTRACT_REFUSALS[word]
            raise CodedError(message + ('（%s）' % note if word == 'occupied' and note else ''), code)
        raise CodedError('机器人没能解开这个包%s' % ('：' + note if word == 'failed' and note else '（没有答复）'), 'bundle_extract_failed')

    # --- the cloud side -----------------------------------------------------------------------

    def _fields(self, robot, directory, name, packed):
        family = robot.get('family') if isinstance(robot.get('family'), str) else ''
        stamp = _stamp()
        return {'filename': '%s-%s.tar.gz' % (name, stamp), 'size': packed['size'], 'sha256': packed['sha256'], 'kind': KIND,
                'body_type': family if family and all(c.isalnum() or c in '._-' for c in family) and family == family.lower() else 'custom',
                'vendor': VENDOR, 'model': name, 'version': stamp,
                'description': '来自“%s”的 %s' % (shown(robot.get('name') or '', 80), directory),
                'device_id': robot['id'], 'device_name': shown(robot.get('name') or '', 200) or robot['id']}     # the cloud wants both

    def _same_in_library(self, robot, sha256):
        """The bundle of this robot with this content the library already holds, or None."""
        listed = self.platform.library.list({'kind': KIND, 'device_id': robot['id'], 'limit': 200})
        return next((m for m in listed['materials'] if m.get('sha256') == sha256), None)

    def _upload(self, robot, fields, local, tell, stop):
        library = self.platform.library
        cancelled = False
        try:
            upload = library._upload(library._call('POST', '/library/uploads', fields))
        except CloudError as exc:
            # The same file was once started as a driver package and never finished. An unfinished upload
            # is only ever visible to the account that started it, so it is this user's own: it is given
            # up, and this one starts as what it is.
            stale = exc.body.get('upload_id') if exc.code == 'upload_kind_conflict' and isinstance(exc.body, dict) else None
            if not isinstance(stale, str):
                raise
            library.cancel({'upload_id': stale})
            cancelled = True
            upload = library._upload(library._call('POST', '/library/uploads', fields))
        chunk, total = upload.get('chunk_bytes'), upload.get('chunk_count')
        if not isinstance(chunk, int) or not isinstance(total, int) or chunk < 1 or total < 1:
            raise CloudError('云平台没有说明怎样分片，没有上传')
        done = set(upload['received'])
        with open(local, 'rb') as source:
            for index in range(total):
                self._check_stop(stop)
                if index not in done:
                    source.seek(index * chunk)
                    self._send_chunk(upload['id'], index, source.read(chunk), stop)
                tell('uploading', min(fields['size'], (index + 1) * chunk), fields['size'])
        answer = library._call('POST', '/library/uploads/%s/complete' % upload['id'], {}, timeout=COMPLETE_TIMEOUT_S)
        return _scalars(answer, MATERIAL_KEYS), cancelled

    def _send_chunk(self, upload_id, index, data, stop):
        for attempt in range(1, CHUNK_TRIES + 1):
            try:
                return self.platform.library.chunk(upload_id, index, data)
            except CloudError as exc:
                again = exc.code == 'cloud_unreachable' or (exc.status or 0) >= 500
                if not again or attempt == CHUNK_TRIES:
                    raise
                (stop.wait if stop is not None else time.sleep)(self._waits()[min(attempt, len(self._waits())) - 1])
                self._check_stop(stop)

    # --- 收进云端 ------------------------------------------------------------------------------

    def collect(self, robot_id, project_id, directory, *, authorized, name=None, on_progress=None, stop=None):
        """Pack ``directory`` on the robot, read the archive back and keep it in the library as a
        bundle of this robot. Runs in the calling thread; returns what was kept.

        Raises ``CodedError`` (no consent, a directory that may not be packed, the robot, the
        checksum) or ``CloudError``. The robot's directory is only read."""
        consent(authorized)
        directory = self._directory(directory)
        robot, connection = self._target(robot_id, project_id)
        self.platform.library._require_login()
        return self._collect(robot, connection, directory, self._name(name, directory), _Tell(on_progress), stop)

    @staticmethod
    def _name(name, directory):
        """The bundle's name on a device: the one given, else the directory's last part. Either becomes a
        plain name; one that cannot gets digits of the whole directory, so two never share a place."""
        wanted = name.strip() if isinstance(name, str) and name.strip() else directory.rsplit('/', 1)[1]
        return on_device.plain_name(wanted, seed=directory)

    @staticmethod
    def _directory(value):
        try:
            return on_device.directory(value)
        except ValueError as exc:
            raise CodedError(str(exc), 'bundle_directory_invalid') from None

    def _collect(self, robot, connection, directory, name, tell, stop):
        token = secrets.token_hex(8)
        local = self._local(token)
        tell('packing', 0, 0)
        device = self._connect(connection['id'])
        cancelled = False
        try:                            # whatever ends it, a stop while packing too: the archive does not stay on the robot
            try:
                packed = self._pack(device, directory, token, stop)
            finally:
                device_transfer._close(device)
            # The same files pack to the same bytes: a copy the library already holds is not stored again.
            material = self._same_in_library(robot, packed['sha256'])
            already = material is not None
            if not already:
                self._read_back(connection['id'], token, packed, local, tell, stop)
                material, cancelled = self._upload(robot, self._fields(robot, directory, name, packed), local, tell, stop)
        finally:
            self._forget_on_robot(connection['id'], token)
            package_fetch._remove(local)
        result = {'robot_id': robot['id'], 'directory': directory, 'name': name, 'material_id': material.get('id'),
                  'filename': material.get('filename', ''), 'bytes': packed['size'], 'sha256': packed['sha256'],
                  'files': packed['files'], 'changed_while_packing': packed['changed'], 'outside_links': packed['outside_links'],
                  'skipped': packed['skipped'], 'guarded': packed['guarded'], 'already_in_library': already,
                  'cancelled_other_upload': cancelled, 'collected_at': now()}
        self.platform.audit('bundle.collected', result, robot['project_id'])
        tell('done', packed['size'], packed['size'])
        return result

    def _local(self, token):
        package_fetch._directory(self.cache)
        return str(self.cache / (token + '.fetch'))

    # --- 装到机器人 ----------------------------------------------------------------------------

    def material(self, material_id):
        return self.platform.package_delivery.material(material_id)

    @staticmethod
    def check(material, robot):
        """Refuse what may not be unpacked on this robot: anything but a usable bundle of this robot."""
        if material.get('kind') != KIND:
            raise CodedError('这份资料不是“程序和场景数据”，不能这样装到机器人上', 'bundle_not_bundle')
        if material.get('status') != 'usable':
            raise CodedError('这份程序和场景数据还不能用（云平台还没有收完或核对完），稍后再试', 'bundle_not_usable')
        if material.get('device_id') != robot['id']:
            other = material.get('device_name') if isinstance(material.get('device_name'), str) and material['device_name'] else '另一台设备'
            raise CodedError('这份程序和场景数据是从“%s”收上来的，只对那台机器人的场地有意义，不能装到这台机器人上' % other,
                             'bundle_other_device')
        package_fetch._record(material)
        if material['size'] > on_device.MAX_ARCHIVE_BYTES:
            raise CodedError('这份程序和场景数据超过 %d MiB 的上限，不能装' % (on_device.MAX_ARCHIVE_BYTES // 2 ** 20), 'bundle_too_large')

    def install(self, robot_id, project_id, material_id, *, authorized, on_progress=None, stop=None):
        """Fetch the bundle ``material_id``, send it to the robot, unpack it there and point
        ``current`` at it. Runs in the calling thread; returns where it is on the robot."""
        consent(authorized)
        robot, connection = self._target(robot_id, project_id)
        material = self.material(material_id)
        self.check(material, robot)
        return self._install(robot, connection, material, _Tell(on_progress), stop)

    def _install(self, robot, connection, material, tell, stop):
        sha256, size, name = material['sha256'], material['size'], bundle_name(material)
        digits = sha256[:on_device.DIGITS]
        place = '%s/%s/%s.tar.gz' % (on_device.STORE, name, digits)
        cloud = self.platform.cloud
        source = {'base_url': cloud.settings().get('base_url'), 'token': cloud._token()}
        try:
            fetched = package_fetch.fetch(source, material, self.cache, stop=stop, **self.tuning.get('fetch', {}),
                                          on_progress=lambda e: tell('fetching', e['received'], e['total']))
        except CloudError as exc:
            if exc.code == 'unauthorized':
                cloud._drop_token()
            raise
        sent = device_transfer.send_to_robot(self.platform, connection['id'], fetched['path'], place, roots=ROOTS, mode=0o600,
                                             on_progress=lambda e: tell('sending', e['sent'], e['total']), stop=stop,
                                             **self.tuning.get('transfer', {}))
        if sent.get('verified') is not True or sent.get('sha256') != sha256 or sent.get('bytes') != size:
            raise CodedError('机器人上核对过的文件不是云端的那一份，没有解开', 'bundle_checksum_mismatch')
        tell('unpacking', size, size)
        unpacked = self._extract(connection['id'], name, digits, stop)
        package_fetch.forget(self.cache, sha256)
        folder = '%s/%s' % (on_device.STORE, name)
        result = {'robot_id': robot['id'], 'material_id': material['id'], 'filename': material.get('filename', ''), 'name': name,
                  'device_dir': '%s/%s' % (folder, digits), 'current': folder + '/current', 'archive': place, 'bytes': size,
                  'sha256': sha256, 'files': unpacked['files'], 'already_unpacked': unpacked['already'],
                  'set_aside': '%s/%s' % (folder, unpacked['aside']) if unpacked['aside'] else None, 'note': CONFIG_HINT,
                  'installed_at': now()}
        self.platform.audit('bundle.installed', result, robot['project_id'])
        tell('done', size, size)
        return result

    # --- what a page asks ---------------------------------------------------------------------

    def overview(self, p):
        """``robots/bundles``: the bundles of this robot in the library, what is running and what was done."""
        robot = self.platform.same_project('robots', p.get('robot_id'), p.get('project_id'))
        view = {'robot_id': robot['id'], 'ssh': bool(robot.get('connection_id')), 'store': on_device.STORE,
                'logged_in': self.platform.cloud.summary()['logged_in'], 'bundles': [], 'bundles_error': '',
                'limit_bytes': on_device.MAX_ARCHIVE_BYTES, 'skipped': list(on_device.SKIPPED)}
        if view['logged_in']:
            try:
                view['bundles'] = self._listed(robot)
            except CloudError as exc:
                view['bundles_error'] = str(exc)
                view['logged_in'] = self.platform.cloud.summary()['logged_in']
        with self.lock:
            jobs = [dict(job) for job in self.jobs.values() if job['robot_id'] == robot['id']]
        view['jobs'] = sorted(jobs, key=lambda job: job['started_at'], reverse=True)
        events = [e for e in self.store.list('events') if e.get('action') in ACTIONS
                  and isinstance(e.get('details'), dict) and e['details'].get('robot_id') == robot['id']]
        events.sort(key=lambda e: e.get('created_at', ''), reverse=True)
        view['history'] = [{'kind': ACTIONS[e['action']], 'at': e.get('created_at'), **e['details']} for e in events[:HISTORY]]
        return view

    def _listed(self, robot):
        """The bundles the library holds for this robot, each with where it came from. Anyone in the
        organisation can upload one under a robot's id, so every row says who did (``created_by``, as the
        library records it) and whether this console collected it from this robot (``collected_here``)."""
        query = urllib.parse.urlencode({'kind': KIND, 'device_id': robot['id'], 'limit': 100})
        answer = self.platform.library._call('GET', '/library/materials?' + query)
        rows = answer.get('materials') if isinstance(answer.get('materials'), list) else []
        own = {e['details'].get('material_id'): e['details'] for e in self.store.list('events')
               if e.get('action') == 'bundle.collected' and isinstance(e.get('details'), dict)
               and e['details'].get('robot_id') == robot['id']}
        found = []
        for raw in rows:
            view = _scalars(raw, MATERIAL_KEYS) if isinstance(raw, dict) else {}
            if view.get('kind') != KIND or view.get('device_id') != robot['id']:
                continue
            for key in ('filename', 'description', 'device_name'):
                if isinstance(view.get(key), str):
                    view[key] = shown(view[key], 255)
            mine = own.get(view.get('id'))
            here = bool(mine) and mine.get('sha256') == view.get('sha256')
            view.update(collected_here=here, directory=mine['directory'] if here else '',
                        created_by=shown(raw['created_by'], 80) if isinstance(raw.get('created_by'), str) else '')
            found.append(view)
        return found

    def start_collect(self, p):
        consent(p.get('authorized'))
        directory = self._directory(p.get('directory'))
        robot, connection = self._target(p.get('robot_id'), p.get('project_id'))
        self.platform.library._require_login()
        name = self._name(p.get('name'), directory)
        return self._start('collect', robot, directory,
                           lambda tell, stop: self._collect(robot, connection, directory, name, tell, stop))

    def start_install(self, p):
        consent(p.get('authorized'))
        robot, connection = self._target(p.get('robot_id'), p.get('project_id'))
        material = self.material(p.get('material_id'))
        self.check(material, robot)
        return self._start('install', robot, material.get('filename', ''),
                           lambda tell, stop: self._install(robot, connection, material, tell, stop))

    def _start(self, kind, robot, subject, work):
        job = {'id': uid('bundle-job'), 'kind': kind, 'project_id': robot['project_id'], 'robot_id': robot['id'],
               'subject': subject, 'status': 'running', 'phase': 'starting', 'done': 0, 'total': 0, 'error': None, 'code': None,
               'note': None, 'result': None, 'started_at': now(), 'finished_at': None}
        stop = threading.Event()
        with self.lock:
            if self.closing:
                raise ValueError('应用正在退出')
            if any(other['status'] == 'running' and other['robot_id'] == robot['id'] for other in self.jobs.values()):
                raise CodedError('这台机器人的程序和场景数据正在收或正在装，等它结束后再试', 'bundle_busy')
            for old in [key for key, other in self.jobs.items() if other['status'] != 'running'][:-KEEP_JOBS]:
                del self.jobs[old]
            thread = threading.Thread(target=self._run, args=(job, work, stop), daemon=True)
            self.jobs[job['id']], self.stops[job['id']], self.threads[job['id']] = job, stop, thread
            thread.start()
            return dict(job)

    def _run(self, job, work, stop):
        def progress(event):
            with self.lock:
                job.update(phase=event['phase'], done=event['done'], total=event['total'])
        outcome = {'status': 'failed', 'error': '内部错误，这一步没有完成；可以重新开始', 'code': 'desktop_internal_error'}
        try:
            outcome = {'status': 'done', 'phase': 'done', 'result': work(_Tell(progress), stop)}
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
                raise ValueError('找不到这一次操作；应用重启后正在进行的不会保留，可以重新开始')
            return job

    def status(self, p):
        job = self._job(p)
        with self.lock:
            return dict(job)

    def stop(self, p):
        job = self._job(p)
        with self.lock:
            stop = self.stops.get(job['id'])
            if stop is not None:
                stop.set()
            return dict(job)

    def close(self):
        """Stop what is running, before the store is closed."""
        with self.lock:
            self.closing = True
            threads = list(self.threads.values())
            for stop in self.stops.values():
                stop.set()
        deadline = time.monotonic() + JOIN_SECONDS
        for thread in threads:
            thread.join(max(0, deadline - time.monotonic()))


class _Tell:
    """One shape for what every step reports: ``phase``, ``done`` and ``total`` bytes of that step."""

    def __init__(self, on_progress):
        self.on_progress = on_progress

    def __call__(self, phase, done, total):
        if self.on_progress is not None:
            self.on_progress({'phase': phase, 'done': done, 'total': total})

