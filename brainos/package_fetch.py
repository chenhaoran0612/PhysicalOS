"""A driver package comes from the cloud library to this computer (issue #31, the first half).

The library keeps packages in the cloud (issue #33); the channel that puts a file on a device
(issue #18, ``device_transfer``) sends a local file. This module is the piece in between: it
fetches one package into a private directory of the workspace and hands back a file whose
SHA-256 is the one the library recorded.

How a package travels:

* ``GET /library/materials/{id}/content`` is read block by block into ``<sha256>.part``. A
  connection that breaks, goes silent or is stopped costs nothing that already arrived: the next
  connection, or the next call, asks for the rest with ``Range: bytes=<what is here>-``.
* Every answer is checked before a byte of it is kept: the checksum the cloud announces
  (``X-Checksum-SHA256``) and the length must be the ones in the material's record, and a range
  must start where this computer's copy ends. An answer that fits neither is not appended.
* When all bytes are here the SHA-256 of the whole file is computed. Only if it equals the
  recorded one does the file become ``<sha256>.pkg`` (read-only); otherwise it is deleted and the
  fetch is refused. It is not retried: the next call starts from nothing.

Nothing here opens the package. Its content is untrusted (the cloud only recognised its type):
it is bytes with a checksum, never unpacked, never run.

The same rules as every other call to the cloud apply: no proxy, no redirect, the address policy
on the address actually connected to, https unless the cloud runs on this computer. The session
token goes into the request header and nowhere else.
"""
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import stat
import threading
import time
import urllib.parse
from pathlib import Path

from .cloud_account import (API_PREFIX, ERROR_MESSAGES, CloudError, _CheckedHTTPConnection, _CheckedHTTPSConnection,
                            cloud_url)
from .device_transfer_target import shown
from .errors import CodedError

BLOCK_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 4 * 1024 ** 3       # what the device channel sends at most; the cloud's own limit for a package is lower
CONNECT_SECONDS = 20
STALL_SECONDS = 60                      # no byte for this long: the connection is given up and the rest asked for again
RETRY_WAITS = (1, 2, 5, 10, 20, 30)     # seconds before each new connection after failures in a row without progress
SPACE_MARGIN_BYTES = 32 * 1024 * 1024   # kept free on this computer's disk besides the package
STALE_SECONDS = 7 * 24 * 3600           # an unfinished or undelivered copy nobody touched for this long is removed
ERROR_BODY_BYTES = 200_000
PART, WHOLE = '.part', '.pkg'
MATERIAL_ID = re.compile(r'mat_[0-9a-f]{20}')
SHA256 = re.compile(r'[0-9a-f]{64}')
CONTENT_RANGE = re.compile(r'bytes (\d+)-(\d+)/(\d+)')
_OWN_FILE = re.compile(r'[0-9a-f]{64}\.(?:part|pkg)')
_RETRY_STATUS = (408, 425, 429)
_NOFOLLOW, _DIRECTORY = getattr(os, 'O_NOFOLLOW', 0), getattr(os, 'O_DIRECTORY', 0)
UNRESOLVED = '无法解析云平台地址'      # what cloud_account._connect_checked says when a name does not resolve (may pass)
SHOWN_CHARS = 300
PROBE_SECONDS = 1                       # after the last byte of an answer without a length: is there more?


class FetchError(CodedError):
    """A fetch that was refused or did not finish.

    ``received`` of ``total`` bytes are on this computer. ``resumable`` says whether calling again
    goes on from there (the network, a stop request, this computer's disk) or starts from nothing
    (the content was not what the library recorded: nothing of it was kept)."""

    def __init__(self, message, code, resumable=False, received=0, total=0):
        super().__init__(message, code)
        self.resumable, self.received, self.total = resumable, received, total


class ProgressHandlerError(RuntimeError):
    """The caller's ``on_progress`` raised. Kept apart from network errors, which are retried."""


class _Retry(Exception):
    """This connection got no further; another one may."""


def _problem(exc):
    return (str(exc) or type(exc).__name__)[:300]


