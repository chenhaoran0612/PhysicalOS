"""Remote calls of a robot's skills (issue #43): the owner's switch per installed skill.

Third parties call a skill through the cloud's open platform; the robot's management layer fetches
those calls itself and runs one only if the owner has switched "允许远程调用" on for that skill
(docs/CLOUD_SKILLS.md, section 3). The switch lives on the robot. This module reads and sets it
through the same path every other ``/v2`` call of the console takes (``connector``: an address, or
the robot's SSH connection):

* ``GET /v2/remote``: whether the robot is linked to the cloud, the switch of each skill and the
  recent remote calls;
* ``POST /v2/remote/skills``: one switch. Switching on needs a person's confirmation
  (``confirmed``), sent on as ``hardware_authorized``, records the signed-in user as ``set_by`` and
  carries the ``digest`` of the installed skill the confirmation was about: the robot refuses (409
  ``skill_changed``) when the skill has become something else in the meantime.
  Switching off needs neither and is never held back by anything here.

A management layer without ``/v2/remote`` (an older version) is not an error: ``supported`` is false
and the page says so in plain words.

What this module returns changes by itself (the link state, the calls). It is asked for by the page
on its own endpoint and never put into the workspace snapshot, which would redraw the page.
"""
import re

from . import connector, robot_caps
from .contracts import required_text
from .errors import CodedError
from .safety_limits import filled_by

REMOTE_PATH, SWITCH_PATH = '/v2/remote', '/v2/remote/skills'
LINK_STATES = ('off', 'connecting', 'online', 'error')
CALL_STATES = ('running', 'succeeded', 'failed', 'cancelled', 'stopped', 'rejected')
MAX_CALLS = 50
MAX_SKILLS = 200
SKILL_ID = re.compile(r'[a-z0-9][a-z0-9_.\-]{0,99}')
DIGEST = re.compile(r'[0-9a-f]{16,128}')
UNSUPPORTED = '这台机器人的管理层还不支持远程调用'
NO_ENDPOINT = '这台机器人还没有连上管理层，读不到远程调用的设置'
CONFIRM_REQUIRED = '打开“允许远程调用”需要你的确认'
MOTION_WARNING = '这个技能会让机器人运动。打开后，第三方可以经云平台让这台机器人运动，不需要有人每次确认。'


def _text(value, limit=200):
    return value[:limit] if isinstance(value, str) else ''


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value else None


def _unsupported(exc):
    """True for the answer of a management layer that has no such endpoint."""
    return exc.status == 404 and exc.code in (None, 'not_found', 'unknown_path')


def link_view(raw):
    raw = raw if isinstance(raw, dict) else {}
    state = raw.get('state') if raw.get('state') in LINK_STATES else 'off'
    return {'configured': raw.get('configured') is True, 'enabled': raw.get('enabled') is True, 'state': state,
            'host': _text(raw.get('host'), 255), 'last_sync_at': _number(raw.get('last_sync_at')),
            'last_error': _text(raw.get('last_error'), 300), 'device_id': _text(raw.get('device_id'), 64)}


def call_view(raw):
    error = raw.get('error') if isinstance(raw.get('error'), dict) else {}
    status = raw.get('status') if raw.get('status') in CALL_STATES else _text(raw.get('status'), 20) or 'unknown'
    return {'call_id': _text(raw.get('call_id'), 64), 'skill_id': _text(raw.get('skill_id'), 100), 'status': status,
            'run_id': _text(raw.get('run_id'), 64), 'started_at': _number(raw.get('started_at')),
            'finished_at': _number(raw.get('finished_at')),
            'error': {'code': _text(error.get('code'), 64), 'message': _text(error.get('message'), 300)} if error else None}


def _capability_ids(items):
    """The ids in a ``requires`` / ``optional`` list of a skill, or None when it is not such a list."""
    if not isinstance(items, list):
        return None
    ids = [x.get('id') if isinstance(x, dict) else x for x in items]
    return ids if all(isinstance(x, str) and x for x in ids) else None


def may_move(skill):
    """True unless every capability the skill may use is known not to move the robot. A skill may use
    what it lists as optional too (the management layer lets it), so those count; a capability the
    standard does not know, or a list that cannot be read, counts as moving."""
    required, optional = _capability_ids(skill.get('requires')), _capability_ids(skill.get('optional', []))
    if required is None or optional is None:
        return True
    return any(robot_caps.capability_row(cap_id)['safety'] != 'none' for cap_id in required + optional)


def installed(robot):
    """``GET /v2/skills`` as this section needs it: with ``optional``, which the shared reader drops.
    Returns (rows, error text or None); never raises."""
    try:
        result = connector.call(robot['endpoint'], '/v2/skills', robot.get('token', ''))
    except connector.DeviceError as exc:
        return [], '读取已安装技能失败：%s' % exc
    skills = result.get('skills')
    if not isinstance(skills, list):
        return [], '设备返回的技能列表格式无效'
    rows = []
    for raw in skills[:MAX_SKILLS]:
        if not isinstance(raw, dict) or not isinstance(raw.get('id'), str):
            continue
        description = _text(raw.get('description'), 500)
        rows.append({'id': _text(raw['id'], 100), 'version': _text(raw.get('version'), 50), 'title': _text(raw.get('title'), 100) or _text(raw['id'], 100),
                     'summary': robot_caps.plain_text(description), 'digest': _text(raw.get('digest'), 128),
                     'requires': raw.get('requires'), 'optional': raw.get('optional', [])})
    return rows, None


