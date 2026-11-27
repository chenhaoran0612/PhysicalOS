"""Which brand and model a bundled skill package is for (issue #69).

A bundled package (``runtime/examples/skills/<name>``) is for the brands and models it names, or for
every robot (``generic``: packages that only read state). 「安装技能包」 lists what the robot's brand and
model can install plus the generic ones, and a package of another brand and model is refused before
anything is read from or sent to the robot. Only brand and model count: a simulated robot and a real
one of the same brand and model are the same product (:func:`robot_model.key`).

The table is ``products.json`` next to the packages. It is not inside a package on purpose: a package's
digest covers its content, and a release published from the same content must keep its digest.

Not covered: a package installed from a folder the person chose (``path``); it has no brand and model
and is still checked by capability only, as before.
"""
import json

from . import robot_model, runtime_bridge
from .errors import CodedError

INDEX = 'products.json'
SCHEMA = 'skill.products.v1'
INDEX_MAX_BYTES = 256 * 1024
NOBODY = {'generic': False, 'products': []}


def _entry(raw):
    """One package's entry as it is used, or None when it names nobody (or both everybody and somebody)."""
    if not isinstance(raw, dict):
        return None
    generic = raw.get('generic') is True
    listed = raw.get('products') if isinstance(raw.get('products'), list) else []
    products = [{**product, 'alias': item.get('alias') is True}
                for item, product in ((item, robot_model.of({robot_model.FIELD: item})) for item in listed) if product]
    if generic == bool(products):
        return None
    return {'generic': generic, 'products': products}


def _named(entry):
    """The brands and models to name: an ``alias`` is another spelling of one already listed (what the
    device itself reports) and is not named, unless there is nothing else."""
    plain = [p for p in entry['products'] if not p.get('alias')]
    return [robot_model.label(p) for p in (plain or entry['products'])]


def table(root=None):
    """``{package folder: {'generic', 'products'}}``. A missing or unreadable table is an empty one:
    then no bundled package is offered to anybody."""
    root = runtime_bridge.examples_dir() if root is None else root
    if not root:
        return {}
    try:
        with open(root / INDEX, 'rb') as handle:
            data = json.loads(handle.read(INDEX_MAX_BYTES + 1)[:INDEX_MAX_BYTES].decode('utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get('schema') != SCHEMA or not isinstance(data.get('packages'), dict):
        return {}
    entries = {str(name): _entry(raw) for name, raw in data['packages'].items()}
    return {name: entry for name, entry in entries.items() if entry}


def scope(example, entries=None):
    """Who the package is for. A package the table does not name is for nobody."""
    entries = table() if entries is None else entries
    return entries.get(example, NOBODY)


def offered(example, product, entries=None):
    """Whether a robot of this brand and model (None: not filled in) is offered the package."""
    entry = scope(example, entries)
    if entry['generic']:
        return True
    mine = robot_model.key(product)
    return mine is not None and any(robot_model.key(p) == mine for p in entry['products'])


def view(example, entries=None):
    """The entry for the page: generic or the names of its brands and models."""
    entry = scope(example, entries)
    return {'generic': entry['generic'], 'products': _named(entry)}


def check(robot, example):
    """Refuse a bundled package that is not for this robot's brand and model."""
    entry = scope(example)
    product = robot_model.of(robot)
    if offered(example, product, {example: entry}):
        return
    if product is None and entry['products']:
        raise CodedError('「%s」还没有填写品牌和型号；请先在「我的机器人」里填写' % robot['name'], 'robot_model_missing')
    owners = '、'.join(_named(entry)) or '别的品牌型号'
    raise CodedError('这个技能包属于「%s」，不能装到「%s」（%s）上' % (owners, robot['name'], robot_model.label(product)),
                     'skill_other_model')