def _append(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _local_failure(exc, received=0, total=0):
    return FetchError('这台电脑上存放安装包的位置用不了（%s）；已经取到的部分没有动，解决后再次开始会接着取'
                      % (getattr(exc, 'strerror', None) or _problem(exc)),
                      code='package_cache_unusable', resumable=True, received=received, total=total)


def _record(material):
    """(id, size, SHA-256) of a material's record, or a refusal when they cannot be what they claim."""
    record = material if isinstance(material, dict) else {}
    material_id, size, sha256 = record.get('id'), record.get('size'), record.get('sha256')
    plain = isinstance(size, int) and not isinstance(size, bool)
    if not (isinstance(material_id, str) and MATERIAL_ID.fullmatch(material_id)) or not (
            isinstance(sha256, str) and SHA256.fullmatch(sha256)) or not plain or size <= 0:
        raise FetchError('云平台给出的资料记录不完整（编号、大小或校验值不对），没有取这个文件', code='package_record_invalid')
    if size > MAX_PACKAGE_BYTES:
        raise FetchError('这个安装包超过了可以送到设备的大小上限（%d 字节），没有取' % MAX_PACKAGE_BYTES, code='package_record_invalid')
    return material_id, size, sha256


def _directory(cache_dir):
    """The directory packages are kept in, private to this account. A link in its place is not used."""
    path = Path(cache_dir)
    try:
        if path.is_symlink():
            raise OSError('它是一个符号链接')
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise OSError('它不是目录')
        _chmod(path, 0o700, _DIRECTORY)
    except OSError as exc:
        raise _local_failure(exc) from None
    return path


def _chmod(path, mode, flags=0):
    """Change the mode of what is at ``path`` itself: a link put there in the meantime is not followed."""
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW | flags)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _plain_directory(cache_dir):
    """True when ``cache_dir`` is a directory and not a link to one."""
    try:
        return stat.S_ISDIR(os.lstat(cache_dir).st_mode)
    except OSError:
        return False


def _size(path):
    """Bytes of a regular file, 0 when nothing is there. Anything else in its place is refused."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(info.st_mode):
        raise OSError('安装包文件的位置被别的东西占着：' + path.name)
    return info.st_size


def _remove(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _digest(path, stop=None):
    """SHA-256 of the file at ``path`` (not through a link); None when ``stop`` was set on the way."""
    digest = hashlib.sha256()
    with open(os.open(path, os.O_RDONLY | _NOFOLLOW), 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            if stop is not None and stop.is_set():
                return None
            digest.update(block)
    return digest.hexdigest()


def identity(path):
    """What tells the file at ``path`` from any other later: device, inode, size, modification time.
    None when nothing (or not a regular file) is there."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns] if stat.S_ISREG(info.st_mode) else None


class _StopGuard:
    """Ends a wait for the cloud as soon as the caller asks to stop: the connection is shut, and the
    read that was waiting fails like on any broken connection."""

    def __init__(self, connection, stop):
        self.connection, self.stop, self.done, self.thread = connection, stop, threading.Event(), None
        self.sock = None                # set once connected: http.client lets go of its own for an answer without a length
        if stop is not None:
            self.thread = threading.Thread(target=self._watch, daemon=True)
            self.thread.start()

    def _watch(self):
        while not self.done.wait(.1):
            sock = self.sock if self.sock is not None else self.connection.sock
            if self.stop.is_set() and sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    def finish(self):
        self.done.set()
        if self.thread is not None:
            self.thread.join(timeout=2)


