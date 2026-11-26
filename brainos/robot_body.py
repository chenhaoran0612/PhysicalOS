"""A robot's body for the management layer: simulated, or the robot's own ROS 2 (issue #38).

Until now "安装管理层" (issue #19) always installed a simulated body: it opens no device and cannot move
the robot, so a skill that sends a quadruped to a place only pretended to. A robot whose card says
"真实本体" gets the install script's ``NX_ADAPTER=ros2`` instead: the management layer joins the robot's
own ROS 2, and what it declares (its capabilities, the places it can be sent to) comes from the robot.

What a person sets here, per robot (kept on the robot record as ``body``):

* ``kind``: ``sim`` (the default) or ``ros2``;
* the ROS 2 settings the install is made with: the setup file (ROS 2 Humble, or a workspace on top of
  it) and any of ``ROS_DOMAIN_ID``, ``ROS_LOCALHOST_ONLY``, ``RMW_IMPLEMENTATION`` and the RMW's own
  configuration file. A robot's nodes often run with settings its login shell does not have, so
  ``detect`` reads them from the programs that run there (read only);
* the device configuration (``ros2-config.json`` of ``runtime/README.md`` §3): topic names, limits and,
  for a robot that brings its own navigation programs, the places it knows and the programs that go
  there (``neuxmind_runtime.adapters.programs``). It is checked here with the adapter's own parser, so
  a mistake is found before anything is sent; the coordinates of a place stay in the robot's programs.

Nothing here reaches a robot except ``detect``. Installing with these settings and putting a changed
configuration on the robot are jobs of ``runtime_install`` (``start`` and ``apply_body``).
"""
import hashlib
import json
import re
import threading
import time

from . import device_access, runtime_package
from . import runtime_install_device as dev
from .errors import CodedError
from .service import now

