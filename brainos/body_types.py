"""Issue #32 (R1-11): the body types a device can be registered as, kept by the platform.

Where the list lives: with the desktop workspace, one list per signed-in account, in the same store as
the devices that use it. "Is this type still in use" is therefore answered exactly, also offline.

Two types are built in and always there; others are added and removed by a person. A type has

* ``name``: what people see;
* ``state_machine_template``: the identifier of the state machine template for such a body. The
  templates themselves are requirement R4 and do not exist yet, so only the identifier is kept;
* ``required_capabilities``: the motion-control interfaces such a body should have, as ids of the
  standard capability registry (the driver check of issue #28 reads them).

``robot['family']`` is the id of the type a person chose. It is not what the device reports: the
``family`` of a manifest is one of the seven values of the standard (``neuxmind_spec.manifest.FAMILIES``,
unchanged) and stays readable in ``robot['manifest']``. Devices registered before the list existed keep
the value they have, also when it is one of the five values that are not in the list. Such a value can
be left by hand (:meth:`BodyTypes.assign`) and cannot be chosen again.

The type of a saved robot changes in one place only, :meth:`BodyTypes.assign`. The store enforces it
(``Store.owned``): whoever else saves a robot record, for instance a copy read before a slow answer of
the device, saves it with the type the store holds at that moment.
"""
import re
import unicodedata

from .errors import CodedError
from .service import now, uid
from .spec_bridge import capabilities as spec_caps

KIND = 'body_types'
TEMPLATE_ID = re.compile(r'[a-z0-9][a-z0-9_.\-]{0,99}')    # an identifier, never a path
NAME_MAX = 40
ADDED_MAX = 100             # types a workspace can add; the whole list travels with every state read
NAMES_IN_MESSAGE = 5        # devices named in the sentence of a refusal; the full list travels beside it
SHOWN_MAX = 100             # characters of a refused value repeated in a message


def built_in():
    """The two types every workspace has. Their ids are values a device can report in its manifest."""
    return [
        {'id': 'quadruped', 'name': '四足机器人', 'state_machine_template': 'quadruped.default',
         'required_capabilities': ['safety.stop', 'base.velocity', 'legged.posture', 'base.odometry'], 'builtin': True},
        {'id': 'manipulator', 'name': '机械臂', 'state_machine_template': 'manipulator.default',
         'required_capabilities': ['safety.stop', 'joint.state', 'arm.joint_position'], 'builtin': True},
    ]


def interfaces():
    """The standard capabilities a type can name as an interface its bodies should have."""
    return [{'id': c.id, 'title': c.title, 'kind': c.kind, 'safety': c.safety} for c in spec_caps.CAPABILITIES.values()]


class TypeInUse(CodedError):
    """A type cannot be removed while devices use it; ``devices`` says which (see ``errors.error_payload``)."""

    def __init__(self, message, devices):
        super().__init__(message, 'body_type_in_use')
        self.devices = devices


def _name(value):
    """A name as people would compare it: one form per character, single spaces, nothing invisible."""
    name = ' '.join(unicodedata.normalize('NFKC', value).split()) if isinstance(value, str) else ''
    if not 1 <= len(name) <= NAME_MAX or not name.isprintable():
        raise CodedError(f'类型名称需要 1–{NAME_MAX} 个字符，不能有换行或看不见的字符', 'body_type_name_invalid', field='name')
    return name


def _template(value):
    if not isinstance(value, str) or not TEMPLATE_ID.fullmatch(value):
        raise CodedError('状态机模板标识需要 1–100 个字符：小写字母、数字、点、下划线或连字符，并以字母或数字开头',
                         'body_type_template_invalid', field='state_machine_template')
    return value


def _capabilities(value):
    if not isinstance(value, list) or not value:
        raise CodedError('请至少选择一个应有的运控接口', 'body_type_interfaces_required', field='required_capabilities')
    chosen = []
    for cap_id in value:
        if not isinstance(cap_id, str) or cap_id not in spec_caps.CAPABILITIES:
            raise CodedError(f'“{str(cap_id)[:SHOWN_MAX]}”不是标准能力编号', 'body_type_interface_unknown',
                             field='required_capabilities')
        if cap_id not in chosen:
            chosen.append(cap_id)
    return chosen


