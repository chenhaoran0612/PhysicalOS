"""Browser fixture for the device profile (issue #27): the console plus an SSH "robot" that answers
the status, software, hardware and ROS 2 commands like a robot running ROS 2 Humble.

    python tests/device_profile_browser_fixture.py [info.json]

Writes the console URL, the SSH port, the fixture login and a control URL to the JSON file and serves
until interrupted. ``POST <control>/ros2/<state>`` changes what the robot answers to the ROS 2 command
from then on: ``humble``, ``foxy`` (another distribution), ``absent`` (no ROS on the device), ``failed``
(the tool exits with an error) or ``stuck`` (no answer; the read gives up after two seconds here
instead of ``ros2_profile.TIMEOUT_SECONDS``). Nothing here reaches a real machine.
"""
import json
import secrets
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler

from brainos import device_software, device_status, hardware_scan, ros2_profile
from brainos.server import Application, make_handler
from hardware_fixture import jetson
from ros2_fixture import NODE_ENV, TRACEBACK, absent, listing, ros2
from software_fixture import software
from ssh_fixture import SSHFixture
from test_product import Server
from test_ssh_jump import STATUS_OUTPUT

STUCK_SECONDS = 2
STATES = {'humble': ros2(),
          'foxy': ros2(installed=(('foxy', 1, 0),), env={**NODE_ENV, 'distro': 'foxy'}, distro='foxy'),
          'absent': absent(),
          'failed': ros2(nodes=listing(TRACEBACK, 1), topics=listing(TRACEBACK, 1), services=listing(TRACEBACK, 1))}


def control(target):
    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            state = self.path.rsplit('/', 1)[-1]
            known = self.path.startswith('/ros2/') and (state in STATES or state == 'stuck')
            if known:
                target.unanswered.discard(ros2_profile.ROS2_EXEC)
                ros2_profile.TIMEOUT_SECONDS = TIMEOUT
                if state == 'stuck':
                    target.unanswered.add(ros2_profile.ROS2_EXEC)
                    ros2_profile.TIMEOUT_SECONDS = STUCK_SECONDS
                else:
                    target.command_outputs[ros2_profile.ROS2_EXEC] = STATES[state].encode()
            self.send_response(204 if known else 404)
            self.send_header('Content-Length', '0')
            self.end_headers()
    return Control


if __name__ == '__main__':
    TIMEOUT = ros2_profile.TIMEOUT_SECONDS
    ssh_login = secrets.token_hex(8)                # made up on every run; not a credential of any machine
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = Server(make_handler(app))
    target = SSHFixture(password=ssh_login,
                        command_outputs={device_status.status_command([]): STATUS_OUTPUT,
                                         device_software.SOFTWARE_EXEC: software().encode(),
                                         hardware_scan.SCAN_EXEC: jetson().encode(),
                                         ros2_profile.ROS2_EXEC: STATES['humble'].encode()})
    switch = Server(control(target))
    info = {'url': api.url, 'target_port': target.port, 'target_user': 'robot', 'target_password': ssh_login,
            'control': switch.url}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-device-profile-ui.json', 'w') as f:
        json.dump(info, f)
    print(api.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        api.close(); app.close(); switch.close(); target.close(); tmp.cleanup()
