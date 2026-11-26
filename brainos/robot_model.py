"""Brand and model of a robot; applications and skills belong to one brand and model (issue #48).

The owner's rule: a robot application is bound to the brand and model of robot it was made for, and
robots of different brands and models are kept apart. What was decided with the owner (2026-11-25):

* **An application belongs to a brand and model, not to one robot.** Every robot of that brand and
  model can check, test and release it (the request names the robot; without a name it is the robot
  the application was made with). A robot of another brand or model cannot. Test evidence and
  releases stay per robot, as before.
* **Where a robot's brand and model come from.** What the device reports (``vendor`` / ``model`` of
  its manifest) is used. When it reports none, a person types them when adding the robot; the two
  entries that add a robot for a person (the routes ``robots`` and ``engineering/ssh/connect``)
  refuse without them. A later connection check that finds reported values replaces typed ones.
* **Skills are kept apart the same way**: a registered skill carries a brand and model, and an
  application only takes skills of its own.

How it is kept: one field, :data:`FIELD` (``product``), ``{'vendor', 'model'}`` on applications and
skills, with ``source`` (``device`` / ``manual``) and ``updated_at`` on robots. Two products are the
same when both texts are equal without regard to case (:func:`key`).

Records without one (from before, or made by code that calls the platform directly):

* a robot without a product is in a class of its own: an application made for it has no product
  either and can only be used with that robot. When the robot gets a product, those applications
  take it (:meth:`RobotModels.adopt`);
* a skill without a product takes the product of the first application that uses it.
"""
import hashlib
import json
from datetime import datetime, timezone

from . import connector, robot_scope
from .errors import CodedError

FIELD = 'product'
MAX = 100
DEVICE, MANUAL = 'device', 'manual'
# What adapters write when nobody filled the field in: not a brand or a model.
UNKNOWN = ('unknown', '待确认', 'n/a', 'none', 'null', '-')
LABELS = {'vendor': '品牌', 'model': '型号'}
REQUIRED = '设备没有上报品牌和型号，请填写这台机器人的品牌和型号'


def _now():
    return datetime.now(timezone.utc).isoformat()


def _plain(value):
    """One line of text without what stands for "not known", or ``''``."""
    if not isinstance(value, str):
        return ''
    text = ' '.join(value.split())
    return '' if text.casefold() in UNKNOWN else text


def _valid(text):
    return 0 < len(text) <= MAX and all(ord(c) >= 32 and ord(c) != 127 for c in text)


def reported(*manifests):
    """``{'vendor', 'model'}`` from the first manifest that names both, or None."""
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        vendor, model = _plain(manifest.get('vendor')), _plain(manifest.get('model'))
        if _valid(vendor) and _valid(model):
            return {'vendor': vendor, 'model': model}
    return None


def typed(p):
    """What a person typed: ``{'vendor', 'model'}``, None when neither was filled in; half of it is refused."""
    values = {name: _plain(p.get(name)) for name in LABELS}
    if not any(values.values()):
        return None
    for name, label in LABELS.items():
        if not values[name]:
            raise CodedError(f'请填写{label}（品牌和型号要一起填）', 'robot_model_required', field=name)
        if not _valid(values[name]):
            raise CodedError(f'{label}最多 {MAX} 个字符，不能有控制字符', 'robot_model_invalid', field=name)
    return values


def of(record):
    """The product of a robot, an application or a skill: ``{'vendor', 'model'}`` or None."""
    product = record.get(FIELD) if isinstance(record, dict) else None
    if isinstance(product, dict) and _plain(product.get('vendor')) and _plain(product.get('model')):
        return {'vendor': product['vendor'], 'model': product['model']}
    return None


def key(product):
    """What two products are compared by (None for no product)."""
    return (product['vendor'].casefold(), product['model'].casefold()) if product else None


def wire(application):
    """The application as a device gets it. The device recomputes the digest over everything it is
    sent (runtime ``v1_validate``), so a product that was added to a saved application afterwards
    (:meth:`RobotModels.adopt`), and is therefore not part of its digest, is left out."""
    if not isinstance(application, dict) or FIELD not in application or 'digest' not in application:
        return application

    def digest(body):
        return hashlib.sha256(json.dumps(body, sort_keys=True, allow_nan=False).encode()).hexdigest()

    body = {k: v for k, v in application.items() if k != 'digest'}
    if digest(body) == application['digest']:
        return application
    without = {k: v for k, v in application.items() if k != FIELD}
    return without if digest({k: v for k, v in without.items() if k != 'digest'}) == application['digest'] else application


def label(product):
    return f"{product['vendor']} {product['model']}" if product else '未填写品牌型号'


