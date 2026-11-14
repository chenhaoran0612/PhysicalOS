"""Browser fixture for issue #9: change the SSH connection of a connected robot.

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/ssh_edit_browser_fixture.py [info.json]``, then
``node tests/ssh_edit_browser.cjs info.json [screenshot dir]``.

The console, two "robots" with different host keys and a "jump host" (local SSH protocol
fixtures), and a control port that marks a robot as being investigated. The logins are random
per run and written to the info file. Nothing here reaches a real machine.

Each robot runs one service named after it (``dog-a-telemetry``), so the status page (issue #8)
shows which machine its software list was read from.
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
from software_fixture import SERVICES as RUNNING_SERVICES, software  # noqa: E402
from ssh_fixture import SSHFixture  # noqa: E402
from test_product import Server  # noqa: E402
from test_ssh_jump import STATUS_OUTPUT  # noqa: E402

WATCH_LISTS = ([], ['bridge'], ['bridge', 'nav-stack'])      # every watch list the browser test sets
FIXTURE_SESSION = 'agent-session-fixture-running'


def robot(hostname, login):
    outputs = {device_status.status_command(names): STATUS_OUTPUT.replace(b'dog-fixture', hostname.encode())
               for names in WATCH_LISTS}
    running = RUNNING_SERVICES + f'{hostname}-telemetry.service        loaded active     running      Telemetry of {hostname}\n'
    return SSHFixture(password=login, command_outputs={**outputs, hardware_scan.SCAN_EXEC: jetson().encode(),
                                                       device_software.SOFTWARE_EXEC: software(services=running).encode()})


def control_handler(app):
    """GET /busy?robot=NAME&on=1|0: put or remove a running investigation of that robot in the store."""
    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            store = next(iter(app.workspaces.values())).store
            target = next(r for r in store.list('robots') if r['name'] == query['robot'][0])
            if query['on'][0] == '1':
                store.put('agent_sessions', {
                    'id': FIXTURE_SESSION, 'project_id': target['project_id'], 'robot_id': target['id'],
                    'connection_id': target['connection_id'], 'mode': 'discovery', 'status': 'running',
                    'name': target['name'] + ' · 本体调查', 'model': 'fixture-model', 'model_id': 'agent-model-fixture',
                    'billing': 'tokenfans', 'objective': '调查本体', 'usage': {}, 'events': [], 'max_tokens': None,
                    'max_minutes': None, 'created_at': now(), 'updated_at': now(), 'thread_id': None, 'turn_id': None,
                    'pending_question': None})
            else:
                store.delete('agent_sessions', FIXTURE_SESSION)
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'ok')
    return Control


if __name__ == '__main__':
    tmp = tempfile.TemporaryDirectory()
    app = Application(tmp.name)
    api = Server(make_handler(app))
    logins = {'robot': secrets.token_hex(8), 'jump': secrets.token_hex(8)}
    first, second = robot('dog-a', logins['robot']), robot('dog-b', logins['robot'])
    jump = SSHFixture(username='gate', password=logins['jump'],
                      forward_to=[('127.0.0.1', first.port), ('127.0.0.1', second.port)])
    control = ThreadingHTTPServer(('127.0.0.1', 0), control_handler(app))
    threading.Thread(target=control.serve_forever, daemon=True).start()
    info = {'url': api.url, 'control': f'http://127.0.0.1:{control.server_port}', 'robot_user': 'robot',
            'robot_login': logins['robot'], 'first_port': first.port, 'second_port': second.port,
            'jump_port': jump.port, 'jump_user': 'gate', 'jump_login': logins['jump']}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-ssh-edit-ui.json', 'w') as f:
        json.dump(info, f)
    print(api.url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        control.shutdown(); control.server_close()
        api.close(); app.close(); first.close(); second.close(); jump.close(); tmp.cleanup()
