"""Browser fixture for issue #32: the platform's list of body types.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/body_types_browser_fixture.py [info.json]``, then
``node tests/body_types_browser.cjs info.json [screenshot dir]``.

The console, one "robot" (a local SSH protocol fixture) the browser test adds over SSH, and a control
port that writes two devices the way an earlier version left them: one with a type that is no longer in
the list, one whose device reports another type than the one registered. The login is random per run and
written to the info file. Nothing here reaches a real machine.
"""
import json
import secrets
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brainos import device_software, device_status, hardware_scan  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from brainos.service import now  # noqa: E402
from hardware_fixture import jetson  # noqa: E402
from software_fixture import software  # noqa: E402
from ssh_fixture import SSHFixture  # noqa: E402
from test_product import Server  # noqa: E402
from test_ssh_jump import STATUS_OUTPUT  # noqa: E402

OLD_NAME, DIFFERS_NAME = '登记较早的小车', '自报机械臂的设备'


def earlier_robot(project_id, name, family, **extra):
    return {'id': 'robot-' + secrets.token_hex(6), 'project_id': project_id, 'name': name, 'family': family,
            'endpoint': '', 'token': '', 'status': 'unconnected', 'capabilities': [], 'created_at': now(), **extra}


def control_handler(app):
    """GET /earlier?project=ID  two devices as an earlier version left them; answers their ids."""

    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            project_id = parse_qs(url.query)['project'][0]
            store = next(iter(app.workspaces.values())).store
            reported = {'schema': 'robot.adapter.v1', 'instance_id': 'stand-in', 'name': 'Stand-in',
                        'family': 'manipulator', 'adapter_version': '1', 'capabilities': ['safety.stop']}
            old = store.put('robots', earlier_robot(project_id, OLD_NAME, 'mobile'))
            differs = store.put('robots', earlier_robot(project_id, DIFFERS_NAME, 'quadruped', manifest=reported))
            data = json.dumps({'old': old['id'], 'differs': differs['id']}).encode()
            self.send_response(200 if url.path == '/earlier' else 404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return Control


if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = Server(make_handler(app))
    login = secrets.token_hex(8)
    robot = SSHFixture(password=login, command_outputs={
        device_status.status_command([]): STATUS_OUTPUT, hardware_scan.SCAN_EXEC: jetson().encode(),
        device_software.SOFTWARE_EXEC: software().encode()})
    control = ThreadingHTTPServer(('127.0.0.1', 0), control_handler(app))
    threading.Thread(target=control.serve_forever, daemon=True).start()
    info = {'url': api.url, 'control': f'http://127.0.0.1:{control.server_port}', 'robot_user': 'robot',
            'robot_login': login, 'robot_port': robot.port, 'old_name': OLD_NAME, 'differs_name': DIFFERS_NAME}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-body-types-ui.json', 'w') as f:
        json.dump(info, f)
    print(api.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        control.shutdown(); control.server_close()
        api.close(); app.close(); robot.close(); tmp.cleanup()
