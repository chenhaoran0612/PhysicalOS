"""Browser fixture for issue #45: an HTML page uploaded on the 资料库 page and parsed.

Serves the desktop app together with the real cloud app (tests/cloud_fixture: local uvicorn, SQLite, a
temporary directory, its parse worker running every second) and a stand-in for the knowledge service
(cloud/tests/fake_weknora.py) whose "model" answers with made-up driver facts. A verified cloud account is
created at start. ``GET /fixture/sent`` answers what the stand-in received (file name and text), so the
test can see that the page went there as its words and not as markup.

Usage: python3 tests/material_html_browser_fixture.py [config.json]
"""
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import cloud_fixture
from brainos.server import Application, make_handler
from test_product import Server

ROOT = Path(__file__).resolve().parents[1]
EMAIL = 'html-ui@example.test'
ANSWERS = {
    'driver_name': {'driver_name': 'demo_page_driver', 'driver_version': '3.2.0', 'source': 'https://example.test/demo_page_driver',
                    'os': ['Ubuntu 22.04'], 'ros2_distro': ['humble']},
    'install_steps': {'install_steps': ['sudo apt install ros-humble-demo-page-driver']},
    'topics': {'topics': [{'name': '/joint_states', 'type': 'sensor_msgs/msg/JointState'}], 'actions': None},
}


def _fake_weknora():
    spec = importlib.util.spec_from_file_location('fake_weknora', ROOT / 'cloud' / 'tests' / 'fake_weknora.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KNOWLEDGE = None        # the stand-in, once it runs


def _answer(question, ids):
    # A page that is not about a driver (its file name says so): the model finds none of the items.
    if any('论文' in KNOWLEDGE.knowledge[k]['upload']['fileName'] for k in ids if k in KNOWLEDGE.knowledge):
        return '{}'
    for key, value in ANSWERS.items():
        if f'"{key}"' in question:
            return json.dumps(value, ensure_ascii=False)
    return '{}'


if __name__ == '__main__':
    config_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-material-html-ui.json'
    tmp = tempfile.TemporaryDirectory()
    weknora = _fake_weknora()
    knowledge = KNOWLEDGE = weknora.FakeWeKnora()
    knowledge.answer = _answer
    cloud = cloud_fixture.CloudServer(models=(), weknora_url=knowledge.url, weknora_api_key=weknora.API_KEY,
                                      weknora_kb_id=knowledge.kb_id, library_parse_interval_seconds=1,
                                      library_parse_threads=1)
    account = cloud.register(EMAIL)
    app = Application(tmp.name + '/app')

    class Handler(make_handler(app)):
        def do_GET(self):
            where = urlparse(self.path).path
            if where == '/fixture/materials':
                return self.respond(cloud.call('GET', '/library/materials?limit=200', None, account))
            if where == '/fixture/sent':
                with knowledge.lock:
                    sent = [{'fileName': u['fileName'], 'text': u['content'].decode('utf-8', 'replace')} for u in knowledge.uploads]
                return self.respond({'sent': sent})
            return super().do_GET()

    server = Server(Handler)
    config = {'url': server.url, 'cloud': cloud.url, 'email': EMAIL, 'password': cloud_fixture.PASSWORD}
    with open(config_path, 'w') as f:
        json.dump(config, f)
    print(server.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        app.close()
        cloud.stop()
        knowledge.close()
        tmp.cleanup()
