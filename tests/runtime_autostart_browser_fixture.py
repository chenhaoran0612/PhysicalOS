"""Browser fixture for issue #20: starting the management layer at boot, confirmed in the console.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:spec:runtime:tests python tests/runtime_autostart_browser_fixture.py [info.json]``, then
``node tests/runtime_autostart_browser.cjs info.json [screenshot dir]``.

The console with a stand-in install package and three "robots" (``autostart_fixture.AutostartDevice``: SSH,
SFTP and the product's scripts run for real in a temporary directory; the install script, sudo and systemd
are stand-ins, nothing runs as root): ``sudo`` needs a sudo password, ``nopasswd`` does not, ``hand``
already runs a management layer someone installed by hand. A control port:

    GET /tune                   the heartbeat and link timers stop; installs wait less between polls
    GET /seen?robot=sudo        JSON: the service file, whether it is enabled and running, the runtime's port,
                                how many root scripts ran and whether sudo got the right password each time
    GET /reboot?robot=sudo      power off and on: the runtime and the SSH sessions end; the service starts it if enabled

The SSH logins and the sudo passwords are random per run and written to the info file. Nothing here reaches a
real machine.
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
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autostart_fixture import AutostartDevice  # noqa: E402
from brainos import device_software, device_status, hardware_scan, runtime_package  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from hardware_fixture import jetson  # noqa: E402
from install_fixture import fake_package  # noqa: E402
from software_fixture import software  # noqa: E402
from test_product import Server  # noqa: E402
from test_ssh_jump import STATUS_OUTPUT  # noqa: E402

ROBOTS = {'sudo': 'password', 'nopasswd': 'nopasswd', 'hand': 'password'}


def robot_device(sudo):
    device = AutostartDevice(sudo=sudo)
    device.password = secrets.token_hex(8)
    device.canned = {device_status.status_command([]): STATUS_OUTPUT, hardware_scan.SCAN_EXEC: jetson().encode(),
                     device_software.SOFTWARE_EXEC: software().encode()}
    return device


def control_handler(app, devices):
    def tune(_):
        for workspace in list(app.workspaces.values()):
            workspace.heartbeat.stop_timer()
            workspace.runtime_link.stop_timer()
            workspace.runtime_install.poll_seconds = .2
            workspace.runtime_install.waits = (.2, .5, 1)

    def seen(device):
        typed = [stdin.split(b'\n', 1)[0].decode('utf-8', 'replace') for stdin in device.sudo_inputs]
        return {'unit': device.unit(), 'enabled': device.enabled(), 'service': device.service('active', 'inactive'),
                'runtime_port': device.runtime_port(), 'root_runs': [list(run[:2]) for run in device.root_runs],
                'right_password': [value == device.sudo_password for value in typed], 'mark': (device.read('neuxmind/.autostart') or b'').decode()}

    steps = {'/tune': tune, '/reboot': lambda d: d.reboot()}

    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            device = devices.get((parse_qs(url.query).get('robot') or ['sudo'])[0])
            step = steps.get(url.path)
            if step:
                step(device)
            data = json.dumps(seen(device)).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return Control


if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    package = fake_package(os.path.join(tmp.name, 'package'))
    os.environ[runtime_package.ENV] = str(package.directory)
    app = Application(os.path.join(tmp.name, 'state'))
    api = Server(make_handler(app))
    devices = {name: robot_device(sudo) for name, sudo in ROBOTS.items()}
    devices['hand'].by_hand()
    control = ThreadingHTTPServer(('127.0.0.1', 0), control_handler(app, devices))
    threading.Thread(target=control.serve_forever, daemon=True).start()
    info = {'url': api.url, 'control': f'http://127.0.0.1:{control.server_port}', 'robot_user': 'robot', 'package_id': package.id,
            'robots': {name: {'port': device.port, 'login': device.password, 'sudo_password': device.sudo_password}
                       for name, device in devices.items()}}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-runtime-autostart-ui.json', 'w') as f:
        json.dump(info, f)
    print(api.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        control.shutdown(); control.server_close()
        api.close(); app.close()
        for device in devices.values():
            device.close()
        tmp.cleanup()
