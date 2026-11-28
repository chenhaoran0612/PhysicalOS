"""Browser acceptance fixture for issue #78: the public library (公共资料库) on the 资料库 page.

Serves the desktop app together with the real cloud app (tests/cloud_fixture: local uvicorn, SQLite,
``NEUXMIND_ENV=test``) whose library volume is a temp directory. A public library is imported at start with
``library_public.import_dir`` from **made-up** files (a two-joint URDF model in a zip, a one-line PDF), and
parsed by the cloud's rules parser, so the model waits for confirmation. Two cloud accounts: an ordinary
one and the platform operator of the test cloud.

Usage: python3 tests/library_public_browser_fixture.py [config.json]
"""
import io
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import cloud_fixture
from brainos.server import Application, make_handler
from test_product import Server

from neuxmind_cloud import library_public, material_parse  # noqa: E402  (cloud_fixture put cloud/ on the path)
from neuxmind_cloud.db import Database  # noqa: E402

EMAIL = 'public-library@example.test'
URDF = b'''<?xml version="1.0"?><robot name="demo_arm">
<link name="base"><visual><geometry><mesh filename="package://demo/meshes/base.stl"/></geometry></visual></link>
<link name="arm"/><joint name="shoulder" type="revolute"><parent link="base"/><child link="arm"/>
<limit lower="-1.5" upper="1.5" velocity="2" effort="20"/></joint></robot>'''
STL = b'solid demo\nendsolid demo\n'
PDF = b'%PDF-1.4\n1 0 obj\n<< /Title (demo manual) >>\nendobj\ntrailer\n<< >>\n%%EOF\n'


def _model_zip():
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('model.urdf', URDF)
        archive.writestr('meshes/base.stl', STL)
    return out.getvalue()


def import_public(cloud, folder):
    folder.mkdir(parents=True)
    (folder / 'models').mkdir()
    (folder / 'models' / 'demo-arm.zip').write_bytes(_model_zip())
    (folder / 'demo-manual.pdf').write_bytes(PDF)
    labels = {'body_type': 'manipulator', 'vendor': '示例公共厂家', 'model': 'PA-1'}
    manifest = {'version': 1, 'materials': [
        {'file': 'models/demo-arm.zip', 'kind': 'model', 'version': 'demo@0000001', 'description': '示例的公共机器人模型', **labels},
        {'file': 'demo-manual.pdf', 'version': '1.0', 'description': '示例的公共手册', **labels}]}
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False))
    db = Database(cloud.settings.database_url)
    result = library_public.import_dir(db, cloud.settings, folder)
    assert result['stored'] == 2 and not result['refused'], result
    clock = time.time()
    for _ in range(30):
        clock += 120
        material_parse.run_pending(db, cloud.settings, at=clock)
    return result


if __name__ == '__main__':
    config_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-library-public-ui.json'
    tmp = tempfile.TemporaryDirectory()
    cloud = cloud_fixture.CloudServer(models=(), library_dir=tmp.name + '/library')
    cloud.register(EMAIL)
    import_public(cloud, Path(tmp.name) / 'public-data')
    app = Application(tmp.name + '/app')
    server = Server(make_handler(app))
    config = {'url': server.url, 'cloud': cloud.url, 'email': EMAIL, 'operator': cloud_fixture.OPERATOR,
              'password': cloud_fixture.PASSWORD}
    with open(config_path, 'w') as f:
        json.dump(config, f)
    print(json.dumps(config), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        app.close()
        cloud.stop()
        tmp.cleanup()
