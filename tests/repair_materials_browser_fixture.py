"""Browser fixture for issue #30: a repair session reads the library's usable materials of its device.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/repair_materials_browser_fixture.py info.json``, then
``node tests/repair_materials_browser.cjs info.json [screenshot dir]``.

The console, an SSH robot that answers the hardware scan like a Jetson robot, and a scripted model
behind the TokenFans stand-in (issue #4). The model reads LIBRARY_MATERIALS.json and reports the
driver problem as needs_user. The cloud library is a stand-in inside this process (the real cloud is
in tests/test_repair_materials_cloud.py): it has no materials until ``POST <control>/found``, then one
usable driver manual for whichever robot asks. The SSH password and the TokenFans key are random per run.
"""
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_binary  # noqa: E402,F401  (optional NEUXMIND_TEST_AGENT_BINARY)
from brainos import agent_runtime, hardware_scan, repair_materials  # noqa: E402
from brainos.cloud_account import CloudError  # noqa: E402
from brainos.material_library import MaterialLibrary  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from hardware_fixture import jetson  # noqa: E402
from ssh_fixture import SSHFixture  # noqa: E402
from test_engineering import ResponsesFixture  # noqa: E402
from test_product import Server  # noqa: E402
import tokenfans_fixture as tkf  # noqa: E402

if not agent_runtime.binary_path():
    from codex_standin import CodexStandIn  # noqa: E402
    _real_available = agent_runtime.available
    agent_runtime.AgentProcess = CodexStandIn
    agent_runtime.available = lambda: {**_real_available(), 'available': True}

MODEL = 'fixture-materials'
ISSUES = ('usb-driver:1-4', 'serial-perm:/dev/ttyUSB0')     # one per repair: a needs_user problem is blocked afterwards (#13)
MATERIAL = 'mat_' + '0' * 19 + '1'
VALUES = {'driver_name': 'demo_driver', 'driver_version': '1.2.0', 'source': 'ROS 2 软件源', 'os': ['Ubuntu 22.04'],
          'ros2_distro': ['humble'], 'install_steps': ['apt-get install -y ros-humble-demo-driver'],
          'topics': [{'name': '/odom', 'type': 'nav_msgs/msg/Odometry'}], 'actions': []}
STATE = {'found': False}


def _row(device_id):
    return {'id': MATERIAL, 'filename': 'demo-driver-manual.md', 'kind': 'document', 'format': 'md', 'body_type': 'quadruped',
            'vendor': '示例厂家', 'model': 'Q1', 'version': '1.0', 'status': 'usable', 'device_id': device_id, 'device_name': ''}


def _list(self, p=None):
    p = p or {}
    rows = [_row(p.get('device_id'))] if STATE['found'] and p.get('status') in (None, 'usable') else []
    return {'materials': rows, 'total': len(rows)}


def _call(self, method, path, payload=None, timeout=None):
    if STATE['found'] and method == 'GET' and path == f'/library/materials/{MATERIAL}/parse':
        return {'material': {'id': MATERIAL}, 'kind': 'document', 'status': 'usable', 'values': VALUES, 'not_applicable': [],
                'fields': [{'key': k} for k in VALUES], 'confirmed_by': 'checker@example.test', 'confirmed_at': '2026-11-22T01:00:00Z'}
    raise CloudError('这个替身只答复修复会话读资料的两个请求')


MaterialLibrary.list, MaterialLibrary._call = _list, _call


def call(name, args, n):
    return {'id': f'fc_{n}', 'type': 'function_call', 'call_id': f'call_{name}_{n}', 'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}


class MaterialUIModel(ResponsesFixture):
    """Decides from the conversation it is sent, so each repair session runs the same three steps."""
    requests = []

    def select_item(self, data, fallback):
        n = len(type(self).requests)
        called = [i.get('name') for i in data.get('input') or [] if isinstance(i, dict) and i.get('type') == 'function_call']
        if 'exec_command' not in called:
            return call('exec_command', {'cmd': 'cat ' + repair_materials.FILE_NAME}, n)
        if 'save_repair_report' not in called:
            asked = json.dumps([i for i in data.get('input') or [] if isinstance(i, dict) and i.get('role') == 'user'], ensure_ascii=False)
            issue = next(i for i in ISSUES if '- ' + i in asked)
            return call('save_repair_report', {'summary': '要装运控驱动，先要有资料', 'followups': ['请先在设备档案的「资料」里上传驱动资料'],
                                               'results': [{'issue_id': issue, 'outcome': 'needs_user', 'actions': '读了资料库文件',
                                                            'verification': '未改动设备', 'evidence_ids': []}]}, n)
        return {'id': f'msg_{n}', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': '已保存报告。', 'annotations': []}]}


class Control(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        STATE['found'] = self.path == '/found'
        self.send_response(204)
        self.end_headers()


if __name__ == '__main__':
    password = secrets.token_hex(10)
    ssh = SSHFixture(command_outputs={hardware_scan.SCAN_EXEC: jetson().encode()}, password=password)
    model = Server(MaterialUIModel)
    tokenfans = tkf.TokenFansFixture(routes={MODEL: model.url + '/v1'})
    os.environ['NEUXMIND_TOKENFANS_URL'] = tokenfans.url
    control = ThreadingHTTPServer(('127.0.0.1', 0), Control)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = Server(make_handler(app))
    info = {'url': api.url, 'ssh_port': ssh.port, 'ssh_user': 'robot', 'ssh_login': password, 'tokenfans': tokenfans.url,
            'tokenfans_key': tkf.API_KEY, 'model': MODEL, 'issues': ISSUES, 'control': f'http://127.0.0.1:{control.server_port}'}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-repair-materials-ui.json', 'w') as f:
        json.dump(info, f)
    print(api.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        control.shutdown()
        api.close()
        app.close()
        model.close()
        ssh.close()
        tokenfans.close()
        tmp.cleanup()
