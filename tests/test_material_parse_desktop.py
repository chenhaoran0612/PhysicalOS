"""Issue #34: the desktop relays parsing results of library materials (read, edit, confirm, parse again).

Page → desktop backend → a real cloud app (cloud/, local uvicorn, SQLite). The
cloud's parse worker is run by the test (no thread). Every file is made up here.
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

LABELS = {'body_type': 'manipulator', 'vendor': '示例厂家', 'model': 'A1', 'version': '1.0'}
DEVICE = {'device_id': 'robot-0123456789ab', 'device_name': '示例机械臂'}
URDF = (b'<?xml version="1.0"?><robot name="demo"><link name="base"/><link name="arm"/>'
        b'<joint name="j1" type="revolute"><parent link="base"/><child link="arm"/>'
        b'<limit lower="-1" upper="1" velocity="2" effort="3"/></joint>'
        b'<joint name="j2" type="revolute"><parent link="base"/><child link="arm"/></joint></robot>')


def sha(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipIf(cloud_fixture is None, SKIP)
class MaterialParseRelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer

        from brainos.server import Application, make_handler
        cls.cloud = cloud_fixture.CloudServer(models=())
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
        type(self).count += 1
        self.email = f'parse{self.count}@library.test'
        desk_secret = f'desk-{secrets.token_hex(8)}-7'
        token, self.user, _ = self.app.identity.sign_in({'email': self.email, 'password': desk_secret, 'name': 'Dev'},
                                                        register=True)
        self.cookie = 'brain_session=' + token
        self.cloud.register(self.email)
        self.ok('cloud/configure', {'base_url': self.cloud.url})
        self.ok('cloud/login', {'email': self.email, 'password': cloud_fixture.PASSWORD})

    def call(self, path, payload, cookie=None):
        req = urllib.request.Request(f'http://{self.host}/api/{path}', data=json.dumps(payload).encode(), method='POST',
                                     headers={'Host': self.host, 'Cookie': cookie or self.cookie,
                                              'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def ok(self, path, payload):
        status, body = self.call(path, payload)
        self.assertEqual(status, 200, body)
        return body

    def upload(self, filename, data, **fields):
        upload = self.ok('library/upload/start', {'filename': filename, 'size': len(data), 'sha256': sha(data),
                                                  **LABELS, **fields})
        req = urllib.request.Request(f'http://{self.host}/api/library/chunk?upload={upload["id"]}&index=0', data=data,
                                     method='PUT', headers={'Host': self.host, 'Cookie': self.cookie,
                                                            'Content-Type': 'application/octet-stream'})
        with urllib.request.urlopen(req, timeout=30) as r:
            self.assertEqual(r.status, 200)
        return self.ok('library/upload/complete', {'upload_id': upload['id']})

    def parse(self):
        from neuxmind_cloud import material_parse
        context = self.cloud.app.state.ctx
        for _ in range(5):
            material_parse.run_pending(context.db, context.settings)

    def test_a_device_material_is_parsed_edited_and_confirmed_through_the_desktop(self):
        material = self.upload('arm.urdf', URDF, **DEVICE)
        self.assertEqual((material.get('device_id', ''), material.get('device_name', '')), ('', ''))   # issue #51: its brand and model's
        self.parse()
        listing = self.ok('library/list', {'vendor': LABELS['vendor'], 'model': LABELS['model'].lower()})
        self.assertEqual([(m['id'], m['status']) for m in listing['materials']], [(material['id'], 'failed')])
        self.assertEqual(self.ok('library/list', {'vendor': LABELS['vendor'], 'model': 'A2'})['materials'], [])
        result = self.ok('library/parse', {'material_id': material['id']})
        self.assertEqual((result['kind'], result['status']), ('urdf', 'failed'))
        self.assertIn('关节 j2', result['status_detail'])
        joints = [{**j, 'lower': -2, 'upper': 2, 'velocity': 1.5, 'effort': 4} if j['name'] == 'j2' else j
                  for j in result['values']['joints']]
        status, refused = self.call('library/parse/save', {'material_id': material['id'], 'revision': result['revision'] - 1,
                                                           'values': {'joints': joints}})
        self.assertEqual((status, refused['code'], refused['device_status']), (400, 'parse_changed', 409))
        done = self.ok('library/parse/confirm', {'material_id': material['id'], 'revision': result['revision'],
                                                 'values': {'joints': joints}})
        self.assertEqual((done['status'], done['confirmed_by']), ('usable', self.email))
        events = [e['action'] for e in self.app.workspace(self.user).store.list('events')]
        self.assertIn('library.parse_confirmed', events)
        again = self.ok('library/parse/retry', {'material_id': material['id']})
        self.assertEqual(again['status'], 'parsing')

    def test_confirming_with_items_missing_is_refused_with_its_code(self):
        material = self.upload('half.urdf', URDF)
        self.parse()
        result = self.ok('library/parse', {'material_id': material['id']})
        status, refused = self.call('library/parse/confirm', {'material_id': material['id'], 'revision': result['revision']})
        self.assertEqual((status, refused['code']), (400, 'parse_incomplete'))

    def test_bad_input_never_reaches_the_cloud(self):
        for payload in ({'material_id': '../../admin'}, {'material_id': 'mat_' + 'g' * 20}, {}):
            status, body = self.call('library/parse', payload)
            self.assertEqual(status, 400, body)
        status, body = self.call('library/parse/save', {'material_id': 'mat_' + '0' * 20, 'revision': '1', 'values': {}})
        self.assertEqual(status, 400, body)

    def test_answers_are_reduced_to_plain_values(self):
        from brainos.material_parse import plain
        deep = {'a': [[[[[[[['too deep']]]]]]]]}
        self.assertEqual(plain(deep), {'a': [[[[[[None]]]]]]})
        self.assertEqual(plain({'x': float('nan'), 'y': 'z' * 6000, 1: 'k', 'o': object()}),
                         {'x': None, 'y': 'z' * 5000, 'o': None})

    def test_signed_out_of_the_cloud(self):
        self.ok('cloud/logout', {})
        status, body = self.call('library/parse', {'material_id': 'mat_' + '0' * 20})
        self.assertEqual((status, body.get('code')), (400, 'cloud_login_required'))


if __name__ == '__main__':
    unittest.main()
