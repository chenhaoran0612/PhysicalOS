"""Standard capability table for a connected robot (INTERFACES §4.5).

The robot's own ``GET /v2/manifest`` is preferred: it carries capability
parameters (speed limits, joint limits, ...) that the safety supervisor uses.
Older devices only offer the v1 view (a list of capability names); then the
table shows names from the spec registry without parameters.

Names, kinds and safety levels always come from the shared spec registry
(``neuxmind_spec.capabilities``), so desktop, runtime, SDK and cloud agree.
"""
import json
import re

from . import connector
from .spec_bridge import capabilities as spec_caps, manifest as spec_manifest

KIND_NAMES = {
    'state': '状态读取',
    'stream': '传感数据',
    'command': '即时指令',
    'action': '持续动作',
}

SAFETY_NAMES = {
    'none': '不产生运动',
    'motion': '会产生运动 · 需现场授权',
    'system': '修改设备软件',
}

# One plain sentence per standard capability: what it lets the robot do, for users
# who do not know the capability IDs. Unknown IDs fall back to the spec description.
USER_DOES = {
    'safety.stop': '随时让机器人立即停下并保持安全状态（停止按钮靠它）。',
    'system.health': '告诉你机器人是否在线、有没有报错、当前处于什么模式。',
    'sensor.battery': '显示剩余电量和是否在充电。',
    'sensor.imu': '感知机器人自身的倾斜、转动和加速度。',
    'sensor.camera.rgb': '用彩色相机看到周围环境。',
    'sensor.camera.depth': '用深度相机测量周围物体的远近。',
    'sensor.lidar': '用激光雷达扫描周围，得到环境轮廓。',
    'base.odometry': '估计机器人自己走了多远、现在朝向哪里。',
    'localization.pose': '知道自己在地图上的位置。',
    'gripper.state': '查看夹爪张开多少、是否夹住了东西。',
    'arm.ee_state': '查看机械臂末端（夹爪）当前在空间中的位置。',
    'joint.state': '读取每个关节的角度和速度。',
    'base.velocity': '按指定速度前进、后退、横移或转向。',
    'base.navigate_to': '自己走到你指定的目标位置。',
    'legged.posture': '站起来、趴下或坐下。',
    'arm.joint_position': '把机械臂的各个关节转到指定角度。',
    'arm.ee_pose': '把机械臂末端移动到空间中的指定位置和朝向。',
    'gripper.position': '张开或合上夹爪，抓取或松开物体。',
    'policy.action_chunk': '执行 AI 策略模型（如 VLA）给出的一段连续动作。',
    'model.install': '在机器人上安装 AI 模型（只安装，不会让机器人动）。',
    'audio.speak': '把文字念出来（语音播报）。',
}

V1_ONLY_NOTE = '设备只提供 v1 能力清单（能力名称列表），没有参数与限值信息'


def fetch_manifest_v2(robot):
    """Read and validate ``GET /v2/manifest``. Raises ValueError with a readable reason."""
    if not robot.get('endpoint'):
        raise ValueError('请先配置设备端适配服务地址')
    try:
        raw = connector.call(robot['endpoint'], '/v2/manifest', robot.get('token', ''))
    except connector.DeviceError as exc:
        if exc.status == 404:
            raise ValueError('设备运行时没有提供 /v2/manifest（旧版接入服务）') from None
        raise ValueError(f'读取 /v2/manifest 失败：{exc}') from None
    try:
        manifest = spec_manifest.validate_manifest_v2(raw)
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f'设备的 v2 能力清单不符合标准：{exc}') from None
    v1 = robot.get('manifest') or {}
    if v1.get('instance_id') and v1['instance_id'] != manifest['instance_id']:
        raise ValueError('v2 能力清单的设备身份与已连接的设备不一致，请重新检查连接')
    return manifest


def refresh(platform, robot):
    """Re-read the v2 manifest (read-only GET) and keep it on the robot record.

    Failure is not an error for the caller: the table falls back to the v1 list
    and records why.
    """
    try:
        robot['manifest_v2'] = fetch_manifest_v2(robot)
        robot.pop('manifest_v2_error', None)
    except ValueError as exc:
        robot.pop('manifest_v2', None)
        robot['manifest_v2_error'] = str(exc)
    platform.store.put('robots', robot)
    return robot