def _in_use_message(body_type, devices):
    named = '、'.join(f"{d['name']}（{d['project_name']}）" for d in devices[:NAMES_IN_MESSAGE])
    more = f' 等 {len(devices)} 台' if len(devices) > NAMES_IN_MESSAGE else ''
    return (f"还有 {len(devices)} 台设备在用“{body_type['name']}”，不能删除：{named}{more}。"
            '先把这些设备改成别的类型，或删除这些设备，再删除这个类型。')


class BodyTypes:
    def __init__(self, platform):
        self.platform = platform
        self.store = platform.store
        self.store.owned['robots'] = ('family',)        # only assign() changes the type of a saved robot

    # ------------------------------------------------------------------ reading
    def list(self):
        """Built-in types first, then the added ones, oldest first."""
        return built_in() + sorted(self.store.list(KIND), key=lambda t: (t.get('created_at', ''), t['id']))

    def get(self, type_id):
        return next((t for t in self.list() if t['id'] == type_id), None)

    def require(self, type_id):
        """``type_id`` when it is in the list; otherwise a refusal a person can act on."""
        if not isinstance(type_id, str) or self.get(type_id) is None:
            raise CodedError('请从本体类型清单里选择一个类型', 'body_type_required', field='family')
        return type_id

    def devices(self, type_id):
        """The devices of this workspace that use a type, across its projects, oldest first."""
        projects = {p['id']: p.get('name', '') for p in self.store.list('projects')}
        using = sorted((r for r in self.store.list('robots') if r.get('family') == type_id),
                       key=lambda r: (r.get('created_at', ''), r['id']))
        return [{'id': r['id'], 'name': r.get('name', ''), 'project_id': r.get('project_id'),
                 'project_name': projects.get(r.get('project_id'), '')} for r in using]

    def snapshot(self):
        return {'body_types': self.list(), 'body_type_interfaces': interfaces()}

    # ------------------------------------------------------------------ changing the list
    def add(self, p):
        name = _name(p.get('name'))
        template = _template(p.get('state_machine_template'))
        capabilities = _capabilities(p.get('required_capabilities'))
        with self.store.atomic():
            listed = self.list()
            if any(t['name'].casefold() == name.casefold() for t in listed):
                raise CodedError(f'已经有名为“{name}”的本体类型', 'body_type_name_taken', field='name')
            if len(listed) - len(built_in()) >= ADDED_MAX:
                raise CodedError(f'清单里最多添加 {ADDED_MAX} 个本体类型，请先删除不用的类型', 'body_type_limit')
            body_type = self.store.put(KIND, {'id': uid('bodytype'), 'name': name, 'state_machine_template': template,
                                              'required_capabilities': capabilities, 'builtin': False, 'created_at': now()})
        self.platform.audit('body_type.added', {'body_type_id': body_type['id'], 'name': name})
        return body_type

    def remove(self, p):
        type_id = p.get('body_type_id')
        # One lock from the check to the delete: a device cannot be registered with the type in between
        # (``Platform.add_robot`` and :meth:`assign` save under the same lock).
        with self.store.atomic():
            body_type = self.get(type_id) if isinstance(type_id, str) else None
            if body_type is None:
                raise CodedError('清单里没有这个本体类型', 'body_type_unknown')
            if body_type['builtin']:
                raise CodedError(f"“{body_type['name']}”是内置类型，不能删除", 'body_type_builtin')
            devices = self.devices(type_id)
            if devices:
                raise TypeInUse(_in_use_message(body_type, devices), devices)
            self.store.delete(KIND, type_id)
        self.platform.audit('body_type.removed', {'body_type_id': type_id, 'name': body_type['name']})
        return {'removed': type_id}

    # ------------------------------------------------------------------ the type of a registered device
    def assign(self, p):
        """Change the type of a registered device to one from the list. Nothing else of the record changes."""
        if not isinstance(p.get('robot_id'), str) or not isinstance(p.get('project_id'), str):
            raise ValueError('找不到这台机器人')
        with self.store.atomic():
            robot = self.platform.same_project('robots', p['robot_id'], p['project_id'])
            if robot.get('sim'):
                raise CodedError('仿真机器人的类型由它的仿真种类决定，不能修改', 'body_type_sim_fixed')
            previous, family = robot.get('family'), self.require(p.get('family'))
            changed = {**robot, 'family': family}
            if family != previous:
                self.store.put('robots', changed, owner=True)
        if family != previous:
            self.platform.audit('robot.body_type_changed', {'robot_id': robot['id'], 'from': previous, 'to': family},
                                robot['project_id'])
        return self.platform.redact(changed)
