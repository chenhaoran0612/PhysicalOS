"""The product's own channel for putting a file on a device (issue #18).

Nothing here is a command an assistant typed. The repair rules that make ``scp`` and ``rsync``
wait for a person (``repair_policy.ASK_FIRST``) are about those and stay as they are; this channel
is called by product code, with a local file the product chose and a destination that is checked
(``device_transfer_target``).

How a file travels:

* It is cut into chunks. A chunk is compressed on its own (one gzip member) when the device has
  gzip and the content is worth it, and written at a known position of one temporary file next
  to the destination. A chunk counts once the device has acknowledged every byte of it.
* After each chunk the position goes into a small local record (``device_transfer_record``). A
  connection that breaks, hangs or is stopped costs at most the chunk in flight: the next
  connection, or the next call, asks the device how much of the temporary file it has and goes
  on from there.
* When everything has arrived, one product-owned script on the device unpacks the temporary
  file, computes its SHA-256 there, and only if that equals the SHA-256 of the local file moves
  it over the destination (one rename: the destination is never half written). A difference
  removes everything that was uploaded and is reported; it is not retried.

"Verified" means the device computed that checksum: it guards against damage on the way and on
the device's disk, not against a device that lies about itself.

Nothing uses ``sudo``. Which local file may leave this computer is the caller's decision: pass
only files the product chose, never a path taken from a model or a page. A symbolic link is not
accepted as the local file, and a file that changes while it is being sent stops the transfer.

What can stay on the device: the temporary file of a transfer that was interrupted or stopped
(``TransferError.remote_temp``). It is what the next call continues from; ``unfinished`` lists
them and ``discard`` removes one.

The device needs SFTP, ``sh`` and one of ``sha256sum``, ``shasum`` or ``openssl``. Without
``gzip`` the file is sent uncompressed.
"""
import gzip
import hashlib
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import device_access, hardware_init
from . import device_transfer_record as records
from . import device_transfer_target as target
from .device_transfer_target import DEFAULT_ROOTS, SCRIPTS, TransferError  # noqa: F401  (part of this module's interface)

CHUNK_BYTES = 256 * 1024
CAREFUL_CHUNK_BYTES = 16 * 1024         # the first chunks after a broken connection: less in flight, confirmed more often
QUICK_SECONDS = 3                       # after a break: a chunk confirmed within this long doubles the next one, a slower one halves it
MIN_CHUNK_BYTES = 1024
PIECE_BYTES = 32768                     # one SFTP write request
MAX_FILE_BYTES = 4 * 1024 ** 3
STALL_SECONDS = 120                     # nothing confirmed for this long: the connection is given up
RETRY_WAITS = (1, 2, 5, 10, 20, 30)     # seconds before each new connection after failures in a row without progress
SCRIPT_SECONDS = 60
VERIFY_BYTES_PER_SECOND = 2 * 1024 * 1024    # the slowest device assumed when it reads a file to hash or unpack it
GUARD_MARGIN_SECONDS = 15
OUTPUT_LIMIT = 16384                    # bytes of a script's answer that are read
COMPRESS_SAMPLE_BYTES = 65536
COMPRESS_WORTH = 0.9                    # compress only what shrinks to this share or less
_FATAL_LOGIN = ('auth', 'key', 'fingerprint', 'forbidden')       # asking again changes nothing, and may lock the account
_NOFOLLOW, _NONBLOCK = getattr(os, 'O_NOFOLLOW', 0), getattr(os, 'O_NONBLOCK', 0)


class ProgressHandlerError(RuntimeError):
    """The caller's ``on_progress`` raised. Kept apart from network errors, which are retried.
    ``result`` is set when the transfer itself had finished and was verified."""
    result = None


class _Retry(Exception):
    """This connection got no further; another one may."""


class _SourceChanged(Exception):
    """The local file is no longer the one whose checksum was taken."""


def _opened(path):
    """The local file, open for reading. A symbolic link put in its place is not followed."""
    return open(os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK), 'rb')


