"""Issue #30: what a repair session reads from a real cloud library (cloud/, local uvicorn, SQLite).

Materials of two robots, one confirmed by a person and one not. The cloud has no knowledge service
configured, so documents come back "没解析出来" and are filled in and confirmed by hand, as
docs/MATERIAL_PARSING.md allows. Every file is made up here.
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

from brainos import repair_materials

try:
    import cloud_fixture
except ImportError as exc:  # cloud dependencies not installed in this interpreter
    cloud_fixture = None
    SKIP = f'cloud dependencies not installed: {exc}'
else:
    SKIP = ''

LABELS = {'body_type': 'quadruped', 'vendor': '示例厂家', 'model': 'Q1', 'version': '1.0'}
ROBOT = {'device_id': 'robot-0123456789ab', 'device_name': '示例四足'}
OTHER = {'device_id': 'robot-ba9876543210', 'device_name': '另一台'}
MANUAL = '# 示例驱动\n\n安装：apt-get install -y ros-humble-demo-driver\n'.encode()
VALUES = {'driver_name': 'demo_driver', 'driver_version': '1.2.0', 'source': 'ROS 2 软件源 ros-humble-demo-driver',
          'os': ['Ubuntu 22.04'], 'ros2_distro': ['humble'], 'install_steps': ['apt-get install -y ros-humble-demo-driver'],
          'topics': [{'name': '/odom', 'type': 'nav_msgs/msg/Odometry'}]}


def sha(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipIf(cloud_fixture is None, SKIP)
class RepairMaterialsCloudTest(unittest.TestCase):
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
        cls.email = 'repair@library.test'
        token, cls.user, _ = cls.app.identity.sign_in({'email': cls.email, 'password': f'desk-{secrets.token_hex(8)}-7', 'name': 'Dev'},
                                                      register=True)
        cls.cookie = 'brain_session=' + token
        cls.cloud.register(cls.email)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.app.close()
        cls.tmp.cleanup()
        cls.cloud.stop()

    def call(self, path, payload):
        req = urllib.request.Request(f'http://{self.host}/api/{path}', data=json.dumps(payload).encode(), method='POST',
                                     headers={'Host': self.host, 'Cookie': self.cookie, 'Content-Type': 'application/json'})
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
        upload = self.ok('library/upload/start', {'filename': filename, 'size': len(data), 'sha256': sha(data), **LABELS, **fields})
        req = urllib.request.Request(f'http://{self.host}/api/library/chunk?upload={upload["id"]}&index=0', data=data, method='PUT',
                                     headers={'Host': self.host, 'Cookie': self.cookie, 'Content-Type': 'application/octet-stream'})
        with urllib.request.urlopen(req, timeout=30) as r:
            self.assertEqual(r.status, 200)
        return self.ok('library/upload/complete', {'upload_id': upload['id']})

    def parse(self):
        from neuxmind_cloud import material_parse
        context = self.cloud.app.state.ctx
        for _ in range(5):
            material_parse.run_pending(context.db, context.settings)

    def test_a_repair_reads_only_its_devices_confirmed_results(self):
        self.ok('cloud/configure', {'base_url': self.cloud.url})
        self.ok('cloud/login', {'email': self.email, 'password': cloud_fixture.PASSWORD})
        platform = self.app.workspace(self.user)
        confirmed = self.upload('driver-manual.md', MANUAL, **ROBOT)
        waiting = self.upload('draft.md', MANUAL + b'draft\n', **ROBOT)
        other = self.upload('other.md', MANUAL + b'other\n', **OTHER)
        self.parse()
        result = self.ok('library/parse', {'material_id': confirmed['id']})
        self.assertEqual(result['status'], 'failed', 'no knowledge service: a person fills it in')
        self.ok('library/parse/confirm', {'material_id': confirmed['id'], 'revision': result['revision'], 'values': VALUES})
        other_result = self.ok('library/parse', {'material_id': other['id']})
        self.ok('library/parse/confirm', {'material_id': other['id'], 'revision': other_result['revision'],
                                          'values': {**VALUES, 'driver_name': 'other_driver'}})

        mine = repair_materials.collect(platform, ROBOT['device_id'])
        self.assertTrue(mine['available'], mine)
        self.assertEqual([m['material_id'] for m in mine['materials']], [confirmed['id']])
        entry = mine['materials'][0]
        self.assertEqual({k: entry['values'][k] for k in VALUES}, VALUES)
        self.assertEqual((entry['kind'], entry['filename'], entry['driver']), ('document', 'driver-manual.md', True))
        self.assertEqual((mine['driver_materials'], mine['not_confirmed']), (1, 1))
        text = json.dumps(mine, ensure_ascii=False)
        for left_out in (self.email, waiting['id'], other['id'], 'other_driver', '示例驱动'):
            self.assertNotIn(left_out, text)

        theirs = repair_materials.collect(platform, OTHER['device_id'])
        self.assertEqual([m['material_id'] for m in theirs['materials']], [other['id']])

        nobody = repair_materials.collect(platform, 'robot-ffffffffffff')
        self.assertEqual((nobody['available'], nobody['materials'], nobody['driver_materials']), (True, [], 0))
        self.assertIn('上传', nobody['what_to_do'])

        self.ok('cloud/logout', {})
        signed_out = repair_materials.collect(platform, ROBOT['device_id'])
        self.assertFalse(signed_out['available'])
        self.assertIn('登录', signed_out['reason'])


if __name__ == '__main__':
    unittest.main()
