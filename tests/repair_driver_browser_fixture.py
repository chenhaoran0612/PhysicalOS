"""Browser fixture for issue #29: the console, one SSH "robot" without its motion driver, a library with its
driver package, and a scripted model for the repair session.

    python tests/repair_driver_browser_fixture.py [info.json]

The robot is ``driver_install_fixture.DriverDevice``: it answers the console's status, software, hardware
and ROS 2 commands, takes the package over the channel of issue #18, and plays installing it and starting
the driver's service from the command text (nothing is run). Its ROS 2 listing lacks ``/cmd_vel`` and
``/odom`` until the driver runs.

``POST <control>/cloud`` signs the console's (first) account in to the library stand-in, once the script
has created that account. ``GET <control>/device`` says what the robot went through: installed, running,
latched, and every command it received.

Models run only through TokenFans (issue #4): the script connects with ``tokenfans_key`` (a fake key, random
per run). The stand-in offers one model, ``fixture-driver-install``, which forwards to the scripted model of
tests/test_repair_driver_install.py (library, package, install, start, rescan, report). One repair per
fixture run: the model counts its requests, so restart the fixture before running the script again.
Nothing here reaches a real machine or server.
"""
import json
import os
import random
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brainos import agent_runtime, device_software, device_status  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from driver_install_fixture import DriverDevice, ListingLibrary  # noqa: E402
from package_cloud_fixture import sha  # noqa: E402
from software_fixture import software  # noqa: E402
from test_product import Server  # noqa: E402
from test_repair_driver_install import NAME, DriverInstallModel  # noqa: E402
from test_ssh_jump import STATUS_OUTPUT  # noqa: E402
import tokenfans_fixture as tkf  # noqa: E402

if not agent_runtime.binary_path():
    from codex_standin import CodexStandIn  # noqa: E402
    _real_available = agent_runtime.available
    agent_runtime.AgentProcess = CodexStandIn
    agent_runtime.available = lambda: {**_real_available(), 'available': True}

PACKAGE = random.Random(2911).randbytes(90_000)


def control(device, cloud, app):
    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, status, body=b''):
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path.strip('/') == 'cloud' and app.workspaces:
                cloud.sign_in(next(iter(app.workspaces.values())))
                return self._send(204)
            self._send(404)

        def do_GET(self):
            if self.path.strip('/') != 'device':
                return self._send(404)
            body = {'installed': device.installed, 'running': device.running, 'latched': device.latched,
                    'commands': list(device.commands), 'refused': list(device.refused)}
            self._send(200, json.dumps(body).encode())
    return Control


if __name__ == '__main__':
    device = DriverDevice()
    device.answer_others = b'Linux test-fixture\nCPU: ARM protocol fixture\n'
    device.outputs.update({device_status.status_command([]): STATUS_OUTPUT, device_software.SOFTWARE_EXEC: software().encode()})
    cloud = ListingLibrary()
    # Issue #51: a package is its brand and model's; the script adds the robot as 示例机器人公司 X1 (robot_model_helper.cjs).
    material = cloud.add(NAME, PACKAGE, vendor='示例机器人公司', model='X1')
    DriverInstallModel.requests = []
    DriverInstallModel.material = material
    DriverInstallModel.path = '%s/neuxmind-pkg/library/%s/%s' % (device.home, sha(PACKAGE)[:32], NAME)
    model = Server(DriverInstallModel)
    tokenfans = tkf.TokenFansFixture(routes={'fixture-driver-install': model.url + '/v1'})
    os.environ['NEUXMIND_TOKENFANS_URL'] = tokenfans.url
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = Server(make_handler(app))
    switch = Server(control(device, cloud, app))
    info = {'url': api.url, 'port': device.port, 'ssh_user': device.username, 'ssh_login': device.password,
            'control': switch.url, 'tokenfans': tokenfans.url, 'tokenfans_key': tkf.API_KEY, 'package': NAME}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-repair-driver-ui.json', 'w') as f:
        json.dump(info, f)
    print(api.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        api.close(); app.close(); switch.close(); model.close(); tokenfans.close(); cloud.close(); device.close(); tmp.cleanup()