def _format_value(value, unit=''):
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, (list, dict)):
        text = json.dumps(value, ensure_ascii=False)
        return text if len(text) <= 120 else text[:117] + '…'
    text = str(value)
    return f'{text} {unit}'.strip()


def _param_rows(cap, declared):
    rows = []
    known = set()
    for f in cap.params:
        known.add(f.name)
        has = f.name in declared
        rows.append({
            'name': f.name,
            'description': f.description,
            'unit': f.unit,
            'declared': has,
            'value': declared.get(f.name) if has else None,
            'text': _format_value(declared[f.name], f.unit) if has else '未声明',
        })
    for name in sorted(set(declared) - known):
        rows.append({'name': name, 'description': '', 'unit': '', 'declared': True,
                     'value': declared[name], 'text': _format_value(declared[name])})
    return rows


def capability_row(cap_id, declared_params=None, source='v2'):
    """One table row: Chinese name, kind, safety level and parameters."""
    declared_params = declared_params or {}
    if cap_id.startswith('x.'):
        return {
            'id': cap_id, 'standard': False, 'title': '厂商扩展能力', 'kind': None,
            'does': '厂商自定义的功能，平台不清楚它的具体效果。',
            'kind_name': '厂商定义', 'safety': 'motion', 'safety_name': SAFETY_NAMES['motion'],
            'mandatory': False, 'description': '能力标准之外的厂商扩展；物理效果未知，一律按运动类处理',
            'params': [{'name': k, 'description': '', 'unit': '', 'declared': True, 'value': v,
                        'text': _format_value(v)} for k, v in sorted(declared_params.items())],
        }
    cap = spec_caps.CAPABILITIES.get(cap_id)
    if cap is None:
        return {
            'id': cap_id, 'standard': False, 'title': '未在能力标准中', 'kind': None, 'kind_name': '未知',
            'does': '平台不认识这项能力，无法说明它的作用。',
            'safety': None, 'safety_name': '未知', 'mandatory': False,
            'description': '设备声明的名称不在 NeuxMind 能力标准中，平台无法判断其类型和安全级', 'params': [],
        }
    return {
        'id': cap.id, 'standard': True, 'title': cap.title, 'kind': cap.kind,
        'does': USER_DOES.get(cap.id, cap.description),
        'kind_name': KIND_NAMES.get(cap.kind, cap.kind), 'safety': cap.safety,
        'safety_name': SAFETY_NAMES.get(cap.safety, cap.safety), 'mandatory': cap.mandatory,
        'description': cap.description,
        'params': _param_rows(cap, declared_params) if source == 'v2' else [],
    }


def _v1_rows(names):
    rows = []
    for name in names:
        try:
            cap_id = spec_caps.normalize(name)
        except ValueError:
            cap_id = name
        row = capability_row(cap_id, source='v1')
        if cap_id != name:
            row['declared_as'] = name
        rows.append(row)
    return rows


def table(robot):
    """Capability table from the stored manifests (no network)."""
    v2 = robot.get('manifest_v2')
    v1 = robot.get('manifest') or {}
    if v2:
        rows = [capability_row(c['id'], c.get('params') or {}) for c in v2['capabilities']]
        return {'source': 'v2', 'source_name': '标准能力清单（/v2/manifest）', 'rows': rows,
                'simulated': v2.get('simulated') is True, 'vendor': v2.get('vendor'), 'model': v2.get('model'),
                'spec_version': v2.get('spec_version'), 'local_spec_version': spec_caps.SPEC_VERSION,
                'model_runtimes': v2.get('model_runtimes', []), 'safety': v2.get('safety', {}),
                'note': None}
    if v1.get('capabilities'):
        return {'source': 'v1', 'source_name': '兼容能力清单（/v1/manifest）', 'rows': _v1_rows(v1['capabilities']),
                'simulated': v1.get('simulated') is True, 'vendor': v1.get('vendor'), 'model': v1.get('model'),
                'spec_version': None, 'local_spec_version': spec_caps.SPEC_VERSION,
                'model_runtimes': v1.get('model_runtimes', []), 'safety': {},
                'note': V1_ONLY_NOTE + ('；' + robot['manifest_v2_error'] if robot.get('manifest_v2_error') else '')}
    return {'source': None, 'source_name': '尚未读取', 'rows': [], 'simulated': False, 'vendor': None, 'model': None,
            'spec_version': None, 'local_spec_version': spec_caps.SPEC_VERSION, 'model_runtimes': [], 'safety': {},
            'note': '尚未连接设备，能力表在“连接检查”通过后显示'}


