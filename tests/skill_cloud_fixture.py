"""A stand-in for the cloud as issue #43's console reads it. Nothing here reaches a real server.

``FakeSkillCloud`` answers, on this computer only and behind a session token made up per instance:

* the skill library of docs/CLOUD_SKILLS.md section 1 (``/api/v1/skills…``). The digest of a release
  is computed the way the robot's management layer does at install
  (``neuxmind_runtime.skills.package_digest`` over the validated ``skill.json`` and the files), not
  with the console's own function;
* the part of the material library a bundle needs (section 2): chunked upload with ``kind:
  "bundle"``, the record of a material, its content with ``Range``, the list filtered by kind and
  device.

Faults a test can switch on: ``tamper`` (a function applied to a package before it is handed out),
``failures`` ((status, body) pairs answered to the next requests), ``offline``.
"""
import hashlib
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

from brainos.httpd import FastBindHTTPServer
from neuxmind_runtime.skills import package_digest
from neuxmind_spec import capabilities as spec_caps
from neuxmind_spec import manifest as spec_manifest

PREFIX = '/api/v1'
RANGE = re.compile(r'bytes=(\d+)-(\d*)')
BUNDLE_SUFFIXES = ('.tar.gz', '.tgz')


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Refusal(Exception):
    def __init__(self, status, code, error, **more):
        super().__init__(error)
        self.status, self.body = status, {'error': error, 'code': code, **more}