class _Fetch:
    """One call of ``fetch``: what is known about the package and how much of it is here."""

    def __init__(self, source, record, cache, options):
        self.source, (self.material_id, self.size, self.sha256) = source, record
        self.part, self.whole = cache / (self.sha256 + PART), cache / (self.sha256 + WHOLE)
        self.cache, self.options = cache, options
        self.stop, self.on_progress = options['stop'], options['on_progress']
        self.have = self.resumed_from = self.received = self.attempts = self.told = 0
        self.started = time.monotonic()

    # --- reporting ----------------------------------------------------------------------------

    def _tell(self, stage, **more):
        self.told = self.have
        if self.on_progress is None:
            return
        event = {'stage': stage, 'received': self.have, 'total': self.size, 'attempt': self.attempts, **more}
        try:
            self.on_progress(event)
        except Exception as exc:
            raise ProgressHandlerError('on_progress raised %s: %s' % (type(exc).__name__, exc)) from exc

    def _position(self):
        return '已取到 %d / %d 字节' % (self.have, self.size)

    def _kept(self):
        return '已取到的部分保留在这台电脑上，再次开始会从这里继续' if self.have else '这台电脑上没有留下未完成的文件'

    def _error(self, message, code, resumable):
        return FetchError(message, code=code, resumable=resumable, received=self.have, total=self.size)

    def _check_stop(self):
        if self.stop is not None and self.stop.is_set():
            raise self._error('已按停止请求中止取安装包：%s。%s' % (self._position(), self._kept()), code='package_fetch_cancelled', resumable=True)

    def _changed(self, what):
        """The cloud no longer serves what the record describes: nothing of it is kept."""
        self._start_over()
        return self._error('云平台上这份安装包的内容和资料记录对不上（%s），没有取。'
                           '请在“资料库”页刷新后再试；一直这样请删除这份资料后重新上传' % what, code='package_content_changed', resumable=False)

    def _result(self, cached):
        return {'path': str(self.whole), 'identity': self._local(identity, self.whole), 'bytes': self.size, 'sha256': self.sha256, 'cached': cached,
                'resumed_from': self.resumed_from, 'received_bytes': self.received, 'attempts': self.attempts,
                'seconds': round(time.monotonic() - self.started, 3)}

    # --- this computer's side: its failures are not the network's -----------------------------

    def _local(self, action, *arguments):
        try:
            return action(*arguments)
        except OSError as exc:
            raise _local_failure(exc, self.have, self.size) from None

    def _start_over(self):
        self._local(_remove, self.part)
        self.have = self.resumed_from = self.told = 0

    def _room(self):
        need = self.size - self.have
        if self._local(shutil.disk_usage, self.cache).free < need + SPACE_MARGIN_BYTES:
            raise self._error('这台电脑的磁盘空间不够放下这个安装包（还需要 %d 字节）；腾出空间后再试。%s'
                              % (need, self._kept()), code='package_no_local_space', resumable=True)

    # --- the loop -----------------------------------------------------------------------------

    def run(self):
        self._check_stop()
        if self._local(_size, self.whole):
            self._tell('verifying')                   # a copy from an earlier call: is it still the package?
            same = self._local(_size, self.whole) == self.size and self._local(_digest, self.whole, self.stop) == self.sha256
            self._check_stop()
            if same:
                self.have = self.size
                self._local(os.utime, self.whole)     # in use again: not one for the sweep of old copies
                self._tell('fetched')
                return self._result(True)
            self._local(_remove, self.whole)
        self.have = self._local(_size, self.part)
        if self.have > self.size:
            self._start_over()                        # longer than the package: not something to go on from
        self.resumed_from = self.have
        waits, failures = self.options['retry_waits'], 0
        while self.have < self.size:
            self._check_stop()
            self._room()                              # before every connection: other fetches use the same disk
            self.attempts += 1
            before = self.have
            try:
                self._attempt()
                continue
            except _Retry as exc:
                problem = _problem(exc)
            self._check_stop()                        # a stop request shuts the connection: that is not a failure
            failures = 1 if self.have > before else failures + 1
            if failures > len(waits):
                raise self._error('从云平台取安装包中断：%s，之后连续 %d 次连接都没有新的进展（最后一次：%s）。%s'
                                  % (self._position(), failures, problem, self._kept()), code='package_fetch_interrupted', resumable=True)
            self._tell('retrying', error=problem, wait=waits[failures - 1])
            if self.stop is not None:
                self.stop.wait(waits[failures - 1])
            else:
                time.sleep(waits[failures - 1])
        return self._verify()

    def _verify(self):
        self._tell('verifying')
        got = self._local(_digest, self.part, self.stop)
        self._check_stop()
        if got != self.sha256:
            self._local(_remove, self.part)
            raise FetchError('取到的安装包和资料库记录的校验值不一致，已从这台电脑上删除，没有送往设备：资料库记录的 SHA-256 是 %s，'
                             '取到的文件是 %s。再试一次会重新取；仍然不一致说明云平台上的文件已损坏，请删除这份资料后重新上传'
                             % (self.sha256, got), code='package_checksum_mismatch', received=0, total=self.size)
        self._local(_chmod, self.part, 0o400)
        self._local(os.replace, self.part, self.whole)
        self._tell('fetched')
        return self._result(False)

    # --- one connection -----------------------------------------------------------------------

    def _connect(self):
        base = urllib.parse.urlsplit(cloud_url(self.source.get('base_url')))
        token = self.source.get('token')
        if not isinstance(token, str) or not token or any(char in token for char in '\r\n'):
            raise CodedError('资料库保存在云平台；请先在“云平台账户”页登录', code='cloud_login_required')
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/octet-stream', 'Accept-Encoding': 'identity'}
        if self.have:
            headers['Range'] = 'bytes=%d-' % self.have
        path = '%s%s/library/materials/%s/content' % (base.path.rstrip('/'), API_PREFIX, self.material_id)
        if base.scheme == 'https':
            connection = _CheckedHTTPSConnection(base.hostname, base.port, timeout=CONNECT_SECONDS, context=ssl.create_default_context())
        else:
            connection = _CheckedHTTPConnection(base.hostname, base.port, timeout=CONNECT_SECONDS)
        return connection, path, headers

    def _attempt(self):
        """One request for what is missing. Returns when the package is complete; raises ``_Retry``
        when this connection got no further."""
        connection, path, headers = self._connect()
        guard = _StopGuard(connection, self.stop)
        try:
            try:
                connection.connect()
                sock = guard.sock = connection.sock     # http.client lets go of it for an answer without a length; the answer still reads it
                sock.settimeout(self.options['stall_seconds'])
                connection.request('GET', path, headers=headers)
                response = connection.getresponse()
                self._accept(response)
                self._read(response, sock)
            except ssl.SSLCertVerificationError:
                raise CloudError('云平台的 HTTPS 证书校验失败，已停止连接') from None      # not something to retry
            except (OSError, http.client.HTTPException) as exc:
                raise _Retry(_problem(exc)) from None
            except CloudError:
                raise
            except CodedError:
                raise
            except ValueError as exc:
                if str(exc) == UNRESOLVED:          # a name that does not resolve right now: may pass
                    raise _Retry(_problem(exc)) from None
                raise CloudError(str(exc)) from None    # the address policy refuses this cloud: asking again changes nothing
        finally:
            guard.finish()
            connection.close()

    def _accept(self, response):
        """Decide from the status and the headers whether the body may be kept, and from where."""
        status = response.status
        if status in (200, 206):
            announced = response.getheader('X-Checksum-SHA256')
            if announced is not None and announced.strip().lower() != self.sha256:
                raise self._changed('云平台给出的校验值变了')
        if status == 200:
            length = response.getheader('Content-Length')
            if length is not None and length.strip() != str(self.size):
                raise self._changed('文件长度不同')
            if self.have:
                self._start_over()                    # the range was ignored: this answer is the whole file
            return
        if status == 206:
            match = CONTENT_RANGE.fullmatch((response.getheader('Content-Range') or '').strip())
            first, last, total = (int(x) for x in match.groups()) if match else (-1, -1, -1)
            if match and total != self.size:
                raise self._changed('文件长度不同')
            if first != self.have or not first <= last < self.size:
                self._start_over()
                raise _Retry('云平台没有按请求的位置续传，改为从头取')
            return
        if status == 416:
            self._start_over()
            raise _Retry('云平台不接受续传的位置，改为从头取')
        if status >= 500 or status in _RETRY_STATUS:
            raise _Retry('云平台暂时不可用（HTTP %d）' % status)
        # Anything else is final: a refusal, or a redirect (never followed).
        body = {}
        try:
            parsed = json.loads(response.read(ERROR_BODY_BYTES + 1)[:ERROR_BODY_BYTES] or b'{}')
            body = parsed if isinstance(parsed, dict) else {}
        except (ValueError, OSError, http.client.HTTPException):
            pass
        code = body.get('code') if isinstance(body.get('code'), str) else None
        if status == 401:
            code = 'unauthorized'
        message = ERROR_MESSAGES.get(code) or (shown(body['error'], SHOWN_CHARS) if isinstance(body.get('error'), str) else None)
        raise CloudError(message or '云平台返回 HTTP %d' % status, status, code if code is None else code[:64], {})

    def _read(self, response, sock):
        try:
            fd = os.open(self.part, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NOFOLLOW, 0o600)
        except OSError as exc:
            raise _local_failure(exc, self.have, self.size) from None
        try:
            if self._local(os.fstat, fd).st_size != self.have:
                raise _local_failure(OSError('未完成的文件被别的程序改动了'), self.have, self.size)
            while self.have < self.size:
                data = response.read1(min(self.options['block_bytes'], self.size - self.have))
                if not data:
                    raise _Retry('云平台在文件传完之前关闭了连接')
                self._local(_append, fd, data)
                self.have += len(data)
                self.received += len(data)
                if self.have - self.told >= self.options['block_bytes'] or self.have == self.size:
                    self._tell('fetching')
                self._check_stop()
            if response.length is None and self._more(response, sock):
                raise self._changed('文件比记录的长')
            self._local(os.fsync, fd)
        finally:
            os.close(fd)

    @staticmethod
    def _more(response, sock):
        """True when an answer without a declared length goes on after the last byte of the package.
        A server that keeps the connection open after it is not waited for long."""
        try:
            sock.settimeout(PROBE_SECONDS)
            return bool(response.read1(1))
        except (OSError, http.client.HTTPException):
            return False                              # the whole file is here; its checksum decides


