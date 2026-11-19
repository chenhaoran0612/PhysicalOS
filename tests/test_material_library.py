"""Issue #33: the desktop relays material uploads to the cloud library.

Page → desktop backend (this computer) → a real cloud app (cloud/, local uvicorn,
SQLite, a temp directory as its library volume). The page never holds the cloud
session token; the backend adds it. Every file here is made up in the test.
"""
import hashlib
import json
import secrets
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

try:
    import cloud_fixture
except ImportError as exc:  # cloud dependencies not installed in this interpreter
    cloud_fixture = None
    SKIP = f'cloud dependencies not installed: {exc}'
else:
    SKIP = ''

CHUNK = 128 * 1024
LABELS = {'body_type': 'quadruped', 'vendor': '示例厂家', 'model': 'X1', 'version': '1.0'}
OCTET = 'application/octet-stream'


def pdf(text='example'):
    return f'%PDF-1.4\n1 0 obj\n<< /Title ({text}) >>\nendobj\ntrailer\n<< >>\n%%EOF\n'.encode()


def long_text(size):
    line = '关节限位说明，第 {} 行\n'
    out, total, i = [], 0, 0
    while total < size:
        piece = line.format(i).encode()
        out.append(piece)
        total += len(piece)
        i += 1
    return b''.join(out)


