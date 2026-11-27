"""Model files and data files of the model library, kept on this computer (issue #71, docs/MODEL_HUB.md).

A file reaches the app in chunks: ``model-hub/upload/start`` says what is coming, each chunk is one
``PUT /api/model-hub/chunk?upload=<id>&index=<n>`` (binary, in order), ``model-hub/upload/complete``
checks the length and keeps the file under the SHA-256 of its content. Nothing leaves this computer.

A file is a record (kind ``hub_files``) and its content, ``<workspace>/model-hub/files/<sha256>``. No
path is ever built from what a person typed: an upload is named by a random id, a file by its record.
A file that no version and no set took within a day is removed when the workspace is next opened.
"""
import hashlib
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from .errors import CodedError, error_payload

KIND = 'hub_files'
CHUNK_PATH = '/api/model-hub/chunk'
FILE_PATH = '/api/model-hub/file'
OCTET_STREAM = 'application/octet-stream'
CHUNK_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024
MAX_NAME = 200
UNCLAIMED_S = 24 * 3600
MAX_UPLOADS = 8                 # uploads under way at once, per account
UPLOAD_IDLE_S = 3600            # an upload nothing arrived for is dropped
FREE_MARGIN_BYTES = 512 * 1024 * 1024
READ_BLOCK = 1 << 20
UPLOAD_ID = re.compile(r'^upload-[0-9a-f]{32}$')
CONTROL = re.compile(r'[\x00-\x1f\x7f]')


def _name(value):
    """The last part of a file name a person's computer gave, without control characters."""
    text = CONTROL.sub('', value if isinstance(value, str) else '').replace('\\', '/').rsplit('/', 1)[-1].strip()
    if not text or text in ('.', '..'):
        raise ValueError('请选择一个文件')
    return text[:MAX_NAME]


def _now():
    return datetime.now(timezone.utc).isoformat()


