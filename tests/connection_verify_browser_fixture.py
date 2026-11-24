"""Browser fixture for issue #16: 「验证连接」 on "我的机器人".

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/connection_verify_browser_fixture.py [info.json]``, then
``node tests/connection_verify_browser.cjs info.json [screenshot dir]``.

The console, one "robot", one jump host that reaches it and a chain of three jump hosts of which only
the last reaches it: all local SSH protocol fixtures with logins made up on every run (written to the
info file). Through the control port the browser test changes what a verification will meet, and asks
what reached the machines:

    GET /refuse?level=N   the Nth jump host of the chain refuses its saved login from now on
    GET /cut              nothing answers on the robot's port any more
    GET /slow?seconds=S   the robot takes S seconds to accept a login
    GET /scan?name=NAME   the hardware of the robot called NAME counts as being scanned
    GET /restore          everything above is undone
    GET /seen             JSON: logins per machine, verifications and hardware scans that reached the robot

The heartbeat's timer is stopped in every workspace (issue #24 logs in to each robot once a minute and
keeps that session): the logins the test counts are then the verifications' own. How the two live
side by side is checked in tests/test_connection_verify_holds.py.

Nothing here reaches a real machine.
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

from brainos import connection_verify, device_software, device_status, hardware_scan  # noqa: E402
from brainos.server import Application, make_handler  # noqa: E402
from hardware_fixture import jetson  # noqa: E402
from heartbeat_support import FlakyDevice  # noqa: E402
from software_fixture import software  # noqa: E402
from ssh_fixture import SSHFixture  # noqa: E402
from test_product import Server  # noqa: E402
from test_ssh_jump import STATUS_OUTPUT  # noqa: E402

SYSTEM = b'##os\nUbuntu 22.04.4 LTS\n##kernel\nLinux 5.15.148-tegra\n##arch\naarch64\n##end\n'


def seen(robot, jump, chain):
    commands = list(robot.commands)
    return {'robot_logins': len(robot.auth_attempts), 'jump_logins': len(jump.auth_attempts),
            'chain_logins': [len(gate.auth_attempts) for gate in chain],
            'verifications': commands.count(connection_verify.VERIFY_COMMAND),
            'hardware_scans': commands.count(hardware_scan.SCAN_EXEC)}


def control_handler(app, robot, jump, chain, logins):
    def scanning(name, on):
        for workspace in list(app.workspaces.values()):
            for record in workspace.store.list('robots'):
                if record['name'] == name:
                    (workspace.hardware.scanning.add if on else workspace.hardware.scanning.discard)(record['id'])

    def restore():
        for gate, login in zip(chain, logins):
            gate.password = login
        if robot.closed:
            robot.restore()
        robot.auth_delay = 0
        for workspace in list(app.workspaces.values()):
            workspace.hardware.scanning.clear()

    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            url, query = urlparse(self.path), parse_qs(urlparse(self.path).query)
            if url.path == '/refuse':
                chain[int(query['level'][0]) - 1].password = secrets.token_hex(6)
            elif url.path == '/cut':
                robot.cut()
            elif url.path == '/slow':
                robot.auth_delay = float(query['seconds'][0])
            elif url.path == '/scan':
                scanning(query['name'][0], True)
            elif url.path == '/restore':
                restore()
            data = json.dumps(seen(robot, jump, chain)).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return Control


def without_heartbeat(app):
    """Every workspace the console opens from now on does no heartbeat rounds by itself."""
    opened = app.workspace

    def workspace(user):
        result = opened(user)
        result.heartbeat.stop_timer()
        return result
    app.workspace = workspace


if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    without_heartbeat(app)
    api = Server(make_handler(app))
    robot_login = secrets.token_hex(8)
    robot = FlakyDevice(password=robot_login, command_outputs={
        connection_verify.VERIFY_COMMAND: SYSTEM, device_status.status_command([]): STATUS_OUTPUT,
        hardware_scan.SCAN_EXEC: jetson().encode(), device_software.SOFTWARE_EXEC: software().encode()})
    reach_robot = [('127.0.0.1', robot.port)]
    jump = SSHFixture(username='gate', password=secrets.token_hex(6), forward_to=reach_robot)
    # Three jump hosts in a row: this machine reaches the first, each one the next, the last the robot.
    chain, reach = [], robot
    for level in (3, 2, 1):
        reach = SSHFixture(username=f'gate{level}', password=secrets.token_hex(6), forward_to=[('127.0.0.1', reach.port)])
        chain.insert(0, reach)
    logins = [gate.password for gate in chain]
    control = ThreadingHTTPServer(('127.0.0.1', 0), control_handler(app, robot, jump, chain, logins))
    threading.Thread(target=control.serve_forever, daemon=True).start()
    info = {'url': api.url, 'control': f'http://127.0.0.1:{control.server_port}',
            'robot': {'port': robot.port, 'user': 'robot', 'login': robot_login},
            'jump': {'port': jump.port, 'user': jump.username, 'login': jump.password},
            'chain': [{'port': gate.port, 'user': gate.username, 'login': login} for gate, login in zip(chain, logins)]}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-connection-verify-ui.json', 'w') as f:
        json.dump(info, f)
    print(api.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        control.shutdown(); control.server_close()
        api.close(); app.close(); robot.close(); jump.close()
        for gate in chain:
            gate.close()
        tmp.cleanup()
