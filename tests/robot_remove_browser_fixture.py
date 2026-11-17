"""Browser fixture for issue #11: delete a robot from "我的机器人".

Run from the repository root with ``NEUXMIND_MAIL_DEV_ECHO=1``:
``PYTHONPATH=.:tests python tests/robot_remove_browser_fixture.py [info.json]``, then
``node tests/robot_remove_browser.cjs info.json [screenshot dir]``.

The console, one "robot" (a local SSH protocol fixture) that the browser test adds twice, as in
the issue, and a control port that gives a robot a finished investigation, marks it as being
investigated, and reports what is left of a robot in the store and on disk. The login is random
per run and written to the info file. Nothing here reaches a real machine.
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

ROBOT_KINDS = ('device_connections', 'agent_sessions', 'device_evidence', 'robot_inventories', 'hardware_scans',
               'hardware_repairs')


def session_record(robot, status):
    return {'id': 'agent-session-' + secrets.token_hex(6), 'project_id': robot['project_id'], 'robot_id': robot['id'],
            'connection_id': robot['connection_id'], 'mode': 'discovery', 'status': status,
            'name': robot['name'] + ' · 本体调查', 'model': 'fixture-model', 'model_id': 'agent-model-fixture',
            'billing': 'tokenfans', 'objective': '调查本体', 'usage': {}, 'events': [], 'max_tokens': None,
            'max_minutes': None, 'created_at': now(), 'updated_at': now(), 'thread_id': None, 'turn_id': None,
            'pending_question': None}


def control_handler(app):
    """GET /investigated?robot=ID  a finished investigation of that robot, with a file it wrote and one evidence record
    GET /busy?robot=ID&on=1|0     start or end a running investigation of that robot
    GET /scanning?robot=ID&on=1|0 mark that robot's hardware as being scanned, or not
    GET /left?robot=ID            JSON: what the store and the disk still hold about that robot"""
    running, written = {}, {}

    class Control(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            store = next(iter(app.workspaces.values())).store
            robot_id, body = query['robot'][0], {'ok': True}
            if url.path == '/left':
                body = {kind: sum(1 for x in store.list(kind) if x.get('robot_id') == robot_id) for kind in ROBOT_KINDS}
                body['robots'] = sum(1 for x in store.list('robots') if x['id'] == robot_id)
                body['files'] = sum(1 for path in written.get(robot_id, []) if path.exists())
            elif url.path == '/investigated':
                robot = store.get('robots', robot_id)
                session = store.put('agent_sessions', session_record(robot, 'interrupted'))
                work = store.root / 'engineering' / session['id'] / 'workspace'
                work.mkdir(parents=True)
                (work / 'DEVICE_CONTEXT.json').write_text('{"robot": "read from the device"}')
                written.setdefault(robot_id, []).append(work / 'DEVICE_CONTEXT.json')
                store.put('device_evidence', {'id': 'evidence-' + secrets.token_hex(6), 'project_id': robot['project_id'],
                                              'robot_id': robot_id, 'session_id': session['id'],
                                              'connection_id': robot['connection_id'], 'tool': 'device_probe',
                                              'arguments': {'probe': 'system'}, 'result': {'stdout': 'Linux'},
                                              'created_at': now()})
            elif url.path == '/scanning':
                scanning = next(iter(app.workspaces.values())).hardware.scanning
                (scanning.add if query['on'][0] == '1' else scanning.discard)(robot_id)
            elif query['on'][0] == '1':
                running[robot_id] = store.put('agent_sessions', session_record(store.get('robots', robot_id), 'running'))['id']
            else:
                store.delete('agent_sessions', running.pop(robot_id))
            data = json.dumps(body).encode()
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
    robot = SSHFixture(password=login, command_outputs={
        device_status.status_command([]): STATUS_OUTPUT, hardware_scan.SCAN_EXEC: jetson().encode(),
        device_software.SOFTWARE_EXEC: software().encode()})
    control = ThreadingHTTPServer(('127.0.0.1', 0), control_handler(app))
    threading.Thread(target=control.serve_forever, daemon=True).start()
    info = {'url': api.url, 'control': f'http://127.0.0.1:{control.server_port}', 'robot_user': 'robot',
            'robot_login': login, 'robot_port': robot.port}
    with open(sys.argv[1] if len(sys.argv) > 1 else '/tmp/neuxmind-robot-remove-ui.json', 'w') as f:
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
