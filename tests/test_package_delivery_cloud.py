"""Issue #31 against the real cloud app: the library's own routes, as issue #33 built them.

``cloud_fixture.CloudServer`` is the cloud (cloud/, local uvicorn, SQLite, a temp directory as
its library volume), the device is ``upload_fixture.UploadDevice``. A package is uploaded through
the cloud's API, as the "资料库" page would; it is then marked usable straight in the cloud's
database, because the step that does this for real (parsing and a person's confirmation,
issue #34) does not exist yet. Every file here is made up.
"""
import hashlib
import io
import json
import os
import random
import tempfile
import unittest
import urllib.request
import zipfile

try:
    import cloud_fixture
except ImportError as exc:  # cloud dependencies not installed in this interpreter
    cloud_fixture = None
    SKIP = f'cloud dependencies not installed: {exc}'
else:
    SKIP = ''

from brainos import package_delivery
from brainos.errors import CodedError
from brainos.service import Platform
from package_cloud_fixture import CuttingRelay
from test_device_transfer import quiet_paramiko
from upload_fixture import UploadDevice

CHUNK = 128 * 1024
ROBOT, PROJECT, CONNECTION = 'robot-fixture', 'project-fixture', 'connection-fixture'
LABELS = {'body_type': 'quadruped', 'vendor': '示例厂家', 'model': 'X1', 'version': '1.0'}


