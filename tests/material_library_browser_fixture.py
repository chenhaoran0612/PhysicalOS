"""Browser acceptance fixture for issue #33: the 资料库 page.

Serves the desktop app together with the real cloud app (tests/cloud_fixture:
local uvicorn, SQLite, ``NEUXMIND_ENV=test``) whose library volume is a temp
directory. Small chunks (128 KiB) so that a file of about a megabyte takes
several requests. A verified cloud account is created at start; the page logs
in to it.

Sample files are **made up here** (a Word and an Excel container with one line
of text, a tar.gz with one text file): no real vendor material.

``GET /fixture/materials`` answers what the cloud holds for the account (read
with the account's own cloud session, not through the desktop), so the test
can check that the bytes really arrived.

Usage: python3 tests/material_library_browser_fixture.py [config.json]
"""
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import zipfile
from urllib.parse import urlparse

import cloud_fixture
from brainos.server import Application, make_handler
from test_product import Server

EMAIL = 'library@example.test'
CHUNK = 128 * 1024
DOCUMENT_LIMIT = 2 * 1024 * 1024


def _zip(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return out.getvalue()


def _tar_gz():
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as archive:
        payload = b'synthetic driver payload\n'
        info = tarfile.TarInfo('driver/install.txt')
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return gzip.compress(out.getvalue(), mtime=0)


SAMPLES = {
    '操作手册.docx': _zip({'[Content_Types].xml': b'<Types/>', 'word/document.xml': '<w:document>示例手册</w:document>'.encode()}),
    '关节参数表.xlsx': _zip({'[Content_Types].xml': b'<Types/>', 'xl/workbook.xml': '<workbook>示例参数</workbook>'.encode()}),
    'driver-1.0.tar.gz': _tar_gz(),
}

if __name__ == '__main__':
    config_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-material-library-ui.json'
    tmp = tempfile.TemporaryDirectory()
    cloud = cloud_fixture.CloudServer(models=(), library_chunk_bytes=CHUNK, library_max_document_bytes=DOCUMENT_LIMIT)
    account = cloud.register(EMAIL)
    samples = {}
    os.makedirs(tmp.name + '/samples')
    for name, data in SAMPLES.items():
        path = os.path.join(tmp.name, 'samples', name)
        with open(path, 'wb') as handle:
            handle.write(data)
        samples[name] = {'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    app = Application(tmp.name + '/app')

    class Handler(make_handler(app)):
        def do_GET(self):
            if urlparse(self.path).path == '/fixture/materials':
                return self.respond(cloud.call('GET', '/library/materials?limit=200', None, account))
            return super().do_GET()

    server = Server(Handler)
    config = {'url': server.url, 'cloud': cloud.url, 'email': EMAIL, 'password': cloud_fixture.PASSWORD,
              'chunk_bytes': CHUNK, 'document_limit': DOCUMENT_LIMIT, 'samples': samples}
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
