"""Material library (issue #33): the desktop's side of the cloud library.

Documents (Word, Excel, PDF, text, URDF) and driver packages are kept in the
cloud platform, labelled with body type, vendor and model, and version. The
page never talks to the cloud itself: the cloud session token lives only in the
workspace credential file (see cloud_account.py), so every call goes through
this module, which adds the token and passes on only the fields it knows.

* JSON operations (list, start, status, complete, cancel, delete) go through
  ``CloudAccount._request``.
* The bytes of a file travel chunk by chunk: the page sends
  ``PUT /api/library/chunk?upload=<id>&index=<n>`` (binary) and
  :func:`handle_chunk` relays that one chunk with its SHA-256. Nothing is
  written to this computer's disk on the way.

What is accepted, the size limits and who may upload are decided by the cloud
(cloud/neuxmind_cloud/library.py); this module does not repeat those rules.
"""
import hashlib
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .cloud_account import API_PREFIX, ERROR_MESSAGES, CloudError, _CheckedHTTPHandler, _CheckedHTTPSHandler
from .connector import NoRedirect
from .errors import CodedError, error_payload

CHUNK_PATH = '/api/library/chunk'
UPLOAD_ID = re.compile(r'upl_[0-9a-f]{20}')
MATERIAL_ID = re.compile(r'mat_[0-9a-f]{20}')
MAX_CHUNK_BYTES = 64 * 1024 * 1024      # the largest chunk size a cloud can be configured with
MAX_CHUNK_INDEX = 1_000_000
CHUNK_TIMEOUT_S = 120
# The last step makes the cloud read the whole file (SHA-256, type check): minutes for a 2 GB package.
COMPLETE_TIMEOUT_S = 900
OCTET_STREAM = 'application/octet-stream'
LIST_FILTERS = ('body_type', 'vendor', 'model', 'version', 'kind', 'format', 'status', 'q')
START_FIELDS = ('filename', 'size', 'sha256', 'body_type', 'vendor', 'model', 'version', 'description')
MATERIAL_KEYS = ('id', 'filename', 'kind', 'format', 'body_type', 'vendor', 'model', 'version', 'description',
                 'size', 'sha256', 'status', 'status_detail', 'created_at', 'updated_at')
# Issue #34: a material belongs to one device (a robot of this app), named when it is uploaded.
LIST_FILTERS += ('device_id',)
START_FIELDS += ('device_id', 'device_name')
MATERIAL_KEYS += ('device_id', 'device_name')
# Issue #51: ``kind: "model"`` asks for the upload to be kept as the robot model of its brand and model.
START_FIELDS += ('kind',)
# Issue #78: a material of the public library (every account sees it; only platform operators change it).
MATERIAL_KEYS += ('public', 'read_only')
UPLOAD_KEYS = ('id', 'status', 'resumed', 'filename', 'kind', 'format', 'size', 'sha256', 'chunk_bytes',
               'chunk_count', 'received_count', 'received_bytes', 'expires_at')
CHUNK_KEYS = ('upload_id', 'index', 'received_count', 'chunk_count', 'received_bytes')
# Refusals of the cloud that read better in the words of this page.
MESSAGES = {
    'forbidden': '你在这个组织里的角色只能查看和下载资料，不能上传或删除',
    'scope_missing': '当前凭据没有资料库的权限',
    'email_unverified': '云平台账户的邮箱尚未验证；验证后才能上传或删除资料（在“云平台账户”页完成验证）',
    'payload_too_large': '这一片数据超过了云平台允许的大小，请刷新页面后重新上传',
}


def _scalars(source, keys):
    """Only the listed keys, and only plain values: what the cloud answers is not passed on blindly."""
    if not isinstance(source, dict):
        raise CloudError('云平台返回格式无效')
    return {k: source[k] for k in keys if isinstance(source.get(k), (str, int, float, bool))}


def _counts(source, keys):
    """The listed keys that hold a non-negative whole number (totals, sizes, positions)."""
    return {k: source[k] for k in keys
            if isinstance(source.get(k), int) and not isinstance(source[k], bool) and source[k] >= 0}


def _model_formats(source):
    """What the cloud takes as a robot model (issue #51): its suffixes and size limit; None from an older cloud."""
    if not isinstance(source, dict):
        return None
    extensions = [e for e in source.get('extensions', []) if isinstance(e, str) and len(e) <= 16][:16] \
        if isinstance(source.get('extensions'), list) else []
    return {'extensions': extensions, **_counts(source, ('max_bytes',))} if extensions else None


