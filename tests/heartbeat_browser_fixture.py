"""Browser fixture for issue #24: the heartbeat and 未接入 / 接入中 / 在线 / 离线 on "我的机器人".

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/heartbeat_browser_fixture.py [info.json]``, then
``node tests/heartbeat_browser.cjs info.json [screenshot dir]``.

The console, one "robot" (a local SSH protocol fixture whose network can be cut and restored) and a
control port. Through the control port the browser test cuts the robot off, lets minutes pass
without waiting for them, and asks what reached the robot:

    GET /cut              nothing answers on the robot's port any more; its open sessions die
    GET /restore          the robot is back: the same port and host key
    GET /advance?seconds  that much later: the clock of the heartbeat moves, and a round that is due runs
    GET /seen             JSON: how many heartbeats, status reads and logins reached the robot

From the first ``/advance`` on, the heartbeat's own timer is stopped and rounds run only when the
test moves the clock. The login is random per run and written to the info file. Nothing here
reaches a real machine.
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

from brainos import device_software, device_status, hardware_scan, heartbeat  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from hardware_fixture import jetson  # noqa: E402
from heartbeat_support import FlakyDevice, ShiftedClock  # noqa: E402
from software_fixture import software  # noqa: E402
from test_product import Server  # noqa: E402
from test_ssh_jump import STATUS_OUTPUT  # noqa: E402


def seen(robot):
    commands = list(robot.commands)
    return {'heartbeats': commands.count(heartbeat.HEARTBEAT_COMMAND),
            'status_reads': sum(1 for command in commands if command.startswith(device_status.BASE_COMMAND)),
            'software_reads': commands.count(device_software.SOFTWARE_EXEC),
            'hardware_scans': commands.count(hardware_scan.SCAN_EXEC), 'logins': robot.logins()}


def control_handler(app, robot, clock):
    def advance(seconds):
        clock.advance(seconds)
        for workspace in list(app.workspaces.values()):
            beat = workspace.heartbeat
            beat.stop_timer()                 # from now on a round runs only when the test moves the clock
            beat.clock = clock
            beat.tick()
            beat.wait_idle(30)

    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == '/cut':
                robot.cut()
            elif url.path == '/restore':
                robot.restore()
            elif url.path == '/advance':
                advance(float(parse_qs(url.query)['seconds'][0]))
            data = json.dumps(seen(robot)).encode()
            self.send_response(200)
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
    robot = FlakyDevice(password=login, command_outputs={
        device_status.status_command([]): STATUS_OUTPUT, hardware_scan.SCAN_EXEC: jetson().encode(),
        device_software.SOFTWARE_EXEC: software().encode()})
    control = ThreadingHTTPServer(('127.0.0.1', 0), control_handler(app, robot, ShiftedClock()))
    threading.Thread(target=control.serve_forever, daemon=True).start()
    info = {'url': api.url, 'control': f'http://127.0.0.1:{control.server_port}', 'robot_user': 'robot',
            'robot_login': login, 'robot_port': robot.port}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-heartbeat-ui.json', 'w') as f:
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