def package(size=300_000, seed=31):
    """A stored (uncompressed) zip of made-up bytes: a file the cloud takes for a driver package."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_STORED) as archive:
        archive.writestr('driver/blob.bin', random.Random(seed).randbytes(size))
    return out.getvalue()


@unittest.skipIf(cloud_fixture is None, SKIP)
class RealCloudDelivery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cloud = cloud_fixture.CloudServer(models=(), library_chunk_bytes=CHUNK)
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.cloud.stop()

    def setUp(self):
        quiet_paramiko(self)
        type(self).count += 1
        self.email = f'dev{self.count}@delivery.test'
        self.user = self.cloud.register(self.email)
        self.device = UploadDevice()
        self.addCleanup(self.device.close)
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.platform = Platform(os.path.join(self.work.name, 'state'))
        self.addCleanup(self.platform.close)
        connection, secret = self.device.login()
        self.platform.store.put('robots', {'id': ROBOT, 'project_id': PROJECT, 'name': '测试机器人', 'connection_id': CONNECTION,
                                           'product': {'vendor': '示例厂家', 'model': 'X1', 'source': 'manual'}})
        self.platform.store.put('device_connections', {**connection, 'id': CONNECTION, 'robot_id': ROBOT, 'project_id': PROJECT})
        self.platform.store.put('agent_secrets', {**secret, 'id': CONNECTION})
        self.sign_in(self.cloud.url)

    def sign_in(self, base_url):
        self.platform.cloud.configure({'base_url': base_url})
        self.platform.cloud.login({'email': self.email, 'password': cloud_fixture.PASSWORD})

    def upload(self, filename, data):
        """Through the cloud's upload API, as the library page does it. The material ends in 「解析中」."""
        digest = hashlib.sha256(data).hexdigest()
        upload = self.cloud.call('POST', '/library/uploads', {'filename': filename, 'size': len(data), 'sha256': digest, **LABELS}, self.user)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for index in range(upload['chunk_count']):
            request = urllib.request.Request(
                f"{self.cloud.url}/api/v1/library/uploads/{upload['id']}/chunks/{index}", data=data[index * CHUNK:(index + 1) * CHUNK],
                headers={'Authorization': 'Bearer ' + self.user['token'], 'Content-Type': 'application/octet-stream'}, method='PUT')
            with opener.open(request, timeout=20) as response:
                json.loads(response.read())
        return self.cloud.call('POST', f"/library/uploads/{upload['id']}/complete", {}, self.user)

    def mark_usable(self, material_id):
        """Stand-in for issue #34: what parsing plus a person's confirmation will do to a material."""
        from neuxmind_cloud.library_models import Material
        with self.cloud.app.state.ctx.db.transaction() as session:
            session.get(Material, material_id).status = package_delivery.USABLE_STATUSES[0]

    def deliver(self, material_id):
        return self.platform.package_delivery.deliver(ROBOT, PROJECT, material_id, authorized=True, fetch={'retry_waits': (0, 0, 0)},
                                                      transfer={'retry_waits': (0, 0, 0)})

    def test_a_package_uploaded_to_the_library_reaches_the_device_and_is_verified(self):
        data = package()
        material = self.upload('驱动 安装包 (v1.0).zip', data)
        self.mark_usable(material['id'])

        result = self.deliver(material['id'])

        self.assertEqual(self.device.read(result['device_path']), data)
        self.assertEqual((result['sha256'], result['bytes'], result['verified']), (hashlib.sha256(data).hexdigest(), len(data), True))
        self.assertEqual((result['filename'], result['device_name']), ('驱动 安装包 (v1.0).zip', 'v1.0.zip'))
        self.assertEqual(result['labels']['vendor'], '示例厂家')
        self.assertEqual(self.device.refused, [])

    def test_a_package_as_the_library_leaves_it_today_is_not_delivered(self):
        material = self.upload('driver.zip', package(20_000, seed=7))
        self.assertEqual(material['status'], 'parsing')                        # until issue #34 nothing moves on from here
        with self.assertRaises(CodedError) as caught:
            self.deliver(material['id'])
        self.assertEqual(caught.exception.code, 'package_not_usable')
        self.assertEqual(self.device.connections, 0)

    def test_a_fetch_cut_on_the_way_from_the_real_cloud_goes_on_with_a_range(self):
        data = package(seed=9)
        material = self.upload('driver.zip', data)
        self.mark_usable(material['id'])
        relay = CuttingRelay(int(self.cloud.url.rsplit(':', 1)[1]), cuts=[90_000, 90_000])
        self.addCleanup(relay.close)
        self.sign_in(relay.url)

        result = self.deliver(material['id'])

        self.assertEqual(relay.content_connections, 3)                         # two cut, the third finished
        self.assertGreater(result['fetch']['attempts'], 2)
        self.assertEqual(self.device.read(result['device_path']), data)

    def test_a_package_uploaded_for_another_robot_is_not_sent_to_this_one(self):
        from neuxmind_cloud.library_models import Material
        if not hasattr(Material, 'device_id'):
            self.skipTest('the cloud of this tree does not record which device a material belongs to (issue #34)')
        data = package(20_000, seed=13)
        material = self.upload('driver.zip', data)
        self.mark_usable(material['id'])
        for owner, delivered in (('robot-0123456789ab', False), (ROBOT, True)):
            with self.subTest(owner=owner):
                with self.cloud.app.state.ctx.db.transaction() as session:      # who it was uploaded for, as #34 records it
                    session.get(Material, material['id']).device_id = owner
                if delivered:
                    self.assertEqual(self.device.read(self.deliver(material['id'])['device_path']), data)
                    continue
                with self.assertRaises(CodedError) as caught:
                    self.deliver(material['id'])
                self.assertEqual(caught.exception.code, 'package_other_device')
                self.assertEqual(self.device.connections, 0)

    def test_a_package_of_another_organisation_cannot_be_fetched(self):
        material = self.upload('driver.zip', package(20_000, seed=11))
        self.mark_usable(material['id'])
        self.email = f'stranger{self.count}@delivery.test'
        self.cloud.register(self.email)
        self.sign_in(self.cloud.url)
        with self.assertRaises(ValueError) as caught:
            self.deliver(material['id'])
        self.assertEqual(getattr(caught.exception, 'status', None), 404)
        self.assertEqual(self.device.connections, 0)


if __name__ == '__main__':
    unittest.main()