def _upload_id(value):
    if not isinstance(value, str) or not UPLOAD_ID.fullmatch(value):
        raise ValueError('上传编号无效')
    return value


def _material_id(value):
    if not isinstance(value, str) or not MATERIAL_ID.fullmatch(value):
        raise ValueError('资料编号无效')
    return value


class MaterialLibrary:
    def __init__(self, platform):
        self.platform = platform
        self.cloud = platform.cloud

    # ------------------------------------------------------------- cloud calls
    def _require_login(self):
        if not self.cloud.summary()['logged_in']:
            raise CodedError('资料库保存在云平台；请先在“云平台账户”页登录', 'cloud_login_required')

    def _call(self, method, path, payload=None, timeout=None):
        self._require_login()
        try:
            if timeout is None:
                return self.cloud._request(method, path, payload)[1]
            return self.cloud._request(method, path, payload, timeout=timeout)[1]
        except CloudError as exc:
            raise self._reworded(exc) from None

    @staticmethod
    def _reworded(exc):
        message = MESSAGES.get(exc.code)
        return CloudError(message, exc.status, exc.code, exc.body) if message else exc

    def _put(self, path, data, digest):
        """One binary PUT to the cloud (``CloudAccount._request`` only speaks JSON)."""
        self._require_login()
        headers = {'Authorization': 'Bearer ' + self.cloud._token(), 'Content-Type': OCTET_STREAM,
                   'Accept': 'application/json', 'X-Chunk-SHA256': digest}
        request = urllib.request.Request(self.cloud.settings()['base_url'] + API_PREFIX + path, data=data,
                                         headers=headers, method='PUT')
        # Same rules as every other cloud call: no proxy, no redirect, the address policy on the real peer.
        opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}),
                                             _CheckedHTTPHandler(), _CheckedHTTPSHandler())
        try:
            with opener.open(request, timeout=CHUNK_TIMEOUT_S) as response:
                raw = response.read(200_001)
        except urllib.error.HTTPError as exc:
            body = {}
            try:
                parsed = json.loads(exc.read(200_001) or b'{}')
                body = parsed if isinstance(parsed, dict) else {}
            except (ValueError, OSError):
                pass
            code = body.get('code') if isinstance(body.get('code'), str) else None
            if exc.code == 401:
                self.cloud._drop_token()
                code = 'unauthorized'
            message = (MESSAGES.get(code) or ERROR_MESSAGES.get(code)
                       or (body.get('error') if isinstance(body.get('error'), str) else None))
            raise CloudError(message or f'云平台返回 HTTP {exc.code}', exc.code, code, body) from None
        except ssl.SSLError:
            raise CloudError('云平台的 HTTPS 证书校验失败，已停止连接') from None       # not something to retry
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                raise CloudError('云平台的 HTTPS 证书校验失败，已停止连接') from None
            raise self._unreachable() from None
        except (TimeoutError, OSError):
            raise self._unreachable() from None
        try:
            result = json.loads(raw) if len(raw) <= 200_000 else None
        except (UnicodeError, ValueError):
            result = None
        if not isinstance(result, dict):
            raise CloudError('云平台未返回有效 JSON')
        return result

    @staticmethod
    def _unreachable():
        return CloudError('无法连接云平台；这一片没有送到，网络恢复后可以继续上传', code='cloud_unreachable')

    # ------------------------------------------------------------- views
    @staticmethod
    def _upload(result):
        view = _scalars(result, UPLOAD_KEYS)
        received = result.get('received')
        view['received'] = [i for i in received if isinstance(i, int) and not isinstance(i, bool)] \
            if isinstance(received, list) else []
        return view

    @staticmethod
    def _formats(result):
        formats = [{**_scalars(f, ('id', 'label', 'kind')),
                    'extensions': [e for e in f.get('extensions', []) if isinstance(e, str)]}
                   for f in result.get('formats', []) if isinstance(f, dict)]
        limits = result.get('limits') if isinstance(result.get('limits'), dict) else {}
        return {'formats': formats, 'extensions': [e for f in formats for e in f['extensions']],
                'supported': result.get('supported') if isinstance(result.get('supported'), str) else '',
                'limits': _scalars(limits, ('document_bytes', 'package_bytes', 'org_quota_bytes', 'chunk_bytes')),
                'model': _model_formats(result.get('model'))}          # issue #51: what a robot model may be

    # ------------------------------------------------------------- operations
    def list(self, p=None):
        p = p or {}
        query = {k: p[k].strip() for k in LIST_FILTERS if isinstance(p.get(k), str) and p[k].strip()}
        for key in ('limit', 'offset'):
            if p.get(key) is not None:
                if isinstance(p[key], bool) or not isinstance(p[key], int) or p[key] < 0:
                    raise ValueError(f'{key} 须为非负整数')
                query[key] = p[key]
        result = self._call('GET', '/library/materials' + ('?' + urllib.parse.urlencode(query) if query else ''))
        materials = result.get('materials') if isinstance(result.get('materials'), list) else []
        return {'materials': [_scalars(m, MATERIAL_KEYS) for m in materials if isinstance(m, dict)],
                **_counts(result, ('total', 'limit', 'offset'))}

    def overview(self, p=None):
        """What the page needs to draw itself: account, accepted types with limits, the first page of materials."""
        account = self.cloud.summary()
        if not account['logged_in']:
            return {'account': account, 'formats': None, 'materials': [], 'total': 0}
        try:
            formats = self._formats(self._call('GET', '/library/formats'))
            listing = self.list(p)
        except CloudError as exc:
            if exc.code == 'unauthorized':          # the session ended: the page shows the sign-in notice
                return {'account': self.cloud.summary(), 'formats': None, 'materials': [], 'total': 0, 'error': str(exc)}
            raise
        return {'account': self.cloud.summary(), 'formats': formats, **listing}

    def start(self, p):
        body = {k: p[k] for k in START_FIELDS if p.get(k) is not None}
        return self._upload(self._call('POST', '/library/uploads', body))

    def status(self, p):
        return self._upload(self._call('GET', '/library/uploads/' + _upload_id(p.get('upload_id'))))

    def chunk(self, upload_id, index, data):
        """Relay one chunk as it came from the page; the cloud checks it against the digest sent with it."""
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= MAX_CHUNK_INDEX:
            raise ValueError('分片序号无效')
        path = f'/library/uploads/{_upload_id(upload_id)}/chunks/{index}'
        return _scalars(self._put(path, data, hashlib.sha256(data).hexdigest()), CHUNK_KEYS)

    def complete(self, p):
        material = _scalars(self._call('POST', f"/library/uploads/{_upload_id(p.get('upload_id'))}/complete", {},
                                       timeout=COMPLETE_TIMEOUT_S), MATERIAL_KEYS)
        self.platform.audit('library.uploaded', {k: material.get(k) for k in ('id', 'filename', 'size', 'sha256')})
        return material

    def cancel(self, p):
        return _scalars(self._call('DELETE', '/library/uploads/' + _upload_id(p.get('upload_id'))), ('cancelled', 'id'))

    def remove(self, p):
        result = _scalars(self._call('DELETE', '/library/materials/' + _material_id(p.get('material_id'))),
                          ('deleted', 'id'))
        self.platform.audit('library.deleted', {'id': result.get('id')})
        return result