def _finite_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and v not in (float('inf'), float('-inf'))


def as_declared(ptype, v):
    """ux05-fix2 (Q7b-04): a plain number in the JSON number type its parameter declares.

    ``float`` -> always a float (``1`` -> ``1.0``); ``int`` -> an int when the value is whole (``2.0`` -> ``2``).
    Anything else (other types, bools, strings, non-whole numbers for ``int``) is returned unchanged:
    the device still decides.
    """
    if not _plain_number(v):
        return v
    if ptype == 'float' and isinstance(v, int):
        try:
            return float(v)
        except OverflowError:
            return v
    if ptype == 'int' and isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def parameter_view(decl, typed=False):
    """A skill.json parameter declaration, reduced to what a run form needs (ux02).

    Read-only and defensive: anything malformed is dropped rather than shown.
    ``required`` follows the runtime's rule (absent → optional).

    ``typed`` (ux05-fix2, Q7b-04): numbers (default, range, option values) take the declared ``float`` /
    ``int`` type -- used when the desktop *stores* a hand-entered declaration. A device's own declaration
    (``/v2/skills``) is shown as the device wrote it.
    """
    if not isinstance(decl, dict) or not isinstance(decl.get('name'), str):
        return None
    out = {'name': decl['name'][:100], 'type': str(decl.get('type', 'object'))[:20],
           'required': decl.get('required', False) is True and 'default' not in decl,
           'description': str(decl.get('description', ''))[:300]}
    ptype = out['type'] if typed else None
    if 'default' in decl:
        try:
            json.dumps(decl['default'], allow_nan=False)
            out['default'] = as_declared(ptype, decl['default'])
        except (TypeError, ValueError):
            pass
    for key in ('min', 'max', 'minimum', 'maximum', 'step'):
        if _finite_number(decl.get(key)):
            out[{'minimum': 'min', 'maximum': 'max'}.get(key, key)] = as_declared(ptype, decl[key])
    if isinstance(decl.get('unit'), str):
        out['unit'] = decl['unit'][:20]
    if isinstance(decl.get('title'), str) and decl['title'].strip():
        out['title'] = decl['title'].strip()[:60]          # ux03: human label for run / step forms
    if isinstance(decl.get('length'), int) and not isinstance(decl.get('length'), bool):
        out['length'] = decl['length']
    options = choice_options(decl)
    if options is not None and ptype in ('float', 'int'):
        # Q7b-04: option values carry the declared number type (a hand-entered ``1`` of a float is ``1.0``).
        typed, seen = [], set()
        for o in options:
            value = as_declared(ptype, o['value'])
            key = json.dumps(value)
            if key not in seen:
                seen.add(key)
                typed.append({**o, 'value': value})
        options = typed
    if options is not None:
        # ux05 (Q6-07): ``choices`` are the raw values (typed: the runtime compares strictly), ``options`` pair
        # each value with what the user sees. Never ``str()`` of an object: that was shown and sent verbatim.
        out['choices'] = [o['value'] for o in options]
        out['options'] = options
    return out


def _choice_value(v):
    """A choice value the browser can send back unchanged: a JSON scalar (finite), else None."""
    if v is None or isinstance(v, bool):
        return v, True
    if isinstance(v, (int, float)):
        return (v, True) if _finite_number(v) else (None, False)
    if isinstance(v, str):
        return (v, True) if len(v) <= 200 else (None, False)
    return None, False