def _number(value, low, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < low:
        raise ValueError(what + '无效')
    return value


def fetch(source, material, cache_dir, *, on_progress=None, stop=None, retry_waits=RETRY_WAITS,
          stall_seconds=STALL_SECONDS, block_bytes=BLOCK_BYTES):
    """Fetch the package of ``material`` (its record from the cloud: ``id``, ``size``, ``sha256``)
    into ``cache_dir`` and verify it.

    ``source`` is ``{'base_url', 'token'}``: the cloud this workspace is signed in to. ``on_progress(event)``
    gets ``stage`` ('fetching', 'retrying', 'verifying', 'fetched'), ``received`` and ``total`` bytes on
    this computer, and ``attempt``. ``stop`` is an optional ``threading.Event``: once set the fetch
    ends, also while it waits for the cloud; what arrived is kept.

    Returns ``path`` (the verified file, read-only), ``identity`` (what ``identity(path)`` said of it
    once it was verified), ``bytes``, ``sha256``, ``cached`` (it was here
    already, verified again), ``resumed_from``, ``received_bytes`` (in this call), ``attempts`` and
    ``seconds``.

    Raises ``FetchError`` (interrupted, stopped, no room, wrong content), or ``CloudError`` when the
    cloud refuses (not signed in, no such material, the file is gone)."""
    record = _record(material)
    if not isinstance(retry_waits, (tuple, list)):
        raise ValueError('重试间隔无效')
    options = {'stop': stop, 'on_progress': on_progress, 'block_bytes': int(_number(block_bytes, 1024, '读取块大小')),
               'stall_seconds': _number(stall_seconds, 0.1, '无进展时限'),
               'retry_waits': tuple(_number(wait, 0, '重试间隔') for wait in retry_waits)}
    return _Fetch(source if isinstance(source, dict) else {}, record, _directory(cache_dir), options).run()


def cached(cache_dir, sha256):
    """How much of a package is on this computer: {'bytes', 'complete'} or None when nothing is."""
    if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
        return None
    cache = Path(cache_dir)
    if not _plain_directory(cache):
        return None
    for suffix in (WHOLE, PART):
        try:
            size = _size(cache / (sha256 + suffix))
        except OSError:
            continue
        if size:
            return {'bytes': size, 'complete': suffix == WHOLE}
    return None


def forget(cache_dir, sha256):
    """Remove this computer's copy of a package, finished or not. True when something was removed."""
    if not isinstance(sha256, str) or not SHA256.fullmatch(sha256) or not _plain_directory(cache_dir):
        return False
    removed = False
    for suffix in (PART, WHOLE):
        path = Path(cache_dir) / (sha256 + suffix)
        try:
            os.unlink(path)
            removed = True
        except OSError:
            pass
    return removed


def sweep(cache_dir, older_than=STALE_SECONDS, keep=()):
    """Remove this module's own files that nobody touched for ``older_than`` seconds: a fetch that
    was given up, or a package that was never delivered. The files of the checksums in ``keep``
    (packages in use right now) stay. Returns how many went."""
    cache, removed, limit, keep = Path(cache_dir), 0, time.time() - older_than, set(keep)
    try:
        if not _plain_directory(cache):
            return 0
        entries = list(os.scandir(cache))
    except OSError:
        return 0
    for entry in entries:
        try:
            if (_OWN_FILE.fullmatch(entry.name) and entry.name[:64] not in keep and entry.is_file(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_mtime < limit):
                os.unlink(entry.path)
                removed += 1
        except OSError:
            pass
    return removed
