"""Browser fixture for issue #17: a robot's management layer reached over its SSH connection.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/runtime_link_browser_fixture.py [info.json]``, then
``node tests/runtime_link_browser.cjs info.json [screenshot dir]``.

The console, one "robot" (a local SSH protocol fixture whose network can be cut and restored) with a
stand-in of the management layer on "its" loopback port 8790 and the token file of the standard
installation, and a control port:

    GET /tune         the heartbeat's own timer stops (the logins that are counted are the link's), and the
                      link looks after itself faster than in the product: a second between two tries
    GET /cut          nothing answers on the robot's port any more; its open sessions die
    GET /restore      the robot is back: the same port and host key
    GET /refuse       the robot's password is another one now, and its open sessions die
    GET /accept       the password is the saved one again
    GET /stop-layer   nothing listens on the robot's loopback port (the management layer is not running)
    GET /start-layer  it listens again
    GET /seen         JSON: logins that reached the robot, channels to its loopback, token reads,
                      requests the management layer answered

The login is random per run and written to the info file. Nothing here reaches a real machine.
"""
import json
import secrets
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brainos import device_software, device_status, hardware_scan, runtime_link  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from hardware_fixture import jetson  # noqa: E402
from management_fixture import ManagementLayer  # noqa: E402
from runtime_link_support import PORT, LoopbackDevice  # noqa: E402
from software_fixture import software  # noqa: E402
from test_product import Server  # noqa: E402
from test_ssh_jump import STATUS_OUTPUT  # noqa: E402

RETRY_PAUSE_SECONDS = 1
TICK_SECONDS = .2


def seen(robot, layer):
    return {'logins': robot.logins(), 'loopback_channels': len(robot.loopback_channels),
            'token_reads': robot.commands.count(runtime_link.TOKEN_COMMAND), 'layer_calls': list(layer.calls)}


def control_handler(app, robot, layer, login):
    def tune():
        for workspace in list(app.workspaces.values()):
            workspace.heartbeat.stop_timer()
            links = workspace.runtime_link
            links.retry_seconds = lambda failures: RETRY_PAUSE_SECONDS
            links.tick_seconds = TICK_SECONDS

    def refuse():
        robot.password = secrets.token_hex(8)
        robot.drop_sessions()

    def accept():
        robot.password = login

    steps = {'/tune': tune, '/cut': robot.cut, '/restore': robot.restore, '/refuse': refuse, '/accept': accept,
             '/stop-layer': lambda: robot.stop_serving(PORT), '/start-layer': lambda: robot.serve_on_loopback(PORT, layer.port)}

    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            step = steps.get(urlparse(self.path).path)
            if step:
                step()
            data = json.dumps(seen(robot, layer)).encode()
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
    layer = ManagementLayer()
    robot = LoopbackDevice(password=login, command_outputs={
        device_status.status_command([]): STATUS_OUTPUT, hardware_scan.SCAN_EXEC: jetson().encode(),
        device_software.SOFTWARE_EXEC: software().encode(),
        runtime_link.TOKEN_COMMAND: (layer.token + '\n').encode()})
    robot.serve_on_loopback(PORT, layer.port)
    control = ThreadingHTTPServer(('127.0.0.1', 0), control_handler(app, robot, layer, login))
    threading.Thread(target=control.serve_forever, daemon=True).start()
    info = {'url': api.url, 'control': f'http://127.0.0.1:{control.server_port}', 'robot_user': 'robot',
            'robot_login': login, 'robot_port': robot.port, 'layer_port': PORT}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-runtime-link-ui.json', 'w') as f:
        json.dump(info, f)
    print(api.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        control.shutdown(); control.server_close()
        api.close(); app.close(); robot.close(); layer.close(); tmp.cleanup()
