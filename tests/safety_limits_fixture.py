"""Protocol double of a device that keeps safety limits (issue #22, R1-06). Test only.

It answers the part of the device API the console uses for safety limits and moves nothing:

* ``GET  /v2/safety/limits``  what the device holds: the limits a person filled in, who and when, what
  it enforces, and the range it accepts for each value;
* ``POST /v2/safety/limits``  fill them in or change them (all three values, a named person);
* ``GET  /v1/status``         carries the short ``safety_limits`` summary the robot cards show;
* ``GET  /v1/manifest``, ``/v2/manifest``, ``/v2/skills``  enough for a connection check and the
  robot's detail page.

``supported=False`` is a device with an older management layer: the two limit calls answer 404.
Messages, field names and the order of the checks follow ``runtime/neuxmind_runtime/device_limits.py``.
The access token is made up for every instance. Used by ``tests/test_safety_limits.py`` and
``tests/safety_limits_browser_fixture.py``.
"""
import json
import math
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler

from brainos.httpd import FastBindHTTPServer

SCHEMA = 'neuxmind.device_limits.v1'
LIMITS_PATH = '/v2/safety/limits'
GROUP = 'base'
# field -> (unit, minimum, minimum is exclusive, the platform's maximum)
PLATFORM = {'max_linear_speed': ('m/s', 0.0, True, 3.0),
            'max_angular_speed': ('rad/s', 0.0, True, 3.0),
            'command_timeout_s': ('s', 0.05, False, 2.0)}
FIELDS = tuple(PLATFORM)
BODY_MAX = {'max_linear_speed': 1.0, 'max_angular_speed': 1.5}      # what the body itself declares
MISSING = '这台设备还没有填写安全限值；填写之前，任何能力都不能让它运动'
HISTORY_MAX = 20
INSTANCE = 'limits-double'


class Refusal(Exception):
    def __init__(self, status, message, code, **extra):
        super().__init__(message)
        self.status, self.body = status, {'error': message, 'code': code, **extra}