def _identity(handle):
    info = os.fstat(handle.fileno())
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _source(local_path):
    """The local file as it is now: its size, its SHA-256, and what tells it from any other file later."""
    try:
        path = Path(local_path)
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise target.refuse('source', '要传输的本地路径不是普通文件（符号链接也不接受）')
        with _opened(path) as handle:
            identity = _identity(handle)
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise target.refuse('source', '要传输的本地路径不是普通文件')
            if identity[2] > MAX_FILE_BYTES:
                raise target.refuse('source', '本地文件超过可传输的大小上限（%d 字节）' % MAX_FILE_BYTES)
            digest = hashlib.sha256()
            for block in iter(lambda: handle.read(1 << 20), b''):
                digest.update(block)
            if _identity(handle) != identity:
                raise target.refuse('source', '本地文件正在被修改，稍后再传')
    except TypeError:
        raise target.refuse('source', '要传输的本地路径无效') from None
    except OSError as exc:
        raise target.refuse('source', '读不到要传输的本地文件：' + (exc.strerror or type(exc).__name__)) from None
    return {'path': path, 'size': identity[2], 'identity': identity, 'sha256': digest.hexdigest()}


def _read_at(source, offset, length):
    """Bytes of the local file, after making sure it is still the file that was hashed."""
    with _opened(source['path']) as handle:
        if _identity(handle) != source['identity']:
            raise _SourceChanged()
        handle.seek(offset)
        return handle.read(length)


_ACTIVE, _ACTIVE_LOCK = set(), threading.Lock()


@contextmanager
def _exclusive(record_file):
    """One transfer per device and destination at a time, within this process."""
    key = str(record_file)
    with _ACTIVE_LOCK:
        if key in _ACTIVE:
            raise target.refuse('busy', '这个文件正在向这台设备传输，等它结束后再试')
        _ACTIVE.add(key)
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(key)


def _running(record_file):
    with _ACTIVE_LOCK:
        return str(record_file) in _ACTIVE


def _close(device):
    try:
        device.close()
    except Exception:
        pass        # the connection is being thrown away, often because it is already broken


def _problem(exc):
    """What went wrong, for a person. paramiko often raises with no text at all when a connection ends."""
    return target.shown(str(exc)) or '连接中断（%s）' % type(exc).__name__


class _Guard:
    """Ends the waits paramiko would sit out: closes the connection when nothing was confirmed
    before the deadline, or as soon as the caller asks to stop. The call that was waiting then
    fails like on any broken connection."""

    def __init__(self, device, stop, limit):
        self.device, self.stop, self.limit, self.allowed = device, stop, limit, limit
        self.deadline, self.done, self.waited = time.monotonic() + limit, threading.Event(), None
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()

    def confirm(self, allow=None):
        self.allowed = self.limit if allow is None else allow
        self.deadline = time.monotonic() + self.allowed

    def _watch(self):
        while not self.done.wait(.1):
            silent = time.monotonic() > self.deadline
            if silent or (self.stop is not None and self.stop.is_set()):
                self.waited = self.allowed if silent else None       # seconds of silence it gave up after
                _close(self.device)
                return

    def finish(self):
        self.done.set()
        self.thread.join(timeout=2)


def _sftp(device):
    """The device's SFTP session. A device that turns the request down while the connection is
    fine has no SFTP service: that is said at once, a new connection would be turned down too."""
    import paramiko
    try:
        return device.client.open_sftp()
    except AttributeError:          # paramiko has no transport left: the connection is already gone
        raise _Retry('与设备的连接已断开') from None
    except paramiko.SSHException as exc:
        transport = device.client.get_transport()
        if transport is None or not transport.is_active():
            raise
        raise TransferError('connect', 'no_sftp', '设备没有接受 SFTP 请求（它的 SSH 服务可能没有开启 SFTP），无法传输：' + _problem(exc)) from None


def _run_script(device, kind, seconds, *arguments):
    """The answer of one of ``SCRIPTS``, run on the device by ``sh -s``. Nothing else is ever run."""
    try:
        result = hardware_init.run_command(device, target.command(*arguments), seconds, stdin=SCRIPTS[kind],
                                           keep='head', limit=OUTPUT_LIMIT, redact=False)
    except AttributeError:
        raise _Retry('与设备的连接已断开') from None
    if result['timed_out']:
        raise _Retry('设备在 %d 秒内没有答复' % seconds)
    return result['stdout']