class LocalFiles:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store
        self.root = Path(self.store.root) / 'model-hub'
        self.lock = threading.Lock()
        self.uploads = {}                       # upload id → {'filename', 'size', 'count', 'bytes', 'digest'}

    def _dir(self, name):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    def _upload(self, upload_id):
        found = self.uploads.get(upload_id) if isinstance(upload_id, str) and UPLOAD_ID.match(upload_id) else None
        if found is None:
            raise CodedError('这次上传已经不在了（应用重新启动过，或者已经取消），请重新选择文件', 'hub_upload_unknown')
        return found

    # ------------------------------------------------------------ uploading
    def start(self, p):
        """Begin an upload (route ``model-hub/upload/start``): ``filename`` and ``size`` in bytes."""
        name, size = _name(p.get('filename')), p.get('size')
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError('文件是空的')
        if size > MAX_FILE_BYTES:
            raise ValueError(f'文件太大：一个文件最多 {MAX_FILE_BYTES // 1024 ** 3} GB')
        upload_id = 'upload-' + secrets.token_hex(16)
        with self.lock:
            for key in [k for k, u in self.uploads.items() if time.monotonic() - u['at'] > UPLOAD_IDLE_S]:
                self._drop(key)
            if len(self.uploads) >= MAX_UPLOADS:
                raise CodedError('同时在上传的文件太多了，等它们传完再试', 'hub_upload_busy')
            under_way = sum(u['size'] - u['bytes'] for u in self.uploads.values())
            if shutil.disk_usage(self._dir('uploads')).free < size + under_way + FREE_MARGIN_BYTES:
                raise CodedError('这台电脑的磁盘空间不够放这个文件', 'hub_upload_space')
            self.uploads[upload_id] = {'filename': name, 'size': size, 'count': 0, 'bytes': 0, 'digest': hashlib.sha256(), 'at': time.monotonic()}
            (self._dir('uploads') / upload_id).write_bytes(b'')
        return {'upload_id': upload_id, 'chunk_bytes': CHUNK_BYTES, 'chunk_count': -(-size // CHUNK_BYTES)}

    def _drop(self, upload_id):
        self.uploads.pop(upload_id, None)
        (self._dir('uploads') / upload_id).unlink(missing_ok=True)

    def chunk(self, upload_id, index, data):
        """Append chunk ``index`` (the next one, or the last one again after a lost answer)."""
        with self.lock:
            upload = self._upload(upload_id)
            if index == upload['count'] - 1 and upload.get('last') == len(data):
                return {'received_count': upload['count'], 'received_bytes': upload['bytes']}       # the same chunk sent again
            if index != upload['count']:
                raise CodedError('上传的顺序乱了，请重新选择文件上传', 'hub_upload_order')
            if upload['bytes'] + len(data) > upload['size'] or len(data) > CHUNK_BYTES:
                raise CodedError('收到的内容比文件本身长，请重新选择文件上传', 'hub_upload_size')
            # Written at the place this chunk belongs: what a failed write left behind is overwritten, not kept.
            with open(self._dir('uploads') / upload_id, 'r+b') as out:
                out.seek(upload['bytes'])
                out.truncate()
                out.write(data)
            upload['digest'].update(data)
            upload.update(count=upload['count'] + 1, bytes=upload['bytes'] + len(data), last=len(data), at=time.monotonic())
            return {'received_count': upload['count'], 'received_bytes': upload['bytes']}

    def complete(self, p):
        """Keep an upload as a file (route ``model-hub/upload/complete``); returns the file's record."""
        with self.lock:
            upload = self._upload(p.get('upload_id'))
            part = self._dir('uploads') / p['upload_id']
            if upload['bytes'] != upload['size'] or part.stat().st_size != upload['size']:
                raise CodedError('文件没有传完，请重新选择文件上传', 'hub_upload_size')
            del self.uploads[p['upload_id']]
            sha256 = upload['digest'].hexdigest()
            target = self._dir('files') / sha256
            if target.exists() and target.stat().st_size == upload['size']:
                part.unlink(missing_ok=True)                    # the same content is kept once
            else:
                os.replace(part, target)
                target.chmod(0o600)
            record = {'id': 'file-' + secrets.token_hex(12), 'filename': upload['filename'], 'size': upload['size'],
                      'sha256': sha256, 'created_at': _now()}
            self.store.put(KIND, record)                        # under the lock: remove() of the same content waits for it
        return dict(record)

    def cancel(self, p):
        """Drop an upload that was begun (route ``model-hub/upload/cancel``)."""
        with self.lock:
            known = isinstance(p.get('upload_id'), str) and p['upload_id'] in self.uploads
            if known:
                self._drop(p['upload_id'])
        return {'cancelled': known}

    # ------------------------------------------------------------ kept files
    def record(self, file_id):
        try:
            if not isinstance(file_id, str):
                raise ValueError('file')
            return self.store.get(KIND, file_id)
        except ValueError:
            raise CodedError('没有找到这个文件，请重新上传', 'hub_file_unknown') from None

    def view(self, file_id):
        record = self.record(file_id)
        return {key: record[key] for key in ('id', 'filename', 'size', 'sha256', 'created_at')}

    def path(self, file_id):
        """``(path, filename, size)`` of a kept file."""
        record = self.record(file_id)
        path = self.root / 'files' / record['sha256']
        if not re.fullmatch(r'[0-9a-f]{64}', record['sha256']) or not path.is_file():
            raise CodedError('这个文件的内容已经不在这台电脑上了', 'hub_file_unknown')
        return path, record['filename'], record['size']

    def remove(self, file_ids):
        """Forget files; the content goes when no other file has the same."""
        with self.lock:
            for file_id in file_ids:
                try:
                    record = self.store.get(KIND, file_id)
                except ValueError:
                    continue
                self.store.delete(KIND, file_id)
                if not any(other['sha256'] == record['sha256'] for other in self.store.list(KIND)):
                    (self.root / 'files' / record['sha256']).unlink(missing_ok=True)

    def sweep(self, claimed):
        """Remove what nobody took: unfinished uploads of an earlier run, files no version or set names."""
        uploads = self.root / 'uploads'
        if uploads.is_dir():
            for part in uploads.iterdir():
                if part.name not in self.uploads:
                    part.unlink(missing_ok=True)
        limit = time.time() - UNCLAIMED_S
        stale = [f['id'] for f in self.store.list(KIND) if f['id'] not in claimed
                 and datetime.fromisoformat(f['created_at']).timestamp() < limit]
        self.remove(stale)
        return len(stale)


def handle_chunk(handler, application):
    """``PUT /api/model-hub/chunk?upload=<id>&index=<n>``: one chunk of a file from the page.

    The caller has already checked that the request comes from the page itself (``Handler.trusted``)."""
    parsed = urllib.parse.urlparse(handler.path)
    user = handler.user()
    if not user:
        return None
    if handler.headers.get('Content-Type', '').split(';')[0].strip().lower() != OCTET_STREAM:
        return handler.respond({'error': f'请使用 {OCTET_STREAM}'}, 415)
    try:
        length = int(handler.headers.get('Content-Length', '0'))
        if not 0 < length <= CHUNK_BYTES:
            raise ValueError(f'一片需要是 1 字节到 {CHUNK_BYTES // 1024 ** 2} MB')
        query = urllib.parse.parse_qs(parsed.query)
        try:
            index = int((query.get('index') or [''])[0])
        except ValueError:
            raise ValueError('分片序号无效') from None
        data = handler.rfile.read(length)
        if len(data) != length:
            raise ValueError('这一片没有收全，请重发')
        return handler.respond(application.workspace(user).model_hub.files.chunk((query.get('upload') or [''])[0], index, data))
    except ValueError as exc:
        return handler.respond(error_payload(exc), 400)
    except Exception:
        return handler.respond({'error': '内部错误，这一片没有确认收到；可以重新上传', 'code': 'desktop_internal_error'}, 500)


def handle_download(handler, application, user):
    """``GET /api/model-hub/file?id=<file id>``: a kept file, sent as a download."""
    wanted = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query).get('id', [''])[0]
    try:
        path, name, size = application.workspace(user).model_hub.files.path(wanted)
    except ValueError as exc:
        return handler.respond(error_payload(exc), 404)
    handler.send_response(200)
    handler.send_header('Content-Type', OCTET_STREAM)
    handler.send_header('Content-Length', str(size))
    handler.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(name, safe=''))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.end_headers()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(READ_BLOCK), b''):
            handler.wfile.write(block)
    return None