class LimitsDevice:
    """One device. ``required=False`` is a simulated body that brings its own limits."""

    def __init__(self, required=True, body_max=None, supported=True):
        self.token = secrets.token_hex(16)
        self.required, self.supported = required, supported
        self.body_max = dict(BODY_MAX if body_max is None else body_max)
        self.limits = self.set_by = self.set_at = None
        self.invalid = None             # why the record stored on the device was not used, if it was not
        self.revision = 0
        self.history = []
        self.busy = False               # a run is active: a change is refused
        self.conformance = False        # a conformance test is running: likewise
        self.fail_status = None         # answer the next write with this HTTP status and no JSON
        self.calls, self.posts = [], []
        self.answers = {}               # request_id -> (what was asked, status, answer) once it got past the checks
        self.lock = threading.Lock()
        self.closed = False
        self.http = FastBindHTTPServer(('127.0.0.1', 0), self._handler())
        self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.http.server_port}'

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(5)

    # ------------------------------------------------------------ what the device says
    def bounds(self):
        out = {}
        for name, (unit, low, exclusive, platform_max) in PLATFORM.items():
            body = self.body_max.get(name)
            out[name] = {'unit': unit, 'min': low, 'min_exclusive': exclusive,
                         'max': platform_max if body is None else min(platform_max, body),
                         'platform_max': platform_max, 'body_max': body}
        return {GROUP: out}

    def effective(self):
        if self.limits is None:
            return None
        return {GROUP: {name: value if self.body_max.get(name) is None else min(value, self.body_max[name])
                        for name, value in self.limits[GROUP].items()}}

    def summary(self):
        filled = self.limits is not None
        blocked = self.required and not filled
        reason = f'设备上保存的安全限值无效（{self.invalid}），已按没有填写处理。{MISSING}' if self.invalid else MISSING
        return {'required': self.required, 'filled': filled, 'motion_blocked': blocked,
                'reason': reason if blocked else None, 'limits': self.limits, 'set_by': self.set_by,
                'set_at': self.set_at}

    def view(self):
        return {'schema': SCHEMA, **self.summary(), 'revision': self.revision, 'effective': self.effective(),
                'bounds': self.bounds(), 'groups': [GROUP], 'history': self.history[:HISTORY_MAX],
                'invalid': self.invalid}

    def fill(self, linear, angular, timeout_s, who):
        """Fill the limits in as if it had been done before (no request goes over HTTP)."""
        return self._store({'max_linear_speed': linear, 'max_angular_speed': angular,
                            'command_timeout_s': timeout_s}, who)

    # ------------------------------------------------------------ a write
    def check(self, payload):
        """Shape, range and who: answered before the request id is looked at. Returns (values, who)."""
        if self.conformance:
            raise Refusal(409, '设备正在做一致性测试，这期间不能修改安全限值', 'conformance_running')
        limits = payload.get('limits')
        if not isinstance(limits, dict) or set(limits) != {GROUP} or not isinstance(limits[GROUP], dict):
            raise Refusal(400, '安全限值需要 limits.base 这一组', 'limits_invalid')
        values = limits[GROUP]
        for name in values:
            if name not in PLATFORM:
                raise Refusal(400, f'{GROUP} 分组里没有这些项目：{name}', 'limits_invalid', field=name)
        for name in FIELDS:
            value = values.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise Refusal(400, f'{name} 须为有限数值', 'limits_invalid', field=name)
        bounds = self.bounds()[GROUP]
        for name in FIELDS:
            value, bound = values[name], bounds[name]
            below = value <= bound['min'] if bound['min_exclusive'] else value < bound['min']
            if below or value > bound['max']:
                source = 'body' if not below and bound['body_max'] is not None and bound['body_max'] < bound['platform_max'] \
                    else 'platform'
                where = '这台本体自己声明的上限' if source == 'body' else '平台允许的上限'
                raise Refusal(400, f'{name} 填的是 {value!r}，允许的范围不超过 {bound["max"]:g}（{where}）',
                              'limits_out_of_range', field=name, value=value, min=bound['min'], max=bound['max'],
                              bound=source)
        who = payload.get('set_by')
        if not isinstance(who, str) or not who.strip():
            raise Refusal(400, '需要填写人（set_by）：安全限值由人填写，设备会记下是谁填的', 'set_by_required')
        return {name: float(values[name]) for name in FIELDS}, who.strip()

    def apply(self, values, who):
        if self.busy:
            raise Refusal(409, '有运行尚未结束，或速度指令仍在生效：这时不能修改安全限值。请等它结束，或先停止', 'runs_active')
        return {'accepted': True, **self._store(values, who)}

    def _store(self, values, who):
        self.revision += 1
        self.limits, self.set_by, self.set_at, self.invalid = {GROUP: values}, who, time.time(), None
        self.history.insert(0, {'revision': self.revision, 'limits': self.limits, 'set_by': who, 'set_at': self.set_at})
        return self.view()

    # ------------------------------------------------------------ HTTP
    def manifest_v1(self):
        return {'schema': 'robot.adapter.v1', 'instance_id': INSTANCE, 'name': '安全限值测试替身', 'family': 'quadruped',
                'adapter_version': '0.1.0', 'capabilities': ['safety.stop', 'system.health', 'base.velocity'],
                'simulated': not self.required, 'vendor': 'Fixture', 'model': 'limits-double'}

    def manifest_v2(self):
        return {'schema': 'robot.adapter.v2', 'spec_version': '1.0', 'instance_id': INSTANCE,
                'name': '安全限值测试替身', 'adapter_version': '0.1.0', 'vendor': 'Fixture', 'model': 'limits-double',
                'family': 'quadruped', 'simulated': not self.required,
                'capabilities': [{'id': 'safety.stop', 'params': {'hardware_estop': True}}, {'id': 'system.health'},
                                 {'id': 'base.velocity', 'params': {'max_vx': 1.0, 'max_vy': 0.0, 'max_wz': 1.5,
                                                                    'holonomic': False}}],
                'model_runtimes': []}

    def status(self):
        status = {'instance_id': INSTANCE, 'mode': 'idle', 'estop_latched': False, 'latch_reason': None,
                  'simulated': not self.required}
        return {**status, 'safety_limits': self.summary()} if self.supported else status

    def _handler(self):
        device = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, status=200):
                data = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def allowed(self):
                if self.headers.get('Authorization') == 'Bearer ' + device.token:
                    return True
                self.send({'error': '访问令牌不对', 'code': 'unauthorized'}, 401)
                return False

            def unknown(self):
                self.send({'error': '未知接口', 'code': 'not_found'}, 404)

            def do_GET(self):
                if not self.allowed():
                    return
                device.calls.append('GET ' + self.path)
                answers = {'/v1/manifest': device.manifest_v1, '/v2/manifest': device.manifest_v2,
                           '/v1/status': device.status, '/v2/skills': lambda: {'skills': []}}
                if self.path in answers:
                    return self.send(answers[self.path]())
                if self.path == LIMITS_PATH and device.supported:
                    with device.lock:
                        return self.send(device.view())
                return self.unknown()

            def do_POST(self):
                if not self.allowed():
                    return
                payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
                device.calls.append('POST ' + self.path)
                if self.path != LIMITS_PATH or not device.supported:
                    return self.unknown()
                with device.lock:
                    device.posts.append(payload)
                    if device.fail_status:
                        status, device.fail_status = device.fail_status, None
                        self.send_response(status)
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return None
                    try:
                        values, who = device.check(payload)
                    except Refusal as refusal:
                        return self.send(refusal.body, refusal.status)
                    request_id, asked = payload.get('request_id'), json.dumps(payload, sort_keys=True)
                    if request_id in device.answers:
                        before, status, answer = device.answers[request_id]
                        if before != asked:        # an id belongs to one request: other content under it is refused
                            return self.send({'error': '这个请求标识已经用于另一次请求', 'code': 'request_id_reused'}, 409)
                        return self.send(answer, status)       # the same request again: the same answer, no new write
                    try:
                        status, answer = 200, device.apply(values, who)
                    except Refusal as refusal:
                        status, answer = refusal.status, refusal.body
                    if request_id is not None:
                        device.answers[request_id] = (asked, status, answer)
                    return self.send(answer, status)

        return Handler
