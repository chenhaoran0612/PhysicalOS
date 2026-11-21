"""Browser fixture for the motion driver check (issue #28): the console, three SSH "robots" and a
scripted model for the repair session.

    python tests/motion_drivers_browser_fixture.py [info.json]

The robots answer the status, software, hardware and ROS 2 commands. ``dog`` runs ROS 2 Humble
without its chassis driver (no velocity topic, no odometry), ``arm`` without its trajectory
controller (joint states only), ``complete`` has everything a quadruped should have.

``POST <control>/drivers/<robot>/<state>`` changes what a robot answers to the ROS 2 command from
then on: ``installed`` (its motion interfaces are there) or ``missing``. ``POST <control>/family/
<robot name>/<family>`` gives a registered robot a body type behind the console's back, the way a
device registered before the list of body types has one that is not in the list.

Models run only through TokenFans (issue #4): the script connects with ``tokenfans_key`` (a fake
key, random per run). The stand-in offers one model, ``fixture-drivers``, which forwards to the
scripted repair model of tests/test_motion_drivers.py: it reports ``needs_user`` for the two
problems of ``dog``. One repair per fixture run: the model counts its requests, so restart the
fixture before running the script again. Nothing here reaches a real machine.
"""
import json
import os
import secrets
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brainos import agent_runtime, device_software, device_status, hardware_scan, ros2_profile  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from hardware_fixture import jetson  # noqa: E402
from motion_drivers_fixture import ARM_ACTION, arm, chassis  # noqa: E402
from ros2_fixture import ros2  # noqa: E402
from software_fixture import software  # noqa: E402
from ssh_fixture import SSHFixture  # noqa: E402
from test_motion_drivers import DriverRepairModel  # noqa: E402
from test_product import Server  # noqa: E402
from test_ssh_jump import STATUS_OUTPUT  # noqa: E402
import tokenfans_fixture as tkf  # noqa: E402

if not agent_runtime.binary_path():
    from codex_standin import CodexStandIn  # noqa: E402
    _real_available = agent_runtime.available
    agent_runtime.AgentProcess = CodexStandIn
    agent_runtime.available = lambda: {**_real_available(), 'available': True}

STATES = {'dog': {'missing': chassis('/cmd_vel', '/odom'), 'installed': ros2()},
          'arm': {'missing': arm(ARM_ACTION), 'installed': arm()},
          'complete': {'missing': chassis('/cmd_vel', '/odom'), 'installed': ros2()}}
START = {'dog': 'missing', 'arm': 'missing', 'complete': 'installed'}


def control(robots, app):
    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            parts = [unquote(part) for part in self.path.strip('/').split('/')]
            known = False
            if len(parts) == 3 and parts[0] == 'drivers' and parts[2] in STATES.get(parts[1], {}):
                robots[parts[1]].command_outputs[ros2_profile.ROS2_EXEC] = STATES[parts[1]][parts[2]].encode()
                known = True
            elif len(parts) == 3 and parts[0] == 'family':
                platform = next(iter(app.workspaces.values()))
                robot = next((r for r in platform.store.list('robots') if r['name'] == parts[1]), None)
                if robot:
                    platform.store.put('robots', {**robot, 'family': parts[2]}, owner=True)
                    known = True
            self.send_response(204 if known else 404)
            self.send_header('Content-Length', '0')
            self.end_headers()
    return Control


if __name__ == '__main__':
    ssh_login = secrets.token_hex(8)                # made up on every run; not a credential of any machine
    robots = {name: SSHFixture(password=ssh_login,
                               command_outputs={device_status.status_command([]): STATUS_OUTPUT,
                                                device_software.SOFTWARE_EXEC: software().encode(),
                                                hardware_scan.SCAN_EXEC: jetson().encode(),
                                                ros2_profile.ROS2_EXEC: STATES[name][START[name]].encode()})
              for name in STATES}
    DriverRepairModel.requests = []
    model = Server(DriverRepairModel)
    tokenfans = tkf.TokenFansFixture(routes={'fixture-drivers': model.url + '/v1'})
    os.environ['NEUXMIND_TOKENFANS_URL'] = tokenfans.url
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = Server(make_handler(app))
    switch = Server(control(robots, app))
    info = {'url': api.url, 'ports': {name: robot.port for name, robot in robots.items()}, 'ssh_user': 'robot', 'ssh_login': ssh_login,
            'control': switch.url, 'tokenfans': tokenfans.url, 'tokenfans_key': tkf.API_KEY}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-motion-drivers-ui.json', 'w') as f:
        json.dump(info, f)
    print(api.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        api.close(); app.close(); switch.close(); model.close(); tokenfans.close(); tmp.cleanup()
        for robot in robots.values():
            robot.close()
