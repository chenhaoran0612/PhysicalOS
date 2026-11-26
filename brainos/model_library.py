"""The library of a brand and model of robot (issue #51, docs/MODEL_LIBRARY.md): the console's side.

Issue #34 kept every material for the one device it was uploaded for. Since issue #51 a material
belongs to a brand and model (the robot's ``product``, issue #48): robots of the same brand and model
share it, and the robot model (the URDF, kind ``model``) is one of those materials, kept apart.

* **Whose a material is** (:func:`owned`): a robot's, when the material's ``vendor`` and ``model`` are the
  robot's brand and model (compared without regard to case). A material that is still bound to one
  device (stored before this issue and not moved yet) is that device's only.
* **Old materials** (:meth:`ModelLibrary.sync`): the cloud does not know which robot is of which brand
  and model, so the console tells it, once per robot and brand-and-model: ``POST
  /library/materials/rebind``. It is tried again whenever the library is opened or a repair reads it,
  until it has worked. A robot without a brand and model keeps its materials for itself until it has one.
* **A wrong label** (:meth:`ModelLibrary.move`): one material can be given another brand and model.

Only a robot's programs and scene data (kind ``bundle``, issue #43) still belong to one device.
"""
from . import robot_model, robot_scope
from .cloud_account import CloudError
from .errors import CodedError
from .material_library import MATERIAL_KEYS, _material_id, _scalars

MODEL = 'model'
BUNDLE = 'bundle'
SETTING = 'library_rebind'              # settings record: {robot id: the brand-and-model key it was moved to}
REBIND_KEYS = ('moved', 'merged')
MAX_PRODUCTS = 200


def _fold(value):
    return ' '.join(value.split()).casefold() if isinstance(value, str) else ''


def product_key(product):
    """What two brands-and-models are compared by; ``''`` for none."""
    return f"{_fold(product.get('vendor'))}\n{_fold(product.get('model'))}" if product else ''


def of_material(material):
    """The brand and model a material is labelled with, or ``None`` when a label is missing."""
    if isinstance(material, dict) and _fold(material.get('vendor')) and _fold(material.get('model')):
        return {'vendor': material['vendor'], 'model': material['model']}
    return None


def owned(material, robot):
    """Whether ``material`` is one ``robot`` uses (see the module's docstring)."""
    if not isinstance(material, dict) or material.get('kind') == BUNDLE:
        return False
    device = material.get('device_id')
    if device not in (None, ''):
        return device == robot['id']
    product = robot_model.of(robot)
    return bool(product) and product_key(of_material(material)) == product_key(product)


def listing(library, robot, filters=None, limit=50):
    """The materials the cloud lists for ``robot``: those of its brand and model, and those still bound
    to it alone. The cloud's filters are not trusted: every row is checked with :func:`owned`."""
    filters = {k: v for k, v in (filters or {}).items() if v is not None}
    product = robot_model.of(robot)
    rows, seen, full = [], set(), False
    for query in ([{'vendor': product['vendor'], 'model': product['model']}] if product else []) + [{'device_id': robot['id']}]:
        answer = library.list({**filters, **query, 'limit': limit})
        found = [m for m in answer.get('materials') or [] if isinstance(m, dict)]
        full = full or len(found) >= limit          # the cloud may have more than this page
        for material in found:
            if material.get('id') not in seen and owned(material, robot):
                seen.add(material.get('id'))
                rows.append(material)
    # ``total`` counts what is this robot's; one more when a page was full, so a caller sees that it has not seen all.
    return {'materials': rows, 'total': len(rows) + (1 if full else 0)}


def check_delivery(material, robot):
    """Refuse a driver package that is not this robot's (``package_delivery.check``)."""
    device = material.get('device_id')
    if device not in (None, ''):
        if device != robot['id']:
            other = material.get('device_name') if isinstance(material.get('device_name'), str) and material['device_name'] else '另一台设备'
            raise CodedError('这份安装包属于“%s”，不能送到这台机器人；请在这台机器人的资料里上传它' % other, code='package_other_device')
        return
    product = robot_model.of(robot)
    if product is None:
        raise CodedError('这台机器人还没有品牌型号，资料库里的安装包按品牌型号归属，不知道哪些是它的；请先给它填写品牌和型号',
                         code='robot_model_missing')
    if product_key(of_material(material)) != product_key(product):
        mine = robot_model.label(of_material(material)) if of_material(material) else '没有标明品牌型号'
        raise CodedError(f'这份安装包属于「{mine}」，这台机器人是「{robot_model.label(product)}」，不能送到它上面', code='package_other_model')


