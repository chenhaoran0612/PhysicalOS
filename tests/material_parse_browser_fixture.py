"""Browser fixture for issue #34: materials of a device, parsed and confirmed in its device profile.

Serves the desktop app, an SSH "robot" that answers like a robot running ROS 2 Humble (as in the
device profile fixture), the real cloud app (tests/cloud_fixture: local uvicorn, SQLite, its parse
worker running every second) and a stand-in for the knowledge service (cloud/tests/fake_weknora.py)
whose "model" answers with made-up driver facts. A verified cloud account is created at start.

Sample files are made up here: a URDF whose second joint has no limits, and a Word container with one
line of text. ``ANSWERS`` is what the stand-in's model says; one answer carries markup, so the page
must show it as text.

Usage: python3 tests/material_parse_browser_fixture.py [config.json]
"""
import importlib.util
import json
import os
import secrets
import sys
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path

import cloud_fixture
from brainos import device_software, device_status, hardware_scan, ros2_profile
from brainos.server import Application, make_handler
from hardware_fixture import jetson
from ros2_fixture import ros2
from software_fixture import software
from ssh_fixture import SSHFixture
from test_product import Server
from test_ssh_jump import STATUS_OUTPUT

ROOT = Path(__file__).resolve().parents[1]
EMAIL = 'parse-ui@example.test'
MARKUP = '<img src=x onerror="window.__markupRan=1">'
ANSWERS = {
    'driver_name': {'driver_name': 'demo_arm_driver' + MARKUP, 'driver_version': '2.1.0', 'source': 'https://example.test/demo_arm_driver',
                    'os': ['Ubuntu 22.04'], 'ros2_distro': ['humble']},
    'install_steps': {'install_steps': ['sudo apt install ros-humble-demo-arm-driver', 'ros2 launch demo_arm_driver bringup.launch.py']},
    'topics': {'topics': [{'name': '/joint_states', 'type': 'sensor_msgs/msg/JointState'}], 'actions': None},
}
URDF = (b'<?xml version="1.0"?>\n<robot name="demo_arm">\n  <link name="base_link"/>\n  <link name="upper_arm"/>\n'
        b'  <joint name="shoulder" type="revolute"><parent link="base_link"/><child link="upper_arm"/>\n'
        b'    <limit lower="-1.57" upper="1.57" velocity="2.0" effort="40"/></joint>\n'
        b'  <joint name="elbow" type="revolute"><parent link="upper_arm"/><child link="base_link"/></joint>\n</robot>\n')


def _docx():
    out = BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', b'<Types/>')
        archive.writestr('_rels/.rels', b'<Relationships/>')
        archive.writestr('word/document.xml', '<w:document>示例驱动手册</w:document>'.encode())
    return out.getvalue()


def _fake_weknora():
    spec = importlib.util.spec_from_file_location('fake_weknora', ROOT / 'cloud' / 'tests' / 'fake_weknora.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _answer(question, _ids):
    for key, value in ANSWERS.items():
        if f'"{key}"' in question:
            return json.dumps(value, ensure_ascii=False)
    return '{}'


if __name__ == '__main__':
    config_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-material-parse-ui.json'
    tmp = tempfile.TemporaryDirectory()
    weknora = _fake_weknora()
    knowledge = weknora.FakeWeKnora()
    knowledge.answer = _answer
    cloud = cloud_fixture.CloudServer(models=(), weknora_url=knowledge.url, weknora_api_key=weknora.API_KEY,
                                      weknora_kb_id=knowledge.kb_id, library_parse_interval_seconds=1,
                                      library_parse_threads=1)
    cloud.register(EMAIL)
    ssh_login = secrets.token_hex(8)                # made up on every run; not a credential of any machine
    target = SSHFixture(password=ssh_login,
                        command_outputs={device_status.status_command([]): STATUS_OUTPUT,
                                         device_software.SOFTWARE_EXEC: software().encode(),
                                         hardware_scan.SCAN_EXEC: jetson().encode(),
                                         ros2_profile.ROS2_EXEC: ros2().encode()})
    samples = {}
    os.makedirs(tmp.name + '/samples')
    for name, data in (('demo_arm.urdf', URDF), ('驱动手册.docx', _docx())):
        path = os.path.join(tmp.name, 'samples', name)
        with open(path, 'wb') as handle:
            handle.write(data)
        samples[name] = path
    app = Application(tmp.name + '/app')
    server = Server(make_handler(app))
    config = {'url': server.url, 'cloud': cloud.url, 'email': EMAIL, 'password': cloud_fixture.PASSWORD,
              'target_port': target.port, 'target_user': 'robot', 'target_password': ssh_login, 'samples': samples}
    with open(config_path, 'w') as f:
        json.dump(config, f)
    print(server.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close(); app.close(); cloud.stop(); target.close(); knowledge.close(); tmp.cleanup()