def _write_confirmed(remote, offset, data):
    """Write ``data`` at ``offset``; returns once the device has acknowledged all of it. The pieces
    of a large chunk go out back to back and the last one waits for every answer, so a refusal of
    any piece (a full disk, say) is raised here."""
    if not data:
        return
    tail = len(data) % PIECE_BYTES or min(len(data), PIECE_BYTES)
    remote.seek(offset)
    if len(data) > tail:
        remote.set_pipelined(True)
        remote.write(data[:-tail])
    remote.set_pipelined(False)
    remote.write(data[-tail:])


def _remove(sftp, path):
    try:
        sftp.remove(path)
    except FileNotFoundError:
        pass


class _Transfer:
    """One call of ``upload``: what is known about the file, the device and the progress so far."""

    def __init__(self, connect, source, remote_path, names, mode, record_file, options):
        self.connect, self.source, self.remote_path, self.names = connect, source, remote_path, names
        self.mode, self.record_file, self.options = mode, record_file, options
        self.stop, self.on_progress = options['stop'], options['on_progress']
        saved = records.load(record_file)
        same = saved is not None and (saved['sha256'], saved['size']) == (source['sha256'], source['size'])
        # A record of another version of the file stays on disk until the device has been cleaned of
        # that version (at the probe) and this transfer writes its own.
        self.record = saved if same else None
        self.sent = self.record['sent'] if self.record else 0
        self.dest = self.part = self.resumed_from = None
        self.attempts, self.wire_bytes, self.stage, self.silent_for = 0, 0, 'connect', None
        self.careful = self.finalizing = self.cleaned = False
        self.started = time.monotonic()

    # --- reporting ----------------------------------------------------------------------------

    def _tell(self, stage, **more):
        if self.on_progress is None:
            return
        event = {'stage': stage, 'sent': self.sent, 'total': self.source['size'], 'attempt': self.attempts, **more}
        try:
            self.on_progress(event)
        except Exception as exc:
            raise ProgressHandlerError('on_progress raised %s: %s' % (type(exc).__name__, exc)) from exc

    def _error(self, stage, reason, message, resumable):
        """An error that ends the call. With ``resumable`` the record is kept and says how it ended;
        without, the record goes, and so must have whatever this transfer put on the device."""
        left = self.record['part'] if resumable and self.record else None
        if left is not None:
            self.record = records.ended(self.record, 'cancelled' if reason == 'cancelled' else 'interrupted', message)
            self._remember()
        elif not resumable and (self.record is not None or self.cleaned):
            self.record = None
            records.forget(self.record_file)
        return TransferError(stage, reason, message, self.sent, self.source['size'], left, left is not None, self.attempts)

    def _position(self):
        return '设备已确认收到 %d / %d 字节' % (self.sent, self.source['size'])

    def _leftover(self):
        if not self.record:
            return '这次传输没有在设备上留下未完成的文件。'
        return '未完成的部分留在设备上的 %s，再次传输会从这里继续；不再需要时可以放弃这次传输来清理。' % self.record['part']

    def _check_stop(self):
        if self.stop is not None and self.stop.is_set():
            raise self._error(self.stage, 'cancelled', '已按停止请求中止传输：%s。%s' % (self._position(), self._leftover()), True)

    # --- this computer's side: its failures are not the device's ------------------------------

    def _local(self, offset, length):
        """Bytes of the local file. If it cannot be read, what arrived stays: the same file can be continued later."""
        try:
            return _read_at(self.source, offset, length)
        except OSError as exc:
            raise self._error('send', 'source', '读不到要传输的本地文件了（%s）。%s。%s'
                              % (exc.strerror or type(exc).__name__, self._position(), self._leftover()), True) from None

    def _remember(self):
        try:
            records.save(self.record_file, self.record)
        except OSError as exc:
            message = '无法在这台电脑上保存续传记录 %s（%s），传输已停止。%s' % (
                self.record_file.parent, exc.strerror or type(exc).__name__, self._position())
            raise TransferError('send', 'records', message, self.sent, self.source['size'], self.part, False, self.attempts) from None

    # --- the retry loop -----------------------------------------------------------------------

    def run(self):
        import paramiko
        waits, failures = self.options['retry_waits'], 0
        while True:
            self._check_stop()
            self.attempts += 1
            before, self.silent_for = self.sent, None
            try:
                return self._attempt()
            except (_Retry, OSError, EOFError, paramiko.SSHException) as exc:
                problem = _problem(exc)
            if self.silent_for is not None:       # the guard ended it: say that, not what paramiko raised when it was cut
                problem = '设备 %g 秒没有任何答复，这次连接已放弃' % self.silent_for
            self._check_stop()                    # a stop request closes the connection: that is not a failure
            failures = 1 if self.sent > before else failures + 1
            if failures > len(waits):
                raise self._error(self.stage, 'interrupted', '传输中断：%s，之后连续 %d 次连接都没有新的进展（最后一次：%s）。%s'
                                  % (self._position(), failures, problem, self._leftover()), True)
            self.careful = True
            self._tell('retrying', error=problem, wait=waits[failures - 1])
            if self.stop is not None:
                self.stop.wait(waits[failures - 1])
            else:
                time.sleep(waits[failures - 1])

    def _open(self):
        self.stage = 'connect'
        try:
            return self.connect()
        except device_access.ConnectError as exc:
            if exc.reason in _FATAL_LOGIN:
                raise self._error('connect', exc.reason, '无法登录设备，传输没有继续：%s。%s' % (exc, self._leftover()), True) from None
            raise _Retry(str(exc)) from None

    def _attempt(self):
        device = self._open()
        guard = _Guard(device, self.stop, self.options['stall_seconds'])
        try:
            device.stop_event = self.stop
            try:
                sftp = _sftp(device)
            except TransferError as exc:
                raise self._error('connect', exc.reason, '%s。%s' % (exc, self._leftover()), True) from None
            self.stage = 'send'
            try:
                return self._work(device, sftp, guard)
            except OSError as exc:
                channel = sftp.get_channel()
                if isinstance(exc, TimeoutError) or channel.closed or not channel.get_transport().is_active():
                    raise                           # the connection, not the device's answer
                raise self._error(self.stage, 'refused', '设备拒绝了这次写入（磁盘已满、没有写权限或配额不足）：%s。%s。%s'
                                  % (_problem(exc), self._position(), self._leftover()), True) from None
        finally:
            guard.finish()
            self.silent_for = guard.waited
            _close(device)

    # --- one connection -----------------------------------------------------------------------

    def _work(self, device, sftp, guard):
        sha256 = self.source['sha256']
        if self.dest is None:
            self.dest = target.resolve(sftp, self.names)
            self.part = target.temp_name(self.dest, target.PART, sha256)
        self._tell('checking')
        probe = self._probe(device, guard)
        if probe['dest'] == sha256:
            _remove(sftp, self.part)          # what a lost answer to the final check may have left behind
            sftp.chmod(self.dest, int(self.mode, 8))
            already = not self.finalizing and self.wire_bytes == 0
            return self._result(already, None if already or not self.record else self.record['encoding'])
        if probe['hash'] != target.EMPTY_SHA256:
            _remove(sftp, self.part)
            raise self._error('verify', 'no_checksum_tool', '设备上没有可用的 sha256sum、shasum 或 openssl，'
                              '无法在设备上核对校验值，所以没有传输', False)
        remote = None
        try:
            encoding = self._encoding(probe['gzip'])
            remote, wire = self._open_part(sftp, encoding)
            self._send_chunks(remote, wire, encoding, guard)
            self._local(0, 0)                 # still the file that was hashed, now that its last chunk is out
        except _SourceChanged:
            _remove(sftp, self.part)          # what was sent is of no use
            raise self._error('send', 'source_changed', '本地文件在传输过程中被修改或替换，已停止并清理了设备上未完成的部分；请重新传输', False) from None
        finally:
            try:
                if remote is not None:
                    remote.close()
            except Exception:
                pass        # on a broken connection there is nothing left to close
        return self._finalize(device, guard, encoding)

    def _script(self, device, guard, kind, seconds, *arguments):
        """(word, checksum, note) a script answered with. The note is cleaned for showing."""
        guard.confirm(seconds + GUARD_MARGIN_SECONDS)
        try:
            text = _run_script(device, kind, seconds, *arguments)
        finally:
            guard.confirm()
            self._check_stop()
        word, checksum, note = target.verdict(text)
        if word == 'moved':
            raise self._error(self.stage, 'outside', '设备上的目标目录要经过符号链接才能到达（主目录本身是链接、而设备的 SFTP 不解析链接时也会这样），'
                              '或者在检查之后被移走了；传输已停止。%s' % self._leftover(), True)
        return word, checksum, target.shown(device.clean(note)) or '没有更多信息', text

    def _seconds(self, passes):
        """Time allowed for a script that reads the whole file ``passes`` times on the device."""
        return SCRIPT_SECONDS + passes * self.source['size'] // VERIFY_BYTES_PER_SECOND

    def _probe(self, device, guard):
        word, _, _, text = self._script(device, guard, 'probe', self._seconds(1), self.dest, self.source['size'], self.part)
        if word != 'probed':
            raise _Retry('设备没有完成传输前的检查')
        self.cleaned = True                   # no temporary file of another version of this destination is left there
        # A login script may print before ours does: the last value of a line is ours.
        fields = dict(line.partition('=')[::2] for line in text.splitlines() if '=' in line)
        return {'hash': fields.get('hash', '').strip(), 'gzip': fields.get('gzip') == 'yes', 'dest': fields.get('dest', '').strip()}

    def _encoding(self, has_gzip):
        """gzip or identity. A transfer that is being continued keeps the one it started with."""
        if self.record and (self.record['encoding'] == 'identity' or has_gzip):
            return self.record['encoding']
        sample = self._local(0, min(self.source['size'], COMPRESS_SAMPLE_BYTES))
        worth = bool(sample) and len(gzip.compress(sample, 6, mtime=0)) <= COMPRESS_WORTH * len(sample)
        encoding = 'gzip' if has_gzip and worth else 'identity'
        self.sent, self.record = 0, records.new(self.source, self.remote_path, encoding, self.dest, self.part)
        return encoding

    def _open_part(self, sftp, encoding):
        """Open the temporary file at the last position both sides agree on. Returns (file, position in it)."""
        info = target.lstat(sftp, self.part)
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise self._error('send', 'occupied', '设备上临时文件的位置被别的东西占用：' + self.part, False)
        size = info.st_size if info is not None else 0
        have = size if isinstance(size, int) and size > 0 else 0
        if encoding == 'identity':
            sent = wire = min(have, self.record['sent'])         # uncompressed: any length that arrived can be continued
            ends = []
        else:
            ends = [end for end in self.record['boundaries'] if end[1] <= have]
            sent, wire = ends[-1] if ends else (0, 0)
        if info is None:
            remote = sftp.open(self.part, 'wxb')                 # only if nothing is there: a link put there is not written through
            sftp.chmod(self.part, 0o600)
        else:
            remote = sftp.open(self.part, 'r+b')
            if have != wire:
                remote.truncate(wire)                            # drops the chunk that was cut off, or what cannot be continued
        self.sent = sent
        self.record = records.advanced(self.record, sent, wire, ends, path=self.dest, part=self.part, status='sending', error='')
        self._remember()
        if self.resumed_from is None:
            self.resumed_from = sent
        return remote, wire

    def _send_chunks(self, remote, wire, encoding, guard):
        size, most = self.source['size'], self.options['chunk_bytes']
        # After a broken connection the chunks start small: if it breaks again, little was in flight.
        # From there the size follows the link: a chunk the device confirms quickly lets the next be
        # twice as large, a slow one makes it half, between the small size and what the caller asked
        # for. A link that carries them gets its speed back, and what one more break can cost stays
        # a few seconds of sending.
        least = min(most, CAREFUL_CHUNK_BYTES)
        limit = least if self.careful else most
        while self.sent < size:
            self._check_stop()
            raw = self._local(self.sent, min(size - self.sent, limit))
            if not raw:
                raise _SourceChanged()        # the same size, yet nothing to read: not the file it was
            data = gzip.compress(raw, 6, mtime=0) if encoding == 'gzip' else raw
            began = time.monotonic()
            _write_confirmed(remote, wire, data)
            if self.careful:
                quick = time.monotonic() - began < QUICK_SECONDS
                limit = min(most, limit * 2) if quick else max(least, limit // 2)
            wire += len(data)
            self.wire_bytes += len(data)
            self.sent += len(raw)
            ends = [*self.record['boundaries'], [self.sent, wire]] if encoding == 'gzip' else []
            self.record = records.advanced(self.record, self.sent, wire, ends)
            self._remember()
            guard.confirm()
            self._tell('sending')

    def _finalize(self, device, guard, encoding):
        self.stage, self.finalizing = 'verify', True
        self._tell('verifying')
        want = self.source['sha256']
        word, got, note, _ = self._script(device, guard, 'finalize', self._seconds(3), self.part, self.dest, want, encoding,
                                          self.mode, target.temp_name(self.dest, target.UNPACKED, want))
        cleaned = '已清理设备上这次传输的内容，目标位置原有的文件没有动'
        if word == 'ok' and got == want:
            return self._result(False, encoding)
        if word == 'mismatch':
            raise self._error('verify', 'checksum', '校验值不一致，文件没有放到目标位置：本地 SHA-256 %s，设备上算出 %s。%s' % (want, got, cleaned), False)
        if word == 'damaged':
            raise self._error('verify', 'damaged', '传到设备上的压缩内容已损坏，解不开：%s。%s' % (note, cleaned), False)
        if word == 'full':
            raise self._error('verify', 'no_space', '设备上解压失败，多半是磁盘空间不够放下解压后的文件：%s。%s' % (note, self._leftover()), True)
        if word == 'unplaced':
            raise self._error('verify', 'unplaced', '校验值一致，但无法把文件放到目标位置：%s。%s' % (note, self._leftover()), True)
        if word == 'nohash':
            raise self._error('verify', 'no_checksum_tool', '设备没能算出校验值（校验工具不可用了），文件没有放到目标位置。%s' % self._leftover(), True)
        raise _Retry('设备没有完成核对（%s）' % (word or '没有答复'))

    def _result(self, already, encoding):
        self.record = None
        try:
            records.forget(self.record_file)
        except OSError:
            pass        # the file has arrived and is verified; a record that cannot be removed says so at the next call
        self.sent = self.source['size']
        result = {'path': self.dest, 'bytes': self.source['size'], 'sha256': self.source['sha256'], 'verified': True,
                  'already_present': already, 'resumed_from': self.resumed_from or 0, 'sent_bytes': self.wire_bytes,
                  'encoding': encoding, 'attempts': self.attempts, 'seconds': round(time.monotonic() - self.started, 3)}
        try:
            self._tell('done')
        except ProgressHandlerError as exc:
            exc.result = result
            raise
        return result


def _number(value, low, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < low:
        raise target.refuse('options', what + '无效')
    return value


def upload(connect, local_path, remote_path, record_dir, *, device_key, roots=DEFAULT_ROOTS, mode=0o644, on_progress=None,
           stop=None, chunk_bytes=CHUNK_BYTES, stall_seconds=STALL_SECONDS, retry_waits=RETRY_WAITS):
    """Put the local file at ``remote_path`` on a device and verify it there.

    ``connect()`` returns a new ``device_access.SSHDevice`` each time it is called (a transfer may
    need several connections). ``remote_path`` is ``~/dir/name`` below one of ``roots``.
    ``record_dir`` holds the resume records; ``device_key`` tells the caller's devices apart.
    ``on_progress(event)`` gets ``stage`` ('checking', 'sending', 'retrying', 'verifying', 'done'),
    ``sent`` and ``total`` bytes confirmed on the device, and ``attempt``. ``stop`` is an optional
    ``threading.Event``: once set the transfer ends, also while it waits for the device.

    Returns ``path`` (as the device sees it), ``bytes``, ``sha256``, ``verified`` (always True: a
    file that could not be verified raises), ``already_present``, ``resumed_from``, ``sent_bytes``
    (on the wire, in this call), ``encoding``, ``attempts`` and ``seconds``.

    Raises ``TransferError``, which says where it stopped and what is left on the device."""
    names = target.check_remote_path(remote_path, target.check_roots(roots))
    mode = target.mode_text(mode)
    if not isinstance(retry_waits, (tuple, list)):
        raise target.refuse('options', '重试间隔无效')
    options = {'stop': stop, 'on_progress': on_progress, 'chunk_bytes': int(_number(chunk_bytes, MIN_CHUNK_BYTES, '分块大小')),
               'stall_seconds': _number(stall_seconds, 0.1, '无进展时限'), 'retry_waits': tuple(_number(w, 0, '重试间隔') for w in retry_waits)}
    record_file = records.record_file(record_dir, device_key, remote_path)
    with _exclusive(record_file):
        return _Transfer(connect, _source(local_path), remote_path, names, mode, record_file, options).run()


def transfer_status(record_dir, device_key, remote_path):
    """Where an unfinished transfer stands, or None when there is none (never started, finished,
    rejected or given up). ``sent`` of ``total`` bytes are confirmed on the device."""
    target.path_names(remote_path)
    record_file = records.record_file(record_dir, device_key, remote_path)
    record = records.load(record_file)
    return records.summary(record, _running(record_file)) if record else None


def unfinished(record_dir, device_key):
    """Every transfer to one device that did not finish: what ``transfer_status`` says of each,
    with its ``remote_path``. Each of them has left a temporary file on the device."""
    return [records.summary(record, _running(entry)) for entry, record in records.of_device(record_dir, device_key)]


def discard(connect, remote_path, record_dir, *, device_key, roots=DEFAULT_ROOTS, stop=None, stall_seconds=STALL_SECONDS):
    """Give up an unfinished transfer: remove what it left on the device, then its local record.
    The file at the destination, if there is one, is not touched."""
    import paramiko
    names = target.check_remote_path(remote_path, target.check_roots(roots))
    record_file = records.record_file(record_dir, device_key, remote_path)
    kept = '传输记录保留着，可以稍后再清理'
    with _exclusive(record_file):
        try:
            device = connect()
        except device_access.ConnectError as exc:
            raise TransferError('connect', exc.reason, '连不上设备，没能清理它上面未完成的文件：%s。%s' % (exc, kept)) from None
        guard = _Guard(device, stop, _number(stall_seconds, 0.1, '无进展时限') + SCRIPT_SECONDS)
        try:
            dest = target.resolve(_sftp(device), names, create=False)
            if dest is not None and target.verdict(_run_script(device, 'clean', SCRIPT_SECONDS, dest))[0] != 'cleaned':
                raise TransferError('send', 'interrupted', '设备没有完成清理。' + kept)
        except (_Retry, OSError, EOFError, paramiko.SSHException) as exc:
            raise TransferError('connect', 'interrupted', '没能清理设备上未完成的文件：%s。%s' % (_problem(exc), kept)) from None
        finally:
            guard.finish()
            _close(device)
        records.forget(record_file)


# --- by saved connection: what the features built on this channel call -------------------------

def _records(platform):
    return Path(platform.store.root) / 'transfers'


def _login(platform, connection_id):
    try:
        platform.store.get('device_connections', connection_id)    # an unknown connection fails here, before any work
    except ValueError:
        raise target.refuse('device', '找不到这台机器人保存的 SSH 连接') from None
    return lambda: device_access.SSHDevice(*platform.ssh_edit.saved_login(connection_id))


def send_to_robot(platform, connection_id, local_path, remote_path, **options):
    """``upload`` to the robot of a saved SSH connection, with the login as it is saved at each new
    connection. The resume records live in the workspace (``transfers/``) and hold no login."""
    return upload(_login(platform, connection_id), local_path, remote_path, _records(platform), device_key=connection_id, **options)


def robot_transfer_status(platform, connection_id, remote_path):
    return transfer_status(_records(platform), connection_id, remote_path)


def robot_unfinished_transfers(platform, connection_id):
    return unfinished(_records(platform), connection_id)


def discard_robot_transfer(platform, connection_id, remote_path, **options):
    return discard(_login(platform, connection_id), remote_path, _records(platform), device_key=connection_id, **options)


def forget_robot_transfers(platform, connection_id):
    """Drop the local records of a connection that is being removed. Contacts nothing."""
    return records.forget_device(_records(platform), connection_id)