def skill_rows(installed, switches):
    """One row per installed skill with its switch. A switch set for other content of the skill (it
    was installed again since) no longer counts: it is shown as off, and ``stale`` says why."""
    switches = switches if isinstance(switches, dict) else {}
    rows = []
    for skill in installed[:MAX_SKILLS]:
        switch = switches.get(skill['id']) if isinstance(switches.get(skill['id']), dict) else {}
        bound = _text(switch.get('digest'), 64)
        stale = switch.get('allowed') is True and bool(bound) and bool(skill['digest']) and bound != skill['digest']
        # The management layer voids the switch itself and says so (``invalidated``); the digest it then
        # shows is the installed one, so the comparison above cannot see it.
        stale = stale or switch.get('invalidated') is True
        rows.append({'id': skill['id'], 'version': skill['version'], 'title': skill['title'], 'summary': skill['summary'],
                     'motion': may_move(skill), 'digest': skill['digest'],
                     'allowed': switch.get('allowed') is True and not stale, 'stale': stale,
                     'set_by': _text(switch.get('set_by'), 200), 'set_at': _number(switch.get('set_at'))})
    return rows


class RemoteCalls:
    def __init__(self, platform):
        self.platform = platform

    def _robot(self, p):
        robot = self.platform.same_project('robots', p.get('robot_id'), p.get('project_id'))
        if not robot.get('endpoint') or robot.get('status') != 'connected':
            raise CodedError(NO_ENDPOINT, 'remote_no_runtime')
        return robot

    @staticmethod
    def _view(robot, raw):
        skills, problem = installed(robot)
        calls = raw.get('calls') if isinstance(raw.get('calls'), list) else []
        return {'supported': True, 'robot_id': robot['id'], 'link': link_view(raw.get('link')),
                'skills': skill_rows(skills, raw.get('skills')), 'skills_error': problem,
                'calls': [call_view(c) for c in calls[:MAX_CALLS] if isinstance(c, dict)],
                'motion_warning': MOTION_WARNING}

    def state(self, p):
        """``robots/remote``: the link, the switches and the recent calls of one robot. Read only."""
        robot = self._robot(p)
        try:
            raw = connector.call(robot['endpoint'], REMOTE_PATH, robot.get('token', ''))
        except connector.DeviceError as exc:
            if _unsupported(exc):
                return {'supported': False, 'robot_id': robot['id'], 'message': UNSUPPORTED}
            # Not reachable right now: the page keeps what it showed and says so; nothing to classify here.
            return {'supported': None, 'robot_id': robot['id'], 'message': '读不到远程调用的状态：%s' % exc,
                    'code': exc.code}
        return self._view(robot, raw)

    def set_skill(self, p, user):
        """``robots/remote/skill``: switch remote calls of one installed skill on or off."""
        allowed = p.get('allowed')
        if not isinstance(allowed, bool):
            raise ValueError('请说明是打开还是关闭')
        skill_id = p.get('skill_id')
        if not isinstance(skill_id, str) or not SKILL_ID.fullmatch(skill_id):
            raise ValueError('技能编号无效')
        request_id = required_text(p.get('request_id'), '请求标识', 100)
        if allowed and p.get('confirmed') is not True:
            raise CodedError(CONFIRM_REQUIRED, 'remote_confirm_required')
        body = {'skill_id': skill_id, 'allowed': allowed, 'set_by': filled_by(user),
                'hardware_authorized': allowed, 'request_id': 'remote-switch-' + request_id}
        if allowed:
            # What the person confirmed: the installed content the dialog was about. The robot refuses
            # (409 ``skill_changed``) when the skill is something else by now.
            digest = p.get('digest')
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                raise ValueError('缺少确认时看到的技能内容摘要；请刷新后重新打开')
            body['digest'] = digest
        robot = self._robot(p)
        try:
            raw = connector.call(robot['endpoint'], SWITCH_PATH, robot.get('token', ''), body)
        except connector.DeviceError as exc:
            if _unsupported(exc):
                raise CodedError(UNSUPPORTED, 'remote_unsupported') from None
            raise
        if raw.get('accepted') is not True:
            raise CodedError('机器人没有确认这次修改；请刷新后看开关现在的状态', 'remote_switch_unconfirmed')
        self.platform.audit('remote.skill_switched', {'robot_id': robot['id'], 'skill_id': skill_id, 'allowed': allowed,
                                                      'set_by': body['set_by']}, robot['project_id'])
        return self._view(robot, raw)