class RobotModels:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store
        # Code that read a robot, waited for a device and saves its copy must not undo a product set meanwhile.
        self.store.owned['robots'] = (*self.store.owned.get('robots', ()), FIELD)
        try:
            self.migrate()
        except Exception as error:                 # the workspace opens as it was, and the log says so
            platform.audit('robot_model.migration_failed', {'error': str(error)[:500]})

    def _keep(self, kind, record):
        """Save a changed record where it is: the lists keep their order (``Store.put`` moves a record to the front)."""
        with self.store.atomic():
            self.store.db.execute('UPDATE objects SET payload=? WHERE kind=? AND id=?',
                                  (json.dumps(record, ensure_ascii=False, allow_nan=False), kind, record['id']))
            self.store.db.commit()
        return record

    # ------------------------------------------------------------------ robots
    def initial(self, p):
        """The fields a new robot record gets from ``p`` (``vendor`` / ``model``): the product, or nothing."""
        product = typed(p)
        if product is None:
            return {}
        source = DEVICE if p.get('product_source') == DEVICE else MANUAL
        return {FIELD: {**product, 'source': source, 'updated_at': _now()}}

    def register(self, p):
        """Add a robot for a person (route ``robots``): with the brand and model typed, or, when
        nothing was typed and an address was given, with what the device at that address reports."""
        product, source = typed(p), MANUAL
        if product is None:
            product, source = self._ask_device(p), DEVICE
        if product is None:
            raise CodedError(REQUIRED, 'robot_model_required', field='vendor')
        return self.platform.add_robot({**p, **product, 'product_source': source})

    def connect_ssh(self, p):
        """Add a robot over SSH (route ``engineering/ssh/connect``): a device that is only reached
        over SSH reports nothing, so a new robot needs the brand and model typed. Connecting an
        existing robot, or changing its connection, asks for nothing."""
        if not p.get('robot_id') and not p.get('edit_robot_id'):
            product = typed(p)
            if product is None:
                raise CodedError('请填写这台机器人的品牌和型号', 'robot_model_required', field='vendor')
            p = {**p, **product}
        return self.platform.engineering.connect(p)

    @staticmethod
    def _ask_device(p):
        if not p.get('endpoint'):
            return None
        endpoint = connector.endpoint_url(p['endpoint'])        # an address that is not one is said as that
        try:
            token = p.get('token', '')
            manifest = connector.request(endpoint, '/v1/manifest', token if isinstance(token, str) else '')
        except ValueError:
            return None                              # not reachable now: the person types the two values
        return reported(manifest)

    def note_device(self, robot):
        """After a connection check: what the device reports is the robot's product. Returns the robot as saved."""
        found = reported(robot.get('manifest_v2'), robot.get('manifest'))
        if found is None:
            return robot
        with self.store.atomic():
            saved = self.store.get('robots', robot['id'])
            before = saved.get(FIELD) if isinstance(saved.get(FIELD), dict) else None
            if before and before.get('source') == DEVICE and all(before.get(k) == found[k] for k in found):
                return {**robot, FIELD: before}
            product = {**found, 'source': DEVICE, 'updated_at': _now()}
            self.store.put('robots', {**saved, FIELD: product}, owner=True)
            self.adopt(saved['id'])
        if key(of(saved)) != key(found):
            self.platform.audit('robot.model_changed', {'robot_id': saved['id'], 'from': of(saved), 'to': found, 'source': DEVICE},
                                saved.get('project_id'))
        return {**robot, FIELD: product}

    def assign(self, p):
        """A person fills in (or corrects) the brand and model of a robot whose device reports none."""
        if not isinstance(p.get('robot_id'), str) or not isinstance(p.get('project_id'), str):
            raise ValueError('找不到这台机器人')
        product = typed(p)
        if product is None:
            raise CodedError('请填写这台机器人的品牌和型号', 'robot_model_required', field='vendor')
        with self.store.atomic():
            robot = self.platform.same_project('robots', p['robot_id'], p['project_id'])
            before = robot.get(FIELD) if isinstance(robot.get(FIELD), dict) else None
            if before and before.get('source') == DEVICE:
                raise CodedError('这台机器人的品牌和型号是设备自己上报的，不能手工修改', 'robot_model_from_device')
            changed = {**robot, FIELD: {**product, 'source': MANUAL, 'updated_at': _now()}}
            self.store.put('robots', changed, owner=True)
            self.adopt(robot['id'])
        if key(of(robot)) != key(product):
            self.platform.audit('robot.model_changed', {'robot_id': robot['id'], 'from': of(robot), 'to': product, 'source': MANUAL},
                                robot.get('project_id'))
        return self.platform.redact(changed)

    # ------------------------------------------------------------------ applications
    def fits(self, application, robot):
        """Whether ``robot`` may be used with ``application``."""
        product = of(application)
        if product is None:
            return robot['id'] == application.get('robot_id')
        return key(of(robot)) == key(product)

    def robots_for(self, application):
        """The robots this application can be used with, the one it was made with first, then oldest first."""
        home = (application.get('project_id'), robot_scope.HOME)      # the robots a task can use (Platform.same_project)
        fitting = [r for r in self.store.list('robots') if r.get('project_id') in home and self.fits(application, r)]
        fitting.sort(key=lambda r: (r['id'] != application.get('robot_id'), r.get('created_at', ''), r['id']))
        return fitting

    def target(self, application, robot_id=None):
        """The robot an operation on ``application`` is for: the named one when it is of the
        application's brand and model; without a name, the robot the application was made with, or,
        when that one is gone or has changed, another robot of the same brand and model."""
        if robot_id:
            robot = self.platform.same_project('robots', robot_id, application['project_id'])
            if not self.fits(application, robot):
                raise CodedError(f"这个应用属于「{label(of(application))}」，不能用在「{robot['name']}」"
                                 f"（{label(of(robot))}）上", 'app_other_model')
            return robot
        fitting = self.robots_for(application)
        if not fitting:
            raise CodedError(f"没有可以使用这个应用的机器人：它属于「{label(of(application))}」", 'app_no_robot')
        return fitting[0]

    def application_fields(self, robot, skills):
        """What a new application version gets: the product of its robot (nothing when the robot has
        none). Refuses a skill of another brand and model."""
        product = of(robot)
        for skill in skills:
            other = of(skill)
            if other and key(other) != key(product):
                raise CodedError(f"技能「{skill['name']}」属于「{label(other)}」，不能用在「{label(product)}」的应用里",
                                 'skill_other_model')
        return {FIELD: product} if product else {}

    def check_revision(self, source, p):
        """A new version stays with the brand and model of the application it changes."""
        if of(source) is None or not isinstance(p.get('robot_id'), str):
            return                                  # what is missing is said where the version is put together
        robot = self.platform.same_project('robots', p['robot_id'], source['project_id'])
        if key(of(source)) != key(of(robot)):
            raise CodedError(f"这个应用属于「{label(of(source))}」，新版本不能改到另一个品牌型号的机器人上；"
                             '请为那种机器人另建一个应用', 'app_other_model')

    def bind_skills(self, application):
        """The skills of a saved application that have no product yet take the application's."""
        product = of(application)
        if product is None:
            return
        with self.store.atomic():
            for skill_id in {step['skill_id'] for step in application.get('steps', []) if isinstance(step, dict)}:
                try:
                    skill = self.store.get('skills', skill_id)
                except ValueError:
                    continue
                if of(skill) is None:
                    self._keep('skills', {**skill, FIELD: product})

    # ------------------------------------------------------------------ skills
    def skill_fields(self, p):
        """What a newly registered skill gets: the product typed for it, or that of the robot it was
        read from (``robot_id``); nothing when neither says one."""
        product = typed(p)
        if product is None and p.get('robot_id'):
            product = of(self.platform.same_project('robots', p['robot_id'], p['project_id']))
        return {FIELD: product} if product else {}

    # ------------------------------------------------------------------ records from before
    def adopt(self, robot_id):
        """Applications made for this robot while it had no product take the one it has now."""
        with self.store.atomic():
            try:
                product = of(self.store.get('robots', robot_id))
            except ValueError:
                return
            if product is None:
                return
            for application in self.store.list('applications'):
                if application.get('robot_id') == robot_id and of(application) is None:
                    if not self._skills_allow(application, product):
                        continue                    # it stays with this one robot: a skill of it is another brand's
                    application = self._keep('applications', {**application, FIELD: product})   # the digest names what was tested: unchanged
                    self.bind_skills(application)

    def _skills_allow(self, application, product):
        """Whether every skill of ``application`` is of ``product`` or of none yet."""
        for step in application.get('steps', []):
            try:
                other = of(self.store.get('skills', step.get('skill_id'))) if isinstance(step, dict) else None
            except ValueError:
                other = None
            if other and key(other) != key(product):
                return False
        return True

    def migrate(self):
        """Once per opening: robots whose saved manifest names a brand and model get them; then
        applications and skills take the product of their robot (see the module text)."""
        with self.store.atomic():
            for robot in self.store.list('robots'):
                found = None if of(robot) else reported(robot.get('manifest_v2'), robot.get('manifest'))
                if found:
                    self._keep('robots', {**robot, FIELD: {**found, 'source': DEVICE, 'updated_at': _now()}})
                if found or of(robot):
                    self.adopt(robot['id'])
            for application in self.store.list('applications'):
                self.bind_skills(application)
