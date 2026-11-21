"""Safety limits of a device, filled in by a person (issue #22, requirement R1-06).

How fast a device may go is not guessed by the platform. A person fills in, for each device, its
maximum linear speed, its maximum angular speed and how long a motion command lasts without being
refreshed. Until that is done the device refuses every motion command (HTTP 409
``safety_limits_missing``): no capability can make it move.

The device keeps the values, checks them and enforces them. The console is the way to them:

* ``read``  asks the device what it holds (``GET /v2/safety/limits``): the values, who filled them in
  and when, what is enforced, and the range the device accepts for each value;
* ``save``  sends what a person filled in (``POST /v2/safety/limits``) and records it in the audit log.

Rules of this side:

* Who filled the values in is the signed-in account, decided here. It is never taken from a request.
* The device is the authority on what is allowed. A value is checked here only as far as the form
  needs a field to mark (it is there, it is a finite number); a refusal of the device is passed on
  as it is, with its ``code`` and the field it is about.
* A write is sent once. Whatever happens to it, it is never sent again by the console.
* What the device says is kept on the robot record (``device_status.safety_limits``, the same
  summary ``/v1/status`` carries at a connection check), where the robot's card reads it.
* A device whose management layer does not have this API yet (HTTP 404) is not an error when
  reading: the answer says so, and that the device has to be upgraded.

Only the chassis group (``base``) exists so far; the device names the groups it knows.
"""
import math

from . import connector
from .contracts import required_text
from .errors import CodedError

LIMITS_PATH = '/v2/safety/limits'
SCHEMA = 'neuxmind.device_limits.v1'
GROUP = 'base'
FIELD_NAMES = {'max_linear_speed': '最大线速度', 'max_angular_speed': '最大角速度', 'command_timeout_s': '运动指令超时'}
BASE_FIELDS = tuple(FIELD_NAMES)
# What ``/v1/status`` carries as ``safety_limits`` and the robot record keeps for the robot's card.
SUMMARY_KEYS = ('required', 'filled', 'motion_blocked', 'reason', 'limits', 'set_by', 'set_at')
INVALID = 'limits_invalid'              # the device's code for values that are not usable; used here for the same
MISSING_API_CODES = (None, 'not_found')
HISTORY_SHOWN = 20
GROUPS_SHOWN = 10
SET_BY_MAX = 200
REASON_MAX = 500
REQUEST_ID_MAX = 200

UNSUPPORTED = ('这台设备上的管理层（NeuxMind 运行时）版本较旧，还不能填写安全限值。'
               '请先把设备上的运行时升级到支持安全限值的版本，再回来填写。')
NO_ENDPOINT = ('这台机器人还没有配置设备端服务地址，读不到也填不了安全限值。'
               '请先配置连接，并做一次“连接检查”。')
NOT_SIGNED_IN = '需要登录后才能填写安全限值：填写人会和限值一起记录'
NOT_UNDERSTOOD = '设备返回的安全限值格式无法识别；请确认设备上的运行时和这台电脑上的应用版本相配'
NOT_CONFIRMED = '设备没有确认保存安全限值；请重新读取，核对设备上现在的限值。不会自动重发'
SAVED_NOT_UNDERSTOOD = ('设备确认了保存，但它返回的安全限值格式无法识别；'
                        '请重新读取，核对设备上现在的限值。不会自动重发')
NOTHING_FILLED = '请填写安全限值：最大线速度、最大角速度和运动指令超时'