def handle_chunk(handler, application):
    """``PUT /api/library/chunk?upload=<id>&index=<n>``: one chunk from the page, relayed to the cloud.

    The only request of the desktop API that is not JSON; the caller has
    already checked that it comes from the page itself (``Handler.trusted``)."""
    parsed = urllib.parse.urlparse(handler.path)
    if parsed.path != CHUNK_PATH:
        return handler.respond({'error': '未找到资源'}, 404)
    user = handler.user()
    if not user:
        return None
    if handler.headers.get('Content-Type', '').split(';')[0].strip().lower() != OCTET_STREAM:
        return handler.respond({'error': f'请使用 {OCTET_STREAM}'}, 415)
    try:
        length = int(handler.headers.get('Content-Length', '0'))
        if not 0 < length <= MAX_CHUNK_BYTES:
            raise ValueError('分片须为 1 字节到 64 MB')
        query = urllib.parse.parse_qs(parsed.query)
        upload_id = _upload_id((query.get('upload') or [''])[0])
        try:
            index = int((query.get('index') or [''])[0])
        except ValueError:
            raise ValueError('分片序号无效') from None
        data = handler.rfile.read(length)
        if len(data) != length:
            raise ValueError('这一片没有收全，请重发')
        return handler.respond(application.workspace(user).library.chunk(upload_id, index, data))
    except ValueError as exc:
        return handler.respond(error_payload(exc), 400)
    except Exception:
        return handler.respond({'error': '内部错误，这一片没有确认送达；可以继续上传', 'code': 'desktop_internal_error'}, 500)