class ModelLibrary:
    def __init__(self, platform):
        self.platform = platform
        self.store = platform.store

    # ------------------------------------------------------------ old materials move to their brand and model
    def _done(self):
        try:
            record = self.store.get('settings', SETTING)
        except ValueError:
            return {}
        return record.get('robots') if isinstance(record.get('robots'), dict) else {}

    def sync(self, robots=None):
        """Tell the cloud the brand and model of every robot whose materials were not moved yet. Never
        raises: what could not be done now is tried the next time. Returns what was moved."""
        if not self.platform.cloud.summary().get('logged_in'):
            return []
        done, results = dict(self._done()), []
        robots = self.store.list('robots') if robots is None else robots
        for robot in robots:
            product = robot_model.of(robot)
            key = product_key(product)
            if not key or robot.get('sim') or done.get(robot['id']) == key:
                continue
            try:
                answer = self.platform.library._call('POST', '/library/materials/rebind',
                                                     {'device_id': robot['id'], 'vendor': product['vendor'], 'model': product['model']})
            except (CloudError, ValueError):
                continue                             # not allowed (a viewer), not reachable: tried again later
            if not isinstance(answer, dict):
                continue
            if answer.get('more') is not True:       # the cloud moved all there was; otherwise the next time goes on
                done[robot['id']] = key
            moved = {k: answer.get(k) for k in REBIND_KEYS if isinstance(answer.get(k), int)}
            kept = [k for k in answer.get('kept', []) if isinstance(k, dict)] if isinstance(answer.get('kept'), list) else []
            if moved.get('moved') or moved.get('merged') or kept:
                self.platform.audit('library.rebound', {'robot_id': robot['id'], **product, **moved, 'kept': len(kept)},
                                    robot.get('project_id'))
                results.append({'robot_id': robot['id'], **moved, 'kept': len(kept)})
        if done != self._done():
            self.store.put('settings', {'id': SETTING, 'robots': done})
        return results

    # ------------------------------------------------------------ what the pages ask
    def products(self):
        """The brands and models of this account's robots (simulated ones aside), each with its body type."""
        found = {}
        for robot in sorted(self.store.list('robots'), key=lambda r: (r.get('created_at', ''), r['id'])):
            product = robot_model.of(robot)
            if product is None or robot.get('sim'):
                continue
            entry = found.setdefault(product_key(product), {**product, 'family': robot.get('family'), 'robots': []})
            entry['robots'].append(robot['name'])
        return list(found.values())[:MAX_PRODUCTS]

    def overview(self, p=None):
        """``library/overview`` plus the brands and models to choose from; old materials are moved first."""
        moved = self.sync()
        view = self.platform.library.overview(p)
        return {**view, 'products': self.products(), 'rebound': moved}

    def robot(self, p):
        """The materials of one robot: its brand and model's, and what is still bound to it alone."""
        robot = self.platform.same_project('robots', p.get('robot_id'), p.get('project_id'))
        product = robot_model.of(robot)
        self.sync([robot])
        rows = listing(self.platform.library, robot, limit=200)
        return {'robot_id': robot['id'], 'product': product, 'family': robot.get('family'),
                'materials': rows['materials'], 'total': rows['total'],
                'shared_with': [r['name'] for r in self.store.list('robots')
                                if product and r['id'] != robot['id'] and not r.get('sim')
                                and product_key(robot_model.of(r)) == product_key(product)
                                and r.get('project_id') in (robot.get('project_id'), robot_scope.HOME)][:20]}

    def move(self, p):
        """Give one material another brand and model (``library/move``)."""
        product = robot_model.typed(p)
        if product is None:
            raise CodedError('请填写品牌和型号', 'robot_model_required')
        path = '/library/materials/%s/product' % _material_id(p.get('material_id'))
        material = _scalars(self.platform.library._call('PUT', path, {'vendor': product['vendor'], 'model': product['model']}),
                            MATERIAL_KEYS)
        self.platform.audit('library.moved', {'id': material.get('id'), **product})
        return material