KINDS = ('sim', 'ros2')
DEFAULT_SETUP = '/opt/ros/humble/setup.bash'
CONFIG_MAX_BYTES = 256 * 1024
DETECT_SECONDS = 60
_PATH = re.compile(r'/[A-Za-z0-9._/+-]{1,200}', re.ASCII)
_URI = re.compile(r'(file://)?/[A-Za-z0-9._/+-]{1,200}', re.ASCII)
_RULES = {
    'ROS_DOMAIN_ID': (re.compile(r'[0-9]{1,3}', re.ASCII), 'ROS_DOMAIN_ID 须为 0–232 的整数'),
    'ROS_LOCALHOST_ONLY': (re.compile(r'[01]', re.ASCII), 'ROS_LOCALHOST_ONLY 只能是 0 或 1'),
    'RMW_IMPLEMENTATION': (re.compile(r'rmw_[a-z0-9_]{1,60}', re.ASCII), 'RMW_IMPLEMENTATION 须形如 rmw_fastrtps_cpp'),
    'FASTRTPS_DEFAULT_PROFILES_FILE': (_PATH, 'FASTRTPS_DEFAULT_PROFILES_FILE 须为设备上的绝对路径（只含字母、数字和 . _ / + -）'),
    'CYCLONEDDS_URI': (_URI, 'CYCLONEDDS_URI 须为设备上配置文件的绝对路径（可带 file://）；直接写 XML 内容不支持'),
    'ZENOH_SESSION_CONFIG_URI': (_PATH, 'ZENOH_SESSION_CONFIG_URI 须为设备上的绝对路径（只含字母、数字和 . _ / + -）'),
}
MAX_DOMAIN_ID = 232
BAD_KIND = '本体须为“仿真本体”或“真实本体（ROS 2）”'
BAD_SETUP = 'ROS 2 环境文件须为设备上的绝对路径（只含字母、数字和 . _ / + -），例如 /opt/ros/humble/setup.bash'
BAD_SETTINGS = 'ROS 2 设置须为对象：{名称: 值}'
UNKNOWN_SETTING = '不认识的 ROS 2 设置：{name}（可用：{known}）'
CONFIG_TOO_BIG = '本体配置太大（%d KB 以内）' % (CONFIG_MAX_BYTES // 1024)
CONFIG_INVALID = '本体配置有问题：{reason}'
DETECT_FAILED = '没能从机器人读到 ROS 2 的情况：{reason}'
ONLY_SSH = '只有通过 SSH 接入的真实机器人才能设置本体'


def clean_settings(raw):
    """The ROS settings a person gave, as ``{KEY: text}`` with empty ones left out. Raises CodedError."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise CodedError(BAD_SETTINGS, 'body_settings_invalid')
    settings = {}
    for name, value in raw.items():
        if name not in _RULES:
            raise CodedError(UNKNOWN_SETTING.format(name=str(name)[:60], known='、'.join(_RULES)), 'body_settings_invalid')
        text = '' if value is None else str(value).strip()
        if text == '':
            continue
        pattern, message = _RULES[name]
        if not pattern.fullmatch(text) or (name == 'ROS_DOMAIN_ID' and int(text) > MAX_DOMAIN_ID):
            raise CodedError(message, 'body_settings_invalid')
        settings[name] = str(int(text)) if name == 'ROS_DOMAIN_ID' else text
    return {name: settings[name] for name in dev.ROS_KEYS if name in settings}


def clean_setup(value):
    text = DEFAULT_SETUP if value in (None, '') else str(value).strip()
    if not _PATH.fullmatch(text) or '/../' in text + '/':
        raise CodedError(BAD_SETUP, 'body_settings_invalid')
    return text


def _merged(user):
    """The adapter's defaults with ``user`` laid over them, the way ``ros2.load_config`` does."""
    from neuxmind_runtime.adapters import ros2
    config = json.loads(json.dumps(ros2.DEFAULT_CONFIG))
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def clean_config(text):
    """``(configuration or None, what it declares)`` of a device configuration typed or imported as JSON.
    Checked with the adapter's own parser and manifest builder (no ROS 2 needed). Raises CodedError."""
    if text is None or (isinstance(text, str) and not text.strip()):
        return None, None
    if isinstance(text, dict):
        text = json.dumps(text, ensure_ascii=False)
    if not isinstance(text, str):
        raise CodedError(CONFIG_INVALID.format(reason='须为 JSON 文本'), 'body_config_invalid')
    if len(text.encode('utf-8', 'replace')) > CONFIG_MAX_BYTES:
        raise CodedError(CONFIG_TOO_BIG, 'body_config_invalid')
    from neuxmind_runtime.adapters import ros2
    try:
        user = ros2.parse_config_text(text)
        manifest = ros2.build_manifest(_merged(user), navigation=False)
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else '结构不对（%s）' % type(exc).__name__
        raise CodedError(CONFIG_INVALID.format(reason=reason[:600]), 'body_config_invalid') from None
    # The install script refuses to start with these (runtime/deploy/device_install.sh, ros2_config_check): a
    # simulated body skips the safety limits and is not device evidence. Said here, before anything is sent.
    flags = [key for key in ('simulated', 'use_sim_time') if user.get(key) is True]
    if flags:
        raise CodedError(CONFIG_INVALID.format(reason='%s 不能设为 true：真实本体接的是机器人自己的 ROS 2，'
                                               '设备上的安装脚本会拒绝启动' % ' 和 '.join(flags)), 'body_config_invalid')
    return user, declares(manifest)


def declares(manifest):
    """What a body with this manifest offers, for the page: its name, capability ids and places."""
    capabilities = [c for c in manifest.get('capabilities', []) if isinstance(c, dict)]
    place = next((c for c in capabilities if c.get('id') == 'base.navigate_to_place'), None)
    places = (place or {}).get('params', {}).get('places')
    return {'name': manifest.get('name'), 'family': manifest.get('family'),
            'capabilities': [str(c.get('id')) for c in capabilities],
            'places': [name for name in places if isinstance(name, str)] if isinstance(places, list) else [],
            'explore': any(c.get('id') == 'base.explore' for c in capabilities)}


def config_text(config):
    return '' if config is None else json.dumps(config, ensure_ascii=False, indent=2) + '\n'


def of(robot):
    """The body of a robot as it is saved, with the defaults for what was never set."""
    saved = robot.get('body') if isinstance(robot.get('body'), dict) else {}
    kind = saved.get('kind') if saved.get('kind') in KINDS else 'sim'
    settings = saved.get('settings') if isinstance(saved.get('settings'), dict) else {}
    config = saved.get('config') if isinstance(saved.get('config'), dict) else None
    return {'kind': kind, 'setup': saved.get('setup') or DEFAULT_SETUP,
            'settings': {name: str(settings[name]) for name in dev.ROS_KEYS if settings.get(name) not in (None, '')},
            'config': config, 'updated_at': saved.get('updated_at')}


def body_env(body):
    """The text of the body file the install job reads on the robot (``runtime_install_device.BODY_FILE``)."""
    lines = ['NX_ROS_SETUP=' + body['setup']]
    lines += ['%s=%s' % (name, body['settings'][name]) for name in dev.ROS_KEYS if name in body['settings']]
    lines.append('NX_BODY_CONFIG=' + ('yes' if body['config'] is not None else 'no'))
    return '\n'.join(lines) + '\n'


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def install_digest(setup, settings):
    """The mark of the ROS settings an install was made with; the same for a saved body and for what an install
    recorded on the robot (``read_probe``), so the two can be compared."""
    return _digest([setup, {name: str(settings[name]) for name in dev.ROS_KEYS if name in settings}])


def digests(body):
    """What an install was made with (``install``: the ROS settings, which only a reinstall changes) and what a
    start puts on the robot (``config``)."""
    return {'install': install_digest(body['setup'], body['settings']), 'config': _digest(body['config'])}


def state_of(robot):
    """How the saved body relates to what is on the robot, for the page: ``sim``, ``unset`` (a real body never
    installed from this console), ``unknown`` (an install this console found and linked: what it runs with was
    not put there from here), ``current``, ``config_changed`` (a restart puts it there) or
    ``reinstall`` (the ROS settings or the kind changed: only a new install changes those)."""
    body, applied = of(robot), robot.get('body_applied') if isinstance(robot.get('body_applied'), dict) else None
    if applied is None:
        return 'sim' if body['kind'] == 'sim' else 'unset'
    if body['kind'] != applied.get('kind'):
        return 'reinstall'
    if body['kind'] == 'sim':
        return 'sim'
    now_ = digests(body)
    if applied.get('install') not in (None, now_['install']):
        return 'reinstall'
    if applied.get('install') is None or applied.get('config') is None:
        # An install this console found and linked: the configuration it runs with was not sent from here, or
        # the ROS settings it was made with could not be read back. Nothing is claimed about it.
        return 'unknown'
    return 'current' if now_['config'] == applied.get('config') else 'config_changed'


def view(robot):
    body = of(robot)
    return {'kind': body['kind'], 'setup': body['setup'], 'settings': body['settings'],
            'config_text': config_text(body['config']), 'updated_at': body['updated_at'], 'state': state_of(robot)}


def _hex_text(value):
    try:
        return bytes.fromhex(value).decode('utf-8')
    except (ValueError, UnicodeDecodeError):
        return None


def _found_settings(keys, prefix):
    """The ROS settings among a probe's keys that are usable as they are; what is not is left out."""
    found = {}
    for name in dev.ROS_KEYS:
        value = keys.get(prefix + name.lower(), '').strip()
        if not value:
            continue
        try:
            found.update(clean_settings({name: value}))
        except CodedError:
            pass
    return found


def read_probe(keys):
    """What ``rosprobe`` answered, as the page shows it."""
    setups = [path for path in keys.get('setups', '').split() if _PATH.fullmatch(path)][:8]
    count = keys.get('run_count', '')
    result = {'setups': setups,
              'running': {'count': int(count) if count.isdigit() and len(count) < 7 else 0,
                          'settings': _found_settings(keys, 'run_')},
              'installed': None}
    if keys.get('root') == 'installed':
        setup = keys.get('rec_ros_setup', '')
        size = keys.get('config_bytes', '')
        text = _hex_text(keys['config_hex']) if keys.get('config_hex') else None
        if text is not None:
            try:
                clean_config(text)
            except CodedError:
                text = None                        # shown as "could not be read": never put into the form unchecked
        result['installed'] = {'adapter': keys.get('adapter', '')[:40] or 'sim_quadruped',
                               'setup': setup if _PATH.fullmatch(setup) else '',
                               'settings': _found_settings(keys, 'rec_'),
                               'config_bytes': int(size) if size.isdigit() and len(size) < 10 else None,
                               'config_text': text}
    return result


class RobotBody:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store

    def _robot(self, p):
        robot, connection = self.platform.engineering._ssh_connection(p)
        if robot.get('sim') or robot.get('managed_resource_id'):
            raise ValueError(ONLY_SSH)
        return robot, connection

    def save(self, p):
        """``robots/body/save``: ``kind``, ``setup``, ``settings`` and ``config_text`` of this robot's body.
        Nothing reaches the robot: the next install, or "应用到机器人", uses what is saved here."""
        kind = p.get('kind')
        if kind not in KINDS:
            raise CodedError(BAD_KIND, 'body_settings_invalid')
        setup, settings = clean_setup(p.get('setup')), clean_settings(p.get('settings'))
        config, offered = clean_config(p.get('config_text'))
        with self.platform.engineering.lock:
            robot, _ = self._robot(p)
            installs = getattr(self.platform, 'runtime_install', None)
            if installs is not None and installs.running(robot['id']):
                raise CodedError('这台机器人正在安装管理层；结束后再改本体设置', 'install_in_progress')
            with self.platform.runtime_link._robot_lock(robot['id']), self.store.atomic():
                robot = self.store.get('robots', robot['id'])
                robot['body'] = {'kind': kind, 'setup': setup, 'settings': settings, 'config': config, 'updated_at': now()}
                self.store.put('robots', robot)
        self.platform.audit('robot.body_saved', {'robot_id': robot['id'], 'kind': kind, 'settings': sorted(settings),
                                                 'config': config is not None,
                                                 'places': (offered or {}).get('places', [])}, robot['project_id'])
        return {'robot_id': robot['id'], 'body': view(robot), 'declares': offered}

    def check(self, p):
        """``robots/body/check``: what a device configuration would declare, or why it is not usable. Saves nothing."""
        self._robot(p)
        config, offered = clean_config(p.get('config_text'))
        return {'robot_id': p.get('robot_id'), 'empty': config is None, 'declares': offered}

    def detect(self, p):
        """``robots/body/detect``: read from the robot (read only, one fixed script) which ROS 2 setup files it
        has, the ROS settings its running programs use, and the body of an install that is already there."""
        robot, connection = self._robot(p)
        connection_id, stop = connection['id'], threading.Event()

        def login():
            return device_access.SSHDevice(*self.platform.ssh_edit.saved_login(connection_id))
        try:
            package_id = runtime_package.bundled().id
        except runtime_package.PackageError:
            package_id = 'runtime-0-000000000000'           # the script only uses it to name a directory it does not touch
        session = dev.DeviceSession(login, stop, waits=(2, 5))
        try:
            word, keys, _ = session.run('rosprobe', package_id, seconds=30, deadline=time.monotonic() + DETECT_SECONDS)
        except dev.Unreachable as exc:
            raise CodedError(DETECT_FAILED.format(reason=str(exc)[:300]), 'body_detect_failed') from None
        except ValueError as exc:
            raise CodedError(DETECT_FAILED.format(reason=str(exc)[:300]), 'body_detect_failed') from None
        finally:
            session.close()
        if word != 'rosprobed':
            raise CodedError(DETECT_FAILED.format(reason='设备没有完成读取（%s）' % (word or '没有答复')), 'body_detect_failed')
        try:
            self.platform.heartbeat.heard(connection)
        except (ValueError, AttributeError):
            pass
        return {'robot_id': robot['id'], **read_probe(keys)}