def choice_options(decl):
    """``[{value, label}]`` from ``choices`` (values) or ``options`` (values or ``{value, label}`` objects).

    Values keep their JSON type (``1`` stays a number, ``"1"`` a string). Entries that cannot be sent back
    unchanged (objects, lists, non-finite numbers) are dropped. ``None`` when nothing usable is declared.
    """
    raw = decl.get('choices') if isinstance(decl.get('choices'), list) else decl.get('options')
    if not isinstance(raw, list):
        return None
    out, seen = [], set()
    for item in raw[:50]:
        if isinstance(item, dict):
            if 'value' not in item:
                continue
            value, ok = _choice_value(item['value'])
            label = item.get('label') if isinstance(item.get('label'), str) and item['label'].strip() else None
        else:
            value, ok = _choice_value(item)
            label = None
        if not ok:
            continue
        key = json.dumps(value)
        if key in seen:
            continue
        seen.add(key)
        text = label.strip()[:100] if label else ('是' if value is True else '否' if value is False
                                                   else '（空）' if value is None else str(value)[:100])
        out.append({'value': value, 'label': text})
    return out


_CAP_ID = re.compile(r'(?<![\w.])([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)(?![\w.])')


def plain_text(text):
    """Q5-12: a skill description with standard capability IDs replaced by their Chinese names.

    "任何提供 base.navigate_to 的本体" -> "任何提供「导航到目标点」的本体". Unknown dotted
    words (file names, versions) are left alone; the original text stays available.
    """
    if not isinstance(text, str):
        return ''

    def name(m):
        cap = spec_caps.CAPABILITIES.get(m.group(1))
        return f'「{cap.title}」' if cap else m.group(0)
    out = _CAP_ID.sub(name, text)
    return re.sub(r' ?(「[^」]*」) ?', r'\1', out) if out != text else text


def _plain_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and _finite_number(v)


def typed_parameters(decls, params, device_decls=False):
    """ux05-fix (Q7-04) / ux05-fix2 (Q7b-04): parameter values with the JSON number type the skill declared.

    The browser cannot tell ``1.0`` from ``1`` (both are the JS number 1), while a device may compare choices
    strictly by type (INTERFACES §9). So a number is serialized by its declaration:

    * ``float`` / ``int`` parameters: as the declared type (``1`` -> ``1.0`` for a float, ``2.0`` -> ``2`` for an
      int). Exception: with ``device_decls`` (the declaration was read from the device itself, ``/v2/skills``)
      a value equal to one of its declared choices is sent exactly as the device wrote that choice -- the
      device's own literal is what an older, strict runtime compares against (Q7-07: ``float`` + ``choices:[1,2]``).
    * other types with numeric choices (``enum``): the declared choice it equals, exactly as declared.
    * strings, booleans, lists and values of undeclared parameters: unchanged (the device still decides).
    """
    if not isinstance(params, dict) or not isinstance(decls, list):
        return params
    by_name = {d['name']: d for d in decls if isinstance(d, dict) and isinstance(d.get('name'), str)}
    out = dict(params)
    for name, value in params.items():
        decl = by_name.get(name)
        if decl is None or not _plain_number(value):
            continue
        allowed = decl.get('choices')
        if not isinstance(allowed, list) and isinstance(decl.get('options'), list):
            allowed = [o.get('value') if isinstance(o, dict) else o for o in decl['options']]
        same = [c for c in allowed if _plain_number(c) and c == value] if isinstance(allowed, list) else []
        ptype = decl.get('type')
        if ptype in ('float', 'int'):
            if device_decls and same:
                exact = [c for c in same if type(c) is type(as_declared(ptype, c))]
                out[name] = (exact or same)[0]
            else:
                out[name] = as_declared(ptype, value)
            continue
        if same and not [c for c in same if type(c) is type(value)]:
            out[name] = same[0]
    return out


def typed_application(platform, app):
    """ux05-fix2 (Q7b-04): the application as sent to a device (validate / evaluate / deploy).

    Every step's parameters are serialized by the declared types of the skill it names (the project's own
    registered declaration), so an application saved before the fix -- or by another client -- still sends
    ``1.0`` for a float parameter. The stored application and its digest are unchanged (the values are
    numerically equal; the device echoes ``digest`` and does not recompute it).
    """
    steps = app.get('steps') if isinstance(app, dict) else None
    if not isinstance(steps, list):
        return app
    out_steps = []
    for step in steps:
        if isinstance(step, dict) and isinstance(step.get('parameters'), dict):
            try:
                skill = platform.store.get('skills', step.get('skill_id'))
            except (KeyError, ValueError):
                skill = None
            decls = skill.get('parameters') if isinstance(skill, dict) else None
            if decls:
                step = {**step, 'parameters': typed_parameters(decls, step['parameters'])}
        out_steps.append(step)
    return {**app, 'steps': out_steps}