def sha(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipIf(cloud_fixture is None, SKIP)
class MaterialLibraryRelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer

        from brainos.server import Application, make_handler
        cls.cloud = cloud_fixture.CloudServer(models=(), library_chunk_bytes=CHUNK)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = Application(Path(cls.tmp.name), '')
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(cls.app))
        cls.httpd.daemon_threads = True
        cls.host = f'127.0.0.1:{cls.httpd.server_port}'
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.close()
        cls.tmp.cleanup()
        cls.cloud.stop()

    def setUp(self):
        """A fresh desktop workspace, signed in to a fresh, verified cloud account."""
        type(self).count += 1
        self.email = f'dev{self.count}@library.test'
        desk_secret = f'desk-{secrets.token_hex(8)}-7'       # the desktop account's own, made up per test
        token, self.user, _ = self.app.identity.sign_in({'email': self.email, 'password': desk_secret, 'name': 'Dev'},
                                                        register=True)
        self.cookie = 'brain_session=' + token
        self.cloud_user = self.cloud.register(self.email)
        self.ok('cloud/configure', {'base_url': self.cloud.url})
        self.ok('cloud/login', {'email': self.email, 'password': cloud_fixture.PASSWORD})

    # ------------------------------------------------------------ desktop HTTP
    def send(self, method, path, body=None, headers=None, cookie=None):
        merged = {'Host': self.host, 'Cookie': self.cookie if cookie is None else cookie, **(headers or {})}
        req = urllib.request.Request(f'http://{self.host}{path}', data=body, headers=merged, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def call(self, path, payload, **kwargs):
        return self.send('POST', '/api/' + path, json.dumps(payload).encode(),
                         {'Content-Type': 'application/json'}, **kwargs)

    def ok(self, path, payload):
        status, body = self.call(path, payload)
        self.assertEqual(status, 200, body)
        return body

    def put(self, upload_id, index, data, headers=None, **kwargs):
        merged = {'Content-Type': OCTET, **(headers or {})}
        return self.send('PUT', f'/api/library/chunk?upload={upload_id}&index={index}', data, merged, **kwargs)

    def start(self, filename, data, **fields):
        return self.call('library/upload/start', {'filename': filename, 'size': len(data), 'sha256': sha(data),
                                                  **LABELS, **fields})

    def stored_bytes(self, material_id):
        req = urllib.request.Request(f'{self.cloud.url}/api/v1/library/materials/{material_id}/content',
                                     headers={'Authorization': 'Bearer ' + self.cloud_user['token']})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=20) as r:
            return r.read()

    # ------------------------------------------------------------ tests
    def test_a_file_goes_from_the_page_to_the_cloud_in_chunks(self):
        data = long_text(2 * CHUNK + 999)
        status, upload = self.start('长说明.txt', data)
        self.assertEqual(status, 200, upload)
        self.assertEqual((upload['chunk_bytes'], upload['chunk_count'], upload['received']), (CHUNK, 3, []))
        for index in range(3):
            status, answer = self.put(upload['id'], index, data[index * CHUNK:(index + 1) * CHUNK])
            self.assertEqual(status, 200, answer)
            self.assertEqual(answer['received_count'], index + 1)
        material = self.ok('library/upload/complete', {'upload_id': upload['id']})
        self.assertEqual((material['filename'], material['status'], material['sha256']), ('长说明.txt', 'parsing', sha(data)))
        self.assertEqual(self.stored_bytes(material['id']), data)
        listing = self.ok('library/list', {})
        self.assertEqual([m['id'] for m in listing['materials']], [material['id']])
        self.assertEqual(listing['total'], 1)
        overview = self.ok('library/overview', {})
        self.assertTrue(overview['account']['logged_in'])
        self.assertIn('.tar.gz', overview['formats']['extensions'])
        self.assertEqual(overview['formats']['limits']['chunk_bytes'], CHUNK)
        self.assertEqual(overview['total'], 1)
        events = [e['action'] for e in self.app.workspace(self.user).store.list('events')]
        self.assertIn('library.uploaded', events)

    def test_an_interrupted_upload_continues_with_what_the_cloud_already_has(self):
        data = long_text(2 * CHUNK + 5)
        _, upload = self.start('long.txt', data)
        self.assertEqual(self.put(upload['id'], 1, data[CHUNK:2 * CHUNK])[0], 200)
        # The page was closed. It offers the same file again and gets the same upload back.
        status, again = self.start('long.txt', data)
        self.assertEqual(status, 200, again)
        self.assertEqual((again['id'], again['resumed'], again['received']), (upload['id'], True, [1]))
        self.assertEqual(self.ok('library/upload/status', {'upload_id': upload['id']})['received'], [1])
        for index in (0, 2):
            self.assertEqual(self.put(upload['id'], index, data[index * CHUNK:(index + 1) * CHUNK])[0], 200)
        material = self.ok('library/upload/complete', {'upload_id': upload['id']})
        self.assertEqual(self.stored_bytes(material['id']), data)

    def test_a_refusal_reaches_the_page_in_words_with_what_is_supported(self):
        status, body = self.start('setup.exe', b'MZ' + bytes(100))
        self.assertEqual((status, body['code']), (400, 'unsupported_type'))
        for word in ('Word', 'PDF', '.urdf', '.tar.gz', '.deb'):
            self.assertIn(word, body['error'])
        # A program renamed to .pdf gets as far as its first chunk.
        fake = b'\x7fELF' + bytes(300)
        _, upload = self.start('手册.pdf', fake)
        status, body = self.put(upload['id'], 0, fake)
        self.assertEqual((status, body['code']), (400, 'unsupported_type'))
        self.assertIn('PDF', body['error'])
        self.assertEqual(self.ok('library/list', {})['total'], 0)

    def test_a_duplicate_is_refused_and_a_material_can_be_deleted(self):
        data = pdf()
        _, upload = self.start('spec.pdf', data)
        self.assertEqual(self.put(upload['id'], 0, data)[0], 200)
        material = self.ok('library/upload/complete', {'upload_id': upload['id']})
        status, body = self.start('spec.pdf', data)
        self.assertEqual((status, body['code']), (400, 'material_exists'))
        self.assertEqual(self.ok('library/delete', {'material_id': material['id']}), {'deleted': True, 'id': material['id']})
        self.assertEqual(self.ok('library/list', {})['total'], 0)
        _, upload = self.start('spec.pdf', data)
        self.assertEqual(self.ok('library/upload/cancel', {'upload_id': upload['id']})['cancelled'], True)

    def test_listing_passes_only_known_filters(self):
        for name, fields in (('a.pdf', {}), ('b.pdf', {'body_type': 'manipulator', 'vendor': '另一家'})):
            data = pdf(name)
            _, upload = self.start(name, data, **fields)
            self.put(upload['id'], 0, data)
            self.ok('library/upload/complete', {'upload_id': upload['id']})
        names = lambda p: [m['filename'] for m in self.ok('library/list', p)['materials']]
        self.assertEqual(names({'body_type': 'manipulator'}), ['b.pdf'])
        self.assertEqual(names({'q': 'A.PDF'}), ['a.pdf'])
        self.assertEqual(sorted(names({'body_type': '', 'unknown&x=1': 'y', 'limit': 50})), ['a.pdf', 'b.pdf'])
        self.assertEqual(self.call('library/list', {'limit': 'all'})[0], 400)

    def test_the_chunk_route_needs_the_desktop_login_and_a_request_from_the_page(self):
        data = pdf()
        _, upload = self.start('spec.pdf', data)
        self.assertEqual(self.put(upload['id'], 0, data, cookie='')[0], 401)
        self.assertEqual(self.put(upload['id'], 0, data, cookie='brain_session=forged')[0], 401)
        self.assertEqual(self.put(upload['id'], 0, data, headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.put(upload['id'], 0, data, headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.put(upload['id'], 0, data, headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.put(upload['id'], 0, data, headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.put('../../admin', 0, data)[0], 400)
        self.assertEqual(self.put(upload['id'], 'x', data)[0], 400)
        self.assertEqual(self.put(upload['id'], -1, data)[0], 400)
        self.assertEqual(self.send('PUT', '/api/library/chunk', data, {'Content-Type': OCTET})[0], 400)
        self.assertEqual(self.send('PUT', '/api/state', data, {'Content-Type': OCTET})[0], 404)
        self.assertEqual(self.ok('library/upload/status', {'upload_id': upload['id']})['received'], [])
        self.assertEqual(self.put(upload['id'], 0, data)[0], 200)

    def test_ids_from_the_page_are_checked_before_they_go_into_a_cloud_address(self):
        for path, key in (('library/upload/status', 'upload_id'), ('library/upload/complete', 'upload_id'),
                          ('library/upload/cancel', 'upload_id'), ('library/delete', 'material_id')):
            for bad in ('../../admin/models', 'upl_', '', None, 5, 'mat_' + 'z' * 20):
                with self.subTest(path=path, bad=bad):
                    status, body = self.call(path, {key: bad})
                    self.assertEqual(status, 400, body)

    def test_the_cloud_session_token_never_reaches_the_page(self):
        token = self.cloud_token()
        self.assertTrue(token.startswith('nxs_'))
        data = pdf()
        _, upload = self.start('spec.pdf', data)
        answers = [upload, self.put(upload['id'], 0, data)[1], self.ok('library/upload/complete', {'upload_id': upload['id']}),
                   self.ok('library/overview', {}), self.ok('library/list', {})]
        self.assertNotIn(token, json.dumps(answers))

    def cloud_token(self):
        path = Path(self.app.workspace(self.user).store.root) / 'credentials' / 'cloud-session.json'
        return json.loads(path.read_text())['session_token']

    def test_without_a_cloud_login_the_page_is_told_to_sign_in(self):
        self.ok('cloud/logout', {})
        overview = self.ok('library/overview', {})
        self.assertFalse(overview['account']['logged_in'])
        self.assertEqual((overview['materials'], overview['formats']), ([], None))
        status, body = self.start('spec.pdf', pdf())
        self.assertEqual((status, body['code']), (400, 'cloud_login_required'))
        status, body = self.put('upl_' + '0' * 20, 0, b'x')
        self.assertEqual((status, body['code']), (400, 'cloud_login_required'))

    def test_a_cloud_login_that_ended_is_reported_as_such(self):
        data = long_text(CHUNK + 10)
        _, upload = self.start('long.txt', data)
        self.cloud.call('POST', '/auth/logout', {}, self.cloud_user_session())
        status, body = self.put(upload['id'], 0, data[:CHUNK])
        self.assertEqual((status, body['code']), (400, 'unauthorized'))
        self.assertIn('重新登录', body['error'])
        self.assertFalse(self.ok('library/overview', {})['account']['logged_in'])

    def cloud_user_session(self):
        """The desktop's own cloud session, as a fixture user (to end it from the cloud side)."""
        return {'token': self.cloud_token()}

    def test_an_unreachable_cloud_is_said_plainly(self):
        data = pdf()
        _, upload = self.start('spec.pdf', data)
        library = self.app.workspace(self.user).library
        settings = library.cloud.settings()
        library.cloud._save_settings({**settings, 'base_url': 'http://127.0.0.1:9'})
        token_file = Path(library.cloud.dir) / 'cloud-session.json'
        token_file.write_text(json.dumps({'base_url': 'http://127.0.0.1:9', 'session_token': self.cloud_token_raw(token_file)}))
        status, body = self.put(upload['id'], 0, data)
        self.assertEqual(status, 400, body)
        self.assertIn('无法连接云平台', body['error'])

    @staticmethod
    def cloud_token_raw(path):
        return json.loads(path.read_text())['session_token']

    def test_a_certificate_problem_is_not_presented_as_a_network_blip(self):
        import ssl
        from unittest import mock
        data = pdf()
        _, upload = self.start('spec.pdf', data)
        failing = mock.Mock()
        failing.open.side_effect = urllib.error.URLError(ssl.SSLCertVerificationError('certificate verify failed'))
        with mock.patch('brainos.material_library.urllib.request.build_opener', return_value=failing):
            status, body = self.put(upload['id'], 0, data)
        self.assertEqual(status, 400, body)
        self.assertIn('证书', body['error'])
        self.assertNotEqual(body.get('code'), 'cloud_unreachable')           # the page must not keep retrying

    def test_the_last_step_waits_long_enough_for_a_large_file(self):
        from unittest import mock

        from brainos import material_library
        library = self.app.workspace(self.user).library
        seen = {}

        def record(method, path, payload=None, **kwargs):
            seen.update(kwargs, path=path)
            return 201, {'id': 'mat_' + '0' * 20, 'filename': 'big.zip'}
        with mock.patch.object(library.cloud, '_request', side_effect=record):
            library.complete({'upload_id': 'upl_' + '0' * 20})
        self.assertEqual(seen['timeout'], material_library.COMPLETE_TIMEOUT_S)
        self.assertGreaterEqual(material_library.COMPLETE_TIMEOUT_S, 600)

    def test_a_request_without_a_length_or_with_too_much_is_refused(self):
        import http.client
        host, port = self.host.split(':')
        _, upload = self.start('spec.pdf', pdf())
        for extra in ({}, {'Content-Length': str(65 * 1024 * 1024)}, {'Content-Length': 'many'}, {'Content-Length': '0'}):
            with self.subTest(extra=extra):
                conn = http.client.HTTPConnection(host, int(port), timeout=20)
                conn.putrequest('PUT', f"/api/library/chunk?upload={upload['id']}&index=0")
                for name, value in {'Cookie': self.cookie, 'Content-Type': OCTET, **extra}.items():
                    conn.putheader(name, value)
                conn.endheaders()
                response = conn.getresponse()
                self.assertEqual(response.status, 400)
                self.assertIn('error', json.loads(response.read()))
                conn.close()
        self.assertEqual(self.ok('library/upload/status', {'upload_id': upload['id']})['received'], [])

    def test_counts_from_the_cloud_reach_the_page_only_as_numbers(self):
        from unittest import mock
        library = self.app.workspace(self.user).library
        hostile = {'materials': [{'id': 'mat_' + '0' * 20, 'filename': 'a.pdf', 'size': 3, 'extra': {'x': 1}}],
                   'total': '<img src=x>', 'limit': True, 'offset': -5}
        with mock.patch.object(library.cloud, '_request', return_value=(200, hostile)):
            listing = library.list({})
        self.assertEqual(listing, {'materials': [{'id': 'mat_' + '0' * 20, 'filename': 'a.pdf', 'size': 3}]})


if __name__ == '__main__':
    unittest.main()