class FakeSkillCloud:
    def __init__(self, chunk_bytes=64 * 1024, quota=500):
        self.token = 'nxs_' + secrets.token_hex(16)
        self.chunk_bytes, self.quota = chunk_bytes, quota
        self.releases = {}              # (skill id, version) -> {'release', 'package'}
        self.uploads, self.materials = {}, {}
        self.failures, self.tamper, self.offline = [], None, False
        self.requests = []              # (method, path) of every request that was answered
        self.lock = threading.Lock()
        owner = self

        class Server(FastBindHTTPServer):
            daemon_threads = True

            def verify_request(self, request, client_address):
                return not owner.offline

            def handle_error(self, request, client_address):
                pass                    # a connection a test cut on purpose

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, fmt, *args):
                pass

            def _any(self):
                owner.answer(self)

            do_GET = do_POST = do_PUT = do_DELETE = _any

        self.httpd = Server(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    # --- what a test prepares and reads -------------------------------------------------------

    def sign_in(self, platform):
        """Point a workspace at this stand-in, as if its user had signed in to the cloud."""
        platform.cloud._save_settings({'base_url': self.url, 'email': 'dev@fixture.test', 'email_verified': True})
        platform.cloud._store_token(self.token)

    def add_bundle(self, filename, data, device_id, device_name='测试机器人', status='usable', kind='bundle', **more):
        """A material as the cloud would describe it, without going through an upload."""
        material_id = 'mat_' + secrets.token_hex(10)
        view = {'id': material_id, 'filename': filename, 'kind': kind, 'format': 'tar.gz', 'body_type': 'quadruped',
                'vendor': '机器人上的程序', 'model': 'walk', 'version': '1', 'description': '', 'size': len(data),
                'sha256': sha(data), 'status': status, 'status_detail': '', 'device_id': device_id,
                'device_name': device_name, 'created_by': 'usr_fixture', 'created_at': time.time(), 'updated_at': time.time(), **more}
        self.materials[material_id] = {'view': view, 'data': data}
        return material_id

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # --- plumbing -----------------------------------------------------------------------------

    @staticmethod
    def _send(handler, status, body, headers=()):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        handler.send_response(status)
        handler.send_header('Content-Type', 'application/octet-stream' if isinstance(body, bytes) else 'application/json')
        handler.send_header('Content-Length', str(len(raw)))
        for name, value in headers:
            handler.send_header(name, value)
        handler.end_headers()
        handler.wfile.write(raw)

    def answer(self, handler):
        parsed = urlparse(handler.path)
        length = int(handler.headers.get('Content-Length') or 0)
        raw = handler.rfile.read(length) if length else b''
        if handler.headers.get('Authorization') != 'Bearer ' + self.token:
            return self._send(handler, 401, {'error': 'not signed in', 'code': 'unauthorized'})
        with self.lock:
            self.requests.append((handler.command, parsed.path))
            failure = self.failures.pop(0) if self.failures else None
        if failure:
            return self._send(handler, *failure)
        if not parsed.path.startswith(PREFIX + '/'):
            return self._send(handler, 404, {'error': 'no such route', 'code': 'not_found'})
        parts = [unquote(x) for x in parsed.path[len(PREFIX) + 1:].split('/')]
        try:
            if parts[0] == 'skills':
                status, body = self._skills(handler.command, parts[1:], raw)
            elif parts[0] == 'library':
                answered = self._library(handler, parts[1:], raw, parse_qs(parsed.query))
                if answered is None:
                    return None
                status, body = answered
            else:
                raise Refusal(404, 'not_found', 'no such route')
        except Refusal as exc:
            status, body = exc.status, exc.body
        return self._send(handler, status, body)

    @staticmethod
    def _json(raw):
        try:
            body = json.loads(raw or b'{}')
        except ValueError:
            raise Refusal(400, 'bad_request', 'not JSON') from None
        if not isinstance(body, dict):
            raise Refusal(400, 'bad_request', 'not an object')
        return body

    # --- the skill library --------------------------------------------------------------------

    def _release(self, package, notes):
        files = package.get('files')
        if not isinstance(files, dict) or not files or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
            raise Refusal(400, 'invalid_skill_package', 'files 须为 路径 → 文本')
        try:
            spec = spec_manifest.validate_skill_package({k: v for k, v in package.items() if k != 'files'})
        except (ValueError, KeyError, TypeError) as exc:
            raise Refusal(400, 'invalid_skill_package', str(exc)) from None
        caps = [r['id'] for r in spec['requires'] + spec['optional']]
        motion = any(c.startswith('x.') or spec_caps.CAPABILITIES[c].safety != 'none' for c in caps)
        sizes = [{'path': path, 'size': len(text.encode())} for path, text in sorted(files.items())]
        return {'skill_id': spec['id'], 'version': spec['version'], 'title': spec['title'], 'description': spec['description'],
                'digest': package_digest({'package': spec, 'files': files}), 'requires': [r['id'] for r in spec['requires']],
                'optional': [r['id'] for r in spec['optional']], 'parameters': spec['parameters'], 'motion': motion,
                'timeout_s': spec['timeout_s'], 'files': sizes, 'size': sum(f['size'] for f in sizes), 'notes': notes,
                'created_by': 'dev@fixture.test', 'created_at': time.time()}

    def _publish(self, raw):
        body = self._json(raw)
        if not isinstance(body.get('package'), dict):
            raise Refusal(400, 'invalid_skill_package', 'package 须为对象')
        release = self._release(body['package'], body.get('notes') or '')
        release['product'] = self._product(body)                # issue #48: the brand and model it is for, or None
        key = (release['skill_id'], release['version'])
        held = self.releases.get(key)
        if held:
            if held['release']['digest'] != release['digest']:
                raise Refusal(409, 'skill_version_exists', '%s@%s 已存在且内容不同' % key)
            if release['product'] and held['release']['product'] and not self._same(release['product'], held['release']['product']):
                raise Refusal(409, 'skill_other_model', '%s@%s 已经发布给另一个品牌型号' % key)
            held['release']['product'] = held['release']['product'] or release['product']
            return 200, {**held['release'], 'existing': True}
        if len(self.releases) >= self.quota:
            raise Refusal(409, 'skill_quota_exceeded', '份数已到上限')
        self.releases[key] = {'release': release, 'package': body['package']}
        return 201, release

    @staticmethod
    def _product(body, required=False):
        vendor, model = (' '.join(str(body.get(k) or '').split()) for k in ('vendor', 'model'))
        if bool(vendor) != bool(model) or (required and not vendor):
            raise Refusal(400, 'invalid_product', '品牌和型号要一起给')
        return {'vendor': vendor, 'model': model} if vendor else None

    @staticmethod
    def _same(a, b):
        return all(a[k].casefold() == b[k].casefold() for k in ('vendor', 'model'))

    def _of(self, skill_id):
        found = sorted((held['release'] for key, held in self.releases.items() if key[0] == skill_id),
                       key=lambda r: r['created_at'], reverse=True)
        if not found:
            raise Refusal(404, 'skill_not_found', '没有这个技能')
        return found

    def _held(self, skill_id, version):
        held = self.releases.get((skill_id, version))
        if not held:
            raise Refusal(404, 'skill_not_found', '没有这个版本')
        return held

    def _skills(self, method, parts, raw):
        if method == 'POST' and parts == ['releases']:
            return self._publish(raw)
        if method == 'GET' and not parts:
            rows = []
            for skill_id in sorted({key[0] for key in self.releases}):
                found = self._of(skill_id)
                rows.append({'id': skill_id, 'title': found[0]['title'], 'description': found[0]['description'],
                             'latest': found[0], 'versions': [r['version'] for r in found]})
            return 200, {'skills': rows}
        if method == 'GET' and len(parts) == 1:
            return 200, {'id': parts[0], 'releases': self._of(parts[0])}
        if len(parts) >= 3 and parts[1] == 'releases':
            held = self._held(parts[0], parts[2])
            if method == 'GET' and len(parts) == 3:
                return 200, held['release']
            if method == 'GET' and parts[3:] == ['package']:
                package = json.loads(json.dumps(held['package']))
                if self.tamper:
                    package = self.tamper(package) or package
                return 200, {'release': held['release'], 'package': package}
            if method == 'PUT' and parts[3:] == ['product']:
                product = self._product(self._json(raw), required=True)
                if held['release']['product'] and not self._same(product, held['release']['product']):
                    raise Refusal(409, 'skill_product_set', '这个版本已经属于另一个品牌型号')
                held['release']['product'] = held['release']['product'] or product
                return 200, held['release']
            if method == 'DELETE' and len(parts) == 3:
                del self.releases[(parts[0], parts[2])]
                return 200, {'deleted': True}
        raise Refusal(404, 'not_found', 'no such route')

    # --- the material library, as far as a bundle goes ----------------------------------------

    def _upload_view(self, upload, resumed=False):
        received = sorted(upload['chunks'])
        return {'id': upload['id'], 'status': 'open', 'resumed': resumed, 'filename': upload['fields']['filename'],
                'kind': upload['fields']['kind'], 'format': 'tar.gz', 'size': upload['fields']['size'],
                'sha256': upload['fields']['sha256'], 'chunk_bytes': self.chunk_bytes, 'chunk_count': upload['count'],
                'received': received, 'received_count': len(received),
                'received_bytes': sum(len(upload['chunks'][i]) for i in received), 'expires_at': time.time() + 3600}

    def _start(self, raw):
        body = self._json(raw)
        name = body.get('filename')
        if body.get('kind') != 'bundle':
            raise Refusal(400, 'bad_request', '这个替身只接收 kind=bundle')
        if not isinstance(name, str) or not name.lower().endswith(BUNDLE_SUFFIXES):
            raise Refusal(415, 'unsupported_type', 'bundle 只接受 .tar.gz / .tgz')
        if not isinstance(body.get('device_id'), str) or not body['device_id']:
            raise Refusal(400, 'bad_request', 'bundle 必须绑定一台设备')
        for key in ('body_type', 'vendor', 'model', 'version', 'sha256'):
            if not isinstance(body.get(key), str) or not body[key]:
                raise Refusal(400, 'bad_request', '缺少 ' + key)
        if any(m['view']['sha256'] == body['sha256'] and m['view']['device_id'] == body['device_id'] for m in self.materials.values()):
            raise Refusal(409, 'material_exists', '已经有这份资料')
        for upload in self.uploads.values():
            if upload['fields']['sha256'] == body['sha256']:
                return 200, self._upload_view(upload, resumed=True)
        upload = {'id': 'upl_' + secrets.token_hex(10), 'fields': body, 'chunks': {},
                  'count': max(1, -(-body['size'] // self.chunk_bytes))}
        self.uploads[upload['id']] = upload
        return 201, self._upload_view(upload)

    def _complete(self, upload):
        if len(upload['chunks']) != upload['count']:
            raise Refusal(409, 'chunks_missing', '还有分片没到')
        data = b''.join(upload['chunks'][i] for i in range(upload['count']))
        fields = upload['fields']
        if sha(data) != fields['sha256'] or len(data) != fields['size']:
            raise Refusal(409, 'digest_mismatch', '内容和校验值不一致')
        del self.uploads[upload['id']]
        labels = {key: fields[key] for key in ('body_type', 'vendor', 'model', 'version')}
        material_id = self.add_bundle(fields['filename'], data, fields['device_id'], fields.get('device_name') or '',
                                      description=fields.get('description') or '', **labels)
        return 201, self.materials[material_id]['view']

    def _uploads(self, handler, parts, raw):
        method = handler.command
        if method == 'POST' and not parts:
            return self._start(raw)
        upload = self.uploads.get(parts[0]) if parts else None
        if upload is None:
            raise Refusal(404, 'upload_lost', '没有这次上传')
        if method == 'GET' and len(parts) == 1:
            return 200, self._upload_view(upload)
        if method == 'DELETE' and len(parts) == 1:
            del self.uploads[upload['id']]
            return 200, {'cancelled': True, 'id': upload['id']}
        if method == 'PUT' and len(parts) == 3 and parts[1] == 'chunks':
            if handler.headers.get('X-Chunk-SHA256') != sha(raw):
                raise Refusal(400, 'chunk_digest_mismatch', '这一片的校验值不对')
            upload['chunks'][int(parts[2])] = raw
            view = self._upload_view(upload)
            return 200, {'upload_id': upload['id'], 'index': int(parts[2]), 'received_count': view['received_count'],
                         'chunk_count': upload['count'], 'received_bytes': view['received_bytes']}
        if method == 'POST' and parts[1:] == ['complete']:
            return self._complete(upload)
        raise Refusal(404, 'not_found', 'no such route')

    def _library(self, handler, parts, raw, query):
        method = handler.command
        if parts[0] == 'uploads':
            return self._uploads(handler, parts[1:], raw)
        if parts[0] != 'materials':
            raise Refusal(404, 'not_found', 'no such route')
        if method == 'GET' and len(parts) == 1:
            rows = [m['view'] for m in self.materials.values()
                    if all(m['view'].get(key) == value[0] for key, value in query.items() if key in ('kind', 'device_id', 'status'))]
            rows.sort(key=lambda view: view['created_at'], reverse=True)
            return 200, {'materials': rows, 'total': len(rows), 'limit': 200, 'offset': 0}
        material = self.materials.get(parts[1])
        if material is None:
            raise Refusal(404, 'not_found', '没有这份资料')
        if method == 'GET' and len(parts) == 2:
            return 200, material['view']
        if method == 'DELETE' and len(parts) == 2:
            del self.materials[parts[1]]
            return 200, {'deleted': True, 'id': parts[1]}
        if method == 'GET' and parts[2:] == ['content']:
            return self._content(handler, material)
        raise Refusal(404, 'not_found', 'no such route')

    def _content(self, handler, material):
        data, match = material['data'], RANGE.fullmatch(handler.headers.get('Range') or '')
        headers = [('X-Checksum-SHA256', material['view']['sha256'])]
        if not match:
            self._send(handler, 200, data, headers)
            return None
        start = int(match.group(1))
        if start >= len(data):
            self._send(handler, 416, b'', [('Content-Range', 'bytes */%d' % len(data))])
            return None
        self._send(handler, 206, data[start:], [*headers, ('Content-Range', 'bytes %d-%d/%d' % (start, len(data) - 1, len(data)))])
        return None