def parameter_views(decls, typed=False):
    return [v for v in (parameter_view(d, typed) for d in (decls if isinstance(decls, list) else [])) if v]


def installed_skills(robot):
    """``GET /v2/skills`` → readable list. Never raises."""
    if not robot.get('endpoint'):
        return {'available': False, 'items': [], 'error': '请先配置设备端适配服务地址'}
    try:
        result = connector.call(robot['endpoint'], '/v2/skills', robot.get('token', ''))
    except connector.DeviceError as exc:
        reason = '设备运行时没有提供 /v2/skills（旧版接入服务）' if exc.status == 404 else f'读取已安装技能失败：{exc}'
        return {'available': False, 'items': [], 'error': reason}
    skills = result.get('skills')
    if not isinstance(skills, list):
        return {'available': False, 'items': [], 'error': '设备返回的技能列表格式无效'}
    items = []
    for s in skills:
        if not isinstance(s, dict):
            continue
        compat = s.get('compatibility') if isinstance(s.get('compatibility'), dict) else {}
        requires = [r.get('id') for r in s.get('requires', []) if isinstance(r, dict)]
        items.append({
            'id': str(s.get('id', '')), 'version': str(s.get('version', '')), 'title': str(s.get('title') or s.get('id', '')),
            'description': str(s.get('description', ''))[:500],
            'summary': plain_text(str(s.get('description', ''))[:500]), 'digest': str(s.get('digest', '')),
            'requires': [{'id': r, 'title': capability_row(r)['title']} for r in requires if isinstance(r, str)],
            'compatible': compat.get('compatible') is not False,
            'installed_at': s.get('installed_at'),
            'parameters': parameter_views(s.get('parameters')),
        })
    return {'available': True, 'items': items, 'error': None}


def detail(platform, p):
    """Robot detail: capability table plus installed skill packages.

    Only read-only GETs reach the device; nothing moves.
    """
    robot = platform.store.get('robots', p.get('robot_id'))
    if p.get('project_id') and robot.get('project_id') != p['project_id']:
        raise ValueError('该机器人不属于当前项目')
    if robot.get('endpoint') and robot.get('status') == 'connected':
        robot = refresh(platform, robot)
        skills = installed_skills(robot) if robot.get('manifest_v2') else {
            'available': False, 'items': [], 'error': '技能包需要设备提供 v2 接口（/v2/skills）'}
    else:
        skills = {'available': False, 'items': [], 'error': '连接检查通过后才能查看和安装技能包'}
    return {'robot': platform.redact(robot), 'capabilities': table(robot), 'skills': skills}


def cloud_manifest(robot):
    """A ``robot.adapter.v2`` manifest to ask the cloud which models fit.

    Uses the device's own v2 manifest; for v1-only devices a v2 view is built
    from the standard capability names (no parameters). Raises ValueError when
    that is not a valid v2 manifest (e.g. mandatory capabilities missing).
    """
    if robot.get('manifest_v2'):
        return robot['manifest_v2']
    v1 = robot.get('manifest')
    if not v1:
        raise ValueError('请先连接机器人，读取它的能力清单')
    caps = []
    for name in v1.get('capabilities', []):
        try:
            cap_id = spec_caps.normalize(name)
        except ValueError:
            continue
        if not any(c['id'] == cap_id for c in caps):
            caps.append({'id': cap_id})
    built = {'schema': 'robot.adapter.v2', 'instance_id': v1['instance_id'], 'name': v1['name'],
             'adapter_version': v1['adapter_version'], 'family': v1['family'],
             'vendor': v1.get('vendor', 'unknown'), 'model': v1.get('model', 'unknown'),
             'simulated': v1.get('simulated') is True, 'capabilities': caps,
             'model_runtimes': v1.get('model_runtimes', [])}
    try:
        return spec_manifest.validate_manifest_v2(built)
    except ValueError as exc:
        raise ValueError(f'这台机器人只提供 v1 能力清单，无法换算为标准清单（{exc}），因此不能按能力筛选模型') from None