def _number(value):
    """A finite number (never a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:               # an int too large for a float
        return None


def _text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def _revision(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _values(raw):
    """``{"base": {field: number}}`` with the known fields a device sent, or None when it sent none."""
    base = raw.get(GROUP) if isinstance(raw, dict) else None
    if not isinstance(base, dict):
        return None
    values = {name: _number(base.get(name)) for name in BASE_FIELDS}
    values = {name: value for name, value in values.items() if value is not None}
    return {GROUP: values} if values else None


def _bounds(raw):
    base = raw.get(GROUP) if isinstance(raw, dict) else None
    out = {}
    for name in BASE_FIELDS:
        bound = base.get(name) if isinstance(base, dict) else None
        if isinstance(bound, dict):
            out[name] = {'unit': _text(bound.get('unit'), 20) or '', 'min': _number(bound.get('min')),
                         'min_exclusive': bound.get('min_exclusive') is True, 'max': _number(bound.get('max')),
                         'platform_max': _number(bound.get('platform_max')), 'body_max': _number(bound.get('body_max'))}
    return {GROUP: out}


def _history(raw):
    entries = raw[:HISTORY_SHOWN] if isinstance(raw, list) else []
    return [{'revision': _revision(entry.get('revision')), 'limits': _values(entry.get('limits')),
             'set_by': _text(entry.get('set_by'), SET_BY_MAX), 'set_at': _number(entry.get('set_at'))}
            for entry in entries if isinstance(entry, dict)]


def device_view(body):
    """What a device answered on ``/v2/safety/limits``, reduced to the parts the console knows.

    The answer comes from outside: only known keys are passed on, each with the type it should
    have. Raises ValueError when it is not a limits view at all."""
    if not isinstance(body, dict) or body.get('schema') != SCHEMA:
        raise ValueError(NOT_UNDERSTOOD)
    required, filled = body.get('required'), body.get('filled')
    if not isinstance(required, bool) or not isinstance(filled, bool):
        raise ValueError(NOT_UNDERSTOOD)
    blocked = body.get('motion_blocked')
    groups = body.get('groups') if isinstance(body.get('groups'), list) else []
    return {'required': required, 'filled': filled,
            'motion_blocked': blocked if isinstance(blocked, bool) else required and not filled,
            'reason': _text(body.get('reason'), REASON_MAX), 'limits': _values(body.get('limits')),
            'set_by': _text(body.get('set_by'), SET_BY_MAX), 'set_at': _number(body.get('set_at')),
            'revision': _revision(body.get('revision')), 'effective': _values(body.get('effective')),
            'bounds': _bounds(body.get('bounds')),
            'groups': [g[:40] for g in groups[:GROUPS_SHOWN] if isinstance(g, str) and g],
            'history': _history(body.get('history')), 'invalid': _text(body.get('invalid'), REASON_MAX)}


def requested_limits(raw):
    """The three values of a request, checked only as far as the form needs a field to mark: each is
    there and is a finite number. Whether a value is allowed is the device's decision."""
    base = raw.get(GROUP) if isinstance(raw, dict) else None
    if not isinstance(base, dict):
        raise CodedError(NOTHING_FILLED, INVALID, field=GROUP)
    values = {}
    for name in BASE_FIELDS:
        value = _number(base.get(name))
        if value is None:
            raise CodedError(f'请填写{FIELD_NAMES[name]}：需要一个数字', INVALID, field=name)
        values[name] = value
    return {GROUP: values}


def filled_by(user):
    """Who is recorded with the limits: the account's display name, else its e-mail."""
    for key in ('name', 'email'):
        value = user.get(key) if isinstance(user, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(NOT_SIGNED_IN)


def _api_missing(exc):
    return exc.status == 404 and exc.code in MISSING_API_CODES


class SafetyLimits:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store

    def read(self, p):
        """``robots/limits``: what the device of ``robot_id`` (in ``project_id``) holds.

        Returns ``{robot_id, supported: True, ...view}``, or ``{robot_id, supported: False, reason}``
        for a device without this API. A device that cannot be reached raises."""
        robot = self._robot(p)
        try:
            body = connector.call(robot['endpoint'], LIMITS_PATH, robot.get('token', ''))
        except connector.DeviceError as exc:
            if _api_missing(exc):
                return {'robot_id': robot['id'], 'supported': False, 'reason': UNSUPPORTED}
            raise
        view = device_view(body)
        self._remember(robot['id'], view)
        return {'robot_id': robot['id'], 'supported': True, **view}

    def save(self, p, user):
        """``robots/limits/save``: send ``limits`` to the device as filled in by ``user``, the signed-in
        account. Returns the device's view after the write. A refusal of the device raises as it came."""
        robot = self._robot(p)
        request = {'limits': requested_limits(p.get('limits')), 'set_by': filled_by(user)}
        if p.get('request_id') is not None:
            request['request_id'] = required_text(p['request_id'], '请求标识', REQUEST_ID_MAX)
        try:
            body = connector.call(robot['endpoint'], LIMITS_PATH, robot.get('token', ''), request)
        except connector.DeviceError as exc:
            if _api_missing(exc):
                raise ValueError(UNSUPPORTED) from None
            raise                           # the device's own refusal or an unknown outcome: never sent again
        if body.get('accepted') is not True:
            raise ValueError(NOT_CONFIRMED)
        try:
            view = device_view(body)
        except ValueError:
            raise ValueError(SAVED_NOT_UNDERSTOOD) from None
        self._remember(robot['id'], view)
        details = {'robot_id': robot['id'], 'limits': view['limits'], 'set_by': view['set_by'] or request['set_by'],
                   'set_at': view['set_at'], 'revision': view['revision']}
        if 'request_id' in request:
            details['request_id'] = request['request_id']
        self.platform.audit('robot.limits_set', details, robot['project_id'])
        return {'robot_id': robot['id'], 'supported': True, 'accepted': True, **view}

    def _robot(self, p):
        robot = self.platform.same_project('robots', p.get('robot_id'), p.get('project_id'))
        if not robot.get('endpoint'):
            raise ValueError(NO_ENDPOINT)
        return robot

    def _remember(self, robot_id, view):
        """Keep the device's summary on the robot record, where the robot's card reads it."""
        summary = {key: view[key] for key in SUMMARY_KEYS}
        with self.store.atomic():
            try:
                robot = self.store.get('robots', robot_id)
            except ValueError:
                return                      # deleted while the device was answering: nothing to keep it on
            status = robot.get('device_status') if isinstance(robot.get('device_status'), dict) else {}
            if status.get('safety_limits') != summary:
                self.store.put('robots', {**robot, 'device_status': {**status, 'safety_limits': summary}})
