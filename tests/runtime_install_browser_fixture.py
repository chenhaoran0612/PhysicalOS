"""Browser fixture for issue #19: installing the management layer from the console.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:spec:runtime:tests python tests/runtime_install_browser_fixture.py [info.json]``, then
``node tests/runtime_install_browser.cjs info.json [screenshot dir]``.

The console with a stand-in install package (``install_fixture.fake_package``) and three "robots"
(``install_fixture.InstallDevice``: SSH, SFTP and the product's scripts run for real in a temporary
directory, its install script is a stand-in): ``fresh`` has nothing installed, ``failing`` fails to
start its management layer, ``existing`` already runs one that someone installed by hand. A control port:

    GET /tune                 the heartbeat and link timers stop; installs wait less between polls
    GET /hold?robot=fresh     the robot's install script waits before it installs (release: /release)
    GET /seen?robot=fresh     JSON: what that robot's stand-in install script ran, the files the console
                              put on it, and the token the install made there (to look for it in the page)

The logins are random per run and written to the info file. Nothing here reaches a real machine.
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

import os  # noqa: E402

from brainos import device_software, device_status, hardware_scan, runtime_package  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from hardware_fixture import jetson  # noqa: E402
from install_fixture import InstallDevice, fake_package  # noqa: E402
from software_fixture import software  # noqa: E402
from test_product import Server  # noqa: E402
from test_ssh_jump import STATUS_OUTPUT  # noqa: E402

ROBOTS = ('fresh', 'failing', 'existing')


def robot_device():
    device = InstallDevice()
    device.password = secrets.token_hex(8)
    device.canned = {device_status.status_command([]): STATUS_OUTPUT, hardware_scan.SCAN_EXEC: jetson().encode(),
                     device_software.SOFTWARE_EXEC: software().encode()}
    return device


def control_handler(app, devices, package):
    def tune(_):
        for workspace in list(app.workspaces.values()):
            workspace.heartbeat.stop_timer()
            workspace.runtime_link.stop_timer()
            workspace.runtime_install.poll_seconds = .2
            workspace.runtime_install.waits = (.2, .5, 1)

    def seen(device):
        directory = 'neuxmind-pkg/console/' + package.id
        return {'runs': [list(run) for run in device.runs()], 'files': device.names(directory),
                'installed': device.read('neuxmind/.neuxmind-device-install') is not None, 'token': device.token(),
                'runtime_port': device.runtime_port()}

    steps = {'/tune': tune, '/hold': lambda d: d.control('hold-install'), '/release': lambda d: d.control('hold-install', False)}

    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            device = devices.get((parse_qs(url.query).get('robot') or ['fresh'])[0])
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
    devices = {name: robot_device() for name in ROBOTS}
    devices['failing'].control('fail-start')
    devices['existing'].by_hand()
    control = ThreadingHTTPServer(('127.0.0.1', 0), control_handler(app, devices, package))
    threading.Thread(target=control.serve_forever, daemon=True).start()
    info = {'url': api.url, 'control': f'http://127.0.0.1:{control.server_port}', 'robot_user': 'robot',
            'package_id': package.id, 'runtime_version': package.runtime_version,
            'robots': {name: {'port': device.port, 'login': device.password} for name, device in devices.items()}}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-runtime-install-ui.json', 'w') as f:
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
