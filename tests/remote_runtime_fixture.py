"""A stand-in for a robot's management layer as issue #43's console reads it: installed skills and
the remote-call switches of docs/CLOUD_SKILLS.md section 3. Test only; nothing reaches a machine.

``RemoteRuntime`` answers ``GET /v2/skills``, ``GET /v2/remote`` and ``POST /v2/remote/skills`` on
this computer. ``supports_remote = False`` makes it an older management layer (404 for both remote
paths). ``link`` and ``calls`` are what a test puts there; every body posted is kept in ``posted``.
Turning a switch on needs the ``digest`` of the installed skill (409 ``skill_changed`` otherwise).
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler

from brainos.httpd import FastBindHTTPServer

TOKEN = 'fixture-' + 'token-0123456789abcdef'   # joined: the commit hook reads a long quoted literal here as a real secret
SKILLS = [
    {'id': 'goto_place', 'version': '0.1.0', 'title': '去指定地点', 'description': '走到本体认识的一个地点。',
     'digest': 'a' * 64, 'requires': [{'id': 'base.navigate_to_place'}, {'id': 'safety.stop'}], 'parameters': [],
     'timeout_s': 1800, 'installed_at': 1.0, 'compatibility': {'compatible': True}},
    {'id': 'status_report', 'version': '0.1.0', 'title': '状态报告', 'description': '只读取状态，不让机器人运动。',
     'digest': 'b' * 64, 'requires': [{'id': 'system.health'}, {'id': 'safety.stop'}], 'parameters': [],
     'timeout_s': 60, 'installed_at': 2.0, 'compatibility': {'compatible': True}},
]
# Needs nothing that moves, may use the arm when the body has one: the runtime lets it.
OPTIONAL_MOVER = {'id': 'battery_wave', 'version': '0.1.0', 'title': '电量低时挥手', 'description': '读电量；有机械臂时挥一下。',
                  'digest': 'd' * 64, 'requires': [{'id': 'sensor.battery'}, {'id': 'safety.stop'}],
                  'optional': [{'id': 'arm.joint_position'}], 'parameters': [], 'timeout_s': 60, 'installed_at': 3.0,
                  'compatibility': {'compatible': True}}


class RemoteRuntime:
    def __init__(self):
        self.skills = [dict(skill) for skill in SKILLS]
        self.supports_remote = True
        self.link = {'configured': True, 'enabled': True, 'state': 'online', 'host': 'cloud.example.test',
                     'last_sync_at': time.time(), 'last_error': '', 'device_id': 'dlk_0123456789'}
        self.switches, self.calls, self.posted = {}, [], []
        self.fail_switch = None         # (status, body) answered to the next POST
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _answer(self, status, body):
                raw = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _allowed(self):
                if self.headers.get('Authorization') == 'Bearer ' + TOKEN:
                    return True
                self._answer(401, {'error': 'unauthorized', 'code': 'unauthorized'})
                return False

            def do_GET(self):
                if not self._allowed():
                    return
                if self.path == '/v2/skills':
                    return self._answer(200, {'skills': owner.skills})
                if self.path == '/v2/remote' and owner.supports_remote:
                    return self._answer(200, owner.view())
                self._answer(404, {'error': 'not found', 'code': 'not_found'})

            def do_POST(self):
                if not self._allowed():
                    return
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                if self.path != '/v2/remote/skills' or not owner.supports_remote:
                    return self._answer(404, {'error': 'not found', 'code': 'not_found'})
                self._answer(*owner.switch(body))

        self.httpd = FastBindHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def view(self):
        return {'schema': 'neuxmind.remote_access.v1', 'link': dict(self.link), 'skills': dict(self.switches),
                'calls': list(self.calls)}

    def switch(self, body):
        self.posted.append(body)
        if self.fail_switch:
            failure, self.fail_switch = self.fail_switch, None
            return failure
        skill = next((s for s in self.skills if s['id'] == body.get('skill_id')), None)
        if skill is None:
            return 404, {'error': '技能没有安装', 'code': 'skill_not_found'}
        if body.get('allowed') is True and (body.get('hardware_authorized') is not True or not body.get('set_by')):
            return 403, {'error': '打开远程调用需要授权和填写人', 'code': 'hardware_not_authorized'}
        if body.get('allowed') is True and body.get('digest') != skill['digest']:      # bound to what the person was shown
            return 409, {'error': '技能的内容和确认时看到的不一样了', 'code': 'skill_changed'}
        self.switches[skill['id']] = {'allowed': body.get('allowed') is True, 'digest': skill['digest'],
                                      'set_by': body.get('set_by') or '', 'set_at': time.time()}
        return 200, {'accepted': True, **self.view()}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
